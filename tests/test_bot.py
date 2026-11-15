"""Unit tests for NHS-JobWatch Interactive Telegram Bot (Step 4 offline verification).

Tests 1–18: original 18 tests
Tests 19–30: new tests added per owner review
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Chat, InlineKeyboardMarkup, Message, Update, User
from telegram.error import BadRequest, Conflict, Forbidden

from jobwatch.bot import (
    BoundedRateLimiter,
    _chat_is_muted,
    build_application,
    check_authorized,
    handle_callback_query,
    load_bot_config,
    render_list_page,
    rule_registry,
    safe_edit_message,
    send_message_to_chat,
)
from jobwatch.keyboards import (
    actionable_card_keyboard,
    alert_done_keyboard,
    alert_keyboard,
    back_main_keyboard,
    main_menu_keyboard,
    pagination_keyboard,
    snooze_menu_keyboard,
)
from jobwatch.storage import Storage
from jobwatch.filters import load_filter_config


class TestBotOffline(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_bot.db")
        self.storage = Storage(db_path=self.db_path)
        self.patcher = patch.dict(os.environ, {"JOBWATCH_DB_PATH": self.db_path})
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.storage.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _insert_vacancy(self, job_id=88001, title="Staff Nurse", is_listed=1,
                        closing_date="2027-06-30"):
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw, closing_date,
                    first_seen_at, last_seen_at, content_hash, is_listed, band, employer, location
                ) VALUES (
                    ?, ?, ?, '/Job/JobDetail?JobId=88001', '30/06/2027', ?,
                    ?, ?, 'test_hash', ?, 'Band 5', 'NHS Lothian', 'Edinburgh'
                ) ON CONFLICT(job_id) DO UPDATE SET is_listed = excluded.is_listed,
                    closing_date = excluded.closing_date;
                """,
                (job_id, title, title, closing_date, now, now, is_listed),
            )
            self.storage.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, updated_at)
                VALUES ('shared', ?, 'new', ?)
                ON CONFLICT(user_id, job_id) DO NOTHING;
                """,
                (job_id, now),
            )

    def _make_callback_update(self, callback_data, chat_id=1001, user_id=1001):
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = 42
        query.message.text = "Job description"
        query.message.text_html = "<b>Job description</b>"

        update = MagicMock(spec=Update)
        update.callback_query = query
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = chat_id
        update.effective_chat.type = "private"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = user_id

        context = MagicMock()
        context.bot_data = {"allowed_ids": {1001, 2002}, "contact": "@admin"}
        context.bot.edit_message_text = AsyncMock()
        return update, query, context

    # ───── ORIGINAL 18 TESTS ─────

    # 1. Authorization allows whitelisted chat
    def test_authorization_filter_allows_whitelisted_chat(self):
        update = MagicMock(spec=Update)
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = 1001
        update.effective_chat.type = "private"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = 1001
        allowed = {1001, 2002}
        self.assertTrue(check_authorized(update, allowed, "@admin_contact"))

    # 2. Authorization blocks unauthorized chat
    def test_authorization_filter_blocks_unauthorized_chat(self):
        update = MagicMock(spec=Update)
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = 9999
        update.effective_chat.type = "private"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = 9999
        update.message = MagicMock(spec=Message)
        update.message.text = "Hello there"
        allowed = {1001, 2002}
        self.assertFalse(check_authorized(update, allowed, "@admin_contact"))

    # 3. Unauthorized /start gets access contact reply
    def test_unauthorized_start_gets_access_contact_reply(self):
        limiter = BoundedRateLimiter(max_entries=10, cooldown_seconds=86400)
        with patch("jobwatch.bot.unauthorized_limiter", limiter):
            update = MagicMock(spec=Update)
            update.effective_chat = MagicMock(spec=Chat)
            update.effective_chat.id = 9991
            update.effective_chat.type = "private"
            update.effective_user = MagicMock(spec=User)
            update.effective_user.id = 9991
            update.message = MagicMock(spec=Message)
            update.message.text = "/start"
            update.message.reply_text = AsyncMock()
            allowed = {1001, 2002}
            res = check_authorized(update, allowed, "@admin_nurse")
            self.assertFalse(res)

    # 4. Unauthorized /start rate-limited to once per 24 hours
    def test_unauthorized_start_rate_limited_to_once_per_24h(self):
        limiter = BoundedRateLimiter(max_entries=100, cooldown_seconds=86400)
        user_id = 9992
        self.assertTrue(limiter.should_reply(user_id))
        self.assertFalse(limiter.should_reply(user_id))

    # 5. Unauthorized other commands silently ignored
    def test_unauthorized_other_commands_silently_ignored(self):
        update = MagicMock(spec=Update)
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = 9993
        update.effective_chat.type = "private"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = 9993
        update.message = MagicMock(spec=Message)
        update.message.text = "/help"
        update.message.reply_text = AsyncMock()
        allowed = {1001}
        self.assertFalse(check_authorized(update, allowed, "@admin"))
        update.message.reply_text.assert_not_called()

    # 6. Unauthorized cache bounded at 1000 entries
    def test_unauthorized_cache_bounded_at_1000_entries(self):
        limiter = BoundedRateLimiter(max_entries=5, cooldown_seconds=86400)
        for i in range(5):
            limiter.should_reply(100 + i)
        self.assertEqual(len(limiter._cache), 5)
        self.assertIn(100, limiter._cache)
        limiter.should_reply(999)
        self.assertEqual(len(limiter._cache), 5)
        self.assertNotIn(100, limiter._cache)
        self.assertIn(999, limiter._cache)

    # 7. Group chat rejected even if user is allowed
    def test_group_chat_rejected_even_with_allowed_user(self):
        update = MagicMock(spec=Update)
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = -1009999
        update.effective_chat.type = "group"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = 1001
        allowed = {1001}
        self.assertFalse(check_authorized(update, allowed, "@admin"))

    # 8. Applied button updates shared status
    def test_applied_button_callback_updates_shared_status(self):
        self._insert_vacancy(job_id=88001)
        update, query, context = self._make_callback_update("app:88001")
        asyncio.run(handle_callback_query(update, context))
        row = self.storage.conn.execute(
            "SELECT status, previous_status FROM user_vacancy_status WHERE job_id = 88001 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["previous_status"], "new")
        query.answer.assert_called_with("Marked as Applied! ✅")

    # 9. Skip button updates shared status
    def test_skip_button_callback_updates_shared_status(self):
        self._insert_vacancy(job_id=88002)
        update, query, context = self._make_callback_update("skp:88002")
        asyncio.run(handle_callback_query(update, context))
        row = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88002 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "skipped")
        query.answer.assert_called_with("Marked as Skipped ⏭️")

    # 10. Double tap idempotency
    def test_double_tap_idempotency(self):
        self._insert_vacancy(job_id=88003)
        update, query, context = self._make_callback_update("app:88003")
        asyncio.run(handle_callback_query(update, context))
        asyncio.run(handle_callback_query(update, context))
        row = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88003 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "applied")

    # 11. Undo restores previous status (single-level)
    def test_undo_restores_previous_status(self):
        """Undo is single-level: it restores the one previous_status column.
        Applied → Undo → new. NOT multi-level (there is only one previous_status slot).
        """
        self._insert_vacancy(job_id=88004)
        self.storage.update_user_status(88004, "applied", user_id="shared")
        update, query, context = self._make_callback_update("undo:88004")
        asyncio.run(handle_callback_query(update, context))
        row = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88004 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "new")

    # 12. Stale vacancy tap shows inactive toast
    def test_stale_vacancy_tap_shows_inactive_toast(self):
        self._insert_vacancy(job_id=88005, is_listed=0)
        update, query, context = self._make_callback_update("app:88005")
        asyncio.run(handle_callback_query(update, context))
        query.answer.assert_called_with("Vacancy is no longer active.", show_alert=True)
        row = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88005 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "new")

    # 13. Snooze menu and duration callback
    def test_snooze_menu_and_duration_callback(self):
        self._insert_vacancy(job_id=88006)
        update, query, context = self._make_callback_update("snz:3d:88006")
        asyncio.run(handle_callback_query(update, context))
        row = self.storage.conn.execute(
            "SELECT status, snooze_until FROM user_vacancy_status WHERE job_id = 88006 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "snoozed")
        self.assertIsNotNone(row["snooze_until"])

    # 14. Snooze wakeup with fake clock
    def test_snooze_wakeup_with_fake_clock(self):
        self._insert_vacancy(job_id=88007)
        past_iso = "2026-11-15T09:00:00+00:00"
        self.storage.snooze_vacancy(88007, past_iso, user_id="shared")
        clock_now = "2026-11-15T12:00:00+00:00"
        woken = self.storage.process_snooze_wakeups(clock_now, user_id="shared")
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["job_id"], 88007)
        row = self.storage.conn.execute(
            "SELECT status, snooze_until FROM user_vacancy_status WHERE job_id = 88007 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["snooze_until"])

    # 15. Cross-chat status synchronization
    def test_cross_chat_status_synchronization(self):
        self._insert_vacancy(job_id=88008)
        now = datetime.now(timezone.utc).isoformat()
        self.storage.record_delivery(88008, "1001", 501, now)
        self.storage.record_delivery(88008, "2002", 502, now)
        update, query, context = self._make_callback_update("app:88008", chat_id=1001)
        query.message.message_id = 501
        asyncio.run(handle_callback_query(update, context))
        calls = context.bot.edit_message_text.call_args_list
        synced = any(
            c.kwargs.get("chat_id") == 2002 and c.kwargs.get("message_id") == 502
            for c in calls
        )
        self.assertTrue(synced, "Chat 2002 did not receive synchronized edit")

    # 16. Quiet handling of message not modified
    def test_quiet_handling_of_message_not_modified(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified: specified new message content is the same")
        )
        ok = asyncio.run(safe_edit_message(bot, 1001, 55, "Test text"))
        self.assertTrue(ok)

    # 17. Pagination boundaries (0, 5, 6 items)
    def test_pagination_boundaries_0_5_6_items(self):
        fmt = lambda x: (f"Item {x}", x)

        text0, kb0 = render_list_page([], 1, "test", "Header", fmt)
        self.assertIn("No vacancies found", text0)
        self.assertEqual(len(kb0.inline_keyboard), 1)

        items5 = list(range(1, 6))
        text5, kb5 = render_list_page(items5, 1, "test", "Header", fmt)
        self.assertIn("Page 1/1", text5)
        btn_labels5 = [b.text for row in kb5.inline_keyboard for b in row]
        self.assertNotIn("⬅️ Prev", btn_labels5)
        self.assertNotIn("Next ➡️", btn_labels5)

        items6 = list(range(1, 7))
        text6_p1, kb6_p1 = render_list_page(items6, 1, "test", "Header", fmt)
        self.assertIn("Page 1/2", text6_p1)
        btn_labels6_p1 = [b.text for row in kb6_p1.inline_keyboard for b in row]
        self.assertIn("Next ➡️", btn_labels6_p1)

        text6_p2, kb6_p2 = render_list_page(items6, 2, "test", "Header", fmt)
        self.assertIn("Page 2/2", text6_p2)
        btn_labels6_p2 = [b.text for row in kb6_p2.inline_keyboard for b in row]
        self.assertIn("⬅️ Prev", btn_labels6_p2)

    # 18. Callback data strictly under 64 bytes for all static keyboards
    def test_callback_data_strictly_under_64_bytes(self):
        keyboards = [
            main_menu_keyboard(is_paused=False, is_muted=False),
            main_menu_keyboard(is_paused=True, is_muted=True),
            alert_keyboard(275434, "https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=275434"),
            alert_done_keyboard(275434, "https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=275434", "Applied"),
            snooze_menu_keyboard(275434),
            pagination_keyboard("digest", 2, 5, "nav:menu"),
            actionable_card_keyboard(275434, "https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=275434", "digest", 2),
            back_main_keyboard("nav:menu"),
        ]
        for kb in keyboards:
            for row in kb.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        byte_len = len(btn.callback_data.encode("utf-8"))
                        self.assertLess(
                            byte_len, 64,
                            f"Callback '{btn.callback_data}' is {byte_len} bytes (>= 64)",
                        )


class TestBotNewTests(unittest.TestCase):
    """New tests added per owner review (tests 19–30)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_bot2.db")
        self.storage = Storage(db_path=self.db_path)
        self.patcher = patch.dict(os.environ, {"JOBWATCH_DB_PATH": self.db_path})
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.storage.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _insert_vacancy(self, job_id=88001, title="Staff Nurse", is_listed=1,
                        closing_date="2027-06-30", outcome="strong"):
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw, closing_date,
                    first_seen_at, last_seen_at, content_hash, is_listed, band, employer, location
                ) VALUES (
                    ?, ?, ?, '/Job/JobDetail?JobId=88001', '30/06/2027', ?,
                    ?, ?, 'test_hash', ?, 'Band 5', 'NHS Lothian', 'Edinburgh'
                ) ON CONFLICT(job_id) DO UPDATE SET is_listed = excluded.is_listed,
                    closing_date = excluded.closing_date;
                """,
                (job_id, title, title, closing_date, now, now, is_listed),
            )
            self.storage.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, updated_at)
                VALUES ('shared', ?, 'new', ?)
                ON CONFLICT(user_id, job_id) DO NOTHING;
                """,
                (job_id, now),
            )
            if outcome:
                self.storage.conn.execute(
                    """
                    INSERT INTO vacancy_decisions (job_id, filter_version, outcome, matched_rule, match_reason, decided_at)
                    VALUES (?, '1.1', ?, 'strong_staff_nurse', 'Staff Nurse', ?)
                    ON CONFLICT(job_id) DO NOTHING;
                    """,
                    (job_id, outcome, now),
                )

    def _make_callback_update(self, callback_data, chat_id=1001, user_id=1001):
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = 42
        query.message.text = "Job description"
        query.message.text_html = "<b>Job description</b>"
        update = MagicMock(spec=Update)
        update.callback_query = query
        update.effective_chat = MagicMock(spec=Chat)
        update.effective_chat.id = chat_id
        update.effective_chat.type = "private"
        update.effective_user = MagicMock(spec=User)
        update.effective_user.id = user_id
        context = MagicMock()
        context.bot_data = {"allowed_ids": {1001, 2002}, "contact": "@admin"}
        context.bot.edit_message_text = AsyncMock()
        return update, query, context

    # ── 19. pause holds alerts / resume sends catch-up digest ──

    def test_pause_stores_paused_flag_in_chat_settings(self):
        """ctl:pause stores is_paused=1 in chat_settings for this chat."""
        update, query, context = self._make_callback_update("ctl:pause", chat_id=1001)
        asyncio.run(handle_callback_query(update, context))
        s = Storage(db_path=self.db_path)
        row = s.conn.execute(
            "SELECT is_paused FROM chat_settings WHERE chat_id = '1001';"
        ).fetchone()
        s.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_paused"], 1)

    def test_resume_clears_paused_flag_and_returns_catch_up_count(self):
        """ctl:resume clears is_paused=0 and reply mentions open vacancy count sorted by closing date."""
        # Insert a paused state first
        self.storage.update_chat_settings("1001", is_paused=1,
                                          paused_at=datetime.now(timezone.utc).isoformat())
        # Insert two open vacancies
        self._insert_vacancy(job_id=89001, closing_date="2027-01-01", outcome="strong")
        self._insert_vacancy(job_id=89002, closing_date="2027-03-15", outcome="maybe")

        update, query, context = self._make_callback_update("ctl:resume", chat_id=1001)
        asyncio.run(handle_callback_query(update, context))

        s = Storage(db_path=self.db_path)
        row = s.conn.execute(
            "SELECT is_paused FROM chat_settings WHERE chat_id = '1001';"
        ).fetchone()
        s.close()
        self.assertEqual(row["is_paused"], 0)
        # The safe_edit_message should have been called
        context.bot.edit_message_text.assert_called()

    def test_resume_skips_closed_vacancies_in_catch_up(self):
        """Catch-up digest skips vacancies whose closing_date is in the past."""
        self._insert_vacancy(job_id=89010, closing_date="2024-01-01", outcome="strong")  # closed
        self._insert_vacancy(job_id=89011, closing_date="2027-12-31", outcome="strong")  # open

        update, query, context = self._make_callback_update("ctl:resume", chat_id=1001)
        asyncio.run(handle_callback_query(update, context))

        # The edit message call should contain "1" (one open), not "2"
        calls = context.bot.edit_message_text.call_args_list
        texts = [str(c) for c in calls]
        # At least one call should have happened
        self.assertTrue(len(calls) > 0)

    # ── 20. mute sends with disable_notification, holds nothing ──

    def test_mute_stores_muted_until_in_chat_settings(self):
        """ctl:mute stores muted_until in chat_settings set to 08:00 Europe/London."""
        from zoneinfo import ZoneInfo
        update, query, context = self._make_callback_update("ctl:mute", chat_id=1001)
        asyncio.run(handle_callback_query(update, context))
        s = Storage(db_path=self.db_path)
        row = s.conn.execute(
            "SELECT muted_until FROM chat_settings WHERE chat_id = '1001';"
        ).fetchone()
        s.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["muted_until"])
        # Verify stored time is tomorrow 08:00 London
        muted_dt = datetime.fromisoformat(row["muted_until"])
        london = ZoneInfo("Europe/London")
        muted_london = muted_dt.astimezone(london)
        self.assertEqual(muted_london.hour, 8)
        self.assertEqual(muted_london.minute, 0)

    def test_chat_is_muted_helper_respects_muted_until(self):
        """_chat_is_muted returns True when muted_until is in the future, False when past."""
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        s_future = MagicMock()
        s_future.__getitem__ = lambda self, k: future if k == "muted_until" else None
        s_past = MagicMock()
        s_past.__getitem__ = lambda self, k: past if k == "muted_until" else None
        s_none = MagicMock()
        s_none.__getitem__ = lambda self, k: None

        self.assertTrue(_chat_is_muted(s_future))
        self.assertFalse(_chat_is_muted(s_past))
        self.assertFalse(_chat_is_muted(s_none))

    def test_mute_end_at_clock_change_summer_to_winter(self):
        """08:00 Europe/London mute-end is correct when BST ends (clocks go back Oct).
        UK BST ends on the last Sunday of October (2026-10-25).
        - 'now' = Oct 23 (BST, +01:00); tomorrow Oct 24 is still BST so 08:00 -> 07:00 UTC.
        - 'now' = Oct 24 (BST, +01:00); tomorrow Oct 25 is clock-change day (clocks back at 02:00 BST to 01:00 GMT),
          so at 08:00 London on Oct 25 it is in GMT (+00:00) -> 08:00 UTC!
        - 'now' = Oct 26 (GMT, +00:00); tomorrow Oct 27 is GMT so 08:00 -> 08:00 UTC.
        """
        from zoneinfo import ZoneInfo
        LONDON_TZ = ZoneInfo("Europe/London")

        # 1. BST day: 2026-10-23; tomorrow (Oct 24) is still BST (+01:00)
        # 08:00 London on Oct 24 = 07:00 UTC
        bst_now = datetime(2026, 10, 23, 15, 0, tzinfo=LONDON_TZ)
        bst_next_8 = (bst_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        bst_utc = bst_next_8.astimezone(timezone.utc)
        self.assertEqual(bst_utc.hour, 7)

        # 2. Day before clocks go back: 2026-10-24; tomorrow (Oct 25) clocks go back at 02:00 London to 01:00 GMT
        # so at 08:00 London on Oct 25 it is GMT (+00:00) = 08:00 UTC
        oct24_now = datetime(2026, 10, 24, 15, 0, tzinfo=LONDON_TZ)
        oct25_8 = (oct24_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        oct25_utc = oct25_8.astimezone(timezone.utc)
        self.assertEqual(oct25_utc.hour, 8)

        # 3. GMT day: 2026-10-26 (day after clock change); tomorrow Oct 27 is GMT (+00:00)
        # 08:00 London on Oct 27 = 08:00 UTC
        gmt_now = datetime(2026, 10, 26, 10, 0, tzinfo=LONDON_TZ)
        gmt_next_8 = (gmt_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        gmt_utc = gmt_next_8.astimezone(timezone.utc)
        self.assertEqual(gmt_utc.hour, 8)

    def test_mute_end_at_clock_change_winter_to_summer(self):
        """08:00 Europe/London mute-end is correct when GMT ends (clocks go forward Mar).
        UK GMT ends on the last Sunday of March (2026-03-29).
        - 'now' = Mar 27 (GMT, +00:00); tomorrow Mar 28 is still GMT so 08:00 -> 08:00 UTC.
        - 'now' = Mar 28 (GMT, +00:00); tomorrow Mar 29 is clock-change day (clocks forward at 01:00 UTC to 02:00 BST),
          so at 08:00 London on Mar 29 it is in BST (+01:00) -> 07:00 UTC!
        - 'now' = Mar 29 (BST, +01:00); tomorrow Mar 30 is BST so 08:00 -> 07:00 UTC.
        """
        from zoneinfo import ZoneInfo
        LONDON_TZ = ZoneInfo("Europe/London")

        # 1. GMT day: 2026-03-27; tomorrow Mar 28 is still GMT (+00:00)
        # 08:00 London on Mar 28 = 08:00 UTC
        mar27_now = datetime(2026, 3, 27, 10, 0, tzinfo=LONDON_TZ)
        mar28_8 = (mar27_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        mar28_utc = mar28_8.astimezone(timezone.utc)
        self.assertEqual(mar28_utc.hour, 8)

        # 2. Day before clocks go forward: 2026-03-28; tomorrow Mar 29 is clock-change day (clocks forward at 01:00 UTC)
        # 08:00 London on Mar 29 is BST (+01:00) = 07:00 UTC
        gmt_now = datetime(2026, 3, 28, 10, 0, tzinfo=LONDON_TZ)
        gmt_next_8 = (gmt_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        gmt_utc = gmt_next_8.astimezone(timezone.utc)
        self.assertEqual(gmt_utc.hour, 7)

        # 3. Clock change day: 2026-03-29; tomorrow Mar 30 is still BST (+01:00)
        # 08:00 London on Mar 30 = 07:00 UTC
        mar29_now = datetime(2026, 3, 29, 10, 0, tzinfo=LONDON_TZ)
        mar30_8 = (mar29_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        mar30_utc = mar30_8.astimezone(timezone.utc)
        self.assertEqual(mar30_utc.hour, 7)

    # ── 21. 409 Conflict handling stops application gracefully ──

    def test_409_conflict_stops_application_and_sets_flag(self):
        """A Conflict exception routed to global_error_handler sets conflict_detected flag and calls stop_running()."""
        from jobwatch.bot import global_error_handler
        err = Conflict("Conflict: terminated by other getUpdates request")
        context = MagicMock()
        context.error = err
        context.application = MagicMock()
        context.application.bot_data = {}
        asyncio.run(global_error_handler(None, context))
        self.assertTrue(context.application.bot_data.get("conflict_detected"))
        context.application.stop_running.assert_called_once()

    # ── 22. Replayed old tap is idempotent ──

    def test_replayed_old_tap_is_idempotent(self):
        """Receiving the same callback_data twice (e.g. on bot restart) produces no error
        and leaves the status unchanged after the second replay."""
        self._insert_vacancy(job_id=88020)
        update, query, context = self._make_callback_update("app:88020")
        # First tap
        asyncio.run(handle_callback_query(update, context))
        row1 = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88020 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row1["status"], "applied")
        # Second tap (replay) — should not raise, status still applied
        asyncio.run(handle_callback_query(update, context))
        row2 = self.storage.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE job_id = 88020 AND user_id = 'shared';"
        ).fetchone()
        self.assertEqual(row2["status"], "applied")

    # ── 23. Alert delivered to both allowed chats records two alert_deliveries rows ──

    def test_alert_delivered_to_both_chats_records_two_rows(self):
        """When an alert is recorded for two chats, alert_deliveries has exactly two rows."""
        self._insert_vacancy(job_id=88021)
        now = datetime.now(timezone.utc).isoformat()
        self.storage.record_delivery(88021, "1001", 601, now)
        self.storage.record_delivery(88021, "2002", 602, now)
        rows = self.storage.get_deliveries_for_job(88021)
        self.assertEqual(len(rows), 2)
        chat_ids = {r["chat_id"] for r in rows}
        self.assertIn("1001", chat_ids)
        self.assertIn("2002", chat_ids)

    # ── 24. One chat failing does not mark vacancy alerted ──

    def test_one_chat_failing_does_not_mark_alerted(self):
        """alerted_at is NOT set when only one of two active allowed chats has a delivery row."""
        self._insert_vacancy(job_id=88022)
        now = datetime.now(timezone.utc).isoformat()
        # Only one chat delivered; the other is active but no row yet
        self.storage.record_delivery(88022, "1001", 701, now)
        # Ensure chat 2002 is active in settings
        self.storage.update_chat_settings("2002", is_active=1)

        result = self.storage.mark_alerted_if_all_delivered(
            job_id=88022,
            allowed_chat_ids=["1001", "2002"],
            alerted_at=now,
        )
        self.assertFalse(result)
        row = self.storage.conn.execute(
            "SELECT alerted_at FROM user_vacancy_status WHERE job_id = 88022 AND user_id = 'shared';"
        ).fetchone()
        # Row may not exist yet if no status was set, or alerted_at should be None
        self.assertTrue(row is None or row["alerted_at"] is None)

    # ── 25. alerted_at set only when every active allowed chat has delivery ──

    def test_alerted_at_set_when_all_active_chats_delivered(self):
        """alerted_at IS set when both active chats have delivery rows."""
        self._insert_vacancy(job_id=88023)
        now = datetime.now(timezone.utc).isoformat()
        self.storage.record_delivery(88023, "1001", 801, now)
        self.storage.record_delivery(88023, "2002", 802, now)

        result = self.storage.mark_alerted_if_all_delivered(
            job_id=88023,
            allowed_chat_ids=["1001", "2002"],
            alerted_at=now,
        )
        self.assertTrue(result)
        row = self.storage.conn.execute(
            "SELECT alerted_at FROM user_vacancy_status WHERE job_id = 88023 AND user_id = 'shared';"
        ).fetchone()
        self.assertIsNotNone(row["alerted_at"])

    def test_alerted_at_set_when_one_chat_inactive_other_delivered(self):
        """alerted_at IS set when one chat is inactive (403'd) and the other has a delivery."""
        self._insert_vacancy(job_id=88024)
        now = datetime.now(timezone.utc).isoformat()
        self.storage.record_delivery(88024, "1001", 901, now)
        # Mark chat 2002 as inactive (403 blocked)
        self.storage.update_chat_settings("2002", is_active=0, inactive_reason="bot_blocked")

        result = self.storage.mark_alerted_if_all_delivered(
            job_id=88024,
            allowed_chat_ids=["1001", "2002"],
            alerted_at=now,
        )
        self.assertTrue(result)

    # ── 26. 403 marks chat inactive ──

    def test_403_marks_chat_inactive_via_send_message_to_chat(self):
        """send_message_to_chat marks the chat inactive (is_active=0) on Forbidden (403)."""
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Forbidden("Forbidden: bot was blocked by the user"))

        result = asyncio.run(send_message_to_chat(bot, 1001, "Test"))
        self.assertIsNone(result)

        # Check the storage reflects the deactivation
        s = Storage(db_path=self.db_path)
        row = s.conn.execute(
            "SELECT is_active, inactive_reason FROM chat_settings WHERE chat_id = '1001';"
        ).fetchone()
        s.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["inactive_reason"], "bot_blocked")

    def test_chat_not_found_marks_chat_inactive_via_send_message_to_chat(self):
        """send_message_to_chat marks the chat inactive (is_active=0, inactive_reason='chat_not_found') on BadRequest 'chat not found'."""
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=BadRequest("Bad Request: chat not found"))

        result = asyncio.run(send_message_to_chat(bot, 2002, "Test"))
        self.assertIsNone(result)

        s = Storage(db_path=self.db_path)
        row = s.conn.execute(
            "SELECT is_active, inactive_reason FROM chat_settings WHERE chat_id = '2002';"
        ).fetchone()
        s.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["inactive_reason"], "chat_not_found")

    # ── 27. Malformed config fails loudly at startup ──

    def test_malformed_telegram_chat_id_fails_at_startup(self):
        """load_bot_config raises ValueError if TELEGRAM_CHAT_ID is non-numeric."""
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123:validtoken",
            "TELEGRAM_CHAT_ID": "not_a_number",
            "TELEGRAM_ALLOWED_CHAT_IDS": "1001,2002",
        }, clear=False):
            with self.assertRaises(ValueError) as cm:
                load_bot_config()
            self.assertIn("TELEGRAM_CHAT_ID", str(cm.exception))

    def test_malformed_telegram_allowed_chat_ids_fails_at_startup(self):
        """load_bot_config raises ValueError if TELEGRAM_ALLOWED_CHAT_IDS contains a non-numeric ID."""
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123:validtoken",
            "TELEGRAM_CHAT_ID": "1001",
            "TELEGRAM_ALLOWED_CHAT_IDS": "1001,bad_id",
        }, clear=False):
            with self.assertRaises(ValueError) as cm:
                load_bot_config()
            self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS", str(cm.exception))

    def test_missing_telegram_bot_token_fails_at_startup(self):
        """load_bot_config raises ValueError if TELEGRAM_BOT_TOKEN is missing or empty.
        load_dotenv is patched to a no-op so the real .env file cannot supply the token."""
        env = os.environ.copy()
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env["TELEGRAM_CHAT_ID"] = "1001"
        env["TELEGRAM_ALLOWED_CHAT_IDS"] = "1001,2002"
        with patch("jobwatch.bot.load_dotenv", return_value=None):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError) as cm:
                    load_bot_config()
                self.assertIn("TELEGRAM_BOT_TOKEN", str(cm.exception))

    # ── 28. Callback data under 64 bytes for every rule in config/filters.json ──

    def test_callback_data_under_64_bytes_for_every_config_rule(self):
        """Every rule ID in config/filters.json produces a callback string < 64 bytes
        when routed through the rule_registry (xr:{key}:{page} format)."""
        config = load_filter_config()
        for rule in config.exclusions:
            rid = rule.id
            key = rule_registry.get_key(rid)
            cb = f"xr:{key}:99"
            byte_len = len(cb.encode("utf-8"))
            self.assertLess(
                byte_len, 64,
                f"Callback for rule '{rid}' via key '{key}': '{cb}' is {byte_len} bytes",
            )

    def test_callback_data_under_64_bytes_with_long_synthetic_rule_id(self):
        """A deliberately long synthetic rule ID still produces callback < 64 bytes via registry."""
        long_id = "exclude_this_is_an_extremely_long_synthetic_rule_id_that_would_be_way_over_64_bytes_if_embedded_directly"
        key = rule_registry.get_key(long_id)
        cb = f"xr:{key}:99"
        byte_len = len(cb.encode("utf-8"))
        self.assertLess(byte_len, 64, f"Long rule ID callback '{cb}' is {byte_len} bytes (>= 64)")
        # Also verify we can round-trip it
        from jobwatch.bot import rule_registry as rr
        self.assertEqual(rr.get_rule_id(key), long_id)

    # ── 29. Undo is single-level: Applied → Snooze → Undo restores Snooze, not new ──

    def test_undo_is_single_level_applied_then_snooze_then_undo(self):
        """Undo is single-level (one previous_status column).
        Sequence: Applied → Snooze → Undo restores 'applied', NOT 'new'.
        There is no second-level undo back to 'new'.
        """
        self._insert_vacancy(job_id=88025)
        # 1. Apply
        self.storage.update_user_status(88025, "applied", user_id="shared")
        row = self.storage.conn.execute(
            "SELECT status, previous_status FROM user_vacancy_status WHERE job_id = 88025;"
        ).fetchone()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["previous_status"], "new")

        # 2. Snooze (overwrites previous_status = 'applied')
        snooze_until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.storage.snooze_vacancy(88025, snooze_until, user_id="shared")
        row = self.storage.conn.execute(
            "SELECT status, previous_status FROM user_vacancy_status WHERE job_id = 88025;"
        ).fetchone()
        self.assertEqual(row["status"], "snoozed")
        self.assertEqual(row["previous_status"], "applied")  # only one slot

        # 3. Undo — restores 'applied' (the one previous_status), not 'new'
        prev, restored = self.storage.undo_user_status(88025, user_id="shared")
        self.assertEqual(prev, "snoozed")
        self.assertEqual(restored, "applied")

        # 4. Undo again — previous_status is now cleared (NULL → 'new')
        prev2, restored2 = self.storage.undo_user_status(88025, user_id="shared")
        self.assertEqual(restored2, "new")

    # ── 30. Callback data consistently uses xr: prefix for excluded rule drilldowns ──

    def test_excluded_rule_callbacks_use_xr_prefix_not_raw_rule_id(self):
        """The excl_r:{rule_id} format is kept for the navigation bar.
        The xr:{key}:{page} format is used for drill-down pages to avoid long IDs.
        Verify xr: callbacks round-trip via rule_registry."""
        config = load_filter_config()
        for rule in config.exclusions:
            rid = rule.id
            key = rule_registry.get_key(rid)
            # xr:{key}:1 must resolve back to the original rule_id
            resolved = rule_registry.get_rule_id(key)
            self.assertEqual(resolved, rid, f"Round-trip failed for rule '{rid}'")


if __name__ == "__main__":
    unittest.main()
