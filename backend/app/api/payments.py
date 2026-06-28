"""Payments: list, detail, (re)verify on-chain, manual confirm/reject/record."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import payment_code
from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import BotUser, Invoice, Payment, Reseller
from app.models.enums import PaymentStatus
from app.schemas.payment import (
    InvoiceBrief,
    PaymentActionResult,
    PaymentOut,
)
from app.services import payments as payments_service

router = APIRouter(
    prefix="/api/payments", tags=["payments"], dependencies=[Depends(get_current_subject)]
)


def _equiv_str(
    method: str, toman: float | None, usdt: float | None, ton_rate: int, avax_rate: int
) -> str:
    """The invoice's crypto equivalent in the PAID currency — ONLY for crypto methods: TON for a
    TON payment, AVAX for an AVAX payment, USDT for a USDT payment. A card/screenshot/manual
    payment is in Toman, so no crypto equivalent is shown (the tooltip then has only the Toman)."""
    if not toman:
        return ""
    if method == "ton_txid":
        return f"{float(toman) / ton_rate:,.2f} GRAM" if ton_rate else ""
    if method == "avax_txid":
        return f"{float(toman) / avax_rate:,.4f} AVAX" if avax_rate else ""
    if method == "usdt_txid":
        return f"{float(usdt or 0):,.2f} USDT"
    return ""


def _briefs_for(
    p: Payment, inv_map: dict[int, Invoice], ton_rate: int, avax_rate: int
) -> list[InvoiceBrief]:
    """The covered invoices of a payment, in stored order (the first is the primary)."""
    briefs: list[InvoiceBrief] = []
    for iid in payments_service._settled_ids(p):
        inv = inv_map.get(iid)
        if inv is None:
            continue
        briefs.append(InvoiceBrief(
            id=inv.id, period=inv.period_label,
            amount_toman=float(inv.amount_toman or 0),
            equiv=_equiv_str(p.method.value, float(inv.amount_toman or 0),
                             float(inv.amount_usdt or 0), ton_rate, avax_rate),
        ))
    return briefs


def _to_out(
    p: Payment, reseller_name: str | None,
    *, briefs: list[InvoiceBrief],
    reseller_chat_id: int | None = None, reseller_username: str | None = None,
) -> PaymentOut:
    primary = briefs[0] if briefs else None
    return PaymentOut(
        id=p.id, number=payment_code(p.id),
        reseller_id=p.reseller_id, reseller_name=reseller_name, invoice_id=p.invoice_id,
        reseller_chat_id=reseller_chat_id, reseller_username=reseller_username,
        invoice_period=primary.period if primary else None,
        invoice_amount_toman=primary.amount_toman if primary else 0,
        invoice_equiv=primary.equiv if primary else "",
        invoices=briefs, invoice_ids=[b.id for b in briefs],
        invoice_count=len(briefs) or 1,
        total_amount_toman=float(sum(b.amount_toman for b in briefs)),
        method=p.method.value, status=p.status.value, chain=p.chain, txid=p.txid,
        from_address=p.from_address, to_address=p.to_address, amount_usdt=float(p.amount_usdt),
        confirmations=p.confirmations, verified_at=p.verified_at, created_at=p.created_at, note=p.note,
        has_proof=bool(p.proof_path),
    )


async def _load_invoice_map(
    session: AsyncSession, payments_list: list[Payment]
) -> dict[int, Invoice]:
    """Batch-load every invoice referenced by any payment's set, in one query."""
    ids: set[int] = set()
    for p in payments_list:
        ids.update(payments_service._settled_ids(p))
    if not ids:
        return {}
    rows = (await session.execute(select(Invoice).where(Invoice.id.in_(ids)))).scalars().all()
    return {inv.id: inv for inv in rows}


