"""Inline keyboards for the bot."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


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


def cancel_row(label: str = "✖️ انصراف", target: str = "cancel") -> list[InlineKeyboardButton]:
    """A single visible-exit button row, appended to every FSM-entry keyboard so a prompt is
    never a button-less dead-end. `target` defaults to the global `cancel` callback (clears the
    FSM state and returns the role-aware main menu)."""
    return [InlineKeyboardButton(text=label, callback_data=target)]


def with_cancel(
    rows: list[list[InlineKeyboardButton]], *, label: str = "✖️ انصراف", target: str = "cancel"
) -> InlineKeyboardMarkup:
    """Wrap keyboard rows and append a visible exit button."""
    return InlineKeyboardMarkup(inline_keyboard=[*rows, cancel_row(label, target)])


def cancel_keyboard(label: str = "✖️ انصراف", target: str = "cancel") -> InlineKeyboardMarkup:
    """A keyboard that is ONLY a visible-exit button — for free-text prompts (TXID, support, etc.)."""
    return InlineKeyboardMarkup(inline_keyboard=[cancel_row(label, target)])


def pay_chain_keyboard() -> InlineKeyboardMarkup:
    """Disambiguate a bare 0x hash when BOTH USDT(BSC) and AVAX are enabled (same hash format):
    ask which network the customer paid on. The pending hash is held in FSM, so the buttons carry
    only the chain."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❄️ اوالانچ (AVAX)", callback_data="paychain:avax"),
         InlineKeyboardButton(text="🟢 تتر (USDT-BSC)", callback_data="paychain:bsc")],
        cancel_row("✖️ انصراف از پرداخت"),
    ])


# ── lean gateway menu: one persistent reply keyboard (≤5) ───────────────────────────────────────
# The everyday menu is a persistent reply keyboard (docked in the typing area) — prettier and always
# at hand. Rare actions live under «⋯ بیشتر». «🌐 پنل تحت وب» is a NORMAL menu button now: it used to
# ride its own extra inline message above every menu (a reply button can't carry a URL), which looked
# bolted-on; tapping it simply replies with the reseller's permanent portal link.
# Labels MUST match the maps below exactly so a tap routes to the right action.
CANCEL_LABEL = "✖️ انصراف"
MORE_LABEL = "⋯ بیشتر"
BACK_LABEL = "« بازگشت به منو"
REGISTER_LABEL = "🔗 ثبت پنل"

# label → action. `newuser`/`storefront` are conditional (shown only when the feature applies).
RESELLER_MAIN: list[tuple[str, str]] = [
    ("🌐 پنل تحت وب", "portal"),
    ("➕ ساخت سرویس", "newuser"),
    ("🧾 فاکتور و پرداخت", "pay"),
    ("💬 پشتیبانی", "support"),
]
RESELLER_MORE: list[tuple[str, str]] = [
    ("🧾 فاکتورهای من", "invoices"),
    ("📄 فاکتور علی‌الحساب", "interim"),
    ("🖥 پنل‌های من", "panels"),
    ("👥 زیرمجموعه‌ها", "subs"),
    ("🏪 راه‌اندازی ربات فروشگاهی", "storefront"),
    ("🔗 ثبت/تغییر لینک پنل", "register"),
    ("🗑 حذف لینک‌ها", "removelink"),
]
OWNER_MAIN: list[tuple[str, str]] = [
    ("📊 آمار", "stats"),
    ("💳 پرداخت‌ها", "payments"),
    ("💰 بدهکاران", "debtors"),
]
OWNER_MORE: list[tuple[str, str]] = [
    ("🩺 سلامت سامانه", "health"),
    ("🔎 جستجوی نماینده", "search"),
    ("📢 پیام همگانی", "broadcast"),
    ("🔄 همگام‌سازی پنل‌ها", "sync"),
    ("🗄 پشتیبان‌گیری اکنون", "backup"),
]
# First-timer (no registered panel): «ثبت پنل» is front-and-center, not buried in «بیشتر».
FIRST_TIMER: list[tuple[str, str]] = [
    (REGISTER_LABEL, "register"),
    ("💬 پشتیبانی", "support"),
]

