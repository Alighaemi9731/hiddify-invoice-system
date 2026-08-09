"""The shop's own profit-and-loss: quota sold vs what the owner invoices vs what the bot collected.

Every assertion here exists because the underlying data lies in a specific way. The purchase path
records `gb = 0` on its operation; `StorefrontOrder.gb` is rewritten by renewals; an auto-renew's
wallet row is created at ARM time and only relabelled into a purchase when it fires. A report that
takes any of those at face value produces a plausible, wrong profit — so the resolution rules are
pinned, not just the arithmetic.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.core.storefront_access import StorefrontAccess
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import storefront_reporting

UTC = dt.timezone.utc
PRICE_PER_GB = 2000


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                await body(s, engine)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s, *, exclude_from_billing: bool = False):
    panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    reseller = Reseller(
        panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=1,
        price_per_gb=PRICE_PER_GB, exclude_from_billing=exclude_from_billing,
    )
    s.add(reseller)
    await s.flush()
    shop = StorefrontBot(reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="t", enabled=True)
    s.add(shop)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=5, name="C")
    plan = StorefrontPlan(storefront_bot_id=shop.id, gb=50, days=30, price_toman=150_000)
    free_plan = StorefrontPlan(storefront_bot_id=shop.id, gb=1, days=1, price_toman=5_000)
    s.add_all([cust, plan, free_plan])
    await s.flush()
    access = StorefrontAccess(shop=shop, reseller=reseller, panel=panel)
    return access, shop, cust, plan, free_plan


def _tehran_noon(day: dt.date) -> dt.datetime:
    """Noon Tehran → UTC, so the row lands unambiguously inside that Tehran calendar day."""
    return dt.datetime.combine(day, dt.time(12), storefront_reporting.TEHRAN).astimezone(UTC)


def _op(shop, cust, *, op_type, when, plan=None, order=None, gb=0, by_admin=False):
    return StorefrontOperation(
        op_id=f"{op_type}-{when.isoformat()}-{gb}-{id(when)}",
        op_type=op_type, storefront_bot_id=shop.id, customer_id=cust.id, status="done",
        plan_id=(plan.id if plan is not None else None),
        order_id=(order.id if order is not None else None),
        gb=gb, by_admin=by_admin, created_at=when,
    )


def _sale(shop, cust, amount, when, *, kind="purchase", decided_at=None):
    return StorefrontWalletTxn(
        storefront_bot_id=shop.id, customer_id=cust.id, kind=kind, status="done",
        amount_toman=amount, created_at=when, decided_at=decided_at or when,
    )


def _month(result, label):
    return next(m for m in result.months if m.label == label)


def test_purchase_gb_recovered_from_plan_and_renewal_uses_its_own():
    """A purchase operation stores gb=0 (claim overwrites what reserve saved) — the plan is the
    fallback. A renewal carries the real figure and must be trusted over any plan edit since."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        day = dt.date(2026, 7, 10)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
            # The plan has since been repriced/resized to 50 GB; this renewal sold 20.
            _op(shop, cust, op_type="renewal", when=_tehran_noon(day), plan=plan, gb=20),
            _sale(shop, cust, -150_000, _tehran_noon(day)),
            _sale(shop, cust, -60_000, _tehran_noon(day)),
        ])
        await s.flush()

        result = await storefront_reporting.finance(s, access)
        july = _month(result, "2026-07")
        assert (july.purchases, july.renewals) == (1, 1)
        assert july.gb_sold == 70          # 50 from the plan + 20 from the renewal itself
        assert july.gb_billable == 70
        assert july.cost_toman == 70 * PRICE_PER_GB
        assert july.net_sales_toman == 210_000
        assert july.profit_toman == 210_000 - 140_000
        assert result.cost_per_gb_toman == PRICE_PER_GB
        assert result.totals.gb_sold == 70 and result.totals.label == ""

    _run(body)


