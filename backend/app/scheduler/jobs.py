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
    storefront_autorenew_minutes: int = 15  # deferred auto-renew fire-sweep cadence (1–1440)
    trial_reset_day: int = 1   # fleet-wide free-trial re-arm: day of month (1–28)
    trial_reset_hour: int = 8  # fleet-wide free-trial re-arm: hour (0–23)
    traffic_audit_hour: int = 5  # daily reseller-traffic audit: hour (0–23)


def _anchor(tz, minute_offset: int) -> datetime:  # noqa: ANN001
    """The fixed epoch a repeating job's IntervalTrigger counts from, shifted by `minute_offset`.

    The anchor must be FIXED (not "now"), or the job re-anchors to process start and a frequent
    redeploy can starve it forever — that is why the 2-hourly backup once never fired. The
    per-job OFFSET is the second half of the rule: with a shared epoch every repeating job is in
    phase, so their memory peaks add instead of interleaving (see `register`).
    """
    return datetime(2000, 1, 1, 0, 0, tzinfo=tz) + timedelta(minutes=minute_offset)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    """Coerce a setting to an int within [lo, hi], falling back to `default` if it's
    missing or unparseable — a bad value can never break the scheduler."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# Every setting `load_config` reads. Exported because `api/settings.py` must live-apply exactly
# this set — the two lists used to be maintained separately and drifted: the storefront delivery
# and auto-renew cadences were readable here but absent there, so editing them in the panel
# appeared to work and then quietly needed a restart. One list, no drift.
SCHEDULE_SETTING_KEYS: tuple[str, ...] = (
    "invoice_day_of_month", "invoice_hour", "dunning_hour",
    "sync_interval_hours", "guard_interval_minutes", "backup_interval_hours",
    "rate_refresh_hours", "enforcement_worker_interval_minutes", "daily_digest_hour",
    "storefront_pending_order_reaper_minutes", "storefront_delivery_worker_interval_minutes",
    "storefront_autorenew_interval_minutes",
    "storefront_trial_reset_day", "storefront_trial_reset_hour",
    "traffic_audit_hour",
)


async def load_config(session: AsyncSession) -> ScheduleConfig:
    """Read the owner-configured scheduler timings (clamped to safe ranges)."""
    s = await settings_service.get_many(session, list(SCHEDULE_SETTING_KEYS))
    return ScheduleConfig(
        invoice_day=_clamp(s.get("invoice_day_of_month"), 1, 28, 1),
        invoice_hour=_clamp(s.get("invoice_hour"), 0, 23, 9),
        dunning_hour=_clamp(s.get("dunning_hour"), 0, 23, 10),
        sync_hours=_clamp(s.get("sync_interval_hours"), 1, 24, 1),
        guard_minutes=_clamp(s.get("guard_interval_minutes"), 1, 60, 10),
        backup_hours=_clamp(s.get("backup_interval_hours"), 1, 24, 2),
        rate_hours=_clamp(s.get("rate_refresh_hours"), 1, 24, 1),
        enforcement_minutes=_clamp(s.get("enforcement_worker_interval_minutes"), 1, 60, 5),
        digest_hour=_clamp(s.get("daily_digest_hour"), 0, 23, 9),
        storefront_reaper_minutes=_clamp(
            s.get("storefront_pending_order_reaper_minutes"), 1, 1440, 15),
        storefront_delivery_minutes=_clamp(
            s.get("storefront_delivery_worker_interval_minutes"), 1, 60, 1),
        storefront_autorenew_minutes=_clamp(
            s.get("storefront_autorenew_interval_minutes"), 1, 1440, 15),
        # 28, not 31: the re-arm day must exist in February too, or the sweep would silently skip
        # a month and the whole feature would be «چرا این ماه ریست نشد؟».
        trial_reset_day=_clamp(s.get("storefront_trial_reset_day"), 1, 28, 1),
        trial_reset_hour=_clamp(s.get("storefront_trial_reset_hour"), 0, 23, 8),
        traffic_audit_hour=_clamp(s.get("traffic_audit_hour"), 0, 23, 5),
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
                    "\n\n⚠️ پنل‌های زیر به‌دلیل ناموفق‌بودنِ همگام‌سازی فاکتور نشدند "
                    "(لطفاً بررسی کرده و سپس «صدور فاکتورهای دوره» را برای آن‌ها اجرا کنید):\n"
                    + "\n".join(f"• {p}" for p in summary.skipped_panels)
                )
            # Surface resellers that fell through the hierarchy entirely (parent deleted / a
            # parent cycle) — they were NOT billed and the shortfall is otherwise silent.
            if summary.unbilled_subtrees:
                msg += (
                    "\n\n⚠️ نماینده‌های زیر در هیچ فاکتوری لحاظ نشدند (نمایندهٔ بالادستیِ آن‌ها حذف شده "
                    "یا حلقهٔ والد دارند)؛ لطفاً سلسله‌مراتب را در پنل اصلاح کنید:\n"
                    + "\n".join(f"• {p}" for p in summary.unbilled_subtrees)
                )
            await owner_notify.notify_owner(session, msg)
    except Exception:  # noqa: BLE001
        log.exception("monthly_invoicing_job failed")


async def daily_dunning_job() -> None:
    try:
        async with SessionLocal() as session:
            # A suspended debtor can re-enable their own users from the Hiddify panel (zeroing
            # their limits stops new users, not their admin login) and dunning would never look
            # again — it only triggers for an `active` reseller. Re-assert first, so the run that
            # chases the debt also repairs a suspension that was quietly undone.
            try:
                drift = await enforcement.reassert_enforced(session)
                if drift.get("requeued"):
                    log.warning("re-asserted suspension for %d reseller(s) whose users came back "
                                "online", drift["requeued"])
            except Exception:  # noqa: BLE001 — never let this block the dunning run
                log.exception("reassert_enforced failed")
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
            from app.services import traffic_audit

            dropped = await traffic_audit.prune(session)
            if dropped:
                log.info("traffic audit retention: pruned %s history rows", dropped)
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
        async with SessionLocal() as session:
            # Re-send below-cost sweep notices that hit a transient delivery failure. This only
            # walks shops the owner already swept — it never re-scans and never disables, so
            # raising `default_price_per_gb` can't turn it into an unattended mass-disable.
            from app.services import storefront_belowcost
            retried = await storefront_belowcost.retry_pending_notices(session)
            if retried.get("sent"):
                log.info("below-cost sweep notices retried: %s", retried)
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


async def storefront_autorenew_job() -> None:
    """Fire deferred one-shot auto-renewals for armed configs that are near exhaustion, and clean up
    dangling arms. Money-moving + panel I/O, but fully crash-safe (deterministic op_id + the durable
    renewal reconciler). Never crashes the scheduler loop."""
    try:
        from app.services import storefront_autorenew

        await storefront_autorenew.sweep()
    except Exception:  # noqa: BLE001
        log.exception("storefront_autorenew_job failed")


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


async def storefront_trial_reset_job() -> None:
    """Fleet-wide automatic monthly free-trial re-arm (+ the customer announcement).

    Registered on the configured day AND the two days after it (see `_trial_reset_days`). Those
    extra days are a RETRY window, not extra resets: a shop already stamped with the current
    period is filtered out before anything is called, so a healed run touches only what the first
    one could not finish."""
    try:
        from app.services import storefront_trial_reset

        summary = await storefront_trial_reset.sweep()
        if summary.get("shops") or summary.get("failed"):
            log.info("storefront trial auto-reset: %s", summary)
    except Exception:  # noqa: BLE001
        log.exception("storefront_trial_reset_job failed")


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


async def traffic_audit_job() -> None:
    """Daily reseller-traffic audit: record what each top-level reseller ACTUALLY moved.

    Runs in the scheduler container, so its result never reaches the API's in-memory status dict —
    the panel reads it back from the stored history (`/api/ops/traffic-audit/latest`).

    Report-only by design: this writes a history row and logs. It never bills, warns or enforces.
    """
    try:
        from app.services import traffic_audit

        async with SessionLocal() as session:
            summary = await traffic_audit.run_daily(session)
        if summary.get("flagged"):
            log.warning("traffic audit: %s reseller(s) flagged", summary["flagged"])
    except Exception:  # noqa: BLE001 — never crash the scheduler loop
        log.exception("traffic_audit_job failed")


def _traffic_audit_hours(hour: int) -> str:
    """The cron `hour` field for the traffic audit: the configured hour plus two.

    Same reasoning as `_trial_reset_days`, one cadence down. A daily fire's next chance is 24 hours
    away, and `server_status` offers no way to re-read a missed "yesterday" — so a scheduler that
    was mid-deploy at that minute would lose the day permanently. The retry hours are nearly free:
    a reseller already stored for that day is skipped before any panel call is made.
    """
    hour = max(0, min(23, int(hour or 0)))
    return f"{hour},{(hour + 2) % 24}"


def _trial_reset_days(day: int) -> str:
    """The cron `day` field for the fleet-wide trial re-arm: the configured day plus two.

    The extra days exist because a once-a-month cron fire is the one shape with no second chance:
    if the scheduler is mid-deploy at that minute, or the sweep dies halfway through 150 shops,
    the fleet waits a MONTH. The `trial_reset_period` stamp already makes a completed shop a
    no-op, so the retry days cost one cheap query and can never double-reset. Capped at 28 so
    every day in the expression exists in February."""
    day = max(1, min(28, int(day or 1)))
    return f"{day}-{min(day + 2, 28)}" if day < 28 else "28"


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
    # Per-job anchor offsets — see _anchor(). Jobs used to SHARE one midnight epoch, which put
    # every repeating job in phase: eight of them fired together at 00:00 and the two heaviest,
    # periodic_sync (~416 MB) and backup (~79 MB), coincided four times a day (6h and 2h both
    # divide 6h). Peaks ADD — APScheduler bounds instances per job, not globally — so the
    # scheduler sat at ~87% of its 768 MB cap at those moments, which is the shape of the
    # Jul 2026 OOM kills. Spreading the phases makes the peak a max() instead of a sum().
    #
    # Offsets are chosen so no two jobs share a firing minute at their default cadences —
    # asserted by tests/test_scheduler_stagger.py, which fails if an offset is reused.
    # `storefront_delivery` is exempt: at a 1-minute cadence it fires every minute by
    # definition, and it is the lightest job here.
    interval_anchor = _anchor(tz, 0)          # heartbeat only (fixed 2-min cadence, ~1 MB)
    enforcement_anchor = _anchor(tz, 2)
    guard_anchor = _anchor(tz, 3)
    rate_anchor = _anchor(tz, 5)              # kept at :05 (was already staggered)
    reaper_anchor = _anchor(tz, 6)            # not 8: 8+15=23 lands on channel_guard's :23
    autorenew_anchor = _anchor(tz, 10)
    backup_anchor = _anchor(tz, 20)           # the two heavyweights, 25 minutes apart and
    sync_anchor = _anchor(tz, 45)             # on periods that can never realign (20≢45 mod 120)

    # Build + validate ALL triggers BEFORE mutating the jobstore. add_job(..., "cron", ...)
    # constructs the trigger internally, so a bad field would throw mid-loop and leave the
    # running scheduler half-updated (register also serves the live apply_settings path).
    # Constructing the CronTrigger objects up front makes registration all-or-nothing.
    #   • Monthly invoice: day N of each month at HH:00 (bill prev month + deliver)
    #   • Daily dunning at HH:00 (reminders + enforcement)
    #   • Channel/group guard every N minutes from its own fixed anchor
    #   • Panel sync and auto-backup every N hours, from anchors 25 minutes apart
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
         IntervalTrigger(minutes=cfg.guard_minutes, start_date=guard_anchor, timezone=tz), 300),
        ("enforcement_queue", enforcement_queue_job,
         IntervalTrigger(minutes=cfg.enforcement_minutes, start_date=enforcement_anchor,
                         timezone=tz), 300),
        # The two heavyweights. periodic_sync peaks around 416 MB (3 concurrent panel upserts)
        # and backup around 79 MB; on the old shared anchor they landed together 4×/day. 20 vs
        # 45 minutes past keeps them apart at EVERY cadence pair the settings can produce.
        ("periodic_sync", periodic_sync_job,
         IntervalTrigger(hours=cfg.sync_hours, start_date=sync_anchor, timezone=tz), 1800),
        ("backup", backup_job,
         IntervalTrigger(hours=cfg.backup_hours, start_date=backup_anchor, timezone=tz), 3600),
        # Live USDT→Toman rate refresh, a few minutes past the hour so it doesn't collide
        # with the on-the-hour jobs above.
        ("rate_refresh", rate_refresh_job,
         IntervalTrigger(hours=cfg.rate_hours, start_date=rate_anchor, timezone=tz), 3600),
        # Storefront pending-order reaper: complete/refund purchases orphaned by a mid-buy crash.
        ("storefront_reaper", storefront_reaper_job,
         IntervalTrigger(minutes=cfg.storefront_reaper_minutes, start_date=reaper_anchor,
                         timezone=tz), 300),
        # Storefront durable delivery worker: send queued broadcasts + direct messages.
        # Default cadence is 1 minute, so this one fires every minute by definition — it is the
        # lightest job here and is the documented exception to the no-shared-minute rule.
        ("storefront_delivery", storefront_delivery_worker_job,
         IntervalTrigger(minutes=cfg.storefront_delivery_minutes, start_date=interval_anchor,
                         timezone=tz), 300),
        # Deferred one-shot auto-renew: fire armed configs that are near exhaustion.
        ("storefront_autorenew", storefront_autorenew_job,
         IntervalTrigger(minutes=cfg.storefront_autorenew_minutes, start_date=autorenew_anchor,
                         timezone=tz), 300),
        # Liveness stamp read by /health; fixed cadence on purpose (not a setting).
        ("scheduler_heartbeat", scheduler_heartbeat_job,
         IntervalTrigger(minutes=2, start_date=interval_anchor, timezone=tz), 60),
        # Storefront near-expiry reminders, daily at a fixed quiet hour (11:15 local; the
        # threshold in days is the `storefront_expiry_notify_days` setting, 0 = off).
        ("storefront_expiry", storefront_expiry_job,
         CronTrigger(hour=11, minute=15, timezone=tz), 6 * 3600),
        # Fleet-wide automatic monthly free-trial re-arm. A calendar event like the invoicing run,
        # so cron — and the same generous 12h grace, for the same reason: a missed monthly tick has
        # no next chance. Minute :25 is unused by every other cron job here at any owner-set hour.
        ("storefront_trial_reset", storefront_trial_reset_job,
         CronTrigger(day=_trial_reset_days(cfg.trial_reset_day), hour=cfg.trial_reset_hour,
                     minute=25, timezone=tz), 12 * 3600),
        # Daily reseller-traffic audit. Cron, not interval: it records "yesterday", a calendar
        # event. Minute :45 is free at EVERY owner-settable hour (:00 invoicing/dunning, :15
        # expiry, :25 trial reset, :30 maintenance/digest) — the cron analogue of the interval
        # jobs' distinct anchor offsets. Runs at the configured hour AND two hours later; the
        # second fire makes no panel calls once the day is stored.
        ("traffic_audit", traffic_audit_job,
         CronTrigger(hour=_traffic_audit_hours(cfg.traffic_audit_hour), minute=45, timezone=tz),
         6 * 3600),
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
