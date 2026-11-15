"""Closing-date reminders and far-future bank post filtering for unactioned vacancies."""

import html
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Tuple

from telegram import InlineKeyboardMarkup

from jobwatch.keyboards import alert_keyboard
from jobwatch.storage import Storage


def find_due_closing_reminders(storage: Storage, today_date_str: str) -> List[Dict[str, Any]]:
    """Identifies unactioned Strong vacancies closing in 5 days or 1 day.

    Exempts far-future bank posts (closing > 60 days in future).
    """
    today_dt = date.fromisoformat(today_date_str[:10])

    cur = storage.conn.execute(
        """
        SELECT v.*, d.outcome, d.matched_rule, s.status as user_status
        FROM vacancies v
        JOIN vacancy_decisions d ON v.job_id = d.job_id
        JOIN user_vacancy_status s ON v.job_id = s.job_id AND s.user_id = 'shared'
        WHERE s.status = 'new'
          AND d.outcome = 'strong'
          AND v.is_listed = 1
          AND v.closing_date IS NOT NULL;
        """
    )
    rows = cur.fetchall()
    due = []

    for row in rows:
        c_date_raw = row["closing_date"]
        if not c_date_raw:
            continue
        try:
            c_date = date.fromisoformat(c_date_raw[:10])
        except ValueError:
            continue

        days_left = (c_date - today_dt).days

        # Exclude far-future bank posts (> 60 days)
        if days_left > 60:
            continue

        if days_left in (5, 1):
            if not storage.has_reminder_been_sent(row["job_id"], days_left):
                item = dict(row)
                item["days_left"] = days_left
                due.append(item)

    return due


def format_closing_reminder(vacancy: Dict[str, Any], days_left: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Formats an urgent closing-date reminder message with action buttons."""
    job_id = vacancy["job_id"]
    title = html.escape(vacancy.get("title") or "Unknown Vacancy")
    band = html.escape(vacancy.get("band") or "Band Unspecified")
    employer = html.escape(vacancy.get("employer") or "NHS Scotland")
    location = html.escape(vacancy.get("location") or "Scotland")
    closing_date = vacancy.get("closing_date", "")[:10]
    detail_url = vacancy.get("detail_url") or f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}"

    day_word = "day" if days_left == 1 else "days"
    text = (
        f"⏳ <b>CLOSING SOON: {days_left} {day_word.upper()} LEFT</b>\n\n"
        f"⭐ <b>{title}</b>\n"
        f"<b>Band:</b> {band}\n"
        f"<b>Location:</b> {location}\n"
        f"<b>Employer:</b> {employer}\n"
        f"<b>Closes:</b> {closing_date}\n\n"
        f"<i>This high-match role has not been actioned yet.</i>"
    )
    kb = alert_keyboard(job_id, detail_url)
    return text, kb

