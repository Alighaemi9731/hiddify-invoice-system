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

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/state.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import DeliveryLog, FinancialRecord, Invoice, Payment, Reseller  # noqa: E402
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
        r = _reseller(); s.add(r); await s.flush()
        inv = _invoice(r.id, status=S.paid); inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add(inv); await s.flush()
        # Two confirmed payments both claim to settle this invoice (e.g. a duplicate).
        p1 = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(inv.id))
        p2 = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(inv.id))
        s.add_all([p1, p2]); await s.commit()

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
        r = _reseller(); s.add(r); await s.flush()
        inv = _invoice(r.id, status=S.paid, sent_days_ago=20)
        inv.paid_at = dt.datetime.now(dt.timezone.utc)
        s.add(inv); await s.flush()
        # Ledger row with a txid + a stale 'warning' dunning mark.
        await financial_archive.record(s, inv, txid="0xdeadbeef")
        s.add(DeliveryLog(invoice_id=inv.id, kind=DeliveryKind.warning,
                          status=DeliveryStatus.sent, reseller_id=r.id))
        p = Payment(reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.usdt_txid,
                    txid="0xdeadbeef", status=PaymentStatus.confirmed,
                    settled_invoice_ids=str(inv.id))
        s.add(p); await s.commit()
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
        r = _reseller(); s.add(r); await s.flush()
        paid = _invoice(r.id, status=S.paid, label="2026-01")
        paid.paid_at = dt.datetime.now(dt.timezone.utc)
        owed = _invoice(r.id, status=S.sent, label="2026-02")
        deferred = _invoice(r.id, status=S.sent, label="2026-03",
                            deferred_until=dt.date.today() + dt.timedelta(days=30))
        s.add_all([paid, owed, deferred]); await s.commit()

        # Another non-deferred owed invoice remains → restore must be HELD.
        assert await payments._reseller_has_other_due(s, r.id, exclude_invoice_id=paid.id) is True

        # Pay it off too; now only deferred (future) remains → no current debt → restore allowed.
        owed.status = S.paid; owed.paid_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        assert await payments._reseller_has_other_due(s, r.id, exclude_invoice_id=paid.id) is False

    _run(body, tmp_path, "p3.db")


# ------------------------------ bot revalidation ------------------------------
def test_revalidate_payable_rejects_stale_invoice(tmp_path):
    async def body(s):
        from app.bot import handlers
        r = _reseller(); s.add(r); await s.flush()
        owed = _invoice(r.id, status=S.sent)
        paid = _invoice(r.id, status=S.paid, label="2026-02")
        canceled = _invoice(r.id, status=S.canceled, label="2026-03")
        deferred = _invoice(r.id, status=S.sent, label="2026-04",
                            deferred_until=dt.date.today() + dt.timedelta(days=5))
        s.add_all([owed, paid, canceled, deferred]); await s.commit()
        ids = {r.id}
        assert (await handlers._revalidate_payable(s, owed, ids)) is not None
        assert (await handlers._revalidate_payable(s, paid, ids)) is None
        assert (await handlers._revalidate_payable(s, canceled, ids)) is None
        assert (await handlers._revalidate_payable(s, deferred, ids)) is None
        assert (await handlers._revalidate_payable(s, owed, {9999})) is None  # not owner's

    _run(body, tmp_path, "p4.db")
