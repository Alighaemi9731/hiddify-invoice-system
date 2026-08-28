"""Traffic audit: reseller selection, failure isolation, and history persistence.

⚠️ FIXTURE GOTCHA — two traps of the same family CLAUDE.md documents for the CRM board:

1. A reseller MUST have `last_seen_at == panel.last_synced_at`, and the panel MUST have
   `status=ok` + `last_synced_at` set. `_reseller_present` fails OPEN (returns True) when either
   timestamp is missing, so omitting them makes a presence assertion pass for the wrong reason.
2. There MUST be an explicit `is_owner=True` row. Without one, `select_billable_roots` falls back
   to promoting every structural root — so a test asserting "sub-resellers are excluded" would
   silently assert nothing.

The panel client is stubbed everywhere; no test here touches the network.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/traffic.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    ResellerTrafficDaily,
)
from app.models.enums import PanelStatus  # noqa: E402
from app.services import traffic_audit as ta  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=2)
OWNER = "owner-uuid"


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


class FakeClient:
    """Stands in for AdminApiClient. Maps api_key (an admin uuid) → a usage_history payload."""

    def __init__(self, by_uuid: dict, raises: set | None = None):
        self.by_uuid = by_uuid
        self.raises = raises or set()
        self.calls: list[str] = []

    async def get_server_status(self, panel, *, api_key=None):  # noqa: ANN001
        self.calls.append(api_key)
        if api_key in self.raises:
            raise RuntimeError("panel said no")
        return self.by_uuid.get(api_key)


def _history(yday_gb: float, online: int, gb30: float, users: int = 10) -> dict:
    gib = 1024**3
    return {
        "yesterday": {"usage": str(int(yday_gb * gib)), "online": str(online)},
        "last_30_days": {"usage": str(int(gb30 * gib)), "online": online},
        "total": {"usage": str(int(gb30 * gib)), "online": online, "users": users},
    }


async def _seed(s):
    """One good panel with: an owner, a normal root, an exempt root, a sub, an absent root."""
    panel = Panel(key="s7", host="s7.invalid", proxy_path_enc="x", owner_uuid=OWNER,
                  status=PanelStatus.ok, last_synced_at=NOW)
    stale = Panel(key="bad", host="b.invalid", proxy_path_enc="x", owner_uuid="o2",
                  status=PanelStatus.error, last_synced_at=NOW)
    s.add_all([panel, stale])
    await s.flush()

    owner = Reseller(panel_id=panel.id, admin_uuid=OWNER, name="Owner", is_owner=True,
                     parent_admin_uuid=OWNER, last_seen_at=NOW)
    root = Reseller(panel_id=panel.id, admin_uuid="root-a", name="RootA",
                    parent_admin_uuid=OWNER, last_seen_at=NOW)
    exempt = Reseller(panel_id=panel.id, admin_uuid="root-x", name="Exempt",
                      parent_admin_uuid=OWNER, exclude_from_billing=True, last_seen_at=NOW)
    absent = Reseller(panel_id=panel.id, admin_uuid="root-gone", name="Gone",
                      parent_admin_uuid=OWNER, last_seen_at=OLD)
    s.add_all([owner, root, exempt, absent])
    await s.flush()
    sub = Reseller(panel_id=panel.id, admin_uuid="sub-a", name="SubA",
                   parent_admin_uuid="root-a", last_seen_at=NOW)
    s.add(sub)
    await s.flush()

    # RootA sold 100 GB directly; its sub sold 20 → the bundle ceiling is 120.
    s.add_all([
        EndUserSnapshot(panel_id=panel.id, user_uuid="u1", added_by_uuid="root-a",
                        usage_limit_gb=100, current_usage_gb=40, last_synced_at=NOW),
        EndUserSnapshot(panel_id=panel.id, user_uuid="u2", added_by_uuid="sub-a",
                        usage_limit_gb=20, current_usage_gb=5, last_synced_at=NOW),
    ])
    await s.commit()
    return panel, root


# ------------------------------------------------------------- the predicate
def test_only_billable_top_level_resellers_are_measured(tmp_path, monkeypatch):
    async def body(s):
        _panel, _root = await _seed(s)
        fake = FakeClient({
            "root-a": _history(10, 5, 300), OWNER: _history(50, 20, 1000),
        })
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        result = await ta.scan(s)
        names = {r.reseller_name for r in result.rows}
        assert names == {"RootA"}, names
        # The owner is never billed, so it is never audited...
        assert "Owner" not in names
        # ...but it IS asked once per panel, for the panel-wide total behind «سهم پنل».
        assert OWNER in fake.calls
        assert "Exempt" not in names       # exclude_from_billing
        assert "SubA" not in names         # rolls up into RootA, never its own row
        assert "Gone" not in names         # last_seen_at older than the panel's sync
        # The failed panel never reaches the network phase and is named in the report.
        assert [p["panel_key"] for p in result.skipped_panels] == ["bad"]

    _run(body, tmp_path)


def test_sub_reseller_quota_rolls_into_its_parent(tmp_path, monkeypatch):
    """The panel already rolls TRAFFIC up over the sub-tree, so the ceiling must roll up too —
    otherwise a reseller that sells through a sub looks like it consumed twice its quota."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({"root-a": _history(10, 5, 300), OWNER: _history(50, 20, 1000)})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        row = (await ta.scan(s)).rows[0]
        assert row.quota_gb == 120.0        # 100 (root) + 20 (sub), not 100
        assert row.counter_gb == 45.0       # 40 + 5
        assert row.sub_count == 1
        assert row.ratio == pytest.approx(2.5, abs=0.01)   # 300 / 120

    _run(body, tmp_path)


