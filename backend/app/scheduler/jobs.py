"""Scheduled jobs: monthly invoicing, daily dunning, periodic panel sync.

Each job opens its own session and never lets an exception escape (which would stop
the scheduler). All timings are owner-configurable from the panel (Settings → زمان‌بندی),
read by `load_config` and registered with deterministic wall-clock anchors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    maintenance,
    owner_notify,
    rates,
    settings_service,
    storefront_audit,
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
    digest_hour: int = 9       # daily owner digest: hour (0–23)
    storefront_reaper_minutes: int = 15  # storefront pending-order reaper cadence (1–1440)
    storefront_delivery_minutes: int = 1  # storefront broadcast/direct delivery worker cadence (1–60)


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
        "rate_refresh_hours", "enforcement_worker_interval_minutes", "daily_digest_hour",
        "storefront_pending_order_reaper_minutes", "storefront_delivery_worker_interval_minutes",
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
        digest_hour=_clamp(s.get("daily_digest_hour"), 0, 23, 9),
        storefront_reaper_minutes=_clamp(
            s.get("storefront_pending_order_reaper_minutes"), 1, 1440, 15),
        storefront_delivery_minutes=_clamp(
            s.get("storefront_delivery_worker_interval_minutes"), 1, 60, 1),
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
            # Surface resellers that fell through the hierarchy entirely (parent deleted / a
            # parent cycle) — they were NOT billed and the shortfall is otherwise silent.
            if summary.unbilled_subtrees:
                msg += (
                    "\n\n⚠️ نماینده‌های زیر به هیچ باندلی وصل نشدند (نمایندهٔ بالادستی حذف شده "
                    "یا حلقهٔ والد دارند) و فاکتور نشدند؛ سلسله‌مراتب را در پنل اصلاح کنید:\n"
                    + "\n".join(f"• {p}" for p in summary.unbilled_subtrees)
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


async def daily_digest_job() -> None:
    """Send the owner a concise daily summary (KPIs + health) to their Telegram PV."""
    try:
        async with SessionLocal() as session:
            from app.services import owner_report

            if not await owner_report.digest_enabled(session):
                return
            text = await owner_report.daily_digest(session)
            # Append any NEW tracked errors since the last digest, so exceptions that never
            # became their own Telegram alert still reach the owner once a day. Best-effort:
            # the digest must go out even if error aggregation breaks.
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            advance_cursor = False
            try:
                from app.core import errortrack

                cursor = str(await settings_service.get(session, "error_digest_last_ts", "") or "")
                since = errortrack.parse_ts(cursor) or (
                    datetime.now(timezone.utc) - timedelta(hours=24))
                section = owner_report.render_errors(errortrack.summary(since=since))
                if section:
                    text += "\n\n" + section
                advance_cursor = True
            except Exception:  # noqa: BLE001
                log.exception("error digest section failed")
            sent = await owner_notify.notify_owner(session, text)
            # Advance the cursor only after a DELIVERED digest — an undelivered day's errors
            # show up in the next successful digest instead of vanishing.
            if sent and advance_cursor:
                await settings_service.set_value(session, "error_digest_last_ts", now_iso)
    except Exception:  # noqa: BLE001
        log.exception("daily_digest_job failed")


async def scheduler_heartbeat_job() -> None:
    """Stamp `scheduler_last_heartbeat` so /health can tell a silently-dead scheduler from a
    healthy one (a stopped scheduler means invoicing/dunning/backups quietly never run).
    Fixed ~2-minute cadence, not owner-configurable; one settings upsert per tick."""
    try:
        async with SessionLocal() as session:
            await settings_service.set_value(
                session, "scheduler_last_heartbeat",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001
        log.exception("scheduler_heartbeat_job failed")


async def daily_maintenance_job() -> None:
    """Daily retention sweep: aged log/audit rows + stale (removed-from-Hiddify) end-user
    snapshots older than the previous billing month + their orphaned usage meters + stale storefront
    tire-kicker data (never the financial ledger)."""
    try:
        async with SessionLocal() as session:
            await maintenance.prune_old_logs(session)
        async with SessionLocal() as session:
            await maintenance.prune_stale_snapshots(session)
        async with SessionLocal() as session:
            await maintenance.prune_stale_storefront(session)
        async with SessionLocal() as session:
            await maintenance.prune_owner_data(session)
        async with SessionLocal() as session:
            await storefront_audit.prune_commands(session)
            await session.commit()
        async with SessionLocal() as session:
            from app.services import storefront_delivery
            retention = _clamp(
                await settings_service.get(session, "storefront_delivery_retention_days", 90),
                0, 3650, 90)
            pruned = await storefront_delivery.prune_recipients(session, retention)
            await session.commit()
            if pruned:
                log.info("storefront delivery retention: pruned %s recipient rows", pruned)
    except Exception:  # noqa: BLE001
        log.exception("daily_maintenance_job failed")


async def storefront_delivery_worker_job() -> None:
    """Deliver queued storefront broadcasts + direct messages in bounded, resumable batches. Never
    crashes the scheduler loop; the worker itself is fully restart-safe."""
    try:
        from app.services import storefront_delivery

        summary = await storefront_delivery.run_once()
        if summary.get("claimed"):
            log.info("storefront delivery: %s", summary)
    except Exception:  # noqa: BLE001
        log.exception("storefront_delivery_worker_job failed")


async def storefront_reaper_job() -> None:
    """Reconcile storefront orders stuck in `pending` (process died mid-purchase): complete the ones
    whose config actually got created on the panel, refund the rest. Money-correctness backstop."""
    try:
        from app.services import storefront_provision

        async with SessionLocal() as session:
            cfg = await load_config(session)
            older_than = datetime.now(timezone.utc) - timedelta(
                minutes=cfg.storefront_reaper_minutes)
            res = await storefront_provision.reap_pending_orders(session, older_than=older_than)
            if res.get("completed") or res.get("refunded"):
                log.info("storefront reaper: %s", res)
    except Exception:  # noqa: BLE001
        log.exception("storefront_reaper_job failed")


async def storefront_expiry_job() -> None:
    """Daily proactive storefront notices: near-expiry reminders, «free trial ended → buy» nudges,
    «~80% volume used → renew» warnings, and win-back notices for services that already lapsed.
    Each runs independently so one failing never blocks the others."""
    from app.services import storefront_expiry

    for name, fn in (
        ("notify_expiring", storefront_expiry.notify_expiring),
        ("notify_trial_ended", storefront_expiry.notify_trial_ended),
        ("notify_usage_high", storefront_expiry.notify_usage_high),
        ("notify_expired", storefront_expiry.notify_expired),
    ):
        try:
            async with SessionLocal() as session:
                await fn(session)
        except Exception:  # noqa: BLE001
            log.exception("storefront_expiry_job: %s failed", name)


async def rate_refresh_job() -> None:
    """Refresh the live USDT→Toman rate (auto mode), the TON→Toman rate (when TON payment is
    enabled), and the AVAX→Toman rate (when AVAX payment is enabled). All independent and
    best-effort."""
    try:
        async with SessionLocal() as session:
            if str(await settings_service.get(session, "rate_mode", "manual")).lower() == "auto":
                await rates.refresh_auto_rate(session)
            if await settings_service.get(session, "pay_ton_enabled", False):
                await rates.refresh_ton_rate(session)
            if await settings_service.get(session, "pay_avax_enabled", False):
                await rates.refresh_avax_rate(session)
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
        # Daily log-retention sweep at a fixed quiet hour (04:30 local). Deletes aged
        # sync_runs / delivery_log / terminal enforcement_actions; never the ledger.
        ("daily_maintenance", daily_maintenance_job,
         CronTrigger(hour=4, minute=30, timezone=tz), 6 * 3600),
        # Daily owner digest (KPIs + health) to the owner's Telegram PV at the configured hour.
        ("daily_digest", daily_digest_job,
         CronTrigger(hour=cfg.digest_hour, minute=30, timezone=tz), 6 * 3600),
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
        # Storefront pending-order reaper: complete/refund purchases orphaned by a mid-buy crash.
        ("storefront_reaper", storefront_reaper_job,
         IntervalTrigger(minutes=cfg.storefront_reaper_minutes, start_date=interval_anchor,
                         timezone=tz), 300),
        # Storefront durable delivery worker: send queued broadcasts + direct messages.
        ("storefront_delivery", storefront_delivery_worker_job,
         IntervalTrigger(minutes=cfg.storefront_delivery_minutes, start_date=interval_anchor,
                         timezone=tz), 300),
        # Liveness stamp read by /health; fixed cadence on purpose (not a setting).
        ("scheduler_heartbeat", scheduler_heartbeat_job,
         IntervalTrigger(minutes=2, start_date=interval_anchor, timezone=tz), 60),
        # Storefront near-expiry reminders, daily at a fixed quiet hour (11:15 local; the
        # threshold in days is the `storefront_expiry_notify_days` setting, 0 = off).
        ("storefront_expiry", storefront_expiry_job,
         CronTrigger(hour=11, minute=15, timezone=tz), 6 * 3600),
    ]
    for job_id, func, trigger, grace in specs:
        sched.add_job(func, trigger, id=job_id, replace_existing=True,
                      coalesce=True, misfire_grace_time=grace)
    log.info(
        "Registered %d jobs (tz=%s): invoice day=%d@%02d:00, dunning %02d:00, "
        "maintenance 04:30, sync every %dh, guard every %dm, enforcement every %dm, "
        "backup every %dh, rate every %dh.",
        len(specs), sched.timezone, cfg.invoice_day, cfg.invoice_hour, cfg.dunning_hour,
        cfg.sync_hours, cfg.guard_minutes, cfg.enforcement_minutes, cfg.backup_hours, cfg.rate_hours,
    )
