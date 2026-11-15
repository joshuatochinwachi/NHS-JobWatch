"""Interactive Telegram bot for NHS-JobWatch using python-telegram-bot."""

import asyncio
import collections
import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from jobwatch.formatter import format_new_vacancy_alert, parse_full_time
from jobwatch.keyboards import (
    actionable_card_keyboard,
    alert_done_keyboard,
    alert_keyboard,
    back_main_keyboard,
    main_menu_keyboard,
    pagination_keyboard,
    snooze_menu_keyboard,
)
from jobwatch.models import VacancyCard
from jobwatch.notifier import sanitize_error
from jobwatch.storage import Storage

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jobwatch.bot")

LONDON_TZ = ZoneInfo("Europe/London")
PAGE_SIZE = 5

# Bounded LRU cache for unauthorized /start rate limiting (max 1000 entries)
class BoundedRateLimiter:
    def __init__(self, max_entries: int = 1000, cooldown_seconds: float = 86400.0):
        self.max_entries = max_entries
        self.cooldown = cooldown_seconds
        self._cache: collections.OrderedDict[int, float] = collections.OrderedDict()

    def should_reply(self, user_id: int) -> bool:
        now = time.monotonic()
        if user_id in self._cache:
            last = self._cache[user_id]
            if now - last < self.cooldown:
                return False
            self._cache.move_to_end(user_id)
            self._cache[user_id] = now
            return True
        if len(self._cache) >= self.max_entries:
            self._cache.popitem(last=False)
        self._cache[user_id] = now
        return True


unauthorized_limiter = BoundedRateLimiter(max_entries=1000, cooldown_seconds=86400.0)
unauthorized_attempts_count = 0


class RuleKeyRegistry:
    """Bi-directional mapping between rule IDs and short numeric keys to ensure callback data is strictly < 64 bytes."""

    def __init__(self):
        self._id_to_key: Dict[str, str] = {}
        self._key_to_id: Dict[str, str] = {}
        self._counter: int = 0
        self._init_defaults()

    def _init_defaults(self):
        try:
            from jobwatch.filters import load_filter_config
            cfg = load_filter_config()
            for r in cfg.exclusions:
                self.get_key(r.id)
        except Exception:
            pass
        self.get_key("default_unmatched")

    def get_key(self, rule_id: str) -> str:
        if rule_id not in self._id_to_key:
            k = str(self._counter)
            self._counter += 1
            self._id_to_key[rule_id] = k
            self._key_to_id[k] = rule_id
        return self._id_to_key[rule_id]

    def get_rule_id(self, key: str) -> Optional[str]:
        return self._key_to_id.get(key)


rule_registry = RuleKeyRegistry()


def load_bot_config() -> Tuple[str, List[int], int, Optional[str]]:
    """Loads and strictly validates bot configuration from environment variables."""
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(repo_dir, ".env"))

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not token.strip():
        raise ValueError("Missing or empty TELEGRAM_BOT_TOKEN in environment")

    allowed_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")
    if not allowed_raw or not allowed_raw.strip():
        raise ValueError("Missing or empty TELEGRAM_ALLOWED_CHAT_IDS in environment")

    allowed_ids = []
    for part in allowed_raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            allowed_ids.append(int(p))
        except ValueError:
            raise ValueError("Malformed TELEGRAM_ALLOWED_CHAT_IDS: each ID must be a numeric integer")

    if not allowed_ids:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS contains no valid numeric IDs")

    admin_raw = os.getenv("TELEGRAM_CHAT_ID")
    if not admin_raw or not admin_raw.strip():
        raise ValueError("Missing or empty TELEGRAM_CHAT_ID in environment")
    try:
        admin_id = int(admin_raw.strip())
    except ValueError:
        raise ValueError("Malformed TELEGRAM_CHAT_ID: must be a numeric integer")

    contact = os.getenv("ACCESS_CONTACT")
    return token.strip(), allowed_ids, admin_id, contact.strip() if contact else None


def get_thread_storage(db_path: Optional[str] = None) -> Storage:
    """Creates a short-lived SQLite Storage connection for thread-safe operations."""
    path = db_path if db_path is not None else os.getenv("JOBWATCH_DB_PATH")
    return Storage(db_path=path)


# Authorization helper
def check_authorized(update: Update, allowed_ids: Set[int], contact: Optional[str]) -> bool:
    """Dual-check authorization against user ID and chat ID."""
    global unauthorized_attempts_count
    if not update.effective_chat or not update.effective_user:
        unauthorized_attempts_count += 1
        return False

    chat = update.effective_chat
    user = update.effective_user

    # Reject group chats completely
    if chat.type in ("group", "supergroup"):
        unauthorized_attempts_count += 1
        return False

    if chat.id not in allowed_ids or user.id not in allowed_ids:
        unauthorized_attempts_count += 1
        # Check single exception: private /start
        if update.message and update.message.text and update.message.text.strip().startswith("/start"):
            if unauthorized_limiter.should_reply(user.id):
                msg = (
                    f"This is a private bot. To request access, please contact the admin: {contact}"
                    if contact
                    else "This is a private bot."
                )
                try:
                    asyncio.create_task(update.message.reply_text(msg))
                except Exception:
                    pass
        return False

    return True


def _chat_is_muted(settings) -> bool:
    """Returns True if the chat has an active mute_until in the future (UTC)."""
    if not settings or not settings["muted_until"]:
        return False
    try:
        muted_until_dt = datetime.fromisoformat(settings["muted_until"])
        return datetime.now(timezone.utc) < muted_until_dt
    except Exception:
        return False


