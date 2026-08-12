"""Board metric assembly: who is eligible, how subtrees roll up, and where the numbers come from.

The expensive part of the follow-up board is `load_board_metrics`, which replaces ~400
per-reseller round-trips with a fixed set of grouped queries. These tests pin the parts that
are easy to get wrong in that shape: panel scoping of a shared uuid, the presence filter,
subtree roll-up, and the fact that a month with no invoice row means ZERO — not "no data".
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/crmboard.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Invoice,
    Panel,
    Reseller,
    StorefrontOrder,
)
from app.models.enums import EnforcementState, InvoiceStatus, PanelStatus  # noqa: E402
from app.services import crm  # noqa: E402

TODAY = dt.date(2026, 8, 20)          # well past MIN_ELAPSED_DAYS_FOR_TREND
NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.timezone.utc)
LONG_AGO = NOW - dt.timedelta(days=400)


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crm.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                crm.invalidate_metrics_cache()
                await body(s)
        finally:
            crm.invalidate_metrics_cache()
            await engine.dispose()
    asyncio.run(go())


async def _panel(s, key, owner_uuid="owner"):
    p = Panel(key=key, host=f"{key}.invalid", proxy_path_enc="x", owner_uuid=owner_uuid,
              status=PanelStatus.ok, last_synced_at=NOW, enabled=True)
    s.add(p)
    await s.flush()
    s.add(Reseller(panel_id=p.id, admin_uuid=owner_uuid, name="Owner", is_owner=True,
                   last_seen_at=NOW, created_at=LONG_AGO))
    await s.flush()
    return p


async def _reseller(s, panel, uuid, *, name=None, parent=None, created=LONG_AGO, **kw):
    r = Reseller(panel_id=panel.id, admin_uuid=uuid, name=name or uuid,
                 parent_admin_uuid=parent, last_seen_at=NOW, created_at=created, **kw)
    s.add(r)
    await s.flush()
    return r


def _user(panel, added_by, uuid, gb, start):
    return EndUserSnapshot(panel_id=panel.id, user_uuid=uuid, added_by_uuid=added_by,
                           usage_limit_gb=gb, start_date=start, last_synced_at=NOW)


def _by_name(metrics):
    return {m.name: m for m in metrics}


# ---------------------------------------------------------------- eligibility
def test_only_present_billable_top_level_resellers_are_on_the_board(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        await _reseller(s, p, "root", name="Root")
        await _reseller(s, p, "sub", name="Sub", parent="root")          # not top-level
        await _reseller(s, p, "exempt", name="Exempt", exclude_from_billing=True)
        gone = await _reseller(s, p, "gone", name="Gone")
        gone.last_seen_at = NOW - dt.timedelta(days=5)                    # removed from panel
        await s.commit()

        metrics = await crm.load_board_metrics(s, today_=TODAY)
        assert {m.name for m in metrics} == {"Root"}
        # The sub-reseller is still counted INSIDE its root, just not as its own row.
        assert _by_name(metrics)["Root"].sub_reseller_count == 1
    _run(body, tmp_path)


def test_the_same_uuid_on_two_panels_stays_two_resellers(tmp_path):
    """`parent_admin_uuid` is only unique within a panel. A children map keyed on the bare uuid
    would merge two unrelated businesses into one row."""
    async def body(s):
        p1 = await _panel(s, "p1", owner_uuid="o1")
        p2 = await _panel(s, "p2", owner_uuid="o2")
        await _reseller(s, p1, "shared", name="OnP1")
        await _reseller(s, p2, "shared", name="OnP2")
        await _reseller(s, p2, "child", name="ChildOnP2", parent="shared")
        s.add_all([
            _user(p1, "shared", "u1", 50, dt.date(2026, 8, 5)),
            _user(p2, "shared", "u2", 10, dt.date(2026, 8, 5)),
            _user(p2, "child", "u3", 20, dt.date(2026, 8, 5)),
        ])
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))
        assert set(m) == {"OnP1", "OnP2"}
        assert m["OnP1"].sub_reseller_count == 0 and m["OnP1"].mtd_gb == 50
        # Only panel 2's child rolls into panel 2's root.
        assert m["OnP2"].sub_reseller_count == 1 and m["OnP2"].mtd_gb == 30
    _run(body, tmp_path)


def test_subtree_rolls_up_to_the_root_at_any_depth(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        await _reseller(s, p, "root", name="Root")
        await _reseller(s, p, "a", name="A", parent="root")
        await _reseller(s, p, "b", name="B", parent="a")                  # depth 2
        # A self-parenting row is its own parent, so it is not a root — same rule as
        # `reseller_stats.top_level_roots`, and the traversal's `seen` set stops it hanging.
        await _reseller(s, p, "loop", name="Loop", parent="loop")
        s.add_all([
            _user(p, "root", "u1", 10, dt.date(2026, 8, 2)),
            _user(p, "a", "u2", 20, dt.date(2026, 8, 3)),
            _user(p, "B", "u3", 30, dt.date(2026, 8, 4)),                 # uuid case differs
        ])
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))
        assert set(m) == {"Root"}
        assert m["Root"].mtd_services == 3 and m["Root"].mtd_gb == 60
        assert m["Root"].sub_reseller_count == 2
    _run(body, tmp_path)


# ---------------------------------------------------------------- what counts as a sale
def test_free_and_trial_configs_are_not_sales(tmp_path):
    """A shop that only hands out free trials must not read as a healthy seller, and a config
    at or under the free threshold is a test config, not revenue."""
    async def body(s):
        p = await _panel(s, "p1")
        await _reseller(s, p, "root", name="Root")
        s.add_all([
            _user(p, "root", "free", 1, dt.date(2026, 8, 2)),      # == free threshold
            _user(p, "root", "trial", 30, dt.date(2026, 8, 3)),    # storefront giveaway
            _user(p, "root", "real", 50, dt.date(2026, 8, 4)),
        ])
        s.add(StorefrontOrder(customer_id=1, panel_id=p.id,
                              panel_user_uuid="trial", is_trial=True))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))["Root"]
        assert m.mtd_services == 1 and m.mtd_gb == 50
    _run(body, tmp_path)


def test_a_reseller_that_never_sold_is_flagged_but_a_new_one_is_not(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        await _reseller(s, p, "old", name="OldEmpty")
        await _reseller(s, p, "new", name="NewEmpty", created=NOW - dt.timedelta(days=2))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))
        t = crm.Thresholds()
        assert m["OldEmpty"].ever_sold is False
        assert crm.classify(m["OldEmpty"], t, elapsed_days=20) == "never_active"
        assert crm.classify(m["NewEmpty"], t, elapsed_days=20) == "onboarding"
    _run(body, tmp_path)


# ---------------------------------------------------------------- history
def test_a_month_without_an_invoice_counts_as_zero_not_as_missing(tmp_path):
    """Invoicing never persists a zero bundle, so "no row" IS the zero. If the 3-month mean were
    taken over only the months that exist, a reseller who sold nothing for two months would
    average out to their one good month and read as healthy."""
    async def body(s):
        p = await _panel(s, "p1")
        root = await _reseller(s, p, "root", name="Root")
        # 120 GB in May; nothing at all in June or July.
        s.add(Invoice(reseller_id=root.id, panel_id=p.id, period_start=dt.date(2026, 5, 1),
                      period_end=dt.date(2026, 5, 31), period_label="2026-05", usage_gb=120,
                      users_count=4, price_per_gb=2000, amount_toman=240_000,
                      status=InvoiceStatus.paid))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))["Root"]
        labels = [x["label"] for x in m.months]
        assert len(labels) == crm.HISTORY_MONTHS and labels[-1] == "2026-08"
        assert dict(zip(labels, [x["gb"] for x in m.months]))["2026-06"] == 0.0
        # mean over May..July = 120/3 = 40, not 120.
        assert m.avg_prev_gb == 40.0
        # No sale since May → churned, even though the snapshots for those users are long gone.
        assert m.last_sale_date == dt.date(2026, 5, 31)
        assert crm.classify(m, crm.Thresholds(), elapsed_days=20) == "churned"
    _run(body, tmp_path)


def test_draft_invoices_do_not_count_as_sales(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        root = await _reseller(s, p, "root", name="Root")
        s.add(Invoice(reseller_id=root.id, panel_id=p.id, period_start=dt.date(2026, 7, 1),
                      period_end=dt.date(2026, 7, 31), period_label="2026-07", usage_gb=99,
                      users_count=3, price_per_gb=2000, amount_toman=198_000,
                      status=InvoiceStatus.draft))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))["Root"]
        assert m.ever_sold is False and m.avg_prev_gb == 0.0
    _run(body, tmp_path)


def test_value_at_risk_averages_the_last_three_earning_months(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        root = await _reseller(s, p, "root", name="Root")
        for month, amount in ((4, 100_000), (5, 200_000), (6, 300_000), (7, 400_000)):
            s.add(Invoice(reseller_id=root.id, panel_id=p.id,
                          period_start=dt.date(2026, month, 1),
                          period_end=dt.date(2026, month, 28),
                          period_label=f"2026-{month:02d}", usage_gb=10, users_count=1,
                          price_per_gb=2000, amount_toman=amount, status=InvoiceStatus.paid))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))["Root"]
        assert m.value_at_risk_toman == 300_000     # mean of May/June/July, April dropped
    _run(body, tmp_path)


# ---------------------------------------------------------------- debt
def test_a_future_payment_deadline_is_not_currently_due(tmp_path):
    """Granting a deadline must not read as "chase them today" — but the invoice is still debt,
    so the amount is reported either way."""
    async def body(s):
        p = await _panel(s, "p1")
        due = await _reseller(s, p, "due", name="Due")
        deferred = await _reseller(s, p, "def", name="Deferred")
        for r, until in ((due, None), (deferred, dt.date(2026, 9, 30))):
            s.add(Invoice(reseller_id=r.id, panel_id=p.id, period_start=dt.date(2026, 7, 1),
                          period_end=dt.date(2026, 7, 31), period_label="2026-07", usage_gb=10,
                          users_count=1, price_per_gb=2000, amount_toman=20_000,
                          status=InvoiceStatus.sent, deferred_until=until))
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))
        assert m["Due"].has_due_debt is True and m["Due"].outstanding_toman == 20_000
        assert m["Deferred"].has_due_debt is False
        assert m["Deferred"].outstanding_toman == 20_000     # still owed, just not yet chased
        t = crm.Thresholds()
        assert crm.classify(m["Due"], t, elapsed_days=20) == "debtor"
        assert crm.classify(m["Deferred"], t, elapsed_days=20) != "debtor"
    _run(body, tmp_path)


def test_enforcement_state_outranks_everything(tmp_path):
    async def body(s):
        p = await _panel(s, "p1")
        await _reseller(s, p, "sus", name="Suspended",
                        enforcement_state=EnforcementState.enforced)
        await _reseller(s, p, "fro", name="Frozen", enforcement_state=EnforcementState.frozen)
        await s.commit()

        m = _by_name(await crm.load_board_metrics(s, today_=TODAY))
        t = crm.Thresholds()
        assert crm.classify(m["Suspended"], t, elapsed_days=20) == "suspended"
        assert crm.classify(m["Frozen"], t, elapsed_days=20) == "frozen"
    _run(body, tmp_path)
