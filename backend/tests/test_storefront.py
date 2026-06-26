"""Storefront Phase-1: wallet ledger correctness + owner monthly-fee (active-only)."""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/storefront.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import Panel, Reseller, StorefrontBot  # noqa: E402
from app.services import storefront, storefront_wallet  # noqa: E402


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
