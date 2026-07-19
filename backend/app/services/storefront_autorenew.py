"""Deferred ONE-SHOT auto-renew for storefront (customer-bot) subscriptions.

The customer arms auto-renew on a config → the plan price is LOCKED right then as a wallet `hold`
(reserved out of their spendable balance) → a periodic sweep FIRES the renewal exactly once when the
config is nearly out of volume OR days → the arm is consumed (one-shot; the customer re-arms for the
next cycle). This replaces the old "renew now" button: a customer who wants MORE volume mid-cycle
buys a NEW config instead, and everyone renews the same way.

Why this shape:
  * It never needs a usage-RESET (impossible with the current panel adapter). The renewal stays the
    normal cumulative `used + gb`; firing at near-exhaustion means the leftover is ≈0, so nothing is
    lost, and Phase-1 billing already caps the reseller's invoice at the sold plan, not the inflated
    panel quota.
  * Exactly-once, crash-safe, no double-charge: arm/disarm/fire all take the per-customer in-process
    lock + the order row lock, the armed columns are the state guard, and the reserved money is a real
    `hold` debit. Fire renews through `storefront_subscription.renew(prepaid_hold_txn_id=…)` with a
    DETERMINISTIC op_id, so a retry REPLAYs and the durable reconciler covers a mid-fire crash; renew()
    SETTLES the hold as payment (money already out) and clears the armed columns atomically on success.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal
from app.models import (
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import broadcast, settings_service, storefront, storefront_wallet
from app.services.periods import today as tehran_today
from app.services.storefront_expiry import (
    BotFactory,
    _days_left,
    _default_bot_factory,
    _load_snaps,
)
from app.services.storefront_provision import _customer_lock, live_status
from app.services.storefront_subscription import (
    SubResult,
    _order_for_update,
    _panel_ctx,
    renew,
)

log = logging.getLogger("bot.storefront")

# Arm is allowed on a live config (paused configs can be armed too — they'll fire once resumed and
# near-exhaustion). Same set `renew()` treats as renewable.
_ARMABLE = ("provisioned", "disabled")
# Cap candidates processed per sweep tick (fire does panel + wallet I/O under a lock).
_SWEEP_LIMIT = 100


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class ArmResult:
    ok: bool
    reason: str | None = None    # not_found | trial | already | insufficient | error
    price: int = 0
    short_toman: int = 0


def is_armed(order: StorefrontOrder) -> bool:
    return order.autorenew_armed_at is not None


async def _plan_price(s: AsyncSession, order: StorefrontOrder) -> int:
    """The price to lock: the current plan's price if the plan still exists+enabled, else the price
    the order last carried (a deleted/disabled plan still renews at its last known price)."""
    plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
    if plan and plan.enabled:
        return int(plan.price_toman)
    return int(order.price_toman)


async def arm(
    session: AsyncSession, order_id: int, *, expected_sf_id: int | None = None
) -> ArmResult:
    """Arm one-shot auto-renew: lock the plan price as a wallet hold and stamp the armed columns.
    Refuses if the wallet can't cover the price (→ "top up first"). Idempotent: re-arming an already
    armed order is a no-op success. Under the per-customer lock + the order row lock."""
    order0 = await session.get(StorefrontOrder, order_id)
    if order0 is None:
        return ArmResult(False, "not_found")
    sf, customer, _reseller, _panel = await _panel_ctx(session, order0)
    if not (sf and customer):
        return ArmResult(False, "error")
    if expected_sf_id is not None and sf.id != expected_sf_id:
        return ArmResult(False, "not_found")   # tenant guard — never touch another shop's order
    async with _customer_lock(sf.id, customer.id):
        order = await _order_for_update(session, order_id)
        if order is None or order.status not in _ARMABLE:
            return ArmResult(False, "not_found")
        if order.is_trial:
            return ArmResult(False, "trial")   # trials are one-time, never renewable
        if not order.panel_user_uuid:
            return ArmResult(False, "error")
        if is_armed(order):
            return ArmResult(True, "already", price=int(order.autorenew_price_toman or 0))
        price = await _plan_price(session, order)
        if price <= 0:
            return ArmResult(False, "error")
        ok, hold = await storefront_wallet.place_hold(
            session, customer.id, price, order_id=order_id)
        if not ok or hold is None:
            # Short balance: place_hold wrote nothing (it returns before the debit), so there's
            # nothing to roll back — and `place_hold` refreshed `customer` under its lock, so its
            # balance is current. (A collision on the active-hold index would RAISE, not return here.)
            bal = int(storefront_wallet.balance(customer))
            return ArmResult(False, "insufficient", price=price, short_toman=max(0, price - bal))
        order.autorenew_armed_at = _now()
        order.autorenew_price_toman = price
        order.autorenew_hold_txn_id = hold.id
        await session.commit()
        return ArmResult(True, price=price)


async def _disarm_locked(session: AsyncSession, order_id: int) -> bool:
    """Release the hold (if still held) and clear the armed columns, under an already-held lock.
    Returns True if the order was armed. Idempotent."""
    order = await _order_for_update(session, order_id)
    if order is None or not is_armed(order):
        return False
    if order.autorenew_hold_txn_id is not None:
        await storefront_wallet.release_hold(session, order.autorenew_hold_txn_id)
    order.autorenew_armed_at = None
    order.autorenew_price_toman = None
    order.autorenew_hold_txn_id = None
    await session.commit()
    return True


async def disarm(
    session: AsyncSession, order_id: int, *, expected_sf_id: int | None = None
) -> ArmResult:
    """Turn auto-renew off: return the reserved funds to the wallet and clear the armed columns.
    Idempotent (turning off an off order is a no-op success). Under the per-customer lock."""
    order0 = await session.get(StorefrontOrder, order_id)
    if order0 is None:
        return ArmResult(False, "not_found")
    sf, customer, _reseller, _panel = await _panel_ctx(session, order0)
    if not (sf and customer):
        return ArmResult(False, "error")
    if expected_sf_id is not None and sf.id != expected_sf_id:
        return ArmResult(False, "not_found")
    async with _customer_lock(sf.id, customer.id):
        await _disarm_locked(session, order_id)
        return ArmResult(True)


async def disarm_quietly(session: AsyncSession, order_id: int) -> bool:
    """Release the hold + clear the armed columns for an order being deleted/terminated by another
    flow (delete, pause-to-terminal). Resolves the lock itself; safe to call unconditionally."""
    order0 = await session.get(StorefrontOrder, order_id)
    if order0 is None or not is_armed(order0):
        return False
    sf, customer, _r, _p = await _panel_ctx(session, order0)
    if not (sf and customer):
        return False
    async with _customer_lock(sf.id, customer.id):
        return await _disarm_locked(session, order_id)


async def fire(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal, *, order_id: int
) -> SubResult:
    """Fire the armed one-shot renewal for `order_id`. Uses a DETERMINISTIC op_id per arm cycle so a
    retry REPLAYs with no second settle, and `renew()` settles the hold + clears the armed columns
    atomically on success. On a DEFINITIVE failure the arm is torn down (hold released, columns
    cleared); a merely AMBIGUOUS "processing" is left for the durable reconciler."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or not is_armed(order):
            return SubResult(False, "not_found")
        hold_id = order.autorenew_hold_txn_id
        price = int(order.autorenew_price_toman or 0)
    if hold_id is None:
        # Corrupt arm (armed with no reserved funds) — never fall through to renew() with no hold,
        # which would CHARGE the wallet again. Tear the (moneyless) arm down instead.
        async with session_factory() as s:
            await disarm_quietly(s, order_id)
        return SubResult(False, "error")
    op_id = f"autorenew:{order_id}:{hold_id}"   # deterministic per (order, arm) → retry REPLAYs
    res = await renew(
        session_factory, order_id=order_id, op_id=op_id,
        prepaid_hold_txn_id=hold_id, locked_price_toman=price)
    if res.ok or res.reason == "processing":
        return res
    # Definitive failure (not_found / trial / error / cancelled / hold-missing): tear the arm down so
    # the reservation returns and the order isn't left falsely armed. release_hold is idempotent — if
    # renew already settled/reversed the hold it's a no-op.
    async with session_factory() as s:
        await disarm_quietly(s, order_id)
    return res


