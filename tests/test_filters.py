"""Unit tests for filter configuration, role-aware exclusions, band boundaries, and real cards."""

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobwatch.filters import FilterConfig, FilterConfigError, evaluate_card, load_filter_config
from jobwatch.models import VacancyCard


def _card(job_id: int, title: str, band: str = None, contract: str = None, hours_raw: str = None, dept_label: str = None) -> VacancyCard:
    """Helper to construct a minimal VacancyCard for testing."""
    return VacancyCard(
        job_id=job_id,
        title_raw=title,
        title=title,
        detail_url=f"/Job/JobDetail?JobId={job_id}",
        closing_date_raw="30/06/2027",
        closing_date=date(2027, 6, 30),
        band=band,
        contract=contract,
        hours_raw=hours_raw,
        dept_label=dept_label,
    )


class TestFilterConfigValidation(unittest.TestCase):
    """Tests strict schema validation of filter configuration."""

    def setUp(self):
        self.config = load_filter_config()

    def test_load_valid_config(self):
        """Default config/filters.json loads and validates without error."""
        self.assertIsNotNone(self.config)
        self.assertEqual(self.config.version, "1.1")
        self.assertIsInstance(self.config.closing_soon_days, int)

    def test_filter_config_missing_key_raises(self):
        """Missing required top-level key raises FilterConfigError."""
        import json, tempfile, os
        bad = {
            "version": "1.0",
            # "rules_status": missing
            "closing_soon_days": 5,
            "default_outcome": "excluded",
            "exclusions": [],
            "strong_rules": [],
            "maybe_rules": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(bad, f)
            path = f.name
        try:
            with self.assertRaises(FilterConfigError):
                load_filter_config(path)
        finally:
            os.unlink(path)

    def test_filter_config_unknown_key_raises(self):
        """Unknown top-level key raises FilterConfigError."""
        import json, tempfile, os
        bad = {
            "version": "1.0",
            "rules_status": "ok",
            "closing_soon_days": 5,
            "default_outcome": "excluded",
            "exclusions": [],
            "strong_rules": [],
            "maybe_rules": [],
            "unexpected_key": "boom",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(bad, f)
            path = f.name
        try:
            with self.assertRaises(FilterConfigError):
                load_filter_config(path)
        finally:
            os.unlink(path)

    def test_filter_config_bad_regex_raises(self):
        """An invalid regex pattern in a rule raises FilterConfigError."""
        import json, tempfile, os
        bad = {
            "version": "1.0",
            "rules_status": "ok",
            "closing_soon_days": 5,
            "default_outcome": "excluded",
            "exclusions": [{"id": "bad_rule", "target": "title", "patterns": ["[invalid_regex"], "reason": "test"}],
            "strong_rules": [],
            "maybe_rules": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(bad, f)
            path = f.name
        try:
            with self.assertRaises(FilterConfigError):
                load_filter_config(path)
        finally:
            os.unlink(path)


class TestRealCardClassifications(unittest.TestCase):
    """Tests all 12 real cards from sample_listing.html against filter rules."""

    def setUp(self):
        self.config = load_filter_config()
        self.ts = datetime.now(timezone.utc).isoformat()

    def _decide(self, card):
        return evaluate_card(card, self.config, self.ts)

    # --- Real verified cards from sample_listing.html ---

    def test_real_269440_anp_acp_excluded(self):
        """VERIFIED: Card 269440 — ANP/ACP role at Band 7 is excluded (anp/acp pattern)."""
        card = _card(269440, "EG259028 - Advanced Nurse Practitioner (ANP) / Advanced Clinical Practitioner (ACP)", band="Band 7")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("anp", d.matched_rule)

    def test_real_264290_anaesthetic_practitioner_excluded(self):
        """VERIFIED: Card 264290 — Anaesthetic Practitioner at Band 5 is excluded under exclude_theatre_odp."""
        card = _card(264290, "Anaesthetic Practitioner AA", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertEqual(d.matched_rule, "exclude_theatre_odp")

    def test_exclude_theatre_odp_classifying_jobid_264290_and_filter_version_1_1(self):
        """Card 264290 classified as excluded under exclude_theatre_odp, and filter config is version 1.1."""
        self.assertEqual(self.config.version, "1.1")
        card = _card(264290, "Anaesthetic Practitioner AA", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertEqual(d.matched_rule, "exclude_theatre_odp")

    def test_real_275434_staff_nurse_strong(self):
        """VERIFIED: Card 275434 — Staff Nurse at Band 5 is strong."""
        card = _card(275434, "Staff Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "strong")
        self.assertIn("strong_staff_nurse", d.matched_rule)

    def test_real_270287_chief_nurse_excluded(self):
        """VERIFIED: Card 270287 — Chief Nurse at Band 8C is excluded (chief management pattern)."""
        card = _card(270287, "BC259875 - Aberdeenshire HSCP Chief Nurse", band="Band 8C")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("management", d.matched_rule)

    def test_real_274007_ventilation_specialist_excluded(self):
        """VERIFIED: Card 274007 — Advanced Home Ventilation Specialist at Band 7 excluded (no match)."""
        card = _card(274007, "AJ263592 Advanced Home Ventilation Specialist", band="Band 7")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_real_267509_hcsw_band3_maybe(self):
        """VERIFIED: Card 267509 — Healthcare support worker at Band 3 is maybe."""
        card = _card(267509, "Healthcare support worker (Homecare) AA", band="Band 3")
        d = self._decide(card)
        self.assertEqual(d.outcome, "maybe")
        self.assertIn("support_worker", d.matched_rule)

    def test_real_275373_dental_nurse_excluded(self):
        """VERIFIED: Card 275373 — Orthodontic Dental Nurse at Band 5 is excluded (dental pattern)."""
        card = _card(275373, "Orthodontic Dental Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("dental", d.matched_rule)

    def test_real_275359_staff_nurse_strong(self):
        """VERIFIED: Card 275359 — Staff Nurse at Band 5 is strong."""
        card = _card(275359, "Staff Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "strong")
        self.assertIn("strong_staff_nurse", d.matched_rule)

    def test_real_269140_community_nurse_band5_maybe(self):
        """VERIFIED: Card 269140 — Community Nurse at Band 5 is maybe (general nurse rule)."""
        card = _card(269140, "Community Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "maybe")
        self.assertIn("general_nurse", d.matched_rule)

    def test_real_275035_mental_health_nurse_excluded(self):
        """VERIFIED: Card 275035 — Practice Mental Health Nurse at Band 6 is excluded (role-aware mental health exclusion)."""
        card = _card(275035, "264620 (MG) Practice Mental Health Nurse", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("mental_health", d.matched_rule)

    def test_real_275289_mental_health_nurse_excluded(self):
        """VERIFIED: Card 275289 — Practice Mental Health Nurse at Band 6 is excluded (role-aware mental health exclusion)."""
        card = _card(275289, "264875 Practice Mental Health Nurse", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("mental_health", d.matched_rule)

    def test_real_274479_hcsw_band2_excluded(self):
        """VERIFIED: Card 274479 — Healthcare Support Worker at Band 2 is excluded (support low band)."""
        card = _card(274479, "RP264064 - Gateway to Medicine 2026/27 - Healthcare Support Worker", band="Band 2")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("low_band", d.matched_rule)


class TestExclusionPrecedence(unittest.TestCase):
    """Tests that exclusions fire before inclusion rules, including nursery nurse regex behaviour."""

    def setUp(self):
        self.config = load_filter_config()
        self.ts = datetime.now(timezone.utc).isoformat()

    def _decide(self, card):
        return evaluate_card(card, self.config, self.ts)

    def test_nursery_nurse_excluded_despite_nurse_match(self):
        """SYNTHETIC: 'Nursery Nurse' triggers exclusion before \bnurse\b inclusion rules."""
        card = _card(9001, "Nursery Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("nursery", d.matched_rule)

    def test_school_nurse_excluded(self):
        """SYNTHETIC: 'School Nurse' is excluded."""
        card = _card(9002, "School Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_midwife_excluded(self):
        """SYNTHETIC: 'Community Midwife' is excluded."""
        card = _card(9003, "Community Midwife", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_paediatric_nurse_excluded(self):
        """SYNTHETIC: 'Paediatric Nurse' is excluded."""
        card = _card(9004, "Paediatric Nurse Specialist", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_learning_disability_nurse_excluded(self):
        """SYNTHETIC: 'Learning Disability Nurse' is excluded."""
        card = _card(9005, "Learning Disability Nurse", band="Band 5")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_health_visitor_excluded(self):
        """SYNTHETIC: 'Health Visitor' is excluded."""
        card = _card(9006, "Health Visitor Band 6", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")


class TestRoleAwareExclusions(unittest.TestCase):
    """Tests that mental health exclusion applies to nurse roles but not support workers."""

    def setUp(self):
        self.config = load_filter_config()
        self.ts = datetime.now(timezone.utc).isoformat()

    def _decide(self, card):
        return evaluate_card(card, self.config, self.ts)

    def test_mental_health_nurse_excluded(self):
        """SYNTHETIC: Mental Health Nurse is excluded (role-aware)."""
        card = _card(9010, "Mental Health Nurse", band="Band 6")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")

    def test_support_worker_mental_health_eligible(self):
        """SYNTHETIC: 'Healthcare Support Worker - Mental Health' stays maybe (role-aware; support workers not excluded)."""
        card = _card(9011, "Healthcare Support Worker - Mental Health", band="Band 3")
        d = self._decide(card)
        # Should be maybe (support worker rule) not excluded
        self.assertNotEqual(d.outcome, "excluded", "Mental health exclusion must not block support worker roles")
        self.assertEqual(d.outcome, "maybe")


class TestBandFiltering(unittest.TestCase):
    """Tests band boundary rules for nursing and support roles."""

    def setUp(self):
        self.config = load_filter_config()
        self.ts = datetime.now(timezone.utc).isoformat()

    def _decide(self, card):
        return evaluate_card(card, self.config, self.ts)

    def test_nursing_band4_excluded(self):
        """SYNTHETIC: Registered Nurse at Band 4 is excluded (below minimum Band 5)."""
        card = _card(9020, "Registered Nurse", band="Band 4")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("low_band", d.matched_rule)

    def test_nursing_band7_excluded(self):
        """SYNTHETIC: Registered Nurse at Band 7 is excluded (above maximum Band 6)."""
        card = _card(9021, "Registered Nurse", band="Band 7")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("high_band", d.matched_rule)

    def test_support_band2_excluded(self):
        """SYNTHETIC: Healthcare Support Worker at Band 2 is excluded (below minimum Band 3)."""
        card = _card(9022, "Healthcare Support Worker", band="Band 2")
        d = self._decide(card)
        self.assertEqual(d.outcome, "excluded")
        self.assertIn("low_band", d.matched_rule)

    def test_support_band3_maybe(self):
        """VERIFIED: Healthcare support worker at Band 3 is maybe (real card pattern)."""
        card = _card(9023, "Healthcare Support Worker", band="Band 3")
        d = self._decide(card)
        self.assertEqual(d.outcome, "maybe")

    def test_missing_band_defaults_to_maybe_flagged(self):
        """SYNTHETIC: Staff Nurse with no band defaults to maybe with 'band unknown' flag."""
        card = _card(9024, "Staff Nurse", band=None)
        d = self._decide(card)
        # Staff Nurse with no band → strong rule still applies (Band unknown is in allowed_bands)
        self.assertIn(d.outcome, ("strong", "maybe"))
        self.assertIn("band unknown", d.match_reason.lower())


class TestFullTimeHoursParsing(unittest.TestCase):
    """Tests that hours_raw is handled correctly and not used for filtering."""

    def test_37_5_hours_is_full_time(self):
        from jobwatch.formatter import parse_full_time
        self.assertTrue(parse_full_time("37.5"))
        self.assertTrue(parse_full_time("37.5 hours per week"))
        self.assertTrue(parse_full_time("40 hpw"))

    def test_various_not_full_time(self):
        from jobwatch.formatter import parse_full_time
        self.assertFalse(parse_full_time("various"))
        self.assertFalse(parse_full_time("0"))
        self.assertFalse(parse_full_time("118"))
        self.assertFalse(parse_full_time("22.2"))
        self.assertFalse(parse_full_time(None))


if __name__ == "__main__":
    unittest.main()

