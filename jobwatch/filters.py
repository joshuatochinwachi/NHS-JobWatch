"""Filter configuration loader, schema validator, and rule evaluation engine."""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Pattern

from jobwatch.models import FilterDecision, VacancyCard


class FilterConfigError(Exception):
    """Raised when filter configuration JSON is invalid or fails schema validation."""
    pass


def extract_band_number(band_str: Optional[str]) -> Optional[int]:
    """Extracts integer band number from a band string like 'Band 5', 'Band 8A', 'Band 2'."""
    if not band_str:
        return None
    match = re.search(r"Band\s*(\d+)", band_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


@dataclass
class ExclusionRule:
    id: str
    target: str
    reason: str
    patterns: List[Pattern] = field(default_factory=list)
    requires_role: Optional[str] = None
    role: Optional[str] = None
    max_band_exclusive: Optional[int] = None
    min_band_inclusive: Optional[int] = None


@dataclass
class InclusionRule:
    id: str
    role: str
    target: str
    reason: str
    patterns: List[Pattern] = field(default_factory=list)
    allowed_bands: List[str] = field(default_factory=list)


@dataclass
class FilterConfig:
    version: str
    rules_status: str
    closing_soon_days: int
    default_outcome: str
    exclusions: List[ExclusionRule]
    strong_rules: List[InclusionRule]
    maybe_rules: List[InclusionRule]


def load_filter_config(path: Optional[str] = None) -> FilterConfig:
    """Loads and strictly validates filter configuration from JSON file."""
    if path is None:
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_dir, "config", "filters.json")

    if not os.path.exists(path):
        raise FilterConfigError(f"Filter configuration file not found at '{path}'")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise FilterConfigError(f"Failed to read or parse JSON from '{path}': {e}") from e

    # Top-level validation
    allowed_top_keys = {
        "version",
        "rules_status",
        "closing_soon_days",
        "default_outcome",
        "exclusions",
        "strong_rules",
        "maybe_rules",
    }
    unknown_keys = set(data.keys()) - allowed_top_keys
    if unknown_keys:
        raise FilterConfigError(f"Unknown top-level keys in filter config: {sorted(unknown_keys)}")

    for req in allowed_top_keys:
        if req not in data:
            raise FilterConfigError(f"Missing required key in filter config: '{req}'")

    if data["default_outcome"] not in ("strong", "maybe", "excluded"):
        raise FilterConfigError(f"Invalid default_outcome '{data['default_outcome']}'")

    # Parse exclusions
    exclusions: List[ExclusionRule] = []
    for idx, rule in enumerate(data.get("exclusions", [])):
        if "id" not in rule or "reason" not in rule:
            raise FilterConfigError(f"Exclusion rule #{idx} missing required 'id' or 'reason'")

        compiled_patterns = []
        for pat in rule.get("patterns", []):
            try:
                compiled_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                raise FilterConfigError(f"Invalid regex '{pat}' in exclusion '{rule['id']}': {e}") from e

        exclusions.append(
            ExclusionRule(
                id=rule["id"],
                target=rule.get("target", "title"),
                reason=rule["reason"],
                patterns=compiled_patterns,
                requires_role=rule.get("requires_role"),
                role=rule.get("role"),
                max_band_exclusive=rule.get("max_band_exclusive"),
                min_band_inclusive=rule.get("min_band_inclusive"),
            )
        )

    # Parse strong rules
    strong_rules: List[InclusionRule] = []
    for idx, rule in enumerate(data.get("strong_rules", [])):
        if "id" not in rule or "reason" not in rule:
            raise FilterConfigError(f"Strong rule #{idx} missing required 'id' or 'reason'")

        compiled_patterns = []
        for pat in rule.get("patterns", []):
            try:
                compiled_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                raise FilterConfigError(f"Invalid regex '{pat}' in strong rule '{rule['id']}': {e}") from e

        strong_rules.append(
            InclusionRule(
                id=rule["id"],
                role=rule.get("role", "nurse"),
                target=rule.get("target", "title"),
                reason=rule["reason"],
                patterns=compiled_patterns,
                allowed_bands=rule.get("allowed_bands", []),
            )
        )

    # Parse maybe rules
    maybe_rules: List[InclusionRule] = []
    for idx, rule in enumerate(data.get("maybe_rules", [])):
        if "id" not in rule or "reason" not in rule:
            raise FilterConfigError(f"Maybe rule #{idx} missing required 'id' or 'reason'")

        compiled_patterns = []
        for pat in rule.get("patterns", []):
            try:
                compiled_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                raise FilterConfigError(f"Invalid regex '{pat}' in maybe rule '{rule['id']}': {e}") from e

        maybe_rules.append(
            InclusionRule(
                id=rule["id"],
                role=rule.get("role", "nurse"),
                target=rule.get("target", "title"),
                reason=rule["reason"],
                patterns=compiled_patterns,
                allowed_bands=rule.get("allowed_bands", []),
            )
        )

    return FilterConfig(
        version=str(data["version"]),
        rules_status=str(data["rules_status"]),
        closing_soon_days=int(data["closing_soon_days"]),
        default_outcome=str(data["default_outcome"]),
        exclusions=exclusions,
        strong_rules=strong_rules,
        maybe_rules=maybe_rules,
    )


