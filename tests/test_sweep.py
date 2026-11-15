"""Unit tests for the sweep coordinator, lifecycle management, and deduplication."""

import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from jobwatch.models import VacancyCard
from jobwatch.parser import ListingParser
from jobwatch.storage import (
    ConcurrentRunRefusedError,
    DatabaseStateCorruptError,
    Storage,
    UnbaselinedScopeError,
)
from jobwatch.sweep import (
    SweepCoordinator,
    compute_content_hash,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(filename: str) -> str:
    with open(os.path.join(FIXTURES_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


class TestSweepCoordinator(unittest.TestCase):
    """Verifies sweep execution, baselining, deduplication, unlisting, and drift handling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(self.temp_dir, "test.db")
        self.storage = Storage(db_path=db_path)
        self.raw_dir = os.path.join(self.temp_dir, "raw_runs")
        self.listing_html = load_fixture("sample_listing.html")
        self.past_end_html = load_fixture("sample_past_end.html")

    def tearDown(self):
        self.storage.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _single_page_source(self, scope_key: str, skip: int) -> str:
        """Page source returning sample_listing.html on skip 0 and sample_past_end on skip 12."""
        if skip == 0:
            # Modify totalMatchRecords to 12 so Page 1 expects exactly 12 cards
            return re.sub(
                r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                '<input type="hidden" id="totalMatchRecords" value="12">',
                self.listing_html,
            )
        return self.past_end_html

    def test_baseline_run(self):
        """First full sweep executes silent baseline: status='baseline', alerted_at populated, scope baselined."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        res = coord.execute_sweep("Departments=9", run_type="full_sweep")

        self.assertEqual(res.outcome, "success")
        self.assertTrue(res.is_complete)
        self.assertTrue(res.is_baseline)
        self.assertEqual(res.new_count, 12)
        self.assertEqual(res.changed_count, 0)
        self.assertEqual(res.unlisted_count, 0)

        # Verify scope metadata updated
        meta = self.storage.get_scope_metadata("Departments=9")
        self.assertEqual(meta["is_baselined"], 1)

        # Verify cards have status='baseline' and alerted_at populated
        cur = self.storage.conn.execute("SELECT status, alerted_at FROM user_vacancy_status WHERE job_id = 269440;")
        status_row = cur.fetchone()
        self.assertEqual(status_row["status"], "baseline")
        self.assertIsNotNone(status_row["alerted_at"])

    def test_identical_re_sweep(self):
        """Second identical sweep produces 0 new, 0 changed, 0 unlisted; updates last_seen_at."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Second sweep
        res2 = coord.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res2.outcome, "success")
        self.assertTrue(res2.is_complete)
        self.assertFalse(res2.is_baseline)
        self.assertEqual(res2.new_count, 0)
        self.assertEqual(res2.changed_count, 0)
        self.assertEqual(res2.unlisted_count, 0)

    def test_change_detection(self):
        """Modifying salary and closing date registers as changed, records in vacancy_changes, does not mark new."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Second sweep with modified card 1 salary and closing date
        def modified_source(scope_key, skip):
            if skip == 0:
                html = self._single_page_source(scope_key, skip)
                # Change salary and closing date for card 1
                html = html.replace(
                    '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 7 (&#xA3;52,845  -  - &#xA3;61,466)</p>',
                    '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 8A (£60,000 - £65,000)</p>'
                )
                html = html.replace(
                    '<p class="jobdetailsitem closingdate"><strong>Closing date:</strong> 04/12/2026</p>',
                    '<p class="jobdetailsitem closingdate"><strong>Closing date:</strong> 15/12/2026</p>'
                )
                return html
            return self.past_end_html

        coord_mod = SweepCoordinator(self.storage, modified_source, self.raw_dir)
        res = coord_mod.execute_sweep("Departments=9", run_type="full_sweep")

        self.assertEqual(res.changed_count, 1)
        self.assertEqual(res.new_count, 0)

        # Check vacancy_changes table
        cur = self.storage.conn.execute(
            "SELECT field_name, old_value, new_value FROM vacancy_changes WHERE job_id = 269440 ORDER BY field_name;"
        )
        changes = {row["field_name"]: (row["old_value"], row["new_value"]) for row in cur.fetchall()}
        self.assertIn("salary", changes)
        self.assertIn("band", changes)
        self.assertIn("closing_date", changes)
        self.assertEqual(changes["closing_date"], ("2026-12-04", "2026-12-15"))

    def test_relisting(self):
        """A previously unlisted card reappears and is restored to is_listed=1."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Manually mark card 269440 unlisted
        with self.storage.conn:
            self.storage.conn.execute("UPDATE vacancies SET is_listed = 0 WHERE job_id = 269440;")

        # Re-sweep sees it again
        coord.execute_sweep("Departments=9", run_type="full_sweep")
        v = self.storage.get_vacancy(269440)
        self.assertEqual(v["is_listed"], 1)

    def test_two_sweep_unlisting(self):
        """Card missing across 2 consecutive complete full sweeps is marked unlisted (is_listed=0)."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Missing card 269440 source (11 cards, totalMatchRecords 11)
        def missing_source(scope_key, skip):
            if skip == 0:
                html = self._single_page_source(scope_key, skip)
                # Remove card 1
                html = re.sub(
                    r'<div tabindex="-1" class="card mb-7 mb-md-20 job-card[\s\S]*?EG259028[\s\S]*?<!-- end of Extra values to display -->[\s\S]*?</div>\s*</div>',
                    '', html, count=1
                )
                html = re.sub(r'<input type="hidden" id="totalCurrentRecords" value="\d+">',
                              '<input type="hidden" id="totalCurrentRecords" value="11">', html)
                html = re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="11">', html)
                return html
            return self.past_end_html

        coord_miss = SweepCoordinator(self.storage, missing_source, self.raw_dir)

        # Sweep 1: missing counter becomes 1, still is_listed = 1
        res1 = coord_miss.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res1.unlisted_count, 0)
        v1 = self.storage.get_vacancy(269440)
        self.assertEqual(v1["is_listed"], 1)

        # Sweep 2: missing counter becomes 2, is_listed becomes 0
        res2 = coord_miss.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res2.unlisted_count, 1)
        v2 = self.storage.get_vacancy(269440)
        self.assertEqual(v2["is_listed"], 0)

    def test_partial_sweep_never_unlists(self):
        """An interrupted sweep (raising an error) never increments missing counters and never unlists."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        def broken_source(scope_key, skip):
            raise ConnectionError("Network failure")

        broken_coord = SweepCoordinator(self.storage, broken_source, self.raw_dir)
        with self.assertRaises(ConnectionError):
            broken_coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Missing counters must still be 0 and cards listed
        cur = self.storage.conn.execute("SELECT consecutive_missing_sweeps FROM vacancy_scopes WHERE job_id = 269440;")
        self.assertEqual(cur.fetchone()[0], 0)
        v = self.storage.get_vacancy(269440)
        self.assertEqual(v["is_listed"], 1)

    def test_in_sweep_deduplication(self):
        """Cards duplicated across multiple pages in a sweep are deduplicated and processed once."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)

        # Page 1 has 12 cards, Page 2 duplicates card 1 and has 1 other card (total expected 13)
        def multi_page_dup(scope_key, skip):
            if skip == 0:
                html = re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="13">', self.listing_html)
                return html
            elif skip == 12:
                # Return synthetic 4 cards fixture but set totalMatchRecords to 13
                p4 = load_fixture("synthetic_partial_4_cards.html")
                return re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="13">', p4)
            return self.past_end_html

        # In-sweep deduplication ensures no duplicate primary key error
        coord_dup = SweepCoordinator(self.storage, multi_page_dup, self.raw_dir)
        # Even if page 2 has duplicate IDs, coordinator deduplicates in-sweep
        res = coord_dup.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res.new_count, 12)

    def test_content_hash_invariance(self):
        """Content hash is invariant to whitespace differences in raw fields."""
        card1 = VacancyCard(
            job_id=1,
            title_raw="Staff  Nurse",
            title="Staff Nurse",
            detail_url="/url",
            closing_date_raw="01/01/2027",
            salary_raw="Band  5",
            salary="Band 5",
        )
        card2 = VacancyCard(
            job_id=1,
            title_raw="Staff \t Nurse",
            title="Staff Nurse",
            detail_url="/url",
            closing_date_raw="01/01/2027",
            salary_raw="Band    5",
            salary="Band 5",
        )
        self.assertEqual(compute_content_hash(card1), compute_content_hash(card2))

    def test_exact_multiple_of_12_cards_terminates_on_empty_page(self):
        """A sweep where card count is an exact multiple of 12 (e.g. 12) terminates cleanly on empty page without drift."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        res = coord.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res.outcome, "success")
        self.assertTrue(res.is_complete)
        self.assertFalse(res.rerun_needed)

    def test_past_the_end_behavior(self):
        """Past the end fixture (sample_past_end.html) parses cleanly as 0 cards, 0 current records."""
        parser = ListingParser(self.past_end_html)
        res = parser.parse()
        self.assertEqual(res.total_current_records, 0)
        self.assertEqual(len(res.cards), 0)

    def test_stalled_page_repeats(self):
        """A page repeating previous job IDs without adding new ones aborts sweep as stalled."""
        def repeating_source(scope_key, skip):
            # Page 1 and Page 2 return identical full pages expecting 24 items
            return re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                          '<input type="hidden" id="totalMatchRecords" value="24">', self.listing_html)

        coord = SweepCoordinator(self.storage, repeating_source, self.raw_dir)
        with self.assertRaises(RuntimeError) as ctx:
            coord.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertIn("Sweep stalled", str(ctx.exception))

    def test_scope_separation(self):
        """Cards discovered under different scope keys are tracked separately in vacancy_scopes."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")
        coord.execute_sweep("Departments=11", run_type="full_sweep")

        cur = self.storage.conn.execute("SELECT DISTINCT scope_key FROM vacancy_scopes;")
        scopes = {row["scope_key"] for row in cur.fetchall()}
        self.assertIn("Departments=9", scopes)
        self.assertIn("Departments=11", scopes)

    def test_per_scope_baseline(self):
        """A new scope triggers its own silent baseline; if table is empty but marker exists, raises DatabaseStateCorruptError."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        # Scope 1 baseline
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Scope 2 baseline (silent baseline even though vacancies exist)
        res2 = coord.execute_sweep("Departments=11", run_type="full_sweep")
        self.assertTrue(res2.is_baseline)

        # Corrupt state test: delete all vacancies while scope is baselined
        with self.storage.conn:
            self.storage.conn.execute("DELETE FROM user_vacancy_status;")
            self.storage.conn.execute("DELETE FROM vacancy_scopes;")
            self.storage.conn.execute("DELETE FROM vacancy_changes;")
            self.storage.conn.execute("DELETE FROM vacancies;")

        with self.assertRaises(DatabaseStateCorruptError):
            coord.execute_sweep("Departments=9", run_type="full_sweep")

    def test_job_ref_edit(self):
        """Editing job_ref on a known job_id records a change in vacancy_changes."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        def ref_edit_source(scope_key, skip):
            if skip == 0:
                html = self._single_page_source(scope_key, skip)
                return html.replace(
                    '<p class="jobdetailsitem jobreference"><strong>Job reference:</strong> 259028</p>',
                    '<p class="jobdetailsitem jobreference"><strong>Job reference:</strong> 259028-NEW</p>'
                )
            return self.past_end_html

        coord_edit = SweepCoordinator(self.storage, ref_edit_source, self.raw_dir)
        res = coord_edit.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res.changed_count, 1)

        cur = self.storage.conn.execute("SELECT field_name, new_value FROM vacancy_changes WHERE job_id = 269440;")
        ch = cur.fetchone()
        self.assertEqual(ch["field_name"], "job_ref")
        self.assertEqual(ch["new_value"], "259028-NEW")

    def test_possible_repost_detection(self):
        """New job_id sharing job_ref OR (title + employer + dept_label + location) is flagged as possible_repost."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Insert a new job_id sharing job_ref 259028
        def repost_source(scope_key, skip):
            if skip == 0:
                html = self._single_page_source(scope_key, skip)
                # Change job_id 269440 to 999999 while keeping same job_ref 259028
                html = html.replace('data-jobId="269440"', 'data-jobId="999999"')
                html = html.replace('href="/Job/JobDetail?JobId=269440"', 'href="/Job/JobDetail?JobId=999999"')
                return html
            return self.past_end_html

        coord_rep = SweepCoordinator(self.storage, repost_source, self.raw_dir)
        coord_rep.execute_sweep("Departments=9", run_type="full_sweep")

        v = self.storage.get_vacancy(999999)
        self.assertIsNotNone(v["repost_flag"])
        self.assertIn("possible_repost", v["repost_flag"])

    def test_repost_negative_same_title_employer_different_dept_location(self):
        """Real cards 275434 and 275359 (both Staff Nurse, NHS Highland, different wards/locations) do NOT flag as repost."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        v1 = self.storage.get_vacancy(275434)
        v2 = self.storage.get_vacancy(275359)
        self.assertIsNone(v1["repost_flag"])
        self.assertIsNone(v2["repost_flag"])

    def test_stale_running_run_recovered_by_concurrency_guard(self):
        """A run still 'running' beyond timeout (60m) is marked failure by concurrency guard and new run proceeds."""
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO source_runs (id, scope_key, run_type, started_at, outcome)
                VALUES (100, 'Departments=9', 'full_sweep', '2020-01-01T00:00:00+00:00', 'running');
                """
            )
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        # Should not raise ConcurrentRunRefusedError because run 100 is older than 60 minutes
        res = coord.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res.outcome, "success")

        # Stale run was marked failure
        cur = self.storage.conn.execute("SELECT outcome, error_message FROM source_runs WHERE id = 100;")
        row = cur.fetchone()
        self.assertEqual(row["outcome"], "failure")
        self.assertEqual(row["error_message"], "stale_running_run_timed_out")

    def test_concurrent_run_refused(self):
        """Attempting a run while another non-stale run is actively running raises ConcurrentRunRefusedError."""
        now_recent = datetime.now(timezone.utc).isoformat()
        with self.storage.conn:
            self.storage.conn.execute(
                """
                INSERT INTO source_runs (id, scope_key, run_type, started_at, outcome)
                VALUES (101, 'Departments=9', 'full_sweep', ?, 'running');
                """,
                (now_recent,)
            )
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        with self.assertRaises(ConcurrentRunRefusedError):
            coord.execute_sweep("Departments=9", run_type="full_sweep")

    def test_shallow_run_never_unlists(self):
        """Shallow run updates last_seen_at but never increments missing counters and never unlists."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        coord.execute_sweep("Departments=9", run_type="full_sweep")

        # Run shallow sweep
        res = coord.execute_sweep("Departments=9", run_type="shallow", shallow_max_pages=1)
        self.assertEqual(res.outcome, "success")
        self.assertFalse(res.is_complete)
        self.assertEqual(res.unlisted_count, 0)

        # Missing counters remain 0
        cur = self.storage.conn.execute("SELECT MAX(consecutive_missing_sweeps) FROM vacancy_scopes;")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_shallow_run_refused_on_unbaselined_scope(self):
        """Attempting a shallow run on an unbaselined scope raises UnbaselinedScopeError."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        with self.assertRaises(UnbaselinedScopeError):
            coord.execute_sweep("UnbaselinedScope", run_type="shallow")

    def test_terminal_empty_page_does_not_cause_drift(self):
        """Terminal empty page returning totalMatchRecords=0 does not trigger drift."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        res = coord.execute_sweep("Departments=9", run_type="full_sweep")
        self.assertEqual(res.outcome, "success")
        self.assertFalse(res.rerun_needed)

    def test_overlapping_scopes(self):
        """Vacancy present in both Departments=9 and what=nurse remains listed if missing from only one scope."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        # Sweep scope 1
        coord.execute_sweep("Departments=9", run_type="full_sweep")
        # Sweep scope 2 with same cards
        coord.execute_sweep("what=nurse", run_type="full_sweep")

        # Now sweep scope 1 missing card 269440 twice
        def missing_scope1(scope_key, skip):
            if skip == 0:
                html = self._single_page_source(scope_key, skip)
                html = re.sub(
                    r'<div tabindex="-1" class="card mb-7 mb-md-20 job-card[\s\S]*?EG259028[\s\S]*?<!-- end of Extra values to display -->[\s\S]*?</div>\s*</div>',
                    '', html, count=1
                )
                html = re.sub(r'<input type="hidden" id="totalCurrentRecords" value="\d+">',
                              '<input type="hidden" id="totalCurrentRecords" value="11">', html)
                html = re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="11">', html)
                return html
            return self.past_end_html

        coord_m1 = SweepCoordinator(self.storage, missing_scope1, self.raw_dir)
        coord_m1.execute_sweep("Departments=9", run_type="full_sweep")
        coord_m1.execute_sweep("Departments=9", run_type="full_sweep")

        # Because card 269440 is active in what=nurse, it must NOT be unlisted
        v = self.storage.get_vacancy(269440)
        self.assertEqual(v["is_listed"], 1)

    def test_incomplete_baseline_leaves_scope_unbaselined(self):
        """An interrupted baseline sweep keeps inserted items with status='baseline' but leaves scope unbaselined."""
        def partial_baseline_source(scope_key, skip):
            if skip == 0:
                # Says 24 items, but we only supply page 1 and then fail
                return re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="24">', self.listing_html)
            raise ConnectionError("Interrupted mid-sweep")

        coord = SweepCoordinator(self.storage, partial_baseline_source, self.raw_dir)
        with self.assertRaises(ConnectionError):
            coord.execute_sweep("Departments=9", run_type="full_sweep")

        meta = self.storage.get_scope_metadata("Departments=9")
        self.assertEqual(meta["is_baselined"], 0)

        # Cards inserted have status 'baseline'
        v = self.storage.get_vacancy(269440)
        self.assertIsNotNone(v)

    def test_drift_keeps_upserts_and_flags_rerun(self):
        """Drift on a card-bearing page keeps page upserts, records outcome 'drifted', skips finalization, sets rerun_needed=True."""
        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)

        # Page 1 has totalMatchRecords 24, Page 2 is card-bearing but has totalMatchRecords 25
        def drifting_source(scope_key, skip):
            if skip == 0:
                return re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="24">', self.listing_html)
            elif skip == 12:
                # Page 2 has cards and reports drifted totalMatchRecords 25
                p4 = load_fixture("synthetic_partial_4_cards.html")
                return re.sub(r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                              '<input type="hidden" id="totalMatchRecords" value="25">', p4)
            return self.past_end_html

        coord_drift = SweepCoordinator(self.storage, drifting_source, self.raw_dir)
        res = coord_drift.execute_sweep("Departments=9", run_type="full_sweep")

        self.assertEqual(res.outcome, "drifted")
        self.assertTrue(res.rerun_needed)
        self.assertFalse(res.is_complete)

        # Page 1 upserts were kept in DB
        v = self.storage.get_vacancy(269440)
        self.assertIsNotNone(v)

    def test_retention_partitioning(self):
        """Prunes raw HTML snapshots retaining 10 complete and 5 failed/drifted runs."""
        os.makedirs(self.raw_dir, exist_ok=True)
        # Create 12 complete runs and 7 failed runs dummy files
        with self.storage.conn:
            for rid in range(1, 13):
                self.storage.conn.execute(
                    "INSERT INTO source_runs (id, scope_key, run_type, started_at, outcome) VALUES (?, 'scope', 'full_sweep', '2026-01-01', 'success');",
                    (rid,)
                )
                with open(os.path.join(self.raw_dir, f"run_{rid}_page_1.html"), "w") as f:
                    f.write("test")
            for rid in range(20, 28):
                self.storage.conn.execute(
                    "INSERT INTO source_runs (id, scope_key, run_type, started_at, outcome) VALUES (?, 'scope', 'full_sweep', '2026-01-01', 'failure');",
                    (rid,)
                )
                with open(os.path.join(self.raw_dir, f"run_{rid}_page_1.html"), "w") as f:
                    f.write("test")

        coord = SweepCoordinator(self.storage, self._single_page_source, self.raw_dir)
        kept, pruned = coord.prune_raw_html_snapshots()
        self.assertEqual(kept, 15)  # 10 complete + 5 failed
        self.assertEqual(pruned, 5)  # 2 excess complete + 3 excess failed


if __name__ == "__main__":
    unittest.main()
