"""Scheduler timing is owner-configurable and preserves true interval spacing."""
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.api.settings import _SCHEDULE_KEYS
from app.scheduler.jobs import ScheduleConfig, _clamp, register


def test_clamp_ranges_and_bad_values():
    assert _clamp(3, 1, 24, 2) == 3
    assert _clamp(999, 1, 24, 2) == 24      # above max -> max
    assert _clamp(0, 1, 24, 2) == 1         # below min -> min
    assert _clamp("7", 0, 23, 9) == 7       # numeric string ok
    assert _clamp(None, 1, 24, 2) == 2      # missing -> default
    assert _clamp("oops", 1, 60, 10) == 10  # unparseable -> default


def test_live_reschedule_includes_rate_refresh():
    assert "rate_refresh_hours" in _SCHEDULE_KEYS


def _jobs(cfg):
    s = AsyncIOScheduler(timezone="Asia/Tehran")
    register(s, cfg)
    return {j.id: j for j in s.get_jobs()}


def _triggers(cfg):
    return {job_id: str(job.trigger) for job_id, job in _jobs(cfg).items()}


def test_register_uses_configured_timings():
    t = _triggers(ScheduleConfig(
        invoice_day=2, invoice_hour=7, dunning_hour=8,
        sync_hours=4, guard_minutes=15, backup_hours=3,
    ))
    assert "day='2'" in t["monthly_invoicing"] and "hour='7'" in t["monthly_invoicing"]
    assert "hour='8'" in t["daily_dunning"]
    assert t["channel_guard"] == "interval[0:15:00]"
    assert t["periodic_sync"] == "interval[4:00:00]"
    assert t["backup"] == "interval[3:00:00]"


def test_register_defaults_when_no_config():
    t = _triggers(None)
    assert t["backup"] == "interval[2:00:00]"
    assert t["periodic_sync"] == "interval[6:00:00]"
    assert t["channel_guard"] == "interval[0:10:00]"
    assert t["rate_refresh"] == "interval[1:00:00]"
    assert len(t) == 6


def test_non_divisor_interval_keeps_true_spacing_across_boundaries():
    jobs = _jobs(ScheduleConfig(sync_hours=7, backup_hours=23, guard_minutes=17))
    now = datetime(2026, 6, 10, 23, 58, tzinfo=ZoneInfo("Asia/Tehran"))
    for job_id, seconds in {
        "periodic_sync": 7 * 3600,
        "backup": 23 * 3600,
        "channel_guard": 17 * 60,
    }.items():
        trigger = jobs[job_id].trigger
        assert isinstance(trigger, IntervalTrigger)
        first = trigger.get_next_fire_time(None, now)
        second = trigger.get_next_fire_time(first, first)
        assert first is not None and second is not None
        assert (second - first).total_seconds() == seconds


def test_full_day_and_hour_boundaries_are_valid():
    t = _triggers(ScheduleConfig(sync_hours=24, backup_hours=24, guard_minutes=60, rate_hours=24))
    assert t["periodic_sync"] == "interval[1 day, 0:00:00]"
    assert t["backup"] == "interval[1 day, 0:00:00]"
    assert t["channel_guard"] == "interval[1:00:00]"
    assert t["rate_refresh"] == "interval[1 day, 0:00:00]"
