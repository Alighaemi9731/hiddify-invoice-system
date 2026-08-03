"""Automatic on-chain confirmation of a submitted TXID.

The rule the owner asked for: a deposit that matches the invoice EXACTLY (same tolerances the
review message already shows), is confirmed deeply enough, and is RECENT, settles itself the
moment the reseller submits it. Everything else — different amount (under OR over), too few
confirmations, an old transaction, an unreadable chain, a screenshot proof — must be left
completely untouched as `pending` for the owner's manual decision, exactly as before.

These tests drive the real `deposit_check` logic (amount comparison, rate conversion, tolerance)
and mock only the network readers.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/autoconfirm.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import FinancialRecord, Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentStatus  # noqa: E402
from app.services import payments as P  # noqa: E402
from app.services import settings_service  # noqa: E402

BSC_HASH = "0x" + "ab" * 32
AVAX_HASH = "0x" + "cd" * 32
TON_HASH = "de" * 32

# The invoice every test settles: 600,000 T == 4 USDT.
INV_TOMAN = 600_000
INV_USDT = 4


def _now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _hours_ago(h: float) -> int:
    return _now_ts() - int(h * 3600)


async def _coro(v):
    return v


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_payable(factory, key: str = "p"):
    async with factory() as s:
        panel = Panel(key=key, host=f"{key}.invalid", proxy_path_enc="x", owner_uuid=key)
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid=key, name="ali", bot_chat_id=999)
        s.add(r)
        await s.flush()
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id, period_start=dt.date(2026, 6, 1),
            period_end=dt.date(2026, 6, 30), period_label="2026-06",
            usage_gb=10, amount_toman=INV_TOMAN, amount_usdt=INV_USDT,
            status=InvoiceStatus.sent,
        )
        s.add(inv)
        await s.commit()
        return r.id, inv.id


async def _submit(factory, *, txid: str, chain: str) -> tuple[int, int, int]:
    """Seed an owed invoice and submit `txid` against it. Returns (reseller, invoice, payment)."""
    rid, iid = await _seed_payable(factory)
    async with factory() as s:
        res = await P.submit_reseller_payment(
            s, reseller_ids={rid}, invoice_id=iid, txid=txid, chain=chain)
        assert res.status == "ok"
        return rid, iid, res.payment.id


async def _state(factory, pid: int, iid: int) -> tuple[Payment, Invoice]:
    async with factory() as s:
        return await s.get(Payment, pid), await s.get(Invoice, iid)


def _patch_usdt(monkeypatch, *, usdt: str, confs: int | None = 20, age_h: float = 1.0):
    monkeypatch.setattr(
        P, "_usdt_received",
        lambda *a, **k: _coro((Decimal(usdt), confs, _hours_ago(age_h))))


# ─────────────────────────── the pure verdict function ───────────────────────────

def _chk(**over) -> dict:
    base = {"available": True, "kind": "usdt", "match": True,
            "confirmations": 20, "tx_age_hours": 1.0}
    base.update(over)
    return base


def test_verdict_accepts_an_exact_fresh_confirmed_match():
    assert P._auto_confirm_verdict(
        _chk(), min_confirmations=12, max_age_hours=24) == "matched"


def test_verdict_rejects_each_failed_gate():
    v = lambda c: P._auto_confirm_verdict(c, min_confirmations=12, max_age_hours=24)  # noqa: E731
    assert v(_chk(available=False)) == "unavailable"
    assert v(_chk(kind="none")) == "unavailable"
    assert v(_chk(match=False)) == "amount_mismatch"
    assert v(_chk(match=None)) == "zero_amount"
    assert v(_chk(confirmations=3)) == "low_confirmations"
    assert v(_chk(confirmations=None)) == "low_confirmations"
    assert v(_chk(tx_age_hours=None)) == "unknown_age"
    assert v(_chk(tx_age_hours=48.0)) == "too_old"


def test_verdict_skips_confirmation_depth_for_ton():
    """TON has no confirmation count — a transaction toncenter returns is already committed."""
    chk = _chk(kind="ton", confirmations=None)
    assert P._auto_confirm_verdict(chk, min_confirmations=12, max_age_hours=24) == "matched"


def test_verdict_still_ages_a_ton_transaction():
    chk = _chk(kind="ton", confirmations=None, tx_age_hours=99.0)
    assert P._auto_confirm_verdict(chk, min_confirmations=12, max_age_hours=24) == "too_old"


# ─────────────────────────── the happy path, per chain ───────────────────────────

def test_usdt_exact_match_settles_everything(monkeypatch):
    """The full settlement: payment confirmed + stamped, invoice paid, ledger written."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            _patch_usdt(monkeypatch, usdt="4")
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is True and out.reason == "matched"

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.confirmed
            assert pay.verified_at is not None
            assert P._AUTO_CONFIRM_TAG in (pay.note or "")
            assert pay.confirmations == 20        # chain evidence kept for the owner's audit
            assert "received_usdt" in (pay.raw_json or "")
            assert inv.status == InvoiceStatus.paid and inv.paid_at is not None

            async with factory() as s:
                rec = (await s.execute(
                    select(FinancialRecord).where(FinancialRecord.invoice_id == iid)
                )).scalars().first()
            assert rec is not None and rec.txid == BSC_HASH
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ton_exact_match_confirms(monkeypatch):
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=TON_HASH, chain="ton")
            async with factory() as s:
                await settings_service.set_value(s, "ton_wallet_address", "0:" + "a" * 64)
                await settings_service.set_value(s, "ton_rate_mode", "manual")
                await settings_service.set_value(s, "ton_toman_manual", 100_000)
            # 6 GRAM × 100,000 = 600,000 T == the invoice.
            monkeypatch.setattr(
                P, "_ton_received", lambda *a, **k: _coro((Decimal("6"), _hours_ago(2))))
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is True

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.confirmed
            assert inv.status == InvoiceStatus.paid
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_avax_exact_match_confirms(monkeypatch):
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=AVAX_HASH, chain="avax")
            async with factory() as s:
                await settings_service.set_value(s, "avax_address", "0xwalletavax")
                await settings_service.set_value(s, "avax_rate_mode", "manual")
                await settings_service.set_value(s, "avax_toman_manual", 1_000_000)
            # 0.6 AVAX × 1,000,000 = 600,000 T == the invoice.
            monkeypatch.setattr(
                P, "_avax_received",
                lambda *a, **k: _coro((Decimal("0.6"), 20, _hours_ago(1))))
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is True

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.confirmed
            assert inv.status == InvoiceStatus.paid
        finally:
            await engine.dispose()

    asyncio.run(run())


