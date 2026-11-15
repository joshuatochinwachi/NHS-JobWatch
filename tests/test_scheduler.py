"""Unit tests for Step 5: AsyncScheduler, CircuitBreaker, Reminders, Shallow Misses, and Bot Admin Commands."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from jobwatch.circuit_breaker import CircuitBreaker
from jobwatch.config import MONITORED_SCOPES
from jobwatch.fetcher import ChallengePageError
from jobwatch.reminders import find_due_closing_reminders, format_closing_reminder
from jobwatch.scheduler import AsyncScheduler, LONDON_TZ
from jobwatch.storage import Storage

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE_LISTING = os.path.join(FIXTURES_DIR, "sample_listing.html")
SAMPLE_PAST_END = os.path.join(FIXTURES_DIR, "sample_past_end.html")


class MockBotMessage:
    def __init__(self, message_id=101, text=""):
        self.message_id = message_id
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        return self


class MockBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_documents = []
        self.message_id_counter = 1000

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, disable_notification=False):
        self.message_id_counter += 1
        msg = MockBotMessage(self.message_id_counter, text)
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "disable_notification": disable_notification,
            "msg": msg,
        })
        return msg

    async def send_document(self, chat_id, document, caption=None, parse_mode=None):
        self.sent_documents.append({
            "chat_id": chat_id,
            "caption": caption,
        })
        return MockBotMessage(1001, "Document sent")


class TestSchedulerAndStep5(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.bot = MockBot()
        with open(SAMPLE_LISTING, "r", encoding="utf-8") as f:
            raw_html = f.read()
            # Normalize totalMatchRecords to 12 to match the 12 cards in fixture
            self.sample_html = re.sub(
                r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                '<input type="hidden" id="totalMatchRecords" value="12">',
                raw_html,
            )
        with open(SAMPLE_PAST_END, "r", encoding="utf-8") as f:
            self.empty_html = f.read()

    def tearDown(self):
        self.storage.close()

    def _mark_scope_baselined(self, scope: str, baseline_run_id: int = 1):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.storage.ensure_scope_metadata(scope)
        # Ensure at least one vacancy in DB to satisfy SweepCoordinator integrity check
        self.storage.conn.execute(
            """INSERT OR IGNORE INTO vacancies (
                job_id, title_raw, title, detail_url, closing_date_raw, closing_date,
                first_seen_at, last_seen_at, is_listed, content_hash
            ) VALUES (1, 'Baseline Job', 'Baseline Job', 'https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=1',
                      '2026-12-01', '2026-12-01', ?, ?, 1, 'hash1');""",
            (now_iso, now_iso)
        )
        self.storage.conn.execute(
            "UPDATE scope_metadata SET is_baselined = 1, baseline_run_id = ?, baselined_at = ? WHERE scope_key = ?;",
            (baseline_run_id, now_iso, scope),
        )
        self.storage.conn.commit()

    # 1. Scheduler Disabled Check
    async def test_scheduler_disabled_when_scheduler_enabled_is_zero(self):
        """When SCHEDULER_ENABLED=0, scheduler skips automated sweep cycles."""
        with patch.dict(os.environ, {"SCHEDULER_ENABLED": "0"}):
            scheduler = AsyncScheduler(
                storage=self.storage,
                bot=self.bot,
                allowed_chat_ids=[12345],
                admin_chat_id=12345,
            )
            self.assertFalse(scheduler.is_enabled)
            await scheduler.run_sweep_cycle("shallow")
            # Verify no runs recorded
            row = self.storage.conn.execute("SELECT COUNT(*) FROM source_runs;").fetchone()[0]
            self.assertEqual(row, 0)

    # 2. Scheduler Skips Unbaselined Scope
    async def test_scheduler_skips_unbaselined_scope(self):
        """Rule 5: The scheduler never sweeps an unbaselined scope."""
        with patch.dict(os.environ, {"SCHEDULER_ENABLED": "1"}):
            scheduler = AsyncScheduler(
                storage=self.storage,
                bot=self.bot,
                allowed_chat_ids=[12345],
                admin_chat_id=12345,
            )
            self.assertTrue(scheduler.is_enabled)
            # Scopes are unbaselined by default
            await scheduler.run_sweep_cycle("shallow")
            row = self.storage.conn.execute("SELECT COUNT(*) FROM source_runs;").fetchone()[0]
            self.assertEqual(row, 0)

    # 3. Baselined Scope Sweeps
    async def test_scheduler_runs_baselined_scope_shallow_and_full(self):
        """When scope is baselined, automated shallow and full sweeps execute."""
        scope = "Departments=9"
        self._mark_scope_baselined(scope, baseline_run_id=1)

        def mock_page_source(scope_key, skip):
            if skip == 0:
                return self.sample_html
            return self.empty_html

        with patch.dict(os.environ, {"SCHEDULER_ENABLED": "1"}):
            scheduler = AsyncScheduler(
                storage=self.storage,
                bot=self.bot,
                allowed_chat_ids=[12345],
                admin_chat_id=12345,
                page_source=mock_page_source,
            )
            # Run shallow sweep cycle
            await scheduler.run_sweep_cycle("shallow")
            run = self.storage.conn.execute("SELECT run_type, outcome FROM source_runs ORDER BY id DESC LIMIT 1;").fetchone()
            self.assertIsNotNone(run)
            self.assertEqual(run["run_type"], "shallow")
            self.assertEqual(run["outcome"], "success")

            # Run full sweep cycle
            await scheduler.run_sweep_cycle("full_sweep")
            run2 = self.storage.conn.execute("SELECT run_type, outcome FROM source_runs ORDER BY id DESC LIMIT 1;").fetchone()
            self.assertEqual(run2["run_type"], "full_sweep")
            self.assertEqual(run2["outcome"], "success")

    # 4. Circuit Breaker Hard Trip on 403 and 429
    async def test_circuit_breaker_hard_trips_on_403_and_429(self):
        """Hard trips immediately on HTTP 403 and 429 and dispatches admin alert."""
        alerts = []
        async def alert_callback(msg):
            alerts.append(msg)

        cb = CircuitBreaker(storage=self.storage, alert_callback=alert_callback)
        self.assertFalse(cb.is_tripped())

        # Test 403 Forbidden
        exc_403 = urllib.error.HTTPError(url="http://test", code=403, msg="Forbidden", hdrs={}, fp=None)
        tripped = await cb.handle_error(exc_403, context_info="test 403")
        self.assertTrue(tripped)
        self.assertTrue(cb.is_tripped())
        self.assertIn("403", cb.get_trip_reason())
        self.assertEqual(len(alerts), 1)
        self.assertIn("CIRCUIT BREAKER HARD TRIP", alerts[0])

        # Reset and test 429 Too Many Requests
        cb.resume()
        self.assertFalse(cb.is_tripped())
        exc_429 = urllib.error.HTTPError(url="http://test", code=429, msg="Too Many Requests", hdrs={}, fp=None)
        tripped_429 = await cb.handle_error(exc_429, context_info="test 429")
        self.assertTrue(tripped_429)
        self.assertTrue(cb.is_tripped())
        self.assertIn("429", cb.get_trip_reason())
        self.assertEqual(len(alerts), 2)

    # 5. Circuit Breaker Hard Trip on Challenge Page
    async def test_circuit_breaker_hard_trips_on_challenge_page(self):
        """ChallengePageError immediately trips circuit breaker and does not retry."""
        alerts = []
        cb = CircuitBreaker(storage=self.storage, alert_callback=lambda m: alerts.append(m) or asyncio.sleep(0))
        exc_chal = ChallengePageError("Cloudflare verification challenge encountered")
        tripped = await cb.handle_error(exc_chal, context_info="scope=Departments=9")
        self.assertTrue(tripped)
        self.assertTrue(cb.is_tripped())
        self.assertIn("Anti-bot", cb.get_trip_reason())
        self.assertEqual(len(alerts), 1)

    # 6. Circuit Breaker Transient Failures Trip After 3 Consecutive
    async def test_circuit_breaker_transient_failures_trip_after_3_consecutive(self):
        """5xx / timeout errors trip only after 3 consecutive failures."""
        cb = CircuitBreaker(storage=self.storage, alert_callback=lambda m: asyncio.sleep(0))

        exc_transient = urllib.error.URLError("Connection timed out")

        # 1st failure: increments count, does NOT trip
        tripped1 = await cb.handle_error(exc_transient, context_info="run 1")
        self.assertFalse(tripped1)
        self.assertFalse(cb.is_tripped())
        st = self.storage.get_circuit_breaker_state()
        self.assertEqual(st["consecutive_failures"], 1)

        # 2nd failure: increments count, does NOT trip
        tripped2 = await cb.handle_error(exc_transient, context_info="run 2")
        self.assertFalse(tripped2)
        self.assertFalse(cb.is_tripped())
        st = self.storage.get_circuit_breaker_state()
        self.assertEqual(st["consecutive_failures"], 2)

        # 3rd failure: trips!
        tripped3 = await cb.handle_error(exc_transient, context_info="run 3")
        self.assertTrue(tripped3)
        self.assertTrue(cb.is_tripped())
        st = self.storage.get_circuit_breaker_state()
        self.assertEqual(st["is_tripped"], 1)
        self.assertIn("3 consecutive", st["tripped_reason"])

    # 7. Circuit Breaker Rate Limits Admin Alerts to 1 Per Hour
    async def test_circuit_breaker_rate_limits_admin_alerts_to_one_per_hour(self):
        """Failure alert sent on first failure, rate-limited to at most once per hour."""
        alerts = []
        cb = CircuitBreaker(storage=self.storage, alert_callback=lambda m: alerts.append(m) or asyncio.sleep(0))
        exc_500 = urllib.error.HTTPError(url="http://test", code=500, msg="Internal Server Error", hdrs={}, fp=None)

        # 1st failure: sends alert
        await cb.handle_error(exc_500)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Sweep Failure", alerts[0])

        # 2nd failure within 1 hour: no new alert
        await cb.handle_error(exc_500)
        self.assertEqual(len(alerts), 1)

        # Fast forward last_failure_alert_at by 2 hours in DB
        old_alert_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.storage.update_circuit_breaker(last_failure_alert_at=old_alert_time)

        # Another failure: now sends alert again
        await cb.handle_error(exc_500)
        self.assertEqual(len(alerts), 2)

    # 8. Resume Command Clears Tripped State
    async def test_circuit_breaker_resume_command_clears_tripped_state(self):
        """resume() resets tripped state, consecutive failures, and reason."""
        cb = CircuitBreaker(storage=self.storage)
        exc_403 = urllib.error.HTTPError(url="http://test", code=403, msg="Forbidden", hdrs={}, fp=None)
        await cb.handle_error(exc_403)
        self.assertTrue(cb.is_tripped())

        cb.resume()
        self.assertFalse(cb.is_tripped())
        st = self.storage.get_circuit_breaker_state()
        self.assertEqual(st["is_tripped"], 0)
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertIsNone(st["tripped_reason"])

    # 9. Shallow-Miss Detector
    async def test_shallow_miss_detection_and_alert(self):
        """Detects and alerts if full sweep finds a new vacancy beyond page 2."""
        from jobwatch.sweep import SweepCoordinator
        scope = "Departments=9"
        self._mark_scope_baselined(scope, baseline_run_id=1)

        soup = BeautifulSoup(self.sample_html, "html.parser")
        base_card = soup.find("div", class_="job-card")

        def make_page(job_ids, total=25):
            cards = []
            for jid in job_ids:
                c = copy.deepcopy(base_card)
                fav = c.find("div", class_="job-card__favourite")
                fav["data-jobid"] = str(jid)
                title_link = c.find("a", href=lambda h: h and "/Job/JobDetail?JobId=" in h)
                title_link["href"] = f"/Job/JobDetail?JobId={jid}"
                title_link.string = f"Staff Nurse {jid}"
                cards.append(str(c))
            return (
                f'<div><input type="hidden" id="totalMatchRecords" value="{total}">'
                f'<input type="hidden" id="totalCurrentRecords" value="{len(job_ids)}">'
                f'<div class="search-results">{"".join(cards)}</div></div>'
            )

        page1 = make_page(list(range(201, 213)), 25)
        page2 = make_page(list(range(213, 225)), 25)
        page3 = make_page([999888], 25)
        page_term = (
            '<div><input type="hidden" id="totalMatchRecords" value="0">'
            '<input type="hidden" id="totalCurrentRecords" value="0">'
            '<div class="search-results"></div></div>'
        )

        def mock_pages(scope_key, skip):
            if skip == 0:
                return page1
            elif skip == 12:
                return page2
            elif skip == 24:
                return page3
            return page_term

        coordinator = SweepCoordinator(storage=self.storage, page_source=mock_pages)
        res = coordinator.execute_sweep(scope_key=scope, run_type="full_sweep")
        self.assertEqual(res.outcome, "success")
        self.assertEqual(len(res.shallow_misses), 1)
        self.assertEqual(res.shallow_misses[0]["job_id"], 999888)
        self.assertEqual(res.shallow_misses[0]["page_no"], 3)

        # Verify recorded in DB
        cnt = self.storage.get_shallow_misses_count()
        self.assertEqual(cnt, 1)

    # 10. Closing Reminders & Bank Post Exclusion
    async def test_closing_reminders_filters_bank_posts_and_triggers_at_5_and_1_days(self):
        """Closing reminders trigger at 5 and 1 days, and skip far-future (>60d) bank posts."""
        today = datetime(2026, 11, 15, tzinfo=timezone.utc)
        today_str = "2026-11-15"

        for jid, c_date, title in [
            (101, "2026-11-20", "Staff Nurse Post A"),
            (102, "2026-11-16", "Staff Nurse Post B"),
            (103, "2027-03-15", "Bank Staff Nurse Post C"),
        ]:
            self.storage.conn.execute(
                """INSERT INTO vacancies (job_id, detail_url, title_raw, title, closing_date_raw, closing_date, first_seen_at, last_seen_at, is_listed, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, '2026-11-01', '2026-11-15', 1, 'h1');""",
                (jid, f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={jid}", title, title, c_date, c_date),
            )
            self.storage.record_decision(
                job_id=jid, filter_version="1.1", outcome="strong", matched_rule="strong_rule",
                match_reason="test", decided_at="2026-11-01"
            )
            self.storage.set_user_status(jid, "new", None, "2026-11-01T00:00:00Z", user_id="shared")

        due = find_due_closing_reminders(self.storage, today_str)
        due_jids = [d["job_id"] for d in due]
        self.assertIn(101, due_jids)
        self.assertIn(102, due_jids)
        self.assertNotIn(103, due_jids)  # Bank post filtered out

        # Test executing run_closing_reminders via AsyncScheduler
        scheduler = AsyncScheduler(
            storage=self.storage,
            bot=self.bot,
            allowed_chat_ids=[12345],
            admin_chat_id=12345,
            clock=lambda: today,
        )
        count = await scheduler.run_closing_reminders()
        self.assertEqual(count, 2)
        # Check reminder milestones recorded in DB
        self.assertTrue(self.storage.has_reminder_been_sent(101, 5))
        self.assertTrue(self.storage.has_reminder_been_sent(102, 1))

    # 11. Snooze Wakeups
    async def test_snooze_wakeups_dispatch_reminder_alerts(self):
        """Wakes up snoozed vacancy when snooze_until expires and dispatches reminder alert."""
        jid = 555
        self.storage.conn.execute(
            """INSERT INTO vacancies (job_id, detail_url, title_raw, title, closing_date_raw, closing_date, first_seen_at, last_seen_at, is_listed, content_hash)
               VALUES (?, 'https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=555', 'Snoozed Post', 'Snoozed Post', '2026-12-01', '2026-12-01', '2026-11-01', '2026-11-15', 1, 'h555');""",
            (jid,),
        )
        self.storage.set_user_status(jid, "new", None, "2026-11-01T00:00:00Z", user_id="shared")
        # Set status to snoozed until 1 hour ago
        past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.storage.snooze_vacancy(jid, past_iso, user_id="shared")

        scheduler = AsyncScheduler(
            storage=self.storage,
            bot=self.bot,
            allowed_chat_ids=[12345],
            admin_chat_id=12345,
        )
        woken_count = await scheduler.run_snooze_wakeups()
        self.assertEqual(woken_count, 1)

        # Status returned to 'new'
        st = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = ? AND user_id = ?;",
            (jid, "shared"),
        ).fetchone()
        self.assertEqual(st["status"], "new")
        # Bot delivered alert
        self.assertTrue(any("SNOOZE EXPIRED" in m["text"] for m in self.bot.sent_messages))

    # 12. Night Hours and Mute Notification Settings
    def test_night_hours_sets_disable_notification_true(self):
        """22:00 to 07:00 London time returns is_night_hours() True."""
        scheduler = AsyncScheduler(storage=self.storage)

        dt_2300 = datetime(2026, 11, 15, 23, 30, tzinfo=LONDON_TZ)
        dt_0300 = datetime(2026, 11, 15, 3, 15, tzinfo=LONDON_TZ)
        dt_1400 = datetime(2026, 11, 15, 14, 0, tzinfo=LONDON_TZ)

        self.assertTrue(scheduler.is_night_hours(dt_2300))
        self.assertTrue(scheduler.is_night_hours(dt_0300))
        self.assertFalse(scheduler.is_night_hours(dt_1400))

    # 13. SQLite Native Database Backup
    def test_database_backup_creates_sqlite_backup(self):
        """Storage.backup_database creates a valid SQLite database with WAL consistency."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_path = self.storage.backup_database(backup_dir=tmp_dir, keep=3)
            self.assertTrue(os.path.exists(backup_path))
            self.assertTrue(backup_path.endswith(".db"))

            # Connect to the backup and verify tables and schema version
            bak_conn = sqlite3.connect(backup_path)
            ver = bak_conn.execute("PRAGMA user_version;").fetchone()[0]
            tables = [r[0] for r in bak_conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
            bak_conn.close()

            self.assertEqual(ver, 4)
            self.assertIn("vacancies", tables)
            self.assertIn("circuit_breaker", tables)

    # 14. Admin /probe Command
    async def test_admin_probe_command_executes_single_request(self):
        """Admin /probe performs exactly 1 fetch and reports card count; non-admin is refused."""
        from jobwatch.bot import cmd_probe

        # 1. Non-admin chat
        update_non_admin = MagicMock()
        update_non_admin.effective_chat.id = 99999
        context = MagicMock()
        context.bot_data = {"admin_id": 12345}

        await cmd_probe(update_non_admin, context)
        update_non_admin.message.reply_text.assert_not_called()

        # 2. Admin chat
        update_admin = MagicMock()
        update_admin.effective_chat.id = 12345
        status_msg = AsyncMock()
        update_admin.message.reply_text = AsyncMock(return_value=status_msg)

        with patch("jobwatch.fetcher.Fetcher.fetch_url", return_value=self.sample_html) as mock_fetch:
            await cmd_probe(update_admin, context)
            self.assertEqual(mock_fetch.call_count, 1)
            status_msg.edit_text.assert_called_once()
            call_args = status_msg.edit_text.call_args[0][0]
            self.assertIn("NHS Scotland Probe Succeeded", call_args)
            self.assertIn("Cards on Page", call_args)

    # 15. Admin /baseline Command
    async def test_admin_baseline_command_executes_silent_sweep(self):
        """Admin /baseline runs silent baseline for valid scope; non-admin is refused."""
        from jobwatch.bot import cmd_baseline

        context = MagicMock()
        context.bot_data = {"admin_id": 12345}

        # Non-admin
        update_non_admin = MagicMock()
        update_non_admin.effective_chat.id = 99999
        await cmd_baseline(update_non_admin, context)
        update_non_admin.message.reply_text.assert_not_called()

        # Admin without args -> prints usage
        update_admin = MagicMock()
        update_admin.effective_chat.id = 12345
        update_admin.message.reply_text = AsyncMock()
        context.args = []
        await cmd_baseline(update_admin, context)
        call_text = update_admin.message.reply_text.call_args[0][0]
        self.assertIn("Usage:", call_text)

        # Admin with valid unbaselined scope using a temporary file DB so threads have independent connections
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_db = os.path.join(tmp_dir, "test_jobwatch.db")
            init_s = Storage(test_db)
            init_s.close()

            context.args = ["Departments=9"]
            status_msg = AsyncMock()
            update_admin.message.reply_text = AsyncMock(return_value=status_msg)

            def mock_fetch(scope_key, skip):
                if skip == 0:
                    return self.sample_html
                return self.empty_html

            with patch("jobwatch.fetcher.Fetcher.fetch_scope", side_effect=mock_fetch), \
                 patch.dict(os.environ, {"JOBWATCH_DB_PATH": test_db}):
                await cmd_baseline(update_admin, context)
                status_msg.edit_text.assert_called_once()
                baseline_result_text = status_msg.edit_text.call_args[0][0]
                self.assertIn("Baseline Complete", baseline_result_text)
                self.assertIn("Zero alerts were sent", baseline_result_text)

                chk_s = Storage(test_db)
                meta = chk_s.get_scope_metadata("Departments=9")
                chk_s.close()
                self.assertTrue(meta["is_baselined"])


if __name__ == "__main__":
    unittest.main()
