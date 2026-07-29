"""Analytics: sales (sortable), sales-by-panel, debts, dashboard summary."""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import proccache
from app.core.db import SessionLocal, get_session
from app.core.security import get_current_subject
from app.models import (
    BotUser,
    DeliveryLog,
    EnforcementAction,
    FinancialRecord,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import InvoiceStatus, PanelStatus
from app.schemas.reports import (
    DashboardSummary,
    DebtRow,
    DeliveryLogRow,
    EnforcementActionRow,
    HighVolumeDeleteResult,
    HighVolumeDeleteRow,
    PanelSalesRow,
    SalesByDayRow,
    SalesRow,
    StatusCount,
)
from app.services import invoicing
from app.services.periods import current_month, month_period, parse_period

router = APIRouter(
    prefix="/api/reports", tags=["reports"], dependencies=[Depends(get_current_subject)]
)

log = logging.getLogger("api.reports")

# "Owed" = delivered but not yet paid.
OUTSTANDING = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
# Sales/analytics count only DELIVERED invoices — drafts are not real sales yet.
COUNTED = (
    InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced, InvoiceStatus.paid,
)

# sales-by-day and zero-invoices run the FULL billing preview (`invoicing.preview_bundles`
# loads every enabled panel's resellers + end-user snapshots as ORM objects) and were
# recomputed on EVERY Dashboard/Invoices visit — seconds of single-worker CPU per view.
# Their inputs only change when a panel syncs (or pricing settings change), so the result
# is cached per (period, per-panel last_synced_at) with a short TTL as the settings-edit
# safety valve. Billing generation itself is NEVER cached.
_preview_cache = proccache.TTLCache(ttl_seconds=300.0)


async def _panel_sync_state(session: AsyncSession) -> tuple:
    rows = (
        await session.execute(
            select(Panel.id, Panel.last_synced_at)
            .where(Panel.enabled.is_(True))
            .order_by(Panel.id)
        )
    ).all()
    return tuple((pid, ts.isoformat() if ts else "") for pid, ts in rows)


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


def _panel_sales_from_rows(rows) -> list[PanelSalesRow]:
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


@router.get("/sales-by-panel", response_model=list[PanelSalesRow])
async def sales_by_panel(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[PanelSalesRow]:
    label = period or current_month().label
    return _panel_sales_from_rows(await _period_rows(session, label, None))


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
    def _progress(a: EnforcementAction) -> dict | None:
        p = (a.snapshot or {}).get("progress") if a.snapshot else None
        if not isinstance(p, dict):
            return None
        return {
            "phase": p.get("phase"),
            "users_done": len(p.get("users_done") or []),
            "users_missing": len(p.get("users_missing") or []),
            "users_failed": len(p.get("users_failed") or {}),
            "admins_done": len(p.get("admins_done") or []),
            "admins_missing": len(p.get("admins_missing") or []),
            "admins_failed": len(p.get("admins_failed") or {}),
        }

    return [
        EnforcementActionRow(
            id=a.id, reseller_id=a.reseller_id, reseller_name=name, invoice_id=a.invoice_id,
            action=a.action.value, status=a.status.value, dry_run=a.dry_run,
            affected_count=a.affected_count, error=a.error, progress=_progress(a),
            created_at=a.created_at,
        )
        for a, name in rows
    ]


@router.get("/financial-history")
async def financial_history(
    response: Response,
    period: str | None = None,
    panel_key: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Durable ledger: panel, reseller, month, amount, paid/unpaid — kept permanently
    even after a data wipe or panel/reseller removal. Server-paged: the ledger grows
    forever by design, so the SPA must never download it whole; the filtered total is
    returned in `X-Total-Count`. The sort is served by ix_finrec_period_amount."""
    filters = []
    if period:
        filters.append(FinancialRecord.period_label == period)
    if panel_key:
        filters.append(FinancialRecord.panel_key == panel_key)
    if status:
        filters.append(FinancialRecord.status == status)
    if q:
        filters.append(FinancialRecord.reseller_name.ilike(f"%{q}%"))

    total, amount_sum, paid_sum = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(FinancialRecord.amount_toman), 0),
                func.coalesce(
                    func.sum(
                        case((FinancialRecord.status == "paid", FinancialRecord.amount_toman), else_=0)
                    ),
                    0,
                ),
            ).select_from(FinancialRecord).where(*filters)
        )
    ).one()
    response.headers["X-Total-Count"] = str(total)
    # Whole-filtered-set sums for the header cards (a single page can't compute them).
    response.headers["X-Total-Amount-Toman"] = str(int(amount_sum))
    response.headers["X-Paid-Amount-Toman"] = str(int(paid_sum))

    query = (
        select(FinancialRecord)
        .where(*filters)
        .order_by(
            FinancialRecord.period_label.desc(),
            FinancialRecord.amount_toman.desc(),
            FinancialRecord.id.desc(),
        )
    )
    rows = (await session.execute(query.limit(limit).offset(offset))).scalars().all()
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
    cache_key = (proccache.engine_ns(session), "zero-invoices", p.label,
                 await _panel_sync_state(session))
    hit = _preview_cache.get(cache_key)
    if hit is not proccache.MISS:
        return hit
    pairs = await invoicing.preview_bundles(session, p)
    zero = [(panel, b) for panel, b in pairs if b.total_gb <= 0]
    # Resolve Telegram @usernames for the connected resellers → clickable PV deep-link.
    chat_ids = [b.root.bot_chat_id for _, b in zero if b.root.bot_chat_id]
    usernames: dict[int, str] = {}
    if chat_ids:
        usernames = {
            tid: uname
            for tid, uname in (
                await session.execute(
                    select(BotUser.telegram_id, BotUser.username).where(
                        BotUser.telegram_id.in_(chat_ids), BotUser.username.is_not(None)
                    )
                )
            ).all()
        }
    rows = [
        {
            "reseller_id": b.root.id,
            "reseller_name": b.root.name,
            "panel_key": panel.key,
            "sub_resellers": max(0, len(b.admin_uuids) - 1),
            "registered": b.root.bot_chat_id is not None,
            "reseller_chat_id": b.root.bot_chat_id,
            "reseller_username": usernames.get(b.root.bot_chat_id) if b.root.bot_chat_id else None,
        }
        for panel, b in zero
    ]
    rows.sort(key=lambda r: r["reseller_name"])
    _preview_cache.put(cache_key, rows)
    return rows


# ---- high-volume users (the 1000 GB mistake) + warn their responsible admin ----
class HighVolumeWarnBody(BaseModel):
    threshold: float | None = None
    panel_id: int | None = None
    this_month_only: bool = True
    root_reseller_ids: list[int] | None = None  # None = all admins in the list; else only these


async def _resolve_threshold(session: AsyncSession, threshold: float | None) -> float:
    if threshold is not None:
        return threshold
    from app.services import settings_service

    return float(await settings_service.get(session, "high_volume_gb_threshold", 1000) or 1000)


@router.get("/high-volume-users")
async def high_volume_users(
    threshold: float | None = None, panel_id: int | None = None,
    this_month_only: bool = True, session: AsyncSession = Depends(get_session),
) -> dict:
    """End-users with a very large sold quota (the 1000 GB default left by mistake) + the
    responsible top-level reseller. Mirrors the invoice engine, so it matches what will be billed."""
    from app.services import high_volume as hv

    th = await _resolve_threshold(session, threshold)
    rows = await hv.high_volume_users(
        session, threshold=th, panel_id=panel_id, this_month_only=this_month_only)
    return {"threshold": th, "rows": rows}


async def _high_volume_warn_bg(reachable: list, texts: dict, unregistered: int) -> None:
    try:
        async with SessionLocal() as session:
            from app.services import broadcast as bc

            await bc.run_broadcast(
                session, "", reachable, unregistered=unregistered,
                render=lambda rec: texts.get(rec["reseller_id"], ""), parse_mode="HTML")
    except Exception:  # noqa: BLE001
        log.exception("high-volume warn broadcast failed")


@router.post("/high-volume-users/warn")
async def high_volume_users_warn(
    body: HighVolumeWarnBody, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """DM each responsible top-level reseller one aggregated warning listing their high-volume
    users. Runs in the background via the shared broadcast worker; nothing is persisted."""
    from app.services import high_volume as hv

    th = await _resolve_threshold(session, body.threshold)
    reachable, texts, unregistered = await hv.build_warnings(
        session, threshold=th, panel_id=body.panel_id,
        this_month_only=body.this_month_only, root_reseller_ids=body.root_reseller_ids)
    if reachable:
        background.add_task(_high_volume_warn_bg, reachable, texts, unregistered)
    return {"status": "started", "total": len(reachable), "unregistered": unregistered}


class HighVolumeDeleteBody(BaseModel):
    # Capped because every id costs two panel round-trips plus a share of a heavy CSRF page load;
    # the UI chunks well below this, so the cap is a backstop against a runaway request.
    snapshot_ids: Annotated[list[int], Field(min_length=1, max_length=200)]
    threshold: float | None = None   # the same box as the list; the service floors it


@router.post("/high-volume-users/delete", response_model=HighVolumeDeleteResult)
async def high_volume_users_delete(
    body: HighVolumeDeleteBody, session: AsyncSession = Depends(get_session)
) -> HighVolumeDeleteResult:
    """Delete the given high-volume end-users on their Hiddify panel and purge them from billing.

    Panel first, then a per-user verification, then the local rows — so a row is only ever dropped
    once the panel is proven to no longer hold that user, and ANY panel failure purges nothing.
    Issued invoices and the financial ledger are untouched: only future computation changes, so the
    period's draft must be re-issued to reflect it."""
    from app.services import high_volume as hv

    th = await _resolve_threshold(session, body.threshold)
    res = await hv.delete_high_volume_users(
        session, snapshot_ids=body.snapshot_ids, threshold=th)
    return HighVolumeDeleteResult(
        deleted=res.deleted, already_absent=res.already_absent, purged=res.purged,
        skipped=res.skipped, failed=res.failed, meters_deleted=res.meters_deleted,
        rows=[
            HighVolumeDeleteRow(
                snapshot_id=r.snapshot_id,
                user_uuid=(r.user_uuid or "")[:8],   # truncated, exactly like the list row
                name=r.name, panel_key=r.panel_key,
                status=r.status.value, purged=r.purged, error=r.error,
            )
            for r in res.rows
        ],
    )


@router.get("/sales-by-day", response_model=list[SalesByDayRow])
async def sales_by_day(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[SalesByDayRow]:
    """Daily sale trend for the period: one bucket per day of the month, height = that day's share
    of the sale by each service's CREATION date (line usage_gb × the bundle root's price). Computed
    live via the invoice engine (present-filtered) so it matches what's billed and works for the
    in-progress current month. NOTE: per-bundle floor (min-sale) and metering overage are
    bundle-level, not per-line, so this is the faithful BASE-sale trend (sum of days ≈ month base)."""
    p = parse_period(period) if period else current_month()
    cache_key = (proccache.engine_ns(session), "sales-by-day", p.label,
                 await _panel_sync_state(session))
    hit = _preview_cache.get(cache_key)
    if hit is not proccache.MISS:
        return hit
    n_days = p.end.day
    buckets = [0.0] * n_days
    for _panel, b in await invoicing.preview_bundles(session, p):
        for line in b.lines:
            if line.start_date and p.contains(line.start_date):
                buckets[line.start_date.day - 1] += line.usage_gb * b.price_per_gb
    out = [
        SalesByDayRow(
            day=i + 1,
            date=dt.date(p.start.year, p.start.month, i + 1).isoformat(),
            amount_toman=round(v),
        )
        for i, v in enumerate(buckets)
    ]
    _preview_cache.put(cache_key, out)
    return out


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    period: str | None = None, session: AsyncSession = Depends(get_session)
) -> DashboardSummary:
    selected_period = parse_period(period) if period else current_month()
    label = selected_period.label
    previous_day = selected_period.start - dt.timedelta(days=1)
    previous_label = month_period(previous_day.year, previous_day.month).label

    panels = (await session.execute(select(func.count(Panel.id)))).scalar_one()
    active_panels = (
        await session.execute(
            select(func.count(Panel.id)).where(Panel.enabled.is_(True))
        )
    ).scalar_one()
    healthy_panels = (
        await session.execute(
            select(func.count(Panel.id)).where(
                Panel.enabled.is_(True), Panel.status == PanelStatus.ok
            )
        )
    ).scalar_one()
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
    previous_rows = await _period_rows(session, previous_label, None)
    previous_billed_t = sum(float(i.amount_toman) for i, _, _ in previous_rows)

    status_map: dict[str, int] = defaultdict(int)
    for i, _, _ in rows:
        status_map[i.status.value] += 1

    # outstanding across all periods — aggregated in SQL (was: load every outstanding
    # row into Python to sum/dedupe)
    out_t, outstanding_resellers = (
        await session.execute(
            select(
                func.coalesce(func.sum(Invoice.amount_toman), 0),
                func.count(func.distinct(Invoice.reseller_id)),
            ).where(Invoice.status.in_(OUTSTANDING))
        )
    ).one()
    out_t = float(out_t)

    # Reuse the period rows already loaded above (was: an identical second query
    # inside sales_by_panel).
    by_panel = _panel_sales_from_rows(rows)
    sales_rows = [_sales_row(i, n, k) for i, n, k in rows]
    sales_rows.sort(key=lambda r: r.amount_toman, reverse=True)

    return DashboardSummary(
        period=label, previous_period=previous_label,
        panels=panels, active_panels=active_panels, healthy_panels=healthy_panels,
        resellers=resellers, billable_resellers=billable,
        registered_resellers=registered, invoices_total=invoices_total,
        period_invoices=len(rows),
        period_billed_toman=billed_t,
        previous_period_billed_toman=previous_billed_t,
        period_paid_toman=paid_t, outstanding_toman=out_t,
        outstanding_resellers=outstanding_resellers,
        status_counts=[StatusCount(status=k, count=v) for k, v in status_map.items()],
        sales_by_panel=by_panel, top_resellers=sales_rows[:10],
    )