RESELLER_LABEL_TO_ACTION = {lbl: act for lbl, act in [*RESELLER_MAIN, *RESELLER_MORE, *FIRST_TIMER]}
OWNER_LABEL_TO_ACTION = dict([*OWNER_MAIN, *OWNER_MORE])
_NAV_LABELS = {MORE_LABEL, BACK_LABEL}
# All labels the label-router listens for (nav labels included so «بیشتر»/«بازگشت» route too).
ALL_MENU_LABELS = set(RESELLER_LABEL_TO_ACTION) | set(OWNER_LABEL_TO_ACTION) | _NAV_LABELS


def _reply_grid(labels: list[str], *, extra: list[str] | None = None) -> ReplyKeyboardMarkup:
    """A 2-column persistent reply keyboard; each label in `extra` gets its own full-width row
    (used for «بیشتر»/«بازگشت»)."""
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
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def flow_cancel_kb() -> ReplyKeyboardMarkup:
    """Docked on EVERY flow entry — its ONLY button is «✖️ انصراف», so the main menu is hidden and the
    user can't fire another command mid-operation. Combined with the state-aware label/text routers,
    only cancel (or /start) exits a flow."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_LABEL)]], resize_keyboard=True, is_persistent=True)


def first_timer_reply_kb() -> ReplyKeyboardMarkup:
    return _reply_grid([lbl for lbl, _ in FIRST_TIMER])


def reseller_main_reply_kb(*, show_create_user: bool = False) -> ReplyKeyboardMarkup:
    labels = [lbl for lbl, act in RESELLER_MAIN if not (act == "newuser" and not show_create_user)]
    return _reply_grid(labels, extra=[MORE_LABEL])


def reseller_more_reply_kb(*, show_storefront: bool = False) -> ReplyKeyboardMarkup:
    labels = [lbl for lbl, act in RESELLER_MORE if not (act == "storefront" and not show_storefront)]
    return _reply_grid(labels, extra=[BACK_LABEL])


def owner_main_reply_kb() -> ReplyKeyboardMarkup:
    return _reply_grid([lbl for lbl, _ in OWNER_MAIN], extra=[MORE_LABEL])


def owner_more_reply_kb() -> ReplyKeyboardMarkup:
    return _reply_grid([lbl for lbl, _ in OWNER_MORE], extra=[BACK_LABEL])


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
    sub_id: int, state: str, months: list[str] | None = None, has_cap: bool = False
) -> InlineKeyboardMarkup:
    """`state` is the sub's enforcement_state value: 'active' | 'frozen' | 'enforced'."""
    rows: list[list[InlineKeyboardButton]] = []
    # Per-month invoice PDFs the reseller can hand to this sub-reseller.
    for label in (months or [])[:3]:
        rows.append([InlineKeyboardButton(text=f"📄 فاکتور {label}", callback_data=f"subinv:{sub_id}:{label}")])
    # Set / change the monthly GB cap for this sub-reseller.
    rows.append([InlineKeyboardButton(
        text=("✏️ تغییر سقف حجم ماهانه" if has_cap else "🎯 تعیین سقف حجم ماهانه"),
        callback_data=f"subcap:{sub_id}",
    )])
    if state == "enforced":
        rows.append([InlineKeyboardButton(text="✅ آزادسازی", callback_data=f"subr:{sub_id}")])
    elif state == "frozen":
        # New-user creation is blocked; offer to lift it, or escalate to a full suspend.
        rows.append([InlineKeyboardButton(text="✅ رفع توقف ساخت کاربر", callback_data=f"subr:{sub_id}")])
        rows.append([InlineKeyboardButton(text="⛔️ مسدودسازی کامل", callback_data=f"subx:{sub_id}")])
    else:  # active
        rows.append([InlineKeyboardButton(text="🚫 توقف ساخت کاربر", callback_data=f"subf:{sub_id}")])
        rows.append([InlineKeyboardButton(text="⛔️ مسدودسازی", callback_data=f"subx:{sub_id}")])
    rows.append([InlineKeyboardButton(text="« بازگشت", callback_data="menu:subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


SUB_CAP_PRESETS = [50, 100, 250, 500, 1000]


def sub_cap_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    """Set a sub-reseller's monthly GB cap WITHOUT typing: preset taps set it directly
    (data: setcap:<sub_id>:<gb>), «نامحدود» clears it (gb=0), «مقدار دلخواه» opens the text path
    (data: capcustom:<sub_id>), and «بازگشت» returns to the sub's detail (data: subv:<sub_id>)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for gb in SUB_CAP_PRESETS:
        row.append(InlineKeyboardButton(text=f"{gb} گیگ", callback_data=f"setcap:{sub_id}:{gb}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="♾ حذف سقف (نامحدود)", callback_data=f"setcap:{sub_id}:0")])
    rows.append([InlineKeyboardButton(text="✏️ مقدار دلخواه", callback_data=f"capcustom:{sub_id}")])
    rows.append([InlineKeyboardButton(text="« بازگشت", callback_data=f"subv:{sub_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CAP_BUMP_PRESETS = [100, 200, 500, 1000]


def cap_bump_keyboard(reseller_id: int) -> InlineKeyboardMarkup:
    """Approve a capacity-increase request by a preset amount WITHOUT typing (data:
    capok:<reseller_id>:<n>, which bumps + notifies the reseller), «مقدار دلخواه» opens the text
    path (data: bumptype:<reseller_id>), and «انصراف» exits."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in CAP_BUMP_PRESETS:
        row.append(InlineKeyboardButton(text=f"➕{n}", callback_data=f"capok:{reseller_id}:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ مقدار دلخواه", callback_data=f"bumptype:{reseller_id}")])
    rows.append(cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_audience_keyboard() -> InlineKeyboardMarkup:
    """The bot's compact audience picker (the panel's Broadcast page carries the FULL filter set)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 همه نمایندگان", callback_data="bcaud:all")],
            [InlineKeyboardButton(text="💰 بدهکاران", callback_data="bcaud:debtors")],
            [InlineKeyboardButton(text="⏰ سررسیدگذشته‌ها", callback_data="bcaud:overdue")],
            [InlineKeyboardButton(text="📅 مهلت‌دارها (به مهلت نرسیده)", callback_data="bcaud:deferred")],
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


def storefront_setup_panels_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Pick which top-level reseller/panel the storefront bot will sell from. data: sfsetup:<reseller_id>."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"sfsetup:{rid}")] for rid, label in items]
    rows.append([InlineKeyboardButton(text="✖️ انصراف", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_user_panels_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Pick which top-level reseller/panel to create the user under. data: cupanel:<reseller_id>."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"cupanel:{rid}")] for rid, label in items]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def create_user_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👤 تکی", callback_data="cumode:single"),
        InlineKeyboardButton(text="👥 گروهی", callback_data="cumode:bulk"),
    ], [InlineKeyboardButton(text="« انصراف", callback_data="cucancel")]])


def _num_grid(prefix: str, values: list[int], *, unit: str = "") -> InlineKeyboardMarkup:
    """A compact grid (2 per row) of numeric choices → data: <prefix>:<value>."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for v in values:
        row.append(InlineKeyboardButton(text=f"{v}{unit}", callback_data=f"{prefix}:{v}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="« انصراف", callback_data="cucancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_user_count_keyboard(counts: list[int]) -> InlineKeyboardMarkup:
    return _num_grid("cucount", counts, unit=" تا")


def create_user_gb_keyboard(gbs: list[int]) -> InlineKeyboardMarkup:
    return _num_grid("cugb", gbs, unit=" گیگ")


def create_user_days_keyboard(days: list[int]) -> InlineKeyboardMarkup:
    return _num_grid("cudays", days, unit=" روزه")


def create_user_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله، بساز", callback_data="cuok"),
        InlineKeyboardButton(text="❌ لغو", callback_data="cucancel"),
    ]])


def capacity_request_keyboard(reseller_id: int, amount: int) -> InlineKeyboardMarkup:
    """Owner actions on a reseller's capacity-increase request: approve the requested amount,
    pick a custom amount, or reject. `amount` is the reseller's requested bump (0 = unspecified)."""
    rows: list[list[InlineKeyboardButton]] = []
    if amount and amount > 0:
        rows.append([InlineKeyboardButton(
            text=f"✅ تأیید (+{amount})", callback_data=f"capok:{reseller_id}:{amount}")])
        rows.append([
            InlineKeyboardButton(text="✏️ مبلغِ دیگر", callback_data=f"capmore:{reseller_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"capno:{reseller_id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="✏️ تعیین و افزایش", callback_data=f"capmore:{reseller_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"capno:{reseller_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_reply_keyboard(user_id: int, message_id: int) -> InlineKeyboardMarkup:
    # Carry the user's original message id so the owner's reply quotes (replies to) it.
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ پاسخ", callback_data=f"sup:{user_id}:{message_id}")]]
    )


def owner_stats_keyboard(active_label: str, periods: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Period switch for the stats view + a per-panel breakdown button. `periods` is a list of
    (label, caption); the active one is marked. data: ostat:<label> / opanel:<label>."""
    row = [
        InlineKeyboardButton(
            text=("• " + cap + " •") if label == active_label else cap,
            callback_data=f"ostat:{label}",
        )
        for label, cap in periods
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="🖥 تفکیکِ پنل", callback_data=f"opanel:{active_label}")],
    ])


def owner_pending_payments_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per pending payment (id, caption). data: opv:<payment_id>."""
    rows = [[InlineKeyboardButton(text=cap, callback_data=f"opv:{pid}")] for pid, cap in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="—", callback_data="noop")]])


def owner_payment_detail_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید", callback_data=f"opok:{payment_id}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"opno:{payment_id}"),
    ], [InlineKeyboardButton(text="« پرداخت‌های در انتظار", callback_data="owner:payments")]])


