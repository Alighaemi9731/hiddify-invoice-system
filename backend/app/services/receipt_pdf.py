"""Render a payment RECEIPT PDF for a reseller after their payment is confirmed (M-receipt).

Unlike the invoice PDF (volume-only, M29), a receipt DOES show money — it is the reseller's own
confirmed payment to the owner. Reuses the reportlab renderer in `pdf.build_receipt_pdf`, run off
the event loop via `asyncio.to_thread` (same as `invoice_pdf`). Data is read from the persisted
Payment + the invoices it settled, so it always matches what was actually confirmed.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import payment_code
from app.models import Invoice, Payment, Reseller
from app.services import pdf as pdf_service
from app.services import settings_service
from app.services.invoice_pdf import _safe_name
from app.services.periods import to_local_date
from app.services.periods import today as tehran_today

# Payment method → Persian label for the receipt.
_METHOD_FA = {
    "usdt_txid": "USDT (BEP-20)",
    "ton_txid": "تون (TON)",
    "avax_txid": "آواکس (AVAX)",
    "card": "کارت‌به‌کارت",
    "screenshot": "رسید تصویری",
    "manual": "ثبت دستی",
}


def _settled_ids(payment: Payment) -> list[int]:
    """Invoice ids this payment covers (settled_invoice_ids, else the primary invoice_id).
    Inlined (not imported from payments.py) to avoid an import cycle."""
    if payment.settled_invoice_ids:
        return [int(x) for x in payment.settled_invoice_ids.split(",") if x.strip().isdigit()]
    return [payment.invoice_id] if payment.invoice_id else []


async def render_payment_receipt_pdf(
    session: AsyncSession, payment: Payment
) -> tuple[str, str]:
    """Build the receipt PDF for a confirmed payment; return (path, telegram_filename)."""
    reseller = await session.get(Reseller, payment.reseller_id)
    owner_name = await settings_service.get(session, "owner_name", "") or ""

    ids = _settled_ids(payment)
    invoices: list[dict] = []
    total = 0.0
    if ids:
        rows = (
            await session.execute(select(Invoice).where(Invoice.id.in_(ids)))
        ).scalars().all()
        by_id = {i.id: i for i in rows}
        for iid in ids:  # keep the stored order (primary first)
            inv = by_id.get(iid)
            if inv is not None:
                amt = float(inv.amount_toman or 0)
                invoices.append({"period": inv.period_label, "amount_toman": amt})
                total += amt

    amount = float(payment.amount_toman or 0) or total
    issued = to_local_date(payment.verified_at) if payment.verified_at else tehran_today()
    method_val = getattr(payment.method, "value", str(payment.method))
    method_label = _METHOD_FA.get(method_val, "—")
    code = payment_code(payment.id)

    safe = _safe_name(reseller.name if reseller else "reseller")
    out_path = f"data/receipts/receipt_{safe}_{payment.id}.pdf"
    await asyncio.to_thread(
        pdf_service.build_receipt_pdf, out_path,
        reseller_name=(reseller.name if reseller else "—"),
        owner_name=owner_name, receipt_no=code, issued_at=issued,
        amount_toman=amount, method_label=method_label,
        invoices=invoices, chain=(payment.chain or ""), txid=(payment.txid or ""),
    )
    return out_path, f"رسید-{code}.pdf"
