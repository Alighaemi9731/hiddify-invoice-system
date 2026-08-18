"""Release B HTTP contracts for storefront settings, managers, channel and pure preview."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import portal_storefront
from app.core import crypto
from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontPlan,
)
from app.models.setting import Setting
from app.services import settings_service


class _FakeBotSession:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeTelegramBot:
    instances: list[_FakeTelegramBot] = []

    def __init__(self, token: str, session=None) -> None:  # noqa: ANN001
        assert token == "123456:TESTTOKENVALUE"
        # Production passes `session=new_session()` so every Bot shares one TLS trust store
        # (app/bot/session.py). The double ignores it and keeps its own closable stand-in — the
        # assertions below are about the session being CLOSED, not about which one it is.
        self.session = _FakeBotSession()
        self.instances.append(self)

    async def get_me(self):  # noqa: ANN201
        return SimpleNamespace(id=999)

    async def get_chat_member(self, channel_id: str, bot_id: int):  # noqa: ANN201
        assert channel_id == "@validchannel" and bot_id == 999
        return SimpleNamespace(status=SimpleNamespace(value="administrator"))

    async def get_chat(self, channel_id: str):  # noqa: ANN201
        assert channel_id == "@validchannel"
        return SimpleNamespace(username="validchannel")

    async def create_chat_invite_link(self, _channel_id: str):  # noqa: ANN201
        raise AssertionError("public channel must not create an invite")


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
    panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    owner = Reseller(
        panel_id=panel.id, admin_uuid="owner", name="Owner", bot_chat_id=111,
        storefront_enabled=True,
    )
    session.add(owner)
    await session.flush()
    shop = StorefrontBot(
        reseller_id=owner.id,
        panel_id=panel.id,
        bot_token_enc=crypto.encrypt("123456:TESTTOKENVALUE") or "",
        bot_telegram_id=999,
        bot_username="shop_bot",
        config_version=1,
        pay_card_enabled=True,
    )
    session.add(shop)
    await session.flush()
    session.add(StorefrontPlan(
        storefront_bot_id=shop.id, title="", gb=10, days=30, price_toman=100_000,
        enabled=True, sort_order=0,
    ))
    # Pin the owner's trial ceiling instead of inheriting the shipped default (1 GB): these tests
    # are about partial-patch and replay semantics, and would otherwise start failing the day the
    # default moves. The cap itself is covered in test_storefront_trial.py.
    session.add(Setting(key="storefront_trial_max_gb", value=100))
    await session.commit()
    settings_service.clear_settings_cache()
    return owner, shop


def test_settings_managers_preview_and_redaction(monkeypatch):  # noqa: ANN001
    async def body(session):  # noqa: ANN001
        owner, shop = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        monkeypatch.setattr(portal_storefront, "Bot", _FakeTelegramBot)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            settings = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
            assert settings.status_code == 200
            assert settings.headers["etag"] == '"sf-config-1"'
            assert "bot_token" not in settings.text

            payment = await client.patch(
                f"/api/portal/storefronts/{shop.id}/settings/payment",
                json={
                    "card_number": "۶۰۳۷ ۹۹۱۲-۳۴۵۶ ۷۸۹۰",
                    "card_holder": "Test Owner",
                },
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "pay-1"},
            )
            assert payment.status_code == 200
            assert payment.headers["etag"] == '"sf-config-2"'
            assert payment.json()["result"]["payment_updated"] is True
            settings = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
            assert settings.json()["payment"]["card_number"] == "6037991234567890"

            audit = (
                await session.execute(
                    select(StorefrontAuditEvent)
                    .where(StorefrontAuditEvent.action == "settings.payment")
                )
            ).scalar_one()
            assert "6037991234567890" not in repr(audit.before_json)
            assert "6037991234567890" not in repr(audit.after_json)

            trial = await client.patch(
                f"/api/portal/storefronts/{shop.id}/settings/trial",
                json={"free_trial_gb": 2},
                headers={"If-Match": '"sf-config-2"', "Idempotency-Key": "trial-1"},
            )
            assert trial.status_code == 200
            assert trial.json()["result"]["trial"] == {"enabled": True, "gb": 2, "days": 1}

            manager = await client.post(
                f"/api/portal/storefronts/{shop.id}/managers",
                json={"telegram_id": 333},
                headers={"If-Match": '"sf-config-3"', "Idempotency-Key": "manager-1"},
            )
            assert manager.status_code == 200
            assert manager.headers["etag"] == '"sf-config-4"'
            managers = await client.get(f"/api/portal/storefronts/{shop.id}/managers")
            assert [item["telegram_id"] for item in managers.json()["items"]] == ["111", "333"]

            customers_before = await session.scalar(select(func.count(StorefrontCustomer.id)))
            preview = await client.get(f"/api/portal/storefronts/{shop.id}/preview")
            customers_after = await session.scalar(select(func.count(StorefrontCustomer.id)))
            assert preview.status_code == 200
            assert customers_before == customers_after == 0
            assert preview.json()["enabled_plans"][0]["gb"] == 10

            extra_link = await client.post(
                f"/api/portal/storefronts/{shop.id}/channel",
                json={"channel_id": "@validchannel", "channel_link": "https://evil.invalid"},
                headers={"If-Match": '"sf-config-4"', "Idempotency-Key": "channel-extra"},
            )
            assert extra_link.status_code == 422

            channel = await client.post(
                f"/api/portal/storefronts/{shop.id}/channel",
                json={"channel_id": "@validchannel"},
                headers={"If-Match": '"sf-config-4"', "Idempotency-Key": "channel-1"},
            )
            assert channel.status_code == 200
            assert channel.headers["etag"] == '"sf-config-5"'
            settings = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
            assert settings.json()["channel"]["channel_link"] == "https://t.me/validchannel"
            assert _FakeTelegramBot.instances[-1].session.closed is True

            disabled = await client.put(
                f"/api/portal/storefronts/{shop.id}/channel/enabled",
                json={"enabled": False},
                headers={"If-Match": '"sf-config-5"', "Idempotency-Key": "channel-off"},
            )
            assert disabled.status_code == 200
            settings = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
            assert settings.json()["channel"]["channel_required"] is False

            enabled = await client.put(
                f"/api/portal/storefronts/{shop.id}/channel/enabled",
                json={"enabled": True},
                headers={"If-Match": '"sf-config-6"', "Idempotency-Key": "channel-on"},
            )
            assert enabled.status_code == 200
            assert enabled.headers["etag"] == '"sf-config-7"'
            assert _FakeTelegramBot.instances[-1].session.closed is True
            settings = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
            assert settings.json()["channel"]["channel_required"] is True
            assert settings.json()["channel"]["verified"] is True

    _run(body)


def test_settings_validation_and_stale_etag():
    async def body(session):  # noqa: ANN001
        owner, shop = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            bad_card = await client.patch(
                f"/api/portal/storefronts/{shop.id}/settings/payment",
                json={"card_number": "1234"},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "bad-card"},
            )
            assert bad_card.status_code == 422
            empty = await client.patch(
                f"/api/portal/storefronts/{shop.id}/settings/messages",
                json={},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "empty"},
            )
            assert empty.status_code == 422
            invalid_header = await client.patch(
                f"/api/portal/storefronts/{shop.id}/settings/trial",
                json={"free_trial_gb": 2},
                headers={"If-Match": "1", "Idempotency-Key": "header"},
            )
            assert invalid_header.status_code == 422

    _run(body)


def test_partial_trial_patch_replays_cleanly_after_concurrent_change(monkeypatch):  # noqa: ANN001
    """A partial settings PATCH must fold ONLY the sent field into the idempotency hash. After
    the exclude_unset fix, a network retry of `{free_trial_enabled}` replays cleanly even though a
    concurrent PATCH changed `free_trial_gb` in between — previously the handler re-read the shop
    and the changed gb turned the retry into a 409 conflict."""
    async def body(session):  # noqa: ANN001
        owner, shop = await _seed(session)
        shop.free_trial_enabled = True
        shop.free_trial_gb = 2
        shop.free_trial_days = 1
        await session.commit()

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        trial_url = f"/api/portal/storefronts/{shop.id}/settings/trial"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            first = await client.patch(
                trial_url, json={"free_trial_enabled": False},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "t"})
            assert first.status_code == 200
            assert first.headers["etag"] == '"sf-config-2"'
            assert first.json()["result"]["trial"]["enabled"] is False

            # A concurrent change to a DIFFERENT trial field (bumps the version to 3).
            gb = await client.patch(
                trial_url, json={"free_trial_gb": 9},
                headers={"If-Match": '"sf-config-2"', "Idempotency-Key": "gb"})
            assert gb.status_code == 200 and gb.headers["etag"] == '"sf-config-3"'

            # A retry of the ORIGINAL request (same key, same stale If-Match) replays cleanly,
            # NOT a 409 — because only `enabled` is in the request hash, not the changed gb.
            retry = await client.patch(
                trial_url, json={"free_trial_enabled": False},
                headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "t"})
            assert retry.status_code == 200
            assert retry.json()["result"]["trial"]["enabled"] is False

    _run(body)


def test_trial_reset_has_no_reseller_endpoint_but_the_state_is_still_reported(monkeypatch):  # noqa: ANN001
    """The re-arm TRIGGER left the portal; the state it reported did not.

    The monthly free-trial re-arm is a scheduled fleet job now, so no reseller-facing route may
    run it: a shop that could still fire it by hand would consume the month's single reset and
    make the sweep skip the very shop that just used it. The settings GET keeps serving
    `trial.reset` so the page can still say when it last happened."""
    async def body(session):  # noqa: ANN001
        owner, shop = await _seed(session)
        session.add(StorefrontCustomer(
            storefront_bot_id=shop.id, telegram_id=5001, name="C1", free_trial_used=True))
        session.add(StorefrontCustomer(
            storefront_bot_id=shop.id, telegram_id=5002, name="C2", free_trial_used=False))
        await session.commit()

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        monkeypatch.setattr(portal_storefront, "Bot", _FakeTelegramBot)
        url = f"/api/portal/storefronts/{shop.id}/settings/trial/reset"
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as client:
                state = await client.get(f"/api/portal/storefronts/{shop.id}/settings")
                assert state.json()["trial"]["reset"]["available"] is True
                assert state.json()["trial"]["reset"]["eligible_count"] == 1
                assert state.json()["trial"]["reset"]["max_gb"] == 100

                gone = await client.post(
                    url, headers={"If-Match": '"sf-config-1"', "Idempotency-Key": "reset-1"})
                assert gone.status_code == 404   # the route is gone, not merely refused

                # And nothing was re-armed by the attempt.
                used = await session.scalar(
                    select(func.count(StorefrontCustomer.id)).where(
                        StorefrontCustomer.free_trial_used.is_(True)))
                assert used == 1
        finally:
            app.dependency_overrides.clear()

    _run(body)
