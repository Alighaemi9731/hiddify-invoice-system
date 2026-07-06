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

# Reuse the main bot's generic membership primitives: `_is_member` (restricted-safe, fail-closed) and
# `_join_link` (per-user one-time invite link, falls back to a static link). Safe — app.bot.handlers
# does not import the storefront at module load.
from app.bot.handlers import _is_member, _join_link
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
from app.services import (
    settings_service,
    storefront,
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
    usercreate,
)

log = logging.getLogger("bot.storefront")

_PER_PAGE = 8          # customers per page in the admin «مشتری‌ها» list
_SEARCH_LIMIT = 20     # max matches shown for a customer search (refine if more)
_TRIAL_NO_RENEW = "⛔️ سرویسِ تستِ رایگان قابلِ تمدید نیست؛ برای ادامه لطفاً یک پلن تهیه کنید."

storefront_router = Router()
storefront_router.message.filter(F.chat.type == "private")


class SF(StatesGroup):
    plan_gb = State()
    plan_days = State()
    plan_price = State()
    edit_gb = State()          # edit an existing plan; data: edit_plan_id
    edit_days = State()        # data: edit_plan_id, e_gb
    edit_price = State()       # data: edit_plan_id, e_gb, e_days
    pay_value = State()        # data: method (+ card sub-step)
    trial_gb = State()         # admin sets free-trial volume
    trial_days = State()       # admin sets free-trial duration; data: t_gb
    buy_name = State()         # customer names the config; data: buy_plan_id
    topup_amount = State()     # data: nothing yet
    topup_proof = State()      # data: amount, method
    confirm_amount = State()   # admin sets credited Toman; data: txn_id
    adjust_amount = State()    # admin manual wallet edit; data: customer_id, sign
    support = State()
    welcome = State()          # admin sets the storefront welcome text
    join_channel = State()     # admin sets the forced-join channel (forward a post / send @id)
    cust_search = State()      # admin searches customers by name / telegram id
    broadcast = State()
    add_admin = State()        # admin appoints a co-admin (numeric id or forwarded message)


# ── helpers ───────────────────────────────────────────────────────────────────

