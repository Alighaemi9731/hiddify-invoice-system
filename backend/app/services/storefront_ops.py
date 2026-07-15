"""Durable idempotency + crash-recovery records for storefront money actions (F4/F11/F5).

Each money-moving action (a purchase or a renewal) is claimed by a random `op_id` minted BEFORE the
confirm button is shown. A repeat claim with the SAME `op_id` — a double-tap, a re-delivered callback,
a cross-process retry — returns the existing row:
  • TERMINAL (done/failed/reversed) → the caller replays the CACHED result (no second debit / panel call)
  • IN_PROGRESS → another worker holds it; the caller tells the user to wait
The unique index on `op_id` is the cross-process backstop (a losing concurrent INSERT re-reads the winner).
The operation also anchors crash recovery: the wallet debit carries `operation_id`, so the reconciler
can tell whether a crashed renewal actually charged and reverse it exactly once (F11).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontOperation
from app.services import settings_service

_TERMINAL = ("done", "failed", "reversed")

# claim() outcomes
CLAIMED = "claimed"      # brand-new (or a stale pending we just took) → do the work
REPLAY = "replay"        # a terminal row already exists → return its cached result
IN_FLIGHT = "in_flight"  # another worker holds it (in_progress) → ask the user to wait
CONFLICT = "conflict"    # op_id belongs to a different tenant/customer/action → never replay it

_LEASE_DEFAULT = 300
_LEASE_MIN, _LEASE_MAX = 300, 3600


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def lease_seconds(session: AsyncSession) -> int:
    """The operation lease TTL, clamped above the longest renewal request sequence.

    Renewal preparation performs a GET and the apply performs a PATCH; each may use the 90-second panel
    timeout. Five minutes leaves a safety margin over both calls so the reaper cannot race live I/O.
    """
    try:
        v = int(await settings_service.get(session, "storefront_operation_lease_seconds", _LEASE_DEFAULT)
                or _LEASE_DEFAULT)
    except (TypeError, ValueError):
        v = _LEASE_DEFAULT
    return max(_LEASE_MIN, min(_LEASE_MAX, v))


async def lease_deadline(session: AsyncSession) -> dt.datetime:
    return now() + dt.timedelta(seconds=await lease_seconds(session))


async def _for_update(session: AsyncSession, op_id: str) -> StorefrontOperation | None:
    stmt = select(StorefrontOperation).where(StorefrontOperation.op_id == op_id)
    try:
        stmt = stmt.with_for_update()
    except Exception:  # noqa: BLE001 — dialect without row locks (sqlite)
        pass
    return (await session.execute(stmt)).scalar_one_or_none()


def _matches_context(
    op: StorefrontOperation, *, op_type: str, storefront_bot_id: int, customer_id: int,
    order_id: int | None, by_admin: bool,
) -> bool:
    """Bind an idempotency token to the principal/action that minted it.

    Callback data is transport, not authorization: Telegram messages can be forwarded and stale
    callback payloads can be replayed. A terminal operation must therefore never disclose its cached
    order/subscription link to a different customer or shop. `order_id` is mandatory for renewals but
    intentionally absent for purchases until their order is created.
    """
    return (
        op.op_type == op_type
        and op.storefront_bot_id == storefront_bot_id
        and op.customer_id == customer_id
        and bool(op.by_admin) == bool(by_admin)
        and (order_id is None or op.order_id == order_id)
    )


async def reserve(
    session: AsyncSession, op_id: str, *, op_type: str, storefront_bot_id: int, customer_id: int,
    order_id: int | None = None, plan_id: int | None = None, price_toman: int = 0, gb: int = 0,
    days: int = 0, by_admin: bool = False, prior_status: str | None = None,
) -> StorefrontOperation:
    """Persist a pending operation before its confirmation button is sent.

    This closes the pre-claim forwarding window: the random token is already bound to the intended
    tenant/customer when it becomes visible in Telegram callback data.
    """
    op = StorefrontOperation(
        op_id=op_id, op_type=op_type, storefront_bot_id=storefront_bot_id,
        customer_id=customer_id, order_id=order_id, plan_id=plan_id, status="pending",
        prior_status=prior_status, price_toman=int(price_toman), gb=int(gb), days=int(days),
        by_admin=by_admin,
    )
    session.add(op)
    await session.flush()
    return op


async def claim(
    session: AsyncSession, op_id: str, *, op_type: str, storefront_bot_id: int, customer_id: int,
    order_id: int | None = None, plan_id: int | None = None, price_toman: int = 0, gb: int = 0,
    days: int = 0, by_admin: bool = False, prior_status: str | None = None,
) -> tuple[StorefrontOperation | None, str]:
    """Get-or-create the operation, returning (op, outcome). CLAIMED → op is `in_progress`, the caller
    does the work and finalize()s it; REPLAY → op is terminal (read result_*); IN_FLIGHT → busy. Does
    NOT commit — the caller commits the claim together with the first durable step of its work."""
    existing = await _for_update(session, op_id)
    if existing is not None:
        if not _matches_context(
            existing, op_type=op_type, storefront_bot_id=storefront_bot_id,
            customer_id=customer_id, order_id=order_id, by_admin=by_admin,
        ):
            return None, CONFLICT
        if existing.status in _TERMINAL:
            return existing, REPLAY
        if existing.status == "in_progress":
            return existing, IN_FLIGHT
        # Refresh values that are intentionally chosen at execution time (for example a plan's current
        # price), while preserving the identity fields that were bound by reserve().
        existing.plan_id = plan_id
        existing.price_toman = int(price_toman)
        existing.gb = int(gb)
        existing.days = int(days)
        existing.prior_status = prior_status
        existing.status = "in_progress"   # a stale 'pending' → take it
        return existing, CLAIMED
    op = StorefrontOperation(
        op_id=op_id, op_type=op_type, storefront_bot_id=storefront_bot_id, customer_id=customer_id,
        order_id=order_id, plan_id=plan_id, status="in_progress", prior_status=prior_status,
        price_toman=int(price_toman), gb=int(gb), days=int(days), by_admin=by_admin,
    )
    session.add(op)
    try:
        await session.flush()   # unique op_id → IntegrityError if a concurrent worker won the race
    except IntegrityError:
        await session.rollback()
        existing = await _for_update(session, op_id)
        if existing is None:
            return None, IN_FLIGHT   # extremely unlikely; treat as busy
        if not _matches_context(
            existing, op_type=op_type, storefront_bot_id=storefront_bot_id,
            customer_id=customer_id, order_id=order_id, by_admin=by_admin,
        ):
            return None, CONFLICT
        return existing, (REPLAY if existing.status in _TERMINAL else IN_FLIGHT)
    return op, CLAIMED


async def finalize(
    session: AsyncSession, op: StorefrontOperation, status: str, *,
    order_id: int | None = None, result_order_id: int | None = None,
    result_sub_link: str | None = None, debit_txn_id: int | None = None,
) -> None:
    """Set the operation's terminal state + cached result. Does NOT commit."""
    op.status = status
    if order_id is not None:
        op.order_id = order_id
    if result_order_id is not None:
        op.result_order_id = result_order_id
    if result_sub_link is not None:
        op.result_sub_link = result_sub_link
    if debit_txn_id is not None:
        op.debit_txn_id = debit_txn_id
