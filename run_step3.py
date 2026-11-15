"""Step 3 offline demonstration: classifies all 12 real cards and shows filter decisions."""

import os
import re
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from jobwatch.filters import evaluate_card, load_filter_config
from jobwatch.formatter import parse_full_time
from jobwatch.models import VacancyCard
from jobwatch.notifier import DryRunNotifier, FakeNotifier
from jobwatch.parser import ListingParser

FIXTURES_DIR = os.path.join(REPO_DIR, "tests", "fixtures")


def load_fixture(name: str) -> str:
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


OUTCOME_ICON = {
    "strong": "⭐ STRONG ",
    "maybe":  "📌 MAYBE  ",
    "excluded": "❌ EXCLUDED",
}


def main():
    print("=" * 80)
    print("NHS-JOBWATCH STEP 3 DEMONSTRATION: FILTERS & ALERTS (OFFLINE)")
    print("=" * 80)

    # 1. Load and validate filter config
    print("\n[1] Loading and validating config/filters.json...")
    config = load_filter_config()
    print(f"    version={config.version}  closing_soon_days={config.closing_soon_days}  "
          f"default={config.default_outcome}")
    print(f"    {len(config.exclusions)} exclusion rule(s), "
          f"{len(config.strong_rules)} strong rule(s), "
          f"{len(config.maybe_rules)} maybe rule(s).")

    # 2. Parse the real 12 cards from sample_listing.html
    print("\n[2] Parsing sample_listing.html ...")
    html_text = load_fixture("sample_listing.html")
    parser = ListingParser(html_text)
    result = parser.parse()
    print(f"    Parsed {len(result.cards)} cards. Parse errors: {len(result.errors)}.")

    # 3. Evaluate each card against filter rules
    print("\n[3] Filter classification for all 12 real cards:")
    print("-" * 80)
    print(f"{'ID':>8}  {'Outcome':<12}  {'Rule':<35}  Title / Band")
    print("-" * 80)

    decisions_by_id = {}
    for card in result.cards:
        decision = evaluate_card(card, config)
        decisions_by_id[card.job_id] = decision
        icon = OUTCOME_ICON.get(decision.outcome, decision.outcome)
        band_str = card.band or "Band unknown"
        short_title = card.title[:45] + ("…" if len(card.title) > 45 else "")
        print(f"{card.job_id:>8}  {icon}  {decision.matched_rule:<35}  {short_title!r} [{band_str}]")

    print("-" * 80)
    strong_count = sum(1 for d in decisions_by_id.values() if d.outcome == "strong")
    maybe_count  = sum(1 for d in decisions_by_id.values() if d.outcome == "maybe")
    excl_count   = sum(1 for d in decisions_by_id.values() if d.outcome == "excluded")
    print(f"    Summary: {strong_count} strong, {maybe_count} maybe, {excl_count} excluded")

    # 4. Hours parsing demonstration
    print("\n[4] Hours_raw verbatim (never converted to a number, full-time flag only 35–40):")
    for card in result.cards:
        ft = parse_full_time(card.hours_raw)
        flag = " 🕒 Full-Time" if ft else ""
        print(f"    ID {card.job_id:>6}  hours_raw={card.hours_raw!r:>10}{flag}")

    # 5. Notifier dry-run: format one strong alert
    print("\n[5] DRY-RUN alert for first strong match:")
    from jobwatch.formatter import format_new_vacancy_alert
    strong_cards = [(result.cards[i], decisions_by_id[result.cards[i].job_id])
                    for i in range(len(result.cards))
                    if decisions_by_id[result.cards[i].job_id].outcome == "strong"]
    if strong_cards:
        card, dec = strong_cards[0]
        formatted = format_new_vacancy_alert(card, dec.outcome, dec.match_reason, config.closing_soon_days)
        # Strip HTML tags for console readability
        plain = re.sub(r"<[^>]+>", "", formatted)
        print(plain)

    # 6. FakeNotifier verification
    print("\n[6] FakeNotifier send test (no network):")
    fake = FakeNotifier()
    fake.send("NHS-JobWatch test message (offline)")
    print(f"    FakeNotifier recorded {len(fake.sent)} message(s): {fake.sent[0]!r}")

    print("\n" + "=" * 80)
    print("STEP 3 DEMONSTRATION COMPLETE (OFFLINE). Zero requests to source site.")
    print("=" * 80)


if __name__ == "__main__":
    main()

