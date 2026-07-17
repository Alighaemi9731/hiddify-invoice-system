"""Plan 006 credit-code portal contracts: CRUD, the before/after-redemption edit boundary, archive
(history-preserving, code_ci non-reusable), tenant isolation, idempotency, and a Postgres last-use
redemption race."""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCreditCode,
    StorefrontCreditRedemption,
    StorefrontCustomer,
)
from app.services import storefront_credit
from tests.pg_barrier import make_engine, requires_pg

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
    other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2",
                          config_version=1)
    session.add_all([shop, other])
    await session.flush()
    cust = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=501, name="Alice")
    session.add(cust)
    await session.commit()
    return owner, shop, other, cust


def _client(session, owner):  # noqa: ANN001, ANN202
    async def session_override():
        yield session

    async def context_override():
        return ResellerContext(chat_id=111, resellers=[owner])

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_reseller] = context_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_credit_crud_archive_and_edit_boundary():
    async def body(session):  # noqa: ANN001
        owner, shop, other, cust = await _seed(session)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/credits"

            # create (201) a percent code
            r = await client.post(base, json={"code": "welcome", "kind": "percent", "percent_off": 20,
                                              "max_bonus_toman": 50_000},
                                  headers={"Idempotency-Key": "k1"})
            assert r.status_code == 201, r.text
            code_id = r.json()["result"]["credit"]["id"]

            # a percent code needs percent_off
            bad = await client.post(base, json={"code": "nop", "kind": "percent"},
                                    headers={"Idempotency-Key": "kbad"})
            assert bad.status_code == 422

            # duplicate normalized code → 422 code_exists
            dup = await client.post(base, json={"code": "WELCOME", "kind": "fixed", "amount_toman": 1000},
                                    headers={"Idempotency-Key": "kdup"})
            assert dup.status_code == 422 and dup.json()["detail"]["code"] == "code_exists"

            # replay same key → same result, no second code
            again = await client.post(base, json={"code": "welcome", "kind": "percent",
                                                  "percent_off": 20, "max_bonus_toman": 50_000},
                                      headers={"Idempotency-Key": "k1"})
            assert again.status_code == 201 and again.json()["result"]["credit"]["id"] == code_id

            # same key + different payload → 409 conflict
            conflict = await client.post(base, json={"code": "welcome", "kind": "percent",
                                                     "percent_off": 30},
                                         headers={"Idempotency-Key": "k1"})
            assert conflict.status_code == 409

            # list (default hides archived) → one code
            lst = await client.get(base)
            assert lst.status_code == 200 and len(lst.json()["items"]) == 1

            # BEFORE redemption: economic field editable
            up = await client.patch(f"{base}/{code_id}", json={"percent_off": 25},
                                    headers={"Idempotency-Key": "e1"})
            assert up.status_code == 200 and up.json()["result"]["credit"]["percent_off"] == 25

            # absolute disable then archive
            en = await client.put(f"{base}/{code_id}/enabled", json={"enabled": False},
                                  headers={"Idempotency-Key": "en1"})
            assert en.status_code == 200 and en.json()["result"]["credit"]["enabled"] is False

            arch = await client.post(f"{base}/{code_id}/archive", headers={"Idempotency-Key": "ar1"})
            assert arch.status_code == 200 and arch.json()["result"]["credit"]["archived"] is True

            # archived hidden by default, visible with include_archived
            assert len((await client.get(base)).json()["items"]) == 0
            assert len((await client.get(f"{base}?include_archived=true")).json()["items"]) == 1

    _run(body)


