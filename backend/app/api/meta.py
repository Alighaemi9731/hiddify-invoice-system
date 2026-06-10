"""Health / version endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app import __version__
from app.core.config import settings
from app.core.db import SessionLocal

router = APIRouter(tags=["meta"])
log = logging.getLogger("api.meta")


@router.get("/health")
async def health() -> dict:
    """Readiness probe: the API is ready only when its database is reachable."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any database outage must become a 503
        log.warning("database readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok", "version": __version__}


@router.get("/api/info")
async def info() -> dict:
    return {
        "name": "Hiddify Reseller Invoicing System",
        "version": __version__,
        "env": settings.app_env,
    }
