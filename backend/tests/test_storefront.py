"""Storefront Phase-1: wallet ledger correctness + owner monthly-fee (active-only)."""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/storefront.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot import keyboards  # noqa: E402
from app.bot.storefront import keyboards as sfkb  # noqa: E402
from app.core import crypto  # noqa: E402
from app.models import Panel, Reseller, StorefrontBot, StorefrontOrder  # noqa: E402
from app.models.storefront import StorefrontCustomer, StorefrontPlan  # noqa: E402
from app.services import (  # noqa: E402
    storefront,
    storefront_provision,
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

    async def fake_create(session, reseller, *, count, gb, days, base_name):  # noqa: ANN001, ANN002
        captured["base_name"] = base_name
        captured["count"] = count
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid="u-1",
                                     sub_link=f"https://h/p/u-1/#{base_name}")],
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
