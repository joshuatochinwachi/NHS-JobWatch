"""Sweep coordinator executing multi-page vacancy sweeps with deduplication and unlisting."""

import glob
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from jobwatch.models import VacancyCard
from jobwatch.parser import ListingParser
from jobwatch.storage import (
    DatabaseStateCorruptError,
    Storage,
    UnbaselinedScopeError,
)

# Signature for pluggable page fetching function: (scope_key, skip) -> html_markup
PageSource = Callable[[str, int], str]

# 10 Tracked fields for change detection and content hashing
TRACKED_FIELDS = [
    "job_ref",
    "title",
    "closing_date",
    "salary",
    "band",
    "contract",
    "hours_raw",
    "location",
    "dept_label",
    "employer",
]


def compute_content_hash(card: VacancyCard) -> str:
    """Computes a SHA-256 hash over the 10 tracked fields using canonical JSON."""
    closing_str = card.closing_date.isoformat() if card.closing_date else (card.closing_date_raw or "")
    payload = {
        "job_ref": card.job_ref or "",
        "title": card.title or "",
        "closing_date": closing_str,
        "salary": card.salary or "",
        "band": card.band or "",
        "contract": card.contract or "",
        "hours_raw": card.hours_raw or "",
        "location": card.location or "",
        "dept_label": card.dept_label or "",
        "employer": card.employer or "",
    }
    canonical_json = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_tracked_values(card: VacancyCard) -> Dict[str, Optional[str]]:
    """Extracts a dictionary of the 10 tracked field values from a VacancyCard."""
    closing_str = card.closing_date.isoformat() if card.closing_date else None
    return {
        "job_ref": card.job_ref,
        "title": card.title,
        "closing_date": closing_str,
        "salary": card.salary,
        "band": card.band,
        "contract": card.contract,
        "hours_raw": card.hours_raw,
        "location": card.location,
        "dept_label": card.dept_label,
        "employer": card.employer,
    }


@dataclass
class SweepResult:
    """Summary of a completed, drifted, or failed vacancy sweep."""
    run_id: int
    scope_key: str
    run_type: str
    outcome: str  # 'success', 'drifted', 'partial_failure', 'failure'
    is_complete: bool
    is_baseline: bool
    pages_fetched: int
    vacancies_seen: int
    new_count: int
    changed_count: int
    unlisted_count: int
    parse_errors_count: int
    rerun_needed: bool = False
    error_message: Optional[str] = None
    shallow_misses: List[Dict[str, Any]] = field(default_factory=list)


