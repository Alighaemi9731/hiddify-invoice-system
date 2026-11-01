"""Separate-process settings changes, targeted rescheduling, and durable missed-run recovery."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.settings import schedule_status
from app.core import db
from app.core.db import Base
from app.models import MonthlyBillingRun
from app.scheduler import jobs, scheduler
from app.services import monthly_billing, settings_service
from app.services.invoicing import GenerationSummary

NOW = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)


async def database(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False), url


def services(monkeypatch, *, fail=False):
    sync = AsyncMock()
    generate = AsyncMock(return_value=GenerationSummary(period="2026-10", created=2))
    send = AsyncMock(return_value={"sent": 2, "failed": int(fail), "unmatched": 0})
    notify = AsyncMock()
    monkeypatch.setattr(monthly_billing.sync_service, "sync_all", sync)
    monkeypatch.setattr(monthly_billing.invoicing, "generate_invoices", generate)
    monkeypatch.setattr(monthly_billing.delivery, "send_period", send)
    monkeypatch.setattr(monthly_billing.owner_notify, "notify_owner", notify)
    return sync, generate, send, notify


def test_api_save_in_another_process_reconciles_4_to_1_and_back(tmp_path, monkeypatch):
    async def run():
        engine, factory, url = await database(tmp_path)
        sched = AsyncIOScheduler(timezone="Asia/Tehran")
        jobs.register(sched, jobs.ScheduleConfig(invoice_day=4, sync_hours=1))
        sched.start(paused=True)
        monkeypatch.setattr(scheduler, "_scheduler", sched)
        try:
            sync_job = sched.get_job("periodic_sync")
            sync_next = sync_job.next_run_time
            for day in (1, 4, 1):
                child = '''
import asyncio
from app.core.db import SessionLocal, engine
from app.api.settings import update_bulk
from app.schemas.setting import SettingsBulkUpdate
async def main():
    async with SessionLocal() as session:
        result = await update_bulk(SettingsBulkUpdate(items=[{"key":"invoice_day_of_month", "value": DAY}]), session)
        assert result["status"] == "ok"
    await engine.dispose()
asyncio.run(main())
'''.replace("DAY", str(day))
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", child, cwd=Path(__file__).parents[1],
                    env={**os.environ, "DATABASE_URL": url, "SECRET_KEY": "test-key", "RUN_SCHEDULER": "false"},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                assert proc.returncode == 0, (stdout, stderr)
                settings_service.clear_settings_cache()  # In production, the 15s poll exceeds the 5s TTL.
                async with factory() as session:
                    assert await scheduler.apply_settings(session)
                    state = await schedule_status(session)
                assert state["status"] == "applied"
                assert state["desired"]["invoice_day"] == day
                assert f"day='{day}'" in str(sched.get_job("monthly_invoicing").trigger)
                assert sched.get_job("periodic_sync") is sync_job
                assert sync_job.next_run_time == sync_next
            monthly = sched.get_job("monthly_invoicing")
            next_run = monthly.next_run_time
            async with factory() as session:
                await scheduler.apply_settings(session)
            assert monthly.next_run_time == next_run
        finally:
            sched.shutdown(wait=False)
            await engine.dispose()
    asyncio.run(run())


def test_unchanged_poll_preserves_an_overdue_pending_fire():
    async def run():
        sched = AsyncIOScheduler(timezone="Asia/Tehran")
        cfg = jobs.ScheduleConfig()
        jobs.register(sched, cfg)
        sched.start(paused=True)
        try:
            overdue = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
            sched.modify_job("monthly_invoicing", next_run_time=overdue)
            assert jobs.register(sched, cfg, update_existing=True) == []
            assert sched.get_job("monthly_invoicing").next_run_time == overdue
        finally:
            sched.shutdown(wait=False)
    asyncio.run(run())


def test_running_worker_picks_up_changes_without_api_callback(tmp_path, monkeypatch):
    """Exercise the real fixed 15-second reconciliation job, with all business jobs stubbed."""
    async def run():
        engine, factory, _ = await database(tmp_path)
        monkeypatch.setattr(db, "SessionLocal", factory)
        monkeypatch.setattr(scheduler, "_scheduler", None)
        for name in vars(jobs):
            if name.endswith("_job") and callable(getattr(jobs, name)):
                monkeypatch.setattr(jobs, name, AsyncMock())
        try:
            await scheduler.start()
            sched = scheduler.get_scheduler()
            assert sched.get_job("schedule_refresh").trigger.interval.total_seconds() == 15
            async with factory() as session:
                await settings_service.set_value(session, "invoice_day_of_month", 4)
                assert (await schedule_status(session))["status"] == "pending"
            async def applied():
                while True:
                    async with factory() as session:
                        state = await schedule_status(session)
                    if state["status"] == "applied" and state["active"]["invoice_day"] == 4:
                        return
                    await asyncio.sleep(0.25)
            await asyncio.wait_for(applied(), 18)
            assert "day='4'" in str(sched.get_job("monthly_invoicing").trigger)
        finally:
            await scheduler.shutdown()
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("now,day,expected", [
    (NOW.replace(hour=5), 1, "not_due"),  # 08:30 Tehran
    (NOW, 4, "not_due"),
    (NOW, 1, "completed"),
    (NOW.replace(day=5), 4, "completed"),
])
def test_recovery_obeys_saved_tehran_deadline(tmp_path, monkeypatch, now, day, expected):
    sync, generate, send, _ = services(monkeypatch)
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as session:
                await settings_service.set_value(session, "invoice_day_of_month", day)
                result = await monthly_billing.run_due(session, now=now)
                assert result["status"] == expected
                assert generate.await_count == int(expected == "completed")
                if expected == "completed":
                    assert generate.call_args.args[1].label == "2026-10"
                    assert send.call_args.args[1] == "2026-10"
                    assert (await monthly_billing.run_due(session, now=now))["status"] == "completed"
                    assert sync.await_count == 1
            # A new worker/session sees the persisted completion rather than replaying.
            async with factory() as session:
                await monthly_billing.run_due(session, now=now)
                assert generate.await_count == int(expected == "completed")
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_partial_delivery_retries_with_backoff_then_stops(tmp_path, monkeypatch):
    _, _, send, _ = services(monkeypatch, fail=True)
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as session:
                assert (await monthly_billing.run_due(session, now=NOW))["status"] == "retry"
                assert (await monthly_billing.run_due(session, now=NOW + dt.timedelta(minutes=1)))["status"] == "retry_wait"
                assert send.await_count == 1
                assert (await monthly_billing.run_due(session, now=NOW + dt.timedelta(minutes=5)))["status"] == "retry"
                assert (await monthly_billing.run_due(session, now=NOW + dt.timedelta(minutes=10)))["status"] == "failed"
                await monthly_billing.run_due(session, now=NOW + dt.timedelta(hours=1))
                assert send.await_count == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_crash_resume_and_notification_failure_do_not_repeat_completed_cycle(tmp_path, monkeypatch):
    _, generate, send, notify = services(monkeypatch)
    notify.side_effect = RuntimeError("telegram offline")
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as session:
                session.add(MonthlyBillingRun(period_start=dt.date(2026, 10, 1), status="running", attempts=1))
                await session.commit()
                assert (await monthly_billing.run_due(session, now=NOW))["status"] == "completed"
            async with factory() as session:
                await monthly_billing.run_due(session, now=NOW)
                assert generate.await_count == send.await_count == 1
                assert (await session.get(MonthlyBillingRun, dt.date(2026, 10, 1))).attempts == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_generation_exception_rolls_back_and_recovers_after_restart(tmp_path, monkeypatch):
    _, generate, send, _ = services(monkeypatch)
    generate.side_effect = [RuntimeError("temporary failure"), GenerationSummary(period="2026-10")]
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as session:
                assert (await monthly_billing.run_due(session, now=NOW))["status"] == "retry"
                assert send.await_count == 0
            async with factory() as session:
                assert (await monthly_billing.run_due(session, now=NOW + dt.timedelta(minutes=5)))["status"] == "completed"
                assert send.await_count == 1
                await settings_service.set_value(session, "invoice_day_of_month", 4)
                assert (await monthly_billing.run_due(session, now=NOW.replace(day=5)))["status"] == "completed"
                assert send.await_count == 1  # Editing the day cannot repeat a completed cycle.
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_status_distinguishes_pending_stale_and_applied(tmp_path):
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as session:
                assert (await schedule_status(session))["status"] == "unavailable"
                cfg = asdict(await jobs.load_config(session))
                state = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "config": {**cfg, "invoice_day": 4}}
                await settings_service.set_value(session, "scheduler_runtime_state", state)
                assert (await schedule_status(session))["status"] == "pending"
                state = {**state, "config": cfg}
                await settings_service.set_value(session, "scheduler_runtime_state", state)
                assert (await schedule_status(session))["status"] == "applied"
                state = {**state, "checked_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat()}
                await settings_service.set_value(session, "scheduler_runtime_state", state)
                assert (await schedule_status(session))["status"] == "unavailable"
                assert "scheduler_runtime_state" not in {r["key"] for r in await settings_service.all_for_api(session)}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_configuration_read_bypasses_cross_process_ttl_and_identity_map(tmp_path):
    async def run():
        engine, factory, _ = await database(tmp_path)
        try:
            async with factory() as old:
                await settings_service.set_value(old, "invoice_day_of_month", 1)
                assert (await jobs.load_config(old)).invoice_day == 1
                async with factory() as other:
                    # A raw write simulates an API process with a DIFFERENT local cache.
                    from sqlalchemy import text
                    await other.execute(text("UPDATE settings SET value='4' WHERE key='invoice_day_of_month'"))
                    await other.commit()
                assert (await jobs.load_config(old)).invoice_day == 4
        finally:
            await engine.dispose()
    asyncio.run(run())
