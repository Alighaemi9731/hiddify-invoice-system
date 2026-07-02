"""I06: the payment_settlements join table mirrors every payment's invoice set (dual-write),
and the hot lookups (duplicate-pending block, revert protection) work off the indexed table."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/paysettle.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    Invoice,
    Panel,
    Payment,
    PaymentSettlement,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import payments  # noqa: E402

HASH = "0x" + "cd" * 32


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(s, *, n_invoices=2):
    panel = Panel(key="p", host="h.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    r = Reseller(panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=1)
    s.add(r)
    await s.flush()
    invs = []
    for i in range(n_invoices):
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id,
            period_start=dt.date(2026, i + 1, 1), period_end=dt.date(2026, i + 1, 28),
            period_label=f"2026-0{i + 1}", usage_gb=10, amount_toman=100_000, amount_usdt=1,
            status=InvoiceStatus.sent, sent_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(inv)
        invs.append(inv)
    await s.commit()
    return r, invs


async def _settlement_rows(s, payment_id):
    return set(
        (
            await s.execute(
                select(PaymentSettlement.invoice_id).where(
                    PaymentSettlement.payment_id == payment_id)
            )
        ).scalars().all()
    )


def test_submission_mirrors_set_into_join_table(tmp_path):
    async def body(s):
        r, (a, b) = await _seed(s)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid=HASH, chain="bsc")
        assert res.status == "ok" and res.payment is not None
        assert await _settlement_rows(s, res.payment.id) == {a.id, b.id}
        # …and the comma column stayed byte-equal (dual-write, rollback safety).
        assert res.payment.settled_invoice_ids == f"{a.id},{b.id}"

    _run(body, tmp_path, "s1.db")


def test_duplicate_pending_blocked_via_join_table(tmp_path):
    async def body(s):
        r, (a, b) = await _seed(s)
        first = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id], txid=HASH, chain="bsc")
        assert first.status == "ok"
        # A second submission including invoice `a` must be blocked by the pending set.
        second = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id],
            txid="0x" + "ef" * 32, chain="bsc")
        assert second.status == "pending_exists"
        # The lookup helpers see it through the indexed table.
        assert await payments._pending_invoice_ids_in_sets(s, {a.id, b.id}, {r.id}) == {a.id}
        found = await payments._pending_payment_for_invoice(s, a.id)
        assert found is not None and found.id == first.payment.id
        assert await payments._pending_payment_for_invoice(s, b.id) is None

    _run(body, tmp_path, "s2.db")


def test_legacy_payment_confirm_populates_join_table(tmp_path):
    """A payment row that predates the join table (comma column only — e.g. restored from an
    old backup after the migration already ran) gets its settlement rows on confirm."""
    async def body(s):
        r, (a, _b) = await _seed(s)
        legacy = Payment(reseller_id=r.id, invoice_id=a.id, method=PaymentMethod.manual,
                         status=PaymentStatus.pending, settled_invoice_ids=str(a.id))
        s.add(legacy)
        await s.commit()
        assert await _settlement_rows(s, legacy.id) == set()   # no rows yet
        res = await payments.confirm_manually(s, legacy.id)
        assert res.status == "confirmed"
        assert await _settlement_rows(s, legacy.id) == {a.id}  # synced at confirm
        await s.refresh(a)
        assert a.status == InvoiceStatus.paid

    _run(body, tmp_path, "s3.db")


def test_revert_protection_via_join_table(tmp_path):
    """Rejecting one of two confirmed payments covering the same invoice must keep it paid —
    the overlap check now runs on the settlements table."""
    async def body(s):
        r, (a, _b) = await _seed(s)
        a.status = InvoiceStatus.paid
        a.paid_at = dt.datetime.now(dt.timezone.utc)
        p1 = Payment(reseller_id=r.id, invoice_id=a.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(a.id))
        p2 = Payment(reseller_id=r.id, invoice_id=a.id, method=PaymentMethod.manual,
                     status=PaymentStatus.confirmed, settled_invoice_ids=str(a.id))
        s.add_all([p1, p2])
        await s.flush()
        s.add_all([PaymentSettlement(payment_id=p1.id, invoice_id=a.id),
                   PaymentSettlement(payment_id=p2.id, invoice_id=a.id)])
        await s.commit()

        assert await payments._settled_by_other_confirmed(s, a.id, p1.id) is True
        await payments.reject_payment(s, p1.id)
        await s.refresh(a)
        assert a.status == InvoiceStatus.paid          # p2 still settles it
        await payments.reject_payment(s, p2.id)
        await s.refresh(a)
        assert a.status == InvoiceStatus.sent           # last settler gone → reverts

    _run(body, tmp_path, "s4.db")


def test_deleting_payment_cascades_settlements(tmp_path):
    async def body(s):
        r, (a, _b) = await _seed(s)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id], txid=HASH, chain="bsc")
        pid = res.payment.id
        assert await _settlement_rows(s, pid) == {a.id}
        assert await payments.delete_payment(s, pid)
        assert await _settlement_rows(s, pid) == set()

    _run(body, tmp_path, "s5.db")
