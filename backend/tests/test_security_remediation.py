"""Security remediation — Batch 1.

F7 (SQLite, real payment-method policy): submit_reseller_payment must reject a method the owner
has DISABLED or left UNCONFIGURED, on the shared path used by both the bot and the portal.

F6 (Postgres pg_contract, row locking): confirming a payment and canceling its invoice must
serialize on the invoice row lock — the DB may never end up with a CONFIRMED payment attached to
a CANCELED invoice (or a silently-dropped cancel). SQLite has no FOR UPDATE, so this only asserts
on real Postgres via the CI backend-postgres job.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secrem.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402
from app.services import payments, settings_service  # noqa: E402
from tests.pg_barrier import make_engine, requires_pg, run_two  # noqa: E402

_TX = "0x" + "a" * 64
S = InvoiceStatus


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _sent_invoice(reseller_id, panel_id=1, label="2026-03"):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(
        reseller_id=reseller_id, panel_id=panel_id, period_start=start, period_end=end,
        period_label=label, usage_gb=10, amount_toman=10000, amount_usdt=1,
        status=S.sent, sent_at=dt.datetime.now(dt.timezone.utc),
    )


# ─────────────────────────────── F7: method policy ───────────────────────────────
@pytest.mark.real_pay_options
def test_f7_disabled_method_is_rejected(tmp_path):
    async def body(s):
        r = Reseller(panel_id=1, admin_uuid="u", name="R")
        s.add(r)
        await s.flush()
        inv = _sent_invoice(r.id)
        s.add(inv)
        # Only USDT is enabled+configured; TON is off.
        await settings_service.set_value(s, "pay_usdt_enabled", True)
        await settings_service.set_value(s, "usdt_bep20_address", "0xwallet")
        await s.commit()
        # Submitting a TON payment (disabled) must be refused, not stored.
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid="a" * 64, chain="ton")
        assert res.status == "not_payable"
        assert res.payment is None
        # USDT (enabled + configured) still works.
        ok = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=_TX, chain="bsc")
        assert ok.status == "ok" and ok.payment is not None
    _run(body, tmp_path, "f7a.db")


@pytest.mark.real_pay_options
def test_f7_enabled_but_unconfigured_destination_is_rejected(tmp_path):
    async def body(s):
        r = Reseller(panel_id=1, admin_uuid="u", name="R")
        s.add(r)
        await s.flush()
        inv = _sent_invoice(r.id)
        s.add(inv)
        # USDT enabled but NO wallet address → load_options treats it as unavailable.
        await settings_service.set_value(s, "pay_usdt_enabled", True)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=_TX, chain="bsc")
        assert res.status == "not_payable"
        assert res.payment is None
    _run(body, tmp_path, "f7b.db")


def test_f7_unknown_chain_is_rejected(tmp_path):
    async def body(s):
        r = Reseller(panel_id=1, admin_uuid="u", name="R")
        s.add(r)
        await s.flush()
        inv = _sent_invoice(r.id)
        s.add(inv)
        await s.commit()
        # An unknown chain must be rejected by the allow-list, never coerced to BSC/USDT.
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=_TX, chain="doge")
        assert res.status == "invalid_txid"
        assert res.payment is None
    _run(body, tmp_path, "f7c.db")


# ─────────────────────── F6: confirm vs cancel must serialize ───────────────────────
async def _cancel_invoice(session, invoice_id):  # noqa: ANN001
    """Mirror the api/invoices.py cancel route: lock the invoice, re-validate, cancel, commit."""
    from app.services import invoice_state
    inv = await session.get(Invoice, invoice_id, with_for_update=True)
    invoice_state.ensure_can_cancel(inv.status)  # raises if already paid
    inv.status = InvoiceStatus.canceled
    await session.commit()


@pytest.mark.pg_contract
@requires_pg
def test_f6_confirm_and_cancel_never_leave_confirmed_on_canceled(monkeypatch):
    # Isolate the concurrency invariant: no-op confirm's POST-commit side effects (owner/reseller
    # Telegram notify, receipt, restore) so the test can't hang on network or fail on unrelated I/O.
    async def _noop(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None
    monkeypatch.setattr("app.services.notifier.send_to_reseller", _noop)
    monkeypatch.setattr(payments, "_send_receipt", _noop)
    monkeypatch.setattr(payments, "_maybe_restore", _noop)

    async def run():
        engine, factory = make_engine()
        try:
            # Seed a panel + reseller + owed invoice + its pending payment (Postgres enforces the
            # panel FK that SQLite ignores, so a real Panel row is required).
            async with factory() as s:
                pnl = Panel(key="f6panel", host="f6.invalid", proxy_path_enc="x", owner_uuid="of6")
                s.add(pnl)
                await s.flush()
                r = Reseller(panel_id=pnl.id, admin_uuid="uf6", name="R6")
                s.add(r)
                await s.flush()
                inv = _sent_invoice(r.id, panel_id=pnl.id, label="2026-04")
                s.add(inv)
                await s.flush()
                p = Payment(reseller_id=r.id, invoice_id=inv.id,
                            method=PaymentMethod.usdt_txid, status=PaymentStatus.pending,
                            settled_invoice_ids=str(inv.id), txid=_TX)
                s.add(p)
                await s.commit()
                inv_id, pay_id, rid, pid = inv.id, p.id, r.id, pnl.id

            # Race: one coro confirms the payment, another cancels the invoice. Both take the
            # invoice row lock, so they serialize — either the confirm wins (invoice paid, cancel
            # refused) or the cancel wins (invoice canceled, confirm holds pending). Each coro OWNS
            # its session so its FOR UPDATE lock releases on ANY exit (commit/rollback/close);
            # sharing one `async with` across gather would hang if a coro finished holding a lock.
            from app.services.invoice_state import InvoiceStateError

            async def _do_confirm():
                async with factory() as s:
                    return await payments.confirm_manually(s, pay_id)

            async def _do_cancel():
                async with factory() as s:
                    return await _cancel_invoice(s, inv_id)

            ra, rb = await run_two(_do_confirm(), _do_cancel())
            # The LOSER's guard rejects: confirm returns a "pending" result (cancel won) OR cancel
            # raises InvoiceStateError (confirm won). Any OTHER exception is a real bug — surface it.
            if isinstance(ra, BaseException):
                raise ra
            if isinstance(rb, BaseException) and not isinstance(rb, InvoiceStateError):
                raise rb

            async with factory() as s:
                inv = await s.get(Invoice, inv_id)
                p = await s.get(Payment, pay_id)
                # THE invariant: never a confirmed payment on a canceled invoice.
                assert not (
                    inv.status == InvoiceStatus.canceled
                    and p.status == PaymentStatus.confirmed
                ), f"inconsistent: invoice={inv.status} payment={p.status}"
                # And the two states are mutually consistent.
                if p.status == PaymentStatus.confirmed:
                    assert inv.status == InvoiceStatus.paid
                if inv.status == InvoiceStatus.canceled:
                    assert p.status != PaymentStatus.confirmed
                # cleanup (payment → invoice → reseller → panel)
                await s.delete(p)
                await s.delete(inv)
                r = await s.get(Reseller, rid)
                if r is not None:
                    await s.delete(r)
                pnl = await s.get(Panel, pid)
                if pnl is not None:
                    await s.delete(pnl)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
