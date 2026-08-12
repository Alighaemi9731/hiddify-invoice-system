"""
Reseller follow-up board — lifecycle segmentation + churn metrics.

The owner works ~400 top-level resellers by hand and needs three answers the rest of the
system never gave: who never started, who stopped, and who did I already chase. This module
computes the first two; `reseller_crm_state` / `reseller_followups` remember the third.

Two design rules everything here follows:

1. **One reseller, one segment.** `classify()` is a priority-ordered ladder where the first
   match wins, so a suspended debtor who also stopped selling appears exactly once. Without
   that, the same admin shows up in three lists and the whole board becomes noise.
2. **Six queries for the whole board, not six per reseller.** `load_board_metrics()` fans out
   a fixed set of grouped queries and rolls subtrees up in Python. `reseller_report.node_months`
   is the per-reseller equivalent and would be ~400 round-trips here.

Only the metric half is cached. Follow-up state is read fresh on every request, otherwise
logging a follow-up would not drop the row off the list for another five minutes.
"""
from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import proccache
from app.models import (
    EndUserSnapshot,
    Invoice,
    Panel,
    Reseller,
    ResellerCrmState,
    StorefrontOrder,
)
from app.models.enums import EnforcementState, InvoiceStatus
from app.services import pricing, settings_service
from app.services.periods import current_month, today
from app.services.presence import reseller_absent

# "Owed" = delivered but not yet paid. Mirrors app.api.reports.OUTSTANDING; redeclared here
# so the service layer does not import the API layer.
OUTSTANDING = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)

# How many months of invoice history the board loads (the drawer chart shows all of them).
HISTORY_MONTHS = 12
# The trend window compared against the current month's projection.
TREND_MONTHS = 3
# Below this many elapsed days, a month-to-date projection is noise: one quiet weekend at the
# start of a month would mark a healthy reseller as shrinking. The trend rules are skipped.
MIN_ELAPSED_DAYS_FOR_TREND = 5

# Priority order — FIRST MATCH WINS. This tuple IS the no-duplicates guarantee; every
# consumer (API filters, UI chips, tests) reads the order from here.
SEGMENTS: tuple[str, ...] = (
    "suspended",      # enforcement_state == enforced
    "frozen",         # enforcement_state == frozen
    "debtor",         # owes money that is actually due
    "never_active",   # old enough to have sold something, never did
    "onboarding",     # too new to judge
    "churned",        # no sale for a long time
    "dormant",        # no sale for a while
    "declining",      # still selling, but shrinking fast
    "growing",        # expanding
    "healthy",        # everything else
)

SETTING_KEYS = [
    "crm_dormant_days",
    "crm_churned_days",
    "crm_never_active_min_age_days",
    "crm_onboarding_days",
    "crm_declining_pct",
    "crm_growing_pct",
    "crm_snooze_default_days",
]


@dataclass(frozen=True)
class Thresholds:
    dormant_days: int = 14
    churned_days: int = 45
    never_active_min_age_days: int = 14
    onboarding_days: int = 30
    declining_pct: int = 50
    growing_pct: int = 125
    snooze_default_days: int = 15


@dataclass
class RootMetrics:
    """Everything `classify()` needs about one top-level reseller. DB-free by design."""

    reseller_id: int
    panel_id: int
    panel_key: str
    name: str
    admin_uuid: str
    enforcement_state: str
    bot_chat_id: int | None
    sub_reseller_count: int

    # Month-to-date, live from snapshots (this month has no invoice yet).
    mtd_services: int = 0
    mtd_gb: float = 0.0
    # Projected to a whole month so early-month partials are comparable with past months.
    projected_gb: float = 0.0

    # Densified history, oldest → newest, one slot per month, missing month == 0.0.
    months: list[dict[str, Any]] = field(default_factory=list)
    avg_prev_gb: float = 0.0          # mean GB of the previous TREND_MONTHS months
    value_at_risk_toman: float = 0.0  # mean Toman of the last 3 months that had a sale

    last_sale_date: dt.date | None = None
    first_sale_date: dt.date | None = None
    days_since_last_sale: int | None = None
    account_age_days: int = 0
    ever_sold: bool = False

    outstanding_toman: float = 0.0
    outstanding_count: int = 0
    oldest_unpaid_period: str | None = None
    has_due_debt: bool = False        # owes something NOT deferred into the future


