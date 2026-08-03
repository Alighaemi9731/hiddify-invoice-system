"""Storefront-bot setup wizard, the create-user picker flow, invoice/interim views,
the locked pay flow (TXID / receipt / chain pick), and remove-link callbacks."""
from __future__ import annotations

import asyncio
import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import (
    _OWED,
    CreateUserState,
    PayState,
    StorefrontSetupState,
    SupportState,
    _is_top_level_reseller,
    _iso,
    _parse_txid,
    _proof_wanted_fa,
    _resellers_for_chat,
    _reshow_menu,
    _track_user,
    log,
    router,
)
from app.bot.handlers.intake import _handle_payment_proof, _handle_txid
from app.bot.handlers.views import (
    _BOTFATHER_GUIDE,
    _begin_storefront_setup,
    _pending_invoice_ids,
    _pending_payment_for_invoice,
    _send_invoices,
    _send_pay,
    _send_removelink,
    _send_self_interim,
    _storefront_target_line,
)
from app.bot.rtl import rtl
from app.models import Invoice, Panel, Reseller
from app.services import owner_notify


@router.callback_query(F.data == "menu:storefront")
async def cb_menu_storefront(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as s:
        await _begin_storefront_setup(cb.message.answer, cb.from_user.id, s, state)
    await cb.answer()


@router.callback_query(F.data.startswith("sfsetup:"))
async def cb_sf_setup_panel(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        r = await s.get(Reseller, rid)
        # Defensive: must be this person's OWN, storefront-enabled, TOP-LEVEL reseller (never a sub).
        if r is None or r.bot_chat_id != cb.from_user.id \
                or not getattr(r, "storefront_enabled", False) \
                or not await _is_top_level_reseller(s, r):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        target = await _storefront_target_line(s, r)
    await state.set_state(StorefrontSetupState.token)
    await state.update_data(sf_reseller_id=rid)
    await cb.message.answer(rtl(target + _BOTFATHER_GUIDE), parse_mode="HTML",
                            reply_markup=keyboards.flow_cancel_kb())
    await cb.answer()


@router.message(StorefrontSetupState.token, F.text)
async def on_sf_setup_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if ":" not in token or len(token) < 20:
        await message.answer(rtl("توکن نامعتبر است؛ توکنِ BotFather را بفرستید."),
                             reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    try:
        # aiogram validates the token FORMAT synchronously in the constructor — a malformed
        # paste that slips past the cheap pre-check above must get the same friendly reply,
        # not crash the handler (seen live: TokenValidationError, 2026-07-02).
        probe = Bot(token=token)
    except Exception:  # noqa: BLE001
        await message.answer(rtl("توکن نامعتبر است؛ توکنِ BotFather را بفرستید."),
                             reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    try:
        me = await probe.get_me()
    except Exception:  # noqa: BLE001
        await message.answer(rtl("این توکن معتبر نبود؛ مطمئن شوید درست کپی شده و دوباره بفرستید."),
                             reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    finally:
        await probe.session.close()
    data = await state.get_data()
    rid = int(data.get("sf_reseller_id") or 0)
    await state.clear()
    from app.services import storefront

    async with common.SessionLocal() as s:
        r = await s.get(Reseller, rid)
        # Must be this person's OWN, storefront-enabled, TOP-LEVEL reseller (never a sub-reseller).
        if r is None or r.bot_chat_id != message.from_user.id \
                or not getattr(r, "storefront_enabled", False) \
                or not await _is_top_level_reseller(s, r):
            await message.answer(rtl("دسترسی ندارید."))
            return
        # One bot per panel: if THIS panel already has a bot, the token replaces it (data preserved).
        replaced = await storefront.get_bot_for_reseller(s, rid) is not None
        # Each panel needs its OWN Telegram bot/token — reject reusing one already bound elsewhere.
        by_tgid = await storefront.get_bot_by_telegram_id(s, me.id)
        if by_tgid is not None and by_tgid.reseller_id != rid:
            await message.answer(rtl(
                "این توکن قبلاً برای پنلِ دیگری ثبت شده است؛ برای هر پنل یک ربات/توکنِ جداگانه لازم است."))
            return
        await storefront.upsert_bot(s, reseller_id=rid, panel_id=r.panel_id, token=token,
                                    bot_username=me.username, bot_telegram_id=me.id)
    try:  # start it now (the storefront manager runs in this same process)
        from app.bot.storefront import manager

        await manager.reconcile()
    except Exception:  # noqa: BLE001
        log.warning("storefront reconcile after setup failed", exc_info=True)
    if replaced:
        await message.answer(rtl(
            f"✅ رباتِ فروشگاهیِ شما به توکنِ جدید منتقل شد: @{me.username}\n"
            "همهٔ داده‌ها (پلن‌ها، مشتری‌ها، سرویس‌ها، کیفِ پول) حفظ شد و ربات قبلی از کار افتاد.\n"
            "واردِ رباتِ جدید شوید و /start را بزنید."))
    else:
        await message.answer(rtl(
            f"✅ رباتِ فروشگاهیِ شما آماده شد: @{me.username}\n"
            "اکنون واردِ رباتِ خودتان شوید و /start را بزنید تا پنلِ مدیریت را ببینید."))


@router.callback_query(F.data.startswith("cupanel:"))
async def cb_cu_panel(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        r = await s.get(Reseller, rid)
        if r is None or r.bot_chat_id != cb.from_user.id or not await _is_top_level_reseller(s, r):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        name = r.name
    await state.update_data(cu_reseller_id=rid)
    await cb.message.answer(
        f"➕ ساخت سرویس روی پنلِ «{_iso(name)}»\nنوعِ ساخت را انتخاب کنید:",
        reply_markup=keyboards.create_user_mode_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cumode:"))
async def cb_cu_mode(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":")[1]
    data = await state.get_data()
    if "cu_reseller_id" not in data:
        await cb.answer("لطفاً از ابتدا شروع کنید.", show_alert=True)
        return
    async with common.SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if mode == "bulk":
        await state.update_data(cu_mode="bulk")
        await cb.message.answer(
            "تعدادِ سرویس را انتخاب کنید:",
            reply_markup=keyboards.create_user_count_keyboard(opts.counts),
        )
    else:
        await state.update_data(cu_mode="single", cu_count=1)
        await cb.message.answer(
            "حجم را انتخاب کنید:", reply_markup=keyboards.create_user_gb_keyboard(opts.gb)
        )
    await cb.answer()


@router.callback_query(F.data.startswith("cucount:"))
async def cb_cu_count(cb: CallbackQuery, state: FSMContext) -> None:
    count = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if count not in opts.counts:
        await cb.answer("مقدار نامعتبر است.", show_alert=True)
        return
    await state.update_data(cu_count=count)
    await cb.message.answer(
        "حجم را انتخاب کنید:", reply_markup=keyboards.create_user_gb_keyboard(opts.gb)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cugb:"))
async def cb_cu_gb(cb: CallbackQuery, state: FSMContext) -> None:
    gb = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if gb not in opts.gb:
        await cb.answer("مقدار نامعتبر است.", show_alert=True)
        return
    await state.update_data(cu_gb=gb)
    await cb.message.answer(
        "مدت را انتخاب کنید:", reply_markup=keyboards.create_user_days_keyboard(opts.days)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cudays:"))
async def cb_cu_days(cb: CallbackQuery, state: FSMContext) -> None:
    days = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if days not in opts.days:
        await cb.answer("مقدار نامعتبر است.", show_alert=True)
        return
    data = await state.get_data()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_count")):
        await cb.answer("لطفاً از ابتدا شروع کنید.", show_alert=True)
        return
    await state.update_data(cu_days=days)
    await state.set_state(CreateUserState.name)
    is_bulk = data.get("cu_mode") == "bulk"
    prompt = (
        "یک نامِ پایه برای سرویس‌ها بفرستید (به انتهای آن شماره اضافه می‌شود):"
        if is_bulk else "یک نام برای سرویس بفرستید:"
    )
    await cb.message.answer(
        prompt, reply_markup=keyboards.flow_cancel_kb()
    )
    await cb.answer()


@router.message(CreateUserState.name)
async def on_cu_name(message: Message, state: FSMContext) -> None:
    name = " ".join((message.text or "").split())  # collapse whitespace
    if not name or len(name) > 40:
        await message.answer(
            "نام نامعتبر است (۱ تا ۴۰ نویسه)؛ لطفاً دوباره بفرستید.",
            reply_markup=keyboards.cancel_keyboard("« انصراف", "cucancel"),
        )
        return
    await state.update_data(cu_name=name)
    data = await state.get_data()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_days", "cu_count")):
        await state.clear()
        await message.answer("اطلاعات ناقص است؛ لطفاً از «➕ ساخت سرویس» دوباره شروع کنید.")
        return
    count = int(data["cu_count"])
    gb = int(data["cu_gb"])
    days = int(data["cu_days"])
    if data.get("cu_mode") == "bulk":
        summary = (
            "تأیید ساخت گروهیِ سرویس؟\n"
            f"• تعداد: {count} (با نام {_iso(f'{name}1')} تا {_iso(f'{name}{count}')})\n"
            f"• حجم: {gb} گیگابایت\n• مدت: {days} روز"
        )
    else:
        summary = f"تأیید ساخت سرویس؟\n• نام: {_iso(name)}\n• حجم: {gb} گیگابایت\n• مدت: {days} روز"
    await message.answer(summary, reply_markup=keyboards.create_user_confirm_keyboard())


@router.callback_query(F.data == "cuok")
async def cb_cu_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_days", "cu_name", "cu_count")):
        await cb.answer("اطلاعات ناقص است؛ لطفاً از ابتدا شروع کنید.", show_alert=True)
        return
    await cb.answer("در حال ساخت…")
    await _finish_create_user(cb.message, cb.from_user.id, data, user=cb.from_user)


async def _finish_create_user(message: Message, chat_id: int, data: dict, *, user=None) -> None:  # noqa: ANN001
    # `message` is cb.message (bot-authored), so its from_user is the BOT — use the passed real
    # `user` (the reseller) for the menu re-show, falling back to message.from_user defensively.
    menu_user = user or message.from_user
    from app.services import usercreate

    rid = int(data["cu_reseller_id"])
    gb = int(data["cu_gb"])
    days = int(data["cu_days"])
    count = int(data.get("cu_count", 1))
    base = str(data["cu_name"])
    async with common.SessionLocal() as s:
        reseller = await s.get(Reseller, rid)
        if reseller is None or reseller.bot_chat_id != chat_id or not await _is_top_level_reseller(s, reseller):
            await message.answer("دسترسی ندارید.")
            return
        result = await usercreate.create_for_reseller(
            s, reseller, count=count, gb=gb, days=days, base_name=base
        )
        rname = reseller.name
        panel = await s.get(Panel, reseller.panel_id)
        pname = panel.key if panel else "?"

    if result.capacity_blocked:
        remaining = result.remaining if result.remaining is not None else 0
        await message.answer(
            f"⛔️ ظرفیتِ شما برای ساخت {count} سرویس کافی نیست "
            f"(باقی‌مانده: {remaining} از {result.max_users}).\n"
            "برای افزایشِ ظرفیت، از «👥 زیرمجموعه‌ها» یا پشتیبانی اقدام کنید."
        )
        async with common.SessionLocal() as s:
            await _reshow_menu(message, s, menu_user)
        return

    for u in result.created:
        link_attr = html.escape(u.sub_link, quote=True)
        link_text = html.escape(u.sub_link)
        caption = rtl(
            f"✅ سرویس «{html.escape(u.name)}» ساخته شد — {gb} گیگابایت · {days} روز\n\n"
            f"🔗 لینکِ اشتراک:\n<a href=\"{link_attr}\">{link_text}</a>\n<code>{link_text}</code>"
        )
        try:
            png = usercreate.qr_png(u.sub_link)
            await message.bot.send_photo(
                chat_id, BufferedInputFile(png, filename="sub.png"),
                caption=caption, parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001 — fall back to a text message if the photo fails
            log.warning("failed to send created-user QR", exc_info=True)
            await message.bot.send_message(
                chat_id, caption, parse_mode="HTML", disable_web_page_preview=True
            )
        await asyncio.sleep(0.4)  # gentle pacing for Telegram's per-chat rate limit

    made = len(result.created)
    if result.limit_hit:
        await message.answer(
            f"⚠️ {made} سرویس از {count} سرویس ساخته شد؛ ادامه به‌دلیلِ سقفِ ظرفیتِ پنل ممکن نشد. "
            "لطفاً افزایشِ ظرفیت را درخواست کنید."
        )
    elif result.error:
        await message.answer(f"⚠️ {made} سرویس از {count} سرویس ساخته شد؛ ادامه به‌دلیلِ خطا متوقف شد.")
    elif made:
        await message.answer(f"✅ {made} سرویس ساخته شد.")

    if made:
        async with common.SessionLocal() as s:
            await owner_notify.notify_owner(
                s, f"➕ نمایندهٔ «{rname}» روی پنل {pname} تعداد {made} سرویس ساخت "
                   f"({gb} گیگابایت، {days} روز)."
            )
    async with common.SessionLocal() as s:
        await _reshow_menu(message, s, menu_user)


@router.callback_query(F.data == "menu:support")
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await cb.message.answer(
        "پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.flow_cancel_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "menu:invoices")
async def cb_invoices(cb: CallbackQuery, state: FSMContext) -> None:
    await common.clear_stale_flow(state)  # leaving the pay flow → drop its stale invoice set
    async with common.SessionLocal() as s:
        await _send_invoices(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


@router.callback_query(F.data.startswith("inv:"))
async def cb_invoice_view(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Re-send the full content of one of the caller's invoices (text + per-node PDFs)."""
    # Viewing an invoice is NOT the pay flow — clear any stale PayState so a txid sent after
    # this can't be mis-attributed to a previously-selected invoice.
    await common.clear_stale_flow(state)
    inv_id = common._safe_int(cb.data)
    if inv_id is None:
        await cb.answer()
        return
    await cb.answer("در حال ارسال فاکتور…")
    async with common.SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        owned_ids = {r.id for r in resellers}
        inv = await s.get(Invoice, inv_id)
        # Ownership check: a reseller may only view invoices of their own reseller rows.
        if inv is None or inv.reseller_id not in owned_ids:
            await cb.message.answer("این فاکتور در دسترس شما نیست.")
            return
        reseller = await s.get(Reseller, inv.reseller_id)
        from app.services import delivery

        try:
            await delivery.send_invoice_content(s, bot, cb.from_user.id, inv, reseller)
        except Exception:  # noqa: BLE001
            log.warning("invoice view failed for %s", inv_id, exc_info=True)
            await cb.message.answer("ارسال فاکتور با خطا مواجه شد؛ لطفاً بعداً دوباره تلاش کنید.")


@router.callback_query(F.data == "menu:interim")
async def cb_interim(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    await cb.answer("در حال ساخت فاکتور علی‌الحساب…")
    async with common.SessionLocal() as s:
        await _send_self_interim(cb.message.answer, cb.from_user.id, s, bot=bot)
        await _reshow_menu(cb.message, s, cb.from_user)


@router.callback_query(F.data == "menu:pay")
async def cb_pay(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # tapping «پرداخت فاکتور» again resets any prior invoice selection
    async with common.SessionLocal() as s:
        await _send_pay(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: payable-invoice picker — a trailing menu would bury it.
    await cb.answer()


async def _load_pay_invoices(session, data: dict) -> list[Invoice]:
    """The Invoice rows chosen in the pay flow, from FSM data. Reads the new `pay_invoice_ids`
    list and falls back to the legacy single `pay_invoice_id` (for any in-flight state)."""
    ids = list(data.get("pay_invoice_ids") or [])
    if not ids and data.get("pay_invoice_id"):
        ids = [int(data["pay_invoice_id"])]
    invs: list[Invoice] = []
    for iid in ids:
        inv = await session.get(Invoice, int(iid))
        if inv is not None:
            invs.append(inv)
    return invs


async def _start_pay_flow(cb: CallbackQuery, state: FSMContext, invoices: list[Invoice]) -> None:
    """Show the payable total + tap-to-copy instructions for the chosen invoice(s) and lock the
    customer into PayState.waiting with the chosen ids — the TXID/receipt they send next is
    attributed to exactly this set."""
    from app.services import payment_methods

    total_toman = float(sum(float(i.amount_toman or 0) for i in invoices))
    total_usdt = float(sum(float(i.amount_usdt or 0) for i in invoices))
    n = len(invoices)
    async with common.SessionLocal() as s:
        opts = await payment_methods.load_options(s)
        amount_ton = None
        amount_avax = None
        if opts.ton or opts.avax:
            from app.services import rates
            if opts.ton:
                ton_rate = await rates.get_ton_toman(s)
                if ton_rate:
                    amount_ton = f"{total_toman / ton_rate:,.2f}"
            if opts.avax:
                avax_rate = await rates.get_avax_toman(s)
                if avax_rate:
                    amount_avax = f"{total_toman / avax_rate:,.4f}"
    if n == 1:
        header = f"💳 پرداخت فاکتور دوره {invoices[0].period_label}\n"
        footer = ("\n\nℹ️ این مبلغ فقط برای همین فاکتور است. پس از واریز، شناسهٔ تراکنش (TXID) یا "
                  "تصویر رسید را همین‌جا بفرستید (برای لغو: /cancel).")
    else:
        periods = "، ".join(i.period_label for i in invoices)
        header = f"💳 پرداخت {n} فاکتور (دوره‌های {periods})\n"
        footer = (f"\n\nℹ️ این مبلغ برای {n} فاکتورِ انتخاب‌شده است. پس از واریز، شناسهٔ تراکنش "
                  "(TXID) یا تصویر رسید را همین‌جا بفرستید (برای لغو: /cancel).")
    text = (
        header
        + f"مبلغ کل: {total_toman:,.0f} تومان\n\n"
        + payment_methods.instructions_text(
            opts, amount_usdt=f"{total_usdt:,.2f}",
            amount_toman=f"{total_toman:,.0f}", amount_ton=amount_ton,
            amount_avax=amount_avax, html=True)
        + footer
    )
    await state.set_state(PayState.waiting)
    await state.update_data(pay_invoice_ids=[i.id for i in invoices])
    await cb.message.answer(
        rtl(text), parse_mode="HTML",
        reply_markup=keyboards.flow_cancel_kb(),
    )


@router.callback_query(F.data.startswith("payinv:"))
async def cb_pay_invoice(cb: CallbackQuery, state: FSMContext) -> None:
    """Start paying ONE chosen invoice: show its amount + instructions and wait for the
    customer's TXID / receipt photo, which is then attributed to exactly this invoice."""
    # Already mid-payment → don't start another flow. The flow stays locked on the chosen
    # invoice(s) until they send a proof or /cancel; to switch they re-open «💳 پرداخت فاکتور».
    if await state.get_state() == PayState.waiting.state:
        await cb.answer(
            "شما در حال پرداخت هستید. رسید یا شناسهٔ تراکنش (TXID) را بفرستید، "
            "یا برای انتخاب دوباره ابتدا /cancel را بزنید.",
            show_alert=True,
        )
        return

    try:
        inv_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    async with common.SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        owned = {r.id for r in resellers}
        inv = await s.get(Invoice, inv_id)
        # Not payable if: not the caller's, or not owed. A payment deadline (`deferred_until`) is
        # deliberately NOT a blocker — it only re-anchors the reminder/suspension cycle, so a
        # reseller may always settle early if they want to.
        if inv is None or inv.reseller_id not in owned or inv.status not in _OWED:
            await cb.answer("این فاکتور در حال حاضر قابل پرداخت نیست.", show_alert=True)
            return
        # Already submitted a payment covering this invoice → don't let them pay again (one
        # pending per invoice); tell them it's under review.
        if await _pending_payment_for_invoice(s, inv.id) is not None:
            await cb.answer(
                "برای این فاکتور قبلاً رسید فرستاده‌اید و در انتظار تأیید است؛ لطفاً منتظر بمانید.",
                show_alert=True,
            )
            return
        await _start_pay_flow(cb, state, [inv])
    await cb.answer()


@router.callback_query(F.data == "payall")
async def cb_pay_all(cb: CallbackQuery, state: FSMContext) -> None:
    """Pay ALL the customer's payable invoices with one transfer. Gathers every owed invoice not
    already inside a pending payment — including ones with a payment deadline, which are payable
    like any other — and locks the customer into one combined pay flow whose proof settles the
    whole set."""
    if await state.get_state() == PayState.waiting.state:
        await cb.answer(
            "شما در حال پرداخت هستید. رسید یا شناسهٔ تراکنش (TXID) را بفرستید، "
            "یا برای انتخاب دوباره ابتدا /cancel را بزنید.",
            show_alert=True,
        )
        return
    async with common.SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        if not resellers:
            await cb.answer("این فاکتورها در حال حاضر قابل پرداخت نیستند.", show_alert=True)
            return
        ids = [r.id for r in resellers]
        owed = (
            await s.execute(
                select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
                .order_by(Invoice.period_start.asc())
            )
        ).scalars().all()
        pending = await _pending_invoice_ids(s, ids)
        payable = [i for i in owed if i.id not in pending]
        if not payable:
            await cb.answer("بدهی قابل پرداختی ندارید.", show_alert=True)
            return
        await _start_pay_flow(cb, state, payable)
    await cb.answer()


@router.message(PayState.waiting, F.photo)
async def pay_state_photo(message: Message, state: FSMContext) -> None:
    from app.services import payment_methods

    async with common.SessionLocal() as s:
        opts = await payment_methods.load_options(s)
        # A photo is a valid proof ONLY when a photo-based method (card / receipt) is enabled.
        # If the owner turned those off (e.g. only USDT/TON TXID), a photo isn't accepted —
        # stay locked and ask for the right proof.
        if not (opts.card or opts.screenshot):
            await message.answer(
                "❌ پرداخت با «تصویر رسید» فعال نیست.\n"
                f"لطفاً {_proof_wanted_fa(opts)} را بفرستید.",
                reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
            )
            return
        data = await state.get_data()
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _track_user(s, message.from_user)
        await _handle_payment_proof(message, s, invoices=invs)


@router.message(PayState.waiting, F.text)
async def pay_state_text(message: Message, state: FSMContext) -> None:
    """The pay flow is LOCKED: the customer must send a valid txid/receipt (per the enabled
    methods) or /cancel. Any other text gets a clear error and keeps them in the flow — it does
    NOT fall through to the menu."""
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel", "لغو"):
        await state.clear()
        await message.answer("پرداخت لغو شد. هر زمان مایل بودید، می‌توانید دوباره از «💳 پرداخت فاکتور» اقدام کنید.")
        return
    from app.services import payment_methods

    async with common.SessionLocal() as s:
        await _track_user(s, message.from_user)
        opts = await payment_methods.load_options(s)
        parsed = _parse_txid(text, usdt=opts.usdt, ton=opts.ton, avax=opts.avax)
        if parsed is None:
            # Invalid input → explain what's needed and STAY in the pay flow (no menu), with a
            # tappable exit so the user is never stuck guessing /cancel.
            await message.answer(
                rtl(
                    "❌ ورودی نامعتبر است.\n"
                    f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید."
                ),
                reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
            )
            return
        chain, txid = parsed
        if chain == "ambiguous":
            # A bare 0x hash with BOTH USDT(BSC) and AVAX enabled → ask which network. Hold the
            # hash in FSM and stay in PayState; the paychain:* buttons resolve it.
            await state.update_data(pay_txid=txid)
            await message.answer(
                rtl("این تراکنش روی کدام شبکه انجام شده است؟"),
                reply_markup=keyboards.pay_chain_keyboard(),
            )
            return
        data = await state.get_data()
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _handle_txid(message, s, txid, invoices=invs, chain=chain)


@router.message(PayState.waiting)
async def pay_state_other(message: Message) -> None:
    """Any other content (document, sticker, …) while paying → keep them in the locked flow."""
    from app.services import payment_methods

    async with common.SessionLocal() as s:
        opts = await payment_methods.load_options(s)
    await message.answer(
        rtl(f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید."),
        reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
    )


@router.callback_query(F.data.startswith("paychain:"))
async def cb_pay_chain(cb: CallbackQuery, state: FSMContext) -> None:
    """Resolve the «which network?» ask for an ambiguous bare 0x hash: attribute the pending hash
    (held in FSM by `pay_state_text`) to the chosen chain and record it."""
    if await state.get_state() != PayState.waiting.state:
        await cb.answer()
        return
    chain = cb.data.split(":", 1)[1]
    if chain not in ("avax", "bsc"):
        await cb.answer()
        return
    data = await state.get_data()
    txid = (data.get("pay_txid") or "").strip()
    if not txid:
        await state.clear()
        await cb.answer("نشست منقضی شد؛ دوباره از «💳 پرداخت فاکتور» اقدام کنید.", show_alert=True)
        return
    if chain == "avax":
        txid = txid.lower()
    async with common.SessionLocal() as s:
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _track_user(s, cb.from_user)
        await _handle_txid(cb.message, s, txid, invoices=invs, chain=chain, from_user=cb.from_user)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


@router.callback_query(F.data == "menu:removelink")
async def cb_removelink(cb: CallbackQuery) -> None:
    async with common.SessionLocal() as s:
        await _send_removelink(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: link picker (tap to remove) — a trailing menu would bury it.
    await cb.answer()


@router.callback_query(F.data.startswith("rm:"))
async def cb_rm(cb: CallbackQuery) -> None:
    rid = common._safe_int(cb.data)
    if rid is None:
        await cb.answer("دادهٔ نامعتبر است.", show_alert=True)
        return
    async with common.SessionLocal() as s:
        r = await s.get(Reseller, rid)
        if r and r.bot_chat_id == cb.from_user.id:
            r.bot_chat_id = None
            r.link_tag = None
            r.registered_at = None
            await s.commit()
            await cb.message.answer(f"✅ لینک «{r.name}» حذف شد.")
            await cb.answer()
        else:
            # Single answer on the not-found path (the trailing cb.answer() double-answered).
            await cb.answer("موردی یافت نشد.", show_alert=True)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()
