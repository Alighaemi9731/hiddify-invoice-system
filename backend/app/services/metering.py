"""
Abuse-resistant usage metering (billing model "C").

Normal billing (quota of users whose service was created in the month) is unchanged.
On top of it we ADD the abuse that the snapshot rule misses, measured from the
periodic sync's deltas:

  • overage_gb      — true consumption beyond the paid-for buffer. Catches the
    "create a small package then reset usage daily so it never ends" trick: we track
    cumulative real usage (reset-aware) and bill anything past what was provisioned.
  • edit_renewal_gb — quota topped up WITHOUT updating start_date. Catches "renew an
    expired user with the Edit button instead of the renew button", which keeps the
    old start_date so the snapshot rule never re-bills it.

`apply()` runs inside each sync (no DB I/O — pure state update). `bundle_extra()`
sums the abnormal GB for a reseller bundle at billing time. `notify_abuse_if_any()`
tells the reseller (and the owner) exactly what was detected and that it's billed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageMeter, UsageMeterEvent
from app.models.enums import DeliveryKind
from app.services import settings_service

log = logging.getLogger("metering")

_EPS = 0.05  # ignore sub-50MB noise
# A usage drop is only a real "renew-volume" reset (usage zeroed, start_date kept) when the
# new value lands near zero. A larger drop that DOESN'T reach ~0 is a panel stat-correction /
# re-sync, not abuse — so it must not be counted as consumption (that produced false overage).
_RESET_FLOOR_GB = 5.0


def period_of(d) -> str:
    return d.strftime("%Y-%m") if d else ""


async def is_enabled(session: AsyncSession) -> bool:
    return bool(await settings_service.get(session, "metering_enabled", True))


async def load_period_meters(
    session: AsyncSession, panel_id: int, period_label: str
) -> dict[str, UsageMeter]:
    rows = (
        await session.execute(
            select(UsageMeter).where(
                UsageMeter.panel_id == panel_id, UsageMeter.period_label == period_label
            )
        )
    ).scalars().all()
    return {m.user_uuid: m for m in rows}


@dataclass(frozen=True)
class MeterUpdate:
    """The new ABSOLUTE snapshot/meter values a metering step produces (F10). `compute()` returns one
    without touching any ORM object; the caller applies it via `write()` ONLY after it succeeds, so a
    metering failure leaves the snapshot's baseline and the UsageMeter row untouched (the next sync then
    recaptures the full missed delta exactly once)."""
    meter_provisioned_gb: float
    meter_consumed_gb: float
    meter_init: bool
    added_by_uuid: str | None
    name: str
    quota_added_gb: float
    renew_used_gb: float
    edit_renewal_gb: float
    reset_count: int
    consumed_gb: float
    overage_gb: float
    # Billable events this step DETECTED, as (kind, gb) — "renewal" (the banked closed-cycle
    # consumption) or "edit_topup" (the quota added without a fresh start_date). Persisted by
    # `record_events` so invoices can enumerate each renewal separately.
    events: tuple[tuple[str, float], ...] = ()


def compute(
    *,
    snapshot,                 # EndUserSnapshot (read-only here)
    meter: UsageMeter,        # this month's bucket (read-only here)
    prev_limit: float,
    prev_used: float,
    new_limit: float,
    new_used: float,
    start_date,
    added_by_uuid: str | None,
    name: str | None,
    period_label: str,
) -> MeterUpdate:
    """PURE: read the current running state and return the new absolute values. Mutates nothing — the
    caller writes the result only on success (F10)."""
    # Start every field at its CURRENT value; each branch adjusts only what it touches.
    prov = float(snapshot.meter_provisioned_gb or 0)
    cons = float(snapshot.meter_consumed_gb or 0)
    init = bool(snapshot.meter_init)
    quota_added = float(meter.quota_added_gb or 0)
    renew_used = float(meter.renew_used_gb or 0)
    edit_renewal = float(meter.edit_renewal_gb or 0)
    reset_count = int(meter.reset_count or 0)
    consumed = float(meter.consumed_gb or 0)
    overage_total = float(meter.overage_gb or 0)
    m_name = (name or "")[:255]
    in_period = period_of(start_date) == period_label
    events: list[tuple[str, float]] = []

    def _result() -> MeterUpdate:
        return MeterUpdate(
            meter_provisioned_gb=prov, meter_consumed_gb=cons, meter_init=init,
            added_by_uuid=added_by_uuid, name=m_name, quota_added_gb=quota_added,
            renew_used_gb=renew_used, edit_renewal_gb=edit_renewal, reset_count=reset_count,
            consumed_gb=consumed, overage_gb=overage_total, events=tuple(events),
        )

    # First time we see this user (a brand-new user, OR an existing one when metering
    # is first switched on). Set the baseline; only bill it if genuinely created now.
    if not init:
        prov = new_limit
        cons = new_used
        init = True
        if in_period:
            quota_added += new_limit
        return _result()

    prev_start = snapshot.start_date  # not yet overwritten by the caller

    # RENEWAL: start_date advanced → a fresh cycle. Re-baseline so the PREVIOUS cycle's consumption
    # can't show up as "overage" against the new quota. (The new package is billed by the normal
    # start_date rule, not here.)
    if start_date is not None and (prev_start is None or start_date > prev_start):
        # Bank what the CLOSING cycle actually used (capped at its sold quota) ONLY when the renewal
        # RESET usage into a fresh cycle — detected by the usage counter dropping (it is monotonic
        # within a cycle, so `new_used < prev_used` ⇒ a reset happened). A CUMULATIVE renewal
        # (usage_limit_GB = used+gb with usage CARRIED OVER — the storefront renew, or a native edit
        # that keeps usage) is already billed in full by the base start_date rule on the current
        # `usage_limit_gb`, so banking `renew_used` here would double-count the closing consumption.
        # Also require that the closing cycle STARTED this month (a prior-month cycle was already
        # billed there, so never bill it again).
        usage_reset = new_used + _EPS < prev_used
        if usage_reset and period_of(prev_start) == period_label:
            closing_used = min(cons, prov)
            if closing_used > _EPS:
                banked = round(closing_used, 3)
                renew_used += banked
                events.append(("renewal", banked))
        prov = new_limit
        cons = new_used
        # A proper renewal (renew-day advances start_date) is the CORRECT way and supersedes any
        # earlier renew-by-edit recorded for this user this month: the fresh start_date makes the
        # normal invoice rule bill the full new quota, so a lingering edit_renewal_gb would
        # double-bill the same volume. Clear it (overage from real over-consumption is kept).
        edit_renewal = 0.0
        if in_period:
            quota_added += new_limit
        return _result()

    # Quota increase = new sale / top-up / renewal-by-edit (start_date unchanged). A DECREASE
    # (add <= 0) is intentionally ignored: metering only ADDS the abuse the snapshot rule misses,
    # never credits back. `meter_provisioned_gb` stays at the high-water mark so a later real
    # overage is still measured against what was actually provisioned; an owner-corrected
    # over-provision simply isn't refunded here (the base snapshot rule already bills the final,
    # lower quota). This is by design — see docs/… and CLAUDE.md metering notes.
    add = new_limit - prev_limit
    if add > _EPS:
        quota_added += add
        prov += add
        if not in_period:
            # Topped up without a fresh start_date → renew-by-edit (snapshot rule misses it).
            edit_renewal += add
            events.append(("edit_topup", round(add, 3)))

    # Consumption since last sync (reset-aware).
    if new_used + _EPS >= prev_used:
        dc = new_used - prev_used          # normal forward consumption
    elif new_used <= _RESET_FLOOR_GB:
        # Usage dropped to ~0 while start_date stayed the same → a "renew-volume" reset (the
        # daily-reset abuse): the post-reset usage counts so it accumulates toward overage.
        dc = new_used
        reset_count += 1
    else:
        # Dropped but NOT to ~0 → a legitimate panel stat-correction / re-sync, not a reset.
        # Count no consumption this sync (next sync measures forward from the new lower value).
        dc = 0.0
    if dc < 0:
        dc = 0.0

    buffer = prov - cons
    if buffer < 0:
        buffer = 0.0
    overage = dc - buffer
    if overage < 0:
        overage = 0.0

    consumed += dc
    cons += dc
    if overage > _EPS:
        overage_total += overage
    return _result()


def record_events(
    session: AsyncSession, *, panel_id: int, user_uuid: str, period_label: str, u: MeterUpdate
) -> None:
    """Persist the billable events a compute() step detected — one UsageMeterEvent row per
    (kind, gb). Call together with write() so the event log and the aggregate move as one;
    at billing time a month whose events don't sum to the aggregate falls back to the
    aggregate, so a missed event can shift presentation but never money."""
    for kind, gb in u.events:
        session.add(UsageMeterEvent(
            panel_id=panel_id, user_uuid=user_uuid, period_label=period_label,
            kind=kind, gb=gb,
        ))


def write(snapshot, meter: UsageMeter, u: MeterUpdate) -> None:  # noqa: ANN001
    """Apply a computed MeterUpdate to the ORM objects. Call ONLY after compute() succeeded (F10)."""
    snapshot.meter_provisioned_gb = u.meter_provisioned_gb
    snapshot.meter_consumed_gb = u.meter_consumed_gb
    snapshot.meter_init = u.meter_init
    meter.added_by_uuid = u.added_by_uuid
    meter.name = u.name
    meter.quota_added_gb = u.quota_added_gb
    meter.renew_used_gb = u.renew_used_gb
    meter.edit_renewal_gb = u.edit_renewal_gb
    meter.reset_count = u.reset_count
    meter.consumed_gb = u.consumed_gb
    meter.overage_gb = u.overage_gb


def apply(
    *,
    snapshot,                 # EndUserSnapshot (running state lives here)
    meter: UsageMeter,        # this month's bucket
    prev_limit: float,
    prev_used: float,
    new_limit: float,
    new_used: float,
    start_date,
    added_by_uuid: str | None,
    name: str | None,
    period_label: str,
) -> None:
    """Backward-compatible wrapper: compute() then write(). Prefer compute()/write() at call sites that
    need failure atomicity (the sync loop does), so a raising calculation leaves no partial mutation."""
    write(snapshot, meter, compute(
        snapshot=snapshot, meter=meter, prev_limit=prev_limit, prev_used=prev_used,
        new_limit=new_limit, new_used=new_used, start_date=start_date, added_by_uuid=added_by_uuid,
        name=name, period_label=period_label,
    ))


# Display suffixes for the typed extra lines. Every metered extra on an invoice names WHAT
# it is; multiple renewals of one config in a month are enumerated («تمدید اول»، «تمدید دوم»)
# from the per-event log so the invoice is unambiguous.
LABEL_RENEWAL = " — تمدید"
LABEL_EDIT = " — افزایش حجم با ویرایش"
LABEL_OVERAGE = " — مصرف مازاد بر بسته"

_FA_ORDINALS = (
    "اول", "دوم", "سوم", "چهارم", "پنجم", "ششم",
    "هفتم", "هشتم", "نهم", "دهم", "یازدهم", "دوازدهم",
)


def _fa_ordinal(i: int) -> str:
    """1-based Persian ordinal word; falls back to a numbered form past twelve."""
    return _FA_ORDINALS[i - 1] if 1 <= i <= len(_FA_ORDINALS) else f"شمارهٔ {i}"


# Reconciliation slack between an aggregate accumulator and the sum of its logged events:
# both sides round each addend to 3 decimals, so any real drift means missing/legacy events.
_EVENT_MATCH_TOL = 0.06


def _typed_parts(
    *,
    renew_bill: float,
    edit_bill: float,
    over_bill: float,
    user_events: list[tuple[str, float]],
) -> list[tuple[str, str, float]]:
    """Split one user's billed extra into display parts [(kind, label_suffix, gb), …].

    The per-event log only refines PRESENTATION: renewal/edit parts are taken from the
    events when they reconcile with the billed aggregate (then each renewal gets its own
    enumerated line), otherwise the aggregate renders as a single typed line (legacy
    months, a cleared edit accumulator, or a missed event write). Amounts always come
    from the aggregates — callers re-adjust rounding so the parts sum exactly."""
    parts: list[tuple[str, str, float]] = []
    if renew_bill > 0:
        renew_events = [g for k, g in user_events if k == "renewal" and g > _EPS]
        if renew_events and abs(sum(renew_events) - renew_bill) <= _EVENT_MATCH_TOL:
            if len(renew_events) == 1:
                parts.append(("renewal", LABEL_RENEWAL, renew_events[0]))
            else:
                parts.extend(
                    ("renewal", f"{LABEL_RENEWAL} {_fa_ordinal(i)}", g)
                    for i, g in enumerate(renew_events, start=1)
                )
        else:
            parts.append(("renewal", LABEL_RENEWAL, renew_bill))
    if edit_bill > 0:
        edit_events = [g for k, g in user_events if k == "edit_topup" and g > _EPS]
        if edit_events and abs(sum(edit_events) - edit_bill) <= _EVENT_MATCH_TOL:
            if len(edit_events) == 1:
                parts.append(("edit", LABEL_EDIT, edit_events[0]))
            else:
                parts.extend(
                    ("edit", f"{LABEL_EDIT} (نوبت {_fa_ordinal(i)})", g)
                    for i, g in enumerate(edit_events, start=1)
                )
        else:
            parts.append(("edit", LABEL_EDIT, edit_bill))
    if over_bill > 0:
        parts.append(("overage", LABEL_OVERAGE, over_bill))
    return parts


def _extra_from_rows(
    rows,
    free_threshold_gb: float,
    overage_tol: float,
    exclude_user_uuids: set[str] | None,
    events_by_user: dict[str, list[tuple[str, float]]] | None = None,
) -> dict:
    """Pure per-row extra math shared by bundle_extra / bundle_extra_many.

    A user keeps consuming for a couple of minutes after hitting their quota (xray cuts off
    lazily), so a few hundred MB of "overage" is normal, not abuse. Threshold (NOT subtract):
    overage at/below the tolerance is pure soft-cutoff slack → ignored entirely; above it, it's
    real over-consumption → the FULL overage is billed. Real reset-abuse is many GB → billed.

    Each billed component renders as its OWN typed line (`display_name` = user name + a
    suffix naming the component; renewals enumerated per event) — the money is identical to
    the old single merged line, only the presentation is split."""
    total = 0.0
    lines: list[dict] = []
    abnormal: list[dict] = []
    for m in rows:
        if exclude_user_uuids and m.user_uuid in exclude_user_uuids:
            continue  # free-trial config → never billed
        raw_over = float(m.overage_gb or 0)
        over = raw_over if raw_over > overage_tol else 0.0
        edit = float(m.edit_renewal_gb or 0)
        # ABUSE extra (overage from usage resets + renew-by-edit) → billed AND flagged in the
        # heads-up notice.
        abuse = 0.0
        if over > _EPS:
            abuse += over
        edit_bill = edit if edit > free_threshold_gb else 0.0  # ignore tiny test-config renewals
        abuse += edit_bill
        # LEGITIMATE extra: the actual usage of cycles renewed properly (day+volume) earlier this
        # month — billed, but it's NORMAL (not abuse), so it never triggers the abuse warning.
        renew = float(m.renew_used_gb or 0)
        renew_bill = renew if renew > _EPS else 0.0
        extra = abuse + renew_bill
        if extra <= _EPS:
            continue
        total += extra
        parts = _typed_parts(
            renew_bill=renew_bill,
            edit_bill=edit_bill,
            over_bill=over if over > _EPS else 0.0,
            user_events=(events_by_user or {}).get(m.user_uuid, []),
        )
        # Round each part to 3 decimals and pin the LAST one so the parts sum EXACTLY to the
        # user's billed extra — persisted lines must always add up to the locked total (M75).
        name = m.name or ""
        target = round(extra, 3)
        rounded = [round(g, 3) for _k, _lbl, g in parts]
        if rounded:
            rounded[-1] = round(target - sum(rounded[:-1]), 3)
            if rounded[-1] <= 0:
                # Events reconciled within tolerance yet overshoot the billed aggregate
                # (stale/orphaned event edge case) — never let the split inflate the lines:
                # rebuild from the aggregates alone (those parts always pin positively).
                parts = _typed_parts(
                    renew_bill=renew_bill, edit_bill=edit_bill,
                    over_bill=over if over > _EPS else 0.0, user_events=[],
                )
                rounded = [round(g, 3) for _k, _lbl, g in parts]
                rounded[-1] = round(target - sum(rounded[:-1]), 3)
        for (kind, suffix, _g), gb_part in zip(parts, rounded):
            if gb_part <= 0:
                continue
            lines.append({
                "user_uuid": m.user_uuid, "name": name,
                "display_name": name[:200] + suffix,
                "usage_gb": gb_part, "added_by_uuid": m.added_by_uuid,
                "kind": kind,
            })
        if abuse > _EPS:
            abnormal.append({
                "name": name or m.user_uuid[-6:], "user_uuid": m.user_uuid,
                "overage_gb": round(over, 3), "edit_renewal_gb": round(edit, 3),
                "reset_count": int(m.reset_count or 0), "billed_gb": round(abuse, 3),
            })
    return {"gb": round(total, 3), "lines": lines, "abnormal": abnormal}


async def bundle_extra(
    session: AsyncSession,
    panel_id: int,
    admin_uuids: set[str],
    period_label: str,
    free_threshold_gb: float,
    exclude_user_uuids: set[str] | None = None,
) -> dict:
    """The ABNORMAL extra GB to add to a bundle's invoice for the period, plus a
    per-user breakdown for the notification. Returns {gb, lines, abnormal}.
    `exclude_user_uuids` = storefront free-trial config uuids whose meter (renew-by-edit / overage)
    must never bill — the whole trial is a free giveaway."""
    out = await bundle_extra_many(
        session, panel_id, admin_uuids, [period_label], free_threshold_gb,
        exclude_user_uuids=exclude_user_uuids,
    )
    return out[period_label]


async def bundle_extra_many(
    session: AsyncSession,
    panel_id: int,
    admin_uuids: set[str],
    period_labels: list[str],
    free_threshold_gb: float,
    exclude_user_uuids: set[str] | None = None,
) -> dict[str, dict]:
    """`bundle_extra` for SEVERAL periods with ONE UsageMeter query (`period_label IN …`,
    grouped per label in Python; the per-row math is the shared `_extra_from_rows`).
    Returns {label: {gb, lines, abnormal}} with an entry for EVERY requested label."""
    if not period_labels:
        return {}
    if not admin_uuids or not await is_enabled(session):
        return {lbl: {"gb": 0.0, "lines": [], "abnormal": []} for lbl in period_labels}
    rows = (
        await session.execute(
            select(UsageMeter).where(
                UsageMeter.panel_id == panel_id,
                UsageMeter.period_label.in_(period_labels),
                UsageMeter.added_by_uuid.in_(admin_uuids),
            )
        )
    ).scalars().all()
    overage_tol = float(await settings_service.get(session, "overage_tolerance_gb", 0.5) or 0)
    by_label: dict[str, list] = {lbl: [] for lbl in period_labels}
    for m in rows:
        if m.period_label in by_label:
            by_label[m.period_label].append(m)
    events_by_label = await _load_events(
        session, panel_id, period_labels, {m.user_uuid for m in rows}
    )
    return {
        lbl: _extra_from_rows(
            by_label[lbl], free_threshold_gb, overage_tol, exclude_user_uuids,
            events_by_user=events_by_label.get(lbl),
        )
        for lbl in period_labels
    }


async def bundle_extra_for_bundles(
    session: AsyncSession,
    panel_id: int,
    bundles: list[set[str]],
    period_label: str,
    free_threshold_gb: float,
    exclude_user_uuids: set[str] | None = None,
) -> list[dict]:
    """`bundle_extra` for EVERY bundle of a panel in one pass (Wave 6): ONE UsageMeter
    query + ONE events pass for the union of all bundles' admin uuids, bucketed per
    bundle in Python (root subtrees are disjoint, so each meter row belongs to exactly
    one bundle). Was: 2 queries PER bundle — an N+1 across every billable root on every
    generation/preview. Per-row math is the shared `_extra_from_rows`, identical to the
    per-bundle path (parity-tested in tests/test_metering_batch_parity.py)."""
    empty = lambda: {"gb": 0.0, "lines": [], "abnormal": []}  # noqa: E731
    if not bundles:
        return []
    if not await is_enabled(session):
        return [empty() for _ in bundles]
    union: set[str] = set()
    for b in bundles:
        union |= b or set()
    if not union:
        return [empty() for _ in bundles]
    rows = (
        await session.execute(
            select(UsageMeter).where(
                UsageMeter.panel_id == panel_id,
                UsageMeter.period_label == period_label,
                UsageMeter.added_by_uuid.in_(union),
            )
        )
    ).scalars().all()
    overage_tol = float(await settings_service.get(session, "overage_tolerance_gb", 0.5) or 0)
    events_by_label = await _load_events(
        session, panel_id, [period_label], {m.user_uuid for m in rows}
    )
    events_for_label = events_by_label.get(period_label)
    owner_of: dict[str, int] = {}
    for i, b in enumerate(bundles):
        for uuid in b or set():
            owner_of[uuid] = i
    rows_by_bundle: list[list] = [[] for _ in bundles]
    for m in rows:
        if m.added_by_uuid is None:
            continue
        bundle_idx = owner_of.get(m.added_by_uuid)
        if bundle_idx is not None:
            rows_by_bundle[bundle_idx].append(m)
    return [
        _extra_from_rows(
            rows_by_bundle[idx], free_threshold_gb, overage_tol, exclude_user_uuids,
            events_by_user=events_for_label,
        )
        if (bundles[idx] or set())
        else empty()
        for idx in range(len(bundles))
    ]


async def _load_events(
    session: AsyncSession,
    panel_id: int,
    period_labels: list[str],
    user_uuids: set[str],
) -> dict[str, dict[str, list[tuple[str, float]]]]:
    """The logged metering events for these users/periods, keyed {label: {uuid: [(kind, gb)…]}}
    in detection (id) order — the data behind the per-renewal enumeration. Chunked IN() so a
    huge bundle can't overflow SQLite's bound-parameter limit."""
    out: dict[str, dict[str, list[tuple[str, float]]]] = {}
    uuids = sorted(user_uuids)
    for i in range(0, len(uuids), 500):
        rows = (
            await session.execute(
                select(UsageMeterEvent).where(
                    UsageMeterEvent.panel_id == panel_id,
                    UsageMeterEvent.period_label.in_(period_labels),
                    UsageMeterEvent.user_uuid.in_(uuids[i:i + 500]),
                ).order_by(UsageMeterEvent.id)
            )
        ).scalars().all()
        for ev in rows:
            out.setdefault(ev.period_label, {}).setdefault(ev.user_uuid, []).append(
                (ev.kind, float(ev.gb or 0))
            )
    return out


def _abuse_text(period: str, abnormal: list[dict]) -> str:
    """A clear, complete heads-up for the reseller: WHAT was detected (per user), WHY it's
    billed, and HOW to renew correctly next month so it doesn't recur. Plain text (sent via
    notifier without parse_mode), structured with separators so it reads cleanly RTL."""
    total = round(sum(float(a["billed_gb"]) for a in abnormal), 3)
    n = len(abnormal)
    lines = [
        f"⚠️ هشدارِ مصرفِ غیرعادی — دورهٔ {period}",
        "",
        f"نمایندهٔ گرامی، برای {n} کاربرِ شما روشِ تمدیدِ نادرست شناسایی شد. فاکتورِ عادی فقط "
        "حجمِ سرویس‌هایی را می‌شمارد که «تاریخِ شروع»شان در همین ماه باشد؛ این کاربرها به همین "
        "دلیل در فاکتورِ عادی دیده نمی‌شدند، اما سامانه مصرفِ واقعی‌شان را رصد کرده و جمعاً "
        f"{total:g} گیگابایت به فاکتورِ این دوره اضافه کرده است.",
        "",
        "──────── جزئیات ────────",
    ]
    for a in abnormal:
        lines.append(f"👤 {a['name']}")
        if a["overage_gb"] > 0:
            extra = f" ({a['reset_count']} بار)" if a["reset_count"] else ""
            lines.append(
                f"   ▫️ «تمدیدِ حجم» بدونِ «تمدیدِ روز» (بازنشانیِ مصرف){extra}: "
                f"{a['overage_gb']:g} گیگابایت مصرفِ مازاد"
            )
        if a["edit_renewal_gb"] > 0:
            lines.append(
                f"   ▫️ افزایشِ حجم با «ویرایش» (تاریخِ شروع عوض نشده): {a['edit_renewal_gb']:g} گیگابایت"
            )
        lines.append(f"   ➕ اضافه‌شده به فاکتور: {a['billed_gb']:g} گیگابایت")
    lines += [
        "",
        "──────── چرا؟ ────────",
        "وقتی کاربری را فقط «تمدیدِ حجم» می‌کنید یا حجمش را «ویرایش» می‌کنید، بدونِ اینکه «روز» "
        "را هم تمدید کنید، تاریخِ شروعِ کاربر قدیمی می‌ماند و سرویس در فاکتورِ عادی شمرده نمی‌شود "
        "— در حالی که مصرفش ادامه دارد. این به معنای فروشِ حجم بدونِ ثبت در فاکتور است، که سامانه آن را "
        "به‌صورت خودکار جبران می‌کند.",
        "",
        "──────── روشِ درست (از ماهِ بعد) ────────",
        "✅ برای تمدیدِ هر کاربر، «هم روز و هم حجم» را با هم تمدید/بازنشانی کنید.",
        "به این ترتیب تاریخِ شروع به‌روز می‌شود، سرویس در فاکتورِ همان ماه به‌صورت عادی محاسبه می‌شود، "
        "و دیگر چیزی به‌عنوانِ «مصرفِ غیرعادی» اضافه نخواهد شد.",
        "",
        "❌ این موارد رصد و به فاکتور اضافه می‌شوند:",
        "• تمدیدِ فقط حجم، با نگه‌داشتنِ روزِ قدیمی",
        "• افزایشِ حجم از طریقِ «ویرایش»",
        "• بازنشانیِ مصرفِ کاربر برای دور زدنِ فاکتور",
    ]
    return "\n".join(lines)


async def notify_abuse_if_any(session: AsyncSession, invoice, reseller, *, bot=None) -> None:
    """If this invoice's bundle has abnormal (metered-extra) usage, message the reseller
    with a full breakdown and ping the owner. Best-effort; never raises into delivery."""
    try:
        if not await is_enabled(session):
            return
        from app.services import notifier, owner_notify, pricing, storefront
        from app.services.reseller_report import node_descendants

        descendants = await node_descendants(session, reseller)
        uuids = {d.admin_uuid for d in descendants}
        free_threshold = await pricing.get_free_threshold_gb(session)
        extra = await bundle_extra(
            session, invoice.panel_id, uuids, invoice.period_label, free_threshold,
            exclude_user_uuids=await storefront.trial_user_uuids(session, invoice.panel_id),
        )
        if not extra["abnormal"]:
            return

        text = _abuse_text(invoice.period_label, extra["abnormal"])
        await notifier.send_to_reseller(
            session, reseller, text, kind=DeliveryKind.abuse_notice, invoice_id=invoice.id, bot=bot
        )
        # Owner heads-up (clickable reseller link).
        total_extra = extra["gb"]
        await owner_notify.notify_owner(
            session,
            f"🚨 مصرف غیرعادی شناسایی شد — نماینده {owner_notify.user_link(reseller)} "
            f"(پنل #{invoice.panel_id})، دورهٔ {invoice.period_label}: "
            f"{len(extra['abnormal'])} کاربر، مجموع {total_extra:g} گیگابایت اضافه به فاکتور.",
            html=True,
        )
    except Exception:  # noqa: BLE001
        log.warning("notify_abuse_if_any failed for invoice %s", getattr(invoice, "id", "?"),
                    exc_info=True)
