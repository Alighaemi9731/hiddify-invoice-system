"""Keyboards for the per-reseller storefront bots (admin side + customer side)."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot.rtl import rtl
from app.core.tg_links import (
    clean_username,  # noqa: F401 (re-exported for callers/tests)
)
from app.models import (
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)

# ── docked reply-keyboard menus (label → action) ──────────────────────────────
ADMIN_MENU: list[tuple[str, str]] = [
    ("🧩 پلن‌ها", "plans"),
    ("💳 روش‌های پرداخت", "pay"),
    ("🎁 تنظیماتِ تست رایگان", "trialcfg"),
    ("🔒 عضویت اجباری", "joincfg"),
    ("📝 پیام خوش‌آمد", "welcome"),
    ("🧾 شارژهای در انتظار", "topups"),
    ("👥 مشتری‌ها", "customers"),
    ("📊 آمار", "stats"),
    ("📢 پیام همگانی", "broadcast"),
    ("💬 پشتیبانی", "support"),
    ("🛡 مدیرانِ ربات", "admins"),
    ("🎁 کدهای شارژ", "credits"),
    ("🔴 وضعیت فروشگاه", "shopstate"),
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
# Lean-menu nav + flow-lock cancel (mirrors the main bot). All labels stay routable; only which are
# DOCKED (top-level vs «بیشتر») changes — so the plan-007 parity contract (ADMIN_LABEL_TO_ACTION) holds.
CANCEL_LABEL = "✖️ انصراف"
# Navigation, NOT a capability — deliberately kept out of ADMIN_MENU/ADMIN_LABEL_TO_ACTION so the
# bot↔portal parity contract keeps describing real management actions only. Owner-only: a co-admin
# is not a portal principal.
PORTAL_LABEL = "🌐 مدیریت فروشگاه در مرورگر"
MORE_LABEL = "⋯ بیشتر"
BACK_LABEL = "« بازگشت به منو"
_ACTION_TO_LABEL = {act: lbl for lbl, act in ADMIN_MENU}
# The ≤5 docked top-level admin actions; everything else lives under «بیشتر». «admins» is owner-only.
ADMIN_MAIN_ACTIONS = ["topups", "customers", "broadcast"]
ADMIN_MORE_ACTIONS = [act for _, act in ADMIN_MENU if act not in ADMIN_MAIN_ACTIONS]
ALL_LABELS = (set(ADMIN_LABEL_TO_ACTION) | set(CUSTOMER_LABEL_TO_ACTION)
              | {BACK_TO_ADMIN, MORE_LABEL, BACK_LABEL, PORTAL_LABEL})


def _reply_kb(rows: list[list[KeyboardButton]]) -> ReplyKeyboardMarkup:
    """Build a docked reply keyboard.

    NOTE — deliberately NOT `is_persistent=True`. That flag asks clients to always show the custom
    keyboard and never let it be collapsed; on Android the system BACK button's normal job is to
    collapse the keyboard, so with it set, back appeared dead — shop CUSTOMERS reported exactly
    that. Without it the keyboard still shows on every menu message and can be reopened from the
    keyboard icon."""
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _grid(labels: list[str], *, extra: list[str] | None = None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for label in labels:
        row.append(KeyboardButton(text=label))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    for e in extra or []:
        rows.append([KeyboardButton(text=e)])
    return _reply_kb(rows)


def flow_cancel_kb() -> ReplyKeyboardMarkup:
    """Docked on a flow entry: cancel-only, so the admin/customer can't fire another command mid-flow."""
    return _reply_kb([[KeyboardButton(text=CANCEL_LABEL)]])


def admin_reply_kb() -> ReplyKeyboardMarkup:
    """Legacy full 14-label admin keyboard (kept for compatibility / tests)."""
    return _grid([label for label, _ in ADMIN_MENU])


