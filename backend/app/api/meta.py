"""Health / version endpoints."""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core import errortrack
from app.core.config import settings
from app.core.db import SessionLocal

router = APIRouter(tags=["meta"])
log = logging.getLogger("api.meta")

# The heartbeat job stamps every ~2 minutes; well past that means the scheduler is dead
# (or the whole process just restarted — boot re-stamps immediately to avoid a false alarm).
_STALE_AFTER = dt.timedelta(minutes=10)


async def _scheduler_state(session: AsyncSession) -> str:
    """`ok` | `stale`. Stale means jobs (invoicing, dunning, backups) are silently not
    firing even though the API answers — the exact failure a plain DB probe can't see."""
    try:
        from app.services import settings_service

        raw = str(await settings_service.get(session, "scheduler_last_heartbeat", "") or "")
        ts = errortrack.parse_ts(raw)
        if ts is None:
            return "stale"
        return "ok" if dt.datetime.now(dt.timezone.utc) - ts <= _STALE_AFTER else "stale"
    except Exception:  # noqa: BLE001 - the DB is up (SELECT 1 passed); don't turn this into a 503
        log.warning("scheduler heartbeat check failed", exc_info=True)
        return "stale"


@router.get("/health")
async def health() -> dict:
    """Readiness + liveness probe. Returns 503 ONLY on a database outage (the compose
    healthcheck `curl -fsS` and deploy/smoke.sh key off that); every other problem is
    reported as `status: degraded` INSIDE a 200 response with `database` kept intact."""
    scheduler_state = ""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            # The heartbeat lives in the settings table — global state written by
            # whichever process runs the scheduler. Since Wave 5 that is a separate
            # container, so EVERY process reports it (previously gated on run_scheduler).
            scheduler_state = await _scheduler_state(session)
    except Exception as exc:  # noqa: BLE001 - any database outage must become a 503
        log.warning("database readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    out = {
        "status": "degraded" if scheduler_state == "stale" else "ok",
        "database": "ok",
        "version": __version__,
        "errors_24h": errortrack.recent_total(),
    }
    if scheduler_state:
        out["scheduler"] = scheduler_state
    return out


@router.get("/api/info")
async def info() -> dict:
    return {
        "name": "Hiddify Reseller Invoicing System",
        "version": __version__,
        "env": settings.app_env,
    }
