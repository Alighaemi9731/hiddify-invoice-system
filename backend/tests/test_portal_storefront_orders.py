"""Plan 004 HTTP contracts: order detail (no panel call), throttled live refresh (429),
idempotent admin renew, absolute enable, and idempotent delete — all tenant-checked.

Uses a single shared-connection SQLite (StaticPool) so the route session and the internal
short sessions (renew/set_enabled/delete/refresh open their own via SessionLocal) share one DB."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
)
from app.services import storefront_admin, storefront_provision, storefront_subscription


class _FakePanel:
    calls: dict[str, int] = {}

    async def get_user(self, panel, uuid, api_key=None):  # noqa: ANN001, ANN201
        _FakePanel.calls["get_user"] = _FakePanel.calls.get("get_user", 0) + 1
        return {"current_usage_GB": 3.5, "usage_limit_GB": 10, "remaining_day": 20}

    async def set_user_enabled(self, panel, uuid, enabled, api_key=None):  # noqa: ANN001, ANN201
        _FakePanel.calls["set_user_enabled"] = _FakePanel.calls.get("set_user_enabled", 0) + 1

    async def delete_user(self, panel, uuid, api_key=None):  # noqa: ANN001, ANN201
        _FakePanel.calls["delete_user"] = _FakePanel.calls.get("delete_user", 0) + 1

    async def prepare_renew_user(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001, ANN201
        _FakePanel.calls["prepare"] = _FakePanel.calls.get("prepare", 0) + 1
        return SimpleNamespace(usage_limit_gb=float(gb), prior_start_date=None)

    async def apply_renew_user_target(self, panel, uuid, target, api_key=None):  # noqa: ANN001, ANN201
        _FakePanel.calls["apply"] = _FakePanel.calls.get("apply", 0) + 1


def _run(monkeypatch, body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool,
            connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        _FakePanel.calls = {}
        monkeypatch.setattr(storefront_provision, "AdminApiClient", _FakePanel)
        monkeypatch.setattr(storefront_subscription, "AdminApiClient", _FakePanel)
        monkeypatch.setattr(storefront_admin, "SessionLocal", factory)
        monkeypatch.setattr(storefront_provision, "SessionLocal", factory)

        async def session_override():
            async with factory() as s:
                yield s

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[await _owner(factory)])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        try:
            await body(factory)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _owner(factory):  # noqa: ANN001, ANN202
    async with factory() as s:
        return (await s.execute(
            __import__("sqlalchemy").select(Reseller).where(Reseller.bot_chat_id == 111)
        )).scalars().first()


async def _seed(factory):  # noqa: ANN001, ANN202
    async with factory() as s:
        panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        owner = Reseller(panel_id=panel.id, admin_uuid="owner-admin", name="Owner",
                         bot_chat_id=111, storefront_enabled=True)
        foreign = Reseller(panel_id=panel.id, admin_uuid="foreign-admin", name="Foreign",
                           bot_chat_id=222, storefront_enabled=True)
        s.add_all([owner, foreign])
        await s.flush()
        shop = StorefrontBot(reseller_id=owner.id, panel_id=panel.id, bot_token_enc="t",
                             config_version=1)
        other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2",
                              config_version=1)
        s.add_all([shop, other])
        await s.flush()
        a = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=501, name="Alice")
        fc = StorefrontCustomer(storefront_bot_id=other.id, telegram_id=502, name="Carol")
        s.add_all([a, fc])
        await s.flush()
        order = StorefrontOrder(customer_id=a.id, panel_id=panel.id, panel_user_uuid="uuid-a",
                                status="provisioned", gb=10, days=30, price_toman=50_000)
        forder = StorefrontOrder(customer_id=fc.id, panel_id=panel.id, panel_user_uuid="uuid-c",
                                 status="provisioned", gb=5, days=15, price_toman=20_000)
        s.add_all([order, forder])
        await s.commit()
        return shop.id, order.id, forder.id


def _client():  # noqa: ANN202
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _order_status(factory, order_id):  # noqa: ANN001, ANN202
    async def _q():
        async with factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            return o.status if o else None
    return _q()


def test_order_detail_is_stored_only_and_foreign_is_404(monkeypatch):  # noqa: ANN001
    async def body(factory):  # noqa: ANN001
        shop_id, order_id, forder_id = await _seed(factory)
        async with _client() as client:
            r = await client.get(f"/api/portal/storefronts/{shop_id}/orders/{order_id}")
            assert r.status_code == 200 and r.json()["status"] == "provisioned"
            assert _FakePanel.calls.get("get_user", 0) == 0  # detail NEVER hits the panel
            assert (await client.get(
                f"/api/portal/storefronts/{shop_id}/orders/{forder_id}")).status_code == 404

    _run(monkeypatch, body)


def test_live_refresh_returns_freshness_then_throttles_429(monkeypatch):  # noqa: ANN001
    async def body(factory):  # noqa: ANN001
        shop_id, order_id, forder_id = await _seed(factory)
        async with _client() as client:
            r = await client.post(f"/api/portal/storefronts/{shop_id}/orders/{order_id}/refresh")
            assert r.status_code == 200 and r.json()["freshness"] == "live"
            assert r.json()["status"]["used_gb"] == 3.5
            again = await client.post(f"/api/portal/storefronts/{shop_id}/orders/{order_id}/refresh")
            assert again.status_code == 429 and int(again.headers["retry-after"]) >= 1
            assert (await client.post(
                f"/api/portal/storefronts/{shop_id}/orders/{forder_id}/refresh")).status_code == 404

    _run(monkeypatch, body)


def test_admin_renew_is_free_and_idempotent(monkeypatch):  # noqa: ANN001
    async def body(factory):  # noqa: ANN001
        shop_id, order_id, forder_id = await _seed(factory)
        async with _client() as client:
            url = f"/api/portal/storefronts/{shop_id}/orders/{order_id}/renew"
            r = await client.post(url, headers={"Idempotency-Key": "renew-1"})
            assert r.status_code == 200 and r.json()["result"]["order"]["renewed"] is True
            applied = _FakePanel.calls.get("apply", 0)
            again = await client.post(url, headers={"Idempotency-Key": "renew-1"})
            assert again.status_code == 200
            assert _FakePanel.calls.get("apply", 0) == applied  # no second grant
            assert (await client.post(
                f"/api/portal/storefronts/{shop_id}/orders/{forder_id}/renew",
                headers={"Idempotency-Key": "renew-f"})).status_code == 404

    _run(monkeypatch, body)


def test_enable_absolute_and_delete_idempotent(monkeypatch):  # noqa: ANN001
    async def body(factory):  # noqa: ANN001
        shop_id, order_id, forder_id = await _seed(factory)
        async with _client() as client:
            en = f"/api/portal/storefronts/{shop_id}/orders/{order_id}/enabled"
            r = await client.put(en, json={"enabled": False}, headers={"Idempotency-Key": "en-1"})
            assert r.status_code == 200
            assert await _order_status(factory, order_id) == "disabled"

            de = f"/api/portal/storefronts/{shop_id}/orders/{order_id}"
            assert (await client.request(
                "DELETE", de, json={"confirm": "yes", "reason": "cleanup here"},
                headers={"Idempotency-Key": "del-bad"})).status_code == 422
            d = await client.request(
                "DELETE", de, json={"confirm": "DELETE", "reason": "customer left"},
                headers={"Idempotency-Key": "del-1"})
            assert d.status_code == 200
            assert await _order_status(factory, order_id) == "deleted"
            d2 = await client.request(
                "DELETE", de, json={"confirm": "DELETE", "reason": "customer left"},
                headers={"Idempotency-Key": "del-2"})
            assert d2.status_code == 200

    _run(monkeypatch, body)
