import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import meta
from app.services import settings_service


class _Session:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        if self.error:
            raise self.error
        return 1


def _stamp(minutes_ago: int) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")


def _with_scheduler(monkeypatch, stamp: str) -> None:
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(meta, "settings", SimpleNamespace(run_scheduler=True))

    async def _get(_session, _key, default=None):
        return stamp or default

    monkeypatch.setattr(settings_service, "get", _get)


def test_health_checks_database(monkeypatch):
    # Wave 5: the scheduler runs in its own container, so EVERY process reports the
    # shared heartbeat. No heartbeat recorded → scheduler stale → degraded (DB still ok,
    # still HTTP 200 — the container healthcheck keys off the 503-on-db-outage only).
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session())

    async def _get(_session, _key, default=None):
        return "" or default

    monkeypatch.setattr(settings_service, "get", _get)
    result = asyncio.run(meta.health())
    assert result["status"] == "degraded"
    assert result["database"] == "ok"
    assert result["scheduler"] == "stale"
    assert "errors_24h" in result


def test_health_ok_with_fresh_heartbeat_regardless_of_process_role(monkeypatch):
    # A fresh heartbeat written by the scheduler CONTAINER must read as ok from the API
    # process too (run_scheduler is False there since Wave 5).
    _with_scheduler(monkeypatch, _stamp(minutes_ago=1))
    result = asyncio.run(meta.health())
    assert result["status"] == "ok"
    assert result["scheduler"] == "ok"


def test_health_returns_503_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session(RuntimeError("db down")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(meta.health())
    assert exc.value.status_code == 503
    assert exc.value.detail == "database unavailable"


def test_health_scheduler_ok_when_heartbeat_fresh(monkeypatch):
    _with_scheduler(monkeypatch, _stamp(minutes_ago=1))
    result = asyncio.run(meta.health())
    assert result["status"] == "ok"
    assert result["scheduler"] == "ok"


def test_health_degrades_on_stale_heartbeat_but_stays_200(monkeypatch):
    _with_scheduler(monkeypatch, _stamp(minutes_ago=30))
    result = asyncio.run(meta.health())  # returns (200), does NOT raise
    assert result["status"] == "degraded"
    assert result["scheduler"] == "stale"
    # deploy/smoke.sh greps '"database":"ok"' — a degraded scheduler must never break it.
    assert result["database"] == "ok"


def test_health_missing_heartbeat_counts_as_stale(monkeypatch):
    _with_scheduler(monkeypatch, "")
    result = asyncio.run(meta.health())
    assert result["status"] == "degraded"
    assert result["scheduler"] == "stale"


def test_health_settings_failure_degrades_not_503(monkeypatch):
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(meta, "settings", SimpleNamespace(run_scheduler=True))

    async def _get(_session, _key, default=None):
        raise RuntimeError("settings table broken")

    monkeypatch.setattr(settings_service, "get", _get)
    result = asyncio.run(meta.health())  # DB is up → still 200
    assert result["status"] == "degraded"
    assert result["database"] == "ok"


def test_health_reports_error_counter(monkeypatch):
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(meta.errortrack, "recent_total", lambda: 7)
    result = asyncio.run(meta.health())
    assert result["errors_24h"] == 7
