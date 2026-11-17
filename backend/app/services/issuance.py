"""Financial issuance, independent of delivery. Shares the invoice delivery lock."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, Reseller
from app.models.enums import EnforcementState, InvoiceStatus
from app.services import financial_archive, invoice_state, periods, settings_service
from app.services.job_lock import session_lock

INVOICE_LOCK_BASE = 8_100_000_000


def collection_anchor(inv: Invoice) -> dt.date | None:
    if inv.deferred_until is not None:
        return inv.deferred_until
    stamp = getattr(inv, "collection_started_at", None) or getattr(inv, "issued_at", None) or inv.sent_at
    return periods.to_local_date(stamp) if stamp else None


async def schedule_days(session: AsyncSession) -> dict[str, int]:
    cfg = await settings_service.get_many(session, ["warning_day", "enforcement_day"])
    return {k: int(cfg[k]) if cfg.get(k) is not None else default
            for k, default in (("warning_day", 10), ("enforcement_day", 30))}


def dates(inv: Invoice, cfg: dict[str, int]) -> dict:
    anchor = collection_anchor(inv)
    if inv.status not in invoice_state.OWED or anchor is None:
        return {"due_on": None, "enforcement_on": None}
    return {
        "due_on": anchor + dt.timedelta(days=cfg["warning_day"]),
        "enforcement_on": anchor + dt.timedelta(days=cfg["enforcement_day"]),
    }


async def issue_locked(
    session: AsyncSession, inv: Invoice, *, now: dt.datetime | None = None,
) -> bool:
    """Caller owns INVOICE_LOCK_BASE + id; commit issuance BEFORE any network send."""
    if inv.status != InvoiceStatus.draft:
        return False
    reseller = await session.get(Reseller, inv.reseller_id)
    if reseller is None or reseller.is_owner or reseller.exclude_from_billing:
        raise invoice_state.InvoiceStateError("این نماینده مشمول صدور فاکتور نیست.")
    if inv.amount_toman <= 0:
        raise invoice_state.InvoiceStateError("فاکتور صفر را نمی‌توان به بدهی قطعی تبدیل کرد.")
    now = now or dt.datetime.now(dt.timezone.utc)
    inv.issued_at = now
    inv.collection_started_at = now
    from app.services import registration_policy
    financial_suspension = reseller.enforcement_state == EnforcementState.enforced and not (
        await registration_policy.only_registration_suspension(session, reseller)
    )
    inv.status = InvoiceStatus.enforced if financial_suspension else InvoiceStatus.sent
    inv.telegram_delivery_status = "pending"
    await financial_archive.record(session, inv, reseller=reseller, strict=True)
    await session.commit()
    return True


async def issue_invoice(session: AsyncSession, invoice_id: int) -> bool:
    async with session_lock(session, INVOICE_LOCK_BASE + invoice_id, wait=True):
        from app.services.invoicing import _serialize_billing
        await _serialize_billing(session)
        inv = await session.get(Invoice, invoice_id, populate_existing=True, with_for_update=True)
        if inv is None:
            raise invoice_state.InvoiceStateError("فاکتور پیدا نشد.")
        return await issue_locked(session, inv)


async def issue_period(session: AsyncSession, period: str) -> dict:
    ids = list((await session.scalars(select(Invoice.id).where(
        Invoice.period_label == period, Invoice.status == InvoiceStatus.draft,
    ).order_by(Invoice.id))).all())
    issued = 0
    skipped = []
    for invoice_id in ids:
        try:
            issued += int(await issue_invoice(session, invoice_id))
        except invoice_state.InvoiceStateError as exc:
            skipped.append({"id": invoice_id, "reason": str(exc)})
    return {"period": period, "issued": issued, "skipped": skipped}
