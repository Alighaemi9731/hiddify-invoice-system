"""Cascade admin deletion (Tools) + the owner-id rebind fix.

- `queue_admin_deletion` + `_process_delete_action`: deletes the subtree's users on the panel
  (bulk delete), then the subtree's admins DEEPEST-FIRST (Hiddify does NOT cascade sub-admins —
  it answers 500/MySQL-1451 while a child still references the parent), then purges the subtree
  from our DB while KEEPING the financial ledger. Owner is refused.
- The panel-admin phase is retry-capped and gives up as `failed` (→ owner alert) instead of
  retrying forever, and refuses to run at all once the subtree has grown past what was confirmed.
- `_is_owner_user`: a numeric `owner_telegram` is authoritative (a Settings change applies even
  when a stale `owner_chat_id` is pinned).
"""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/admindel.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    FinancialRecord,
    Panel,
    Reseller,
    UsageMeter,
)
from app.models.enums import EnforcementActionStatus, EnforcementState  # noqa: E402
from app.services import enforcement, settings_service  # noqa: E402


def _run(body, tmp_path, name="x.db"):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


class _FakeClient:
    """Captures the panel calls the cascade makes (no real HTTP)."""
    bulk_deleted: list[int] = []
    admins_deleted: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def get_user_id(self, panel, user_uuid, *, api_key=None):
        # The panel is the ONLY source of truth for numeric ids — the cascade delete deliberately
        # re-resolves every uuid instead of trusting the cached panel_user_id (a stale id here
        # would DELETE another reseller's user). See test_enforcement_stale_ids.py.
        return {"u1": 101, "u2": 102}.get(user_uuid)

    async def bulk_delete_users(self, panel, user_ids, *, api_key=None):
        _FakeClient.bulk_deleted.extend(int(i) for i in user_ids)

    async def delete_admin(self, panel, admin_uuid, *, api_key=None):
        _FakeClient.admins_deleted.append(admin_uuid)


