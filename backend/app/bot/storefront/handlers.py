"""Storefront bot handlers (one router shared by every reseller's bot; tenant resolved by bot.id).

Admin side = the reseller (their Telegram id == the reseller's bot_chat_id): plans, payment settings,
top-up review, customers + manual wallet edit, broadcast, support, customer preview.
Customer side = everyone else: buy (wallet-funded) → auto-provision config, wallet + top-up, my services.
All money is manual: the admin confirms each top-up and sets the credited Toman (no rates/API).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select

from app.bot.rtl import rtl
from app.bot.storefront import keyboards as kb
from app.core.db import SessionLocal
from app.models import (
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront, storefront_provision, storefront_wallet, usercreate

log = logging.getLogger("bot.storefront")

storefront_router = Router()
storefront_router.message.filter(F.chat.type == "private")


class SF(StatesGroup):
    plan_gb = State()
    plan_days = State()
    plan_price = State()
    pay_value = State()        # data: method (+ card sub-step)
    trial_gb = State()         # admin sets free-trial volume
    trial_days = State()       # admin sets free-trial duration; data: t_gb
    buy_name = State()         # customer names the config; data: buy_plan_id
    topup_amount = State()     # data: nothing yet
    topup_proof = State()      # data: amount, method
    confirm_amount = State()   # admin sets credited Toman; data: txn_id
    adjust_amount = State()    # admin manual wallet edit; data: customer_id, sign
    support = State()
    broadcast = State()


# ── helpers ───────────────────────────────────────────────────────────────────

def _digits(text: str | None) -> str:
    return (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬,", "0123456789  ")).replace(" ", "")


def _toman(value) -> str:  # noqa: ANN001
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


async def _resolve(session, bot: Bot, user) -> tuple[StorefrontBot | None, Reseller | None, bool]:  # noqa: ANN001
    """Resolve the tenant for the physical bot, plus whether `user` is the reseller-admin."""
    sf = await storefront.get_bot_by_telegram_id(session, bot.id)
    if sf is None:
        return None, None, False
    reseller = await session.get(Reseller, sf.reseller_id)
    is_admin = reseller is not None and reseller.bot_chat_id == user.id
    return sf, reseller, is_admin


async def _send_admin_menu(answer, sf: StorefrontBot) -> None:  # noqa: ANN001
    await answer(
        rtl(f"🛠 پنلِ مدیریتِ فروشگاه\nربات: @{sf.bot_username or '—'}\nیک گزینه را انتخاب کنید:"),
        reply_markup=kb.admin_reply_kb(),
    )


def _trial_available(sf: StorefrontBot, customer: StorefrontCustomer) -> bool:
    return bool(sf.free_trial_enabled) and not customer.free_trial_used


async def _send_customer_menu(answer, sf: StorefrontBot, customer: StorefrontCustomer, *, preview=False) -> None:  # noqa: ANN001
    bal = _toman(storefront_wallet.balance(customer))
    text = sf.welcome_text or "🛍 به فروشگاه خوش آمدید!"
    lines = [text, "", f"👛 موجودیِ کیفِ پولِ شما: {bal} تومان"]
    show_trial = _trial_available(sf, customer)
    if show_trial:
        lines.append(f"🎁 تستِ رایگان ({sf.free_trial_gb} گیگ · {sf.free_trial_days} روز) فعال است.")
    await answer(
        rtl("\n".join(lines)),
        reply_markup=kb.customer_reply_kb(is_admin_preview=preview, show_free_trial=show_trial),
    )


async def _notify_admin(bot: Bot, reseller: Reseller, text: str, **kw) -> None:  # noqa: ANN001, ANN003
    if not reseller or not reseller.bot_chat_id:
        return
    try:
        await bot.send_message(reseller.bot_chat_id, rtl(text), **kw)
    except Exception:  # noqa: BLE001
        log.warning("notify storefront admin failed", exc_info=True)


async def _deliver_config(  # noqa: ANN001
    bot: Bot, chat_id: int, *, gb: int, days: int, sub_link: str, name: str | None = None
) -> None:
    head = f"✅ سرویسِ «{name}» آماده شد" if name else "✅ سرویسِ شما آماده شد"
    caption = rtl(
        f"{head} — {gb} گیگ · {days} روز\n\n"
        f"🔗 لینکِ اشتراک:\n<code>{sub_link}</code>"
    )
    try:
        png = usercreate.qr_png(sub_link)
        await bot.send_photo(chat_id, BufferedInputFile(png, filename="sub.png"),
                             caption=caption, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=True)


# ── /start + menu dispatch ──────────────────────────────────────────────────

@storefront_router.message(Command("start"))
async def sf_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None:
            await message.answer("این ربات هنوز پیکربندی نشده است.")
            return
        if is_admin:
            await _send_admin_menu(message.answer, sf)
            return
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        if customer.banned:
            await message.answer(rtl("دسترسیِ شما مسدود شده است."))
            return
        await _send_customer_menu(message.answer, sf, customer)


@storefront_router.message(F.text.in_(kb.ALL_LABELS))
async def sf_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        await state.clear()
        if text == kb.BACK_TO_ADMIN and is_admin:
            await _send_admin_menu(message.answer, sf)
            return
        if is_admin and text in kb.ADMIN_LABEL_TO_ACTION:
            await _admin_action(kb.ADMIN_LABEL_TO_ACTION[text], message, state, s, sf, reseller)
            return
        # customer (or admin in preview)
        if text in kb.CUSTOMER_LABEL_TO_ACTION:
            customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
            if customer.banned:
                await message.answer(rtl("دسترسیِ شما مسدود شده است."))
                return
            await _customer_action(
                kb.CUSTOMER_LABEL_TO_ACTION[text], message, state, s, sf, customer, bot)


# ── ADMIN ───────────────────────────────────────────────────────────────────

async def _admin_action(action, message, state, s, sf, reseller) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "plans":
        plans = await storefront.list_plans(s, sf.id)
        await ans(rtl("🧩 پلن‌های فروشگاه:"), reply_markup=kb.plans_manage_kb(plans))
    elif action == "pay":
        await ans(rtl(
            "💳 روش‌های پرداخت را تنظیم کنید.\n"
            f"کارت: {sf.card_number or '—'} ({sf.card_holder or '—'})\n"
            f"تتر: {sf.usdt_address or '—'}\nگرام/تون: {sf.ton_address or '—'}"),
            reply_markup=kb.pay_settings_kb(sf))
    elif action == "trialcfg":
        await ans(rtl(
            f"🎁 تستِ رایگان (یک‌بار برای هر مشتری)\n"
            f"وضعیت: {'فعال ✅' if sf.free_trial_enabled else 'غیرفعال ❌'}\n"
            f"حجم: {sf.free_trial_gb} گیگ · مدت: {sf.free_trial_days} روز\n\n"
            "نکته: حجمِ ۱ گیگ (یا کمتر) برای شما رایگان است؛ بیشتر از آن در فاکتورِ شما حساب می‌شود."),
            reply_markup=kb.trial_settings_kb(sf))
    elif action == "topups":
        pend = await storefront_wallet.pending_topups_for_bot(s, sf.id)
        if not pend:
            await ans(rtl("شارژِ در انتظاری وجود ندارد."))
            return
        await ans(rtl(f"🧾 {len(pend)} شارژِ در انتظارِ تأیید:"))
        for txn in pend:
            cust = await s.get(StorefrontCustomer, txn.customer_id)
            cap = (f"#{txn.id} — {_toman(txn.amount_toman)} ت — {txn.method or '—'}\n"
                   f"مشتری: {(cust.name if cust else '') or txn.customer_id}")
            await ans(rtl(cap), reply_markup=kb.topup_decide_kb(txn.id))
    elif action == "customers":
        custs = await storefront.list_customers(s, sf.id)
        if not custs:
            await ans(rtl("هنوز مشتری‌ای ندارید."))
            return
        await ans(rtl(f"👥 {len(custs)} مشتری:"))
        for c in custs[:30]:
            await ans(
                rtl(f"{c.name or c.telegram_id} — موجودی: {_toman(c.wallet_balance_toman)} ت"),
                reply_markup=kb.customer_row_kb(c.id))
    elif action == "stats":
        plans = await storefront.list_plans(s, sf.id)
        custs = await storefront.list_customers(s, sf.id)
        pend = await storefront_wallet.pending_topups_for_bot(s, sf.id)
        await ans(rtl(
            f"📊 آمار\nمشتری‌ها: {len(custs)}\nپلن‌ها: {len(plans)}\n"
            f"شارژِ در انتظار: {len(pend)}"))
    elif action == "broadcast":
        await state.set_state(SF.broadcast)
        await ans(rtl("متنِ پیامِ همگانی به مشتری‌ها را بفرستید:"), reply_markup=kb.cancel_kb())
    elif action == "support":
        await state.set_state(SF.support)
        await ans(rtl(
            f"شناسهٔ پشتیبانی (مثلاً @yourID) را بفرستید.\nفعلی: {sf.support_contact or '—'}"),
            reply_markup=kb.cancel_kb())
    elif action == "preview":
        await state.update_data(sf_preview=True)
        cust = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        await _send_customer_menu(message.answer, sf, cust, preview=True)


# ── CUSTOMER ──────────────────────────────────────────────────────────────────

async def _customer_action(action, message, state, s, sf, customer, bot) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "buy":
        plans = await storefront.list_plans(s, sf.id, only_enabled=True)
        if not plans:
            await ans(rtl("در حال حاضر پلنی برای فروش موجود نیست."))
            return
        await ans(rtl("🛒 یک سرویس را انتخاب کنید:"), reply_markup=kb.buy_plans_kb(plans))
    elif action == "wallet":
        bal = _toman(storefront_wallet.balance(customer))
        lines = [f"👛 کیفِ پولِ شما\n\nموجودی: {bal} تومان"]
        pend = await storefront_wallet.pending_topups_for_customer(s, customer.id)
        if pend:
            lines.append("\n⏳ در انتظارِ تأییدِ مدیر:")
            lines += [f"• #{t.id} — {_toman(t.amount_toman)} تومان" for t in pend]
        await ans(rtl("\n".join(lines)), reply_markup=kb.wallet_kb())
    elif action == "orders":
        orders = (await s.execute(
            select(StorefrontOrder).where(StorefrontOrder.customer_id == customer.id)
            .order_by(StorefrontOrder.id.desc())
        )).scalars().all()
        if not orders:
            await ans(rtl("هنوز سرویسی نخریده‌اید."))
            return
        live = [o for o in orders if o.status == "provisioned"]
        other = [o for o in orders if o.status != "provisioned"]
        if live:
            await ans(rtl("📦 سرویس‌های شما — برای دیدنِ مصرف و لینک، روی هرکدام بزنید:"),
                      reply_markup=kb.orders_kb(live[:20]))
        if other:
            labels = {"pending": "در حالِ پردازش", "failed": "ناموفق"}
            lines = ["⏳ سایر:"]
            lines += [f"• {o.label or 'سرویس'} — {o.gb}گیگ/{o.days}روز — "
                      f"{labels.get(o.status, o.status)}" for o in other[:10]]
            await ans(rtl("\n".join(lines)))
    elif action == "support":
        await ans(rtl(f"💬 پشتیبانی: {sf.support_contact or 'به‌زودی'}"))
    elif action == "trial":
        await _claim_free_trial(message, s, sf, customer, bot)


async def _claim_free_trial(message, s, sf, customer, bot) -> None:  # noqa: ANN001
    """Give this customer their ONE free trial config. Idempotent: the used-flag is set only after a
    successful provision, and re-checked, so a double-tap can't mint two trials."""
    if not sf.free_trial_enabled:
        await message.answer(rtl("تستِ رایگان فعال نیست."))
        return
    if customer.free_trial_used:
        await message.answer(rtl("شما قبلاً تستِ رایگان دریافت کرده‌اید."))
        return
    gb, days = int(sf.free_trial_gb or 1), int(sf.free_trial_days or 1)
    name = "تست رایگان"
    await message.answer(rtl(f"🎁 در حال ساختِ تستِ رایگان ({gb} گیگ · {days} روز)…"))
    order = StorefrontOrder(
        customer_id=customer.id, plan_id=None, label=name, gb=gb, days=days,
        price_toman=0, status="pending")
    s.add(order)
    await s.commit()
    res = await storefront_provision.provision(s, sf, customer, gb=gb, days=days, label=name)
    if res.ok and res.sub_link:
        order.status = "provisioned"
        order.panel_user_uuid = res.uuid
        order.sub_link = res.sub_link
        customer.free_trial_used = True
        await s.commit()
        await _deliver_config(bot, message.from_user.id, gb=gb, days=days, sub_link=res.sub_link, name=name)
    else:
        order.status = "failed"
        await s.commit()
        await message.answer(rtl("❌ ساختِ تستِ رایگان ناموفق بود. لطفاً بعداً دوباره تلاش کنید."))


