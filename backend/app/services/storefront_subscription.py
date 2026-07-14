"""Subscription lifecycle for storefront orders: renew (charge the wallet at the CURRENT plan price,
reset the panel config IN PLACE so the link/QR keep working), delete (remove the panel config), and
pause/resume. Short DB sessions + the per-customer lock (shared with the purchase path); no connection
is held across the panel I/O. Ownership is enforced by the caller (handlers)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront_wallet
from app.services.panel_client.admin_api import AdminApiClient
from app.services.storefront_provision import _customer_lock

log = logging.getLogger("bot.storefront")

_ACTIVE = ("provisioned", "disabled")


@dataclass
class SubResult:
    ok: bool
    reason: str | None = None      # not_found | insufficient | error
    price: int = 0
    gb: int = 0
    days: int = 0
    short_toman: int = 0
    message: str | None = None


async def _panel_ctx(s: AsyncSession, order: StorefrontOrder):  # noqa: ANN001, ANN202
    """Resolve (sf, customer, reseller, panel) for an order, or (None, …) if anything is missing."""
    customer = await s.get(StorefrontCustomer, order.customer_id)
    sf = await s.get(StorefrontBot, customer.storefront_bot_id) if customer else None
    reseller = await s.get(Reseller, sf.reseller_id) if sf else None
    panel = await s.get(Panel, reseller.panel_id) if reseller else None
    return sf, customer, reseller, panel


async def _order_for_update(s: AsyncSession, order_id: int) -> StorefrontOrder | None:
    """Fetch an order under a write lock (Postgres FOR UPDATE; no-op on SQLite)."""
    stmt = select(StorefrontOrder).where(StorefrontOrder.id == order_id)
    try:
        stmt = stmt.with_for_update()
    except Exception:  # noqa: BLE001 — dialect without row locks
        pass
    return (await s.execute(stmt)).scalar_one_or_none()


async def renew(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
    by_admin: bool = False,
) -> SubResult:
    """Renew a subscription in place at the CURRENT plan price (admin renews are free grants).

    F4-renewal (idempotency): the order is locked and CAS-flipped to "renewing" up front, so a rapid
    double-tap that slips past the button-strip blocks on the row lock, then sees "renewing" (not
    renewable) and bails — never a second charge. The status is restored on every exit; a hard crash
    mid-renew leaves it "renewing" until the pending-order reaper reverts it.

    F11 (charge ordering): funds are DEBITED FIRST, then the panel is renewed; if the panel write
    fails the charge is compensated via a distinct `renew_reversal` (never leaving a customer charged
    for a service they didn't get), instead of the old renew-first-charge-last (which could silently
    grant a free renewal)."""
    # 1. Claim the renewal atomically.
    async with session_factory() as s:
        order = await _order_for_update(s, order_id)
        if order is None or order.status not in _ACTIVE:
            return SubResult(False, "not_found")
        # Free trials are one-time and NEVER renewable (price==0 bypassed the wallet charge → a
        # perpetual free config). Covers customer + by_admin.
        if order.is_trial:
            return SubResult(False, "trial")
        prior_status = order.status
        order.status = "renewing"
        await s.commit()

    async def _restore() -> None:
        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is not None and o.status == "renewing":
                o.status = prior_status
                await s.commit()

    try:
        async with session_factory() as s:
            order = await s.get(StorefrontOrder, order_id)
            if order is None:
                await _restore()
                return SubResult(False, "not_found")
            sf, customer, reseller, panel = await _panel_ctx(s, order)
            if not (sf and customer and reseller and panel and order.panel_user_uuid):
                await _restore()
                return SubResult(False, "error")
            plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
            gb = int(plan.gb) if plan else int(order.gb)
            days = int(plan.days) if plan else int(order.days)
            price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
            sf_id, customer_id = sf.id, customer.id
            uuid, api_key = order.panel_user_uuid, reseller.admin_uuid
            reseller_id, panel_id = reseller.id, panel.id

        async with _customer_lock(sf_id, customer_id):
            charged = False
            # F11: DEBIT FIRST (under the per-customer lock so the balance can't drop underneath us).
            if not by_admin and price > 0:
                async with session_factory() as s:
                    ok, _ = await storefront_wallet.charge_purchase(
                        s, customer_id, price, order_id=order_id)
                    if not ok:
                        await s.rollback()
                        await _restore()
                        async with session_factory() as s2:
                            c = await s2.get(StorefrontCustomer, customer_id)
                            bal = int(storefront_wallet.balance(c)) if c else 0
                        return SubResult(False, "insufficient", price=price,
                                         short_toman=max(0, price - bal))
                    await s.commit()
                    charged = True

            # Renew on the panel; on failure, COMPENSATE the debit and restore the order.
            try:
                async with session_factory() as s:
                    reseller = await s.get(Reseller, reseller_id)
                    panel = await s.get(Panel, panel_id)
                    if reseller is None or panel is None:
                        raise RuntimeError("panel/reseller vanished")
                    await AdminApiClient().renew_user(panel, uuid, gb=gb, days=days, api_key=api_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("renew_user failed for order %s", order_id, exc_info=True)
                if charged:
                    async with session_factory() as s:
                        await storefront_wallet.reverse_charge(
                            s, customer_id, price, order_id=order_id)
                        await s.commit()
                await _restore()
                return SubResult(False, "error", message=str(exc)[:200])

        # Success — finalize the order.
        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is not None:
                o.status = "provisioned"
                o.gb, o.days = gb, days
                o.last_renewed_at = dt.datetime.now(dt.timezone.utc)
                await s.commit()
        return SubResult(True, price=(0 if by_admin else price), gb=gb, days=days)
    except Exception:
        log.exception("renew crashed for order %s; restoring status", order_id)
        await _restore()
        raise


async def delete_subscription(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
) -> SubResult:
    """Remove the panel config and mark the order deleted (no refund). Idempotent."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.status in ("deleted", "failed"):
            return SubResult(False, "not_found")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        uuid, api_key = order.panel_user_uuid, (reseller.admin_uuid if reseller else None)
        panel_id = panel.id if panel else None
    if panel_id and uuid:
        try:
            async with session_factory() as s:
                panel = await s.get(Panel, panel_id)
                if panel is not None:
                    await AdminApiClient().delete_user(panel, uuid, api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_user failed for order %s", order_id, exc_info=True)
            return SubResult(False, "error", message=str(exc)[:200])
    async with session_factory() as s:
        o = await s.get(StorefrontOrder, order_id)
        if o is not None:
            o.status = "deleted"
            await s.commit()
    return SubResult(True)


async def set_enabled(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
    enabled: bool,
) -> SubResult:
    """Pause (disable) or resume (enable) the panel config; reflect it in the order status."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.status not in _ACTIVE:
            return SubResult(False, "not_found")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        uuid, api_key = order.panel_user_uuid, (reseller.admin_uuid if reseller else None)
        panel_id = panel.id if panel else None
    if not (panel_id and uuid):
        return SubResult(False, "error")
    try:
        async with session_factory() as s:
            panel = await s.get(Panel, panel_id)
            if panel is None:
                return SubResult(False, "error")
            await AdminApiClient().set_user_enabled(panel, uuid, enabled, api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("set_user_enabled failed for order %s", order_id, exc_info=True)
        return SubResult(False, "error", message=str(exc)[:200])
    async with session_factory() as s:
        o = await s.get(StorefrontOrder, order_id)
        if o is not None:
            o.status = "provisioned" if enabled else "disabled"
            await s.commit()
    return SubResult(True)


async def reset_over_renewed_trials(
    session: AsyncSession, *, limit: int = 5000
) -> dict[str, int]:
    """One-time cleanup: free-trial configs that were renewed (a now-fixed bug) had their panel
    `usage_limit_GB` ratcheted up (1→2→3…). Reset each back to an EXACT 1 GB via the admin API.

    NOTE: renewing a trial left `order.gb` at 1 (renew set `gb = order.gb`); only the panel quota
    grew. So over-renewed trials are identified by the LATEST SNAPSHOT's `usage_limit_gb > 1`
    (joined on panel_id + user_uuid), not `order.gb`. Then the live panel quota is confirmed before
    the reset (idempotent: a config already ≤1 GB is skipped). Per-order try/except so one bad panel
    never aborts the sweep. Returns {checked, reset, skipped, failed}."""
    from app.models import EndUserSnapshot
    from app.services import storefront_provision

    counts = {"checked": 0, "reset": 0, "skipped": 0, "failed": 0}
    orders = (
        await session.execute(
            select(StorefrontOrder)
            .join(
                EndUserSnapshot,
                and_(
                    EndUserSnapshot.panel_id == StorefrontOrder.panel_id,
                    EndUserSnapshot.user_uuid == StorefrontOrder.panel_user_uuid,
                ),
            )
            .where(
                StorefrontOrder.is_trial.is_(True),
                StorefrontOrder.status.in_(_ACTIVE),
                StorefrontOrder.panel_user_uuid.is_not(None),
                EndUserSnapshot.usage_limit_gb > 1,
            ).limit(limit)
        )
    ).scalars().all()
    client = AdminApiClient()
    for order in orders:
        counts["checked"] += 1
        try:
            customer = await session.get(StorefrontCustomer, order.customer_id)
            sf = await session.get(StorefrontBot, customer.storefront_bot_id) if customer else None
            reseller = await session.get(Reseller, sf.reseller_id) if sf else None
            panel = await session.get(Panel, reseller.panel_id) if reseller else None
            uuid = order.panel_user_uuid
            if not (sf and reseller and panel and uuid):
                counts["skipped"] += 1
                continue
            live = await storefront_provision.live_status(session, sf, order)
            if not live.ok:
                counts["failed"] += 1   # panel read failed → leave it, retry next run
                continue
            if live.limit_gb > 1.0:
                await client.patch_user(
                    panel, uuid, {"usage_limit_GB": 1.0}, api_key=reseller.admin_uuid)
                log.info("reset trial quota: order=%s panel=%s uuid=%s %.3f->1.0",
                         order.id, panel.id, uuid, live.limit_gb)
                counts["reset"] += 1
            else:
                counts["skipped"] += 1  # snapshot was stale; panel already ≤1 GB
        except Exception:  # noqa: BLE001 — never crash the loop (sync_all convention)
            await session.rollback()
            log.warning("reset_over_renewed_trials failed for order %s", order.id, exc_info=True)
            counts["failed"] += 1
            continue
    log.info("reset_over_renewed_trials: %s", counts)
    return counts