def test_credit_after_redemption_edit_lock_and_usage():
    async def body(session):  # noqa: ANN001
        owner, shop, other, cust = await _seed(session)
        code = await storefront_credit.create_code(
            session, shop.id, code="paid", kind="fixed", amount_toman=10_000)
        # Simulate a redemption (money moved): bump used_count + a stored redemption row.
        code.used_count = 1
        session.add(StorefrontCreditRedemption(
            code_id=code.id, customer_id=cust.id, wallet_txn_id=None, bonus_toman=10_000))
        await session.commit()

        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/credits"
            # economic edit after a redemption → 422 locked_after_redemption
            locked = await client.patch(f"{base}/{code.id}", json={"amount_toman": 99_000},
                                        headers={"Idempotency-Key": "L1"})
            assert locked.status_code == 422 and locked.json()["detail"]["code"] == "locked_after_redemption"
            # enabled + expires_at STILL editable after a redemption
            ok = await client.patch(
                f"{base}/{code.id}",
                json={"enabled": False, "expires_at": "2027-01-01T00:00:00Z"},
                headers={"Idempotency-Key": "L2"})
            assert ok.status_code == 200

            # usage reads the STORED bonus_toman
            usage = await client.get(f"{base}/{code.id}/usage")
            assert usage.status_code == 200
            assert usage.json()["total_redemptions"] == 1 and usage.json()["total_bonus_toman"] == 10_000
            reds = await client.get(f"{base}/{code.id}/redemptions")
            assert reds.status_code == 200 and len(reds.json()["items"]) == 1

    _run(body)


def test_credit_tenant_isolation():
    async def body(session):  # noqa: ANN001
        owner, shop, other, cust = await _seed(session)
        foreign_code = await storefront_credit.create_code(
            session, other.id, code="theirs", kind="fixed", amount_toman=5_000)
        async with _client(session, owner) as client:
            # A foreign shop / foreign code → 404 (no enumeration).
            assert (await client.get(f"/api/portal/storefronts/{other.id}/credits")).status_code == 404
            assert (await client.get(
                f"/api/portal/storefronts/{shop.id}/credits/{foreign_code.id}/usage")).status_code == 404

    _run(body)


@pytest.mark.pg_contract
@requires_pg
def test_pg_credit_last_use_race():
    """On real Postgres 16: two concurrent redemptions of a max_uses=1 code — exactly one succeeds
    (the FOR UPDATE lock on the code row serializes the used_count check), the loser is exhausted."""
    async def run():
        import uuid
        suffix = uuid.uuid4().hex[:10]
        engine, factory = make_engine()
        try:
            async with factory() as s:
                panel = Panel(key=f"cc{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x",
                              owner_uuid=f"o-{suffix}")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid=f"a-{suffix}", name="race",
                             bot_chat_id=9_300_000_000 + int(suffix[:5], 16))
                s.add(r)
                await s.flush()
                shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="x")
                s.add(shop)
                await s.flush()
                c1 = StorefrontCustomer(storefront_bot_id=shop.id,
                                        telegram_id=7_300_000 + int(suffix[:4], 16), name="C1")
                c2 = StorefrontCustomer(storefront_bot_id=shop.id,
                                        telegram_id=7_400_000 + int(suffix[:4], 16), name="C2")
                s.add_all([c1, c2])
                await s.flush()
                code = StorefrontCreditCode(
                    storefront_bot_id=shop.id, code="LAST", code_ci="LAST", kind="fixed",
                    amount_toman=Decimal("1000"), is_gift=True, max_uses=1, per_customer_limit=1,
                    enabled=True)
                s.add(code)
                await s.commit()
                shop_id, cid1, cid2 = shop.id, c1.id, c2.id

            async def redeem(customer_id):  # noqa: ANN001
                async with factory() as s:
                    q = await storefront_credit.reserve(
                        s, shop_id, "LAST", topup_toman=0, customer_id=customer_id)
                    await s.commit()
                    return q.ok

            a, b = await asyncio.gather(redeem(cid1), redeem(cid2), return_exceptions=True)
            oks = [x for x in (a, b) if x is True]
            assert len(oks) == 1, (a, b)
            async with factory() as s:
                code_row = (await s.execute(
                    select(StorefrontCreditCode).where(
                        StorefrontCreditCode.storefront_bot_id == shop_id))).scalar_one()
                assert int(code_row.used_count) == 1
                assert int(await s.scalar(select(func.count(StorefrontCreditRedemption.id)))) <= 1
        finally:
            await engine.dispose()

    asyncio.run(run())