def _digits(text: str | None) -> str:
    return (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬,", "0123456789  ")).replace(" ", "")


def _toman(value) -> str:  # noqa: ANN001
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _usage_line(used_gb: float, limit_gb: float, plan_gb: int) -> str:
    """The «مصرف» line for a config. Renewal ADDS quota (a renewed 10-گیگ plan shows a 20-گیگ
    limit), which reads confusingly next to «پلن: ۱۰ گیگ» — so when the live limit exceeds the
    plan size, label it «(شاملِ تمدید)» to make clear the total allowance includes renewals."""
    base = f"مصرف: {used_gb:.2f} از {limit_gb:.0f} گیگ"
    return f"{base} (شاملِ تمدید)" if limit_gb > float(plan_gb) + 0.5 else base


async def _resolve(session, bot: Bot, user) -> tuple[StorefrontBot | None, Reseller | None, bool]:  # noqa: ANN001
    """Resolve the tenant for the physical bot, plus whether `user` is the reseller-admin."""
    sf = await storefront.get_bot_by_telegram_id(session, bot.id)
    if sf is None:
        return None, None, False
    reseller = await session.get(Reseller, sf.reseller_id)
    # The owning reseller OR any appointed co-admin may manage the shop.
    is_admin = storefront.is_shop_admin(sf, reseller, user.id)
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


def _admin_chat_ids(reseller: Reseller | None, sf: StorefrontBot | None) -> list[int]:
    """Every Telegram id that should receive admin messages for this shop: the owning reseller plus
    any appointed co-admins (deduped, order-preserving). Co-admins get EXACTLY what the owner gets."""
    ids: list[int] = []
    if reseller is not None and reseller.bot_chat_id:
        ids.append(reseller.bot_chat_id)
    if sf is not None:
        for tid in storefront.co_admin_ids(sf):
            if tid not in ids:
                ids.append(tid)
    return ids


async def _notify_admin(  # noqa: ANN003
    bot: Bot, reseller: Reseller | None, text: str, *, sf: StorefrontBot | None = None, **kw
) -> None:
    """Send an admin notification to the owner AND every co-admin (pass `sf` to include co-admins)."""
    for chat_id in _admin_chat_ids(reseller, sf):
        try:
            await bot.send_message(chat_id, rtl(text), **kw)
        except Exception:  # noqa: BLE001 — one blocked admin shouldn't stop the others
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


# ── banned gate (every callback + non-/start message) ────────────────────────

_BAN_EXEMPT_COMMANDS = {"/start", "/cancel"}


async def _is_banned(bot: Bot, user) -> bool:  # noqa: ANN001
    """A non-admin customer flagged banned. Fails OPEN on any error (a transient DB blip must not lock
    everyone out; ban is a rare, deliberate action)."""
    try:
        async with SessionLocal() as s:
            sf, _r, is_admin = await _resolve(s, bot, user)
            if sf is None or is_admin:
                return False
            customer = await storefront.get_or_create_customer(s, sf.id, user)
            return bool(customer.banned)
    except Exception:  # noqa: BLE001
        log.warning("storefront ban check failed", exc_info=True)
        return False


@storefront_router.callback_query.outer_middleware
async def _sf_ban_cb_mw(handler, event, data):  # noqa: ANN001, ANN202
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None and await _is_banned(bot, user):
        await event.answer("دسترسیِ شما مسدود شده است.", show_alert=True)
        return None
    return await handler(event, data)


@storefront_router.message.outer_middleware
async def _sf_ban_msg_mw(handler, event, data):  # noqa: ANN001, ANN202
    cmd = (getattr(event, "text", None) or "").strip().split()[0].lower() if getattr(
        event, "text", None) else ""
    if cmd in _BAN_EXEMPT_COMMANDS:  # /start shows the banned notice itself; /cancel always allowed
        return await handler(event, data)
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None and await _is_banned(bot, user):
        await event.answer(rtl("دسترسیِ شما مسدود شده است."))
        return None
    return await handler(event, data)


# ── forced-join gate (customer must be a member of the reseller's channel) ────

_JOIN_EXEMPT_CALLBACKS = {"sfjoincheck", "sfcancel"}


async def _channel_block(bot: Bot, user) -> dict | None:  # noqa: ANN001
    """If this NON-admin customer must join the storefront's channel and isn't a member yet, return
    {id, link}; else None. Admins are never gated. Membership uses `_is_member` (fails closed on a
    Telegram error → blocked until the admin fixes the channel/bot-admin); resolution errors fail OPEN."""
    try:
        async with SessionLocal() as s:
            sf, _r, is_admin = await _resolve(s, bot, user)
            if sf is None or is_admin or not sf.channel_required or not sf.channel_id:
                return None
            link = sf.channel_link
            chan = sf.channel_id
        if await _is_member(bot, chan, user.id):
            return None
        return {"id": chan, "link": link}
    except Exception:  # noqa: BLE001 — a resolution blip must not lock everyone out
        log.warning("storefront channel gate check failed", exc_info=True)
        return None


async def _send_join_prompt(bot: Bot, chat_id: int, block: dict) -> None:  # noqa: ANN001
    link = await _join_link(bot, block["id"], block.get("link") or "", True)
    await bot.send_message(
        chat_id,
        rtl("برای استفاده از فروشگاه، ابتدا باید در کانالِ ما عضو شوید، سپس «بررسی عضویت» را بزنید."),
        reply_markup=kb.join_prompt_kb(link))


@storefront_router.callback_query.outer_middleware
async def _sf_join_cb_mw(handler, event, data):  # noqa: ANN001, ANN202
    cb_data = getattr(event, "data", "") or ""
    if cb_data in _JOIN_EXEMPT_CALLBACKS:
        return await handler(event, data)
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None and await _channel_block(bot, user) is not None:
        await event.answer("ابتدا در کانال عضو شوید و سپس /start را بزنید.", show_alert=True)
        return None
    return await handler(event, data)


@storefront_router.message.outer_middleware
async def _sf_join_msg_mw(handler, event, data):  # noqa: ANN001, ANN202
    cmd = (getattr(event, "text", None) or "").strip().split()[0].lower() if getattr(
        event, "text", None) else ""
    if cmd in _BAN_EXEMPT_COMMANDS:  # /start shows the join prompt itself; /cancel always allowed
        return await handler(event, data)
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None:
        block = await _channel_block(bot, user)
        if block is not None:
            await _send_join_prompt(bot, user.id, block)
            return None
    return await handler(event, data)


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
    block = await _channel_block(bot, message.from_user)   # forced-join gate (outside the session)
    if block is not None:
        await _send_join_prompt(bot, message.from_user.id, block)
        return
    async with SessionLocal() as s:
        sf = await storefront.get_bot_by_telegram_id(s, bot.id)
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        await _send_customer_menu(message.answer, sf, customer)


@storefront_router.message(F.text.in_(kb.ALL_LABELS))
async def sf_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    trial_ids: tuple[int, int] | None = None
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
            action = kb.CUSTOMER_LABEL_TO_ACTION[text]
            if action == "trial":  # heavy (panel I/O) → run it WITHOUT holding the menu's connection
                trial_ids = (sf.id, customer.id)
            else:
                await _customer_action(action, message, state, s, sf, customer, bot)
    if trial_ids is not None:
        await _claim_free_trial(message, trial_ids[0], trial_ids[1], bot)


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
        rows, total = await storefront.list_customers_page(s, sf.id, offset=0, limit=_PER_PAGE)
        if total == 0:
            await ans(rtl("هنوز مشتری‌ای ندارید."))
            return
        await ans(rtl(f"👥 مشتری‌ها ({total})"),
                  reply_markup=kb.customers_page_kb(rows, page=0, per_page=_PER_PAGE, total=total))
    elif action == "stats":
        st_ = await storefront.stats_for_bot(s, sf.id)
        expiring = f"  (⏳ نزدیک به انقضا: {st_.expiring_soon})" if st_.expiring_soon else ""
        await ans(rtl(
            "📊 آمار فروشگاه\n\n"
            f"👥 مشتری‌ها: {st_.customers}  (فعال در ۳۰ روز اخیر: {st_.active_30d})\n"
            f"📦 پلن‌ها: {st_.plans_enabled} فعال از {st_.plans_total}\n"
            f"🟢 سرویس‌های فعال: {st_.provisioned}{expiring}\n\n"
            f"💰 فروش این ماه: {st_.sales_month_toman:,.0f} تومان ({st_.sales_month_count} خرید)\n"
            f"💳 شارژ تأییدشدهٔ این ماه: {st_.topups_month_toman:,.0f} تومان\n"
            f"⏸ شارژِ در انتظار تأیید: {st_.pending_topups}\n"
            f"🏦 موجودی کیف پول مشتری‌ها: {st_.wallet_liability_toman:,.0f} تومان"))
    elif action == "broadcast":
        await state.set_state(SF.broadcast)
        await ans(rtl("متنِ پیامِ همگانی به مشتری‌ها را بفرستید:"), reply_markup=kb.cancel_kb())
    elif action == "support":
        await state.set_state(SF.support)
        await ans(rtl(
            f"شناسهٔ پشتیبانی (مثلاً @yourID) را بفرستید.\nفعلی: {sf.support_contact or '—'}"),
            reply_markup=kb.cancel_kb())
    elif action == "welcome":
        await state.set_state(SF.welcome)
        await ans(rtl(
            "متنِ خوش‌آمدگوییِ فروشگاه را بفرستید (به مشتری در شروع نشان داده می‌شود).\n"
            f"فعلی: {sf.welcome_text or '🛍 به فروشگاه خوش آمدید!'}"),
            reply_markup=kb.cancel_kb())
    elif action == "joincfg":
        cur = sf.channel_id or "—"
        await ans(rtl(
            f"🔒 عضویت اجباری در کانال\n"
            f"وضعیت: {'فعال ✅' if sf.channel_required else 'غیرفعال ❌'}\nکانال: {cur}\n\n"
            "برای تنظیم، ابتدا ربات (@" + (sf.bot_username or "—") + ") را در کانالِ خود ادمین کنید، "
            "سپس «✏️ تنظیم/تغییرِ کانال» را بزنید."),
            reply_markup=kb.join_settings_kb(sf))
    elif action == "admins":
        if _is_shop_owner(reseller, message.from_user.id):
            await _send_admins_panel(ans, s, sf, reseller)
        else:
            await ans(rtl("فقط مدیرِ اصلیِ فروشگاه می‌تواند مدیران را مدیریت کند."))
    elif action == "preview":
        await state.update_data(sf_preview=True)
        cust = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        await _send_customer_menu(message.answer, sf, cust, preview=True)


# ── admin: co-admins (extra managers) ────────────────────────────────────────

def _is_shop_owner(reseller, user_id: int) -> bool:  # noqa: ANN001
    """Only the OWNING reseller manages the admin list (a co-admin can run the shop but not appoint
    or remove admins, and can't lock the owner out)."""
    return reseller is not None and reseller.bot_chat_id == user_id


async def _send_admins_panel(ans, s, sf, reseller) -> None:  # noqa: ANN001
    owner_id = reseller.bot_chat_id if reseller else None
    co_ids = storefront.co_admin_ids(sf)
    lines = ["🛡 مدیرانِ ربات", "", f"👑 مدیرِ اصلی: {owner_id or '—'} (شما؛ غیرقابلِ حذف)"]
    if co_ids:
        lines += ["", "مدیرانِ اضافه:"] + [f"• {tid}" for tid in co_ids]
    else:
        lines += ["", "هنوز مدیرِ اضافه‌ای ندارید."]
    lines += ["", "«➕ افزودن مدیر» را بزنید تا یک آیدیِ دیگر هم بتواند این فروشگاه را مدیریت کند."]
    await ans(rtl("\n".join(lines)), reply_markup=kb.admins_manage_kb(co_ids))


@storefront_router.callback_query(F.data == "sfaddadmin")
async def sf_add_admin_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, cb.from_user)
    if sf is None or not _is_shop_owner(reseller, cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی می‌تواند مدیر اضافه کند.", show_alert=True)
        return
    await state.set_state(SF.add_admin)
    await cb.message.answer(rtl(
        "👤 آیدیِ عددیِ تلگرامِ فرد را بفرستید،\n"
        "یا یک پیام از او را برای همین ربات فوروارد کنید.\n\n"
        "نکته: آن فرد باید حداقل یک‌بار ربات را /start کرده باشد تا بتواند وارد پنلِ مدیریت شود."),
        reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.add_admin)
async def sf_add_admin_set(message: Message, state: FSMContext, bot: Bot) -> None:
    # Resolve the target Telegram id from a forwarded message (privacy permitting) or a typed id.
    target_id: int | None = None
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None)
    if sender is not None and getattr(sender, "id", None):
        target_id = int(sender.id)
    elif getattr(message, "forward_from", None):  # legacy forward field
        target_id = int(message.forward_from.id)
    elif message.text:
        t = _digits(message.text)
        if t.lstrip("-").isdigit():
            target_id = int(t)
    if not target_id or target_id <= 0:
        await message.answer(rtl(
            "نشد. یک آیدیِ عددیِ معتبر بفرستید، یا پیامی از آن فرد را فوروارد کنید.\n"
            "(اگر فوروارد اثری نداشت، آن فرد در تنظیماتِ حریمِ خصوصیِ تلگرام فوروارد را بسته است؛ "
            "آیدیِ عددی‌اش را دستی بفرستید.)"),
            reply_markup=kb.cancel_kb())
        return
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, message.from_user)
        if sf is None or not _is_shop_owner(reseller, message.from_user.id):
            await state.clear()
            await message.answer(rtl("فقط مدیرِ اصلی می‌تواند مدیر اضافه کند."),
                                 reply_markup=kb.admin_reply_kb())
            return
        result = await storefront.add_co_admin(s, sf, target_id)
        await state.clear()
        note = {
            "ok": f"✅ آیدیِ {target_id} به‌عنوان مدیرِ فروشگاه اضافه شد.",
            "exists": f"این آیدی ({target_id}) از قبل مدیر است.",
            "is_owner": "این آیدیِ خودِ شماست؛ شما مدیرِ اصلی هستید.",
            "full": f"حداکثر {storefront.MAX_CO_ADMINS} مدیرِ اضافه مجاز است.",
        }.get(result, "خطا در افزودنِ مدیر.")
        await message.answer(rtl(note), reply_markup=kb.admin_reply_kb())
        await _send_admins_panel(message.answer, s, sf, reseller)
    if result == "ok":
        # Best-effort: let the new admin know (only works if they've started the bot).
        try:
            await bot.send_message(target_id, rtl(
                f"🛡 شما به‌عنوان مدیرِ فروشگاهِ @{sf.bot_username or ''} انتخاب شدید.\n"
                "برای ورود به پنلِ مدیریت، /start را بزنید."))
        except Exception:  # noqa: BLE001 — they may not have started the bot yet
            pass


