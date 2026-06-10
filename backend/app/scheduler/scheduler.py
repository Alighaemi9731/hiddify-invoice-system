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
    log.info("Scheduler started with %d job(s).", len(sched.get_jobs()))


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
    jobs.register(_scheduler, cfg)  # replace_existing=True rewrites each trigger in place
    return True


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