@router.get("", response_model=list[PaymentOut])
async def list_payments(
    status: PaymentStatus | None = None,
    reseller_id: int | None = None,
    limit: int = Query(200, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[PaymentOut]:
    q = (
        select(Payment, Reseller.name, Reseller.bot_chat_id, BotUser.username)
        .outerjoin(Reseller, Payment.reseller_id == Reseller.id)
        .outerjoin(BotUser, BotUser.telegram_id == Reseller.bot_chat_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    if status is not None:
        q = q.where(Payment.status == status)
    if reseller_id is not None:
        q = q.where(Payment.reseller_id == reseller_id)
    rows = (await session.execute(q)).all()
    from app.services import rates

    ton_rate = await rates.get_ton_toman(session)
    avax_rate = await rates.get_avax_toman(session)
    inv_map = await _load_invoice_map(session, [p for p, *_ in rows])
    return [
        _to_out(p, name, briefs=_briefs_for(p, inv_map, ton_rate, avax_rate),
                reseller_chat_id=chat_id, reseller_username=username)
        for p, name, chat_id, username in rows
    ]


@router.get("/{payment_id}/deposit-check")
async def deposit_check(payment_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Read the actual on-chain deposit for this payment's txid (TON via toncenter, USDT/BEP-20 via
    a public BSC RPC node) and compare it to the invoice amount — a decision aid for MANUAL
    confirmation. Best-effort; never auto-confirms. Returns {'available': False, 'kind': 'none'}
    when there's no txid or the chain can't be read. `kind` ∈ {'ton','usdt','none'}."""
    from app.services import payments as payments_service

    p = await session.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    return await payments_service.deposit_check(session, p)


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentOut:
    p = await session.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    reseller = await session.get(Reseller, p.reseller_id)
    username = None
    if reseller and reseller.bot_chat_id:
        bu = (await session.execute(
            select(BotUser).where(BotUser.telegram_id == reseller.bot_chat_id)
        )).scalars().first()
        username = bu.username if bu else None
    from app.services import rates

    ton_rate = await rates.get_ton_toman(session)
    avax_rate = await rates.get_avax_toman(session)
    inv_map = await _load_invoice_map(session, [p])
    return _to_out(p, reseller.name if reseller else None,
                   briefs=_briefs_for(p, inv_map, ton_rate, avax_rate),
                   reseller_chat_id=reseller.bot_chat_id if reseller else None,
                   reseller_username=username)


@router.post("/{payment_id}/verify", response_model=PaymentActionResult)
async def verify(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    if not await session.get(Payment, payment_id):
        raise HTTPException(404, "Payment not found")
    r = await payments_service.verify_payment(session, payment_id, notify_reseller=True)
    return PaymentActionResult(status=r.status, paid=r.paid, message=r.detail or r.message_fa)


@router.post("/{payment_id}/confirm", response_model=PaymentActionResult)
async def confirm(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    """Confirm the payment for ALL invoices it covers (a payment may settle several at once)."""
    if not await session.get(Payment, payment_id):
        raise HTTPException(404, "Payment not found")
    r = await payments_service.confirm_manually(session, payment_id)
    return PaymentActionResult(status=r.status, paid=r.paid, message=r.message_fa)


@router.post("/{payment_id}/reject", response_model=PaymentActionResult)
async def reject(payment_id: int, session: AsyncSession = Depends(get_session)) -> PaymentActionResult:
    if not await session.get(Payment, payment_id):
        raise HTTPException(404, "Payment not found")
    r = await payments_service.reject_payment(session, payment_id)
    return PaymentActionResult(status=r.status, paid=r.paid, message=r.message_fa)


@router.delete("/{payment_id}", response_model=PaymentActionResult)
async def delete_payment(
    payment_id: int, session: AsyncSession = Depends(get_session)
) -> PaymentActionResult:
    """Delete a payment row (e.g. test-data cleanup). A confirmed payment's invoice is reverted
    to owed first so accounting stays consistent."""
    ok = await payments_service.delete_payment(session, payment_id)
    if not ok:
        raise HTTPException(404, "Payment not found")
    return PaymentActionResult(status="deleted", paid=False, message="پرداخت حذف شد.")


@router.get("/{payment_id}/proof")
async def proof(payment_id: int, session: AsyncSession = Depends(get_session)) -> FileResponse:
    """Serve the deposit screenshot a reseller sent (method=screenshot)."""
    p = await session.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if not p.proof_path or not os.path.exists(p.proof_path):
        raise HTTPException(404, "No proof image for this payment")
    # Defense-in-depth: only ever serve a file that resolves INSIDE the proofs directory, so a
    # tampered proof_path (e.g. "/etc/passwd") can't turn this into arbitrary file disclosure.
    proof_dir = os.path.realpath("data/payment_proofs")
    if not os.path.realpath(p.proof_path).startswith(proof_dir + os.sep):
        raise HTTPException(404, "No proof image for this payment")
    return FileResponse(p.proof_path, media_type="image/jpeg",
                        filename=f"proof_{payment_id}.jpg")


