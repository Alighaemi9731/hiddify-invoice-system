"""Storefront Phase-1: wallet ledger correctness + owner monthly-fee (active-only)."""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/storefront.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot import keyboards  # noqa: E402
from app.bot.storefront import keyboards as sfkb  # noqa: E402
from app.core import crypto  # noqa: E402
from app.models import Panel, Reseller, StorefrontBot, StorefrontOrder  # noqa: E402
from app.models.storefront import (  # noqa: E402
    StorefrontCustomer,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    maintenance,
    storefront,
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
    usercreate,
)


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(s, *, tag="1", storefront_enabled=True, with_bot=True, fee=None):
    p = Panel(key=f"p{tag}", host=f"p{tag}.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(
        panel_id=p.id, admin_uuid=f"A{tag}", name="Ali",
        storefront_enabled=storefront_enabled, storefront_monthly_fee_toman=fee,
    )
    s.add(r)
    await s.flush()
    bot = None
    if with_bot:
        bot = StorefrontBot(
            reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("123:abc") or "",
            bot_telegram_id=int(f"99{tag}"), enabled=True,
        )
        s.add(bot)
        await s.flush()
    cust = None
    if bot is not None:
        cust = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=555, first_name="Cust", username="c")
        )
    await s.commit()
    return r, bot, cust


def test_topup_confirm_credits_once_and_purchase_is_atomic(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        txn = await storefront_wallet.create_topup(s, cust, 300_000, method="card")
        assert txn.status == "pending"
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(0)          # not credited while pending

        changed, _ = await storefront_wallet.confirm_topup(s, txn.id)
        await s.refresh(cust)
        assert changed and storefront_wallet.balance(cust) == Decimal(300_000)

        # double-confirm must NOT double-credit
        changed2, _ = await storefront_wallet.confirm_topup(s, txn.id)
        await s.refresh(cust)
        assert changed2 is False and storefront_wallet.balance(cust) == Decimal(300_000)

        # purchase debits atomically; a too-expensive buy is refused (balance untouched)
        ok, _ = await storefront_wallet.charge_purchase(s, cust.id, 200_000)
        await s.refresh(cust)
        assert ok and storefront_wallet.balance(cust) == Decimal(100_000)
        broke, _ = await storefront_wallet.charge_purchase(s, cust.id, 200_000)
        await s.refresh(cust)
        assert broke is False and storefront_wallet.balance(cust) == Decimal(100_000)

    _run(body, tmp_path, "wallet.db")


def test_reject_topup_and_manual_adjust_floor(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        txn = await storefront_wallet.create_topup(s, cust, 50_000, method="card")
        changed, _ = await storefront_wallet.reject_topup(s, txn.id)
        await s.refresh(cust)
        assert changed and storefront_wallet.balance(cust) == Decimal(0)  # rejected → no credit

        await storefront_wallet.manual_adjust(s, cust, 80_000, note="gift")
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(80_000)
        # a debit larger than the balance floors at 0 (never negative)
        await storefront_wallet.manual_adjust(s, cust, -200_000, note="correction")
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(0)

    _run(body, tmp_path, "wallet2.db")


def test_storefront_telegram_id_columns_are_bigint():
    # Telegram bot/user ids exceed int32; the columns MUST be BigInteger or Postgres rejects them
    # ("value out of int32 range"). SQLite has no int32 cap, so only a metadata check catches this.
    assert isinstance(StorefrontBot.__table__.c.bot_telegram_id.type, BigInteger)
    assert isinstance(StorefrontCustomer.__table__.c.telegram_id.type, BigInteger)


def test_customer_with_id_above_int32_roundtrips(tmp_path):
    async def body(s):
        _r, bot, _c = await _seed(s)
        big = 8_640_657_004  # a real-world Telegram id, > int32 max (2_147_483_647)
        cust = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=big, first_name="Big", username="b")
        )
        await s.commit()
        assert cust.telegram_id == big
        again = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=big, first_name="Big", username="b")
        )
        assert again.id == cust.id  # same customer, no duplicate

    _run(body, tmp_path, "bigid.db")


