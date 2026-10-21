"""Owner broadcast (audience pick + background send) and the global cancel command/callback."""
from __future__ import annotations

import asyncio
import csv
import hmac
import html
import io
import secrets

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import BroadcastState, log, router
from app.bot.rtl import rtl
from app.models import Panel
from app.schemas.broadcast import BroadcastFilters
from app.services import broadcast as bc

# --------------------------- broadcast (owner) ---------------------------
_AUDIENCE_FA = {
    "all": "همه نمایندگان",
    "debtors": "بدهکاران",
    "overdue": "سررسیدگذشته‌ها",
    "deferred": "مهلت‌دارها (هنوز به مهلت نرسیده)",
    "zero_sale": "فروش صفر این ماه",
    "enforced": "مسدودشده‌ها", "frozen": "فریزشده‌ها",
    "pending_payment": "پرداخت در انتظار تأیید", "paid_up": "بدون بدهی",
    "few_active": "کاربران فعال کم", "invoice_below": "فروش ماه جاری زیر مبلغ",
    "invoice_above": "فروش ماه جاری بالای مبلغ", "new_resellers": "نمایندگان جدید",
}


async def _audience_label(session, audience: str, panel_id: int | None) -> str:
    if audience == "panel" and panel_id is not None:
        panel = await session.get(Panel, panel_id)
        return f"نمایندگان پنل {panel.key}" if panel else "نمایندگان یک پنل"
    return _AUDIENCE_FA.get(audience, audience)


async def _bot_broadcast_bg(text: str, reachable: list, unregistered: int) -> None:
    """Background send for an owner bot-triggered broadcast — its own session/bot (the handler's
    session is already closed). The final summary reaches the owner via owner_notify.notify_owner."""
    try:
        async with common.SessionLocal() as session:
            from app.services import broadcast as bc

            await bc.run_broadcast(session, text, reachable, unregistered=unregistered)
    except Exception:  # noqa: BLE001
        log.exception("bot background broadcast failed")


