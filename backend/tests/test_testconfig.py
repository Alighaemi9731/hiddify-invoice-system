"""The owner's one-tap test config: which admin builds it (billing!), which panel it lands on,
and the settings that bound it."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/tc.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import Panel  # noqa: E402
from app.services import settings_service, testconfig  # noqa: E402
from app.services.invoice_engine import compute_invoices  # noqa: E402
from app.services.panel_client import admin_api  # noqa: E402
from app.services.panel_client.admin_api import UserLimitError  # noqa: E402
from app.services.periods import month_period  # noqa: E402


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


def _panel(s, key: str, *, owner_uuid: str = "OWNER-UUID", enabled: bool = True) -> Panel:
    p = Panel(key=key, name=f"panel {key}", host=f"{key}.invalid", proxy_path_enc="x",
              owner_uuid=owner_uuid, enabled=enabled)
    p.proxy_path = "adminpath"
    p.client_proxy_path = "clientpath"  # avoids the on-demand backup fetch
    s.add(p)
    return p


# ---------------- create: identity is the panel's OWN super-admin ----------------

def test_create_posts_as_the_panel_super_admin_and_returns_the_client_link(monkeypatch):
    """The api_key MUST be the panel's owner uuid, not None.

    With None the header builder falls back to `panel.admin_api_key`, which may belong to a
    different admin entirely — and then the owner's free test would land in a RESELLER's invoice."""
    seen: dict = {}

    async def fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):  # noqa: ANN001
        seen.update(panel=panel, name=name, gb=gb, days=days, api_key=api_key, uuid=user_uuid)
        return user_uuid or "generated"

    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", fake_create)

    async def body(s):
        p = _panel(s, "p1")
        p.admin_api_key = "SOME-OTHER-ADMIN"  # must NOT be the identity we create with
        await s.commit()
        res = await testconfig.create(s, p, gb=2, days=2, name="test")
        assert res.ok and res.uuid
        assert seen["api_key"] == "OWNER-UUID"
        assert (seen["name"], seen["gb"], seen["days"]) == ("test", 2, 2)
        assert res.sub_link == p.user_sub_link(res.uuid, name="test")
        assert "clientpath" in res.sub_link  # the CLIENT path, not the admin one
    _run(body)


def test_create_reports_panel_user_limit_and_errors_without_raising(monkeypatch):
    async def limit(self, panel, **kw):  # noqa: ANN001, ANN003
        raise UserLimitError("max users")

    async def boom(self, panel, **kw):  # noqa: ANN001, ANN003
        raise RuntimeError("panel down")

    async def body(s):
        p = _panel(s, "p1")
        await s.commit()
        monkeypatch.setattr(admin_api.AdminApiClient, "create_user", limit)
        assert (await testconfig.create(s, p, gb=2, days=2, name="test")).reason == "limit"
        monkeypatch.setattr(admin_api.AdminApiClient, "create_user", boom)
        assert (await testconfig.create(s, p, gb=2, days=2, name="test")).reason == "error"
    _run(body)


def test_create_refuses_a_panel_without_a_super_admin_uuid(monkeypatch):
    async def never(self, panel, **kw):  # noqa: ANN001, ANN003
        raise AssertionError("must not reach the panel")

    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", never)

    async def body(s):
        p = _panel(s, "p1", owner_uuid="")
        await s.commit()
        res = await testconfig.create(s, p, gb=2, days=2, name="test")
        assert not res.ok and res.reason == "no_admin"
    _run(body)


# ---------------- options + panel resolution ----------------

def test_defaults_are_two_gb_two_days_named_test():
    async def body(s):
        await settings_service.seed_defaults(s)
        opts = await testconfig.load_options(s)
        assert (opts.gb, opts.days, opts.name, opts.panel_id) == (2, 2, "test", 0)
    _run(body)


def test_resolve_panel_uses_the_saved_one_and_never_guesses():
    async def body(s):
        await settings_service.seed_defaults(s)
        a, b = _panel(s, "aa"), _panel(s, "bb")
        await s.commit()

        # unset + several panels → ask; there is no defensible "first" panel to pick.
        panel, reason = await testconfig.resolve_panel(s)
        assert panel is None and reason == "unset"

        await testconfig.set_panel(s, b.id)
        panel, reason = await testconfig.resolve_panel(s)
        assert reason == "ok" and panel.id == b.id

        # a saved panel that got disabled must NOT silently fall back to the other one
        b.enabled = False
        await s.commit()
        panel, reason = await testconfig.resolve_panel(s)
        assert panel is None and reason == "missing"

        # unset + exactly ONE enabled panel → deterministic, nothing to choose between
        await testconfig.set_panel(s, 0)
        panel, reason = await testconfig.resolve_panel(s)
        assert reason == "ok" and panel.id == a.id
    _run(body)


def test_panel_choices_lists_only_enabled_panels_by_key():
    async def body(s):
        _panel(s, "bb")
        _panel(s, "aa")
        _panel(s, "zz", enabled=False)
        await s.commit()
        assert [label.split(" ")[0] for _, label in await testconfig.panel_choices(s)] == ["aa", "bb"]
    _run(body)


# ---------------- settings validation ----------------

def test_test_config_settings_are_bounded():
    assert settings_service.validate_api_value("test_config_panel_id", 0) == 0
    assert settings_service.validate_api_value("test_config_gb", 2) == 2
    assert settings_service.validate_api_value("test_config_days", 365) == 365
    assert settings_service.validate_api_value("test_config_name", "trial") == "trial"
    for key, bad in (("test_config_panel_id", -1), ("test_config_gb", 0),
                     ("test_config_days", 0), ("test_config_days", 366),
                     ("test_config_gb", "2")):
        with pytest.raises(ValueError):
            settings_service.validate_api_value(key, bad)


# ---------------- the owner pays: a super-admin config is in nobody's invoice ----------------

def test_a_config_owned_by_the_panel_admin_is_billed_to_nobody():
    """The whole reason `create` authenticates as the super-admin: `select_billable_roots` skips
    `is_owner` rows, so a 2 GB test (well over the 1 GB free threshold) bills no one."""
    owner = SimpleNamespace(admin_uuid="OWNER-UUID", parent_admin_uuid=None, is_owner=True,
                            exclude_from_billing=False, price_per_gb=None, name="Owner", id=1,
                            min_sale_toman=None)
    reseller = SimpleNamespace(admin_uuid="A", parent_admin_uuid="OWNER-UUID", is_owner=False,
                               exclude_from_billing=False, price_per_gb=None, name="Ali", id=2,
                               min_sale_toman=None)
    period = month_period(2026, 8)
    made_on = dt.date(2026, 8, 5)
    users = [
        SimpleNamespace(user_uuid="t1", added_by_uuid="OWNER-UUID", name="test",
                        start_date=made_on, usage_limit_gb=2.0),
        SimpleNamespace(user_uuid="r1", added_by_uuid="A", name="real",
                        start_date=made_on, usage_limit_gb=50.0),
    ]
    bundles = compute_invoices([owner, reseller], users, period,
                               default_price_per_gb=2000, excluded_usage_gb=set())
    assert [b.root.admin_uuid for b in bundles] == ["A"]                # no bundle for the owner
    assert bundles[0].total_gb == 50.0                                 # the test GB is nowhere
