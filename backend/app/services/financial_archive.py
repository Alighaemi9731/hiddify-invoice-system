"""
Write-through to the durable financial ledger (`financial_records`).

Call `record(...)` whenever an invoice is created/updated or its payment status
changes. Keyed by invoice_id (one ledger row per invoice within an install). The
ledger is never deleted by the "wipe data" reset, so financial history is permanent.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FinancialRecord, Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus

log = logging.getLogger("financial_archive")


def _status_str(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


async def record(
    session: AsyncSession,
    invoice: Invoice,
    *,
    panel: Panel | None = None,
    reseller: Reseller | None = None,
    txid: str | None = None,
    commit: bool = False,
) -> None:
    """Upsert the ledger row for `invoice`. Best-effort: never raises into callers.

    Drafts are NOT recorded (they're transient until sent); if an invoice reverts to
    draft, its ledger row is removed — so the durable ledger only ever holds real
    (sent / paid / overdue / enforced / canceled) invoices."""
    try:
        if _status_str(invoice.status) == InvoiceStatus.draft.value:
            await session.execute(
                delete(FinancialRecord).where(FinancialRecord.invoice_id == invoice.id)
            )
            if commit:
                await session.commit()
            return

        if panel is None:
            panel = await session.get(Panel, invoice.panel_id)
        if reseller is None:
            reseller = await session.get(Reseller, invoice.reseller_id)

        row = (
            await session.execute(
                select(FinancialRecord).where(FinancialRecord.invoice_id == invoice.id)
            )
        ).scalar_one_or_none()
        if row is None:
            row = FinancialRecord(invoice_id=invoice.id)
            session.add(row)

        row.panel_key = (getattr(panel, "key", "") or "")[:128]
        row.reseller_name = (getattr(reseller, "name", "") or "")[:255]
        row.reseller_admin_uuid = (getattr(reseller, "admin_uuid", "") or "")[:64]
        row.period_label = invoice.period_label or ""
        row.period_start = invoice.period_start
        row.period_end = invoice.period_end
        row.usage_gb = invoice.usage_gb
        row.price_per_gb = invoice.price_per_gb
        row.amount_toman = invoice.amount_toman
        row.amount_usdt = invoice.amount_usdt
        row.status = invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status)
        row.paid_at = invoice.paid_at
        if txid:
            row.txid = txid[:128]
        if commit:
            await session.commit()
    except Exception:  # noqa: BLE001 — the ledger must never break a billing/payment flow
        log.warning("financial_archive.record failed for invoice %s", getattr(invoice, "id", "?"),
                    exc_info=True)
