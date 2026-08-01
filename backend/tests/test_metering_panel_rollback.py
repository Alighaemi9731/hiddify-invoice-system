"""Panel-rollback handling in the usage meter.

Restoring a Hiddify backup onto another server (the owner's mid-month migrations) rewinds every
user's usage counter to the backup point. Before this, the meter kept its banked consumption and
then counted the SAME GB again while the panel climbed back — surfacing on the invoice as
«مصرف مازاد بر بسته» for users who had consumed nothing extra at all (2026-07: the rewound stretch
clustered at ~4-5 GB per user regardless of package size, which is what gave it away).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.services import metering


def _snap(*, used: float, limit: float = 10.0, start=dt.date(2026, 7, 1), init: bool = True,
          owner: str = "admin-a"):
    return SimpleNamespace(
        meter_provisioned_gb=limit, meter_consumed_gb=used, meter_init=init,
        start_date=start, current_usage_gb=used, usage_limit_gb=limit, added_by_uuid=owner,
    )


def _meter():
    return SimpleNamespace(
        quota_added_gb=0.0, renew_used_gb=0.0, edit_renewal_gb=0.0,
        reset_count=0, consumed_gb=0.0, overage_gb=0.0,
    )


def _user(uuid: str, used: float, *, start=dt.date(2026, 7, 1), limit: float = 10.0,
          owner: str = "admin-a"):
    return SimpleNamespace(uuid=uuid, current_usage_gb=used, usage_limit_gb=limit,
                           start_date=start, added_by_uuid=owner)


# ---------------------------------------------------------------- detection
def test_rollback_detected_only_when_the_drop_is_panel_wide():
    existing = {f"u{i}": _snap(used=8.0, owner=f"admin-{i % 4}") for i in range(40)}
    # 12 of 40 counters rewound to 3.5 GB, across 4 resellers → past every threshold.
    users = [_user(f"u{i}", 3.5 if i < 12 else 8.0, owner=f"admin-{i % 4}") for i in range(40)]
    assert metering.detect_panel_rollback(users, existing) == 12


def test_a_few_genuine_resets_are_not_a_rollback():
    """Three resellers zeroing a customer each must stay billable abuse, not be excused."""
    existing = {f"u{i}": _snap(used=8.0, owner=f"admin-{i % 4}") for i in range(40)}
    users = [_user(f"u{i}", 0.0 if i < 3 else 8.0, owner=f"admin-{i % 4}") for i in range(40)]
    assert metering.detect_panel_rollback(users, existing) == 0


def test_one_reseller_mass_resetting_is_abuse_not_a_rollback():
    """The discriminator that matters: a restore rewinds EVERY reseller's users. A single reseller
    scripting resets across their whole customer base — exactly what this meter is for — must keep
    being billed no matter how many users it covers."""
    existing = {f"u{i}": _snap(used=8.0, owner="admin-cheat" if i < 30 else "admin-b")
                for i in range(60)}
    users = [_user(f"u{i}", 0.0 if i < 30 else 8.0,
                   owner="admin-cheat" if i < 30 else "admin-b") for i in range(60)]
    assert metering.detect_panel_rollback(users, existing) == 0


def test_renewals_do_not_look_like_a_rollback():
    """A renewal drops the counter legitimately (fresh cycle) — a busy renewal day must not be read
    as a restore, or a whole panel's real renewals would stop being metered."""
    existing = {f"u{i}": _snap(used=9.0, owner=f"admin-{i % 5}") for i in range(30)}
    users = [_user(f"u{i}", 0.2, start=dt.date(2026, 7, 20), owner=f"admin-{i % 5}")
             for i in range(30)]
    assert metering.detect_panel_rollback(users, existing) == 0


def test_first_sighting_is_not_a_drop():
    existing = {}
    users = [_user(f"u{i}", 0.0) for i in range(30)]
    assert metering.detect_panel_rollback(users, existing) == 0


# ---------------------------------------------------------------- accounting
def test_rewound_usage_is_given_back_and_not_billed_twice():
    """The core money property: after a restore, re-consuming the SAME GB costs nothing extra, and
    only genuinely new usage past the package is billed."""
    snap, meter = _snap(used=6.0), _meter()

    # Restore rewinds 6.0 → 1.5 GB. Nothing is consumed, nothing is an abuse reset…
    u1 = metering.compute(
        snapshot=snap, meter=meter, prev_limit=10.0, prev_used=6.0,
        new_limit=10.0, new_used=1.5, start_date=snap.start_date,
        added_by_uuid="a", name="n", period_label="2026-07", panel_rollback=True,
    )
    assert u1.consumed_gb == 0.0
    assert u1.overage_gb == 0.0
    assert u1.reset_count == 0
    # …and the banked consumption is rewound by the same 4.5 GB, so the climb back is free.
    assert u1.meter_consumed_gb == 1.5

    metering.write(snap, meter, u1)
    snap.current_usage_gb = 1.5

    # The panel climbs back to 6.0 (the same GB, already paid for) → still no overage.
    u2 = metering.compute(
        snapshot=snap, meter=meter, prev_limit=10.0, prev_used=1.5,
        new_limit=10.0, new_used=6.0, start_date=snap.start_date,
        added_by_uuid="a", name="n", period_label="2026-07",
    )
    assert u2.overage_gb == 0.0
    assert u2.meter_consumed_gb == 6.0

    metering.write(snap, meter, u2)
    snap.current_usage_gb = 6.0

    # Real over-consumption past the 10 GB package is STILL caught: 6 → 13 = 3 GB past the buffer.
    u3 = metering.compute(
        snapshot=snap, meter=meter, prev_limit=10.0, prev_used=6.0,
        new_limit=10.0, new_used=13.0, start_date=snap.start_date,
        added_by_uuid="a", name="n", period_label="2026-07",
    )
    assert round(u3.overage_gb, 3) == 3.0


def test_without_the_rollback_flag_a_reset_is_still_abuse():
    """The fix must not blunt the feature: an isolated zeroed counter is billed exactly as before."""
    snap, meter = _snap(used=6.0), _meter()
    u = metering.compute(
        snapshot=snap, meter=meter, prev_limit=10.0, prev_used=6.0,
        new_limit=10.0, new_used=0.0, start_date=snap.start_date,
        added_by_uuid="a", name="n", period_label="2026-07",
    )
    assert u.reset_count == 1
    assert u.meter_consumed_gb == 6.0        # banked consumption is KEPT, not handed back


def test_rollback_flag_does_not_disturb_users_whose_counter_moved_forward():
    """A restore is panel-wide, but individual users keep consuming normally during the same sync —
    their forward usage must still be measured."""
    snap, meter = _snap(used=6.0), _meter()
    u = metering.compute(
        snapshot=snap, meter=meter, prev_limit=10.0, prev_used=6.0,
        new_limit=10.0, new_used=7.0, start_date=snap.start_date,
        added_by_uuid="a", name="n", period_label="2026-07", panel_rollback=True,
    )
    assert round(u.consumed_gb, 3) == 1.0
    assert u.meter_consumed_gb == 7.0