# ─────────────────────────── the near-exhaustion sweep ───────────────────────────

def _new_counts() -> dict:
    return {"checked": 0, "backstop_cleared": 0, "due": 0, "fired": 0, "failed": 0, "skipped": 0}


def _post_fire_msg(label: str | None) -> str:
    name = f"«{label}» " if label else ""
    return (
        f"🔁 بستهٔ سرویس {name}شما رو به پایان بود و چون «تمدید خودکار» را روشن کرده بودید، "
        "سرویس‌تان یک دوره تمدید شد و قطع نشدید."
    )


def _fire_due(remaining_gb: float | None, days_left: int | None,
              fire_gb: float, fire_days: int) -> bool:
    """Near-exhaustion: under the volume margin, OR within the days margin. Either alone triggers."""
    if remaining_gb is not None and remaining_gb < fire_gb:
        return True
    if days_left is not None and days_left < fire_days:
        return True
    return False


async def sweep(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    bot_factory: BotFactory | None = None,
) -> dict:
    """Fire every armed config that is near exhaustion, and clean up any dangling arm (armed but the
    hold is no longer `held` — a reconciler-completed or crashed fire). Returns counters.

    Phase 1 (read txn): from a cheap snapshot, decide the fire list (and clear dangling arms), then
    commit to drop the transaction. Phase 2 (no txn held): per candidate, a LIVE panel confirm (the
    snapshot is ≤ sync-interval stale) then `fire()`, then a best-effort post-fire notice."""
    counts = _new_counts()
    bot_factory = bot_factory or _default_bot_factory
    try:
        fire_gb = float(await _get(session_factory, "storefront_autorenew_fire_gb", 1))
    except (TypeError, ValueError):
        fire_gb = 1.0
    try:
        fire_days = int(await _get(session_factory, "storefront_autorenew_fire_days", 1))
    except (TypeError, ValueError):
        fire_days = 1

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(StorefrontOrder, StorefrontCustomer, StorefrontBot)
                .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
                .join(StorefrontBot, StorefrontBot.id == StorefrontCustomer.storefront_bot_id)
                .where(
                    StorefrontOrder.autorenew_armed_at.is_not(None),
                    StorefrontOrder.status == "provisioned",
                    StorefrontOrder.is_trial.is_(False),
                    StorefrontCustomer.banned.is_(False),
                    StorefrontBot.enabled.is_(True),
                    StorefrontBot.status != "errored",
                )
            )
        ).all()
        if not rows:
            await s.commit()
            return counts
        snaps = await _load_snaps(s, rows)
        held = await _held_hold_ids(s, [o.autorenew_hold_txn_id for o, _c, _b in rows])
        today = tehran_today()
        work: list[tuple[int, str | None, str, int, int]] = []  # order_id, label, token, chat, bot_id
        for order, customer, sf_bot in rows:
            counts["checked"] += 1
            # Backstop: armed but the hold is no longer `held` → the arm is spent (a reconciler
            # finished the fire) or was released; clear the dangling armed columns.
            if order.autorenew_hold_txn_id not in held:
                await disarm_quietly(s, order.id)
                counts["backstop_cleared"] += 1
                continue
            snap = snaps.get((order.panel_id, order.panel_user_uuid or ""))
            remaining_gb = None
            if snap is not None:
                remaining_gb = float(snap.usage_limit_gb or 0) - float(snap.current_usage_gb or 0)
            days_left = _days_left(order, snap, today)
            if not _fire_due(remaining_gb, days_left, fire_gb, fire_days):
                continue
            token = storefront.bot_token(sf_bot)
            if not token:
                counts["skipped"] += 1
                continue
            counts["due"] += 1
            work.append((order.id, order.label, token, customer.telegram_id, sf_bot.id))
            if len(work) >= _SWEEP_LIMIT:
                # Cap this tick's fires; the rest are picked up next tick (not silently dropped).
                log.info("autorenew sweep hit the per-tick cap (%d) — remainder deferred to the "
                         "next run", _SWEEP_LIMIT)
                break
        await s.commit()

    if not work:
        return counts

    limiter = broadcast.rate_limiter()
    bots: dict[int, Bot] = {}
    try:
        for order_id, label, token, chat_id, bot_id in work:
            # A tight LIVE confirm — the snapshot is up to one sync interval stale and a fast line can
            # burn through its margin between syncs. Best-effort: on a live-read failure we trust the
            # snapshot that already flagged it (we don't skip a due fire just because the panel blipped).
            if not await _live_confirm(session_factory, order_id, fire_gb, fire_days):
                counts["skipped"] += 1
                continue
            res = await fire(session_factory, order_id=order_id)
            if res.ok:
                counts["fired"] += 1
                bot = bots.get(bot_id)
                if bot is None:
                    try:
                        bot = await bot_factory(token)
                    except Exception:  # noqa: BLE001 — bad token: the renewal still happened
                        log.warning("autorenew: post-fire bot init failed (sf %s)", bot_id)
                        continue
                    bots[bot_id] = bot
                await broadcast.send_with_flood_control(
                    bot, chat_id, _post_fire_msg(label), limiter)
            elif res.reason == "processing":
                counts["skipped"] += 1   # reconciler owns it; the next sweep confirms
            else:
                counts["failed"] += 1
                log.warning("autorenew fire failed for order %s: %s", order_id, res.reason)
    finally:
        for bot in bots.values():
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
    if counts["due"] or counts["backstop_cleared"]:
        log.info("autorenew sweep: %s", counts)
    return counts


