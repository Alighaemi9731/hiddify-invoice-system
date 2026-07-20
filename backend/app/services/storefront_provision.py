"""Provision a VPN config for a storefront purchase.

Reuses `usercreate.create_for_reseller`, which creates the user on the reseller's panel AS the
reseller (`api_key = reseller.admin_uuid`) — so the config counts toward the reseller's own usage that
the owner bills. Returns the subscription link + uuid, or a typed failure the caller turns into a
wallet refund + admin nudge.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import uuid as uuidlib
import weakref
from dataclasses import dataclass

from sqlalchemy import select
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
from app.services import storefront_ops, storefront_wallet, usercreate
from app.services.panel_client.admin_api import AdminApiClient, RenewUserTarget

log = logging.getLogger("bot.storefront")

# Order statuses that represent a live, renewable/restorable subscription (mirrors
# storefront_subscription._ACTIVE) — the reconciler restores a crashed renewal to one of these.
_ACTIVE = ("provisioned", "disabled")


def _lease_active(lease_expires_at: dt.datetime | None, now: dt.datetime) -> bool:
    """True if a provisioning lease is still in the future (F5). Normalizes a naive value to UTC —
    SQLite reads DateTime(timezone=True) back as naive, so a bare `>=` against an aware `now` raises."""
    if lease_expires_at is None:
        return False
    if lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=dt.timezone.utc)
    return lease_expires_at >= now


def _renewal_target_applied(
    data: dict, target: RenewUserTarget, prior_start_date: str | None,
) -> bool:
    """Whether the live panel row reflects a persisted renewal target.

    `start_date` is cleared by the PATCH, but Hiddify may set it again as soon as the customer connects.
    A changed start date therefore also proves application; quota/days/enable must always match.
    """
    raw_limit = data.get("usage_limit_GB")
    raw_days = data.get("package_days")
    if raw_limit is None or raw_days is None:
        return False
    try:
        limit_ok = abs(float(raw_limit) - target.usage_limit_gb) <= 0.001
        days_ok = int(raw_days) == target.package_days
    except (TypeError, ValueError):
        return False
    enabled = data.get("enable")
    if not limit_ok or not days_ok or enabled not in (True, 1, "true", "True"):
        return False
    current_start = data.get("start_date")
    if current_start in (None, ""):
        return True
    return str(current_start)[:32] != prior_start_date

# Serialize a single customer's money/provision actions in-process (buy / trial / renew) so a
# double-tap can't mint two configs or double-charge. The durable guards (row-lock debit, free-trial
# compare-and-set) back this up across processes. A WeakValueDictionary evicts each lock once it is
# released AND unreferenced (F15) — a WHILE-HELD lock is referenced by its `async with`, so it stays
# in the map and still serializes; only idle entries are GC'd, so the map can't grow with churn.
# The get-or-create runs synchronously in one event loop, so there's no create-race between coros.
_customer_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _customer_lock(sf_id: int, customer_id: int) -> asyncio.Lock:
    key = (sf_id, customer_id)
    lock = _customer_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _customer_locks[key] = lock
    return lock


async def _lock_order_skip(session: AsyncSession, order_id: int) -> StorefrontOrder | None:
    """Lock one order FOR UPDATE SKIP LOCKED (Postgres): returns the row if we got the lock, else
    None (it's being finalized by a live provision — leave it for the next tick). No-op on SQLite."""
    stmt = select(StorefrontOrder).where(StorefrontOrder.id == order_id)
    try:
        stmt = stmt.with_for_update(skip_locked=True)
    except Exception:  # noqa: BLE001 — dialect without row locks
        pass
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass
class ProvisionResult:
    ok: bool
    sub_link: str | None = None
    uuid: str | None = None
    reason: str | None = None      # "capacity" | "limit" | "error"
    message: str | None = None


@dataclass
class PurchaseResult:
    ok: bool
    order_id: int | None = None
    sub_link: str | None = None
    gb: int = 0
    days: int = 0
    label: str | None = None
    reason: str | None = None      # insufficient | plan_gone | disabled | used | capacity | limit | error
    short_toman: int = 0           # how much the wallet is short (reason == insufficient)
    message: str | None = None


@dataclass
class LiveStatus:
    ok: bool                       # False → the panel read failed; show the stored plan only
    used_gb: float = 0.0
    limit_gb: float = 0.0
    remaining_days: int | None = None


async def provision(
    session: AsyncSession,
    bot: StorefrontBot,
    customer: StorefrontCustomer,
    *,
    gb: int,
    days: int,
    label: str | None = None,
    user_uuid: str | None = None,
) -> ProvisionResult:
    """Create ONE config for this purchase on the reseller's chosen panel.

    `label` is the customer-chosen name: it becomes the config's name on the panel AND the sub-link
    `#slug`, so a customer who buys several services can tell them apart. `user_uuid` pins the config's
    uuid (the order pre-generates it so a mid-create crash is recoverable). Falls back to a
    name+telegram-id base when no label is supplied."""
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return ProvisionResult(False, reason="error", message="reseller not found")
    base = (label or "").strip() or f"{(customer.name or 'cust')[:16]}-{customer.telegram_id}"
    try:
        result = await usercreate.create_for_reseller(
            session, reseller, count=1, gb=int(gb), days=int(days), base_name=base,
            user_uuid=user_uuid,
        )
    except Exception as exc:  # noqa: BLE001 — surface any panel error as a typed failure (caller refunds)
        return ProvisionResult(False, reason="error", message=str(exc)[:300])
    if getattr(result, "error", None):
        return ProvisionResult(False, reason="error", message=str(result.error)[:300])
    if getattr(result, "capacity_blocked", False):
        return ProvisionResult(False, reason="capacity", message="capacity reached")
    if getattr(result, "limit_hit", False) or not result.created:
        return ProvisionResult(False, reason="limit", message="panel user limit hit")
    u = result.created[0]
    return ProvisionResult(True, sub_link=u.sub_link, uuid=u.uuid)


def _to_float(value) -> float:  # noqa: ANN001
    try:
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


async def live_status(
    session: AsyncSession, bot: StorefrontBot, order: StorefrontOrder
) -> LiveStatus:
    """Read this order's config LIVE from the panel (used GB, limit GB, remaining days). Best-effort:
    any failure (no uuid, panel error, missing user) returns ok=False so the caller falls back to the
    stored plan figures."""
    if not order.panel_user_uuid:
        return LiveStatus(False)
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return LiveStatus(False)
    from app.models import Panel
    from app.services.panel_client.admin_api import AdminApiClient

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        return LiveStatus(False)
    try:
        data = await AdminApiClient().get_user(
            panel, order.panel_user_uuid, api_key=reseller.admin_uuid
        )
    except Exception:  # noqa: BLE001 — live read is best-effort; caller shows the stored plan
        log.warning("storefront live_status read failed", exc_info=True)
        return LiveStatus(False)
    if not data:
        return LiveStatus(False)
    remaining = data.get("remaining_day")
    try:
        remaining_days = int(remaining) if remaining is not None else None
    except (TypeError, ValueError):
        remaining_days = None
    return LiveStatus(
        True,
        used_gb=_to_float(data.get("current_usage_GB")),
        limit_gb=_to_float(data.get("usage_limit_GB")) or float(order.gb or 0),
        remaining_days=remaining_days,
    )


# ── atomic, crash-safe purchase + free trial ─────────────────────────────────

async def purchase(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    sf_id: int,
    customer_id: int,
    plan_id: int,
    label: str,
    op_id: str | None = None,
) -> PurchaseResult:
    """Buy a plan: charge the wallet and provision the config, atomically and crash-safely.

    F4 (idempotency): the buy is a durable StorefrontOperation keyed by `op_id` (minted before the
    confirm button). A repeat call with the SAME `op_id` returns the CACHED result — no second order or
    debit — so a re-delivered callback / retry can't double-charge.

    F5 (lease): the order is stamped with a provisioning LEASE before the (unlocked) panel call, so the
    reaper never refunds this order while its provision is still in flight. Order + debit + operation are
    committed together BEFORE any network call; the order pre-stores the panel uuid so a mid-provision
    crash is recoverable. DB connections are never held across the panel/Telegram I/O; serialized per
    customer to defeat double-taps."""
    op_id = op_id or secrets.token_urlsafe(16)
    async with _customer_lock(sf_id, customer_id):
        # 1) short txn — claim the op, validate, pre-generate uuid, create the pending order (+lease)
        #    + debit, commit together (idempotent: a replay returns the cached result here).
        async with session_factory() as s:
            op, outcome = await storefront_ops.claim(
                s, op_id, op_type="purchase", storefront_bot_id=sf_id, customer_id=customer_id,
                plan_id=plan_id)
            if outcome == storefront_ops.REPLAY and op is not None:
                o = await s.get(StorefrontOrder, op.result_order_id) if op.result_order_id else None
                if o is not None and o.status == "provisioned" and o.sub_link:
                    return PurchaseResult(True, order_id=o.id, sub_link=o.sub_link,
                                          gb=o.gb, days=o.days, label=o.label)
                return PurchaseResult(False, order_id=(o.id if o else None), reason="error",
                                      gb=(o.gb if o else 0), days=(o.days if o else 0),
                                      label=(o.label if o else label))
            if outcome == storefront_ops.CONFLICT:
                return PurchaseResult(False, reason="invalid_operation")
            if op is None or outcome == storefront_ops.IN_FLIGHT:
                return PurchaseResult(False, reason="processing")
            op_pk = op.id
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            plan = await s.get(StorefrontPlan, plan_id)
            if sf is None or customer is None:
                await storefront_ops.finalize(s, op, "failed")
                await s.commit()
                return PurchaseResult(False, reason="error")
            if plan is None or plan.storefront_bot_id != sf.id or not plan.enabled:
                await storefront_ops.finalize(s, op, "failed")
                await s.commit()
                return PurchaseResult(False, reason="plan_gone")
            price = int(plan.price_toman)
            bal = int(storefront_wallet.balance(customer))
            if bal < price:
                # never charged → drop the op so the customer can top up and retry with a fresh op
                await s.rollback()
                return PurchaseResult(False, reason="insufficient", short_toman=price - bal)
            new_uuid = str(uuidlib.uuid4())
            order = StorefrontOrder(
                customer_id=customer.id, plan_id=plan.id, panel_id=sf.panel_id,
                label=(label or "").strip()[:64], gb=plan.gb, days=plan.days,
                price_toman=price, status="pending", panel_user_uuid=new_uuid,
                lease_expires_at=await storefront_ops.lease_deadline(s),
            )
            s.add(order)
            await s.flush()
            ok, txn = await storefront_wallet.charge_purchase(
                s, customer.id, price, order_id=order.id, operation_id=op_pk)
            if not ok:  # lost a race for the balance → drop the op + order
                await s.rollback()
                return PurchaseResult(False, reason="insufficient", short_toman=price - bal)
            op.order_id = order.id
            if txn is not None:
                op.debit_txn_id = txn.id
            await s.commit()
            order_id, gb, days = order.id, order.gb, order.days

        # 2) provision OUTSIDE the first transaction (its own short session for the panel write). The
        #    active lease keeps the reaper from touching this order while the panel call is in flight.
        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            if sf is None or customer is None:
                res = ProvisionResult(False, reason="error", message="bot/customer vanished")
            else:
                res = await provision(s, sf, customer, gb=gb, days=days, label=label,
                                      user_uuid=new_uuid)

        # 3) short txn — finalize: provisioned (+link) or failed (+refund), clear the lease, cache the
        #    result on the operation. If the reconciler already decided this order (lease expired mid a
        #    very long provision), respect its outcome (defense in depth).
        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id, with_for_update=True)
            op = await s.get(StorefrontOperation, op_pk)
            if o is None:
                return PurchaseResult(False, reason="error")
            if o.status != "pending":
                if o.status == "provisioned" and o.sub_link:
                    if op is not None:
                        await storefront_ops.finalize(s, op, "done", result_order_id=order_id,
                                                      result_sub_link=o.sub_link)
                        await s.commit()
                    return PurchaseResult(True, order_id=order_id, sub_link=o.sub_link,
                                          gb=gb, days=days, label=label)
                # Some OTHER writer terminal-ised this order while we were talking to the panel.
                # This assumed that writer was always the reaper, which refunds — but a customer
                # or admin delete lands here too, and that path deliberately does not refund. The
                # debit was already committed before the panel call, so the customer paid for a
                # service that will never exist. Refund here as well; `order_has_refund` keeps it
                # exactly-once, so the reaper having already refunded is a no-op.
                if o.price_toman and not await storefront_wallet.order_has_refund(s, order_id):
                    await storefront_wallet.refund(
                        s, customer_id, price, order_id=order_id,
                        note="order finalized elsewhere mid-provision",
                    )
                if op is not None:
                    await storefront_ops.finalize(s, op, "failed", result_order_id=order_id)
                await s.commit()
                return PurchaseResult(False, order_id=order_id, reason="reaped",
                                      message="این سفارش به‌صورت خودکار توسط سیستم نهایی شد.", gb=gb, days=days,
                                      label=label)
            if res.ok and res.sub_link:
                o.status = "provisioned"
                o.panel_user_uuid = res.uuid or new_uuid
                o.sub_link = res.sub_link
                o.lease_expires_at = None
                if op is not None:
                    await storefront_ops.finalize(s, op, "done", result_order_id=order_id,
                                                  result_sub_link=res.sub_link)
                await s.commit()
                return PurchaseResult(True, order_id=order_id, sub_link=res.sub_link,
                                      gb=gb, days=days, label=label)
            o.status = "failed"
            o.lease_expires_at = None
            if o.price_toman and not await storefront_wallet.order_has_refund(s, order_id):
                await storefront_wallet.refund(s, customer_id, price, order_id=order_id,
                                               note=f"provision {res.reason}")
            if op is not None:
                await storefront_ops.finalize(s, op, "failed", result_order_id=order_id)
            await s.commit()
            return PurchaseResult(False, order_id=order_id, reason=res.reason or "error",
                                  message=res.message, gb=gb, days=days, label=label)


