"""The bot interim («علی‌الحساب») must equal the real end-of-month invoice.

Both must apply the SAME rules: the snapshot quota rule PLUS the abuse-metered extra
(overage / renew-by-edit). Before the fix the interim omitted the metered extra and
under-reported whenever a reseller renewed by edit or reset usage.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/interim.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    UsageMeter,
)
from app.models.enums import PanelStatus  # noqa: E402
from app.services import invoicing, reseller_report  # noqa: E402
from app.services.periods import current_month, today  # noqa: E402


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


def test_interim_includes_metered_extra_and_matches_real_invoice(tmp_path):
    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        period = current_month()

        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner",
                    status=PanelStatus.ok, last_synced_at=now))
        s.add(Reseller(panel_id=1, admin_uuid="owner", name="Owner", is_owner=True))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", parent_admin_uuid="owner")
        s.add(r)
        await s.flush()

        # A normal service created this month → billed on sold quota (10 GB) by the snapshot rule.
        s.add(EndUserSnapshot(
            panel_id=1, user_uuid="u1", name="u1", added_by_uuid="A",
            usage_limit_gb=10, current_usage_gb=2, start_date=today(),
            enable=True, last_synced_at=now,
        ))
        # A renew-by-edit on another service (start_date stayed in a PRIOR month, so the snapshot
        # rule misses it) → 20 GB captured by metering as edit_renewal for this period.
        s.add(UsageMeter(
            panel_id=1, user_uuid="u2", period_label=period.label,
            added_by_uuid="A", name="u2", edit_renewal_gb=20,
        ))
        await s.commit()

        # Real invoice for the period.
        summary = await invoicing.generate_invoices(s, period)
        inv = next(iter(summary.invoice_ids), None)
        assert inv is not None
        from app.models import Invoice
        invoice = await s.get(Invoice, inv)
        assert float(invoice.usage_gb) == 30.0  # 10 sold + 20 metered extra

        # Interim breakdown for the same node/period must match (own = 10 + 20).
        interim = await reseller_report.interim_breakdown(s, r, period)
        assert interim["total_gb"] == 30.0
        assert interim["own"]["gb"] == 30.0

    _run(body, tmp_path, "interim.db")
