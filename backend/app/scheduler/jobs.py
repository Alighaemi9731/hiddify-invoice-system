"""Scheduled jobs: monthly invoicing, daily dunning, periodic panel sync.

Each job opens its own session and never lets an exception escape (which would stop
the scheduler). All timings are owner-configurable from the panel (Settings → زمان‌بندی),
read by `load_config` and registered with deterministic wall-clock anchors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.services import (
    backup_delivery,
    channel_guard,
    delivery,
    dunning,
    enforcement,
    invoicing,
    owner_notify,
    rates,
    settings_service,
)
from app.services import (
    sync as sync_service,
)
from app.services.periods import previous_month

log = logging.getLogger("scheduler.jobs")


# ----------------------------- configurable timings -----------------------------
@dataclass(frozen=True)
class ScheduleConfig:
    invoice_day: int = 1       # monthly invoice: day of month (1–28)
    invoice_hour: int = 9      # monthly invoice: hour (0–23)
    dunning_hour: int = 10     # daily reminders/enforcement: hour (0–23)
    sync_hours: int = 6        # panel sync: every N hours (1–24)
    guard_minutes: int = 10    # channel/group guard: every N minutes (1–60)
    backup_hours: int = 2      # auto-backup: every N hours (1–24)
    rate_hours: int = 1        # live USDT→Toman rate refresh: every N hours (1–24)
    enforcement_minutes: int = 5  # queued live enforcement worker cadence (1–60)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    """Coerce a setting to an int within [lo, hi], falling back to `default` if it's
    missing or unparseable — a bad value can never break the scheduler."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


async def load_config(session: AsyncSession) -> ScheduleConfig:
    """Read the owner-configured scheduler timings (clamped to safe ranges)."""
    s = await settings_service.get_many(session, [
        "invoice_day_of_month", "invoice_hour", "dunning_hour",
        "sync_interval_hours", "guard_interval_minutes", "backup_interval_hours",
        "rate_refresh_hours", "enforcement_worker_interval_minutes",
    ])
    return ScheduleConfig(
        invoice_day=_clamp(s.get("invoice_day_of_month"), 1, 28, 1),
        invoice_hour=_clamp(s.get("invoice_hour"), 0, 23, 9),
        dunning_hour=_clamp(s.get("dunning_hour"), 0, 23, 10),
        sync_hours=_clamp(s.get("sync_interval_hours"), 1, 24, 6),
        guard_minutes=_clamp(s.get("guard_interval_minutes"), 1, 60, 10),
        backup_hours=_clamp(s.get("backup_interval_hours"), 1, 24, 2),
        rate_hours=_clamp(s.get("rate_refresh_hours"), 1, 24, 1),
        enforcement_minutes=_clamp(s.get("enforcement_worker_interval_minutes"), 1, 60, 5),
    )


async def monthly_invoicing_job() -> None:
    try:
        async with SessionLocal() as session:
            await sync_service.sync_all(session)
            period = previous_month()
            summary = await invoicing.generate_invoices(session, period)
            d = await delivery.send_period(session, period.label)
            log.info("Monthly invoicing job completed for %s", period.label)
            msg = (
                f"🧾 صدور و ارسال خودکار فاکتورهای دورهٔ {period.label} انجام شد.\n"
                f"• ساخته‌شده: {summary.created}\n"
                f"• مبلغ کل: {summary.total_amount_toman:,.0f} تومان\n"
                f"• ارسال موفق: {d.get('sent', 0)} | بدون ربات: {d.get('unmatched', 0)} | "
                f"ناموفق: {d.get('failed', 0)}"
            )
            # Surface any panel skipped because its sync failed — those resellers were NOT billed
            # this run and need attention (otherwise the shortfall is silent).
            if summary.skipped_panels:
                msg += (
                    "\n\n⚠️ پنل‌های زیر به‌دلیل ناموفق‌بودن همگام‌سازی فاکتور نشدند "
                    "(بررسی و سپس «صدور فاکتورهای دوره» را برای آن‌ها بزنید):\n"
                    + "\n".join(f"• {p}" for p in summary.skipped_panels)
                )
            await owner_notify.notify_owner(session, msg)
    except Exception:  # noqa: BLE001
        log.exception("monthly_invoicing_job failed")