def test_panel_share_uses_the_owner_total_not_the_sum_of_roots(tmp_path, monkeypatch):
    """Summing the roots excludes the Owner's own users and would inflate every share."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({"root-a": _history(25, 5, 300), OWNER: _history(100, 40, 2000)})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        row = (await ta.scan(s)).rows[0]
        assert row.panel_share_pct == pytest.approx(25.0)   # 25/100, not 25/25

    _run(body, tmp_path)


# --------------------------------------------------------- failure isolation
def test_one_unreachable_reseller_does_not_abort_the_panel(tmp_path, monkeypatch):
    async def body(s):
        panel, _root = await _seed(s)
        second = Reseller(panel_id=panel.id, admin_uuid="root-b", name="RootB",
                          parent_admin_uuid=OWNER, last_seen_at=NOW)
        s.add(second)
        await s.commit()

        fake = FakeClient(
            {"root-b": _history(5, 2, 50), OWNER: _history(50, 20, 1000)},
            raises={"root-a"},
        )
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        result = await ta.scan(s)
        by_name = {r.reseller_name: r for r in result.rows}
        assert by_name["RootA"].reachable is False
        assert by_name["RootA"].flagged is False
        assert by_name["RootB"].reachable is True        # the other reseller still measured

    _run(body, tmp_path)


def test_unreachable_resellers_are_never_stored(tmp_path, monkeypatch):
    """A 0 GB row would read as "they went quiet" forever after — the opposite of "we could not
    ask" — and would poison every later trend."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({OWNER: _history(50, 20, 1000)}, raises={"root-a"})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        result = await ta.scan(s)
        stored = await ta.store(s, result)
        assert stored == 0
        rows = (await s.execute(select(ResellerTrafficDaily))).scalars().all()
        assert rows == []

    _run(body, tmp_path)