async def _do_broadcast(
    message: Message, session, text: str, audience: str = "all", panel_id: int | None = None
) -> None:
    from app.services import broadcast as bc

    reachable, unregistered = await bc.resolve_recipients(session, audience, panel_id, None)
    label = await _audience_label(session, audience, panel_id)
    if not text.strip() or not reachable:
        await message.answer(rtl(f"📢 «{label}»: گیرنده‌ای برای ارسال وجود نداشت."))
        return
    # Send in the background (bounded concurrency + rate limit); reply immediately. The summary
    # arrives here when it finishes (notify_owner targets the owner's PV = this chat).
    asyncio.create_task(_bot_broadcast_bg(text, reachable, len(unregistered)))
    note = [f"📣 ارسال به «{label}» در پس‌زمینه شروع شد — {len(reachable)} گیرنده."]
    if unregistered:
        note.append(f"ℹ️ {len(unregistered)} نماینده در ربات ثبت‌نام نکرده‌اند (پیام نمی‌گیرند).")
    note.append("خلاصهٔ نتیجه پس از پایان، همین‌جا ارسال می‌شود.")
    await message.answer(rtl("\n".join(note)))


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    if not any(data == "cancel" for row in rows for _label, data in row):
        rows = [*rows, [("✖️ انصراف", "cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


async def show_filters(answer, state: FSMContext) -> None:
    data = await state.get_data()
    f = BroadcastFilters.model_validate(data.get("filters", {}))
    audience = data.get("audience", "all")
    description = (
        f"📢 {_AUDIENCE_FA.get(audience, audience)}\n"
        f"پنل‌ها: {', '.join(map(str, f.panel_ids)) or 'همه'}\n"
        f"ماه فاکتور (میلادی): {f.period_from or 'از ابتدا'} تا {f.period_to or 'تاکنون'}\n"
        f"دسترسی: {f.access_state or 'همه'} | بدهی: {f.min_debt or 0:,.0f} تا {f.max_debt if f.max_debt is not None else '∞'} تومان\n"
        f"سن بدهی: {f.min_age_days if f.min_age_days is not None else 'آزاد'} تا {f.max_age_days if f.max_age_days is not None else 'آزاد'} روز\n"
        f"حذف پرداخت منتظر تأیید: {'بله' if f.exclude_pending else 'خیر'}\n"
        f"جستجو: {f.search or '—'} | حساب کنارگذاشته: {len(f.excluded_reseller_ids)}\n"
        "همهٔ شرط‌ها هم‌زمان اعمال می‌شوند. سن بدهی از ارسال یا مهلت جدید شمرده می‌شود."
    )
    rows = [[("🖥 انتخاب چند پنل", "bcf:panels"), ("📅 ماه / بازه فاکتور", "bcf:months")],
            [("🔐 وضعیت دسترسی", "bcf:access"), ("💰 بازه بدهی", "bcf:value:debt")],
            [("⏳ بازه سن بدهی", "bcf:value:age"), ("🧾 تغییر حذف منتظرها", "bcf:pending")],
            [("🔎 جستجوی نام / شناسه", "bcf:value:search"), ("حذف شناسه‌ها از ارسال", "bcf:value:exclude")]]
    if audience in ("few_active", "invoice_below", "invoice_above", "new_resellers"):
        description += f"\nآستانه: {data.get('threshold', 'تعیین نشده')}"
        rows.append([("✏️ مقدار آستانه", "bcf:value:threshold")])
    rows += [[("👀 پیش‌نمایش و نوشتن پیام", "bcf:compose")],
             [("پاک کردن فیلترها", "bcf:reset"), ("✖️ انصراف", "cancel")]]
    await state.set_state(BroadcastState.configuring)
    await answer(rtl(html.escape(description)), reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("bcaud:"))
async def cb_broadcast_audience(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    parts = cb.data.split(":")
    audience = parts[1]
    if audience == "panel":
        audience = "all"
    if audience not in bc.AUDIENCES:
        await cb.answer("فیلتر نامعتبر است.", show_alert=True)
        return
    await state.clear()
    await state.update_data(audience=audience, filters={})
    await show_filters(cb.message.answer, state)
    await cb.answer()


async def _resolve(session, data):
    return await bc.preview(session, audience=data.get("audience", "all"),
                            threshold=data.get("threshold"),
                            filters=BroadcastFilters.model_validate(data.get("filters", {})))


def _preview_text(result) -> str:
    lines = [f"👀 {result['matched_accounts']} حساب؛ {result['total']} گیرنده یکتا؛ "
             f"{result['unregistered']} بدون ربات",
             f"بدهی دوره انتخابی: {result['debt_toman']:,.0f} تومان",
             f"{result['duplicate_chats']} حساب تکراری ادغام شد."]
    for rec in (result["recipients"] + result["skipped"])[:20]:
        for account in rec.get("accounts", []):
            lines.append(f"• {account['name'][:60]} | {account['panel']} | "
                         f"{account['debt_toman']:,.0f} تومان | {', '.join(account['periods'])}")
    if result["matched"] > 20:
        lines.append("فهرست کامل در پیش‌نمایش سامانه در دسترس است.")
    return "\n".join(lines)[:3500]


@router.callback_query(F.data.startswith("bcf:"))
async def cb_broadcast_filters(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if await state.get_state() not in (BroadcastState.configuring.state, BroadcastState.filter_value.state):
            await cb.answer("انتخاب قدیمی است؛ پیام همگانی را دوباره باز کنید.", show_alert=True)
            return
        data = await state.get_data()
        f = BroadcastFilters.model_validate(data.get("filters", {}))
        parts = cb.data.split(":")
        action = parts[1]
        if action in ("panels", "panel"):
            panels = (await session.execute(select(Panel).where(Panel.enabled.is_(True)).order_by(Panel.key))).scalars().all()
            if action == "panel":
                pid = int(parts[2])
                if pid not in {p.id for p in panels}:
                    await cb.answer("پنل معتبر نیست.", show_alert=True)
                    return
                f.panel_ids = [p for p in f.panel_ids if p != pid] if pid in f.panel_ids else [*f.panel_ids, pid]
                await state.update_data(filters=f.model_dump())
            await cb.message.answer("انتخاب پنل‌ها؛ انتخاب خالی یعنی همهٔ پنل‌ها.", reply_markup=_kb([
                [(('✅ ' if p.id in f.panel_ids else '') + p.key, f"bcf:panel:{p.id}")] for p in panels
            ] + [[("تمام / بازگشت", "bcf:back")]]))
        elif action == "months":
            await cb.message.answer("ماه فاکتور، نه تاریخ ارسال یا مسدودی؛ دوره‌ها میلادی هستند.", reply_markup=_kb([
                [("فقط یک ماه", "bcf:monthpick:single:0")],
                [("از ماه", "bcf:monthpick:from:0"), ("تا ماه", "bcf:monthpick:to:0")],
                [("همه ماه‌ها", "bcf:monthclear"), ("بازگشت", "bcf:back")]]))
        elif action == "monthpick":
            months = (await bc.filter_options(session))["periods"]
            field, page = parts[2], max(0, int(parts[3]))
            rows = [[(month, f"bcf:monthset:{field}:{month}")] for month in months[page*12:(page+1)*12]]
            nav = []
            if page > 0:
                nav.append(("قبلی", f"bcf:monthpick:{field}:{page-1}"))
            if len(months) > (page+1)*12:
                nav.append(("بعدی", f"bcf:monthpick:{field}:{page+1}"))
            await cb.message.answer("دوره فاکتور را انتخاب کنید:", reply_markup=_kb(rows + ([nav] if nav else []) + [[("بازگشت", "bcf:months")]]))
        elif action in ("monthset", "monthclear"):
            values = f.model_dump()
            if action == "monthclear":
                values.update(period_from=None, period_to=None)
            else:
                field, month = parts[2], parts[3]
                if field not in ("single", "from", "to"):
                    await cb.answer("نامعتبر", show_alert=True)
                    return
                if field == "single":
                    values.update(period_from=month, period_to=month)
                else:
                    values['period_' + field] = month
            try:
                f = BroadcastFilters.model_validate(values)
            except ValueError:
                await cb.answer("بازه ماه‌ها معکوس است؛ ابتدا همه ماه‌ها را انتخاب کنید.", show_alert=True)
                return
            await state.update_data(filters=f.model_dump())
            await show_filters(cb.message.answer, state)
        elif action == "access":
            await cb.message.answer("وضعیت فعلی دسترسی:", reply_markup=_kb([
                [(label, "bcf:accessset:" + key)] for key, label in
                [("all", "همه"), ("active", "فعال"), ("frozen", "فریز"), ("enforced", "مسدود")]
            ]))
        elif action == "accessset":
            values = f.model_dump()
            values["access_state"] = None if parts[2] == "all" else parts[2]
            f = BroadcastFilters.model_validate(values)
            await state.update_data(filters=f.model_dump())
            await show_filters(cb.message.answer, state)
        elif action == "pending":
            f.exclude_pending = not f.exclude_pending
            await state.update_data(filters=f.model_dump())
            await show_filters(cb.message.answer, state)
        elif action == "value":
            field = parts[2]
            if field not in ("debt", "age", "search", "threshold", "exclude"):
                await cb.answer("نامعتبر", show_alert=True)
                return
            await state.update_data(filter_field=field)
            await state.set_state(BroadcastState.filter_value)
            prompt = ("بازه را به صورت حداقل:حداکثر بنویسید. مثال 100000:500000 تومان یا 50:55 روز. "
                      "هر طرف می‌تواند خالی باشد؛ برای پاک کردن بنویسید - .")
            if field == "search":
                prompt = "بخشی از نام یا شناسه نماینده را بنویسید؛ پاک کردن: -"
            if field == "exclude":
                prompt = "شناسه حساب‌هایی که نباید پیام بگیرند با فاصله جدا کنید؛ پاک کردن: -"
            if field == "threshold":
                prompt = "آستانه مثبت را وارد کنید (تومان / تعداد کاربر / روز بر اساس گروه انتخابی)."
            await cb.message.answer(prompt, reply_markup=keyboards.flow_cancel_kb())
        elif action == "compose":
            if data.get("audience") in ("few_active", "invoice_below", "invoice_above", "new_resellers") and not data.get("threshold"):
                await cb.answer("ابتدا مقدار آستانه را مشخص کنید.", show_alert=True)
                return
            result = await _resolve(session, data)
            await cb.message.answer(_preview_text(result), parse_mode=None)
            if result["total"]:
                await state.set_state(BroadcastState.waiting)
                await cb.message.answer("متن پیام را بنویسید؛ قبل از ارسال تأیید نهایی می‌گیرید.", reply_markup=keyboards.flow_cancel_kb())
        elif action in ("reset", "back"):
            if action == "reset":
                await state.update_data(filters={})
            await show_filters(cb.message.answer, state)
    await cb.answer()


async def on_broadcast_filter_value(message: Message, state: FSMContext) -> None:
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, message.from_user):
            return
    data = await state.get_data()
    value = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "").replace("٬", "")
    field = data.get("filter_field")
    values = dict(data.get("filters", {}))
    try:
        if field == "search":
            values["search"] = "" if value == "-" else value
        elif field == "exclude":
            values["excluded_reseller_ids"] = [] if value == "-" else [int(v) for v in value.split()]
        elif field == "threshold":
            number = float(value)
            if not 0 < number <= (36500 if data.get("audience") == "new_resellers" else 1e15):
                raise ValueError()
            await state.update_data(threshold=number)
        else:
            low, high = ("", "") if value == "-" else value.split(":")
            convert = int if field == "age" else float
            suffix = "age_days" if field == "age" else "debt"
            values['min_' + suffix] = convert(low) if low.strip() else None
            values['max_' + suffix] = convert(high) if high.strip() else None
        f = BroadcastFilters.model_validate(values)
    except (ValueError, TypeError):
        await message.answer("مقدار یا بازه معتبر نیست؛ دوباره وارد کنید.")
        return
    await state.update_data(filters=f.model_dump())
    await show_filters(message.answer, state)


async def prepare_confirmation(message: Message, state: FSMContext, session, text: str) -> None:
    data = await state.get_data()
    result = await _resolve(session, data)
    if not result["total"]:
        await message.answer("گیرنده‌ای برای ارسال نیست.")
        await show_filters(message.answer, state)
        return
    nonce = secrets.token_hex(6)
    await state.update_data(text=text, preview_token=result["preview_token"], confirm_nonce=nonce)
    await state.set_state(BroadcastState.confirming)
    await message.answer(_preview_text(result), parse_mode=None)
    await message.answer("متن پیام:\n" + text, parse_mode=None)
    await message.answer("ارسال به همین گیرندگان انجام شود؟", reply_markup=_kb([
        [(f"📤 ارسال به {result['total']} نفر", "bcconfirm:" + nonce)],
        [("📋 فهرست کامل CSV", "bclist")],
        [("تغییر فیلترها", "bcedit"), ("✖️ انصراف", "cancel")]]))


@router.callback_query(F.data == "bclist")
async def cb_broadcast_recipient_list(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if await state.get_state() != BroadcastState.confirming.state:
            await cb.answer("ابتدا پیش‌نمایش بگیرید.", show_alert=True)
            return
        result = await _resolve(session, await state.get_data())
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["شناسه", "نام", "پنل", "دوره", "بدهی تومان", "سن بدهی", "وضعیت ربات"])
    for rec in result["recipients"] + result["skipped"]:
        for a in rec.get("accounts", []):
            row = [a['reseller_id'], a['name'], a['panel'], ' / '.join(a['periods']),
                   a['debt_toman'], a['oldest_debt_days'], rec['status']]
            writer.writerow([("'" + str(v)) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
    await cb.message.answer_document(BufferedInputFile(out.getvalue().encode('utf-8-sig'), filename="broadcast-recipients.csv"))
    await cb.answer()


_confirmation_lock = asyncio.Lock()


@router.callback_query(F.data == "bcedit")
async def cb_broadcast_edit(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    if await state.get_state() != BroadcastState.confirming.state:
        await cb.answer("انتخاب قدیمی است.", show_alert=True)
        return
    await show_filters(cb.message.answer, state)
    await cb.answer()


@router.callback_query(F.data.startswith("bcconfirm:"))
async def cb_broadcast_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    async with _confirmation_lock:
        async with common.SessionLocal() as session:
            if not await common._is_owner_user(session, cb.from_user):
                await cb.answer("دسترسی ندارید.", show_alert=True)
                return
            data = await state.get_data()
            if (await state.get_state() != BroadcastState.confirming.state
                    or cb.data != "bcconfirm:" + data.get("confirm_nonce", "")):
                await cb.answer("این ارسال قبلاً انجام شده یا منقضی است.", show_alert=True)
                return
            result = await _resolve(session, data)
            if not hmac.compare_digest(data.get("preview_token", ""), result["preview_token"]):
                await cb.message.answer("گیرندگان یا بدهی تغییر کرده‌اند؛ دوباره پیش‌نمایش بگیرید.")
                await show_filters(cb.message.answer, state)
                await cb.answer()
                return
            await state.clear()
            asyncio.create_task(_bot_broadcast_bg(data["text"], result["recipients"], result["unregistered"]))
            await cb.message.answer(f"📣 ارسال به {result['total']} نفر شروع شد؛ خلاصه پس از پایان ارسال می‌شود.")
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Always restore the menu, even when no flow was active (see on_cancel_label)."""
    await state.clear()
    await message.answer("لغو شد.")
    async with common.SessionLocal() as s:
        await common._reshow_menu(message, s, message.from_user)


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Universal exit button carried by every FSM prompt: clear the state and return the role-aware
    main menu, so a user can ALWAYS tap their way out (never forced to remember /cancel)."""
    await state.clear()
    async with common.SessionLocal() as s:
        await common._send_menu(cb.message.answer, s, cb.from_user, bot=bot)
    await cb.answer("لغو شد.")