def test_reseller_menu_keyboard_storefront_button():
    on = keyboards.reseller_menu_keyboard(show_storefront=True)
    cbs = [b.callback_data for row in on.inline_keyboard for b in row]
    assert "menu:storefront" in cbs
    off = keyboards.reseller_menu_keyboard(show_storefront=False)
    cbs_off = [b.callback_data for row in off.inline_keyboard for b in row]
    assert "menu:storefront" not in cbs_off


def test_plan_label_is_gb_days_price_never_title():
    p = StorefrontPlan(title="اقتصادی", gb=30, days=30, price_toman=120000)
    label = sfkb.plan_label(p)
    assert "اقتصادی" not in label          # owner: «عنوان نمی‌خواهیم»
    assert "30 گیگ" in label and "30 روزه" in label and "120,000" in label


def test_storefront_order_has_label_column(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        o = StorefrontOrder(customer_id=cust.id, label="گوشی", gb=30, days=30,
                            price_toman=1000, status="provisioned")
        s.add(o)
        await s.commit()
        await s.refresh(o)
        assert o.label == "گوشی"

    _run(body, tmp_path, "orderlabel.db")


def test_provision_passes_customer_label_as_config_name(tmp_path, monkeypatch):
    captured = {}

    async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        captured["base_name"] = base_name
        captured["count"] = count
        uid = user_uuid or "u-1"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid,
                                     sub_link=f"https://h/p/{uid}/#{base_name}")],
            error=None, capacity_blocked=False, limit_hit=False)

    monkeypatch.setattr(usercreate, "create_for_reseller", fake_create)

    async def body(s):
        _r, bot, cust = await _seed(s)
        res = await storefront_provision.provision(s, bot, cust, gb=30, days=30, label="گوشی")
        assert res.ok and res.uuid == "u-1"
        assert captured["base_name"] == "گوشی"   # exactly the customer's text (Q1)
        assert captured["count"] == 1

    _run(body, tmp_path, "provlabel.db")


def test_live_status_maps_panel_fields(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 3.5, "usage_limit_GB": 30, "remaining_day": 12}

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)

    async def body(s):
        _r, bot, cust = await _seed(s)
        order = StorefrontOrder(customer_id=cust.id, label="x", gb=30, days=30,
                                price_toman=1000, status="provisioned", panel_user_uuid="u-1")
        s.add(order)
        await s.commit()
        st = await storefront_provision.live_status(s, bot, order)
        assert st.ok and st.used_gb == 3.5 and st.limit_gb == 30.0 and st.remaining_days == 12

        order.panel_user_uuid = None            # no uuid → best-effort failure, not a crash
        st2 = await storefront_provision.live_status(s, bot, order)
        assert st2.ok is False

    _run(body, tmp_path, "livestatus.db")


def test_wallet_kb_offers_topup_then_amount():
    cbs = [b.callback_data for row in sfkb.wallet_kb().inline_keyboard for b in row]
    assert "sftopup" in cbs                     # wallet screen offers top-up (not an immediate prompt)


def test_orders_kb_label_is_rtl_isolated():
    o = StorefrontOrder(id=1, customer_id=1, label="phone", gb=30, days=30,
                        price_toman=0, status="provisioned")
    text = sfkb.orders_kb([o]).inline_keyboard[0][0].text
    assert "⁨phone⁩" in text                     # English name isolated → order doesn't scramble


def test_customer_kb_shows_free_trial_only_when_flagged():
    on = [b.text for row in sfkb.customer_reply_kb(show_free_trial=True).keyboard for b in row]
    off = [b.text for row in sfkb.customer_reply_kb(show_free_trial=False).keyboard for b in row]
    assert sfkb.FREE_TRIAL_LABEL in on and sfkb.FREE_TRIAL_LABEL not in off


def test_trial_available_logic():
    from app.bot.storefront.handlers import _trial_available
    assert _trial_available(SimpleNamespace(free_trial_enabled=True),
                            SimpleNamespace(free_trial_used=False)) is True
    assert _trial_available(SimpleNamespace(free_trial_enabled=True),
                            SimpleNamespace(free_trial_used=True)) is False
    assert _trial_available(SimpleNamespace(free_trial_enabled=False),
                            SimpleNamespace(free_trial_used=False)) is False