async def claim_trial(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    sf_id: int,
    customer_id: int,
) -> PurchaseResult:
    """Claim the one-time free trial, concurrency-safe: the used-flag is compare-and-set under a row
    lock BEFORE provisioning (reverted only if provisioning fails), so a double-tap mints exactly one."""
    async with _customer_lock(sf_id, customer_id):
        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            if sf is None or not sf.free_trial_enabled:
                return PurchaseResult(False, reason="disabled")
            stmt = select(StorefrontCustomer).where(StorefrontCustomer.id == customer_id)
            try:
                stmt = stmt.with_for_update()
            except Exception:  # noqa: BLE001 — sqlite has no row locks; the in-memory lock covers it
                pass
            customer = (await s.execute(stmt)).scalar_one_or_none()
            if customer is None:
                return PurchaseResult(False, reason="error")
            if customer.free_trial_used:
                return PurchaseResult(False, reason="used")
            gb, days = int(sf.free_trial_gb or 1), int(sf.free_trial_days or 1)
            new_uuid = str(uuidlib.uuid4())
            order = StorefrontOrder(
                customer_id=customer.id, plan_id=None, panel_id=sf.panel_id, label="تست رایگان",
                gb=gb, days=days, price_toman=0, status="pending", panel_user_uuid=new_uuid,
                is_trial=True, lease_expires_at=await storefront_ops.lease_deadline(s),  # F5: reaper skips in-flight
            )
            s.add(order)
            customer.free_trial_used = True  # CAS: claimed before provisioning, under the lock
            await s.commit()
            order_id = order.id

        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            if sf is None or customer is None:
                res = ProvisionResult(False, reason="error", message="bot/customer vanished")
            else:
                res = await provision(s, sf, customer, gb=gb, days=days, label="تست رایگان",
                                      user_uuid=new_uuid)

        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is None:
                return PurchaseResult(False, reason="error")
            if res.ok and res.sub_link:
                o.status = "provisioned"
                o.panel_user_uuid = res.uuid or new_uuid
                o.sub_link = res.sub_link
                o.lease_expires_at = None
                await s.commit()
                return PurchaseResult(True, order_id=order_id, sub_link=res.sub_link,
                                      gb=gb, days=days, label="تست رایگان")
            o.status = "failed"
            o.lease_expires_at = None
            cust = await s.get(StorefrontCustomer, customer_id)
            if cust is not None:
                cust.free_trial_used = False  # let them retry a failed trial
            await s.commit()
            return PurchaseResult(False, order_id=order_id, reason=res.reason or "error",
                                  message=res.message, gb=gb, days=days, label="تست رایگان")


