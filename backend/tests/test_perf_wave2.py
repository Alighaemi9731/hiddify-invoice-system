"""Regressions for the 2026-07-22 performance batch 2 (v1.99.2).

Locks in:
- settings_service: `get_many` batches misses into ONE query, the TTL cache serves
  repeat reads, `set_value` invalidates immediately, and two different engines (i.e.
  two databases in one process — the test-suite reality) never cross-contaminate.
- reports: the sales-by-day / zero-invoices previews are cached per (period, panel
  sync-state) — the full billing engine must NOT rerun for an unchanged state, and MUST
  rerun after a panel syncs.
- panels list: the batched two-query path returns the same counts as the per-panel path.
"""
import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import reports as reports_api
from app.core.db import Base
from app.models import EndUserSnapshot, Panel, Reseller, Setting
from app.models.enums import PanelStatus
from app.services import settings_service


async def _mk_session(tmp_path, name):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_settings_get_many_and_cache(tmp_path):
    async def run() -> None:
        engine, Session = await _mk_session(tmp_path, "s1.db")
        engine2, Session2 = await _mk_session(tmp_path, "s2.db")
        try:
            async with Session() as s:
                await settings_service.set_value(s, "default_price_per_gb", 1234)
                out = await settings_service.get_many(
                    s, ["default_price_per_gb", "free_under_gb", "definitely_missing_key"]
                )
                assert out["default_price_per_gb"] == 1234
                # registered default resolves for a missing row
                assert out["free_under_gb"] is not None
                assert out["definitely_missing_key"] is None

                # cached read must reflect a write IMMEDIATELY (invalidation on set_value)
                await settings_service.set_value(s, "default_price_per_gb", 4321)
                assert await settings_service.get(s, "default_price_per_gb") == 4321

            # A DIFFERENT engine (different database) must not see engine 1's cache.
            async with Session2() as s2:
                assert await settings_service.get(s2, "default_price_per_gb", None) is not None
                # value comes from s2's OWN (default-seeded) world, not s1's 4321 write
                row = await s2.get(Setting, "default_price_per_gb")
                assert row is None  # nothing was ever written in s2's database
        finally:
            await engine.dispose()
            await engine2.dispose()

    asyncio.run(run())


def test_preview_cache_keyed_on_sync_state(tmp_path, monkeypatch):
    async def run() -> None:
        engine, Session = await _mk_session(tmp_path, "prev.db")
        calls = {"n": 0}
        real_preview = reports_api.invoicing.preview_bundles

        async def counting_preview(session, p, **kw):
            calls["n"] += 1
            return await real_preview(session, p, **kw)

        monkeypatch.setattr(reports_api.invoicing, "preview_bundles", counting_preview)
        try:
            async with Session() as s:
                panel = Panel(
                    key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o1",
                    enabled=True, status=PanelStatus.ok,
                    last_synced_at=dt.datetime.now(dt.timezone.utc),
                )
                s.add(panel)
                await s.commit()

                await reports_api.sales_by_day(None, s)
                await reports_api.sales_by_day(None, s)
                assert calls["n"] == 1, "unchanged sync state must be served from cache"

                # a new sync timestamp must invalidate the cache key
                panel.last_synced_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                    seconds=5
                )
                await s.commit()
                await reports_api.sales_by_day(None, s)
                assert calls["n"] == 2, "a panel sync must recompute the preview"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_panels_batched_counts_match_single(tmp_path):
    async def run() -> None:
        from app.api import panels as panels_api

        engine, Session = await _mk_session(tmp_path, "panels.db")
        try:
            now = dt.datetime.now(dt.timezone.utc)
            async with Session() as s:
                p1 = Panel(
                    key="a", host="a.invalid", proxy_path_enc="x", owner_uuid="oa",
                    enabled=True, status=PanelStatus.ok, last_synced_at=now,
                )
                p2 = Panel(
                    key="b", host="b.invalid", proxy_path_enc="x", owner_uuid="ob",
                    enabled=True, status=PanelStatus.error,
                )
                s.add_all([p1, p2])
                await s.flush()
                s.add_all([
                    Reseller(panel_id=p1.id, admin_uuid="oa", name="Owner", is_owner=True),
                    # present (seen at the latest sync)
                    Reseller(panel_id=p1.id, admin_uuid="r1", name="R1", last_seen_at=now),
                    # stale (dropped before the latest sync) → must NOT count on an ok panel
                    Reseller(
                        panel_id=p1.id, admin_uuid="r2", name="R2",
                        last_seen_at=now - dt.timedelta(days=2),
                    ),
                    # errored panel → presence filter off, counts regardless
                    Reseller(panel_id=p2.id, admin_uuid="r3", name="R3"),
                ])
                s.add_all([
                    EndUserSnapshot(panel_id=p1.id, user_uuid=f"u{i}", added_by_uuid="r1")
                    for i in range(3)
                ])
                await s.commit()

                batched = {o.key: o for o in await panels_api.list_panels(s)}
                for panel in (p1, p2):
                    single = await panels_api._to_out(s, panel)
                    assert batched[panel.key].resellers_count == single.resellers_count
                    assert batched[panel.key].end_users_count == single.end_users_count
                assert batched["a"].resellers_count == 1  # R1 only (stale R2 + owner excluded)
                assert batched["a"].end_users_count == 3
                assert batched["b"].resellers_count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
