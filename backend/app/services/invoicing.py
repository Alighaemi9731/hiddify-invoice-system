"""
Invoice orchestration: run the engine over snapshot data and persist Invoice +
InvoiceLine rows. Delivery (bot) is M4; this only generates draft invoices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Invoice, InvoiceLine, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import financial_archive, metering, pricing
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
    from app.services import rates, settings_service

    # In auto mode, pull a fresh live rate right before billing so the Toman→USDT figures are
    # current. Strictly best-effort — a fetch OR settings-write failure must never abort the
    # billing run; the last good rate stays in place.
    if str(await settings_service.get(session, "rate_mode", "manual")).lower() == "auto":
        try:
            await rates.refresh_auto_rate(session)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("invoicing").warning("pre-billing rate refresh failed", exc_info=True)
    # TON payment shows the customer a TON amount → keep that rate fresh at billing time too.
    if await settings_service.get(session, "pay_ton_enabled", False):
        try:
            await rates.refresh_ton_rate(session)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("invoicing").warning("pre-billing TON rate refresh failed", exc_info=True)

    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    free_threshold = await pricing.get_free_threshold_gb(session)
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
            default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
            panel_synced_at=panel.last_synced_at,
        )
        for bundle in bundles:
            # Abuse-resistant extra (overage from usage resets + renew-by-edit), added
            # on top of the normal snapshot total. See app.services.metering.
            extra = await metering.bundle_extra(
                session, panel.id, bundle.admin_uuids, period.label, free_threshold
            )
            if bundle.total_gb + extra["gb"] <= 0:
                summary.zero_skipped += 1
                continue
            await _persist_bundle(session, panel, bundle, extra, period, rate, summary, force)

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
    free_threshold = await pricing.get_free_threshold_gb(session)
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
            default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
            panel_synced_at=panel.last_synced_at,
        ):
            out.append((panel, b))
    return out


async def recompute_invoice(
    session: AsyncSession, invoice: Invoice, *, sync_first: bool = True
) -> dict:
    """Refresh ONE invoice's figures from the panel's CURRENT data, keeping its status
    (sent/overdue/…). Used to correct an already-sent invoice after the reseller fixed
    a mistake (e.g. removed a wrong user) in the panel. Paid invoices are protected."""
    if invoice.status == InvoiceStatus.paid:
        raise ValueError("paid invoice cannot be recomputed")

    panel = await session.get(Panel, invoice.panel_id)
    if panel is None:
        raise ValueError("panel not found")

    synced = False
    if sync_first:
        try:
            from app.models.enums import SyncStatus
            from app.services import sync as sync_service

            run = await sync_service.sync_panel(session, panel)
            synced = run.status == SyncStatus.success
            panel = await session.get(Panel, invoice.panel_id)        # re-attach post-commit
            invoice = await session.get(Invoice, invoice.id)
        except Exception:  # noqa: BLE001 — fall back to existing snapshots
            log.warning("recompute: panel sync failed, using existing snapshots", exc_info=True)

    period = Period(invoice.period_start, invoice.period_end)
    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    free_threshold = await pricing.get_free_threshold_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)
    rate = await pricing.get_rate(session) or int(invoice.usdt_rate or 0)

    resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
    ).scalars().all()
    users = (
        await session.execute(select(EndUserSnapshot).where(EndUserSnapshot.panel_id == panel.id))
    ).scalars().all()
    bundles = compute_invoices(
        resellers, users, period,
        default_price_per_gb=default_price, excluded_usage_gb=excluded,
        default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
        panel_synced_at=panel.last_synced_at,
    )
    bundle = next((b for b in bundles if b.root.id == invoice.reseller_id), None)

    base_gb = bundle.total_gb if bundle else 0.0
    base_lines = bundle.lines if bundle else []
    price = bundle.price_per_gb if bundle else invoice.price_per_gb
    min_sale = bundle.min_sale_toman if bundle else 0
    if bundle:
        admin_uuids = bundle.admin_uuids
    else:
        from app.services.reseller_report import node_descendants
        reseller = await session.get(Reseller, invoice.reseller_id)
        admin_uuids = {d.admin_uuid for d in await node_descendants(session, reseller)} if reseller else set()
    extra = await metering.bundle_extra(session, panel.id, admin_uuids, period.label, free_threshold)

    total_gb = round(base_gb + float(extra["gb"] or 0), 3)
    base_amount = round(total_gb * price)
    floor_applied = base_amount > 0 and min_sale > 0 and base_amount < min_sale
    amount_toman = float(min_sale) if floor_applied else float(base_amount)

    await session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    invoice.usage_gb = total_gb
    invoice.users_count = (bundle.users_count if bundle else 0) + len(extra["lines"])
    invoice.price_per_gb = price
    invoice.base_amount_toman = base_amount
    invoice.min_sale_toman = min_sale
    invoice.floor_applied = floor_applied
    invoice.amount_toman = amount_toman
    for line in base_lines:
        # A user removed from the panel is billed on consumption and flagged in its name so the
        # reseller sees why (same naming convention as the metering "extra" lines below).
        nm = ((line.name or "")[:235] + " — مصرف حذف‌شده از پنل") if line.from_deleted else line.name[:255]
        session.add(InvoiceLine(
            invoice_id=invoice.id, end_user_uuid=line.user_uuid, name=nm,
            start_date=line.start_date, usage_gb=line.usage_gb,
            added_by_uuid=line.added_by_uuid,
            sub_reseller_name=(line.sub_reseller_name or "")[:255],
        ))
    for el in extra["lines"]:
        session.add(InvoiceLine(
            invoice_id=invoice.id, end_user_uuid=el["user_uuid"],
            name=((el.get("name") or "")[:235] + " — مصرف اضافه/تمدید"),
            start_date=None, usage_gb=el["usage_gb"], added_by_uuid=el.get("added_by_uuid"),
            sub_reseller_name="",
        ))
    invoice.usdt_rate = rate
    invoice.amount_usdt = float(pricing.toman_to_usdt(invoice.amount_toman, rate))
    await financial_archive.record(session, invoice)
    await session.commit()
    return {"synced": synced, "found": bundle is not None,
            "amount_toman": float(invoice.amount_toman), "usage_gb": float(invoice.usage_gb)}


async def _persist_bundle(
    session: AsyncSession,
    panel: Panel,
    bundle: BundleResult,
    extra: dict,
    period: Period,
    rate: int,
    summary: GenerationSummary,
    force: bool,
) -> None:
    reseller: Reseller = bundle.root
    # Combine the normal snapshot total with the abuse-metered extra, then apply the floor.
    price = bundle.price_per_gb
    total_gb = round(bundle.total_gb + float(extra.get("gb", 0) or 0), 3)
    base_amount = round(total_gb * price)
    min_sale = bundle.min_sale_toman
    floor_applied = base_amount > 0 and min_sale > 0 and base_amount < min_sale
    amount_toman = float(min_sale) if floor_applied else float(base_amount)
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

    # A PAID invoice is settled accounting — NEVER recompute it, even with force.
    if existing and existing.status == InvoiceStatus.paid:
        summary.skipped_existing += 1
        return
    # Other already-delivered invoices (sent/overdue/enforced) are left as-is unless
    # the caller explicitly forces a recompute.
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
    invoice.usage_gb = total_gb
    invoice.users_count = bundle.users_count + len(extra.get("lines", []))
    invoice.price_per_gb = price
    invoice.amount_toman = amount_toman
    invoice.base_amount_toman = base_amount
    invoice.min_sale_toman = min_sale
    invoice.floor_applied = floor_applied
    invoice.usdt_rate = rate
    invoice.amount_usdt = amount_usdt
    if existing is None or existing.status == InvoiceStatus.draft:
        invoice.status = InvoiceStatus.draft
    await session.flush()

    for line in bundle.lines:
        # A user removed from the panel is billed on consumption and flagged in its name (same
        # convention as the metering "extra" lines below) so the reseller sees why.
        nm = ((line.name or "")[:235] + " — مصرف حذف‌شده از پنل") if line.from_deleted else line.name[:255]
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                end_user_uuid=line.user_uuid,
                name=nm,
                start_date=line.start_date,
                usage_gb=line.usage_gb,
                added_by_uuid=line.added_by_uuid,
                sub_reseller_name=(line.sub_reseller_name or "")[:255],
            )
        )
    # Abnormal (metered) extra as explicit lines so the PDF/detail shows them.
    for el in extra.get("lines", []):
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                end_user_uuid=el["user_uuid"],
                name=((el.get("name") or "")[:235] + " — مصرف اضافه/تمدید"),
                start_date=None,
                usage_gb=el["usage_gb"],
                added_by_uuid=el.get("added_by_uuid"),
                sub_reseller_name="",
            )
        )

    # Mirror into the durable financial ledger (survives wipes / panel removal).
    await financial_archive.record(session, invoice, panel=panel, reseller=reseller)

    summary.total_amount_toman += amount_toman
    summary.invoice_ids.append(invoice.id)