def test_free_test_size_earns_revenue_but_costs_nothing():
    """A <= free_under_gb plan is a free test config on the owner's invoice. It still took money
    from the customer, so it is pure margin — counted in gb_sold, excluded from gb_billable."""
    async def body(s, _engine):
        access, shop, cust, plan, free_plan = await _seed(s)
        day = dt.date(2026, 7, 4)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=free_plan, gb=0),
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
            _sale(shop, cust, -5_000, _tehran_noon(day)),
            _sale(shop, cust, -150_000, _tehran_noon(day)),
        ])
        await s.flush()

        july = _month(await storefront_reporting.finance(s, access), "2026-07")
        assert (july.gb_sold, july.gb_free, july.gb_billable) == (51, 1, 50)
        assert july.cost_toman == 50 * PRICE_PER_GB   # the 1 GB test is not billed
        assert july.profit_toman == 155_000 - 100_000

    _run(body)


def test_refund_and_reversal_reduce_the_month_they_are_issued_in():
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        day = dt.date(2026, 7, 20)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
            _sale(shop, cust, -150_000, _tehran_noon(day)),
            _sale(shop, cust, 40_000, _tehran_noon(day), kind="refund"),
            _sale(shop, cust, 10_000, _tehran_noon(day), kind="renew_reversal"),
        ])
        await s.flush()

        july = _month(await storefront_reporting.finance(s, access), "2026-07")
        assert (july.gross_sales_toman, july.reversals_toman) == (150_000, 50_000)
        assert july.net_sales_toman == 100_000
        assert july.profit_toman == 100_000 - 50 * PRICE_PER_GB

    _run(body)


def test_settled_autorenew_hold_is_booked_when_it_fired_not_when_it_was_armed():
    """`place_hold` writes the ledger row at ARM time and `settle_hold` relabels that SAME row into
    a purchase when the renewal fires, leaving `created_at` behind. Bucketing on `created_at` would
    put the revenue in March and its cost in April — a fabricated profit in one month and a
    fabricated loss in the next."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        armed = _tehran_noon(dt.date(2026, 3, 28))
        fired = _tehran_noon(dt.date(2026, 4, 2))
        s.add_all([
            _op(shop, cust, op_type="renewal", when=fired, plan=plan, gb=50),
            _sale(shop, cust, -150_000, armed, decided_at=fired),
        ])
        await s.flush()

        result = await storefront_reporting.finance(s, access)
        assert [m.label for m in result.months] == ["2026-04"]
        april = _month(result, "2026-04")
        assert april.net_sales_toman == 150_000
        assert april.cost_toman == 50 * PRICE_PER_GB
        assert april.profit_toman == 50_000

    _run(body)


def test_admin_renewal_has_cost_but_no_revenue():
    """A shop admin renewing on a customer's behalf is free by design (no debit). The GB are still
    bought from the owner, so the month legitimately reads as a loss — pinned so it is not
    'fixed' into silently dropping the cost."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        day = dt.date(2026, 5, 9)
        s.add(_op(shop, cust, op_type="renewal", when=_tehran_noon(day), plan=plan, gb=50,
                  by_admin=True))
        await s.flush()

        may = _month(await storefront_reporting.finance(s, access), "2026-05")
        assert may.renewals == 1
        assert may.cost_toman == 50 * PRICE_PER_GB
        assert may.net_sales_toman == 0
        assert may.profit_toman == -100_000

    _run(body)


def test_months_are_tehran_calendar_not_utc():
    """22:00 Tehran on 31 July is 18:30 UTC the same day, but 01:00 Tehran on 1 August is 21:30 UTC
    on 31 July — a raw UTC `.date()` would file the August sale under July."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        late_july = dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC)     # 22:00 Tehran, 31 July
        early_august = dt.datetime(2026, 7, 31, 21, 30, tzinfo=UTC)  # 01:00 Tehran, 1 August
        s.add_all([
            _op(shop, cust, op_type="purchase", when=late_july, plan=plan, gb=0),
            _op(shop, cust, op_type="purchase", when=early_august, plan=plan, gb=0),
            _sale(shop, cust, -150_000, late_july),
            _sale(shop, cust, -150_000, early_august),
        ])
        await s.flush()

        result = await storefront_reporting.finance(s, access)
        assert [m.label for m in result.months] == ["2026-08", "2026-07"]   # newest first
        assert _month(result, "2026-07").purchases == 1
        assert _month(result, "2026-08").purchases == 1

    _run(body)


def test_trial_grant_contributes_nothing():
    """A trial creates an order but no operation and no wallet debit, so it must not appear on
    either side of the equation — no free quota counted as cost, no phantom revenue."""
    async def body(s, _engine):
        access, shop, cust, _plan, _free = await _seed(s)
        s.add(StorefrontOrder(
            customer_id=cust.id, panel_id=access.panel.id, gb=1, days=1, price_toman=0,
            status="provisioned", is_trial=True, panel_user_uuid="trial-uuid",
            created_at=_tehran_noon(dt.date(2026, 7, 5)),
        ))
        await s.flush()

        result = await storefront_reporting.finance(s, access)
        assert result.months == []
        assert result.totals.gb_sold == 0 and result.totals.net_sales_toman == 0

    _run(body)


def test_operation_with_no_plan_and_no_order_is_reported_not_silently_zeroed():
    """Maintenance can prune both the order and the plan behind an old purchase. Counting that as
    0 GB would keep its revenue while dropping its cost — i.e. inflate the profit. Surface it."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        day = dt.date(2026, 6, 15)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), gb=0),
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
            _sale(shop, cust, -150_000, _tehran_noon(day)),
        ])
        await s.flush()

        june = _month(await storefront_reporting.finance(s, access), "2026-06")
        assert june.purchases == 2
        assert june.unresolved_ops == 1
        assert june.gb_sold == 50   # only the resolvable one

    _run(body)


