"""Proactive storefront notices (I10 + v1.66.0): near-expiry reminders, «free trial
ended → buy» nudges, and «~80% volume used → renew» warnings.

A daily scheduler job scans every PROVISIONED storefront order and messages the
customer through their storefront's own bot; the three notices share one
scan→send→stamp runner (`_run_sweep`).

Days-left source of truth: the panel snapshot (`end_user_snapshots.start_date +
package_days` for the order's `(panel_id, panel_user_uuid)`), refreshed by every sync;
fallback when the snapshot is missing: `(last_renewed_at | created_at) + order.days`.

Dedup: per-notice `*_alerted_at` stamps — an order is reminded ONCE per service
period; a renewal re-arms it (`last_renewed_at` newer than the stamp), mirroring the
`gb_cap_alerted_period` pattern.

Delivery semantics (N01): a DELIVERED notice and a BLOCKED customer
(`TelegramForbiddenError` — permanent) are stamped; a TRANSIENT failure (flood-wait
past the retry budget, network blip, dead bot token) is NOT stamped, so the next
daily run retries it instead of silently dropping the reminder forever. Sends are
paced by the shared broadcast flood-control policy
(`broadcast.send_with_flood_control`), and stamps are committed in bounded batches
with no DB transaction held across Telegram I/O (a crash re-sends at most one batch
window, not the whole sweep).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

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
from app.services import broadcast, settings_service, storefront
from app.services.periods import today as tehran_today

log = logging.getLogger("storefront_expiry")

BotFactory = Callable[[str], Awaitable[Bot]]

# Stamps are committed every N processed outcomes so a mid-sweep crash duplicates at
# most one batch window on the next daily run (instead of the whole sweep).
_STAMP_BATCH = 25


async def _default_bot_factory(token: str) -> Bot:
    from app.bot import rtl_middleware

    bot = Bot(token=token)
    rtl_middleware.install(bot)
    return bot


def _new_counts() -> dict:
    return {"checked": 0, "due": 0, "sent": 0, "blocked": 0, "failed": 0}


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
            # anchor is a UTC timestamp; take the TEHRAN-local day (raw .date() gave the UTC
            # day, so an order created 00:00–03:29 Tehran expired a day early and the final
            # «امروز منقضی می‌شود» reminder could be skipped). Same fix as the dunning bug.
            from app.services.periods import to_local_date

            return (to_local_date(anchor) + dt.timedelta(days=int(order.days)) - today).days
    return None


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce a possibly naive timestamp (SQLite loses tz) to aware UTC for safe comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _needs_alert(order: StorefrontOrder) -> bool:
    """Never alerted, or renewed since the last alert (re-armed)."""
    if order.expiry_alerted_at is None:
        return True
    lr, ea = _aware(order.last_renewed_at), _aware(order.expiry_alerted_at)
    return lr is not None and ea is not None and lr > ea


def _message_fa(label: str | None, days_left: int) -> str:
    name = f"«{label}» " if label else ""
    when = "امروز" if days_left <= 0 else (
        "فردا" if days_left == 1 else f"تا {days_left} روز دیگر")
    return (
        f"⏳ سرویس {name}شما {when} منقضی می‌شود.\n"
        "برای جلوگیری از قطعِ سرویس، «تمدید خودکار» را روشن کنید تا درست پیش از پایان، یک‌بار به‌صورت "
        "خودکار تمدید شود (مبلغِ یک تمدید از کیفِ پولِ شما رزرو می‌شود)."
    )


def _expired_msg(label: str | None, days_ago: int) -> str:
    name = f"«{label}» " if label else ""
    when = "امروز" if days_ago <= 0 else ("دیروز" if days_ago == 1 else f"{days_ago} روز پیش")
    return (
        f"⛔️ سرویس {name}شما {when} منقضی شد و اتصالِ شما قطع شده است.\n"
        "برای بازگشت به سرویس، «تمدید خودکار» را روشن کنید تا بلافاصله تمدید شود و دفعهٔ بعد نیز "
        "پیش از قطعی، به‌صورت خودکار تمدید شود."
    )


def _needs_expired_alert(order: StorefrontOrder) -> bool:
    """Never win-backed, or renewed since the last one (re-armed for the next service period)."""
    if order.expired_alerted_at is None:
        return True
    lr, ea = _aware(order.last_renewed_at), _aware(order.expired_alerted_at)
    return lr is not None and ea is not None and lr > ea


def _renew_keyboard(order_id: int) -> InlineKeyboardMarkup:
    # CTA is now ARM (one-shot auto-renew), not an immediate renew — the customer reserves one plan's
    # price and the config renews itself right before it runs out, so they don't get cut off.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 روشن‌کردنِ تمدید خودکار", callback_data=f"sfauto:{order_id}")
    ]])


async def _load_snaps(session: AsyncSession, rows: Sequence[Any]) -> dict:
    """One-query snapshot lookup for every (panel_id, uuid) in `rows` (order⋈customer⋈bot tuples)."""
    keys = {(o.panel_id, o.panel_user_uuid) for o, _c, _b in rows
            if o.panel_id is not None and o.panel_user_uuid}
    snaps: dict[tuple[int, str], EndUserSnapshot] = {}
    if keys:
        for sn in (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.user_uuid.in_([u for (_p, u) in keys])
                )
            )
        ).scalars().all():
            snaps[(sn.panel_id, sn.user_uuid)] = sn
    return snaps


async def _run_sweep(
    session: AsyncSession,
    rows: Sequence[Any],
    snaps: dict,
    *,
    due: Callable[[StorefrontOrder, EndUserSnapshot | None], Any],
    render: Callable[[StorefrontOrder, Any], tuple[str, InlineKeyboardMarkup]],
    stamp_attr: str,
    bot_factory: BotFactory,
    counts: dict,
    log_label: str,
) -> dict:
    """Shared scan→send→stamp runner for the three notices.

    Phase 1 (read transaction): decide the due list via `due(order, snap)` (None = not
    due; any other payload is handed to `render`) and materialize everything the send
    needs, then COMMIT to release the transaction before any network I/O.
    Phase 2 (no session I/O): send each notice through the shared flood-control
    policy; "sent" and "blocked" stamp `stamp_attr` (blocked customers must not be
    retried daily forever), "failed" does NOT stamp (retried by the next daily run).
    Stamps are flushed every `_STAMP_BATCH` outcomes and once at the end.
    """
    work: list[tuple[StorefrontOrder, int, int, str, str, InlineKeyboardMarkup]] = []
    for order, customer, sf_bot in rows:
        counts["checked"] += 1
        snap = snaps.get((order.panel_id, order.panel_user_uuid or ""))
        payload = due(order, snap)
        if payload is None:
            continue
        counts["due"] += 1
        token = storefront.bot_token(sf_bot)
        if not token:
            counts["failed"] += 1  # no usable token → not stamped, retried next run
            continue
        text, keyboard = render(order, payload)
        work.append((order, customer.telegram_id, sf_bot.id, token, text, keyboard))
    # End the read transaction BEFORE Telegram I/O — no connection/transaction is held
    # across the (potentially minutes-long) send loop.
    await session.commit()
    if not work:
        return counts

    now = dt.datetime.now(dt.timezone.utc)
    limiter = broadcast.rate_limiter()
    bots: dict[int, Bot] = {}
    stamped: list[StorefrontOrder] = []

    async def _flush() -> None:
        if not stamped:
            return
        for o in stamped:
            setattr(o, stamp_attr, now)
        await session.commit()
        stamped.clear()

    try:
        for order, chat_id, bot_id, token, text, keyboard in work:
            bot = bots.get(bot_id)
            if bot is None:
                try:
                    bot = await bot_factory(token)
                except Exception:  # noqa: BLE001 — bad token/init: transient, retry next run
                    counts["failed"] += 1
                    log.warning("%s: bot init failed (storefront_bot %s)",
                                log_label, bot_id, exc_info=True)
                    continue
                bots[bot_id] = bot
            outcome = await broadcast.send_with_flood_control(
                bot, chat_id, text, limiter, reply_markup=keyboard)
            counts[outcome] += 1
            if outcome == "failed":
                log.warning("%s: send failed (order %s) — not stamped, retried next run",
                            log_label, order.id)
                continue
            stamped.append(order)  # "sent" and "blocked" both stamp
            if len(stamped) >= _STAMP_BATCH:
                await _flush()
        await _flush()
    finally:
        for bot in bots.values():
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
    if counts["due"]:
        log.info("%s: %s", log_label, counts)
    return counts


async def notify_expiring(
    session: AsyncSession, *, bot_factory: BotFactory | None = None
) -> dict:
    """Scan + remind near-expiry PAID configs. Returns counters."""
    counts = _new_counts()
    try:
        threshold = int(await settings_service.get(session, "storefront_expiry_notify_days", 3))
    except (TypeError, ValueError):
        threshold = 3
    if threshold <= 0:
        return counts

    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
            .where(
                StorefrontOrder.status == "provisioned",
                StorefrontOrder.is_trial.is_(False),  # trials get the «trial ended» nudge instead
                StorefrontCustomer.banned.is_(False),
                StorefrontBot.enabled.is_(True),
                StorefrontBot.status != "errored",
            )
        )
    ).all()
    if not rows:
        return counts
    snaps = await _load_snaps(session, rows)
    today = tehran_today()

    def due(order: StorefrontOrder, snap: EndUserSnapshot | None) -> Any:
        if order.autorenew_armed_at is not None:
            return None   # armed → it auto-renews near exhaustion; don't nag to renew
        days_left = _days_left(order, snap, today)
        if days_left is None or days_left < 0 or days_left > threshold:
            return None
        if not _needs_alert(order):
            return None
        return days_left

    def render(order: StorefrontOrder, days_left: Any) -> tuple[str, InlineKeyboardMarkup]:
        return _message_fa(order.label, int(days_left)), _renew_keyboard(order.id)

    return await _run_sweep(
        session, rows, snaps, due=due, render=render, stamp_attr="expiry_alerted_at",
        bot_factory=bot_factory or _default_bot_factory, counts=counts,
        log_label="storefront expiry reminders",
    )


def _buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 خرید سرویس", callback_data="sfbuylist")
    ]])


def _trial_ended_msg(label: str | None) -> str:
    name = f"«{label}» " if label else ""
    return (
        f"🎁 سرویسِ تستِ رایگانِ {name}شما به پایان رسید.\n"
        "امیدواریم از کیفیتِ آن راضی بوده باشید. برای ادامه، می‌توانید از دکمهٔ زیر یکی از پلن‌ها را تهیه کنید."
    )


async def notify_trial_ended(
    session: AsyncSession, *, bot_factory: BotFactory | None = None
) -> dict:
    """Nudge customers whose FREE TRIAL just expired to buy a plan — once per trial
    (`trial_ended_alerted_at`, dedup lives in the query). No discount, just a friendly
    «trial ended → buy» with a buy button.

    Bounded by a LOOKBACK, for the same reason as `notify_expired`: trials are never moved out of
    `provisioned` when they lapse, and the migration that added the dedup stamp did no backfill —
    so without a bound the first sweep after a deploy would message every trial the shop has ever
    issued, at once, to real customers. Only trials that ended within
    `storefront_trial_ended_notify_days` (default 7, 0 = off) are contacted.
    """
    counts = _new_counts()
    try:
        lookback = int(
            await settings_service.get(session, "storefront_trial_ended_notify_days", 7))
    except (TypeError, ValueError):
        lookback = 7
    if lookback <= 0:
        return counts
    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
            .where(
                StorefrontOrder.status == "provisioned",
                StorefrontOrder.is_trial.is_(True),
                StorefrontOrder.trial_ended_alerted_at.is_(None),
                StorefrontCustomer.banned.is_(False),
                StorefrontBot.enabled.is_(True),
                StorefrontBot.status != "errored",
            )
        )
    ).all()
    if not rows:
        return counts
    snaps = await _load_snaps(session, rows)
    today = tehran_today()

    def due(order: StorefrontOrder, snap: EndUserSnapshot | None) -> Any:
        days_left = _days_left(order, snap, today)
        if days_left is None or days_left >= 0:  # trial not expired yet
            return None
        if -days_left > lookback:                # too old — never bulk-message ancient history
            return None
        return days_left

    def render(order: StorefrontOrder, _payload: Any) -> tuple[str, InlineKeyboardMarkup]:
        return _trial_ended_msg(order.label), _buy_keyboard()

    return await _run_sweep(
        session, rows, snaps, due=due, render=render, stamp_attr="trial_ended_alerted_at",
        bot_factory=bot_factory or _default_bot_factory, counts=counts,
        log_label="storefront trial-ended nudges",
    )


async def notify_expired(
    session: AsyncSession, *, bot_factory: BotFactory | None = None
) -> dict:
    """Win-back a PAID service that has ALREADY lapsed — the moment a customer is most likely to
    renew, and until now the one moment we said nothing (the near-expiry reminder deliberately
    stops at days_left < 0, so once a service actually died the customer never heard from us).

    Bounded by a LOOKBACK window on purpose: without it the first run after deploy would message
    every customer whose service ever expired — years of them, all at once. Only services that
    lapsed within `storefront_expired_notify_days` (default 7, 0 = off) are contacted, once per
    service period (re-armed by a renewal).
    """
    counts = _new_counts()
    try:
        lookback = int(await settings_service.get(session, "storefront_expired_notify_days", 7))
    except (TypeError, ValueError):
        lookback = 7
    if lookback <= 0:
        return counts

    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
            .where(
                StorefrontOrder.status == "provisioned",
                StorefrontOrder.is_trial.is_(False),   # trials get the «trial ended» nudge instead
                StorefrontCustomer.banned.is_(False),
                StorefrontBot.enabled.is_(True),
                StorefrontBot.status != "errored",
            )
        )
    ).all()
    if not rows:
        return counts
    snaps = await _load_snaps(session, rows)
    today = tehran_today()

    def due(order: StorefrontOrder, snap: EndUserSnapshot | None) -> Any:
        if order.autorenew_armed_at is not None:
            return None   # armed → auto-renew will bring it back; no win-back nag
        days_left = _days_left(order, snap, today)
        if days_left is None or days_left >= 0:      # not expired yet → the reminder handles it
            return None
        days_ago = -days_left
        if days_ago > lookback:                      # too old — never bulk-message ancient history
            return None
        if not _needs_expired_alert(order):
            return None
        return days_ago

    def render(order: StorefrontOrder, days_ago: Any) -> tuple[str, InlineKeyboardMarkup]:
        return _expired_msg(order.label, int(days_ago)), _renew_keyboard(order.id)

    return await _run_sweep(
        session, rows, snaps, due=due, render=render, stamp_attr="expired_alerted_at",
        bot_factory=bot_factory or _default_bot_factory, counts=counts,
        log_label="storefront win-back notices",
    )


def _usage_needs_alert(order: StorefrontOrder) -> bool:
    """Never alerted, or renewed since the last alert (fresh quota re-arms it)."""
    if order.usage_alerted_at is None:
        return True
    lr, ua = _aware(order.last_renewed_at), _aware(order.usage_alerted_at)
    return lr is not None and ua is not None and lr > ua


def _usage_high_msg(label: str | None, used_gb: float, limit_gb: float) -> str:
    name = f"«{label}» " if label else ""
    pct = int(round(used_gb / limit_gb * 100)) if limit_gb else 0
    return (
        f"⚠️ حجمِ سرویسِ {name}شما رو به پایان است (حدود {pct}٪ مصرف شده است).\n"
        "برای جلوگیری از قطعِ سرویس، «تمدید خودکار» را روشن کنید تا درست پیش از پایانِ حجم، یک‌بار به‌صورت "
        "خودکار تمدید شود."
    )


async def notify_usage_high(
    session: AsyncSession, *, bot_factory: BotFactory | None = None
) -> dict:
    """Warn customers whose PAID config has used most of its volume, with a renew button — once per
    quota cycle (`usage_alerted_at`, re-armed by a renewal). Trials are excluded (they get the
    trial-ended nudge). Volume comes from the synced snapshot (no per-user panel call).

    The threshold is `storefront_usage_alert_percent` (default 80), and the sweep now SKIPS an
    already-expired service. It never computed `_days_left` at all, and an expired config keeps
    both `status="provisioned"` and its last exhausted snapshot — so a service that lapsed a year
    ago, at 100% used, was told to "renew before you're cut off". That is a filter, not a lookback:
    an expired service should get no quota warning at ANY age.
    """
    counts = _new_counts()
    try:
        percent = int(await settings_service.get(session, "storefront_usage_alert_percent", 80))
    except (TypeError, ValueError):
        percent = 80
    threshold = min(max(percent, 1), 100) / 100.0
    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
            .where(
                StorefrontOrder.status == "provisioned",
                StorefrontOrder.is_trial.is_(False),
                StorefrontCustomer.banned.is_(False),
                StorefrontBot.enabled.is_(True),
                StorefrontBot.status != "errored",
            )
        )
    ).all()
    if not rows:
        return counts
    snaps = await _load_snaps(session, rows)
    today = tehran_today()

    def due(order: StorefrontOrder, snap: EndUserSnapshot | None) -> Any:
        if snap is None:
            return None
        if order.autorenew_armed_at is not None:
            return None   # armed → it auto-renews near exhaustion; don't nag about high usage
        days_left = _days_left(order, snap, today)
        # `is not None and < 0` on purpose: `_days_left` returns None when there is neither a
        # snapshot date nor `order.days`, and treating "unknown" as "expired" would silently drop
        # a legitimate cohort from the warning entirely.
        if days_left is not None and days_left < 0:
            return None                      # already expired → the win-back notice handles it
        limit_gb = float(snap.usage_limit_gb or 0)
        used_gb = float(snap.current_usage_gb or 0)
        if limit_gb <= 0 or used_gb / limit_gb < threshold:
            return None
        if not _usage_needs_alert(order):
            return None
        return (used_gb, limit_gb)

    def render(order: StorefrontOrder, payload: Any) -> tuple[str, InlineKeyboardMarkup]:
        used_gb, limit_gb = payload
        return _usage_high_msg(order.label, used_gb, limit_gb), _renew_keyboard(order.id)

    return await _run_sweep(
        session, rows, snaps, due=due, render=render, stamp_attr="usage_alerted_at",
        bot_factory=bot_factory or _default_bot_factory, counts=counts,
        log_label="storefront usage-high warnings",
    )


async def count_expiring_soon(
    session: AsyncSession, storefront_bot_id: int, threshold: int
) -> int:
    """How many of ONE storefront's provisioned orders expire within `threshold` days
    (for the admin stats view). Same days-left math as the reminder sweep."""
    if threshold <= 0:
        return 0
    today = tehran_today()
    rows = (
        await session.execute(
            select(StorefrontOrder)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .where(
                StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                StorefrontOrder.status == "provisioned",
            )
        )
    ).scalars().all()
    if not rows:
        return 0
    uuids = [o.panel_user_uuid for o in rows if o.panel_user_uuid]
    snaps: dict[tuple[int, str], EndUserSnapshot] = {}
    if uuids:
        for sn in (
            await session.execute(
                select(EndUserSnapshot).where(EndUserSnapshot.user_uuid.in_(uuids))
            )
        ).scalars().all():
            snaps[(sn.panel_id, sn.user_uuid)] = sn
    n = 0
    for order in rows:
        snap = None
        if order.panel_id is not None:
            snap = snaps.get((order.panel_id, order.panel_user_uuid or ""))
        days_left = _days_left(order, snap, today)
        if days_left is not None and 0 <= days_left <= threshold:
            n += 1
    return n
