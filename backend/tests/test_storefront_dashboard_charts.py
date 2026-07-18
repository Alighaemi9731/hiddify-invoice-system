"""The redesigned storefront dashboard aggregates: daily sales trend + best-selling plans.

The old page's only chart restated the same three numbers printed beneath it and the range was
locked to the current month. These are the aggregates that replace it, so their money semantics
have to be pinned: net-of-refunds per Tehran day, and per-plan revenue taken from what was actually
CHARGED (plans are edited in place, so today's price must never restate past sales).
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import storefront_reporting

UTC = dt.timezone.utc


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    r = Reseller(panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=1)
    s.add(r)
    await s.flush()
    shop = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="t", enabled=True)
    s.add(shop)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=5, name="C")
    plan_a = StorefrontPlan(storefront_bot_id=shop.id, gb=30, days=30, price_toman=100)
    plan_b = StorefrontPlan(storefront_bot_id=shop.id, gb=10, days=7, price_toman=50)
    s.add_all([cust, plan_a, plan_b])
    await s.flush()
    return shop, cust, plan_a, plan_b


def _tehran_noon(day: dt.date) -> dt.datetime:
    """Noon Tehran → UTC, so the row lands unambiguously inside that Tehran calendar day."""
    return dt.datetime.combine(day, dt.time(12), storefront_reporting.TEHRAN).astimezone(UTC)


def test_daily_sales_is_net_per_tehran_day_and_zero_filled():
    async def body(s):
        shop, cust, _a, _b = await _seed(s)
        d1, d3 = dt.date(2026, 7, 1), dt.date(2026, 7, 3)
        s.add_all([
            # two sales on day 1 …
            StorefrontWalletTxn(storefront_bot_id=shop.id, customer_id=cust.id, kind="purchase",
                                status="done", amount_toman=-100, created_at=_tehran_noon(d1)),
            StorefrontWalletTxn(storefront_bot_id=shop.id, customer_id=cust.id, kind="purchase",
                                status="done", amount_toman=-50, created_at=_tehran_noon(d1)),
            # … one refunded on day 3 (subtracted on the day it happened, like the period totals)
            StorefrontWalletTxn(storefront_bot_id=shop.id, customer_id=cust.id, kind="refund",
                                status="done", amount_toman=40, created_at=_tehran_noon(d3)),
            # a top-up must never count as a sale
            StorefrontWalletTxn(storefront_bot_id=shop.id, customer_id=cust.id, kind="topup",
                                status="confirmed", amount_toman=999, created_at=_tehran_noon(d1)),
        ])
        await s.commit()

        pts = await storefront_reporting._daily_sales(s, shop.id, d1, d3)
        assert [p.date for p in pts] == [d1, dt.date(2026, 7, 2), d3]   # zero-filled, no gaps
        assert [p.amount_toman for p in pts] == [150, 0, -40]
        assert [p.orders for p in pts] == [2, 0, 0]                     # refunds aren't orders
    _run(body)


def test_top_plans_ranks_by_revenue_actually_charged_not_todays_price():
    async def body(s):
        shop, cust, plan_a, plan_b = await _seed(s)
        day = dt.date(2026, 7, 5)
        s.add_all([
            StorefrontOperation(storefront_bot_id=shop.id, customer_id=cust.id, plan_id=plan_b.id,
                                op_type="purchase", status="done", price_toman=50,
                                op_id="o1", created_at=_tehran_noon(day)),
            StorefrontOperation(storefront_bot_id=shop.id, customer_id=cust.id, plan_id=plan_b.id,
                                op_type="renewal", status="done", price_toman=50,
                                op_id="o2", created_at=_tehran_noon(day)),
            StorefrontOperation(storefront_bot_id=shop.id, customer_id=cust.id, plan_id=plan_a.id,
                                op_type="purchase", status="done", price_toman=70,
                                op_id="o3", created_at=_tehran_noon(day)),
            # not counted: never completed
            StorefrontOperation(storefront_bot_id=shop.id, customer_id=cust.id, plan_id=plan_a.id,
                                op_type="purchase", status="failed", price_toman=100,
                                op_id="o4", created_at=_tehran_noon(day)),
        ])
        await s.commit()

        # Plan A's price is raised AFTER the sales — historic revenue must not move.
        plan_a.price_toman = 5000
        await s.commit()

        top = await storefront_reporting._top_plans(s, shop.id, day, day)
        assert [(t.plan_id, t.orders, t.amount_toman) for t in top] == [
            (plan_b.id, 2, 100),    # best seller by revenue (2 × 50), renewals included
            (plan_a.id, 1, 70),     # still 70 — NOT the new 5000 price
        ]
        assert "گیگ" in top[0].title
    _run(body)
