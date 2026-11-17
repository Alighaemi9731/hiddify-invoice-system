"""Resumable, once-per-install recovery of invoices stranded by Telegram delivery."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import invoice_code
from app.models import DeliveryLog, Invoice, Panel, Reseller
from app.models.enums import DeliveryKind, DeliveryStatus, InvoiceStatus
from app.services import issuance, periods, settings_service
from app.services.invoice_engine import select_billable_roots
from app.services.job_lock import session_lock

REPORT_KEY = "billing_issuance_recovery_v1"
_LOCK_KEY = 734_137_052


async def recover(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    """No network writes; preserve stored amounts and start a FULL new collection cycle."""
    recovery_now = now or dt.datetime.now(dt.timezone.utc)
    async with session_lock(session, _LOCK_KEY, wait=True):
        report = await settings_service.get(session, REPORT_KEY, None)
        if isinstance(report, dict) and report.get("completed_at"):
            return report
        if not isinstance(report, dict):
            report = {"started_at": recovery_now.isoformat(), "recovered_ids": [], "review": []}
            await settings_service.set_value(session, REPORT_KEY, report)
        started = dt.datetime.fromisoformat(report["started_at"])
        month_start = periods.to_local_date(started).replace(day=1)
        cfg = await issuance.schedule_days(session)
        panel_rows = list((await session.scalars(select(Panel))).all())
        panels = {p.id: p for p in panel_rows}
        groups: dict[int, list[Reseller]] = defaultdict(list)
        for r in (await session.scalars(select(Reseller))).all():
            groups[r.panel_id].append(r)
        roots = {r.id for rows in groups.values() for r in select_billable_roots(rows)}
        # Every EXISTING unregistered root gets the full current cycle, not 72h and not
        # its historical creation date. Never shorten a deadline across an interrupted run.
        for rows in groups.values():
            for r in rows:
                if r.id in roots and r.bot_chat_id is None and r.registration_deadline_at is None:
                    r.registration_deadline_at = recovery_now + dt.timedelta(days=cfg["enforcement_day"])
        from app.services import invoice_state
        for existing_invoice in (await session.scalars(select(Invoice).where(
            Invoice.status.in_(invoice_state.OWED), Invoice.issued_at.is_(None),
        ))).all():
            existing_invoice.issued_at = existing_invoice.sent_at or recovery_now
            existing_invoice.collection_started_at = existing_invoice.sent_at or recovery_now
        await session.commit()
        candidates = list((await session.scalars(select(Invoice.id).where(
            Invoice.status == InvoiceStatus.draft, Invoice.period_end < month_start,
        ).order_by(Invoice.id))).all())
        review = []
        recovered = set(report.get("recovered_ids", []))
        for invoice_id in candidates:
            async with session_lock(session, issuance.INVOICE_LOCK_BASE + invoice_id, wait=True):
                from app.services.invoicing import _serialize_billing
                await _serialize_billing(session)
                inv = await session.get(Invoice, invoice_id, populate_existing=True, with_for_update=True)
                if inv is None or inv.status != InvoiceStatus.draft:
                    continue
                reseller = await session.get(Reseller, inv.reseller_id)
                p = panels.get(inv.panel_id)
                attempts = list((await session.scalars(select(DeliveryLog.status).where(
                    DeliveryLog.invoice_id == invoice_id, DeliveryLog.kind == DeliveryKind.invoice,
                ).order_by(DeliveryLog.id.desc()))).all())
                reason = None
                if reseller is None or reseller.id not in roots or p is None or not p.enabled:
                    reason = "نماینده یا پنل مشمول صدور خودکار نیست"
                elif inv.amount_toman <= 0:
                    reason = "مبلغ فاکتور صفر است"
                elif not attempts:
                    reason = "سابقه عدم ارسال وجود ندارد؛ بررسی دستی لازم است"
                elif DeliveryStatus.sent in attempts:
                    reason = "سابقه ارسال موفق دارد؛ احتمال بازگردانی عمدی به پیش‌نویس"
                if reason:
                    review.append({"id": inv.id, "number": invoice_code(inv.id),
                                   "reseller_name": reseller.name if reseller else "",
                                   "period": inv.period_label, "reason": reason})
                    continue
                # Commit this stamp together with issuance, so a crash can never re-age it.
                inv.legacy_recovered_at = recovery_now
                inv.telegram_delivery_status = attempts[0].value
                await issuance.issue_locked(session, inv, now=recovery_now)
                inv.telegram_delivery_status = attempts[0].value
                await session.commit()
                recovered.add(invoice_id)
        # Include invoices committed before an interrupted run saved its summary.
        recovered.update((await session.scalars(select(Invoice.id).where(
            Invoice.legacy_recovered_at.is_not(None),
        ))).all())
        report = {**report, "recovered_ids": sorted(recovered), "review": review,
                  "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        await settings_service.set_value(session, REPORT_KEY, report)
        return report