def _fake_create_factory(calls):  # noqa: ANN001, ANN202
    async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        calls["n"] += 1
        uid = user_uuid or f"u{calls['n']}"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid, sub_link=f"https://h/p/{uid}/#x")],
            error=None, capacity_blocked=False, limit_hit=False)
    return fake_create


def _engine_session(tmp_path, name):  # noqa: ANN001, ANN202
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_free_trial_is_one_per_customer(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "trial.db")
        from app.core.db import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            bot.free_trial_enabled, bot.free_trial_gb, bot.free_trial_days = True, 1, 1
            await s.commit()
            sf_id, cid = bot.id, cust.id

        r1 = await storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid)
        assert r1.ok and calls["n"] == 1
        async with Session() as s:
            assert (await s.get(StorefrontCustomer, cid)).free_trial_used is True
        # second claim refused — no second config minted
        r2 = await storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid)
        assert r2.ok is False and r2.reason == "used" and calls["n"] == 1
        await engine.dispose()

    asyncio.run(go())


def test_free_trial_concurrent_double_tap_mints_one(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "trial2.db")
        from app.core.db import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            bot.free_trial_enabled = True
            await s.commit()
            sf_id, cid = bot.id, cust.id
        a, b = await asyncio.gather(
            storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid),
            storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid),
        )
        assert sum(1 for r in (a, b) if r.ok) == 1   # exactly one trial minted
        assert calls["n"] == 1
        await engine.dispose()

    asyncio.run(go())