def admin_main_reply_kb(*, owner: bool = False) -> ReplyKeyboardMarkup:
    """Lean ≤5 top-level admin menu (urgent actions) + «بیشتر».

    The OWNER also gets «🌐 مدیریت فروشگاه در مرورگر» as a normal menu button. It used to ride its
    own inline message above every menu, which looked bolted-on; tapping it now simply replies with
    their permanent portal link. Co-admins don't see it — they aren't portal principals."""
    labels = [_ACTION_TO_LABEL[a] for a in ADMIN_MAIN_ACTIONS]
    extra = ([PORTAL_LABEL] if owner else []) + [MORE_LABEL]
    return _grid(labels, extra=extra)


def admin_more_reply_kb(*, owner: bool = False) -> ReplyKeyboardMarkup:
    """The rest of the admin actions under «بیشتر». «مدیرانِ ربات» (admins) is owner-only."""
    labels = [_ACTION_TO_LABEL[a] for a in ADMIN_MORE_ACTIONS if owner or a != "admins"]
    return _grid(labels, extra=[BACK_LABEL])


def owner_portal_inline_kb(url: str) -> InlineKeyboardMarkup:
    """One-tap browser button for the shop OWNER (must be inline — a reply button can't carry a URL)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 مدیریت فروشگاه در مرورگر", url=url)]])


def customer_reply_kb(*, is_admin_preview: bool = False, show_free_trial: bool = False) -> ReplyKeyboardMarkup:
    labels = [label for label, _ in CUSTOMER_MENU]
    if show_free_trial:
        labels = [FREE_TRIAL_LABEL, *labels]
    if is_admin_preview:
        labels = [*labels, BACK_TO_ADMIN]
    return _grid(labels)


def cancel_kb(label: str = "✖️ انصراف") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="sfcancel")]])


def admins_manage_kb(co_admin_ids: list[int]) -> InlineKeyboardMarkup:
    """Manage the shop's co-admins: add one, or remove any existing co-admin (the owning reseller is
    the permanent main admin and is never listed here)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ افزودن مدیر", callback_data="sfaddadmin")]
    ]
    for tid in co_admin_ids:
        rows.append([InlineKeyboardButton(text=f"🗑 حذف {tid}", callback_data=f"sfdeladm:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    # Each plan gets a label row + an action row (edit · move up · move down · delete), so plans can
    # be edited and reordered in place — no need to delete everything and re-add to fix the order.
    rows: list[list[InlineKeyboardButton]] = []
    n = len(plans)
    for i, p in enumerate(plans):
        flag = "" if p.enabled else " (غیرفعال)"
        rows.append([InlineKeyboardButton(text=rtl(f"{plan_label(p)}{flag}"), callback_data="sfnoop")])
        actions = [InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"sfplanedit:{p.id}")]
        if i > 0:
            actions.append(InlineKeyboardButton(text="⬆️", callback_data=f"sfplanup:{p.id}"))
        if i < n - 1:
            actions.append(InlineKeyboardButton(text="⬇️", callback_data=f"sfplandown:{p.id}"))
        actions.append(InlineKeyboardButton(text="🗑", callback_data=f"sfplandel:{p.id}"))
        rows.append(actions)
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


def order_actions_kb(
    order_id: int, renew_price: int, *, paused: bool, is_trial: bool = False
) -> InlineKeyboardMarkup:
    """Customer controls for one provisioned service: renew (at the current price), pause/resume,
    delete. Free trials get NO renew button — they're one-time (non-renewable)."""
    toggle = ("▶️ فعال‌سازی", "sftgl") if paused else ("⏸ توقف", "sftgl")
    rows = []
    if not is_trial:
        rows.append([InlineKeyboardButton(text=rtl(f"🔄 تمدید ({renew_price:,} تومان)"),
                                          callback_data=f"sfrenew:{order_id}")])
    rows.append([InlineKeyboardButton(text=toggle[0], callback_data=f"{toggle[1]}:{order_id}"),
                 InlineKeyboardButton(text="🗑 حذف", callback_data=f"sfdel:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def admin_sub_actions_kb(
    order_id: int, *, paused: bool, is_trial: bool = False
) -> InlineKeyboardMarkup:
    toggle = ("▶️ فعال‌سازی", "sfatgl") if paused else ("⏸ توقف", "sfatgl")
    rows = []
    if not is_trial:  # trials are one-time — no free admin renew either
        rows.append([InlineKeyboardButton(text="🔄 تمدیدِ رایگان",
                                          callback_data=f"sfarenew:{order_id}")])
    rows.append([InlineKeyboardButton(text=toggle[0], callback_data=f"{toggle[1]}:{order_id}"),
                 InlineKeyboardButton(text="🗑 حذف", callback_data=f"sfadel:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def buy_confirm_kb(op_id: str) -> InlineKeyboardMarkup:
    """Final confirm before charging the wallet + creating the config. `op_id` (a durable idempotency
    token minted here) rides in the callback so a re-tapped/re-delivered confirm can't double-charge."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ خرید و ساختِ سرویس", callback_data=f"sfbuyok:{op_id}")],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")],
    ])


def wallet_kb(*, gift: bool = False) -> InlineKeyboardMarkup:
    """Wallet screen — show balance first, then offer top-up (owner: don't jump straight to amount).
    When the shop has a gift code available, also offer «🎁 کدِ هدیه» (standalone credit, no payment)."""
    rows = [[InlineKeyboardButton(text="➕ افزایش موجودی", callback_data="sftopup")]]
    if gift:
        rows.append([InlineKeyboardButton(text="🎁 کدِ هدیه دارم", callback_data="sfgift")])
    rows.append([InlineKeyboardButton(text="✖️ بستن", callback_data="sfcancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── credit codes (کد شارژ/هدیه) ────────────────────────────────────────────────
def credit_label(c) -> str:  # noqa: ANN001 — StorefrontCreditCode
    if c.is_gift:
        kind = f"🎁 هدیه {c.amount_toman:,}ت"
    elif c.kind == "percent":
        cap = f" (سقف {c.max_bonus_toman:,}ت)" if c.max_bonus_toman else ""
        kind = f"+{c.percent_off}٪{cap}"
    else:
        kind = f"+{c.amount_toman:,}ت"
    used = f"{c.used_count}" + (f"/{c.max_uses}" if c.max_uses else "")
    return f"{c.code} · {kind} · {used} · {'فعال ✅' if c.enabled else 'غیرفعال ❌'}"


def credit_codes_manage_kb(codes) -> InlineKeyboardMarkup:  # noqa: ANN001
    rows: list[list[InlineKeyboardButton]] = []
    for c in codes:
        rows.append([InlineKeyboardButton(text=rtl(credit_label(c)), callback_data="sfnoop")])
        rows.append([
            InlineKeyboardButton(text="🔴 غیرفعال" if c.enabled else "🟢 فعال",
                                 callback_data=f"sfcredtog:{c.id}"),
            InlineKeyboardButton(text="📊 مصرف", callback_data=f"sfcredusage:{c.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"sfcreddel:{c.id}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن کد", callback_data="sfcredadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def credit_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="٪ درصدی روی شارژ", callback_data="sfcredtype:percent")],
        [InlineKeyboardButton(text="＋ مبلغِ ثابت روی شارژ", callback_data="sfcredtype:fixed")],
        [InlineKeyboardButton(text="🎁 هدیهٔ بدونِ پرداخت", callback_data="sfcredtype:gift")],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")],
    ])


def credit_skip_kb(cb_data: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=cb_data)],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")],
    ])


def credit_percustomer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="۱ بار (پیش‌فرض)", callback_data="sfcredpc:1"),
         InlineKeyboardButton(text="نامحدود", callback_data="sfcredpc:0")],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data="sfcancel")],
    ])


def topup_code_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 کدِ شارژ دارم", callback_data="sftopcode")],
        [InlineKeyboardButton(text="➡️ ادامه بدون کد", callback_data="sftopnocode")],
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
        [InlineKeyboardButton(
            text=f"کارت‌به‌کارت {t(bot.pay_card_enabled)}",
            callback_data=f"sfpaytog:card:{int(not bot.pay_card_enabled)}:{bot.config_version}"),
         InlineKeyboardButton(text="✏️ کارت", callback_data="sfpayset:card")],
        [InlineKeyboardButton(
            text=f"تتر USDT {t(bot.pay_usdt_enabled)}",
            callback_data=f"sfpaytog:usdt:{int(not bot.pay_usdt_enabled)}:{bot.config_version}"),
         InlineKeyboardButton(text="✏️ آدرس", callback_data="sfpayset:usdt")],
        [InlineKeyboardButton(
            text=f"گرام/تون {t(bot.pay_ton_enabled)}",
            callback_data=f"sfpaytog:ton:{int(not bot.pay_ton_enabled)}:{bot.config_version}"),
         InlineKeyboardButton(text="✏️ آدرس", callback_data="sfpayset:ton")],
    ])


def join_settings_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Admin: toggle forced channel-join on/off, set/change the channel, or clear it."""
    state = "✅ فعال" if bot.channel_required else "❌ غیرفعال"
    rows = [[InlineKeyboardButton(
        text=f"وضعیت: {state} (تغییر)",
        callback_data=f"sfjointog:{int(not bot.channel_required)}:{bot.config_version}")],
            [InlineKeyboardButton(text="✏️ تنظیم/تغییرِ کانال", callback_data="sfjoinset")]]
    if bot.channel_id:
        rows.append([InlineKeyboardButton(text="🗑 حذفِ کانال", callback_data="sfjoinclear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def join_prompt_kb(link: str | None) -> InlineKeyboardMarkup:
    """Customer gate: a join button (when a link is known) + a re-check button."""
    rows: list[list[InlineKeyboardButton]] = []
    if link:
        rows.append([InlineKeyboardButton(text="📢 عضویت در کانال", url=link)])
    rows.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="sfjoincheck")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trial_settings_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Admin toggles the one-time free trial on/off and edits its volume/duration."""
    state = "✅ فعال" if bot.free_trial_enabled else "❌ غیرفعال"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"وضعیت: {state} (تغییر)",
            callback_data=f"sftrialtog:{int(not bot.free_trial_enabled)}:{bot.config_version}")],
        [InlineKeyboardButton(text="✏️ تغییرِ حجم/مدت", callback_data="sftrialset")],
    ])


def broadcast_segment_kb() -> InlineKeyboardMarkup:
    """Admin picks WHO a broadcast targets before composing it."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 همهٔ مشتری‌ها", callback_data="sfbcseg:all")],
        [InlineKeyboardButton(text="⛔️ منقضی‌شده‌ها", callback_data="sfbcseg:expired")],
        [InlineKeyboardButton(text="😴 ۳۰ روز غیرفعال", callback_data="sfbcseg:inactive30")],
        [InlineKeyboardButton(text="🎁 تست‌گرفته، نخریده", callback_data="sfbcseg:trial_no_purchase")],
    ])


def shop_state_kb(bot: StorefrontBot) -> InlineKeyboardMarkup:
    """Admin toggles «temporarily closed» on/off and edits the closed message. When closed,
    customers can't buy/renew (they see the message) but the bot stays online."""
    toggle_text = "🟢 بازکردنِ فروشگاه" if bot.shop_closed else "🔴 بستنِ موقتِ فروشگاه"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=toggle_text,
            callback_data=f"sfshoptog:{int(not bot.shop_closed)}:{bot.config_version}")],
        [InlineKeyboardButton(text="✏️ متنِ پیامِ بسته‌بودن", callback_data="sfshopmsg")],
    ])


def topup_decide_kb(txn_id: int, *, portal_url: str | None = None) -> InlineKeyboardMarkup:
    """Admin confirm/reject a pending top-up. «تأیید با مبلغ» lets them set the credited Toman (crypto).
    `portal_url` (owner-only) adds a plain HTTPS button that opens the shop's top-up queue in the
    browser — the urgent confirm/reject stays inline for everyone."""
    rows = [
        [InlineKeyboardButton(text="✅ تأیید", callback_data=f"sfok:{txn_id}"),
         InlineKeyboardButton(text="✏️ تأیید با مبلغِ دلخواه", callback_data=f"sfokamt:{txn_id}")],
        [InlineKeyboardButton(text="❌ رد", callback_data=f"sfno:{txn_id}")],
    ]
    if portal_url:
        rows.append([InlineKeyboardButton(text="🌐 مشاهده در پنل", url=portal_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customers_page_kb(
    rows: list[StorefrontCustomer], *, page: int, per_page: int, total: int, searching: bool = False
) -> InlineKeyboardMarkup:
    """A tidy single-message customer list: one button per customer (→ detail), plus page nav + search
    (full list) or a back-to-list button (search results)."""
    kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=rtl(f"{c.name or c.telegram_id} — {int(c.wallet_balance_toman or 0):,} ت"),
            callback_data=f"sfcust:{c.id}")]
        for c in rows
    ]
    if searching:
        kb.append([InlineKeyboardButton(text="‹ بازگشت به فهرست", callback_data="sfcustpg:0")])
        return InlineKeyboardMarkup(inline_keyboard=kb)
    pages = max(1, (total + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ قبلی", callback_data=f"sfcustpg:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"صفحه {page + 1}/{pages}", callback_data="sfnoop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="بعدی ›", callback_data=f"sfcustpg:{page + 1}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(text="🔍 جستجو", callback_data="sfcustsearch")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def customer_detail_kb(
    customer_id: int, *, username: str | None = None, chat_id: int | None = None,
    banned: bool = False,
) -> InlineKeyboardMarkup:
    """Admin customer actions. Reaching the customer is a bot-RELAY («💬 پیام به مشتری» →
    `sfmsg:<id>`): the shop bot forwards the admin's message to the customer, so it works for EVERY
    customer regardless of username or privacy settings. When the customer ALSO has a public
    @username we add a direct-PV shortcut (t.me/<username> always resolves). We deliberately never
    emit a `tg://user?id=` link for a username-less customer — Telegram can't resolve it for the
    admin (a third party who has never met them), so it would be a dead link (that was the old bug)."""
    rows: list[list[InlineKeyboardButton]] = []
    u = clean_username(username)
    if u:
        rows.append([InlineKeyboardButton(text="↗️ گفتگوی مستقیم", url=f"https://t.me/{u}")])
    rows.append([InlineKeyboardButton(text="💬 پیام به مشتری", callback_data=f"sfmsg:{customer_id}")])
    rows += [
        [InlineKeyboardButton(text="➕ شارژ دستی", callback_data=f"sfadj:{customer_id}:+"),
         InlineKeyboardButton(text="➖ کسر دستی", callback_data=f"sfadj:{customer_id}:-")],
        [InlineKeyboardButton(text="📦 سرویس‌ها", callback_data=f"sfacust:{customer_id}")],
    ]
    if banned:
        rows.append([InlineKeyboardButton(text="✅ رفعِ مسدودی", callback_data=f"sfcustunban:{customer_id}")])
    else:
        rows.append([InlineKeyboardButton(text="⛔️ مسدود کردن", callback_data=f"sfcustban:{customer_id}")])
    rows.append([InlineKeyboardButton(text="‹ بازگشت به فهرست", callback_data="sfcustpg:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def relay_reply_kb(customer_id: int, *, portal_url: str | None = None) -> InlineKeyboardMarkup:
    """A «💬 پاسخ» button attached to a customer's relayed message → opens the admin reply-compose
    flow (`sfmsg:<id>`, the same one the customer-detail card uses). `portal_url` (owner-only) adds a
    plain HTTPS button that opens that customer's page in the browser."""
    rows = [[InlineKeyboardButton(text="💬 پاسخ به مشتری", callback_data=f"sfmsg:{customer_id}")]]
    if portal_url:
        rows.append([InlineKeyboardButton(text="🌐 مشاهده در پنل", url=portal_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
