"""
Background scheduler (APScheduler). Jobs are registered here:

  * monthly invoice generation + delivery  (added in M3/M4)
  * daily dunning + enforcement            (added in M6)
  * periodic panel sync                    (added in M2)

For the MVP the scheduler runs inside the `backend` process (RUN_SCHEDULER=true).
Each job opens its own DB session and is wrapped so a failure never kills the loop.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("scheduler")

# Calendar jobs and deterministic interval anchors use the OWNER's local clock. Iran has
# no DST, so this is a stable UTC+3:30. Overridable for deployments in another timezone.
SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_TIMEZONE", "Asia/Tehran")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
    return _scheduler


async def start() -> None:
    sched = get_scheduler()
    await _register_jobs(sched)
    sched.start()
    # These control jobs are fixed, owned only by this process, and never rescheduled
    # by editable settings. API processes communicate through the shared database.
    sched.add_job(refresh_schedule, "interval", seconds=15, id="schedule_refresh",
                  coalesce=True, max_instances=1, misfire_grace_time=30)
    from app.scheduler.jobs import monthly_invoicing_job

    sched.add_job(monthly_invoicing_job, "cron", second=37, id="monthly_recovery",
                  coalesce=True, max_instances=1, misfire_grace_time=60)
    await refresh_schedule()
    log.info("Scheduler started with %d job(s).", len(sched.get_jobs()))
    # Stamp the heartbeat immediately so /health doesn't report a stale value left over
    # from before a restart while the first interval tick is still up to 2 minutes away.
    try:
        from app.scheduler.jobs import scheduler_heartbeat_job

        await scheduler_heartbeat_job()
    except Exception:  # noqa: BLE001 - the liveness stamp must never affect boot
        log.exception("initial scheduler heartbeat stamp failed")


async def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def apply_settings(session) -> bool:
    """Re-read the owner-configured timings and live-update the running jobs' triggers.
    Called from the settings API after a schedule-affecting change so edits take effect
    immediately (no restart). No-op (returns False) if the scheduler isn't running here —
    e.g. in the separate bot process — so it's safe to call from anywhere."""
    if _scheduler is None or not _scheduler.running:
        return False
    from app.scheduler import jobs

    cfg = await jobs.load_config(session)
    jobs.register(_scheduler, cfg, update_existing=True)
    await _publish_state(session, cfg)
    return True


async def _publish_state(session, cfg) -> None:
    from app.models import Setting
    from app.services import settings_service

    assert _scheduler is not None
    now = datetime.now(timezone.utc).isoformat()
    previous = await session.get(Setting, "scheduler_runtime_state", populate_existing=True)
    old = previous.value if previous is not None and isinstance(previous.value, dict) else {}
    config = asdict(cfg)
    value = {
        "config": config, "checked_at": now,
        "applied_at": old.get("applied_at", now) if old.get("config") == config else now,
        "jobs": {job.id: job.next_run_time.isoformat() if job.next_run_time else None
                 for job in _scheduler.get_jobs()},
        "timezone": str(_scheduler.timezone),
    }
    await settings_service.set_value(session, "scheduler_runtime_state", value)


async def refresh_schedule() -> None:
    """Reconcile committed DB settings with this worker, then publish actual next runs."""
    from app.core.db import SessionLocal

    try:
        async with SessionLocal() as session:
            await apply_settings(session)
    except Exception:
        # Keep the last working schedule. Its stale checked_at is visible through the API.
        log.exception("Scheduler settings reconciliation failed")


async def _register_jobs(sched: AsyncIOScheduler) -> None:
    """Register jobs with the owner-configured timings. Imported lazily to avoid circular
    imports during startup; never lets a failure crash boot."""
    try:
        from app.core.db import SessionLocal
        from app.scheduler import jobs

        async with SessionLocal() as session:
            cfg = await jobs.load_config(session)
        jobs.register(sched, cfg)
    except Exception:  # pragma: no cover - never let registration crash boot
        log.exception("Failed to register scheduler jobs")
        # Fall back to defaults so the jobs still run on a sane schedule.
        try:
            from app.scheduler import jobs

            jobs.register(sched)
        except Exception:
            log.exception("Fallback job registration also failed")
