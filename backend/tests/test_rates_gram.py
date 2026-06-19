"""The Gram (ex-Toncoin) rate fetch reads Wallex's GRAMTMN market (TON→GRAM rebrand, 2026-06-15),
falling back to the old TONTMN symbol if present."""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/rates.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.services import rates  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


def _fake_client(data):
    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return _Resp(data)
    return FakeClient


def _symbols(data):
    return {"result": {"symbols": data}}


def test_fetch_uses_gramtmn(monkeypatch):
    monkeypatch.setattr(rates.httpx, "AsyncClient", _fake_client(_symbols({
        "GRAMTMN": {"stats": {"bidPrice": "255500"}},
        "USDTTMN": {"stats": {"bidPrice": "60000"}},
    })))
    assert asyncio.run(rates.fetch_ton_toman()) == 255500


def test_fetch_falls_back_to_tontmn(monkeypatch):
    monkeypatch.setattr(rates.httpx, "AsyncClient", _fake_client(_symbols({
        "TONTMN": {"stats": {"bidPrice": "240000"}},
    })))
    assert asyncio.run(rates.fetch_ton_toman()) == 240000


def test_fetch_none_when_neither_present(monkeypatch):
    monkeypatch.setattr(rates.httpx, "AsyncClient", _fake_client(_symbols({
        "USDTTMN": {"stats": {"bidPrice": "60000"}},
    })))
    assert asyncio.run(rates.fetch_ton_toman()) is None
