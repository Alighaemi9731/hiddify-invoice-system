"""Security remediation — Batch 4 (billing & sync correctness).

F9  a panel whose last successful sync is older than the configured cap is not billed.
F10 when metering.apply fails for a user, that snapshot's usage/limit BASELINE is NOT advanced,
    so the next sync recaptures the full delta (no silent underbilling).
F12 concurrent syncs of ONE panel serialize (advisory lock) — no unique-violation / spurious error.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremb4.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import EndUserSnapshot, Panel  # noqa: E402
from app.models.enums import PanelStatus  # noqa: E402
from app.services import sync as sync_service  # noqa: E402
from app.services.panel_client.base import (  # noqa: E402
    PanelAdmin,
    PanelData,
    PanelUser,
)
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402

_OWNER = "owner-uuid"


def _data(users):  # noqa: ANN001
    return PanelData(
        admins=[PanelAdmin(uuid=_OWNER, name="Owner", parent_admin_uuid=None, mode="super_admin",
                           comment=None, telegram_id=None, max_users=100, max_active_users=100,
                           can_add_admin=True)],
        users=users,
    )


def _user(uuid, *, limit, used):  # noqa: ANN001
    return PanelUser(uuid=uuid, name="u", added_by_uuid=_OWNER, start_date=dt.date(2026, 7, 1),
                     usage_limit_gb=limit, current_usage_gb=used, package_days=30, enable=True,
                     is_active=True, mode="no_reset", last_online=None, comment=None)


# ───────────────────────── F9: stale-snapshot age gate ─────────────────────────
def test_f9_panel_billable_age_gate():
    from app.services.invoicing import _panel_billable
    now = dt.datetime(2026, 7, 15, 12, tzinfo=dt.timezone.utc)
    p = Panel(key="x", host="h", proxy_path_enc="e", owner_uuid="o")
    p.status = PanelStatus.ok
    p.last_synced_at = now - dt.timedelta(hours=1)
    assert _panel_billable(p, now=now, max_age_hours=26)[0] is True       # fresh
    p.last_synced_at = now - dt.timedelta(hours=48)
    assert _panel_billable(p, now=now, max_age_hours=26)[0] is False      # stale → skipped
    assert _panel_billable(p, now=now, max_age_hours=0)[0] is True        # 0 = cap disabled
    p.last_synced_at = None
    assert _panel_billable(p, now=now, max_age_hours=26)[0] is False      # never synced


# ───────────────────────── F10: metering failure doesn't lose usage ─────────────────────────
def test_f10_metering_failure_preserves_baseline(tmp_path, monkeypatch):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'f10.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        try:
            from app.services import metering, settings_service
            async with S() as s:
                await settings_service.set_value(s, "metering_enabled", True)
                panel = Panel(key="p", host="h", proxy_path_enc=crypto.encrypt("x") or "",
                              owner_uuid=_OWNER)
                s.add(panel)
                await s.flush()
                pid = panel.id
                s.add(EndUserSnapshot(panel_id=pid, user_uuid="u1", added_by_uuid=_OWNER,
                                      usage_limit_gb=5, current_usage_gb=2))
                await s.commit()

            def _boom(**k):  # metering.apply raises for this user
                raise RuntimeError("meter fail")
            monkeypatch.setattr(metering, "apply", _boom)

            # New panel data would advance u1 to 10/8 — but metering failed, so the baseline must hold.
            async with S() as s:
                panel = await s.get(Panel, pid)
                from app.models.enums import SyncStatus
                run = await sync_service.sync_panel(
                    s, panel, data=_data([_user("u1", limit=10, used=8)]))
                assert run.status == SyncStatus.success  # sync stayed alive despite metering fail

            async with S() as s:
                from sqlalchemy import select
                snap = (await s.execute(select(EndUserSnapshot).where(
                    EndUserSnapshot.user_uuid == "u1"))).scalar_one()
                # baseline NOT advanced (still 5/2) → next sync recomputes the full delta
                assert float(snap.usage_limit_gb) == 5 and float(snap.current_usage_gb) == 2
        finally:
            await engine.dispose()
    asyncio.run(go())


# ───────────────────────── F12 (PG): per-panel sync serialization ─────────────────────────
@pytest.mark.pg_contract
@requires_pg
def test_f12_concurrent_panel_syncs_serialize():
    async def run():
        engine, factory = make_engine()
        try:
            async with factory() as s:
                panel = Panel(key="pz", host="hz", proxy_path_enc=crypto.encrypt("x") or "",
                              owner_uuid=_OWNER)
                s.add(panel)
                await s.commit()
                pid = panel.id

            data = _data([_user("uu1", limit=5, used=1), _user("uu2", limit=5, used=1)])

            async def _sync():
                async with factory() as s:
                    p = await s.get(Panel, pid)
                    return await sync_service.sync_panel(s, p, data=data)

            # Two concurrent first-time syncs of the SAME panel would collide on
            # uq_enduser_panel_uuid without the advisory lock; with it they serialize.
            r1, r2 = await asyncio.gather(_sync(), _sync(), return_exceptions=True)
            assert not isinstance(r1, BaseException), r1
            assert not isinstance(r2, BaseException), r2

            async with factory() as s:
                from sqlalchemy import func, select
                p = await s.get(Panel, pid)
                nsnap = (await s.execute(select(func.count()).select_from(EndUserSnapshot)
                         .where(EndUserSnapshot.panel_id == pid))).scalar_one()
                assert p.status == PanelStatus.ok      # not spuriously errored
                assert nsnap == 2                      # one snapshot per user, no duplicates
                # cleanup
                await s.execute(EndUserSnapshot.__table__.delete().where(
                    EndUserSnapshot.panel_id == pid))
                from app.models import Reseller, SyncRun
                await s.execute(SyncRun.__table__.delete().where(SyncRun.panel_id == pid))
                await s.execute(Reseller.__table__.delete().where(Reseller.panel_id == pid))
                await s.delete(await s.get(Panel, pid))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())
