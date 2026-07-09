"""Reseller name search relevance: when many resellers share a term (e.g. "ali"), the EXACT
match must surface within the result window instead of being buried alphabetically past the
limit (the bug: searching "ali" returned dozens of "Ali…"/"Alireza…" but not the reseller
literally named "ali")."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/resellersearch.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import resellers as resellers_api  # noqa: E402
from app.models import Panel, Reseller  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_exact_match_ranks_first_within_limit(tmp_path):
    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o",
                    last_synced_at=now))
        await s.flush()
        # 30 CONTAINS-only names ("…ali…", not starting with ali) that sort before the exact
        # match alphabetically ("Aaa…"), plus the exact "ali" and a prefix "alireza".
        for i in range(30):
            s.add(Reseller(panel_id=1, admin_uuid=f"u{i}", name=f"Aaa vpnali {i:02d}",
                           last_seen_at=now))
        s.add(Reseller(panel_id=1, admin_uuid="exact", name="ali", last_seen_at=now))
        s.add(Reseller(panel_id=1, admin_uuid="pref", name="alireza", last_seen_at=now))
        await s.commit()

        rows = await resellers_api.list_resellers(
            panel_id=None, q="ali", include_owners=False, registered=None,
            top_level_only=False, limit=5, offset=0, session=s)
        names = [r.name for r in rows]
        # Exact "ali" first (rank 0), then prefix "alireza" (rank 1) — NOT buried past the
        # limit behind the 30 "…vpnali…" contains-only matches (rank 2).
        assert names[0] == "ali", names
        assert names[1] == "alireza", names

    _run(body, tmp_path, "s1.db")
