"""Storefront bot handlers (one router shared by every reseller's bot; tenant resolved by bot.id).

Admin side = the reseller (their Telegram id == the reseller's bot_chat_id): plans, payment settings,
top-up review, customers + manual wallet edit, broadcast, support, customer preview.
Customer side = everyone else: buy (wallet-funded) → auto-provision config, wallet + top-up, my services.
All money is manual: the admin confirms each top-up and sets the credited Toman (no rates/API).
"""
from __future__ import annotations

import contextlib
import html
import logging
import secrets
import uuid

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
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
    StorefrontWalletTxn,
)
from app.services import (
    enforcement,
    settings_service,
    storefront,
    storefront_admin,
    storefront_autorenew,
    storefront_credit,
    storefront_ops,
    storefront_pricing,
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
    usercreate,
)

log = logging.getLogger("bot.storefront")

_PER_PAGE = 8          # customers per page in the admin «مشتری‌ها» list
_SEARCH_LIMIT = 20     # max matches shown for a customer search (refine if more)
_TRIAL_NO_RENEW = "⛔️ سرویسِ تستِ رایگان قابلِ تمدید نیست. برای ادامهٔ استفاده، لطفاً یکی از پلن‌ها را تهیه کنید."

# Credit-code rejection reasons → Persian (customer-facing).
_CREDIT_ERR = {
    "not_found": "کدِ واردشده معتبر نیست.",
    "disabled": "این کد در حال حاضر غیرفعال است.",
    "not_started": "این کد هنوز فعال نشده است.",
    "expired": "اعتبارِ این کد به پایان رسیده است.",
    "min_not_met": "این کد برای مبلغِ شارژِ واردشده قابلِ استفاده نیست.",
    "per_customer_exhausted": "شما پیش از این از این کد استفاده کرده‌اید.",
    "global_exhausted": "ظرفیتِ استفاده از این کد تکمیل شده است.",
    "no_bonus": "این کد برای این مبلغ، پاداشی در نظر نگرفته است.",
}

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
    # The amount is captured and the flow is now PARKED on inline buttons (credit code? which
    # payment method?). This must be a real state, not `None`: the re-dock middlewares treat
    # active → None as "the flow ended" and re-show the customer menu — which begins with the
    # shop's welcome text. Parking on `None` made that welcome pop up in the middle of a top-up,
    # right after the customer sent the amount. Keeping a state also keeps the flow FSM-LOCKED,
    # so a stray message gets the «✖️ انصراف» re-prompt instead of navigating away.
    topup_choice = State()     # data: topup_amount, topup_code_id, topup_bonus
    topup_proof = State()      # data: amount, method
    confirm_amount = State()   # admin sets credited Toman; data: txn_id
    adjust_amount = State()    # admin manual wallet edit; data: customer_id, sign
    support = State()
    welcome = State()          # admin sets the storefront welcome text
    closed_text = State()      # admin sets the «shop temporarily closed» message
    join_channel = State()     # admin sets the forced-join channel (forward a post / send @id)
    cust_search = State()      # admin searches customers by name / telegram id
    broadcast = State()
    add_admin = State()        # admin appoints a co-admin (numeric id or forwarded message)
    dm_compose = State()       # admin composes a relay message to a customer; data: dm_customer_id
    topup_code = State()       # customer enters a credit code during top-up; data: topup_amount
    gift_code = State()        # customer redeems a standalone gift code
    # credit-code admin wizard (کد شارژ). data threads through: c_code, c_kind, c_is_gift, c_percent,
    # c_amount, c_maxbonus, c_mintopup, c_maxuses, c_percustomer.
    credit_code = State()
    credit_value = State()
    credit_maxbonus = State()
    # Same «parked, not finished» role as `topup_choice`, for the wizard's inline-button steps
    # (code type, per-customer limit). Clearing the state there re-showed the admin menu mid-wizard.
    credit_choice = State()
    credit_mintopup = State()
    credit_maxuses = State()
    credit_percustomer = State()
    credit_expiry = State()


# ── helpers ───────────────────────────────────────────────────────────────────

def _digits(text: str | None) -> str:
    return (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬,", "0123456789  ")).replace(" ", "")


def _toman(value) -> str:  # noqa: ANN001
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _usage_line(used_gb: float, limit_gb: float, plan_gb: int) -> str:
    """The «مصرف» line for a config. Renewal ADDS quota (a renewed 10-گیگابایت plan shows a 20-گیگابایت
    limit), which reads confusingly next to «پلن: ۱۰ گیگابایت» — so when the live limit exceeds the
    plan size, label it «(شاملِ تمدید)» to make clear the total allowance includes renewals."""
    base = f"مصرف: {used_gb:.2f} از {limit_gb:.0f} گیگابایت"
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


async def _send_admin_menu(  # noqa: ANN001
    message, sf: StorefrontBot, *, session, reseller, user_id: int,
) -> None:
    """Show the lean shop-admin home: ONE message docking a ≤5 reply keyboard.

    The PRIMARY OWNER's keyboard includes «🌐 مدیریت فروشگاه در مرورگر» as a normal button — it used
    to be pushed as a separate inline message above every single menu, which looked bolted-on and
    cluttered the chat. A CO-ADMIN doesn't get it (not a portal principal). All 14 management labels
    stay routable via `sf_menu` (top-level or under «بیشتر»)."""
    await message.answer(
        rtl(f"🛠 پنلِ مدیریتِ فروشگاه\nربات: @{sf.bot_username or '—'}\nیک گزینه را انتخاب کنید:"),
        reply_markup=kb.admin_main_reply_kb(owner=_is_shop_owner(reseller, user_id)),
    )


async def _send_shop_portal_link(message, sf: StorefrontBot, *, session, reseller) -> None:  # noqa: ANN001
    """Reply with the owner's permanent shop-management link (tapped from the menu button)."""
    from app.bot.handlers.common import portal_stable_url

    url = await portal_stable_url(
        session, reseller.bot_chat_id, with_login_token=True,
        next_path=f"/portal/storefront/{sf.id}")
    if not url:
        await message.answer(rtl("🌐 پنلِ تحتِ وب هنوز پیکربندی نشده است."))
        return
    await message.answer(
        rtl("🌐 مدیریتِ کاملِ فروشگاه در مرورگر — پلن‌ها، مشتری‌ها، شارژها و تنظیمات:"),
        reply_markup=kb.owner_portal_inline_kb(url),
    )


async def _reshow_admin_menu(message, note: str, *, bot: Bot) -> None:  # noqa: ANN001, ARG001
    """Send a flow-completion `note`. Re-docking the menu is handled uniformly by the
    `_sf_redock_after_flow` middleware (which fires whenever a flow's state goes active → None), so
    every exit — success or early-return — restores the right menu without each site remembering to."""
    await message.answer(rtl(note))


async def _redock_sf_menu_for(msg, user, bot: Bot) -> None:  # noqa: ANN001
    """Re-dock the correct menu (admin home / customer menu) for `user` — the HUMAN — sending through
    `msg` (any Message we can `.answer`). `user` is explicit so this works from a CALLBACK too, where
    `msg.from_user` is the bot, not the customer."""
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, user)
        if sf is None:
            return
        if is_admin:
            await _send_admin_menu(msg, sf, session=s, reseller=reseller, user_id=user.id)
        else:
            cust = await storefront.get_or_create_customer(s, sf.id, user)
            await _send_customer_menu(msg.answer, sf, cust)


async def _redock_sf_menu(message, bot: Bot) -> None:  # noqa: ANN001
    """Re-dock after a MESSAGE-driven flow ends (a message's `from_user` IS the human)."""
    await _redock_sf_menu_for(message, message.from_user, bot)


def _trial_available(sf: StorefrontBot, customer: StorefrontCustomer) -> bool:
    return bool(sf.free_trial_enabled) and not customer.free_trial_used


async def _send_customer_menu(answer, sf: StorefrontBot, customer: StorefrontCustomer, *, preview=False) -> None:  # noqa: ANN001
    bal = _toman(storefront_wallet.balance(customer))
    text = sf.welcome_text or "🛍 به فروشگاهِ ما خوش آمدید!"
    lines = [text, "", f"👛 موجودیِ کیفِ پولِ شما: {bal} تومان"]
    show_trial = _trial_available(sf, customer)
    if show_trial:
        lines.append(f"🎁 تستِ رایگان ({sf.free_trial_gb} گیگابایت · {sf.free_trial_days} روز) فعال است.")
    await answer(
        rtl("\n".join(lines)),
        reply_markup=kb.customer_reply_kb(is_admin_preview=preview, show_free_trial=show_trial),
    )


async def _send_customer_preview(answer, preview: dict) -> None:  # noqa: ANN001
    """Render the admin preview from a pure DTO; never creates/updates a customer row."""
    lines = [preview["welcome_text"], "", "👛 موجودیِ کیفِ پولِ شما: ۰ تومان"]
    if preview["shop_closed"]:
        lines.extend(["", preview["closed_text"] or _SHOP_CLOSED_DEFAULT])
    trial = preview["trial"]
    if trial["enabled"]:
        lines.append(f"🎁 تستِ رایگان ({trial['gb']} گیگابایت · {trial['days']} روز) فعال است.")
    if preview["channel_required"]:
        lines.append("🔒 عضویت در کانال پیش از استفاده الزامی است.")
    plans = preview["plans"]
    if plans:
        lines.extend(["", "📦 پلن‌های فعال:"])
        lines.extend(
            f"• {plan['title'] or 'پلن فروشگاه'}: {plan['gb']} گیگابایت · {plan['days']} روز · "
            f"{_toman(plan['price_toman'])} تومان"
            for plan in plans[:10]
        )
    methods = [
        label for name, label in (("card", "کارت"), ("usdt", "USDT"), ("ton", "TON"))
        if preview["payment_methods"].get(name)
    ]
    if methods:
        lines.extend(["", f"💳 روش‌های پرداخت: {'، '.join(methods)}"])
    if preview["support_contact"]:
        lines.append(f"☎️ پشتیبانی: {preview['support_contact']}")
    await answer(
        rtl("\n".join(lines)),
        reply_markup=kb.customer_reply_kb(
            is_admin_preview=True, show_free_trial=bool(trial["enabled"])),
    )


def _command_ctx(user_id: int, version: int, key: str) -> storefront_admin.CommandContext:
    return storefront_admin.CommandContext(
        actor_telegram_id=user_id,
        actor_role="admin",
        source="bot",
        idempotency_key=key,
        expected_version=version,
        correlation_id=key,
    )


def _callback_ctx(
    cb: CallbackQuery, sf: StorefrontBot, *, rendered_version: int | None = None,
) -> storefront_admin.CommandContext:
    return _command_ctx(
        cb.from_user.id,
        sf.config_version if rendered_version is None else rendered_version,
        f"tg-callback:{cb.id}",
    )


def _absolute_toggle(data: str | None) -> tuple[bool, int] | None:
    """Read the desired state and config version embedded in a rendered inline button.

    Old Telegram messages have legacy toggle callbacks without these fields. They are deliberately
    treated as stale and only refreshed; deriving intent from today's DB value could reverse a
    portal change made after that keyboard was rendered.
    """
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[-2] not in {"0", "1"}:
        return None
    try:
        version = int(parts[-1])
    except ValueError:
        return None
    if version < 1:
        return None
    return parts[-2] == "1", version


async def _start_admin_fsm(state: FSMContext, sf: StorefrontBot) -> None:
    await state.update_data(
        sf_config_version=sf.config_version,
        sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
    )


def _price_prompt(head: str, data: dict, cost_key: str, gb_key: str) -> str:
    """The price prompt plus the unit warning and this shop's own price floor. Spelling the unit
    out BEFORE the reseller types is what actually prevents the ×1000 slip; the guard behind it is
    only the backstop."""
    hint = storefront_pricing.price_prompt_hint_fa(
        cost=int(data.get(cost_key) or 0), gb=int(data.get(gb_key) or 0))
    return f"{head}\n\n{hint}"


async def _rotate_command_key(state: FSMContext) -> None:
    """Mint a fresh idempotency key so the reseller can retype a CORRECTED value in the same FSM
    state. `storefront_audit.claim_command` returns `conflict` when a key reappears with a different
    request hash, so reusing the flow's original key after a rejection would answer the fixed input
    with «این درخواست هنوز در حال انجام است» instead of accepting it. `sf_config_version` stays
    valid: a cached failure never CASes."""
    await state.update_data(sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}")


def _fsm_ctx(user_id: int, data: dict, sf: StorefrontBot) -> storefront_admin.CommandContext:
    return _command_ctx(
        user_id,
        int(data.get("sf_config_version", sf.config_version)),
        str(data.get("sf_command_key") or f"tg-fsm:{secrets.token_urlsafe(18)}"),
    )


