"""Security remediation — Batch 2 (storefront money integrity).

F3  cross-tenant top-up decisions are refused (an admin may only decide their OWN shop's top-ups).
F14 a crypto txid is replay-protected per shop (casing-normalized, tenant-scoped unique).
F5  at most one refund per order (partial-unique index; + a real-Postgres concurrent barrier).
F11 a renewal whose panel write fails compensates the debit (customer never charged-without-service).
F4  a renewal in progress ("renewing") can't be re-charged by a second tap.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremb2.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
)
from tests.pg_barrier import make_engine, requires_pg, run_two  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s, Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _shop(s, tag, *, balance=0):  # noqa: ANN001
    """Create panel+reseller+bot+customer for a shop; return (bot, customer)."""
    n = sum(ord(c) for c in tag)  # stable distinct number from the tag
    p = Panel(key=f"p{tag}", host=f"p{tag}.invalid", proxy_path_enc=crypto.encrypt("x") or "",
              owner_uuid=f"o{tag}")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid=f"A{tag}", name=f"R{tag}")
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=p.id,
                        bot_token_enc=crypto.encrypt(f"{tag}:tok") or "",
                        bot_telegram_id=7_700_000 + n, enabled=True)
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=5_000_000 + n,
                              name="c", wallet_balance_toman=balance)
    s.add(cust)
    await s.flush()
    return bot, cust


# ───────────────────────────── F3: cross-tenant isolation ─────────────────────────────
def test_f3_cross_tenant_topup_decision_refused(tmp_path):
    async def body(s, _S):
        botA, _cA = await _shop(s, "a")
        botB, custB = await _shop(s, "b")
        # A pending top-up in shop B.
        txn = await storefront_wallet.create_topup(s, custB, 100_000, method="card")
        await s.commit()
        # Shop A's admin tries to confirm shop B's top-up → refused, no credit.
        changed, _ = await storefront_wallet.confirm_topup(
            s, txn.id, expected_storefront_bot_id=botA.id)
        await s.refresh(custB)
        assert changed is False and storefront_wallet.balance(custB) == 0
        # Reject cross-tenant → also refused.
        rj, _ = await storefront_wallet.reject_topup(s, txn.id, expected_storefront_bot_id=botA.id)
        assert rj is False
        # The rightful shop (B) can confirm.
        ok, _ = await storefront_wallet.confirm_topup(
            s, txn.id, expected_storefront_bot_id=botB.id)
        await s.refresh(custB)
        assert ok and storefront_wallet.balance(custB) == 100_000
    _run(body, tmp_path, "f3.db")


# ───────────────────────────── F14: txid replay protection ─────────────────────────────
def test_f14_crypto_txid_replay_rejected_per_shop_casing_insensitive(tmp_path):
    async def body(s, _S):
        _botA, custA = await _shop(s, "a")
        _botB, custB = await _shop(s, "b")
        custA_id, custB_id = custA.id, custB.id
        tx = "0x" + "AbCd" * 16  # 64 hex, mixed case
        await storefront_wallet.create_topup(s, custA, 50_000, method="usdt", txid=tx)
        # Same deposit re-submitted (different casing) in the SAME shop → rejected.
        with pytest.raises(storefront_wallet.DuplicateTopupTxid):
            await storefront_wallet.create_topup(s, custA, 50_000, method="usdt", txid=tx.lower())
        # (the dup rolled the session back → re-fetch the committed customers)
        custB = await s.get(StorefrontCustomer, custB_id)
        # A DIFFERENT shop may legitimately use the same string (tenant-scoped).
        other = await storefront_wallet.create_topup(s, custB, 50_000, method="usdt", txid=tx)
        assert other.txid == tx.lower()  # normalized
        # Card top-ups carry no replay-protected txid (free-text reference) → not constrained.
        custA = await s.get(StorefrontCustomer, custA_id)
        await storefront_wallet.create_topup(s, custA, 1000, method="card", txid="receipt#1")
        custA = await s.get(StorefrontCustomer, custA_id)
        await storefront_wallet.create_topup(s, custA, 1000, method="card", txid="receipt#1")
    _run(body, tmp_path, "f14.db")


# ───────────────────────────── F5: one refund per order ─────────────────────────────
def test_f5_double_refund_same_order_rejected(tmp_path):
    async def body(s, _S):
        _bot, cust = await _shop(s, "a", balance=0)
        cid = cust.id  # capture before the failed flush/rollback expires the ORM object
        await storefront_wallet.refund(s, cid, 30_000, order_id=1234)
        await s.commit()
        # A second refund for the same order violates the partial-unique index (fires at flush).
        with pytest.raises(IntegrityError):
            await storefront_wallet.refund(s, cid, 30_000, order_id=1234)
        await s.rollback()
        # A refund for a DIFFERENT order is fine.
        await storefront_wallet.refund(s, cid, 10_000, order_id=5678)
        await s.commit()
    _run(body, tmp_path, "f5.db")


@pytest.mark.pg_contract
@requires_pg
def test_f5_concurrent_refunds_yield_exactly_one():
    async def run():
        engine, factory = make_engine()
        try:
            async with factory() as s:
                bot, cust = await _shop(s, "z", balance=0)
                await s.commit()
                cid, oid, sfid = cust.id, 91234, bot.id

            async def _do_refund():
                async with factory() as s:
                    await storefront_wallet.refund(s, cid, 20_000, order_id=oid)
                    await s.commit()

            await run_two(_do_refund(), _do_refund())

            async with factory() as s:
                from sqlalchemy import func, select
                stmt = select(func.count()).select_from(StorefrontWalletTxn).where(
                    StorefrontWalletTxn.order_id == oid,
                    StorefrontWalletTxn.kind == "refund")
                n = (await s.execute(stmt)).scalar_one()
                assert n == 1, f"expected exactly one refund, got {n}"
                # cleanup
                await s.execute(StorefrontWalletTxn.__table__.delete().where(
                    StorefrontWalletTxn.order_id == oid))
                c = await s.get(StorefrontCustomer, cid)
                b = await s.get(StorefrontBot, sfid)
                r_id, p_id = b.reseller_id, b.panel_id
                await s.delete(c)
                await s.delete(b)
                await s.delete(await s.get(Reseller, r_id))
                await s.delete(await s.get(Panel, p_id))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


# ───────────────────────── F11: ambiguous renewal is durably reconciled ─────────────────────────
def test_f11_renew_panel_failure_compensates_charge(tmp_path, monkeypatch):
    async def body(s, S):
        _bot, cust = await _shop(s, "a", balance=100_000)
        order = StorefrontOrder(
            customer_id=cust.id, panel_id=1, panel_user_uuid="uuid-1", status="provisioned",
            is_trial=False, gb=5, days=30, price_toman=50_000, label="svc")
        s.add(order)
        await s.commit()
        oid, cid = order.id, cust.id

        from app.services.panel_client.admin_api import RenewUserTarget

        async def _prepare(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            return RenewUserTarget(5.0, 30, "2026-06-01")

        async def _boom(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("panel down")
        monkeypatch.setattr(storefront_subscription.AdminApiClient, "prepare_renew_user", _prepare)
        monkeypatch.setattr(storefront_subscription.AdminApiClient, "apply_renew_user_target", _boom)

        res = await storefront_subscription.renew(S, order_id=oid, by_admin=False)
        assert res.ok is False and res.reason == "processing"

        # The ambiguous write is NOT refunded inline. Once the lease expires, a definitive 404 lets
        # the reconciler compensate exactly once.
        async with S() as s2:
            o = await s2.get(StorefrontOrder, oid)
            o.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
            await s2.commit()

        async def _missing(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            return None
        monkeypatch.setattr(storefront_provision.AdminApiClient, "get_user", _missing)
        async with S() as s2:
            await storefront_provision.reap_pending_orders(
                s2, older_than=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))

        async with S() as s2:
            c = await s2.get(StorefrontCustomer, cid)
            o = await s2.get(StorefrontOrder, oid)
            # Charged then compensated → balance whole again; order restored (not stuck "renewing").
            assert storefront_wallet.balance(c) == 100_000
            assert o.status == "provisioned"
            from sqlalchemy import select
            kinds = sorted(k for (k,) in (await s2.execute(
                select(StorefrontWalletTxn.kind).where(StorefrontWalletTxn.order_id == oid))).all())
            assert kinds == ["purchase", "renew_reversal"]
    _run(body, tmp_path, "f11.db")


# ───────────────────────── F4: a renewal in progress can't be re-charged ─────────────────────────
def test_f4_renew_while_renewing_is_refused(tmp_path):
    async def body(s, S):
        _bot, cust = await _shop(s, "a", balance=100_000)
        order = StorefrontOrder(
            customer_id=cust.id, panel_id=1, panel_user_uuid="uuid-2", status="renewing",
            is_trial=False, gb=5, days=30, price_toman=50_000, label="svc")
        s.add(order)
        await s.commit()
        oid, cid = order.id, cust.id
        # Status "renewing" is not renewable → no charge.
        res = await storefront_subscription.renew(S, order_id=oid, by_admin=False)
        assert res.ok is False and res.reason == "not_found"
        async with S() as s2:
            c = await s2.get(StorefrontCustomer, cid)
            assert storefront_wallet.balance(c) == 100_000  # untouched
    _run(body, tmp_path, "f4.db")
