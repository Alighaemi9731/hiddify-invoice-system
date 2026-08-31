"""Read models for the storefront customer/service portal (plan 004).

Every function is tenant-scoped by `shop_id` and returns plain dicts shaped for the portal DTOs —
never a raw ORM row, never a `sub_link` or `proof_path`. Lists use opaque HMAC keyset cursors
(`storefront_cursor`) ordered `(created_at DESC, id DESC)`; per-page order/customer service figures
come from the STORED `EndUserSnapshot` (batched, no N+1) — the live panel read is a separate,
rate-limited endpoint. Nothing here calls the panel client.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.services import storefront_cursor

# Order statuses that mean the customer currently has a real config on the panel (provisioned,
# paused, or mid-renew) — as opposed to pending/failed/deleted.
ACTIVE_ORDER_STATUSES = ("provisioned", "disabled", "renewing")
# LTV counts only settled money that represents net spend on service.
_LTV_KINDS = ("purchase", "refund", "renew_reversal")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _activity_expr():  # noqa: ANN202
    return func.coalesce(StorefrontCustomer.last_seen_at, StorefrontCustomer.created_at)


def _has_service_exists():  # noqa: ANN202
    return exists().where(
        StorefrontOrder.customer_id == StorefrontCustomer.id,
        StorefrontOrder.status.in_(ACTIVE_ORDER_STATUSES),
    )


async def net_ltv_toman(session: AsyncSession, customer_id: int) -> int:
    """Lifetime net spend on service = |settled purchases| − refunds − renewal reversals.

    Purchase rows are negative and refund/renew_reversal rows positive, so negating the sum yields
    Σ(−purchase) − Σ(refund) − Σ(renew_reversal). Top-ups, gifts and manual adjustments are excluded;
    renewals are recorded as `purchase` rows and are already netted against their reversals."""
    total = await session.scalar(
        select(func.coalesce(func.sum(StorefrontWalletTxn.amount_toman), 0)).where(
            StorefrontWalletTxn.customer_id == customer_id,
            StorefrontWalletTxn.status == "done",
            StorefrontWalletTxn.kind.in_(_LTV_KINDS),
        )
    )
    return int(-(Decimal(total or 0)))


async def _get_owned_customer(
    session: AsyncSession, shop_id: int, customer_id: int
) -> StorefrontCustomer | None:
    cust = await session.get(StorefrontCustomer, customer_id)
    if cust is None or cust.storefront_bot_id != shop_id:
        return None
    return cust


def _snap_for(
    snaps: dict[tuple[int, str], EndUserSnapshot], order: StorefrontOrder
) -> EndUserSnapshot | None:
    if order.panel_id is None or not order.panel_user_uuid:
        return None
    return snaps.get((order.panel_id, order.panel_user_uuid))


async def _batch_snapshots(
    session: AsyncSession, orders: Sequence[StorefrontOrder]
) -> dict[tuple[int, str], EndUserSnapshot]:
    keys = {
        (o.panel_id, o.panel_user_uuid)
        for o in orders
        if o.panel_id is not None and o.panel_user_uuid
    }
    if not keys:
        return {}
    panel_ids = {p for p, _u in keys}
    uuids = {u for _p, u in keys}
    rows = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id.in_(panel_ids),
                EndUserSnapshot.user_uuid.in_(uuids),
            )
        )
    ).scalars().all()
    return {(s.panel_id, s.user_uuid): s for s in rows}


def _order_dto(order: StorefrontOrder, snap: EndUserSnapshot | None) -> dict:
    """Stored order view: locked plan figures + the last-synced snapshot (NOT a live read).
    `freshness='stored'` here; the explicit refresh endpoint returns 'live'/'stale'/'unknown'."""
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "label": order.label,
        "status": order.status,
        "is_trial": order.is_trial,
        "gb": order.gb,
        "days": order.days,
        "price_toman": order.price_toman,
        "created_at": order.created_at,
        "last_renewed_at": order.last_renewed_at,
        "live_refreshed_at": order.live_refreshed_at,
        "snapshot": (
            None if snap is None else {
                "used_gb": float(snap.current_usage_gb or 0),
                "limit_gb": float(snap.usage_limit_gb or 0),
                "start_date": snap.start_date,
                "enabled": bool(snap.enable),
                "last_online": snap.last_online,
            }
        ),
        "freshness": "stored",
    }


def _ledger_dto(row: StorefrontWalletTxn) -> dict:
    # Redacted: never expose proof_path.
    return {
        "id": row.id,
        "kind": row.kind,
        "amount_toman": int(Decimal(row.amount_toman or 0)),
        "status": row.status,
        "method": row.method,
        "order_id": row.order_id,
        "txid": row.txid,
        "chain": row.chain,
        "note": row.note,
        "created_at": row.created_at,
        "decided_at": row.decided_at,
    }


async def list_customers_keyset(
    session: AsyncSession, shop_id: int, *,
    q: str | None = None, banned: bool | None = None, activity: str | None = None,
    has_service: bool | None = None, cursor: str | None = None, limit: int = 25,
) -> tuple[list[dict], str | None]:
    where = [StorefrontCustomer.storefront_bot_id == shop_id]
    if q:
        digits = q.strip()
        if digits.isdigit():
            where.append(StorefrontCustomer.telegram_id == int(digits))
        else:
            like = f"%{q.strip()}%"
            where.append(or_(
                StorefrontCustomer.name.ilike(like),
                StorefrontCustomer.username.ilike(like),
            ))
    if banned is not None:
        where.append(StorefrontCustomer.banned.is_(banned))
    if activity in ("active30", "inactive30"):
        cutoff = _now() - dt.timedelta(days=30)
        where.append(
            _activity_expr() >= cutoff if activity == "active30" else _activity_expr() < cutoff
        )
    if has_service is True:
        where.append(_has_service_exists())
    elif has_service is False:
        where.append(~_has_service_exists())

    if cursor:
        c_at, c_id = storefront_cursor.decode_cursor(f"customers:{shop_id}", cursor)
        where.append(or_(
            StorefrontCustomer.created_at < c_at,
            and_(StorefrontCustomer.created_at == c_at, StorefrontCustomer.id < c_id),
        ))

    rows = (
        await session.execute(
            select(StorefrontCustomer).where(*where)
            .order_by(StorefrontCustomer.created_at.desc(), StorefrontCustomer.id.desc())
            .limit(limit + 1)
        )
    ).scalars().all()

    page = rows[:limit]
    next_cursor = (
        storefront_cursor.encode_cursor(
            endpoint=f"customers:{shop_id}", created_at=page[-1].created_at, row_id=page[-1].id)
        if len(rows) > limit and page else None
    )
    # has_service per row (one grouped query over the page's customers).
    ids = [c.id for c in page]
    served: set[int] = set()
    if ids:
        served = set(
            (await session.execute(
                select(StorefrontOrder.customer_id).where(
                    StorefrontOrder.customer_id.in_(ids),
                    StorefrontOrder.status.in_(ACTIVE_ORDER_STATUSES),
                ).distinct()
            )).scalars().all()
        )
    items = [
        {
            "id": c.id, "telegram_id": c.telegram_id, "name": c.name, "username": c.username,
            "banned": c.banned, "free_trial_used": c.free_trial_used,
            "wallet_balance_toman": int(Decimal(c.wallet_balance_toman or 0)),
            "last_seen_at": c.last_seen_at, "created_at": c.created_at,
            "has_service": c.id in served,
        }
        for c in page
    ]
    return items, next_cursor


async def customer_detail(session: AsyncSession, shop_id: int, customer_id: int) -> dict | None:
    cust = await _get_owned_customer(session, shop_id, customer_id)
    if cust is None:
        return None
    orders = (
        await session.execute(
            select(StorefrontOrder).where(StorefrontOrder.customer_id == customer_id)
        )
    ).scalars().all()
    counts: dict[str, int] = {}
    for o in orders:
        counts[o.status] = counts.get(o.status, 0) + 1
    active = sum(counts.get(s, 0) for s in ACTIVE_ORDER_STATUSES)
    return {
        "id": cust.id, "telegram_id": cust.telegram_id, "name": cust.name,
        "username": cust.username, "banned": cust.banned, "free_trial_used": cust.free_trial_used,
        "wallet_balance_toman": int(Decimal(cust.wallet_balance_toman or 0)),
        "net_ltv_toman": await net_ltv_toman(session, customer_id),
        "last_seen_at": cust.last_seen_at, "created_at": cust.created_at,
        "service_counts": {"total": len(orders), "active": active, "by_status": counts},
    }


async def list_customer_orders_keyset(
    session: AsyncSession, shop_id: int, customer_id: int, *,
    status: str | None = None, cursor: str | None = None, limit: int = 25,
) -> tuple[list[dict], str | None] | None:
    if await _get_owned_customer(session, shop_id, customer_id) is None:
        return None
    where = [StorefrontOrder.customer_id == customer_id]
    if status:
        where.append(StorefrontOrder.status == status)
    if cursor:
        c_at, c_id = storefront_cursor.decode_cursor(f"orders:{customer_id}", cursor)
        where.append(or_(
            StorefrontOrder.created_at < c_at,
            and_(StorefrontOrder.created_at == c_at, StorefrontOrder.id < c_id),
        ))
    rows = (
        await session.execute(
            select(StorefrontOrder).where(*where)
            .order_by(StorefrontOrder.created_at.desc(), StorefrontOrder.id.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    page = rows[:limit]
    next_cursor = (
        storefront_cursor.encode_cursor(
            endpoint=f"orders:{customer_id}", created_at=page[-1].created_at, row_id=page[-1].id)
        if len(rows) > limit and page else None
    )
    snaps = await _batch_snapshots(session, page)
    items = [_order_dto(o, _snap_for(snaps, o)) for o in page]
    return items, next_cursor


async def customer_ledger(
    session: AsyncSession, shop_id: int, customer_id: int, *,
    kind: str | None = None, status: str | None = None,
    dt_from: dt.datetime | None = None, dt_to: dt.datetime | None = None,
    cursor: str | None = None, limit: int = 25,
) -> tuple[list[dict], str | None] | None:
    if await _get_owned_customer(session, shop_id, customer_id) is None:
        return None
    where = [StorefrontWalletTxn.customer_id == customer_id]
    if kind:
        where.append(StorefrontWalletTxn.kind == kind)
    if status:
        where.append(StorefrontWalletTxn.status == status)
    if dt_from is not None:
        where.append(StorefrontWalletTxn.created_at >= dt_from)
    if dt_to is not None:
        where.append(StorefrontWalletTxn.created_at <= dt_to)
    if cursor:
        c_at, c_id = storefront_cursor.decode_cursor(f"ledger:{customer_id}", cursor)
        where.append(or_(
            StorefrontWalletTxn.created_at < c_at,
            and_(StorefrontWalletTxn.created_at == c_at, StorefrontWalletTxn.id < c_id),
        ))
    rows = (
        await session.execute(
            select(StorefrontWalletTxn).where(*where)
            .order_by(StorefrontWalletTxn.created_at.desc(), StorefrontWalletTxn.id.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    page = rows[:limit]
    next_cursor = (
        storefront_cursor.encode_cursor(
            endpoint=f"ledger:{customer_id}", created_at=page[-1].created_at, row_id=page[-1].id)
        if len(rows) > limit and page else None
    )
    return [_ledger_dto(r) for r in page], next_cursor


async def order_detail(session: AsyncSession, shop_id: int, order_id: int) -> dict | None:
    """Stored order view via the order→customer→shop chain. NEVER calls the panel client."""
    order = await session.get(StorefrontOrder, order_id)
    if order is None:
        return None
    cust = await session.get(StorefrontCustomer, order.customer_id)
    if cust is None or cust.storefront_bot_id != shop_id:
        return None
    snaps = await _batch_snapshots(session, [order])
    dto = _order_dto(order, _snap_for(snaps, order))
    dto["customer"] = {
        "id": cust.id, "telegram_id": cust.telegram_id, "name": cust.name,
        "username": cust.username, "banned": cust.banned,
    }
    return dto


# ── top-ups (plan 005) ───────────────────────────────────────────────────────
def _topup_item_dto(txn: StorefrontWalletTxn, cust: StorefrontCustomer) -> dict:
    """A top-up row for the ops queue. `amount_toman` is the current/credited value;
    `requested_amount_toman` preserves the customer's original request. Never exposes proof_path."""
    return {
        "id": txn.id,
        "customer": {"id": cust.id, "telegram_id": cust.telegram_id,
                     "name": cust.name, "username": cust.username},
        "amount_toman": int(Decimal(txn.amount_toman or 0)),
        "requested_amount_toman": (
            int(Decimal(txn.requested_amount_toman)) if txn.requested_amount_toman is not None else None),
        "status": txn.status, "method": txn.method, "chain": txn.chain, "txid": txn.txid,
        "has_proof": bool(txn.proof_path), "created_at": txn.created_at, "decided_at": txn.decided_at,
    }