def _admin_error(exc: storefront_admin.AdminCommandError) -> str:
    if exc.code == "config_conflict":
        return "⚠️ تنظیمات به‌طور همزمان تغییر کرد؛ اطلاعاتِ تازه بارگذاری شد. لطفاً دوباره تلاش کنید."
    if exc.code == "in_flight":
        return "این درخواست هنوز در حال انجام است؛ چند لحظه بعد دوباره بررسی کنید."
    if exc.code == "unknown":
        return "نتیجهٔ درخواست نامشخص است؛ برای جلوگیری از اجرای دوباره، ابتدا وضعیت فعلی را بررسی کنید."
    if exc.code == "not_found":
        return "مورد پیدا نشد یا دسترسی ندارید."
    return str(exc)


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


async def _relay_message(bot: Bot, chat_id: int, header: str, message: Message,
                         *, reply_markup=None) -> bool:  # noqa: ANN001
    """Relay a person's composed message (text or photo+caption) to `chat_id`, prefixed with a plain
    `header` line so the recipient knows who it's from. Plain text (no HTML) so the composer's content
    is never interpreted as markup. Returns False if the recipient is unreachable (blocked the bot /
    deactivated) or the send fails; True on success."""
    try:
        if message.photo:
            cap = (message.caption or "").strip()
            body = rtl(f"{header}\n\n{cap}") if cap else rtl(header)
            await bot.send_photo(chat_id, message.photo[-1].file_id, caption=body,
                                 reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id, rtl(f"{header}\n\n{(message.text or '').strip()}"),
                                   reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except TelegramForbiddenError:
        return False
    except Exception:  # noqa: BLE001 — a bad-request / transient error must not crash the handler
        log.warning("storefront relay failed", exc_info=True)
        return False


async def _relay_to_admins(bot: Bot, admin_ids: list[int], cust_name: str, customer_id: int,
                           message: Message, *, owner_id: int | None = None,
                           portal_url: str | None = None) -> bool:
    """Deliver a customer's message to every shop admin with a «پاسخ» button. Returns True if at
    least one admin received it. The OWNER additionally gets a portal deep-link to that customer's
    page (co-admins aren't portal principals)."""
    header = f"💬 پیام از مشتری «{cust_name}» (#{customer_id}):"
    delivered = False
    for aid in admin_ids:
        markup = kb.relay_reply_kb(
            customer_id, portal_url=portal_url if aid == owner_id else None)
        if await _relay_message(bot, aid, header, message, reply_markup=markup):
            delivered = True
    return delivered


async def _deliver_config(  # noqa: ANN001
    bot: Bot, chat_id: int, *, gb: int, days: int, sub_link: str, name: str | None = None
) -> None:
    head = f"✅ سرویسِ «{name}» آماده شد" if name else "✅ سرویسِ شما آماده شد"
    caption = rtl(
        f"{head} — {gb} گیگابایت · {days} روز\n\n"
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


# ── suspension gate (the shop is closed while its reseller is suspended) ──────

_SUSPENDED_CUSTOMER = (
    "🛠 فروشگاه موقتاً در دسترس نیست.\n"
    "سرویس‌های فعالِ شما محفوظ است و چیزی از بین نمی‌رود؛ فقط تا اطلاع بعدی امکانِ خرید، تمدید و "
    "تغییر وضعیت وجود ندارد.\n"
    "لطفاً کمی بعد دوباره تلاش کنید یا با پشتیبانی در تماس باشید. از شکیباییِ شما سپاسگزاریم."
)
_SUSPENDED_ADMIN = (
    "⛔️ پنلِ شما در سامانه مسدود شده و به همین دلیل رباتِ فروشگاهیِ شما موقتاً از کار افتاده است.\n"
    "تا زمانِ رفعِ مسدودی، مشتریانِ شما نمی‌توانند خرید، تمدید یا فعال‌سازی انجام دهند و پیامِ "
    "«در دسترس نبودنِ موقت» می‌بینند.\n"
    "برای بازگشتِ سرویس، فاکتورِ باز خود را پرداخت کنید یا با پشتیبانی تماس بگیرید."
)


async def _suspended(bot: Bot, user) -> bool | None:  # noqa: ANN001
    """True when this shop's reseller (or an ancestor) is suspended, None when it isn't.

    Returns the ADMIN-vs-customer distinction via `_suspension_is_admin` below. Fails OPEN on any
    error: a transient DB blip must not close a healthy shop."""
    try:
        async with SessionLocal() as s:
            sf, reseller, is_admin = await _resolve(s, bot, user)
            if sf is None:
                return None
            if await enforcement.suspended_branch_root(s, reseller) is None:
                return None
            return bool(is_admin)
    except Exception:  # noqa: BLE001
        log.warning("storefront suspension check failed", exc_info=True)
        return None


@storefront_router.callback_query.outer_middleware
async def _sf_suspended_cb_mw(handler, event, data):  # noqa: ANN001, ANN202
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None:
        is_admin = await _suspended(bot, user)
        if is_admin is not None:
            await event.answer("فروشگاه موقتاً در دسترس نیست.", show_alert=True)
            with contextlib.suppress(Exception):
                await event.message.answer(
                    rtl(_SUSPENDED_ADMIN if is_admin else _SUSPENDED_CUSTOMER))
            return None
    return await handler(event, data)


@storefront_router.message.outer_middleware
async def _sf_suspended_msg_mw(handler, event, data):  # noqa: ANN001, ANN202
    bot, user = data.get("bot"), getattr(event, "from_user", None)
    if bot is not None and user is not None:
        is_admin = await _suspended(bot, user)
        if is_admin is not None:
            await event.answer(rtl(_SUSPENDED_ADMIN if is_admin else _SUSPENDED_CUSTOMER))
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
            await _send_admin_menu(
                message, sf, session=s, reseller=reseller, user_id=message.from_user.id)
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


@storefront_router.message.outer_middleware
async def _sf_redock_after_flow(handler, event, data):  # noqa: ANN001, ANN202
    """Every flow entry docks the cancel-only `flow_cancel_kb`. This re-docks the right menu whenever a
    flow's state goes active → None during a message (any exit — success or early-return), mirroring the
    main bot. Registered after the ban/join gates so it wraps the actual handler."""
    state = data.get("state")
    before = await state.get_state() if state is not None else None
    result = await handler(event, data)
    try:
        # `/start` and the cancel handlers rebuild the menu themselves → skip to avoid doubling it.
        text = (getattr(event, "text", "") or "").strip()
        first = (text.split(maxsplit=1) or [""])[0].lower()
        if (first not in ("/start", "/cancel") and text != kb.CANCEL_LABEL
                and before is not None and state is not None
                and await state.get_state() is None):
            bot = data.get("bot")
            if bot is not None and getattr(event, "from_user", None) is not None:
                await _redock_sf_menu(event, bot)
    except Exception:  # noqa: BLE001 — a menu re-show must never break the handler
        log.warning("storefront re-dock after flow failed", exc_info=True)
    return result


# Callbacks that already restore the menu themselves → don't let the middleware double-dock.
_CB_SELF_REDOCK = {"sfcancel"}


@storefront_router.callback_query.outer_middleware
async def _sf_redock_after_cb_flow(handler, event, data):  # noqa: ANN001, ANN202
    """Companion to `_sf_redock_after_flow` for flows that COMPLETE via a CALLBACK (the purchase
    confirm, renew confirm, top-up submit, …). Those clear the FSM state inside a callback, which the
    message-only middleware never sees — so the cancel-only «✖️ انصراف» keyboard used to linger after a
    successful purchase. Re-dock the right menu whenever a callback takes the flow state active → None."""
    state = data.get("state")
    before = await state.get_state() if state is not None else None
    result = await handler(event, data)
    try:
        cb_data = getattr(event, "data", "") or ""
        if (cb_data not in _CB_SELF_REDOCK and before is not None and state is not None
                and await state.get_state() is None):
            bot = data.get("bot")
            user = getattr(event, "from_user", None)
            msg = getattr(event, "message", None)
            if bot is not None and user is not None and msg is not None:
                await _redock_sf_menu_for(msg, user, bot)
    except Exception:  # noqa: BLE001 — a menu re-show must never break the handler
        log.warning("storefront re-dock after callback flow failed", exc_info=True)
    return result


@storefront_router.message(Command("cancel"))
async def sf_cmd_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    """Global cancel: clear the flow and ALWAYS restore the menu. Registered before every FSM-state
    text handler, and the ban/join middlewares exempt `/cancel`."""
    await state.clear()
    await message.answer(rtl("لغو شد."))
    await _redock_sf_menu(message, bot)


@storefront_router.message(F.text == kb.CANCEL_LABEL)
async def sf_cancel_label(message: Message, state: FSMContext, bot: Bot) -> None:
    """The docked «✖️ انصراف» button — it must ALWAYS hand the menu back.

    It used to rely on the re-dock middleware, which only fires when a flow was actually active. A
    customer whose flow state had vanished (the storefront FSM lives in memory, so EVERY bot restart
    wipes it) was left with the cancel-only keyboard on their phone and no state on the server: each
    tap answered «لغو شد.» and re-docked nothing, trapping them in a loop with no way back except
    /start. Cancel now re-docks unconditionally."""
    await state.clear()
    await message.answer(rtl("لغو شد."))
    await _redock_sf_menu(message, bot)


@storefront_router.message(F.text.in_(kb.ALL_LABELS))
async def sf_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    # FSM-LOCK: while a flow is active, a menu label must NOT navigate/clear — re-prompt cancel.
    if text != kb.CANCEL_LABEL and await state.get_state() is not None:
        await message.answer(
            rtl("شما در حال یک عملیات هستید؛ برای خروج «✖️ انصراف» را بزنید."),
            reply_markup=kb.flow_cancel_kb())
        return
    trial_ids: tuple[int, int] | None = None
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        await state.clear()
        if text in (kb.BACK_TO_ADMIN, kb.BACK_LABEL) and is_admin:
            await _send_admin_menu(
                message, sf, session=s, reseller=reseller, user_id=message.from_user.id)
            return
        if is_admin and text == kb.PORTAL_LABEL:
            if _is_shop_owner(reseller, message.from_user.id):
                await _send_shop_portal_link(message, sf, session=s, reseller=reseller)
            return
        if is_admin and text == kb.MORE_LABEL:
            is_owner = _is_shop_owner(reseller, message.from_user.id)
            await message.answer(rtl("گزینه‌های بیشتر:"), reply_markup=kb.admin_more_reply_kb(owner=is_owner))
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
            f"حجم: {sf.free_trial_gb} گیگابایت · مدت: {sf.free_trial_days} روز\n\n"
            "نکته: حجمِ ۱ گیگابایت (یا کمتر) برای شما رایگان است؛ بیشتر از آن در فاکتورِ شما حساب می‌شود."),
            reply_markup=kb.trial_settings_kb(sf))
    elif action == "topups":
        pend = await storefront_wallet.pending_topups_for_bot(s, sf.id)
        if not pend:
            await ans(rtl("شارژِ در انتظاری وجود ندارد."))
            return
        await ans(rtl(f"🧾 {len(pend)} شارژِ در انتظارِ تأیید:"))
        for txn in pend:
            cust = await s.get(StorefrontCustomer, txn.customer_id)
            cap = (f"#{txn.id} — {_toman(txn.amount_toman)} تومان — {txn.method or '—'}\n"
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
            f"🎁 کدهای شارژ (این ماه): {st_.credit_redemptions_month} استفاده · "
            f"{st_.credit_bonus_month_toman:,.0f} تومان پاداش\n"
            f"🏦 موجودی کیف پول مشتری‌ها: {st_.wallet_liability_toman:,.0f} تومان"))
    elif action == "broadcast":
        await ans(rtl("📢 پیامِ همگانی — گیرندگان را انتخاب کنید:"),
                  reply_markup=kb.broadcast_segment_kb())
    elif action == "support":
        await _start_admin_fsm(state, sf)
        await state.set_state(SF.support)
        await ans(rtl(
            f"شناسهٔ پشتیبانی خود را بفرستید (برای مثال @Support).\nفعلی: {sf.support_contact or '—'}"),
            reply_markup=kb.flow_cancel_kb())
    elif action == "welcome":
        await _start_admin_fsm(state, sf)
        await state.set_state(SF.welcome)
        await ans(rtl(
            "متنِ خوش‌آمدگوییِ فروشگاه را بفرستید (به مشتری در شروع نشان داده می‌شود).\n"
            f"فعلی: {sf.welcome_text or '🛍 به فروشگاهِ ما خوش آمدید!'}"),
            reply_markup=kb.flow_cancel_kb())
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
    elif action == "credits":
        codes = await storefront_credit.list_codes(s, sf.id)
        await ans(rtl(
            "🎁 کدهای شارژ\n\nمشتری هنگامِ شارژِ کیفِ پول، کد را وارد می‌کند و پاداش می‌گیرد (یا کدِ "
            "هدیهٔ بدونِ پرداخت). هر تخفیف فقط از سودِ شما کم می‌شود؛ بنابراین مبلغ‌ها را با دقت تنظیم کنید."),
            reply_markup=kb.credit_codes_manage_kb(codes))
    elif action == "shopstate":
        state_fa = "🔴 بسته" if sf.shop_closed else "🟢 باز"
        await ans(rtl(
            f"وضعیتِ فروشگاه: {state_fa}\n\n"
            "وقتی «بسته» باشد، مشتری‌ها نمی‌توانند خرید یا تمدید کنند و پیامِ زیر را می‌بینند؛ ربات "
            "روشن می‌ماند و «سرویس‌های من» و «کیف پول» همچنان کار می‌کند.\n\n"
            f"پیامِ فعلی: {sf.closed_text or _SHOP_CLOSED_DEFAULT}"),
            reply_markup=kb.shop_state_kb(sf))
    elif action == "preview":
        await state.update_data(sf_preview=True)
        preview = await storefront_admin.customer_preview(
            s, sf.id, message.from_user.id, "bot")
        await _send_customer_preview(message.answer, preview)


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
    lines += ["", "«➕ افزودن مدیر» را بزنید تا فردِ دیگری هم بتواند این فروشگاه را مدیریت کند."]
    await ans(rtl("\n".join(lines)), reply_markup=kb.admins_manage_kb(co_ids))


