"""Scheduled jobs: monthly invoicing, daily dunning, periodic panel sync.

Each job opens its own session and never lets an exception escape (which would stop
the scheduler). Cron times use sensible defaults; the owner can also trigger any of
these on demand from the panel (see app.api.operations).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.db import SessionLocal
from app.services import (
    backup_delivery,
    channel_guard,
    delivery,
    dunning,
    invoicing,
    owner_notify,
    settings_service,
    sync as sync_service,
)
from app.services.periods import previous_month

log = logging.getLogger("scheduler.jobs")


async def monthly_invoicing_job() -> None:
    try:
        async with SessionLocal() as session:
            await sync_service.sync_all(session)
            period = previous_month()
            summary = await invoicing.generate_invoices(session, period)
            d = await delivery.send_period(session, period.label)
            log.info("Monthly invoicing job completed for %s", period.label)
            await owner_notify.notify_owner(
                session,
                f"🧾 صدور و ارسال خودکار فاکتورهای دورهٔ {period.label} انجام شد.\n"
                f"• ساخته‌شده: {summary.created}\n"
                f"• مبلغ کل: {summary.total_amount_toman:,.0f} تومان\n"
                f"• ارسال موفق: {d.get('sent', 0)} | بدون ربات: {d.get('unmatched', 0)} | "
                f"ناموفق: {d.get('failed', 0)}",
            )
    except Exception:  # noqa: BLE001
        log.exception("monthly_invoicing_job failed")


async def daily_dunning_job() -> None:
    try:
        async with SessionLocal() as session:
            res = await dunning.run_dunning(session)
            # Only ping the owner when something actionable happened.
            acted = res["reminder1"] + res["reminder2"] + res["warning"] + res["enforced"] + res["enforced_dry"]
            if acted:
                lines = [
                    "🔔 گزارش روزانهٔ یادآوری/مسدودسازی:",
                    f"• یادآوری اول: {res['reminder1']} | یادآوری دوم: {res['reminder2']} | اخطار: {res['warning']}",
                ]
                if res["enforced"]:
                    lines.append(f"• مسدودسازی واقعی: {res['enforced']}")
                if res["enforced_dry"]:
                    lines.append(f"• مسدودسازی (حالت آزمایشی): {res['enforced_dry']}")
                enforced = res.get("enforced_resellers") or []
                if enforced:
                    lines.append("\nنماینده‌های مسدودشده (برای پیام مستقیم کلیک کنید):")
                    lines += [f"• {link}" for link in enforced]
                await owner_notify.notify_owner(session, "\n".join(lines), html=bool(enforced))
    except Exception:  # noqa: BLE001
        log.exception("daily_dunning_job failed")


async def periodic_sync_job() -> None:
    try:
        async with SessionLocal() as session:
            await sync_service.sync_all(session)
            # Re-evaluate per-sub GB caps against the freshly-synced data; alert any that
            # crossed their monthly ceiling (once per month).
            try:
                from app.services import gb_cap

                await gb_cap.check_caps(session)
            except Exception:  # noqa: BLE001
                log.exception("gb_cap check failed")
    except Exception:  # noqa: BLE001
        log.exception("periodic_sync_job failed")


async def channel_guard_job() -> None:
    try:
        async with SessionLocal() as session:
            await channel_guard.enforce_channel(session)
    except Exception:  # noqa: BLE001
        log.exception("channel_guard_job failed")


async def backup_job() -> None:
    try:
        async with SessionLocal() as session:
            if await settings_service.get(session, "backup_enabled", True):
                await backup_delivery.send_backup_to_owner(session)
    except Exception:  # noqa: BLE001
        log.exception("backup_job failed")


def register(sched: AsyncIOScheduler) -> None:
    # 1st of each month, 09:00 UTC — bill the previous month and deliver.
    sched.add_job(monthly_invoicing_job, "cron", day=1, hour=9, id="monthly_invoicing",
                  replace_existing=True)
    # Daily 10:00 UTC — reminders + enforcement.
    sched.add_job(daily_dunning_job, "cron", hour=10, id="daily_dunning", replace_existing=True)
    # Daily 11:00 UTC — remove non-reseller members from the channel (dry-run unless enabled).
    sched.add_job(channel_guard_job, "cron", hour=11, id="channel_guard", replace_existing=True)
    # Every 6 hours — keep snapshots fresh for the dashboard.
    sched.add_job(periodic_sync_job, "interval", hours=6, id="periodic_sync", replace_existing=True)
    # Every 2 hours — auto-backup to the owner's Telegram PV.
    sched.add_job(backup_job, "interval", hours=2, id="backup", replace_existing=True)
    log.info("Registered 5 scheduled jobs.")