def test_cascade_delete_removes_subtree_and_keeps_ledger(tmp_path, monkeypatch):
    async def body(s):
        _FakeClient.bulk_deleted = []
        _FakeClient.admins_deleted = []
        monkeypatch.setattr(enforcement, "AdminApiClient", _FakeClient)

        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        a = Reseller(panel_id=1, admin_uuid="A", name="Top", parent_admin_uuid="owner",
                     enforcement_state=EnforcementState.active)
        b = Reseller(panel_id=1, admin_uuid="B", name="Sub", parent_admin_uuid="A",
                     enforcement_state=EnforcementState.active)
        s.add_all([a, b])
        await s.flush()
        # users: u1 under A, u2 under B — both have a cached numeric id.
        s.add_all([
            EndUserSnapshot(panel_id=1, user_uuid="u1", name="u1", added_by_uuid="A",
                            panel_user_id=101, enable=True),
            EndUserSnapshot(panel_id=1, user_uuid="u2", name="u2", added_by_uuid="B",
                            panel_user_id=102, enable=True),
            UsageMeter(panel_id=1, user_uuid="u1", period_label="2026-06"),
        ])
        # A durable ledger row must survive the purge.
        s.add(FinancialRecord(panel_key="p", reseller_name="Top", period_label="2026-06",
                              amount_toman=1000, status="paid"))
        await s.commit()

        action = await enforcement.queue_admin_deletion(s, a)
        res = await enforcement._process_delete_action(s, action, user_chunk_size=500)
        assert res["done"] == 1

        # Panel: both users bulk-deleted; BOTH admins deleted, child strictly before the parent.
        # Deleting "A" first would fail with MySQL 1451 while "B" still points at it.
        assert sorted(_FakeClient.bulk_deleted) == [101, 102]
        assert _FakeClient.admins_deleted == ["B", "A"]

        # DB: users + meters + both resellers gone; ledger kept.
        assert (await s.execute(select(func.count(EndUserSnapshot.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(UsageMeter.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(Reseller.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(FinancialRecord.id)))).scalar_one() == 1
        assert action.status == EnforcementActionStatus.done

    _run(body, tmp_path, "c1.db")


def test_delete_order_is_deepest_first_and_cycle_safe():
    """Children strictly before parents; the root (parent outside the bundle) is always last."""
    def node(uuid, parent):
        return SimpleNamespace(admin_uuid=uuid, parent_admin_uuid=parent)

    # root A → B, C; B → D (a branch deeper than its sibling).
    order = enforcement._delete_order(
        [node("A", "owner"), node("B", "A"), node("C", "A"), node("D", "B")]
    )
    assert order[-1] == "A"                      # root last
    assert order.index("D") < order.index("B")   # grandchild before its parent
    assert order.index("B") < order.index("A")
    assert order.index("C") < order.index("A")

    # Single node, and a mutual-parent cycle (a panel restore can produce one) must terminate.
    assert enforcement._delete_order([node("A", "owner")]) == ["A"]
    assert sorted(enforcement._delete_order([node("X", "Y"), node("Y", "X")])) == ["X", "Y"]


def test_cascade_delete_gives_up_after_max_retries(tmp_path, monkeypatch):
    """A child that will not delete must not be retried forever: after _MAX_RETRIES the action
    goes `failed` (which is what raises the owner alert) instead of sitting `partial` for weeks."""
    class _Stubborn(_FakeClient):
        async def delete_admin(self, panel, admin_uuid, *, api_key=None):
            raise RuntimeError(
                'DELETE admin_user 500: {"msg":"(MySQLdb.IntegrityError) (1451, \'Cannot delete '
                "or update a parent row: a foreign key constraint fails')\"}"
            )

    async def body(s):
        monkeypatch.setattr(enforcement, "AdminApiClient", _Stubborn)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        a = Reseller(panel_id=1, admin_uuid="A", name="Top", parent_admin_uuid="owner")
        b = Reseller(panel_id=1, admin_uuid="B", name="Sub", parent_admin_uuid="A")
        s.add_all([a, b])
        await s.commit()

        action = await enforcement.queue_admin_deletion(s, a)
        for _ in range(enforcement._MAX_RETRIES - 1):
            res = await enforcement._process_delete_action(s, action, user_chunk_size=500)
            assert res["partial"] == 1
            assert action.status == EnforcementActionStatus.partial

        res = await enforcement._process_delete_action(s, action, user_chunk_size=500)
        assert res["failed"] == 1                       # → _notify_owner_failed fires
        assert action.status == EnforcementActionStatus.failed
        assert "1451" in (action.error or "")
        # Nothing was purged — the resellers are still there for the owner to deal with.
        assert (await s.execute(select(func.count(Reseller.id)))).scalar_one() == 2

    _run(body, tmp_path, "c4.db")


def test_cascade_delete_refuses_a_subtree_that_grew(tmp_path, monkeypatch):
    """A sub-admin created AFTER the owner confirmed the deletion must stop the action, not get
    silently destroyed along with its users (prod action #204, stuck 15 days while the reseller
    kept selling)."""
    async def body(s):
        _FakeClient.bulk_deleted = []
        _FakeClient.admins_deleted = []
        monkeypatch.setattr(enforcement, "AdminApiClient", _FakeClient)

        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        a = Reseller(panel_id=1, admin_uuid="A", name="Top", parent_admin_uuid="owner")
        s.add(a)
        await s.commit()

        action = await enforcement.queue_admin_deletion(s, a)   # snapshot records {"A"}
        # ... days pass, and the reseller adds a sub-reseller with a user of its own.
        s.add(Reseller(panel_id=1, admin_uuid="NEW", name="Late", parent_admin_uuid="A"))
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u9", name="u9", added_by_uuid="NEW",
                              enable=True))
        await s.commit()

        res = await enforcement._process_delete_action(s, action, user_chunk_size=500)
        assert res["failed"] == 1
        assert action.status == EnforcementActionStatus.failed
        assert "NEW" in (action.error or "")
        # Nothing touched: no panel calls, no rows removed.
        assert _FakeClient.admins_deleted == []
        assert _FakeClient.bulk_deleted == []
        assert (await s.execute(select(func.count(EndUserSnapshot.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(Reseller.id)))).scalar_one() == 2

    _run(body, tmp_path, "c5.db")


def test_queue_admin_deletion_refuses_owner(tmp_path):
    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        owner = Reseller(panel_id=1, admin_uuid="owner", name="Owner", is_owner=True)
        s.add(owner)
        await s.commit()
        with pytest.raises(ValueError):
            await enforcement.queue_admin_deletion(s, owner)
    _run(body, tmp_path, "c2.db")


def test_owner_telegram_numeric_is_authoritative(tmp_path):
    """A numeric owner_telegram set in Settings makes that id the owner even when a stale
    owner_chat_id is pinned — and re-pins owner_chat_id to it."""
    from app.bot import handlers

    async def body(s):
        # Old owner pinned; admin then sets owner_telegram to a NEW numeric id.
        await settings_service.set_value(s, "owner_chat_id", "111")
        await settings_service.set_value(s, "owner_telegram", "222")

        assert await handlers._is_owner_user(s, SimpleNamespace(id=222, username=None)) is True
        assert await handlers._is_owner_user(s, SimpleNamespace(id=111, username=None)) is False
        # owner_chat_id re-pinned to the new id.
        assert str(await settings_service.get(s, "owner_chat_id", "")) == "222"
    _run(body, tmp_path, "c3.db")
