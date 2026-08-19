"""Segment purity for the reseller follow-up board.

The whole point of the board is that a reseller appears in exactly ONE bucket — the owner's
complaint was that the same admin kept resurfacing in overlapping lists. `crm.classify` is a
priority ladder, and these tests pin both the ladder's order and the guards that stop it from
producing nonsense (partial-month projections, an empty baseline, a wiped `created_at`).

Pure classification only — no database.
"""
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/crmseg.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402

from app.services import crm  # noqa: E402

T = crm.Thresholds()  # 14 / 45 / 14 / 30 / 50% / 125%
FULL_MONTH = 20  # elapsed days — well past MIN_ELAPSED_DAYS_FOR_TREND


def _m(**kw) -> crm.RootMetrics:
    """A plain healthy reseller; every test perturbs one axis of it."""
    base = dict(
        reseller_id=1, panel_id=1, panel_key="p1", name="R", admin_uuid="u1",
        enforcement_state="active", bot_chat_id=100, sub_reseller_count=0,
        ever_sold=True, days_since_last_sale=1, account_age_days=400,
    )
    base.update(kw)
    return crm.RootMetrics(**base)


def _classify(m, elapsed=FULL_MONTH, t=T):
    return crm.classify(m, t, elapsed_days=elapsed)


# ---------------------------------------------------------------- the ladder
def test_priority_order_first_match_wins():
    """A suspended reseller who ALSO owes money and ALSO stopped selling is `suspended` only —
    this single assertion is what keeps one admin out of three lists."""
    worst = _m(
        enforcement_state="enforced", has_due_debt=True, outstanding_toman=500_000,
        ever_sold=False, days_since_last_sale=900, account_age_days=900,
    )
    assert _classify(worst) == "suspended"
    assert _classify(_m(enforcement_state="frozen", has_due_debt=True)) == "frozen"
    assert _classify(_m(has_due_debt=True, days_since_last_sale=900)) == "debtor"


def test_no_bot_link_outranks_every_lifecycle_bucket_but_not_the_money_ones():
    """A reseller who never gave the bot their panel link cannot be DMed at all, and every
    other bucket's ready-made text tells them to look in the bot. So «وصل‌نشده به ربات» wins
    over never_active / onboarding / churned / dormant / the trend rules — but stays BELOW
    suspension, freeze and due debt, which are true whether or not anyone can reach them."""
    for lifecycle in (
        dict(ever_sold=False, days_since_last_sale=None, account_age_days=400),  # never_active
        dict(account_age_days=3),                                               # onboarding
        dict(days_since_last_sale=900),                                         # churned
        dict(days_since_last_sale=20),                                          # dormant
        dict(projected_gb=1.0, avg_prev_gb=100.0),                              # declining
        dict(projected_gb=500.0, avg_prev_gb=100.0),                            # growing
        dict(),                                                                 # healthy
    ):
        assert _classify(_m(bot_chat_id=None, **lifecycle)) == "unregistered"

    assert _classify(_m(bot_chat_id=None, enforcement_state="enforced")) == "suspended"
    assert _classify(_m(bot_chat_id=None, enforcement_state="frozen")) == "frozen"
    # Only reachable after the owner unlinks an already-billed reseller: an undelivered
    # invoice never leaves `draft`, and the debt query ignores drafts.
    assert _classify(_m(bot_chat_id=None, has_due_debt=True)) == "debtor"


def test_a_linked_reseller_is_never_called_unregistered():
    """The whole segment hangs on one nullable column; a falsy-vs-None slip (chat id 0) would
    quietly move a reachable reseller into the "cannot be contacted" list."""
    assert _classify(_m(bot_chat_id=0)) != "unregistered"
    assert _classify(_m(bot_chat_id=100)) != "unregistered"


def test_every_shape_lands_in_exactly_one_known_segment():
    """Exhaustive-ish sweep: whatever the metric combination, the result is always one of the
    declared segments — `classify` has no fall-through hole."""
    seen = set()
    for state in ("active", "frozen", "enforced"):
        for due in (False, True):
            for chat in (None, 100):
                for sold in (False, True):
                    for days in (0, 13, 14, 44, 45, 900):
                        for age in (1, 13, 14, 29, 30, 400):
                            for proj, avg in ((0.0, 0.0), (10.0, 100.0), (400.0, 100.0),
                                              (100.0, 100.0)):
                                seg = _classify(_m(
                                    enforcement_state=state, has_due_debt=due, bot_chat_id=chat,
                                    ever_sold=sold,
                                    days_since_last_sale=days if sold else None,
                                    account_age_days=age, projected_gb=proj, avg_prev_gb=avg,
                                ))
                                assert seg in crm.SEGMENTS
                                seen.add(seg)
    # Every declared segment is actually reachable — a dead branch would be a silent bug.
    assert seen == set(crm.SEGMENTS)


