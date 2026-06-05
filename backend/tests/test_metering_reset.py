"""Regression tests for the metering reset/renewal logic (anti-fraud).

`metering.apply()` is pure (no DB), so we drive it with simple namespace objects that
stand in for the EndUserSnapshot (running state) and the monthly UsageMeter bucket.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.services.metering import apply

PERIOD = "2026-06"
MAY1 = dt.date(2026, 5, 1)
JUNE1 = dt.date(2026, 6, 1)


def _snap(**kw):
    d = dict(meter_init=True, meter_provisioned_gb=0.0, meter_consumed_gb=0.0, start_date=None)
    d.update(kw)
    return SimpleNamespace(**d)


def _meter(**kw):
    d = dict(added_by_uuid=None, name="", quota_added_gb=0.0, edit_renewal_gb=0.0,
             consumed_gb=0.0, overage_gb=0.0, reset_count=0)
    d.update(kw)
    return SimpleNamespace(**d)


def _apply(snap, m, *, prev_limit, prev_used, new_limit, new_used, start_date):
    apply(snapshot=snap, meter=m, prev_limit=prev_limit, prev_used=prev_used,
          new_limit=new_limit, new_used=new_used, start_date=start_date,
          added_by_uuid="a", name="u", period_label=PERIOD)


def test_drop_to_near_zero_is_a_reset_and_feeds_overage():
    # provisioned 10, consumed 9, start_date last month (so it's not re-baselined).
    snap = _snap(meter_provisioned_gb=10.0, meter_consumed_gb=9.0, start_date=MAY1)
    m = _meter()
    # usage drops 9 -> 0.5 with start_date UNCHANGED → renew-volume reset.
    _apply(snap, m, prev_limit=10, prev_used=9.0, new_limit=10, new_used=0.5, start_date=MAY1)
    assert m.reset_count == 1
    # consume again past the buffer → overage is now billed.
    _apply(snap, m, prev_limit=10, prev_used=0.5, new_limit=10, new_used=8.0, start_date=MAY1)
    assert m.overage_gb > 0


def test_legit_drop_not_to_zero_is_not_a_reset():
    # #7: an 80 -> 60 panel stat-correction must NOT be read as a reset (no false overage).
    snap = _snap(meter_provisioned_gb=100.0, meter_consumed_gb=80.0, start_date=MAY1)
    m = _meter()
    _apply(snap, m, prev_limit=100, prev_used=80.0, new_limit=100, new_used=60.0, start_date=MAY1)
    assert m.reset_count == 0
    assert m.overage_gb == 0
    assert snap.meter_consumed_gb == 80.0   # dc == 0, running consumption untouched


def test_renew_day_clears_edit_renewal_no_double_count():
    # #3: an earlier renew-by-edit, then a proper renew-day, must not double-bill the quota.
    snap = _snap(meter_provisioned_gb=5.0, meter_consumed_gb=0.0, start_date=MAY1)
    m = _meter()
    # renew-by-edit: quota 5 -> 15, start_date stays May → edit_renewal += 10.
    _apply(snap, m, prev_limit=5, prev_used=0.0, new_limit=15, new_used=0.0, start_date=MAY1)
    assert m.edit_renewal_gb == 10.0
    # proper renew-day: start_date advances to June → base rule will bill the full new quota.
    _apply(snap, m, prev_limit=15, prev_used=0.0, new_limit=20, new_used=0.0, start_date=JUNE1)
    assert m.edit_renewal_gb == 0.0   # cleared → the 20 GB is billed once (by the start_date rule)
