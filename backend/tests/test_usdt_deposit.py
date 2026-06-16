"""USDT (BEP-20) on-chain deposit reader via BSC JSON-RPC. Network is mocked."""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/usdt.db")
os.environ.setdefault("SECRET_KEY", "k")

import app.services.payments as P  # noqa: E402

WALLET = "0x" + "a" * 40
CONTRACT = "0x55d398326f99059ff775485246999027b3197955"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TXID = "0x" + "1" * 64


def _topic_addr(addr: str) -> str:  # ERC-20 indexed address = 32-byte left-padded
    return "0x" + "0" * 24 + addr[2:].lower()


class _Resp:
    def __init__(self, d):
        self._d = d

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


class _Client:
    def __init__(self, receipt, block="0x100"):
        self._receipt, self._block = receipt, block

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        method = (json or {}).get("method")
        if method == "eth_getTransactionReceipt":
            return _Resp({"result": self._receipt})
        if method == "eth_blockNumber":
            return _Resp({"result": self._block})
        return _Resp({})


def _patch(monkeypatch, receipt, block="0x100"):
    monkeypatch.setattr(P.httpx, "AsyncClient", lambda *a, **k: _Client(receipt, block))


def test_usdt_received_sums_transfers_to_us(monkeypatch):
    receipt = {"status": "0x1", "blockNumber": "0xf0", "logs": [
        {"address": CONTRACT, "topics": [TRANSFER, _topic_addr("0x" + "b" * 40), _topic_addr(WALLET)],
         "data": hex(5 * 10**18)},                                   # 5 USDT to us
        {"address": CONTRACT, "topics": [TRANSFER, _topic_addr(WALLET), _topic_addr("0x" + "c" * 40)],
         "data": hex(1 * 10**18)},                                   # 1 USDT FROM us → ignore
    ]}
    _patch(monkeypatch, receipt, block="0xf5")
    amt, confs = asyncio.run(P._usdt_received(TXID, WALLET, CONTRACT, "http://rpc"))
    assert amt == Decimal("5")
    assert confs == int("0xf5", 16) - int("0xf0", 16) + 1  # 6


def test_usdt_received_none_on_revert(monkeypatch):
    _patch(monkeypatch, {"status": "0x0", "blockNumber": "0xf0", "logs": []})
    assert asyncio.run(P._usdt_received(TXID, WALLET, CONTRACT, "http://rpc")) == (None, None)


def test_usdt_received_none_on_wrong_token(monkeypatch):
    receipt = {"status": "0x1", "blockNumber": "0xf0", "logs": [
        {"address": "0x" + "e" * 40, "topics": [TRANSFER, _topic_addr("0x" + "b" * 40), _topic_addr(WALLET)],
         "data": hex(5 * 10**18)}]}                                  # right amount, wrong contract
    _patch(monkeypatch, receipt)
    assert asyncio.run(P._usdt_received(TXID, WALLET, CONTRACT, "http://rpc")) == (None, None)


def test_usdt_received_none_on_network_error(monkeypatch):
    class _Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("rpc down")
    monkeypatch.setattr(P.httpx, "AsyncClient", lambda *a, **k: _Boom())
    assert asyncio.run(P._usdt_received(TXID, WALLET, CONTRACT, "http://rpc")) == (None, None)


def test_deposit_check_dispatches_by_chain(monkeypatch):
    async def fake_ton(s, p): return {"available": True, "received_ton": 1}
    async def fake_usdt(s, p): return {"available": True, "received_usdt": 1}
    monkeypatch.setattr(P, "ton_deposit_check", fake_ton)
    monkeypatch.setattr(P, "usdt_deposit_check", fake_usdt)

    ton = asyncio.run(P.deposit_check(None, SimpleNamespace(chain="ton", txid="x")))
    assert ton["kind"] == "ton"
    usdt = asyncio.run(P.deposit_check(None, SimpleNamespace(chain="bsc", txid="0xabc")))
    assert usdt["kind"] == "usdt"
    none = asyncio.run(P.deposit_check(None, SimpleNamespace(chain="bsc", txid="")))
    assert none["kind"] == "none"
