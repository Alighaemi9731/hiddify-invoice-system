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
    p = SimpleNamespace(proxy_base="https://h.example/secret")
    assert PanelModel.user_sub_link(p, "UUID") == "https://h.example/secret/UUID/auto/"
    assert PanelModel.user_sub_link(p, "UUID", auto=False) == "https://h.example/secret/UUID/sub/"


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
        assert res.created[0].sub_link.endswith("/uuid-bob1/auto/")
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
