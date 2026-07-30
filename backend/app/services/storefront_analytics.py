"""Fleet-wide storefront-bot analytics for the OWNER panel.

`storefront_reporting` answers «how is MY shop doing?» for one reseller inside the portal. This
module answers the owner's question — «how are ALL the shops doing?» — over every storefront bot at
once: how many are actually able to sell right now, what they sold today / this month, who their
customers are, which services are about to lapse, and which shops need attention.

Design notes:
  * Money is read from the reseller↔customer wallet ledger (`storefront_wallet_txns`), the same
    source the portal dashboard uses, so an owner figure and a reseller figure never disagree.
    Purchases are stored as NEGATIVE `done` rows; refunds/reversals are positive rows subtracted on
    the day they happen.
  * The purchase/renewal split and the best-selling plan shapes come from `storefront_operations`
    (`price_toman` charged at the time), never from a plan's CURRENT price — plans are edited in
    place, so joining today's price to historic sales would quietly restate past revenue.
  * Day boundaries are Tehran-local, so daily buckets are built in Python from a `created_at` scan
    (SQLite and Postgres truncate timestamps differently) — the precedent set by the portal.
  * Everything is aggregated in SQL except the expiry/usage pass, which needs the same day-math as
    the reminder job and is bounded by the number of ACTIVE storefront services.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCreditCode,
    StorefrontCreditRedemption,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.models.enums import PanelStatus
from app.schemas.storefront_analytics import (
    BotsOut,
    CreditsOut,
    CustomersOut,
    DailyPointOut,
    MethodRowOut,
    OperationsOut,
    PlanShapeOut,
    SalesWindowOut,
    ServicesOut,
    ShopRowOut,
    StorefrontAnalyticsOut,
    TopupsOut,
    TrialConversionOut,
)
from app.services.periods import Period, month_period
from app.services.reseller_stats import load_billable_roots
from app.services.storefront_reporting import classify_health_error, tehran_bounds

TEHRAN = ZoneInfo("Asia/Tehran")

# A service is "active" while it is provisioned or mid-renewal — `renewing` is an in-flight
# service, never a gone one (same rule the portal read models use).
ACTIVE_SERVICE_STATES = ("provisioned", "renewing")
SERVICE_STATES = ("pending", "provisioned", "renewing", "disabled", "failed", "deleted")
OPERATION_STATES = ("pending", "in_progress", "done", "failed", "reversed")
SALE_KINDS = ("purchase", "refund", "renew_reversal")
# ≥ this share of the sold quota consumed → the customer is about to run out of volume.
HIGH_USAGE_RATIO = 0.8


def _int(value) -> int:
    return int(value or 0)


def _aware(ts: dt.datetime | None) -> dt.datetime | None:
    """SQLite loses the tzinfo; treat a naive stamp as UTC (how timestamps are stored)."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts


