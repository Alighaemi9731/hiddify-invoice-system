"""Integration test: an invoice generated for a panel where one user is still present and
another was removed bills the present user on SOLD quota and the removed one on CONSUMPTION,
labelling the removed line « — مصرف حذف‌شده از پنل». Covers the full generate→persist path
(the engine flag AND _persist_bundle's labelling)."""
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import EndUserSnapshot, Invoice, InvoiceLine, Panel, Reseller
from app.services import invoicing
from app.services.periods import current_month

OWNER = "owner-uuid"
R = "reseller-uuid"


@pytest.mark.asyncio
async def test_generate_bills_deleted_user_on_consumption_with_label(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    period = current_month()
    now = dt.datetime(period.start.year, period.start.month, 15, 12, 0)
    created = period.start + dt.timedelta(days=3)

    async with Session() as s:
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid=OWNER, last_synced_at=now)
        s.add(panel)
        await s.flush()
        s.add_all([
            Reseller(panel_id=panel.id, admin_uuid=OWNER, name="O", is_owner=True),
            Reseller(panel_id=panel.id, admin_uuid=R, name="R", parent_admin_uuid=OWNER),
            EndUserSnapshot(panel_id=panel.id, user_uuid="present", name="A", added_by_uuid=R,
                            usage_limit_gb=30, current_usage_gb=8, start_date=created,
                            enable=True, is_active=True, last_synced_at=now),
            EndUserSnapshot(panel_id=panel.id, user_uuid="gone", name="B", added_by_uuid=R,
                            usage_limit_gb=30, current_usage_gb=5, start_date=created,
                            enable=True, is_active=True, last_synced_at=now - dt.timedelta(days=1)),
        ])
        await s.commit()

        await invoicing.generate_invoices(s, period, force=True)

        r = (await s.execute(select(Reseller).where(Reseller.admin_uuid == R))).scalar_one()
        inv = (await s.execute(select(Invoice).where(Invoice.reseller_id == r.id))).scalar_one()
        lines = (await s.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        )).scalars().all()

        assert inv.usage_gb == 30 + 5  # present billed on sold 30, deleted on consumed 5
        by_uuid = {ln.end_user_uuid: ln for ln in lines}
        assert by_uuid["present"].usage_gb == 30
        assert "مصرف حذف‌شده از پنل" not in by_uuid["present"].name
        assert by_uuid["gone"].usage_gb == 5
        assert by_uuid["gone"].name.endswith("مصرف حذف‌شده از پنل")

    await engine.dispose()
