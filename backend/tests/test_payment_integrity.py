"""H01 — payment verification & submission integrity.

Regression coverage for the hardening batch:
- on-chain verify must HOLD (manual review) a payment whose invoice set contains a
  draft/canceled/deleted member — never auto-confirm and burn the unique txid;
- verify still auto-closes when the WHOLE set exists and is already paid;
- a COLD resubmit of a rejected txid re-validates its original coverage exactly like a
  fresh submission (owed / one-pending) instead of blindly re-opening;
- a reopen refreshes method/chain from the current submission (wrong-network recovery);
- mark-paid leaves a confirmed `manual` Payment row that shields the invoice from an
  unrelated reject; unmark-paid retires that row;
- submission keeps the chosen order (locks are taken sorted, the payment stores the
  submitted order); confirm_manually preserves the stored set order;
- a concurrent duplicate-txid insert maps to `dup_pending`, not an exception;
- verify never overwrites the payment's set-sum amount_usdt with the on-chain deposit.
"""
import asyncio
import datetime as dt
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/payintegrity.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Payment, PaymentSettlement, Reseller  # noqa: E402
from app.models.enums import (  # noqa: E402
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import payments, settings_service  # noqa: E402

S = InvoiceStatus
TX = "0x" + "a1" * 32


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


def _reseller(**kw):
    return Reseller(panel_id=1, admin_uuid=kw.pop("uuid", "u1"), name=kw.pop("name", "R"), **kw)


def _invoice(reseller_id, *, status=S.sent, label="2026-01", **kw):
    year, month = (int(x) for x in label.split("-"))
    start = dt.date(year, month, 1)
    end = (dt.date(year + (month // 12), (month % 12) + 1, 1)) - dt.timedelta(days=1)
    return Invoice(
        reseller_id=reseller_id, panel_id=1,
        period_start=start, period_end=end, period_label=label,
        usage_gb=10, amount_toman=10000, amount_usdt=1, status=status,
        sent_at=dt.datetime.now(dt.timezone.utc) if status != S.draft else None, **kw,
    )


class _VerifyStub:
    """Stub the BscScan config + lookup so verify_payment reaches the settle logic."""

    def __init__(self, amount="5", to_address="0xwallet", contract="0xtoken"):
        self._check = payments._ChainCheck(
            found=True, to_address=to_address, from_address="0xfrom",
            amount_usdt=Decimal(amount), confirmations=10, contract_address=contract)

    def __enter__(self):
        self._get_many = settings_service.get_many
        self._lookup = payments._bscscan_tokentx

        async def fake_get_many(_s, _keys):
            return {"bscscan_api_key": "k", "bscscan_api_url": "u",
                    "usdt_bep20_address": "0xwallet", "usdt_bep20_contract": "0xtoken",
                    "min_confirmations": 0, "payment_amount_tolerance_usdt": 0}

        async def fake_lookup(_url, _key, _wallet, _contract, _txid):
            return self._check

        settings_service.get_many = fake_get_many
        payments._bscscan_tokentx = fake_lookup
        return self

    def __exit__(self, *a):
        settings_service.get_many = self._get_many
        payments._bscscan_tokentx = self._lookup
        return False


# --------------------------------------------------------- verify: hold vs confirm
def test_verify_holds_pending_when_set_member_draft(tmp_path):
    """A set member reverted to draft after submission → verify must hold for manual review,
    not auto-confirm (the old `if not targets: confirmed` burned the customer's txid)."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        assert res.status == "ok"
        inv.status = S.draft   # owner: «بازگردانی به پیش‌نویس»
        inv.sent_at = None
        await s.commit()
        with _VerifyStub():
            result = await payments.verify_payment(s, res.payment.id)
        assert result.status == "pending"
        await s.refresh(res.payment)
        assert res.payment.status == PaymentStatus.pending
        assert "[needs manual review: invoice unpayable]" in (res.payment.note or "")

    _run(body, tmp_path, "v1.db")


def test_verify_holds_pending_when_invoice_deleted(tmp_path):
    """A set member deleted from the DB (draft discarded) → hold, never confirm."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        pid = res.payment.id
        await s.execute(
            PaymentSettlement.__table__.delete().where(
                PaymentSettlement.invoice_id == inv.id)
        )
        await s.delete(inv)
        await s.commit()
        with _VerifyStub():
            result = await payments.verify_payment(s, pid)
        assert result.status == "pending"
        p = await s.get(Payment, pid)
        assert p.status == PaymentStatus.pending

    _run(body, tmp_path, "v2.db")


def test_verify_confirms_when_all_already_paid(tmp_path):
    """Every set member exists and is PAID (settled meanwhile) → verify may auto-close."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        inv.status = S.paid
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        with _VerifyStub():
            result = await payments.verify_payment(s, res.payment.id)
        assert result.status == "confirmed"
        await s.refresh(res.payment)
        assert res.payment.status == PaymentStatus.confirmed

    _run(body, tmp_path, "v3.db")


def test_verify_does_not_overwrite_amount_usdt(tmp_path):
    """The payment's amount_usdt is the invoice-set sum; the on-chain deposit (5 USDT here)
    must not overwrite it — the panel's Toman/USDT pair has to keep corresponding."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, label="2026-01")
        b = _invoice(r.id, label="2026-02")
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid=TX, chain="bsc")
        with _VerifyStub(amount="5"):
            result = await payments.verify_payment(s, res.payment.id)
        assert result.status == "confirmed"
        await s.refresh(res.payment)
        assert float(res.payment.amount_usdt) == 2.0  # set sum, not the 5-USDT deposit

    _run(body, tmp_path, "v4.db")


# --------------------------------------------------------- cold resubmit re-validation
def test_cold_resubmit_revalidates_against_paid_invoice(tmp_path):
    """A rejected txid cold-resent (no fresh selection) after its invoice was paid must NOT
    reopen — the old path resurrected coverage over settled invoices."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        res.payment.status = PaymentStatus.rejected
        inv.status = S.paid
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        cold = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, txid=TX, chain="bsc")   # no invoice selection
        assert cold.status == "not_payable"
        p = await s.get(Payment, res.payment.id)
        assert p.status == PaymentStatus.rejected           # untouched

    _run(body, tmp_path, "c1.db")


def test_cold_resubmit_blocked_by_other_pending(tmp_path):
    """Cold reopen must respect one-pending-per-invoice: a screenshot payment already pending
    on the same invoice blocks the resurrection (old path created a second pending)."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        first = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        first.payment.status = PaymentStatus.rejected
        await s.commit()
        shot = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], screenshot=True)
        assert shot.status == "ok"
        cold = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, txid=TX, chain="bsc")
        assert cold.status == "pending_exists"
        p = await s.get(Payment, first.payment.id)
        assert p.status == PaymentStatus.rejected           # not resurrected

    _run(body, tmp_path, "c2.db")


