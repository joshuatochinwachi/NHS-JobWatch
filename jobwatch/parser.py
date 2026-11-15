"""HTML parser for NHS Scotland vacancy listing cards."""

import html
import re
from datetime import datetime
from typing import Optional, Set

from bs4 import BeautifulSoup, Tag

from jobwatch.models import (
    CardParseError,
    ListingParseResult,
    PageParseError,
    VacancyCard,
)


def collapse_whitespace(text: Optional[str]) -> Optional[str]:
    """Collapses runs of whitespace into a single space and trims ends."""
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip()


class ListingParser:
    """Parses vacancy listing partials returned by /Home/_JobCard."""

    def __init__(self, markup: str):
        self.raw_markup = markup
        self.soup = BeautifulSoup(markup, "html.parser")

    def parse(self) -> ListingParseResult:
        """Parses the listing HTML fragment into a structured ListingParseResult.

        Raises:
            PageParseError: if page metadata is corrupt or card count != totalCurrentRecords.
        """
        # 1. Page-level metadata validation
        total_curr_elem = self.soup.find("input", id="totalCurrentRecords")
        total_match_elem = self.soup.find("input", id="totalMatchRecords")

        if total_curr_elem is None:
            raise PageParseError("Hidden input 'totalCurrentRecords' not found on listing page.")
        if total_match_elem is None:
            raise PageParseError("Hidden input 'totalMatchRecords' not found on listing page.")

        try:
            total_current = int(total_curr_elem.get("value", ""))
        except (ValueError, TypeError) as err:
            raise PageParseError(f"Invalid integer value for 'totalCurrentRecords': {err}")

        try:
            total_match = int(total_match_elem.get("value", ""))
        except (ValueError, TypeError) as err:
            raise PageParseError(f"Invalid integer value for 'totalMatchRecords': {err}")

        # 2. Extract card containers
        card_elements = self.soup.select("div.card.job-card")
        actual_card_count = len(card_elements)

        if actual_card_count != total_current:
            raise PageParseError(
                f"Card count mismatch: DOM contains {actual_card_count} cards, "
                f"but totalCurrentRecords specifies {total_current}."
            )

        # 3. Parse individual cards
        result = ListingParseResult(
            total_match_records=total_match,
            total_current_records=total_current,
        )

        seen_job_ids: Set[int] = set()

        for idx, card_tag in enumerate(card_elements, 1):
            try:
                card = self._parse_card(card_tag, idx, seen_job_ids)
                result.cards.append(card)
            except CardParseError as parse_err:
                result.errors.append(parse_err)

        return result

    def _parse_card(self, card_tag: Tag, index: int, seen_job_ids: Set[int]) -> VacancyCard:
        """Parses a single vacancy card element into a VacancyCard instance.

        Raises:
            CardParseError: on missing mandatory fields, invalid dates, or duplicate IDs.
        """
        card_snippet = str(card_tag)[:300]

        # 1. Job ID (Mandatory)
        fav_elem = card_tag.find("div", class_="job-card__favourite")
        if not fav_elem:
            raise CardParseError(index, "job_id", "Missing div.job-card__favourite element", card_snippet)

        job_id_raw = fav_elem.get("data-jobid") or fav_elem.get("data-jobId")
        if not job_id_raw:
            raise CardParseError(index, "job_id", "Missing data-jobId attribute", card_snippet)

        try:
            job_id = int(job_id_raw)
        except ValueError:
            raise CardParseError(index, "job_id", f"Non-integer data-jobId: '{job_id_raw}'", card_snippet)

        # Check for duplicates on single page
        if job_id in seen_job_ids:
            raise CardParseError(index, "job_id", f"Duplicate job_id {job_id} encountered on page", card_snippet)
        seen_job_ids.add(job_id)

        # 2. Title & Detail URL (Mandatory)
        title_elem = card_tag.find("a", href=lambda h: h and h.startswith("/Job/JobDetail?JobId="))
        if not title_elem:
            raise CardParseError(index, "title", "Missing vacancy title anchor (/Job/JobDetail?JobId=)", card_snippet)

        # Preserve verbatim text while stripping outer boundary whitespace
        title_raw = html.unescape(title_elem.get_text()).strip()
        if not title_raw:
            raise CardParseError(index, "title", "Empty vacancy title text", card_snippet)
        title = collapse_whitespace(title_raw)

        detail_url = title_elem.get("href", "").strip()
        if not detail_url:
            raise CardParseError(index, "detail_url", "Empty href attribute on title anchor", card_snippet)

        # 3. Closing Date (Mandatory)
        closing_raw = self._extract_field_text(card_tag, ["closingdate"])
        if not closing_raw:
            raise CardParseError(index, "closing_date", "Missing closing date field", card_snippet)

        try:
            closing_date = datetime.strptime(closing_raw, "%d/%m/%Y").date()
        except ValueError as val_err:
            raise CardParseError(
                index,
                "closing_date",
                f"Unparseable closing date format '{closing_raw}': {val_err}",
                card_snippet,
            )

        # 4. Optional fields - keep both verbatim raw and collapsed whitespace versions
        job_ref_raw = self._extract_field_text(card_tag, ["jobreference"])
        job_ref = collapse_whitespace(job_ref_raw)

        salary_raw = self._extract_field_text(card_tag, ["salary"])
        salary = collapse_whitespace(salary_raw)

        location_raw = self._extract_field_text(card_tag, ["location"])
        location = collapse_whitespace(location_raw)

        dept_label_raw = self._extract_field_text(card_tag, ["shift", "department"])
        dept_label = collapse_whitespace(dept_label_raw)

        employer_raw = self._extract_field_text(card_tag, ["school", "employer"])
        employer = collapse_whitespace(employer_raw)

        contract_raw = self._extract_field_text(card_tag, ["contract", "employmenttype"])
        contract = collapse_whitespace(contract_raw)

        # Hours: store hours_raw only and never convert it to a number.
        # Observed values in audit/testing include "various", "0", and "118".
        hours_raw = self._extract_field_text(card_tag, ["hours"])

        # Band extraction from raw salary (without altering salary_raw)
        band: Optional[str] = None
        if salary_raw:
            match = re.search(r"\b(Band\s+\d+[a-zA-Z]?)\b", salary_raw, re.IGNORECASE)
            if match:
                band = match.group(1).title()

        return VacancyCard(
            job_id=job_id,
            title_raw=title_raw,
            title=title,
            detail_url=detail_url,
            closing_date_raw=closing_raw,
            closing_date=closing_date,
            job_ref_raw=job_ref_raw,
            job_ref=job_ref,
            salary_raw=salary_raw,
            salary=salary,
            band=band,
            location_raw=location_raw,
            location=location,
            dept_label_raw=dept_label_raw,
            dept_label=dept_label,
            employer_raw=employer_raw,
            employer=employer,
            contract_raw=contract_raw,
            contract=contract,
            hours_raw=hours_raw,
        )

    def _extract_field_text(self, card_tag: Tag, class_candidates: list) -> Optional[str]:
        """Extracts verbatim raw text from p.jobdetailsitem matching one of the candidate classes."""
        for candidate in class_candidates:
            p_elem = card_tag.find(
                "p",
                class_=lambda cls: cls and "jobdetailsitem" in cls.split() and candidate in cls.split(),
            )
            if p_elem:
                text_parts = []
                for child in p_elem.children:
                    if isinstance(child, Tag) and child.name == "strong":
                        continue
                    text_parts.append(str(child.get_text() if isinstance(child, Tag) else child))
                combined = "".join(text_parts).strip()
                # Decode HTML entities while preserving internal whitespace
                raw_text = html.unescape(combined).strip()
                return raw_text if raw_text else None
        return None

