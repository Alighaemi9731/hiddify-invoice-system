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

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
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