def evaluate_card(
    card: VacancyCard,
    config: FilterConfig,
    now_utc_iso: Optional[str] = None,
) -> FilterDecision:
    """Evaluates a single VacancyCard against filter rules, returning a FilterDecision."""
    decided_at = now_utc_iso or datetime.now(timezone.utc).isoformat()
    title = card.title or ""
    dept = card.dept_label or ""
    band_str = card.band or ""

    # 1. Identify roles
    is_nurse = bool(re.search(r"\bnurse\b", title, re.IGNORECASE))
    is_support = bool(
        re.search(
            r"\b(healthcare support worker|health care assistant|care assistant|senior support worker|hcsw|clinical support worker)\b",
            title,
            re.IGNORECASE,
        )
    )

    # 2. Extract numeric band
    band_num = extract_band_number(band_str)
    band_unknown = (band_num is None)
    band_repr = band_str if not band_unknown else "Band unknown"

    # 3. Evaluate exclusions FIRST
    for rule in config.exclusions:
        # Check role constraint if any
        if rule.requires_role == "nurse" and not is_nurse:
            continue

        # Band-based exclusions
        if rule.role == "nurse" and is_nurse and not band_unknown:
            if rule.max_band_exclusive is not None and band_num < rule.max_band_exclusive:
                return FilterDecision(
                    job_id=card.job_id,
                    filter_version=config.version,
                    outcome="excluded",
                    matched_rule=rule.id,
                    match_reason=f"{rule.reason} ({band_str})",
                    decided_at=decided_at,
                )
            if rule.min_band_inclusive is not None and band_num >= rule.min_band_inclusive:
                return FilterDecision(
                    job_id=card.job_id,
                    filter_version=config.version,
                    outcome="excluded",
                    matched_rule=rule.id,
                    match_reason=f"{rule.reason} ({band_str})",
                    decided_at=decided_at,
                )

        if rule.role == "support" and is_support and not band_unknown:
            if rule.max_band_exclusive is not None and band_num < rule.max_band_exclusive:
                return FilterDecision(
                    job_id=card.job_id,
                    filter_version=config.version,
                    outcome="excluded",
                    matched_rule=rule.id,
                    match_reason=f"{rule.reason} ({band_str})",
                    decided_at=decided_at,
                )

        # Pattern-based exclusions
        for pat in rule.patterns:
            if pat.search(title):
                return FilterDecision(
                    job_id=card.job_id,
                    filter_version=config.version,
                    outcome="excluded",
                    matched_rule=rule.id,
                    match_reason=rule.reason,
                    decided_at=decided_at,
                )

    # 4. Evaluate strong inclusion rules
    for rule in config.strong_rules:
        for pat in rule.patterns:
            if pat.search(title):
                # Check band eligibility
                if not rule.allowed_bands or band_repr in rule.allowed_bands:
                    reason = rule.reason
                    if band_unknown:
                        reason += " (band unknown)"
                    return FilterDecision(
                        job_id=card.job_id,
                        filter_version=config.version,
                        outcome="strong",
                        matched_rule=rule.id,
                        match_reason=reason,
                        decided_at=decided_at,
                    )

    # 5. Evaluate maybe inclusion rules
    for rule in config.maybe_rules:
        for pat in rule.patterns:
            if pat.search(title):
                # Check band eligibility
                if not rule.allowed_bands or band_repr in rule.allowed_bands:
                    reason = rule.reason
                    if band_unknown:
                        reason += " (band unknown)"
                    return FilterDecision(
                        job_id=card.job_id,
                        filter_version=config.version,
                        outcome="maybe",
                        matched_rule=rule.id,
                        match_reason=reason,
                        decided_at=decided_at,
                    )

    # 6. Default outcome fallthrough
    return FilterDecision(
        job_id=card.job_id,
        filter_version=config.version,
        outcome=config.default_outcome,
        matched_rule="default_unmatched",
        match_reason="Did not match any strong or maybe inclusion rules",
        decided_at=decided_at,
    )