# ---------------------------------------------------------------- never_active vs onboarding
def test_a_brand_new_admin_is_not_accused_of_never_activating():
    """Below the age floor, "sold nothing" is normal, not a problem to chase."""
    fresh = _m(ever_sold=False, days_since_last_sale=None, account_age_days=3)
    assert _classify(fresh) == "onboarding"
    aged = _m(ever_sold=False, days_since_last_sale=None,
              account_age_days=T.never_active_min_age_days)
    assert _classify(aged) == "never_active"


def test_onboarding_shields_a_new_account_that_did_sell():
    assert _classify(_m(account_age_days=T.onboarding_days - 1, days_since_last_sale=90)) \
        == "onboarding"
    assert _classify(_m(account_age_days=T.onboarding_days, days_since_last_sale=90)) == "churned"


# ---------------------------------------------------------------- dormant / churned boundaries
@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "healthy"), (13, "healthy"), (14, "dormant"), (44, "dormant"),
     (45, "churned"), (900, "churned")],
)
def test_dormant_and_churn_boundaries_are_inclusive(days, expected):
    assert _classify(_m(days_since_last_sale=days)) == expected


# ---------------------------------------------------------------- trend guards
def test_trend_rules_are_skipped_early_in_the_month():
    """On day 2, month-to-date is noise. Without this guard the entire board would read
    «رو به افول» for the first few days of every month."""
    shrinking = _m(projected_gb=1.0, avg_prev_gb=100.0)
    assert _classify(shrinking, elapsed=crm.MIN_ELAPSED_DAYS_FOR_TREND - 1) == "healthy"
    assert _classify(shrinking, elapsed=crm.MIN_ELAPSED_DAYS_FOR_TREND) == "declining"


def test_trend_rules_need_a_real_baseline():
    """A reseller with no history has no 'usual' to shrink from — dividing by it would either
    crash or mark every newcomer as declining."""
    assert _classify(_m(projected_gb=0.0, avg_prev_gb=0.0)) == "healthy"
    assert _classify(_m(projected_gb=500.0, avg_prev_gb=0.0)) == "healthy"


def test_declining_and_growing_use_the_configured_percentages():
    assert _classify(_m(projected_gb=49.0, avg_prev_gb=100.0)) == "declining"
    assert _classify(_m(projected_gb=50.0, avg_prev_gb=100.0)) == "healthy"   # not < 50%
    assert _classify(_m(projected_gb=125.0, avg_prev_gb=100.0)) == "healthy"  # not > 125%
    assert _classify(_m(projected_gb=126.0, avg_prev_gb=100.0)) == "growing"


def test_thresholds_are_owner_tunable():
    strict = crm.Thresholds(dormant_days=3, churned_days=7)
    assert _classify(_m(days_since_last_sale=4), t=strict) == "dormant"
    assert _classify(_m(days_since_last_sale=8), t=strict) == "churned"


def test_load_thresholds_from_survives_a_hand_edited_row():
    """The settings API validates ranges on write, but a value edited straight in the DB must
    fall back to the default rather than crash the board."""
    t = crm.load_thresholds_from({"crm_dormant_days": "21", "crm_churned_days": "oops"})
    assert t.dormant_days == 21
    assert t.churned_days == crm.Thresholds().churned_days


# ---------------------------------------------------------------- helpers
def test_month_labels_are_ascii_periods_oldest_first():
    labels = crm._month_labels(dt.date(2026, 2, 10), 4)
    assert labels == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_segment_counts_are_zero_filled():
    counts = crm.segment_counts(["dormant", "dormant", "healthy"])
    assert set(counts) == set(crm.SEGMENTS)   # UI never has to guess a missing key
    assert counts["dormant"] == 2 and counts["healthy"] == 1 and counts["churned"] == 0


def test_is_due_hides_muted_and_future_snoozes():
    day = dt.date(2026, 8, 12)

    class _S:
        def __init__(self, muted=False, until=None):
            self.muted, self.snoozed_until = muted, until

    assert crm.is_due(None, day) is True                                  # never touched
    assert crm.is_due(_S(until=day + dt.timedelta(days=1)), day) is False  # snoozed
    assert crm.is_due(_S(until=day), day) is False                        # snoozed through today
    assert crm.is_due(_S(until=day - dt.timedelta(days=1)), day) is True   # expired
    assert crm.is_due(_S(muted=True), day) is False
    # Mute outranks an expired snooze.
    assert crm.is_due(_S(muted=True, until=day - dt.timedelta(days=30)), day) is False
