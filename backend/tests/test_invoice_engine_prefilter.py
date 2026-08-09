"""Feeding the invoice engine only the period's snapshots must not change a single Toman.

Why this file exists: `generate_invoices` / `preview_bundles` / `recompute_invoice` used to load
EVERY end-user snapshot a panel had ever synced and hand the whole set to `compute_invoices`,
which discards ~92% of them on the first line of `billable_gb_for_user`
(`if not period.contains(u.start_date): return None`). At production scale that is 200k ORM
instances at ~2.02 KB each — measured 5.10 s / 45 MB versus 0.78 s / 4 MB for the 16.5k rows the
period actually contains — and it ran on the API event loop behind /reports/sales-by-day.

Moving that predicate into SQL is safe only because the engine reads `users` for nothing else:
lines come from `billable_gb_for_user`, and `users_count` / `raw_gb` / `total_gb` all derive from
lines, while roots and the hierarchy come from `resellers`. This file is the proof, and it is a
MONEY test: it pins that the filtered feed and the unfiltered feed produce identical invoices,
including the edge cases where "outside the period" is subtle — a config deleted from the panel,
a NULL start_date, and both month boundaries.
"""
from __future__ import annotations

import datetime as dt
import os
import tracemalloc

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/prefilter.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import PanelStatus  # noqa: E402
from app.services import invoicing  # noqa: E402
from app.services.invoice_engine import compute_invoices  # noqa: E402
from app.services.periods import month_period  # noqa: E402

