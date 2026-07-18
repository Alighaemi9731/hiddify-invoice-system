"""Payment & invoice state machine (B03).

Covers the central transition guards (pure) plus the money-critical DB behaviours:
- rejecting one payment must NOT un-pay an invoice another confirmed payment still settles;
- reverting a confirmed payment reverts the invoice to owed, clears the ledger txid, and
  resets the dunning cycle;
- a reseller is restored only when no OTHER due invoice remains;
- a chosen invoice is re-validated under lock at proof-submission time.
"""
import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/state.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    DeliveryLog,
    FinancialRecord,
    Invoice,
    Payment,
    PaymentSettlement,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    DeliveryKind,
    DeliveryStatus,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import invoice_state as st  # noqa: E402

S = InvoiceStatus


# ----------------------------------------------------------------- pure guards
def test_transition_matrix_core_rules():
    assert st.can_transition(S.sent, S.paid)
    assert st.can_transition(S.paid, S.sent)          # via unmark
    assert st.can_transition(S.enforced, S.sent)      # via defer/restore
    assert st.can_transition(S.canceled, S.draft)     # via revert
    # illegal
    assert not st.can_transition(S.paid, S.canceled)
    assert not st.can_transition(S.canceled, S.paid)
    assert not st.can_transition(S.draft, S.paid)     # must be issued first
    assert not st.can_transition(S.canceled, S.sent)


def test_operation_guards():
    # mark_paid only from owed
    for s in (S.sent, S.overdue, S.enforced):
        st.ensure_can_mark_paid(s)
    for s in (S.draft, S.paid, S.canceled):
        with pytest.raises(st.InvoiceStateError):
            st.ensure_can_mark_paid(s)
    # cancel: anything but paid
    for s in (S.draft, S.sent, S.overdue, S.enforced, S.canceled):
        st.ensure_can_cancel(s)
    with pytest.raises(st.InvoiceStateError):
        st.ensure_can_cancel(S.paid)
    # defer: only owed
    for s in (S.draft, S.paid, S.canceled):
        with pytest.raises(st.InvoiceStateError):
            st.ensure_can_defer(s)
    st.ensure_can_defer(S.sent)
    # edit: not paid/canceled
    for s in (S.paid, S.canceled):
        with pytest.raises(st.InvoiceStateError):
            st.ensure_can_edit(s)
    st.ensure_can_edit(S.draft)
    st.ensure_can_edit(S.overdue)
    # unmark: only paid
    st.ensure_can_unmark_paid(S.paid)
    with pytest.raises(st.InvoiceStateError):
        st.ensure_can_unmark_paid(S.sent)


# ----------------------------------------------------------------- DB harness
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


def _invoice(reseller_id, *, status=S.sent, label="2026-01", sent_days_ago=10, **kw):
    # Distinct period per label so the (reseller, period_start, period_end) unique constraint
    # is satisfied when a reseller has several invoices.
    year, month = (int(x) for x in label.split("-"))
    start = dt.date(year, month, 1)
    end = (dt.date(year + (month // 12), (month % 12) + 1, 1)) - dt.timedelta(days=1)
    sent_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sent_days_ago)
    return Invoice(
        reseller_id=reseller_id, panel_id=1,
        period_start=start, period_end=end, period_label=label,
        usage_gb=10, amount_toman=10000, amount_usdt=1, status=status,
        sent_at=sent_at if status in st.OWED or status == S.paid else None, **kw,
    )


# ------------------------------ multi-payment: don't un-pay -------------------
def test_reject_does_not_unpay_invoice_settled_by_other_payment(tmp_path):
    async def body(s):
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id, status=S.paid)
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add(inv)
        await s.flush()
        # Two confirmed payments both claim to settle this invoice (e.g. a duplicate).
        p1 = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(inv.id))
        p2 = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(inv.id))
        s.add_all([p1, p2])
        await s.flush()
        # I06: directly-seeded payments must mirror their set into payment_settlements
        # (production rows get this from the submit path / the backfill migration).
        s.add_all([PaymentSettlement(payment_id=p1.id, invoice_id=inv.id),
                   PaymentSettlement(payment_id=p2.id, invoice_id=inv.id)])
        await s.commit()

        from app.services import payments
        # Rejecting p1 must leave the invoice PAID (p2 still settles it).
        await payments.reject_payment(s, p1.id)
        await s.refresh(inv)
        assert inv.status == S.paid, "invoice settled by another payment must stay paid"

        # Now reject the remaining settler → the invoice reverts to owed.
        await payments.reject_payment(s, p2.id)
        await s.refresh(inv)
        assert inv.status == S.sent and inv.paid_at is None

    _run(body, tmp_path, "p1.db")


