"""Cascade admin deletion (Tools) + the owner-id rebind fix.

- `queue_admin_deletion` + `_process_delete_action`: deletes the subtree's users on the panel
  (bulk delete), then the admin (cascades sub-admins), then purges the subtree from our DB while
  KEEPING the financial ledger. Owner is refused.
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
        return None  # ids come from cached panel_user_id in these tests

    async def bulk_delete_users(self, panel, user_ids):
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

        # Panel: both users bulk-deleted; the top admin deleted (Hiddify cascades sub-admins).
        assert sorted(_FakeClient.bulk_deleted) == [101, 102]
        assert _FakeClient.admins_deleted == ["A"]

        # DB: users + meters + both resellers gone; ledger kept.
        assert (await s.execute(select(func.count(EndUserSnapshot.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(UsageMeter.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(Reseller.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(FinancialRecord.id)))).scalar_one() == 1
        assert action.status == EnforcementActionStatus.done

    _run(body, tmp_path, "c1.db")


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
