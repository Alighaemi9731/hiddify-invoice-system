"""Near-expiry reminders for storefront customers (I10).

A daily scheduler job scans every PROVISIONED storefront order and messages the customer
through their storefront's own bot when the config is `storefront_expiry_notify_days`
(default 3, 0 = off) or fewer days from expiring — with the existing renew button
(`sfrenew:<order_id>`), which drives renewals instead of silent churn.

Days-left source of truth: the panel snapshot (`end_user_snapshots.start_date +
package_days` for the order's `(panel_id, panel_user_uuid)`), refreshed by every sync;
fallback when the snapshot is missing: `(last_renewed_at | created_at) + order.days`.

Dedup: `StorefrontOrder.expiry_alerted_at` — an order is reminded ONCE per service
period; a renewal re-arms it (`last_renewed_at` newer than the stamp), mirroring the
`gb_cap_alerted_period` pattern. The stamp is written even when Telegram rejects the
send (blocked bot), so a blocked customer isn't retried daily forever.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
)
from app.services import settings_service, storefront
from app.services.periods import today as tehran_today

log = logging.getLogger("storefront_expiry")

BotFactory = Callable[[str], Awaitable[Bot]]


async def _default_bot_factory(token: str) -> Bot:
    from app.bot import rtl_middleware

    bot = Bot(token=token)
    rtl_middleware.install(bot)
    return bot


def _days_left(
    order: StorefrontOrder, snap: EndUserSnapshot | None, today: dt.date
) -> int | None:
    """Days until the order's config expires (negative = already expired), or None when
    no expiry can be derived (no snapshot AND no order duration)."""
    if snap is not None and snap.start_date is not None and (snap.package_days or 0) > 0:
        return (snap.start_date + dt.timedelta(days=int(snap.package_days or 0)) - today).days
    if (order.days or 0) > 0:
        anchor = order.last_renewed_at or order.created_at
        if anchor is not None:
            return (anchor.date() + dt.timedelta(days=int(order.days)) - today).days
    return None


def _needs_alert(order: StorefrontOrder) -> bool:
    """Never alerted, or renewed since the last alert (re-armed)."""
    if order.expiry_alerted_at is None:
        return True
    return order.last_renewed_at is not None and order.last_renewed_at > order.expiry_alerted_at


def _message_fa(label: str | None, days_left: int) -> str:
    name = f"«{label}» " if label else ""
    when = "امروز" if days_left <= 0 else (
        "فردا" if days_left == 1 else f"تا {days_left} روز دیگر")
    return (
        f"⏳ سرویس {name}شما {when} منقضی می‌شود.\n"
        "برای ادامهٔ استفاده، از دکمهٔ زیر تمدید کنید."
    )


def _renew_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data=f"sfrenew:{order_id}")
    ]])


async def notify_expiring(
    session: AsyncSession, *, bot_factory: BotFactory | None = None
) -> dict:
    """Scan + remind. Returns counters; never raises into the caller."""
    counts = {"checked": 0, "due": 0, "sent": 0, "failed": 0}
    try:
        threshold = int(await settings_service.get(session, "storefront_expiry_notify_days", 3))
    except (TypeError, ValueError):
        threshold = 3
    if threshold <= 0:
        return counts

    factory = bot_factory or _default_bot_factory
    today = tehran_today()
    now = dt.datetime.now(dt.timezone.utc)

    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
            .where(
                StorefrontOrder.status == "provisioned",
                StorefrontCustomer.banned.is_(False),
                StorefrontBot.enabled.is_(True),
                StorefrontBot.status != "errored",
            )
        )
    ).all()
    if not rows:
        return counts

    # Snapshot lookup for every order's (panel_id, uuid) in one query.
    keys = {(o.panel_id, o.panel_user_uuid) for o, _c, _b in rows
            if o.panel_id is not None and o.panel_user_uuid}
    snaps: dict[tuple[int, str], EndUserSnapshot] = {}
    if keys:
        snap_rows = (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.user_uuid.in_([u for (_p, u) in keys])
                )
            )
        ).scalars().all()
        for sn in snap_rows:
            snaps[(sn.panel_id, sn.user_uuid)] = sn

    bots: dict[int, Bot] = {}
    try:
        for order, customer, sf_bot in rows:
            counts["checked"] += 1
            snap = snaps.get((order.panel_id, order.panel_user_uuid or ""))
            days_left = _days_left(order, snap, today)
            if days_left is None or days_left < 0 or days_left > threshold:
                continue
            if not _needs_alert(order):
                continue
            counts["due"] += 1
            try:
                bot = bots.get(sf_bot.id)
                if bot is None:
                    token = storefront.bot_token(sf_bot)
                    if not token:
                        counts["failed"] += 1
                        continue
                    bot = await factory(token)
                    bots[sf_bot.id] = bot
                await bot.send_message(
                    customer.telegram_id,
                    _message_fa(order.label, days_left),
                    reply_markup=_renew_keyboard(order.id),
                )
                counts["sent"] += 1
            except Exception:  # noqa: BLE001 - blocked customer / dead bot: stamp anyway below
                counts["failed"] += 1
                log.warning("expiry reminder send failed (order %s)", order.id, exc_info=True)
            # Stamp regardless of delivery: a blocked customer must not be retried daily.
            order.expiry_alerted_at = now
        await session.commit()
    finally:
        for bot in bots.values():
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
    if counts["due"]:
        log.info("storefront expiry reminders: %s", counts)
    return counts
