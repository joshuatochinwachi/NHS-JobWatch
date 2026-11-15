"""Inline keyboard layouts for NHS-JobWatch interactive Telegram bot."""

from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(is_paused: bool = False, is_muted: bool = False) -> InlineKeyboardMarkup:
    """Builds the main menu inline keyboard with sub-screen options and quick controls."""
    pause_btn = (
        InlineKeyboardButton("▶️ Resume Alerts", callback_data="ctl:resume")
        if is_paused
        else InlineKeyboardButton("⏸️ Pause Alerts", callback_data="ctl:pause")
    )
    mute_btn = (
        InlineKeyboardButton("🔔 Unmute", callback_data="ctl:unmute")
        if is_muted
        else InlineKeyboardButton("🌙 Mute until 8am", callback_data="ctl:mute")
    )

    buttons = [
        [
            InlineKeyboardButton("📋 Digest", callback_data="nav:digest"),
            InlineKeyboardButton("✅ Applied", callback_data="nav:applied"),
        ],
        [
            InlineKeyboardButton("⏭️ Skipped", callback_data="nav:skipped"),
            InlineKeyboardButton("💤 Snoozed", callback_data="nav:snoozed"),
        ],
        [
            InlineKeyboardButton("❌ Excluded", callback_data="nav:excluded"),
            InlineKeyboardButton("📊 Status", callback_data="nav:status"),
        ],
        [
            InlineKeyboardButton("⚙️ Filters", callback_data="nav:filters"),
            InlineKeyboardButton("❓ Help", callback_data="nav:help"),
        ],
        [pause_btn, mute_btn],
    ]
    return InlineKeyboardMarkup(buttons)


def alert_keyboard(job_id: int, detail_url: str) -> InlineKeyboardMarkup:
    """Builds action buttons for a vacancy alert."""
    buttons = [
        [
            InlineKeyboardButton("📝 Applied", callback_data=f"app:{job_id}"),
            InlineKeyboardButton("⏭️ Skip", callback_data=f"skp:{job_id}"),
            InlineKeyboardButton("💤 Snooze", callback_data=f"snz_m:{job_id}"),
        ],
        [
            InlineKeyboardButton("🔗 View on NHS Scotland", url=detail_url),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def alert_done_keyboard(job_id: int, detail_url: str, status_label: str) -> InlineKeyboardMarkup:
    """Builds post-action keyboard with an Undo button and direct vacancy link."""
    buttons = [
        [
            InlineKeyboardButton(f"↩️ Undo ({status_label})", callback_data=f"undo:{job_id}"),
        ],
        [
            InlineKeyboardButton("🔗 View on NHS Scotland", url=detail_url),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def snooze_menu_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Builds snooze duration options."""
    buttons = [
        [
            InlineKeyboardButton("1 Day", callback_data=f"snz:1d:{job_id}"),
            InlineKeyboardButton("3 Days", callback_data=f"snz:3d:{job_id}"),
        ],
        [
            InlineKeyboardButton("1 Week", callback_data=f"snz:7d:{job_id}"),
            InlineKeyboardButton("🔙 Back", callback_data=f"snz_b:{job_id}"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def pagination_keyboard(
    view_name: str,
    current_page: int,
    total_pages: int,
    back_callback: str = "nav:menu",
) -> InlineKeyboardMarkup:
    """Builds pagination controls with Prev/Next and Back/Main buttons."""
    nav_row = []
    if current_page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"p:{view_name}:{current_page - 1}"))
    nav_row.append(InlineKeyboardButton(f"Page {current_page}/{total_pages}", callback_data="noop"))
    if current_page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"p:{view_name}:{current_page + 1}"))

    buttons = [
        nav_row,
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_callback),
            InlineKeyboardButton("🏠 Main Menu", callback_data="nav:menu"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def actionable_card_keyboard(
    job_id: int,
    detail_url: str,
    from_view: str,
    current_page: int = 1,
) -> InlineKeyboardMarkup:
    """Builds card view keyboard allowing direct workflow actions from a list item."""
    buttons = [
        [
            InlineKeyboardButton("📝 Applied", callback_data=f"app:{job_id}"),
            InlineKeyboardButton("⏭️ Skip", callback_data=f"skp:{job_id}"),
            InlineKeyboardButton("💤 Snooze", callback_data=f"snz_m:{job_id}"),
        ],
        [
            InlineKeyboardButton("🔗 View on NHS Scotland", url=detail_url),
        ],
        [
            InlineKeyboardButton("🔙 Back to List", callback_data=f"p:{from_view}:{current_page}"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="nav:menu"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def back_main_keyboard(back_callback: str = "nav:menu") -> InlineKeyboardMarkup:
    """Builds standard Back and Main Menu navigation buttons."""
    buttons = [
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_callback),
            InlineKeyboardButton("🏠 Main Menu", callback_data="nav:menu"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

