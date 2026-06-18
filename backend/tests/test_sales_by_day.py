"""Daily sale trend: one bucket per day of the month, height = that day's sale by service creation
date. Mirrors the invoice engine (present-filtered), zero-filled, correct month lengths."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sbd.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.reports import sales_by_day  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import PanelStatus  # noqa: E402

NOW = dt.datetime.now()  # naive → SQLite-consistent present-filter comparisons


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _u(panel_id, added_by, gb, day):
    return EndUserSnapshot(
        panel_id=panel_id, user_uuid=f"{added_by}-{gb}-{day}", added_by_uuid=added_by,
        usage_limit_gb=gb, current_usage_gb=0, start_date=dt.date(2026, 6, day),
        last_synced_at=NOW, enable=True, is_active=True, name=f"u{day}")


async def _seed(s):
    p = Panel(key="p1", host="p1", proxy_path_enc="x", owner_uuid="OWNER",
              status=PanelStatus.ok, last_synced_at=NOW)
    s.add(p)
    await s.flush()
    s.add_all([
        Reseller(panel_id=p.id, admin_uuid="OWNER", name="Owner", is_owner=True, last_seen_at=NOW),
        Reseller(panel_id=p.id, admin_uuid="ROOT", parent_admin_uuid="OWNER", name="Root",
                 price_per_gb=2000, last_seen_at=NOW),
    ])
    return p


def test_buckets_per_day_zero_filled_and_amounts():
    async def body(s):
        p = await _seed(s)
        s.add_all([
            _u(p.id, "ROOT", 10, 5),    # day 5: 10×2000 = 20,000
            _u(p.id, "ROOT", 5, 5),     # day 5 again: +5×2000 = 10,000 → 30,000
            _u(p.id, "ROOT", 3, 17),    # day 17: 3×2000 = 6,000
        ])
        await s.commit()

        rows = await sales_by_day(period="2026-06", session=s)
        assert len(rows) == 30                      # June has 30 days
        assert rows[0].day == 1 and rows[0].date == "2026-06-01"
        amounts = {r.day: r.amount_toman for r in rows}
        assert amounts[5] == 30_000
        assert amounts[17] == 6_000
        assert amounts[1] == 0 and amounts[30] == 0  # zero-filled
        assert sum(r.amount_toman for r in rows) == 36_000  # ≈ base bundle (18 GB × 2000)
    _run(body)


def test_present_filter_excludes_removed_reseller_and_failed_panel():
    async def body(s):
        p = await _seed(s)
        # a removed root (stale last_seen) with a big day-10 user → must NOT count
        s.add(Reseller(panel_id=p.id, admin_uuid="GONE", parent_admin_uuid="OWNER", name="Gone",
                       price_per_gb=2000, last_seen_at=NOW - dt.timedelta(days=5)))
        s.add(_u(p.id, "GONE", 50, 10))
        await s.commit()
        rows = await sales_by_day(period="2026-06", session=s)
        assert all(r.amount_toman == 0 for r in rows)   # removed reseller's user excluded

        # a failed-sync panel → its sales are excluded entirely
        await s.execute(Panel.__table__.update().values(status=PanelStatus.error))
        await s.commit()
        rows2 = await sales_by_day(period="2026-06", session=s)
        assert all(r.amount_toman == 0 for r in rows2)
    _run(body)


def test_month_lengths():
    async def body(s):
        await _seed(s)
        await s.commit()
        assert len(await sales_by_day(period="2026-02", session=s)) == 28   # 2026 not leap
        assert len(await sales_by_day(period="2024-02", session=s)) == 29   # leap
        assert len(await sales_by_day(period="2026-04", session=s)) == 30
        assert len(await sales_by_day(period="2026-07", session=s)) == 31
    _run(body)
