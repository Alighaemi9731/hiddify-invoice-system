"""Invoices: generate, list (sortable), detail, PDF, manual edits & status changes."""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.codes import decode_invoice_code, invoice_code
from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import (
    BotUser,
    DeliveryLog,
    FinancialRecord,
    Invoice,
    InvoiceLine,
    Panel,
    Reseller,
)
from app.models.enums import DeliveryKind, EnforcementState, InvoiceStatus
from app.schemas.invoice import (
    BulkDefer,
    BulkDeferResult,
    GenerateRequest,
    GenerateResult,
    InvoiceDefer,
    InvoiceDetail,
    InvoiceEdit,
    InvoiceLineOut,
    InvoiceOut,
)
from app.services import (
    delivery,
    financial_archive,
    invoice_state,
    invoicing,
    pricing,
)
from app.services import (
    invoice_pdf as invoice_pdf_service,
)
from app.services.periods import parse_period
from app.services.periods import today as tehran_today

log = logging.getLogger("invoices")

router = APIRouter(
    prefix="/api/invoices", tags=["invoices"], dependencies=[Depends(get_current_subject)]
)

_SORT_COLUMNS = {
    "amount": Invoice.amount_toman,
    "usage": Invoice.usage_gb,
    "date": Invoice.period_start,
    "created": Invoice.created_at,
    # UI column ids (the SPA sends its SortTh ids directly so header clicks sort the
    # WHOLE dataset server-side, not just the fetched page).
    "amount_toman": Invoice.amount_toman,
    "usage_gb": Invoice.usage_gb,
    "reseller_name": Reseller.name,
    "panel_key": Panel.key,
    "status": Invoice.status,
    "period_label": Invoice.period_start,
    "created_at": Invoice.created_at,
}


