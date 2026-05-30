"""Payments: list, detail, (re)verify on-chain, manual confirm/reject/record."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import Invoice, Payment, Reseller
from app.models.enums import PaymentMethod, PaymentStatus
from app.schemas.payment import ManualPaymentCreate, PaymentActionResult, PaymentOut
from app.services import payments as payments_service

router = APIRouter(
    prefix="/api/payments", tags=["payments"], dependencies=[Depends(get_current_subject)]
)


def _to_out(p: Payment, reseller_name: str | None) -> PaymentOut:
    return PaymentOut(
        id=p.id, reseller_id=p.reseller_id, reseller_name=reseller_name, invoice_id=p.invoice_id,
        method=p.method.value, status=p.status.value, chain=p.chain, txid=p.txid,
        from_address=p.from_address, to_address=p.to_address, amount_usdt=float(p.amount_usdt),
        confirmations=p.confirmations, verified_at=p.verified_at, created_at=p.created_at, note=p.note,
    )


@router.get("", response_model=list[PaymentOut])
async def list_payments(
    status: PaymentStatus | None = None,
    reseller_id: int | None = None,
    limit: int = Query(200, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[PaymentOut]:
    q = (
        select(Payment, Reseller.name)
        .outerjoin(Reseller, Payment.reseller_id == Reseller.id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    if status is not None:
        q = q.where(Payment.status == status)
    if reseller_id is not None:
        q = q.where(Payment.reseller_id == reseller_id)
    rows = (await session.execute(q)).all()
    return [_to_out(p, name) for p, name in rows]


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentOut:
    p = await session.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    reseller = await session.get(Reseller, p.reseller_id)
    return _to_out(p, reseller.name if reseller else None)


@router.post("/{payment_id}/verify", response_model=PaymentActionResult)
async def verify(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    if not await session.get(Payment, payment_id):
        raise HTTPException(404, "Payment not found")
    r = await payments_service.verify_payment(session, payment_id)
    return PaymentActionResult(status=r.status, paid=r.paid, message=r.detail or r.message_fa)


@router.post("/{payment_id}/confirm", response_model=PaymentActionResult)
async def confirm(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    if not await session.get(Payment, payment_id):
        raise HTTPException(404, "Payment not found")
    r = await payments_service.confirm_manually(session, payment_id)
    return PaymentActionResult(status=r.status, paid=r.paid, message=r.message_fa)


@router.post("/{payment_id}/reject", response_model=PaymentActionResult)
async def reject(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    p = await session.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    p.status = PaymentStatus.rejected
    await session.commit()
    return PaymentActionResult(status="rejected", paid=False, message="Rejected")


@router.post("", response_model=PaymentOut, status_code=201)
async def record_manual(
    body: ManualPaymentCreate, session: AsyncSession = Depends(get_session)
) -> PaymentOut:
    """Owner records an off-chain / manual payment against an invoice and applies it."""
    invoice = await session.get(Invoice, body.invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    payment = Payment(
        reseller_id=invoice.reseller_id, invoice_id=invoice.id, method=PaymentMethod.manual,
        status=PaymentStatus.pending, amount_usdt=body.amount_usdt or float(invoice.amount_usdt),
        note=body.note,
    )
    session.add(payment)
    await session.commit()
    await payments_service.confirm_manually(session, payment.id)
    await session.refresh(payment)
    reseller = await session.get(Reseller, payment.reseller_id)
    return _to_out(payment, reseller.name if reseller else None)