# ------------------------------ revert clears txid + resets cycle -------------
def test_revert_clears_ledger_txid_and_resets_dunning(tmp_path):
    async def body(s):
        from app.services import financial_archive, payments
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id, status=S.paid, sent_days_ago=20)
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add(inv)
        await s.flush()
        # Ledger row with a txid + a stale 'warning' dunning mark.
        await financial_archive.record(s, inv, txid="0xdeadbeef")
        s.add(DeliveryLog(invoice_id=inv.id, kind=DeliveryKind.warning,
                          status=DeliveryStatus.sent, reseller_id=r.id))
        p = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.usdt_txid,
                    txid="0xdeadbeef", status=PaymentStatus.confirmed,
                    settled_invoice_ids=str(inv.id))
        s.add(p)
        await s.flush()
        s.add(PaymentSettlement(payment_id=p.id, invoice_id=inv.id))
        await s.commit()
        old_sent = inv.sent_at

        await payments.reject_payment(s, p.id)
        await s.refresh(inv)
        assert inv.status == S.sent and inv.paid_at is None
        # ledger txid cleared
        fr = (await s.execute(
            FinancialRecord.__table__.select().where(FinancialRecord.invoice_id == inv.id)
        )).first()
        assert fr is not None and fr.txid in (None, "")
        # dunning cycle reset: the reminder/warning marks are gone + sent_at re-anchored to ~now
        from sqlalchemy import select as _select
        kinds = (await s.execute(
            _select(DeliveryLog.kind).where(
                DeliveryLog.invoice_id == inv.id,
                DeliveryLog.kind.in_([DeliveryKind.reminder1, DeliveryKind.reminder2,
                                      DeliveryKind.warning]),
            )
        )).scalars().all()
        assert kinds == []
        # sent_at re-anchored forward (normalize tz: SQLite drops tzinfo on reload).
        assert inv.sent_at.replace(tzinfo=None) > old_sent.replace(tzinfo=None)

    _run(body, tmp_path, "p2.db")


# ------------------------------ restore only when no other debt ---------------
def test_restore_held_when_other_due_invoice_remains(tmp_path):
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        paid = _invoice(r.id, status=S.paid, label="2026-01")
        paid.paid_at = dt.datetime.now(dt.timezone.utc)
        owed = _invoice(r.id, status=S.sent, label="2026-02")
        deferred = _invoice(r.id, status=S.sent, label="2026-03",
                            deferred_until=dt.date.today() + dt.timedelta(days=30))
        s.add_all([paid, owed, deferred])
        await s.commit()

        # Another non-deferred owed invoice remains → restore must be HELD.
        assert await payments._reseller_has_other_due(s, r.id, {paid.id}) is True

        # Pay it off too; now only deferred (future) remains → no current debt → restore allowed.
        owed.status = S.paid
        owed.paid_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        assert await payments._reseller_has_other_due(s, r.id, {paid.id}) is False

    _run(body, tmp_path, "p3.db")


# ------------------------------ bot revalidation ------------------------------
def test_submit_revalidates_stale_invoice(tmp_path):
    # The payable re-validation now lives in payments.submit_reseller_payment: an owed invoice
    # the caller owns is accepted; paid/canceled/future-deferred/other-owner are rejected.
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        owed = _invoice(r.id, status=S.sent)
        paid = _invoice(r.id, status=S.paid, label="2026-02")
        canceled = _invoice(r.id, status=S.canceled, label="2026-03")
        deferred = _invoice(r.id, status=S.sent, label="2026-04",
                            deferred_until=dt.date.today() + dt.timedelta(days=5))
        s.add_all([owed, paid, canceled, deferred])
        await s.commit()
        ids = {r.id}

        async def status(inv_id, ids_, tx):
            return (await payments.submit_reseller_payment(
                s, reseller_ids=ids_, invoice_id=inv_id, txid=tx)).status

        assert await status(owed.id, ids, "0x" + "a" * 64) == "ok"
        assert await status(paid.id, ids, "0x" + "b" * 64) == "not_payable"
        assert await status(canceled.id, ids, "0x" + "c" * 64) == "not_payable"
        assert await status(deferred.id, ids, "0x" + "d" * 64) == "not_payable"
        assert await status(owed.id, {9999}, "0x" + "e" * 64) == "not_payable"  # not owner's

    _run(body, tmp_path, "p4.db")


