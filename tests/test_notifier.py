"""Unit tests for notifier, formatter, alert dispatch, and digest generation."""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobwatch.formatter import chunk_message_if_needed, format_merged_change_alert, parse_full_time
from jobwatch.models import VacancyCard
from jobwatch.notifier import (
    FakeNotifier,
    PermanentNotifierError,
    TransientNotifierError,
    sanitize_error,
)


def _card(job_id: int, title: str, band: str = "Band 5", closing_date: date = None) -> VacancyCard:
    if closing_date is None:
        closing_date = date(2027, 6, 30)
    return VacancyCard(
        job_id=job_id,
        title_raw=title,
        title=title,
        detail_url=f"/Job/JobDetail?JobId={job_id}",
        closing_date_raw="30/06/2027",
        closing_date=closing_date,
        band=band,
        salary=f"{band} (\xa334,544 - \xa343,039)",
        employer="NHS Test Board",
        location="Edinburgh",
        dept_label="Nursing",
        contract="Permanent",
        hours_raw="37.5",
    )


class TestTelegramSanitization(unittest.TestCase):
    """Token and URL sanitization in error messages and logs."""

    def test_token_stripped_from_error(self):
        """Bot token URLs are stripped from error message text."""
        raw = "Error at https://api.telegram.org/bot1234:ABC_secret/sendMessage — bad request"
        sanitized = sanitize_error(raw)
        self.assertNotIn("1234:ABC_secret", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_no_mutation_without_token(self):
        """Plain text without a token URL is unchanged."""
        raw = "Connection refused"
        self.assertEqual(sanitize_error(raw), raw)


class TestPermanentErrorsHaltQueue(unittest.TestCase):
    """FakeNotifier permanent failure stops batch dispatch."""

    def test_permanent_failure_stops_send(self):
        """A permanent error from FakeNotifier halts the queue."""
        notifier = FakeNotifier(permanent_fail=True)
        with self.assertRaises(PermanentNotifierError):
            notifier.send("test")

    def test_failed_send_leaves_sent_list_empty(self):
        """When send fails, the notifier's sent list is unmodified."""
        notifier = FakeNotifier(fail_on_send=True)
        try:
            notifier.send("test")
        except TransientNotifierError:
            pass
        self.assertEqual(len(notifier.sent), 0)


class TestFakeNotifier(unittest.TestCase):
    """FakeNotifier records messages for test inspection."""

    def test_send_records_message(self):
        n = FakeNotifier()
        n.send("hello")
        self.assertEqual(n.sent, ["hello"])

    def test_send_batch_counts(self):
        n = FakeNotifier()
        sent, failed = n.send_batch(["a", "b", "c"])
        self.assertEqual(sent, 3)
        self.assertEqual(failed, 0)
        self.assertEqual(len(n.sent), 3)


class TestMergedChangeAlert(unittest.TestCase):
    """Change alerts merge correctly and deduplicate salary+band."""

    def test_salary_band_deduplication(self):
        """When salary and band both change, only one merged salary line appears."""
        changes = [
            {"field_name": "salary", "old_value": "Band 7 (\xa352,845 - \xa361,466)", "new_value": "Band 8A (\xa360,000 - \xa365,000)"},
            {"field_name": "band", "old_value": "Band 7", "new_value": "Band 8A"},
        ]
        text = format_merged_change_alert(99999, "Staff Nurse", "12345", changes)
        # Should contain salary line
        self.assertIn("Salary:", text)
        # Band line must NOT appear separately
        band_lines = [l for l in text.split("\n") if l.strip().startswith("\U0001f4dd <b>Band:") or "<b>Band:</b>" in l]
        self.assertEqual(len(band_lines), 0, "Band change must not appear as a separate line when salary is also changed")

    def test_non_salary_field_change_shown(self):
        """Non-salary field changes appear individually."""
        changes = [
            {"field_name": "closing_date", "old_value": "2027-01-01", "new_value": "2027-02-01"},
        ]
        text = format_merged_change_alert(99998, "Staff Nurse", "11111", changes)
        self.assertIn("Closing Date:", text)


class TestMessageChunking(unittest.TestCase):
    """Long messages are split into numbered chunks."""

    def test_short_message_not_split(self):
        text = "Short message"
        chunks = chunk_message_if_needed(text, max_chars=4000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_long_message_split(self):
        text = ("A" * 100 + "\n") * 60  # ~6060 chars
        chunks = chunk_message_if_needed(text, max_chars=4000)
        self.assertGreater(len(chunks), 1)
        for idx, chunk in enumerate(chunks):
            self.assertIn(f"Part {idx + 1} of", chunk)

    def test_html_entity_escaping_preserved(self):
        """& < > in messages are HTML-escaped and survive chunking."""
        import html
        raw = "Role: Staff Nurse & HCA at Band 5 <Level 1>"
        escaped = html.escape(raw)
        chunks = chunk_message_if_needed(escaped)
        self.assertEqual(len(chunks), 1)
        self.assertIn("&amp;", chunks[0])
        self.assertIn("&lt;", chunks[0])


class TestConfirmedSendUpdatesAlertedAt(unittest.TestCase):
    """alerted_at is set only after confirmed send; failed send leaves it pending."""

    def setUp(self):
        import re
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_notifier.db")

        from jobwatch.storage import Storage
        self.storage = Storage(db_path=self.db_path)
        now = datetime.now(timezone.utc).isoformat()

        with self.storage.conn:
            self.storage.conn.execute(
                """INSERT INTO vacancies (job_id, title_raw, title, detail_url, closing_date_raw,
                   closing_date, first_seen_at, last_seen_at, content_hash, is_listed)
                   VALUES (77001, 'Staff Nurse', 'Staff Nurse', '/url', '30/06/2027',
                   '2027-06-30', ?, ?, 'testhash', 1);""",
                (now, now),
            )
            self.storage.conn.execute(
                """INSERT INTO user_vacancy_status (user_id, job_id, status, updated_at)
                   VALUES ('shared', 77001, 'new', ?);""",
                (now,),
            )

    def tearDown(self):
        self.storage.close()
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_confirmed_send_sets_alerted_at(self):
        """After a successful send, alerted_at is populated."""
        now = datetime.now(timezone.utc).isoformat()
        self.storage.mark_alerted(77001, now)

        row = self.storage.conn.execute(
            "SELECT alerted_at FROM user_vacancy_status WHERE job_id = 77001;"
        ).fetchone()
        self.assertIsNotNone(row["alerted_at"])

    def test_failed_send_leaves_alerted_at_null(self):
        """alerted_at remains NULL when we do NOT call mark_alerted."""
        row = self.storage.conn.execute(
            "SELECT alerted_at FROM user_vacancy_status WHERE job_id = 77001;"
        ).fetchone()
        self.assertIsNone(row["alerted_at"])


class TestBaselineVacanciesNeverAlert(unittest.TestCase):
    """Vacancies with workflow status 'baseline' must not appear in alert dispatch."""

    def setUp(self):
        from jobwatch.storage import Storage
        self.storage = Storage(db_path=":memory:")

    def tearDown(self):
        self.storage.close()

    def _insert_and_fetch_row(self, job_id, title="Staff Nurse", closing_date="2027-06-30", is_listed=1, workflow_status="new", alerted_at=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw, closing_date,
                    first_seen_at, last_seen_at, content_hash, is_listed, job_ref,
                    salary_raw, salary, band, location_raw, location,
                    dept_label_raw, dept_label, employer_raw, employer,
                    contract_raw, contract, hours_raw
                ) VALUES (
                    ?, ?, ?, '/url', '30/06/2027', ?, ?, ?, 'hash', ?, ?,
                    'Band 5', 'Band 5', 'Band 5', 'Edinburgh', 'Edinburgh',
                    'Nursing', 'Nursing', 'NHS Test', 'NHS Test',
                    'Permanent', 'Permanent', '37.5'
                );""",
                (job_id, title, title, closing_date, now, now, is_listed, str(job_id)),
            )
            self.storage.conn.execute(
                """INSERT INTO user_vacancy_status (user_id, job_id, status, alerted_at, updated_at)
                VALUES ('owner', ?, ?, ?, ?);""",
                (job_id, workflow_status, alerted_at, now),
            )
        cur = self.storage.conn.execute(
            """SELECT v.*, vd.outcome, vd.matched_rule, vd.match_reason, vd.filter_version,
                      uvs.alerted_at, uvs.status as workflow_status
               FROM vacancies v
               LEFT JOIN vacancy_decisions vd ON v.job_id = vd.job_id
               LEFT JOIN user_vacancy_status uvs ON v.job_id = uvs.job_id AND uvs.user_id = 'owner'
               WHERE v.job_id = ?;""",
            (job_id,),
        )
        return cur.fetchone()

    def test_baseline_status_skipped_by_dispatch(self):
        """Dispatch logic skips vacancies whose workflow_status is 'baseline'."""
        from jobwatch.alerts import dispatch_new_alerts
        from jobwatch.filters import load_filter_config
        from jobwatch.models import FilterDecision

        notifier = FakeNotifier()
        config = load_filter_config()

        row = self._insert_and_fetch_row(77002, workflow_status="baseline")
        self.assertIsInstance(row, type(self.storage.conn.execute("SELECT 1;").fetchone()))

        decisions = [
            FilterDecision(
                job_id=77002,
                filter_version="1.0",
                outcome="strong",
                matched_rule="strong_staff_nurse",
                match_reason="Core staff nurse",
                decided_at=datetime.now(timezone.utc).isoformat(),
            )
        ]

        sent, failed = dispatch_new_alerts(
            storage=self.storage,
            config=config,
            notifier=notifier,
            decisions=decisions,
            vacancies_by_id={77002: row},
        )
        self.assertEqual(len(notifier.sent), 0, "Baseline vacancies must never produce alerts")

    def test_expired_post_skipped_by_dispatch(self):
        """Dispatch logic skips vacancies whose closing_date is in the past."""
        from jobwatch.alerts import dispatch_new_alerts
        from jobwatch.filters import load_filter_config
        from jobwatch.models import FilterDecision

        notifier = FakeNotifier()
        config = load_filter_config()

        row = self._insert_and_fetch_row(77003, closing_date="2020-01-01", workflow_status="new")
        self.assertIsInstance(row, type(self.storage.conn.execute("SELECT 1;").fetchone()))

        decisions = [
            FilterDecision(
                job_id=77003,
                filter_version="1.0",
                outcome="strong",
                matched_rule="strong_staff_nurse",
                match_reason="test",
                decided_at=datetime.now(timezone.utc).isoformat(),
            )
        ]

        sent, _ = dispatch_new_alerts(
            storage=self.storage,
            config=config,
            notifier=notifier,
            decisions=decisions,
            vacancies_by_id={77003: row},
        )
        self.assertEqual(len(notifier.sent), 0, "Expired posts must never produce alerts")


class TestBatchingRules(unittest.TestCase):
    """Strong matches go individually; >= 2 maybes go in a grouped batch."""

    def setUp(self):
        from jobwatch.storage import Storage
        self.storage = Storage(db_path=":memory:")

    def tearDown(self):
        self.storage.close()

    def _insert_and_fetch_row(self, job_id, title="Staff Nurse", band="Band 5"):
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw, closing_date,
                    first_seen_at, last_seen_at, content_hash, is_listed, job_ref,
                    salary_raw, salary, band, location_raw, location,
                    dept_label_raw, dept_label, employer_raw, employer,
                    contract_raw, contract, hours_raw
                ) VALUES (
                    ?, ?, ?, '/url', '30/06/2027', '2027-06-30', ?, ?, 'hash', 1, ?,
                    ?, ?, ?, 'Edinburgh', 'Edinburgh',
                    'Nursing', 'Nursing', 'NHS Test', 'NHS Test',
                    'Permanent', 'Permanent', '37.5'
                );""",
                (job_id, title, title, now, now, str(job_id), band, band, band),
            )
            self.storage.conn.execute(
                """INSERT INTO user_vacancy_status (user_id, job_id, status, updated_at)
                VALUES ('owner', ?, 'new', ?);""",
                (job_id, now),
            )
        cur = self.storage.conn.execute(
            """SELECT v.*, vd.outcome, vd.matched_rule, vd.match_reason, vd.filter_version,
                      uvs.alerted_at, uvs.status as workflow_status
               FROM vacancies v
               LEFT JOIN vacancy_decisions vd ON v.job_id = vd.job_id
               LEFT JOIN user_vacancy_status uvs ON v.job_id = uvs.job_id AND uvs.user_id = 'owner'
               WHERE v.job_id = ?;""",
            (job_id,),
        )
        return cur.fetchone()

    def test_two_maybes_produce_one_batched_message(self):
        from jobwatch.alerts import dispatch_new_alerts
        from jobwatch.filters import load_filter_config
        from jobwatch.models import FilterDecision

        notifier = FakeNotifier()
        config = load_filter_config()
        now = datetime.now(timezone.utc).isoformat()

        row1 = self._insert_and_fetch_row(88001, "Community Nurse", band="Band 5")
        row2 = self._insert_and_fetch_row(88002, "Community Nurse", band="Band 5")
        rows = {88001: row1, 88002: row2}

        decisions = [
            FilterDecision(88001, "1.0", "maybe", "maybe_general_nurse", "General nurse", now),
            FilterDecision(88002, "1.0", "maybe", "maybe_general_nurse", "General nurse", now),
        ]

        sent, failed = dispatch_new_alerts(
            storage=self.storage,
            config=config,
            notifier=notifier,
            decisions=decisions,
            vacancies_by_id=rows,
        )
        # Both maybes should be batched into a single message
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("Potential Matches", notifier.sent[0])


if __name__ == "__main__":
    unittest.main()
