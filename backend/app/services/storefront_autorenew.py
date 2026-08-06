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
from app.services import (
    broadcast,
    settings_service,
    storefront,
    storefront_pricing,
    storefront_wallet,
)
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

# Arm requires a LIVE config. A paused (`disabled`) order used to be armable on the promise that it
# would "fire once resumed" — but the sweep selects `status == "provisioned"` only, so the arm just
# sat there with the customer's money reserved and no renewal ever came, silently, until they thought
# to resume. Three real orders were stuck that way. Refusing at arm time is the honest half of the
# fix; widening the sweep to `disabled` is the WRONG half, because a renewal PATCHes `enable: True`
# and would quietly undo a shop admin's deliberate pause.
_ARMABLE = ("provisioned",)
# Cap candidates processed per sweep tick (fire does panel + wallet I/O under a lock).
_SWEEP_LIMIT = 100


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class ArmResult:
    ok: bool
    reason: str | None = None    # not_found | trial | already | insufficient | paused | error
    price: int = 0
    short_toman: int = 0


def is_armed(order: StorefrontOrder) -> bool:
    return order.autorenew_armed_at is not None


async def _plan_price(s: AsyncSession, order: StorefrontOrder) -> int:
    """The price to lock: the current plan's price if the plan still exists, else the price the
    order last carried (a DELETED plan still renews at its last known price).

    `enabled` is deliberately NOT consulted — it used to be, which meant a plan disabled for
    underpricing kept renewing at the stale snapshot price and a repricing could never reach it.
    Matches `storefront_subscription.renew`, which reads gb/days from the live plan the same way."""
    plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
    if plan:
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
    sf, customer, reseller, _panel = await _panel_ctx(session, order0)
    if not (sf and customer):
        return ArmResult(False, "error")
    if expected_sf_id is not None and sf.id != expected_sf_id:
        return ArmResult(False, "not_found")   # tenant guard — never touch another shop's order
    async with _customer_lock(sf.id, customer.id):
        order = await _order_for_update(session, order_id)
        if order is None:
            return ArmResult(False, "not_found")
        if order.status == "disabled":
            # Distinct from not_found on purpose: the customer owns a real service and the remedy is
            # a single tap («▶️ فعال‌سازی»), so tell them that instead of a dead end.
            return ArmResult(False, "paused")
        if order.status not in _ARMABLE:
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
        # Refuse BEFORE any hold is placed, so a broken price never reserves the customer's money
        # for a renewal the fire job would then have to refuse anyway.
        if reseller is not None and storefront_pricing.is_below_cost(
            cost=await storefront_pricing.cost_per_gb(session, reseller),
            gb=int(order.gb), price_toman=price,
        ):
            return ArmResult(False, "below_cost")
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
    return {"checked": 0, "backstop_cleared": 0, "due": 0, "fired": 0, "failed": 0, "skipped": 0,
            "below_cost_disarmed": 0}


def _post_fire_msg(label: str | None) -> str:
    name = f"«{label}» " if label else ""
    return (
        f"🔁 سرویس {name}شما رو به پایان بود و چون «تمدید خودکار» را روشن کرده بودید، "
        "به‌صورت خودکار یک دوره تمدید شد و بدون قطعی ادامه یافت."
    )


async def _suspended_bot_ids(session: AsyncSession, bots: list[StorefrontBot]) -> set[int]:
    """Storefront-bot ids whose owning reseller branch is under a live suspension. Fails OPEN (empty
    set) so a lookup error can't stop legitimate renewals for every shop."""
    try:
        from app.models import Reseller
        from app.services import enforcement

        blocked: set[int] = set()
        checked: dict[int, bool] = {}
        for sf_bot in bots:
            if sf_bot.id in blocked:
                continue
            cached = checked.get(sf_bot.reseller_id)
            if cached is None:
                reseller = await session.get(Reseller, sf_bot.reseller_id)
                cached = await enforcement.suspended_branch_root(session, reseller) is not None
                checked[sf_bot.reseller_id] = cached
            if cached:
                blocked.add(sf_bot.id)
        return blocked
    except Exception:  # noqa: BLE001
        log.warning("autorenew suspension filter failed", exc_info=True)
        return set()


async def _reseller_costs(session: AsyncSession, bots: list[StorefrontBot]) -> dict[int, int]:
    """Cost-per-GB for each shop id, cached per reseller. Fails OPEN (empty map) so a lookup error
    can never disarm a healthy customer's auto-renew."""
    try:
        from app.models import Reseller

        costs: dict[int, int] = {}
        per_reseller: dict[int, int] = {}
        for sf_bot in bots:
            if sf_bot.id in costs:
                continue
            cached = per_reseller.get(sf_bot.reseller_id)
            if cached is None:
                reseller = await session.get(Reseller, sf_bot.reseller_id)
                if reseller is None:
                    continue
                cached = await storefront_pricing.cost_per_gb(session, reseller)
                per_reseller[sf_bot.reseller_id] = cached
            costs[sf_bot.id] = cached
        return costs
    except Exception:  # noqa: BLE001
        log.warning("autorenew cost lookup failed", exc_info=True)
        return {}


