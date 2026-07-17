"""Plan 006 communications contracts.

PART A (routes, single in-memory session): audience preview, broadcast/direct enqueue (202),
validation, idempotent replay, cancel, status.
PART B (durable worker, StaticPool + monkeypatched SessionLocal + FakeBot): claim → send → record
classification, terminal-never-reclaimed, expired-lease reclaim, two-shop credential isolation.
PART C (@pg_contract): two workers claim disjoint batches under FOR UPDATE SKIP LOCKED.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCustomer,
    StorefrontDeliveryRecipient,
)
from app.services import storefront, storefront_delivery
from tests.pg_barrier import make_engine, requires_pg

UTC = dt.timezone.utc


# ─────────────────────────────── PART A: routes ──────────────────────────────

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


async def _seed(session, *, n_active=2, n_banned=1):  # noqa: ANN001, ANN202
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
    custs = []
    for i in range(n_active):
        custs.append(StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=1000 + i, name=f"A{i}"))
    for i in range(n_banned):
        custs.append(StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=2000 + i,
                                        name=f"B{i}", banned=True))
    session.add_all(custs)
    await session.commit()
    return owner, shop, other, custs


def _client(session, owner):  # noqa: ANN001, ANN202
    async def session_override():
        yield session

    async def context_override():
        return ResellerContext(chat_id=111, resellers=[owner])

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_reseller] = context_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_audience_preview_and_broadcast_lifecycle():
    async def body(session):  # noqa: ANN001
        owner, shop, other, custs = await _seed(session, n_active=2, n_banned=1)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}"

            # audience preview excludes banned
            pv = await client.get(f"{base}/audience/preview", params={"segment": "all"})
            assert pv.status_code == 200 and pv.json()["count"] == 2 and len(pv.json()["sample"]) == 2
            # unknown segment → 422
            assert (await client.get(f"{base}/audience/preview",
                                     params={"segment": "nope"})).status_code == 422

            # create broadcast → 202, 2 recipients snapshotted (banned excluded)
            r = await client.post(f"{base}/broadcasts", json={"segment": "all", "text": "سلام"},
                                  headers={"Idempotency-Key": "b1"})
            assert r.status_code == 202, r.text
            job_id = r.json()["result"]["job_id"]
            assert r.json()["result"]["total"] == 2
            rows = (await session.execute(select(StorefrontDeliveryRecipient).where(
                StorefrontDeliveryRecipient.job_id == job_id))).scalars().all()
            assert len(rows) == 2 and all(row.status == "pending" for row in rows)

            # empty / oversized text → 422
            assert (await client.post(f"{base}/broadcasts", json={"segment": "all", "text": ""},
                                      headers={"Idempotency-Key": "b0"})).status_code == 422
            assert (await client.post(f"{base}/broadcasts",
                                      json={"segment": "all", "text": "x" * 4001},
                                      headers={"Idempotency-Key": "bx"})).status_code == 422

            # replay same key → same job, no second fan-out
            again = await client.post(f"{base}/broadcasts", json={"segment": "all", "text": "سلام"},
                                      headers={"Idempotency-Key": "b1"})
            assert again.status_code == 202 and again.json()["result"]["job_id"] == job_id
            assert int(await session.scalar(
                select(func.count(StorefrontBroadcastJob.id)))) == 1

            # status endpoint
            st = await client.get(f"{base}/broadcasts/{job_id}")
            assert st.status_code == 200 and st.json()["job"]["pending"] == 2

            # cancel → job canceled, unsent recipients canceled
            cx = await client.post(f"{base}/broadcasts/{job_id}/cancel",
                                   headers={"Idempotency-Key": "c1"})
            assert cx.status_code == 200 and cx.json()["result"]["status"] == "canceled"
            rows = (await session.execute(select(StorefrontDeliveryRecipient).where(
                StorefrontDeliveryRecipient.job_id == job_id))).scalars().all()
            assert all(row.status == "canceled" for row in rows)

    _run(body)


def test_direct_message_banned_and_rate_gate():
    async def body(session):  # noqa: ANN001
        owner, shop, other, custs = await _seed(session, n_active=1, n_banned=1)
        active, banned = custs[0], custs[1]
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}"
            # banned → 422
            bad = await client.post(f"{base}/customers/{banned.id}/message",
                                    json={"text": "hi"}, headers={"Idempotency-Key": "d-ban"})
            assert bad.status_code == 422
            # 10 allowed, the 11th → rate_limited 422
            for i in range(10):
                ok = await client.post(f"{base}/customers/{active.id}/message",
                                       json={"text": f"hi {i}"}, headers={"Idempotency-Key": f"d{i}"})
                assert ok.status_code == 202, ok.text
            over = await client.post(f"{base}/customers/{active.id}/message",
                                     json={"text": "one too many"}, headers={"Idempotency-Key": "d10"})
            assert over.status_code == 422 and over.json()["detail"]["code"] == "rate_limited"

    _run(body)


# ─────────────────────────── PART B: durable worker ──────────────────────────

def _m() -> SendMessage:
    return SendMessage(chat_id=1, text="x")


class _FakeBot:
    behavior: dict[int, str] = {}     # chat_id -> sent|blocked|fail|429
    built: list[str] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[int] = []
        self.closed = False
        _FakeBot.built.append(token)

    class _Session:
        def __init__(self, parent) -> None:  # noqa: ANN001
            self._p = parent

        async def close(self) -> None:
            self._p.closed = True

    @property
    def session(self):  # noqa: ANN202
        return _FakeBot._Session(self)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):  # noqa: ANN001, ANN201
        beh = _FakeBot.behavior.get(int(chat_id), "sent")
        if beh == "blocked":
            raise TelegramForbiddenError(method=_m(), message="blocked")
        if beh == "fail":
            raise RuntimeError("transient boom")
        if beh == "429":
            raise TelegramRetryAfter(method=_m(), message="flood", retry_after=0)
        self.sent.append(int(chat_id))


def _worker_run(monkeypatch, body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        _FakeBot.behavior = {}
        _FakeBot.built = []
        monkeypatch.setattr(storefront_delivery, "SessionLocal", factory)
        monkeypatch.setattr(storefront, "bot_token", lambda bot: f"tok-{bot.id}")
        monkeypatch.setattr(storefront_delivery, "_build_bot", lambda token: _FakeBot(token))
        try:
            await body(factory)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed_shop(factory, *, chat_ids):  # noqa: ANN001, ANN202
    async with factory() as s:
        panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="owner", name="Owner", bot_chat_id=111,
                     storefront_enabled=True)
        s.add(r)
        await s.flush()
        shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="t", config_version=1)
        s.add(shop)
        await s.flush()
        custs = [StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=cid, name=f"C{cid}")
                 for cid in chat_ids]
        s.add_all(custs)
        await s.commit()
        return shop.id, [c.id for c in custs]


async def _enqueue(factory, shop_id, cust_ids, text="hello"):  # noqa: ANN001, ANN202
    async with factory() as s:
        custs = [await s.get(StorefrontCustomer, cid) for cid in cust_ids]
        job = await storefront_delivery.snapshot_job(
            s, storefront_bot_id=shop_id, kind="broadcast", segment="all", message_text=text,
            actor_telegram_id=111, idempotency_key=None, customers=custs)
        await s.commit()
        return job.id


def test_worker_delivers_classifies_and_completes(monkeypatch):
    async def body(factory):  # noqa: ANN001
        shop_id, cust_ids = await _seed_shop(factory, chat_ids=[1, 2, 3])
        job_id = await _enqueue(factory, shop_id, cust_ids)
        _FakeBot.behavior = {2: "blocked"}   # 1,3 sent; 2 blocked
        summary = await storefront_delivery.run_once(worker_id="w1")
        assert summary["sent"] == 2 and summary["blocked"] == 1
        async with factory() as s:
            job = await s.get(StorefrontBroadcastJob, job_id)
            assert job.status == "completed"
            assert job.sent_count == 2 and job.blocked_count == 1 and job.pending_count == 0
            statuses = {r.chat_id: r.status for r in (await s.execute(
                select(StorefrontDeliveryRecipient))).scalars().all()}
            assert statuses == {1: "sent", 2: "blocked", 3: "sent"}
        # terminal rows are never re-claimed
        again = await storefront_delivery.run_once(worker_id="w1")
        assert again["claimed"] == 0

    _worker_run(monkeypatch, body)


def test_worker_transient_failure_schedules_retry_then_terminal(monkeypatch):
    async def body(factory):  # noqa: ANN001
        shop_id, cust_ids = await _seed_shop(factory, chat_ids=[1])
        await _enqueue(factory, shop_id, cust_ids)
        _FakeBot.behavior = {1: "fail"}
        s1 = await storefront_delivery.run_once(worker_id="w1")
        assert s1["retry"] == 1
        async with factory() as s:
            row = (await s.execute(select(StorefrontDeliveryRecipient))).scalar_one()
            assert row.status == "retry_wait" and row.attempt_count == 1
            assert row.next_attempt_at is not None   # backoff scheduled
            # Fast-forward to the max attempt to prove it becomes terminal 'failed'.
            row.attempt_count = 5
            row.next_attempt_at = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
            await s.commit()
        s2 = await storefront_delivery.run_once(worker_id="w1")
        assert s2["failed"] == 1
        async with factory() as s:
            row = (await s.execute(select(StorefrontDeliveryRecipient))).scalar_one()
            assert row.status == "failed"

    _worker_run(monkeypatch, body)


def test_worker_reclaims_expired_lease_then_delivers(monkeypatch):
    async def body(factory):  # noqa: ANN001
        shop_id, cust_ids = await _seed_shop(factory, chat_ids=[1])
        await _enqueue(factory, shop_id, cust_ids)
        # Simulate a crashed worker: row stuck 'sending' with an expired lease.
        async with factory() as s:
            row = (await s.execute(select(StorefrontDeliveryRecipient))).scalar_one()
            row.status = "sending"
            row.lease_owner = "dead"
            row.lease_expires_at = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
            row.attempt_count = 1
            await s.commit()
        _FakeBot.behavior = {1: "sent"}
        summary = await storefront_delivery.run_once(worker_id="w2")
        # reclaimed to 'unknown' then delivered (at-least-once)
        assert summary["reclaimed"] == 1 and summary["sent"] == 1
        async with factory() as s:
            row = (await s.execute(select(StorefrontDeliveryRecipient))).scalar_one()
            assert row.status == "sent"

    _worker_run(monkeypatch, body)


def test_worker_two_shop_credential_isolation(monkeypatch):
    async def body(factory):  # noqa: ANN001
        # Two shops, each with its own customer + token; each recipient must send via its OWN token.
        shop_a, custs_a = await _seed_shop(factory, chat_ids=[10])
        async with factory() as s:
            panel = Panel(key="p2", name="P2", host="p2.invalid", proxy_path_enc="x", owner_uuid="o2")
            s.add(panel)
            await s.flush()
            r = Reseller(panel_id=panel.id, admin_uuid="own2", name="Own2", bot_chat_id=222,
                         storefront_enabled=True)
            s.add(r)
            await s.flush()
            shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="t2",
                                 config_version=1)
            s.add(shop)
            await s.flush()
            c = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=20, name="B")
            s.add(c)
            await s.commit()
            shop_b, custs_b = shop.id, [c.id]
        await _enqueue(factory, shop_a, custs_a, text="A")
        await _enqueue(factory, shop_b, custs_b, text="B")
        _FakeBot.behavior = {}
        await storefront_delivery.run_once(worker_id="w1")
        # One bot built per shop, each with its own token.
        assert set(_FakeBot.built) == {f"tok-{shop_a}", f"tok-{shop_b}"}

    _worker_run(monkeypatch, body)


# ─────────────────────────────── PART C: PG races ────────────────────────────

@pytest.mark.pg_contract
@requires_pg
def test_pg_two_workers_claim_disjoint_batches():
    """On real Postgres 16: two workers claiming concurrently take DISJOINT recipient sets (FOR UPDATE
    SKIP LOCKED) — no recipient is claimed twice, and together they claim all of them."""
    async def run():
        import uuid
        suffix = uuid.uuid4().hex[:10]
        engine, factory = make_engine()
        try:
            async with factory() as s:
                panel = Panel(key=f"cm{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x",
                              owner_uuid=f"o-{suffix}")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid=f"a-{suffix}", name="race",
                             bot_chat_id=9_400_000_000 + int(suffix[:5], 16), storefront_enabled=True)
                s.add(r)
                await s.flush()
                shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="x")
                s.add(shop)
                await s.flush()
                custs = [StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=100 + i,
                                            name=f"C{i}") for i in range(6)]
                s.add_all(custs)
                await s.flush()
                job = StorefrontBroadcastJob(
                    storefront_bot_id=shop.id, actor_telegram_id=1, kind="broadcast", segment="all",
                    message_text="hi", status="running", total_count=6, pending_count=6)
                s.add(job)
                await s.flush()
                now = dt.datetime.now(UTC)
                for i, c in enumerate(custs):
                    s.add(StorefrontDeliveryRecipient(
                        job_id=job.id, customer_id=c.id, chat_id=100 + i,
                        status="pending", attempt_count=0, next_attempt_at=now))
                await s.commit()

            async def claim(worker):  # noqa: ANN001
                claims, _meta = await storefront_delivery._claim_batch(worker)
                return [c.recipient_id for c in claims]

            monkey = factory
            orig = storefront_delivery.SessionLocal
            storefront_delivery.SessionLocal = monkey
            try:
                a, b = await asyncio.gather(claim("wa"), claim("wb"))
            finally:
                storefront_delivery.SessionLocal = orig
            assert set(a).isdisjoint(set(b))
            assert len(set(a) | set(b)) == 6
        finally:
            await engine.dispose()

    asyncio.run(run())