def load_thresholds_from(values: dict[str, Any]) -> Thresholds:
    """Build Thresholds from a settings dict, ignoring anything unparseable (the settings API
    validates ranges on write; a hand-edited DB row must not crash the board)."""
    d = Thresholds()

    def _int(key: str, fallback: int) -> int:
        try:
            return int(values.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return Thresholds(
        dormant_days=_int("crm_dormant_days", d.dormant_days),
        churned_days=_int("crm_churned_days", d.churned_days),
        never_active_min_age_days=_int(
            "crm_never_active_min_age_days", d.never_active_min_age_days
        ),
        onboarding_days=_int("crm_onboarding_days", d.onboarding_days),
        declining_pct=_int("crm_declining_pct", d.declining_pct),
        growing_pct=_int("crm_growing_pct", d.growing_pct),
        snooze_default_days=_int("crm_snooze_default_days", d.snooze_default_days),
    )


async def load_thresholds(session: AsyncSession) -> Thresholds:
    return load_thresholds_from(await settings_service.get_many(session, SETTING_KEYS))


def classify(m: RootMetrics, t: Thresholds, *, elapsed_days: int) -> str:
    """Assign exactly one segment. Priority-ordered; the first match wins.

    `elapsed_days` is how far into the current month we are — the trend rules are meaningless
    before `MIN_ELAPSED_DAYS_FOR_TREND` and are skipped rather than guessed.

    Pure and DB-free so segment purity is unit-testable without a database.
    """
    if m.enforcement_state == EnforcementState.enforced.value:
        return "suspended"
    if m.enforcement_state == EnforcementState.frozen.value:
        return "frozen"
    if m.has_due_debt:
        return "debtor"
    if not m.ever_sold:
        # A brand-new admin who has legitimately not sold yet is not a problem — only one that
        # has had time to. Below the age floor they fall through to `onboarding`.
        if m.account_age_days >= t.never_active_min_age_days:
            return "never_active"
        return "onboarding"
    if m.account_age_days < t.onboarding_days:
        return "onboarding"
    days = m.days_since_last_sale
    if days is not None:
        if days >= t.churned_days:
            return "churned"
        if days >= t.dormant_days:
            return "dormant"
    # Trend rules need both a real baseline and enough of the month to project from.
    if elapsed_days >= MIN_ELAPSED_DAYS_FOR_TREND and m.avg_prev_gb > 0:
        ratio = m.projected_gb / m.avg_prev_gb * 100.0
        if ratio < t.declining_pct:
            return "declining"
        if ratio > t.growing_pct:
            return "growing"
    return "healthy"


def _is_present(row: Any) -> bool:
    """Is this reseller still on its panel?

    Python-side rather than a WHERE clause on purpose. The SQL twin used elsewhere
    (`app.api.resellers._present_filter`) subtracts a `timedelta` from a column, which SQLite
    binds as a DATETIME parameter and compares as garbage — so on the test database it reports
    every removed admin as present. The board loads these columns anyway, so filtering here
    costs nothing and behaves identically on both engines.

    Delegates to `presence.reseller_absent`, the single source of truth for the rule (which
    fails CLOSED — an unhealthy or never-synced panel means "assume still there").
    """
    panel = SimpleNamespace(status=row.panel_status, last_synced_at=row.panel_synced_at)
    return not reseller_absent(SimpleNamespace(last_seen_at=row.last_seen_at), panel)


def _month_labels(anchor: dt.date, count: int) -> list[str]:
    """`count` month labels ending at (and including) `anchor`'s month, oldest first."""
    labels: list[str] = []
    y, mo = anchor.year, anchor.month
    for _ in range(count):
        labels.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return list(reversed(labels))


def _subtree_uuids(
    roots: Sequence[Any], rows: Sequence[Any]
) -> dict[int, set[str]]:
    """Map root reseller id → lowercased admin_uuids of the root and every descendant.

    Built PER PANEL: `parent_admin_uuid` is only unique within a panel, and the same uuid does
    legitimately exist on two panels (`test_reseller_tree_is_panel_scoped_and_cycle_safe`).
    Cycle-safe — a self-parenting row must not hang the board.
    """
    children: dict[int, dict[str, list[Any]]] = {}
    for r in rows:
        parent = (r.parent_admin_uuid or "").lower()
        if not parent:
            continue
        children.setdefault(r.panel_id, {}).setdefault(parent, []).append(r)

    out: dict[int, set[str]] = {}
    for root in roots:
        panel_children = children.get(root.panel_id, {})
        seen = {(root.admin_uuid or "").lower()}
        queue = [root]
        while queue:
            node = queue.pop()
            for child in panel_children.get((node.admin_uuid or "").lower(), []):
                key = (child.admin_uuid or "").lower()
                if key and key not in seen:
                    seen.add(key)
                    queue.append(child)
        out[root.id] = seen
    return out


def _top_level(rows: Sequence[Any]) -> list[Any]:
    """Non-owner roots, keyed by `(panel_id, admin_uuid)`.

    Same rule as `reseller_stats.top_level_roots`, re-expressed over lightweight column rows
    (that helper takes ORM entities; loading 400+ Reseller objects here would be wasteful).
    Note this is NOT `invoice_engine.select_billable_roots`, whose sets are keyed on the bare
    uuid and are only safe when called one panel at a time.
    """
    owner_keys = {(r.panel_id, (r.admin_uuid or "").lower()) for r in rows if r.is_owner}
    all_keys = {(r.panel_id, (r.admin_uuid or "").lower()) for r in rows}
    roots = []
    for r in rows:
        if r.is_owner:
            continue
        parent_key = (r.panel_id, (r.parent_admin_uuid or "").lower())
        if not r.parent_admin_uuid or parent_key in owner_keys or parent_key not in all_keys:
            roots.append(r)
    return roots


async def _panel_sync_state(session: AsyncSession) -> tuple:
    rows = (
        await session.execute(
            select(Panel.id, Panel.last_synced_at)
            .where(Panel.enabled.is_(True))
            .order_by(Panel.id)
        )
    ).all()
    return tuple((pid, ts.isoformat() if ts else "") for pid, ts in rows)


# The month-to-date and first/last-sale aggregates scan end_user_snapshots, the largest and
# most heavily upserted table in the system. Their inputs only change when a panel syncs, so
# the derived metrics are cached exactly like `reports._preview_cache`. Deliberately NOT an
# index on end_user_snapshots: revision a4e7c2b9f1d6 rejected adding read-side indexes there.
_metrics_cache = proccache.TTLCache(ttl_seconds=300.0)


async def load_board_metrics(
    session: AsyncSession, *, today_: dt.date | None = None
) -> list[RootMetrics]:
    """Compute per-root churn metrics for every eligible reseller. Cached per panel-sync state."""
    day = today_ or today()
    key = (proccache.engine_ns(session), "crm-board", day.isoformat(),
           await _panel_sync_state(session))
    hit = _metrics_cache.get(key)
    if hit is not proccache.MISS:
        return hit
    metrics = await _compute_board_metrics(session, day)
    _metrics_cache.put(key, metrics)
    return metrics


def invalidate_metrics_cache() -> None:
    """Drop the metric cache — called after anything that changes panel/reseller eligibility."""
    _metrics_cache.clear()


async def _compute_board_metrics(session: AsyncSession, day: dt.date) -> list[RootMetrics]:
    free_threshold = await pricing.get_free_threshold_gb(session)
    excluded_sizes = await pricing.get_excluded_usage_gb(session)

    # ---- query 1: resellers + hierarchy (columns only, no ORM entities) -------------------
    all_rows = (
        await session.execute(
            select(
                Reseller.id, Reseller.panel_id, Reseller.admin_uuid,
                Reseller.parent_admin_uuid, Reseller.name, Reseller.is_owner,
                Reseller.exclude_from_billing, Reseller.enforcement_state,
                Reseller.bot_chat_id, Reseller.created_at, Reseller.last_seen_at,
                Panel.key.label("panel_key"),
                Panel.status.label("panel_status"),
                Panel.last_synced_at.label("panel_synced_at"),
            ).join(Panel, Reseller.panel_id == Panel.id)
        )
    ).all()
    rows = [r for r in all_rows if _is_present(r)]
    if not rows:
        return []

    roots = [r for r in _top_level(rows) if not r.exclude_from_billing]
    if not roots:
        return []
    subtrees = _subtree_uuids(roots, rows)
    root_ids = [r.id for r in roots]
    panel_ids = sorted({r.panel_id for r in rows})
    # uuid → owning root, per panel. A uuid can repeat across panels, so never key on it alone.
    owner_of: dict[tuple[int, str], int] = {}
    for root in roots:
        for uuid in subtrees[root.id]:
            owner_of[(root.panel_id, uuid)] = root.id

    period = current_month(day)
    elapsed_days = max(1, (day - period.start).days + 1)
    days_in_month = calendar.monthrange(day.year, day.month)[1]

    # A billable service is one the invoice engine would actually charge for: above the free
    # threshold, not an exactly-excluded size, and not a storefront free trial.
    billable_clause = [
        EndUserSnapshot.panel_id.in_(panel_ids),
        EndUserSnapshot.added_by_uuid.is_not(None),
        EndUserSnapshot.usage_limit_gb > free_threshold,
    ]
    # `get_excluded_usage_gb` is an exact-match set, matching `invoice_engine._excluded`.
    for size in sorted(excluded_sizes):
        billable_clause.append(EndUserSnapshot.usage_limit_gb != size)
    trial_uuids = await _trial_uuids(session, panel_ids)

    # ---- query 2: month-to-date services, live from snapshots -----------------------------
    # panel_id is pinned so `ix_enduser_panel_start_date` can be used; a bare start_date range
    # cannot use that index and would seq-scan the biggest table in the system.
    mtd_rows = (
        await session.execute(
            select(
                EndUserSnapshot.panel_id,
                func.lower(EndUserSnapshot.added_by_uuid),
                EndUserSnapshot.user_uuid,
                EndUserSnapshot.usage_limit_gb,
            ).where(
                *billable_clause,
                EndUserSnapshot.start_date >= period.start,
                EndUserSnapshot.start_date <= day,
            )
        )
    ).all()

    # ---- query 3: first + last billable sale (one pass, two aggregates) -------------------
    # No index covers (panel_id, lower(added_by_uuid)) → start_date, so this is a scan +
    # HashAggregate by design; the TTL cache is what makes it affordable.
    span_rows = (
        await session.execute(
            select(
                EndUserSnapshot.panel_id,
                func.lower(EndUserSnapshot.added_by_uuid),
                func.max(EndUserSnapshot.start_date),
                func.min(EndUserSnapshot.start_date),
            )
            .where(*billable_clause, EndUserSnapshot.start_date.is_not(None))
            .group_by(EndUserSnapshot.panel_id, func.lower(EndUserSnapshot.added_by_uuid))
        )
    ).all()

    # ---- query 4: invoice history --------------------------------------------------------
    history_labels = _month_labels(day, HISTORY_MONTHS)
    hist_rows = (
        await session.execute(
            select(
                Invoice.reseller_id, Invoice.period_label, Invoice.usage_gb,
                Invoice.users_count, Invoice.amount_toman, Invoice.status,
                Invoice.period_start,
            ).where(
                Invoice.reseller_id.in_(root_ids),
                Invoice.period_start >= dt.date.fromisoformat(f"{history_labels[0]}-01"),
                Invoice.status != InvoiceStatus.draft,
            )
        )
    ).all()

    # ---- query 5: outstanding debt + whether any of it is actually due --------------------
    # The min(CASE …) column is 0 when at least one open invoice has NO future payment
    # deadline — i.e. the reseller genuinely owes money right now. A deferral moves only that
    # clock; the invoice stays payable and still counts as debt everywhere else.
    debt_rows = (
        await session.execute(
            select(
                Invoice.reseller_id,
                func.count(Invoice.id),
                func.sum(Invoice.amount_toman),
                func.min(Invoice.period_label),
                func.min(
                    case(
                        (
                            or_(
                                Invoice.deferred_until.is_(None),
                                Invoice.deferred_until <= day,
                            ),
                            0,
                        ),
                        else_=1,
                    )
                ),
            )
            .where(Invoice.reseller_id.in_(root_ids), Invoice.status.in_(OUTSTANDING))
            .group_by(Invoice.reseller_id)
        )
    ).all()

    # ---- assemble -------------------------------------------------------------------------
    sub_counts = {r.id: max(0, len(subtrees[r.id]) - 1) for r in roots}
    out: list[RootMetrics] = []
    by_id: dict[int, RootMetrics] = {}
    for r in roots:
        state = r.enforcement_state
        m = RootMetrics(
            reseller_id=r.id,
            panel_id=r.panel_id,
            panel_key=r.panel_key or "",
            name=r.name or "",
            admin_uuid=r.admin_uuid or "",
            enforcement_state=state.value if hasattr(state, "value") else str(state or ""),
            bot_chat_id=r.bot_chat_id,
            sub_reseller_count=sub_counts[r.id],
            months=[{"label": lbl, "gb": 0.0, "services": 0, "amount_toman": 0.0}
                    for lbl in history_labels],
        )
        # Account age floor: `Reseller.created_at` alone lies after a wipe-data reset, which
        # deletes every reseller row and lets the next sync recreate them all "today".
        created = r.created_at.date() if r.created_at else day
        m.account_age_days = max(0, (day - created).days)
        by_id[r.id] = m
        out.append(m)

    for panel_id, uuid, user_uuid, limit_gb in mtd_rows:
        if user_uuid in trial_uuids.get(panel_id, ()):
            continue
        target = by_id.get(owner_of.get((panel_id, uuid or ""), -1))
        if target is None:
            continue
        target.mtd_services += 1
        target.mtd_gb += float(limit_gb or 0)

    for panel_id, uuid, max_start, min_start in span_rows:
        target = by_id.get(owner_of.get((panel_id, uuid or ""), -1))
        if target is None:
            continue
        if max_start and (target.last_sale_date is None or max_start > target.last_sale_date):
            target.last_sale_date = max_start
        if min_start and (target.first_sale_date is None or min_start < target.first_sale_date):
            target.first_sale_date = min_start

    slot = {lbl: i for i, lbl in enumerate(history_labels)}
    for rid, label, gb, users, amount, _status, period_start in hist_rows:
        target = by_id.get(rid)
        if target is None:
            continue
        idx = slot.get(label)
        if idx is not None:
            target.months[idx] = {
                "label": label,
                "gb": float(gb or 0),
                "services": int(users or 0),
                "amount_toman": float(amount or 0),
            }
        if users:
            # Month granularity, but it survives snapshot pruning: `prune_stale_snapshots`
            # deletes deleted-user rows older than last month, so a long-dormant reseller's
            # old snapshots are gone and only the invoice remembers the sale.
            # Clamped to today: the current month's invoice would otherwise stamp a
            # last-sale date in the future.
            end = min(_month_end(period_start), day)
            if target.last_sale_date is None or end > target.last_sale_date:
                target.last_sale_date = end
            if target.first_sale_date is None or period_start < target.first_sale_date:
                target.first_sale_date = period_start

    for rid, count, total, oldest, min_due in debt_rows:
        target = by_id.get(rid)
        if target is None:
            continue
        target.outstanding_count = int(count or 0)
        target.outstanding_toman = float(total or 0)
        target.oldest_unpaid_period = oldest
        target.has_due_debt = (min_due == 0) and target.outstanding_toman > 0

    for m in out:
        # A month with no invoice row means ZERO sales, not "no data": invoicing skips a
        # bundle whose total is 0 and deletes stale zero drafts. The slots were pre-filled
        # with zeros above precisely so the mean below is over 3 real months, not 1.
        prev = [s["gb"] for s in m.months[-(TREND_MONTHS + 1):-1]]
        m.avg_prev_gb = sum(prev) / len(prev) if prev else 0.0
        # Month-to-date quota sold, scaled to a whole month. Not identical to what the invoice
        # will finally say (that adds metering extras and the deleted-user rule), but the trend
        # rules only ask "bigger or smaller than usual", and they are the last two segments to
        # be reached anyway.
        m.projected_gb = m.mtd_gb / elapsed_days * days_in_month
        sold_months = [s["amount_toman"] for s in m.months if s["amount_toman"] > 0][-3:]
        m.value_at_risk_toman = sum(sold_months) / len(sold_months) if sold_months else 0.0
        m.ever_sold = m.last_sale_date is not None or m.mtd_services > 0
        if m.mtd_services > 0:
            m.days_since_last_sale = 0
        elif m.last_sale_date is not None:
            m.days_since_last_sale = max(0, (day - m.last_sale_date).days)
        if m.first_sale_date is not None:
            m.account_age_days = max(m.account_age_days, (day - m.first_sale_date).days)
    return out


def _month_end(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


async def _trial_uuids(
    session: AsyncSession, panel_ids: Sequence[int]
) -> dict[int, set[str]]:
    """Cross-panel version of `storefront.trial_user_uuids` — one query for every panel.

    A free-trial config is a giveaway, never a sale; counting one would make a shop that only
    hands out trials read as a healthy seller.
    """
    if not panel_ids:
        return {}
    rows = (
        await session.execute(
            select(StorefrontOrder.panel_id, StorefrontOrder.panel_user_uuid).where(
                StorefrontOrder.panel_id.in_(panel_ids),
                StorefrontOrder.is_trial.is_(True),
                StorefrontOrder.panel_user_uuid.is_not(None),
            )
        )
    ).all()
    out: dict[int, set[str]] = {}
    for panel_id, uuid in rows:
        if panel_id is not None and uuid:
            out.setdefault(panel_id, set()).add(uuid)
    return out


async def load_states(
    session: AsyncSession, reseller_ids: Iterable[int]
) -> dict[int, ResellerCrmState]:
    """Follow-up state per reseller. Read FRESH on every request — never cached, so logging a
    follow-up drops the row off the "due" view immediately instead of after the metric TTL."""
    ids = list(reseller_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ResellerCrmState).where(ResellerCrmState.reseller_id.in_(ids))
        )
    ).scalars().all()
    return {s.reseller_id: s for s in rows}


