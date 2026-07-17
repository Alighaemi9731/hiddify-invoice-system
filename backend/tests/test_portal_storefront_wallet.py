"""Plan 005 money contracts: manual wallet adjustment (credit/debit, overdraw clamp, idempotent,
tenant-safe, audited). The money commands run on the request session, so a plain single-session
harness suffices."""
from __future__ import annotations

import asyncio
import datetime as dt

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontWalletTxn,
)

UTC = dt.timezone.utc


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
    owner = Reseller(panel_id=panel.id, admin_uuid="owner", name="Owner",
                     bot_chat_id=111, storefront_enabled=True)
    foreign = Reseller(panel_id=panel.id, admin_uuid="foreign", name="Foreign",
                       bot_chat_id=222, storefront_enabled=True)
    session.add_all([owner, foreign])
    await session.flush()
    shop = StorefrontBot(reseller_id=owner.id, panel_id=panel.id, bot_token_enc="t", config_version=1)
    other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2", config_version=1)
    session.add_all([shop, other])
    await session.flush()
    a = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=501, name="Alice",
                           wallet_balance_toman=0)
    c = StorefrontCustomer(storefront_bot_id=other.id, telegram_id=502, name="Carol")
    session.add_all([a, c])
    await session.commit()
    return owner, shop, a, c


def _client(session, owner):  # noqa: ANN001, ANN202
    async def session_override():
        yield session

    async def context_override():
        return ResellerContext(chat_id=111, resellers=[owner])

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_reseller] = context_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_wallet_adjust_credit_debit_overdraw_idempotent_and_tenant_safe():
    async def body(session):  # noqa: ANN001
        owner, shop, a, c = await _seed(session)
        async with _client(session, owner) as client:
            url = f"/api/portal/storefronts/{shop.id}/customers/{a.id}/wallet-adjustments"

            # Credit 50,000.
            r = await client.post(url, json={"amount_toman_signed": 50_000, "reason": "bonus credit"},
                                  headers={"Idempotency-Key": "adj-1"})
            assert r.status_code == 200
            res = r.json()["result"]
            assert res["applied_delta"] == 50_000 and res["new_balance"] == 50_000
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 50_000

            # Same key + payload → replay: ONE ledger effect (still 50,000, not 100,000).
            again = await client.post(url, json={"amount_toman_signed": 50_000, "reason": "bonus credit"},
                                      headers={"Idempotency-Key": "adj-1"})
            assert again.status_code == 200
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 50_000

            # Debit past zero → clamp: requested −80,000 but applied −50,000, balance floored at 0.
            d = await client.post(url, json={"amount_toman_signed": -80_000, "reason": "correction debit"},
                                  headers={"Idempotency-Key": "adj-2"})
            assert d.status_code == 200
            db = d.json()["result"]
            assert db["requested_delta"] == -80_000 and db["applied_delta"] == -50_000
            assert db["new_balance"] == 0
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 0

            # Zero amount + short reason → 422.
            assert (await client.post(url, json={"amount_toman_signed": 0, "reason": "nope nope"},
                                      headers={"Idempotency-Key": "adj-z"})).status_code == 422
            assert (await client.post(url, json={"amount_toman_signed": 100, "reason": "x"},
                                      headers={"Idempotency-Key": "adj-r"})).status_code == 422

            # Foreign customer → 404 (no leak).
            assert (await client.post(
                f"/api/portal/storefronts/{shop.id}/customers/{c.id}/wallet-adjustments",
                json={"amount_toman_signed": 1000, "reason": "cross tenant"},
                headers={"Idempotency-Key": "adj-f"})).status_code == 404

            # Audit trail + the ledger rows carry the non-null shop id.
            events = (await session.execute(select(StorefrontAuditEvent).where(
                StorefrontAuditEvent.action == "wallet.adjust"))).scalars().all()
            assert any(e.outcome == "succeeded" for e in events)
            null_shop = await session.scalar(select(func.count(StorefrontWalletTxn.id)).where(
                StorefrontWalletTxn.storefront_bot_id.is_(None)))
            assert null_shop == 0

    _run(body)
