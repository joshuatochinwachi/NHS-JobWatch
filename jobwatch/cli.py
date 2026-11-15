"""CLI entrypoint for NHS-JobWatch Step 3 operations."""

import argparse
import os
import sys

from dotenv import load_dotenv

# Load .env silently — never print token values
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def _get_notifier(dry_run: bool):
    """Returns the appropriate notifier based on --dry-run and environment."""
    from jobwatch.notifier import DryRunNotifier, PermanentNotifierError, TelegramNotifier

    if dry_run:
        return DryRunNotifier()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    token_status = "present" if token else "missing"
    chat_status = "present" if chat_id else "missing"

    if not token:
        print(f"ERROR: TELEGRAM_BOT_TOKEN is {token_status} in .env", file=sys.stderr)
        sys.exit(1)
    if not chat_id:
        print(f"ERROR: TELEGRAM_CHAT_ID is {chat_status} in .env", file=sys.stderr)
        sys.exit(1)

    return TelegramNotifier(token=token, chat_id=chat_id)


def cmd_find_chat_id(_args) -> int:
    """Calls getUpdates once and prints discovered chat_id and chat_type values."""
    from jobwatch.notifier import PermanentNotifierError, TelegramNotifier, sanitize_error

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing in .env", file=sys.stderr)
        return 1

    # We use a temporary notifier with a placeholder chat_id just for getUpdates
    notifier = TelegramNotifier(token=token, chat_id="0")

    print("Calling Telegram getUpdates (1 request)...")
    try:
        chats = notifier.find_chat_ids()
    except PermanentNotifierError as e:
        print(f"ERROR (permanent): {sanitize_error(str(e))}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {sanitize_error(str(e))}", file=sys.stderr)
        return 1

    if not chats:
        print(
            "\nNo chats found.\n"
            "Please send a message to your bot first, then run --find-chat-id again."
        )
        return 0

    print(f"\nFound {len(chats)} chat(s):")
    for chat in chats:
        print(f"  chat_id: {chat['chat_id']}  |  type: {chat['chat_type']}")
    print("\nCopy the chat_id you want into .env as TELEGRAM_CHAT_ID.")
    return 0


def cmd_test_telegram(args) -> int:
    """Sends exactly one test message to verify delivery."""
    from jobwatch.notifier import PermanentNotifierError, TransientNotifierError, sanitize_error

    notifier = _get_notifier(args.dry_run)
    test_text = "NHS-JobWatch test message"
    print(f"Sending test message{'(DRY RUN)' if args.dry_run else ''}...")

    try:
        ok = notifier.send(test_text)
        if ok:
            print("Test message sent successfully.")
            return 0
        else:
            print("Send returned False — message may not have been delivered.", file=sys.stderr)
            return 1
    except PermanentNotifierError as e:
        print(f"ERROR (permanent): {sanitize_error(str(e))}", file=sys.stderr)
        return 1
    except TransientNotifierError as e:
        print(f"ERROR (transient): {sanitize_error(str(e))}", file=sys.stderr)
        return 1


def cmd_digest(args) -> int:
    """Generates and sends an on-demand digest of open strong and maybe vacancies."""
    from jobwatch.alerts import generate_digest
    from jobwatch.filters import load_filter_config
    from jobwatch.storage import Storage

    db_path = os.getenv("JOBWATCH_DB_PATH")
    notifier = _get_notifier(args.dry_run)

    try:
        storage = Storage(db_path=db_path)
        config = load_filter_config()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        digest = generate_digest(storage, config, notifier, dry_run=args.dry_run)
        print(f"Digest sent ({len(digest)} chars).")
        return 0
    except Exception as e:
        print(f"ERROR generating digest: {e}", file=sys.stderr)
        return 1
    finally:
        storage.close()