async def reap_pending_orders(
    session: AsyncSession, *, older_than: dt.datetime
) -> dict[str, int]:
    """Reconcile orders orphaned by a crash. PURCHASE pending past `older_than` (and with no active
    lease): look the pre-stored uuid up on the panel — present → provisioned + rebuild the sub-link;
    absent → refund (once) + failed. RENEWAL stuck in `renewing` (lease expired): verify/reapply a new
    operation's persisted absolute panel target and finalize it forward; only a definitively missing user
    is reversed. Legacy target-less operations retain exactly-once compensation. Idempotent. Returns counts.

    F5: an order whose provisioning LEASE is still valid is left alone (a live purchase/renew is holding
    it across the panel call), so the reaper can never refund an in-flight provision."""
    now = storefront_ops.now()
    counts = {
        "checked": 0, "completed": 0, "refunded": 0,
        "renewing_recovered": 0, "renewing_completed": 0, "reversed": 0,
    }
    # Candidate ids only (unlocked). We re-lock EACH order right before deciding it — a single
    # FOR UPDATE over the whole batch would drop every lock on the first per-order commit, letting
    # the live provisioner slip in. `SKIP LOCKED` skips an order purchase()'s step-3 is finalizing.
    # The provisioning LEASE (F5) is checked per-order in Python (dialect-safe tz handling) after the
    # lock, so an order whose lease is still valid is never touched.
    ids = (
        await session.execute(
            select(StorefrontOrder.id).where(
                StorefrontOrder.status == "pending",
                StorefrontOrder.created_at < older_than,
            ).limit(200)
        )
    ).scalars().all()
    client = AdminApiClient()
    for oid in ids:
        order = await _lock_order_skip(session, oid)
        if order is None or order.status != "pending":
            await session.rollback()
            continue  # gone, locked by a live provision (skip this tick), or already decided
        if _lease_active(order.lease_expires_at, now):
            await session.rollback()  # release the row lock immediately; live finalization must not wait
            continue  # F5: a live provision is holding this order across the panel call — leave it
        counts["checked"] += 1
        customer = await session.get(StorefrontCustomer, order.customer_id)
        sf = await session.get(StorefrontBot, customer.storefront_bot_id) if customer else None
        reseller = await session.get(Reseller, sf.reseller_id) if sf else None
        panel = await session.get(Panel, reseller.panel_id) if reseller else None
        if not (customer and sf and reseller and panel and order.panel_user_uuid):
            await session.rollback()
            continue
        try:
            data = await client.get_user(panel, order.panel_user_uuid, api_key=reseller.admin_uuid)
        except Exception:  # noqa: BLE001 — leave it pending; try again next tick
            log.warning("reaper get_user failed for order %s", order.id, exc_info=True)
            await session.rollback()
            continue
        if data:  # the config WAS created before the crash → complete it
            order.status = "provisioned"
            order.sub_link = panel.user_sub_link(order.panel_user_uuid, name=order.label or None)
            order.lease_expires_at = None
            await _finalize_purchase_op(session, order.id, order.sub_link)
            await session.commit()
            counts["completed"] += 1
            await _notify_completed(sf, customer, order)
        else:  # nothing on the panel → refund (once) and fail
            if order.price_toman and not await storefront_wallet.order_has_refund(session, order.id):
                await storefront_wallet.refund(session, order.customer_id, int(order.price_toman),
                                               order_id=order.id, note="reaper: provision lost")
            # A reaped-failed FREE TRIAL must free the one-time flag so the customer can retry —
            # a trial has no refund path (price 0), so without this they'd be stuck unable to
            # re-claim (same recovery claim_trial does on its own provisioning failure).
            if order.is_trial and customer.free_trial_used:
                customer.free_trial_used = False
            order.status = "failed"
            order.lease_expires_at = None
            await _finalize_purchase_op(session, order.id, None)
            await session.commit()
            counts["refunded"] += 1

    # Recover RENEWALS stuck in "renewing" whose lease expired. New operations carry an absolute panel
    # target: verify/reapply it and finalize forward, so a successful remote write is never blindly
    # refunded. Legacy target-less operations retain the conservative exactly-once reversal path.
    renewing_ids = (
        await session.execute(
            select(StorefrontOrder.id).where(
                StorefrontOrder.status == "renewing",
                StorefrontOrder.updated_at < older_than,
            ).limit(200)
        )
    ).scalars().all()
    for oid in renewing_ids:
        o = await _lock_order_skip(session, oid)
        if o is None or o.status != "renewing":
            await session.rollback()
            continue
        if _lease_active(o.lease_expires_at, now):
            await session.rollback()  # release the row lock immediately; live finalization must not wait
            continue   # a live renewal still holds this order — leave it
        op = (
            await session.execute(
                select(StorefrontOperation).where(
                    StorefrontOperation.order_id == oid,
                    StorefrontOperation.op_type == "renewal",
                    StorefrontOperation.status == "in_progress",
                ).order_by(StorefrontOperation.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if op is not None:
            charged = await storefront_wallet.operation_has_charge(session, op.id)
            # A paid/customer renewal with a persisted target is recoverable in the FORWARD direction:
            # verify the panel mutation and, when necessary, idempotently reapply the same absolute target.
            # This removes the "panel succeeded, DB finalize crashed, blind refund grants free service" gap.
            if op.target_usage_limit_gb is not None and (charged or op.by_admin):
                customer = await session.get(StorefrontCustomer, op.customer_id) if op.customer_id else None
                sf = await session.get(StorefrontBot, op.storefront_bot_id) if op.storefront_bot_id else None
                reseller = await session.get(Reseller, sf.reseller_id) if sf else None
                panel = await session.get(Panel, reseller.panel_id) if reseller else None
                if not (customer and sf and reseller and panel and o.panel_user_uuid):
                    await session.rollback()
                    continue  # context may be transiently unavailable; keep the durable operation pending
                target = RenewUserTarget(
                    usage_limit_gb=float(op.target_usage_limit_gb),
                    package_days=int(op.days),
                    prior_start_date=op.prior_panel_start_date,
                )
                try:
                    data = await client.get_user(
                        panel, o.panel_user_uuid, api_key=reseller.admin_uuid)
                    if data is None:
                        # The service definitively does not exist: make a charged customer whole.
                        if charged and op.customer_id is not None:
                            await storefront_wallet.reverse_charge(
                                session, op.customer_id, int(op.price_toman), order_id=oid,
                                operation_id=op.id, note="reconciler: renewal user missing")
                            op.status = "reversed"
                            counts["reversed"] += 1
                        else:
                            op.status = "failed"
                        o.status = op.prior_status if op.prior_status in _ACTIVE else "provisioned"
                    else:
                        if not _renewal_target_applied(data, target, op.prior_panel_start_date):
                            await client.apply_renew_user_target(
                                panel, o.panel_user_uuid, target, api_key=reseller.admin_uuid)
                        op.status = "done"
                        op.result_order_id = oid
                        o.status = "provisioned"
                        o.gb, o.days = int(op.gb), int(op.days)
                        o.last_renewed_at = storefront_ops.now()
                        counts["renewing_completed"] += 1
                    o.lease_expires_at = None
                    await session.commit()
                    counts["renewing_recovered"] += 1
                except Exception:  # noqa: BLE001 — ambiguity stays pending; next tick retries safely
                    log.warning("renewal recovery failed for order %s", oid, exc_info=True)
                    await session.rollback()
                continue

            if op.customer_id is not None and charged:
                await storefront_wallet.reverse_charge(
                    session, op.customer_id, int(op.price_toman), order_id=oid, operation_id=op.id,
                    note="reconciler: crashed renewal")
                op.status = "reversed"
                counts["reversed"] += 1
            else:
                op.status = "failed"
            o.status = op.prior_status if op.prior_status in _ACTIVE else "provisioned"
        else:
            o.status = "provisioned"   # legacy in-flight renewal (pre-deploy, no operation record)
        o.lease_expires_at = None
        await session.commit()
        counts["renewing_recovered"] += 1
    return counts


async def _finalize_purchase_op(session: AsyncSession, order_id: int, sub_link: str | None) -> None:
    """Mark this order's in-progress PURCHASE operation terminal so a replay returns the reaper's
    outcome (idempotency stays consistent after a reaper-completed/refunded order)."""
    op = (
        await session.execute(
            select(StorefrontOperation).where(
                StorefrontOperation.order_id == order_id,
                StorefrontOperation.op_type == "purchase",
                StorefrontOperation.status == "in_progress",
            ).order_by(StorefrontOperation.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if op is not None:
        op.status = "done" if sub_link else "failed"
        op.result_order_id = order_id
        if sub_link:
            op.result_sub_link = sub_link


async def _notify_completed(
    sf: StorefrontBot, customer: StorefrontCustomer, order: StorefrontOrder
) -> None:
    """Best-effort: deliver the recovered config's link to the customer from the reaper (backend process,
    so it builds a transient Bot from the stored token). A failure here is harmless — the order is already
    provisioned and visible in «my services»."""
    from aiogram import Bot

    from app.bot.rtl import rtl
    from app.services import storefront

    token = storefront.bot_token(sf)
    if not token or not order.sub_link:
        return
    bot = Bot(token=token)
    try:
        await bot.send_message(
            customer.telegram_id,
            rtl(f"✅ سرویسِ «{order.label or 'شما'}» آماده شد.\n\n🔗 لینکِ اشتراک:\n"
                f"<code>{order.sub_link}</code>"),
            parse_mode="HTML", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.info("reaper notify failed for order %s", order.id, exc_info=True)
    finally:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass


class RateLimited(Exception):
    """The owner refreshed this order's live status too soon; retry after `retry_after` seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("live refresh rate limited")
        self.retry_after = int(retry_after)


async def refresh_order_live(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *, order_id: int, window_seconds: int,
) -> tuple[dict, str]:
    """Owner-triggered LIVE panel read for one order, throttled to one per `window_seconds` (cross
    process, via `storefront_orders.live_refreshed_at` under a row lock). No DB session is held across
    the panel network call. Returns (status, freshness): freshness ∈ {live, stale, unknown}. The caller
    resolves ownership first; this raises `RateLimited` when called again inside the window."""
    session_factory = session_factory or SessionLocal
    now = dt.datetime.now(dt.timezone.utc)
    # STEP 1 — short session: lock the order row, enforce the throttle, stamp, capture ctx, release.
    async with session_factory() as s:
        stmt = select(StorefrontOrder).where(StorefrontOrder.id == order_id)
        try:
            stmt = stmt.with_for_update()
        except Exception:  # noqa: BLE001 — dialect without row locks
            pass
        order = (await s.execute(stmt)).scalar_one_or_none()
        if order is None:
            return {"ok": False}, "unknown"
        last = order.live_refreshed_at
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
            elapsed = (now - last).total_seconds()
            if elapsed < window_seconds:
                await s.rollback()
                raise RateLimited(int(window_seconds - elapsed) + 1)
        order.live_refreshed_at = now
        customer = await s.get(StorefrontCustomer, order.customer_id)
        sf = await s.get(StorefrontBot, customer.storefront_bot_id) if customer else None
        reseller = await s.get(Reseller, sf.reseller_id) if sf else None
        panel = await s.get(Panel, reseller.panel_id) if reseller else None
        uuid, api_key = order.panel_user_uuid, (reseller.admin_uuid if reseller else None)
        panel_id, order_gb = (panel.id if panel else None), order.gb
        await s.commit()  # releases the row lock BEFORE any panel I/O
    # STEP 2 — no DB session held across the panel read.
    if not (panel_id and uuid):
        return {"ok": False}, "unknown"
    try:
        async with session_factory() as s:
            panel = await s.get(Panel, panel_id)
        data = await AdminApiClient().get_user(panel, uuid, api_key=api_key) if panel else None
    except Exception:  # noqa: BLE001 — live read is best-effort; fall back to the stored snapshot
        log.warning("refresh_order_live panel read failed for order %s", order_id, exc_info=True)
        return {"ok": False}, "stale"
    if not data:
        return {"ok": False}, "stale"
    remaining = data.get("remaining_day")
    try:
        remaining_days = int(remaining) if remaining is not None else None
    except (TypeError, ValueError):
        remaining_days = None
    return {
        "ok": True,
        "used_gb": _to_float(data.get("current_usage_GB")),
        "limit_gb": _to_float(data.get("usage_limit_GB")) or float(order_gb or 0),
        "remaining_days": remaining_days,
    }, "live"
