"""Plan 005 money contracts: top-up decisions (confirm/reject/corrected, idempotent, tenant-safe),
bounded bulk decisions, and secure proof streaming (realpath containment)."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCustomer,
    StorefrontWalletTxn,
)
from app.services import storefront_admin
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
    other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2", config_version=1)
    session.add_all([shop, other])
    await session.flush()
    a = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=501, name="Alice",
                           wallet_balance_toman=0)
    c = StorefrontCustomer(storefront_bot_id=other.id, telegram_id=502, name="Carol")
    session.add_all([a, c])
    await session.commit()
    return owner, shop, other, a, c


async def _topup(session, shop, cust, amount, *, method="card", status="pending",  # noqa: ANN001
                 proof_path=None, txid=None):
    txn = StorefrontWalletTxn(
        storefront_bot_id=shop.id, customer_id=cust.id, kind="topup",
        amount_toman=Decimal(str(amount)), requested_amount_toman=Decimal(str(amount)),
        status=status, method=method, proof_path=proof_path, txid=txid)
    session.add(txn)
    await session.commit()
    return txn


def _client(session, owner):  # noqa: ANN001, ANN202
    async def session_override():
        yield session

    async def context_override():
        return ResellerContext(chat_id=111, resellers=[owner])

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_reseller] = context_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _direct_jobs(session, shop_id):  # noqa: ANN001, ANN202
    """The durable kind='direct' notification jobs a top-up decision enqueued (plan 006 replaced the
    fire-and-forget Telegram send with an in-transaction durable enqueue delivered by the worker)."""
    return list((await session.execute(
        select(StorefrontBroadcastJob).where(
            StorefrontBroadcastJob.storefront_bot_id == shop_id,
            StorefrontBroadcastJob.kind == "direct"))).scalars().all())


def test_confirm_reject_corrected_idempotent_and_notify_durable(monkeypatch):  # noqa: ANN001
    async def body(session):  # noqa: ANN001
        owner, shop, other, a, c = await _seed(session)
        t1 = await _topup(session, shop, a, 100_000)
        t2 = await _topup(session, shop, a, 60_000)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/topups"

            # Confirm t1 → credits 100,000 once; a durable notification rides the same txn.
            r = await client.post(f"{base}/{t1.id}/decision", json={"decision": "confirm"},
                                  headers={"Idempotency-Key": "c1"})
            assert r.status_code == 200 and r.json()["result"]["credited"] == 100_000
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 100_000
            # A durable direct notification was enqueued in the SAME transaction as the money.
            assert len(await _direct_jobs(session, shop.id)) == 1

            # Replay same key → NO second credit AND no second notification (idempotent).
            again = await client.post(f"{base}/{t1.id}/decision", json={"decision": "confirm"},
                                      headers={"Idempotency-Key": "c1"})
            assert again.status_code == 200
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 100_000
            assert len(await _direct_jobs(session, shop.id)) == 1

            # A fresh decision on the already-confirmed t1 → already_decided, no credit.
            dup = await client.post(f"{base}/{t1.id}/decision", json={"decision": "confirm"},
                                    headers={"Idempotency-Key": "c1b"})
            assert dup.json()["result"]["already_decided"] is True
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 100_000

            # Confirm t2 with a CORRECTED amount → credits 40,000; requested preserved at 60,000.
            corr = await client.post(f"{base}/{t2.id}/decision",
                                     json={"decision": "confirm", "corrected_amount": 40_000,
                                           "reason": "crypto rate correction"},
                                     headers={"Idempotency-Key": "c2"})
            assert corr.status_code == 200 and corr.json()["result"]["credited"] == 40_000
            detail = (await client.get(f"{base}/{t2.id}")).json()
            assert detail["amount_toman"] == 40_000 and detail["requested_amount_toman"] == 60_000
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 140_000

    _run(body)


def test_reject_credits_nothing_and_validation(monkeypatch):  # noqa: ANN001
    async def body(session):  # noqa: ANN001
        owner, shop, other, a, c = await _seed(session)
        t = await _topup(session, shop, a, 50_000)
        tf = await _topup(session, other, c, 30_000)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/topups"
            # Reject requires a reason (3..255).
            assert (await client.post(f"{base}/{t.id}/decision", json={"decision": "reject"},
                                      headers={"Idempotency-Key": "r0"})).status_code == 422
            # corrected_amount on a reject → 422.
            assert (await client.post(f"{base}/{t.id}/decision",
                                      json={"decision": "reject", "corrected_amount": 5, "reason": "no no"},
                                      headers={"Idempotency-Key": "r0b"})).status_code == 422
            # Valid reject → credits nothing.
            rj = await client.post(f"{base}/{t.id}/decision",
                                   json={"decision": "reject", "reason": "invalid receipt"},
                                   headers={"Idempotency-Key": "r1"})
            assert rj.status_code == 200 and rj.json()["result"]["changed"] is True
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 0
            # Foreign top-up (other shop) → 404.
            assert (await client.post(f"{base}/{tf.id}/decision",
                                      json={"decision": "confirm"},
                                      headers={"Idempotency-Key": "rf"})).status_code == 404

    _run(body)


def test_bulk_decisions_partial_and_validation(monkeypatch):  # noqa: ANN001
    async def body(session):  # noqa: ANN001
        owner, shop, other, a, c = await _seed(session)
        p1 = await _topup(session, shop, a, 10_000)
        p2 = await _topup(session, shop, a, 20_000)
        done = await _topup(session, shop, a, 5_000, status="confirmed")
        tf = await _topup(session, other, c, 1_000)
        async with _client(session, owner) as client:
            url = f"/api/portal/storefronts/{shop.id}/topups/bulk-decisions"
            body_json = {"items": [
                {"txn_id": p1.id, "decision": "confirm"},
                {"txn_id": p2.id, "decision": "reject"},
                {"txn_id": done.id, "decision": "confirm"},   # already decided
                {"txn_id": tf.id, "decision": "confirm"},     # foreign → not_found
            ], "reason": "batch review of receipts"}
            r = await client.post(url, json=body_json, headers={"Idempotency-Key": "bulk-1"})
            assert r.status_code == 200
            counts = r.json()["result"]["counts"]
            assert counts["changed"] == 2 and counts["already_decided"] == 1 and counts["not_found"] == 1
            # p1 credited, p2 rejected (no credit); balance = 10,000 only.
            assert int((await session.get(StorefrontCustomer, a.id)).wallet_balance_toman) == 10_000

            # Same parent key, DIFFERENT item set → conflict 409.
            conflict = await client.post(url, json={"items": [{"txn_id": p1.id, "decision": "reject"}],
                                                    "reason": "changed my mind here"},
                                         headers={"Idempotency-Key": "bulk-1"})
            assert conflict.status_code == 409
            # empty / oversized / duplicate → 422.
            assert (await client.post(url, json={"items": []},
                                      headers={"Idempotency-Key": "b0"})).status_code == 422
            assert (await client.post(url, json={"items": [
                {"txn_id": p1.id, "decision": "confirm"},
                {"txn_id": p1.id, "decision": "reject"}]},
                headers={"Idempotency-Key": "bdup"})).status_code == 422

    _run(body)


def test_proof_streaming_and_realpath_containment():
    async def body(session):  # noqa: ANN001
        owner, shop, other, a, c = await _seed(session)
        os.makedirs("data/storefront_proofs", exist_ok=True)
        good = "data/storefront_proofs/test_proof_plan005.jpg"
        with open(good, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0JFIFdata")
        with_proof = await _topup(session, shop, a, 10_000, method="card", proof_path=good)
        no_proof = await _topup(session, shop, a, 20_000, method="usdt", txid="0xabc")
        escaped = await _topup(session, shop, a, 30_000, method="card",
                               proof_path="data/storefront_proofs/../../../etc/passwd")
        missing = await _topup(session, shop, a, 40_000, method="card",
                               proof_path="data/storefront_proofs/gone.jpg")
        tf = await _topup(session, other, c, 1_000, method="card", proof_path=good)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/topups"
            ok = await client.get(f"{base}/{with_proof.id}/proof")
            assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
            assert ok.content.startswith(b"\xff\xd8\xff")
            for txn in (no_proof, escaped, missing, tf):
                assert (await client.get(f"{base}/{txn.id}/proof")).status_code == 404
            # never leaks the path
            assert "storefront_proofs" not in ok.text
        os.remove(good)

    _run(body)


@pytest.mark.pg_contract
@requires_pg
def test_pg_topup_confirm_and_adjust_money_races():
    """On real Postgres 16: two concurrent confirms of one top-up with the SAME idempotency key
    credit the wallet exactly ONCE (one command row); two concurrent adjustments with DIFFERENT
    keys both apply with no lost update (row lock serializes them)."""
    async def run():
        import uuid
        suffix = uuid.uuid4().hex[:10]
        engine, factory = make_engine()
        try:
            async with factory() as s:
                panel = Panel(key=f"tp{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x",
                              owner_uuid=f"o-{suffix}")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid=f"a-{suffix}", name="race",
                             bot_chat_id=9_200_000_000 + int(suffix[:5], 16))
                s.add(r)
                await s.flush()
                shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="x",
                                     pay_card_enabled=False)
                s.add(shop)
                await s.flush()
                cust = StorefrontCustomer(storefront_bot_id=shop.id,
                                          telegram_id=7_100_000 + int(suffix[:4], 16), name="C",
                                          wallet_balance_toman=0)
                s.add(cust)
                await s.flush()
                topup = StorefrontWalletTxn(
                    storefront_bot_id=shop.id, customer_id=cust.id, kind="topup",
                    amount_toman=Decimal("50000"), requested_amount_toman=Decimal("50000"),
                    status="pending", method="card")
                s.add(topup)
                await s.commit()
                ids = panel.id, r.id, shop.id, cust.id, topup.id, r.bot_chat_id

            def ctx(key):  # noqa: ANN001, ANN202 — source='bot' owner → no best-effort notify
                return storefront_admin.CommandContext(
                    actor_telegram_id=ids[5], actor_role="owner", source="bot",
                    idempotency_key=key, expected_version=1)

            async def confirm():
                async with factory() as s:
                    return await storefront_admin.set_topup_decision(
                        s, ids[2], ids[4], ctx("confirm-race"), decision="confirm")

            a, b = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
            assert sum(isinstance(x, storefront_admin.CommandResult) for x in (a, b)) >= 1, (a, b)
            async with factory() as s:
                assert await s.scalar(select(func.count(StorefrontApiCommand.id)).where(
                    StorefrontApiCommand.storefront_bot_id == ids[2],
                    StorefrontApiCommand.idempotency_key == "confirm-race")) == 1
                assert int((await s.get(StorefrontCustomer, ids[3])).wallet_balance_toman) == 50_000

            async def adjust(key, amt):  # noqa: ANN001, ANN202
                async with factory() as s:
                    return await storefront_admin.adjust_wallet(
                        s, ids[2], ids[3], ctx(key), amount_toman_signed=amt, reason="concurrent adjust")

            await asyncio.gather(adjust("adj-x", 10_000), adjust("adj-y", 20_000),
                                 return_exceptions=True)
            async with factory() as s:
                assert int((await s.get(StorefrontCustomer, ids[3])).wallet_balance_toman) == 80_000

            async with factory() as s:
                await s.execute(delete(StorefrontAuditEvent).where(
                    StorefrontAuditEvent.storefront_bot_id == ids[2]))
                await s.execute(delete(StorefrontApiCommand).where(
                    StorefrontApiCommand.storefront_bot_id == ids[2]))
                await s.execute(delete(StorefrontWalletTxn).where(
                    StorefrontWalletTxn.storefront_bot_id == ids[2]))
                await s.execute(delete(StorefrontCustomer).where(StorefrontCustomer.id == ids[3]))
                await s.execute(delete(StorefrontBot).where(StorefrontBot.id == ids[2]))
                await s.execute(delete(Reseller).where(Reseller.id == ids[1]))
                await s.execute(delete(Panel).where(Panel.id == ids[0]))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
