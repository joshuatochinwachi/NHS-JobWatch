"""Step 1 entrypoint: parses the saved listing fixture and prints structured records."""

import os
import sys

# Ensure repo root is on sys.path
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from jobwatch.parser import ListingParser


def main():
    fixture_path = os.path.join(REPO_DIR, "tests", "fixtures", "sample_listing.html")
    if not os.path.exists(fixture_path):
        print(f"Error: Fixture not found at {fixture_path}")
        sys.exit(1)

    with open(fixture_path, "r", encoding="utf-8") as f:
        html = f.read()

    parser = ListingParser(html)
    result = parser.parse()

    print(f"=" * 80)
    print(f"NHS-JOBWATCH STEP 1: LISTING PARSE OUTPUT")
    print(f"=" * 80)
    print(f"Total Match Records (Pool):    {result.total_match_records}")
    print(f"Total Current Records (Page):  {result.total_current_records}")
    print(f"Successfully Parsed Cards:     {len(result.cards)}")
    print(f"Card Parse Errors:             {len(result.errors)}")
    print(f"=" * 80)

    for idx, card in enumerate(result.cards, 1):
        print(f"\n--- Card {idx:02d} [JobId: {card.job_id}] ---")
        print(f"  Title:        {card.title} (raw: {repr(card.title_raw)})")
        print(f"  Reference:    {card.job_ref}")
        print(f"  Detail URL:   {card.detail_url}")
        print(f"  Salary Raw:   {card.salary_raw}")
        print(f"  Band:         {card.band}")
        print(f"  Closing Date: {card.closing_date} (raw: '{card.closing_date_raw}')")
        print(f"  Location:     {card.location}")
        print(f"  Department:   {card.dept_label}")
        print(f"  Employer:     {card.employer}")
        print(f"  Contract:     {card.contract}")
        print(f"  Hours Raw:    {card.hours_raw}")

    if result.errors:
        print(f"\n" + "=" * 80)
        print(f"PARSE ERRORS ({len(result.errors)}):")
        for err in result.errors:
            print(f"  Card #{err.card_index} [{err.field_name}]: {err.reason}")

    print(f"\n" + "=" * 80)


if __name__ == "__main__":
    main()

