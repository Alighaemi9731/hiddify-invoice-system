"""Repeating scheduler jobs must not fire on the same minute.

Why this file exists: every repeating job used to share ONE epoch anchor
(`datetime(2000, 1, 1, 0, 0)`), which put them all in phase. Eight jobs fired together at
00:00, and the two heaviest — `periodic_sync` (~416 MB across three concurrent panel upserts)
and `backup` (~79 MB) — coincided four times a day, because 6h and 2h both divide 6h. Their
peaks ADD: APScheduler's AsyncIOScheduler bounds instances per job (`max_instances=1`), not
globally, so colliding jobs are concurrent asyncio tasks on one loop. With the ~105 MB import
baseline that put the scheduler at ~666 MB of its 768 MB cap — the shape of the Jul 2026 OOM
kills, which the v1.105.0 backup fix masked (it removed ~400 MB from the backup side of the
sum) without removing the collision itself.

The fix is a phase offset per job, so the guard has to be structural: replay the REAL triggers
`register()` builds over a day of wall clock and assert nothing heavy lands on the same minute.
It fails if someone reuses an anchor, and it fails if someone converts these back to cron.
"""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sched.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

from app.scheduler import jobs  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))  # Asia/Tehran, no DST since 2022

# Two jobs are exempt from the no-shared-minute rule because their cadence makes sharing
# unavoidable, and both are trivially small:
#   • storefront_delivery  — 1-minute cadence (durable broadcast/DM worker) → every minute.
#   • scheduler_heartbeat  — fixed 2-minute cadence → half of all minutes. One settings upsert
#     (~1 MB) and deliberately NOT owner-configurable, because /health reads it to tell a dead
#     scheduler from a healthy one. Staggering it would buy nothing and weaken that signal.
UBIQUITOUS = {"storefront_delivery", "scheduler_heartbeat"}


class _StubScheduler:
    """Captures what register() would put in the jobstore, without a running scheduler."""

    timezone = TZ

    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}

    def add_job(self, func, trigger, *, id, **kwargs):  # noqa: A002, ANN001, ARG002
        self.jobs[id] = trigger


def _registered() -> dict[str, object]:
    sched = _StubScheduler()
    jobs.register(sched)
    return sched.jobs


def _fire_minutes(trigger, day: dt.datetime) -> set[dt.datetime]:
    """Every minute in `day` on which this trigger fires.

    `previous_fire_time` must stay None: IntervalTrigger short-circuits to
    `previous_fire_time + interval` when it is given, which would enumerate from the walk
    cursor instead of from the trigger's own epoch anchor — exactly the phase we are testing.
    Passing (None, cursor) asks the real question: "what is the first fire at or after this
    instant, given your anchor?"
    """
    out: set[dt.datetime] = set()
    cursor = day
    end = day + dt.timedelta(days=1)
    while cursor < end:
        nxt = trigger.get_next_fire_time(None, cursor)
        if nxt is None or nxt >= end:
            break
        out.add(nxt.replace(second=0, microsecond=0))
        cursor = nxt + dt.timedelta(minutes=1)
    return out


def _interval_jobs() -> dict[str, object]:
    return {k: v for k, v in _registered().items() if isinstance(v, IntervalTrigger)}


def test_sync_and_backup_never_fire_together():
    """The two heavyweights are the whole point: their peaks must never add."""
    regs = _interval_jobs()
    day = dt.datetime(2026, 8, 10, 0, 0, tzinfo=TZ)
    sync = _fire_minutes(regs["periodic_sync"], day)
    backup = _fire_minutes(regs["backup"], day)
    assert sync and backup, "both jobs must actually fire within the sampled day"
    overlap = sync & backup
    assert not overlap, (
        "periodic_sync (~416 MB) and backup (~79 MB) fire on the same minute at "
        f"{sorted(t.strftime('%H:%M') for t in overlap)} — their memory peaks add, which is "
        "what put the scheduler at ~87% of its cap and OOM-killed it in Jul 2026"
    )


def test_no_two_repeating_jobs_share_a_firing_minute():
    """A shared anchor is the failure mode; assert the phases stay spread."""
    regs = {k: v for k, v in _interval_jobs().items() if k not in UBIQUITOUS}
    day = dt.datetime(2026, 8, 10, 0, 0, tzinfo=TZ)
    fires = {name: _fire_minutes(trigger, day) for name, trigger in regs.items()}

    collisions: list[str] = []
    names = sorted(fires)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = fires[a] & fires[b]
            if both:
                when = sorted(t.strftime("%H:%M") for t in both)[:3]
                collisions.append(f"{a} + {b} at {when}")
    assert not collisions, (
        "repeating jobs share a firing minute (an anchor offset was reused or dropped):\n  "
        + "\n  ".join(collisions)
    )


def test_every_repeating_job_has_a_distinct_anchor_offset():
    """The offsets themselves — a duplicate here is the bug, even if the cadences happen to
    hide it today (an owner can change any cadence from the panel)."""
    regs = _interval_jobs()
    offsets: dict[int, list[str]] = {}
    for name, trigger in regs.items():
        if name in UBIQUITOUS:
            continue
        start = trigger.start_date
        offsets.setdefault(start.hour * 60 + start.minute, []).append(name)
    dupes = {off: names for off, names in offsets.items() if len(names) > 1}
    assert not dupes, f"these jobs share an anchor offset: {dupes}"


def test_repeating_jobs_keep_a_fixed_epoch_anchor():
    """The original invariant: a BARE interval re-anchors to process start, so a frequent
    redeploy starves the job — that is why the 2-hourly backup once never fired."""
    for name, trigger in _interval_jobs().items():
        assert trigger.start_date is not None, f"{name} has no fixed anchor"
        assert trigger.start_date.year == 2000, (
            f"{name} is anchored at {trigger.start_date}, not the fixed epoch — it will "
            "re-anchor to process start"
        )


def test_calendar_jobs_stay_cron():
    """Monthly invoicing and the daily jobs are calendar events and must not drift to interval."""
    regs = _registered()
    for name in ("monthly_invoicing", "daily_dunning", "daily_maintenance",
                 "daily_digest", "storefront_expiry"):
        assert isinstance(regs[name], CronTrigger), f"{name} must stay a CronTrigger"


@pytest.mark.parametrize("sync_hours,backup_hours", [(1, 1), (2, 2), (3, 2), (6, 2), (12, 4), (23, 23)])
def test_sync_and_backup_stay_apart_at_every_owner_cadence(sync_hours, backup_hours):
    """Cadences are owner-editable, so the separation must hold for any pair — not just the
    defaults. Both are whole-hour intervals, so a 25-minute phase gap can never close."""
    cfg = jobs.ScheduleConfig(sync_hours=sync_hours, backup_hours=backup_hours)
    sched = _StubScheduler()
    jobs.register(sched, cfg)
    day = dt.datetime(2026, 8, 10, 0, 0, tzinfo=TZ)
    overlap = _fire_minutes(sched.jobs["periodic_sync"], day) & _fire_minutes(
        sched.jobs["backup"], day)
    assert not overlap, f"sync={sync_hours}h backup={backup_hours}h collide at {sorted(overlap)}"
