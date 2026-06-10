import asyncio

import pytest
from fastapi import HTTPException

from app.api import meta


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


def test_health_checks_database(monkeypatch):
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session())
    result = asyncio.run(meta.health())
    assert result["status"] == "ok"
    assert result["database"] == "ok"


def test_health_returns_503_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(meta, "SessionLocal", lambda: _Session(RuntimeError("db down")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(meta.health())
    assert exc.value.status_code == 503
    assert exc.value.detail == "database unavailable"
