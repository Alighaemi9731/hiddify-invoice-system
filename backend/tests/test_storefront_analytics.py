"""Fleet-wide storefront analytics (the owner's «آنالیز ربات‌های فروشگاهی» page).

The portal dashboard answers «how is MY shop doing?». This is the owner's cross-shop view, so what
has to be pinned here is that the aggregation stays HONEST once there is more than one shop: money
is net of refunds per Tehran day, a top-up is never a sale, a shop with no rows still appears with
zeros, and the live service-health pass uses the same expiry math as the reminder job.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.services import storefront_analytics as sfa
from app.services.periods import month_period

UTC = dt.timezone.utc
PERIOD = month_period(2026, 7)
TODAY = dt.date(2026, 7, 20)
NOW = dt.datetime.combine(TODAY, dt.time(12), sfa.TEHRAN).astimezone(UTC)


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


def _noon(day: dt.date) -> dt.datetime:
    """Noon Tehran → UTC, so a row lands unambiguously inside that Tehran calendar day."""
    return dt.datetime.combine(day, dt.time(12), sfa.TEHRAN).astimezone(UTC)


async def _shop(s, key: str, *, enabled=True, status="active", closed=False):
    panel = Panel(key=key, host=f"{key}.example", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    reseller = Reseller(panel_id=panel.id, admin_uuid=f"a-{key}", name=f"R-{key}")
    s.add(reseller)
    await s.flush()
    bot = StorefrontBot(
        reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="t",
        enabled=enabled, status=status, shop_closed=closed, bot_username=f"{key}_bot",
        created_at=_noon(dt.date(2026, 1, 1)),
    )
    s.add(bot)
    await s.flush()
    customer = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=hash(key) % 10_000, name="C")
    s.add(customer)
    await s.flush()
    return panel, reseller, bot, customer


def _sale(bot, cust, amount: int, day: dt.date, *, kind="purchase", status="done", **kw):
    return StorefrontWalletTxn(
        storefront_bot_id=bot.id, customer_id=cust.id, kind=kind, status=status,
        amount_toman=amount, created_at=_noon(day), **kw,
    )


def test_fleet_totals_are_net_of_refunds_and_split_by_operation():
    async def body(s):
        _p1, _r1, shop_a, cust_a = await _shop(s, "pa")
        _p2, _r2, shop_b, cust_b = await _shop(s, "pb", closed=True)
        op = StorefrontOperation(
            storefront_bot_id=shop_a.id, customer_id=cust_a.id, op_type="renewal",
            status="done", op_id="op-1", price_toman=300, created_at=_noon(TODAY),
        )
        s.add(op)
        await s.flush()
        s.add_all([
            _sale(shop_a, cust_a, -1000, TODAY),                       # today, purchase
            _sale(shop_a, cust_a, -300, TODAY, operation_id=op.id),    # today, renewal
            _sale(shop_a, cust_a, 200, TODAY, kind="refund"),          # today, refunded
            _sale(shop_b, cust_b, -500, dt.date(2026, 7, 4)),          # earlier in the month
            _sale(shop_b, cust_b, 9_999, TODAY, kind="topup", status="confirmed"),  # NOT a sale
            _sale(shop_b, cust_b, -700, dt.date(2026, 6, 10)),         # previous month
        ])
        await s.commit()

        out = await sfa.analytics(s, PERIOD, now=NOW)

        assert (out.sales_today.gross_toman, out.sales_today.reversals_toman) == (1300, 200)
        assert out.sales_today.net_toman == 1100
        assert out.sales_today.orders == 2
        # the split comes from the linked operation; the unlinked purchase stays «unknown»
        assert (out.sales_today.renewal_count, out.sales_today.renewal_toman) == (1, 300)
        assert (out.sales_today.unknown_count, out.sales_today.unknown_toman) == (1, 1000)
        assert out.sales_period.net_toman == 1600          # 1300 − 200 + 500
        assert out.sales_previous_period.net_toman == 700  # June is reported on its own
        assert out.customers.avg_order_toman == 600        # 1800 gross ÷ 3 paid orders

        # bots: both exist, one is temporarily closed → only one can actually sell
        assert (out.bots.total, out.bots.enabled, out.bots.closed, out.bots.selling) == (2, 2, 1, 1)
        assert out.bots.eligible_resellers == 2

        by_shop = {row.shop_id: row for row in out.shops}
        assert by_shop[shop_a.id].net_sales_toman == 1100
        assert by_shop[shop_a.id].today_net_toman == 1100
        assert by_shop[shop_b.id].net_sales_toman == 500
        assert by_shop[shop_b.id].today_net_toman == 0
        assert out.shops[0].shop_id == shop_a.id           # ranked by period net sales
        assert by_shop[shop_a.id].last_sale_at is not None
    _run(body)


def test_daily_series_is_zero_filled_and_separates_topups_from_sales():
    async def body(s):
        _p, _r, shop, cust = await _shop(s, "pd")
        d1, d3 = dt.date(2026, 7, 1), dt.date(2026, 7, 3)
        s.add_all([
            _sale(shop, cust, -150, d1),
            _sale(shop, cust, 40, d3, kind="refund"),
            _sale(shop, cust, 5_000, d1, kind="topup", status="confirmed"),
            _sale(shop, cust, 7_000, d1, kind="topup", status="pending"),   # not credited yet
            StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=99, created_at=_noon(d3)),
        ])
        await s.commit()

        out = await sfa.analytics(s, PERIOD, now=NOW)
        days = {p.date: p for p in out.daily}

        assert len(out.daily) == 31 and out.daily[0].date == dt.date(2026, 7, 1)
        assert days[d1].net_toman == 150 and days[d1].orders == 1
        assert days[d1].topups_toman == 5_000               # only the CONFIRMED top-up
        assert days[dt.date(2026, 7, 2)].net_toman == 0     # zero-filled gap
        assert days[d3].net_toman == -40 and days[d3].orders == 0
        assert days[d3].new_customers == 1
        assert out.topups.pending_count == 1 and out.topups.pending_toman == 7_000
        assert out.topups.confirmed_toman == 5_000
    _run(body)


def test_service_health_uses_panel_expiry_then_falls_back_to_the_order():
    async def body(s):
        panel, _r, shop, cust = await _shop(s, "ph")
        # 1) a panel-backed service expiring in 2 days, 90٪ of its volume consumed
        s.add(EndUserSnapshot(
            panel_id=panel.id, user_uuid="u1", usage_limit_gb=10, current_usage_gb=9,
            start_date=TODAY - dt.timedelta(days=28), package_days=30,
        ))
        # 2) a panel-backed service that already lapsed
        s.add(EndUserSnapshot(
            panel_id=panel.id, user_uuid="u2", usage_limit_gb=10, current_usage_gb=1,
            start_date=TODAY - dt.timedelta(days=40), package_days=30,
        ))
        s.add_all([
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="u1",
                            status="provisioned", gb=10, days=30),
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="u2",
                            status="provisioned", gb=10, days=30),
            # 3) no snapshot → the order's own duration decides (created 5 days ago, 30 days)
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="u3",
                            status="provisioned", gb=5, days=30,
                            created_at=_noon(TODAY - dt.timedelta(days=5))),
            # 4) a cancelled service is not "active" at all
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="u4",
                            status="deleted", gb=5, days=30),
            # 5) a free trial still running
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="u5",
                            status="provisioned", is_trial=True, gb=1, days=1,
                            created_at=_noon(TODAY)),
        ])
        await s.commit()

        out = await sfa.analytics(s, PERIOD, now=NOW)

        assert out.services.active == 4 and out.services.deleted == 1
        assert out.services.trials_active == 1 and out.services.trials_in_period == 1
        assert out.services.expired == 1                     # u2
        assert out.services.expiring_3d == 2                 # u1 (2 days) + u5 (trial, today+1)
        assert out.services.high_usage == 1                  # u1 at 90٪
        assert out.services.quota_gb == 20.0                 # only the snapshot-backed volumes
        assert {row.shop_id: row.expiring_3d for row in out.shops} == {shop.id: 2}
    _run(body)


def test_trial_conversion_counts_a_trial_customer_who_later_paid():
    async def body(s):
        panel, _r, shop, cust = await _shop(s, "pt")
        never_paid = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=4242)
        s.add(never_paid)
        await s.flush()
        s.add_all([
            StorefrontOrder(customer_id=cust.id, panel_id=panel.id, panel_user_uuid="t1",
                            status="deleted", is_trial=True, gb=1, days=1),
            StorefrontOrder(customer_id=never_paid.id, panel_id=panel.id, panel_user_uuid="t2",
                            status="provisioned", is_trial=True, gb=1, days=1),
            _sale(shop, cust, -900, TODAY),
        ])
        await s.commit()

        out = await sfa.analytics(s, PERIOD, now=NOW)

        assert out.trial.trial_customers == 2
        assert out.trial.converted_customers == 1
        assert out.trial.rate == 0.5
        assert out.customers.buyers_in_period == 1
        assert out.customers.repeat_buyers_in_period == 0
        assert out.customers.arppu_toman == 900
    _run(body)


def test_endpoint_requires_auth_and_serializes_the_report():
    """The route is owner-only, validates the period, and its cache is per-period."""
    from httpx import ASGITransport, AsyncClient

    from app.api import storefront_analytics as api
    from app.core.db import get_session
    from app.core.security import get_current_subject
    from app.main import app

    async def body(s):
        _p, _r, shop, cust = await _shop(s, "pe")
        s.add(_sale(shop, cust, -2_500, TODAY))
        await s.commit()
        api._cache.clear()

        async def session_override():
            yield s

        app.dependency_overrides[get_session] = session_override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/api/storefront-analytics")).status_code == 401

                app.dependency_overrides[get_current_subject] = lambda: "owner"
                ok = await client.get("/api/storefront-analytics", params={"period": "2026-07"})
                assert ok.status_code == 200
                payload = ok.json()
                assert payload["period"] == "2026-07"
                assert payload["sales_period"]["net_toman"] == 2_500
                assert [row["shop_id"] for row in payload["shops"]] == [shop.id]

                # A different month must never be served from the cached month's entry.
                other = await client.get("/api/storefront-analytics", params={"period": "2026-06"})
                assert other.status_code == 200
                assert other.json()["sales_period"]["net_toman"] == 0

                bad = await client.get("/api/storefront-analytics", params={"period": "nope"})
                assert bad.status_code == 422
        finally:
            app.dependency_overrides.pop(get_current_subject, None)
            app.dependency_overrides.pop(get_session, None)
            api._cache.clear()

    _run(body)