def _ascii_digits(s: str) -> str:
    """Normalize Persian/Arabic digits to ASCII so a hand-typed «۱۲۳» matches."""
    out = []
    for ch in s:
        if "\u06f0" <= ch <= "\u06f9":       # Persian ۰-۹
            out.append(chr(ord(ch) - 0x06F0 + ord("0")))
        elif "\u0660" <= ch <= "\u0669":     # Arabic ٠-٩
            out.append(chr(ord(ch) - 0x0660 + ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def _to_out(
    inv: Invoice, reseller_name: str, panel_key: str,
    reseller_chat_id: int | None = None, reseller_username: str | None = None,
) -> InvoiceOut:
    return InvoiceOut(
        id=inv.id, number=invoice_code(inv.id),
        reseller_id=inv.reseller_id, reseller_name=reseller_name,
        reseller_chat_id=reseller_chat_id, reseller_username=reseller_username,
        panel_id=inv.panel_id, panel_key=panel_key,
        period_label=inv.period_label, period_start=inv.period_start, period_end=inv.period_end,
        usage_gb=float(inv.usage_gb), users_count=inv.users_count, price_per_gb=inv.price_per_gb,
        amount_toman=float(inv.amount_toman),
        base_amount_toman=float(inv.base_amount_toman or 0),
        min_sale_toman=int(inv.min_sale_toman or 0), floor_applied=bool(inv.floor_applied),
        status=inv.status.value,
        sent_at=inv.sent_at, paid_at=inv.paid_at,
        deferred_until=inv.deferred_until, defer_note=inv.defer_note,
        created_at=inv.created_at,
    )


async def _invoice_context(
    session: AsyncSession, inv: Invoice
) -> tuple[Reseller, Panel]:
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    if reseller is None or panel is None:
        raise HTTPException(409, "Invoice references a missing reseller or panel")
    return reseller, panel


@router.post("/generate", response_model=GenerateResult)
async def generate(body: GenerateRequest, session: AsyncSession = Depends(get_session)) -> GenerateResult:
    try:
        period = parse_period(body.period)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    summary = await invoicing.generate_invoices(
        session, period, panel_id=body.panel_id, force=body.force
    )
    return GenerateResult(**summary.__dict__)


@router.post("/discard-drafts")
async def discard_drafts(
    period: str | None = None,
    panel_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete DRAFT invoices (never sent) — for a period, a panel, or all. Sent/paid/
    overdue/enforced invoices are never touched. Use to throw away a draft run you
    don't want to keep or send."""
    # Serialize with generation/recompute — otherwise a discard interleaving a running
    # «صدور فاکتورها» could delete a draft between that run's read and its update.
    from app.services.invoicing import _serialize_billing
    await _serialize_billing(session)
    q = select(Invoice.id).where(Invoice.status == InvoiceStatus.draft)
    if period:
        q = q.where(Invoice.period_label == period)
    if panel_id is not None:
        q = q.where(Invoice.panel_id == panel_id)
    ids = (await session.execute(q)).scalars().all()
    if ids:
        await session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(ids)))
        await session.execute(delete(FinancialRecord).where(FinancialRecord.invoice_id.in_(ids)))
        await session.execute(delete(Invoice).where(Invoice.id.in_(ids)))
        await session.commit()
    return {"discarded": len(ids), "period": period}


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    response: Response,
    period: str | None = None,
    panel_id: int | None = None,
    reseller_id: int | None = None,
    status: InvoiceStatus | None = None,
    q: str | None = Query(None, description="search: invoice number or reseller name"),
    sort: str = Query("amount"),
    order: str = Query("desc"),
    limit: int = Query(200, le=2000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[InvoiceOut]:
    """Server-side pagination: the full filtered count is returned in `X-Total-Count`
    (additive header — the response body/contract is unchanged) so the SPA pages the
    dataset on the server instead of downloading it whole."""
    filters: list[ColumnElement[bool]] = []
    if period:
        filters.append(Invoice.period_label == period)
    if panel_id is not None:
        filters.append(Invoice.panel_id == panel_id)
    if reseller_id is not None:
        filters.append(Invoice.reseller_id == reseller_id)
    if status is not None:
        filters.append(Invoice.status == status)
    if isinstance(q, str) and q.strip():
        needle = _ascii_digits(q.strip())
        ors: list[ColumnElement[bool]] = [Reseller.name.ilike(f"%{q.strip()}%")]
        if needle.isdigit():
            decoded = decode_invoice_code(needle)  # the public 8-digit «شماره فاکتور»
            if decoded is not None:
                ors.append(Invoice.id == decoded)
        filters.append(or_(*ors))

    total, total_amount = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Invoice.amount_toman), 0))
            .select_from(Invoice)
            .join(Reseller, Invoice.reseller_id == Reseller.id)
            .join(Panel, Invoice.panel_id == Panel.id)
            .where(*filters)
        )
    ).one()
    response.headers["X-Total-Count"] = str(total)
    # Whole-filtered-set money sum for the list header (the page alone can't compute it).
    response.headers["X-Total-Amount-Toman"] = str(int(total_amount))

    query = (
        select(Invoice, Reseller.name, Panel.key, Reseller.bot_chat_id, BotUser.username)
        .join(Reseller, Invoice.reseller_id == Reseller.id)
        .join(Panel, Invoice.panel_id == Panel.id)
        .outerjoin(BotUser, BotUser.telegram_id == Reseller.bot_chat_id)
        .where(*filters)
    )
    col = _SORT_COLUMNS.get(sort, Invoice.amount_toman)
    # Deterministic tiebreaker: equal sort values must not shuffle rows across pages.
    query = (
        query.order_by(col.asc() if order == "asc" else col.desc(), Invoice.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(query)).all()
    return [_to_out(inv, name, key, chat_id, username) for inv, name, key, chat_id, username in rows]


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
                end_user_uuid=line.end_user_uuid, name=line.name, start_date=line.start_date,
                usage_gb=float(line.usage_gb), added_by_uuid=line.added_by_uuid,
                sub_reseller_name=line.sub_reseller_name or "",
            )
            for line in lines
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
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_mark_paid(inv.status)
    inv.status = InvoiceStatus.paid
    inv.paid_at = dt.datetime.now(dt.timezone.utc)
    reseller, panel = await _invoice_context(session, inv)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    # The manual settlement is recorded as a real (confirmed) Payment row so the payments list
    # shows it AND a later reject of an unrelated pending payment covering this invoice can't
    # un-pay it (`_settled_by_other_confirmed` needs a confirmed row to see).
    from app.services.payments import (
        _maybe_restore,
        _payment_received_text,
        record_manual_payment,
    )
    await record_manual_payment(session, inv)
    # Manually marking an invoice paid must restore a suspended reseller, exactly like
    # confirming a payment does — but ONLY when no other debt remains (see _maybe_restore),
    # otherwise recording one cash payment would un-suspend a reseller who still owes.
    await _maybe_restore(session, reseller, exclude_invoice_id=inv.id)
    await session.commit()
    # Tell the reseller their invoice was confirmed paid — same acknowledgement as confirming a
    # submitted payment, so a manually-recorded «ثبت پرداخت» isn't silent. Sent after commit
    # (a delivery failure must not roll back the paid status); no-op if they aren't on the bot.
    if reseller is not None and reseller.bot_chat_id:
        from app.services import notifier
        await notifier.send_to_reseller(
            session, reseller, await _payment_received_text(session, inv.period_label),
            kind=DeliveryKind.payment_ack, invoice_id=inv.id,
        )
    return _to_out(inv, reseller.name, panel.key)


