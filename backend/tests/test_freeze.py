"""Limits-only «freeze»: zero a reseller subtree's max_users (block new-user creation) WITHOUT
disabling existing users — they stay ONLINE. Unfreeze == restore (re-applies the captured max_users,
no user writes). Escalation frozen→full-suspend recovers the real pre-freeze limits and disables users."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/freeze.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import EnforcementState  # noqa: E402
from app.services import enforcement, settings_service  # noqa: E402
from tests.panel_fakes import as_identity  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await coro_fn(Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(Session) -> int:
    async with Session() as s:
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                     panel_max_active_users=10, enforcement_state=EnforcementState.active)
        s.add(r)
        for u, uid in (("u1", 101), ("u2", 102)):
            s.add(EndUserSnapshot(panel_id=1, user_uuid=u, name=u, added_by_uuid="A",
                                  enable=True, panel_user_id=uid))
        await s.commit()
        return r.id


def _panel_sim(monkeypatch, set_calls, bulk_calls, *, start=(10, 10)):
    """Simulate the panel's admin-limits so get/set stay consistent across freeze→suspend→restore."""
    limits = {"A": list(start)}

    async def fake_get_limits(self, panel, admin_uuid, api_key=None):
        return tuple(limits.get(admin_uuid, [None, None]))

    async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
        set_calls.append((admin_uuid, mu, mau))
        limits[admin_uuid] = [mu, mau]

    async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
        bulk_calls.append((sorted(user_ids), enabled))

    async def fake_get_user_id(self, panel, user_uuid, *, api_key=None):
        return {"u1": 101, "u2": 102}.get(user_uuid)

    monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_get_user_id))


async def _process(Session) -> dict:
    async with Session() as s:
        return await enforcement.process_enforcement_queue(s, action_limit=5)


async def _state(Session, rid):
    """Fresh read of (enforcement_state, max_users_snapshot, {uuid: enable})."""
    async with Session() as s:
        r = await s.get(Reseller, rid)
        snaps = {x.user_uuid: x.enable for x in (await s.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.panel_id == 1))).scalars().all()}
        return r.enforcement_state, r.max_users_snapshot, snaps


async def _freeze(Session, rid):
    async with Session() as s:
        r = await s.get(Reseller, rid)
        return await enforcement.queue_freeze(s, r)


async def _suspend(Session, rid):
    async with Session() as s:
        r = await s.get(Reseller, rid)
        return await enforcement.enforce_reseller(s, r, dry_run=False)


async def _restore(Session, rid):
    async with Session() as s:
        r = await s.get(Reseller, rid)
        return await enforcement.queue_restore(s, r, reason="test")


def test_freeze_zeros_max_users_keeps_active_users_online(tmp_path, monkeypatch):
    async def body(Session):
        rid = await _seed(Session)
        set_calls: list = []
        bulk_calls: list = []
        _panel_sim(monkeypatch, set_calls, bulk_calls)

        action = await _freeze(Session, rid)
        assert action is not None
        res = await _process(Session)
        assert res["done"] == 1

        # max_users zeroed, max_active_users KEPT (10); existing users never touched.
        assert set_calls == [("A", 0, 10)]
        assert bulk_calls == []
        state, snap_mu, enables = await _state(Session, rid)
        assert state == EnforcementState.frozen
        assert all(enables.values())  # u1 & u2 still online
        assert snap_mu == 10  # real max_users captured for restore
    _run(body, tmp_path, "freeze.db")


def test_unfreeze_restores_max_users_without_user_writes(tmp_path, monkeypatch):
    async def body(Session):
        rid = await _seed(Session)
        set_calls: list = []
        bulk_calls: list = []
        _panel_sim(monkeypatch, set_calls, bulk_calls)

        await _freeze(Session, rid)
        await _process(Session)
        assert (await _state(Session, rid))[0] == EnforcementState.frozen

        restore = await _restore(Session, rid)  # unfreeze == restore
        assert restore is not None
        res = await _process(Session)
        assert res["done"] == 1

        # max_users restored; NO user enable/disable writes at all (they were never disabled).
        assert ("A", 10, 10) in set_calls
        assert bulk_calls == []
        state, snap_mu, enables = await _state(Session, rid)
        assert state == EnforcementState.active
        assert all(enables.values())
        assert snap_mu is None  # snapshot cleared on restore
    _run(body, tmp_path, "freeze.db")


def test_escalate_frozen_to_full_suspend_then_restore(tmp_path, monkeypatch):
    async def body(Session):
        rid = await _seed(Session)
        set_calls: list = []
        bulk_calls: list = []
        _panel_sim(monkeypatch, set_calls, bulk_calls)

        await _freeze(Session, rid)
        await _process(Session)
        assert (await _state(Session, rid))[0] == EnforcementState.frozen

        # Escalate to a full suspend: disables users AND zeros both limits.
        await _suspend(Session, rid)
        res = await _process(Session)
        assert res["done"] == 1
        state, snap_mu, enables = await _state(Session, rid)
        assert state == EnforcementState.enforced
        assert ("A", 0, 0) in set_calls           # both limits zeroed now
        assert ([101, 102], False) in bulk_calls  # users disabled
        assert not any(enables.values())
        assert snap_mu == 10  # real pre-freeze limit survived (so restore brings back 10, not 0)

        # Restore everything.
        await _restore(Session, rid)
        res = await _process(Session)
        assert res["done"] == 1
        state, _, enables = await _state(Session, rid)
        assert state == EnforcementState.active
        assert ("A", 10, 10) in set_calls
        assert ([101, 102], True) in bulk_calls   # users re-enabled
        assert all(enables.values())
    _run(body, tmp_path, "freeze.db")


def test_queue_freeze_is_idempotent_and_guards_state(tmp_path, monkeypatch):
    async def body(Session):
        rid = await _seed(Session)
        set_calls: list = []
        bulk_calls: list = []
        _panel_sim(monkeypatch, set_calls, bulk_calls)

        a1 = await _freeze(Session, rid)
        a2 = await _freeze(Session, rid)  # in-flight → same planned action
        assert a1 is not None and a2 is not None and a1.id == a2.id

        await _process(Session)
        # Already frozen → queue_freeze is a no-op (None).
        assert await _freeze(Session, rid) is None
    _run(body, tmp_path, "freeze.db")