# ── sales windows ───────────────────────────────────────────────────────────────────────────
async def _sales_windows(
    session: AsyncSession, windows: Mapping[str, tuple[dt.datetime, dt.datetime]]
) -> dict[str, SalesWindowOut]:
    """Every requested window in ONE ledger scan (a CASE column per window), fleet-wide.

    The scan is bounded by the union of the windows, so asking for «today» and «this month» never
    walks the whole ledger. Mirrors `storefront_reporting._sales_periods`, minus the tenant filter.
    """
    columns = []
    for prefix, (start, end) in windows.items():
        window = and_(
            StorefrontWalletTxn.created_at >= start, StorefrontWalletTxn.created_at < end
        )
        purchase = and_(
            window, StorefrontWalletTxn.kind == "purchase", StorefrontWalletTxn.amount_toman < 0
        )
        reversal = and_(
            window,
            StorefrontWalletTxn.kind.in_(("refund", "renew_reversal")),
            StorefrontWalletTxn.amount_toman > 0,
        )
        columns.extend([
            func.coalesce(
                func.sum(case((purchase, -StorefrontWalletTxn.amount_toman), else_=0)), 0
            ).label(f"{prefix}_gross"),
            func.coalesce(
                func.sum(case((reversal, StorefrontWalletTxn.amount_toman), else_=0)), 0
            ).label(f"{prefix}_reversals"),
            func.sum(case((purchase, 1), else_=0)).label(f"{prefix}_orders"),
        ])
        for bucket in ("purchase", "renewal"):
            hit = and_(purchase, StorefrontOperation.op_type == bucket)
            columns.extend([
                func.sum(case((hit, 1), else_=0)).label(f"{prefix}_{bucket}_count"),
                func.coalesce(
                    func.sum(case((hit, -StorefrontWalletTxn.amount_toman), else_=0)), 0
                ).label(f"{prefix}_{bucket}_amount"),
            ])
        unknown = and_(
            purchase,
            or_(
                StorefrontOperation.id.is_(None),
                StorefrontOperation.op_type.not_in(("purchase", "renewal")),
            ),
        )
        columns.extend([
            func.sum(case((unknown, 1), else_=0)).label(f"{prefix}_unknown_count"),
            func.coalesce(
                func.sum(case((unknown, -StorefrontWalletTxn.amount_toman), else_=0)), 0
            ).label(f"{prefix}_unknown_amount"),
        ])

    earliest = min(start for start, _end in windows.values())
    latest = max(end for _start, end in windows.values())
    row = (await session.execute(
        select(*columns)
        .select_from(StorefrontWalletTxn)
        .outerjoin(
            StorefrontOperation, StorefrontOperation.id == StorefrontWalletTxn.operation_id
        )
        .where(
            StorefrontWalletTxn.status == "done",
            StorefrontWalletTxn.kind.in_(SALE_KINDS),
            StorefrontWalletTxn.created_at >= earliest,
            StorefrontWalletTxn.created_at < latest,
        )
    )).mappings().one()

    out: dict[str, SalesWindowOut] = {}
    for prefix in windows:
        gross = _int(row[f"{prefix}_gross"])
        reversals = _int(row[f"{prefix}_reversals"])
        out[prefix] = SalesWindowOut(
            gross_toman=gross,
            reversals_toman=reversals,
            net_toman=gross - reversals,
            orders=_int(row[f"{prefix}_orders"]),
            purchase_count=_int(row[f"{prefix}_purchase_count"]),
            purchase_toman=_int(row[f"{prefix}_purchase_amount"]),
            renewal_count=_int(row[f"{prefix}_renewal_count"]),
            renewal_toman=_int(row[f"{prefix}_renewal_amount"]),
            unknown_count=_int(row[f"{prefix}_unknown_count"]),
            unknown_toman=_int(row[f"{prefix}_unknown_amount"]),
        )
    return out


async def _sales_by_shop(
    session: AsyncSession, start: dt.datetime, end: dt.datetime
) -> dict[int, tuple[int, int]]:
    """(net_toman, orders) per shop for one window — the per-shop table's money column."""
    rows = (await session.execute(
        select(
            StorefrontWalletTxn.storefront_bot_id,
            func.coalesce(func.sum(-StorefrontWalletTxn.amount_toman), 0),
            func.sum(case((StorefrontWalletTxn.kind == "purchase", 1), else_=0)),
        )
        .where(
            StorefrontWalletTxn.status == "done",
            StorefrontWalletTxn.kind.in_(SALE_KINDS),
            StorefrontWalletTxn.created_at >= start,
            StorefrontWalletTxn.created_at < end,
        )
        .group_by(StorefrontWalletTxn.storefront_bot_id)
    )).all()
    # A purchase is negative and a refund positive, so `sum(-amount)` IS the net for the window.
    return {shop_id: (_int(net), _int(orders)) for shop_id, net, orders in rows}