@storefront_router.callback_query(F.data == "sfaddadmin")
async def sf_add_admin_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, cb.from_user)
    if sf is None or not _is_shop_owner(reseller, cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی می‌تواند مدیر اضافه کند.", show_alert=True)
        return
    await _start_admin_fsm(state, sf)
    await state.set_state(SF.add_admin)
    await cb.message.answer(rtl(
        "👤 شناسهٔ عددیِ تلگرامِ فرد را بفرستید،\n"
        "یا یک پیام از او را برای همین ربات فوروارد کنید.\n\n"
        "نکته: آن فرد باید حداقل یک‌بار ربات را /start کرده باشد تا بتواند وارد پنلِ مدیریت شود."),
        reply_markup=kb.flow_cancel_kb())
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
            "ورودیِ واردشده معتبر نبود. یک شناسهٔ عددیِ معتبر بفرستید، یا پیامی از آن فرد را فوروارد کنید.\n"
            "(اگر فوروارد بی‌اثر بود، آن فرد در تنظیماتِ حریمِ خصوصیِ تلگرام فوروارد را بسته است؛ "
            "شناسهٔ عددیِ او را به‌صورت دستی بفرستید.)"),
            reply_markup=kb.flow_cancel_kb())
        return
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, reseller, _a = await _resolve(s, bot, message.from_user)
        if sf is None or not _is_shop_owner(reseller, message.from_user.id):
            await state.clear()
            await message.answer(rtl("فقط مدیرِ اصلی می‌تواند مدیر اضافه کند."))
            return
        try:
            await storefront_admin.add_manager(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf), telegram_id=target_id)
            result = "ok"
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "config_conflict":
                result = "conflict"
            elif "limit" in str(exc):
                result = "full"
            elif "banned" in str(exc):
                result = "banned"
            else:
                result = "invalid" if exc.code in ("validation", "not_found") else "error"
        await state.clear()
        note = {
            "ok": f"✅ شناسهٔ {target_id} به‌عنوان مدیرِ فروشگاه اضافه شد.",
            "exists": f"این شناسه ({target_id}) از قبل مدیر است.",
            "is_owner": "این شناسهٔ خودِ شماست؛ شما مدیرِ اصلی هستید.",
            "full": f"حداکثر {storefront.MAX_CO_ADMINS} مدیرِ اضافه مجاز است.",
            "invalid": "شناسهٔ نامعتبر است؛ یک عددِ مثبت بفرستید.",
            "banned": "این کاربر مسدود است؛ ابتدا رفعِ مسدودی را انجام دهید، سپس او را مدیر کنید.",
            "conflict": "⚠️ فهرستِ مدیران تغییر کرده است؛ لطفاً دوباره تلاش کنید.",
        }.get(result, "خطا در افزودنِ مدیر.")
        await message.answer(rtl(note))
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
        try:
            await storefront_admin.remove_manager(
                s, sf.id, _callback_ctx(cb, sf), telegram_id=tid)
            removed = True
        except storefront_admin.AdminCommandError as exc:
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        co_ids = storefront.co_admin_ids(sf)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.admins_manage_kb(co_ids))
    except Exception:  # noqa: BLE001 — markup unchanged / message too old
        pass
    await cb.answer("حذف شد." if removed else "یافت نشد.")


# ── CUSTOMER ──────────────────────────────────────────────────────────────────

_SHOP_CLOSED_DEFAULT = (
    "🔴 فروشگاه موقتاً بسته است؛ خرید و تمدیدِ سرویس در دسترس نیست. لطفاً کمی بعد دوباره مراجعه کنید."
)


def _shop_closed_text(sf: StorefrontBot) -> str:
    return (sf.closed_text or "").strip() or _SHOP_CLOSED_DEFAULT


async def _customer_action(action, message, state, s, sf, customer, bot) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "buy":
        if sf.shop_closed:  # «temporarily closed» → no buying (bot stays online for the rest)
            await ans(rtl(_shop_closed_text(sf)))
            return
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
        has_gift = await storefront_credit.has_gift_codes(s, sf.id)
        await ans(rtl("\n".join(lines)), reply_markup=kb.wallet_kb(gift=has_gift))
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
            lines += [f"• {o.label or 'سرویس'} — {o.gb}گیگابایت/{o.days}روز — "
                      f"{labels.get(o.status, o.status)}" for o in other[:10]]
            await ans(rtl("\n".join(lines)))
    elif action == "support":
        contact = f"💬 پشتیبانی: {sf.support_contact}\n" if sf.support_contact else ""
        await ans(rtl(f"{contact}✍️ هر پیامی همین‌جا بنویسید تا مستقیم به پشتیبانیِ فروشگاه برسد و "
                      "پاسخ بگیرید."))


async def _claim_free_trial(message, sf_id: int, customer_id: int, bot: Bot) -> None:  # noqa: ANN001
    """Claim the one-time free trial via the concurrency-safe service (compare-and-set + atomic
    provision), then deliver the config. No DB connection is held across the panel/Telegram I/O."""
    async with SessionLocal() as s:
        sf = await s.get(StorefrontBot, sf_id)
    if sf is not None and sf.shop_closed:  # closed → no new configs, incl. free trials
        await message.answer(rtl(_shop_closed_text(sf)))
        return
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

@storefront_router.callback_query(F.data == "sfbuylist")
async def sf_buy_list(cb: CallbackQuery, bot: Bot) -> None:
    """Open the plan list — the button on the «free trial ended → buy» proactive nudge."""
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        if customer.banned:
            await cb.answer("دسترسیِ شما مسدود شده است.", show_alert=True)
            return
        if sf.shop_closed:
            await cb.answer()
            await cb.message.answer(rtl(_shop_closed_text(sf)))
            return
        plans = await storefront.list_plans(s, sf.id, only_enabled=True)
    if not plans:
        await cb.message.answer(rtl("در حال حاضر پلنی برای فروش موجود نیست."))
    else:
        await cb.message.answer(rtl("🛒 یک سرویس را انتخاب کنید:"),
                                reply_markup=kb.buy_plans_kb(plans))
    await cb.answer()

@storefront_router.callback_query(F.data.startswith("sfbuy:"))
async def sf_buy(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Step 1: a plan is tapped. Check the balance, then ask the customer to NAME this service."""
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        if sf.shop_closed:
            await cb.answer()
            await cb.message.answer(rtl(_shop_closed_text(sf)))
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
        "یک نام دلخواه برای این سرویس انتخاب کنید تا در فهرستِ سرویس‌هایتان به‌راحتی آن را از بقیه تشخیص دهید:"),
        reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.buy_name, F.text)
async def sf_buy_name(message: Message, state: FSMContext, bot: Bot) -> None:
    """Step 2: validate the chosen name and show a final confirm card before charging."""
    name = " ".join((message.text or "").split())
    if not name or len(name) > 40:
        await message.answer(rtl("نامِ واردشده معتبر نیست (۱ تا ۴۰ نویسه). لطفاً دوباره وارد کنید."),
                             reply_markup=kb.flow_cancel_kb())
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
        op_id = secrets.token_urlsafe(16)
        await storefront_ops.reserve(
            s, op_id, op_type="purchase", storefront_bot_id=sf.id, customer_id=customer.id,
            plan_id=plan.id, price_toman=int(plan.price_toman), gb=int(plan.gb), days=int(plan.days),
        )
        await s.commit()  # bind the token BEFORE it is exposed in the confirmation callback
    await state.update_data(buy_name=name, buy_op=op_id)
    await message.answer(rtl(
        f"🧾 تأییدِ خرید\n\n{plan_text}\nنام: {name}\n\n"
        f"موجودیِ پس از خرید: {_toman(after)} تومان"),
        reply_markup=kb.buy_confirm_kb(op_id))


@storefront_router.callback_query(F.data.startswith("sfbuyok"))
async def sf_buy_ok(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Step 3: hand off to the atomic, crash-safe, IDEMPOTENT purchase service (charge + provision in
    short txns, no DB connection held across the panel/Telegram I/O), then deliver or report.

    F4: the durable `op_id` (minted at the confirm card, carried in the callback data + FSM) makes a
    re-tapped / re-delivered confirm exactly-once — purchase() returns the cached result instead of a
    second order/debit."""
    await _strip_buttons(cb)
    data = await state.get_data()
    op_id = (cb.data.split(":", 1)[1] if (cb.data and ":" in cb.data) else None) or data.get("buy_op")
    plan_id = int(data.get("buy_plan_id") or 0)
    name = (data.get("buy_name") or "").strip()
    await state.clear()
    # With an op_id we always call through (purchase() replays the cached result even if the FSM is
    # gone). Only bail when we have neither an op_id nor a fresh plan/name to work with.
    if not op_id and (not plan_id or not name):
        await cb.answer()
        return
    async with SessionLocal() as s:
        sf, reseller, _ = await _resolve(s, bot, cb.from_user)
        if sf is None:
            await cb.answer()
            return
        if sf.shop_closed:
            await cb.answer()
            await cb.message.answer(rtl(_shop_closed_text(sf)))
            return
        customer = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
        sf_id, customer_id, reseller_id = sf.id, customer.id, sf.reseller_id
    await cb.answer()
    await cb.message.answer(rtl("⏳ در حال ساختِ سرویس…"))
    res = await storefront_provision.purchase(
        SessionLocal, sf_id=sf_id, customer_id=customer_id, plan_id=plan_id, label=name, op_id=op_id)
    if res.reason == "processing":
        await cb.message.answer(rtl("⏳ سفارشِ شما در حال پردازش است؛ چند لحظه صبر کنید."))
        return
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
        armed = storefront_autorenew.is_armed(order)
        status = await storefront_provision.live_status(s, sf, order)
    lines = [f"📦 {name}", f"پلن: {gb} گیگابایت · {days} روز"]
    if paused:
        lines.append("وضعیت: متوقف ⏸")
    if status.ok:
        lines.append(_usage_line(status.used_gb, status.limit_gb, gb))
        if status.remaining_days is not None:
            lines.append(f"روزهای باقی‌مانده: {status.remaining_days}")
    else:
        lines.append("اطلاعاتِ مصرف فعلاً در دسترس نیست.")
    if not order.is_trial:
        lines.append("🔁 تمدید خودکار: فعال (یک‌بار) ✅" if armed else "🔁 تمدید خودکار: خاموش")
    if sub_link:
        lines += ["", "🔗 لینکِ اشتراک:", f"<code>{sub_link}</code>"]
    caption = rtl("\n".join(lines))
    markup = kb.order_actions_kb(order_id, renew_price, paused=paused, is_trial=order.is_trial,
                                 armed=armed)
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


async def _report_arm(cb: CallbackQuery, res: storefront_autorenew.ArmResult, price_hint: int) -> None:
    """Tell the customer the result of ARMING one-shot auto-renew."""
    if res.ok:
        await cb.message.answer(rtl(
            "✅ «تمدید خودکار» روشن شد.\n"
            f"مبلغِ یک تمدید ({_toman(res.price or price_hint)} تومان) در کیف پول شما رزرو شد و "
            "درست پیش از اتمامِ حجم یا زمانِ سرویس، یک‌بار به‌صورت خودکار تمدید می‌شوید تا قطع نشوید."))
    elif res.reason == "insufficient":
        await cb.message.answer(rtl(
            "برای روشن‌کردنِ «تمدید خودکار» باید مبلغِ یک تمدید در کیف پول رزرو شود.\n"
            f"{_toman(res.short_toman)} تومان کم دارید؛ ابتدا کیف پول را شارژ کنید."),
            reply_markup=kb.wallet_kb())
    elif res.reason == "trial":
        await cb.message.answer(rtl(_TRIAL_NO_RENEW))
    elif res.reason == "below_cost":
        # The shop's pricing is broken (this plan sells under the reseller's own cost), so we
        # refuse to lock in another loss-making renewal. NEVER surface the reseller's cost to a
        # customer — this is the shop's problem to fix, not the customer's to understand.
        await cb.message.answer(rtl(
            "🔁 «تمدید خودکار» فعلاً برای این سرویس در دسترس نیست. "
            "لطفاً با پشتیبانیِ فروشگاه تماس بگیرید."))
    else:
        await cb.message.answer(rtl("❌ انجام نشد. لطفاً بعداً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."))


@storefront_router.callback_query(F.data.startswith("sfauto:"))
async def sf_autorenew_toggle(cb: CallbackQuery, bot: Bot) -> None:
    """Toggle ONE-SHOT auto-renew for a config: arm (reserve the plan price) if off, disarm (return
    the reservation) if on. This is the ONLY customer renew path now."""
    order_id = int((cb.data or "").split(":")[1])
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
        if sf.shop_closed:
            await cb.answer()
            await cb.message.answer(rtl(_shop_closed_text(sf)))
            return
        sf_id = sf.id
        paused = order.status == "disabled"
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        renew_price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        was_armed = storefront_autorenew.is_armed(order)
    await cb.answer()
    if was_armed:
        async with SessionLocal() as s:
            await storefront_autorenew.disarm(s, order_id, expected_sf_id=sf_id)
        new_armed = False
        await cb.message.answer(rtl("🔁 «تمدید خودکار» خاموش شد و مبلغِ رزروشده به کیفِ پولِ شما بازگردانده شد."))
    else:
        async with SessionLocal() as s:
            res = await storefront_autorenew.arm(s, order_id, expected_sf_id=sf_id)
        new_armed = res.ok
        await _report_arm(cb, res, renew_price)
    # Flip the toggle label in place on the card the button lives on (best-effort — a photo/old
    # message may reject the edit; the confirmation text above already tells the customer the state).
    try:
        await cb.message.edit_reply_markup(
            reply_markup=kb.order_actions_kb(order_id, renew_price, paused=paused,
                                             is_trial=False, armed=new_armed))
    except Exception:  # noqa: BLE001
        pass


@storefront_router.callback_query(F.data.startswith("sfrenewok:"))
@storefront_router.callback_query(F.data.startswith("sfrenew:"))
async def sf_renew_legacy(cb: CallbackQuery, bot: Bot) -> None:
    """Legacy immediate-renew buttons (cards/notices sent before auto-renew shipped). Immediate renew
    is gone — a customer who wants more volume buys a new config, and continuity is handled by
    auto-renew — so route an old «🔄 تمدید» tap into ARMING one-shot auto-renew, with a short note."""
    order_id = int((cb.data or "").split(":")[1])
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
        if sf.shop_closed:
            await cb.answer()
            await cb.message.answer(rtl(_shop_closed_text(sf)))
            return
        sf_id = sf.id
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        renew_price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        already = storefront_autorenew.is_armed(order)
    await cb.answer()
    if already:
        await cb.message.answer(rtl("🔁 «تمدید خودکار» از قبل برای این سرویس روشن است."))
        return
    await cb.message.answer(rtl(
        "ℹ️ تمدید اکنون «خودکار» است: به‌جای تمدیدِ زودهنگام (که حجمِ باقی‌ماندهٔ شما را از بین می‌برد)، "
        "با روشن‌کردنِ آن، سرویس درست پیش از اتمام یک‌بار خودکار تمدید می‌شود."))
    async with SessionLocal() as s:
        res = await storefront_autorenew.arm(s, order_id, expected_sf_id=sf_id)
    await _report_arm(cb, res, renew_price)


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
        sf_id = sf.id
    res = await storefront_subscription.set_enabled(
        SessionLocal, order_id=order_id, enabled=enable, expected_sf_id=sf_id)
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
        rtl("🗑 از حذفِ این سرویس مطمئن هستید؟ سرویس از پنل حذف می‌شود و مبلغی بازگردانده نمی‌شود."),
        reply_markup=kb.confirm_kb(rtl("🗑 بله، حذف شود"), f"sfdelok:{order_id}"))
    await cb.answer()


