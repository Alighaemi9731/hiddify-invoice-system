"""Broadcast audience always starts from the base set — top-level resellers in the main list
that are NOT exempt from billing and are present on an active panel — then each filter narrows it.
Sub-resellers, billing-exempt resellers, and the owner are never included."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bcast.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PanelStatus  # noqa: E402
from app.services import broadcast as bc  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bc.db'}")
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
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="own",
                  status=PanelStatus.ok, last_synced_at=NOW)
    s.add(panel)
    await s.flush()

    def res(uuid, name, *, parent="own", chat=None, exempt=False, owner=False):
        return Reseller(panel_id=panel.id, admin_uuid=uuid, name=name,
                        parent_admin_uuid=None if owner else parent, is_owner=owner,
                        bot_chat_id=chat, exclude_from_billing=exempt, last_seen_at=NOW)

    owner = res("own", "Owner", owner=True)
    a = res("a", "RootA", chat=101)
    b = res("b", "RootB", chat=102)            # will have an owed invoice
    c = res("c", "RootC-exempt", chat=103, exempt=True)   # exempt → excluded
    d = res("d", "RootD-noreg", chat=None)     # matched but unregistered
    sub = res("s", "SubOfA", parent="a", chat=104)        # sub-reseller → excluded
    s.add_all([owner, a, b, c, d, sub])
    await s.flush()

    s.add(Invoice(reseller_id=b.id, panel_id=panel.id, period_start=dt.date(2026, 6, 1),
                  period_end=dt.date(2026, 6, 30), period_label="2026-06",
                  usage_gb=5, amount_toman=50_000, status=InvoiceStatus.overdue))

    # active users: RootA = 15 (over), RootB = 3 (few). RootD = 0.
    def users(uuid, n):
        for i in range(n):
            s.add(EndUserSnapshot(panel_id=panel.id, user_uuid=f"{uuid}-{i}",
                                  added_by_uuid=uuid, enable=True, is_active=True))
    users("a", 15)
    users("b", 3)
    await s.commit()
    return a, b, c, d, sub


def test_base_set_excludes_owner_subs_and_exempt(tmp_path):
    async def body(s):
        a, b, c, d, sub = await _seed(s)
        reachable, unreg = await bc.resolve_recipients(s, "all", None, None)
        reach_ids = {r["reseller_id"] for r in reachable}
        unreg_ids = {r["reseller_id"] for r in unreg}
        assert reach_ids == {a.id, b.id}          # registered roots only
        assert unreg_ids == {d.id}                # matched the base set but no bot
        assert c.id not in reach_ids | unreg_ids  # exempt excluded
        assert sub.id not in reach_ids | unreg_ids  # sub-reseller excluded
    _run(body, tmp_path)


def test_debtors_filter(tmp_path):
    async def body(s):
        _a, b, _c, _d, _sub = await _seed(s)
        reachable, unreg = await bc.resolve_recipients(s, "debtors", None, None)
        assert {r["reseller_id"] for r in reachable} == {b.id}
        assert unreg == []
    _run(body, tmp_path)


def test_few_active_filter(tmp_path):
    async def body(s):
        _a, b, _c, d, _sub = await _seed(s)
        reachable, unreg = await bc.resolve_recipients(s, "few_active", None, 10)
        # RootB (3 active) reachable; RootD (0 active) matched but unregistered; RootA (15) excluded.
        assert {r["reseller_id"] for r in reachable} == {b.id}
        assert {r["reseller_id"] for r in unreg} == {d.id}
    _run(body, tmp_path)


def test_debt_lifecycle_and_state_filters(tmp_path):
    """The professional segments: overdue (due now), deferred (deadline in future), pending_payment,
    paid_up, enforced/frozen, invoice_above, and new_resellers — each selects exactly its stage."""
    from app.models import Payment
    from app.models.enums import EnforcementState, PaymentMethod, PaymentStatus

    async def body(s):
        a, b, _c, _d, _sub = await _seed(s)
        # a: no invoices yet → paid_up. b: has an OVERDUE invoice due now (from _seed).
        panel_id = a.panel_id
        today = dt.date.today()

        def inv(rid, status, deferred=None, label="2026-05"):
            return Invoice(reseller_id=rid, panel_id=panel_id,
                           period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
                           period_label=label, usage_gb=1, amount_toman=10_000,
                           status=status, deferred_until=deferred)

        def root(uuid, name, chat, **kw):
            return Reseller(panel_id=panel_id, admin_uuid=uuid, name=name, parent_admin_uuid="own",
                            bot_chat_id=chat, last_seen_at=NOW, **kw)

        # e: overdue-status invoice BUT deadline in the future → deferred, NOT overdue.
        e = root("e", "RootE-deferred", 105)
        # f: enforced reseller; g: frozen reseller.
        f = root("f", "RootF-enforced", 106, enforcement_state=EnforcementState.enforced)
        g = root("g", "RootG-frozen", 107, enforcement_state=EnforcementState.frozen)
        s.add_all([e, f, g])
        await s.flush()
        s.add(inv(e.id, InvoiceStatus.overdue, deferred=today + dt.timedelta(days=5)))
        # b also gets a pending payment (awaiting the owner's confirm).
        s.add(Payment(reseller_id=b.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.pending))
        await s.commit()

        async def ids(audience, threshold=None):
            reachable, unreg = await bc.resolve_recipients(s, audience, None, threshold)
            return {r["reseller_id"] for r in reachable} | {r["reseller_id"] for r in unreg}

        assert await ids("overdue") == {b.id}            # e's deadline shields it
        assert await ids("deferred") == {e.id}
        assert await ids("pending_payment") == {b.id}
        assert b.id not in await ids("paid_up")          # debtor is NOT paid-up
        assert a.id in await ids("paid_up")              # no invoices → paid-up
        assert e.id not in await ids("paid_up")
        assert await ids("enforced") == {f.id}
        assert await ids("frozen") == {g.id}
        # new_resellers: everyone seeded just now is "new" within 30 days; nobody within 0 days.
        assert a.id in await ids("new_resellers", 30)
        assert await ids("new_resellers", 0) == set()
    _run(body, tmp_path)


def test_invoice_above_filter(tmp_path):
    """invoice_above (VIP) picks bundles at/over the Toman threshold this month.
    Re-baselined by N03: a missing/zero threshold now matches NOBODY — pre-N03 it
    collapsed to «>= 0» and matched every billable root (the dangerous direction)."""
    async def body(s):
        _a, _b, _c, _d, _sub = await _seed(s)
        # _seed users have no usage_limit_gb/start_date → nobody has sales this month.
        for bad in (0, None):
            reachable, unreg = await bc.resolve_recipients(s, "invoice_above", None, bad)
            assert reachable == [] and unreg == []
        reachable, unreg = await bc.resolve_recipients(s, "invoice_above", None, 1)
        assert reachable == [] and unreg == []
    _run(body, tmp_path)


def test_sales_amount_filters_with_real_usage(tmp_path):
    """zero_sale / invoice_below / invoice_above against SEEDED current-month usage —
    the _bundle_amounts math (quota sold × price) actually decides the audience."""
    async def body(s):
        a, b, _c, d, _sub = await _seed(s)
        from app.services.periods import today as tehran_today
        # RootA sells one 50 GB config this month → bundle = 50 GB × 1000 T = 50,000 T.
        s.add(EndUserSnapshot(panel_id=a.panel_id, user_uuid="a-sale-1", added_by_uuid="a",
                              usage_limit_gb=50, start_date=tehran_today(), enable=True,
                              is_active=True))
        await s.commit()

        async def ids(audience, threshold=None):
            reachable, unreg = await bc.resolve_recipients(s, audience, None, threshold)
            return {r["reseller_id"] for r in reachable} | {r["reseller_id"] for r in unreg}

        assert a.id in await ids("invoice_above", 40_000)          # 50k >= 40k
        assert await ids("invoice_above", 60_000) == set()         # nobody at 60k
        below = await ids("invoice_below", 40_000)
        assert a.id not in below and {b.id, d.id} <= below         # no-sales roots are < 40k
        zero = await ids("zero_sale")
        assert a.id not in zero and {b.id, d.id} <= zero
    _run(body, tmp_path)


def test_unknown_audience_matches_nobody_and_panel_alias_still_works(tmp_path):
    """N03: an unknown audience string must resolve to NOBODY (pre-N03 it silently fell
    through to «all»); the bot's "panel" alias keeps resolving to the panel's roots."""
    async def body(s):
        a, b, _c, d, _sub = await _seed(s)
        reachable, unreg = await bc.resolve_recipients(s, "typo-oops", None, None)
        assert reachable == [] and unreg == []
        # "panel" (bot alias): narrowing happens via load_billable_roots(panel_id).
        ids = {r["reseller_id"] for r in (await bc.resolve_recipients(s, "panel", a.panel_id, None))[0]}
        assert ids == {a.id, b.id}
    _run(body, tmp_path)


def test_panel_restriction_combines_with_audience(tmp_path):
    """The single-panel restriction narrows every audience to that panel's roots."""
    async def body(s):
        a, b, _c, _d, _sub = await _seed(s)
        # Second panel with its own owner, one registered root, and an owed invoice.
        p2 = Panel(key="p2", host="p2.invalid", proxy_path_enc="x", owner_uuid="own2",
                   status=PanelStatus.ok, last_synced_at=NOW)
        s.add(p2)
        await s.flush()
        s.add(Reseller(panel_id=p2.id, admin_uuid="own2", name="Owner2",
                       parent_admin_uuid=None, is_owner=True, last_seen_at=NOW))
        x = Reseller(panel_id=p2.id, admin_uuid="x", name="RootX", parent_admin_uuid="own2",
                     bot_chat_id=201, last_seen_at=NOW)
        s.add(x)
        await s.flush()
        s.add(Invoice(reseller_id=x.id, panel_id=p2.id, period_start=dt.date(2026, 6, 1),
                      period_end=dt.date(2026, 6, 30), period_label="2026-06",
                      usage_gb=2, amount_toman=20_000, status=InvoiceStatus.overdue))
        await s.commit()

        async def ids(audience, panel_id):
            reachable, unreg = await bc.resolve_recipients(s, audience, panel_id, None)
            return {r["reseller_id"] for r in reachable} | {r["reseller_id"] for r in unreg}

        assert await ids("debtors", None) == {b.id, x.id}   # both panels
        assert await ids("debtors", a.panel_id) == {b.id}   # panel 1 only
        assert await ids("debtors", p2.id) == {x.id}        # panel 2 only
    _run(body, tmp_path)


def test_api_validator_requires_positive_threshold():
    """N03: /broadcast + /broadcast/preview reject a threshold audience without a
    positive threshold (400) instead of passing it through."""
    import pytest
    from fastapi import HTTPException

    from app.api.operations import _validate_audience

    for audience in ("few_active", "invoice_below", "invoice_above"):
        for bad in (None, 0, -5):
            with pytest.raises(HTTPException) as e:
                _validate_audience(audience, bad)
            assert e.value.status_code == 400
    _validate_audience("invoice_above", 50_000)   # positive threshold → accepted
    _validate_audience("debtors", None)           # threshold-free audience → accepted
    with pytest.raises(HTTPException):
        _validate_audience("panel", None)         # bot-only alias is NOT a valid API audience
