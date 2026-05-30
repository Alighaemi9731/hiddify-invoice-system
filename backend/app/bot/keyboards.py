"""Inline keyboards for the bot."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def membership_keyboard(channel_link: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_link:
        rows.append([InlineKeyboardButton(text="📢 عضویت در کانال", url=channel_link)])
    rows.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_membership")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ثبت لینک پنل من", callback_data="menu:register")],
            [InlineKeyboardButton(text="🖥 پنل‌های من", callback_data="menu:panels")],
            [InlineKeyboardButton(text="🧾 فاکتورهای من", callback_data="menu:invoices")],
            [InlineKeyboardButton(text="💳 پرداخت", callback_data="menu:pay")],
            [InlineKeyboardButton(text="📊 بدهی من", callback_data="menu:debt")],
            [InlineKeyboardButton(text="💬 پیام به پشتیبانی", callback_data="menu:support")],
            [InlineKeyboardButton(text="🗑 حذف لینک‌های من", callback_data="menu:removelink")],
        ]
    )


def broadcast_audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 همه نمایندگان", callback_data="bcaud:all")],
            [InlineKeyboardButton(text="💰 بدهکاران", callback_data="bcaud:debtors")],
            [InlineKeyboardButton(text="🟡 فروش صفر این ماه", callback_data="bcaud:zero_sale")],
        ]
    )


def support_reply_keyboard(user_id: int, message_id: int) -> InlineKeyboardMarkup:
    # Carry the user's original message id so the owner's reply quotes (replies to) it.
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ پاسخ", callback_data=f"sup:{user_id}:{message_id}")]]
    )


def owner_menu_keyboard() -> InlineKeyboardMarkup:
    # Note: invoicing + reminders run automatically on a schedule; their manual
    # "run now" triggers live in the web panel to avoid accidental taps in the bot.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 آمار کلی", callback_data="owner:stats")],
            [InlineKeyboardButton(text="💰 بدهکاران", callback_data="owner:debtors")],
            [InlineKeyboardButton(text="📢 پیام همگانی", callback_data="owner:broadcast")],
        ]
    )


def remove_links_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"rm:{rid}")] for rid, name in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]])
