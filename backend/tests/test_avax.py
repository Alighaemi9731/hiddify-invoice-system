"""AVAX (Avalanche) payment currency: rate derivation (CoinGecko AVAX→USD × USDT→Toman), the
payment-options/instructions block, the shared submit path (manual confirm only), and the bot's
0x-hash disambiguation (AVAX shares BSC's hash format)."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal

import pytest  # noqa: E402

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/avax.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod  # noqa: E402
from app.services import payment_methods, rates, settings_service  # noqa: E402
from app.services import payments as payments_service

HASH = "0x" + "ab" * 32          # a well-formed 0x + 64-hex hash (BSC == AVAX format)
HASH_UC = "0x" + "AB" * 32


# ───────────────────────── _parse_txid disambiguation ─────────────────────────

def test_parse_snowtrace_url_is_avax():
    from app.bot.handlers import _parse_txid

    url = f"https://snowtrace.io/tx/{HASH_UC}"
    assert _parse_txid(url, usdt=True, ton=False, avax=True) == ("avax", HASH)  # lowercased


def test_parse_bare_hash_avax_only():
    from app.bot.handlers import _parse_txid

    assert _parse_txid(HASH_UC, usdt=False, ton=False, avax=True) == ("avax", HASH)


def test_parse_bare_hash_usdt_only_is_bsc():
    from app.bot.handlers import _parse_txid

    assert _parse_txid(HASH, usdt=True, ton=False, avax=False) == ("bsc", HASH)


def test_parse_bare_hash_both_enabled_is_ambiguous():
    from app.bot.handlers import _parse_txid

    chain, txid = _parse_txid(HASH, usdt=True, ton=False, avax=True)
    assert chain == "ambiguous" and txid == HASH


def test_parse_bscscan_url_still_bsc_even_with_avax():
    from app.bot.handlers import _parse_txid

    url = f"https://bscscan.com/tx/{HASH}"
    assert _parse_txid(url, usdt=True, ton=False, avax=True) == ("bsc", HASH)


def test_parse_snowtrace_url_rejected_when_avax_off():
    """An Avalanche-explorer link must NOT fall through to the bare-0x scanner and be mis-attributed
    to BSC when AVAX is disabled — reject it instead."""
    from app.bot.handlers import _parse_txid

    url = f"https://snowtrace.io/tx/{HASH}"
    assert _parse_txid(url, usdt=True, ton=False, avax=False) is None


def test_parse_bscscan_url_rejected_when_usdt_off():
    """Symmetric: a BSC-explorer link must not be mis-attributed to AVAX when USDT is disabled."""
    from app.bot.handlers import _parse_txid

    url = f"https://bscscan.com/tx/{HASH}"
    assert _parse_txid(url, usdt=False, ton=False, avax=True) is None


def test_parse_bare_64hex_not_ton_when_avax_enabled():
    """A bare 64-hex without 0x is only TON when NEITHER 0x-chain (USDT/AVAX) is enabled."""
    from app.bot.handlers import _parse_txid

    bare = "ab" * 32  # no 0x prefix, 64 hex
    assert _parse_txid(bare, usdt=False, ton=True, avax=True) is None
    assert _parse_txid(bare, usdt=False, ton=True, avax=False) == ("ton", bare)


# ───────────────────────────── rates ─────────────────────────────

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


def test_fetch_avax_usd(monkeypatch):
    monkeypatch.setattr(rates.httpx, "AsyncClient", _fake_client({"avalanche-2": {"usd": 6.34}}))
    assert asyncio.run(rates.fetch_avax_usd()) == 6.34


def test_fetch_avax_usd_none_on_missing(monkeypatch):
    monkeypatch.setattr(rates.httpx, "AsyncClient", _fake_client({"bitcoin": {"usd": 1}}))
    assert asyncio.run(rates.fetch_avax_usd()) is None


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def test_refresh_avax_rate_derives_and_guards(monkeypatch):
    async def run():
        engine, factory = await _session()
        try:
            async with factory() as s:
                # USDT→Toman = 100,000 (manual), AVAX→USD = 6 → AVAX→Toman = 600,000.
                await settings_service.set_value(s, "rate_mode", "manual")
                await settings_service.set_value(s, "toman_per_usdt", 100_000)

                monkeypatch.setattr(rates, "fetch_avax_usd", lambda: _coro(6.0))
                got = await rates.refresh_avax_rate(s)
                assert got == 600_000
                assert int(await settings_service.get(s, "avax_toman_auto", 0)) == 600_000

                # A wildly different next value (>3×) is rejected; the cached value stays.
                monkeypatch.setattr(rates, "fetch_avax_usd", lambda: _coro(60.0))  # → 6,000,000
                assert await rates.refresh_avax_rate(s) is None
                assert int(await settings_service.get(s, "avax_toman_auto", 0)) == 600_000

                # No USDT rate → cannot derive.
                await settings_service.set_value(s, "toman_per_usdt", 0)
                monkeypatch.setattr(rates, "fetch_avax_usd", lambda: _coro(6.0))
                assert await rates.refresh_avax_rate(s) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_get_avax_toman_manual_auto_split():
    async def run():
        engine, factory = await _session()
        try:
            async with factory() as s:
                import datetime as _dt
                await settings_service.set_value(s, "avax_toman_manual", 500_000)
                await settings_service.set_value(s, "avax_toman_auto", 620_000)
                # A FRESH cache stamp so the H09 staleness guard accepts the auto rate.
                await settings_service.set_value(
                    s, "avax_toman_auto_at",
                    _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))

                await settings_service.set_value(s, "avax_rate_mode", "auto")
                assert await rates.get_avax_toman(s) == 620_000

                await settings_service.set_value(s, "avax_rate_mode", "manual")
                assert await rates.get_avax_toman(s) == 500_000

                # auto with no cached live rate → falls back to manual
                await settings_service.set_value(s, "avax_rate_mode", "auto")
                await settings_service.set_value(s, "avax_toman_auto", 0)
                assert await rates.get_avax_toman(s) == 500_000
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _coro(v):
    return v


# ───────────────────────── payment options & instructions ─────────────────────────

@pytest.mark.real_pay_options
def test_load_options_avax_gating():
    async def run():
        engine, factory = await _session()
        try:
            async with factory() as s:
                # enabled but no address → not available
                await settings_service.set_value(s, "pay_avax_enabled", True)
                opts = await payment_methods.load_options(s)
                assert opts.avax is False
                # with an address → available
                await settings_service.set_value(s, "avax_address", "0xWalletAvax")
                opts = await payment_methods.load_options(s)
                assert opts.avax is True and opts.avax_address == "0xWalletAvax"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_avax_rate_mode_is_validated():
    """The settings API rejects a bad avax_rate_mode (parity with rate_mode/ton_rate_mode)."""
    import pytest

    assert settings_service.validate_api_value("avax_rate_mode", "manual") == "manual"
    assert settings_service.validate_api_value("avax_rate_mode", "auto") == "auto"
    with pytest.raises(ValueError):
        settings_service.validate_api_value("avax_rate_mode", "banana")


def test_instructions_text_has_avax_block():
    opts = payment_methods.PaymentOptions(
        usdt=False, screenshot=False, card=False, ton=False, avax=True,
        wallet="", card_number="", card_holder="", ton_address="", avax_address="0xWalletAvax",
    )
    txt = payment_methods.instructions_text(opts, amount_avax="1.2345", amount_toman="600,000")
    assert "AVAX" in txt and "0xWalletAvax" in txt
    assert "1.2345 AVAX" in txt
    assert "Avalanche C-Chain" in txt


# ───────────────────────── submit / validate / verify ─────────────────────────

async def _seed_payable(factory):
    async with factory() as s:
        panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="a", name="ali", bot_chat_id=999)
        s.add(r)
        await s.flush()
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id, period_start=dt.date(2026, 6, 1),
            period_end=dt.date(2026, 6, 30), period_label="2026-06",
            usage_gb=10, amount_toman=600_000, amount_usdt=4, status=InvoiceStatus.sent,
        )
        s.add(inv)
        await s.commit()
        return r.id, inv.id


def test_submit_avax_lowercases_and_sets_method():
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)
            async with factory() as s:
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, txid=HASH_UC, chain="avax")
                assert res.status == "ok"
                assert res.payment.method == PaymentMethod.avax_txid
                assert res.payment.chain == "avax"
                assert res.payment.txid == HASH  # lowercased
                assert "AVAX" in res.user_message
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_submit_avax_rejects_bad_hash():
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)
            async with factory() as s:
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, txid="not-a-hash", chain="avax")
                assert res.status == "invalid_txid"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verify_holds_avax_for_manual():
    """The on-chain read is a display aid only — verify_payment must still HOLD AVAX for the owner's
    manual decision (never auto-confirm, never a BscScan lookup)."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)
            async with factory() as s:
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, txid=HASH, chain="avax")
                pid = res.payment.id
            async with factory() as s:
                r = await payments_service.verify_payment(s, pid)
                assert r.status == "pending" and r.paid is False
        finally:
            await engine.dispose()

    asyncio.run(run())


