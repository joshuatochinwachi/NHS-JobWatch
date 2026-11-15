"""Alert dispatch coordinator: evaluates filters, records decisions, dispatches messages."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from jobwatch.filters import FilterConfig, evaluate_card, load_filter_config
from jobwatch.formatter import (
    chunk_message_if_needed,
    format_batched_maybe_alert,
    format_merged_change_alert,
    format_new_vacancy_alert,
    get_london_today,
    is_closing_soon,
)
from jobwatch.models import FilterDecision, VacancyCard
from jobwatch.notifier import (
    FakeNotifier,
    Notifier,
    PermanentNotifierError,
    TransientNotifierError,
)
from jobwatch.storage import Storage


def evaluate_and_record_all(
    storage: Storage,
    config: FilterConfig,
    vacancies: Optional[List[Any]] = None,
) -> Tuple[List[FilterDecision], int]:
    """
    Evaluates all listed vacancies against the current filter config and records decisions.
    Returns (list_of_decisions, changed_count).
    'changed_count' reflects decisions whose outcome changed from a prior filter version.
    """
    if vacancies is None:
        vacancies = storage.get_open_vacancies()

    now_iso = datetime.now(timezone.utc).isoformat()
    decisions = []
    changed_count = 0

    for row in vacancies:
        # Build a minimal VacancyCard for evaluation
        card = VacancyCard(
            job_id=int(row["job_id"]),
            title_raw=row["title_raw"] or "",
            title=row["title"] or "",
            detail_url=row["detail_url"] or "",
            closing_date_raw=row["closing_date_raw"] or "",
            closing_date=None,  # already stored as ISO string
            job_ref=row["job_ref"],
            salary_raw=row["salary_raw"],
            salary=row["salary"],
            band=row["band"],
            location=row["location"],
            dept_label=row["dept_label"],
            employer=row["employer"],
            contract=row["contract"],
            hours_raw=row["hours_raw"],
        )

        decision = evaluate_card(card, config, now_iso)

        # Check if outcome has changed
        prior = storage.get_decision(card.job_id)
        if prior is not None:
            if prior["outcome"] != decision.outcome:
                changed_count += 1

        storage.record_decision(
            job_id=decision.job_id,
            filter_version=decision.filter_version,
            outcome=decision.outcome,
            matched_rule=decision.matched_rule,
            match_reason=decision.match_reason,
            decided_at=decision.decided_at,
        )
        decisions.append(decision)

    return decisions, changed_count


def dispatch_new_alerts(
    storage: Storage,
    config: FilterConfig,
    notifier: Notifier,
    decisions: List[FilterDecision],
    vacancies_by_id: Dict[int, Any],
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Dispatches new vacancy alerts. Strong matches sent individually.
    >= 2 maybe matches batched into one grouped message.
    Returns (sent_count, failed_count).
    Skips expired and unlisted vacancies.
    """
    today = get_london_today()
    strong_items = []
    maybe_items = []

    for dec in decisions:
        if dec.outcome not in ("strong", "maybe"):
            continue

        row = vacancies_by_id.get(dec.job_id)
        if not row:
            continue

        # Guard: skip unlisted
        if not row["is_listed"]:
            continue

        # Guard: skip if already alerted
        if row["alerted_at"]:
            continue

        # Guard: skip if workflow status is 'baseline'
        if row["workflow_status"] == "baseline":
            continue

        # Guard: skip expired
        closing_date = None
        closing_str = row["closing_date"]
        if closing_str:
            try:
                closing_date = datetime.fromisoformat(closing_str).date()
            except (ValueError, AttributeError):
                pass

        if closing_date and closing_date < today:
            continue

        card = _row_to_card(row)
        if dec.outcome == "strong":
            strong_items.append({"card": card, "decision": dec})
        elif dec.outcome == "maybe":
            maybe_items.append({"card": card, "decision": dec})

    sent, failed = 0, 0
    now_iso = datetime.now(timezone.utc).isoformat()

    # Dispatch strong matches individually
    for item in strong_items:
        card = item["card"]
        dec = item["decision"]
        text = format_new_vacancy_alert(card, dec.outcome, dec.match_reason, config.closing_soon_days)
        for chunk in chunk_message_if_needed(text):
            try:
                ok = notifier.send(chunk)
                if ok:
                    sent += 1
                    storage.mark_alerted(card.job_id, now_iso)
            except (PermanentNotifierError, TransientNotifierError):
                failed += 1

    # Dispatch maybe matches — batch if >= 2
    if len(maybe_items) >= 2:
        text = format_batched_maybe_alert(maybe_items)
        for chunk in chunk_message_if_needed(text):
            try:
                ok = notifier.send(chunk)
                if ok:
                    sent += 1
                    for item in maybe_items:
                        storage.mark_alerted(item["card"].job_id, now_iso)
            except (PermanentNotifierError, TransientNotifierError):
                failed += 1
    elif len(maybe_items) == 1:
        card = maybe_items[0]["card"]
        dec = maybe_items[0]["decision"]
        text = format_new_vacancy_alert(card, dec.outcome, dec.match_reason, config.closing_soon_days)
        for chunk in chunk_message_if_needed(text):
            try:
                ok = notifier.send(chunk)
                if ok:
                    sent += 1
                    storage.mark_alerted(card.job_id, now_iso)
            except (PermanentNotifierError, TransientNotifierError):
                failed += 1

    return sent, failed


