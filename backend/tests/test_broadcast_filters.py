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
