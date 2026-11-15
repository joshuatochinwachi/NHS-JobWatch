"""Asynchronous background scheduler for automated sweeps, alerts, reminders, and health monitoring."""

import asyncio
import html
import logging
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from jobwatch.circuit_breaker import CircuitBreaker
from jobwatch.config import (
    LISTING_ENDPOINT,
    MONITORED_SCOPES,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from jobwatch.fetcher import ChallengePageError, Fetcher
from jobwatch.filters import evaluate_card, load_filter_config
from jobwatch.formatter import format_new_vacancy_alert
from jobwatch.keyboards import alert_keyboard
from jobwatch.notifier import sanitize_error
from jobwatch.reminders import find_due_closing_reminders, format_closing_reminder
from jobwatch.storage import Storage
from jobwatch.sweep import PageSource, SweepCoordinator

logger = logging.getLogger("jobwatch.scheduler")
LONDON_TZ = ZoneInfo("Europe/London")


class AsyncScheduler:
    """Orchestrates scheduled tasks inside the Telegram bot process."""

    def __init__(
        self,
        storage: Storage,
        bot=None,
        allowed_chat_ids: Optional[List[int]] = None,
        admin_chat_id: Optional[int] = None,
        page_source: Optional[PageSource] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], Coroutine]] = None,
    ):
        self.storage = storage
        self.bot = bot
        self.allowed_chat_ids = allowed_chat_ids or []
        self.admin_chat_id = admin_chat_id
        self.page_source = page_source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep_func = sleep_func or asyncio.sleep

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.circuit_breaker = CircuitBreaker(
            storage=self.storage,
            alert_callback=self._send_admin_alert,
        )

    @property
    def is_enabled(self) -> bool:
        """Returns True if automated sweeps are enabled via SCHEDULER_ENABLED env var."""
        val = os.getenv("SCHEDULER_ENABLED", "0").strip().lower()
        return val in ("1", "true", "yes")

    def start(self) -> None:
        """Starts the background scheduler loop task."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._main_loop())
            logger.info(f"AsyncScheduler started (SCHEDULER_ENABLED={self.is_enabled}).")

    def stop(self) -> None:
        """Cancels and stops the background scheduler loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("AsyncScheduler stopped.")

    async def _send_admin_alert(self, text: str) -> None:
        """Helper to send an operational alert to the admin chat without secrets."""
        if not self.bot or not self.admin_chat_id:
            logger.info(f"[Admin Alert (no bot attached)]: {text}")
            return
        try:
            await self.bot.send_message(
                chat_id=self.admin_chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send admin alert: {sanitize_error(str(e))}")

    # ── Polite Page Fetcher with Jitter ──

    def _default_page_source(self, scope_key: str, skip: int) -> str:
        """Default page fetcher enforcing 3.5s floor + random jitter."""
        fetcher = Fetcher(user_agent=USER_AGENT)
        # Add random jitter between 0.2s and 1.0s
        jitter = random.uniform(0.2, 1.0)
        time.sleep(jitter)
        return fetcher.fetch_scope(scope_key, skip)

    # ── Night Hours & Alert Dispatching ──

    def is_night_hours(self, now_dt: Optional[datetime] = None) -> bool:
        """Returns True between 22:00 and 07:00 Europe/London time."""
        dt = (now_dt or self.clock()).astimezone(LONDON_TZ)
        hour = dt.hour
        return hour >= 22 or hour < 7

    async def dispatch_new_vacancy_alerts(self, new_vacancies: List[Dict[str, Any]]) -> int:
        """Evaluates and dispatches alerts for newly discovered vacancies."""
        if not new_vacancies or not self.bot or not self.allowed_chat_ids:
            return 0

        config = load_filter_config()
        now_dt = self.clock()
        now_iso = now_dt.isoformat()
        is_night = self.is_night_hours(now_dt)

        sent_count = 0

        for vac in new_vacancies:
            jid = vac["job_id"]
            # Evaluate against current filter config
            from jobwatch.models import VacancyCard
            card = VacancyCard(
                job_id=jid,
                job_ref_raw=vac.get("job_ref_raw"),
                job_ref=vac.get("job_ref"),
                title_raw=vac.get("title_raw", ""),
                title=vac.get("title", ""),
                detail_url=vac.get("detail_url", f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={jid}"),
                closing_date_raw=vac.get("closing_date_raw", ""),
                closing_date=datetime.fromisoformat(vac["closing_date"]) if vac.get("closing_date") else None,
                salary_raw=vac.get("salary_raw"),
                salary=vac.get("salary"),
                band=vac.get("band"),
                location_raw=vac.get("location_raw"),
                location=vac.get("location"),
                dept_label_raw=vac.get("dept_label_raw"),
                dept_label=vac.get("dept_label"),
                employer_raw=vac.get("employer_raw"),
                employer=vac.get("employer"),
                contract_raw=vac.get("contract_raw"),
                contract=vac.get("contract"),
                hours_raw=vac.get("hours_raw"),
            )
            dec = evaluate_card(card, config, now_iso)
            self.storage.record_decision(
                jid, dec.filter_version, dec.outcome, dec.matched_rule, dec.match_reason, dec.decided_at
            )

            # Only strong and maybe matches trigger notifications
            if dec.outcome not in ("strong", "maybe"):
                continue

            alert_text = format_new_vacancy_alert(card, outcome=dec.outcome, matched_rule=dec.matched_rule)
            kb = alert_keyboard(jid, card.detail_url)

            for cid in self.allowed_chat_ids:
                chat_id_str = str(cid)
                st = self.storage.get_chat_settings(chat_id_str)

                # Skip if chat is inactive (blocked/not found)
                if st and not st["is_active"]:
                    continue

                # Hold alert if paused
                if st and st["is_paused"]:
                    continue

                # Check mute setting
                is_muted = bool(st and st["muted_until"] and now_dt < datetime.fromisoformat(st["muted_until"]))
                disable_notif = is_night or is_muted

                try:
                    msg = await self.bot.send_message(
                        chat_id=cid,
                        text=alert_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                        disable_notification=disable_notif,
                    )
                    self.storage.record_delivery(jid, chat_id_str, msg.message_id, now_iso)
                except Forbidden:
                    self.storage.update_chat_settings(chat_id_str, is_active=0, inactive_reason="bot_blocked")
                except BadRequest as b_err:
                    if "chat not found" in str(b_err).lower():
                        self.storage.update_chat_settings(chat_id_str, is_active=0, inactive_reason="chat_not_found")
                except Exception as e:
                    logger.error(f"Failed to deliver alert for job {jid} to chat {cid}: {sanitize_error(str(e))}")

            # Mark alerted if all active chats delivered
            self.storage.mark_alerted_if_all_delivered(
                jid,
                allowed_chat_ids=[str(c) for c in self.allowed_chat_ids],
                alerted_at=now_iso,
            )
            sent_count += 1

        return sent_count

    # ── Sweep Runner ──

    async def run_sweep_cycle(self, run_type: str = "shallow") -> None:
        """Executes a sweep cycle across all currently baselined scopes."""
        if not self.is_enabled:
            logger.info("SCHEDULER_ENABLED=0: Skipping automated sweep cycle.")
            return

        if self.circuit_breaker.is_tripped():
            reason = self.circuit_breaker.get_trip_reason() or "Unknown"
            logger.warning(f"Circuit breaker is TRIPPED ({reason}). Skipping automated sweep cycle.")
            return

        ps = self.page_source or self._default_page_source
        coordinator = SweepCoordinator(
            storage=self.storage,
            page_source=ps,
            raw_retention_dir=os.path.join(self.storage.db_path + "_snapshots") if self.storage.db_path != ":memory:" else None,
        )

        for scope in MONITORED_SCOPES:
            # Rule 5: "The scheduler never sweeps an unbaselined scope."
            meta = self.storage.get_scope_metadata(scope)
            if not meta or not meta["is_baselined"]:
                logger.info(f"Scope '{scope}' is not baselined yet. Skipping automated {run_type} sweep.")
                continue

            try:
                # Run the sweep in a worker thread to keep the event loop responsive
                result = await asyncio.to_thread(
                    coordinator.execute_sweep,
                    scope_key=scope,
                    run_type=run_type,
                    shallow_max_pages=2,
                )

                if result.outcome == "success":
                    self.circuit_breaker.record_success()

                    # Handle newly seen vacancies
                    if result.new_count > 0:
                        # Fetch new vacancies for alert dispatch
                        new_vacs = self.storage.get_unalerted_vacancies(user_id="shared")
                        await self.dispatch_new_vacancy_alerts(new_vacs)

                    # Rule 3: Shallow-miss detector alert
                    if result.shallow_misses:
                        for sm in result.shallow_misses:
                            alert_msg = (
                                f"⚠️ <b>SHALLOW-MISS DETECTED</b>\n\n"
                                f"• <b>Job ID:</b> {sm['job_id']}\n"
                                f"• <b>Title:</b> {html.escape(sm.get('title') or 'Unknown')}\n"
                                f"• <b>Scope:</b> <code>{sm['scope_key']}</code>\n"
                                f"• <b>Page Discovered:</b> Page {sm['page_no']}\n\n"
                                f"<i>This vacancy was first seen on page > 2 during a full sweep, "
                                f"indicating it was not captured during hourly shallow sweeps.</i>"
                            )
                            await self._send_admin_alert(alert_msg)

                elif result.outcome in ("drifted", "partial_failure"):
                    logger.warning(f"Sweep for scope '{scope}' ended with outcome '{result.outcome}'")

            except Exception as exc:
                is_tripped = await self.circuit_breaker.handle_error(exc, context_info=f"scope={scope}, run_type={run_type}")
                if is_tripped:
                    # Halt subsequent scopes in this cycle
                    break

    # ── Periodic Maintenance Jobs ──

    async def run_snooze_wakeups(self) -> int:
        """Wakes up snoozed vacancies whose snooze_until has expired."""
        now_iso = self.clock().isoformat()
        woken = self.storage.process_snooze_wakeups(user_id="shared", current_time_iso=now_iso)
        if not woken or not self.bot or not self.allowed_chat_ids:
            return len(woken)

        for raw_row in woken:
            row = dict(raw_row)
            jid = row["job_id"]
            title = html.escape(row.get("title") or "Unknown")
            c_date = row.get("closing_date", "")[:10]
            url = row.get("detail_url") or f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={jid}"

            text = (
                f"⏰ <b>SNOOZE EXPIRED: REMINDER</b>\n\n"
                f"⭐ <b>{title}</b>\n"
                f"<b>Band:</b> {html.escape(row.get('band') or '')}\n"
                f"<b>Closes:</b> {c_date}\n\n"
                f"<i>This previously snoozed post has returned to your active queue.</i>"
            )
            kb = alert_keyboard(jid, url)

            for cid in self.allowed_chat_ids:
                try:
                    await self.bot.send_message(
                        chat_id=cid,
                        text=text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        return len(woken)

    async def run_closing_reminders(self) -> int:
        """Sends closing-date reminders (at 5 days and 1 day) for unactioned Strong matches."""
        now_dt = self.clock()
        today_str = now_dt.astimezone(LONDON_TZ).strftime("%Y-%m-%d")
        due = find_due_closing_reminders(self.storage, today_str)

        if not due or not self.bot or not self.allowed_chat_ids:
            return len(due)

        for item in due:
            jid = item["job_id"]
            days_left = item["days_left"]
            text, kb = format_closing_reminder(item, days_left)

            for cid in self.allowed_chat_ids:
                try:
                    await self.bot.send_message(
                        chat_id=cid,
                        text=text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            self.storage.record_reminder_sent(jid, days_left, now_dt.isoformat())

        return len(due)

    async def run_daily_health_report(self) -> None:
        """Sends daily 08:30 health report to the admin chat."""
        if not self.bot or not self.admin_chat_id:
            return

        shallow_count, full_count = self.storage.get_recent_sweeps_count(hours=24)
        shallow_misses_24h = self.storage.get_shallow_misses_since(
            (self.clock() - timedelta(hours=24)).isoformat()
        )
        total_tracked = self.storage.get_vacancy_count()
        applied = self.storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'applied';").fetchone()[0]
        skipped = self.storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'skipped';").fetchone()[0]
        snoozed = self.storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'snoozed';").fetchone()[0]
        cb_state = self.storage.get_circuit_breaker_state()
        cb_status = "🚨 TRIPPED (" + str(cb_state.get("tripped_reason")) + ")" if cb_state.get("is_tripped") else "✅ Healthy"

        msg = (
            f"📊 <b>NHS-JobWatch Daily Health Report</b>\n\n"
            f"• <b>Scheduler:</b> {'Enabled' if self.is_enabled else 'Disabled (SCHEDULER_ENABLED=0)'}\n"
            f"• <b>Circuit Breaker:</b> {cb_status}\n"
            f"• <b>Sweeps (Last 24h):</b> {shallow_count} shallow, {full_count} full\n"
            f"• <b>Shallow Misses (24h):</b> {shallow_misses_24h}\n"
            f"• <b>Tracked Vacancies:</b> {total_tracked}\n"
            f"• <b>User Actions:</b> {applied} Applied, {skipped} Skipped, {snoozed} Snoozed\n"
        )
        await self._send_admin_alert(msg)

    async def run_database_backup(self, backup_dir: str = "/app/data/backups") -> Optional[str]:
        """Creates a database backup and dispatches it to the admin chat."""
        try:
            backup_file = self.storage.backup_database(backup_dir, keep=7)
            file_size_mb = os.path.getsize(backup_file) / (1024 * 1024)

            if self.bot and self.admin_chat_id:
                if file_size_mb <= 50.0:
                    with open(backup_file, "rb") as doc:
                        await self.bot.send_document(
                            chat_id=self.admin_chat_id,
                            document=doc,
                            caption=f"📦 Daily Database Backup ({file_size_mb:.2f} MB)",
                        )
                else:
                    await self._send_admin_alert(
                        f"⚠️ Backup file size ({file_size_mb:.2f} MB) exceeds Telegram 50 MB limit. "
                        f"Backup saved locally to persistent volume: <code>{backup_file}</code>"
                    )
            return backup_file
        except Exception as e:
            err_msg = sanitize_error(str(e))
            logger.error(f"Daily backup failed: {err_msg}")
            await self._send_admin_alert(f"⚠️ <b>Daily Backup Failed:</b> <code>{err_msg}</code>")
            return None

    async def check_watchdogs(self) -> None:
        """Alerts if shallow run is absent for 3h or full sweep is absent for 30h."""
        if not self.is_enabled or self.circuit_breaker.is_tripped():
            return

        now_dt = self.clock()

        # 1. Shallow Run Watchdog (3 hours)
        last_shallow = self.storage.get_last_successful_run(run_type="shallow")
        if last_shallow and last_shallow["finished_at"]:
            try:
                finished = datetime.fromisoformat(last_shallow["finished_at"])
                if (now_dt - finished).total_seconds() > (3 * 3600):
                    await self._send_admin_alert("⚠️ <b>WATCHDOG ALERT:</b> No successful shallow sweep in over 3 hours.")
            except Exception:
                pass

        # 2. Full Sweep Watchdog (30 hours)
        last_full = self.storage.get_last_successful_run(run_type="full_sweep")
        if last_full and last_full["finished_at"]:
            try:
                finished = datetime.fromisoformat(last_full["finished_at"])
                if (now_dt - finished).total_seconds() > (30 * 3600):
                    await self._send_admin_alert("⚠️ <b>WATCHDOG ALERT:</b> No successful full sweep in over 30 hours.")
            except Exception:
                pass

    # ── Main Scheduler Loop ──

    async def _main_loop(self) -> None:
        """Main loop evaluating jobs based on persistent scheduler state and wall clock."""
        last_hourly_check = -1
        last_snooze_check = 0.0

        while self._running:
            try:
                now_dt = self.clock()
                now_london = now_dt.astimezone(LONDON_TZ)
                current_hour = now_london.hour
                current_minute = now_london.minute

                # 1. Snooze Wakeups (Every 15 minutes)
                if time.monotonic() - last_snooze_check >= 900:
                    last_snooze_check = time.monotonic()
                    await self.run_snooze_wakeups()

                # 2. Hourly Sweep and Maintenance Trigger
                if current_hour != last_hourly_check and current_minute >= 0:
                    last_hourly_check = current_hour

                    # Check for 04:00 Database Backup
                    if current_hour == 4:
                        await self.run_database_backup()

                    # Check for 08:30 Daily Health Report
                    if current_hour == 8:
                        await self.run_daily_health_report()

                    # Check for 09:00 Closing Reminders
                    if current_hour == 9:
                        await self.run_closing_reminders()

                    # Sweeps: 06:00 and 18:00 are Full Sweeps; all other hours are Shallow Sweeps
                    if current_hour in (6, 18):
                        await self.run_sweep_cycle(run_type="full_sweep")
                    else:
                        await self.run_sweep_cycle(run_type="shallow")

                    # Check watchdogs after sweep cycle
                    await self.check_watchdogs()

                # Sleep 30 seconds between loop iterations
                await self.sleep_func(30.0)

            except asyncio.CancelledError:
                logger.info("Scheduler task cancelled cleanly.")
                break
            except Exception as e:
                logger.error(f"Error in scheduler main loop: {sanitize_error(str(e))}")
                await self.sleep_func(10.0)