async def _daily(
    session: AsyncSession, day_from: dt.date, day_to: dt.date
) -> list[DailyPointOut]:
    """Net sales, paid orders, confirmed top-ups and new customers per Tehran day, zero-filled."""
    start, end = tehran_bounds(day_from, day_to)
    ledger = (await session.execute(
        select(
            StorefrontWalletTxn.created_at,
            StorefrontWalletTxn.kind,
            StorefrontWalletTxn.status,
            StorefrontWalletTxn.amount_toman,
        ).where(
            StorefrontWalletTxn.created_at >= start,
            StorefrontWalletTxn.created_at < end,
            or_(
                and_(
                    StorefrontWalletTxn.kind.in_(SALE_KINDS),
                    StorefrontWalletTxn.status == "done",
                ),
                and_(
                    StorefrontWalletTxn.kind == "topup",
                    StorefrontWalletTxn.status == "confirmed",
                ),
            ),
        )
    )).all()
    joined = (await session.execute(
        select(StorefrontCustomer.created_at).where(
            StorefrontCustomer.created_at >= start, StorefrontCustomer.created_at < end
        )
    )).scalars().all()

    net: dict[dt.date, int] = defaultdict(int)
    orders: dict[dt.date, int] = defaultdict(int)
    topups: dict[dt.date, int] = defaultdict(int)
    newcomers: dict[dt.date, int] = defaultdict(int)

    def _day(ts: dt.datetime) -> dt.date:
        return _aware(ts).astimezone(TEHRAN).date()  # type: ignore[union-attr]

    for created_at, kind, _status, amount in ledger:
        day = _day(created_at)
        value = _int(amount)
        if kind == "topup":
            topups[day] += value
        elif kind == "purchase" and value < 0:
            net[day] += -value
            orders[day] += 1
        elif value > 0:                      # refund / renew_reversal
            net[day] -= value
    for created_at in joined:
        newcomers[_day(created_at)] += 1

    out: list[DailyPointOut] = []
    cursor = day_from
    while cursor <= day_to:
        out.append(DailyPointOut(
            date=cursor, day=cursor.day,
            net_toman=net.get(cursor, 0), orders=orders.get(cursor, 0),
            new_customers=newcomers.get(cursor, 0), topups_toman=topups.get(cursor, 0),
        ))
        cursor += dt.timedelta(days=1)
    return out


async def _top_plan_shapes(
    session: AsyncSession, start: dt.datetime, end: dt.datetime, limit: int = 10
) -> list[PlanShapeOut]:
    """Best-selling plan SHAPES (GB × days) fleet-wide — plan rows are per-shop, so the shops can
    only be compared at the shape level. Revenue is what was actually charged."""
    rows = (await session.execute(
        select(
            StorefrontOperation.gb,
            StorefrontOperation.days,
            func.count(StorefrontOperation.id),
            func.coalesce(func.sum(StorefrontOperation.price_toman), 0),
        )
        .where(
            StorefrontOperation.status == "done",
            StorefrontOperation.op_type.in_(("purchase", "renewal")),
            StorefrontOperation.price_toman > 0,
            StorefrontOperation.created_at >= start,
            StorefrontOperation.created_at < end,
        )
        .group_by(StorefrontOperation.gb, StorefrontOperation.days)
    )).all()
    out = [
        PlanShapeOut(gb=_int(gb), days=_int(days), orders=_int(count), amount_toman=_int(amount))
        for gb, days, count, amount in rows
    ]
    out.sort(key=lambda p: (p.amount_toman, p.orders), reverse=True)
    return out[:limit]


