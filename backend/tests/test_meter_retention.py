"""usage_meters retention: prune meters of periods older than meter_retention_months (default 6),
keeping recent ones — so the metering table stays bounded for long-lived users."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/meterret.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, UsageMeter  # noqa: E402
from app.models.enums import PanelStatus  # noqa: E402
from app.services import maintenance  # noqa: E402


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_old_period_meters_pruned_recent_kept(tmp_path):
    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o",
                    status=PanelStatus.ok, last_synced_at=now))
        # A present user (seen in the latest sync) so its meters aren't orphan-pruned.
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="U", start_date=dt.date.today(),
                              last_synced_at=now))
        old = maintenance._months_ago_label(12)   # well past the 6-month window
        recent = maintenance._months_ago_label(1)  # inside the window
        s.add(UsageMeter(panel_id=1, user_uuid="u1", period_label=old))
        s.add(UsageMeter(panel_id=1, user_uuid="u1", period_label=recent))
        await s.commit()

        counts = await maintenance.prune_stale_snapshots(s)
        assert counts["old_meters"] == 1

        labels = set((await s.execute(select(UsageMeter.period_label))).scalars().all())
        assert labels == {recent}
        assert (await s.execute(select(func.count(EndUserSnapshot.id)))).scalar_one() == 1
    _run(body, tmp_path)