# ───────────────────────── on-chain deposit read (display-only) ─────────────────────────

def _fake_rpc(*, to, value_wei, status="0x1", tx_block=100, latest=112):
    """A fake Avalanche JSON-RPC client: answers getTransactionByHash / getTransactionReceipt /
    blockNumber by the request's `method`."""
    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None):
            m = (json or {}).get("method")
            if m == "eth_getTransactionByHash":
                return _Resp({"result": {"to": to, "value": hex(value_wei),
                                         "blockNumber": hex(tx_block)}})
            if m == "eth_getTransactionReceipt":
                return _Resp({"result": {"status": status, "blockNumber": hex(tx_block)}})
            if m == "eth_blockNumber":
                return _Resp({"result": hex(latest)})
            return _Resp({"result": None})

    return FakeClient


def test_avax_received_reads_native_transfer(monkeypatch):
    monkeypatch.setattr(payments_service.httpx, "AsyncClient",
                        _fake_rpc(to="0xWALLET", value_wei=2 * 10**18, tx_block=100, latest=112))
    recv, confs = asyncio.run(payments_service._avax_received(HASH, "0xwallet", "http://rpc"))
    assert recv == Decimal(2) and confs == 13  # 112 - 100 + 1


def test_avax_received_wrong_recipient(monkeypatch):
    monkeypatch.setattr(payments_service.httpx, "AsyncClient",
                        _fake_rpc(to="0xSOMEONE_ELSE", value_wei=2 * 10**18))
    recv, confs = asyncio.run(payments_service._avax_received(HASH, "0xwallet", "http://rpc"))
    assert recv is None and confs is None