class SweepCoordinator:
    """Coordinates page traversal, in-sweep deduplication, drift checking, and unlisting."""

    def __init__(
        self,
        storage: Storage,
        page_source: PageSource,
        raw_retention_dir: Optional[str] = None,
        max_absolute_pages: int = 100,
    ):
        self.storage = storage
        self.page_source = page_source
        self.raw_retention_dir = raw_retention_dir
        self.max_absolute_pages = max_absolute_pages

    def prune_raw_html_snapshots(self) -> Tuple[int, int]:
        """Prunes raw HTML snapshots retaining last 10 complete and 5 failed/drifted runs."""
        if not self.raw_retention_dir or not os.path.exists(self.raw_retention_dir):
            return (0, 0)

        # Query recent complete vs failed run IDs
        cur_succ = self.storage.conn.execute(
            "SELECT id FROM source_runs WHERE outcome = 'success' ORDER BY id DESC LIMIT 10;"
        )
        kept_success_ids = {row["id"] for row in cur_succ.fetchall()}

        cur_fail = self.storage.conn.execute(
            "SELECT id FROM source_runs WHERE outcome != 'success' ORDER BY id DESC LIMIT 5;"
        )
        kept_failed_ids = {row["id"] for row in cur_fail.fetchall()}

        kept_all_ids = kept_success_ids | kept_failed_ids

        all_files = glob.glob(os.path.join(self.raw_retention_dir, "run_*_page_*.html"))
        pruned_count = 0
        kept_count = 0

        for fpath in all_files:
            fname = os.path.basename(fpath)
            # Expecting filename format: run_<id>_page_<page_no>.html
            parts = fname.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                rid = int(parts[1])
                if rid not in kept_all_ids:
                    try:
                        os.remove(fpath)
                        pruned_count += 1
                    except OSError:
                        pass
                else:
                    kept_count += 1

        return (kept_count, pruned_count)

    def execute_sweep(
        self,
        scope_key: str,
        run_type: str = "full_sweep",
        shallow_max_pages: int = 2,
    ) -> SweepResult:
        """Executes a vacancy sweep for scope_key using the injected page source."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # 1. Scope metadata and baseline validation
        scope_meta = self.storage.ensure_scope_metadata(scope_key)
        is_baselined = bool(scope_meta["is_baselined"])

        # Shallow runs are refused on unbaselined scopes
        if run_type == "shallow" and not is_baselined:
            raise UnbaselinedScopeError(
                f"Cannot run shallow sweep: scope '{scope_key}' has not been baselined yet."
            )

        # Baseline mode applies only to unbaselined full sweeps
        is_baseline_sweep = (run_type == "full_sweep" and not is_baselined)

        # Integrity check: if vacancies table has 0 records but scope is marked baselined, stop immediately
        total_vacancies_in_db = self.storage.get_vacancy_count()
        if total_vacancies_in_db == 0 and is_baselined:
            raise DatabaseStateCorruptError(
                f"Integrity check failed: database contains 0 vacancies but scope '{scope_key}' "
                f"is marked as baselined (baseline_run_id={scope_meta['baseline_run_id']})."
            )

        # 2. Initiate run record with single-run concurrency guard
        run_id = self.storage.start_run(
            scope_key=scope_key,
            run_type=run_type,
            started_at_iso=now_iso,
            is_baseline=is_baseline_sweep,
        )

        seen_job_ids_in_sweep: Set[int] = set()
        shallow_misses: List[Dict[str, Any]] = []
        pages_fetched = 0
        vacancies_seen = 0
        new_count = 0
        changed_count = 0
        parse_errors_count = 0

        skip = 0
        page_no = 1
        expected_total: Optional[int] = None
        dynamic_max_pages: int = self.max_absolute_pages
        reached_terminal_stop = False

        try:
            while True:
                # Enforce shallow run page limit
                if run_type == "shallow" and page_no > shallow_max_pages:
                    break

                # Fetch page HTML via injected page source
                markup = self.page_source(scope_key, skip)
                pages_fetched += 1

                # Save raw HTML snapshot if retention directory configured
                if self.raw_retention_dir:
                    os.makedirs(self.raw_retention_dir, exist_ok=True)
                    snap_path = os.path.join(self.raw_retention_dir, f"run_{run_id}_page_{page_no}.html")
                    with open(snap_path, "w", encoding="utf-8") as f:
                        f.write(markup)

                # Parse page
                parser = ListingParser(markup)
                parse_result = parser.parse()
                parse_errors_count += len(parse_result.errors)

                # Metadata validation from Page 1
                if expected_total is None:
                    expected_total = parse_result.total_match_records
                    # Derive dynamic page cap: ceil(total / 12) + 3, up to max_absolute_pages
                    # Note: a skip equal to totalMatchRecords was never observed in live audits; handling is INFERRED.
                    calculated_cap = math.ceil(expected_total / 12) + 3 if expected_total > 0 else 1
                    dynamic_max_pages = min(calculated_cap, self.max_absolute_pages)

                actual_cards_on_page = len(parse_result.cards)

                # Drift check: compare totalMatchRecords only on pages that contain cards (actual_cards_on_page > 0)
                # Terminal empty pages return totalMatchRecords = 0, which must NOT cause drift
                if page_no > 1 and actual_cards_on_page > 0:
                    if parse_result.total_match_records != expected_total:
                        # Drift detected on card-bearing page!
                        finished_iso = datetime.now(timezone.utc).isoformat()
                        self.storage.close_run_as_drifted(
                            run_id=run_id,
                            finished_at_iso=finished_iso,
                            pages_fetched=pages_fetched,
                            vacancies_seen=vacancies_seen,
                            new_count=new_count,
                            changed_count=changed_count,
                            parse_errors_count=parse_errors_count,
                        )
                        self.prune_raw_html_snapshots()
                        return SweepResult(
                            run_id=run_id,
                            scope_key=scope_key,
                            run_type=run_type,
                            outcome="drifted",
                            is_complete=False,
                            is_baseline=is_baseline_sweep,
                            pages_fetched=pages_fetched,
                            vacancies_seen=vacancies_seen,
                            new_count=new_count,
                            changed_count=changed_count,
                            unlisted_count=0,
                            parse_errors_count=parse_errors_count,
                            rerun_needed=True,
                            error_message="totalMatchRecords changed mid-sweep on card-bearing page",
                        )

                # Stall check: if page returned cards but none are new to this sweep, abort as stalled
                if actual_cards_on_page > 0:
                    page_job_ids = {c.job_id for c in parse_result.cards}
                    if page_no > 1 and page_job_ids.issubset(seen_job_ids_in_sweep):
                        # Stall detected: page is repeating previous IDs
                        raise RuntimeError(f"Sweep stalled: page {page_no} returned zero new job IDs.")

                # Process card writes inside page transaction
                with self.storage.conn:
                    for card in parse_result.cards:
                        vacancies_seen += 1
                        card_hash = compute_content_hash(card)

                        # In-sweep deduplication: process each job_id only once per sweep
                        if card.job_id in seen_job_ids_in_sweep:
                            continue
                        seen_job_ids_in_sweep.add(card.job_id)

                        existing = self.storage.get_vacancy(card.job_id)

                        if existing is None:
                            # 1. Truly new job_id: check for possible repost
                            repost_match_id = self.storage.find_possible_repost(
                                job_ref=card.job_ref,
                                title=card.title,
                                employer=card.employer,
                                dept_label=card.dept_label,
                                location=card.location,
                            )
                            repost_flag = f"possible_repost (matches job_id {repost_match_id})" if repost_match_id else None

                            self.storage.upsert_vacancy_record(card, card_hash, now_iso, repost_flag=repost_flag)
                            self.storage.upsert_vacancy_scope(card.job_id, scope_key, now_iso)

                            if is_baseline_sweep:
                                # Baseline items get status 'baseline' with alerted_at set, suppressing alerts/reminders
                                self.storage.set_user_status(card.job_id, status="baseline", alerted_at=now_iso, now_iso=now_iso)
                            else:
                                self.storage.set_user_status(card.job_id, status="new", alerted_at=None, now_iso=now_iso)

                            new_count += 1
                            if not is_baseline_sweep and run_type == "full_sweep" and page_no > 2:
                                self.storage.record_shallow_miss(
                                    job_id=card.job_id,
                                    scope_key=scope_key,
                                    page_index=page_no,
                                    card_offset=skip,
                                    detected_at=now_iso,
                                )
                                shallow_misses.append({
                                    "job_id": card.job_id,
                                    "title": card.title,
                                    "scope_key": scope_key,
                                    "page_no": page_no,
                                })

                        else:
                            # 2. Known job_id: update last_seen_at and scope
                            self.storage.upsert_vacancy_scope(card.job_id, scope_key, now_iso)

                            old_hash = existing["content_hash"]
                            if old_hash != card_hash:
                                # Content changed! Compare the 10 tracked fields
                                old_fields = {
                                    "job_ref": existing["job_ref"],
                                    "title": existing["title"],
                                    "closing_date": existing["closing_date"],
                                    "salary": existing["salary"],
                                    "band": existing["band"],
                                    "contract": existing["contract"],
                                    "hours_raw": existing["hours_raw"],
                                    "location": existing["location"],
                                    "dept_label": existing["dept_label"],
                                    "employer": existing["employer"],
                                }
                                new_fields = extract_tracked_values(card)

                                for field_name in TRACKED_FIELDS:
                                    if old_fields[field_name] != new_fields[field_name]:
                                        self.storage.record_change(
                                            job_id=card.job_id,
                                            field_name=field_name,
                                            old_value=old_fields[field_name],
                                            new_value=new_fields[field_name],
                                            now_iso=now_iso,
                                            run_id=run_id,
                                        )

                                self.storage.upsert_vacancy_record(card, card_hash, now_iso)
                                changed_count += 1
                            else:
                                # Content unchanged: refresh last_seen_at and relist if unlisted
                                self.storage.upsert_vacancy_record(card, card_hash, now_iso)

                # Check stopping conditions:
                # A: Partial page (< 12 cards)
                # B: Terminal empty page (totalCurrentRecords == 0 or 0 cards)
                if actual_cards_on_page < 12 or parse_result.total_current_records == 0:
                    reached_terminal_stop = True
                    break

                # C: Hit dynamic page cap
                if page_no >= dynamic_max_pages:
                    break

                # Advance to next page
                skip += 12
                page_no += 1

            finished_iso = datetime.now(timezone.utc).isoformat()

            # 3. Finalize run based on run type and completion conditions
            if run_type == "shallow":
                # Shallow run: records success, but never marks scope complete, never unlists, never increments missing
                with self.storage.conn:
                    self.storage.conn.execute(
                        """
                        UPDATE source_runs
                        SET outcome = 'success',
                            finished_at = ?,
                            is_complete_sweep = 0,
                            pages_fetched = ?,
                            vacancies_seen = ?,
                            new_count = ?,
                            changed_count = ?,
                            unlisted_count = 0,
                            parse_errors_count = ?
                        WHERE id = ?;
                        """,
                        (finished_iso, pages_fetched, vacancies_seen, new_count, changed_count, parse_errors_count, run_id)
                    )
                self.prune_raw_html_snapshots()
                return SweepResult(
                    run_id=run_id,
                    scope_key=scope_key,
                    run_type=run_type,
                    outcome="success",
                    is_complete=False,
                    is_baseline=False,
                    pages_fetched=pages_fetched,
                    vacancies_seen=vacancies_seen,
                    new_count=new_count,
                    changed_count=changed_count,
                    unlisted_count=0,
                    parse_errors_count=parse_errors_count,
                )

            # Full sweep: verify all four completion conditions:
            # (a) every page parsed without error (parse_errors_count == 0)
            # (b) distinct job_ids seen equals expected total
            # (c) reached terminal stop (partial page or empty page)
            # (d) totalMatchRecords did not change during sweep
            is_complete = (
                parse_errors_count == 0
                and expected_total is not None
                and len(seen_job_ids_in_sweep) == expected_total
                and reached_terminal_stop
            )

            if is_complete:
                # Finalize sweep in a single atomic transaction
                unlisted_count = self.storage.finalize_sweep_transaction(
                    scope_key=scope_key,
                    run_id=run_id,
                    sweep_started_at_iso=now_iso,
                    finished_at_iso=finished_iso,
                    seen_job_ids_in_sweep=seen_job_ids_in_sweep,
                    pages_fetched=pages_fetched,
                    vacancies_seen=vacancies_seen,
                    new_count=new_count,
                    changed_count=changed_count,
                    parse_errors_count=parse_errors_count,
                    is_baseline=is_baseline_sweep,
                )
                self.prune_raw_html_snapshots()
                return SweepResult(
                    run_id=run_id,
                    scope_key=scope_key,
                    run_type=run_type,
                    outcome="success",
                    is_complete=True,
                    is_baseline=is_baseline_sweep,
                    pages_fetched=pages_fetched,
                    vacancies_seen=vacancies_seen,
                    new_count=new_count,
                    changed_count=changed_count,
                    unlisted_count=unlisted_count,
                    parse_errors_count=parse_errors_count,
                    shallow_misses=shallow_misses,
                )
            else:
                # Incomplete sweep: record partial_failure (does NOT unlist, does NOT mark baseline complete)
                with self.storage.conn:
                    self.storage.conn.execute(
                        """
                        UPDATE source_runs
                        SET outcome = 'partial_failure',
                            finished_at = ?,
                            is_complete_sweep = 0,
                            pages_fetched = ?,
                            vacancies_seen = ?,
                            new_count = ?,
                            changed_count = ?,
                            unlisted_count = 0,
                            parse_errors_count = ?,
                            error_message = 'Sweep ended before satisfying all completion conditions'
                        WHERE id = ?;
                        """,
                        (finished_iso, pages_fetched, vacancies_seen, new_count, changed_count, parse_errors_count, run_id)
                    )
                self.prune_raw_html_snapshots()
                return SweepResult(
                    run_id=run_id,
                    scope_key=scope_key,
                    run_type=run_type,
                    outcome="partial_failure",
                    is_complete=False,
                    is_baseline=is_baseline_sweep,
                    pages_fetched=pages_fetched,
                    vacancies_seen=vacancies_seen,
                    new_count=new_count,
                    changed_count=changed_count,
                    unlisted_count=0,
                    parse_errors_count=parse_errors_count,
                    error_message="Sweep ended before satisfying all completion conditions",
                    shallow_misses=shallow_misses,
                )

        except Exception as exc:
            finished_iso = datetime.now(timezone.utc).isoformat()
            self.storage.close_run_failure(
                run_id=run_id,
                finished_at_iso=finished_iso,
                error_message=str(exc),
                pages_fetched=pages_fetched,
                vacancies_seen=vacancies_seen,
                parse_errors_count=parse_errors_count,
            )
            self.prune_raw_html_snapshots()
            raise

