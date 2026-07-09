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
    """invoice_above (VIP) picks bundles at/over the Toman threshold this month."""
    async def body(s):
        a, b, _c, d, _sub = await _seed(s)
        # a's current-month bundle: 15 users × 0 GB… _seed users have no usage_limit_gb/start_date,
        # so nobody has sales → above-0 matches all, above-1 matches none.
        assert {r["reseller_id"] for r in (await bc.resolve_recipients(s, "invoice_above", None, 0))[0]} \
            == {r["reseller_id"] for r in (await bc.resolve_recipients(s, "all", None, None))[0]}
        reachable, unreg = await bc.resolve_recipients(s, "invoice_above", None, 1)
        assert reachable == [] and unreg == []
    _run(body, tmp_path)
