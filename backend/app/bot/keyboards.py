"""Inline keyboards for the bot."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def membership_keyboard(targets: list[dict] | str | None) -> InlineKeyboardMarkup:
    """A join button per required chat + a single «بررسی عضویت» button.
    `targets` is a list of {label, link}; a bare string is accepted for back-compat."""
    if isinstance(targets, str) or targets is None:
        targets = [{"label": "کانال", "link": targets}] if targets else []
    rows: list[list[InlineKeyboardButton]] = []
    for t in targets:
        if t.get("link"):
            rows.append([InlineKeyboardButton(text=f"📢 عضویت در {t.get('label', 'کانال')}", url=t["link"])])
    rows.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_membership")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ثبت لینک پنل من", callback_data="menu:register")],
            [InlineKeyboardButton(text="🖥 پنل‌های من", callback_data="menu:panels")],
            [InlineKeyboardButton(text="👥 مدیریت زیرمجموعه‌ها", callback_data="menu:subs")],
            [InlineKeyboardButton(text="🧾 فاکتورهای من", callback_data="menu:invoices")],
            [InlineKeyboardButton(text="📄 فاکتور علی‌الحساب من", callback_data="menu:interim")],
            [InlineKeyboardButton(text="💳 پرداخت", callback_data="menu:pay")],
            [InlineKeyboardButton(text="📊 بدهی من", callback_data="menu:debt")],
            [InlineKeyboardButton(text="💬 پیام به پشتیبانی", callback_data="menu:support")],
            [InlineKeyboardButton(text="🗑 حذف لینک‌های من", callback_data="menu:removelink")],
        ]
    )


def sub_panels_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per panel the reseller has sub-resellers on. data: subp:<reseller_id>."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"subp:{rid}")] for rid, label in items]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def sub_list_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per sub-reseller. data: subv:<sub_id>. Plus a back button."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"subv:{sid}")] for sid, label in items]
    rows.append([InlineKeyboardButton(text="« بازگشت", callback_data="menu:subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_detail_keyboard(
    sub_id: int, enforced: bool, months: list[str] | None = None, has_cap: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Per-month invoice PDFs the reseller can hand to this sub-reseller.
    for label in (months or [])[:3]:
        rows.append([InlineKeyboardButton(text=f"📄 فاکتور {label}", callback_data=f"subinv:{sub_id}:{label}")])
    # Set / change the monthly GB cap for this sub-reseller.
    rows.append([InlineKeyboardButton(
        text=("✏️ تغییر سقف حجم ماهانه" if has_cap else "🎯 تعیین سقف حجم ماهانه"),
        callback_data=f"subcap:{sub_id}",
    )])
    if enforced:
        rows.append([InlineKeyboardButton(text="✅ آزادسازی", callback_data=f"subr:{sub_id}")])
    else:
        rows.append([InlineKeyboardButton(text="⛔️ مسدودسازی", callback_data=f"subx:{sub_id}")])
    rows.append([InlineKeyboardButton(text="« بازگشت", callback_data="menu:subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 همه نمایندگان", callback_data="bcaud:all")],
            [InlineKeyboardButton(text="💰 بدهکاران", callback_data="bcaud:debtors")],
            [InlineKeyboardButton(text="🟡 فروش صفر این ماه", callback_data="bcaud:zero_sale")],
            [InlineKeyboardButton(text="🖥 نمایندگان یک پنل", callback_data="bcaud:panel")],
        ]
    )


def broadcast_panel_keyboard(panels: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Pick which panel's resellers receive the broadcast. data: bcaud:panel:<panel_id>."""
    rows = [[InlineKeyboardButton(text=name, callback_data=f"bcaud:panel:{pid}")] for pid, name in panels]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def support_reply_keyboard(user_id: int, message_id: int) -> InlineKeyboardMarkup:
    # Carry the user's original message id so the owner's reply quotes (replies to) it.
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ پاسخ", callback_data=f"sup:{user_id}:{message_id}")]]
    )


def owner_menu_keyboard() -> InlineKeyboardMarkup:
    # Heavy/irreversible actions (monthly invoice issue+send) stay in the web panel to
    # avoid accidental taps. The bot exposes the safe, frequently-useful ones.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 آمار کلی", callback_data="owner:stats")],
            [InlineKeyboardButton(text="💰 بدهکاران", callback_data="owner:debtors")],
            [InlineKeyboardButton(text="🟡 فروش صفر این ماه", callback_data="owner:zerosale")],
            [InlineKeyboardButton(text="📢 پیام همگانی", callback_data="owner:broadcast")],
            [InlineKeyboardButton(text="🔄 همگام‌سازی پنل‌ها", callback_data="owner:sync")],
            [InlineKeyboardButton(text="🔔 اجرای یادآوری‌ها", callback_data="owner:dunning")],
            [InlineKeyboardButton(text="🗄 پشتیبان‌گیری اکنون", callback_data="owner:backup")],
        ]
    )


def remove_links_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 حذف {name}", callback_data=f"rm:{rid}")] for rid, name in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]])