@storefront_router.callback_query(F.data.startswith("sfdeladm:"))
async def sf_del_admin(cb: CallbackQuery, bot: Bot) -> None:
    try:
        tid = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, cb.from_user)
        if sf is None or not _is_shop_owner(reseller, cb.from_user.id):
            await cb.answer("فقط مدیرِ اصلی می‌تواند مدیر حذف کند.", show_alert=True)
            return
        removed = await storefront.remove_co_admin(s, sf, tid)
        co_ids = storefront.co_admin_ids(sf)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.admins_manage_kb(co_ids))
    except Exception:  # noqa: BLE001 — markup unchanged / message too old
        pass
    await cb.answer("حذف شد." if removed else "یافت نشد.")


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


async def _claim_free_trial(message, sf_id: int, customer_id: int, bot: Bot) -> None:  # noqa: ANN001
    """Claim the one-time free trial via the concurrency-safe service (compare-and-set + atomic
    provision), then deliver the config. No DB connection is held across the panel/Telegram I/O."""
    await message.answer(rtl("🎁 در حال ساختِ تستِ رایگان…"))
    res = await storefront_provision.claim_trial(SessionLocal, sf_id=sf_id, customer_id=customer_id)
    if res.ok and res.sub_link:
        await _deliver_config(bot, message.from_user.id, gb=res.gb, days=res.days,
                              sub_link=res.sub_link, name=res.label)
    elif res.reason == "used":
        await message.answer(rtl("شما قبلاً تستِ رایگان دریافت کرده‌اید."))
    elif res.reason == "disabled":
        await message.answer(rtl("تستِ رایگان فعلاً فعال نیست."))
    else:
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
    """Step 3: hand off to the atomic, crash-safe purchase service (charge + provision in short txns,
    no DB connection held across the panel/Telegram I/O), then deliver or report."""
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
        sf_id, customer_id, reseller_id = sf.id, customer.id, sf.reseller_id
    await cb.answer()
    await cb.message.answer(rtl("⏳ در حال ساختِ سرویس…"))
    res = await storefront_provision.purchase(
        SessionLocal, sf_id=sf_id, customer_id=customer_id, plan_id=plan_id, label=name)
    if res.ok and res.sub_link:
        await _deliver_config(bot, cb.from_user.id, gb=res.gb, days=res.days,
                              sub_link=res.sub_link, name=res.label)
        return
    if res.reason == "insufficient":
        await cb.message.answer(rtl(
            f"موجودیِ کیفِ پولِ شما کافی نیست. {_toman(res.short_toman)} تومان کم دارید."),
            reply_markup=kb.wallet_kb())
        return
    if res.reason == "plan_gone":
        await cb.message.answer(rtl("این پلن دیگر در دسترس نیست."))
        return
    # provisioning failed → the service already refunded; tell the customer + nudge the admin
    await cb.message.answer(rtl(
        "❌ ساختِ سرویس ناموفق بود؛ مبلغ به کیفِ پولِ شما بازگردانده شد. با پشتیبانی تماس بگیرید."))
    async with SessionLocal() as s:
        reseller = await s.get(Reseller, reseller_id)
        sf = await storefront.get_bot_for_reseller(s, reseller_id)
        if reseller is not None:
            await _notify_admin(bot, reseller,
                                f"⚠️ ساختِ سرویس برای یک مشتری ناموفق بود ({res.reason}). "
                                "احتمالاً ظرفیتِ پنل پُر است.", sf=sf)
    await cb.answer()


