"""Scheduler timing is owner-configurable: clamping + cron-trigger construction."""
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler.jobs import ScheduleConfig, _clamp, register


def test_clamp_ranges_and_bad_values():
    assert _clamp(3, 1, 24, 2) == 3
    assert _clamp(999, 1, 24, 2) == 24      # above max -> max
    assert _clamp(0, 1, 24, 2) == 1         # below min -> min
    assert _clamp("7", 0, 23, 9) == 7       # numeric string ok
    assert _clamp(None, 1, 24, 2) == 2      # missing -> default
    assert _clamp("oops", 1, 60, 10) == 10  # unparseable -> default


def _triggers(cfg):
    s = AsyncIOScheduler(timezone="Asia/Tehran")
    register(s, cfg)
    return {j.id: str(j.trigger) for j in s.get_jobs()}


def test_register_uses_configured_timings():
    t = _triggers(ScheduleConfig(
        invoice_day=2, invoice_hour=7, dunning_hour=8,
        sync_hours=4, guard_minutes=15, backup_hours=3,
    ))
    assert "day='2'" in t["monthly_invoicing"] and "hour='7'" in t["monthly_invoicing"]
    assert "hour='8'" in t["daily_dunning"]
    assert "minute='*/15'" in t["channel_guard"]
    assert "hour='*/4'" in t["periodic_sync"]
    assert "hour='*/3'" in t["backup"]


def test_register_defaults_when_no_config():
    t = _triggers(None)
    assert "hour='*/2'" in t["backup"]       # default 2h
    assert "hour='*/6'" in t["periodic_sync"]  # default 6h
    assert "minute='*/10'" in t["channel_guard"]
    assert len(t) == 5


def test_boundary_interval_builds_valid_trigger():
    # The clamp caps hour-intervals at 23 because '*/24' is an INVALID cron field. The max
    # allowed value must register without raising; one past it must be rejected by cron.
    import pytest
    from apscheduler.triggers.cron import CronTrigger

    t = _triggers(ScheduleConfig(sync_hours=23, backup_hours=23, guard_minutes=59))
    assert "hour='*/23'" in t["periodic_sync"] and "hour='*/23'" in t["backup"]
    assert "minute='*/59'" in t["channel_guard"]
    with pytest.raises(ValueError):
        CronTrigger(hour="*/24")  # documents why load_config caps at 23, not 24