@router.post("/{invoice_id}/unmark-paid", response_model=InvoiceOut)
async def unmark_paid(invoice_id: int, session: AsyncSession = Depends(get_session)) -> InvoiceOut:
    """Undo an accidental 'paid' — revert to its delivered/draft state."""
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_unmark_paid(inv.status)  # pre-check on the unlocked read
    # LOCK ORDER (deadlock-safe): unmark is the only invoice route that also mutates Payment rows,
    # so — like the payment paths (confirm/reject) — it must lock Payment BEFORE Invoice. Retire the
    # covering confirmed `manual` rows (now FOR UPDATE) first, THEN lock + re-validate the invoice.
    # Locking the invoice first and retiring after would be Invoice→Payment, the reverse order, and
    # could deadlock a concurrent reject_payment (Payment→Invoice).
    from app.services.payments import retire_manual_payments
    await retire_manual_payments(session, inv)
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_unmark_paid(inv.status)  # re-validate under the row lock
    inv.status = InvoiceStatus.sent if inv.sent_at else InvoiceStatus.draft
    inv.paid_at = None
    # An un-paid invoice gets a fresh dunning window (reminders restart) instead of jumping
    # straight back to overdue/enforcement on the next run; the txid is cleared from the ledger.
    if inv.status == InvoiceStatus.sent:
        from app.services import dunning
        await dunning.reset_cycle(session, inv, restamp_sent_at=True)
    reseller, panel = await _invoice_context(session, inv)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.patch("/{invoice_id}", response_model=InvoiceOut)
async def edit_invoice(
    invoice_id: int, body: InvoiceEdit, session: AsyncSession = Depends(get_session)
) -> InvoiceOut:
    """Manually correct an invoice's usage/price/amount and recompute the USDT total."""
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_edit(inv.status)
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
    reseller, panel = await _invoice_context(session, inv)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


