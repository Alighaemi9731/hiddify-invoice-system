"""Security remediation — Round 2, Batch C (sync / metering / bot resource).

F12 a failed sync's bookkeeping is re-serialized under the per-panel advisory lock and guarded by
    recency, so an older failure never clobbers a newer success's `ok` status.
F13 the storefront poll loop applies backpressure BEFORE creating a handler task, so the number of
    LIVE task objects is bounded (not just the number of actively-running handlers).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremr2c.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import Panel, SyncRun  # noqa: E402
from app.models.enums import PanelStatus, SyncStatus  # noqa: E402
from app.services import sync as sync_service  # noqa: E402
from app.services.panel_client.base import (  # noqa: E402
    PanelAdmin,
    PanelData,
    PanelUser,
)
from tests.pg_barrier import Barrier, make_engine, requires_pg  # noqa: E402

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


# ───────────────────────── F12: recency guard (unit) ─────────────────────────
def test_f12_newer_success_since():
    from app.services.sync import _newer_success_since
    t0 = dt.datetime(2026, 7, 15, 12, tzinfo=dt.timezone.utc)
    assert _newer_success_since(None, t0) is False                                  # never synced
    assert _newer_success_since(t0 - dt.timedelta(minutes=1), t0) is False          # older success
    assert _newer_success_since(t0 + dt.timedelta(minutes=1), t0) is True           # newer success wins
    # naive value (as SQLite returns) is treated as UTC, not a crash
    assert _newer_success_since(dt.datetime(2026, 7, 15, 12, 1), t0) is True


# ───────────────────────── F12 (PG): a late failure can't clobber a newer success ─────────────────────────
@pytest.mark.pg_contract
@requires_pg
def test_f12_late_failure_does_not_clobber_success():
    """A (fails) acquires the per-panel lock first; B (succeeds) queues behind it. After A rolls back,
    B wins the lock and commits `ok`; A's failure bookkeeping then re-acquires the lock and — via the
    recency guard — must NOT overwrite B's `ok`. Without the fix A would clobber it to `error`."""
    async def run():
        engine, factory = make_engine()
        try:
            async with factory() as s:
                panel = Panel(key="pf12", host="hf12", proxy_path_enc=crypto.encrypt("x") or "",
                              owner_uuid=_OWNER)
                s.add(panel)
                await s.commit()
                pid = panel.id

            barrier = Barrier()
            good = _data([_user("uu1", limit=5, used=1)])

            class FailClient:
                async def fetch_backup(self, panel):  # noqa: ANN001
                    barrier.arrive()            # A holds the lock now → let B start (it blocks on the lock)
                    await asyncio.sleep(0.1)     # give B time to queue on the advisory lock
                    raise RuntimeError("boom")

            async def coro_a():
                async with factory() as s:
                    p = await s.get(Panel, pid)
                    return await sync_service.sync_panel(s, p, client=FailClient())

            async def coro_b():
                await barrier.wait()             # start only after A holds the lock
                async with factory() as s:
                    p = await s.get(Panel, pid)
                    return await sync_service.sync_panel(s, p, data=good)

            await asyncio.gather(coro_a(), coro_b(), return_exceptions=True)

            async with factory() as s:
                from sqlalchemy import select
                p = await s.get(Panel, pid)
                assert p.status == PanelStatus.ok            # B's success preserved
                assert p.last_synced_at is not None
                statuses = {
                    r.status for r in (await s.execute(
                        select(SyncRun).where(SyncRun.panel_id == pid))).scalars()
                }
                assert SyncStatus.success in statuses and SyncStatus.failed in statuses  # A recorded too
                # cleanup
                from app.models import EndUserSnapshot, Reseller
                await s.execute(EndUserSnapshot.__table__.delete().where(EndUserSnapshot.panel_id == pid))
                await s.execute(SyncRun.__table__.delete().where(SyncRun.panel_id == pid))
                await s.execute(Reseller.__table__.delete().where(Reseller.panel_id == pid))
                await s.execute(Panel.__table__.delete().where(Panel.id == pid))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


# ───────────────────────── F13: poll-loop backpressure bounds live tasks ─────────────────────────
def test_f13_backpressure_bounds_live_tasks(monkeypatch):
    from app.bot.storefront import manager as M

    async def run():
        gate = asyncio.Event()
        feeding = {"n": 0}

        async def blocking_feed(dp, bot, update):  # noqa: ANN001
            feeding["n"] += 1
            await gate.wait()   # hold every handler so slots stay occupied

        monkeypatch.setattr(M, "_feed", blocking_feed)
        monkeypatch.setattr(M, "_dispatcher",
                            lambda: SimpleNamespace(resolve_used_update_types=lambda: None))

        class FakeBot:
            def __init__(self) -> None:
                self.calls = 0

            async def get_updates(self, offset=None, timeout=None, allowed_updates=None):  # noqa: ANN001
                self.calls += 1
                if self.calls == 1:
                    return [SimpleNamespace(update_id=i) for i in range(200)]  # a big first batch
                await asyncio.sleep(3600)   # block further polling
                return []

        sem = asyncio.Semaphore(M._HANDLER_CONCURRENCY)
        tasks: set[asyncio.Task[None]] = set()
        poll = asyncio.create_task(M._poll_one(FakeBot(), 1, sem, tasks))
        await asyncio.sleep(0.25)   # let it fill exactly one cap's worth of handler slots

        # The loop blocks on `await sem.acquire()` past the cap → only _HANDLER_CONCURRENCY tasks exist,
        # NOT all 200. (Before the fix, every update became a task immediately → 200 live tasks.)
        assert len(tasks) <= M._HANDLER_CONCURRENCY
        assert feeding["n"] <= M._HANDLER_CONCURRENCY

        gate.set()                  # release handlers → slots free → loop drains the rest
        await asyncio.sleep(0.25)
        poll.cancel()
        try:
            await poll
        except asyncio.CancelledError:
            pass
        for t in list(tasks):
            t.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)

    asyncio.run(run())