async def daily_dunning_job() -> None:
    try:
        async with SessionLocal() as session:
            res = await dunning.run_dunning(session)
            # Only ping the owner when something actionable happened.
            acted = (
                res["reminder1"] + res["reminder2"] + res["warning"]
                + res["enforced"] + res["enforced_dry"] + res.get("enforcement_queued", 0)
            )
            if acted:
                # Show DELIVERED counts with ATTEMPTED in parentheses, so a reminder that was
                # tried but didn't reach the reseller (blocked/unmatched/Telegram error) is
                # visible rather than reported as a success.
                def _da(sent_key: str, att_key: str) -> str:
                    sent, att = res.get(sent_key, 0), res.get(att_key, 0)
                    return f"{sent}" + (f" (از {att} تلاش)" if att != sent else "")
                lines = [
                    "🔔 گزارش روزانهٔ یادآوری/مسدودسازی (ارسال‌شده / تلاش):",
                    f"• یادآوری اول: {_da('reminder1_sent', 'reminder1')} | "
                    f"یادآوری دوم: {_da('reminder2_sent', 'reminder2')} | "
                    f"اخطار: {_da('warning_sent', 'warning')}",
                ]
                if res["enforced"]:
                    lines.append(f"• مسدودسازی واقعی: {res['enforced']}")
                if res.get("enforcement_queued"):
                    lines.append(f"• مسدودسازی در صف اجرا: {res['enforcement_queued']}")
                if res["enforced_dry"]:
                    lines.append(f"• مسدودسازی (حالت آزمایشی): {res['enforced_dry']}")
                enforced = res.get("enforced_resellers") or []
                if enforced:
                    lines.append("\nنماینده‌های مسدودشده (برای پیام مستقیم کلیک کنید):")
                    lines += [f"• {link}" for link in enforced]
                await owner_notify.notify_owner(session, "\n".join(lines), html=bool(enforced))
    except Exception:  # noqa: BLE001
        log.exception("daily_dunning_job failed")


async def enforcement_queue_job() -> None:
    try:
        async with SessionLocal() as session:
            res = await enforcement.process_enforcement_queue(session)
            if res.get("picked"):
                log.info("Enforcement queue worker: %s", res)
    except Exception:  # noqa: BLE001
        log.exception("enforcement_queue_job failed")


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
    except Exception as exc:  # noqa: BLE001
        log.exception("backup_job failed")
        # A failed backup used to silently produce a dump-less archive reported as success;
        # now it fails loudly — tell the owner so they know automated backups need attention.
        try:
            async with SessionLocal() as session:
                await owner_notify.notify_owner(
                    session,
                    "⚠️ پشتیبان‌گیری خودکار ناموفق بود. لطفاً وضعیت سرور/دیتابیس را بررسی کنید.\n"
                    f"خطا: {exc}",
                )
        except Exception:  # noqa: BLE001
            log.exception("backup_job failure notification failed")


async def rate_refresh_job() -> None:
    """Refresh the live USDT→Toman rate (auto mode) and the TON→Toman rate (when TON payment
    is enabled). Both are independent and best-effort."""
    try:
        async with SessionLocal() as session:
            if str(await settings_service.get(session, "rate_mode", "manual")).lower() == "auto":
                await rates.refresh_auto_rate(session)
            if await settings_service.get(session, "pay_ton_enabled", False):
                await rates.refresh_ton_rate(session)
    except Exception:  # noqa: BLE001
        log.exception("rate_refresh_job failed")


