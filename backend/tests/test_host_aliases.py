"""Panel host_aliases: matcher-only old hosts. They keep stale reseller links matching after a
domain move, but never affect billing/backup/Admin-API (those derive from `host`)."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/alias.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.services.broadcast as B  # noqa: E402
from app.bot import handlers  # noqa: E402
from app.bot.matching import parse_link  # noqa: E402
from app.core import crypto  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Panel, Reseller  # noqa: E402


def _run(body, name):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_set_host_aliases_normalizes_dedups_drops_main_and_blank():
    p = Panel(key="p", host="New.Example.COM", proxy_path_enc="x", owner_uuid="o")
    p.set_host_aliases(["OLD.example.com.", "https://old.example.com/ignored", "", "new.example.com"])
    # normalized + de-duped; the entry equal to the (normalized) main host is dropped.
    assert p.host_alias_list == ["old.example.com"]


def test_admin_link_uses_given_or_current_host():
    p = Panel(key="p", host="new.example.com", proxy_path_enc=crypto.encrypt("secret/path"), owner_uuid="o")
    uuid = "e5ed5732-1b80-489d-ac17-269d254e5c49"
    assert p.admin_link(uuid, tag="PH") == f"https://new.example.com/secret/path/{uuid}/#PH"
    assert p.admin_link(uuid) == f"https://new.example.com/secret/path/{uuid}/"
    assert p.admin_link(uuid, host="old.example.com") == f"https://old.example.com/secret/path/{uuid}/"


def test_migration_text_has_both_links():
    p = Panel(key="p", host="new.example.com", proxy_path_enc=crypto.encrypt("path"), owner_uuid="o")
    uuid = "11111111-2222-3333-4444-555555555555"
    txt = B.migration_text(p, "old.example.com", uuid, "PH")
    assert f"https://new.example.com/path/{uuid}/#PH" in txt
    assert f"https://old.example.com/path/{uuid}/#PH" in txt
    assert "<code>" in txt  # tap-to-copy


def test_matcher_matches_main_and_alias_rejects_unrelated():
    uuid = "22222222-3333-4444-5555-666666666666"

    async def body(s):
        p = Panel(key="p", host="new.example.com", proxy_path_enc=crypto.encrypt("path"), owner_uuid="o")
        p.set_host_aliases(["old.example.com"])
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid=uuid, name="r")
        s.add(r)
        await s.commit()

        async def match(host):
            return await handlers._registration_candidate(s, parse_link(f"https://{host}/path/{uuid}/"))
        assert (await match("new.example.com")).id == r.id      # current host
        assert (await match("old.example.com")).id == r.id      # alias host
        assert await match("unrelated.example.com") is None     # neither
    _run(body, "m.db")


def test_matcher_ambiguous_returns_none():
    uuid = "33333333-4444-5555-6666-777777777777"

    async def body(s):
        # Two DIFFERENT panels sharing the same host + path, each with the same reseller uuid →
        # ambiguous → fail-closed None (never guess / never take the first).
        for key in ("p1", "p2"):
            p = Panel(key=key, host="same.example.com", proxy_path_enc=crypto.encrypt("path"), owner_uuid=key)
            s.add(p)
            await s.flush()
            s.add(Reseller(panel_id=p.id, admin_uuid=uuid, name=key))
        await s.commit()
        assert await handlers._registration_candidate(
            s, parse_link(f"https://same.example.com/path/{uuid}/")) is None
    _run(body, "amb.db")


def test_send_panels_shows_only_current_link_never_previous():
    uuid = "44444444-5555-6666-7777-888888888888"

    async def body(s):
        p = Panel(key="fa1", host="new.example.com", proxy_path_enc=crypto.encrypt("path"), owner_uuid="o")
        p.set_host_aliases(["old.example.com"])   # even WITH an alias…
        s.add(p)
        await s.flush()
        s.add(Reseller(panel_id=p.id, admin_uuid=uuid, name="r", bot_chat_id=555, link_tag="PH"))
        await s.commit()

        captured: list = []

        async def answer(text, **kwargs):
            captured.append(text)

        await handlers._send_panels(answer, 555, s)
        # …only the CURRENT-host link is shown; the old host is never exposed here.
        assert f"https://new.example.com/path/{uuid}/#PH" in captured[0]
        assert "old.example.com" not in captured[0]
        assert "قبلی" not in captured[0]
    _run(body, "panels.db")


def test_alias_only_update_does_not_resync_but_host_change_does():
    from app.api import panels
    from app.schemas.panel import PanelUpdate

    class _BG:
        def __init__(self):
            self.tasks: list = []

        def add_task(self, fn, *a, **k):
            self.tasks.append(fn)

    async def body(s):
        p = Panel(key="p", host="h.example.com", proxy_path_enc=crypto.encrypt("path"),
                  owner_uuid="o", enabled=True)
        s.add(p)
        await s.commit()

        bg1 = _BG()
        await panels.update_panel(p.id, PanelUpdate(host_aliases=["old.example.com"]), bg1, s)
        assert bg1.tasks == []                       # alias-only → NO re-sync
        assert p.host_alias_list == ["old.example.com"]

        bg2 = _BG()
        await panels.update_panel(p.id, PanelUpdate(host="moved.example.com"), bg2, s)
        assert len(bg2.tasks) == 1                   # host change → re-sync
    _run(body, "resync.db")
