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
            [InlineKeyboardButton(text="🧾 فاکتورهای پرداخت‌نشده", callback_data="menu:invoices"),
             InlineKeyboardButton(text="💳 پرداخت فاکتور", callback_data="menu:pay")],
            [InlineKeyboardButton(text="📄 فاکتور علی‌الحساب (ماه جاری)", callback_data="menu:interim")],
            [InlineKeyboardButton(text="🖥 پنل‌های من", callback_data="menu:panels"),
             InlineKeyboardButton(text="👥 زیرمجموعه‌ها", callback_data="menu:subs")],
            [InlineKeyboardButton(text="🌐 ورود به پنلِ تحتِ وب", callback_data="menu:portal")],
            [InlineKeyboardButton(text="🔗 ثبت لینک پنل من", callback_data="menu:register")],
            [InlineKeyboardButton(text="💬 پیام به پشتیبانی", callback_data="menu:support"),
             InlineKeyboardButton(text="🗑 حذف لینک‌ها", callback_data="menu:removelink")],
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
    # A compact 2-column grid. Heavy/irreversible actions (monthly invoice issue+send) stay in
    # the web panel to avoid accidental taps.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 آمار", callback_data="owner:stats"),
             InlineKeyboardButton(text="🩺 سلامت سامانه", callback_data="owner:health")],
            [InlineKeyboardButton(text="💳 پرداخت‌های در انتظار", callback_data="owner:payments"),
             InlineKeyboardButton(text="💰 بدهکاران", callback_data="owner:debtors")],
            [InlineKeyboardButton(text="🔎 جستجوی نماینده", callback_data="owner:search"),
             InlineKeyboardButton(text="📢 پیام همگانی", callback_data="owner:broadcast")],
            [InlineKeyboardButton(text="🔄 همگام‌سازی پنل‌ها", callback_data="owner:sync"),
             InlineKeyboardButton(text="🗄 پشتیبان‌گیری اکنون", callback_data="owner:backup")],
        ]
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


def pay_invoices_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per UNPAID invoice (data: payinv:<invoice_id>) — tapping starts paying THAT
    invoice on its own (separate from the others)."""
    rows = [[InlineKeyboardButton(text=label, callback_data=f"payinv:{iid}")] for iid, label in items]
    return InlineKeyboardMarkup(
        inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]]
    )


def pay_invoice_button(invoice_id: int) -> InlineKeyboardMarkup:
    """A single «💳 پرداخت فاکتور» glass button placed under a sent invoice — tapping opens the
    locked pay flow for THAT invoice. The ONLY way to submit a payment (no cold txid/photo)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 پرداخت فاکتور", callback_data=f"payinv:{invoice_id}")
    ]])


def remove_links_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 حذف {name}", callback_data=f"rm:{rid}")] for rid, name in items]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="—", callback_data="noop")]])
