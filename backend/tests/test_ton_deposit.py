"""TON deposit reader (decision aid for manual confirmation). Network is mocked."""
import asyncio
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/ton.db")
os.environ.setdefault("SECRET_KEY", "k")

import app.services.payments as P  # noqa: E402

OUR = "0:" + "a" * 64  # raw TON address form


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


class _Client:
    def __init__(self, data):
        self._d = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        return _Resp(self._d)


def _patch(monkeypatch, data):
    monkeypatch.setattr(P.httpx, "AsyncClient", lambda *a, **k: _Client(data))


def test_ton_received_sums_only_incoming_to_our_address(monkeypatch):
    data = {"transactions": [
        {"in_msg": {"destination": OUR, "value": "2500000000"}},          # 2.5 TON to us
        {"in_msg": {"destination": "0:" + "b" * 64, "value": "9000000000"}},  # to someone else
    ]}
    _patch(monkeypatch, data)
    got = asyncio.run(P._ton_received("hash", OUR))
    assert got == Decimal("2.5")


def test_ton_received_none_when_no_match(monkeypatch):
    _patch(monkeypatch, {"transactions": [
        {"in_msg": {"destination": "0:" + "c" * 64, "value": "1000000000"}}]})
    assert asyncio.run(P._ton_received("hash", OUR)) is None


def test_ton_received_none_on_network_error(monkeypatch):
    class _Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("toncenter down")
    monkeypatch.setattr(P.httpx, "AsyncClient", lambda *a, **k: _Boom())
    assert asyncio.run(P._ton_received("hash", OUR)) is None


def test_ton_account_id_equates_raw_and_friendly_forms():
    # raw "0:hex" → its hex account id; gibberish → ""
    assert P._ton_account_id("0:" + "A" * 64) == "a" * 64
    assert P._ton_account_id("") == ""