def test_reopen_updates_chain_and_method(tmp_path):
    """Wrong-network recovery: a 0x hash first sent as BSC and rejected, resubmitted as AVAX,
    must carry chain='avax' / method=avax_txid so the owner review links the right explorer."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        first = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        assert first.payment.chain == "bsc"
        first.payment.status = PaymentStatus.rejected
        await s.commit()
        again = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="avax")
        assert again.status == "reopened" and again.payment.id == first.payment.id
        assert again.payment.chain == "avax"
        assert again.payment.method == PaymentMethod.avax_txid

    _run(body, tmp_path, "c3.db")


# --------------------------------------------------------- manual mark-paid row
def test_manual_row_protects_from_unrelated_reject(tmp_path):
    """mark-paid records a confirmed `manual` payment; rejecting an unrelated payment that
    also covered the invoice must keep it paid (old behavior un-paid it)."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        shot = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], screenshot=True)
        # Owner records the money by hand instead (the mark-paid endpoint path).
        inv.status = S.paid
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        manual = await payments.record_manual_payment(s, inv)
        await s.commit()
        assert manual.status == PaymentStatus.confirmed
        # Owner then confirms the stale screenshot payment (no owed target → coverage only)
        # and changes their mind.
        await payments.confirm_manually(s, shot.payment.id)
        await payments.reject_payment(s, shot.payment.id)
        await s.refresh(inv)
        assert inv.status == S.paid   # shielded by the manual row

    _run(body, tmp_path, "m1.db")


def test_unmark_paid_retires_manual_row(tmp_path):
    """retire_manual_payments rejects the single-invoice manual row so it can't shield an
    invoice that is no longer paid."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        inv.status = S.paid
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add(inv)
        await s.commit()
        manual = await payments.record_manual_payment(s, inv)
        await s.commit()
        n = await payments.retire_manual_payments(s, inv)
        await s.commit()
        assert n == 1
        await s.refresh(manual)
        assert manual.status == PaymentStatus.rejected
        assert "[unmarked from panel]" in (manual.note or "")

    _run(body, tmp_path, "m2.db")


# --------------------------------------------------------- ordering + dup race
def test_submission_order_preserved(tmp_path):
    """Locks are taken in sorted id order but the payment stores the SUBMITTED order (the
    first id is the primary invoice shown in the panel)."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, label="2026-01")
        b = _invoice(r.id, label="2026-02")
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[b.id, a.id], txid=TX, chain="bsc")
        assert res.status == "ok"
        assert res.payment.invoice_id == b.id
        assert res.payment.settled_invoice_ids == f"{b.id},{a.id}"

    _run(body, tmp_path, "o1.db")


def test_confirm_manually_preserves_set_order(tmp_path):
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, label="2026-01")
        b = _invoice(r.id, label="2026-02")
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[b.id, a.id], txid=TX, chain="bsc")
        out = await payments.confirm_manually(s, res.payment.id)
        assert out.status == "confirmed"
        await s.refresh(res.payment)
        assert res.payment.invoice_id == b.id                       # primary stable
        assert res.payment.settled_invoice_ids == f"{b.id},{a.id}"  # order stable

    _run(body, tmp_path, "o2.db")


def test_concurrent_duplicate_txid_maps_to_dup_pending(tmp_path):
    """Two concurrent first-time submissions of one hash: the loser's INSERT hits the unique
    txid constraint and must surface as `dup_pending`, not an exception/500."""
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        # The winner's row is already committed…
        winner = Payment(reseller_id=r.id, invoice_id=inv.id, settled_invoice_ids=str(inv.id),
                         method=PaymentMethod.usdt_txid, chain="bsc",
                         status=PaymentStatus.pending, txid=TX)
        s.add(winner)
        await s.commit()
        # …but the loser's pre-insert lookup raced and saw nothing.
        orig = payments._payment_by_txid

        async def lookup_misses(_s, _tx):
            return None

        payments._payment_by_txid = lookup_misses
        try:
            res = await payments.submit_reseller_payment(
                s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        finally:
            payments._payment_by_txid = orig
        assert res.status == "dup_pending"
        # exactly one row holds the hash
        rows = (await s.execute(select(Payment).where(Payment.txid == TX))).scalars().all()
        assert len(rows) == 1

    _run(body, tmp_path, "d1.db")
