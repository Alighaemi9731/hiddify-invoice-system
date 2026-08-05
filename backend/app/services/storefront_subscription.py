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
from app.services import (
    enforcement,
    storefront_ops,
    storefront_pricing,
    storefront_wallet,
)
from app.services.panel_client.admin_api import AdminApiClient
from app.services.storefront_provision import _customer_lock, _lease_active

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
    expected_sf_id: int | None = None,
    prepaid_hold_txn_id: int | None = None,
    locked_price_toman: int | None = None,
) -> SubResult:
    """Renew a subscription in place at the CURRENT plan price (admin renews are free grants).

    Deferred auto-renew (`prepaid_hold_txn_id` + `locked_price_toman`): when the customer armed
    auto-renew the plan price was already reserved as a wallet `hold` at THAT time. The fire job passes
    the hold id and the locked price, so instead of charging again this SETTLES the hold (the money is
    already out) and bills the locked price, then clears the order's armed columns on success (one-shot).

    F4 (idempotency): the renewal is a durable StorefrontOperation keyed by `op_id` (minted before the
    confirm button). A repeat call with the SAME `op_id` — a double-tap, a re-delivered callback, a
    cross-process retry — returns the CACHED result with no second debit. When no `op_id` is given
    (admin free renews) one is minted per call (free, so no double-charge risk).

    F11 (charge ordering + crash safety): an absolute panel target is persisted before the debit, then
    applied after it. If the process dies or the response is ambiguous, the order stays `renewing` with
    a lease; the reconciler verifies or idempotently reapplies that same target and finalizes the charge.
    A definitive missing panel user is compensated exactly once via `renew_reversal`."""
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
        # Defense-in-depth tenant guard: the shared command resolves ownership too, but making the
        # command itself refuse a foreign shop means no caller can act cross-tenant with a bare id.
        if expected_sf_id is not None and sf.id != expected_sf_id:
            return SubResult(False, "not_found")
        # A renewal PATCHes the panel user with `enable: True` (see RenewUserTarget.body), so a
        # suspended branch must not renew: it would resurrect a disabled user and leave no
        # enforcement trace. Checked before any durable operation/debit is created.
        if await enforcement.suspended_branch_root(s, reseller) is not None:
            return SubResult(False, "suspended")
        plan = await s.get(StorefrontPlan, order.plan_id) if order.plan_id else None
        gb = int(plan.gb) if plan else int(order.gb)
        days = int(plan.days) if plan else int(order.days)
        # gb/days have always come from the LIVE plan whenever the row still exists, `enabled` or
        # not — so taking the price from a different vintage (the original purchase snapshot) was
        # the real bug: a plan disabled for being below cost kept delivering its quota at the old
        # loss-making price, and REPRICING it could never take effect. One row, one vintage.
        price = int(plan.price_toman) if plan else int(order.price_toman)
        if locked_price_toman is not None:   # auto-renew bills the price locked at arm time, not the
            price = int(locked_price_toman)   # current plan price (which may have changed since arming).
        # After the locked-price override on purpose: a price locked at arm time must not slip a
        # below-cost renewal through the back door.
        cost = await storefront_pricing.cost_per_gb(s, reseller)
        if storefront_pricing.is_below_cost(cost=cost, gb=gb, price_toman=price):
            # Carries the full Persian explanation because an ADMIN-initiated renewal surfaces
            # `message` directly (see storefront_admin's order-command result handling).
            return SubResult(False, "below_cost", message=storefront_pricing.below_cost_message_fa(
                cost=cost, gb=gb, price_toman=price))
        sf_id, customer_id = sf.id, customer.id
        uuid, api_key = order.panel_user_uuid, reseller.admin_uuid
        panel_id = panel.id
        prior_status = order.status

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
            if outcome == storefront_ops.CONFLICT:
                return SubResult(False, "error")
            if op is None or outcome == storefront_ops.IN_FLIGHT:
                return SubResult(False, "processing")
            op_pk = op.id
            order = await _order_for_update(s, order_id)
            if order is None or order.status not in _ACTIVE:
                await storefront_ops.finalize(s, op, "failed")
                await s.commit()
                return SubResult(False, "not_found")
            order.status = "renewing"
            # Mint the lease only after the per-customer wait and row claim; otherwise time spent queued
            # behind an earlier operation could consume the lease before this renewal even starts.
            order.lease_expires_at = await storefront_ops.lease_deadline(s)
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

        # 2. Prepare and persist an ABSOLUTE, replayable panel target before any debit. A process death
        #    after the remote PATCH can then be reconciled to service+charge instead of a blind refund.
        client = AdminApiClient()
        try:
            async with session_factory() as s:
                panel = await s.get(Panel, panel_id)
            if panel is None:
                raise RuntimeError("panel vanished")
            target = await client.prepare_renew_user(
                panel, uuid, gb=gb, days=days, api_key=api_key)
            async with session_factory() as s:
                op2 = await s.get(StorefrontOperation, op_pk)
                if op2 is None or op2.status != "in_progress":
                    raise RuntimeError("renewal operation vanished")
                op2.target_usage_limit_gb = target.usage_limit_gb
                op2.prior_panel_start_date = target.prior_start_date
                await s.commit()
        except Exception as exc:  # noqa: BLE001 — preparation happens before charging, so fail cleanly
            log.warning("renew target preparation failed for order %s", order_id, exc_info=True)
            await _restore_and_finalize("failed")
            return SubResult(False, "error", message=str(exc)[:200])

        # 3. Debit FIRST (linked to the operation), committed with the debit_txn link.
        if not by_admin and price > 0:
            async with session_factory() as s:
                if prepaid_hold_txn_id is not None:
                    # Auto-renew: the money was already debited at arm time (a wallet hold). SETTLE that
                    # hold into the renewal purchase instead of charging again. `settle_hold` returns
                    # None only if the hold is no longer `held` (a concurrent disarm released it), in
                    # which case there's no reserved money — fail cleanly rather than grant free service.
                    txn = await storefront_wallet.settle_hold(
                        s, prepaid_hold_txn_id, operation_id=op_pk)
                    if txn is None:
                        await s.rollback()
                        await _restore_and_finalize("failed")
                        return SubResult(False, "error", message="hold missing")
                else:
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

        # 4. Apply the persisted target. An exception is AMBIGUOUS (the remote PATCH may have committed
        #    before the connection failed), so leave the durable operation in-progress. The reconciler
        #    verifies/reapplies the same absolute target after the lease instead of issuing a blind refund.
        try:
            async with session_factory() as s:
                panel = await s.get(Panel, panel_id)
            if panel is None:
                raise RuntimeError("panel vanished")
            await client.apply_renew_user_target(panel, uuid, target, api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("renew target apply uncertain for order %s", order_id, exc_info=True)
            return SubResult(False, "processing", message=str(exc)[:200])

        # 5. Success — finalize the order + operation, but ONLY if the order is still ours.
        #    This write used to be unconditional, so a delete that landed mid-renewal was undone:
        #    the order flipped back to `provisioned` and looked live in «سرویس‌های من» while its
        #    panel config no longer existed. `_restore_and_finalize` above already guards its own
        #    write this way; the success path was the one that did not.
        async with session_factory() as s:
            o = await _order_for_update(s, order_id)
            if o is None or o.status != "renewing":
                # Somebody terminal-ised the order while the panel call was in flight. The quota was
                # granted to a config that has since been removed, so the charge must come back —
                # `renew_reversal` is exactly-once, so this cannot double-refund a reconciler.
                log.warning(
                    "renew finished but order %s left `renewing` (now %s) — reversing the charge",
                    order_id, getattr(o, "status", "gone"),
                )
                op2 = await s.get(StorefrontOperation, op_pk)
                if op2 is not None:
                    await storefront_ops.finalize(s, op2, "reversed", result_order_id=order_id)
                await s.commit()
                if not by_admin and price > 0:
                    async with session_factory() as s2:
                        # Keyed on the OPERATION, so the reconciler and this path cannot both
                        # credit — `uq_sfwallet_reversal_per_operation` is the durable backstop.
                        await storefront_wallet.reverse_charge(
                            s2, customer_id, price, order_id=order_id, operation_id=op_pk,
                            note="بازگشتِ وجهِ تمدیدِ لغوشده",
                        )
                        await s2.commit()
                return SubResult(False, "cancelled", price=price, gb=gb, days=days)
            o.status = "provisioned"
            o.gb, o.days = gb, days
            o.last_renewed_at = dt.datetime.now(dt.timezone.utc)
            o.lease_expires_at = None
            if prepaid_hold_txn_id is not None:
                # One-shot: the arm is spent. Clear it ATOMICALLY with the renewal so the order never
                # lingers "armed" with an already-settled hold (must be re-armed for the next cycle).
                o.autorenew_armed_at = None
                o.autorenew_price_toman = None
                o.autorenew_hold_txn_id = None
            op2 = await s.get(StorefrontOperation, op_pk)
            if op2 is not None:
                await storefront_ops.finalize(s, op2, "done", result_order_id=order_id)
            await s.commit()
        return SubResult(True, price=(0 if by_admin else price), gb=gb, days=days)


async def delete_subscription(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    order_id: int,
    expected_sf_id: int | None = None,
) -> SubResult:
    """Remove the panel config and mark the order deleted (no refund). Idempotent.

    Refuses to touch an order that a purchase or renewal is still working on. The old guard only
    excluded `deleted`/`failed`, so `pending` and `renewing` sailed through — and deleting one of
    those cost the customer real money:

      * mid-PURCHASE the debit is already committed, and `purchase` interprets any non-`pending`
        status as "the reaper got here first, which means it refunded" — so nothing refunded, while
        the customer was told their money had been returned;
      * mid-RENEWAL the panel config is destroyed and `renew`'s final write then flips the order
        back to `provisioned`, resurrecting a service that no longer exists on the panel.

    So: take the same per-customer lock those flows hold, re-read the row under a write lock, and
    refuse while a provisioning lease is live. The tenant check stays FIRST, so a cross-tenant
    caller still gets `not_found` and never learns whether an order is mid-purchase.
    """
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None:
            return SubResult(False, "not_found")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        if expected_sf_id is not None and (sf is None or sf.id != expected_sf_id):
            return SubResult(False, "not_found")
        if sf is None or customer is None:
            return SubResult(False, "not_found")
        sf_id, customer_id = sf.id, customer.id

    async with _customer_lock(sf_id, customer_id):
        async with session_factory() as s:
            order = await _order_for_update(s, order_id)
            if order is None or order.status in ("deleted", "failed"):
                return SubResult(False, "not_found")
            if order.status not in _ACTIVE or _lease_active(
                order.lease_expires_at, dt.datetime.now(dt.timezone.utc)
            ):
                # A live purchase/renewal owns this row. Let it finish; the customer can delete
                # afterwards. Refusing is the only answer that cannot lose money.
                return SubResult(
                    False, "busy",
                    message="این سرویس هم‌اکنون در حالِ پردازش است؛ لطفاً چند لحظه بعد دوباره تلاش کنید.",
                )
            uuid = order.panel_user_uuid
            api_key = reseller.admin_uuid if reseller else None
            panel_id = panel.id if panel else None
            # Auto-renew hold: this config is being destroyed, so return any reserved renewal funds to
            # the wallet (under the same lock + row lock, committed before the panel delete). Without
            # this the customer's money would stay reserved on a config that no longer exists.
            if order.autorenew_armed_at is not None:
                if order.autorenew_hold_txn_id is not None:
                    await storefront_wallet.release_hold(s, order.autorenew_hold_txn_id)
                order.autorenew_armed_at = None
                order.autorenew_price_toman = None
                order.autorenew_hold_txn_id = None
                await s.commit()
        return await _delete_panel_user_and_mark(
            session_factory, order_id=order_id, uuid=uuid, api_key=api_key, panel_id=panel_id
        )


async def _delete_panel_user_and_mark(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    uuid: str | None,
    api_key: str | None,
    panel_id: int | None,
) -> SubResult:
    """Panel delete (network I/O, no DB transaction held) then mark the order deleted."""
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
    expected_sf_id: int | None = None,
) -> SubResult:
    """Pause (disable) or resume (enable) the panel config; reflect it in the order status."""
    async with session_factory() as s:
        order = await s.get(StorefrontOrder, order_id)
        if order is None or order.status not in _ACTIVE:
            return SubResult(False, "not_found")
        sf, customer, reseller, panel = await _panel_ctx(s, order)
        if expected_sf_id is not None and (sf is None or sf.id != expected_sf_id):
            return SubResult(False, "not_found")
        # A RESUME re-enables a panel user, so it must never run while the owning branch is
        # suspended — that is precisely how a suspended reseller's shop put users back online with
        # no enforcement record. Pausing stays allowed: it only ever takes a user OFF.
        if enabled and await enforcement.suspended_branch_root(s, reseller) is not None:
            return SubResult(False, "suspended")
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