# ── buy → name → confirm → wallet debit → provision ──────────────────────────

@storefront_router.callback_query(F.data.startswith("sfbuy:"))
async def sf_buy(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Step 1: a plan is tapped. Check the balance, then ask the customer to NAME this service."""
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        plan = await s.get(StorefrontPlan, plan_id)
        if plan is None or plan.storefront_bot_id != sf.id or not plan.enabled:
            await cb.answer("این پلن در دسترس نیست.", show_alert=True)
            return
        if storefront_wallet.balance(customer) < plan.price_toman:
            short = int(plan.price_toman) - int(storefront_wallet.balance(customer))
            await cb.message.answer(rtl(
                f"موجودیِ کیفِ پولِ شما کافی نیست. {_toman(short)} تومان کم دارید.\n"
                "از «👛 کیف پول» کیفِ خود را شارژ کنید."), reply_markup=kb.wallet_kb())
            await cb.answer()
            return
        plan_text = kb.plan_label(plan)
    await state.set_state(SF.buy_name)
    await state.update_data(buy_plan_id=plan_id)
    await cb.message.answer(rtl(
        f"🛒 {plan_text}\n\n"
        "یک نام برای این سرویس بفرستید (مثلاً: گوشی) تا بعداً آن را از هم تشخیص دهید:"),
        reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.buy_name, F.text)
async def sf_buy_name(message: Message, state: FSMContext, bot: Bot) -> None:
    """Step 2: validate the chosen name and show a final confirm card before charging."""
    name = " ".join((message.text or "").split())
    if not name or len(name) > 40:
        await message.answer(rtl("نام نامعتبر است (۱ تا ۴۰ نویسه). دوباره بفرستید."),
                             reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    plan_id = int(data.get("buy_plan_id") or 0)
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            await state.clear()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        plan = await s.get(StorefrontPlan, plan_id)
        if plan is None or plan.storefront_bot_id != sf.id or not plan.enabled:
            await state.clear()
            await message.answer(rtl("این پلن دیگر در دسترس نیست."))
            return
        after = int(storefront_wallet.balance(customer)) - int(plan.price_toman)
        plan_text = kb.plan_label(plan)
    await state.update_data(buy_name=name)
    await message.answer(rtl(
        f"🧾 تأییدِ خرید\n\n{plan_text}\nنام: {name}\n\n"
        f"موجودیِ پس از خرید: {_toman(after)} تومان"),
        reply_markup=kb.buy_confirm_kb())


@storefront_router.callback_query(F.data == "sfbuyok")
async def sf_buy_ok(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Step 3: charge the wallet, create the order, provision the config under the chosen name."""
    data = await state.get_data()
    plan_id = int(data.get("buy_plan_id") or 0)
    name = (data.get("buy_name") or "").strip()
    await state.clear()
    if not plan_id or not name:
        await cb.answer()
        return
    async with SessionLocal() as s:
        sf, reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        plan = await s.get(StorefrontPlan, plan_id)
        if plan is None or plan.storefront_bot_id != sf.id or not plan.enabled:
            await cb.message.answer(rtl("این پلن دیگر در دسترس نیست."))
            await cb.answer()
            return
        ok, _txn = await storefront_wallet.charge_purchase(s, customer.id, plan.price_toman)
        if not ok:
            await cb.message.answer(rtl(
                "موجودیِ کیفِ پولِ شما کافی نیست. ابتدا کیفِ خود را شارژ کنید."),
                reply_markup=kb.wallet_kb())
            await cb.answer()
            return
        order = StorefrontOrder(
            customer_id=customer.id, plan_id=plan.id, label=name[:64], gb=plan.gb, days=plan.days,
            price_toman=plan.price_toman, status="pending")
        s.add(order)
        await s.commit()
        await cb.message.answer(rtl("⏳ در حال ساختِ سرویس…"))
        res = await storefront_provision.provision(
            s, sf, customer, gb=plan.gb, days=plan.days, label=name)
        if res.ok and res.sub_link:
            order.status = "provisioned"
            order.panel_user_uuid = res.uuid
            order.sub_link = res.sub_link
            await s.commit()
            await _deliver_config(
                bot, cb.from_user.id, gb=plan.gb, days=plan.days, sub_link=res.sub_link, name=name)
        else:
            order.status = "failed"
            await storefront_wallet.refund(s, customer.id, plan.price_toman, note=f"provision {res.reason}")
            await s.commit()
            await cb.message.answer(rtl(
                "❌ ساختِ سرویس ناموفق بود؛ مبلغ به کیفِ پولِ شما بازگردانده شد. با پشتیبانی تماس بگیرید."))
            await _notify_admin(bot, reseller,
                                f"⚠️ ساختِ سرویس برای یک مشتری ناموفق بود ({res.reason}). "
                                "احتمالاً ظرفیتِ پنل پُر است.")
    await cb.answer()


# ── my services: live detail ─────────────────────────────────────────────────

@storefront_router.callback_query(F.data.startswith("sforder:"))
async def sf_order_detail(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.customer_id != customer.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        name = order.label or "سرویس"
        lines = [f"📦 {name}", f"پلن: {order.gb} گیگ · {order.days} روز"]
        status = await storefront_provision.live_status(s, sf, order)
        if status.ok:
            lines.append(f"مصرف: {status.used_gb:.2f} از {status.limit_gb:.0f} گیگ")
            if status.remaining_days is not None:
                lines.append(f"روزهای باقی‌مانده: {status.remaining_days}")
        else:
            lines.append("اطلاعاتِ مصرف فعلاً در دسترس نیست.")
        await cb.message.answer(rtl("\n".join(lines)))
        if order.sub_link:
            await _deliver_config(
                bot, cb.from_user.id, gb=order.gb, days=order.days,
                sub_link=order.sub_link, name=name)
    await cb.answer()


# ── top-up flow ───────────────────────────────────────────────────────────────

@storefront_router.callback_query(F.data == "sftopup")
async def sf_topup_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
    if sf is None:
        await cb.answer()
        return
    await state.set_state(SF.topup_amount)
    await cb.message.answer(rtl("مبلغی که می‌خواهید شارژ کنید (تومان) را بفرستید:"),
                            reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.topup_amount, F.text)
async def sf_topup_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("یک مبلغِ معتبر (تومان) بفرستید."), reply_markup=kb.cancel_kb())
        return
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            await state.clear()
            return
    await state.update_data(topup_amount=int(raw))
    await message.answer(rtl("روشِ پرداخت را انتخاب کنید:"), reply_markup=kb.pay_methods_kb(sf))


@storefront_router.callback_query(F.data.startswith("sftop:"))
async def sf_topup_method(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    method = cb.data.split(":")[1]
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
    detail = {
        "card": f"کارت‌به‌کارت\n<code>{sf.card_number}</code>\nبه‌نامِ: {sf.card_holder or '—'}",
        "usdt": f"تتر USDT (شبکهٔ BEP-20)\n<code>{sf.usdt_address}</code>",
        "ton": f"گرام/تون (TON)\n<code>{sf.ton_address}</code>",
    }.get(method, "")
    await state.update_data(topup_method=method)
    await state.set_state(SF.topup_proof)
    await cb.message.answer(rtl(
        f"💳 مبلغِ {_toman(amount)} تومان را به آدرسِ زیر واریز کنید:\n\n{detail}\n\n"
        "سپس رسید (عکس) یا شناسهٔ تراکنش/متنِ واریز را همین‌جا بفرستید."),
        parse_mode="HTML", reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.topup_proof, F.photo | F.text)
async def sf_topup_proof(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    method = data.get("topup_method") or "card"
    await state.clear()
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        proof_path, txid = None, None
        if message.photo:
            import os
            os.makedirs("data/storefront_proofs", exist_ok=True)
            proof_path = f"data/storefront_proofs/sf{sf.id}_c{customer.id}_{message.message_id}.jpg"
            try:
                await bot.download(message.photo[-1], destination=proof_path)
            except Exception:  # noqa: BLE001
                proof_path = None
        else:
            txid = (message.text or "")[:120]
        txn = await storefront_wallet.create_topup(
            s, customer, amount, method=method, proof_path=proof_path, txid=txid)
        await message.answer(rtl(
            f"✅ درخواستِ شارژِ {_toman(amount)} تومان ثبت شد (#{txn.id}). پس از تأییدِ مدیر، "
            "کیفِ پولِ شما شارژ می‌شود."))
        # notify the admin via the same bot (with the proof)
        cap = (f"🧾 شارژِ جدید #{txn.id}\nمبلغ: {_toman(amount)} ت — روش: {method}\n"
               f"مشتری: {customer.name or customer.telegram_id}"
               + (f"\nمتن/TXID: {txid}" if txid else ""))
        if reseller and reseller.bot_chat_id:
            try:
                if proof_path:
                    await bot.send_photo(
                        reseller.bot_chat_id, BufferedInputFile(open(proof_path, "rb").read(), "p.jpg"),
                        caption=rtl(cap), reply_markup=kb.topup_decide_kb(txn.id))
                else:
                    await bot.send_message(reseller.bot_chat_id, rtl(cap),
                                           reply_markup=kb.topup_decide_kb(txn.id))
            except Exception:  # noqa: BLE001
                log.warning("notify admin of topup failed", exc_info=True)


# ── admin: confirm / reject / set-amount ─────────────────────────────────────

@storefront_router.callback_query(F.data.startswith("sfok:"))
async def sf_topup_ok(cb: CallbackQuery, bot: Bot) -> None:
    txn_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        changed, txn = await storefront_wallet.confirm_topup(s, txn_id)
        if not changed:
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        cust = await s.get(StorefrontCustomer, txn.customer_id)
    await cb.message.answer(rtl(f"✅ شارژِ #{txn_id} تأیید و کیفِ پول شارژ شد."))
    if cust:
        try:
            await bot.send_message(cust.telegram_id, rtl(
                f"✅ شارژِ شما تأیید شد. موجودیِ جدید: {_toman(cust.wallet_balance_toman)} تومان."))
        except Exception:  # noqa: BLE001
            pass
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfno:"))
async def sf_topup_no(cb: CallbackQuery, bot: Bot) -> None:
    txn_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        changed, txn = await storefront_wallet.reject_topup(s, txn_id)
        cust = await s.get(StorefrontCustomer, txn.customer_id) if txn else None
    if changed:
        await cb.message.answer(rtl(f"❌ شارژِ #{txn_id} رد شد."))
        if cust:
            try:
                await bot.send_message(cust.telegram_id, rtl(
                    "❌ شارژِ شما تأیید نشد. در صورتِ نیاز با پشتیبانی تماس بگیرید."))
            except Exception:  # noqa: BLE001
                pass
    else:
        await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfokamt:"))
async def sf_topup_ok_amount(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    txn_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.confirm_amount)
    await state.update_data(confirm_txn=txn_id)
    await cb.message.answer(rtl("مبلغی که باید به کیفِ پولِ مشتری اضافه شود (تومان) را بفرستید:"),
                            reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.confirm_amount, F.text)
async def sf_confirm_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    data = await state.get_data()
    txn_id = int(data.get("confirm_txn") or 0)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("مبلغِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.clear()
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if not is_admin:
            return
        changed, txn = await storefront_wallet.confirm_topup(s, txn_id, amount_toman=int(raw))
        cust = await s.get(StorefrontCustomer, txn.customer_id) if txn else None
    if changed:
        await message.answer(rtl(f"✅ شارژِ #{txn_id} با مبلغِ {_toman(raw)} تومان تأیید شد."))
        if cust:
            try:
                await bot.send_message(cust.telegram_id, rtl(
                    f"✅ کیفِ پولِ شما {_toman(raw)} تومان شارژ شد. موجودی: "
                    f"{_toman(cust.wallet_balance_toman)} تومان."))
            except Exception:  # noqa: BLE001
                pass
    else:
        await message.answer(rtl("این تراکنش قبلاً رسیدگی شده است."))


# ── admin: plans CRUD ─────────────────────────────────────────────────────────

@storefront_router.callback_query(F.data == "sfplanadd")
async def sf_plan_add(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    # No title (owner: «عنوان نمی‌خواهیم») — collect volume → days → price only.
    await state.set_state(SF.plan_gb)
    await cb.message.answer(rtl("حجم به گیگابایت (عدد):"), reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.plan_gb, F.text)
async def sf_plan_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.update_data(p_gb=int(raw))
    await state.set_state(SF.plan_days)
    await message.answer(rtl("مدت به روز (عدد):"), reply_markup=kb.cancel_kb())


@storefront_router.message(SF.plan_days, F.text)
async def sf_plan_days(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.update_data(p_days=int(raw))
    await state.set_state(SF.plan_price)
    await message.answer(rtl("قیمت به تومان (عدد):"), reply_markup=kb.cancel_kb())


@storefront_router.message(SF.plan_price, F.text)
async def sf_plan_price(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        await storefront.add_plan(
            s, sf.id, title="", gb=int(data.get("p_gb", 0)),
            days=int(data.get("p_days", 0)), price_toman=int(raw))
        plans = await storefront.list_plans(s, sf.id)
    await message.answer(rtl("✅ پلن اضافه شد."), reply_markup=kb.plans_manage_kb(plans))


@storefront_router.callback_query(F.data.startswith("sfplandel:"))
async def sf_plan_del(cb: CallbackQuery, bot: Bot) -> None:
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await storefront.delete_plan(s, sf.id, plan_id)
        plans = await storefront.list_plans(s, sf.id)
    await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
    await cb.answer("حذف شد.")


# ── admin: payment settings ───────────────────────────────────────────────────

@storefront_router.callback_query(F.data.startswith("sfpaytog:"))
async def sf_pay_toggle(cb: CallbackQuery, bot: Bot) -> None:
    method = cb.data.split(":")[1]
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        attr = {"card": "pay_card_enabled", "usdt": "pay_usdt_enabled", "ton": "pay_ton_enabled"}[method]
        setattr(sf, attr, not getattr(sf, attr))
        await s.commit()
        await cb.message.edit_reply_markup(reply_markup=kb.pay_settings_kb(sf))
    await cb.answer("بروزرسانی شد.")


@storefront_router.callback_query(F.data.startswith("sfpayset:"))
async def sf_pay_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    method = cb.data.split(":")[1]
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.pay_value)
    await state.update_data(pay_method=method, pay_step="card_number" if method == "card" else "addr")
    prompt = {
        "card": "شمارهٔ کارت را بفرستید:",
        "usdt": "آدرسِ کیفِ USDT (BEP-20) را بفرستید:",
        "ton": "آدرسِ کیفِ گرام/تون را بفرستید:",
    }[method]
    await cb.message.answer(rtl(prompt), reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.pay_value, F.text)
async def sf_pay_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    method = data.get("pay_method")
    step = data.get("pay_step")
    val = (message.text or "").strip()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            await state.clear()
            return
        if method == "card" and step == "card_number":
            sf.card_number = val[:32]
            await s.commit()
            await state.update_data(pay_step="card_holder")
            await message.answer(rtl("نامِ صاحبِ کارت را بفرستید:"), reply_markup=kb.cancel_kb())
            return
        if method == "card":
            sf.card_holder = val[:128]
        elif method == "usdt":
            sf.usdt_address = val[:128]
        elif method == "ton":
            sf.ton_address = val[:128]
        await s.commit()
        await state.clear()
        await message.answer(rtl("✅ ذخیره شد."), reply_markup=kb.pay_settings_kb(sf))


# ── admin: free-trial settings ────────────────────────────────────────────────

@storefront_router.callback_query(F.data == "sftrialtog")
async def sf_trial_toggle(cb: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        sf.free_trial_enabled = not sf.free_trial_enabled
        await s.commit()
        await cb.message.edit_reply_markup(reply_markup=kb.trial_settings_kb(sf))
    await cb.answer("فعال شد." if sf.free_trial_enabled else "غیرفعال شد.")


@storefront_router.callback_query(F.data == "sftrialset")
async def sf_trial_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.trial_gb)
    await cb.message.answer(rtl("حجمِ تستِ رایگان به گیگابایت (عدد):"), reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.trial_gb, F.text)
async def sf_trial_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.update_data(t_gb=int(raw))
    await state.set_state(SF.trial_days)
    await message.answer(rtl("مدتِ تستِ رایگان به روز (عدد):"), reply_markup=kb.cancel_kb())


@storefront_router.message(SF.trial_days, F.text)
async def sf_trial_days(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        sf.free_trial_gb = int(data.get("t_gb", 1))
        sf.free_trial_days = int(raw)
        await s.commit()
        await message.answer(
            rtl(f"✅ تستِ رایگان: {sf.free_trial_gb} گیگ · {sf.free_trial_days} روز"),
            reply_markup=kb.trial_settings_kb(sf))


# ── admin: manual wallet adjust, support, broadcast ──────────────────────────

@storefront_router.callback_query(F.data.startswith("sfadj:"))
async def sf_adjust_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, cid, sign = cb.data.split(":")
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.adjust_amount)
    await state.update_data(adj_customer=int(cid), adj_sign=sign)
    await cb.message.answer(rtl(f"مبلغِ {'افزایش' if sign == '+' else 'کاهش'} (تومان) را بفرستید:"),
                            reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.adjust_amount, F.text)
async def sf_adjust_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    data = await state.get_data()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("مبلغِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.clear()
    signed = int(raw) * (1 if data.get("adj_sign") == "+" else -1)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        cust = await s.get(StorefrontCustomer, int(data.get("adj_customer") or 0))
        if cust is None or cust.storefront_bot_id != sf.id:
            await message.answer(rtl("مشتری پیدا نشد."))
            return
        await storefront_wallet.manual_adjust(s, cust, signed, note="admin")
        await message.answer(rtl(
            f"✅ موجودیِ «{cust.name or cust.telegram_id}» اکنون {_toman(cust.wallet_balance_toman)} تومان است."))


@storefront_router.message(SF.support, F.text)
async def sf_support_set(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        sf.support_contact = (message.text or "").strip()[:128]
        await s.commit()
    await message.answer(rtl("✅ شناسهٔ پشتیبانی ذخیره شد."), reply_markup=kb.admin_reply_kb())


@storefront_router.message(SF.broadcast, F.text)
async def sf_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    text = message.text or ""
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        custs = await storefront.list_customers(s, sf.id)
    sent = 0
    for c in custs:
        try:
            await bot.send_message(c.telegram_id, rtl(text))
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    await message.answer(rtl(f"📢 به {sent} مشتری ارسال شد."), reply_markup=kb.admin_reply_kb())


# ── generic cancel ────────────────────────────────────────────────────────────

@storefront_router.callback_query(F.data == "sfcancel")
async def sf_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        if is_admin:
            await _send_admin_menu(cb.message.answer, sf)
        else:
            cust = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
            await _send_customer_menu(cb.message.answer, sf, cust)
    await cb.answer("لغو شد.")


@storefront_router.callback_query(F.data == "sfnoop")
async def sf_noop(cb: CallbackQuery) -> None:
    await cb.answer()
