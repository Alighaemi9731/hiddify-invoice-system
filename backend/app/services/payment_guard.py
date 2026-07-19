"""Refuse to silently destroy a payment that settled somebody else's invoice.

`Payment.reseller_id` is `ON DELETE CASCADE` and holds only the FIRST invoice's owner, but «پرداخت
همهٔ بدهی» deliberately settles invoices across every reseller row a Telegram account owns — which
routinely means several panels. So deleting one reseller (or a whole panel) can destroy a payment
that is also the evidence for ANOTHER reseller's invoice. That invoice stays `paid` with no payment
behind it: no txid, no receipt image, no reviewer note. For a screenshot payment nothing at all
survives, because the durable ledger only carries a txid.

The money facts themselves are safe — `financial_records` is FK-free and never cascades. What is
lost is the audit trail and the link showing those invoices were settled by one transfer. The real
defect is therefore that it happens *silently*, so the fix is to make it loud and refusable rather
than to redesign the schema:

  * flipping the FK to RESTRICT would need a migration and turn silent loss into a raw
    IntegrityError 500 at three call sites — same protection, worse diagnosis;
  * a payment↔reseller join table would touch payment idempotency and the duplicate-pending guard
    for no gain the ledger does not already provide.

Callers check first and refuse with a 409 naming the affected rows, unless the operator explicitly
forces it — and a forced delete archives the ledger for every covered invoice before the row dies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, Payment, PaymentSettlement


@dataclass
class CrossResellerPayment:
    payment_id: int
    owner_reseller_id: int
    foreign_invoice_ids: list[int] = field(default_factory=list)
    foreign_reseller_ids: list[int] = field(default_factory=list)


async def blocking_payments(
    session: AsyncSession, reseller_ids: set[int]
) -> list[CrossResellerPayment]:
    """Payments owned by `reseller_ids` that also settle an invoice owned by a reseller OUTSIDE it.

    Deleting those resellers would take these payments with them and strand the outside invoices as
    `paid` with nothing behind them. Uses the indexed `payment_settlements` mirror — the table that
    exists precisely so this kind of question is a join rather than a scan.
    """
    if not reseller_ids:
        return []
    ids = list(reseller_ids)
    rows = (
        await session.execute(
            select(Payment.id, Payment.reseller_id, Invoice.id, Invoice.reseller_id)
            .join(PaymentSettlement, PaymentSettlement.payment_id == Payment.id)
            .join(Invoice, Invoice.id == PaymentSettlement.invoice_id)
            .where(Payment.reseller_id.in_(ids), Invoice.reseller_id.notin_(ids))
        )
    ).all()

    by_payment: dict[int, CrossResellerPayment] = {}
    for payment_id, owner_id, invoice_id, invoice_owner in rows:
        entry = by_payment.get(payment_id)
        if entry is None:
            entry = CrossResellerPayment(payment_id=payment_id, owner_reseller_id=owner_id)
            by_payment[payment_id] = entry
        entry.foreign_invoice_ids.append(invoice_id)
        if invoice_owner not in entry.foreign_reseller_ids:
            entry.foreign_reseller_ids.append(invoice_owner)
    return [by_payment[k] for k in sorted(by_payment)]


def refusal_message(blocking: list[CrossResellerPayment]) -> str:
    """Persian 409 body naming exactly what would be destroyed, so the owner can decide knowingly."""
    invoice_ids = sorted({i for b in blocking for i in b.foreign_invoice_ids})
    shown = "، ".join(str(i) for i in invoice_ids[:8])
    more = f" و {len(invoice_ids) - 8} فاکتور دیگر" if len(invoice_ids) > 8 else ""
    return (
        f"{len(blocking)} پرداخت به فاکتورهای نماینده‌های دیگری هم مربوط است "
        f"(فاکتورهای {shown}{more}). با حذف، آن فاکتورها «پرداخت‌شده» می‌مانند ولی سند پرداختشان "
        "(شناسهٔ تراکنش و تصویر رسید) از بین می‌رود. اگر مطمئن هستید، حذف را با گزینهٔ «حذف اجباری» "
        "انجام دهید."
    )


async def prune_deleted_invoice_ids(session: AsyncSession, invoice_ids: set[int]) -> int:
    """Drop `invoice_ids` from every payment's `settled_invoice_ids` before those invoices go.

    `payment_settlements` cascades on `invoice_id`, but the comma-separated column does not — so
    deleting an invoice silently left the two representations of the SAME set disagreeing, with the
    column still naming a row that no longer exists. `_settled_ids` reads that column, so the stale
    id then travelled into confirm/reject logic as though it were real.

    Called before the delete; the caller commits. Returns how many payments were rewritten.
    """
    if not invoice_ids:
        return 0
    payment_ids = list((await session.execute(
        select(PaymentSettlement.payment_id).where(
            PaymentSettlement.invoice_id.in_(list(invoice_ids)))
    )).scalars().all())
    if not payment_ids:
        return 0

    changed = 0
    for payment_id in set(payment_ids):
        payment = await session.get(Payment, payment_id)
        if payment is None or not payment.settled_invoice_ids:
            continue
        kept = [
            part for part in payment.settled_invoice_ids.split(",")
            if part.strip().isdigit() and int(part.strip()) not in invoice_ids
        ]
        new_value = ",".join(p.strip() for p in kept) or None
        if new_value != payment.settled_invoice_ids:
            payment.settled_invoice_ids = new_value
            # The primary id is display/back-compat only; repoint it at a surviving covered invoice
            # so the panel does not link to a row that is about to disappear.
            if payment.invoice_id in invoice_ids:
                payment.invoice_id = int(kept[0].strip()) if kept else None
            changed += 1
    return changed


async def archive_before_delete(
    session: AsyncSession, blocking: list[CrossResellerPayment]
) -> int:
    """Mirror every affected invoice into the durable ledger BEFORE its payment is destroyed.

    `financial_records` is FK-free, so what is written here survives the delete. Best-effort per
    invoice: a forced delete must not be blocked by one unarchivable row, but every row we can
    preserve is one the owner can still reconcile afterwards.
    """
    from app.services import financial_archive

    invoice_ids = sorted({i for b in blocking for i in b.foreign_invoice_ids})
    txid_by_invoice: dict[int, str | None] = {}
    for entry in blocking:
        payment = await session.get(Payment, entry.payment_id)
        for invoice_id in entry.foreign_invoice_ids:
            txid_by_invoice.setdefault(invoice_id, getattr(payment, "txid", None))

    archived = 0
    for invoice_id in invoice_ids:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is None:
            continue
        try:
            await financial_archive.record(session, invoice, txid=txid_by_invoice.get(invoice_id))
            archived += 1
        except Exception:  # noqa: BLE001 — never let archiving block an explicitly forced delete
            continue
    return archived