# ── the expiry / usage pass ─────────────────────────────────────────────────────────────────
def _days_left(
    days: int | None,
    last_renewed_at: dt.datetime | None,
    created_at: dt.datetime | None,
    start_date: dt.date | None,
    package_days: int | None,
    today: dt.date,
) -> int | None:
    """Days until the config expires (negative = already expired), or None when undecidable.

    Same rule as `storefront_expiry._days_left`: the PANEL's own start_date + package_days wins,
    falling back to the order's own duration measured from its last renewal (Tehran-local, never a
    raw UTC `.date()` — that expired 00:00–03:29 orders a day early)."""
    if start_date is not None and (package_days or 0) > 0:
        return (start_date + dt.timedelta(days=int(package_days or 0)) - today).days
    if (days or 0) > 0:
        anchor = last_renewed_at or created_at
        if anchor is not None:
            return (_aware(anchor).astimezone(TEHRAN).date()  # type: ignore[union-attr]
                    + dt.timedelta(days=int(days or 0)) - today).days
    return None


async def _service_health(
    session: AsyncSession, today: dt.date
) -> tuple[dict[str, int | float], dict[int, int]]:
    """Expiry + quota-exhaustion counts over every ACTIVE service, plus per-shop «expiring ≤3d».

    Bounded by the number of live storefront services; only the six columns the day-math needs are
    loaded (never whole ORM rows). The snapshot join is a plain (panel_id, uuid) match — the same
    non-FK reference the order model documents — so a pruned snapshot degrades to the order's own
    duration instead of dropping the service from the count.
    """
    rows = (await session.execute(
        select(
            StorefrontCustomer.storefront_bot_id,
            StorefrontOrder.days,
            StorefrontOrder.last_renewed_at,
            StorefrontOrder.created_at,
            EndUserSnapshot.start_date,
            EndUserSnapshot.package_days,
            EndUserSnapshot.usage_limit_gb,
            EndUserSnapshot.current_usage_gb,
        )
        .select_from(StorefrontOrder)
        .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
        .outerjoin(
            EndUserSnapshot,
            and_(
                EndUserSnapshot.panel_id == StorefrontOrder.panel_id,
                EndUserSnapshot.user_uuid == StorefrontOrder.panel_user_uuid,
            ),
        )
        .where(StorefrontOrder.status.in_(ACTIVE_SERVICE_STATES))
    )).all()

    totals: dict[str, int | float] = {
        "expiring_3d": 0, "expiring_7d": 0, "expired": 0,
        "high_usage": 0, "quota_gb": 0.0, "used_gb": 0.0,
    }
    per_shop: dict[int, int] = defaultdict(int)
    for shop_id, days, renewed, created, start_date, package_days, limit_gb, used_gb in rows:
        left = _days_left(days, renewed, created, start_date, package_days, today)
        if left is not None:
            if left < 0:
                totals["expired"] += 1
            else:
                if left <= 7:
                    totals["expiring_7d"] += 1
                if left <= 3:
                    totals["expiring_3d"] += 1
                    per_shop[shop_id] += 1
        limit_f, used_f = float(limit_gb or 0), float(used_gb or 0)
        totals["quota_gb"] += limit_f
        totals["used_gb"] += used_f
        if limit_f > 0 and used_f / limit_f >= HIGH_USAGE_RATIO:
            totals["high_usage"] += 1
    totals["quota_gb"] = round(float(totals["quota_gb"]), 2)
    totals["used_gb"] = round(float(totals["used_gb"]), 2)
    return totals, dict(per_shop)