def dispatch_change_alerts(
    storage: Storage,
    config: FilterConfig,
    notifier: Notifier,
    run_id: int,
) -> Tuple[int, int]:
    """Dispatches merged change alerts for all changed vacancies in the given run."""
    today = get_london_today()
    cur = storage.conn.execute(
        """
        SELECT vc.job_id, vc.field_name, vc.old_value, vc.new_value
        FROM vacancy_changes vc
        JOIN vacancies v ON v.job_id = vc.job_id
        LEFT JOIN vacancy_decisions vd ON vd.job_id = vc.job_id
        WHERE vc.run_id = ?
          AND v.is_listed = 1
          AND (vd.outcome IN ('strong', 'maybe') OR vd.outcome IS NULL)
        ORDER BY vc.job_id, vc.id;
        """,
        (run_id,),
    )
    rows = cur.fetchall()

    # Group by job_id
    by_job: Dict[int, List[Any]] = {}
    for row in rows:
        by_job.setdefault(row["job_id"], []).append(row)

    sent, failed = 0, 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for job_id, changes in by_job.items():
        vac = storage.conn.execute(
            "SELECT title, job_ref, closing_date FROM vacancies WHERE job_id = ?;",
            (job_id,),
        ).fetchone()
        if not vac:
            continue

        # Guard: skip expired
        closing_date = None
        if vac["closing_date"]:
            try:
                closing_date = datetime.fromisoformat(vac["closing_date"]).date()
            except (ValueError, AttributeError):
                pass
        if closing_date and closing_date < today:
            continue

        changes_dicts = [dict(c) for c in changes]
        text = format_merged_change_alert(
            job_id, vac["title"], vac["job_ref"], changes_dicts
        )
        for chunk in chunk_message_if_needed(text):
            try:
                ok = notifier.send(chunk)
                if ok:
                    sent += 1
            except (PermanentNotifierError, TransientNotifierError):
                failed += 1

    return sent, failed


def generate_digest(
    storage: Storage,
    config: FilterConfig,
    notifier: Notifier,
    tiers: Optional[List[str]] = None,
    dry_run: bool = False,
) -> str:
    """
    Generates an on-demand digest of open strong and maybe vacancies sorted by closing date.
    Returns the formatted digest text. Never alerts automatically — only on demand.
    """
    if tiers is None:
        tiers = ["strong", "maybe"]

    today = get_london_today()
    rows = storage.get_open_vacancies()
    matching = []

    for row in rows:
        outcome = row["outcome"]
        if outcome not in tiers:
            continue

        closing_date = None
        closing_str = row["closing_date"]
        if closing_str:
            try:
                closing_date = datetime.fromisoformat(closing_str).date()
            except (ValueError, AttributeError):
                pass

        if closing_date and closing_date < today:
            continue

        matching.append({
            "row": row,
            "closing_date": closing_date,
            "outcome": outcome,
        })

    matching.sort(key=lambda x: (x["closing_date"] or today.replace(year=today.year + 1), x["row"]["job_id"]))

    if not matching:
        digest = "📋 <b>NHS-JobWatch Digest</b>\n\nNo open strong or maybe matches found."
        notifier.send(digest)
        return digest

    lines = [f"📋 <b>NHS-JobWatch Digest</b> ({len(matching)} vacancies)\n"]
    for item in matching:
        row = item["row"]
        tier = "⭐ STRONG" if item["outcome"] == "strong" else "📌 MAYBE"
        title_esc = html.escape(row["title"] or "Unknown")
        loc_esc = html.escape(row["location"] or "Scotland")
        band_esc = html.escape(row["band"] or row["salary"] or "?")
        closing = item["closing_date"].strftime("%d/%m/%Y") if item["closing_date"] else "?"
        url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={row['job_id']}"
        lines.append(
            f"[{tier}] <a href=\"{url}\"><b>{title_esc}</b></a>\n"
            f"  {band_esc} | {loc_esc} | Closes {closing}"
        )

    digest = "\n\n".join(lines)
    for chunk in chunk_message_if_needed(digest):
        notifier.send(chunk)

    return digest


def _row_to_card(row: Any) -> VacancyCard:
    """Converts a storage row to a VacancyCard for formatting."""
    from datetime import date as date_type
    closing_date = None
    cs = row["closing_date"]
    if cs:
        try:
            closing_date = datetime.fromisoformat(cs).date()
        except (ValueError, AttributeError):
            pass

    return VacancyCard(
        job_id=int(row["job_id"]),
        title_raw=row["title_raw"] or "",
        title=row["title"] or "",
        detail_url=row["detail_url"] or "",
        closing_date_raw=row["closing_date_raw"] or "",
        closing_date=closing_date,
        job_ref=row["job_ref"],
        salary_raw=row["salary_raw"],
        salary=row["salary"],
        band=row["band"],
        location_raw=row["location_raw"],
        location=row["location"],
        dept_label_raw=row["dept_label_raw"],
        dept_label=row["dept_label"],
        employer_raw=row["employer_raw"],
        employer=row["employer"],
        contract_raw=row["contract_raw"],
        contract=row["contract"],
        hours_raw=row["hours_raw"],
    )
