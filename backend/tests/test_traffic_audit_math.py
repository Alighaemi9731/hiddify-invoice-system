"""The traffic audit's classifier and response parser — pure, no database.

This is where the real coverage lives. The service around it is I/O; the decision to put a red flag
next to a named reseller is made entirely by the four functions exercised here, so every boundary
they have is pinned: the byte divisor, the string-typed JSON, the zero denominators, and the exact
threshold comparison.

Numbers in the production fixtures are the measured ones from the incident that motivated the
feature (panel s7, 2026-08) and from the fleet-wide baseline scan of 366 active resellers.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/trafmath.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402

from app.services import traffic_audit as ta  # noqa: E402

T = ta.load_thresholds_from(
    {"traffic_audit_ratio_threshold": 2.0, "traffic_audit_min_gb_30d": 50}
)


# ---------------------------------------------------------------- the divisor
def test_bytes_are_gibibytes_not_gigabytes():
    """1024**3, not 10**9.

    This single assertion is what stands between the feature and a silent 7.4% inflation of every
    ratio. The literal and the expected value are both from a live panel read that was verified
    against the panel's own SQL (`alyas`, 95.71 GB).
    """
    stats = ta.parse_usage_history(
        {"yesterday": {"usage": "102763943759", "online": "39"}, "last_30_days": {"usage": 0}}
    )
    assert stats.yesterday_gb == pytest.approx(95.71, abs=0.01)


# ------------------------------------------------------ string-typed JSON
def test_usage_history_numbers_arrive_as_strings():
    """Hiddify serialises these as JSON strings. A naive read concatenates or raises."""
    stats = ta.parse_usage_history({
        "yesterday": {"usage": "102763943759", "online": "39"},
        "last_30_days": {"usage": "995608792547", "online": 54},
        "total": {"usage": "0", "online": 54, "users": "55"},
    })
    assert isinstance(stats.yesterday_online, int) and stats.yesterday_online == 39
    assert isinstance(stats.total_users, int) and stats.total_users == 55
    assert stats.last_30d_gb == pytest.approx(927.23, abs=0.01)


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), ("", 0), ("abc", 0), ("39", 39), (39, 39), (39.7, 39), ([], 0), ({}, 0)],
)
def test_as_int_never_raises(value, expected):
    """A malformed panel value maps to a default, never an exception — the repo's standing rule for
    panel data. A reseller we could not measure must not look like a big one."""
    assert ta._as_int(value) == expected


@pytest.mark.parametrize("payload", [None, {}, [], "nope", {"yesterday": "not-a-dict"}])
def test_parse_rejects_unusable_payloads(payload):
    if payload == {}:
        # An empty dict is structurally valid and parses to all-zeros; everything else is refused.
        assert ta.parse_usage_history(payload) is not None
        return
    assert ta.parse_usage_history(payload) is None


def test_h24_and_m5_are_ignored():
    """Both are hardcoded 0 upstream — reading them would report a busy reseller as idle."""
    stats = ta.parse_usage_history({
        "yesterday": {"usage": "1073741824", "online": 1},
        "last_30_days": {"usage": "1073741824"},
        "h24": {"usage": 0, "online": 99}, "m5": {"usage": 0, "online": 99},
    })
    assert stats.yesterday_gb == 1.0


# ---------------------------------------------------------------- the ratio
def test_ratio_none_when_no_quota_sold():
    """None, not 0 and not infinity: there is no ceiling to divide by."""
    assert ta.compute_ratio(100.0, 0) is None
    assert ta.compute_ratio(100.0, -5) is None


def test_production_incident_reproduces():
    """The reseller that motivated the feature: 9,647 GB moved against 1,100 GB ever sold."""
    ratio = ta.compute_ratio(9647, 1100)
    assert ratio == pytest.approx(8.77, abs=0.01)
    assert ta.is_flagged(ratio=ratio, traffic_30d_gb=9647, thresholds=T)


def test_highest_legitimate_reseller_is_not_flagged():
    """The worst legitimate ratio on the same panel was 0.90. Fleet-wide, across 366 active
    resellers, NOTHING was above 2.0 — the baseline this threshold is calibrated to."""
    ratio = ta.compute_ratio(35.8, 40.0)
    assert ratio == pytest.approx(0.895, abs=0.001)
    assert not ta.is_flagged(ratio=ratio, traffic_30d_gb=35.8, thresholds=T)


def test_threshold_is_inclusive():
    """`>=`, pinned. A refactor that makes this exclusive changes who gets accused."""
    assert ta.is_flagged(ratio=2.0, traffic_30d_gb=50.0, thresholds=T)
    assert not ta.is_flagged(ratio=1.999, traffic_30d_gb=50.0, thresholds=T)


def test_volume_floor_is_inclusive_and_gates_everything():
    """The only fleet-wide outlier above 1.5 was 45.4 GB across 3 users — noise the floor removes.
    The floor is checked BEFORE the ratio, so it also gates the no-quota arm."""
    assert not ta.is_flagged(ratio=1.51, traffic_30d_gb=45.4, thresholds=T)
    assert ta.is_flagged(ratio=2.0, traffic_30d_gb=50.0, thresholds=T)
    assert not ta.is_flagged(ratio=None, traffic_30d_gb=49.9, thresholds=T)


def test_no_quota_with_real_traffic_is_flagged():
    """The arm that must not fall through the None guard.

    Real traffic against zero sold quota is an infinite ratio, and it is exactly the state an abuser
    ends in after deleting the configs. Treating it as "no data" would put the worst offender in the
    quietest row.
    """
    assert ta.is_flagged(ratio=None, traffic_30d_gb=500.0, thresholds=T)
    assert not ta.is_flagged(ratio=None, traffic_30d_gb=5.0, thresholds=T)


# ------------------------------------------------------- displayed-only metrics
def test_gb_per_user_day_handles_nobody_online():
    """None, not a ZeroDivisionError and not 0."""
    assert ta.gb_per_user_day(10.0, 0) is None
    assert ta.gb_per_user_day(865.22, 23) == pytest.approx(37.62, abs=0.01)


def test_counter_ratio_is_the_reset_evidence():
    """The panel logged 9,647 GB while the same users' own counters summed to 328 — 29×. This is
    the number that proves a reset, where the quota ratio only suggests one."""
    row = ta.ResellerTraffic(
        panel_id=1, panel_key="s7", reseller_id=1, reseller_name="X", admin_uuid="u",
        sub_count=0, last_30d_gb=9647, counter_gb=328, quota_gb=1100,
        ratio=ta.compute_ratio(9647, 1100),
    )
    d = row.as_dict()
    assert d["counter_ratio"] == pytest.approx(29.4, abs=0.1)
    assert d["ratio"] == pytest.approx(8.77, abs=0.01)


def test_counter_ratio_none_when_no_counters():
    row = ta.ResellerTraffic(
        panel_id=1, panel_key="s7", reseller_id=1, reseller_name="X", admin_uuid="u",
        sub_count=0, last_30d_gb=100, counter_gb=0, quota_gb=50,
    )
    assert row.as_dict()["counter_ratio"] is None


def test_unreachable_row_carries_no_numbers():
    """An unreachable reseller must never look like a quiet one."""
    row = ta.ResellerTraffic(
        panel_id=1, panel_key="s7", reseller_id=1, reseller_name="X", admin_uuid="u",
        sub_count=0, reachable=False,
    )
    d = row.as_dict()
    assert d["reachable"] is False
    assert d["flagged"] is False
    assert d["ratio"] is None
