"""Enforcement resolves Hiddify numeric ids per TARGET user (single-user API), never the whole panel
list — gentler on big panels. Ids are cached on EndUserSnapshot.panel_user_id and reused (zero lookups
next time); a user absent on the panel (404 → None) is skipped."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enflight.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import EnforcementState  # noqa: E402
from app.services import enforcement, settings_service  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
    r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                 panel_max_active_users=10, enforcement_state=EnforcementState.active)
    s.add(r)
    # u1,u2 present on the panel; u3 was deleted from the panel (404 on lookup).
    for u in ("u1", "u2", "u3"):
        s.add(EndUserSnapshot(panel_id=1, user_uuid=u, name=u, added_by_uuid="A", enable=True))
    await s.flush()
    return r


def _no_whole_list(monkeypatch):
    async def boom(self, panel):
        raise AssertionError("get_user_ids (whole-panel list) must NOT be called")
    monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", boom)


def _stub_writes(monkeypatch, bulk_calls):
    async def fake_bulk(self, panel, user_ids, enabled):
        bulk_calls.append(sorted(user_ids))
    async def fake_get_limits(self, panel, admin_uuid, api_key=None):
        return (10, 10)
    async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
        return None
    monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)


def test_resolves_per_target_caches_and_skips_absent(tmp_path, monkeypatch):
    async def body(s):
        r = await _seed(s)
        await s.commit()

        _no_whole_list(monkeypatch)
        bulk_calls: list = []
        _stub_writes(monkeypatch, bulk_calls)

        looked_up: list = []
        async def fake_get_user_id(self, panel, user_uuid, *, api_key=None):
            looked_up.append(user_uuid)
            return {"u1": 101, "u2": 102}.get(user_uuid)  # u3 → None (404, absent)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_get_user_id)

        await enforcement.queue_enforcement(s, r, dry_run=False)
        res = await enforcement.process_enforcement_queue(s, action_limit=1)
        assert res["done"] == 1

        # Only the present users' ids were sent to the bulk action; u3 (404) was skipped.
        assert bulk_calls == [[101, 102]]
        assert set(looked_up) == {"u1", "u2", "u3"}

        # The resolved ids are cached on the snapshots for next time.
        rows = {r.user_uuid: r for r in (await s.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.panel_id == 1))).scalars().all()}
        assert rows["u1"].panel_user_id == 101
        assert rows["u2"].panel_user_id == 102
    _run(body, tmp_path, "enflight.db")


def test_cached_ids_mean_zero_lookups(tmp_path, monkeypatch):
    async def body(s):
        r = await _seed(s)
        # Pre-cache ids (as a prior run would have) — only present users.
        rows = {x.user_uuid: x for x in (await s.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.panel_id == 1))).scalars().all()}
        rows["u1"].panel_user_id = 101
        rows["u2"].panel_user_id = 102
        rows["u3"].panel_user_id = 103
        await s.commit()

        _no_whole_list(monkeypatch)
        bulk_calls: list = []
        _stub_writes(monkeypatch, bulk_calls)

        async def boom_single(self, panel, user_uuid, *, api_key=None):
            raise AssertionError("get_user_id must NOT be called when ids are cached")
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", boom_single)

        await enforcement.queue_enforcement(s, r, dry_run=False)
        res = await enforcement.process_enforcement_queue(s, action_limit=1)
        assert res["done"] == 1
        assert bulk_calls == [[101, 102, 103]]  # all from cache, no lookups
    _run(body, tmp_path, "enflight.db")
