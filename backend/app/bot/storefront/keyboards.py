"""Keyboards for the per-reseller storefront bots (admin side + customer side)."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot.rtl import rtl
from app.models import StorefrontBot, StorefrontOrder, StorefrontPlan

# ── docked reply-keyboard menus (label → action) ──────────────────────────────
ADMIN_MENU: list[tuple[str, str]] = [
    ("🧩 پلن‌ها", "plans"),
    ("💳 روش‌های پرداخت", "pay"),
    ("🎁 تنظیماتِ تست رایگان", "trialcfg"),
    ("📝 پیام خوش‌آمد", "welcome"),
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
# Shown only when the admin enabled it AND the customer hasn't claimed it yet (added to the keyboard
# dynamically), but always routable so a tap is never a dead end.
FREE_TRIAL_LABEL = "🎁 تست رایگان"
ADMIN_LABEL_TO_ACTION = dict(ADMIN_MENU)
CUSTOMER_LABEL_TO_ACTION = {**dict(CUSTOMER_MENU), FREE_TRIAL_LABEL: "trial"}
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


def customer_reply_kb(*, is_admin_preview: bool = False, show_free_trial: bool = False) -> ReplyKeyboardMarkup:
    labels = [label for label, _ in CUSTOMER_MENU]
    if show_free_trial:
        labels = [FREE_TRIAL_LABEL, *labels]
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
        [InlineKeyboardButton(text=rtl(plan_label(p)), callback_data=f"sfbuy:{p.id}")]
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
            InlineKeyboardButton(text=rtl(f"{plan_label(p)}{flag}"), callback_data="sfnoop"),
            InlineKeyboardButton(text="🗑", callback_data=f"sfplandel:{p.id}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن پلن", callback_data="sfplanadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_kb(orders: list[StorefrontOrder]) -> InlineKeyboardMarkup:
    """One button per provisioned service (tap → live usage/expiry + link/QR). The label is
    rtl()-wrapped so a mixed Persian/English service name doesn't scramble the order."""
    rows = [
        [InlineKeyboardButton(
            text=rtl(f"📦 {o.gb}گیگ/{o.days}روز — {o.label or 'سرویس'}"),
            callback_data=f"sforder:{o.id}")]
        for o in orders
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="sfnoop")]]
    )


def order_actions_kb(order_id: int, renew_price: int, *, paused: bool) -> InlineKeyboardMarkup:
    """Customer controls for one provisioned service: renew (at the current price), pause/resume, delete."""
    toggle = ("▶️ فعال‌سازی", "sftgl") if paused else ("⏸ توقف", "sftgl")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=rtl(f"🔄 تمدید ({renew_price:,} تومان)"),
                              callback_data=f"sfrenew:{order_id}")],
        [InlineKeyboardButton(text=toggle[0], callback_data=f"{toggle[1]}:{order_id}"),
         InlineKeyboardButton(text="🗑 حذف", callback_data=f"sfdel:{order_id}")],
    ])


def confirm_kb(yes_text: str, yes_cb: str, *, no_cb: str = "sfcancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=yes_text, callback_data=yes_cb)],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data=no_cb)],
    ])


def admin_subs_kb(orders: list[StorefrontOrder]) -> InlineKeyboardMarkup:
    """Admin view of a customer's subscriptions — tap one to manage (renew/pause/delete)."""
    rows = [
        [InlineKeyboardButton(
            text=rtl(f"{'⏸ ' if o.status == 'disabled' else ''}{o.label or 'سرویس'} — "
                     f"{o.gb}گیگ/{o.days}روز"),
            callback_data=f"sfasub:{o.id}")]
        for o in orders
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="sfnoop")]]
    )


def admin_sub_actions_kb(order_id: int, *, paused: bool) -> InlineKeyboardMarkup:
    toggle = ("▶️ فعال‌سازی", "sfatgl") if paused else ("⏸ توقف", "sfatgl")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تمدیدِ رایگان", callback_data=f"sfarenew:{order_id}")],
        [InlineKeyboardButton(text=toggle[0], callback_data=f"{toggle[1]}:{order_id}"),
         InlineKeyboardButton(text="🗑 حذف", callback_data=f"sfadel:{order_id}")],
    ])


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


def trial_settings_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Admin toggles the one-time free trial on/off and edits its volume/duration."""
    state = "✅ فعال" if bot.free_trial_enabled else "❌ غیرفعال"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"وضعیت: {state} (تغییر)", callback_data="sftrialtog")],
        [InlineKeyboardButton(text="✏️ تغییرِ حجم/مدت", callback_data="sftrialset")],
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
        InlineKeyboardButton(text="📦 سرویس‌ها", callback_data=f"sfacust:{customer_id}"),
    ]])
