"""Database storage and schema management for NHS-JobWatch using SQLite."""

import glob
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


class DatabaseError(Exception):
    """Base exception for storage errors."""
    pass


class DatabaseIntegrityError(DatabaseError):
    """Raised when PRAGMA integrity_check fails."""
    pass


class DatabasePathError(DatabaseError):
    """Raised when the database path or parent directory cannot be created or written."""
    pass


class ConcurrentRunRefusedError(DatabaseError):
    """Raised when a sweep is attempted while another run is actively running."""
    pass


class DatabaseStateCorruptError(DatabaseError):
    """Raised when baseline marker exists but vacancy table is unexpectedly empty."""
    pass


class UnbaselinedScopeError(DatabaseError):
    """Raised when a shallow run is attempted on an unbaselined scope."""
    pass


def get_default_db_path() -> str:
    """Returns database path from JOBWATCH_DB_PATH or fixed default (data/jobwatch.db)."""
    env_path = os.getenv("JOBWATCH_DB_PATH")
    if env_path:
        return env_path
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_dir, "data", "jobwatch.db")


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Creates a configured SQLite connection enforcing WAL, foreign keys, and integrity."""
    path = db_path if db_path is not None else get_default_db_path()
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except (OSError, PermissionError) as e:
                raise DatabasePathError(
                    f"Cannot create directory for database at '{parent}': {e}"
                ) from e

            # Probe directory writability
            probe_path = os.path.join(parent, f".probe_write_{os.getpid()}")
            try:
                with open(probe_path, "w") as f:
                    f.write("probe")
                os.remove(probe_path)
            except (OSError, PermissionError) as e:
                raise DatabasePathError(
                    f"Database directory is not writable at '{parent}': {e}"
                ) from e

    try:
        conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    except (sqlite3.Error, OSError) as e:
        raise DatabasePathError(
            f"Cannot connect to SQLite database at '{path}': {e}"
        ) from e

    conn.row_factory = sqlite3.Row

    # Enforce foreign keys and WAL mode
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
    except (sqlite3.Error, OSError) as e:
        conn.close()
        raise DatabasePathError(
            f"Cannot initialize SQLite database settings on '{path}': {e}"
        ) from e

    # Startup integrity check
    try:
        cursor = conn.execute("PRAGMA integrity_check;")
        row = cursor.fetchone()
        if not row or row[0] != "ok":
            conn.close()
            raise DatabaseIntegrityError(f"PRAGMA integrity_check failed on {path}: {row[0] if row else 'null'}")
    except DatabaseIntegrityError:
        raise
    except (sqlite3.Error, OSError) as e:
        conn.close()
        raise DatabasePathError(
            f"Integrity check failed to execute on '{path}': {e}"
        ) from e

    return conn


def _migration_v1(conn: sqlite3.Connection) -> None:
    """Initial schema migration: tables for scopes, vacancies, changes, user status, and runs."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS scope_metadata (
        scope_key TEXT PRIMARY KEY,
        is_active INTEGER NOT NULL DEFAULT 1,
        is_baselined INTEGER NOT NULL DEFAULT 0,
        baselined_at TEXT,
        baseline_run_id INTEGER,
        last_complete_sweep_started_at TEXT
    );

    CREATE TABLE IF NOT EXISTS vacancies (
        job_id INTEGER PRIMARY KEY,
        job_ref_raw TEXT,
        job_ref TEXT,
        title_raw TEXT NOT NULL,
        title TEXT NOT NULL,
        detail_url TEXT NOT NULL,
        closing_date_raw TEXT NOT NULL,
        closing_date TEXT,
        salary_raw TEXT,
        salary TEXT,
        band TEXT,
        location_raw TEXT,
        location TEXT,
        dept_label_raw TEXT,
        dept_label TEXT,
        employer_raw TEXT,
        employer TEXT,
        contract_raw TEXT,
        contract TEXT,
        hours_raw TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        is_listed INTEGER NOT NULL DEFAULT 1,
        content_hash TEXT NOT NULL,
        repost_flag TEXT
    );

    CREATE TABLE IF NOT EXISTS vacancy_scopes (
        job_id INTEGER NOT NULL,
        scope_key TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        consecutive_missing_sweeps INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (job_id, scope_key),
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE CASCADE,
        FOREIGN KEY (scope_key) REFERENCES scope_metadata(scope_key) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS vacancy_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        field_name TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        detected_at TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE RESTRICT,
        FOREIGN KEY (run_id) REFERENCES source_runs(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS user_vacancy_status (
        user_id TEXT NOT NULL DEFAULT 'owner',
        job_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        alerted_at TEXT,
        updated_at TEXT NOT NULL,
        snooze_until TEXT,
        PRIMARY KEY (user_id, job_id),
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS source_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_key TEXT NOT NULL,
        run_type TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        is_baseline INTEGER NOT NULL DEFAULT 0,
        is_complete_sweep INTEGER NOT NULL DEFAULT 0,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        vacancies_seen INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        changed_count INTEGER NOT NULL DEFAULT 0,
        unlisted_count INTEGER NOT NULL DEFAULT 0,
        parse_errors_count INTEGER NOT NULL DEFAULT 0,
        outcome TEXT NOT NULL,
        error_message TEXT
    );
    """)


def _migration_v2(conn: sqlite3.Connection) -> None:
    """Migration v2: creates vacancy_decisions table for filter evaluation decisions."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS vacancy_decisions (
        job_id INTEGER PRIMARY KEY,
        filter_version TEXT NOT NULL,
        outcome TEXT NOT NULL,
        matched_rule TEXT NOT NULL,
        match_reason TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE RESTRICT
    );
    """)


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Migration v3: chat_settings, alert_deliveries, and previous_status on user_vacancy_status."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id TEXT PRIMARY KEY,
        is_paused INTEGER NOT NULL DEFAULT 0,
        paused_at TEXT,
        muted_until TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        inactive_reason TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS alert_deliveries (
        job_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        telegram_message_id INTEGER NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (job_id, chat_id),
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE RESTRICT
    );
    """)
    cursor = conn.execute("PRAGMA table_info(user_vacancy_status);")
    cols = [row[1] for row in cursor.fetchall()]
    if "previous_status" not in cols:
        conn.execute("ALTER TABLE user_vacancy_status ADD COLUMN previous_status TEXT;")


