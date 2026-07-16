"""Read-only, tenant-scoped reporting for the storefront portal.

Every function receives a storefront that has already passed ``StorefrontAccess``.  Queries still
scope through the storefront id (rather than trusting child ids) to keep the reporting layer safe
when it is reused by later portal releases.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.storefront_access import StorefrontAccess
from app.models import (
    EndUserSnapshot,
    StorefrontCreditRedemption,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.schemas.portal_storefront import (
    BotHealthOut,
    CreditMetricsOut,
    CustomerMetricsOut,
    DashboardRangeOut,
    PanelHealthOut,
    PendingTopupsOut,
    SalesBucketOut,
    SalesPeriodOut,
    StorefrontDashboardOut,
    StorefrontHealthOut,
    StorefrontPanelOut,
    StorefrontResellerOut,
    StorefrontSummaryOut,
    TrialConversionOut,
)
from app.services.storefront_expiry import _days_left

TEHRAN = ZoneInfo("Asia/Tehran")
SERVICE_STATES = ("pending", "provisioned", "renewing", "disabled", "failed", "deleted")
OPERATION_STATES = ("pending", "in_progress", "done", "failed", "reversed")


def classify_health_error(error: str | None) -> str | None:
    """Map a persisted error to a stable, non-sensitive class without returning its text."""
    if not error:
        return None
    value = error.casefold()
    if any(term in value for term in ("unauthorized", "forbidden", "401", "403", "revoked")):
        return "unauthorized"
    if any(
        term in value
        for term in ("timeout", "timed out", "network", "connection", "dns", "unreachable")
    ):
        return "network"
    if any(
        term in value
        for term in ("configuration", "not configured", "missing", "decrypt", "invalid token")
    ):
        return "configuration"
    return "unknown"


def storefront_summary(access: StorefrontAccess) -> StorefrontSummaryOut:
    bot_error = classify_health_error(access.shop.last_error)
    panel_error = classify_health_error(access.panel.last_error)
    error_class = bot_error or panel_error
    state_updated_at = max(access.shop.updated_at, access.panel.updated_at)
    if bot_error is not None:
        state_updated_at = access.shop.updated_at
    elif panel_error is not None:
        state_updated_at = access.panel.updated_at
    return StorefrontSummaryOut(
        id=access.shop.id,
        reseller=StorefrontResellerOut(id=access.reseller.id, name=access.reseller.name),
        panel=StorefrontPanelOut(id=access.panel.id, key=access.panel.key),
        bot_username=access.shop.bot_username,
        enabled=access.shop.enabled,
        status=access.shop.status,
        health_error_class=error_class,  # type: ignore[arg-type]
        health_state_updated_at=state_updated_at,
        shop_closed=access.shop.shop_closed,
    )


def tehran_bounds(day_from: dt.date, day_to: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Convert an inclusive Tehran calendar range to a half-open UTC timestamp range."""
    start = dt.datetime.combine(day_from, dt.time.min, TEHRAN).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time.min, TEHRAN).astimezone(
        dt.timezone.utc
    )
    return start, end


def _period_condition(start: dt.datetime, end: dt.datetime):
    return and_(StorefrontWalletTxn.created_at >= start, StorefrontWalletTxn.created_at < end)