def test_avax_received_reverted_tx(monkeypatch):
    monkeypatch.setattr(payments_service.httpx, "AsyncClient",
                        _fake_rpc(to="0xwallet", value_wei=10**18, status="0x0"))
    recv, _ = asyncio.run(payments_service._avax_received(HASH, "0xwallet", "http://rpc"))
    assert recv is None  # a reverted tx credited nothing


def test_avax_deposit_check_and_dispatch(monkeypatch):
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)  # invoice amount_toman = 600_000
            async with factory() as s:
                await settings_service.set_value(s, "avax_address", "0xWallet")
                await settings_service.set_value(s, "avax_rate_mode", "manual")
                await settings_service.set_value(s, "avax_toman_manual", 1_000_000)  # 1 AVAX = 1M T
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, txid=HASH, chain="avax")
                pid = res.payment.id
            # 0.6 AVAX × 1,000,000 = 600,000 T == the invoice → match; never touches the network.
            monkeypatch.setattr(payments_service, "_avax_received",
                                lambda *a, **k: _coro((Decimal("0.6"), 12)))
            async with factory() as s:
                p = await s.get(Payment, pid)
                d = await payments_service.avax_deposit_check(s, p)
                assert d["available"] and d["received_avax"] == 0.6
                assert d["received_toman"] == 600_000 and d["match"] is True
                assert d["confirmations"] == 12
                # the dispatcher tags kind="avax"
                assert (await payments_service.deposit_check(s, p))["kind"] == "avax"
        finally:
            await engine.dispose()

    asyncio.run(run())