def _migration_v4(conn: sqlite3.Connection) -> None:
    """Migration v4: circuit_breaker, scheduler_state, and shallow_misses tables."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS circuit_breaker (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        is_tripped INTEGER NOT NULL DEFAULT 0,
        tripped_at TEXT,
        tripped_reason TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_failure_alert_at TEXT,
        updated_at TEXT NOT NULL
    );

    INSERT OR IGNORE INTO circuit_breaker (id, is_tripped, consecutive_failures, updated_at)
    VALUES (1, 0, 0, datetime('now'));

    CREATE TABLE IF NOT EXISTS scheduler_state (
        job_name TEXT PRIMARY KEY,
        last_started_at TEXT,
        last_completed_at TEXT,
        last_status TEXT NOT NULL DEFAULT 'never_run',
        next_due_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    );

    CREATE TABLE IF NOT EXISTS shallow_misses (
        job_id INTEGER PRIMARY KEY,
        scope_key TEXT NOT NULL,
        page_index INTEGER NOT NULL,
        card_offset INTEGER NOT NULL,
        detected_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS vacancy_reminders (
        job_id INTEGER NOT NULL,
        reminder_milestone INTEGER NOT NULL,
        reminded_at TEXT NOT NULL,
        PRIMARY KEY (job_id, reminder_milestone),
        FOREIGN KEY (job_id) REFERENCES vacancies(job_id) ON DELETE CASCADE
    );
    """)


