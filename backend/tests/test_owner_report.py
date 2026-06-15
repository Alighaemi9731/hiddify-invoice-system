"""Owner report KPIs (app.services.owner_report) — period stats + health."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/ownerrep.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PanelStatus  # noqa: E402
from app.services import owner_report  # noqa: E402


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


def _inv(reseller_id, *, label, status, amount, users):
    y, m = (int(x) for x in label.split("-"))
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=dt.date(y, m, 1),
        period_end=dt.date(y, m, 28), period_label=label, status=status,
        amount_toman=amount, users_count=users, sent_at=dt.datetime.now(dt.timezone.utc),
    )


def test_period_stats_kpis(tmp_path):
    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o",
                    status=PanelStatus.ok, last_synced_at=dt.datetime.now(dt.timezone.utc)))
        a = Reseller(panel_id=1, admin_uuid="A", name="A")
        b = Reseller(panel_id=1, admin_uuid="B", name="B")
        c = Reseller(panel_id=1, admin_uuid="C", name="C")
        s.add_all([a, b, c])
        await s.flush()
        s.add_all([
            _inv(a.id, label="2026-06", status=InvoiceStatus.paid, amount=100000, users=5),
            _inv(b.id, label="2026-06", status=InvoiceStatus.sent, amount=50000, users=3),
            # a draft is NOT counted as delivered/billed
            _inv(c.id, label="2026-06", status=InvoiceStatus.draft, amount=99999, users=9),
        ])
        await s.commit()

        st = await owner_report.period_stats(s, "2026-06")
        assert st.billed == 150000
        assert st.collected == 100000
        assert st.outstanding == 50000
        assert st.collection_rate == 66.7
        assert st.invoices == 2 and st.paid == 1
        assert st.debtors == 1          # only B owes
        assert st.services == 8         # 5 + 3 (draft excluded)
        assert st.total_outstanding == 50000

        # render must not raise and must include the headline figure
        text = owner_report.render_period_stats(st)
        assert "آمار دورهٔ 2026-06" in text

        h = await owner_report.health(s)
        assert h.panels_total == 1 and h.panels_ok == 1
        assert h.problems == [] and h.pending_payments == 0

    _run(body, tmp_path, "ownerrep.db")