def owner_payment_decided_keyboard() -> InlineKeyboardMarkup:
    """Shown after the owner confirms/rejects — the تأیید/رد buttons are gone (so the decision is
    unmistakable and can't be re-tapped); only the back-to-pending link remains."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="« پرداخت‌های در انتظار", callback_data="owner:payments")]])


def owner_reseller_results_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Search results: one button per matched reseller. data: orc:<reseller_id>."""
    rows = [[InlineKeyboardButton(text=cap, callback_data=f"orc:{rid}")] for rid, cap in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="—", callback_data="noop")]])


def owner_reseller_card_keyboard(
    reseller_id: int, *, enforced: bool, tg_href: str | None
) -> InlineKeyboardMarkup:
    """Quick actions on a reseller from the bot: suspend/restore, bump capacity, invoices, PV."""
    rows: list[list[InlineKeyboardButton]] = []
    if enforced:
        rows.append([InlineKeyboardButton(text="🔓 آزادسازی", callback_data=f"ores:{reseller_id}")])
    else:
        rows.append([InlineKeyboardButton(text="⛔️ مسدودسازی", callback_data=f"oenf:{reseller_id}")])
    rows.append([
        InlineKeyboardButton(text="➕۱۰۰", callback_data=f"obump:{reseller_id}:100"),
        InlineKeyboardButton(text="➕۲۰۰", callback_data=f"obump:{reseller_id}:200"),
        InlineKeyboardButton(text="➕۵۰۰", callback_data=f"obump:{reseller_id}:500"),
    ])
    last = [InlineKeyboardButton(text="🧾 فاکتورها", callback_data=f"orcinv:{reseller_id}")]
    if tg_href:
        last.append(InlineKeyboardButton(text="💬 گفتگو", url=tg_href))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_invoices_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per invoice (data: inv:<invoice_id>) — tapping re-sends the full invoice."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"inv:{iid}")] for iid, label in items]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def pay_invoices_keyboard(
    items: list[tuple[int, str]], *, pay_all_label: str | None = None
) -> InlineKeyboardMarkup:
    """One button per UNPAID invoice (data: payinv:<invoice_id>) — tapping starts paying THAT
    invoice on its own. When `pay_all_label` is given (2+ payable invoices), a «pay all» button
    (data: payall) is prepended to settle every payable invoice with one transfer."""
    rows: list[list[InlineKeyboardButton]] = []
    if pay_all_label:
        rows.append([InlineKeyboardButton(text=pay_all_label, callback_data="payall")])
    rows += [[InlineKeyboardButton(text=label, callback_data=f"payinv:{iid}")] for iid, label in items]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def remove_links_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 حذف {name}", callback_data=f"rm:{rid}")] for rid, name in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]])