@storefront_router.callback_query(F.data.startswith("sfdelok:"))
async def sf_delete_ok(cb: CallbackQuery, bot: Bot) -> None:
    order_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, _ = await _resolve(s, bot, cb.from_user)
        if sf is None or await _owned_order(s, sf, cb.from_user, order_id) is None:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        sf_id = sf.id
    await cb.answer()
    res = await storefront_subscription.delete_subscription(
        SessionLocal, order_id=order_id, expected_sf_id=sf_id)
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


def _customer_detail_view(cust: StorefrontCustomer) -> tuple[str, object]:
    """(text, markup) for the admin customer-detail card. HTML (name escaped). Contacting the
    customer is a bot-relay button (see `customer_detail_kb`), so this view has no fragile tg://
    deep link and always renders — it opens for EVERY customer, username or not."""
    lines = [f"👤 {html.escape(cust.name or '—')}", f"🆔 {cust.telegram_id}",
             f"👛 موجودی: {_toman(cust.wallet_balance_toman)} تومان"]
    if cust.banned:
        lines.append("⛔️ این مشتری مسدود است")
    markup = kb.customer_detail_kb(
        cust.id, username=cust.username, chat_id=cust.telegram_id, banned=cust.banned)
    return rtl("\n".join(lines)), markup


async def _show_customer_detail(cb: CallbackQuery, cust: StorefrontCustomer) -> None:
    """Render the customer-detail card by editing in place, falling back to a fresh message."""
    text, markup = _customer_detail_view(cust)
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        if "message is not modified" in str(exc):
            return
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")


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
        await _show_customer_detail(cb, cust)
    await cb.answer()


async def _set_customer_banned(cb: CallbackQuery, bot: Bot, *, banned: bool) -> None:
    cid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=cb.from_user.id, actor_role="admin", source="bot",
            idempotency_key=f"tg-ban:{cb.id}", expected_version=1,
            correlation_id=f"tg-ban:{cb.id}")
        try:
            await storefront_admin.set_customer_banned(
                s, sf.id, cid, ctx, banned=banned, reason="اعمال از طریق ربات")
        except storefront_admin.AdminCommandError as exc:
            await cb.answer("یافت نشد." if exc.code == "not_found" else "ناموفق بود.",
                            show_alert=True)
            return
        cust = await s.get(StorefrontCustomer, cid)
        if cust is not None:
            await _show_customer_detail(cb, cust)
    await cb.answer("مسدود شد." if banned else "رفعِ مسدودی شد.")


@storefront_router.callback_query(F.data.startswith("sfcustban:"))
async def sf_customer_ban(cb: CallbackQuery, bot: Bot) -> None:
    await _set_customer_banned(cb, bot, banned=True)


@storefront_router.callback_query(F.data.startswith("sfcustunban:"))
async def sf_customer_unban(cb: CallbackQuery, bot: Bot) -> None:
    await _set_customer_banned(cb, bot, banned=False)


# ── admin ⇄ customer relay (the shop bot forwards messages both ways) ─────────