def test_manager_polls_each_bot_independently_no_fleet_restart(tmp_path, monkeypatch):
    # Each bot gets its OWN poll loop; adding a bot must NOT restart the others (a fleet-wide restart
    # caused overlapping getUpdates → every update delivered twice). This guards that: the original
    # pollers are the SAME task objects after a new bot is added.
    import asyncio as _aio

    from aiogram import Bot

    from app.bot.storefront import manager

    async def fake_get_me(self):  # noqa: ANN001
        return SimpleNamespace(id=int(self.token.split(":")[0]), username="b" + self.token[:3])

    async def fake_get_updates(self, **kw):  # noqa: ANN001, ANN003 — idle long-poll, no network
        await _aio.sleep(0.2)
        return []

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "get_updates", fake_get_updates)

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mgr.db'}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def add_bot(i, tok):  # noqa: ANN001, ANN202
            async with Session() as s:
                p = Panel(key=f"mp{i}", host=f"mp{i}.invalid",
                          proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
                s.add(p)
                await s.flush()
                r = Reseller(panel_id=p.id, admin_uuid=f"M{i}", name=f"R{i}", storefront_enabled=True)
                s.add(r)
                await s.flush()
                s.add(StorefrontBot(reseller_id=r.id, panel_id=p.id,
                                    bot_token_enc=crypto.encrypt(tok) or "", enabled=True))
                await s.commit()

        await add_bot(1, "111:aaa")
        await add_bot(2, "222:bbb")
        monkeypatch.setattr(manager, "SessionLocal", Session)
        manager._active, manager._dp = {}, None

        await manager.reconcile()
        assert len(manager._active) == 2                       # one independent poller per bot
        tasks = {rid: r.task for rid, r in manager._active.items()}
        assert all(not t.done() for t in tasks.values())

        await manager.reconcile()                              # unchanged → SAME tasks, not restarted
        assert {rid: r.task for rid, r in manager._active.items()} == tasks

        await add_bot(3, "333:ccc")
        await manager.reconcile()
        assert len(manager._active) == 3
        for rid, t in tasks.items():                           # the original two are UNTOUCHED
            assert manager._active[rid].task is t

        for rid in list(manager._active):
            await manager._stop_runner(rid)
        manager._dp = None
        await engine.dispose()

    _aio.run(go())


def test_monthly_fee_active_only(tmp_path):
    async def body(s):
        # enabled + active bot + per-reseller fee → that fee
        r, _bot, _c = await _seed(s, tag="1", storefront_enabled=True, with_bot=True, fee=300_000)
        assert await storefront.monthly_fee_for(s, r) == 300_000

        # enabled flag but NO bot set up yet → no fee (active-only)
        r2, _b2, _c2 = await _seed(s, tag="2", storefront_enabled=True, with_bot=False)
        assert await storefront.monthly_fee_for(s, r2) == 0

        # feature disabled → no fee even if a bot row somehow exists
        r3, _b3, _c3 = await _seed(s, tag="3", storefront_enabled=False, with_bot=True, fee=300_000)
        assert await storefront.monthly_fee_for(s, r3) == 0

    _run(body, tmp_path, "fee.db")


# ── v1.44.0: atomic purchase, reaper, subscription lifecycle, retention ──────────

async def _seed_engine(tmp_path, name):  # noqa: ANN001, ANN202
    engine, Session = _engine_session(tmp_path, name)
    from app.core.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, Session


def test_purchase_is_atomic_and_refunds_on_provision_failure(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = await _seed_engine(tmp_path, "buy.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            sf_id, cid, pid = bot.id, cust.id, plan.id

        res = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="گوشی")
        assert res.ok and res.sub_link
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 30_000      # 80k − 50k debited once
            orders = (await s.execute(
                StorefrontOrder.__table__.select().where(StorefrontOrder.customer_id == cid)
            )).all()
            assert len(orders) == 1
            order = await s.get(StorefrontOrder, orders[0].id)
            assert order.status == "provisioned" and order.label == "گوشی"
            # the order's pre-generated uuid IS the panel user's uuid (embedded in the sub-link)
            assert order.panel_user_uuid and order.panel_user_uuid in res.sub_link
            txn = (await s.execute(
                StorefrontWalletTxn.__table__.select().where(
                    StorefrontWalletTxn.kind == "purchase")
            )).first()
            assert txn is not None and txn.order_id == order.id   # money linked to its order
        await engine.dispose()

    asyncio.run(go())


def test_purchase_refunds_when_provision_fails(tmp_path, monkeypatch):
    async def fail_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        return SimpleNamespace(created=[], error=None, capacity_blocked=True, limit_hit=False)
    monkeypatch.setattr(usercreate, "create_for_reseller", fail_create)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "buyfail.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            sf_id, cid, pid = bot.id, cust.id, plan.id
        res = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")
        assert res.ok is False and res.reason == "capacity"
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 80_000   # fully refunded
            order = (await s.execute(
                StorefrontOrder.__table__.select().where(StorefrontOrder.customer_id == cid)
            )).first()
            assert order.status == "failed"
        await engine.dispose()

    asyncio.run(go())


def test_reaper_completes_existing_and_refunds_missing(tmp_path, monkeypatch):
    import datetime as _dt

    from app.services.panel_client import admin_api

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 0} if uuid == "exists" else None

    async def noop_notify(sf, customer, order):  # noqa: ANN001
        return None

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)
    monkeypatch.setattr(storefront_provision, "_notify_completed", noop_notify)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "reaper.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            a = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="A", gb=10,
                                days=30, price_toman=50_000, status="pending", panel_user_uuid="exists")
            b = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="B", gb=10,
                                days=30, price_toman=50_000, status="pending", panel_user_uuid="gone")
            s.add_all([a, b])
            await s.flush()
            s.add(StorefrontWalletTxn(customer_id=cust.id, kind="purchase", amount_toman=-50_000,
                                      status="done", order_id=b.id))
            await s.commit()
            aid, bid, cid = a.id, b.id, cust.id

        async with Session() as s:
            res = await storefront_provision.reap_pending_orders(
                s, older_than=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1))
        assert res["completed"] == 1 and res["refunded"] == 1
        async with Session() as s:
            assert (await s.get(StorefrontOrder, aid)).status == "provisioned"
            assert (await s.get(StorefrontOrder, bid)).status == "failed"
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 50_000   # B refunded once
        await engine.dispose()

    asyncio.run(go())