PERIOD = month_period(2026, 8)
SYNCED = dt.datetime(2026, 9, 1, 6, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def seeded(tmp_path):
    """A panel whose snapshots straddle the period in every way that matters."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with Session() as s:
        panel = Panel(key="p1", host="h.example.com", proxy_path="x", owner_uuid="own",
                      enabled=True, status=PanelStatus.ok, last_synced_at=SYNCED)
        s.add(panel)
        await s.flush()
        s.add(Reseller(panel_id=panel.id, admin_uuid="own", name="owner", is_owner=True,
                       last_seen_at=SYNCED))
        s.add(Reseller(panel_id=panel.id, admin_uuid="res-a", name="A",
                       parent_admin_uuid="own", last_seen_at=SYNCED))
        s.add(Reseller(panel_id=panel.id, admin_uuid="res-b", name="B",
                       parent_admin_uuid="res-a", last_seen_at=SYNCED))  # sub-reseller
        await s.flush()

        def snap(uuid, start, gb=10, used=3.0, synced=SYNCED, adder="res-a"):
            return EndUserSnapshot(
                panel_id=panel.id, user_uuid=uuid, name=f"u-{uuid}", added_by_uuid=adder,
                usage_limit_gb=gb, current_usage_gb=used, start_date=start,
                package_days=30, enable=True, is_active=True, last_synced_at=synced,
            )

        s.add_all([
            # --- inside the period ---
            snap("in-first", PERIOD.start),                       # exact lower boundary
            snap("in-last", PERIOD.end),                          # exact upper boundary
            snap("in-mid", dt.date(2026, 8, 15)),
            snap("in-sub", dt.date(2026, 8, 20), adder="res-b"),  # billed via the sub-reseller
            snap("in-free", dt.date(2026, 8, 9), gb=1),           # free test config (<= threshold)
            # deleted from the panel (stale snapshot) but created IN the period → billed on
            # consumption; the date check runs BEFORE that branch, so it must survive the filter
            snap("in-deleted", dt.date(2026, 8, 11), gb=50, used=22.0,
                 synced=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)),
            # --- outside the period: every one of these must be irrelevant ---
            snap("out-before", dt.date(2026, 7, 31)),             # one day early
            snap("out-after", dt.date(2026, 9, 1)),               # one day late
            snap("out-null", None),                               # never started
            snap("out-old", dt.date(2025, 12, 3), gb=500),        # big, but last year
        ])
        await s.commit()
        yield Session, panel.id
    await engine.dispose()


async def _bundles(Session, panel_id, *, filtered: bool):
    async with Session() as s:
        panel = await s.get(Panel, panel_id)
        resellers = (await s.execute(
            select(Reseller).where(Reseller.panel_id == panel_id))).scalars().all()
        if filtered:
            q = invoicing._period_users_q(panel_id, PERIOD)
        else:
            q = select(EndUserSnapshot).where(EndUserSnapshot.panel_id == panel_id)
        users = (await s.execute(q)).scalars().all()
        return users, compute_invoices(
            resellers, users, PERIOD,
            default_price_per_gb=1000, excluded_usage_gb=set(), free_threshold_gb=1.0,
            panel_synced_at=panel.last_synced_at, deleted_full_quota_over_gb=0.0,
        )


def _fingerprint(bundles):
    """Everything that reaches an Invoice row, in a comparable form."""
    return sorted(
        (
            b.root.admin_uuid, b.total_gb, b.raw_gb, b.users_count, b.price_per_gb,
            b.base_amount_toman, b.min_sale_toman, b.floor_applied, b.amount_toman,
            sorted((ln.user_uuid, ln.usage_gb, ln.start_date, ln.added_by_uuid,
                    ln.sub_reseller_name, ln.from_deleted) for ln in b.lines),
        )
        for b in bundles
    )


async def test_filtered_and_unfiltered_feeds_produce_identical_invoices(seeded):
    Session, panel_id = seeded
    all_users, full = await _bundles(Session, panel_id, filtered=False)
    few_users, filtered = await _bundles(Session, panel_id, filtered=True)

    assert len(all_users) == 10 and len(few_users) == 6, (
        "the fixture must actually exercise the filter "
        f"(loaded {len(all_users)} vs {len(few_users)})"
    )
    assert _fingerprint(full) == _fingerprint(filtered), (
        "the SQL date pre-filter changed the computed invoice — it must be exactly the engine's "
        "own `period.contains(start_date)` predicate and nothing more"
    )
    # And the result is non-trivial, so an all-zero bug can't pass this test.
    assert sum(b.total_gb for b in filtered) > 0


async def test_the_deleted_in_period_config_is_still_billed(seeded):
    """The subtle one: a config removed from the panel is billed on CONSUMPTION, and that branch
    lives after the date check. Dropping it from the feed would silently shrink an invoice."""
    Session, panel_id = seeded
    _users, bundles = await _bundles(Session, panel_id, filtered=True)
    lines = {ln.user_uuid: ln for b in bundles for ln in b.lines}
    assert "in-deleted" in lines, "a deleted-but-in-period config vanished from the invoice"
    assert lines["in-deleted"].from_deleted is True
    assert lines["in-deleted"].usage_gb == pytest.approx(22.0)


async def test_out_of_period_snapshots_never_reach_the_engine(seeded):
    Session, panel_id = seeded
    users, bundles = await _bundles(Session, panel_id, filtered=True)
    loaded = {u.user_uuid for u in users}
    assert not {u for u in loaded if u.startswith("out-")}, (
        f"out-of-period snapshots were loaded: {sorted(loaded)}"
    )
    billed = {ln.user_uuid for b in bundles for ln in b.lines}
    assert billed == {"in-first", "in-last", "in-mid", "in-sub", "in-deleted"}


async def test_the_prefilter_bounds_what_billing_hydrates(tmp_path):
    """Resource lock, in the spirit of tests/test_backup_memory.py.

    The unfiltered feed hydrates one ~2 KB ORM instance per snapshot EVER synced, so its cost is
    a function of table size rather than of the month being billed. Assert the filtered query
    stays proportional to the period instead — this fails the moment the filter is dropped.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'big.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    in_period, out_period = 400, 6_000
    async with Session() as s:
        panel = Panel(key="p", host="h.example.com", proxy_path="x", owner_uuid="own",
                      enabled=True, status=PanelStatus.ok, last_synced_at=SYNCED)
        s.add(panel)
        await s.flush()
        pid = panel.id
        for i in range(in_period):
            s.add(EndUserSnapshot(
                panel_id=pid, user_uuid=f"in{i}", name="نمایندهٔ آزمایشی" * 4,
                added_by_uuid="res-a", usage_limit_gb=10, current_usage_gb=1,
                start_date=dt.date(2026, 8, (i % 28) + 1), last_synced_at=SYNCED))
        for i in range(out_period):
            s.add(EndUserSnapshot(
                panel_id=pid, user_uuid=f"out{i}", name="نمایندهٔ آزمایشی" * 4,
                added_by_uuid="res-a", usage_limit_gb=10, current_usage_gb=1,
                start_date=dt.date(2026, 2, (i % 28) + 1), last_synced_at=SYNCED))
        await s.commit()

    async def peak(q) -> tuple[int, float]:
        async with Session() as s:
            tracemalloc.start()
            rows = (await s.execute(q)).scalars().all()
            _, pk = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return len(rows), pk / (1024 * 1024)

    n_all, mb_all = await peak(select(EndUserSnapshot).where(EndUserSnapshot.panel_id == pid))
    n_few, mb_few = await peak(invoicing._period_users_q(pid, PERIOD))
    await engine.dispose()

    assert n_all == in_period + out_period and n_few == in_period
    assert mb_few < mb_all / 4, (
        f"the pre-filtered load peaked at {mb_few:.1f} MB against {mb_all:.1f} MB for the whole "
        "table — billing is hydrating snapshots it cannot bill (at production scale this is "
        "~180 MB on the API event loop)"
    )