@storefront_router.callback_query(F.data.startswith("sfmsg:"))
async def sf_dm_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Admin taps «پیام به مشتری» (from the detail card) or «پاسخ» (on a relayed customer message):
    start composing a message the bot will deliver to that customer."""
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
        name = cust.name or str(cust.telegram_id)
    await state.set_state(SF.dm_compose)
    await state.update_data(dm_customer_id=cid)
    await cb.message.answer(
        rtl(f"✍️ پیامی که می‌خواهید برای «{name}» فرستاده شود را بنویسید (متن یا عکس).\n"
            "پیام از طرفِ «پشتیبانیِ فروشگاه» برای مشتری ارسال می‌شود."),
        reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.dm_compose, F.text | F.photo)
async def sf_dm_send(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cid = data.get("dm_customer_id")
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        cust = await s.get(StorefrontCustomer, cid) if cid else None
        if cust is None or cust.storefront_bot_id != sf.id:
            await _reshow_admin_menu(message, "مشتری یافت نشد.", bot=bot)
            return
        target, cust_name = cust.telegram_id, (cust.name or str(cust.telegram_id))
    ok = await _relay_message(bot, target, "📨 پیام از پشتیبانیِ فروشگاه:", message)
    if ok:
        await _reshow_admin_menu(message, f"✅ پیام برای «{cust_name}» ارسال شد.", bot=bot)
    else:
        await _reshow_admin_menu(
            message,
            f"❌ ارسالِ پیام به «{cust_name}» ناموفق بود — احتمالاً ربات را مسدود کرده "
            "یا هنوز /start را نزده است.", bot=bot)


@storefront_router.callback_query(F.data == "sfcustsearch")
async def sf_customers_search_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.cust_search)
    await cb.message.answer(rtl("نام یا شناسهٔ عددیِ مشتری را بفرستید:"), reply_markup=kb.flow_cancel_kb())
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
        await _reshow_admin_menu(message, "مشتری‌ای یافت نشد.", bot=bot)
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
    lines = [f"📦 {name}", f"پلن: {gb} گیگابایت · {days} روز"]
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
        sf_id = sf.id
        if order.is_trial:
            await cb.answer()
            await cb.message.answer(rtl(_TRIAL_NO_RENEW))
            return
    await cb.answer()
    res = await storefront_subscription.renew(
        SessionLocal, order_id=oid, by_admin=True, expected_sf_id=sf_id)
    await cb.message.answer(
        rtl(f"✅ تمدید شد — {res.gb} گیگابایت · {res.days} روز." if res.ok else "❌ تمدید ناموفق بود."))


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
        sf_id = sf.id
    res = await storefront_subscription.set_enabled(
        SessionLocal, order_id=oid, enabled=enable, expected_sf_id=sf_id)
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
        sf_id = sf.id
    await cb.answer()
    res = await storefront_subscription.delete_subscription(
        SessionLocal, order_id=oid, expected_sf_id=sf_id)
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
            f"شما {len(pending)} شارژِ در انتظارِ تأیید دارید؛ لطفاً تا بررسیِ آن‌ها منتظر بمانید."))
        await cb.answer()
        return
    await state.set_state(SF.topup_amount)
    await cb.message.answer(rtl("مبلغی که می‌خواهید شارژ کنید (تومان) را بفرستید:"),
                            reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(StateFilter(SF.topup_amount, SF.topup_choice), F.text)
async def sf_topup_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    """Capture the top-up amount, then park on the inline buttons.

    Also accepted from `topup_choice`, so a customer who mistyped can simply send the corrected
    number instead of cancelling and starting the whole top-up again."""
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک مبلغِ معتبر (به تومان) وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            await state.clear()
            return
        has_codes = await storefront_credit.has_enabled_codes(s, sf.id)
        pay_kb = kb.pay_methods_kb(sf)
    await state.update_data(topup_amount=int(raw), topup_code_id=None, topup_bonus=0)
    # PARK, never clear: `set_state(None)` here made the re-dock middleware think the top-up had
    # finished and greet the customer with the shop's welcome text mid-flow.
    await state.set_state(SF.topup_choice)
    if has_codes:  # offer the credit-code step (no dead-end when the shop has none)
        await message.answer(
            rtl(f"مبلغِ شارژ: {_toman(int(raw))} تومان\nاگر کدِ شارژ دارید، آن را وارد کنید:"),
            reply_markup=kb.topup_code_prompt_kb())
    else:
        await message.answer(rtl("روشِ پرداخت را انتخاب کنید:"), reply_markup=pay_kb)


@storefront_router.callback_query(F.data == "sftopcode")
async def sf_topup_code_prompt(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(SF.topup_code)
    await cb.message.answer(rtl("کدِ شارژ را بفرستید:"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.callback_query(F.data == "sftopnocode")
async def sf_topup_nocode(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    # Stay PARKED (not None) — clearing here fired the callback re-dock middleware, which greeted
    # the customer with the shop welcome text right after they tapped «کد ندارم».
    await state.set_state(SF.topup_choice)
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
    if sf is None:
        await cb.answer()
        return
    await cb.message.answer(rtl("روشِ پرداخت را انتخاب کنید:"), reply_markup=kb.pay_methods_kb(sf))
    await cb.answer()


@storefront_router.message(SF.topup_code, F.text)
async def sf_topup_code_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    code_str = (message.text or "").strip()
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            await state.clear()
            return
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        q = await storefront_credit.quote(s, sf.id, code_str, topup_toman=amount, customer_id=customer.id)
        pay_kb = kb.pay_methods_kb(sf)
    if q.ok:
        await state.set_state(SF.topup_choice)   # parked on the payment-method buttons, not finished
        await state.update_data(topup_code_id=q.code_id, topup_bonus=q.bonus_toman)
        await message.answer(rtl(
            f"✅ کد پذیرفته شد: +{_toman(q.bonus_toman)} تومان پاداش.\n"
            f"مجموعِ اعتبار پس از تأیید: {_toman(amount + q.bonus_toman)} تومان.\n\n"
            "روشِ پرداخت را انتخاب کنید:"), reply_markup=pay_kb)
    else:
        await message.answer(
            rtl(f"❌ {_CREDIT_ERR.get(q.reason or '', 'کدِ واردشده معتبر نیست.')}"),
            reply_markup=kb.credit_skip_kb("sftopnocode", "➡️ ادامه بدون کد"))


@storefront_router.callback_query(F.data == "sfgift")
async def sf_gift_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(SF.gift_code)
    await cb.message.answer(rtl("کدِ هدیه را بفرستید:"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.gift_code, F.text)
async def sf_gift_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    code_str = (message.text or "").strip()
    await state.clear()
    async with SessionLocal() as s:
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        customer = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        cid = customer.id
        q = await storefront_wallet.apply_gift_code(s, cid, sf.id, code_str)
        bal = "?"
        if q.ok:
            c2 = await s.get(StorefrontCustomer, cid)
            bal = _toman(c2.wallet_balance_toman) if c2 else "?"
    if q.ok:
        await message.answer(rtl(
            f"🎁 کدِ هدیه اعمال شد: +{_toman(q.bonus_toman)} تومان.\nموجودیِ جدید: {bal} تومان."))
    else:
        await message.answer(rtl(f"❌ {_CREDIT_ERR.get(q.reason or '', 'کدِ هدیه معتبر نیست.')}"))


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
    bonus = int(data.get("topup_bonus") or 0)
    detail = {
        "card": f"کارت‌به‌کارت\n<code>{sf.card_number}</code>\nبه‌نامِ: {sf.card_holder or '—'}",
        "usdt": f"تتر USDT (شبکهٔ BEP-20)\n<code>{sf.usdt_address}</code>",
        "ton": f"گرام/تون (TON)\n<code>{sf.ton_address}</code>",
    }.get(method, "")
    bonus_line = (f"\n🎁 با کدِ شارژ، پس از تأیید {_toman(bonus)} تومان پاداش هم اضافه می‌شود "
                  f"(مجموع: {_toman(amount + bonus)} تومان)." if bonus else "")
    await state.update_data(topup_method=method)
    await state.set_state(SF.topup_proof)
    await cb.message.answer(rtl(
        f"💳 مبلغِ {_toman(amount)} تومان را به آدرسِ زیر واریز کنید:{bonus_line}\n\n{detail}\n\n"
        "سپس رسید (عکس) یا شناسهٔ تراکنش/متنِ واریز را همین‌جا بفرستید."),
        parse_mode="HTML", reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.topup_proof, F.photo | F.text)
async def sf_topup_proof(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    method = data.get("topup_method") or "card"
    code_id = data.get("topup_code_id")
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
        try:
            txn = await storefront_wallet.create_topup(
                s, customer, amount, method=method, proof_path=proof_path, txid=txid,
                credit_code_id=int(code_id) if code_id else None)
        except storefront_wallet.DuplicateTopupTxid:
            # F14: this crypto txid was already submitted for this shop — reject the replay.
            await message.answer(rtl(
                "این شناسهٔ تراکنش قبلاً ثبت شده است؛ اگر واریزی جدید دارید شناسهٔ همان را بفرستید."))
            return
        await message.answer(rtl(
            f"✅ درخواستِ شارژِ {_toman(amount)} تومان ثبت شد (#{txn.id}). پس از تأییدِ مدیر، "
            "کیفِ پولِ شما شارژ می‌شود."))
        # notify the admin via the same bot (with the proof)
        cap = (f"🧾 شارژِ جدید #{txn.id}\nمبلغ: {_toman(amount)} تومان — روش: {method}\n"
               f"مشتری: {customer.name or customer.telegram_id}"
               + (f"\nمتن/TXID: {txid}" if txid else ""))
        # Send the proof + decide buttons to the owner AND every co-admin (whoever acts first
        # settles it; the confirm/reject is idempotent so a second tap can't double-credit).
        from app.bot.handlers.common import portal_stable_url

        owner_id = reseller.bot_chat_id if reseller is not None else None
        owner_url = (await portal_stable_url(
            s, owner_id, with_login_token=True, next_path=f"/portal/storefront/{sf.id}/topups") if owner_id else None)
        for admin_id in _admin_chat_ids(reseller, sf):
            # Only the OWNER gets the portal deep-link (co-admins aren't portal principals).
            decide_kb = kb.topup_decide_kb(
                txn.id, portal_url=owner_url if admin_id == owner_id else None)
            try:
                if message.photo:  # forward by file_id — no disk read, no leaked file handle
                    await bot.send_photo(
                        admin_id, message.photo[-1].file_id,
                        caption=rtl(cap), reply_markup=decide_kb)
                else:
                    await bot.send_message(admin_id, rtl(cap), reply_markup=decide_kb)
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


_BOT_REJECT_REASON = "رد شده توسط مدیر از طریق ربات"


@storefront_router.callback_query(F.data.startswith("sfok:"))
async def sf_topup_ok(cb: CallbackQuery, bot: Bot) -> None:
    txn_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if not (is_admin and sf):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=cb.from_user.id, actor_role="admin", source="bot",
            idempotency_key=f"sf-topup:{cb.id}", expected_version=1, correlation_id=f"sf-topup:{cb.id}")
        try:
            result = await storefront_admin.set_topup_decision(s, sf.id, txn_id, ctx, decision="confirm")
        except storefront_admin.AdminCommandError:
            await _strip_buttons(cb)
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        body = result.body if isinstance(result.body, dict) else {}
        if not body.get("changed"):
            await _strip_buttons(cb)
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        txn = await s.get(StorefrontWalletTxn, txn_id)
        cust = await s.get(StorefrontCustomer, txn.customer_id) if txn else None
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
    changed = False
    cust = None
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if not (is_admin and sf):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=cb.from_user.id, actor_role="admin", source="bot",
            idempotency_key=f"sf-topup:{cb.id}", expected_version=1, correlation_id=f"sf-topup:{cb.id}")
        try:
            result = await storefront_admin.set_topup_decision(
                s, sf.id, txn_id, ctx, decision="reject", reason=_BOT_REJECT_REASON)
            body = result.body if isinstance(result.body, dict) else {}
            changed = bool(body.get("changed"))
            if changed:
                txn = await s.get(StorefrontWalletTxn, txn_id)
                cust = await s.get(StorefrontCustomer, txn.customer_id) if txn else None
        except storefront_admin.AdminCommandError:
            changed = False
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
    await state.update_data(confirm_txn=txn_id, sf_topup_key=f"sf-topup-amt:{cb.id}")
    await cb.message.answer(rtl("مبلغی که باید به کیفِ پولِ مشتری اضافه شود (تومان) را بفرستید:"),
                            reply_markup=kb.flow_cancel_kb())
    await cb.answer()


_BOT_CORRECTION_REASON = "مبلغ توسط مدیر از طریق ربات اصلاح شد"


@storefront_router.message(SF.confirm_amount, F.text)
async def sf_confirm_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    data = await state.get_data()
    txn_id = int(data.get("confirm_txn") or 0)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً مبلغِ معتبری وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    key = str(data.get("sf_topup_key") or f"sf-topup-amt:{message.message_id}")
    await state.clear()
    changed = False
    cust = None
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if not (is_admin and sf):
            return
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=message.from_user.id, actor_role="admin", source="bot",
            idempotency_key=key, expected_version=1, correlation_id=key)
        try:
            result = await storefront_admin.set_topup_decision(
                s, sf.id, txn_id, ctx, decision="confirm", corrected_amount=int(raw),
                reason=_BOT_CORRECTION_REASON)
            body = result.body if isinstance(result.body, dict) else {}
            changed = bool(body.get("changed"))
            if changed:
                txn = await s.get(StorefrontWalletTxn, txn_id)
                cust = await s.get(StorefrontCustomer, txn.customer_id) if txn else None
        except storefront_admin.AdminCommandError:
            changed = False
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
        sf, r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        # Resolved once here so the price prompt can show this shop's own floor without opening
        # another session mid-flow.
        cost = await storefront_pricing.cost_per_gb(s, r) if r is not None else 0
    # No title (owner: «عنوان نمی‌خواهیم») — collect volume → days → price only.
    await _start_admin_fsm(state, sf)
    await state.update_data(p_cost=cost)
    await state.set_state(SF.plan_gb)
    await cb.message.answer(rtl("حجم به گیگابایت (عدد):"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.plan_gb, F.text)
async def sf_plan_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.update_data(p_gb=int(raw))
    await state.set_state(SF.plan_days)
    await message.answer(rtl("مدت به روز (عدد):"), reply_markup=kb.flow_cancel_kb())


@storefront_router.message(SF.plan_days, F.text)
async def sf_plan_days(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.update_data(p_days=int(raw))
    data = await state.get_data()
    await state.set_state(SF.plan_price)
    await message.answer(
        rtl(_price_prompt("قیمتِ فروش به تومان (عدد):", data, "p_cost", "p_gb")),
        reply_markup=kb.flow_cancel_kb())


@storefront_router.message(SF.plan_price, F.text)
async def sf_plan_price(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.create_plan(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf), title="",
                gb=int(data.get("p_gb", 0)), days=int(data.get("p_days", 0)),
                price_toman=int(raw))
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "below_cost":
                # Recoverable by retyping — keep the reseller in the price prompt instead of
                # dumping them back to the plan list and making them restart the whole flow.
                await _rotate_command_key(state)
                await message.answer(rtl(_admin_error(exc)), reply_markup=kb.flow_cancel_kb())
                return
            await state.clear()
            plans = await storefront.list_plans(s, sf.id)
            await message.answer(rtl(_admin_error(exc)), reply_markup=kb.plans_manage_kb(plans))
            return
        plans = await storefront.list_plans(s, sf.id)
    await state.clear()
    await message.answer(rtl("✅ پلن اضافه شد."), reply_markup=kb.plans_manage_kb(plans))


@storefront_router.callback_query(F.data.startswith("sfplandel:"))
async def sf_plan_del(cb: CallbackQuery, bot: Bot) -> None:
    plan_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        try:
            await storefront_admin.delete_plan(s, sf.id, plan_id, _callback_ctx(cb, sf))
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "config_conflict":
                plans = await storefront.list_plans(s, sf.id)
                await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        plans = await storefront.list_plans(s, sf.id)
    await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
    await cb.answer("حذف شد.")


@storefront_router.callback_query(F.data.startswith("sfplantoggle:"))
async def sf_plan_toggle(cb: CallbackQuery, bot: Bot) -> None:
    """Enable/disable a plan from the bot. The desired state travels in the callback data (not
    "flip whatever it is now") so a stale keyboard can't invert the reseller's intent."""
    _, raw_id, raw_enabled = (cb.data or "").split(":")
    plan_id, enabled = int(raw_id), raw_enabled == "1"
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        try:
            await storefront_admin.set_plan_enabled(
                s, sf.id, plan_id, _callback_ctx(cb, sf), enabled=enabled)
        except storefront_admin.AdminCommandError as exc:
            plans = await storefront.list_plans(s, sf.id)
            if exc.code in ("config_conflict", "below_cost"):
                await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
            if exc.code == "below_cost":
                # Too long for a callback alert, and it is the actionable part — send it as a
                # message so the reseller can read the floor and fix the price.
                await cb.answer()
                await cb.message.answer(rtl(_admin_error(exc)))
                return
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        plans = await storefront.list_plans(s, sf.id)
    await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
    await cb.answer("فعال شد." if enabled else "غیرفعال شد.")


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
        plans = await storefront.list_plans(s, sf.id)
        ids = [plan.id for plan in plans]
        try:
            position = ids.index(plan_id)
        except ValueError:
            await cb.answer("پلن یافت نشد.", show_alert=True)
            return
        swap = position - 1 if direction == "up" else position + 1
        moved = 0 <= swap < len(ids)
        if moved:
            ids[position], ids[swap] = ids[swap], ids[position]
            try:
                await storefront_admin.reorder_plans(
                    s, sf.id, _callback_ctx(cb, sf), ordered_ids=ids)
            except storefront_admin.AdminCommandError as exc:
                if exc.code == "config_conflict":
                    plans = await storefront.list_plans(s, sf.id)
                    await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
                await cb.answer(_admin_error(exc), show_alert=True)
                return
            plans = await storefront.list_plans(s, sf.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.plans_manage_kb(plans))
    except Exception:  # noqa: BLE001 — "not modified" at an edge → ignore
        pass
    await cb.answer("جابه‌جا شد." if moved else "تغییری نکرد.")


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
        cur = f"{plan.gb} گیگابایت · {plan.days} روز · {plan.price_toman:,} تومان"
        cost = await storefront_pricing.cost_per_gb(s, _r) if _r is not None else 0
    await state.set_state(SF.edit_gb)
    await _start_admin_fsm(state, sf)
    await state.update_data(edit_plan_id=plan_id, e_cost=cost)
    await cb.message.answer(
        rtl(f"ویرایش پلن ({cur})\n\nحجمِ جدید به گیگابایت (عدد):"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.edit_gb, F.text)
async def sf_edit_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.update_data(e_gb=int(raw))
    await state.set_state(SF.edit_days)
    await message.answer(rtl("مدتِ جدید به روز (عدد):"), reply_markup=kb.flow_cancel_kb())


@storefront_router.message(SF.edit_days, F.text)
async def sf_edit_days(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.update_data(e_days=int(raw))
    data = await state.get_data()
    await state.set_state(SF.edit_price)
    await message.answer(
        rtl(_price_prompt("قیمتِ جدید به تومان (عدد):", data, "e_cost", "e_gb")),
        reply_markup=kb.flow_cancel_kb())


@storefront_router.message(SF.edit_price, F.text)
async def sf_edit_price(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    data = await state.get_data()
    plan_id = int(data.get("edit_plan_id", 0))
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.update_plan(
                s, sf.id, plan_id, _fsm_ctx(message.from_user.id, data, sf),
                gb=int(data.get("e_gb", 0)), days=int(data.get("e_days", 0)),
                price_toman=int(raw))
            ok = True
        except storefront_admin.AdminCommandError as exc:
            ok = False
            if exc.code == "below_cost":
                await _rotate_command_key(state)
                await message.answer(rtl(_admin_error(exc)), reply_markup=kb.flow_cancel_kb())
                return
            if exc.code != "not_found":
                await state.clear()
                plans = await storefront.list_plans(s, sf.id)
                await message.answer(
                    rtl(_admin_error(exc)), reply_markup=kb.plans_manage_kb(plans))
                return
        plans = await storefront.list_plans(s, sf.id)
    await state.clear()
    await message.answer(
        rtl("✅ پلن ویرایش شد." if ok else "پلن یافت نشد."),
        reply_markup=kb.plans_manage_kb(plans))


# ── admin: credit codes (کد شارژ) CRUD — a guided, mistake-proof wizard ─────────

@storefront_router.callback_query(F.data == "sfcredadd")
async def sf_credit_add(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        _sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.credit_code)
    await cb.message.answer(
        rtl("کدِ تخفیف را بفرستید (فقط حروف/عدد، مثلاً NOWRUZ):"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.credit_code, F.text)
async def sf_credit_code(message: Message, state: FSMContext, bot: Bot) -> None:
    code = storefront_credit.normalize_code(message.text)
    if not code or len(code) > 32 or not code.replace("_", "").isalnum():
        await message.answer(rtl("کدِ واردشده معتبر نیست. فقط حروف و عدد (۱ تا ۳۲ نویسه)."),
                             reply_markup=kb.flow_cancel_kb())
        return
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            await state.clear()
            return
        if await storefront_credit.code_exists(s, sf.id, code):
            await message.answer(rtl("این کد قبلاً وجود دارد؛ کدِ دیگری بفرستید."),
                                 reply_markup=kb.flow_cancel_kb())
            return
    await state.update_data(c_code=code)
    await state.set_state(SF.credit_choice)   # parked on the type buttons — the wizard continues
    await message.answer(rtl("نوعِ کد را انتخاب کنید:"), reply_markup=kb.credit_type_kb())


@storefront_router.callback_query(F.data.startswith("sfcredtype:"))
async def sf_credit_type(cb: CallbackQuery, state: FSMContext) -> None:
    t = cb.data.split(":")[1]
    await state.update_data(c_kind=("percent" if t == "percent" else "fixed"), c_is_gift=(t == "gift"))
    await state.set_state(SF.credit_value)
    if t == "percent":
        await cb.message.answer(rtl("درصدِ تخفیف (عددِ ۱ تا ۱۰۰):"), reply_markup=kb.flow_cancel_kb())
    else:
        await cb.message.answer(
            rtl("مبلغِ هدیه به تومان:" if t == "gift" else "مبلغِ پاداش به تومان:"),
            reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.credit_value, F.text)
async def sf_credit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    val = int(raw)
    if data.get("c_kind") == "percent":
        if val > 100:
            await message.answer(rtl("درصد باید بین ۱ تا ۱۰۰ باشد."), reply_markup=kb.flow_cancel_kb())
            return
        await state.update_data(c_percent=val)
        await state.set_state(SF.credit_maxbonus)
        await message.answer(
            rtl("سقفِ حداکثرِ تخفیف به تومان (مثلاً ۵۰۰۰۰). اگر سقف نمی‌خواهید «بدون سقف»:"),
            reply_markup=kb.credit_skip_kb("sfcredskip:maxbonus", "بدون سقف"))
    else:
        await state.update_data(c_amount=val)
        if data.get("c_is_gift"):
            await _credit_ask_maxuses(message.answer, state)   # gift: no min-topup
        else:
            await _credit_ask_mintopup(message.answer, state)


@storefront_router.message(SF.credit_maxbonus, F.text)
async def sf_credit_maxbonus(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید یا «بدون سقف»:"),
                             reply_markup=kb.credit_skip_kb("sfcredskip:maxbonus", "بدون سقف"))
        return
    await state.update_data(c_maxbonus=int(raw))
    await _credit_ask_mintopup(message.answer, state)


async def _credit_ask_mintopup(ans, state: FSMContext) -> None:  # noqa: ANN001
    await state.set_state(SF.credit_mintopup)
    await ans(rtl("حداقلِ مبلغِ شارژ برای فعال‌شدنِ کد (تومان). اگر نیست «بدون حداقل»:"),
              reply_markup=kb.credit_skip_kb("sfcredskip:mintopup", "بدون حداقل"))


@storefront_router.message(SF.credit_mintopup, F.text)
async def sf_credit_mintopup(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit():
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید یا «بدون حداقل»:"),
                             reply_markup=kb.credit_skip_kb("sfcredskip:mintopup", "بدون حداقل"))
        return
    await state.update_data(c_mintopup=int(raw))
    await _credit_ask_maxuses(message.answer, state)


async def _credit_ask_maxuses(ans, state: FSMContext) -> None:  # noqa: ANN001
    await state.set_state(SF.credit_maxuses)
    await ans(rtl("حداکثرِ تعدادِ کلِ استفاده (بینِ همهٔ مشتری‌ها). اگر نامحدود است «نامحدود»:"),
              reply_markup=kb.credit_skip_kb("sfcredskip:maxuses", "نامحدود"))


@storefront_router.message(SF.credit_maxuses, F.text)
async def sf_credit_maxuses(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید یا «نامحدود»:"),
                             reply_markup=kb.credit_skip_kb("sfcredskip:maxuses", "نامحدود"))
        return
    await state.update_data(c_maxuses=int(raw))
    await _credit_ask_percustomer(message.answer, state)


async def _credit_ask_percustomer(ans, state: FSMContext) -> None:  # noqa: ANN001
    await state.set_state(SF.credit_choice)   # parked on the per-customer buttons, still mid-wizard
    await ans(rtl("هر مشتری چند بار بتواند از این کد استفاده کند؟"),
              reply_markup=kb.credit_percustomer_kb())


@storefront_router.callback_query(F.data.startswith("sfcredpc:"))
async def sf_credit_percustomer(cb: CallbackQuery, state: FSMContext) -> None:
    n = int(cb.data.split(":")[1])
    await state.update_data(c_percustomer=(None if n == 0 else n))
    await state.set_state(SF.credit_expiry)
    await cb.message.answer(
        rtl("این کد تا چند روزِ دیگر معتبر باشد؟ اگر انقضا ندارد «بدون انقضا»:"),
        reply_markup=kb.credit_skip_kb("sfcredskip:expiry", "بدون انقضا"))
    await cb.answer()


@storefront_router.message(SF.credit_expiry, F.text)
async def sf_credit_expiry(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً تعدادِ روزِ معتبری وارد کنید یا «بدون انقضا»:"),
                             reply_markup=kb.credit_skip_kb("sfcredskip:expiry", "بدون انقضا"))
        return
    await state.update_data(c_expiry_days=int(raw))
    await _credit_finalize(message.answer, state, bot, message.from_user)


@storefront_router.callback_query(F.data.startswith("sfcredskip:"))
async def sf_credit_skip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    step = cb.data.split(":")[1]
    if step == "maxbonus":
        await state.update_data(c_maxbonus=None)
        await _credit_ask_mintopup(cb.message.answer, state)
    elif step == "mintopup":
        await state.update_data(c_mintopup=0)
        await _credit_ask_maxuses(cb.message.answer, state)
    elif step == "maxuses":
        await state.update_data(c_maxuses=None)
        await _credit_ask_percustomer(cb.message.answer, state)
    elif step == "expiry":
        await state.update_data(c_expiry_days=None)
        await _credit_finalize(cb.message.answer, state, bot, cb.from_user)
    await cb.answer()


async def _credit_finalize(ans, state: FSMContext, bot: Bot, from_user) -> None:  # noqa: ANN001
    import datetime as _dt

    data = await state.get_data()
    await state.clear()
    expires_at = None
    if data.get("c_expiry_days"):
        expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=int(data["c_expiry_days"]))
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, from_user)
        if sf is None or not is_admin:
            return
        kind = data.get("c_kind", "fixed")
        ctx = _command_ctx(from_user.id, sf.config_version, f"sf-credit-create:{uuid.uuid4().hex}")
        try:
            result = await storefront_admin.create_credit(
                s, sf.id, ctx, code=data.get("c_code", ""), kind=kind,
                percent_off=data.get("c_percent") if kind == "percent" else None,
                amount_toman=data.get("c_amount") if kind == "fixed" else None,
                max_bonus_toman=data.get("c_maxbonus"),
                min_topup_toman=int(data.get("c_mintopup") or 0),
                is_gift=bool(data.get("c_is_gift")), max_uses=data.get("c_maxuses"),
                per_customer_limit=data.get("c_percustomer", 1), expires_at=expires_at,
            )
        except storefront_admin.AdminCommandError:
            await ans(rtl("❌ ساختِ کد ناموفق بود (شاید این کد قبلاً وجود دارد)."))
            return
        credit = result.body.get("credit", {}) if isinstance(result.body, dict) else {}
        codes = await storefront_credit.list_codes(s, sf.id)
        warn = ("\n⚠️ این کد سقفِ تخفیف ندارد؛ روی شارژِ بزرگ، پاداشِ زیادی می‌دهد."
                if kind == "percent" and not credit.get("max_bonus_toman") else "")
    await ans(rtl(f"✅ کدِ «{data.get('c_code', '')}» ساخته شد.{warn}"),
              reply_markup=kb.credit_codes_manage_kb(codes))


@storefront_router.callback_query(F.data.startswith("sfcredtog:"))
async def sf_credit_toggle(cb: CallbackQuery, bot: Bot) -> None:
    code_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        code = await storefront_credit.get_code(s, sf.id, code_id)
        if code is None:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        ctx = _callback_ctx(cb, sf)
        try:
            result = await storefront_admin.set_credit_enabled(
                s, sf.id, code_id, ctx, enabled=not code.enabled)
        except storefront_admin.AdminCommandError:
            await cb.answer("خطا در تغییرِ وضعیت.", show_alert=True)
            return
        credit = result.body.get("credit", {}) if isinstance(result.body, dict) else {}
        now_on = bool(credit.get("enabled"))
        codes = await storefront_credit.list_codes(s, sf.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.credit_codes_manage_kb(codes))
    except Exception:  # noqa: BLE001
        pass
    await cb.answer("فعال شد." if now_on else "غیرفعال شد.")


@storefront_router.callback_query(F.data.startswith("sfcreddel:"))
async def sf_credit_del(cb: CallbackQuery, bot: Bot) -> None:
    code_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        ctx = _callback_ctx(cb, sf)
        try:
            await storefront_admin.archive_credit(s, sf.id, code_id, ctx)
        except storefront_admin.AdminCommandError:
            await cb.answer("خطا در بایگانی.", show_alert=True)
            return
        codes = await storefront_credit.list_codes(s, sf.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.credit_codes_manage_kb(codes))
    except Exception:  # noqa: BLE001
        pass
    await cb.answer("بایگانی شد.")


@storefront_router.callback_query(F.data.startswith("sfcredusage:"))
async def sf_credit_usage(cb: CallbackQuery, bot: Bot) -> None:
    code_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        code = await storefront_credit.get_code(s, sf.id, code_id)
        if code is None:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        total, uniq, given = await storefront_credit.usage_summary(s, code.id)
        cap = f" از {code.max_uses}" if code.max_uses else ""
        codestr = code.code
    await cb.message.answer(rtl(
        f"📊 مصرفِ کدِ «{codestr}»\n"
        f"• دفعاتِ استفاده: {total}{cap}\n"
        f"• مشتریانِ یکتا: {uniq}\n"
        f"• مجموعِ اعتبارِ داده‌شده: {_toman(given)} تومان"))
    await cb.answer()


# ── admin: payment settings ───────────────────────────────────────────────────

@storefront_router.callback_query(F.data.startswith("sfpaytog:"))
async def sf_pay_toggle(cb: CallbackQuery, bot: Bot) -> None:
    parts = (cb.data or "").split(":")
    method = parts[1] if len(parts) > 1 else ""
    target = _absolute_toggle(cb.data)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if method not in {"card", "usdt", "ton"} or target is None:
            await cb.message.edit_reply_markup(reply_markup=kb.pay_settings_kb(sf))
            await cb.answer("این منو قدیمی شده بود و به‌روزرسانی شد؛ لطفاً دوباره انتخاب کنید.", show_alert=True)
            return
        desired, rendered_version = target
        try:
            await storefront_admin.update_payment(
                s, sf.id, _callback_ctx(cb, sf, rendered_version=rendered_version),
                **{f"{method}_enabled": desired})
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "config_conflict":
                await s.refresh(sf)
                await cb.message.edit_reply_markup(reply_markup=kb.pay_settings_kb(sf))
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        await cb.message.edit_reply_markup(reply_markup=kb.pay_settings_kb(sf))
    await cb.answer("به‌روزرسانی شد.")


@storefront_router.callback_query(F.data.startswith("sfpayset:"))
async def sf_pay_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    method = cb.data.split(":")[1]
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if sf is None or not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.pay_value)
    await _start_admin_fsm(state, sf)
    await state.update_data(pay_method=method, pay_step="card_number" if method == "card" else "addr")
    prompt = {
        "card": "شمارهٔ کارت را بفرستید:",
        "usdt": "آدرسِ کیفِ USDT (BEP-20) را بفرستید:",
        "ton": "آدرسِ کیفِ گرام/تون را بفرستید:",
    }[method]
    await cb.message.answer(rtl(prompt), reply_markup=kb.flow_cancel_kb())
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
            await state.update_data(pay_step="card_holder", pending_card_number=val)
            await message.answer(rtl("نامِ صاحبِ کارت را بفرستید:"), reply_markup=kb.flow_cancel_kb())
            return
        changes = (
            {"card_number": data.get("pending_card_number"), "card_holder": val}
            if method == "card" else {f"{method}_address": val}
        )
        try:
            await storefront_admin.update_payment(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf), **changes)
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await message.answer(rtl(_admin_error(exc)), reply_markup=kb.pay_settings_kb(sf))
            return
        await state.clear()
        await message.answer(rtl("✅ ذخیره شد."), reply_markup=kb.pay_settings_kb(sf))


# ── admin: free-trial settings ────────────────────────────────────────────────

@storefront_router.callback_query(F.data.startswith("sftrialtog"))
async def sf_trial_toggle(cb: CallbackQuery, bot: Bot) -> None:
    target = _absolute_toggle(cb.data)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if target is None:
            await cb.message.edit_reply_markup(reply_markup=kb.trial_settings_kb(sf))
            await cb.answer("این منو قدیمی شده بود و به‌روزرسانی شد؛ لطفاً دوباره انتخاب کنید.", show_alert=True)
            return
        desired, rendered_version = target
        try:
            await storefront_admin.update_trial(
                s, sf.id, _callback_ctx(cb, sf, rendered_version=rendered_version), enabled=desired)
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "config_conflict":
                await s.refresh(sf)
                await cb.message.edit_reply_markup(reply_markup=kb.trial_settings_kb(sf))
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        await cb.message.edit_reply_markup(reply_markup=kb.trial_settings_kb(sf))
    await cb.answer("فعال شد." if sf.free_trial_enabled else "غیرفعال شد.")


@storefront_router.callback_query(F.data == "sftrialset")
async def sf_trial_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if sf is None or not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.trial_gb)
    await _start_admin_fsm(state, sf)
    await cb.message.answer(rtl("حجمِ تستِ رایگان به گیگابایت (عدد):"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.trial_gb, F.text)
async def sf_trial_gb(message: Message, state: FSMContext) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.update_data(t_gb=int(raw))
    await state.set_state(SF.trial_days)
    await message.answer(rtl("مدتِ تستِ رایگان به روز (عدد):"), reply_markup=kb.flow_cancel_kb())


@storefront_router.message(SF.trial_days, F.text)
async def sf_trial_days(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً یک عددِ معتبر وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.update_trial(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf),
                gb=int(data.get("t_gb", 1)), days=int(raw))
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await s.refresh(sf)
            await message.answer(rtl(_admin_error(exc)), reply_markup=kb.trial_settings_kb(sf))
            return
        await state.clear()
        await message.answer(
            rtl(f"✅ تستِ رایگان: {sf.free_trial_gb} گیگابایت · {sf.free_trial_days} روز"),
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
    await state.update_data(adj_customer=int(cid), adj_sign=sign, adj_key=f"sf-adj:{cb.id}")
    await cb.message.answer(rtl(f"مبلغِ {'افزایش' if sign == '+' else 'کاهش'} (تومان) را بفرستید:"),
                            reply_markup=kb.flow_cancel_kb())
    await cb.answer()


_BOT_ADJUST_REASON = "تغییرِ دستیِ موجودی توسط مدیر از طریق ربات"


@storefront_router.message(SF.adjust_amount, F.text)
async def sf_adjust_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = _digits(message.text)
    data = await state.get_data()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(rtl("لطفاً مبلغِ معتبری وارد کنید."), reply_markup=kb.flow_cancel_kb())
        return
    await state.clear()
    signed = int(raw) * (1 if data.get("adj_sign") == "+" else -1)
    key = str(data.get("adj_key") or f"sf-adj:{message.message_id}")
    customer_id = int(data.get("adj_customer") or 0)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=message.from_user.id, actor_role="admin", source="bot",
            idempotency_key=key, expected_version=1, correlation_id=key)
        try:
            await storefront_admin.adjust_wallet(
                s, sf.id, customer_id, ctx, amount_toman_signed=signed, reason=_BOT_ADJUST_REASON)
        except storefront_admin.AdminCommandError:
            await message.answer(rtl("مشتری پیدا نشد."))
            return
        cust = await s.get(StorefrontCustomer, customer_id)
        if cust is not None:
            await message.answer(rtl(
                f"✅ موجودیِ «{cust.name or cust.telegram_id}» اکنون "
                f"{_toman(cust.wallet_balance_toman)} تومان است."))


@storefront_router.message(SF.support, F.text)
async def sf_support_set(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.update_messages(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf),
                support_contact=(message.text or "").strip())
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await _reshow_admin_menu(message, _admin_error(exc), bot=bot)
            return
    await state.clear()
    await _reshow_admin_menu(message, "✅ شناسهٔ پشتیبانی ذخیره شد.", bot=bot)


@storefront_router.message(SF.welcome, F.text)
async def sf_welcome_set(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.update_messages(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf),
                welcome_text=(message.text or "").strip() or None)
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await _reshow_admin_menu(message, _admin_error(exc), bot=bot)
            return
    await state.clear()
    await _reshow_admin_menu(message, "✅ پیامِ خوش‌آمد ذخیره شد.", bot=bot)


# ── admin: forced-join (channel membership) ───────────────────────────────────

async def _bot_is_channel_admin(bot: Bot, channel_id: str, bot_telegram_id: int | None) -> bool:  # noqa: ANN001
    """True iff this storefront bot is an admin of the channel (so it can both gate joins and read
    member status). A bot not in the channel → get_chat_member raises → treated as not-admin."""
    if not bot_telegram_id:
        return False
    try:
        m = await bot.get_chat_member(channel_id, bot_telegram_id)
        return m.status in ("administrator", "creator")
    except (TelegramNetworkError, TelegramRetryAfter) as exc:
        raise storefront_admin.AdminCommandError(
            "external_unknown", "Telegram verification outcome is unknown") from exc
    except Exception:  # noqa: BLE001
        return False


@storefront_router.callback_query(F.data == "sfjoinset")
async def sf_join_set(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if sf is None or not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await _start_admin_fsm(state, sf)
    await state.set_state(SF.join_channel)
    await cb.message.answer(rtl(
        f"یک پیام از کانالِ خود را همین‌جا فوروارد کنید (یا @username یا شناسهٔ -100… را بفرستید).\n"
        f"توجه: ربات (@{sf.bot_username or '—'}) باید در آن کانال ادمین باشد."),
        reply_markup=kb.flow_cancel_kb())
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
            "ورودیِ واردشده معتبر نبود. یک پیام از کانال را فوروارد کنید یا @username / شناسهٔ -100… را بفرستید."),
            reply_markup=kb.flow_cancel_kb())
        return
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            await state.clear()
            return
        bot_tg = sf.bot_telegram_id
        ctx = _fsm_ctx(message.from_user.id, data, sf)
        try:
            claim = await storefront_admin.claim_external_channel(
                s, sf.id, ctx, channel_id=channel_id)
        except storefront_admin.AdminCommandError as exc:
            if exc.code == "config_conflict":
                await state.update_data(
                    sf_config_version=exc.current_version or sf.config_version,
                    sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
                )
            await message.answer(rtl(_admin_error(exc)), reply_markup=kb.flow_cancel_kb())
            return
    if isinstance(claim, storefront_admin.CommandResult):
        if claim.response_status >= 400:
            await state.update_data(
                sf_config_version=claim.config_version,
                sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
            )
            await message.answer(rtl(
                "بررسیِ قبلیِ کانال ناموفق بود؛ وضعیت ادمین‌بودن ربات را اصلاح کنید و دوباره بفرستید."),
                reply_markup=kb.flow_cancel_kb())
            return
        await state.clear()
        async with SessionLocal() as s:
            sf, _r, _a = await _resolve(s, bot, message.from_user)
            if sf is None:
                return
            markup = kb.join_settings_kb(sf)
        await _reshow_admin_menu(message, "✅ کانال قبلاً ثبت شده است.", bot=bot)
        await message.answer(rtl("🔒 عضویت اجباری در کانال"), reply_markup=markup)
        return
    try:
        is_channel_admin = await _bot_is_channel_admin(bot, channel_id, bot_tg)
    except storefront_admin.AdminCommandError as exc:
        async with SessionLocal() as s:
            await storefront_admin.mark_external_channel_unknown(
                s, claim, ctx, error_class=exc.code)
        await state.update_data(
            sf_config_version=claim.expected_version,
            sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
        )
        await message.answer(rtl(
            "نتیجه بررسی تلگرام نامشخص ماند؛ چند لحظه بعد دوباره تلاش کنید."),
            reply_markup=kb.flow_cancel_kb())
        return
    if not is_channel_admin:
        async with SessionLocal() as s:
            await storefront_admin.finalize_verified_channel(
                s, claim, ctx, verified=False, resolved_link=None)
        await state.update_data(
            sf_config_version=claim.expected_version,
            sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
        )
        await message.answer(rtl(
            "ابتدا ربات را در کانالِ خود ادمین کنید، سپس دوباره پیام را فوروارد کنید."),
            reply_markup=kb.flow_cancel_kb())
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
    except (TelegramNetworkError, TelegramRetryAfter) as exc:
        async with SessionLocal() as s:
            await storefront_admin.mark_external_channel_unknown(
                s, claim, ctx, error_class="external_unknown")
        await state.update_data(
            sf_config_version=claim.expected_version,
            sf_command_key=f"tg-fsm:{secrets.token_urlsafe(18)}",
        )
        await message.answer(rtl(_admin_error(storefront_admin.AdminCommandError(
            "external_unknown", str(exc)))), reply_markup=kb.flow_cancel_kb())
        return
    except Exception:  # noqa: BLE001 — link is optional; the gate still works via the check button
        link = None
    async with SessionLocal() as s:
        try:
            await storefront_admin.finalize_verified_channel(
                s, claim, ctx, verified=True, resolved_link=link)
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await _reshow_admin_menu(message, _admin_error(exc), bot=bot)
            return
        sf, _r, _a = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        markup = kb.join_settings_kb(sf)
    await state.clear()
    await _reshow_admin_menu(message, "✅ کانال ثبت و عضویت اجباری فعال شد.", bot=bot)
    await message.answer(rtl("🔒 عضویت اجباری در کانال"), reply_markup=markup)


@storefront_router.callback_query(F.data.startswith("sfjointog"))
async def sf_join_toggle(cb: CallbackQuery, bot: Bot) -> None:
    claim: storefront_admin.ChannelVerification | storefront_admin.CommandResult | None = None
    ctx: storefront_admin.CommandContext | None = None
    channel_id: str | None = None
    bot_tg: int | None = None
    existing_link: str | None = None
    target = _absolute_toggle(cb.data)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if target is None:
            await cb.message.edit_reply_markup(reply_markup=kb.join_settings_kb(sf))
            await cb.answer("این منو قدیمی شده بود و به‌روزرسانی شد؛ لطفاً دوباره انتخاب کنید.", show_alert=True)
            return
        desired, rendered_version = target
        if desired:  # turning ON → verify through the durable external command flow
            channel_id, bot_tg, existing_link = sf.channel_id, sf.bot_telegram_id, sf.channel_link
            ctx = _callback_ctx(cb, sf, rendered_version=rendered_version)
            try:
                claim = await storefront_admin.claim_external_channel_enable(s, sf.id, ctx)
            except storefront_admin.AdminCommandError as exc:
                await cb.answer(_admin_error(exc), show_alert=True)
                return
            if isinstance(claim, storefront_admin.ChannelVerification):
                channel_id = claim.channel_id
        else:
            try:
                await storefront_admin.set_channel_required(
                    s, sf.id, _callback_ctx(cb, sf, rendered_version=rendered_version),
                    required=False)
            except storefront_admin.AdminCommandError as exc:
                await cb.answer(_admin_error(exc), show_alert=True)
                return
            await cb.message.edit_reply_markup(reply_markup=kb.join_settings_kb(sf))
    if not desired:
        await cb.answer("غیرفعال شد.")
        return
    if isinstance(claim, storefront_admin.CommandResult):
        if claim.response_status >= 400:
            await cb.answer("بررسیِ قبلیِ کانال ناموفق بود؛ دوباره تلاش کنید.", show_alert=True)
            return
        markup = None
        async with SessionLocal() as s:
            sf, _r, _a = await _resolve(s, bot, cb.from_user)
            if sf is not None:
                markup = kb.join_settings_kb(sf)
        if markup is not None:
            await cb.message.edit_reply_markup(reply_markup=markup)
        await cb.answer("فعال شد.")
        return
    if claim is None or ctx is None or channel_id is None:
        await cb.answer("خطا در بررسی کانال.", show_alert=True)
        return
    try:
        verified = await _bot_is_channel_admin(bot, channel_id, bot_tg)
    except storefront_admin.AdminCommandError as exc:
        async with SessionLocal() as s:
            await storefront_admin.mark_external_channel_unknown(
                s, claim, ctx, error_class=exc.code)
        await cb.answer("نتیجه بررسی تلگرام نامشخص ماند؛ دوباره تلاش کنید.", show_alert=True)
        return
    if not verified:
        async with SessionLocal() as s:
            await storefront_admin.finalize_verified_channel(
                s, claim, ctx, verified=False, resolved_link=None)
        await cb.answer("ابتدا ربات را در کانال ادمین کنید.", show_alert=True)
        return
    link = existing_link
    if not link:
        try:
            full = await bot.get_chat(channel_id)
            if getattr(full, "username", None):
                link = f"https://t.me/{full.username}"
            else:
                invite = await bot.create_chat_invite_link(channel_id)
                link = invite.invite_link
        except (TelegramNetworkError, TelegramRetryAfter):
            async with SessionLocal() as s:
                await storefront_admin.mark_external_channel_unknown(
                    s, claim, ctx, error_class="external_unknown")
            await cb.answer("نتیجه ساخت لینک نامشخص ماند؛ دوباره تلاش کنید.", show_alert=True)
            return
        except Exception:  # noqa: BLE001 - link remains optional
            link = None
    markup = None
    async with SessionLocal() as s:
        try:
            await storefront_admin.finalize_verified_channel(
                s, claim, ctx, verified=True, resolved_link=link)
        except storefront_admin.AdminCommandError as exc:
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        sf, _r, _a = await _resolve(s, bot, cb.from_user)
        if sf is not None:
            markup = kb.join_settings_kb(sf)
    if markup is not None:
        await cb.message.edit_reply_markup(reply_markup=markup)
    await cb.answer("فعال شد.")


@storefront_router.callback_query(F.data.startswith("sfshoptog"))
async def sf_shop_toggle(cb: CallbackQuery, bot: Bot) -> None:
    """Admin flips «shop temporarily closed» on/off."""
    target = _absolute_toggle(cb.data)
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if target is None:
            await cb.message.edit_reply_markup(reply_markup=kb.shop_state_kb(sf))
            await cb.answer("این منو قدیمی شده بود و به‌روزرسانی شد؛ لطفاً دوباره انتخاب کنید.", show_alert=True)
            return
        now_closed, rendered_version = target
        try:
            await storefront_admin.update_shop_state(
                s, sf.id, _callback_ctx(cb, sf, rendered_version=rendered_version),
                closed=now_closed)
        except storefront_admin.AdminCommandError as exc:
            await cb.answer(_admin_error(exc), show_alert=True)
            return
        await cb.message.edit_reply_markup(reply_markup=kb.shop_state_kb(sf))
    await cb.answer("فروشگاه بسته شد." if now_closed else "فروشگاه باز شد.")


@storefront_router.callback_query(F.data == "sfshopmsg")
async def sf_shop_msg(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Admin edits the message customers see while the shop is closed."""
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
    if sf is None or not is_admin:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(SF.closed_text)
    await _start_admin_fsm(state, sf)
    await cb.message.answer(rtl(
        "متنِ پیامی که هنگامِ بسته‌بودنِ فروشگاه به مشتری نشان داده می‌شود را بفرستید.\n"
        f"فعلی: {sf.closed_text or _SHOP_CLOSED_DEFAULT}"), reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.closed_text, F.text)
