"""Owner Tools API: search end-users by name/uuid, and remove one from billing (deletes its
snapshot + usage_meters; 404 on missing)."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/tools.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.tools import remove_end_user, search_end_users  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller, UsageMeter  # noqa: E402


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    s.add(Panel(id=1, key="p1", host="h", proxy_path_enc="x", owner_uuid="o"))
    s.add(Reseller(panel_id=1, admin_uuid="A", name="Ali"))
    s.add(EndUserSnapshot(panel_id=1, user_uuid="uuid-keep-1", name="Sara", added_by_uuid="A",
                          usage_limit_gb=1000, current_usage_gb=8))
    s.add(EndUserSnapshot(panel_id=1, user_uuid="uuid-other-2", name="Reza", added_by_uuid="A",
                          usage_limit_gb=30, current_usage_gb=2))
    s.add(UsageMeter(panel_id=1, user_uuid="uuid-keep-1", period_label="2026-06"))
    s.add(UsageMeter(panel_id=1, user_uuid="uuid-keep-1", period_label="2026-05"))
    await s.commit()


def test_search_by_name_and_uuid(tmp_path):
    async def body(s):
        await _seed(s)
        by_name = await search_end_users(q="sara", session=s)
        assert len(by_name) == 1 and by_name[0]["name"] == "Sara"
        assert by_name[0]["reseller_name"] == "Ali" and by_name[0]["usage_limit_gb"] == 1000
        by_uuid = await search_end_users(q="other-2", session=s)
        assert len(by_uuid) == 1 and by_uuid[0]["user_uuid"] == "uuid-other-2"
        assert await search_end_users(q="nope", session=s) == []
    _run(body, tmp_path)


def test_remove_deletes_snapshot_and_meters(tmp_path):
    async def body(s):
        await _seed(s)
        snap = (await s.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.user_uuid == "uuid-keep-1")
        )).scalar_one()

        out = await remove_end_user(snapshot_id=snap.id, session=s)
        assert out["user_uuid"] == "uuid-keep-1" and out["meters_deleted"] == 2

        # Snapshot + its meters gone; the other user untouched.
        assert (await s.execute(select(func.count(EndUserSnapshot.id)).where(
            EndUserSnapshot.user_uuid == "uuid-keep-1"))).scalar_one() == 0
        assert (await s.execute(select(func.count(UsageMeter.id)).where(
            UsageMeter.user_uuid == "uuid-keep-1"))).scalar_one() == 0
        assert (await s.execute(select(func.count(EndUserSnapshot.id)))).scalar_one() == 1

        with pytest.raises(HTTPException) as ei:
            await remove_end_user(snapshot_id=99999, session=s)
        assert ei.value.status_code == 404
    _run(body, tmp_path)
