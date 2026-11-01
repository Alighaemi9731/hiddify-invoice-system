"""Recover the current billing cycle after downtime, without replaying older months."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MonthlyBillingRun
from app.services import delivery, invoicing, owner_notify, periods
from app.services import sync as sync_service
from app.services.job_lock import session_lock

log = logging.getLogger("monthly_billing")
_LOCK_KEY = 734_137_050
MAX_ATTEMPTS = 3
RETRY_DELAY = dt.timedelta(minutes=5)


def aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


async def run_due(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    from app.scheduler.jobs import load_config
    from app.scheduler.scheduler import SCHEDULER_TIMEZONE

    now = aware(now or dt.datetime.now(dt.timezone.utc))
    local = now.astimezone(ZoneInfo(SCHEDULER_TIMEZONE))
    cfg = await load_config(session)
    due = local.replace(day=cfg.invoice_day, hour=cfg.invoice_hour, minute=0, second=0, microsecond=0)
    if local < due:
        return {"status": "not_due"}
    period = periods.previous_month(local.date())
    async with session_lock(session, _LOCK_KEY) as acquired:
        if not acquired:
            return {"status": "already_running"}
        run = await session.get(MonthlyBillingRun, period.start, populate_existing=True)
        if run is not None:
            if run.status in ("completed", "failed"):
                return {"status": run.status, "period": period.label}
            if run.next_attempt_at and now < aware(run.next_attempt_at):
                return {"status": "retry_wait", "period": period.label}
            if run.attempts >= MAX_ATTEMPTS:
                run.status = "failed"
                run.error = "Automatic retry limit reached after an interrupted run"
                await session.commit()
                return {"status": "failed", "period": period.label}
        else:
            run = MonthlyBillingRun(period_start=period.start, attempts=0)
            session.add(run)
        run.status = "running"
        run.attempts += 1
        run.started_at = now
        run.finished_at = None
        run.next_attempt_at = None
        run.error = None
        await session.commit()
        try:
            await sync_service.sync_all(session)
            summary = await invoicing.generate_invoices(session, period)
            sent = await delivery.send_period(session, period.label)
            result: dict[str, Any] = {
                "period": period.label, "created": summary.created,
                "updated": summary.updated, "amount_toman": float(summary.total_amount_toman),
                "delivery": sent, "skipped_panels": summary.skipped_panels,
                "unbilled_subtrees": summary.unbilled_subtrees,
            }
            problem = bool(sent.get("failed") or summary.skipped_panels or summary.unbilled_subtrees)
            run.result = result
            run.status = ("failed" if run.attempts >= MAX_ATTEMPTS else "retry") if problem else "completed"
            run.error = "Some panels, invoices or deliveries require retry/review" if problem else None
        except Exception as exc:
            await session.rollback()
            run = await session.get(MonthlyBillingRun, period.start, populate_existing=True)
            assert run is not None
            run.status = "failed" if run.attempts >= MAX_ATTEMPTS else "retry"
            run.error = type(exc).__name__
            log.exception("Monthly billing failed for %s", period.label)
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        if run.status == "retry":
            run.next_attempt_at = now + RETRY_DELAY
        await session.commit()
        log.info("Monthly billing %s: %s (attempt %d)", period.label, run.status, run.attempts)
        # Notification failure must never roll back a completed billing cycle.
        result = run.result or {}
        delivered = result.get("delivery", {})
        message = (
            f"🧾 گزارش صدور و ارسال خودکار دورهٔ {period.label}\n"
            f"وضعیت: { {'completed': 'انجام شد', 'retry': 'تلاش مجدد تا ۵ دقیقه دیگر', 'failed': 'نیاز به بررسی'}.get(run.status, run.status) }\n"
            f"ارسال موفق: {delivered.get('sent', 0)} | ناموفق: {delivered.get('failed', 0)} | "
            f"بدون ربات: {delivered.get('unmatched', 0)} | ربات مسدود: {delivered.get('blocked', 0)}"
        )
        if result.get("skipped_panels"):
            message += "\nپنل‌های فاکتورنشده:\n" + "\n".join(result["skipped_panels"])
        if result.get("unbilled_subtrees"):
            message += "\nزیرمجموعه‌های فاکتورنشده:\n" + "\n".join(result["unbilled_subtrees"])
        try:
            await owner_notify.notify_owner(session, message)
        except Exception:
            log.exception("Monthly billing owner notification failed")
        return {"status": run.status, "period": period.label, "attempts": run.attempts}
