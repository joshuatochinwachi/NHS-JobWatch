"""Message formatting and HTML template rendering for Telegram notifications and digests."""

import html
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from jobwatch.models import VacancyCard

LONDON_TZ = ZoneInfo("Europe/London")


def get_london_today() -> date:
    """Returns today's date in Europe/London timezone."""
    return datetime.now(LONDON_TZ).date()


def parse_full_time(hours_raw: Optional[str]) -> bool:
    """Returns True only if hours_raw parses cleanly to a numeric value between 35 and 40."""
    if not hours_raw:
        return False
    # Look for leading numeric float or int
    match = re.search(r"^\s*(\d+(?:\.\d+)?)\s*(?:hours|hrs|hpw)?\b", hours_raw, re.IGNORECASE)
    if not match:
        return False
    try:
        val = float(match.group(1))
        return 35.0 <= val <= 40.0
    except ValueError:
        return False


def is_closing_soon(closing_date: Optional[date], closing_soon_days: int = 5) -> bool:
    """Returns True if closing_date is within closing_soon_days of today in London."""
    if not closing_date:
        return False
    today = get_london_today()
    delta = (closing_date - today).days
    return 0 <= delta <= closing_soon_days


def format_new_vacancy_alert(
    card: VacancyCard,
    outcome: str,
    match_reason: str,
    closing_soon_days: int = 5,
) -> str:
    """Formats a single new vacancy into a rich Telegram HTML message."""
    tier_badge = "⭐ <b>[STRONG MATCH]</b>" if outcome == "strong" else "📌 <b>[POTENTIAL MATCH]</b>"
    title_esc = html.escape(card.title or "Unknown Title")
    employer_esc = html.escape(card.employer or "NHS Scotland")
    dept_esc = html.escape(card.dept_label or "Not specified")
    loc_esc = html.escape(card.location or "Scotland")
    salary_esc = html.escape(card.salary or card.band or "Not specified")
    contract_esc = html.escape(card.contract or "Not specified")
    hours_esc = html.escape(card.hours_raw or "Not specified")
    job_ref_esc = html.escape(card.job_ref or str(card.job_id))
    detail_url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={card.job_id}"

    # Derived flags from card data
    flags = []
    if is_closing_soon(card.closing_date, closing_soon_days):
        flags.append("⏳ <b>Closing Soon!</b>")
    if parse_full_time(card.hours_raw):
        flags.append("🕒 <b>Full-Time</b>")
    contract_lower = (card.contract or "").lower()
    title_lower = (card.title or "").lower()
    if "bank" in contract_lower or "bank" in title_lower:
        flags.append("🏦 <b>Bank Post</b>")
    if "fixed term" in contract_lower or "fixed-term" in contract_lower or "temporary" in contract_lower:
        flags.append("📅 <b>Fixed-Term</b>")

    flags_line = f"\n{' | '.join(flags)}\n" if flags else ""

    closing_str = card.closing_date.strftime("%d/%m/%Y") if card.closing_date else (card.closing_date_raw or "Unknown")

    return (
        f"{tier_badge}\n"
        f"<b>{title_esc}</b>\n"
        f"{flags_line}\n"
        f"🏥 <b>Employer:</b> {employer_esc}\n"
        f"🏢 <b>Department:</b> {dept_esc}\n"
        f"📍 <b>Location:</b> {loc_esc}\n"
        f"💰 <b>Band / Salary:</b> {salary_esc}\n"
        f"📋 <b>Contract:</b> {contract_esc}\n"
        f"🕒 <b>Hours:</b> {hours_esc}\n"
        f"📅 <b>Closing Date:</b> {closing_str}\n"
        f"🔖 <b>Job Ref:</b> {job_ref_esc}\n\n"
        f"🔗 <a href=\"{detail_url}\">View on NHS Scotland</a>\n"
        f"ℹ️ <i>Rule: {html.escape(match_reason)}</i>"
    )


def format_merged_change_alert(
    job_id: int,
    title: str,
    job_ref: Optional[str],
    changes: List[Dict[str, Any]],
) -> str:
    """Consolidates all field updates for a single vacancy in a sweep into one message."""
    title_esc = html.escape(title or "Unknown Title")
    ref_esc = html.escape(job_ref or str(job_id))
    detail_url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}"

    # Check for combined salary and band change
    change_map = {c["field_name"]: c for c in changes}
    diff_lines = []

    if "salary" in change_map:
        sal = change_map["salary"]
        old_sal = html.escape(sal["old_value"] or "None")
        new_sal = html.escape(sal["new_value"] or "None")
        diff_lines.append(f"💰 <b>Salary:</b> {old_sal} ➔ {new_sal}")
        # Mark band as handled so it doesn't duplicate
        change_map.pop("band", None)
        change_map.pop("salary", None)

    for fname, c in change_map.items():
        old_val = html.escape(c["old_value"] or "None")
        new_val = html.escape(c["new_value"] or "None")
        label = fname.replace("_", " ").title()
        icon = "📅" if "date" in fname else ("📍" if "location" in fname else "📝")
        diff_lines.append(f"{icon} <b>{label}:</b> {old_val} ➔ {new_val}")

    diff_body = "\n".join(diff_lines)

    return (
        f"🔄 <b>Job Update: {title_esc}</b>\n"
        f"🔖 <b>Ref:</b> {ref_esc}\n\n"
        f"{diff_body}\n\n"
        f"🔗 <a href=\"{detail_url}\">View Vacancy on NHS Scotland</a>"
    )


def format_batched_maybe_alert(cards_with_decisions: List[Dict[str, Any]]) -> str:
    """Formats multiple maybe vacancies into a single grouped message."""
    count = len(cards_with_decisions)
    header = f"📋 <b>NHS Scotland: {count} Potential Matches</b>\n\n"

    items = []
    for item in cards_with_decisions:
        card: VacancyCard = item["card"]
        title_esc = html.escape(card.title or "Unknown Title")
        loc_esc = html.escape(card.location or "Scotland")
        band_esc = html.escape(card.band or card.salary or "Band unk")
        closing = card.closing_date.strftime("%d/%m") if card.closing_date else "?"
        url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={card.job_id}"
        items.append(f"• <a href=\"{url}\"><b>{title_esc}</b></a> — {band_esc} ({loc_esc}) [Closes {closing}]")

    return header + "\n".join(items)


def chunk_message_if_needed(text: str, max_chars: int = 4000) -> List[str]:
    """Splits a long HTML message into numbered chunks if exceeding Telegram's 4096 character limit."""
    if len(text) <= max_chars:
        return [text]

    lines = text.split("\n")
    chunks = []
    cur_chunk = []
    cur_len = 0

    for line in lines:
        line_len = len(line) + 1
        if cur_len + line_len > max_chars and cur_chunk:
            chunks.append("\n".join(cur_chunk))
            cur_chunk = [line]
            cur_len = line_len
        else:
            cur_chunk.append(line)
            cur_len += line_len

    if cur_chunk:
        chunks.append("\n".join(cur_chunk))

    if len(chunks) > 1:
        return [f"<i>(Part {idx + 1} of {len(chunks)})</i>\n" + c for idx, c in enumerate(chunks)]
    return chunks