# ── my services: live detail ─────────────────────────────────────────────────

async def _owned_order(s, sf, user, order_id: int):  # noqa: ANN001, ANN202
    """Return the order iff it belongs to THIS customer of THIS storefront, else None."""
    customer = await storefront.get_or_create_customer(s, sf.id, user)
    order = await s.get(StorefrontOrder, order_id)
    if order is None or order.customer_id != customer.id:
        return None
    return order


@storefront_router.callback_query(F.data.startswith("sforder:"))
async def sf_order_detail(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        order = await _owned_order(s, sf, cb.from_user, order_id)
        if order is None or order.status in ("deleted", "failed"):
            await cb.answer("یافت نشد.", show_alert=True)
            return
        name, gb, days, sub_link, paused = (
            order.label or "سرویس", order.gb, order.days, order.sub_link, order.status == "disabled")
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        renew_price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        status = await storefront_provision.live_status(s, sf, order)
    lines = [f"📦 {name}", f"پلن: {gb} گیگ · {days} روز"]
    if paused:
        lines.append("وضعیت: متوقف ⏸")
    if status.ok:
        lines.append(_usage_line(status.used_gb, status.limit_gb, gb))
        if status.remaining_days is not None:
            lines.append(f"روزهای باقی‌مانده: {status.remaining_days}")
    else:
        lines.append("اطلاعاتِ مصرف فعلاً در دسترس نیست.")
    if sub_link:
        lines += ["", "🔗 لینکِ اشتراک:", f"<code>{sub_link}</code>"]
    caption = rtl("\n".join(lines))
    markup = kb.order_actions_kb(order_id, renew_price, paused=paused, is_trial=order.is_trial)
    # ONE message: the QR photo carries the status + link + action buttons (no separate "آماده شد" send).
    sent = False
    if sub_link:
        try:
            png = usercreate.qr_png(sub_link)
            await bot.send_photo(cb.from_user.id, BufferedInputFile(png, filename="sub.png"),
                                 caption=caption, parse_mode="HTML", reply_markup=markup)
            sent = True
        except Exception:  # noqa: BLE001 — fall back to a single text message below
            sent = False
    if not sent:
        await cb.message.answer(caption, parse_mode="HTML", reply_markup=markup,
                                disable_web_page_preview=True)
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfrenew:"))
async def sf_renew(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        order = await _owned_order(s, sf, cb.from_user, order_id)
        if order is None or order.status not in ("provisioned", "disabled"):
            await cb.answer("یافت نشد.", show_alert=True)
            return
        if order.is_trial:
            await cb.answer()
            await cb.message.answer(rtl(_TRIAL_NO_RENEW))
            return
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        gb = int(plan.gb) if plan else int(order.gb)
        days = int(plan.days) if plan else int(order.days)
    await cb.message.answer(
        rtl(f"🔄 تمدیدِ «{order.label or 'سرویس'}» — {gb} گیگ · {days} روز\n"
            f"مبلغ: {_toman(price)} تومان از کیفِ پول کسر می‌شود."),
        reply_markup=kb.confirm_kb(rtl("✅ تمدید"), f"sfrenewok:{order_id}"))
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfrenewok:"))
async def sf_renew_ok(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None or await _owned_order(s, sf, cb.from_user, order_id) is None:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await cb.answer()
    await cb.message.answer(rtl("⏳ در حال تمدید…"))
    res = await storefront_subscription.renew(SessionLocal, order_id=order_id, by_admin=False)
    if res.ok:
        await cb.message.answer(rtl(
            f"✅ تمدید شد — {res.gb} گیگ · {res.days} روز. لینکِ شما تغییری نکرده است."))
    elif res.reason == "insufficient":
        await cb.message.answer(
            rtl(f"موجودی کافی نیست. {_toman(res.short_toman)} تومان کم دارید."),
            reply_markup=kb.wallet_kb())
    elif res.reason == "trial":
        await cb.message.answer(rtl(_TRIAL_NO_RENEW))
    else:
        await cb.message.answer(rtl("❌ تمدید ناموفق بود. با پشتیبانی تماس بگیرید."))


@storefront_router.callback_query(F.data.startswith("sftgl:"))
async def sf_toggle(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        order = await _owned_order(s, sf, cb.from_user, order_id)
        if order is None or order.status not in ("provisioned", "disabled"):
            await cb.answer("یافت نشد.", show_alert=True)
            return
        enable = order.status == "disabled"
    res = await storefront_subscription.set_enabled(SessionLocal, order_id=order_id, enabled=enable)
    if res.ok:
        await cb.answer("فعال شد." if enable else "متوقف شد.", show_alert=False)
    else:
        await cb.answer("ناموفق بود.", show_alert=True)


@storefront_router.callback_query(F.data.startswith("sfdel:"))
async def sf_delete(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None or await _owned_order(s, sf, cb.from_user, order_id) is None:
            await cb.answer("یافت نشد.", show_alert=True)
            return
    await cb.message.answer(
        rtl("🗑 از حذفِ این سرویس مطمئن هستید؟ کانفیگ از پنل پاک می‌شود و وجهی بازنمی‌گردد."),
        reply_markup=kb.confirm_kb(rtl("🗑 بله، حذف کن"), f"sfdelok:{order_id}"))
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfdelok:"))
async def sf_delete_ok(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None or await _owned_order(s, sf, cb.from_user, order_id) is None:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await cb.answer()
    res = await storefront_subscription.delete_subscription(SessionLocal, order_id=order_id)
    await cb.message.answer(rtl("✅ سرویس حذف شد." if res.ok else "❌ حذف ناموفق بود."))


# ── admin: a customer's subscriptions (renew / pause / delete) ───────────────

async def _admin_order(s, sf, order_id: int):  # noqa: ANN001, ANN202
    """Return the order iff its customer belongs to THIS admin's storefront, else None."""
    order = await s.get(StorefrontOrder, order_id)
    if order is None:
        return None
    cust = await s.get(StorefrontCustomer, order.customer_id)
    if cust is None or cust.storefront_bot_id != sf.id:
        return None
    return order


# ── admin: customers list (paginated + searchable) ───────────────────────────

@storefront_router.callback_query(F.data.startswith("sfcustpg:"))
async def sf_customers_page(cb: CallbackQuery, bot: Bot) -> None:
    page = max(0, int(cb.data.split(":")[1]))
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        rows, total = await storefront.list_customers_page(
            s, sf.id, offset=page * _PER_PAGE, limit=_PER_PAGE)
    text = rtl(f"👥 مشتری‌ها ({total})")
    markup = kb.customers_page_kb(rows, page=page, per_page=_PER_PAGE, total=total)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — message too old / unchanged → send a fresh one
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfcust:"))
async def sf_customer_detail(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        cust = await s.get(StorefrontCustomer, cid)
        if cust is None or cust.storefront_bot_id != sf.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        name, tgid, bal, banned = (cust.name or "—", cust.telegram_id,
                                   _toman(cust.wallet_balance_toman), cust.banned)
    lines = [f"👤 {name}", f"🆔 {tgid}", f"👛 موجودی: {bal} تومان"]
    if banned:
        lines.append("⛔️ مسدود")
    text = rtl("\n".join(lines))
    markup = kb.customer_detail_kb(cid)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@storefront_router.callback_query(F.data == "sfcustsearch")
async def sf_customers_search_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.cust_search)
    await cb.message.answer(rtl("نام یا آیدیِ عددیِ مشتری را بفرستید:"), reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.cust_search, F.text)
async def sf_customers_search(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    q = (message.text or "").strip()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        rows, total = await storefront.list_customers_page(
            s, sf.id, offset=0, limit=_SEARCH_LIMIT, query=q)
    if total == 0:
        await message.answer(rtl("مشتری‌ای یافت نشد."), reply_markup=kb.admin_reply_kb())
        return
    header = f"🔍 نتایج ({total})"
    if total > _SEARCH_LIMIT:
        header += " — نتایج زیاد است؛ دقیق‌تر جستجو کنید."
    await message.answer(
        rtl(header),
        reply_markup=kb.customers_page_kb(rows, page=0, per_page=_SEARCH_LIMIT, total=total,
                                          searching=True))


@storefront_router.callback_query(F.data.startswith("sfacust:"))
async def sf_admin_customer_subs(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        cust = await s.get(StorefrontCustomer, cid)
        if cust is None or cust.storefront_bot_id != sf.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        orders = (await s.execute(
            select(StorefrontOrder).where(
                StorefrontOrder.customer_id == cid,
                StorefrontOrder.status.in_(("provisioned", "disabled")),
            ).order_by(StorefrontOrder.id.desc())
        )).scalars().all()
        title = cust.name or str(cust.telegram_id)
    if not orders:
        await cb.message.answer(rtl(f"«{title}» سرویسِ فعالی ندارد."))
        await cb.answer()
        return
    await cb.message.answer(rtl(f"📦 سرویس‌های «{title}» — برای مدیریت روی هرکدام بزنید:"),
                            reply_markup=kb.admin_subs_kb(list(orders)))
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfasub:"))
async def sf_admin_sub_detail(cb: CallbackQuery, bot: Bot) -> None:
    oid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        order = await _admin_order(s, sf, oid)
        if order is None:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        name, gb, days, paused = (
            order.label or "سرویس", order.gb, order.days, order.status == "disabled")
        live = await storefront_provision.live_status(s, sf, order)
    lines = [f"📦 {name}", f"پلن: {gb} گیگ · {days} روز"]
    if paused:
        lines.append("وضعیت: متوقف ⏸")
    if live.ok:
        lines.append(_usage_line(live.used_gb, live.limit_gb, gb))
        if live.remaining_days is not None:
            lines.append(f"روزهای باقی‌مانده: {live.remaining_days}")
    await cb.message.answer(rtl("\n".join(lines)),
                            reply_markup=kb.admin_sub_actions_kb(oid, paused=paused,
                                                                is_trial=order.is_trial))
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfarenew:"))
async def sf_admin_renew(cb: CallbackQuery, bot: Bot) -> None:
    oid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        order = await _admin_order(s, sf, oid) if (sf and is_admin) else None
        if sf is None or not is_admin or order is None:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if order.is_trial:
            await cb.answer()
            await cb.message.answer(rtl(_TRIAL_NO_RENEW))
            return
    await cb.answer()
    res = await storefront_subscription.renew(SessionLocal, order_id=oid, by_admin=True)
    await cb.message.answer(
        rtl(f"✅ تمدید شد — {res.gb} گیگ · {res.days} روز." if res.ok else "❌ تمدید ناموفق بود."))


@storefront_router.callback_query(F.data.startswith("sfatgl:"))
async def sf_admin_toggle(cb: CallbackQuery, bot: Bot) -> None:
    oid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        order = await _admin_order(s, sf, oid)
        if order is None:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        enable = order.status == "disabled"
    res = await storefront_subscription.set_enabled(SessionLocal, order_id=oid, enabled=enable)
    await cb.answer(("فعال شد." if enable else "متوقف شد.") if res.ok else "ناموفق بود.",
                    show_alert=not res.ok)


@storefront_router.callback_query(F.data.startswith("sfadel:"))
async def sf_admin_delete(cb: CallbackQuery, bot: Bot) -> None:
    oid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin or await _admin_order(s, sf, oid) is None:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await cb.answer()
    res = await storefront_subscription.delete_subscription(SessionLocal, order_id=oid)
    await cb.message.answer(rtl("✅ سرویس حذف شد." if res.ok else "❌ حذف ناموفق بود."))


# ── top-up flow ───────────────────────────────────────────────────────────────

@storefront_router.callback_query(F.data == "sftopup")
async def sf_topup_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        pending = await storefront_wallet.pending_topups_for_customer(s, customer.id)
        cap = int(await settings_service.get(s, "storefront_max_pending_topups", 3) or 3)
    if len(pending) >= cap:  # anti-spam: don't pile up unreviewed top-ups
        await cb.message.answer(rtl(
            f"شما {len(pending)} شارژِ در انتظارِ تأیید دارید؛ تا بررسیِ آن‌ها صبر کنید."))
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
        # Send the proof + decide buttons to the owner AND every co-admin (whoever acts first
        # settles it; the confirm/reject is idempotent so a second tap can't double-credit).
        for admin_id in _admin_chat_ids(reseller, sf):
            try:
                if message.photo:  # forward by file_id — no disk read, no leaked file handle
                    await bot.send_photo(
                        admin_id, message.photo[-1].file_id,
                        caption=rtl(cap), reply_markup=kb.topup_decide_kb(txn.id))
                else:
                    await bot.send_message(admin_id, rtl(cap),
                                           reply_markup=kb.topup_decide_kb(txn.id))
            except Exception:  # noqa: BLE001 — one blocked admin shouldn't stop the others
                log.warning("notify admin of topup failed", exc_info=True)


# ── admin: confirm / reject / set-amount ─────────────────────────────────────

async def _strip_buttons(cb: CallbackQuery) -> None:
    """Remove the inline buttons from the tapped message (so another admin's copy of the same
    decision self-heals once anyone acts)."""
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — message too old / markup unchanged
        pass


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
            await _strip_buttons(cb)
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        cust = await s.get(StorefrontCustomer, txn.customer_id)
    await _strip_buttons(cb)
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
    await _strip_buttons(cb)
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


@storefront_router.callback_query(F.data.startswith("sfplanup:"))
async def sf_plan_up(cb: CallbackQuery, bot: Bot) -> None:
    await _sf_plan_move(cb, bot, "up")


@storefront_router.callback_query(F.data.startswith("sfplandown:"))
async def sf_plan_down(cb: CallbackQuery, bot: Bot) -> None:
    await _sf_plan_move(cb, bot, "down")


async def _sf_plan_move(cb: CallbackQuery, bot: Bot, direction: str) -> None:
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        moved = await storefront.move_plan(s, sf.id, plan_id, direction)
        plans = await storefront.list_plans(s, sf.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
    except Exception:  # noqa: BLE001 — "not modified" at an edge → ignore
        pass
    await cb.answer("جابه‌جا شد." if moved else "همین‌جاست.")


@storefront_router.callback_query(F.data.startswith("sfplanedit:"))
async def sf_plan_edit(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        plan = await s.get(StorefrontPlan, plan_id)
        if plan is None or plan.storefront_bot_id != sf.id:
            await cb.answer("پلن یافت نشد.", show_alert=True)
            return
        cur = f"{plan.gb} گیگ · {plan.days} روز · {plan.price_toman:,} تومان"
    await state.set_state(SF.edit_gb)
    await state.update_data(edit_plan_id=plan_id)
    await cb.message.answer(
        rtl(f"ویرایش پلن ({cur})\n\nحجمِ جدید به گیگابایت (عدد):"), reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.edit_gb, F.text)
async def sf_edit_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.update_data(e_gb=int(raw))
    await state.set_state(SF.edit_days)
    await message.answer(rtl("مدتِ جدید به روز (عدد):"), reply_markup=kb.cancel_kb())


@storefront_router.message(SF.edit_days, F.text)
async def sf_edit_days(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    await state.update_data(e_days=int(raw))
    await state.set_state(SF.edit_price)
    await message.answer(rtl("قیمتِ جدید به تومان (عدد):"), reply_markup=kb.cancel_kb())


@storefront_router.message(SF.edit_price, F.text)
async def sf_edit_price(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("عددِ معتبر بفرستید."), reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    await state.clear()
    plan_id = int(data.get("edit_plan_id", 0))
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        ok = await storefront.update_plan(
            s, sf.id, plan_id, gb=int(data.get("e_gb", 0)),
            days=int(data.get("e_days", 0)), price_toman=int(raw))
        plans = await storefront.list_plans(s, sf.id)
    await message.answer(
        rtl("✅ پلن ویرایش شد." if ok else "پلن یافت نشد."),
        reply_markup=kb.plans_manage_kb(plans))


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


@storefront_router.message(SF.welcome, F.text)
async def sf_welcome_set(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        sf.welcome_text = (message.text or "").strip()[:1000] or None
        await s.commit()
    await message.answer(rtl("✅ پیامِ خوش‌آمد ذخیره شد."), reply_markup=kb.admin_reply_kb())


# ── admin: forced-join (channel membership) ───────────────────────────────────

async def _bot_is_channel_admin(bot: Bot, channel_id: str, bot_telegram_id: int | None) -> bool:  # noqa: ANN001
    """True iff this storefront bot is an admin of the channel (so it can both gate joins and read
    member status). A bot not in the channel → get_chat_member raises → treated as not-admin."""
    if not bot_telegram_id:
        return False
    try:
        m = await bot.get_chat_member(channel_id, bot_telegram_id)
        return m.status in ("administrator", "creator")
    except Exception:  # noqa: BLE001
        return False


@storefront_router.callback_query(F.data == "sfjoinset")
async def sf_join_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if sf is None or not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.join_channel)
    await cb.message.answer(rtl(
        f"یک پیام از کانالِ خود را همین‌جا فوروارد کنید (یا @username یا آیدیِ -100… را بفرستید).\n"
        f"توجه: ربات (@{sf.bot_username or '—'}) باید در آن کانال ادمین باشد."),
        reply_markup=kb.cancel_kb())
    await cb.answer()


@storefront_router.message(SF.join_channel)
async def sf_join_channel_set(message: Message, state: FSMContext, bot: Bot) -> None:
    # Resolve the channel id from a forwarded post, or from @username / -100… text.
    channel_id: str | None = None
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if chat is not None and getattr(chat, "type", None) == "channel":
        channel_id = str(chat.id)
    elif message.text:
        t = message.text.strip()
        if t.startswith("@") and len(t) > 1:
            channel_id = t
        elif t.lstrip("-").isdigit():
            channel_id = t
    if not channel_id:
        await message.answer(rtl(
            "نشد. یک پیام از کانال را فوروارد کنید یا @username / آیدیِ -100… را بفرستید."),
            reply_markup=kb.cancel_kb())
        return
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            await state.clear()
            return
        bot_tg = sf.bot_telegram_id
    if not await _bot_is_channel_admin(bot, channel_id, bot_tg):
        await message.answer(rtl(
            "ابتدا ربات را در کانالِ خود ادمین کنید، سپس دوباره پیام را فوروارد کنید."),
            reply_markup=kb.cancel_kb())
        return  # stay in the state so they can retry after granting admin
    # Bot is admin → resolve a join link best-effort and save (enable by default).
    link: str | None = None
    try:
        full = await bot.get_chat(channel_id)
        if getattr(full, "username", None):
            link = f"https://t.me/{full.username}"
        else:
            invite = await bot.create_chat_invite_link(channel_id)
            link = invite.invite_link
    except Exception:  # noqa: BLE001 — link is optional; the gate still works via the check button
        link = None
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        sf.channel_id = channel_id[:64]
        sf.channel_link = (link or "")[:255] or None
        sf.channel_required = True
        await s.commit()
        markup = kb.join_settings_kb(sf)
    await message.answer(rtl("✅ کانال ثبت و عضویت اجباری فعال شد."), reply_markup=kb.admin_reply_kb())
    await message.answer(rtl("🔒 عضویت اجباری در کانال"), reply_markup=markup)


@storefront_router.callback_query(F.data == "sfjointog")
async def sf_join_toggle(cb: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if not sf.channel_required:  # turning ON → need a channel + the bot must be its admin
            if not sf.channel_id:
                await cb.answer("اول کانال را تنظیم کنید.", show_alert=True)
                return
            if not await _bot_is_channel_admin(bot, sf.channel_id, sf.bot_telegram_id):
                await cb.answer("اول ربات را در کانال ادمین کنید.", show_alert=True)
                return
        sf.channel_required = not sf.channel_required
        await s.commit()
        await cb.message.edit_reply_markup(reply_markup=kb.join_settings_kb(sf))
    await cb.answer("فعال شد." if sf.channel_required else "غیرفعال شد.")


@storefront_router.callback_query(F.data == "sfjoinclear")
async def sf_join_clear(cb: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        sf.channel_id = None
        sf.channel_link = None
        sf.channel_required = False
        await s.commit()
        await cb.message.edit_reply_markup(reply_markup=kb.join_settings_kb(sf))
    await cb.answer("حذف شد.")


@storefront_router.callback_query(F.data == "sfjoincheck")
async def sf_join_check(cb: CallbackQuery, bot: Bot) -> None:
    block = await _channel_block(bot, cb.from_user)
    if block is not None:
        await cb.answer("هنوز عضو کانال نیستید.", show_alert=True)
        return
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        cust = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        await _send_customer_menu(cb.message.answer, sf, cust)
    await cb.answer("✅ عضویت تأیید شد.")


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


# ── fallback: any other message → re-show the menu (registered LAST = lowest priority) ──────────

@storefront_router.message()
async def sf_fallback(message: Message, bot: Bot) -> None:
    """A stray message that matched no command/label/FSM state → gently re-show the right menu instead
    of ignoring it (a banned customer was already short-circuited by the middleware)."""
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        if is_admin:
            await _send_admin_menu(message.answer, sf)
        else:
            cust = await storefront.get_or_create_customer(s, sf.id, message.from_user)
            await _send_customer_menu(message.answer, sf, cust)
