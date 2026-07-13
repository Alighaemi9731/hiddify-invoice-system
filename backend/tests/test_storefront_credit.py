"""Storefront wallet credit codes («کد شارژ/هدیه»): percent/fixed bonus + standalone gift, caps,
per-customer + global limits, active window, case-insensitivity, and the confirm-topup application
path (bonus credited as a separate ledger row; a rejected top-up consumes nothing)."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfcredit.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCreditRedemption,
    StorefrontCustomer,
)
from app.services import storefront_credit as cc  # noqa: E402
from app.services import storefront_wallet as wallet  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    p = Panel(key="p", host="h", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("1:t") or "")
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=5, wallet_balance_toman=0)
    s.add(cust)
    await s.commit()
    return bot, cust


def test_percent_bonus_with_cap():
    async def body(s):
        bot, cust = await _seed(s)
        await cc.create_code(s, bot.id, code="p20", kind="percent", percent_off=20,
                             max_bonus_toman=50_000)
        # 100k → +20% = 20k (under cap)
        q = await cc.quote(s, bot.id, "P20", topup_toman=100_000, customer_id=cust.id)
        assert q.ok and q.bonus_toman == 20_000
        # 1,000k → 200k but capped at 50k
        q2 = await cc.quote(s, bot.id, "p20", topup_toman=1_000_000, customer_id=cust.id)
        assert q2.ok and q2.bonus_toman == 50_000
    _run(body)


def test_fixed_bonus_and_case_insensitive_unique():
    async def body(s):
        bot, cust = await _seed(s)
        await cc.create_code(s, bot.id, code="Welcome", kind="fixed", amount_toman=30_000)
        assert await cc.code_exists(s, bot.id, "WELCOME")            # case-insensitive
        q = await cc.quote(s, bot.id, "welcome", topup_toman=10_000, customer_id=cust.id)
        assert q.ok and q.bonus_toman == 30_000
        # a fixed BONUS code cannot be redeemed standalone (no top-up)
        q0 = await cc.quote(s, bot.id, "welcome", topup_toman=0, customer_id=cust.id)
        assert not q0.ok and q0.reason == "min_not_met"
    _run(body)


def test_gift_is_standalone_and_credits_wallet():
    async def body(s):
        bot, cust = await _seed(s)
        await cc.create_code(s, bot.id, code="GIFT", kind="fixed", amount_toman=25_000, is_gift=True)
        q = await wallet.apply_gift_code(s, cust.id, bot.id, "gift")
        assert q.ok and q.bonus_toman == 25_000
        await s.refresh(cust)
        assert wallet.balance(cust) == Decimal(25_000)
        # one-per-customer: a second redemption is rejected, balance unchanged
        q2 = await wallet.apply_gift_code(s, cust.id, bot.id, "gift")
        assert not q2.ok and q2.reason == "per_customer_exhausted"
        await s.refresh(cust)
        assert wallet.balance(cust) == Decimal(25_000)
    _run(body)


def test_per_customer_and_global_limits():
    async def body(s):
        bot, cust = await _seed(s)
        await cc.create_code(s, bot.id, code="G", kind="fixed", amount_toman=1000, is_gift=True,
                             max_uses=1, per_customer_limit=1)
        q = await wallet.apply_gift_code(s, cust.id, bot.id, "G")
        assert q.ok
        # global cap of 1 reached → another customer is rejected
        other = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=6, wallet_balance_toman=0)
        s.add(other)
        await s.commit()
        q2 = await wallet.apply_gift_code(s, other.id, bot.id, "G")
        assert not q2.ok and q2.reason == "global_exhausted"
    _run(body)


def test_active_window_and_expiry():
    async def body(s):
        bot, cust = await _seed(s)
        await cc.create_code(s, bot.id, code="future", kind="fixed", amount_toman=1000,
                             starts_at=NOW + dt.timedelta(days=1))
        assert (await cc.quote(s, bot.id, "future", topup_toman=1000, customer_id=cust.id)).reason == "not_started"
        await cc.create_code(s, bot.id, code="past", kind="fixed", amount_toman=1000,
                             expires_at=NOW - dt.timedelta(days=1))
        assert (await cc.quote(s, bot.id, "past", topup_toman=1000, customer_id=cust.id)).reason == "expired"
    _run(body)


def test_confirm_topup_applies_bonus_and_records():
    async def body(s):
        bot, cust = await _seed(s)
        code = await cc.create_code(s, bot.id, code="P10", kind="percent", percent_off=10)
        txn = await wallet.create_topup(s, cust, 100_000, method="card", credit_code_id=code.id)
        changed, _ = await wallet.confirm_topup(s, txn.id)
        assert changed
        await s.refresh(cust)
        assert wallet.balance(cust) == Decimal(110_000)              # 100k base + 10k bonus
        await s.refresh(code)
        assert code.used_count == 1
        red = (await s.execute(select(StorefrontCreditRedemption))).scalars().all()
        assert len(red) == 1 and red[0].bonus_toman == 10_000
    _run(body)


def test_rejected_topup_consumes_no_code():
    async def body(s):
        bot, cust = await _seed(s)
        code = await cc.create_code(s, bot.id, code="P10", kind="percent", percent_off=10)
        txn = await wallet.create_topup(s, cust, 100_000, method="card", credit_code_id=code.id)
        changed, _ = await wallet.reject_topup(s, txn.id)
        assert changed
        await s.refresh(cust)
        await s.refresh(code)
        assert wallet.balance(cust) == Decimal(0) and code.used_count == 0
        assert (await s.execute(select(StorefrontCreditRedemption))).scalars().all() == []
    _run(body)


def test_helpers_and_stats():
    from app.services import storefront as sf_svc

    async def body(s):
        bot, cust = await _seed(s)
        assert not await cc.has_enabled_codes(s, bot.id)
        await cc.create_code(s, bot.id, code="G", kind="fixed", amount_toman=10_000, is_gift=True)
        await cc.create_code(s, bot.id, code="P", kind="percent", percent_off=10)
        assert await cc.has_enabled_codes(s, bot.id)
        assert await cc.has_gift_codes(s, bot.id)
        await wallet.apply_gift_code(s, cust.id, bot.id, "G")   # +10k gift
        n, total = await cc.redemptions_this_month(s, bot.id)
        assert n == 1 and total == 10_000
        st = await sf_svc.stats_for_bot(s, bot.id)
        assert st.credit_redemptions_month == 1 and st.credit_bonus_month_toman == 10_000
    _run(body)


def test_double_confirm_credits_bonus_once():
    async def body(s):
        bot, cust = await _seed(s)
        code = await cc.create_code(s, bot.id, code="F", kind="fixed", amount_toman=5000)
        txn = await wallet.create_topup(s, cust, 50_000, method="card", credit_code_id=code.id)
        await wallet.confirm_topup(s, txn.id)
        await wallet.confirm_topup(s, txn.id)   # re-confirm → no-op
        await s.refresh(cust)
        await s.refresh(code)
        assert wallet.balance(cust) == Decimal(55_000) and code.used_count == 1
    _run(body)