MIGRATIONS = [
    _migration_v1,
    _migration_v2,
    _migration_v3,
    _migration_v4,
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Applies pending migrations in sequence inside a transaction and updates user_version."""
    cursor = conn.execute("PRAGMA user_version;")
    current_version = cursor.fetchone()[0]

    if current_version < len(MIGRATIONS):
        with conn:
            for idx in range(current_version, len(MIGRATIONS)):
                MIGRATIONS[idx](conn)
                conn.execute(f"PRAGMA user_version = {idx + 1};")
    
    cursor = conn.execute("PRAGMA user_version;")
    return cursor.fetchone()[0]


class Storage:
    """High-level repository managing database persistence and atomic sweep transactions."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path if db_path is not None else get_default_db_path()
        self.conn = create_connection(self.db_path)
        apply_migrations(self.conn)

    def close(self) -> None:
        """Closes the underlying database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def get_scope_metadata(self, scope_key: str) -> Optional[sqlite3.Row]:
        """Retrieves metadata for a specific query scope."""
        cur = self.conn.execute(
            "SELECT * FROM scope_metadata WHERE scope_key = ?;",
            (scope_key,)
        )
        return cur.fetchone()

    def ensure_scope_metadata(self, scope_key: str) -> sqlite3.Row:
        """Ensures a scope_metadata row exists for scope_key."""
        meta = self.get_scope_metadata(scope_key)
        if meta is None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO scope_metadata (scope_key, is_active, is_baselined) VALUES (?, 1, 0);",
                    (scope_key,)
                )
            meta = self.get_scope_metadata(scope_key)
        return meta

    def get_vacancy_count(self) -> int:
        """Returns the total number of vacancies stored in the database."""
        cur = self.conn.execute("SELECT COUNT(*) FROM vacancies;")
        return cur.fetchone()[0]

    def get_vacancy(self, job_id: int) -> Optional[sqlite3.Row]:
        """Retrieves a single vacancy by its primary key job_id."""
        cur = self.conn.execute("SELECT * FROM vacancies WHERE job_id = ?;", (job_id,))
        return cur.fetchone()

    def find_possible_repost(
        self,
        job_ref: Optional[str],
        title: str,
        employer: Optional[str],
        dept_label: Optional[str],
        location: Optional[str],
    ) -> Optional[int]:
        """Checks if a new job matches an existing vacancy on (job_ref) OR (title + employer + dept_label + location)."""
        # 1. Match on exact non-empty job_ref
        if job_ref:
            cur = self.conn.execute(
                "SELECT job_id FROM vacancies WHERE job_ref = ? LIMIT 1;",
                (job_ref,)
            )
            row = cur.fetchone()
            if row:
                return row["job_id"]

        # 2. Match on collapsed title + employer + dept_label + location (all must be non-empty and active)
        if title and employer and dept_label and location:
            cur = self.conn.execute(
                """
                SELECT job_id FROM vacancies
                WHERE title = ? AND employer = ? AND dept_label = ? AND location = ? AND is_listed = 1
                LIMIT 1;
                """,
                (title, employer, dept_label, location)
            )
            row = cur.fetchone()
            if row:
                return row["job_id"]

        return None

    def upsert_vacancy_record(
        self,
        card: Any,
        content_hash: str,
        now_iso: str,
        repost_flag: Optional[str] = None,
    ) -> None:
        """Inserts or updates a core vacancy entity."""
        closing_iso = card.closing_date.isoformat() if card.closing_date else None
        self.conn.execute(
            """
            INSERT INTO vacancies (
                job_id, job_ref_raw, job_ref, title_raw, title, detail_url,
                closing_date_raw, closing_date, salary_raw, salary, band,
                location_raw, location, dept_label_raw, dept_label,
                employer_raw, employer, contract_raw, contract, hours_raw,
                first_seen_at, last_seen_at, is_listed, content_hash, repost_flag
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, 1, ?, ?
            )
            ON CONFLICT(job_id) DO UPDATE SET
                job_ref_raw = excluded.job_ref_raw,
                job_ref = excluded.job_ref,
                title_raw = excluded.title_raw,
                title = excluded.title,
                detail_url = excluded.detail_url,
                closing_date_raw = excluded.closing_date_raw,
                closing_date = excluded.closing_date,
                salary_raw = excluded.salary_raw,
                salary = excluded.salary,
                band = excluded.band,
                location_raw = excluded.location_raw,
                location = excluded.location,
                dept_label_raw = excluded.dept_label_raw,
                dept_label = excluded.dept_label,
                employer_raw = excluded.employer_raw,
                employer = excluded.employer,
                contract_raw = excluded.contract_raw,
                contract = excluded.contract,
                hours_raw = excluded.hours_raw,
                last_seen_at = excluded.last_seen_at,
                is_listed = 1,
                content_hash = excluded.content_hash,
                repost_flag = COALESCE(vacancies.repost_flag, excluded.repost_flag);
            """,
            (
                card.job_id, card.job_ref_raw, card.job_ref, card.title_raw, card.title, card.detail_url,
                card.closing_date_raw, closing_iso, card.salary_raw, card.salary, card.band,
                card.location_raw, card.location, card.dept_label_raw, card.dept_label,
                card.employer_raw, card.employer, card.contract_raw, card.contract, card.hours_raw,
                now_iso, now_iso, content_hash, repost_flag
            )
        )

    def upsert_vacancy_scope(self, job_id: int, scope_key: str, now_iso: str) -> None:
        """Records that job_id was observed under scope_key."""
        self.conn.execute(
            """
            INSERT INTO vacancy_scopes (job_id, scope_key, first_seen_at, last_seen_at, consecutive_missing_sweeps)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(job_id, scope_key) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                consecutive_missing_sweeps = 0;
            """,
            (job_id, scope_key, now_iso, now_iso)
        )

    def record_change(
        self,
        job_id: int,
        field_name: str,
        old_value: Optional[str],
        new_value: Optional[str],
        now_iso: str,
        run_id: int,
    ) -> None:
        """Records an entry in vacancy_changes."""
        self.conn.execute(
            """
            INSERT INTO vacancy_changes (job_id, field_name, old_value, new_value, detected_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (job_id, field_name, old_value, new_value, now_iso, run_id)
        )

    def set_user_status(
        self,
        job_id: int,
        status: str,
        alerted_at: Optional[str],
        now_iso: str,
        user_id: str = "owner",
    ) -> None:
        """Sets user vacancy status and alerted timestamp."""
        self.conn.execute(
            """
            INSERT INTO user_vacancy_status (user_id, job_id, status, alerted_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, job_id) DO UPDATE SET
                status = excluded.status,
                alerted_at = COALESCE(user_vacancy_status.alerted_at, excluded.alerted_at),
                updated_at = excluded.updated_at;
            """,
            (user_id, job_id, status, alerted_at, now_iso)
        )

    def recover_stale_runs(self, timeout_minutes: int = 60) -> int:
        """Recovers any run stuck in 'running' state beyond timeout_minutes (or at startup)."""
        now = datetime.now(timezone.utc)
        cur = self.conn.execute("SELECT id, started_at FROM source_runs WHERE outcome = 'running';")
        stale_ids = []
        for row in cur.fetchall():
            run_id = row["id"]
            started_at_str = row["started_at"]
            try:
                started_dt = datetime.fromisoformat(started_at_str)
                elapsed = (now - started_dt).total_seconds() / 60.0
                if elapsed >= timeout_minutes:
                    stale_ids.append(run_id)
            except Exception:
                stale_ids.append(run_id)

        if stale_ids:
            now_iso = now.isoformat()
            with self.conn:
                for rid in stale_ids:
                    self.conn.execute(
                        """
                        UPDATE source_runs
                        SET outcome = 'failure',
                            finished_at = ?,
                            error_message = 'stale_running_run_timed_out'
                        WHERE id = ?;
                        """,
                        (now_iso, rid)
                    )
        return len(stale_ids)

    def get_active_run(self) -> Optional[sqlite3.Row]:
        """Returns currently running run if any, else None."""
        cur = self.conn.execute("SELECT id, scope_key, started_at FROM source_runs WHERE outcome = 'running' LIMIT 1;")
        return cur.fetchone()

    def start_run(
        self,
        scope_key: str,
        run_type: str,
        started_at_iso: str,
        is_baseline: bool = False,
        stale_timeout_minutes: int = 60,
    ) -> int:
        """Initiates a run, enforcing single-run concurrency and recovering stale runs."""
        # Recover stale runs older than timeout
        self.recover_stale_runs(timeout_minutes=stale_timeout_minutes)

        # Check if an active non-stale run is in progress
        cur = self.conn.execute("SELECT id, scope_key, started_at FROM source_runs WHERE outcome = 'running' LIMIT 1;")
        active = cur.fetchone()
        if active:
            raise ConcurrentRunRefusedError(
                f"Cannot start sweep: run #{active['id']} for scope '{active['scope_key']}' "
                f"started at {active['started_at']} is currently running."
            )

        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO source_runs (
                    scope_key, run_type, started_at, is_baseline, outcome
                ) VALUES (?, ?, ?, ?, 'running');
                """,
                (scope_key, run_type, started_at_iso, 1 if is_baseline else 0)
            )
            return cur.lastrowid

    def update_run_progress(
        self,
        run_id: int,
        pages_fetched: int,
        vacancies_seen: int,
        new_count: int,
        changed_count: int,
        parse_errors_count: int,
    ) -> None:
        """Updates live run metrics during sweep execution."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE source_runs
                SET pages_fetched = ?,
                    vacancies_seen = ?,
                    new_count = ?,
                    changed_count = ?,
                    parse_errors_count = ?
                WHERE id = ?;
                """,
                (pages_fetched, vacancies_seen, new_count, changed_count, parse_errors_count, run_id)
            )

    def close_run_as_drifted(
        self,
        run_id: int,
        finished_at_iso: str,
        pages_fetched: int,
        vacancies_seen: int,
        new_count: int,
        changed_count: int,
        parse_errors_count: int,
    ) -> None:
        """Closes a drifted run without finalization (upserts kept, unlisting skipped)."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE source_runs
                SET outcome = 'drifted',
                    finished_at = ?,
                    is_complete_sweep = 0,
                    pages_fetched = ?,
                    vacancies_seen = ?,
                    new_count = ?,
                    changed_count = ?,
                    unlisted_count = 0,
                    parse_errors_count = ?,
                    error_message = 'totalMatchRecords changed mid-sweep on card-bearing page'
                WHERE id = ?;
                """,
                (finished_at_iso, pages_fetched, vacancies_seen, new_count, changed_count, parse_errors_count, run_id)
            )

    def close_run_failure(
        self,
        run_id: int,
        finished_at_iso: str,
        error_message: str,
        pages_fetched: int = 0,
        vacancies_seen: int = 0,
        parse_errors_count: int = 0,
    ) -> None:
        """Closes an interrupted or errored run as failure."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE source_runs
                SET outcome = 'failure',
                    finished_at = ?,
                    is_complete_sweep = 0,
                    pages_fetched = ?,
                    vacancies_seen = ?,
                    parse_errors_count = ?,
                    error_message = ?
                WHERE id = ?;
                """,
                (finished_at_iso, pages_fetched, vacancies_seen, parse_errors_count, error_message, run_id)
            )

    def finalize_sweep_transaction(
        self,
        scope_key: str,
        run_id: int,
        sweep_started_at_iso: str,
        finished_at_iso: str,
        seen_job_ids_in_sweep: set,
        pages_fetched: int,
        vacancies_seen: int,
        new_count: int,
        changed_count: int,
        parse_errors_count: int,
        is_baseline: bool,
    ) -> int:
        """Finalizes a successful complete full sweep inside a single atomic transaction.

        - Increments consecutive_missing_sweeps for unseen vacancies in this scope
        - Enforces the two-sweep unlisting rule across active scopes
        - Marks scope baselined (if baseline run) and updates last_complete_sweep_started_at
        - Closes source_runs record with outcome = 'success'
        """
        unlisted_count = 0

        with self.conn:
            # 1. Fetch all vacancies tracked under this scope
            cur = self.conn.execute(
                "SELECT job_id, consecutive_missing_sweeps FROM vacancy_scopes WHERE scope_key = ?;",
                (scope_key,)
            )
            scope_rows = cur.fetchall()

            for row in scope_rows:
                jid = row["job_id"]
                current_missing = row["consecutive_missing_sweeps"]

                if jid in seen_job_ids_in_sweep:
                    # Seen in this sweep: reset missing counter
                    if current_missing != 0:
                        self.conn.execute(
                            "UPDATE vacancy_scopes SET consecutive_missing_sweeps = 0 WHERE job_id = ? AND scope_key = ?;",
                            (jid, scope_key)
                        )
                else:
                    # Not seen in this complete full sweep: increment missing counter
                    new_missing = current_missing + 1
                    self.conn.execute(
                        "UPDATE vacancy_scopes SET consecutive_missing_sweeps = ? WHERE job_id = ? AND scope_key = ?;",
                        (new_missing, jid, scope_key)
                    )

                    # Check two-sweep unlisting rule
                    if new_missing >= 2:
                        # Check if active in ANY other active scope
                        # Active in another scope = seen in that scope's most recent complete full sweep
                        # (last_seen_at >= that_scope.last_complete_sweep_started_at AND consecutive_missing_sweeps == 0)
                        cur_other = self.conn.execute(
                            """
                            SELECT vs.job_id
                            FROM vacancy_scopes vs
                            JOIN scope_metadata sm ON vs.scope_key = sm.scope_key
                            WHERE vs.job_id = ?
                              AND vs.scope_key != ?
                              AND sm.is_active = 1
                              AND vs.consecutive_missing_sweeps = 0
                              AND sm.last_complete_sweep_started_at IS NOT NULL
                              AND vs.last_seen_at >= sm.last_complete_sweep_started_at
                            LIMIT 1;
                            """,
                            (jid, scope_key)
                        )
                        active_elsewhere = cur_other.fetchone()

                        if not active_elsewhere:
                            # Not active in any other active scope: mark unlisted
                            cur_v = self.conn.execute(
                                "UPDATE vacancies SET is_listed = 0 WHERE job_id = ? AND is_listed = 1;",
                                (jid,)
                            )
                            if cur_v.rowcount > 0:
                                unlisted_count += 1

            # 2. Update scope_metadata
            if is_baseline:
                self.conn.execute(
                    """
                    UPDATE scope_metadata
                    SET is_baselined = 1,
                        baselined_at = ?,
                        baseline_run_id = ?,
                        last_complete_sweep_started_at = ?
                    WHERE scope_key = ?;
                    """,
                    (sweep_started_at_iso, run_id, sweep_started_at_iso, scope_key)
                )
            else:
                self.conn.execute(
                    """
                    UPDATE scope_metadata
                    SET last_complete_sweep_started_at = ?
                    WHERE scope_key = ?;
                    """,
                    (sweep_started_at_iso, scope_key)
                )

            # 3. Close run record
            self.conn.execute(
                """
                UPDATE source_runs
                SET outcome = 'success',
                    finished_at = ?,
                    is_complete_sweep = 1,
                    pages_fetched = ?,
                    vacancies_seen = ?,
                    new_count = ?,
                    changed_count = ?,
                    unlisted_count = ?,
                    parse_errors_count = ?
                WHERE id = ?;
                """,
                (
                    finished_at_iso, pages_fetched, vacancies_seen,
                    new_count, changed_count, unlisted_count, parse_errors_count, run_id
                )
            )

        return unlisted_count

    def record_decision(
        self,
        job_id: int,
        filter_version: str,
        outcome: str,
        matched_rule: str,
        match_reason: str,
        decided_at: str,
    ) -> None:
        """Records or updates a filter outcome decision in vacancy_decisions."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO vacancy_decisions (job_id, filter_version, outcome, matched_rule, match_reason, decided_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    filter_version = excluded.filter_version,
                    outcome = excluded.outcome,
                    matched_rule = excluded.matched_rule,
                    match_reason = excluded.match_reason,
                    decided_at = excluded.decided_at;
                """,
                (job_id, filter_version, outcome, matched_rule, match_reason, decided_at),
            )

    def get_decision(self, job_id: int) -> Optional[sqlite3.Row]:
        """Retrieves the current decision record for a job_id."""
        cur = self.conn.execute("SELECT * FROM vacancy_decisions WHERE job_id = ?;", (job_id,))
        return cur.fetchone()

    def get_open_vacancies(self, user_id: str = "shared") -> List[sqlite3.Row]:
        """Retrieves all currently listed vacancies with decisions and user status."""
        cur = self.conn.execute(
            """
            SELECT v.*, vd.outcome, vd.matched_rule, vd.match_reason, vd.filter_version,
                   uvs.alerted_at, uvs.status as workflow_status, uvs.snooze_until
            FROM vacancies v
            LEFT JOIN vacancy_decisions vd ON v.job_id = vd.job_id
            LEFT JOIN user_vacancy_status uvs ON v.job_id = uvs.job_id AND uvs.user_id = ?
            WHERE v.is_listed = 1
            ORDER BY v.closing_date ASC, v.job_id ASC;
            """,
            (user_id,),
        )
        return cur.fetchall()

    def mark_alerted(self, job_id: int, alerted_at: str, user_id: str = "shared") -> None:
        """Updates user_vacancy_status.alerted_at after confirmed delivery."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, alerted_at, updated_at)
                VALUES (?, ?, 'new', ?, ?)
                ON CONFLICT(user_id, job_id) DO UPDATE SET
                    alerted_at = excluded.alerted_at,
                    updated_at = excluded.updated_at;
                """,
                (user_id, job_id, alerted_at, alerted_at),
            )

    def mark_alerted_if_all_delivered(
        self,
        job_id: int,
        allowed_chat_ids: List[str],
        alerted_at: str,
        user_id: str = "shared",
    ) -> bool:
        """
        Sets alerted_at only if every active allowed chat has a confirmed delivery row
        in alert_deliveries, OR is marked inactive in chat_settings.
        Returns True if alerted_at was set; False if delivery is still partial.
        """
        delivered_chats = {
            str(r["chat_id"])
            for r in self.get_deliveries_for_job(job_id)
        }
        for cid in allowed_chat_ids:
            cid_str = str(cid)
            if cid_str not in delivered_chats:
                # Check if chat is inactive (403'd or similar)
                settings = self.conn.execute(
                    "SELECT is_active FROM chat_settings WHERE chat_id = ?;",
                    (cid_str,),
                ).fetchone()
                # An active chat without a delivery means incomplete
                if settings is None or settings["is_active"] == 1:
                    return False
        # All active chats delivered (or inactive)
        self.mark_alerted(job_id, alerted_at, user_id)
        return True

    def get_chat_settings(self, chat_id: str) -> sqlite3.Row:
        """Retrieves settings for a chat, creating default record if not present."""
        cur = self.conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?;", (str(chat_id),))
        row = cur.fetchone()
        if row:
            return row
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO chat_settings (chat_id, is_paused, is_active, updated_at)
                VALUES (?, 0, 1, ?)
                ON CONFLICT(chat_id) DO NOTHING;
                """,
                (str(chat_id), now),
            )
        cur = self.conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?;", (str(chat_id),))
        return cur.fetchone()

    def update_chat_settings(
        self,
        chat_id: str,
        is_paused: Optional[int] = None,
        paused_at: Optional[str] = None,
        muted_until: Optional[str] = None,
        is_active: Optional[int] = None,
        inactive_reason: Optional[str] = None,
    ) -> None:
        """Updates chat settings fields dynamically."""
        now = datetime.now(timezone.utc).isoformat()
        # Ensure row exists
        self.get_chat_settings(chat_id)

        updates = ["updated_at = ?"]
        params: List[Any] = [now]
        if is_paused is not None:
            updates.append("is_paused = ?")
            params.append(is_paused)
        if paused_at is not None:
            updates.append("paused_at = ?")
            params.append(paused_at)
        if muted_until is not None:
            updates.append("muted_until = ?")
            params.append(muted_until)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(is_active)
        if inactive_reason is not None:
            updates.append("inactive_reason = ?")
            params.append(inactive_reason)

        params.append(str(chat_id))
        sql = f"UPDATE chat_settings SET {', '.join(updates)} WHERE chat_id = ?;"
        with self.conn:
            self.conn.execute(sql, tuple(params))

    def record_delivery(
        self,
        job_id: int,
        chat_id: str,
        telegram_message_id: int,
        delivered_at: str,
    ) -> None:
        """Records confirmed delivery of an alert to a specific chat."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO alert_deliveries (job_id, chat_id, telegram_message_id, delivered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, chat_id) DO UPDATE SET
                    telegram_message_id = excluded.telegram_message_id,
                    delivered_at = excluded.delivered_at;
                """,
                (job_id, str(chat_id), int(telegram_message_id), delivered_at),
            )

    def get_deliveries_for_job(self, job_id: int) -> List[sqlite3.Row]:
        """Returns all confirmed alert deliveries for a vacancy across chats."""
        cur = self.conn.execute(
            "SELECT * FROM alert_deliveries WHERE job_id = ? ORDER BY chat_id ASC;",
            (job_id,),
        )
        return cur.fetchall()

    def update_user_status(
        self,
        job_id: int,
        new_status: str,
        user_id: str = "shared",
    ) -> Tuple[Optional[str], str]:
        """
        Updates vacancy workflow status, saving previous status for undo restoration.
        Returns (previous_status, new_status).
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE user_id = ? AND job_id = ?;",
            (user_id, job_id),
        )
        existing = cur.fetchone()
        prev = existing["status"] if existing else "new"

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, previous_status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, job_id) DO UPDATE SET
                    previous_status = user_vacancy_status.status,
                    status = excluded.status,
                    updated_at = excluded.updated_at;
                """,
                (user_id, job_id, new_status, prev, now),
            )
        return prev, new_status

    def undo_user_status(
        self,
        job_id: int,
        user_id: str = "shared",
    ) -> Tuple[Optional[str], str]:
        """
        Restores the previous workflow status for a vacancy.
        Returns (reverted_from_status, restored_status).
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT status, previous_status FROM user_vacancy_status WHERE user_id = ? AND job_id = ?;",
            (user_id, job_id),
        )
        row = cur.fetchone()
        if not row:
            return None, "new"

        curr_status = row["status"]
        prev_status = row["previous_status"] or "new"

        with self.conn:
            self.conn.execute(
                """
                UPDATE user_vacancy_status
                SET status = ?,
                    previous_status = NULL,
                    updated_at = ?
                WHERE user_id = ? AND job_id = ?;
                """,
                (prev_status, now, user_id, job_id),
            )
        return curr_status, prev_status

    def snooze_vacancy(
        self,
        job_id: int,
        snooze_until: str,
        user_id: str = "shared",
    ) -> Tuple[Optional[str], str]:
        """Snoozes a vacancy until the specified ISO timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT status FROM user_vacancy_status WHERE user_id = ? AND job_id = ?;",
            (user_id, job_id),
        )
        existing = cur.fetchone()
        prev = existing["status"] if existing else "new"

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, previous_status, snooze_until, updated_at)
                VALUES (?, ?, 'snoozed', ?, ?, ?)
                ON CONFLICT(user_id, job_id) DO UPDATE SET
                    previous_status = user_vacancy_status.status,
                    status = 'snoozed',
                    snooze_until = excluded.snooze_until,
                    updated_at = excluded.updated_at;
                """,
                (user_id, job_id, prev, snooze_until, now),
            )
        return prev, "snoozed"

    def get_vacancies_by_status(
        self,
        status: str,
        user_id: str = "shared",
    ) -> List[sqlite3.Row]:
        """Retrieves vacancies matching a specific workflow status."""
        cur = self.conn.execute(
            """
            SELECT v.*, vd.outcome, vd.matched_rule, vd.match_reason,
                   uvs.status as workflow_status, uvs.snooze_until, uvs.updated_at as status_updated_at
            FROM vacancies v
            JOIN user_vacancy_status uvs ON v.job_id = uvs.job_id AND uvs.user_id = ?
            LEFT JOIN vacancy_decisions vd ON v.job_id = vd.job_id
            WHERE uvs.status = ?
              AND v.is_listed = 1
            ORDER BY uvs.updated_at DESC, v.job_id DESC;
            """,
            (user_id, status),
        )
        return cur.fetchall()

    def get_excluded_summary(self) -> List[sqlite3.Row]:
        """Returns summary counts of excluded vacancies grouped by rule."""
        cur = self.conn.execute(
            """
            SELECT vd.matched_rule, vd.match_reason, COUNT(*) as count
            FROM vacancy_decisions vd
            JOIN vacancies v ON vd.job_id = v.job_id
            WHERE vd.outcome = 'excluded'
              AND v.is_listed = 1
            GROUP BY vd.matched_rule, vd.match_reason
            ORDER BY count DESC, vd.matched_rule ASC;
            """
        )
        return cur.fetchall()

    def get_excluded_by_rule(self, rule_id: str) -> List[sqlite3.Row]:
        """Returns excluded vacancies for a specific rule ID, newest first."""
        cur = self.conn.execute(
            """
            SELECT v.*, vd.outcome, vd.matched_rule, vd.match_reason, vd.decided_at
            FROM vacancies v
            JOIN vacancy_decisions vd ON v.job_id = vd.job_id
            WHERE vd.outcome = 'excluded'
              AND vd.matched_rule = ?
              AND v.is_listed = 1
            ORDER BY v.first_seen_at DESC, v.job_id DESC;
            """,
            (rule_id,),
        )
        return cur.fetchall()

    def process_snooze_wakeups(
        self,
        current_time_iso: str,
        user_id: str = "shared",
    ) -> List[sqlite3.Row]:
        """
        Finds snoozed vacancies where snooze_until has expired.
        If vacancy has closed meanwhile, sets status to 'expired'.
        If still open, restores status to 'new', clears snooze_until, and returns the row.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            SELECT v.*, uvs.snooze_until, uvs.status as workflow_status
            FROM vacancies v
            JOIN user_vacancy_status uvs ON v.job_id = uvs.job_id AND uvs.user_id = ?
            WHERE uvs.status = 'snoozed'
              AND uvs.snooze_until IS NOT NULL
              AND uvs.snooze_until <= ?;
            """,
            (user_id, current_time_iso),
        )
        expired_snoozes = cur.fetchall()
        woken_vacancies = []

        with self.conn:
            for row in expired_snoozes:
                jid = row["job_id"]
                # Check if closed or unlisted
                is_closed = (row["is_listed"] == 0)
                if row["closing_date"]:
                    try:
                        # Extract YYYY-MM-DD
                        c_date = row["closing_date"][:10]
                        today_str = current_time_iso[:10]
                        if c_date < today_str:
                            is_closed = True
                    except Exception:
                        pass

                if is_closed:
                    self.conn.execute(
                        """
                        UPDATE user_vacancy_status
                        SET status = 'expired',
                            snooze_until = NULL,
                            updated_at = ?
                        WHERE user_id = ? AND job_id = ?;
                        """,
                        (now, user_id, jid),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE user_vacancy_status
                        SET status = 'new',
                            previous_status = 'snoozed',
                            snooze_until = NULL,
                            updated_at = ?
                        WHERE user_id = ? AND job_id = ?;
                        """,
                        (now, user_id, jid),
                    )
                    woken_vacancies.append(row)

        return woken_vacancies

    # ── Shallow Misses Tracking ──

    def has_reminder_been_sent(self, job_id: int, milestone: int) -> bool:
        """Checks if a closing-date reminder for milestone (e.g. 5 or 1 days) was already recorded."""
        cur = self.conn.execute(
            "SELECT 1 FROM vacancy_reminders WHERE job_id = ? AND reminder_milestone = ?;",
            (job_id, milestone),
        )
        return cur.fetchone() is not None

    def record_reminder_sent(self, job_id: int, milestone: int, reminded_at: str) -> None:
        """Records that a closing-date reminder was dispatched."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO vacancy_reminders (job_id, reminder_milestone, reminded_at)
                VALUES (?, ?, ?);
                """,
                (job_id, milestone, reminded_at),
            )

    def record_shallow_miss(
        self,
        job_id: int,
        scope_key: str,
        page_index: int,
        card_offset: int,
        detected_at: str,
    ) -> None:
        """Records a vacancy discovered on page > 2 that was first seen during a full sweep."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO shallow_misses (
                    job_id, scope_key, page_index, card_offset, detected_at
                ) VALUES (?, ?, ?, ?, ?);
                """,
                (job_id, scope_key, page_index, card_offset, detected_at),
            )

    def get_shallow_misses_count(self) -> int:
        """Returns total lifetime shallow misses detected."""
        cur = self.conn.execute("SELECT COUNT(*) FROM shallow_misses;")
        return cur.fetchone()[0]

    def get_shallow_misses_since(self, since_iso: str) -> int:
        """Returns shallow misses detected since a given timestamp."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM shallow_misses WHERE detected_at >= ?;",
            (since_iso,),
        )
        return cur.fetchone()[0]

    # ── Circuit Breaker State ──

    def get_circuit_breaker_state(self) -> Dict[str, Any]:
        """Returns the single row from circuit_breaker table."""
        cur = self.conn.execute("SELECT * FROM circuit_breaker WHERE id = 1;")
        row = cur.fetchone()
        if not row:
            now = datetime.now(timezone.utc).isoformat()
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO circuit_breaker (id, is_tripped, consecutive_failures, updated_at) VALUES (1, 0, 0, ?);",
                    (now,),
                )
            cur = self.conn.execute("SELECT * FROM circuit_breaker WHERE id = 1;")
            row = cur.fetchone()
        return dict(row)

    def update_circuit_breaker(
        self,
        is_tripped: Optional[int] = None,
        tripped_reason: Optional[str] = None,
        tripped_at: Optional[str] = None,
        consecutive_failures: Optional[int] = None,
        last_failure_alert_at: Optional[str] = None,
    ) -> None:
        """Updates circuit breaker state fields."""
        fields = []
        params = []
        if is_tripped is not None:
            fields.append("is_tripped = ?")
            params.append(is_tripped)
        if tripped_reason is not None:
            fields.append("tripped_reason = ?")
            params.append(tripped_reason)
        if tripped_at is not None:
            fields.append("tripped_at = ?")
            params.append(tripped_at)
        if consecutive_failures is not None:
            fields.append("consecutive_failures = ?")
            params.append(consecutive_failures)
        if last_failure_alert_at is not None:
            fields.append("last_failure_alert_at = ?")
            params.append(last_failure_alert_at)

        fields.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())

        query = f"UPDATE circuit_breaker SET {', '.join(fields)} WHERE id = 1;"
        with self.conn:
            self.conn.execute(query, tuple(params))

    def reset_circuit_breaker(self) -> None:
        """Resets the circuit breaker to clean operating state."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                UPDATE circuit_breaker
                SET is_tripped = 0,
                    tripped_at = NULL,
                    tripped_reason = NULL,
                    consecutive_failures = 0,
                    updated_at = ?
                WHERE id = 1;
                """,
                (now,),
            )

    # ── Scheduler State ──

    def get_scheduler_job(self, job_name: str) -> Optional[Dict[str, Any]]:
        """Retrieves persistent state for a scheduler job."""
        cur = self.conn.execute(
            "SELECT * FROM scheduler_state WHERE job_name = ?;",
            (job_name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def update_scheduler_job(
        self,
        job_name: str,
        status: Optional[str] = None,
        last_started_at: Optional[str] = None,
        last_completed_at: Optional[str] = None,
        next_due_at: Optional[str] = None,
        consecutive_failures: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """Upserts a job's scheduler state."""
        existing = self.get_scheduler_job(job_name)
        if existing is None:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO scheduler_state (
                        job_name, last_started_at, last_completed_at, last_status,
                        next_due_at, consecutive_failures, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        job_name,
                        last_started_at,
                        last_completed_at,
                        status or "never_run",
                        next_due_at,
                        consecutive_failures or 0,
                        last_error,
                    ),
                )
        else:
            fields = []
            params = []
            if status is not None:
                fields.append("last_status = ?")
                params.append(status)
            if last_started_at is not None:
                fields.append("last_started_at = ?")
                params.append(last_started_at)
            if last_completed_at is not None:
                fields.append("last_completed_at = ?")
                params.append(last_completed_at)
            if next_due_at is not None:
                fields.append("next_due_at = ?")
                params.append(next_due_at)
            if consecutive_failures is not None:
                fields.append("consecutive_failures = ?")
                params.append(consecutive_failures)
            if last_error is not None:
                fields.append("last_error = ?")
                params.append(last_error)

            if fields:
                params.append(job_name)
                with self.conn:
                    self.conn.execute(
                        f"UPDATE scheduler_state SET {', '.join(fields)} WHERE job_name = ?;",
                        tuple(params),
                    )

    # ── Database Backup & Metrics ──

    def backup_database(self, backup_dir: str, keep: int = 7) -> str:
        """Executes an online SQLite backup using the backup() API and prunes older backups."""
        os.makedirs(backup_dir, exist_ok=True)
        now_dt = datetime.now(timezone.utc)
        ts_str = now_dt.strftime("%Y%m%d_%H%M%S")
        target_path = os.path.join(backup_dir, f"jobwatch_backup_{ts_str}.db")

        target_conn = sqlite3.connect(target_path)
        try:
            self.conn.backup(target_conn)
        finally:
            target_conn.close()

        pattern = os.path.join(backup_dir, "jobwatch_backup_*.db")
        existing_backups = sorted(glob.glob(pattern), reverse=True)
        for old_file in existing_backups[keep:]:
            try:
                os.remove(old_file)
            except OSError:
                pass

        return target_path

    def get_recent_sweeps_count(self, hours: int = 24) -> Tuple[int, int]:
        """Returns (shallow_count, full_count) of successful runs in the last N hours."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = self.conn.execute(
            """
            SELECT run_type, COUNT(*) as cnt
            FROM source_runs
            WHERE outcome = 'success' AND started_at >= ?
            GROUP BY run_type;
            """,
            (since,),
        )
        shallow = 0
        full = 0
        for row in cur.fetchall():
            if row["run_type"] == "shallow":
                shallow = row["cnt"]
            elif row["run_type"] == "full_sweep":
                full = row["cnt"]
        return shallow, full

    def get_last_successful_run(self, run_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Returns the most recent successful run, optionally filtered by run_type."""
        if run_type:
            cur = self.conn.execute(
                "SELECT * FROM source_runs WHERE outcome = 'success' AND run_type = ? ORDER BY id DESC LIMIT 1;",
                (run_type,),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM source_runs WHERE outcome = 'success' ORDER BY id DESC LIMIT 1;"
            )
        row = cur.fetchone()
        return dict(row) if row else None