def test_stale_payment_selection_never_falls_back_to_another_invoice(tmp_path):
    async def body(s):
        from app.bot import handlers

        r = _reseller(bot_chat_id=123)
        s.add(r)
        await s.flush()
        stale = _invoice(r.id, status=S.paid, label="2026-01")
        other_due = _invoice(r.id, status=S.sent, label="2026-02")
        s.add_all([stale, other_due])
        await s.commit()

        answers = []

        async def answer(text, **_kwargs):
            answers.append(text)

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            answer=answer,
        )
        await handlers._handle_txid(
            message, s, "0x" + "a" * 64, invoices=[stale], chain="bsc"
        )
        payments = (await s.execute(select(Payment))).scalars().all()
        assert payments == []
        assert "دوباره انتخاب کنید" in answers[-1]

    _run(body, tmp_path, "p5.db")


# ============================ multi-invoice payments =========================
def test_submit_multi_creates_one_payment_with_set(tmp_path):
    """Paying two invoices with one transfer → ONE pending payment, primary = first id,
    settled_invoice_ids = the full set, amount = the SUM."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")  # 10000 T / 1 USDT
        b = _invoice(r.id, status=S.sent, label="2026-02")
        s.add_all([a, b])
        await s.commit()

        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid="0x" + "b" * 64)
        assert res.status == "ok"
        p = res.payment
        assert p.invoice_id == a.id
        assert payments._settled_ids(p) == [a.id, b.id]
        assert float(p.amount_usdt) == 2.0          # 1 + 1
        assert float(p.amount_toman) == 20000.0     # 10000 + 10000

    _run(body, tmp_path, "m1.db")


def test_confirm_multi_marks_all_paid_and_restores(tmp_path):
    """Confirming a multi-invoice payment marks EVERY covered invoice paid and lifts enforcement
    only once no due invoice outside the set remains."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")
        b = _invoice(r.id, status=S.sent, label="2026-02")
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], screenshot=True)
        await payments.confirm_manually(s, res.payment.id)
        await s.refresh(a)
        await s.refresh(b)
        assert a.status == S.paid and b.status == S.paid
        assert a.paid_at is not None and b.paid_at is not None

    _run(body, tmp_path, "m2.db")


def test_verify_multi_amount_floor_is_the_sum(tmp_path):
    """On-chain verify must require the deposit to cover the SUM of the set: a deposit between one
    invoice and the total is rejected and NEITHER invoice is marked paid."""
    async def body(s):
        from decimal import Decimal

        from app.services import payments, settings_service
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")  # 1 USDT
        b = _invoice(r.id, status=S.sent, label="2026-02")  # 1 USDT  → sum = 2
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid="0x" + "c" * 64)
        pid = res.payment.id

        # Stub config + the on-chain lookup: a deposit of only 1 USDT (covers one, not both).
        orig_get_many = settings_service.get_many
        orig_lookup = payments._bscscan_tokentx

        async def fake_get_many(_s, _keys):
            return {"bscscan_api_key": "k", "bscscan_api_url": "u",
                    "usdt_bep20_address": "0xwallet", "usdt_bep20_contract": "0xtoken",
                    "min_confirmations": 0, "payment_amount_tolerance_usdt": 0}

        async def fake_lookup(_url, _key, _wallet, _contract, _txid):
            return payments._ChainCheck(
                found=True, to_address="0xwallet", from_address="0xfrom",
                amount_usdt=Decimal("1"), confirmations=10, contract_address="0xtoken")

        settings_service.get_many = fake_get_many
        payments._bscscan_tokentx = fake_lookup
        try:
            result = await payments.verify_payment(s, pid)
        finally:
            settings_service.get_many = orig_get_many
            payments._bscscan_tokentx = orig_lookup
        assert result.status == "rejected"
        await s.refresh(a)
        await s.refresh(b)
        assert a.status == S.sent and b.status == S.sent  # neither settled by a short deposit

    _run(body, tmp_path, "m3.db")


