"""Step 2 demonstration entrypoint: executes offline sweeps against fixtures and demonstrates deduplication/changes."""

import os
import re
import shutil
import sys
import tempfile

# Ensure repo root is on sys.path
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from jobwatch.storage import Storage
from jobwatch.sweep import SweepCoordinator

FIXTURES_DIR = os.path.join(REPO_DIR, "tests", "fixtures")


def load_fixture(name: str) -> str:
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    print("=" * 80)
    print("NHS-JOBWATCH STEP 2 DEMONSTRATION: STORAGE & DEDUPLICATION (OFFLINE)")
    print("=" * 80)

    listing_html = load_fixture("sample_listing.html")
    past_end_html = load_fixture("sample_past_end.html")

    temp_dir = tempfile.mkdtemp(prefix="jobwatch_step2_demo_")
    db_path = os.path.join(temp_dir, "demo.db")
    raw_dir = os.path.join(temp_dir, "raw_runs")

    try:
        storage = Storage(db_path=db_path)

        def fixture_source(scope_key: str, skip: int) -> str:
            if skip == 0:
                # Set totalMatchRecords = 12 so Page 1 expects exactly 12 cards
                return re.sub(
                    r'<input type="hidden" id="totalMatchRecords" value="\d+">',
                    '<input type="hidden" id="totalMatchRecords" value="12">',
                    listing_html,
                )
            return past_end_html

        coord = SweepCoordinator(storage, fixture_source, raw_retention_dir=raw_dir)

        # 1. Sweep 1: Baseline run
        print("\n[RUN 1] Executing initial baseline sweep for 'Departments=9'...")
        res1 = coord.execute_sweep("Departments=9", run_type="full_sweep")
        print(f"  Run ID:           #{res1.run_id}")
        print(f"  Outcome:          {res1.outcome}")
        print(f"  Is Baseline:      {res1.is_baseline}")
        print(f"  Pages Fetched:    {res1.pages_fetched}")
        print(f"  Vacancies Seen:   {res1.vacancies_seen}")
        print(f"  New Vacancies:    {res1.new_count}")
        print(f"  Changed:          {res1.changed_count}")
        print(f"  Unlisted:         {res1.unlisted_count}")

        # 2. Sweep 2: Identical re-sweep
        print("\n[RUN 2] Executing second sweep with identical fixtures...")
        res2 = coord.execute_sweep("Departments=9", run_type="full_sweep")
        print(f"  Run ID:           #{res2.run_id}")
        print(f"  Outcome:          {res2.outcome}")
        print(f"  Is Baseline:      {res2.is_baseline}")
        print(f"  New Vacancies:    {res2.new_count} (expected 0)")
        print(f"  Changed:          {res2.changed_count} (expected 0)")
        print(f"  Unlisted:         {res2.unlisted_count} (expected 0)")

        # 3. Sweep 3: Modified salary and closing date on Card 1
        print("\n[RUN 3] Executing third sweep with modified salary and closing date on Card 1...")
        def modified_source(scope_key: str, skip: int) -> str:
            if skip == 0:
                html = fixture_source(scope_key, skip)
                # Modify Card 1 (JobId 269440) salary and closing date
                html = html.replace(
                    '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 7 (&#xA3;52,845  -  - &#xA3;61,466)</p>',
                    '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 8A (£60,000 - £65,000)</p>'
                )
                html = html.replace(
                    '<p class="jobdetailsitem closingdate"><strong>Closing date:</strong> 04/12/2026</p>',
                    '<p class="jobdetailsitem closingdate"><strong>Closing date:</strong> 25/12/2026</p>'
                )
                return html
            return past_end_html

        coord3 = SweepCoordinator(storage, modified_source, raw_retention_dir=raw_dir)
        res3 = coord3.execute_sweep("Departments=9", run_type="full_sweep")
        print(f"  Run ID:           #{res3.run_id}")
        print(f"  Outcome:          {res3.outcome}")
        print(f"  New Vacancies:    {res3.new_count}")
        print(f"  Changed:          {res3.changed_count} (expected 1)")
        print(f"  Unlisted:         {res3.unlisted_count}")

        cur = storage.conn.execute(
            "SELECT job_id, field_name, old_value, new_value, detected_at FROM vacancy_changes WHERE job_id = 269440;"
        )
        print("\n  Recorded Vacancy Changes:")
        for ch in cur.fetchall():
            print(f"    - JobId {ch['job_id']}: {ch['field_name']} changed from '{ch['old_value']}' -> '{ch['new_value']}'")

        print("\n" + "=" * 80)
        print("STEP 2 DEMONSTRATION COMPLETE: ALL STORAGE & DEDUPLICATION RULES VERIFIED")
        print("=" * 80)

        storage.close()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