# ─────────────────────────── everything else stays manual ───────────────────────────

def _expect_left_pending(monkeypatch, reason: str, *, patch, setting=None):
    """Run one declining scenario and assert NOTHING was touched."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            if setting:
                async with factory() as s:
                    await settings_service.set_value(s, *setting)
            patch(monkeypatch)
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is False
            assert out.reason == reason, f"expected {reason}, got {out.reason}"

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.pending
            assert pay.verified_at is None
            assert P._AUTO_CONFIRM_TAG not in (pay.note or "")
            assert inv.status == InvoiceStatus.sent and inv.paid_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_underpayment_beyond_tolerance_stays_pending(monkeypatch):
    _expect_left_pending(
        monkeypatch, "amount_mismatch",
        patch=lambda mp: _patch_usdt(mp, usdt="3"))          # 4 owed, 3 paid


def test_overpayment_beyond_tolerance_stays_pending(monkeypatch):
    """The owner explicitly chose to review a too-large deposit by hand — it usually means the
    TXID belongs to a different, bigger transfer."""
    _expect_left_pending(
        monkeypatch, "amount_mismatch",
        patch=lambda mp: _patch_usdt(mp, usdt="40"))


def test_amount_within_tolerance_still_confirms(monkeypatch):
    """±0.5 USDT is the configured tolerance and must stay usable — the guard is not exact-equality."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            _patch_usdt(monkeypatch, usdt="3.7")             # 0.3 short, within ±0.5
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_too_few_confirmations_stays_pending(monkeypatch):
    _expect_left_pending(
        monkeypatch, "low_confirmations",
        patch=lambda mp: _patch_usdt(mp, usdt="4", confs=2))