def test_reject_multi_reverts_whole_set_but_protects_overlap(tmp_path):
    """Rejecting a multi-invoice payment reverts every invoice it settled — unless another
    confirmed payment still settles one of them (overlap stays paid)."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.paid, label="2026-01")
        b = _invoice(r.id, status=S.paid, label="2026-02")
        for inv in (a, b):
            inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add_all([a, b])
        await s.flush()
        # P1 covers {a, b}; a second confirmed payment P2 also covers {b}.
        p1 = Payment(reseller_id=r.id, invoice_id=a.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=f"{a.id},{b.id}")
        p2 = Payment(reseller_id=r.id, invoice_id=b.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(b.id))
        s.add_all([p1, p2])
        await s.flush()
        s.add_all([PaymentSettlement(payment_id=p1.id, invoice_id=a.id),
                   PaymentSettlement(payment_id=p1.id, invoice_id=b.id),
                   PaymentSettlement(payment_id=p2.id, invoice_id=b.id)])
        await s.commit()

        await payments.reject_payment(s, p1.id)
        await s.refresh(a)
        await s.refresh(b)
        assert a.status == S.sent and a.paid_at is None     # only P1 settled a → reverts
        assert b.status == S.paid                            # P2 still settles b → stays paid

    _run(body, tmp_path, "m4.db")


def test_pending_set_blocks_overlapping_submission(tmp_path):
    """No invoice may sit in two pending payments: a submission that includes an invoice already
    in a pending payment's set is rejected, and _pending_invoice_ids reports it held."""
    async def body(s):
        from app.bot import handlers
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")
        b = _invoice(r.id, status=S.sent, label="2026-02")
        c = _invoice(r.id, status=S.sent, label="2026-03")
        s.add_all([a, b, c])
        await s.commit()

        # First payment covers {a, b}.
        await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid="0x" + "d" * 64)
        held = await handlers._pending_invoice_ids(s, [r.id])
        assert a.id in held and b.id in held

        # A second submission including b (already pending) is blocked entirely.
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[b.id, c.id], txid="0x" + "e" * 64)
        assert res.status == "pending_exists"

    _run(body, tmp_path, "m5.db")


def test_submit_multi_partial_stale_rejects_whole_batch(tmp_path):
    """If any selected invoice is no longer payable, the whole batch is rejected and NO payment
    row is created (never silently pay a subset)."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")
        b = _invoice(r.id, status=S.paid, label="2026-02")   # already paid → stale
        s.add_all([a, b])
        await s.commit()

        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid="0x" + "f" * 64)
        assert res.status == "not_payable"
        rows = (await s.execute(select(Payment))).scalars().all()
        assert rows == []

    _run(body, tmp_path, "m6.db")


def test_backcompat_single_id_row(tmp_path):
    """A legacy payment with settled_invoice_ids=None and a single invoice_id confirms/reverts
    correctly via the _settled_ids fallback."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        inv = _invoice(r.id, status=S.sent, label="2026-01")
        s.add(inv)
        await s.flush()
        p = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.manual,
                    status=PaymentStatus.pending, settled_invoice_ids=None)
        s.add(p)
        await s.commit()
        assert payments._settled_ids(p) == [inv.id]
        await payments.confirm_manually(s, p.id)
        await s.refresh(inv)
        assert inv.status == S.paid
        await payments.reject_payment(s, p.id)
        await s.refresh(inv)
        assert inv.status == S.sent and inv.paid_at is None

    _run(body, tmp_path, "m7.db")


# ============================ external-review security fixes =========================
def test_bsc_txid_canonicalized_and_validated(tmp_path):
    """Bug 1/4: a BSC tx hash is stored lowercase (so 0xABC… and 0xabc… can't BOTH settle
    invoices), and malformed/overlong hashes are rejected instead of stored."""
    async def body(s):
        from app.services import payments
        r = _reseller()
        s.add(r)
        await s.flush()
        a = _invoice(r.id, status=S.sent, label="2026-01")
        b = _invoice(r.id, status=S.sent, label="2026-02")
        s.add_all([a, b])
        await s.commit()

        res1 = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=a.id, txid="0x" + "A" * 64, chain="bsc")
        assert res1.status == "ok"
        assert res1.payment.txid == "0x" + "a" * 64  # stored canonical (lowercase)

        # The SAME hash in different casing is caught as a duplicate, not a 2nd settling row.
        res2 = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=b.id, txid="0x" + "a" * 64, chain="bsc")
        assert res2.status.startswith("dup")

        # Malformed / non-hex BSC hashes are rejected (would otherwise 500 / create junk rows).
        for bad in ("nothex", "0x" + "z" * 64, "0x" + "a" * 10):
            res = await payments.submit_reseller_payment(
                s, reseller_ids={r.id}, invoice_id=b.id, txid=bad, chain="bsc")
            assert res.status == "invalid_txid"

    _run(body, tmp_path, "txcanon.db")


