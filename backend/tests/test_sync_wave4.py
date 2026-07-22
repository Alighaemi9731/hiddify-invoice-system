"""Wave 4 (B10) sync-pipeline contracts.

Locks in the restructured lifecycle:
1. The panel fetch happens with NO open database transaction (it used to hold a pooled
   connection idle-in-transaction for up to the 45–90 s panel timeout).
2. The superseded guard: with fetch-outside-lock, an attempt whose data predates a
   concurrent committed sync must NOT write (its stamp would mark older data as newer);
   the panel's ok status must survive.
3. `sync_all` fans out with bounded concurrency, one session per panel; one failing
   panel never blocks or poisons the others.
"""
import asyncio
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import Panel, SyncRun
from app.models.enums import PanelStatus, SyncStatus
from app.services import sync as sync_mod
from app.services.panel_client import PanelData


async def _mk(tmp_path, name):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _panel(key: str) -> Panel:
    return Panel(key=key, host=f"{key}.invalid", proxy_path_enc="x", owner_uuid=f"o-{key}",
                 enabled=True, status=PanelStatus.unknown)


def test_fetch_runs_outside_transaction(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "fetch.db")
        try:
            async with Session() as s:
                p = _panel("a")
                s.add(p)
                await s.commit()

                seen = {}

                class Probe:
                    async def fetch_backup(self, panel):  # noqa: ANN001
                        seen["in_txn"] = s.in_transaction()
                        return PanelData(admins=[], users=[])

                await sync_mod.sync_panel(s, p, client=Probe())
                assert seen["in_txn"] is False, (
                    "fetch_backup must run BEFORE the write transaction opens"
                )
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_superseded_attempt_does_not_regress(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "sup.db")
        try:
            async with Session() as s:
                p = _panel("a")
                # a concurrent sync "already committed" strictly in the future of any
                # attempt that starts now
                p.status = PanelStatus.ok
                p.last_synced_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
                s.add(p)
                await s.commit()
                marker = p.last_synced_at

                run_row = await sync_mod.sync_panel(s, p, data=PanelData(admins=[], users=[]))
                assert run_row.status == SyncStatus.failed
                assert "superseded" in (run_row.error or "")
                await s.refresh(p)
                assert p.status == PanelStatus.ok          # never downgraded
                got = p.last_synced_at
                if got.tzinfo is None:  # SQLite returns naive; stored as UTC
                    got = got.replace(tzinfo=dt.timezone.utc)
                assert got == marker                        # newer success kept
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sync_all_bounded_parallel_and_fault_isolated(tmp_path, monkeypatch):
    async def run():
        engine, Session = await _mk(tmp_path, "all.db")
        try:
            async with Session() as seed:
                for i in range(5):
                    seed.add(_panel(f"p{i}"))
                await seed.commit()

            # sync_all opens its own sessions — point it at THIS test database.
            monkeypatch.setattr(sync_mod, "SessionLocal", Session)

            state = {"cur": 0, "max": 0}

            class StubClient:
                async def fetch_backup(self, panel):  # noqa: ANN001
                    state["cur"] += 1
                    state["max"] = max(state["max"], state["cur"])
                    await asyncio.sleep(0.05)
                    try:
                        if panel.key == "p2":
                            raise RuntimeError("panel down")
                        return PanelData(admins=[], users=[])
                    finally:
                        state["cur"] -= 1

            monkeypatch.setattr(sync_mod, "BackupJsonClient", StubClient)

            async with Session() as s:
                runs = await sync_mod.sync_all(s)
                # every panel produced a run row (failure included — fault isolated)
                assert len(runs) == 5
                assert sum(1 for r in runs if r.status == SyncStatus.success) == 4
                assert sum(1 for r in runs if r.status == SyncStatus.failed) == 1
                assert state["max"] <= sync_mod._SYNC_ALL_CONCURRENCY
                panels = (await s.execute(select(Panel))).scalars().all()
                by_key = {p.key: p for p in panels}
                assert by_key["p2"].status == PanelStatus.error
                assert all(by_key[f"p{i}"].status == PanelStatus.ok for i in (0, 1, 3, 4))
                # audit rows persisted for every attempt
                n_runs = len((await s.execute(select(SyncRun))).scalars().all())
                assert n_runs == 5
        finally:
            await engine.dispose()
    asyncio.run(run())