async def sf_closed_set(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        try:
            await storefront_admin.update_shop_state(
                s, sf.id, _fsm_ctx(message.from_user.id, data, sf),
                closed_text=(message.text or "").strip() or None)
        except storefront_admin.AdminCommandError as exc:
            await state.clear()
            await s.refresh(sf)
            await message.answer(rtl(_admin_error(exc)), reply_markup=kb.shop_state_kb(sf))
            return
    await state.clear()
    await _reshow_admin_menu(message, "✅ پیامِ بسته‌بودنِ فروشگاه ذخیره شد.", bot=bot)


@storefront_router.callback_query(F.data == "sfjoinclear")
async def sf_join_clear(cb: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        try:
            await storefront_admin.delete_channel(s, sf.id, _callback_ctx(cb, sf))
        except storefront_admin.AdminCommandError as exc:
            await cb.answer(_admin_error(exc), show_alert=True)
            return
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


_SEGMENT_FA = {
    "all": "همهٔ مشتری‌ها",
    "expired": "مشتریانِ منقضی‌شده",
    "inactive30": "غیرفعال در ۳۰ روز اخیر",
    "trial_no_purchase": "تستِ رایگان گرفته، بدونِ خرید",
}


@storefront_router.callback_query(F.data.startswith("sfbcseg:"))
async def sf_broadcast_segment(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Admin chose the broadcast audience → stash it, show the recipient count, ask for the text."""
    segment = cb.data.split(":", 1)[1]
    if segment not in _SEGMENT_FA:
        await cb.answer()
        return
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        n = len(await storefront.customers_in_segment(s, sf.id, segment))
    if n == 0:
        await state.clear()
        await cb.message.answer(rtl(f"در گروهِ «{_SEGMENT_FA[segment]}» مشتری‌ای نیست."))
    else:
        await state.set_state(SF.broadcast)
        await state.update_data(bc_segment=segment)
        await cb.message.answer(rtl(
            f"📢 گیرنده: {_SEGMENT_FA[segment]} ({n} نفر)\nاکنون متنِ پیام را بفرستید:"),
            reply_markup=kb.flow_cancel_kb())
    await cb.answer()


@storefront_router.message(SF.broadcast, F.text)
async def sf_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text or ""
    data = await state.get_data()
    segment = data.get("bc_segment") or "all"
    await state.clear()
    scope = _SEGMENT_FA.get(segment, "مشتری‌ها")
    async with SessionLocal() as s:
        sf, _r, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None or not is_admin:
            return
        # Durable enqueue: a job + a create-time recipient snapshot in ONE transaction. The scheduler
        # delivery worker sends them with flood control + retries; no fire-and-forget task remains, so
        # a bot restart mid-send never loses the broadcast.
        ctx = _command_ctx(message.from_user.id, sf.config_version,
                           f"sf-broadcast:{message.chat.id}:{message.message_id}")
        try:
            result = await storefront_admin.enqueue_broadcast(s, sf.id, ctx, segment=segment, text=text)
        except storefront_admin.AdminCommandError as exc:
            await _reshow_admin_menu(message, f"❌ ارسال ناموفق بود: {exc}", bot=bot)
            return
        total = (result.body.get("total") if isinstance(result.body, dict) else 0) or 0
    if not total:
        await _reshow_admin_menu(message, f"در گروهِ «{scope}» مشتری‌ای نیست.", bot=bot)
        return
    await _reshow_admin_menu(
        message,
        f"📢 پیام برای {total} نفر ({scope}) در صفِ ارسال قرار گرفت؛ به‌تدریج ارسال می‌شود.", bot=bot)


# ── compact owner home shortcuts (plan 007) ───────────────────────────────────

_OWNER_HOME_HELP = (
    "❓ راهنمای مدیریتِ فروشگاه\n\n"
    "مدیریتِ کاملِ فروشگاه در «پنلِ تحتِ وب» انجام می‌شود: پلن‌ها، روش‌های پرداخت، مشتری‌ها، "
    "شارژِ کیف‌پول، کدهای شارژ/هدیه، پیامِ همگانی و همهٔ تنظیمات.\n\n"
    "روی «🌐 مدیریت فروشگاه در مرورگر» بزنید تا بدون رمز واردِ پنل شوید (هر بار لینکِ تازه و "
    "یک‌بارمصرف است).\n\n"
    "در همین‌جا هم کارهای فوری در دسترس‌اند: «🧾 شارژهای در انتظار» برای تأییدِ سریعِ شارژها، "
    "«📊 آمار سریع» و «👤 نمای مشتری».\n\n"
    "برای گرفتنِ منو و لینکِ تازه، هر زمان /start را بزنید."
)


@storefront_router.callback_query(F.data.startswith("sfhome:"))
async def sf_home_cb(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Compact owner-home shortcuts. Reuses the from_user-safe admin actions (topups/stats); the
    preview is computed with the CALLER's Telegram id (delegating would use the bot's id)."""
    action = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, cb.from_user)
        if sf is None or not is_admin:
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if action == "topups":
            await _admin_action("topups", cb.message, state, s, sf, reseller)
        elif action == "stats":
            await _admin_action("stats", cb.message, state, s, sf, reseller)
        elif action == "preview":
            await state.update_data(sf_preview=True)
            preview = await storefront_admin.customer_preview(s, sf.id, cb.from_user.id, "bot")
            await _send_customer_preview(cb.message.answer, preview)
        elif action == "help":
            await cb.message.answer(rtl(_OWNER_HOME_HELP))
    await cb.answer()


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
            await _send_admin_menu(
                cb.message, sf, session=s, reseller=reseller, user_id=cb.from_user.id)
        else:
            cust = await storefront.get_or_create_customer(s, sf.id, cb.from_user)
            await _send_customer_menu(cb.message.answer, sf, cust)
    await cb.answer("لغو شد.")


@storefront_router.callback_query(F.data == "sfnoop")
async def sf_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ── fallback: any other message → re-show the menu (registered LAST = lowest priority) ──────────

@storefront_router.message()
async def sf_fallback(message: Message, state: FSMContext, bot: Bot) -> None:
    """A stray message that matched no command/label/FSM state.
      • MID-FLOW (a state is active but this message didn't match the flow's own handler, e.g. a wrong
        type) → the flow is LOCKED: re-prompt «✖️ انصراف» and keep the state, so nothing but cancel exits.
      • ADMIN (idle) → re-show the admin menu.
      • CUSTOMER (idle) → gently re-show the customer menu.

    NOTE — stray text is deliberately NOT relayed to the shop owner. It used to be: anything a
    customer typed (a typo, a stray "سلام", a tapped-then-abandoned thought) was forwarded to the
    reseller as a support ticket, which buried them in noise. Reaching support is what the explicit
    «💬 پشتیبانی» button is for — that flow sets `SF.support` and relays deliberately. A banned
    customer was already short-circuited by the middleware."""
    if await state.get_state() is not None:
        # Locked flow: don't clear, don't navigate — only «✖️ انصراف» (or /cancel) leaves.
        await message.answer(
            rtl("شما در حال یک عملیات هستید؛ برای خروج «✖️ انصراف» را بزنید."),
            reply_markup=kb.flow_cancel_kb(),
        )
        return
    async with SessionLocal() as s:
        sf, reseller, is_admin = await _resolve(s, bot, message.from_user)
        if sf is None:
            return
        if is_admin:
            await _send_admin_menu(
                message, sf, session=s, reseller=reseller, user_id=message.from_user.id)
            return
        cust = await storefront.get_or_create_customer(s, sf.id, message.from_user)
        await _send_customer_menu(message.answer, sf, cust)
