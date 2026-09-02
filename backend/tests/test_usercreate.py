"""Reseller bot user-creation: the v2 create POST, the auto sub-link, settings list validation,
and the capacity guard."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/uc.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.panel import Panel as PanelModel  # noqa: E402
from app.services import settings_service, usercreate  # noqa: E402
from app.services.panel_client import admin_api  # noqa: E402
from app.services.panel_client.admin_api import (  # noqa: E402
    AdminApiClient,
    UserLimitError,
)


class _Response:
    def __init__(self, status_code: int, *, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


def _panel():
    return SimpleNamespace(
        key="test", owner_uuid="owner", admin_api_key=None,
        admin_api_base="https://panel.example/proxy/api/v2/admin",
        proxy_base="https://panel.example/proxy",
    )


# ---------------- create_user (mocked HTTP) ----------------
def test_create_user_posts_as_reseller_and_returns_uuid(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return _Response(200, json_data={"uuid": json["uuid"], "name": json["name"]})

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    uid = asyncio.run(AdminApiClient().create_user(
        _panel(), name="cust", gb=30, days=60, api_key="reseller-uuid"))
    assert captured["url"].endswith("/api/v2/admin/user/")
    assert captured["headers"]["Hiddify-API-Key"] == "reseller-uuid"  # acts AS the reseller
    b = captured["body"]
    assert b["name"] == "cust" and b["usage_limit_GB"] == 30.0
    assert b["package_days"] == 60 and b["enable"] is True
    assert "added_by_uuid" not in b  # let the panel default it to the calling admin
    assert b["uuid"] == uid


def test_create_user_max_users_rejection_is_limit_error(monkeypatch):
    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, headers, json):
            return _Response(403, text="You have reached your max users limit")

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    with pytest.raises(UserLimitError):
        asyncio.run(AdminApiClient().create_user(_panel(), name="x", gb=20, days=30, api_key="r"))


def test_create_user_other_error_is_runtime(monkeypatch):
    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, headers, json):
            return _Response(500, text="boom")

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    with pytest.raises(RuntimeError):
        asyncio.run(AdminApiClient().create_user(_panel(), name="x", gb=20, days=30, api_key="r"))


# ---------------- sub link ----------------
def test_user_sub_link_shape():
    # Uses the CLIENT proxy path + a #name slug (matches the Hiddify panel's share link).
    p = SimpleNamespace(host="h.example", client_proxy_path="CLIENT", proxy_path="ADMIN")
    assert PanelModel.user_sub_link(p, "U", name="milad store33") == "https://h.example/CLIENT/U/#milad-store33"
    assert PanelModel.user_sub_link(p, "U", name="تکی اول تست") == "https://h.example/CLIENT/U/#تکی-اول-تست"
    assert PanelModel.user_sub_link(p, "U") == "https://h.example/CLIENT/U/"
    # falls back to the admin path only when the client path isn't captured yet
    p2 = SimpleNamespace(host="h.example", client_proxy_path=None, proxy_path="ADMIN")
    assert PanelModel.user_sub_link(p2, "U") == "https://h.example/ADMIN/U/"


def test_parse_backup_extracts_client_proxy_path():
    from app.services.panel_client.base import parse_backup
    pd = parse_backup({"users": [], "admin_users": [], "hconfigs": [
        {"key": "proxy_path_admin", "value": "ADM", "child_unique_id": ""},
        {"key": "proxy_path_client", "value": "CLI", "child_unique_id": ""},
    ]})
    assert pd.client_proxy_path == "CLI"
    # tolerate the enum-repr key form + strip slashes
    pd2 = parse_backup({"hconfigs": [{"key": "ConfigEnum.proxy_path_client", "value": "/CLI2/"}]})
    assert pd2.client_proxy_path == "CLI2"
    assert parse_backup({"users": []}).client_proxy_path is None


# ---------------- settings list validation ----------------
def test_user_create_list_validation():
    assert settings_service.validate_api_value("user_create_gb_options", [50, 20, 20, 30]) == [20, 30, 50]
    assert settings_service.validate_api_value("user_create_day_options", [60, 30]) == [30, 60]
    for bad in ([0, 30], [-5], [], ["x"], [3.5e9]):
        with pytest.raises(ValueError):
            settings_service.validate_api_value("user_create_bulk_counts", bad)


# ---------------- capacity guard (DB) ----------------
def _run(body):
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


async def _seed(s, *, max_users, existing):
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    p.client_proxy_path = "clientpath"  # encrypts; avoids the on-demand backup fetch
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="A", name="Ali", bot_chat_id=111, panel_max_users=max_users)
    s.add(r)
    await s.flush()
    for i in range(existing):
        s.add(EndUserSnapshot(panel_id=p.id, user_uuid=f"u{i}", added_by_uuid="A"))
    await s.commit()
    return r


def test_capacity_guard_blocks_over_max():
    async def body(s):
        r = await _seed(s, max_users=2, existing=1)
        res = await usercreate.create_for_reseller(s, r, count=2, gb=20, days=30, base_name="x")
        assert res.capacity_blocked and not res.created
        assert res.max_users == 2 and res.remaining == 1
    _run(body)


def test_create_loop_success(monkeypatch):
    async def _fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):
        return f"uuid-{name}"
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", _fake_create)

    async def body(s):
        r = await _seed(s, max_users=10, existing=0)
        res = await usercreate.create_for_reseller(s, r, count=3, gb=50, days=60, base_name="bob")
        assert not res.capacity_blocked and not res.limit_hit and res.error is None
        assert [u.name for u in res.created] == ["bob1", "bob2", "bob3"]
        assert res.created[0].sub_link == "https://p1.invalid/clientpath/uuid-bob1/#bob1"
    _run(body)


def test_create_loop_stops_on_limit(monkeypatch):
    calls = {"n": 0}

    async def _fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise UserLimitError("max users")
        return f"uuid-{name}"
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", _fake_create)

    async def body(s):
        r = await _seed(s, max_users=100, existing=0)
        res = await usercreate.create_for_reseller(s, r, count=5, gb=20, days=30, base_name="c")
        assert res.limit_hit and len(res.created) == 1  # first ok, second hit the limit → stop
    _run(body)


async def _seed_with_sync(s, *, max_users, fresh, stale):
    """`fresh` users present in the panel's latest sync + `stale` retained rows from an earlier
    sync (users since deleted from Hiddify — kept for billing, but occupying no panel slot)."""
    import datetime as dt

    synced = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o",
              last_synced_at=synced)
    p.client_proxy_path = "clientpath"
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="A", name="Ali", bot_chat_id=111,
                 panel_max_users=max_users)
    s.add(r)
    await s.flush()
    for i in range(fresh):
        # Sync stamps ONE `now` on the panel and every user it saw → present means EQUAL.
        s.add(EndUserSnapshot(panel_id=p.id, user_uuid=f"fresh{i}", added_by_uuid="A",
                              last_synced_at=synced))
    for i in range(stale):
        s.add(EndUserSnapshot(panel_id=p.id, user_uuid=f"stale{i}", added_by_uuid="A",
                              last_synced_at=synced - dt.timedelta(days=3)))
    await s.commit()
    return r


def test_fresh_users_count_against_capacity():
    async def body(s):
        r = await _seed_with_sync(s, max_users=3, fresh=3, stale=0)
        assert await usercreate.current_user_count(s, r) == 3
        res = await usercreate.create_for_reseller(s, r, count=1, gb=20, days=30, base_name="x")
        assert res.capacity_blocked
    _run(body)


def test_deleted_users_retained_for_billing_do_not_occupy_capacity():
    """The bug: a reseller who deleted services could not create new ones.

    Snapshots of users removed from Hiddify are kept deliberately (billing must still charge for a
    service sold and then removed mid-month). But they hold no slot on the panel, so counting them
    as occupied capacity blocked creates that Hiddify itself would have accepted — the reseller was
    locked out of quota they had actually freed.
    """
    async def body(s):
        r = await _seed_with_sync(s, max_users=3, fresh=1, stale=2)
        assert await usercreate.current_user_count(s, r) == 1, "stale rows still counted"
        res = await usercreate.create_for_reseller(s, r, count=2, gb=20, days=30, base_name="x")
        assert not res.capacity_blocked
        assert res.remaining == 2
        # …and the retained rows are still in the DB: billing keeps reading them.
        from sqlalchemy import func as _f
        from sqlalchemy import select as _s
        total = (await s.execute(_s(_f.count(EndUserSnapshot.user_uuid)))).scalar_one()
        assert total == 3, "capacity fix must not delete retained snapshots"
    _run(body)


def test_stale_rows_still_block_when_they_would_exceed_nothing():
    """Mixed set: remaining capacity reflects only the users actually present."""
    async def body(s):
        r = await _seed_with_sync(s, max_users=5, fresh=2, stale=3)
        assert await usercreate.current_user_count(s, r) == 2
        res = await usercreate.create_for_reseller(s, r, count=4, gb=20, days=30, base_name="x")
        assert res.capacity_blocked and res.remaining == 3
    _run(body)


def test_never_synced_panel_counts_every_snapshot():
    """Fail-open: with no successful sync we cannot claim anything was deleted. Counting them as
    gone would hand out capacity that may well be occupied."""
    async def body(s):
        r = await _seed(s, max_users=5, existing=3)   # no last_synced_at anywhere
        assert await usercreate.current_user_count(s, r) == 3
    _run(body)


def test_capacity_count_is_case_insensitive_on_added_by():
    async def body(s):
        import datetime as dt
        synced = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
        r = await _seed_with_sync(s, max_users=9, fresh=0, stale=0)
        s.add(EndUserSnapshot(panel_id=r.panel_id, user_uuid="mixed", added_by_uuid="a",
                              last_synced_at=synced))
        await s.commit()
        assert await usercreate.current_user_count(s, r) == 1
    _run(body)


# ---------------- suspension / freeze gate ----------------
# Why these exist: a fully suspended reseller (users disabled, limits 0/0, panel password locked)
# kept minting users through the bot's «ساخت کاربر». Nothing downstream refuses — the create
# authenticates with the reseller's API KEY (Hiddify's password lock only closes the UUID-link web
# login) and Hiddify's REST create never consults `max_users` (`can_have_more_users()` is a
# Flask-Admin UI check). 11 suspended resellers / 46 users were found in production on 2026-09-02;
# the daily re-assert killed each user within a day, but never the door.
from app.models.enums import EnforcementState  # noqa: E402


def _must_not_create(monkeypatch):
    async def _boom(self, panel, *, name, gb, days, api_key=None, user_uuid=None):
        raise AssertionError("panel create must not be attempted for a cut-off reseller")
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", _boom)


def test_suspended_reseller_cannot_create(monkeypatch):
    _must_not_create(monkeypatch)

    async def body(s):
        r = await _seed(s, max_users=100, existing=0)   # plenty of capacity — the gate, not the cap
        r.enforcement_state = EnforcementState.enforced
        await s.commit()
        res = await usercreate.create_for_reseller(s, r, count=1, gb=20, days=30, base_name="x")
        assert res.suspended and res.blocked_reason == "suspended"
        assert not res.created and not res.capacity_blocked and res.error is None
    _run(body)


def test_frozen_reseller_cannot_create(monkeypatch):
    """Freeze = «توقف ساخت کاربر» by definition; the bot must honour it before the next sync
    mirrors the zeroed limit."""
    _must_not_create(monkeypatch)

    async def body(s):
        r = await _seed(s, max_users=100, existing=0)
        r.enforcement_state = EnforcementState.frozen
        await s.commit()
        res = await usercreate.create_for_reseller(s, r, count=1, gb=20, days=30, base_name="x")
        assert res.suspended and res.blocked_reason == "frozen"
    _run(body)


def test_suspended_ancestor_blocks_the_sub(monkeypatch):
    """Ancestor-aware like the storefront gate: a sub inside a suspended branch is cut off too."""
    _must_not_create(monkeypatch)

    async def body(s):
        p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
        p.client_proxy_path = "clientpath"
        s.add(p)
        await s.flush()
        s.add_all([
            Reseller(panel_id=p.id, admin_uuid="OWNER", name="Owner", is_owner=True),
            Reseller(panel_id=p.id, admin_uuid="TL", parent_admin_uuid="OWNER", name="TL",
                     enforcement_state=EnforcementState.enforced, panel_max_users=100),
            Reseller(panel_id=p.id, admin_uuid="SUB", parent_admin_uuid="TL", name="SUB",
                     enforcement_state=EnforcementState.active, panel_max_users=100),
        ])
        await s.commit()
        from sqlalchemy import select as _s
        sub = (await s.execute(_s(Reseller).where(Reseller.admin_uuid == "SUB"))).scalar_one()
        res = await usercreate.create_for_reseller(s, sub, count=1, gb=20, days=30, base_name="x")
        assert res.suspended and res.blocked_reason == "suspended"
    _run(body)


def test_zero_max_users_is_a_cap_not_unlimited(monkeypatch):
    """`if reseller.panel_max_users:` skipped the guard exactly when a suspension/freeze had
    zeroed the limit — 0 read as "unlimited". 0 is the strictest cap there is."""
    _must_not_create(monkeypatch)

    async def body(s):
        r = await _seed(s, max_users=0, existing=0)
        res = await usercreate.create_for_reseller(s, r, count=1, gb=20, days=30, base_name="x")
        assert res.capacity_blocked and res.remaining == 0 and res.max_users == 0
        assert not res.created
    _run(body)


def test_active_reseller_still_creates(monkeypatch):
    """The gate must not over-reach: an active top-level reseller with room creates as before."""
    async def _fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):
        return f"uuid-{name}"
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", _fake_create)

    async def body(s):
        r = await _seed(s, max_users=5, existing=0)
        assert await usercreate.creation_blocked(s, r) is None
        res = await usercreate.create_for_reseller(s, r, count=1, gb=20, days=30, base_name="ok")
        assert not res.suspended and [u.name for u in res.created] == ["ok"]
    _run(body)


# ---------------- bot entry («➕ ساخت کاربر») refuses before the wizard ----------------
class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1

    async def update_data(self, **kw) -> None:
        self.data.update(kw)


def _enable_feature(monkeypatch):
    async def _opts(session):
        return usercreate.CreateOptions(enabled=True, gb=[20, 30], days=[30], counts=[5])
    monkeypatch.setattr(usercreate, "load_options", _opts)


async def _seed_top_level(s, *, panel_key, chat_id, state):
    p = Panel(key=panel_key, host=f"{panel_key}.invalid", proxy_path_enc="x", owner_uuid="o")
    p.client_proxy_path = "clientpath"
    s.add(p)
    await s.flush()
    owner = Reseller(panel_id=p.id, admin_uuid=f"OWNER-{panel_key}", name="Owner", is_owner=True)
    tl = Reseller(panel_id=p.id, admin_uuid=f"TL-{panel_key}", parent_admin_uuid=owner.admin_uuid,
                  name=f"TL {panel_key}", bot_chat_id=chat_id, enforcement_state=state,
                  panel_max_users=100)
    s.add_all([owner, tl])
    await s.commit()
    return tl


def test_begin_create_user_refuses_suspended_reseller(monkeypatch):
    from app.bot.handlers import views

    _enable_feature(monkeypatch)
    sent: list[str] = []

    async def answer(text, **kw):
        sent.append(text)

    async def body(s):
        await _seed_top_level(s, panel_key="p1", chat_id=111, state=EnforcementState.enforced)
        st = _FakeState()
        await views._begin_create_user(answer, 111, s, st)
        assert len(sent) == 1 and "مسدود شده" in sent[0], sent
        assert "cu_reseller_id" not in st.data        # the wizard never started
    _run(body)


def test_begin_create_user_skips_only_the_suspended_panel(monkeypatch):
    """Top-level on two panels, one suspended: the flow continues on the healthy one alone."""
    from app.bot.handlers import views

    _enable_feature(monkeypatch)
    sent: list[str] = []

    async def answer(text, **kw):
        sent.append(text)

    async def body(s):
        await _seed_top_level(s, panel_key="p1", chat_id=111, state=EnforcementState.enforced)
        good = await _seed_top_level(s, panel_key="p2", chat_id=111, state=EnforcementState.active)
        st = _FakeState()
        await views._begin_create_user(answer, 111, s, st)
        assert len(sent) == 1 and "مسدود شده" not in sent[0], sent
        assert st.data.get("cu_reseller_id") == good.id   # single-root path, the healthy account
    _run(body)