async def _get(session_factory: async_sessionmaker[AsyncSession], key: str, default):  # noqa: ANN001
    async with session_factory() as s:
        return await settings_service.get(s, key, default)


async def _held_hold_ids(session: AsyncSession, ids: list[int | None]) -> set[int]:
    """Of the given hold-txn ids, the subset that are still `kind='hold' AND status='held'`."""
    from app.models import StorefrontWalletTxn

    real = [i for i in ids if i is not None]
    if not real:
        return set()
    rows = (
        await session.execute(
            select(StorefrontWalletTxn.id).where(
                StorefrontWalletTxn.id.in_(real),
                StorefrontWalletTxn.kind == "hold",
                StorefrontWalletTxn.status == "held",
            )
        )
    ).scalars().all()
    return set(rows)


async def _live_confirm(
    session_factory: async_sessionmaker[AsyncSession], order_id: int,
    fire_gb: float, fire_days: int,
) -> bool:
    """Re-check the order against a LIVE panel read right before firing. Returns True to fire. A
    failed live read returns True (trust the snapshot that already flagged it — don't drop a due fire
    on a transient panel blip)."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or not is_armed(order) or order.status != "provisioned":
            return False
        sf, _customer, _reseller, _panel = await _panel_ctx(s, order)
        if sf is None:
            return False
        ls = await live_status(s, sf, order)
    if not ls.ok:
        return True   # best-effort — the snapshot already said it's due
    remaining_gb = float(ls.limit_gb) - float(ls.used_gb)
    return _fire_due(remaining_gb, ls.remaining_days, fire_gb, fire_days)
