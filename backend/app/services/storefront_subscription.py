"""Subscription lifecycle for storefront orders: renew (charge the wallet at the CURRENT plan price,
reset the panel config IN PLACE so the link/QR keep working), delete (remove the panel config), and
pause/resume. Short DB sessions + the per-customer lock (shared with the purchase path); no connection
is held across the panel I/O. Ownership is enforced by the caller (handlers)."""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront_ops, storefront_wallet
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


def _cached_renew_result(op: StorefrontOperation) -> SubResult:
    """Build the SubResult for an idempotent REPLAY of a terminal renewal operation (no re-charge)."""
    if op.status == "done":
        return SubResult(True, price=(0 if op.by_admin else int(op.price_toman)),
                         gb=int(op.gb), days=int(op.days))
    return SubResult(False, "error")   # failed / reversed → the renewal did not ultimately succeed


async def renew(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
    by_admin: bool = False,
    op_id: str | None = None,
) -> SubResult:
    """Renew a subscription in place at the CURRENT plan price (admin renews are free grants).

    F4 (idempotency): the renewal is a durable StorefrontOperation keyed by `op_id` (minted before the
    confirm button). A repeat call with the SAME `op_id` — a double-tap, a re-delivered callback, a
    cross-process retry — returns the CACHED result with no second debit. When no `op_id` is given
    (admin free renews) one is minted per call (free, so no double-charge risk).

    F11 (charge ordering + crash safety): funds are DEBITED FIRST (linked to the operation), then the
    panel is renewed; on a panel failure the charge is reversed via a distinct `renew_reversal`. If the
    process crashes between the debit and the finalize (e.g. task cancellation, which `except Exception`
    can't catch), the order stays `renewing` with a LEASE and the reconciler (reap_pending_orders)
    reverses the charge exactly once and restores the order — reverse-on-uncertainty, so a customer is
    never left charged for a renewal they didn't get (renew_user isn't post-hoc idempotent)."""
    op_id = op_id or secrets.token_urlsafe(16)

    # 0. Read-only validation + context (before we create any durable state).
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.status not in _ACTIVE:
            return SubResult(False, "not_found")
        if order.is_trial:   # trials are one-time, never renewable
            return SubResult(False, "trial")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        if not (sf and customer and reseller and panel and order.panel_user_uuid):
            return SubResult(False, "error")
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        gb = int(plan.gb) if plan else int(order.gb)
        days = int(plan.days) if plan else int(order.days)
        price = int(plan.price_toman) if (plan and plan.enabled) else int(order.price_toman)
        sf_id, customer_id = sf.id, customer.id
        uuid, api_key = order.panel_user_uuid, reseller.admin_uuid
        reseller_id, panel_id = reseller.id, panel.id
        prior_status = order.status
        lease_at = await storefront_ops.lease_deadline(s)

    async with _customer_lock(sf_id, customer_id):
        # 1. Claim the operation + CAS the order to "renewing" (with a lease), atomically.
        async with session_factory() as s:
            op, outcome = await storefront_ops.claim(
                s, op_id, op_type="renewal", storefront_bot_id=sf_id, customer_id=customer_id,
                order_id=order_id, plan_id=(plan.id if plan else None),
                price_toman=(0 if by_admin else price), gb=gb, days=days, by_admin=by_admin,
                prior_status=prior_status)
            if outcome == storefront_ops.REPLAY and op is not None:
                return _cached_renew_result(op)   # idempotent replay — no re-charge
            if op is None or outcome == storefront_ops.IN_FLIGHT:
                return SubResult(False, "processing")
            op_pk = op.id
            order = await _order_for_update(s, order_id)
            if order is None or order.status not in _ACTIVE:
                await storefront_ops.finalize(s, op, "failed")
                await s.commit()
                return SubResult(False, "not_found")
            order.status = "renewing"
            order.lease_expires_at = lease_at
            await s.commit()

        async def _restore_and_finalize(status: str) -> None:
            """Restore the order to its prior status and clear the lease; mark the op terminal."""
            async with session_factory() as s:
                o = await s.get(StorefrontOrder, order_id)
                if o is not None and o.status == "renewing":
                    o.status = prior_status
                    o.lease_expires_at = None
                op2 = await s.get(StorefrontOperation, op_pk)
                if op2 is not None:
                    op2.status = status
                await s.commit()

        # 2. Debit FIRST (linked to the operation), committed with the debit_txn link.
        if not by_admin and price > 0:
            async with session_factory() as s:
                ok, txn = await storefront_wallet.charge_purchase(
                    s, customer_id, price, order_id=order_id, operation_id=op_pk)
                if not ok:
                    await s.rollback()
                    await _restore_and_finalize("failed")
                    async with session_factory() as s2:
                        c = await s2.get(StorefrontCustomer, customer_id)
                        bal = int(storefront_wallet.balance(c)) if c else 0
                    return SubResult(False, "insufficient", price=price,
                                     short_toman=max(0, price - bal))
                op2 = await s.get(StorefrontOperation, op_pk)
                if op2 is not None and txn is not None:
                    op2.debit_txn_id = txn.id
                await s.commit()   # debit + operation_id link commit together (no charged-but-unlinked gap)

        # 3. Renew on the panel; on failure COMPENSATE (reverse the charge, restore the order).
        try:
            async with session_factory() as s:
                reseller = await s.get(Reseller, reseller_id)
                panel = await s.get(Panel, panel_id)
                if reseller is None or panel is None:
                    raise RuntimeError("panel/reseller vanished")
                await AdminApiClient().renew_user(panel, uuid, gb=gb, days=days, api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("renew_user failed for order %s", order_id, exc_info=True)
            if not by_admin and price > 0:
                async with session_factory() as s:
                    await storefront_wallet.reverse_charge(
                        s, customer_id, price, order_id=order_id, operation_id=op_pk)
                    await s.commit()
            await _restore_and_finalize("reversed" if (not by_admin and price > 0) else "failed")
            return SubResult(False, "error", message=str(exc)[:200])

        # 4. Success — finalize the order + operation.
        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is not None:
                o.status = "provisioned"
                o.gb, o.days = gb, days
                o.last_renewed_at = dt.datetime.now(dt.timezone.utc)
                o.lease_expires_at = None
            op2 = await s.get(StorefrontOperation, op_pk)
            if op2 is not None:
                await storefront_ops.finalize(s, op2, "done", result_order_id=order_id)
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