async def _sales_periods(
    session: AsyncSession,
    storefront_id: int,
    periods: Mapping[str, tuple[dt.datetime, dt.datetime]],
) -> dict[str, SalesPeriodOut]:
    """Calculate all requested periods in one ledger scan."""
    columns = []
    for prefix, (start, end) in periods.items():
        window = _period_condition(start, end)
        purchase = and_(
            window,
            StorefrontWalletTxn.kind == "purchase",
            StorefrontWalletTxn.amount_toman < 0,
        )
        reversal = and_(
            window,
            StorefrontWalletTxn.kind.in_(("refund", "renew_reversal")),
            StorefrontWalletTxn.amount_toman > 0,
        )
        columns.extend(
            [
                func.coalesce(
                    func.sum(case((purchase, -StorefrontWalletTxn.amount_toman), else_=0)), 0
                ).label(f"{prefix}_gross"),
                func.coalesce(
                    func.sum(case((reversal, StorefrontWalletTxn.amount_toman), else_=0)), 0
                ).label(f"{prefix}_reversals"),
            ]
        )
        for bucket, op_type in (("purchase", "purchase"), ("renewal", "renewal")):
            bucket_condition = and_(purchase, StorefrontOperation.op_type == op_type)
            columns.extend(
                [
                    func.sum(case((bucket_condition, 1), else_=0)).label(
                        f"{prefix}_{bucket}_count"
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (bucket_condition, -StorefrontWalletTxn.amount_toman), else_=0
                            )
                        ),
                        0,
                    ).label(f"{prefix}_{bucket}_amount"),
                ]
            )
        unknown = and_(
            purchase,
            or_(
                StorefrontOperation.id.is_(None),
                StorefrontOperation.op_type.not_in(("purchase", "renewal")),
            ),
        )
        columns.extend(
            [
                func.sum(case((unknown, 1), else_=0)).label(f"{prefix}_unknown_count"),
                func.coalesce(
                    func.sum(
                        case((unknown, -StorefrontWalletTxn.amount_toman), else_=0)
                    ),
                    0,
                ).label(f"{prefix}_unknown_amount"),
            ]
        )

    earliest = min(start for start, _end in periods.values())
    latest = max(end for _start, end in periods.values())
    row = (
        await session.execute(
            select(*columns)
            .select_from(StorefrontWalletTxn)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .outerjoin(
                StorefrontOperation,
                and_(
                    StorefrontOperation.id == StorefrontWalletTxn.operation_id,
                    StorefrontOperation.storefront_bot_id == storefront_id,
                ),
            )
            .where(
                StorefrontCustomer.storefront_bot_id == storefront_id,
                StorefrontWalletTxn.status == "done",
                StorefrontWalletTxn.created_at >= earliest,
                StorefrontWalletTxn.created_at < latest,
                StorefrontWalletTxn.kind.in_(("purchase", "refund", "renew_reversal")),
            )
        )
    ).mappings().one()

    result: dict[str, SalesPeriodOut] = {}
    for prefix in periods:
        gross = int(row[f"{prefix}_gross"] or 0)
        reversals = int(row[f"{prefix}_reversals"] or 0)
        result[prefix] = SalesPeriodOut(
            gross_sales_toman=gross,
            reversals_toman=reversals,
            net_sales_toman=gross - reversals,
            purchase=SalesBucketOut(
                count=int(row[f"{prefix}_purchase_count"] or 0),
                amount_toman=int(row[f"{prefix}_purchase_amount"] or 0),
            ),
            renewal=SalesBucketOut(
                count=int(row[f"{prefix}_renewal_count"] or 0),
                amount_toman=int(row[f"{prefix}_renewal_amount"] or 0),
            ),
            unknown=SalesBucketOut(
                count=int(row[f"{prefix}_unknown_count"] or 0),
                amount_toman=int(row[f"{prefix}_unknown_amount"] or 0),
            ),
        )
    return result


async def _operation_states(session: AsyncSession, storefront_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(StorefrontOperation.status, func.count(StorefrontOperation.id))
            .where(StorefrontOperation.storefront_bot_id == storefront_id)
            .group_by(StorefrontOperation.status)
        )
    ).all()
    counts = {state: 0 for state in OPERATION_STATES}
    for state, count in rows:
        if state in counts:
            counts[state] = int(count)
    return counts


