"""Absent-reseller detection + guarded deletion: a reseller removed from the panel can be deleted
(cascading its invoices/payments) while the durable financial ledger is kept; a present reseller
can never be removed through this path."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/absent.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.resellers import (  # noqa: E402
    delete_absent_reseller,
    list_absent_resellers,
)
from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    FinancialRecord,
    Invoice,
    InvoiceLine,
    Panel,
    Payment,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    InvoiceStatus,
    PanelStatus,
    PaymentMethod,
    PaymentStatus,
)

NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=2)


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    ok = Panel(key="ok", host="ok.invalid", proxy_path_enc="x", owner_uuid="o1",
               status=PanelStatus.ok, last_synced_at=NOW)
    failed = Panel(key="failed", host="f.invalid", proxy_path_enc="x", owner_uuid="o2",
                   status=PanelStatus.error, last_synced_at=NOW)
    s.add_all([ok, failed])
    await s.flush()

    present = Reseller(panel_id=ok.id, admin_uuid="present", name="Present", last_seen_at=NOW)
    absent = Reseller(panel_id=ok.id, admin_uuid="absent", name="Absent", last_seen_at=OLD)
    owner_gone = Reseller(panel_id=ok.id, admin_uuid="o1", name="Owner", is_owner=True,
                          last_seen_at=OLD)                          # owner → excluded
    failed_old = Reseller(panel_id=failed.id, admin_uuid="x", name="OnFailed", last_seen_at=OLD)
    s.add_all([present, absent, owner_gone, failed_old])
    await s.flush()
    return ok, present, absent


def test_absent_detection(tmp_path):
    async def body(s):
        _ok, present, absent = await _seed(s)
        await s.commit()
        rows = await list_absent_resellers(panel_id=None, session=s)
        names = {r.name for r in rows}
        assert names == {"Absent"}                       # only the removed non-owner on a good panel
        assert present.name not in names                 # present excluded
    _run(body, tmp_path)


def test_delete_guard_rejects_present(tmp_path):
    async def body(s):
        _ok, present, _absent = await _seed(s)
        await s.commit()
        with pytest.raises(HTTPException) as ei:
            await delete_absent_reseller(present.id, session=s)
        assert ei.value.status_code == 409
        assert await s.get(Reseller, present.id) is not None  # untouched
    _run(body, tmp_path)


def test_delete_absent_cascades_but_keeps_ledger(tmp_path):
    async def body(s):
        ok, _present, absent = await _seed(s)
        inv = Invoice(reseller_id=absent.id, panel_id=ok.id, period_start=dt.date(2026, 6, 1),
                      period_end=dt.date(2026, 6, 30), period_label="2026-06",
                      usage_gb=10, amount_toman=100_000, status=InvoiceStatus.paid)
        s.add(inv)
        await s.flush()
        s.add(InvoiceLine(invoice_id=inv.id, end_user_uuid="u1", name="u1", usage_gb=10))
        s.add(Payment(reseller_id=absent.id, invoice_id=inv.id, method=PaymentMethod.ton_txid,
                      chain="ton", status=PaymentStatus.confirmed, txid="abc"))
        # The durable ledger row mirrors the money facts and has NO FK to the reseller/invoice.
        s.add(FinancialRecord(invoice_id=inv.id, panel_key="ok", reseller_name="Absent",
                              reseller_admin_uuid="absent", period_label="2026-06",
                              amount_toman=100_000, status="paid"))
        await s.commit()

        res = await delete_absent_reseller(absent.id, session=s)
        assert res["deleted"] is True and res["financial_records_kept"] is True

        assert await s.get(Reseller, absent.id) is None
        assert (await s.execute(select(Invoice).where(Invoice.reseller_id == absent.id))).first() is None
        assert (await s.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id))).first() is None
        assert (await s.execute(select(Payment).where(Payment.reseller_id == absent.id))).first() is None
        # The ledger survives the deletion (intentional — financial history is permanent).
        ledger = (await s.execute(select(FinancialRecord))).scalars().all()
        assert len(ledger) == 1 and ledger[0].reseller_admin_uuid == "absent"
    _run(body, tmp_path)
