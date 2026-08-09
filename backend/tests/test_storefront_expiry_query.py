"""The expiry sweep's snapshot lookup must be panel-scoped and chunked.

Why this file exists: `_load_snaps` keys its result `(panel_id, user_uuid)` but used to query on
`user_uuid` alone, with an unbounded `IN()`. End-user uuids are unique PER PANEL
(`uq_enduser_panel_uuid`), not globally, so that fetched every same-uuid row from every other
panel and threw them away in Python. Two consequences: the query could not use the
`(panel_id, …)` indexes, and across a ~151-shop fleet the parameter list grew without bound —
the exact overflow `metering._load_events` already chunks at 500 to avoid.
"""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfexp.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot  # noqa: E402
from app.services import storefront_expiry  # noqa: E402

SYNCED = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)


def _row(panel_id: int, uuid: str):
    """The (order, customer, bot) tuple shape `_load_snaps` consumes."""
    return (SimpleNamespace(panel_id=panel_id, panel_user_uuid=uuid), None, None)


async def _seeded(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with Session() as s:
        # SAME uuid on two different panels — legal, and the reason panel_id must be in the WHERE.
        for panel_id, gb in ((1, 10), (2, 99)):
            s.add(EndUserSnapshot(
                panel_id=panel_id, user_uuid="shared-uuid", name=f"p{panel_id}",
                added_by_uuid="res", usage_limit_gb=gb, current_usage_gb=0,
                start_date=dt.date(2026, 8, 1), last_synced_at=SYNCED))
        await s.commit()
    return engine, Session


async def test_snapshot_lookup_is_panel_scoped(tmp_path):
    """A shared uuid must resolve to the right panel's row, not whichever came back first."""
    engine, Session = await _seeded(tmp_path)
    async with Session() as s:
        snaps = await storefront_expiry._load_snaps(s, [_row(1, "shared-uuid")])
    assert set(snaps) == {(1, "shared-uuid")}, (
        f"the lookup returned rows for other panels: {sorted(snaps)}"
    )
    assert float(snaps[(1, "shared-uuid")].usage_limit_gb) == 10.0, (
        "panel 2's snapshot was returned for a panel 1 order"
    )
    await engine.dispose()


async def test_both_panels_resolve_independently(tmp_path):
    engine, Session = await _seeded(tmp_path)
    async with Session() as s:
        snaps = await storefront_expiry._load_snaps(
            s, [_row(1, "shared-uuid"), _row(2, "shared-uuid")])
    assert float(snaps[(1, "shared-uuid")].usage_limit_gb) == 10.0
    assert float(snaps[(2, "shared-uuid")].usage_limit_gb) == 99.0
    await engine.dispose()


async def test_the_in_list_is_chunked(tmp_path):
    """No single statement may bind more than the chunk size — the SQLite parameter ceiling."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'big.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    n = storefront_expiry._SNAP_CHUNK * 2 + 25
    async with Session() as s:
        for i in range(n):
            s.add(EndUserSnapshot(
                panel_id=1, user_uuid=f"u{i:05d}", name="x", added_by_uuid="res",
                usage_limit_gb=1, current_usage_gb=0, start_date=dt.date(2026, 8, 1),
                last_synced_at=SYNCED))
        await s.commit()

    widest = 0

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _watch(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        nonlocal widest
        if parameters and "end_user_snapshots" in statement:
            widest = max(widest, len(parameters))

    async with Session() as s:
        snaps = await storefront_expiry._load_snaps(
            s, [_row(1, f"u{i:05d}") for i in range(n)])

    assert len(snaps) == n, "chunking must not drop rows"
    assert widest <= storefront_expiry._SNAP_CHUNK + 1, (
        f"a statement bound {widest} parameters; the IN() list is not chunked, which overflows "
        "SQLite's bound-parameter limit on a large fleet"
    )
    await engine.dispose()
