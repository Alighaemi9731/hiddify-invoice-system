"""
Invoice orchestration: run the engine over snapshot data and persist Invoice +
InvoiceLine rows. Delivery (bot) is M4; this only generates draft invoices.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Invoice, InvoiceLine, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import pricing
from app.services.invoice_engine import BundleResult, compute_invoices
from app.services.periods import Period

log = logging.getLogger("invoicing")


@dataclass
class GenerationSummary:
    period: str
    created: int = 0
    updated: int = 0
    skipped_existing: int = 0
    zero_skipped: int = 0
    total_amount_toman: float = 0.0
    total_amount_usdt: float = 0.0
    invoice_ids: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.invoice_ids is None:
            self.invoice_ids = []


async def generate_invoices(
    session: AsyncSession,
    period: Period,
    *,
    panel_id: int | None = None,
    force: bool = False,
) -> GenerationSummary:
    """Generate draft invoices for `period`. Existing non-draft invoices are kept
    unless `force=True`. Draft invoices for the period are recomputed in place."""
    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)
    rate = await pricing.get_rate(session)

    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = select(Panel).where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    summary = GenerationSummary(period=period.label)

    for panel in panels:
        resellers = (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars().all()
        users = (
            await session.execute(
                select(EndUserSnapshot).where(EndUserSnapshot.panel_id == panel.id)
            )
        ).scalars().all()

        bundles = compute_invoices(
            resellers, users, period,
            default_price_per_gb=default_price, excluded_usage_gb=excluded,
            default_min_sale_toman=default_min_sale,
        )
        for bundle in bundles:
            if bundle.total_gb <= 0:
                summary.zero_skipped += 1
                continue
            await _persist_bundle(session, panel, bundle, period, rate, summary, force)

    await session.commit()
    log.info(
        "Generated invoices for %s: created=%d updated=%d skipped=%d total=%.0f T",
        period.label, summary.created, summary.updated,
        summary.skipped_existing, summary.total_amount_toman,
    )
    return summary


async def preview_bundles(
    session: AsyncSession, period: Period, *, panel_id: int | None = None
) -> list[tuple[Panel, BundleResult]]:
    """Compute bundles for a period WITHOUT persisting (used for the zero-invoice view)."""
    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)

    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = select(Panel).where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    out: list[tuple[Panel, BundleResult]] = []
    for panel in panels:
        resellers = (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars().all()
        users = (
            await session.execute(
                select(EndUserSnapshot).where(EndUserSnapshot.panel_id == panel.id)
            )
        ).scalars().all()
        for b in compute_invoices(
            resellers, users, period,
            default_price_per_gb=default_price, excluded_usage_gb=excluded,
            default_min_sale_toman=default_min_sale,
        ):
            out.append((panel, b))
    return out


async def _persist_bundle(
    session: AsyncSession,
    panel: Panel,
    bundle: BundleResult,
    period: Period,
    rate: int,
    summary: GenerationSummary,
    force: bool,
) -> None:
    reseller: Reseller = bundle.root
    amount_toman = bundle.amount_toman
    amount_usdt = float(pricing.toman_to_usdt(amount_toman, rate))

    existing = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller.id,
                Invoice.period_start == period.start,
                Invoice.period_end == period.end,
            )
        )
    ).scalar_one_or_none()

    if existing and existing.status != InvoiceStatus.draft and not force:
        summary.skipped_existing += 1
        return

    if existing:
        invoice = existing
        # Clear old lines before recomputation (explicit delete avoids a lazy load).
        await session.execute(
            delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
        )
        summary.updated += 1
    else:
        invoice = Invoice(reseller_id=reseller.id, panel_id=panel.id)
        session.add(invoice)
        summary.created += 1

    invoice.period_start = period.start
    invoice.period_end = period.end
    invoice.period_label = period.label
    invoice.usage_gb = bundle.total_gb
    invoice.users_count = bundle.users_count
    invoice.price_per_gb = bundle.price_per_gb
    invoice.amount_toman = amount_toman
    invoice.base_amount_toman = bundle.base_amount_toman
    invoice.min_sale_toman = bundle.min_sale_toman
    invoice.floor_applied = bundle.floor_applied
    invoice.usdt_rate = rate
    invoice.amount_usdt = amount_usdt
    if existing is None or existing.status == InvoiceStatus.draft:
        invoice.status = InvoiceStatus.draft
    await session.flush()

    for line in bundle.lines:
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                end_user_uuid=line.user_uuid,
                name=line.name[:255],
                start_date=line.start_date,
                usage_gb=line.usage_gb,
                added_by_uuid=line.added_by_uuid,
                sub_reseller_name=(line.sub_reseller_name or "")[:255],
            )
        )

    summary.total_amount_toman += amount_toman
    summary.total_amount_usdt += amount_usdt
    summary.invoice_ids.append(invoice.id)