def test_order_gb_is_the_last_resort_when_the_plan_is_gone():
    async def body(s, _engine):
        access, shop, cust, _plan, _free = await _seed(s)
        order = StorefrontOrder(
            customer_id=cust.id, panel_id=access.panel.id, gb=15, days=30, price_toman=45_000,
            status="provisioned", panel_user_uuid="u-1",
        )
        s.add(order)
        await s.flush()
        s.add(_op(shop, cust, op_type="purchase", when=_tehran_noon(dt.date(2026, 2, 3)),
                  order=order, gb=0))
        await s.flush()

        feb = _month(await storefront_reporting.finance(s, access), "2026-02")
        assert feb.gb_sold == 15
        assert feb.cost_toman == 15 * PRICE_PER_GB

    _run(body)


def test_reseller_we_never_invoice_has_no_cost():
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s, exclude_from_billing=True)
        day = dt.date(2026, 7, 1)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
            _sale(shop, cust, -150_000, _tehran_noon(day)),
        ])
        await s.flush()

        result = await storefront_reporting.finance(s, access)
        july = _month(result, "2026-07")
        assert result.cost_per_gb_toman == 0
        assert july.gb_billable == 50      # the quota still moved …
        assert july.cost_toman == 0        # … it just costs them nothing
        assert july.profit_toman == 150_000

    _run(body)


def test_month_limit_truncates_the_list_but_never_the_totals():
    """A `created_at` cutoff in the query would be the obvious implementation and would quietly
    stop `totals` from being all-time."""
    async def body(s, _engine):
        access, shop, cust, plan, _free = await _seed(s)
        for index in range(5):
            day = dt.date(2026, 1, 1) + dt.timedelta(days=31 * index)
            s.add_all([
                _op(shop, cust, op_type="purchase", when=_tehran_noon(day), plan=plan, gb=0),
                _sale(shop, cust, -150_000, _tehran_noon(day)),
            ])
        await s.flush()

        result = await storefront_reporting.finance(s, access, month_limit=2)
        assert len(result.months) == 2
        assert result.totals.purchases == 5
        assert result.totals.gb_sold == 250
        assert result.totals.net_sales_toman == 750_000
        assert result.totals.cost_toman == 250 * PRICE_PER_GB

    _run(body)


def test_finance_query_budget_and_read_only():
    """The dashboard's budget test only instruments `dashboard`; without this one a future widget
    could turn this page into an N+1 unnoticed."""
    async def body(s, engine):
        access, shop, cust, plan, _free = await _seed(s)
        s.add_all([
            _op(shop, cust, op_type="purchase", when=_tehran_noon(dt.date(2026, 7, 2)), plan=plan),
            _sale(shop, cust, -150_000, _tehran_noon(dt.date(2026, 7, 2))),
        ])
        await s.flush()

        statements: list[str] = []

        def record(_conn, _cursor, statement, _params, _context, _many):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await storefront_reporting.finance(s, access)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

        # Two scans + the price per GB + the two free-size settings.
        assert len(statements) <= 6
        assert not any(stmt.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                       for stmt in statements)

    _run(body)