def test_renew_charges_current_price_in_place(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    seen = {}

    async def fake_renew_user(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001, ANN002
        seen.update(uuid=uuid, gb=gb, days=days)

    monkeypatch.setattr(admin_api.AdminApiClient, "renew_user", fake_renew_user)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "renew.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                    label="x", gb=10, days=30, price_toman=50_000,
                                    status="provisioned", panel_user_uuid="u1",
                                    sub_link="https://h/p/u1/#x")
            s.add(order)
            await s.flush()
            plan.price_toman = 60_000   # price went UP after purchase
            await s.commit()
            oid, cid = order.id, cust.id

        res = await storefront_subscription.renew(Session, order_id=oid, by_admin=False)
        assert res.ok and res.price == 60_000 and seen["uuid"] == "u1"
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 20_000   # charged the NEW 60k
            order = await s.get(StorefrontOrder, oid)
            assert order.sub_link == "https://h/p/u1/#x" and order.last_renewed_at is not None
        await engine.dispose()

    asyncio.run(go())


def test_delete_subscription_removes_panel_user(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    seen = {}

    async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        seen["uuid"] = uuid

    monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "del.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            order = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="x", gb=10,
                                    days=30, price_toman=0, status="provisioned", panel_user_uuid="u1")
            s.add(order)
            await s.commit()
            oid = order.id
        res = await storefront_subscription.delete_subscription(Session, order_id=oid)
        assert res.ok and seen["uuid"] == "u1"
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).status == "deleted"
        await engine.dispose()

    asyncio.run(go())


def test_renew_user_grants_fresh_quota_on_top_of_usage(tmp_path, monkeypatch):
    from app.services.panel_client.admin_api import AdminApiClient

    body = {}

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 7.5}

    async def fake_patch_user(self, panel, uuid, b, *, api_key=None):  # noqa: ANN001, ANN002
        body.update(b)

    monkeypatch.setattr(AdminApiClient, "get_user", fake_get_user)
    monkeypatch.setattr(AdminApiClient, "patch_user", fake_patch_user)

    async def go():
        await AdminApiClient().renew_user(SimpleNamespace(), "u1", gb=10, days=30, api_key="k")
        assert body["usage_limit_GB"] == 17.5   # used 7.5 + fresh 10 → remaining is exactly 10
        assert body["package_days"] == 30 and body["start_date"] is None and body["enable"] is True

    asyncio.run(go())


def test_retention_purges_tire_kickers_keeps_ledger(tmp_path):
    import datetime as _dt

    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=200)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "ret.db")
        async with Session() as s:
            _r, bot, _c = await _seed(s)
            # A: pure tire-kicker (no orders, zero balance) → purged
            a = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1001, last_seen_at=old)
            # B: has a provisioned order → kept
            b = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1002, last_seen_at=old)
            # C: has a confirmed top-up → kept
            c = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1003, last_seen_at=old)
            s.add_all([a, b, c])
            await s.flush()
            s.add(StorefrontOrder(customer_id=b.id, panel_id=bot.panel_id, label="x", gb=10, days=30,
                                  price_toman=0, status="provisioned", panel_user_uuid="u1"))
            s.add(StorefrontWalletTxn(customer_id=c.id, kind="topup", amount_toman=100_000,
                                      status="confirmed"))
            # junk for B that should be swept (failed order, old)
            junk = StorefrontOrder(customer_id=b.id, panel_id=bot.panel_id, label="j", gb=1, days=1,
                                   price_toman=0, status="failed", panel_user_uuid="z")
            s.add(junk)
            await s.flush()
            # backdate the failed order so it's older than the window
            junk.created_at = old
            await s.commit()
            aid, bid, cid = a.id, b.id, c.id

        async with Session() as s:
            counts = await maintenance.prune_stale_storefront(s)
        assert counts["customers"] == 1
        async with Session() as s:
            assert await s.get(StorefrontCustomer, aid) is None       # tire-kicker purged
            assert await s.get(StorefrontCustomer, bid) is not None    # kept (provisioned order)
            assert await s.get(StorefrontCustomer, cid) is not None    # kept (confirmed top-up)
            kept_topup = (await s.execute(
                StorefrontWalletTxn.__table__.select().where(StorefrontWalletTxn.customer_id == cid)
            )).all()
            assert len(kept_topup) == 1   # the ledger top-up survives
        await engine.dispose()

    asyncio.run(go())