# ── the report ──────────────────────────────────────────────────────────────────────────────
async def analytics(
    session: AsyncSession, period: Period, *, now: dt.datetime | None = None
) -> StorefrontAnalyticsOut:
    """Fleet-wide storefront analytics for `period` (a Gregorian billing month).

    «Live» figures (today, last 7/30 days, pending queues, service health) are always measured
    against the current clock; everything labelled «this period» follows the picker.
    """
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(TEHRAN)
    today = current.date()
    now_utc = current.astimezone(dt.timezone.utc)
    previous = month_period(
        (period.start - dt.timedelta(days=1)).year, (period.start - dt.timedelta(days=1)).month
    )
    period_start, period_end = tehran_bounds(period.start, period.end)
    today_start, today_end = tehran_bounds(today, today)

    # Two scans instead of one: a single union window would stretch from the selected month all
    # the way to today, so picking an old month would walk every ledger row in between.
    live = await _sales_windows(session, {
        "today": (today_start, today_end),
        "yesterday": tehran_bounds(today - dt.timedelta(days=1), today - dt.timedelta(days=1)),
        "d7": tehran_bounds(today - dt.timedelta(days=6), today),
        "d30": tehran_bounds(today - dt.timedelta(days=29), today),
    })
    ranged = await _sales_windows(session, {
        "period": (period_start, period_end),
        "prev": tehran_bounds(previous.start, previous.end),
    })

    # ── shops ──
    shop_rows = (await session.execute(
        select(StorefrontBot, Reseller, Panel)
        .join(Reseller, Reseller.id == StorefrontBot.reseller_id)
        .join(Panel, Panel.id == StorefrontBot.panel_id)
    )).all()
    plan_counts: dict[int, int] = {
        shop_id: _int(count)
        for shop_id, count in (await session.execute(
            select(StorefrontPlan.storefront_bot_id, func.count(StorefrontPlan.id))
            .where(StorefrontPlan.enabled.is_(True))
            .group_by(StorefrontPlan.storefront_bot_id)
        )).all()
    }
    # The addressable market: TOP-LEVEL resellers allowed a shop (a sub-reseller can never set one
    # up). Counted through `reseller_stats`, the shared root definition, so this figure agrees with
    # the «نمایندهٔ اصلی» count everywhere else instead of re-deriving "top-level" here.
    eligible = sum(
        1 for r in await load_billable_roots(session) if r.storefront_enabled
    )

    tally: dict[str, int] = defaultdict(int)
    for bot, _reseller, panel in shop_rows:
        tally["enabled" if bot.enabled else "disabled"] += 1
        if bot.status == "active":
            tally["active"] += 1
        elif bot.status == "errored":
            tally["errored"] += 1
        if bot.shop_closed:
            tally["closed"] += 1
        if bot.enabled and bot.status == "active" and not bot.shop_closed:
            tally["selling"] += 1
        if not plan_counts.get(bot.id):
            tally["without_plans"] += 1
        if bot.free_trial_enabled:
            tally["trial_enabled"] += 1
        if bot.channel_required:
            tally["channel_locked"] += 1
        if not panel.enabled or panel.status != PanelStatus.ok:
            tally["panel_unhealthy"] += 1
        created = _aware(bot.created_at)
        if created is not None and period_start <= created < period_end:
            tally["new_in_period"] += 1

    bots = BotsOut(
        total=len(shop_rows),
        enabled=tally["enabled"],
        disabled=tally["disabled"],
        active=tally["active"],
        errored=tally["errored"],
        closed=tally["closed"],
        selling=tally["selling"],
        without_plans=tally["without_plans"],
        trial_enabled=tally["trial_enabled"],
        channel_locked=tally["channel_locked"],
        panel_unhealthy=tally["panel_unhealthy"],
        new_in_period=tally["new_in_period"],
        eligible_resellers=eligible,
    )

    # ── customers (fleet totals + the per-shop columns, one grouped scan) ──
    cutoff_7 = now_utc - dt.timedelta(days=7)
    cutoff_30 = now_utc - dt.timedelta(days=30)
    customer_rows = (await session.execute(
        select(
            StorefrontCustomer.storefront_bot_id,
            func.count(StorefrontCustomer.id),
            func.sum(case((StorefrontCustomer.last_seen_at >= cutoff_7, 1), else_=0)),
            func.sum(case((StorefrontCustomer.last_seen_at >= cutoff_30, 1), else_=0)),
            func.sum(case((StorefrontCustomer.banned.is_(True), 1), else_=0)),
            func.coalesce(func.sum(StorefrontCustomer.wallet_balance_toman), 0),
            func.sum(case((
                and_(
                    StorefrontCustomer.created_at >= period_start,
                    StorefrontCustomer.created_at < period_end,
                ), 1), else_=0)),
            func.sum(case((StorefrontCustomer.created_at >= today_start, 1), else_=0)),
        ).group_by(StorefrontCustomer.storefront_bot_id)
    )).all()

    # Buyers in the period: distinct customers with ≥1 paid order, and how many bought more
    # than once (the repeat rate is the single best signal that a shop's customers stick).
    buyer_rows = (await session.execute(
        select(StorefrontWalletTxn.customer_id, func.count(StorefrontWalletTxn.id))
        .where(
            StorefrontWalletTxn.kind == "purchase",
            StorefrontWalletTxn.status == "done",
            StorefrontWalletTxn.amount_toman < 0,
            StorefrontWalletTxn.created_at >= period_start,
            StorefrontWalletTxn.created_at < period_end,
        )
        .group_by(StorefrontWalletTxn.customer_id)
    )).all()
    buyers = len(buyer_rows)
    repeat_buyers = sum(1 for _cid, n in buyer_rows if _int(n) > 1)

    period_sales = ranged["period"]
    customers = CustomersOut(
        total=sum(_int(r[1]) for r in customer_rows),
        new_today=sum(_int(r[7]) for r in customer_rows),
        new_in_period=sum(_int(r[6]) for r in customer_rows),
        active_7d=sum(_int(r[2]) for r in customer_rows),
        active_30d=sum(_int(r[3]) for r in customer_rows),
        banned=sum(_int(r[4]) for r in customer_rows),
        buyers_in_period=buyers,
        repeat_buyers_in_period=repeat_buyers,
        wallet_liability_toman=sum(_int(r[5]) for r in customer_rows),
        avg_order_toman=(
            round(period_sales.gross_toman / period_sales.orders) if period_sales.orders else 0
        ),
        arppu_toman=round(period_sales.net_toman / buyers) if buyers else 0,
    )

    # ── services ──
    order_rows = (await session.execute(
        select(
            StorefrontCustomer.storefront_bot_id,
            StorefrontOrder.status,
            StorefrontOrder.is_trial,
            func.count(StorefrontOrder.id),
        )
        .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
        .group_by(StorefrontCustomer.storefront_bot_id, StorefrontOrder.status,
                  StorefrontOrder.is_trial)
    )).all()
    by_state: dict[str, int] = {state: 0 for state in SERVICE_STATES}
    active_by_shop: dict[int, int] = defaultdict(int)
    trials_active = 0
    for shop_id, state, is_trial, count in order_rows:
        n = _int(count)
        if state in by_state:
            by_state[state] += n
        if state in ACTIVE_SERVICE_STATES:
            active_by_shop[shop_id] += n
            if is_trial:
                trials_active += n
    trials_in_period = _int((await session.execute(
        select(func.count(StorefrontOrder.id)).where(
            StorefrontOrder.is_trial.is_(True),
            StorefrontOrder.created_at >= period_start,
            StorefrontOrder.created_at < period_end,
        )
    )).scalar_one())
    autorenew_armed = _int((await session.execute(
        select(func.count(StorefrontOrder.id)).where(
            StorefrontOrder.autorenew_armed_at.is_not(None),
            StorefrontOrder.status.in_(ACTIVE_SERVICE_STATES),
        )
    )).scalar_one())
    health, expiring_by_shop = await _service_health(session, today)
    services = ServicesOut(
        total=sum(by_state.values()),
        **{state: by_state[state] for state in SERVICE_STATES},
        active=by_state["provisioned"] + by_state["renewing"],
        trials_active=trials_active,
        trials_in_period=trials_in_period,
        expiring_3d=int(health["expiring_3d"]),
        expiring_7d=int(health["expiring_7d"]),
        expired=int(health["expired"]),
        high_usage=int(health["high_usage"]),
        quota_gb=float(health["quota_gb"]),
        used_gb=float(health["used_gb"]),
        autorenew_armed=autorenew_armed,
    )

    # ── top-ups ──
    pending_rows = (await session.execute(
        select(
            StorefrontWalletTxn.storefront_bot_id,
            func.count(StorefrontWalletTxn.id),
            func.coalesce(func.sum(StorefrontWalletTxn.amount_toman), 0),
        )
        .where(StorefrontWalletTxn.kind == "topup", StorefrontWalletTxn.status == "pending")
        .group_by(StorefrontWalletTxn.storefront_bot_id)
    )).all()
    pending_by_shop = {sid: (_int(c), _int(a)) for sid, c, a in pending_rows}
    method_rows = (await session.execute(
        select(
            StorefrontWalletTxn.method,
            StorefrontWalletTxn.status,
            func.count(StorefrontWalletTxn.id),
            func.coalesce(func.sum(StorefrontWalletTxn.amount_toman), 0),
        )
        .where(
            StorefrontWalletTxn.kind == "topup",
            StorefrontWalletTxn.created_at >= period_start,
            StorefrontWalletTxn.created_at < period_end,
        )
        .group_by(StorefrontWalletTxn.method, StorefrontWalletTxn.status)
    )).all()
    by_method: dict[str, MethodRowOut] = {}
    confirmed_count = confirmed_amount = rejected_count = 0
    for method, status, count, amount in method_rows:
        if status == "rejected":
            rejected_count += _int(count)
            continue
        if status != "confirmed":
            continue
        confirmed_count += _int(count)
        confirmed_amount += _int(amount)
        key = method or "unknown"
        row = by_method.get(key) or MethodRowOut(method=key, count=0, amount_toman=0)
        by_method[key] = MethodRowOut(
            method=key, count=row.count + _int(count), amount_toman=row.amount_toman + _int(amount)
        )
    topups = TopupsOut(
        pending_count=sum(c for c, _a in pending_by_shop.values()),
        pending_toman=sum(a for _c, a in pending_by_shop.values()),
        confirmed_count=confirmed_count,
        confirmed_toman=confirmed_amount,
        rejected_count=rejected_count,
        by_method=sorted(by_method.values(), key=lambda m: m.amount_toman, reverse=True),
    )

    # ── credit codes ──
    credit_row = (await session.execute(
        select(
            func.count(StorefrontCreditRedemption.id),
            func.coalesce(func.sum(StorefrontCreditRedemption.bonus_toman), 0),
        ).where(
            StorefrontCreditRedemption.created_at >= period_start,
            StorefrontCreditRedemption.created_at < period_end,
        )
    )).one()
    active_codes = _int((await session.execute(
        select(func.count(StorefrontCreditCode.id)).where(
            StorefrontCreditCode.enabled.is_(True), StorefrontCreditCode.archived_at.is_(None)
        )
    )).scalar_one())
    credits = CreditsOut(
        redemptions=_int(credit_row[0]), bonus_toman=_int(credit_row[1]),
        active_codes=active_codes,
    )

    # ── operations (provisioning/renewal health) ──
    op_rows = (await session.execute(
        select(StorefrontOperation.status, func.count(StorefrontOperation.id))
        .group_by(StorefrontOperation.status)
    )).all()
    op_counts = {state: 0 for state in OPERATION_STATES}
    for state, count in op_rows:
        if state in op_counts:
            op_counts[state] = _int(count)
    failed_24h = _int((await session.execute(
        select(func.count(StorefrontOperation.id)).where(
            StorefrontOperation.status == "failed",
            StorefrontOperation.created_at >= now_utc - dt.timedelta(days=1),
        )
    )).scalar_one())
    operations = OperationsOut(**op_counts, failed_24h=failed_24h)

    # ── trial → paid conversion (fleet-wide) ──
    # A trial config is free, so it never writes a purchase row: «converted» = a customer who
    # claimed a trial and later paid for anything at all.
    trial_customers_sq = (
        select(StorefrontOrder.customer_id.label("customer_id"))
        .where(
            StorefrontOrder.is_trial.is_(True),
            StorefrontOrder.status.in_(("provisioned", "renewing", "disabled", "deleted")),
        )
        .group_by(StorefrontOrder.customer_id)
        .subquery()
    )
    paid = exists(
        select(StorefrontWalletTxn.id).where(
            StorefrontWalletTxn.customer_id == trial_customers_sq.c.customer_id,
            StorefrontWalletTxn.kind == "purchase",
            StorefrontWalletTxn.status == "done",
            StorefrontWalletTxn.amount_toman < 0,
        )
    )
    trial_row = (await session.execute(
        select(func.count(), func.sum(case((paid, 1), else_=0))).select_from(trial_customers_sq)
    )).one()
    trial_total, trial_converted = _int(trial_row[0]), _int(trial_row[1])
    trial = TrialConversionOut(
        trial_customers=trial_total,
        converted_customers=trial_converted,
        rate=round(trial_converted / trial_total, 4) if trial_total else None,
    )

    # ── per-shop table ──
    period_by_shop = await _sales_by_shop(session, period_start, period_end)
    today_by_shop = await _sales_by_shop(session, today_start, today_end)
    last_sale: dict[int, dt.datetime | None] = {
        shop_id: seen
        for shop_id, seen in (await session.execute(
            select(
                StorefrontWalletTxn.storefront_bot_id, func.max(StorefrontWalletTxn.created_at)
            )
            .where(StorefrontWalletTxn.kind == "purchase", StorefrontWalletTxn.status == "done")
            .group_by(StorefrontWalletTxn.storefront_bot_id)
        )).all()
    }
    customer_by_shop = {r[0]: r for r in customer_rows}

    shops: list[ShopRowOut] = []
    for bot, reseller, panel in shop_rows:
        crow = customer_by_shop.get(bot.id)
        net, orders = period_by_shop.get(bot.id, (0, 0))
        pending_count, pending_amount = pending_by_shop.get(bot.id, (0, 0))
        shops.append(ShopRowOut(
            shop_id=bot.id,
            reseller_id=reseller.id,
            reseller_name=reseller.name,
            panel_key=panel.key,
            bot_username=bot.bot_username,
            enabled=bot.enabled,
            status=bot.status,
            shop_closed=bot.shop_closed,
            health_error_class=classify_health_error(bot.last_error),
            plans=_int(plan_counts.get(bot.id)),
            customers=_int(crow[1]) if crow else 0,
            new_customers=_int(crow[6]) if crow else 0,
            active_customers_30d=_int(crow[3]) if crow else 0,
            services_active=active_by_shop.get(bot.id, 0),
            expiring_3d=expiring_by_shop.get(bot.id, 0),
            net_sales_toman=net,
            orders=orders,
            today_net_toman=today_by_shop.get(bot.id, (0, 0))[0],
            wallet_liability_toman=_int(crow[5]) if crow else 0,
            pending_topups_count=pending_count,
            pending_topups_toman=pending_amount,
            last_sale_at=_aware(last_sale.get(bot.id)),
            created_at=_aware(bot.created_at),
        ))
    shops.sort(key=lambda s: (s.net_sales_toman, s.customers), reverse=True)

    return StorefrontAnalyticsOut(
        period=period.label,
        period_start=period.start,
        period_end=period.end,
        previous_period=previous.label,
        generated_at=now_utc,
        bots=bots,
        customers=customers,
        services=services,
        topups=topups,
        credits=credits,
        operations=operations,
        trial=trial,
        sales_today=live["today"],
        sales_yesterday=live["yesterday"],
        sales_7d=live["d7"],
        sales_30d=live["d30"],
        sales_period=period_sales,
        sales_previous_period=ranged["prev"],
        daily=await _daily(session, period.start, period.end),
        top_plans=await _top_plan_shapes(session, period_start, period_end),
        shops=shops,
    )