# ------------------------------------------------------------- persistence
def test_store_is_idempotent_for_the_same_day(tmp_path, monkeypatch):
    """The manual button and the cron job can overlap, and the retry hour re-runs the same day."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({"root-a": _history(10, 5, 300), OWNER: _history(50, 20, 1000)})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        day = dt.date(2026, 8, 27)
        await ta.store(s, await ta.scan(s), day=day)
        await ta.store(s, await ta.scan(s), day=day)

        rows = (await s.execute(select(ResellerTrafficDaily))).scalars().all()
        assert len(rows) == 1                       # upserted, not duplicated
        assert float(rows[0].traffic_30d_gb) == pytest.approx(300, abs=0.5)

    _run(body, tmp_path)


def test_latest_reads_back_without_touching_a_panel(tmp_path, monkeypatch):
    """The page opens on this: the scheduled run happens in another container, so its result is
    only ever visible through the stored history."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({"root-a": _history(10, 5, 300), OWNER: _history(50, 20, 1000)})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)
        await ta.store(s, await ta.scan(s))

        payload = await ta.latest(s)
        assert payload["resellers_scanned"] == 1
        row = payload["rows"][0]
        assert row["reseller_name"] == "RootA"
        assert row["quota_gb"] == 120.0
        assert row["counter_ratio"] == pytest.approx(6.7, abs=0.1)   # 300 / 45
        assert isinstance(row["ratio"], float)      # Decimal from PG must not leak out

    _run(body, tmp_path)


def test_run_daily_skips_resellers_already_stored_today(tmp_path, monkeypatch):
    """This is what makes the scheduler's +2h retry hour nearly free."""
    async def body(s):
        await _seed(s)
        fake = FakeClient({"root-a": _history(10, 5, 300), OWNER: _history(50, 20, 1000)})
        monkeypatch.setattr(ta, "AdminApiClient", lambda **kw: fake)

        first = await ta.run_daily(s)
        assert first["stored"] == 1
        calls_after_first = len(fake.calls)

        second = await ta.run_daily(s)
        assert second["stored"] == 0
        # No reseller call at all on the retry — only whatever the panel-total read costs.
        assert "root-a" not in fake.calls[calls_after_first:]

    _run(body, tmp_path)


def test_prune_drops_only_rows_past_the_window(tmp_path):
    async def body(s):
        s.add_all([
            ResellerTrafficDaily(panel_key="s7", reseller_admin_uuid="a",
                                 day=ta.today() - dt.timedelta(days=400)),
            ResellerTrafficDaily(panel_key="s7", reseller_admin_uuid="b", day=ta.today()),
        ])
        await s.commit()

        dropped = await ta.prune(s, keep_days=180)
        assert dropped == 1
        left = (await s.execute(select(ResellerTrafficDaily))).scalars().all()
        assert [r.reseller_admin_uuid for r in left] == ["b"]

    _run(body, tmp_path)


# ── PG contract: the daily uniqueness must hold across processes ──────────────────────────────
# The manual button (API container) and the cron job (scheduler container) can run at the same
# time, so two processes can race to write the same (panel_key, admin_uuid, day). SQLite cannot
# arbitrate that. Runs in CI's `backend-postgres` job (`pytest -m pg_contract`).
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


@pytest.mark.pg_contract
@requires_pg
def test_pg_one_row_per_reseller_per_day():
    """Two concurrent writers for the same key leave exactly one row, and the constraint — not
    luck — is what enforces it."""
    async def run():
        engine, Session = make_engine()
        tag = "trafuq"
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with Session() as s:
                await s.execute(
                    ResellerTrafficDaily.__table__.delete().where(
                        ResellerTrafficDaily.panel_key == tag
                    )
                )
                await s.commit()

            day = dt.date(2026, 8, 27)

            async def write(gb: float):
                async with Session() as s:
                    s.add(ResellerTrafficDaily(
                        panel_key=tag, reseller_admin_uuid="dup", day=day, traffic_gb=gb,
                    ))
                    await s.commit()

            await write(1.0)
            with pytest.raises(Exception):          # IntegrityError from the unique constraint
                await write(2.0)

            async with Session() as s:
                rows = (await s.execute(
                    select(ResellerTrafficDaily).where(ResellerTrafficDaily.panel_key == tag)
                )).scalars().all()
                assert len(rows) == 1
                await s.execute(
                    ResellerTrafficDaily.__table__.delete().where(
                        ResellerTrafficDaily.panel_key == tag
                    )
                )
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