def test_banned_customer_is_blocked_everywhere(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "ban.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            cust.banned = True
            await s.commit()
            tgid, banned_tg = bot.bot_telegram_id, cust.telegram_id
        monkeypatch.setattr(sfh, "SessionLocal", Session)
        assert await sfh._is_banned(
            SimpleNamespace(id=tgid),
            SimpleNamespace(id=banned_tg, first_name="C", username="c")) is True
        # a different, non-banned customer is allowed through
        assert await sfh._is_banned(
            SimpleNamespace(id=tgid),
            SimpleNamespace(id=424242, first_name="X", username="x")) is False
        await engine.dispose()

    asyncio.run(go())


def test_bot_telegram_id_is_partial_unique(tmp_path):
    from sqlalchemy.exc import IntegrityError

    async def go():
        engine, Session = await _seed_engine(tmp_path, "uq.db")
        async with Session() as s:
            p1 = Panel(key="u1", host="u1.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            p2 = Panel(key="u2", host="u2.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            s.add_all([p1, p2])
            await s.flush()
            r1 = Reseller(panel_id=p1.id, admin_uuid="U1", name="R1")
            r2 = Reseller(panel_id=p2.id, admin_uuid="U2", name="R2")
            s.add_all([r1, r2])
            await s.flush()
            # two NULL bot_telegram_id rows are allowed (partial index excludes NULLs)
            s.add_all([
                StorefrontBot(reseller_id=r1.id, panel_id=p1.id, bot_token_enc="a"),
                StorefrontBot(reseller_id=r2.id, panel_id=p2.id, bot_token_enc="b"),
            ])
            await s.commit()
            # but two rows with the SAME non-null id collide
            await s.execute(
                StorefrontBot.__table__.update()
                .where(StorefrontBot.reseller_id == r1.id).values(bot_telegram_id=777))
            await s.commit()
            with pytest.raises(IntegrityError):
                await s.execute(
                    StorefrontBot.__table__.update()
                    .where(StorefrontBot.reseller_id == r2.id).values(bot_telegram_id=777))
                await s.commit()
        await engine.dispose()

    asyncio.run(go())


# ── v1.45.0: storefront enable default-on, one-bot-per-person, sub-reseller gate ──────

def test_reseller_storefront_enabled_defaults_on(tmp_path):
    async def body(s):
        p = Panel(key="d", host="d.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid="X", name="X")   # storefront_enabled NOT passed
        s.add(r)
        await s.flush()
        await s.refresh(r)
        assert r.storefront_enabled is True   # default ON for everyone

    _run(body, tmp_path, "sfdefault.db")


def test_two_panels_get_separate_bots(tmp_path):
    async def body(s):
        # one person (same bot_chat_id) top-level on two panels → a SEPARATE bot per panel
        rids = []
        for i, tok in enumerate(["111:aaa", "222:bbb"], start=1):
            p = Panel(key=f"tp{i}", host=f"tp{i}.invalid",
                      proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            s.add(p)
            await s.flush()
            r = Reseller(panel_id=p.id, admin_uuid=f"P{i}", name=f"P{i}",
                         bot_chat_id=555, storefront_enabled=True)
            s.add(r)
            await s.flush()
            await storefront.upsert_bot(s, reseller_id=r.id, panel_id=p.id, token=tok,
                                        bot_username=f"b{i}", bot_telegram_id=1000 + i)
            rids.append(r.id)
        a = await storefront.get_bot_for_reseller(s, rids[0])
        b = await storefront.get_bot_for_reseller(s, rids[1])
        assert a is not None and b is not None and a.id != b.id    # two distinct bots, one per panel
        assert {a.bot_telegram_id, b.bot_telegram_id} == {1001, 1002}

    _run(body, tmp_path, "twopanels.db")


def test_upsert_bot_repoint_migrates_data_no_duplicate(tmp_path):
    async def body(s):
        r, bot, cust = await _seed(s)
        plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=1000)
        old_id = bot.id
        updated = await storefront.upsert_bot(
            s, reseller_id=r.id, panel_id=r.panel_id, token="999:newtoken",
            bot_username="newbot", bot_telegram_id=8888)
        assert updated.id == old_id                               # SAME row repointed
        assert updated.bot_telegram_id == 8888 and updated.bot_username == "newbot"
        assert storefront.bot_token(updated) == "999:newtoken"    # token migrated
        plans = await storefront.list_plans(s, bot.id)
        custs = await storefront.list_customers(s, bot.id)
        assert len(plans) == 1 and plans[0].id == plan.id         # data preserved
        assert any(c.id == cust.id for c in custs)
        rows = (await s.execute(
            StorefrontBot.__table__.select().where(StorefrontBot.reseller_id == r.id))).all()
        assert len(rows) == 1                                     # never a second bot

    _run(body, tmp_path, "repoint.db")


def test_subreseller_cannot_setup_storefront(tmp_path):
    from app.bot import handlers as h

    async def body(s):
        p = Panel(key="h", host="h.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        s.add_all([
            Reseller(panel_id=p.id, admin_uuid="OWNER", name="Owner", is_owner=True),
            Reseller(panel_id=p.id, admin_uuid="TL", parent_admin_uuid="OWNER", name="TL",
                     bot_chat_id=111, storefront_enabled=True),
            Reseller(panel_id=p.id, admin_uuid="SUB", parent_admin_uuid="TL", name="SUB",
                     bot_chat_id=222, storefront_enabled=True),
        ])
        await s.commit()
        assert await h._can_setup_storefront(s, 111) is True    # first-tier admin → allowed
        assert await h._can_setup_storefront(s, 222) is False   # their sub-reseller → blocked
        assert await h._top_level_resellers(s, 222) == []

    _run(body, tmp_path, "subgate.db")


def test_my_services_detail_sends_single_message(tmp_path, monkeypatch):
    # Tapping a service in «سرویس‌های من» must send exactly ONE message (the QR photo with the status +
    # link + action buttons), not a detail message AND a separate «… آماده شد» config message.
    from app.bot.storefront import handlers as sfh
    from app.services import storefront_provision as sp

    async def go():
        engine, Session = await _seed_engine(tmp_path, "detail.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            order = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="x", gb=10,
                                    days=30, price_toman=1000, status="provisioned",
                                    panel_user_uuid="u1", sub_link="https://h/p/u1/#x")
            s.add(order)
            await s.commit()
            oid, cust_tg, bot_tg = order.id, cust.telegram_id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        monkeypatch.setattr(sfh.usercreate, "qr_png", lambda link: b"PNG")

        async def fake_live(session, sf, o):  # noqa: ANN001
            return sp.LiveStatus(True, used_gb=1.0, limit_gb=10.0, remaining_days=20)

        monkeypatch.setattr(sfh.storefront_provision, "live_status", fake_live)

        sends = {"n": 0}

        class FakeBot:
            id = bot_tg

            async def send_photo(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

            async def send_message(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

        class FakeMsg:
            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

        class FakeCb:
            data = f"sforder:{oid}"
            from_user = SimpleNamespace(id=cust_tg, first_name="C", username="c")
            message = FakeMsg()

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        await sfh.sf_order_detail(FakeCb(), FakeBot())
        assert sends["n"] == 1   # exactly one outbound message
        await engine.dispose()

    asyncio.run(go())


# ── v1.46.0: storefront forced-join (channel membership) ─────────────────────

def test_join_keyboards_callbacks():
    on = SimpleNamespace(channel_required=True, channel_id="-100x")
    cbs = [b.callback_data for row in sfkb.join_settings_kb(on).inline_keyboard for b in row]
    assert {"sfjointog", "sfjoinset", "sfjoinclear"} <= set(cbs)
    off = SimpleNamespace(channel_required=False, channel_id=None)
    cbs2 = [b.callback_data for row in sfkb.join_settings_kb(off).inline_keyboard for b in row]
    assert "sfjoinclear" not in cbs2          # no clear button when no channel is set
    pk = [b.callback_data for row in sfkb.join_prompt_kb("https://t.me/x").inline_keyboard
          for b in row if b.callback_data]
    assert "sfjoincheck" in pk


def test_channel_block_gates_non_member_customer(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "chanblock.db")
        async with Session() as s:
            r, bot, cust = await _seed(s)
            bot.channel_id, bot.channel_required, bot.channel_link = "-1009999", True, "https://t.me/x"
            r.bot_chat_id = 4242                # the admin's telegram id (customer is 555)
            await s.commit()
            rid, bot_tg, cust_tg = r.id, bot.bot_telegram_id, cust.telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        status = {"v": "left"}

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status=status["v"], is_member=False)

        cust_user = SimpleNamespace(id=cust_tg, first_name="C", username="c")
        admin_user = SimpleNamespace(id=4242, first_name="A", username="a")
        fb = FakeBot()

        assert (await sfh._channel_block(fb, cust_user)) is not None      # non-member → blocked
        status["v"] = "member"
        assert (await sfh._channel_block(fb, cust_user)) is None          # member → allowed
        status["v"] = "left"
        assert (await sfh._channel_block(fb, admin_user)) is None         # admin → never gated
        async with Session() as s:                                       # required OFF → allowed
            b = await storefront.get_bot_for_reseller(s, rid)
            b.channel_required = False
            await s.commit()
        assert (await sfh._channel_block(fb, cust_user)) is None
        await engine.dispose()

    asyncio.run(go())


def test_set_channel_requires_bot_admin(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "setchan.db")
        async with Session() as s:
            r, bot, _c = await _seed(s)
            r.bot_chat_id = 4242
            await s.commit()
            rid, bot_tg = r.id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        admin_status = {"v": "left"}

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status=admin_status["v"])

            async def get_chat(self, chat):  # noqa: ANN001
                return SimpleNamespace(username="mychan")

        class FakeState:
            async def clear(self):
                return None

            async def set_state(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        class FakeMsg:
            from_user = SimpleNamespace(id=4242, first_name="A", username="a")
            text = None
            forward_origin = SimpleNamespace(chat=SimpleNamespace(type="channel", id=-1009999))

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        # bot NOT admin → nothing saved, stays gated off
        await sfh.sf_join_channel_set(FakeMsg(), FakeState(), FakeBot())
        async with Session() as s:
            b = await storefront.get_bot_for_reseller(s, rid)
            assert b.channel_id is None and b.channel_required is False

        # bot IS admin → channel saved + forced-join enabled + link captured
        admin_status["v"] = "administrator"
        await sfh.sf_join_channel_set(FakeMsg(), FakeState(), FakeBot())
        async with Session() as s:
            b = await storefront.get_bot_for_reseller(s, rid)
            assert b.channel_id == "-1009999" and b.channel_required is True
            assert b.channel_link == "https://t.me/mychan"
        await engine.dispose()

    asyncio.run(go())


def test_join_toggle_requires_channel(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "jointog.db")
        async with Session() as s:
            r, bot, _c = await _seed(s)
            r.bot_chat_id = 4242
            await s.commit()
            rid, bot_tg = r.id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status="administrator")

        class FakeCbMsg:
            async def edit_reply_markup(self, **k):  # noqa: ANN003
                return None

        class FakeCb:
            from_user = SimpleNamespace(id=4242, first_name="A", username="a")
            message = FakeCbMsg()

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        await sfh.sf_join_toggle(FakeCb(), FakeBot())          # no channel → can't enable
        async with Session() as s:
            assert (await storefront.get_bot_for_reseller(s, rid)).channel_required is False
        async with Session() as s:                            # set a channel
            b = await storefront.get_bot_for_reseller(s, rid)
            b.channel_id = "-100777"
            await s.commit()
        await sfh.sf_join_toggle(FakeCb(), FakeBot())          # now it enables
        async with Session() as s:
            assert (await storefront.get_bot_for_reseller(s, rid)).channel_required is True
        await engine.dispose()

    asyncio.run(go())