def test_mark_paid_notifies_reseller(tmp_path, monkeypatch):
    """«ثبت پرداخت» (mark an invoice paid by hand) must send the reseller the same confirmation as
    confirming a submitted payment — and must be a safe no-op when they aren't on the bot."""
    from app.api import invoices
    from app.models import Panel
    from app.services import notifier

    sent: list = []

    async def fake_send(session, reseller, text, **kw):  # noqa: ANN001, ANN003
        sent.append((reseller.id, text, kw))

    monkeypatch.setattr(notifier, "send_to_reseller", fake_send)

    async def body(s):
        s.add(Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o"))
        await s.flush()
        r = _reseller(bot_chat_id=555)
        s.add(r)
        await s.flush()
        inv = _invoice(r.id, status=S.sent, label="2026-03")
        s.add(inv)
        await s.commit()

        await invoices.mark_paid(inv.id, session=s)
        await s.refresh(inv)
        assert inv.status == S.paid
        assert len(sent) == 1
        rid, text, kw = sent[0]
        assert rid == r.id and "2026-03" in text
        assert kw.get("invoice_id") == inv.id and kw.get("kind") == DeliveryKind.payment_ack

        # A reseller not on the bot → no send, no crash.
        r2 = _reseller(uuid="u2", name="R2", bot_chat_id=None)
        s.add(r2)
        await s.flush()
        inv2 = _invoice(r2.id, status=S.sent, label="2026-04")
        s.add(inv2)
        await s.commit()
        await invoices.mark_paid(inv2.id, session=s)
        await s.refresh(inv2)
        assert inv2.status == S.paid and len(sent) == 1  # still just the first

    _run(body, tmp_path, "markpaid_notify.db")


def test_portal_login_link_is_one_time(tmp_path):
    """Bug 2: a portal login link can be exchanged only ONCE — a replay within its TTL is rejected."""
    async def body(s):
        from fastapi import HTTPException

        from app.api import portal
        from app.core.portal_auth import create_portal_login_token

        r = _reseller(bot_chat_id=777)
        s.add(r)
        await s.commit()
        token = create_portal_login_token(777)

        out = await portal.exchange(portal.ExchangeBody(token=token), s)
        assert out["access_token"]

        with pytest.raises(HTTPException) as exc:
            await portal.exchange(portal.ExchangeBody(token=token), s)
        assert exc.value.status_code == 401

    _run(body, tmp_path, "nonce.db")


def test_confirm_manually_refuses_when_the_settled_invoice_no_longer_exists(tmp_path):
    """Money accepted, debt uncleared, receipt sent, txid burned forever.

    `_lock_invoices` silently skips ids that are gone, so a payment whose invoice was reverted to
    draft and then discarded (or reconciled away by the monthly run) had an EMPTY settle set: the
    'draft/canceled' guard passed vacuously, nothing was marked paid, `_maybe_restore` was skipped
    so an enforced reseller stayed suspended — and the payment was still stamped CONFIRMED with a
    PDF receipt. Worse, the unique txid was then burned, so the customer could never resubmit it
    against the re-issued invoice. `verify_payment` already refused this exact case."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import Payment, Reseller
    from app.models.enums import PaymentMethod, PaymentStatus
    from app.services import payments as payments_service

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gone.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                r = Reseller(panel_id=1, admin_uuid="A", name="R")
                s.add(r)
                await s.flush()
                # The invoice this payment settles is GONE (id 500 never existed / was discarded).
                p = Payment(reseller_id=r.id, amount_usdt=1, method=PaymentMethod.usdt_txid,
                            status=PaymentStatus.pending, settled_invoice_ids="500")
                s.add(p)
                await s.commit()

                res = await payments_service.confirm_manually(s, p.id)
                await s.refresh(p)
                assert p.status == PaymentStatus.pending, "confirmed a payment that settled nothing"
                assert res.status == "pending" and not res.paid
                assert "وجود ندارد" in (res.message_fa or "")
        finally:
            await engine.dispose()

    asyncio.run(go())