def _below_cost_msg(label: str | None) -> str:
    """Told to the CUSTOMER when their armed renewal is cancelled because the shop's plan is priced
    below the reseller's own cost. Never names the reseller's cost — that is the shop's business."""
    name = f"«{label}» " if label else ""
    return (
        f"🔁 «تمدید خودکارِ» سرویس {name}شما خاموش شد و مبلغِ رزروشده به کیفِ پولِ شما بازگشت.\n"
        "فروشگاه در حالِ به‌روزرسانیِ قیمت‌هاست؛ پس از اصلاح می‌توانید دوباره آن را روشن کنید."
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
                    StorefrontBot.status == "active",
                )
            )
        ).all()
        if not rows:
            await s.commit()
            return counts
        snaps = await _load_snaps(s, rows)
        held = await _held_hold_ids(s, [o.autorenew_hold_txn_id for o, _c, _b in rows])
        today = tehran_today()
        # Shops whose reseller branch is suspended fire nothing: a renewal PATCHes the panel user
        # with `enable: True`, so an unattended sweep would quietly put a suspended debtor's users
        # back online. The arm is LEFT INTACT (not disarmed, the customer's hold is untouched) — once
        # the reseller pays and is restored, the next sweep fires it normally.
        suspended_bots = await _suspended_bot_ids(s, [b for _o, _c, b in rows])
        costs = await _reseller_costs(s, [b for _o, _c, b in rows])
        work: list[tuple[int, str | None, str, int, int]] = []  # order_id, label, token, chat, bot_id
        refunded: list[tuple[str | None, str, int, int]] = []   # label, token, chat, bot_id
        for order, customer, sf_bot in rows:
            counts["checked"] += 1
            # Backstop: armed but the hold is no longer `held` → the arm is spent (a reconciler
            # finished the fire) or was released; clear the dangling armed columns.
            if order.autorenew_hold_txn_id not in held:
                await disarm_quietly(s, order.id)
                counts["backstop_cleared"] += 1
                continue
            if sf_bot.id in suspended_bots:
                counts["skipped"] += 1
                continue
            # Unlike suspension (transient — the reseller pays and is restored, so the hold is kept),
            # a below-cost price may never be fixed. `renew` would refuse this fire anyway, so
            # holding the customer's money indefinitely for a renewal that can never happen is worse
            # than refunding it now. Disarming is idempotent and makes the notice naturally one-shot.
            cost = costs.get(sf_bot.id)
            if cost is not None and storefront_pricing.is_below_cost(
                cost=cost, gb=int(order.gb), price_toman=int(order.autorenew_price_toman or 0)
            ):
                token = storefront.bot_token(sf_bot)
                if await disarm_quietly(s, order.id):
                    counts["below_cost_disarmed"] += 1
                    if token:
                        refunded.append((order.label, token, customer.telegram_id, sf_bot.id))
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

    if not work and not refunded:
        return counts

    limiter = broadcast.rate_limiter()
    bots: dict[int, Bot] = {}

    async def _shop_bot(bot_id: int, token: str) -> Bot | None:
        bot = bots.get(bot_id)
        if bot is None:
            try:
                bot = await bot_factory(token)
            except Exception:  # noqa: BLE001 — bad token: the money move already happened
                log.warning("autorenew: bot init failed (sf %s)", bot_id)
                return None
            bots[bot_id] = bot
        return bot

    try:
        for label, token, chat_id, bot_id in refunded:
            bot = await _shop_bot(bot_id, token)
            if bot is not None:
                await broadcast.send_with_flood_control(
                    bot, chat_id, _below_cost_msg(label), limiter)
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
                bot = await _shop_bot(bot_id, token)
                if bot is None:
                    continue
                await broadcast.send_with_flood_control(
                    bot, chat_id, _post_fire_msg(label), limiter)
            elif res.reason == "processing":
                counts["skipped"] += 1   # reconciler owns it; the next sweep confirms
            elif res.reason == "below_cost":
                # Phase 1 screens on the order's own gb; `renew` is authoritative and reads the
                # LIVE plan, so a plan resized after the arm can only be caught here. Same remedy.
                async with session_factory() as s2:
                    disarmed = await disarm_quietly(s2, order_id)
                if disarmed:
                    counts["below_cost_disarmed"] += 1
                    bot = await _shop_bot(bot_id, token)
                    if bot is not None:
                        await broadcast.send_with_flood_control(
                            bot, chat_id, _below_cost_msg(label), limiter)
            else:
                counts["failed"] += 1
                log.warning("autorenew fire failed for order %s: %s", order_id, res.reason)
    finally:
        for bot in bots.values():
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
    if counts["due"] or counts["backstop_cleared"] or counts["below_cost_disarmed"]:
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
