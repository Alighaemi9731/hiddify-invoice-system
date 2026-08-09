"""Release B HTTP contracts for storefront plan administration."""
from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import Panel, Reseller, StorefrontBot, StorefrontPlan
from app.services import settings_service


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await body(session)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    # Pin the global price the below-cost floor is computed from, so the shipped default
    # can move without rewriting the plan prices below.
    await settings_service.set_value(session, "default_price_per_gb", 1000)
    panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    owner = Reseller(
        panel_id=panel.id, admin_uuid="owner", name="Owner", bot_chat_id=111,
        storefront_enabled=True,
    )
    foreign = Reseller(
        panel_id=panel.id, admin_uuid="foreign", name="Foreign", bot_chat_id=222,
        storefront_enabled=True,
    )
    session.add_all([owner, foreign])
    await session.flush()
    shop = StorefrontBot(
        reseller_id=owner.id, panel_id=panel.id, bot_token_enc="encrypted", config_version=1)
    other_shop = StorefrontBot(
        reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="foreign", config_version=1)
    session.add_all([shop, other_shop])
    await session.flush()
    first = StorefrontPlan(
        storefront_bot_id=shop.id, title="", gb=10, days=30, price_toman=100_000,
        enabled=True, sort_order=0,
    )
    foreign_plan = StorefrontPlan(
        storefront_bot_id=other_shop.id, title="", gb=99, days=99, price_toman=999,
        enabled=True, sort_order=0,
    )
    session.add_all([first, foreign_plan])
    await session.commit()
    return owner, shop, first, foreign_plan


def test_plan_http_etag_idempotency_cas_reorder_delete_and_history():
    async def body(session):  # noqa: ANN001
        owner, shop, first, foreign_plan = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            listed = await client.get(f"/api/portal/storefronts/{shop.id}/plans")
            assert listed.status_code == 200
            assert listed.headers["etag"] == '"sf-config-1"'
            assert [item["id"] for item in listed.json()["items"]] == [first.id]

            create_body = {"gb": 20, "days": 60, "price_toman": 250_000}
            headers = {"If-Match": '"sf-config-1"', "Idempotency-Key": "create-1"}
            created = await client.post(
                f"/api/portal/storefronts/{shop.id}/plans", json=create_body, headers=headers)
            assert created.status_code == 201
            assert created.headers["etag"] == '"sf-config-2"'
            created_id = created.json()["result"]["id"]
            assert created.json()["result"]["title"] == ""

            replay = await client.post(
                f"/api/portal/storefronts/{shop.id}/plans", json=create_body, headers=headers)
            assert replay.status_code == 201
            assert replay.json()["result"]["id"] == created_id
            assert replay.headers["etag"] == '"sf-config-2"'

            conflict = await client.post(
                f"/api/portal/storefronts/{shop.id}/plans",
                json={**create_body, "gb": 21},
                headers=headers,
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "idempotency_conflict"

            stale = await client.patch(
                f"/api/portal/storefronts/{shop.id}/plans/{first.id}",
                json={"gb": 11},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "stale-1"},
            )
            assert stale.status_code == 409
            assert stale.json()["detail"]["current_etag"] == '"sf-config-2"'

            foreign = await client.patch(
                f"/api/portal/storefronts/{shop.id}/plans/{foreign_plan.id}",
                json={"gb": 50},
                headers={"If-Match": '"sf-config-2"', "Idempotency-Key": "foreign-1"},
            )
            assert foreign.status_code == 404

            reordered = await client.put(
                f"/api/portal/storefronts/{shop.id}/plans/order",
                json={"plan_ids": [created_id, first.id]},
                headers={"If-Match": '"sf-config-2"', "Idempotency-Key": "order-1"},
            )
            assert reordered.status_code == 200
            assert reordered.headers["etag"] == '"sf-config-3"'
            assert reordered.json()["result"] == {
                "ordered_ids": [created_id, first.id],
                "count": 2,
                "config_version": 3,
            }
            listed_again = await client.get(f"/api/portal/storefronts/{shop.id}/plans")
            assert [item["id"] for item in listed_again.json()["items"]] == [created_id, first.id]

            deleted = await client.delete(
                f"/api/portal/storefronts/{shop.id}/plans/{created_id}",
                headers={"If-Match": '"sf-config-3"', "Idempotency-Key": "delete-1"},
            )
            assert deleted.status_code == 200
            assert deleted.headers["etag"] == '"sf-config-4"'

            history = await client.get(
                f"/api/portal/storefronts/{shop.id}/plans/{created_id}/history")
            assert history.status_code == 200
            assert {item["action"] for item in history.json()["items"]} == {
                "plan.create", "plan.reorder", "plan.delete",
            }

    _run(body)


def test_plan_request_validation_rejects_extra_empty_and_invalid_order():
    async def body(session):  # noqa: ANN001
        owner, shop, first, _foreign_plan = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        headers = {"If-Match": '"sf-config-1"', "Idempotency-Key": "validation"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            assert (await client.post(
                f"/api/portal/storefronts/{shop.id}/plans",
                json={"gb": 0, "days": 1, "price_toman": 0}, headers=headers,
            )).status_code == 422
            assert (await client.patch(
                f"/api/portal/storefronts/{shop.id}/plans/{first.id}",
                json={}, headers=headers,
            )).status_code == 422
            assert (await client.post(
                f"/api/portal/storefronts/{shop.id}/plans",
                json={"gb": 1, "days": 1, "price_toman": 0, "unexpected": True},
                headers=headers,
            )).status_code == 422
            assert (await client.put(
                f"/api/portal/storefronts/{shop.id}/plans/order",
                json={"plan_ids": [first.id, first.id]}, headers=headers,
            )).status_code == 422

    _run(body)


def test_a_below_cost_plan_is_refused_over_http_with_a_usable_explanation():
    async def body(session):  # noqa: ANN001
        owner, shop, first, _foreign_plan = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            # The shop's cost per GB must reach the client, or the form cannot show the floor.
            shops = await client.get("/api/portal/storefronts")
            assert shops.status_code == 200
            assert shops.json()[0]["cost_per_gb_toman"] == 1000

            # 10 GB at 50 T against a 10,000 T floor — the production ×1000 slip.
            refused = await client.post(
                f"/api/portal/storefronts/{shop.id}/plans",
                json={"gb": 10, "days": 30, "price_toman": 50},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "below-cost"},
            )
            assert refused.status_code == 422
            detail = refused.json()["detail"]
            assert detail["code"] == "below_cost"
            assert "50000" in detail["message"]

            # Disabling stays possible; re-enabling does not.
            off = await client.put(
                f"/api/portal/storefronts/{shop.id}/plans/{first.id}/enabled",
                json={"enabled": False},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "off"},
            )
            assert off.status_code == 200
            cheap = await client.patch(
                f"/api/portal/storefronts/{shop.id}/plans/{first.id}",
                json={"price_toman": 5},
                headers={"If-Match": off.headers["etag"], "Idempotency-Key": "cheap"},
            )
            assert cheap.status_code == 422
            assert cheap.json()["detail"]["code"] == "below_cost"

    _run(body)


def test_the_portal_reports_the_resellers_own_price_when_it_overrides_the_default():
    async def body(session):  # noqa: ANN001
        owner, _shop, _first, _foreign_plan = await _seed(session)
        owner.price_per_gb = 2500
        await session.commit()

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            shops = await client.get("/api/portal/storefronts")
            assert shops.json()[0]["cost_per_gb_toman"] == 2500

    _run(body)