def register(sched: AsyncIOScheduler, cfg: ScheduleConfig | None = None) -> None:
    """(Re)register all jobs with the owner-configured timings. Safe to call on a running
    scheduler — `replace_existing=True` updates each trigger in place, so this doubles as the
    live "apply settings" path. Falls back to defaults if no config is given.

    Monthly invoicing and dunning use cron because they are calendar events. Repeating
    jobs use interval triggers with a fixed epoch anchor. The fixed anchor is important:
    APScheduler computes the next future boundary after a restart instead of starting a
    fresh countdown, while values such as 7 hours or 17 minutes retain their true spacing
    across day/hour boundaries. Times use the scheduler timezone."""
    cfg = cfg or ScheduleConfig()
    tz = sched.timezone
    interval_anchor = datetime(2000, 1, 1, 0, 0, tzinfo=tz)
    rate_anchor = datetime(2000, 1, 1, 0, 5, tzinfo=tz)

    # Build + validate ALL triggers BEFORE mutating the jobstore. add_job(..., "cron", ...)
    # constructs the trigger internally, so a bad field would throw mid-loop and leave the
    # running scheduler half-updated (register also serves the live apply_settings path).
    # Constructing the CronTrigger objects up front makes registration all-or-nothing.
    #   • Monthly invoice: day N of each month at HH:00 (bill prev month + deliver)
    #   • Daily dunning at HH:00 (reminders + enforcement)
    #   • Channel/group guard every N minutes from a fixed midnight anchor
    #   • Panel sync and auto-backup every N hours from the same fixed anchor
    # The 4th value is misfire_grace_time (seconds): how late a fire may run if the scheduler
    # was busy/down at the exact moment. APScheduler's DEFAULT is 1s, which silently SKIPS a
    # job whose tick the loop missed by a second — fatal for the once-a-month invoicing. Give
    # each a generous grace (monthly the largest) and coalesce so a backlog runs once.
    specs = [
        ("monthly_invoicing", monthly_invoicing_job,
         CronTrigger(day=cfg.invoice_day, hour=cfg.invoice_hour, minute=0, timezone=tz), 12 * 3600),
        ("daily_dunning", daily_dunning_job,
         CronTrigger(hour=cfg.dunning_hour, minute=0, timezone=tz), 6 * 3600),
        ("channel_guard", channel_guard_job,
         IntervalTrigger(minutes=cfg.guard_minutes, start_date=interval_anchor, timezone=tz), 300),
        ("enforcement_queue", enforcement_queue_job,
         IntervalTrigger(minutes=cfg.enforcement_minutes, start_date=interval_anchor, timezone=tz), 300),
        ("periodic_sync", periodic_sync_job,
         IntervalTrigger(hours=cfg.sync_hours, start_date=interval_anchor, timezone=tz), 1800),
        ("backup", backup_job,
         IntervalTrigger(hours=cfg.backup_hours, start_date=interval_anchor, timezone=tz), 3600),
        # Live USDT→Toman rate refresh, a few minutes past the hour so it doesn't collide
        # with the on-the-hour jobs above.
        ("rate_refresh", rate_refresh_job,
         IntervalTrigger(hours=cfg.rate_hours, start_date=rate_anchor, timezone=tz), 3600),
    ]
    for job_id, func, trigger, grace in specs:
        sched.add_job(func, trigger, id=job_id, replace_existing=True,
                      coalesce=True, misfire_grace_time=grace)
    log.info(
        "Registered 6 jobs (tz=%s): invoice day=%d@%02d:00, dunning %02d:00, "
        "sync every %dh, guard every %dm, enforcement every %dm, backup every %dh, rate every %dh.",
        sched.timezone, cfg.invoice_day, cfg.invoice_hour, cfg.dunning_hour,
        cfg.sync_hours, cfg.guard_minutes, cfg.enforcement_minutes, cfg.backup_hours, cfg.rate_hours,
    )