async def list_topups_keyset(
    session: AsyncSession, shop_id: int, *,
    status: str | None = None, method: str | None = None,
    min_amount: int | None = None, max_amount: int | None = None,
    dt_from: dt.datetime | None = None, dt_to: dt.datetime | None = None,
    q: str | None = None, cursor: str | None = None, limit: int = 25,
) -> tuple[list[dict], str | None]:
    where = [StorefrontWalletTxn.storefront_bot_id == shop_id, StorefrontWalletTxn.kind == "topup"]
    if status:
        where.append(StorefrontWalletTxn.status == status)
    if method:
        where.append(StorefrontWalletTxn.method == method)
    if min_amount is not None:
        where.append(StorefrontWalletTxn.amount_toman >= min_amount)
    if max_amount is not None:
        where.append(StorefrontWalletTxn.amount_toman <= max_amount)
    if dt_from is not None:
        where.append(StorefrontWalletTxn.created_at >= dt_from)
    if dt_to is not None:
        where.append(StorefrontWalletTxn.created_at <= dt_to)
    if q:
        s = q.strip()
        if s.isdigit():
            where.append(StorefrontCustomer.telegram_id == int(s))
        else:
            like = f"%{s}%"
            where.append(or_(
                StorefrontCustomer.name.ilike(like), StorefrontCustomer.username.ilike(like),
                StorefrontWalletTxn.txid.ilike(like)))
    if cursor:
        c_at, c_id = storefront_cursor.decode_cursor(f"topups:{shop_id}", cursor)
        where.append(or_(
            StorefrontWalletTxn.created_at < c_at,
            and_(StorefrontWalletTxn.created_at == c_at, StorefrontWalletTxn.id < c_id)))
    rows = (
        await session.execute(
            select(StorefrontWalletTxn, StorefrontCustomer)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .where(*where)
            .order_by(StorefrontWalletTxn.created_at.desc(), StorefrontWalletTxn.id.desc())
            .limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    next_cursor = (
        storefront_cursor.encode_cursor(
            endpoint=f"topups:{shop_id}", created_at=page[-1][0].created_at, row_id=page[-1][0].id)
        if len(rows) > limit and page else None
    )
    items = [_topup_item_dto(txn, cust) for txn, cust in page]
    return items, next_cursor


async def topup_detail(session: AsyncSession, shop_id: int, txn_id: int) -> dict | None:
    txn = await session.get(StorefrontWalletTxn, txn_id)
    if txn is None or txn.storefront_bot_id != shop_id or txn.kind != "topup":
        return None
    cust = await session.get(StorefrontCustomer, txn.customer_id)
    if cust is None:
        return None
    dto = _topup_item_dto(txn, cust)
    dto["note"] = txn.note
    dto["credit_code_id"] = txn.credit_code_id
    dto["decided_at"] = txn.decided_at
    return dto