async def dashboard(
    session: AsyncSession,
    access: StorefrontAccess,
    day_from: dt.date,
    day_to: dt.date,
    *,
    now: dt.datetime | None = None,
) -> StorefrontDashboardOut:
    """Return the fixed Release-A dashboard using at most nine reporting queries."""
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(TEHRAN)
    today = current.date()
    month_start = today.replace(day=1)
    periods = {
        "today": tehran_bounds(today, today),
        "month": tehran_bounds(month_start, today),
        "range": tehran_bounds(day_from, day_to),
    }
    sales = await _sales_periods(session, access.shop.id, periods)
    active_cutoff = current.astimezone(dt.timezone.utc) - dt.timedelta(days=30)

    customer_row = (
        await session.execute(
            select(
                func.count(StorefrontCustomer.id),
                func.sum(case((StorefrontCustomer.last_seen_at >= active_cutoff, 1), else_=0)),
                func.coalesce(func.sum(StorefrontCustomer.wallet_balance_toman), 0),
            ).where(StorefrontCustomer.storefront_bot_id == access.shop.id)
        )
    ).one()
    customers = CustomerMetricsOut(
        total=int(customer_row[0] or 0),
        active_30d=int(customer_row[1] or 0),
        wallet_liability_toman=int(customer_row[2] or 0),
    )

    status_rows = (
        await session.execute(
            select(StorefrontOrder.status, func.count(StorefrontOrder.id))
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .where(StorefrontCustomer.storefront_bot_id == access.shop.id)
            .group_by(StorefrontOrder.status)
        )
    ).all()
    service_states = {state: 0 for state in SERVICE_STATES}
    for state, count in status_rows:
        if state in service_states:
            service_states[state] = int(count)

    expiry_orders = list(
        (
            await session.execute(
                select(StorefrontOrder)
                .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
                .where(
                    StorefrontCustomer.storefront_bot_id == access.shop.id,
                    StorefrontOrder.is_trial.is_(False),
                    StorefrontOrder.status == "provisioned",
                )
            )
        ).scalars().all()
    )
    snapshot_keys = {
        (order.panel_id, order.panel_user_uuid)
        for order in expiry_orders
        if order.panel_id is not None and order.panel_user_uuid
    }
    snapshots: dict[tuple[int, str], EndUserSnapshot] = {}
    if snapshot_keys:
        panel_ids = {panel_id for panel_id, _uuid in snapshot_keys}
        uuids = {uuid for _panel_id, uuid in snapshot_keys}
        snapshot_rows = (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.panel_id.in_(panel_ids),
                    EndUserSnapshot.user_uuid.in_(uuids),
                )
            )
        ).scalars().all()
        snapshots = {(snap.panel_id, snap.user_uuid): snap for snap in snapshot_rows}
    near_expiry = 0
    for order in expiry_orders:
        snap = (
            snapshots.get((order.panel_id, order.panel_user_uuid or ""))
            if order.panel_id is not None
            else None
        )
        days_left = _days_left(order, snap, today)
        if days_left is not None and 0 <= days_left <= 3:
            near_expiry += 1

    topup_row = (
        await session.execute(
            select(
                func.count(StorefrontWalletTxn.id),
                func.coalesce(func.sum(StorefrontWalletTxn.amount_toman), 0),
            )
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .where(
                StorefrontCustomer.storefront_bot_id == access.shop.id,
                StorefrontWalletTxn.kind == "topup",
                StorefrontWalletTxn.status == "pending",
            )
        )
    ).one()
    pending_topups = PendingTopupsOut(
        count=int(topup_row[0] or 0), amount_toman=int(topup_row[1] or 0)
    )

    credit_row = (
        await session.execute(
            select(
                func.count(StorefrontCreditRedemption.id),
                func.coalesce(func.sum(StorefrontCreditRedemption.bonus_toman), 0),
            )
            .join(
                StorefrontCustomer,
                StorefrontCustomer.id == StorefrontCreditRedemption.customer_id,
            )
            .where(
                StorefrontCustomer.storefront_bot_id == access.shop.id,
                StorefrontCreditRedemption.created_at >= periods["month"][0],
                StorefrontCreditRedemption.created_at < periods["month"][1],
            )
        )
    ).one()
    credits = CreditMetricsOut(
        redemptions=int(credit_row[0] or 0), bonus_toman=int(credit_row[1] or 0)
    )

    operation_states = await _operation_states(session, access.shop.id)

    trial_dates = (
        select(
            StorefrontOrder.customer_id.label("customer_id"),
            func.min(StorefrontOrder.created_at).label("trial_at"),
        )
        .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
        .where(
            StorefrontCustomer.storefront_bot_id == access.shop.id,
            StorefrontOrder.is_trial.is_(True),
            StorefrontOrder.status.in_(("provisioned", "disabled", "deleted")),
        )
        .group_by(StorefrontOrder.customer_id)
        .subquery()
    )
    paid_txn = aliased(StorefrontWalletTxn)
    refund_txn = aliased(StorefrontWalletTxn)
    purchase_op = aliased(StorefrontOperation)
    refunded = exists(
        select(refund_txn.id).where(
            refund_txn.customer_id == paid_txn.customer_id,
            refund_txn.order_id == paid_txn.order_id,
            refund_txn.kind == "refund",
            refund_txn.status == "done",
            refund_txn.amount_toman > 0,
        )
    )
    converted = exists(
        select(paid_txn.id)
        .join(
            purchase_op,
            and_(
                purchase_op.id == paid_txn.operation_id,
                purchase_op.storefront_bot_id == access.shop.id,
                purchase_op.op_type == "purchase",
                purchase_op.status == "done",
            ),
        )
        .where(
            paid_txn.customer_id == trial_dates.c.customer_id,
            paid_txn.kind == "purchase",
            paid_txn.status == "done",
            paid_txn.amount_toman < 0,
            paid_txn.created_at > trial_dates.c.trial_at,
            ~refunded,
        )
    )
    trial_row = (
        await session.execute(
            select(func.count(), func.sum(case((converted, 1), else_=0))).select_from(trial_dates)
        )
    ).one()
    trial_count = int(trial_row[0] or 0)
    converted_count = int(trial_row[1] or 0)
    trial_conversion = TrialConversionOut(
        trial_customers=trial_count,
        converted_customers=converted_count,
        rate=round(converted_count / trial_count, 4) if trial_count else None,
    )

    return StorefrontDashboardOut(
        storefront_id=access.shop.id,
        range=DashboardRangeOut(from_date=day_from, to_date=day_to),
        sales_today=sales["today"],
        sales_month=sales["month"],
        sales_range=sales["range"],
        customers=customers,
        service_states=service_states,
        near_expiry=near_expiry,
        pending_topups=pending_topups,
        credits=credits,
        operation_states=operation_states,
        trial_conversion=trial_conversion,
    )


async def health(session: AsyncSession, access: StorefrontAccess) -> StorefrontHealthOut:
    """Return only persisted health; never consult in-process bot manager state."""
    bot_error = classify_health_error(access.shop.last_error)
    panel_error = classify_health_error(access.panel.last_error)
    return StorefrontHealthOut(
        storefront_id=access.shop.id,
        bot=BotHealthOut(
            enabled=access.shop.enabled,
            status=access.shop.status,
            error_class=bot_error,  # type: ignore[arg-type]
            state_updated_at=access.shop.updated_at,
        ),
        panel=PanelHealthOut(
            id=access.panel.id,
            key=access.panel.key,
            enabled=access.panel.enabled,
            status=access.panel.status.value,
            last_synced_at=access.panel.last_synced_at,
            error_class=panel_error,  # type: ignore[arg-type]
            state_updated_at=access.panel.updated_at,
        ),
        operation_states=await _operation_states(session, access.shop.id),
    )