async def send_message_to_chat(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
    disable_notification: bool = False,
):
    """Sends a message to a chat with optional silent delivery for muted chats.
    Returns the message_id on success, or None on failure.
    Marks chat inactive (is_active=0) on 403 Forbidden or 'chat not found'."""
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        )
        return msg.message_id
    except Forbidden:
        def _deactivate():
            with get_thread_storage() as s:
                s.update_chat_settings(str(chat_id), is_active=0, inactive_reason="bot_blocked")
        await asyncio.to_thread(_deactivate)
        logger.warning(f"Chat {chat_id} deactivated: bot blocked (403).")
        return None
    except BadRequest as e:
        if "chat not found" in str(e).lower():
            def _deactivate_notfound():
                with get_thread_storage() as s:
                    s.update_chat_settings(str(chat_id), is_active=0, inactive_reason="chat_not_found")
            await asyncio.to_thread(_deactivate_notfound)
            logger.warning(f"Chat {chat_id} deactivated: chat not found.")
            return None
        logger.error(f"Failed to send message to chat {chat_id}: {sanitize_error(str(e))}")
        return None
    except Exception as e:
        logger.error(f"Failed to send message to chat {chat_id}: {sanitize_error(str(e))}")
        return None


async def safe_edit_message(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Edits a message quietly ignoring expected Telegram exceptions."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg or "message to edit not found" in msg:
            return True
        if "chat not found" in msg:
            def _deactivate():
                with get_thread_storage() as s:
                    s.update_chat_settings(str(chat_id), is_active=0, inactive_reason="chat_not_found")
            await asyncio.to_thread(_deactivate)
            logger.warning(f"Chat {chat_id} deactivated: chat not found.")
            return False
        logger.warning(f"BadRequest editing message {message_id} in chat {chat_id}: {sanitize_error(str(e))}")
        return False
    except Forbidden:
        # Bot was blocked or chat deleted
        def _deactivate():
            with get_thread_storage() as s:
                s.update_chat_settings(str(chat_id), is_active=0, inactive_reason="bot_blocked")
        await asyncio.to_thread(_deactivate)
        return False
    except Exception as e:
        logger.error(f"Error editing message {message_id} in chat {chat_id}: {sanitize_error(str(e))}")
        return False


# Command Handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        return

    chat_id = str(update.effective_chat.id)

    def _get_settings():
        with get_thread_storage() as s:
            settings = s.get_chat_settings(chat_id)
            is_paused = bool(settings["is_paused"])
            is_muted = False
            if settings["muted_until"]:
                try:
                    m_until = datetime.fromisoformat(settings["muted_until"])
                    is_muted = datetime.now(timezone.utc) < m_until
                except Exception:
                    pass
            return is_paused, is_muted

    is_paused, is_muted = await asyncio.to_thread(_get_settings)

    text = (
        "🏥 <b>NHS-JobWatch — Vacancy Monitor</b>\n\n"
        "Welcome! Use the menu below to review matching NHS Scotland vacancies, "
        "manage applications, or adjust alerting preferences."
    )
    kb = main_menu_keyboard(is_paused=is_paused, is_muted=is_muted)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        return

    text = (
        "❓ <b>NHS-JobWatch Help & Commands</b>\n\n"
        "• <b>/start</b> — Open Main Menu\n"
        "• <b>/digest</b> — View open Strong and Potential matches\n"
        "• <b>/applied</b> — View vacancies marked as Applied\n"
        "• <b>/skipped</b> — View vacancies marked as Skipped\n"
        "• <b>/snoozed</b> — View vacancies currently snoozed\n"
        "• <b>/excluded</b> — View excluded vacancy counts and rules\n"
        "• <b>/status</b> — System health, database size, and counts\n"
        "• <b>/filters</b> — Current filter configuration rules\n\n"
        "<b>Alert Buttons:</b>\n"
        "• <b>Applied:</b> Marks vacancy applied for both of you.\n"
        "• <b>Skip:</b> Hides vacancy from active lists.\n"
        "• <b>Snooze:</b> Snoozes vacancy for 1 day, 3 days, or 1 week.\n"
        "• <b>Undo:</b> Restores the previous status."
    )
    kb = back_main_keyboard("nav:menu")
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


def format_system_status(storage: Storage) -> str:
    """Formats comprehensive system metrics, counts, and Step 5 status."""
    tot = storage.conn.execute("SELECT COUNT(*) FROM vacancies;").fetchone()[0]
    opn = storage.conn.execute("SELECT COUNT(*) FROM vacancies WHERE is_listed = 1;").fetchone()[0]
    app = storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'applied';").fetchone()[0]
    skp = storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'skipped';").fetchone()[0]
    snz = storage.conn.execute("SELECT COUNT(*) FROM user_vacancy_status WHERE status = 'snoozed';").fetchone()[0]
    excl = storage.conn.execute("SELECT COUNT(*) FROM vacancy_decisions WHERE outcome = 'excluded';").fetchone()[0]
    cur_r = storage.conn.execute("SELECT started_at, outcome FROM source_runs ORDER BY id DESC LIMIT 1;").fetchone()
    lr = cur_r["started_at"][:19].replace("T", " ") if cur_r else "None"

    # Step 5 metrics
    sched_enabled = os.getenv("SCHEDULER_ENABLED", "0").strip().lower() in ("1", "true", "yes")
    sched_txt = "Enabled" if sched_enabled else "Disabled (SCHEDULER_ENABLED=0)"

    cb_state = storage.get_circuit_breaker_state()
    cb_txt = f"🚨 TRIPPED ({cb_state.get('tripped_reason')})" if cb_state.get("is_tripped") else "✅ Healthy"

    shallow_misses = storage.get_shallow_misses_count()
    baselined_scopes = storage.conn.execute("SELECT COUNT(*) FROM scope_metadata WHERE is_baselined = 1;").fetchone()[0]

    return (
        "📊 <b>System Status</b>\n\n"
        f"• <b>Scheduler:</b> {sched_txt}\n"
        f"• <b>Circuit Breaker:</b> {cb_txt}\n"
        f"• <b>Baselined Scopes:</b> {baselined_scopes}/6\n"
        f"• <b>Shallow Misses:</b> {shallow_misses}\n"
        f"• <b>Total Vacancies Tracked:</b> {tot}\n"
        f"• <b>Currently Listed:</b> {opn}\n"
        f"• <b>Applied:</b> {app}\n"
        f"• <b>Skipped:</b> {skp}\n"
        f"• <b>Snoozed:</b> {snz}\n"
        f"• <b>Excluded Decisions:</b> {excl}\n"
        f"• <b>Last Sweep Attempt:</b> {lr} UTC"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        return

    text = await asyncio.to_thread(lambda: format_system_status(get_thread_storage()))
    kb = back_main_keyboard("nav:menu")
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        return

    from jobwatch.filters import load_filter_config
    config = load_filter_config()

    text = (
        f"⚙️ <b>Active Filter Configuration</b> (v{config.version})\n\n"
        f"• <b>Status:</b> {html.escape(config.rules_status)}\n"
        f"• <b>Closing Soon Window:</b> {config.closing_soon_days} days\n"
        f"• <b>Default Outcome:</b> {config.default_outcome}\n"
        f"• <b>Exclusion Rules:</b> {len(config.exclusions)}\n"
        f"• <b>Strong Rules:</b> {len(config.strong_rules)}\n"
        f"• <b>Maybe Rules:</b> {len(config.maybe_rules)}"
    )
    kb = back_main_keyboard("nav:menu")
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# View Rendering Helpers (Paged Lists & Cards)
def render_list_page(
    items: List[Any],
    page: int,
    view_name: str,
    title_header: str,
    item_formatter,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Renders a 5-item slice of a list with an actionable keyboard."""
    total_items = len(items)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    if not items:
        text = f"{title_header}\n\n<i>No vacancies found.</i>"
        kb = back_main_keyboard("nav:menu")
        return text, kb

    lines = [f"{title_header} (Page {page}/{total_pages})\n"]
    buttons = []

    for idx, it in enumerate(page_items):
        item_text, card_id = item_formatter(it)
        lines.append(f"<b>{start + idx + 1}.</b> {item_text}")
        if card_id:
            buttons.append([InlineKeyboardButton(f"👉 #{start + idx + 1} Open Actions", callback_data=f"card:{card_id}:{view_name}:{page}")])

    text = "\n\n".join(lines)
    p_kb = pagination_keyboard(view_name, page, total_pages, "nav:menu")
    combined_buttons = buttons + [list(r) for r in p_kb.inline_keyboard]
    return text, InlineKeyboardMarkup(combined_buttons)


# Callback Query Handler
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        try:
            await query.answer()
        except Exception:
            pass
        return

    data = query.data
    chat_id = str(update.effective_chat.id)

    # 1. Navigation callbacks
    if data == "nav:menu":
        def _get_settings():
            with get_thread_storage() as s:
                st = s.get_chat_settings(chat_id)
                is_p = bool(st["is_paused"])
                is_m = bool(st["muted_until"] and datetime.now(timezone.utc) < datetime.fromisoformat(st["muted_until"]))
                return is_p, is_m
        is_paused, is_muted = await asyncio.to_thread(_get_settings)
        await query.answer()
        text = "🏥 <b>NHS-JobWatch — Vacancy Monitor</b>\n\nSelect an option below:"
        kb = main_menu_keyboard(is_paused=is_paused, is_muted=is_muted)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
        return

    elif data == "nav:help":
        await query.answer()
        text = (
            "❓ <b>NHS-JobWatch Help & Commands</b>\n\n"
            "• <b>/digest</b> — View open Strong and Potential matches\n"
            "• <b>/applied</b> — View vacancies marked as Applied\n"
            "• <b>/skipped</b> — View vacancies marked as Skipped\n"
            "• <b>/snoozed</b> — View vacancies currently snoozed\n"
            "• <b>/excluded</b> — View excluded vacancy counts and rules\n"
            "• <b>/status</b> — System health, database size, and counts\n"
            "• <b>/filters</b> — Current filter configuration rules\n\n"
            "Tapping any vacancy allows you to mark it Applied, Skip it, Snooze it, or Undo."
        )
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, back_main_keyboard("nav:menu"))
        return

    elif data == "nav:status":
        await query.answer()
        text = await asyncio.to_thread(lambda: format_system_status(get_thread_storage()))
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, back_main_keyboard("nav:menu"))
        return
        tot, opn, app, skp, snz, excl, lr = await asyncio.to_thread(_fetch_status)
        text = (
            "📊 <b>System Status</b>\n\n"
            f"• <b>Total Vacancies Tracked:</b> {tot}\n"
            f"• <b>Currently Listed:</b> {opn}\n"
            f"• <b>Applied:</b> {app}\n"
            f"• <b>Skipped:</b> {skp}\n"
            f"• <b>Snoozed:</b> {snz}\n"
            f"• <b>Excluded Decisions:</b> {excl}\n"
            f"• <b>Last Sweep Attempt:</b> {lr} UTC"
        )
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, back_main_keyboard("nav:menu"))
        return

    elif data == "nav:filters":
        await query.answer()
        from jobwatch.filters import load_filter_config
        config = load_filter_config()
        text = (
            f"⚙️ <b>Active Filter Configuration</b> (v{config.version})\n\n"
            f"• <b>Status:</b> {html.escape(config.rules_status)}\n"
            f"• <b>Closing Soon Window:</b> {config.closing_soon_days} days\n"
            f"• <b>Default Outcome:</b> {config.default_outcome}\n"
            f"• <b>Exclusion Rules:</b> {len(config.exclusions)}\n"
            f"• <b>Strong Rules:</b> {len(config.strong_rules)}\n"
            f"• <b>Maybe Rules:</b> {len(config.maybe_rules)}"
        )
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, back_main_keyboard("nav:menu"))
        return

    # 2. Control callbacks (Pause / Mute)
    elif data == "ctl:pause":
        now_iso = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(lambda: get_thread_storage().update_chat_settings(chat_id, is_paused=1, paused_at=now_iso))
        await query.answer("Alerts paused for this chat ⏸️")
        kb = main_menu_keyboard(is_paused=True, is_muted=False)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, "🏥 <b>NHS-JobWatch — Alerts Paused</b>\n\nAlerts will be held until you resume.", kb)
        return

    elif data == "ctl:resume":
        # Resume: clear pause, fetch open vacancies for catch-up digest
        def _do_resume():
            with get_thread_storage() as s:
                s.update_chat_settings(chat_id, is_paused=0, paused_at=None)
                settings = s.get_chat_settings(chat_id)
                vacancies = s.get_open_vacancies(user_id="shared")
                is_muted = _chat_is_muted(settings)
                return vacancies, is_muted
        vacancies, is_muted = await asyncio.to_thread(_do_resume)
        await query.answer("Alerts resumed! ▶️")

        # Catch-up digest: strong/maybe open vacancies, sorted closing soonest first, skip closed
        matching = sorted(
            [v for v in vacancies if v["outcome"] in ("strong", "maybe")
             and v["closing_date"] and v["closing_date"] >= datetime.now(timezone.utc).strftime("%Y-%m-%d")],
            key=lambda v: v["closing_date"] or "9999-99-99",
        )
        catch_up_msg = f"▶️ <b>Alerts Resumed</b>\n\nFound {len(matching)} active strong/maybe post(s) currently open (closing soonest first)."
        kb = main_menu_keyboard(is_paused=False, is_muted=is_muted)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, catch_up_msg, kb)
        return

    elif data == "ctl:mute":
        now_dt = datetime.now(LONDON_TZ)
        tomorrow_8am = (now_dt + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        until_iso = tomorrow_8am.astimezone(timezone.utc).isoformat()
        await asyncio.to_thread(lambda: get_thread_storage().update_chat_settings(chat_id, muted_until=until_iso))
        await query.answer("Muted until 8am tomorrow 🌙")
        kb = main_menu_keyboard(is_paused=False, is_muted=True)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, "🌙 <b>Muted Until 8am</b>\n\nAlerts will be delivered silently without sound/vibration.", kb)
        return

    elif data == "ctl:unmute":
        await asyncio.to_thread(lambda: get_thread_storage().update_chat_settings(chat_id, muted_until=None))
        await query.answer("Unmuted 🔔")
        kb = main_menu_keyboard(is_paused=False, is_muted=False)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, "🔔 <b>Alerts Unmuted</b>\n\nSound/vibration restored.", kb)
        return

    # 3. Paged Views (Digest, Applied, Skipped, Snoozed, Excluded)
    m_p = re.match(r"^p:([a-z_]+):(\d+)$", data)
    m_nav = re.match(r"^nav:([a-z_]+)$", data)
    if m_p or m_nav:
        view_name = m_p.group(1) if m_p else m_nav.group(1)
        page = int(m_p.group(2)) if m_p else 1
        await query.answer()

        if view_name == "digest":
            items = await asyncio.to_thread(lambda: [v for v in get_thread_storage().get_open_vacancies(user_id="shared") if v["outcome"] in ("strong", "maybe")])
            def _fmt(it):
                icon = "⭐" if it["outcome"] == "strong" else "📌"
                c_date = it["closing_date"][:10] if it["closing_date"] else "?"
                return f"{icon} <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or 'Band unk')} | {html.escape(it['employer'] or '')} | Closes {c_date}", it["job_id"]
            text, kb = render_list_page(items, page, "digest", "📋 <b>Active Vacancies Digest</b>", _fmt)
            await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
            return

        elif view_name in ("applied", "skipped", "snoozed"):
            items = await asyncio.to_thread(lambda: get_thread_storage().get_vacancies_by_status(view_name, user_id="shared"))
            icon_map = {"applied": "✅", "skipped": "⏭️", "snoozed": "💤"}
            def _fmt_status(it):
                c_date = it["closing_date"][:10] if it["closing_date"] else "?"
                snz_note = f" (Snoozed until {it['snooze_until'][:10]})" if view_name == "snoozed" and it["snooze_until"] else ""
                return f"{icon_map.get(view_name, '')} <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or '')} | Closes {c_date}{snz_note}", it["job_id"]
            text, kb = render_list_page(items, page, view_name, f"{icon_map.get(view_name, '')} <b>{view_name.title()} Vacancies</b>", _fmt_status)
            await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
            return

        elif view_name == "excluded":
            # Tier 1 summary by rule
            summary = await asyncio.to_thread(lambda: get_thread_storage().get_excluded_summary())
            lines = ["❌ <b>Excluded Vacancies Summary</b>\n\nTap a rule to inspect excluded posts:\n"]
            buttons = []
            for r in summary:
                rule_id = r["matched_rule"]
                count = r["count"]
                reason = r["match_reason"][:40] + ("…" if len(r["match_reason"]) > 40 else "")
                lines.append(f"• <b>{rule_id}:</b> {count} vacancy(ies)\n  <i>{html.escape(reason)}</i>")
                k = rule_registry.get_key(rule_id)
                buttons.append([InlineKeyboardButton(f"🔍 View {rule_id} ({count})", callback_data=f"xr:{k}:1")])
            buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="nav:menu")])
            text = "\n\n".join(lines)
            await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, InlineKeyboardMarkup(buttons))
            return

        elif view_name.startswith("xr_"):
            rule_key = view_name[3:]
            rule_id = rule_registry.get_rule_id(rule_key) or rule_key
            items = await asyncio.to_thread(lambda: get_thread_storage().get_excluded_by_rule(rule_id))
            def _fmt_excl_p(it):
                c_date = it["closing_date"][:10] if it["closing_date"] else "?"
                return f"❌ <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or '')} | {html.escape(it['employer'] or '')} | Closes {c_date}", it["job_id"]
            text, kb = render_list_page(items, page, view_name, f"❌ <b>Excluded under '{rule_id}'</b>", _fmt_excl_p)
            new_rows = [list(r) for r in kb.inline_keyboard]
            if new_rows:
                new_rows[-1] = [InlineKeyboardButton("🔙 Back to Excluded Rules", callback_data="nav:excluded")]
            kb = InlineKeyboardMarkup(new_rows)
            await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
            return

    # 4. Excluded Rule Drill-down (using short key 'xr:{key}:{page}')
    m_xr = re.match(r"^xr:([a-zA-Z0-9_]+):(\d+)$", data)
    if m_xr:
        rule_key = m_xr.group(1)
        page = int(m_xr.group(2))
        rule_id = rule_registry.get_rule_id(rule_key) or rule_key
        await query.answer()

        items = await asyncio.to_thread(lambda: get_thread_storage().get_excluded_by_rule(rule_id))
        def _fmt_excl(it):
            c_date = it["closing_date"][:10] if it["closing_date"] else "?"
            return f"❌ <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or '')} | {html.escape(it['employer'] or '')} | Closes {c_date}", it["job_id"]

        text, kb = render_list_page(items, page, f"xr_{rule_key}", f"❌ <b>Excluded under '{rule_id}'</b>", _fmt_excl)
        # Replace back button to point to excluded summary
        new_rows = [list(r) for r in kb.inline_keyboard]
        if new_rows:
            new_rows[-1] = [InlineKeyboardButton("🔙 Back to Excluded Rules", callback_data="nav:excluded")]
        kb = InlineKeyboardMarkup(new_rows)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
        return

    # 5. Actionable Card View from a list
    m_card = re.match(r"^card:(\d+):([a-z0-9_]+):(\d+)$", data)
    if m_card:
        job_id = int(m_card.group(1))
        from_view = m_card.group(2)
        from_page = int(m_card.group(3))
        await query.answer()

        def _fetch_card():
            with get_thread_storage() as s:
                cur = s.conn.execute("SELECT * FROM vacancies WHERE job_id = ?;", (job_id,))
                return cur.fetchone()
        row = await asyncio.to_thread(_fetch_card)
        if not row:
            await query.answer("Vacancy not found.", show_alert=True)
            return

        url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}"
        c_date = row["closing_date"][:10] if row["closing_date"] else "?"
        text = (
            f"📄 <b>Vacancy #{job_id} Actions</b>\n\n"
            f"<b>{html.escape(row['title'])}</b>\n"
            f"🏥 <b>Employer:</b> {html.escape(row['employer'] or 'NHS Scotland')}\n"
            f"💰 <b>Band/Salary:</b> {html.escape(row['salary'] or row['band'] or '')}\n"
            f"📍 <b>Location:</b> {html.escape(row['location'] or '')}\n"
            f"📅 <b>Closing:</b> {c_date}\n\n"
            "Choose a workflow action below:"
        )
        kb = actionable_card_keyboard(job_id, url, from_view, from_page)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, text, kb)
        return

    # 6. Workflow Action Callbacks: Applied, Skip, Snooze, Undo
    m_app = re.match(r"^app:(\d+)$", data)
    m_skp = re.match(r"^skp:(\d+)$", data)
    m_snz_m = re.match(r"^snz_m:(\d+)$", data)
    m_snz_b = re.match(r"^snz_b:(\d+)$", data)
    m_snz = re.match(r"^snz:([0-9a-z]+):(\d+)$", data)
    m_undo = re.match(r"^undo:(\d+)$", data)

    if m_app or m_skp or m_snz or m_undo:
        job_id = int((m_app or m_skp or m_snz or m_undo).group(m_snz.lastindex if m_snz else 1))

        # Check stale vacancy
        def _check_stale():
            with get_thread_storage() as s:
                r = s.conn.execute("SELECT is_listed, title, closing_date FROM vacancies WHERE job_id = ?;", (job_id,)).fetchone()
                return r
        v_row = await asyncio.to_thread(_check_stale)
        if not v_row or v_row["is_listed"] == 0:
            await query.answer("Vacancy is no longer active.", show_alert=True)
            return

        url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}"
        today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")

        if m_app:
            await asyncio.to_thread(lambda: get_thread_storage().update_user_status(job_id, "applied", user_id="shared"))
            await query.answer("Marked as Applied! ✅")
            status_badge = f"✅ <b>[APPLIED on {today_str}]</b>"
            kb = alert_done_keyboard(job_id, url, "Applied")

        elif m_skp:
            await asyncio.to_thread(lambda: get_thread_storage().update_user_status(job_id, "skipped", user_id="shared"))
            await query.answer("Marked as Skipped ⏭️")
            status_badge = f"⏭️ <b>[SKIPPED on {today_str}]</b>"
            kb = alert_done_keyboard(job_id, url, "Skipped")

        elif m_snz:
            dur = m_snz.group(1)
            days = 1 if dur == "1d" else (3 if dur == "3d" else 7)
            snz_dt = datetime.now(timezone.utc) + timedelta(days=days)
            await asyncio.to_thread(lambda: get_thread_storage().snooze_vacancy(job_id, snz_dt.isoformat(), user_id="shared"))
            await query.answer(f"Snoozed for {days} day(s) 💤")
            status_badge = f"💤 <b>[SNOOZED until {snz_dt.strftime('%d/%m/%Y')}]</b>"
            kb = alert_done_keyboard(job_id, url, "Snoozed")

        elif m_undo:
            prev, restored = await asyncio.to_thread(lambda: get_thread_storage().undo_user_status(job_id, user_id="shared"))
            await query.answer(f"Status restored to {restored} ↩️")
            status_badge = "🔄 <b>[RESTORED]</b>"
            kb = alert_keyboard(job_id, url)

        # Edit current message
        orig_text = query.message.text_html or query.message.text
        # Strip old badges
        clean_text = re.sub(r"^[✅⏭️💤🔄].*?\n\n", "", orig_text, flags=re.DOTALL)
        new_text = f"{status_badge}\n\n{clean_text}"
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, new_text, kb)

        # Cross-chat synchronization: edit other allowed chat's message copy
        def _get_other_deliveries():
            with get_thread_storage() as s:
                return s.get_deliveries_for_job(job_id)
        deliveries = await asyncio.to_thread(_get_other_deliveries)
        for deliv in deliveries:
            if str(deliv["chat_id"]) != chat_id:
                await safe_edit_message(
                    context.bot,
                    int(deliv["chat_id"]),
                    deliv["telegram_message_id"],
                    new_text,
                    kb,
                )
        return

    elif m_snz_m:
        job_id = int(m_snz_m.group(1))
        await query.answer()
        kb = snooze_menu_keyboard(job_id)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, query.message.text_html, kb)
        return

    elif data.startswith("rebase:"):
        scope_key = data.split(":", 1)[1]
        admin_id = context.bot_data.get("admin_id")
        if int(chat_id) != admin_id:
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer("Starting re-baseline...")
        await execute_baseline_for_scope(query.message, scope_key)
        return

    elif m_snz_b:
        job_id = int(m_snz_b.group(1))
        await query.answer()
        url = f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}"
        kb = alert_keyboard(job_id, url)
        await safe_edit_message(context.bot, update.effective_chat.id, query.message.message_id, query.message.text_html, kb)
        return

    await query.answer()


# Global Error Handler
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches exceptions, checks for 409 Conflict, and notifies admin chat without secrets."""
    err = context.error
    if isinstance(err, Conflict):
        logger.fatal("FATAL: Telegram conflict (409): another instance is polling this token. Exiting.")
        if context.application is not None:
            context.application.bot_data["conflict_detected"] = True
            context.application.stop_running()
        return

    clean_err = sanitize_error(str(err))
    logger.error(f"Global error: {clean_err}", exc_info=err)

    admin_id = context.bot_data.get("admin_id")
    if admin_id and not isinstance(err, (BadRequest, Forbidden)):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ <b>NHS-JobWatch Admin Alert</b>\n\n<code>{html.escape(clean_err)[:1000]}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass



# ── Admin-Only Operational Commands (Step 5) ──

async def cmd_probe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: Executes exactly 1 probe GET request to the NHS Scotland site."""
    admin_id = context.bot_data.get("admin_id")
    if not update.effective_chat or update.effective_chat.id != admin_id:
        return

    from jobwatch.config import LISTING_ENDPOINT, USER_AGENT
    from jobwatch.fetcher import Fetcher
    from jobwatch.parser import ListingParser

    status_msg = await update.message.reply_text("🔍 Probing NHS Scotland site (1 GET request)...")
    start_t = time.perf_counter()

    try:
        fetcher = Fetcher(user_agent=USER_AGENT)
        url = f"{LISTING_ENDPOINT}?Departments=9&Skip=0"
        markup = await asyncio.to_thread(fetcher.fetch_url, url)
        elapsed = time.perf_counter() - start_t

        parser = ListingParser(markup)
        res = parser.parse()

        text = (
            "✅ <b>NHS Scotland Probe Succeeded</b>\n\n"
            f"• <b>HTTP Status:</b> 200 OK\n"
            f"• <b>Cards on Page:</b> {len(res.cards)}\n"
            f"• <b>Total Match Records:</b> {res.total_match_records}\n"
            f"• <b>Response Time:</b> {elapsed:.2f}s\n"
            f"• <b>User-Agent:</b> <code>{USER_AGENT}</code>"
        )
        await status_msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        elapsed = time.perf_counter() - start_t
        err_msg = sanitize_error(str(e))
        text = (
            "❌ <b>NHS Scotland Probe Failed</b>\n\n"
            f"• <b>Error:</b> <code>{err_msg}</code>\n"
            f"• <b>Elapsed:</b> {elapsed:.2f}s"
        )
        await status_msg.edit_text(text, parse_mode="HTML")


async def execute_baseline_for_scope(msg_target, scope_key: str) -> None:
    """Helper to execute and report a silent baseline sweep."""
    from jobwatch.config import SCOPE_REQUEST_CAPS, USER_AGENT
    from jobwatch.fetcher import Fetcher
    from jobwatch.sweep import SweepCoordinator

    status_msg = await msg_target.reply_text(f"⏳ Running silent baseline for <code>{html.escape(scope_key)}</code>...", parse_mode="HTML")
    cap = SCOPE_REQUEST_CAPS.get(scope_key, 30)

    def _do_baseline():
        with get_thread_storage() as s:
            fetcher = Fetcher(user_agent=USER_AGENT)
            coordinator = SweepCoordinator(
                storage=s,
                page_source=fetcher.fetch_scope,
                max_absolute_pages=cap,
            )
            return coordinator.execute_sweep(
                scope_key=scope_key,
                run_type="full_sweep",
            )

    try:
        res = await asyncio.to_thread(_do_baseline)
        text = (
            f"✅ <b>Baseline Complete:</b> <code>{html.escape(scope_key)}</code>\n\n"
            f"• <b>Run ID:</b> #{res.run_id}\n"
            f"• <b>Outcome:</b> {res.outcome}\n"
            f"• <b>Pages Fetched:</b> {res.pages_fetched} (Cap: {cap})\n"
            f"• <b>Vacancies Seen:</b> {res.vacancies_seen}\n"
            f"• <b>New Items (Baselined):</b> {res.new_count}\n"
            f"• <b>Parse Errors:</b> {res.parse_errors_count}\n\n"
            f"<i>Zero alerts were sent (baseline status).</i>"
        )
        await status_msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        err_clean = sanitize_error(str(e))
        await status_msg.edit_text(f"❌ <b>Baseline Failed:</b> <code>{err_clean}</code>", parse_mode="HTML")


async def cmd_baseline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: Executes a silent baseline sweep for a single specified scope."""
    admin_id = context.bot_data.get("admin_id")
    if not update.effective_chat or update.effective_chat.id != admin_id:
        return

    from jobwatch.config import MONITORED_SCOPES

    args = context.args or []
    if not args:
        scopes_txt = "\n".join(f"• <code>/baseline {s}</code>" for s in MONITORED_SCOPES)
        await update.message.reply_text(
            f"ℹ️ <b>Usage:</b> <code>/baseline &lt;scope&gt;</code>\n\n<b>Valid scopes:</b>\n{scopes_txt}",
            parse_mode="HTML",
        )
        return

    scope_key = " ".join(args).strip()
    if scope_key not in MONITORED_SCOPES:
        await update.message.reply_text(
            f"❌ Unknown scope <code>{html.escape(scope_key)}</code>. Must be one of:\n" +
            "\n".join(f"• <code>{s}</code>" for s in MONITORED_SCOPES),
            parse_mode="HTML",
        )
        return

    with get_thread_storage() as s:
        active = s.get_active_run()
        if active:
            await update.message.reply_text(
                f"❌ Another sweep is actively running (Run #{active['id']}, scope={active['scope_key']}). "
                "Wait for it to finish before baselining.",
                parse_mode="HTML",
            )
            return

        meta = s.get_scope_metadata(scope_key)
        if meta and meta["is_baselined"]:
            btn = InlineKeyboardButton("⚠️ Confirm Re-Baseline", callback_data=f"rebase:{scope_key}")
            kb = InlineKeyboardMarkup([[btn]])
            await update.message.reply_text(
                f"⚠️ Scope <code>{html.escape(scope_key)}</code> is already baselined (Run #{meta['baseline_run_id']}).\n\n"
                "Re-baselining will reset the baseline. Tap below to confirm:",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

    await execute_baseline_for_scope(update.message, scope_key)


async def cmd_resume_sweeps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: Clears the circuit breaker and resumes automated sweeps."""
    admin_id = context.bot_data.get("admin_id")
    if not update.effective_chat or update.effective_chat.id != admin_id:
        return

    def _reset():
        with get_thread_storage() as s:
            s.reset_circuit_breaker()
    await asyncio.to_thread(_reset)
    await update.message.reply_text("✅ Circuit breaker reset. Automated sweeps resumed.", parse_mode="HTML")


def build_application(
    token: str,
    allowed_ids: List[int],
    admin_id: int,
    contact: Optional[str] = None,
) -> Application:
    """Builds and configures the python-telegram-bot Application."""
    app = Application.builder().token(token).build()

    app.bot_data["allowed_ids"] = set(allowed_ids)
    app.bot_data["admin_id"] = admin_id
    app.bot_data["contact"] = contact

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("digest", lambda u, c: handle_callback_query_direct(u, c, "nav:digest")))
    app.add_handler(CommandHandler("applied", lambda u, c: handle_callback_query_direct(u, c, "nav:applied")))
    app.add_handler(CommandHandler("skipped", lambda u, c: handle_callback_query_direct(u, c, "nav:skipped")))
    app.add_handler(CommandHandler("snoozed", lambda u, c: handle_callback_query_direct(u, c, "nav:snoozed")))
    app.add_handler(CommandHandler("excluded", lambda u, c: handle_callback_query_direct(u, c, "nav:excluded")))
    app.add_handler(CommandHandler("probe", cmd_probe))
    app.add_handler(CommandHandler("baseline", cmd_baseline))
    app.add_handler(CommandHandler("resume_sweeps", cmd_resume_sweeps))

    # Callback query router
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Catch unauthorized plain messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unauthorized_fallback))

    app.add_error_handler(global_error_handler)
    return app


async def unauthorized_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently drops non-command messages from unauthorized users."""
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    check_authorized(update, allowed_ids, contact)


async def handle_callback_query_direct(update: Update, context: ContextTypes.DEFAULT_TYPE, nav_target: str) -> None:
    """Helper to route slash commands into navigation logic."""
    allowed_ids: Set[int] = context.bot_data["allowed_ids"]
    contact: Optional[str] = context.bot_data["contact"]
    if not check_authorized(update, allowed_ids, contact):
        return

    # Fake a query-like call by answering with direct reply
    view_name = nav_target.split(":")[1]
    if view_name == "digest":
        items = await asyncio.to_thread(lambda: [v for v in get_thread_storage().get_open_vacancies(user_id="shared") if v["outcome"] in ("strong", "maybe")])
        def _fmt(it):
            icon = "⭐" if it["outcome"] == "strong" else "📌"
            c_date = it["closing_date"][:10] if it["closing_date"] else "?"
            return f"{icon} <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or '')} | Closes {c_date}", it["job_id"]
        text, kb = render_list_page(items, 1, "digest", "📋 <b>Active Vacancies Digest</b>", _fmt)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    elif view_name in ("applied", "skipped", "snoozed"):
        items = await asyncio.to_thread(lambda: get_thread_storage().get_vacancies_by_status(view_name, user_id="shared"))
        icon_map = {"applied": "✅", "skipped": "⏭️", "snoozed": "💤"}
        def _fmt_s(it):
            c_date = it["closing_date"][:10] if it["closing_date"] else "?"
            return f"{icon_map.get(view_name, '')} <b>{html.escape(it['title'])}</b>\n{html.escape(it['band'] or '')} | Closes {c_date}", it["job_id"]
        text, kb = render_list_page(items, 1, view_name, f"{icon_map.get(view_name, '')} <b>{view_name.title()} Vacancies</b>", _fmt_s)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    elif view_name == "excluded":
        summary = await asyncio.to_thread(lambda: get_thread_storage().get_excluded_summary())
        lines = ["❌ <b>Excluded Vacancies Summary</b>\n\nTap a rule to inspect excluded posts:\n"]
        buttons = []
        for r in summary:
            rule_id = r["matched_rule"]
            count = r["count"]
            lines.append(f"• <b>{rule_id}:</b> {count} vacancy(ies)")
            k = rule_registry.get_key(rule_id)
            buttons.append([InlineKeyboardButton(f"🔍 View {rule_id} ({count})", callback_data=f"xr:{k}:1")])
        buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="nav:menu")])
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def post_init(app: Application) -> None:
    """Sets bot commands in Telegram's autocomplete menu and initializes the background scheduler."""
    commands = [
        BotCommand("start", "Open Main Menu"),
        BotCommand("digest", "View active Strong and Potential matches"),
        BotCommand("applied", "View Applied vacancies"),
        BotCommand("skipped", "View Skipped vacancies"),
        BotCommand("snoozed", "View Snoozed vacancies"),
        BotCommand("excluded", "View Excluded vacancy rules and counts"),
        BotCommand("status", "System metrics and database counts"),
        BotCommand("filters", "Active filter rules and version"),
        BotCommand("help", "Usage guide and buttons explanation"),
        BotCommand("probe", "Admin: probe NHS Scotland site once"),
        BotCommand("baseline", "Admin: run silent baseline for a scope"),
        BotCommand("resume_sweeps", "Admin: reset circuit breaker"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Could not set my commands: {sanitize_error(str(e))}")

    # Initialize and start background AsyncScheduler
    admin_id = app.bot_data.get("admin_id")
    allowed_ids = list(app.bot_data.get("allowed_ids", []))
    storage = get_thread_storage()
    from jobwatch.scheduler import AsyncScheduler
    scheduler = AsyncScheduler(
        storage=storage,
        bot=app.bot,
        allowed_chat_ids=allowed_ids,
        admin_chat_id=admin_id,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler


def main() -> None:
    """Main process entrypoint for python -m jobwatch.bot."""
    try:
        token, allowed_ids, admin_id, contact = load_bot_config()
    except Exception as e:
        print(f"FATAL: Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Starting NHS-JobWatch Interactive Telegram Bot...")
    app = build_application(token, allowed_ids, admin_id, contact)
    app.post_init = post_init

    # Run polling loop without dropping updates (handlers are idempotent)
    try:
        app.run_polling(drop_pending_updates=False)
    except Conflict:
        logger.fatal("FATAL: Telegram conflict (409): another instance is polling this token. Exiting.")
        if "scheduler" in app.bot_data:
            app.bot_data["scheduler"].stop()
        sys.exit(1)

    if "scheduler" in app.bot_data:
        app.bot_data["scheduler"].stop()

    if app.bot_data.get("conflict_detected"):
        logger.fatal("FATAL: Telegram conflict (409) detected during execution. Exiting with code 1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
