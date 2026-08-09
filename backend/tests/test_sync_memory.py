"""Only one panel's sync WRITE phase may be resident at a time.

Why this file exists: `sync_all` fans out to `_SYNC_ALL_CONCURRENCY` (3) panels so one slow or
unreachable panel cannot serialize the rest. That concurrency used to cover the whole of
`sync_panel`, write phase included. One 20k-user panel's upsert peaks around 139 MB — the
existing-snapshot map (~2.02 KB per hydrated ORM row), this month's UsageMeter rows, the parsed
PanelData, and SQLAlchemy's unit-of-work batching every UPDATE — so three concurrent panels meant
~416 MB of simultaneous Python allocation inside a 768 MB container. That was the single largest
term in the scheduler's memory budget and, together with the backup landing on the same wall-clock
minute (see tests/test_scheduler_stagger.py), the shape of the Jul 2026 OOM kills.

Since Wave 4/B10 the network fetch already happens OUTSIDE the per-panel advisory lock, and the
fetch (45–90 s) dominates wall clock while the upsert is ~3.5 s. So the fix gates only the write
phase: the fan-out still hides slow panels, but the peak is one upsert instead of three.

The guard is behavioural, not a byte budget — it observes how many panels are inside the write
phase at once, which is the property that actually bounds memory and is stable across machines.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/syncmem.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel  # noqa: E402
from app.models.enums import PanelStatus, SyncStatus  # noqa: E402
from app.services import sync as sync_service  # noqa: E402
from app.services.panel_client.base import (  # noqa: E402
    PanelAdmin,
    PanelData,
    PanelUser,
)

_PANELS = 4
_USERS = 30


def _panel_data(panel_ix: int) -> PanelData:
    admins = [
        PanelAdmin(uuid=f"own{panel_ix}", name="owner", parent_admin_uuid=None,
                   mode="super_admin", comment=None, telegram_id=None,
                   max_users=None, max_active_users=None),
        PanelAdmin(uuid=f"res{panel_ix}", name="A", parent_admin_uuid=f"own{panel_ix}",
                   mode="agent", comment=None, telegram_id=None,
                   max_users=100, max_active_users=100),
    ]
    users = [
        PanelUser(uuid=f"p{panel_ix}-u{i}", name=f"user {i}", added_by_uuid=f"res{panel_ix}",
                  start_date=dt.date(2026, 8, (i % 28) + 1), usage_limit_gb=10.0,
                  current_usage_gb=1.0, package_days=30, enable=True, is_active=True,
                  mode="no_reset", last_online=None, comment=None)
        for i in range(_USERS)
    ]
    return PanelData(admins=admins, users=users, client_proxy_path=None)


async def test_only_one_panel_writes_at_a_time(tmp_path, monkeypatch):
    """The invariant: the write gate must admit exactly one panel's upsert."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(sync_service, "SessionLocal", Session)

    async with Session() as s:
        for i in range(_PANELS):
            s.add(Panel(key=f"p{i}", host=f"h{i}.example.com", proxy_path="x",
                        owner_uuid=f"own{i}", enabled=True, status=PanelStatus.ok))
        await s.commit()

    inside = 0
    high_water = 0
    real_upsert = sync_service._upsert_users

    async def counting_upsert(session, panel, data, now):  # noqa: ANN001
        nonlocal inside, high_water
        inside += 1
        high_water = max(high_water, inside)
        try:
            # Yield control so any other panel that COULD run concurrently would be observed
            # here. Without the gate this reliably reaches 3 (_SYNC_ALL_CONCURRENCY).
            await asyncio.sleep(0)
            return await real_upsert(session, panel, data, now)
        finally:
            inside -= 1

    monkeypatch.setattr(sync_service, "_upsert_users", counting_upsert)

    class _Client:
        async def fetch_backup(self, panel):  # noqa: ANN001, ANN201
            await asyncio.sleep(0)  # a real fetch is network I/O; keep the fan-out real
            return _panel_data(int(panel.key[1:]))

    real_sync_panel = sync_service.sync_panel

    async def patched(session, panel, **kw):  # noqa: ANN001, ANN003
        kw.setdefault("client", _Client())
        return await real_sync_panel(session, panel, **kw)

    monkeypatch.setattr(sync_service, "sync_panel", patched)

    async with Session() as s:
        runs = await sync_service.sync_all(s)

    assert len(runs) == _PANELS
    assert all(r.status == SyncStatus.success for r in runs), "every panel must sync"
    assert high_water == 1, (
        f"{high_water} panels were inside the sync WRITE phase at once. Each 20k-user upsert "
        "peaks near 139 MB, so concurrent writes put ~416 MB in a 768 MB container — the "
        "sync_all fan-out must cover the network FETCH only (app/services/sync.py:write_gate)"
    )

    # All four panels' data really landed — the gate must serialize, not drop.
    async with Session() as s:
        rows = (await s.execute(
            EndUserSnapshot.__table__.select())).all()
    assert len(rows) == _PANELS * _USERS
    await engine.dispose()


async def test_a_failing_panel_still_releases_the_gate(tmp_path, monkeypatch):
    """A panel that raises inside the write phase must not wedge the fleet: the gate is
    released in `finally`, and `sync_all` already isolates one bad panel from the rest."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(sync_service, "SessionLocal", Session)

    async with Session() as s:
        for i in range(3):
            s.add(Panel(key=f"p{i}", host=f"h{i}.example.com", proxy_path="x",
                        owner_uuid=f"own{i}", enabled=True, status=PanelStatus.ok))
        await s.commit()

    class _Client:
        async def fetch_backup(self, panel):  # noqa: ANN001, ANN201
            if panel.key == "p1":
                raise RuntimeError("panel unreachable")
            return _panel_data(int(panel.key[1:]))

    real_sync_panel = sync_service.sync_panel

    async def patched(session, panel, **kw):  # noqa: ANN001, ANN003
        kw.setdefault("client", _Client())
        return await real_sync_panel(session, panel, **kw)

    monkeypatch.setattr(sync_service, "sync_panel", patched)

    async with Session() as s:
        runs = await sync_service.sync_all(s)

    by_status = sorted(r.status.value for r in runs)
    assert by_status == ["failed", "success", "success"], by_status
    assert not sync_service.write_gate().locked(), "the write gate leaked on the failure path"
    await engine.dispose()
