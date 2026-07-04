"""Subscription lifecycle for storefront orders: renew (charge the wallet at the CURRENT plan price,
reset the panel config IN PLACE so the link/QR keep working), delete (remove the panel config), and
pause/resume. Short DB sessions + the per-customer lock (shared with the purchase path); no connection
is held across the panel I/O. Ownership is enforced by the caller (handlers)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
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


async def renew(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
    by_admin: bool = False,
) -> SubResult:
    """Renew a subscription in place at the CURRENT plan price (admin renews are free grants). Charges
    the wallet first (customer path), then resets the panel config; refunds if the panel write fails."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.status not in _ACTIVE:
            return SubResult(False, "not_found")
        # Free trials are one-time and NEVER renewable (renewing them was the abuse: price==0
        # bypassed the wallet charge, giving a perpetual free config). Covers customer + by_admin.
        if order.is_trial:
            return SubResult(False, "trial")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        if not (sf and customer and reseller and panel and order.panel_user_uuid):
            return SubResult(False, "error")
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        gb = int(plan.gb) if plan else int(order.gb)
        days = int(plan.days) if plan else int(order.days)
        price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        sf_id, customer_id, uuid, api_key = sf.id, customer.id, order.panel_user_uuid, reseller.admin_uuid

    async with _customer_lock(sf_id, customer_id):
        # Order: verify balance (under the per-customer lock, so it can't drop before we charge) → renew
        # on the panel → charge LAST. This way a crash can never leave a customer charged-but-not-renewed
        # (the worst case is a rare renewed-but-not-charged, a small reseller loss, never customer harm).
        if not by_admin and price > 0:
            async with session_factory() as s:
                customer = await s.get(StorefrontCustomer, customer_id)
                bal = int(storefront_wallet.balance(customer)) if customer else 0
            if bal < price:
                return SubResult(False, "insufficient", price=price, short_toman=price - bal)

        try:
            async with session_factory() as s:
                reseller = await s.get(Reseller, reseller.id)
                panel = await s.get(Panel, panel.id)
                if reseller is None or panel is None:
                    raise RuntimeError("panel/reseller vanished")
                await AdminApiClient().renew_user(panel, uuid, gb=gb, days=days, api_key=api_key)
        except Exception as exc:  # noqa: BLE001 — nothing charged yet, so just report failure
            log.warning("renew_user failed for order %s", order_id, exc_info=True)
            return SubResult(False, "error", message=str(exc)[:200])

        if not by_admin and price > 0:
            async with session_factory() as s:
                ok, _ = await storefront_wallet.charge_purchase(s, customer_id, price, order_id=order_id)
                if not ok:  # balance vanished between check and charge (e.g. admin debit) — already
                    await s.rollback()  # renewed; eat the rare loss rather than reverse a live config
                    log.warning("renew: panel renewed but charge failed for order %s", order_id)
                else:
                    await s.commit()

        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is not None:
                o.status = "provisioned"
                o.gb, o.days = gb, days
                o.last_renewed_at = dt.datetime.now(dt.timezone.utc)
                await s.commit()
        return SubResult(True, price=(0 if by_admin else price), gb=gb, days=days)


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
    `usage_limit_GB` ratcheted up (1→2→3…). Reset each back to an EXACT 1 GB via the admin API and
    normalize the stored `order.gb` to 1. Idempotent: only trials with `gb > 1` are selected, and a
    config whose panel is already ≤1 GB is skipped (its `gb` is still normalized so it won't
    re-match). Per-order try/except so one bad panel never aborts the sweep. A later panel sync
    refreshes the snapshot. Returns {checked, reset, skipped, failed}."""
    from app.services import storefront_provision

    counts = {"checked": 0, "reset": 0, "skipped": 0, "failed": 0}
    orders = (
        await session.execute(
            select(StorefrontOrder).where(
                StorefrontOrder.is_trial.is_(True),
                StorefrontOrder.status.in_(_ACTIVE),
                StorefrontOrder.panel_user_uuid.is_not(None),
                StorefrontOrder.gb > 1,
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
                order.gb = 1
                await session.commit()
                counts["reset"] += 1
            else:
                order.gb = 1  # panel already <=1; normalize stored gb so it won't re-select
                await session.commit()
                counts["skipped"] += 1
        except Exception:  # noqa: BLE001 — never crash the loop (sync_all convention)
            await session.rollback()
            log.warning("reset_over_renewed_trials failed for order %s", order.id, exc_info=True)
            counts["failed"] += 1
            continue
    log.info("reset_over_renewed_trials: %s", counts)
    return counts