def test_old_transaction_stays_pending(monkeypatch):
    """The anti-hijack guard: our wallet is public, so an OLD unclaimed deposit must not settle
    someone's debt automatically."""
    _expect_left_pending(
        monkeypatch, "too_old",
        patch=lambda mp: _patch_usdt(mp, usdt="4", age_h=72))


def test_unknown_block_time_stays_pending(monkeypatch):
    _expect_left_pending(
        monkeypatch, "unknown_age",
        patch=lambda mp: monkeypatch.setattr(
            P, "_usdt_received", lambda *a, **k: _coro((Decimal("4"), 20, None))))


def test_unreadable_chain_stays_pending(monkeypatch):
    _expect_left_pending(
        monkeypatch, "unavailable",
        patch=lambda mp: monkeypatch.setattr(
            P, "_usdt_received", lambda *a, **k: _coro((None, None, None))))


def test_unexpected_error_stays_pending_without_raising(monkeypatch):
    """A dead node is already swallowed by the readers themselves; this covers the outer promise:
    ANY unexpected failure degrades to 'leave it for the owner' and never breaks the submission."""
    async def _boom(*a, **k):
        raise RuntimeError("rpc down")

    _expect_left_pending(
        monkeypatch, "error",
        patch=lambda mp: monkeypatch.setattr(P, "_usdt_received", _boom))


def test_disabled_setting_stays_pending(monkeypatch):
    _expect_left_pending(
        monkeypatch, "disabled",
        patch=lambda mp: _patch_usdt(mp, usdt="4"),
        setting=("payment_auto_confirm_enabled", False))


def test_zero_amount_invoice_stays_pending(monkeypatch):
    """No comparable total → nothing to match against; never auto-confirm on a zero invoice."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            async with factory() as s:
                inv = await s.get(Invoice, iid)
                inv.amount_usdt = 0
                await s.commit()
            _patch_usdt(monkeypatch, usdt="4")
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is False and out.reason == "zero_amount"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_screenshot_proof_is_never_auto_confirmed(monkeypatch):
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)
            async with factory() as s:
                res = await P.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, screenshot=True)
                pid = res.payment.id
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is False and out.reason == "no_txid"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_owner_decision_wins_over_a_late_auto_confirm(monkeypatch):
    """If the owner rejected it first, the automatic path must not quietly re-confirm it."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            async with factory() as s:
                await P.reject_payment(s, pid)
            _patch_usdt(monkeypatch, usdt="4")
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is False and out.reason == "not_pending"

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.rejected
            assert inv.status == InvoiceStatus.sent
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canceled_invoice_blocks_the_settlement(monkeypatch):
    """`_confirm_payment`'s guards apply to the automatic path too: a set member that can't be
    settled leaves the payment pending instead of marking it confirmed against nothing."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid, pid = await _submit(factory, txid=BSC_HASH, chain="bsc")
            async with factory() as s:
                inv = await s.get(Invoice, iid)
                inv.status = InvoiceStatus.canceled
                await s.commit()
            _patch_usdt(monkeypatch, usdt="4")
            async with factory() as s:
                out = await P.try_auto_confirm(s, pid)
            assert out.confirmed is False and out.reason == "not_settled"

            pay, inv = await _state(factory, pid, iid)
            assert pay.status == PaymentStatus.pending
            assert inv.status == InvoiceStatus.canceled
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_submit_message_promises_the_automatic_check():
    """The reseller must not be told to «wait for support» right before an instant confirmation."""
    async def run():
        engine, factory = await _session()
        try:
            rid, iid = await _seed_payable(factory)
            async with factory() as s:
                res = await P.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=iid, txid=BSC_HASH, chain="bsc")
                assert "بررسی خودکار" in res.user_message

                await settings_service.set_value(s, "payment_auto_confirm_enabled", False)
            rid2, iid2 = await _seed_payable(factory, key="p2")
            async with factory() as s:
                res2 = await P.submit_reseller_payment(
                    s, reseller_ids={rid2}, invoice_id=iid2,
                    txid="0x" + "ef" * 32, chain="bsc")
                assert "بررسی خودکار" not in res2.user_message
                assert "پشتیبانی" in res2.user_message
        finally:
            await engine.dispose()

    asyncio.run(run())
