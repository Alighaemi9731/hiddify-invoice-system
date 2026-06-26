"""Keyboards for the per-reseller storefront bots (admin side + customer side)."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.models import StorefrontBot, StorefrontOrder, StorefrontPlan

# ── docked reply-keyboard menus (label → action) ──────────────────────────────
ADMIN_MENU: list[tuple[str, str]] = [
    ("🧩 پلن‌ها", "plans"),
    ("💳 روش‌های پرداخت", "pay"),
    ("🧾 شارژهای در انتظار", "topups"),
    ("👥 مشتری‌ها", "customers"),
    ("📊 آمار", "stats"),
    ("📢 پیام همگانی", "broadcast"),
    ("💬 پشتیبانی", "support"),
    ("👤 نمای مشتری", "preview"),
]
CUSTOMER_MENU: list[tuple[str, str]] = [
    ("🛒 خرید سرویس", "buy"),
    ("👛 کیف پول", "wallet"),
    ("📦 سرویس‌های من", "orders"),
    ("💬 پشتیبانی", "support"),
]
ADMIN_LABEL_TO_ACTION = dict(ADMIN_MENU)
CUSTOMER_LABEL_TO_ACTION = dict(CUSTOMER_MENU)
# The admin can jump back from the customer preview.
BACK_TO_ADMIN = "« بازگشت به مدیریت"
ALL_LABELS = set(ADMIN_LABEL_TO_ACTION) | set(CUSTOMER_LABEL_TO_ACTION) | {BACK_TO_ADMIN}


def _grid(labels: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for label in labels:
        row.append(KeyboardButton(text=label))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def admin_reply_kb() -> ReplyKeyboardMarkup:
    return _grid([label for label, _ in ADMIN_MENU])


def customer_reply_kb(*, is_admin_preview: bool = False) -> ReplyKeyboardMarkup:
    labels = [label for label, _ in CUSTOMER_MENU]
    if is_admin_preview:
        labels = [*labels, BACK_TO_ADMIN]
    return _grid(labels)


def cancel_kb(label: str = "✖️ انصراف") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="sfcancel")]])


# ── inline keyboards ──────────────────────────────────────────────────────────
def plan_label(p: StorefrontPlan) -> str:
    """Customer-facing plan label — volume · duration · price, no title (owner: «عنوان نمی‌خواهیم»)."""
    return f"{p.gb} گیگ · {p.days} روزه — {p.price_toman:,} تومان"


def buy_plans_kb(plans: list[StorefrontPlan]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=plan_label(p), callback_data=f"sfbuy:{p.id}")]
        for p in plans
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="sfnoop")]]
    )


def plans_manage_kb(plans: list[StorefrontPlan]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans:
        flag = "" if p.enabled else " (غیرفعال)"
        rows.append([
            InlineKeyboardButton(text=f"{plan_label(p)}{flag}", callback_data="sfnoop"),
            InlineKeyboardButton(text="🗑", callback_data=f"sfplandel:{p.id}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن پلن", callback_data="sfplanadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_kb(orders: list[StorefrontOrder]) -> InlineKeyboardMarkup:
    """One button per provisioned service (tap → live usage/expiry + link/QR)."""
    rows = [
        [InlineKeyboardButton(
            text=f"{o.label or 'سرویس'} — {o.gb}گیگ/{o.days}روز",
            callback_data=f"sforder:{o.id}")]
        for o in orders
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="sfnoop")]]
    )


def buy_confirm_kb() -> InlineKeyboardMarkup:
    """Final confirm before charging the wallet + creating the config."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ خرید و ساختِ سرویس", callback_data="sfbuyok")],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")],
    ])


def wallet_kb() -> InlineKeyboardMarkup:
    """Wallet screen — show balance first, then offer top-up (owner: don't jump straight to amount)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزایش موجودی", callback_data="sftopup")],
        [InlineKeyboardButton(text="✖️ بستن", callback_data="sfcancel")],
    ])


def pay_methods_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Customer picks a payment method (only the ones the admin enabled + configured)."""
    rows: list[list[InlineKeyboardButton]] = []
    if bot.pay_card_enabled and bot.card_number:
        rows.append([InlineKeyboardButton(text="💳 کارت‌به‌کارت", callback_data="sftop:card")])
    if bot.pay_usdt_enabled and bot.usdt_address:
        rows.append([InlineKeyboardButton(text="₮ تتر (USDT-BEP20)", callback_data="sftop:usdt")])
    if bot.pay_ton_enabled and bot.ton_address:
        rows.append([InlineKeyboardButton(text="💎 گرام/تون (GRAM/TON)", callback_data="sftop:ton")])
    rows.append([InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_settings_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Admin toggles + edits each payment method."""
    def t(on: bool) -> str:
        return "✅" if on else "❌"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"کارت‌به‌کارت {t(bot.pay_card_enabled)}", callback_data="sfpaytog:card"),
         InlineKeyboardButton(text="✏️ کارت", callback_data="sfpayset:card")],
        [InlineKeyboardButton(text=f"تتر USDT {t(bot.pay_usdt_enabled)}", callback_data="sfpaytog:usdt"),
         InlineKeyboardButton(text="✏️ آدرس", callback_data="sfpayset:usdt")],
        [InlineKeyboardButton(text=f"گرام/تون {t(bot.pay_ton_enabled)}", callback_data="sfpaytog:ton"),
         InlineKeyboardButton(text="✏️ آدرس", callback_data="sfpayset:ton")],
    ])


def topup_decide_kb(txn_id: int) -> InlineKeyboardMarkup:
    """Admin confirm/reject a pending top-up. «تأیید با مبلغ» lets them set the credited Toman (crypto)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید", callback_data=f"sfok:{txn_id}"),
         InlineKeyboardButton(text="✏️ تأیید با مبلغِ دلخواه", callback_data=f"sfokamt:{txn_id}")],
        [InlineKeyboardButton(text="❌ رد", callback_data=f"sfno:{txn_id}")],
    ])


def customer_row_kb(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ شارژ دستی", callback_data=f"sfadj:{customer_id}:+"),
        InlineKeyboardButton(text="➖ کسر دستی", callback_data=f"sfadj:{customer_id}:-"),
    ]])