def is_due(state: ResellerCrmState | None, day: dt.date) -> bool:
    """Is this reseller due for a follow-up (i.e. shown in the default view)?"""
    if state is None:
        return True
    if state.muted:
        return False
    return not (state.snoozed_until and state.snoozed_until >= day)


async def upsert_state(
    session: AsyncSession,
    reseller_id: int,
    *,
    snoozed_until: dt.date | None,
    muted: bool,
    note: str | None,
    now: dt.datetime,
) -> ResellerCrmState:
    """Create-or-update the follow-up state row and stamp the touch. Caller commits."""
    state = (
        await session.execute(
            select(ResellerCrmState).where(ResellerCrmState.reseller_id == reseller_id)
        )
    ).scalar_one_or_none()
    if state is None:
        state = ResellerCrmState(reseller_id=reseller_id, touch_count=0, note="", muted=False)
        session.add(state)
    state.snoozed_until = snoozed_until
    state.muted = muted
    state.last_touch_at = now
    state.touch_count = (state.touch_count or 0) + 1
    if note is not None:
        state.note = note
    return state


def segment_counts(segments: Iterable[str]) -> dict[str, int]:
    """Zero-filled counts for every segment, so the UI never has to guess a missing key."""
    counts = dict.fromkeys(SEGMENTS, 0)
    for s in segments:
        if s in counts:
            counts[s] += 1
    return counts


__all__ = [
    "HISTORY_MONTHS",
    "MIN_ELAPSED_DAYS_FOR_TREND",
    "OUTSTANDING",
    "SEGMENTS",
    "SETTING_KEYS",
    "TREND_MONTHS",
    "RootMetrics",
    "Thresholds",
    "classify",
    "invalidate_metrics_cache",
    "is_due",
    "load_board_metrics",
    "load_states",
    "load_thresholds",
    "load_thresholds_from",
    "segment_counts",
    "upsert_state",
]
