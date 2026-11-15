"""Unit tests for SQLite storage layer, schema versioning, and constraints."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from jobwatch.storage import (
    DatabaseError,
    DatabaseIntegrityError,
    DatabasePathError,
    Storage,
    apply_migrations,
    create_connection,
)


class TestStorageLayer(unittest.TestCase):
    """Verifies schema creation, migrations, foreign key enforcement, and startup integrity."""

    def setUp(self):
        self.storage = Storage(db_path=":memory:")

    def tearDown(self):
        self.storage.close()

    def test_schema_versioning_and_migrations(self):
        """Applying migrations sets PRAGMA user_version correctly and is idempotent."""
        conn = create_connection(":memory:")
        version = apply_migrations(conn)
        self.assertEqual(version, 4)

        # Applying again must be idempotent
        version_again = apply_migrations(conn)
        self.assertEqual(version_again, 4)
        conn.close()

    def test_foreign_keys_enforced_on_delete_user_status(self):
        """Deleting a vacancy with an attached user_vacancy_status record fails with IntegrityError (ON DELETE RESTRICT)."""
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw,
                    first_seen_at, last_seen_at, content_hash
                ) VALUES (99999, 'Test Nurse', 'Test Nurse', '/url', '01/01/2027', ?, ?, 'hash1');
                """,
                (now, now)
            )
            self.storage.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, updated_at)
                VALUES ('owner', 99999, 'new', ?);
                """,
                (now,)
            )

        # Attempting to delete the vacancy must fail due to ON DELETE RESTRICT
        with self.assertRaises(sqlite3.IntegrityError):
            with self.storage.conn:
                self.storage.conn.execute("DELETE FROM vacancies WHERE job_id = 99999;")

    def test_foreign_keys_enforced_on_delete_vacancy_changes(self):
        """Deleting a vacancy with an attached vacancy_changes record fails with IntegrityError (ON DELETE RESTRICT)."""
        now = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO source_runs (id, scope_key, run_type, started_at, outcome)
                VALUES (1, 'scope1', 'full_sweep', ?, 'success');
                """,
                (now,)
            )
            self.storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw,
                    first_seen_at, last_seen_at, content_hash
                ) VALUES (88888, 'Test Nurse', 'Test Nurse', '/url', '01/01/2027', ?, ?, 'hash1');
                """,
                (now, now)
            )
            self.storage.conn.execute(
                """
                INSERT INTO vacancy_changes (job_id, field_name, old_value, new_value, detected_at, run_id)
                VALUES (88888, 'salary', 'Band 5', 'Band 6', ?, 1);
                """,
                (now,)
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.storage.conn:
                self.storage.conn.execute("DELETE FROM vacancies WHERE job_id = 88888;")

    def test_integrity_check_enforced(self):
        """create_connection succeeds on a healthy database."""
        conn = create_connection(":memory:")
        cur = conn.execute("PRAGMA integrity_check;")
        self.assertEqual(cur.fetchone()[0], "ok")
        conn.close()

    def test_stale_running_run_cleanup(self):
        """Startup or periodic recovery marks orphaned 'running' runs beyond timeout as failure."""
        with self.storage.conn:
            # Insert a run started 120 minutes ago
            self.storage.conn.execute(
                """
                INSERT INTO source_runs (scope_key, run_type, started_at, outcome)
                VALUES ('scope_test', 'full_sweep', '2020-01-01T00:00:00+00:00', 'running');
                """
            )
        recovered = self.storage.recover_stale_runs(timeout_minutes=60)
        self.assertEqual(recovered, 1)

        cur = self.storage.conn.execute("SELECT outcome, error_message FROM source_runs WHERE scope_key = 'scope_test';")
        row = cur.fetchone()
        self.assertEqual(row["outcome"], "failure")
        self.assertEqual(row["error_message"], "stale_running_run_timed_out")

    def test_unwritable_path_fails_loudly(self):
        """Specifying an unwritable or invalid directory path raises DatabasePathError."""
        temp_dir = tempfile.mkdtemp()
        try:
            blocking_file = os.path.join(temp_dir, "blocking_file")
            with open(blocking_file, "w") as f:
                f.write("blocker")

            bad_db_path = os.path.join(blocking_file, "nested", "jobwatch.db")
            with self.assertRaises(DatabasePathError):
                create_connection(bad_db_path)

            with self.assertRaises(DatabasePathError):
                Storage(db_path=bad_db_path)
        finally:
            if os.path.exists(blocking_file):
                os.remove(blocking_file)
            os.rmdir(temp_dir)

    def test_shallow_miss_recording_and_counts(self):
        """Storage records shallow misses and queries total and recent counts."""
        now = datetime.now(timezone.utc).isoformat()
        # Seed a vacancy
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, title_raw, title, detail_url, closing_date_raw,
                    first_seen_at, last_seen_at, content_hash
                ) VALUES (99901, 'Staff Nurse', 'Staff Nurse', '/Job/99901', '30/11/2026', ?, ?, 'h1');
                """,
                (now, now),
            )
        self.storage.record_shallow_miss(99901, "Departments=9", 3, 24, now)
        self.assertEqual(self.storage.get_shallow_misses_count(), 1)
        self.assertEqual(self.storage.get_shallow_misses_since("2020-01-01"), 1)

    def test_circuit_breaker_storage_operations(self):
        """Storage retrieves, updates, and resets circuit breaker state."""
        state = self.storage.get_circuit_breaker_state()
        self.assertEqual(state["is_tripped"], 0)
        self.assertEqual(state["consecutive_failures"], 0)

        now = datetime.now(timezone.utc).isoformat()
        self.storage.update_circuit_breaker(
            is_tripped=1,
            tripped_reason="HTTP 429",
            tripped_at=now,
            consecutive_failures=3,
        )
        s2 = self.storage.get_circuit_breaker_state()
        self.assertEqual(s2["is_tripped"], 1)
        self.assertEqual(s2["tripped_reason"], "HTTP 429")
        self.assertEqual(s2["consecutive_failures"], 3)

        self.storage.reset_circuit_breaker()
        s3 = self.storage.get_circuit_breaker_state()
        self.assertEqual(s3["is_tripped"], 0)
        self.assertIsNone(s3["tripped_reason"])

    def test_scheduler_state_storage_operations(self):
        """Storage upserts and reads scheduler job state."""
        self.assertIsNone(self.storage.get_scheduler_job("shallow_sweep"))
        now = datetime.now(timezone.utc).isoformat()
        self.storage.update_scheduler_job(
            job_name="shallow_sweep",
            status="success",
            last_started_at=now,
            last_completed_at=now,
            next_due_at=now,
        )
        job = self.storage.get_scheduler_job("shallow_sweep")
        self.assertIsNotNone(job)
        self.assertEqual(job["last_status"], "success")

    def test_database_backup_api(self):
        """Storage backup_database creates a valid sqlite3 copy using backup() API."""
        temp_dir = tempfile.mkdtemp()
        try:
            backup_path = self.storage.backup_database(temp_dir, keep=3)
            self.assertTrue(os.path.exists(backup_path))
            # Verify backup can be opened and queried
            target_conn = sqlite3.connect(backup_path)
            cur = target_conn.execute("PRAGMA user_version;")
            self.assertEqual(cur.fetchone()[0], 4)
            target_conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

