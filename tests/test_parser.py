"""Unit tests for vacancy listing card parser."""

import os
import unittest
from datetime import date

from jobwatch.models import PageParseError
from jobwatch.parser import ListingParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(filename: str) -> str:
    with open(os.path.join(FIXTURES_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


class TestListingParser(unittest.TestCase):
    """Test suite verifying ListingParser against real and synthetic fixtures."""

    def test_parse_real_sample_listing(self):
        """Real fixture with 12 cards parses completely with all verified fields."""
        html_content = load_fixture("sample_listing.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        self.assertEqual(result.total_current_records, 12)
        self.assertEqual(result.total_match_records, 292)
        self.assertEqual(len(result.cards), 12)
        self.assertEqual(len(result.errors), 0)

        # Inspect first card in detail
        card1 = result.cards[0]
        self.assertEqual(card1.job_id, 269440)
        self.assertEqual(card1.job_ref, "259028")
        self.assertEqual(card1.job_ref_raw, "259028")
        self.assertEqual(
            card1.title,
            "EG259028 - Advanced Nurse Practitioner (ANP) / Advanced Clinical Practitioner (ACP)",
        )
        self.assertEqual(
            card1.title_raw,
            "EG259028 - Advanced Nurse Practitioner (ANP) / Advanced Clinical Practitioner (ACP)",
        )
        self.assertEqual(card1.detail_url, "/Job/JobDetail?JobId=269440")
        self.assertEqual(card1.closing_date_raw, "04/12/2026")
        self.assertEqual(card1.closing_date, date(2026, 12, 4))
        # salary_raw preserves verbatim spacing (two spaces around dashes)
        self.assertEqual(card1.salary_raw, "Band 7 (£52,845  -  - £61,466)")
        # salary collapses runs of whitespace to single spaces
        self.assertEqual(card1.salary, "Band 7 (£52,845 - - £61,466)")
        self.assertEqual(card1.band, "Band 7")
        self.assertEqual(card1.location, "Woodend Hospital")
        self.assertEqual(card1.employer, "NHS Grampian")
        self.assertEqual(card1.contract, "Various Contract Options")
        self.assertEqual(card1.hours_raw, "various")

        # Verify odd hours values observed in audit (never converted to numeric)
        self.assertEqual(result.cards[7].hours_raw, "118")  # Card 8
        self.assertEqual(result.cards[11].hours_raw, "0")   # Card 12

    def test_whitespace_verbatim_and_collapsed(self):
        """Verbatim values retain exact whitespace runs while collapsed fields normalize them to single spaces."""
        html_content = load_fixture("sample_listing.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        # Card 9 has a double space in the title: "Community  Nurse"
        card9 = result.cards[8]
        self.assertEqual(card9.job_id, 269140)
        self.assertEqual(card9.title_raw, "Community  Nurse")
        self.assertEqual(card9.title, "Community Nurse")

    def test_odd_salary_preservation_and_band_extraction(self):
        """Odd salary text (e.g. double dash, missing currency symbol) stores salary_raw unchanged and extracts band."""
        html_content = load_fixture("sample_listing.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        # Real card 1 has double dash in salary with multiple whitespace runs
        card1 = result.cards[0]
        self.assertEqual(card1.salary_raw, "Band 7 (£52,845  -  - £61,466)")
        self.assertEqual(card1.salary, "Band 7 (£52,845 - - £61,466)")
        self.assertEqual(card1.band, "Band 7")

        # Synthetic check for specifically requested pattern: "Band 2 (£26,696 - - 28,988)"
        synth_odd_salary_markup = html_content.replace(
            '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 7 (&#xA3;52,845  -  - &#xA3;61,466)</p>',
            '<p class="jobdetailsitem salary"><strong>Salary:</strong> Band 2 (£26,696 - - 28,988)</p>',
        )
        odd_parser = ListingParser(synth_odd_salary_markup)
        odd_res = odd_parser.parse()
        self.assertEqual(odd_res.cards[0].salary_raw, "Band 2 (£26,696 - - 28,988)")
        self.assertEqual(odd_res.cards[0].band, "Band 2")

    def test_zero_cards_valid_empty_result(self):
        """A page with totalCurrentRecords = 0 and 0 cards is a valid empty result, not an error."""
        html_content = load_fixture("synthetic_zero_cards.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        self.assertEqual(result.total_current_records, 0)
        self.assertEqual(len(result.cards), 0)
        self.assertEqual(len(result.errors), 0)

    def test_partial_last_page_parses_correctly(self):
        """A partial page (e.g. 4 cards) where card count matches totalCurrentRecords parses cleanly."""
        html_content = load_fixture("synthetic_partial_4_cards.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        self.assertEqual(result.total_current_records, 4)
        self.assertEqual(len(result.cards), 4)
        self.assertEqual(len(result.errors), 0)

    def test_card_count_mismatch_raises_page_parse_error(self):
        """Card count not equal to totalCurrentRecords raises PageParseError."""
        html_content = load_fixture("synthetic_mismatched_records.html")
        parser = ListingParser(html_content)
        with self.assertRaises(PageParseError):
            parser.parse()

    def test_html_entities_and_curly_apostrophes(self):
        """HTML entities (&amp;) and unicode curly apostrophes decode correctly in text."""
        html_content = load_fixture("synthetic_entities_and_quotes.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        card1 = result.cards[0]
        self.assertIn("Children’s & Women’s Health", card1.title)
        self.assertNotIn("&amp;", card1.title)

    def test_unparseable_closing_date_fails_loudly_without_dropping_good_cards(self):
        """An unparseable closing date records a CardParseError while preserving other valid cards."""
        html_content = load_fixture("synthetic_bad_closing_date.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        self.assertEqual(len(result.cards), 11)  # 11 good cards preserved
        self.assertEqual(len(result.errors), 1)  # 1 card parse error recorded
        error = result.errors[0]
        self.assertEqual(error.card_index, 1)
        self.assertEqual(error.field_name, "closing_date")
        self.assertIn("Unparseable closing date", error.reason)

    def test_duplicate_job_id_flagged(self):
        """A duplicate job_id on a single page is flagged in errors and preserved."""
        html_content = load_fixture("synthetic_duplicate_job_id.html")
        parser = ListingParser(html_content)
        result = parser.parse()

        self.assertEqual(len(result.cards), 11)
        self.assertEqual(len(result.errors), 1)
        error = result.errors[0]
        self.assertEqual(error.field_name, "job_id")
        self.assertIn("Duplicate job_id", error.reason)


if __name__ == "__main__":
    unittest.main()

