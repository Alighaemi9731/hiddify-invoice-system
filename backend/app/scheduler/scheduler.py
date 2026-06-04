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

# All cron jobs fire on round hours in the OWNER's local clock (Iran-based business; the
# frontend already renders times in Asia/Tehran). Iran has no DST, so this is a stable
# UTC+3:30 — e.g. the every-2h backup fires at 00,02,…,22:00 Tehran. Overridable via the
# SCHEDULER_TIMEZONE env var if ever deployed elsewhere.
SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_TIMEZONE", "Asia/Tehran")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
    return _scheduler


async def start() -> None:
    sched = get_scheduler()
    _register_jobs(sched)
    sched.start()
    log.info("Scheduler started with %d job(s).", len(sched.get_jobs()))


async def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _register_jobs(sched: AsyncIOScheduler) -> None:
    """Register jobs. Imported lazily to avoid circular imports during startup."""
    try:
        from app.scheduler import jobs

        jobs.register(sched)
    except Exception:  # pragma: no cover - never let registration crash boot
        log.exception("Failed to register scheduler jobs")