def cmd_list_excluded(_args) -> int:
    """Lists all excluded vacancies from the database with title, band, and matched rule."""
    from jobwatch.storage import Storage

    db_path = os.getenv("JOBWATCH_DB_PATH")
    try:
        storage = Storage(db_path=db_path)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        cur = storage.conn.execute(
            """
            SELECT v.job_id, v.title, v.band, vd.matched_rule, vd.match_reason
            FROM vacancies v
            JOIN vacancy_decisions vd ON v.job_id = vd.job_id
            WHERE vd.outcome = 'excluded'
            ORDER BY v.job_id ASC;
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("No excluded vacancies recorded in the database.")
            return 0

        print(f"Excluded vacancies ({len(rows)}):")
        print("-" * 80)
        print(f"{'ID':>8}  {'Band':<10}  {'Rule':<30}  Title")
        print("-" * 80)
        for r in rows:
            band_str = r["band"] or "Unknown"
            print(f"{r['job_id']:>8}  {band_str:<10}  {r['matched_rule']:<30}  {r['title']}")
        print("-" * 80)
        return 0
    finally:
        storage.close()


def cmd_seed_sample_db(_args) -> int:
    """Fills the database specified by JOBWATCH_DB_PATH with sample vacancies, decisions, and statuses."""
    from datetime import datetime, timezone, timedelta
    from jobwatch.parser import ListingParser
    from jobwatch.filters import load_filter_config, evaluate_card
    from jobwatch.storage import Storage

    db_path = os.getenv("JOBWATCH_DB_PATH")
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture_path = os.path.join(repo_dir, "tests", "fixtures", "sample_listing.html")

    if not os.path.exists(fixture_path):
        print(f"ERROR: Fixture not found at {fixture_path}", file=sys.stderr)
        return 1

    with open(fixture_path, "r", encoding="utf-8") as f:
        html_markup = f.read()

    parser = ListingParser(html_markup)
    res = parser.parse()

    storage = Storage(db_path=db_path)
    config = load_filter_config()
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"Seeding {len(res.cards)} sample vacancies into database ({storage.db_path})...")
    with storage.conn:
        for idx, card in enumerate(res.cards):
            closing_str = card.closing_date.isoformat() if card.closing_date else "2027-06-30"
            storage.conn.execute(
                """
                INSERT INTO vacancies (
                    job_id, job_ref_raw, job_ref, title_raw, title, detail_url,
                    closing_date_raw, closing_date, salary_raw, salary, band,
                    location_raw, location, dept_label_raw, dept_label,
                    employer_raw, employer, contract_raw, contract, hours_raw,
                    first_seen_at, last_seen_at, is_listed, content_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'sample_hash'
                ) ON CONFLICT(job_id) DO UPDATE SET is_listed = 1;
                """,
                (
                    card.job_id, card.job_ref_raw, card.job_ref, card.title_raw, card.title, card.detail_url,
                    card.closing_date_raw, closing_str, card.salary_raw, card.salary, card.band,
                    card.location_raw, card.location, card.dept_label_raw, card.dept_label,
                    card.employer_raw, card.employer, card.contract_raw, card.contract, card.hours_raw,
                    now_iso, now_iso,
                ),
            )
            dec = evaluate_card(card, config, now_iso)
            storage.record_decision(
                card.job_id, dec.filter_version, dec.outcome, dec.matched_rule, dec.match_reason, dec.decided_at
            )

            status = "new"
            snooze_until = None
            if idx == 0:
                status = "applied"
            elif idx == 1:
                status = "skipped"
            elif idx == 2:
                status = "snoozed"
                snooze_until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

            storage.conn.execute(
                """
                INSERT INTO user_vacancy_status (user_id, job_id, status, snooze_until, updated_at)
                VALUES ('shared', ?, ?, ?, ?)
                ON CONFLICT(user_id, job_id) DO UPDATE SET
                    status = excluded.status,
                    snooze_until = excluded.snooze_until,
                    updated_at = excluded.updated_at;
                """,
                (card.job_id, status, snooze_until, now_iso),
            )

    print("Database successfully seeded with sample vacancies, decisions, and statuses.")
    storage.close()
    return 0


def cmd_send_sample_alert(_args) -> int:
    """Sends a sample alert from a fixture to every allowed chat. Refuses to run on default db."""
    import urllib.request, json
    from datetime import datetime, timezone
    from jobwatch.config import DEFAULT_DB_PATH
    from jobwatch.parser import ListingParser
    from jobwatch.formatter import format_new_vacancy_alert
    from jobwatch.keyboards import alert_keyboard
    from jobwatch.storage import Storage

    db_path = os.getenv("JOBWATCH_DB_PATH")
    if not db_path:
        print("ERROR: Refusing to send sample alert: JOBWATCH_DB_PATH environment variable is not set.", file=sys.stderr)
        print("Please set JOBWATCH_DB_PATH to a temporary throwaway database before running this command.", file=sys.stderr)
        return 1

    abs_db = os.path.abspath(db_path)
    abs_default = os.path.abspath(DEFAULT_DB_PATH)
    if abs_db == abs_default:
        print("ERROR: Refusing to run on production default database (data/jobwatch.db)!", file=sys.stderr)
        print("Please point JOBWATCH_DB_PATH at a throwaway test database file.", file=sys.stderr)
        return 1

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")
    if not token or not allowed_raw:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS must be set in .env", file=sys.stderr)
        return 1

    try:
        allowed_ids = [int(p.strip()) for p in allowed_raw.split(",") if p.strip()]
    except ValueError:
        print("ERROR: Malformed TELEGRAM_ALLOWED_CHAT_IDS in .env", file=sys.stderr)
        return 1

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture_path = os.path.join(repo_dir, "tests", "fixtures", "sample_listing.html")
    with open(fixture_path, "r", encoding="utf-8") as f:
        parser = ListingParser(f.read())
        res = parser.parse()

    sample_card = res.cards[2]  # Card 275434 Staff Nurse
    alert_text = format_new_vacancy_alert(sample_card, "strong", "Core registered nurse post", 5)
    alert_text = "⚠️ <b>[TEST ALERT - THROWAWAY DB]</b>\n\n" + alert_text

    url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={sample_card.job_id}"
    kb = alert_keyboard(sample_card.job_id, url)
    kb_dict = kb.to_dict()

    storage = Storage(db_path=db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    sent_count = 0

    print(f"Sending sample alert to {len(allowed_ids)} allowed chat(s)...")
    for chat_id in allowed_ids[:2]:
        payload = {
            "chat_id": chat_id,
            "text": alert_text,
            "parse_mode": "HTML",
            "reply_markup": kb_dict,
        }
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    msg_id = data["result"]["message_id"]
                    storage.record_delivery(sample_card.job_id, str(chat_id), msg_id, now_iso)
                    sent_count += 1
        except Exception as e:
            from jobwatch.notifier import sanitize_error
            print(f"Failed to send to chat: {sanitize_error(str(e))}", file=sys.stderr)

    print(f"Sample alert successfully delivered to {sent_count} chat(s).")
    storage.close()
    return 0


def cmd_live_baseline(args) -> int:
    """Executes a silent baseline for a specific scope via CLI."""
    from jobwatch.config import MONITORED_SCOPES, SCOPE_REQUEST_CAPS, USER_AGENT
    from jobwatch.fetcher import Fetcher
    from jobwatch.storage import Storage
    from jobwatch.sweep import SweepCoordinator

    scope = args.scope.strip()
    if scope not in MONITORED_SCOPES:
        print(f"ERROR: Unknown scope '{scope}'. Must be one of:\n" + "\n".join(f"  - {s}" for s in MONITORED_SCOPES), file=sys.stderr)
        return 1

    db_path = os.getenv("JOBWATCH_DB_PATH")
    if not db_path:
        print("ERROR: JOBWATCH_DB_PATH must be explicitly set to a throwaway database path.", file=sys.stderr)
        return 1

    max_req = args.max_requests or SCOPE_REQUEST_CAPS.get(scope, 30)

    print(f"Starting silent baseline for scope '{scope}' (max requests: {max_req})...")
    print(f"Target database: {db_path}")

    storage = Storage(db_path=db_path)
    try:
        active = storage.get_active_run()
        if active:
            print(f"ERROR: Another sweep is actively running (Run #{active['id']}).", file=sys.stderr)
            return 1

        meta = storage.get_scope_metadata(scope)
        if meta and meta["is_baselined"] and not args.force:
            print(f"ERROR: Scope '{scope}' is already baselined (Run #{meta['baseline_run_id']}). Use --force to re-baseline.", file=sys.stderr)
            return 1

        fetcher = Fetcher(user_agent=USER_AGENT)
        coordinator = SweepCoordinator(
            storage=storage,
            page_source=fetcher.fetch_scope,
            max_absolute_pages=max_req,
        )

        result = coordinator.execute_sweep(
            scope_key=scope,
            run_type="full_sweep",
        )

        print("\n--- BASELINE RESULTS ---")
        print(f"Run ID:            #{result.run_id}")
        print(f"Outcome:           {result.outcome}")
        print(f"Pages fetched:     {result.pages_fetched} / {max_req}")
        print(f"Vacancies seen:    {result.vacancies_seen}")
        print(f"New baselined:     {result.new_count}")
        print(f"Parse errors:      {result.parse_errors_count}")
        print("------------------------")
        return 0 if result.outcome == "success" else 1
    finally:
        storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jobwatch.cli",
        description="NHS-JobWatch CLI -- Step 3 and Step 4 offline operations",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --find-chat-id
    p_find = subparsers.add_parser(
        "find-chat-id",
        help="Call Telegram getUpdates once and print discovered chat IDs (never prints token)",
    )

    # --test-telegram
    p_test = subparsers.add_parser(
        "test-telegram",
        help="Send exactly one test message to confirm Telegram delivery",
    )
    p_test.add_argument("--dry-run", action="store_true", help="Print without sending")

    # --digest
    p_digest = subparsers.add_parser(
        "digest",
        help="Generate on-demand digest of open strong and maybe vacancies (never automatic)",
    )
    p_digest.add_argument("--dry-run", action="store_true", help="Print without sending")

    # --list-excluded
    p_excluded = subparsers.add_parser(
        "list-excluded",
        help="Print each excluded vacancy from the database with title, band, and rule",
    )

    # --seed-sample-db
    p_seed = subparsers.add_parser(
        "seed-sample-db",
        help="Fill throwaway database with sample vacancies, decisions, and statuses from fixture",
    )

    # --send-sample-alert
    p_sample_alert = subparsers.add_parser(
        "send-sample-alert",
        help="Send sample alert card with interactive buttons to allowed chats (requires throwaway db)",
    )

    # --live-baseline
    p_baseline = subparsers.add_parser(
        "live-baseline",
        help="Execute a silent baseline sweep for a scope with request cap",
    )
    p_baseline.add_argument("--scope", required=True, help="Scope key (e.g. 'Departments=9')")
    p_baseline.add_argument("--max-requests", type=int, help="Maximum requests cap")
    p_baseline.add_argument("--force", action="store_true", help="Force re-baseline of an already baselined scope")

    args = parser.parse_args()

    if args.command == "find-chat-id":
        return cmd_find_chat_id(args)
    elif args.command == "test-telegram":
        return cmd_test_telegram(args)
    elif args.command == "digest":
        return cmd_digest(args)
    elif args.command == "list-excluded":
        return cmd_list_excluded(args)
    elif args.command == "seed-sample-db":
        return cmd_seed_sample_db(args)
    elif args.command == "send-sample-alert":
        return cmd_send_sample_alert(args)
    elif args.command == "live-baseline":
        return cmd_live_baseline(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

