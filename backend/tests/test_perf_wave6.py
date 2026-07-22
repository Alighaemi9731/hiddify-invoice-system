"""Wave 6 parity: node_report with an injected shared panel-reseller list must equal
the self-loading path exactly (money figures shown to resellers), and the injected
list must not leak across panels."""
import asyncio
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import EndUserSnapshot, Panel, Reseller
from app.models.enums import PanelStatus
from app.services import reseller_report


async def _mk(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'w6.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_node_report_shared_context_parity(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path)
        try:
            async with Session() as s:
                now = dt.datetime.now(dt.timezone.utc)
                panel = Panel(key="p", host="p.invalid", proxy_path_enc="x",
                              owner_uuid="own", enabled=True, status=PanelStatus.ok,
                              last_synced_at=now)
                s.add(panel)
                await s.flush()
                root = Reseller(panel_id=panel.id, admin_uuid="root", name="Root",
                                parent_admin_uuid="own", last_seen_at=now)
                sub = Reseller(panel_id=panel.id, admin_uuid="sub", name="Sub",
                               parent_admin_uuid="root", last_seen_at=now, gb_cap=50)
                subsub = Reseller(panel_id=panel.id, admin_uuid="subsub", name="SubSub",
                                  parent_admin_uuid="sub", last_seen_at=now)
                s.add_all([root, sub, subsub])
                await s.flush()
                today = dt.date.today()
                s.add_all([
                    EndUserSnapshot(panel_id=panel.id, user_uuid=f"u{i}",
                                    added_by_uuid=admin, usage_limit_gb=5 + i,
                                    start_date=today, enable=True,
                                    last_synced_at=now)
                    for i, admin in enumerate(["sub", "sub", "subsub"])
                ])
                await s.commit()

                plain = await reseller_report.node_report(s, sub, months=3)
                panel_resellers = (
                    await s.execute(select(Reseller).where(Reseller.panel_id == panel.id))
                ).scalars().all()
                shared = await reseller_report.node_report(
                    s, sub, months=3, _panel_resellers=panel_resellers
                )
                assert shared == plain  # exact money/cap/user figures

                # a FOREIGN panel's list must not contaminate the subtree
                assert shared["sub_count"] == 1 and shared["total_users"] == 3
        finally:
            await engine.dispose()
    asyncio.run(run())
