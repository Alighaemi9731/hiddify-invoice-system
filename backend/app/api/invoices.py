"""Invoices: generate, list (sortable), detail, PDF, manual edits & status changes."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import DeliveryLog, Invoice, InvoiceLine, Panel, Reseller
from app.models.enums import DeliveryKind, EnforcementState, InvoiceStatus
from app.schemas.invoice import (
    GenerateRequest,
    GenerateResult,
    InvoiceDefer,
    InvoiceDetail,
    InvoiceEdit,
    InvoiceLineOut,
    InvoiceOut,
)
from app.services import (
    delivery, financial_archive, invoice_pdf as invoice_pdf_service, invoicing, pricing,
)
from app.services.periods import parse_period

router = APIRouter(
    prefix="/api/invoices", tags=["invoices"], dependencies=[Depends(get_current_subject)]
)

_SORT_COLUMNS = {
    "amount": Invoice.amount_toman,
    "usage": Invoice.usage_gb,
    "date": Invoice.period_start,
    "created": Invoice.created_at,
}


def _to_out(inv: Invoice, reseller_name: str, panel_key: str) -> InvoiceOut:
    return InvoiceOut(
        id=inv.id, reseller_id=inv.reseller_id, reseller_name=reseller_name,
        panel_id=inv.panel_id, panel_key=panel_key,
        period_label=inv.period_label, period_start=inv.period_start, period_end=inv.period_end,
        usage_gb=float(inv.usage_gb), users_count=inv.users_count, price_per_gb=inv.price_per_gb,
        amount_toman=float(inv.amount_toman),
        base_amount_toman=float(inv.base_amount_toman or 0),
        min_sale_toman=int(inv.min_sale_toman or 0), floor_applied=bool(inv.floor_applied),
        usdt_rate=float(inv.usdt_rate),
        amount_usdt=float(inv.amount_usdt), status=inv.status.value,
        sent_at=inv.sent_at, paid_at=inv.paid_at,
        deferred_until=inv.deferred_until, defer_note=inv.defer_note,
        created_at=inv.created_at,
    )


@router.post("/generate", response_model=GenerateResult)
async def generate(body: GenerateRequest, session: AsyncSession = Depends(get_session)) -> GenerateResult:
    try:
        period = parse_period(body.period)
    except ValueError as e:
        raise HTTPException(400, str(e))
    summary = await invoicing.generate_invoices(
        session, period, panel_id=body.panel_id, force=body.force
    )
    return GenerateResult(**summary.__dict__)


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    period: str | None = None,
    panel_id: int | None = None,
    reseller_id: int | None = None,
    status: InvoiceStatus | None = None,
    sort: str = Query("amount"),
    order: str = Query("desc"),
    limit: int = Query(200, le=2000),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[InvoiceOut]:
    q = (
        select(Invoice, Reseller.name, Panel.key)
        .join(Reseller, Invoice.reseller_id == Reseller.id)
        .join(Panel, Invoice.panel_id == Panel.id)
    )
    if period:
        q = q.where(Invoice.period_label == period)
    if panel_id is not None:
        q = q.where(Invoice.panel_id == panel_id)
    if reseller_id is not None:
        q = q.where(Invoice.reseller_id == reseller_id)
    if status is not None:
        q = q.where(Invoice.status == status)

    col = _SORT_COLUMNS.get(sort, Invoice.amount_toman)
    q = q.order_by(col.asc() if order == "asc" else col.desc()).limit(limit).offset(offset)

    rows = (await session.execute(q)).all()
    return [_to_out(inv, name, key) for inv, name, key in rows]


@router.get("/{invoice_id}", response_model=InvoiceDetail)
async def get_invoice(invoice_id: int, session: AsyncSession = Depends(get_session)) -> InvoiceDetail:
    row = (
        await session.execute(
            select(Invoice, Reseller.name, Panel.key)
            .join(Reseller, Invoice.reseller_id == Reseller.id)
            .join(Panel, Invoice.panel_id == Panel.id)
            .where(Invoice.id == invoice_id)
        )
    ).first()
    if not row:
        raise HTTPException(404, "Invoice not found")
    inv, name, key = row
    lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)
            .order_by(InvoiceLine.usage_gb.desc())
        )
    ).scalars().all()
    out = _to_out(inv, name, key)
    return InvoiceDetail(
        **out.model_dump(),
        lines=[
            InvoiceLineOut(
                end_user_uuid=l.end_user_uuid, name=l.name, start_date=l.start_date,
                usage_gb=float(l.usage_gb), added_by_uuid=l.added_by_uuid,
                sub_reseller_name=l.sub_reseller_name or "",
            )
            for l in lines
        ],
    )


@router.get("/{invoice_id}/pdf")
async def invoice_pdf(invoice_id: int, session: AsyncSession = Depends(get_session)) -> FileResponse:
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    path, filename = await invoice_pdf_service.render_invoice_pdf(session, inv)
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.post("/{invoice_id}/mark-paid", response_model=InvoiceOut)
async def mark_paid(invoice_id: int, session: AsyncSession = Depends(get_session)) -> InvoiceOut:
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv.status = InvoiceStatus.paid
    inv.paid_at = dt.datetime.now(dt.timezone.utc)
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.post("/{invoice_id}/unmark-paid", response_model=InvoiceOut)
async def unmark_paid(invoice_id: int, session: AsyncSession = Depends(get_session)) -> InvoiceOut:
    """Undo an accidental 'paid' — revert to its delivered/draft state."""
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv.status = InvoiceStatus.sent if inv.sent_at else InvoiceStatus.draft
    inv.paid_at = None
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.patch("/{invoice_id}", response_model=InvoiceOut)
async def edit_invoice(
    invoice_id: int, body: InvoiceEdit, session: AsyncSession = Depends(get_session)
) -> InvoiceOut:
    """Manually correct an invoice's usage/price/amount and recompute the USDT total."""
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if body.usage_gb is not None:
        inv.usage_gb = body.usage_gb
    if body.price_per_gb is not None:
        inv.price_per_gb = body.price_per_gb
    if body.amount_toman is not None:
        inv.amount_toman = body.amount_toman
    else:
        inv.amount_toman = round(float(inv.usage_gb) * int(inv.price_per_gb))
    # Manual edit overrides the floor; keep base in sync for the PDF.
    inv.base_amount_toman = inv.amount_toman
    inv.floor_applied = False
    rate = int(inv.usdt_rate) or await pricing.get_rate(session)
    inv.usdt_rate = rate
    inv.amount_usdt = float(pricing.toman_to_usdt(inv.amount_toman, rate))
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.post("/{invoice_id}/defer", response_model=InvoiceOut)
async def defer_invoice(
    invoice_id: int, body: InvoiceDefer, session: AsyncSession = Depends(get_session)
) -> InvoiceOut:
    """Set (or clear) a payment deadline. Setting a future deadline RESTARTS the whole
    dunning cycle from that date: prior reminders are cleared so they re-fire, an
    overdue invoice goes back to 'sent', and an already-suspended reseller is restored
    for the new grace window. Other invoices and panel data are unaffected."""
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)

    inv.deferred_until = body.deferred_until
    inv.defer_note = body.defer_note

    if body.deferred_until and body.deferred_until > dt.date.today():
        # Wipe prior reminder/warning marks so the cycle starts fresh from the deadline.
        await session.execute(
            delete(DeliveryLog).where(
                DeliveryLog.invoice_id == inv.id,
                DeliveryLog.kind.in_(
                    [DeliveryKind.reminder1, DeliveryKind.reminder2, DeliveryKind.warning]
                ),
            )
        )
        if inv.status == InvoiceStatus.overdue:
            inv.status = InvoiceStatus.sent
        # If the reseller was suspended, give their service back during the new window.
        if inv.status == InvoiceStatus.enforced or (
            reseller and reseller.enforcement_state == EnforcementState.enforced
        ):
            try:
                from app.services import enforcement

                await enforcement.restore_reseller(session, reseller)
                inv.status = InvoiceStatus.sent
            except Exception:  # noqa: BLE001 — API creds may be absent; deadline still set
                pass

    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.post("/{invoice_id}/send")
async def send_invoice(invoice_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    dl = await delivery.send_invoice(session, invoice_id)
    return {"invoice_id": invoice_id, "delivery_status": dl.status.value, "error": dl.error}


@router.post("/send-period")
async def send_period(period: str, session: AsyncSession = Depends(get_session)) -> dict:
    return await delivery.send_period(session, parse_period(period).label)


@router.post("/{invoice_id}/cancel", response_model=InvoiceOut)
async def cancel(invoice_id: int, session: AsyncSession = Depends(get_session)) -> InvoiceOut:
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv.status = InvoiceStatus.canceled
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)
