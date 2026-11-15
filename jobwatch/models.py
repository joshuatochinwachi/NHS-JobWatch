"""Data models and custom exceptions for NHS-JobWatch."""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List


class JobWatchError(Exception):
    """Base exception for all NHS-JobWatch errors."""
    pass


class JobWatchParseError(JobWatchError):
    """Base exception for parsing failures."""
    pass


class PageParseError(JobWatchParseError):
    """Raised when page-level validation fails (e.g. card count mismatch)."""
    pass


@dataclass
class CardParseError(JobWatchParseError):
    """Records a failure to parse an individual vacancy card."""
    card_index: int
    field_name: str
    reason: str
    snippet: str = ""

    def __str__(self) -> str:
        return f"Card #{self.card_index} failed on '{self.field_name}': {self.reason}"


@dataclass
class VacancyCard:
    """Structured representation of a vacancy card parsed from listing HTML."""
    job_id: int
    title_raw: str
    title: str  # Collapsed whitespace (for matching and change detection)
    detail_url: str
    closing_date_raw: str
    closing_date: Optional[date] = None
    job_ref_raw: Optional[str] = None
    job_ref: Optional[str] = None  # Collapsed whitespace
    salary_raw: Optional[str] = None
    salary: Optional[str] = None  # Collapsed whitespace
    band: Optional[str] = None
    location_raw: Optional[str] = None
    location: Optional[str] = None  # Collapsed whitespace
    dept_label_raw: Optional[str] = None
    dept_label: Optional[str] = None  # Collapsed whitespace
    employer_raw: Optional[str] = None
    employer: Optional[str] = None  # Collapsed whitespace
    contract_raw: Optional[str] = None
    contract: Optional[str] = None  # Collapsed whitespace
    # Observed values in audit/testing include "various", "0", and "118"; do not convert to a number.
    hours_raw: Optional[str] = None


@dataclass
class ListingParseResult:
    """Contains good cards extracted, card parse errors encountered, and page metadata."""
    cards: List[VacancyCard] = field(default_factory=list)
    errors: List[CardParseError] = field(default_factory=list)
    total_match_records: int = 0
    total_current_records: int = 0


@dataclass
class FilterDecision:
    """Records the outcome and rationale of evaluating a vacancy against filter rules."""
    job_id: int
    filter_version: str
    outcome: str  # 'strong', 'maybe', 'excluded'
    matched_rule: str
    match_reason: str
    decided_at: str  # ISO 8601 UTC string


