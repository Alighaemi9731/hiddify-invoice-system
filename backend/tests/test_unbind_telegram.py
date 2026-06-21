"""Owner can release a reseller's Telegram binding so they can re-register from a new account —
clears bot_chat_id/link_tag/registered_at for that row only; 404 for missing; idempotent."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/unbind.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.resellers import unbind_telegram  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Panel, Reseller  # noqa: E402


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_unbind_clears_binding_and_is_idempotent(tmp_path):
    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", bot_chat_id=555,
                     link_tag="tag", registered_at=dt.datetime.now(dt.timezone.utc))
        s.add(r)
        await s.commit()

        out = await unbind_telegram(reseller_id=r.id, session=s)
        assert out == {"reseller_id": r.id, "name": "R", "registered": False}
        await s.refresh(r)
        assert r.bot_chat_id is None and r.link_tag is None and r.registered_at is None

        # Idempotent: a second call on an already-unbound reseller still succeeds.
        out2 = await unbind_telegram(reseller_id=r.id, session=s)
        assert out2["registered"] is False

    _run(body, tmp_path)


def test_unbind_missing_reseller_404(tmp_path):
    async def body(s):
        with pytest.raises(HTTPException) as ei:
            await unbind_telegram(reseller_id=9999, session=s)
        assert ei.value.status_code == 404
    _run(body, tmp_path)
