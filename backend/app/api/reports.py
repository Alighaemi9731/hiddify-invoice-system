"""Analytics: sales (sortable), sales-by-panel, debts, dashboard summary."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import (
    DeliveryLog, EnforcementAction, FinancialRecord, Invoice, Panel, Reseller,
)
from app.models.enums import InvoiceStatus
from app.schemas.reports import (
    DashboardSummary,
    DebtRow,
    DeliveryLogRow,
    EnforcementActionRow,
    PanelSalesRow,
    SalesRow,
    StatusCount,
)
from app.services.periods import current_month, parse_period

router = APIRouter(
    prefix="/api/reports", tags=["reports"], dependencies=[Depends(get_current_subject)]
)

# "Owed" = delivered but not yet paid.
OUTSTANDING = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
# Sales/analytics count only DELIVERED invoices — drafts are not real sales yet.
COUNTED = (
    InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced, InvoiceStatus.paid,
)


def _sales_row(inv: Invoice, name: str, key: str) -> SalesRow:
    return SalesRow(
        invoice_id=inv.id, reseller_id=inv.reseller_id, reseller_name=name, panel_key=key,
        usage_gb=float(inv.usage_gb), amount_toman=float(inv.amount_toman),
        status=inv.status.value,
    )


async def _period_rows(session: AsyncSession, period_label: str, panel_id: int | None):
    q = (
        select(Invoice, Reseller.name, Panel.key)
        .join(Reseller, Invoice.reseller_id == Reseller.id)
        .join(Panel, Invoice.panel_id == Panel.id)
        .where(Invoice.period_label == period_label, Invoice.status.in_(COUNTED))
    )
    if panel_id is not None:
        q = q.where(Invoice.panel_id == panel_id)
    return (await session.execute(q)).all()


@router.get("/sales", response_model=list[SalesRow])
async def sales(
    period: str | None = None,
    panel_id: int | None = None,
    sort: str = Query("amount"),
    order: str = Query("desc"),
    session: AsyncSession = Depends(get_session),
) -> list[SalesRow]:
    label = period or current_month().label
    rows = await _period_rows(session, label, panel_id)
    out = [_sales_row(inv, name, key) for inv, name, key in rows]
    keyfn = {
        "amount": lambda r: r.amount_toman,
        "usage": lambda r: r.usage_gb,
        "name": lambda r: r.reseller_name,
    }.get(sort, lambda r: r.amount_toman)
    out.sort(key=keyfn, reverse=(order != "asc"))
    return out


@router.get("/sales-by-panel", response_model=list[PanelSalesRow])
async def sales_by_panel(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[PanelSalesRow]:
    label = period or current_month().label
    rows = await _period_rows(session, label, None)
    agg: dict[int, dict] = defaultdict(lambda: {"key": "", "invoices": 0, "gb": 0.0, "t": 0.0})
    for inv, _name, key in rows:
        a = agg[inv.panel_id]
        a["key"] = key
        a["invoices"] += 1
        a["gb"] += float(inv.usage_gb)
        a["t"] += float(inv.amount_toman)
    out = [
        PanelSalesRow(panel_id=pid, panel_key=a["key"], invoices=a["invoices"],
                      usage_gb=round(a["gb"], 2), amount_toman=a["t"])
        for pid, a in agg.items()
    ]
    out.sort(key=lambda r: r.amount_toman, reverse=True)
    return out


@router.get("/debts", response_model=list[DebtRow])
async def debts(session: AsyncSession = Depends(get_session)) -> list[DebtRow]:
    q = (
        select(Invoice, Reseller.name, Panel.key, Reseller.bot_chat_id)
        .join(Reseller, Invoice.reseller_id == Reseller.id)
        .join(Panel, Invoice.panel_id == Panel.id)
        .where(Invoice.status.in_(OUTSTANDING))
    )
    rows = (await session.execute(q)).all()
    agg: dict[int, dict] = defaultdict(
        lambda: {"name": "", "key": "", "reg": False, "count": 0, "t": 0.0, "oldest": None}
    )
    for inv, name, key, chat in rows:
        a = agg[inv.reseller_id]
        a["name"], a["key"], a["reg"] = name, key, chat is not None
        a["count"] += 1
        a["t"] += float(inv.amount_toman)
        if a["oldest"] is None or inv.period_label < a["oldest"]:
            a["oldest"] = inv.period_label
    out = [
        DebtRow(reseller_id=rid, reseller_name=a["name"], panel_key=a["key"],
                bot_registered=a["reg"], invoices_count=a["count"],
                outstanding_toman=a["t"], oldest_period=a["oldest"])
        for rid, a in agg.items()
    ]
    out.sort(key=lambda r: r.outstanding_toman, reverse=True)
    return out


@router.get("/delivery-log", response_model=list[DeliveryLogRow])
async def delivery_log(
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(200, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[DeliveryLogRow]:
    q = (
        select(DeliveryLog, Reseller.name)
        .outerjoin(Reseller, DeliveryLog.reseller_id == Reseller.id)
        .order_by(DeliveryLog.created_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(DeliveryLog.status == status)
    if kind:
        q = q.where(DeliveryLog.kind == kind)
    rows = (await session.execute(q)).all()
    return [
        DeliveryLogRow(
            id=dl.id, reseller_id=dl.reseller_id, reseller_name=name, invoice_id=dl.invoice_id,
            kind=dl.kind.value, status=dl.status.value, error=dl.error, created_at=dl.created_at,
        )
        for dl, name in rows
    ]


@router.get("/enforcement-actions", response_model=list[EnforcementActionRow])
async def enforcement_actions(
    limit: int = Query(200, le=2000), session: AsyncSession = Depends(get_session)
) -> list[EnforcementActionRow]:
    q = (
        select(EnforcementAction, Reseller.name)
        .outerjoin(Reseller, EnforcementAction.reseller_id == Reseller.id)
        .order_by(EnforcementAction.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(q)).all()
    return [
        EnforcementActionRow(
            id=a.id, reseller_id=a.reseller_id, reseller_name=name, invoice_id=a.invoice_id,
            action=a.action.value, status=a.status.value, dry_run=a.dry_run,
            affected_count=a.affected_count, error=a.error, created_at=a.created_at,
        )
        for a, name in rows
    ]


@router.get("/financial-history")
async def financial_history(
    period: str | None = None,
    panel_key: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(1000, le=10000),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Durable ledger: panel, reseller, month, amount, paid/unpaid — kept permanently
    even after a data wipe or panel/reseller removal."""
    query = select(FinancialRecord).order_by(
        FinancialRecord.period_label.desc(), FinancialRecord.amount_toman.desc()
    )
    if period:
        query = query.where(FinancialRecord.period_label == period)
    if panel_key:
        query = query.where(FinancialRecord.panel_key == panel_key)
    if status:
        query = query.where(FinancialRecord.status == status)
    if q:
        query = query.where(FinancialRecord.reseller_name.ilike(f"%{q}%"))
    rows = (await session.execute(query.limit(limit))).scalars().all()
    return [
        {
            "id": r.id,
            "invoice_id": r.invoice_id,
            "panel_key": r.panel_key,
            "reseller_name": r.reseller_name,
            "reseller_admin_uuid": r.reseller_admin_uuid,
            "period_label": r.period_label,
            "usage_gb": float(r.usage_gb or 0),
            "price_per_gb": int(r.price_per_gb or 0),
            "amount_toman": float(r.amount_toman or 0),
            "status": r.status,
            "paid_at": r.paid_at,
            "txid": r.txid,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/zero-invoices")
async def zero_invoices(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Billable resellers whose bundle total for the period is zero (computed live)."""
    from app.services import invoicing
    from app.services.periods import current_month, parse_period

    p = parse_period(period) if period else current_month()
    pairs = await invoicing.preview_bundles(session, p)
    rows = [
        {
            "reseller_id": b.root.id,
            "reseller_name": b.root.name,
            "panel_key": panel.key,
            "sub_resellers": max(0, len(b.admin_uuids) - 1),
            "registered": b.root.bot_chat_id is not None,
        }
        for panel, b in pairs if b.total_gb <= 0
    ]
    rows.sort(key=lambda r: r["reseller_name"])
    return rows


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> DashboardSummary:
    label = (parse_period(period).label if period else current_month().label)

    panels = (await session.execute(select(func.count(Panel.id)))).scalar_one()
    # Count only MAIN (top-level) resellers — NOT their sub-resellers — so the dashboard
    # matches the «فهرست» tab and the bot «آمار کلی» exactly (shared reseller_stats logic).
    from app.services.reseller_stats import load_root_stats

    stats = await load_root_stats(session)
    resellers = stats.billable          # non-exempt main resellers (the «N نمایندهٔ اصلی» figure)
    billable = stats.billable
    registered = stats.connected        # of those, how many are connected to the bot
    invoices_total = (await session.execute(select(func.count(Invoice.id)))).scalar_one()

    rows = await _period_rows(session, label, None)
    billed_t = sum(float(i.amount_toman) for i, _, _ in rows)
    paid_t = sum(float(i.amount_toman) for i, _, _ in rows if i.status == InvoiceStatus.paid)

    status_map: dict[str, int] = defaultdict(int)
    for i, _, _ in rows:
        status_map[i.status.value] += 1

    # outstanding across all periods
    out_rows = (
        await session.execute(
            select(Invoice.amount_toman).where(Invoice.status.in_(OUTSTANDING))
        )
    ).all()
    out_t = sum(float(t) for (t,) in out_rows)

    by_panel = await sales_by_panel(label, session)  # type: ignore[arg-type]
    sales_rows = [_sales_row(i, n, k) for i, n, k in rows]
    sales_rows.sort(key=lambda r: r.amount_toman, reverse=True)

    return DashboardSummary(
        period=label, panels=panels, resellers=resellers, billable_resellers=billable,
        registered_resellers=registered, invoices_total=invoices_total,
        period_billed_toman=billed_t,
        period_paid_toman=paid_t, outstanding_toman=out_t,
        status_counts=[StatusCount(status=k, count=v) for k, v in status_map.items()],
        sales_by_panel=by_panel, top_resellers=sales_rows[:10],
    )
