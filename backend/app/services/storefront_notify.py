"""Shop-admin notifications — «اطلاع‌رسانی فروش».

A storefront's owner used to learn nothing about their own shop: the only two events that ever
reached a shop admin were a customer submitting a wallet top-up proof and a provisioning FAILURE.
A sale, a renewal and a confirmed top-up now all produce a short card in the admin's Telegram.

Three rules make this safe to call from anywhere:

1. **It never participates in the caller's transaction.** `notify_shop_admins` opens its OWN
   session and is only ever awaited AFTER the money has committed. A Telegram outage must not roll
   back a sale, and a committed sale must not be blocked waiting on Telegram.
2. **The per-shop switch is read here and nowhere else.** `StorefrontBot.notify_admin_events` is
   checked inside `notify_shop_admins`, so no event site can add a message and forget the switch —
   which would leave a shop that muted notifications still receiving some of them.
3. **Every recipient is sent independently.** One admin who blocked the bot must not silence the
   others, exactly as the existing top-up fan-out does.

Deliberately NOT routed through `storefront_delivery`: that queue's recipient row carries a NOT
NULL FK to `storefront_customers`, its `kind` is CHECK-constrained, and every job feeds the
reseller's own campaign history and counters — an admin nudge would both fail to fit and pollute
the shop's broadcast statistics.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)

log = logging.getLogger(__name__)

# A notification is a courtesy, never a checkout step. If Telegram is slow, the caller (which has
# already committed) walks away rather than holding a request open.
_SEND_TIMEOUT_SECONDS = 10


def _toman(value: object) -> str:
    try:
        return f"{int(Decimal(str(value or 0))):,}"
    except Exception:  # noqa: BLE001 — a malformed amount must not break the message
        return "0"


def _customer_line(name: str | None, username: str | None, telegram_id: int | None) -> str:
    """Identify the customer the way the shop admin will recognise them: their display name, plus
    the @handle that actually lets the admin reach them. `rtl()` isolates the Latin run."""
    who = (name or "").strip() or (f"@{username}" if username else "") or str(telegram_id or "—")
    if username and who != f"@{username}":
        who = f"{who} (@{username})"
    return who


# ── message builders ──────────────────────────────────────────────────────────
# House voice (see `storefront_belowcost.notice_text_fa`): one leading emoji, a headline, then a
# short block of facts, one per line. No prices the admin did not set, no owner-side costs.

def purchase_text(
    *, plan_label: str, customer: str, price_toman: object, balance_toman: object,
) -> str:
    return (
        "🛒 فروشِ جدید\n\n"
        f"سرویس: {plan_label}\n"
        f"مشتری: {customer}\n"
        f"مبلغ: {_toman(price_toman)} تومان\n"
        f"موجودیِ کیفِ پولِ مشتری پس از خرید: {_toman(balance_toman)} تومان"
    )


def renewal_text(
    *, plan_label: str, customer: str, price_toman: object, automatic: bool,
) -> str:
    how = "تمدیدِ خودکار" if automatic else "تمدید توسطِ مدیر"
    return (
        "🔁 تمدیدِ سرویس\n\n"
        f"سرویس: {plan_label}\n"
        f"مشتری: {customer}\n"
        f"مبلغ: {_toman(price_toman)} تومان\n"
        f"نوع: {how}"
    )


def topup_text(
    *, customer: str, amount_toman: object, bonus_toman: object, balance_toman: object,
) -> str:
    bonus = int(Decimal(str(bonus_toman or 0)))
    bonus_line = f"\nپاداشِ کدِ شارژ: {_toman(bonus)} تومان" if bonus > 0 else ""
    return (
        "💰 شارژِ کیفِ پول تأیید شد\n\n"
        f"مشتری: {customer}\n"
        f"مبلغ: {_toman(amount_toman)} تومان{bonus_line}\n"
        f"موجودیِ جدید: {_toman(balance_toman)} تومان"
    )


# ── recipients ────────────────────────────────────────────────────────────────
async def admin_chat_ids(
    session: AsyncSession, shop: StorefrontBot, reseller: Reseller | None = None,
) -> list[int]:
    """Every Telegram id that manages this shop: the owning reseller, then its co-admins.

    The service-layer twin of `app.bot.storefront.handlers._admin_chat_ids`, which delegates here —
    "who is a shop admin" must have exactly one definition.
    """
    from app.services import storefront

    if reseller is None:
        reseller = await session.get(Reseller, shop.reseller_id)
    ids: list[int] = []
    if reseller is not None and reseller.bot_chat_id:
        ids.append(int(reseller.bot_chat_id))
    for tid in storefront.co_admin_ids(shop):
        if tid not in ids:
            ids.append(tid)
    return ids


def _build_bot(token: str):  # noqa: ANN202 — an aiogram Bot (or a test double)
    from aiogram import Bot

    from app.bot.session import new_session

    return Bot(token=token, session=new_session())


async def notify_shop_admins(
    *,
    shop_id: int,
    text: str,
    bot: object | None = None,
    exclude_chat_id: int | None = None,
    session_factory: object | None = None,
) -> None:
    """Best-effort DM to a shop's admins. Never raises, never blocks, never rolls anything back.

    `bot` is an already-open aiogram Bot for THIS shop when the caller has one (the bot process, the
    auto-renew sweep). Portal and scheduler paths pass none and a transient Bot is built from the
    shop's own stored token — the pattern `storefront_provision._notify_completed` already uses.

    `exclude_chat_id` drops the admin who performed the action: they are looking at the result, and
    telling them what they just did reads as noise. Its real job is the OTHER admins — a co-admin's
    or a portal-side decision otherwise stays invisible to everyone else.
    """
    from app.core.db import SessionLocal
    from app.services import storefront

    factory = session_factory or SessionLocal
    own_bot = None
    try:
        async with factory() as session:  # type: ignore[operator]
            shop = await session.get(StorefrontBot, shop_id)
            if shop is None or not shop.notify_admin_events:
                return
            targets = [
                chat_id for chat_id in await admin_chat_ids(session, shop)
                if chat_id != exclude_chat_id
            ]
            token = storefront.bot_token(shop) if bot is None else None
        if not targets:
            return
        sender = bot
        if sender is None:
            if not token:
                return
            sender = own_bot = _build_bot(token)
        await asyncio.wait_for(_fan_out(sender, targets, text), _SEND_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — a notification must never surface to the caller
        log.info("shop-admin notification failed (shop %s)", shop_id, exc_info=True)
    finally:
        if own_bot is not None:
            try:
                await own_bot.session.close()
            except Exception:  # noqa: BLE001
                pass


async def _fan_out(bot: object, chat_ids: list[int], text: str) -> None:
    from app.bot.rtl import rtl

    body = rtl(text)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, body)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — one blocked admin must not silence the others
            log.info("shop-admin notification not delivered to %s", chat_id, exc_info=True)


# ── event helpers ─────────────────────────────────────────────────────────────
# Each one loads its own facts, so a call site is a single line that cannot get the wording — or
# the switch — wrong. All of them swallow every error: they run AFTER the money is committed.

def _order_label(order: StorefrontOrder, plan: StorefrontPlan | None) -> str:
    """What was actually sold, named the way the shop names it.

    The NAME comes from the live plan row (it is the shop's own vocabulary and may have been
    edited since), but quota/duration/price come from the ORDER — that is what this customer
    bought, and a renewal or a repricing must not rewrite history in a notification.
    """
    from app.bot.storefront import keyboards

    return keyboards.plan_label(StorefrontPlan(
        title=(plan.title if plan is not None else "") or "",
        gb=int(order.gb or 0), days=int(order.days or 0),
        price_toman=int(order.price_toman or 0),
    ))


async def _order_facts(
    session: AsyncSession, order_id: int | None,
) -> tuple[StorefrontOrder, StorefrontCustomer, StorefrontPlan | None] | None:
    if not order_id:
        return None
    order = await session.get(StorefrontOrder, order_id)
    if order is None:
        return None
    customer = await session.get(StorefrontCustomer, order.customer_id)
    if customer is None:
        return None
    plan = await session.get(StorefrontPlan, order.plan_id) if order.plan_id else None
    return order, customer, plan


async def notify_purchase(
    *, sf_id: int, order_id: int | None, bot: object | None = None,
    session_factory: object | None = None,
) -> None:
    """A customer bought a plan. Free trials are deliberately excluded — the owner asked for sales,
    and a trial is a giveaway funded by the platform, not a shop event worth a DM."""
    from app.core.db import SessionLocal

    factory = session_factory or SessionLocal
    try:
        async with factory() as session:  # type: ignore[operator]
            facts = await _order_facts(session, order_id)
            if facts is None:
                return
            order, customer, plan = facts
            if order.is_trial:
                return
            text = purchase_text(
                plan_label=_order_label(order, plan),
                customer=_customer_line(customer.name, customer.username, customer.telegram_id),
                price_toman=order.price_toman,
                balance_toman=customer.wallet_balance_toman,
            )
    except Exception:  # noqa: BLE001
        log.info("purchase notification could not be built (order %s)", order_id, exc_info=True)
        return
    await notify_shop_admins(
        shop_id=sf_id, text=text, bot=bot, session_factory=session_factory)


async def notify_renewal(
    *, sf_id: int, order_id: int | None, automatic: bool, bot: object | None = None,
    exclude_chat_id: int | None = None, session_factory: object | None = None,
) -> None:
    """A service was renewed — by the auto-renew sweep, or by an admin from the bot or the portal."""
    from app.core.db import SessionLocal

    factory = session_factory or SessionLocal
    try:
        async with factory() as session:  # type: ignore[operator]
            facts = await _order_facts(session, order_id)
            if facts is None:
                return
            order, customer, plan = facts
            text = renewal_text(
                plan_label=_order_label(order, plan),
                customer=_customer_line(customer.name, customer.username, customer.telegram_id),
                price_toman=order.price_toman,
                automatic=automatic,
            )
    except Exception:  # noqa: BLE001
        log.info("renewal notification could not be built (order %s)", order_id, exc_info=True)
        return
    await notify_shop_admins(
        shop_id=sf_id, text=text, bot=bot, exclude_chat_id=exclude_chat_id,
        session_factory=session_factory)


async def notify_topup(
    *, sf_id: int, customer_id: int | None, amount_toman: object, bonus_toman: object = 0,
    bot: object | None = None, exclude_chat_id: int | None = None,
    session_factory: object | None = None,
) -> None:
    """A wallet top-up was confirmed.

    `exclude_chat_id` is the admin who pressed تأیید — they are looking at the result already. The
    point of this message is the OTHER admins: before it, a decision taken in the portal or by a
    co-admin was invisible to everyone else in the shop.
    """
    from app.core.db import SessionLocal

    factory = session_factory or SessionLocal
    try:
        async with factory() as session:  # type: ignore[operator]
            customer = (
                await session.get(StorefrontCustomer, customer_id) if customer_id else None)
            if customer is None:
                return
            text = topup_text(
                customer=_customer_line(customer.name, customer.username, customer.telegram_id),
                amount_toman=amount_toman, bonus_toman=bonus_toman,
                balance_toman=customer.wallet_balance_toman,
            )
    except Exception:  # noqa: BLE001
        log.info("top-up notification could not be built (customer %s)", customer_id, exc_info=True)
        return
    await notify_shop_admins(
        shop_id=sf_id, text=text, bot=bot, exclude_chat_id=exclude_chat_id,
        session_factory=session_factory)