async def _apply_defer(
    session: AsyncSession, inv: Invoice, reseller, panel,  # noqa: ANN001
    deferred_until, defer_note,  # noqa: ANN001
) -> None:
    """Apply a payment-deadline change to ONE invoice (no commit) — the shared core of the
    single and bulk defer endpoints so they can't diverge. The caller has already validated
    the transition with `invoice_state.ensure_can_defer` and looked up reseller/panel."""
    inv.deferred_until = deferred_until
    inv.defer_note = defer_note

    # `>= today`, not `> today`: a deadline set to TODAY must reset the cycle exactly like any other
    # deadline. Under `>` it only moved the dunning ANCHOR (dunning counts from `deferred_until`)
    # while leaving the old reminder/warning marks in place — so the invoice went silent (each
    # reminder kind is sent at most once per invoice) and the reseller was never chased again, while
    # a suspended one stayed suspended. «مهلت = امروز» now means «as if issued today, payable today»:
    # payability still uses a strict `> today`, so today itself stays payable.
    if deferred_until and deferred_until >= tehran_today():
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
        # If the reseller was suspended, give their service back for the new window —
        # but ONLY if they have no OTHER still-due (non-deferred) invoice keeping them
        # owing; otherwise the grace on this one would wrongly un-suspend a debtor.
        was_enforced = inv.status == InvoiceStatus.enforced or (
            reseller and reseller.enforcement_state == EnforcementState.enforced
        )
        if was_enforced and reseller:
            today = tehran_today()
            others = (
                await session.execute(
                    select(Invoice).where(
                        Invoice.reseller_id == reseller.id,
                        Invoice.id != inv.id,
                        Invoice.status.in_(
                            (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
                        ),
                    )
                )
            ).scalars().all()
            still_owes = any(not (o.deferred_until and o.deferred_until > today) for o in others)
            if not still_owes:
                try:
                    from app.services import enforcement

                    await enforcement.queue_restore(
                        session,
                        reseller,
                        require_no_due=True,
                        reason="defer",
                    )
                    inv.status = InvoiceStatus.sent
                except Exception:  # noqa: BLE001 — API creds may be absent; deadline still set
                    pass

    await financial_archive.record(session, inv, panel=panel, reseller=reseller)


@router.post("/{invoice_id}/defer", response_model=InvoiceOut)
async def defer_invoice(
    invoice_id: int, body: InvoiceDefer, session: AsyncSession = Depends(get_session)
) -> InvoiceOut:
    """Set (or clear) a payment deadline. Setting a deadline of TODAY OR LATER restarts the whole
    dunning cycle from that date: prior reminders are cleared so they re-fire, an overdue invoice
    goes back to 'sent', and an already-suspended reseller is restored for the new grace window.
    A deadline of today leaves the invoice payable immediately (payability blocks only a FUTURE
    deadline); a past date only re-anchors the day count. Other invoices and panel data are
    unaffected."""
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_defer(inv.status)
    reseller, panel = await _invoice_context(session, inv)
    await _apply_defer(session, inv, reseller, panel, body.deferred_until, body.defer_note)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.post("/bulk-defer", response_model=BulkDeferResult)
async def bulk_defer(
    body: BulkDefer, session: AsyncSession = Depends(get_session)
) -> BulkDeferResult:
    """Set/extend the payment deadline for SEVERAL invoices at once (tick-select in the UI).
    Each invoice is validated with the SAME `ensure_can_defer` guard the single endpoint uses;
    a non-owed (draft/paid/canceled) or missing invoice is reported in `skipped`, never
    silently applied. Money is never moved. One commit for the whole set."""
    done = 0
    skipped: list[dict] = []
    # Lock each invoice FOR UPDATE, in ASCENDING id order (two concurrent bulk-defers over
    # overlapping sets would otherwise deadlock on opposite orders); reported order stays sorted.
    for iid in sorted(dict.fromkeys(body.ids)):  # dedupe + ascending lock order
        inv = await session.get(Invoice, iid, with_for_update=True, populate_existing=True)
        if inv is None:
            skipped.append({"id": iid, "reason": "یافت نشد"})
            continue
        try:
            invoice_state.ensure_can_defer(inv.status)
        except invoice_state.InvoiceStateError:
            skipped.append({"id": iid, "reason": "قابلِ تعیین مهلت نیست (پیش‌نویس/پرداخت‌شده/لغوشده)"})
            continue
        # A dangling reseller/panel reference must not abort the whole batch — the
        # single-invoice endpoints intentionally 409 via _invoice_context; here it's a skip.
        reseller = await session.get(Reseller, inv.reseller_id)
        panel = await session.get(Panel, inv.panel_id)
        if reseller is None or panel is None:
            skipped.append({"id": iid, "reason": "نماینده یا پنلِ فاکتور حذف شده است"})
            continue
        await _apply_defer(session, inv, reseller, panel, body.deferred_until, body.defer_note)
        done += 1
    await session.commit()
    return BulkDeferResult(done=done, skipped=skipped)


@router.post("/{invoice_id}/recompute")
async def recompute_invoice(
    invoice_id: int, sync: bool = True, session: AsyncSession = Depends(get_session)
) -> dict:
    """Refresh this invoice's numbers from the panel's CURRENT data (syncs the panel
    first by default), keeping its status. For correcting an already-sent invoice.
    Returns the updated invoice plus `synced` (whether the panel sync succeeded)."""
    inv = await session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    try:
        result = await invoicing.recompute_invoice(session, inv, sync_first=sync)
    except ValueError as exc:
        # Distinguish the user's state error from internal lookup failures — mapping every
        # ValueError to the paid-invoice message hid real "panel/reseller not found" errors.
        if "paid invoice" in str(exc):
            raise HTTPException(
                400,
                "فاکتور پرداخت‌شده را نمی‌توان بازمحاسبه کرد؛ ابتدا «لغو پرداخت» را بزنید.",
            ) from None
        raise HTTPException(409, f"بازمحاسبه ممکن نیست: {exc}") from None
    inv = await session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found after recompute")
    reseller, panel = await _invoice_context(session, inv)
    return {
        **_to_out(inv, reseller.name, panel.key).model_dump(),
        "synced": bool(result.get("synced")),
    }


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
    """Void an invoice: it stops being debt but stays in «تاریخچهٔ مالی» as `canceled` (unlike
    «بازگردانی به پیش‌نویس», which erases it from the ledger). A paid invoice must be un-paid first.

    Canceling REMOVES debt, so it must be able to lift a suspension exactly like a payment does —
    otherwise voiding a debtor's only invoice would leave them blocked forever with nothing left to
    pay. Restore is queued only when no other still-due (non-deferred) invoice remains."""
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    invoice_state.ensure_can_cancel(inv.status)
    inv.status = InvoiceStatus.canceled
    reseller, panel = await _invoice_context(session, inv)
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    if reseller.enforcement_state in (EnforcementState.enforced, EnforcementState.frozen):
        today = tehran_today()
        others = (
            await session.execute(
                select(Invoice).where(
                    Invoice.reseller_id == reseller.id,
                    Invoice.id != inv.id,
                    Invoice.status.in_(
                        (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
                    ),
                )
            )
        ).scalars().all()
        still_owes = any(not (o.deferred_until and o.deferred_until > today) for o in others)
        if not still_owes:
            try:
                from app.services import enforcement

                await enforcement.queue_restore(
                    session, reseller, require_no_due=True, reason="cancel"
                )
            except Exception:  # noqa: BLE001 — panel creds may be absent; the cancel still stands
                log.warning("cancel: queue_restore failed for reseller %s", reseller.id, exc_info=True)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)


@router.post("/{invoice_id}/revert-to-draft", response_model=InvoiceOut)
async def revert_to_draft(
    invoice_id: int, session: AsyncSession = Depends(get_session)
) -> InvoiceOut:
    """Send a delivered/overdue/canceled invoice BACK to draft — for re-testing the flow or
    correcting a mistaken send. A PAID invoice is protected (un-mark it as paid first). Clears
    sent_at + any payment deadline and removes it from the durable ledger (drafts aren't kept
    there); «صدور فاکتورهای دوره» will then recompute it like any other draft."""
    inv = await session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == InvoiceStatus.paid:
        raise HTTPException(
            400, "این فاکتور پرداخت‌شده است؛ ابتدا «لغو پرداخت» را بزنید، سپس به پیش‌نویس برگردانید."
        )
    # Clear the reminder/warning delivery marks BEFORE dropping sent_at, so a later
    # revert→regenerate→resend gets a FRESH dunning cycle. Without this, the old reminder rows
    # survive: _done_kinds still counts reminder1/2/warning as sent, so the re-sent invoice
    # skips every reminder yet enforcement fires on day D+5 anyway — a suspension with no fresh
    # warning. (reset_cycle deletes the reminder DeliveryLog rows.)
    from app.services import dunning
    await dunning.reset_cycle(session, inv)
    inv.status = InvoiceStatus.draft
    inv.sent_at = None
    inv.deferred_until = None
    inv.defer_note = None
    reseller, panel = await _invoice_context(session, inv)
    # Draft status → financial_archive removes the ledger row.
    await financial_archive.record(session, inv, panel=panel, reseller=reseller)
    await session.commit()
    return _to_out(inv, reseller.name, panel.key)
