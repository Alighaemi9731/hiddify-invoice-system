"""Health / version endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.core.config import settings

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/api/info")
async def info() -> dict:
    return {
        "name": "Hiddify Reseller Invoicing System",
        "version": __version__,
        "env": settings.app_env,
    }
