"""
Traffic audit — find top-level resellers whose REAL traffic exceeds the quota they actually sold.

Billing works on sold quota, and `metering` only sees `current_usage_GB` deltas between syncs, so a
reseller that resets its users' counters is invisible to both. On s7 (2026-08) one reseller moved
9,647 GB against 1,100 GB of quota ever sold — 8.8× — while the highest `overage_gb` metering
recorded for that entire panel that month was 19 GB.

The missing number lives in Hiddify's own `daily_usage` table, reachable only through
`GET /api/v2/admin/server_status/` authenticated AS each reseller (see
`AdminApiClient.get_server_status`). The panel aggregates it over `recursive_sub_admins_ids()`, so a
sub-reseller's traffic is already counted inside its parent — exactly the roll-up the owner wants.

The ceiling it is measured against comes from OUR data and costs no extra panel calls: a user who
bought 30 GB cannot legitimately consume more, so the quota a reseller sold bounds honest traffic.

Deliberately REPORT-ONLY. Nothing here bills, warns, freezes or enforces — the owner reads the board
and decides. The reseller selection mirrors the invoice engine exactly (`_panel_billable` →
`select_billable_roots` → `_reseller_present`), so the audit covers precisely the accounts the owner
actually bills: the panel Owner and `exclude_from_billing` resellers are never listed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    Panel,
    Reseller,
    ResellerTrafficDaily,
    UsageMeter,
)
from app.services import invoicing, pricing, settings_service
from app.services.invoice_engine import (
    build_children_map,
    collect_descendants,
    select_billable_roots,
)
from app.services.panel_client.admin_api import AdminApiClient
from app.services.periods import current_month, today

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024**3

# The client default is 90 s, sized for enforcement PATCHes that reapply the whole proxy config.
# Here we make one read per reseller across hundreds of resellers, so a 90 s hang on a wedged panel
# would turn a 3-minute scan into a 40-minute one. A slow read is treated as "unreachable".
SCAN_TIMEOUT_SECONDS = 15.0

# Measured: ~1.8 s per call, and parallelism WITHIN a panel buys nothing (s1: 6 serial = 10.8 s,
# 6 parallel = 9.9 s — uwsgi runs `cheaper = 1`, and the endpoint recomputes psutil system stats on
# every request). So panels run concurrently and each panel's resellers run serially.
_PANEL_CONCURRENCY = 8


# ── Pure helpers (DB-free, unit-tested) ───────────────────────────────────────


def _as_int(value: Any) -> int:
    """Coerce one `usage_history` number.

    Hiddify serialises these as JSON **strings** (`"102763943759"`, `"39"`) — not ints — so a naive
    read produces string concatenation or a TypeError rather than an obviously wrong number. Missing
    and unparseable values are 0: a reseller we could not measure must never look like a big one.
    """
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class UsageStats:
    """The four numbers we use out of `usage_history`, in GB and whole users."""

    yesterday_gb: float
    yesterday_online: int
    last_30d_gb: float
    total_users: int


def parse_usage_history(history: dict | None) -> UsageStats | None:
    """Parse the panel's `usage_history` block, or None when it is unusable.

    `h24.usage` and `m5.usage` are hardcoded 0 in Hiddify and are deliberately ignored.
    """
    if not isinstance(history, dict):
        return None
    yesterday = history.get("yesterday") or {}
    last30 = history.get("last_30_days") or {}
    total = history.get("total") or {}
    if not isinstance(yesterday, dict) or not isinstance(last30, dict):
        return None
    return UsageStats(
        yesterday_gb=round(_as_int(yesterday.get("usage")) / _BYTES_PER_GB, 3),
        yesterday_online=_as_int(yesterday.get("online")),
        last_30d_gb=round(_as_int(last30.get("usage")) / _BYTES_PER_GB, 3),
        total_users=_as_int(total.get("users")) if isinstance(total, dict) else 0,
    )


@dataclass(frozen=True)
class Thresholds:
    ratio: float
    min_gb_30d: float


async def load_thresholds(session: AsyncSession) -> Thresholds:
    values = await settings_service.get_many(
        session, ["traffic_audit_ratio_threshold", "traffic_audit_min_gb_30d"]
    )
    return load_thresholds_from(values)


def load_thresholds_from(values: dict) -> Thresholds:
    """Split out so the classifier can be exercised without a database."""
    return Thresholds(
        ratio=float(values.get("traffic_audit_ratio_threshold") or 2.0),
        min_gb_30d=float(values.get("traffic_audit_min_gb_30d") or 0),
    )


def compute_ratio(traffic_30d_gb: float, quota_gb: float) -> float | None:
    """Real traffic ÷ sold quota, or None when there is no quota to measure against.

    None is not "zero": a reseller with no live quota (brand new, or everything expired) has no
    ceiling to exceed, and dividing by it would manufacture an infinite ratio out of noise.
    """
    if quota_gb <= 0:
        return None
    return round(float(traffic_30d_gb) / float(quota_gb), 3)


def is_flagged(*, ratio: float | None, traffic_30d_gb: float, thresholds: Thresholds) -> bool:
    """The red flag.

    The volume floor gates everything: a reseller with 3 users and 45 GB can cross any ratio on
    ordinary churn, and a board that cries wolf is a board nobody reads. Fleet-wide today, the floor
    plus a 2.0 ratio flags nobody at all — which is the point, so a flag means something.

    `ratio is None` (no quota sold at all) is flagged, NOT skipped. It reads like a null case but it
    is the extreme one: real traffic against zero sold quota is an infinite ratio, and it is exactly
    the state an abuser ends in after deleting the evidence. Letting the None guard swallow it would
    put the worst offender in the quietest row.
    """
    if float(traffic_30d_gb) < thresholds.min_gb_30d:
        return False
    if ratio is None:
        return True
    return ratio >= thresholds.ratio


def gb_per_user_day(traffic_gb: float, online_users: int) -> float | None:
    """Yesterday's GB per user actually active yesterday. None when nobody was online."""
    if not online_users:
        return None
    return round(float(traffic_gb) / int(online_users), 2)


# ── Findings ──────────────────────────────────────────────────────────────────


@dataclass
class ResellerTraffic:
    panel_id: int
    panel_key: str
    reseller_id: int
    reseller_name: str
    admin_uuid: str
    sub_count: int
    # None everywhere when the panel could not answer for this reseller (deleted admin, refused
    # key, timeout). Kept in the list on purpose — a missing row must be visible, not silently gone.
    reachable: bool = True
    yesterday_gb: float = 0.0
    yesterday_online: int = 0
    last_30d_gb: float = 0.0
    total_users: int = 0
    quota_gb: float = 0.0
    counter_gb: float = 0.0
    ratio: float | None = None
    flagged: bool = False
    panel_share_pct: float | None = None

    def as_dict(self) -> dict:
        return {
            "panel_id": self.panel_id,
            "panel_key": self.panel_key,
            "reseller_id": self.reseller_id,
            "reseller_name": self.reseller_name or "—",
            "admin_uuid": (self.admin_uuid or "")[:8],
            "sub_count": self.sub_count,
            "reachable": self.reachable,
            "yesterday_gb": round(self.yesterday_gb, 2),
            "yesterday_online": self.yesterday_online,
            "last_30d_gb": round(self.last_30d_gb, 2),
            "total_users": self.total_users,
            "quota_gb": round(self.quota_gb, 2),
            "counter_gb": round(self.counter_gb, 2),
            "counter_ratio": (
                round(self.last_30d_gb / self.counter_gb, 1) if self.counter_gb > 0 else None
            ),
            "ratio": self.ratio,
            "flagged": self.flagged,
            "gb_per_user_day": gb_per_user_day(self.yesterday_gb, self.yesterday_online),
            "panel_share_pct": self.panel_share_pct,
        }


@dataclass
class ScanResult:
    rows: list[ResellerTraffic] = field(default_factory=list)
    # Panels we could not scan at all. Surfaced explicitly: a partial scan must never read as a
    # clean bill of health just because the failure produced a shorter list.
    skipped_panels: list[dict] = field(default_factory=list)
    scanned_at: dt.datetime | None = None

    def as_dict(self) -> dict:
        flagged = [r for r in self.rows if r.flagged]
        unreachable = [r for r in self.rows if not r.reachable]
        return {
            "resellers_scanned": len(self.rows),
            "flagged": len(flagged),
            "unreachable": len(unreachable),
            "panels_skipped": len(self.skipped_panels),
            "total_traffic_yesterday_gb": round(sum(r.yesterday_gb for r in self.rows), 2),
            "scanned_at": self.scanned_at.isoformat() if self.scanned_at else None,
            "skipped_panels_detail": self.skipped_panels,
            # Flagged first, then by ratio — the reason a row is on screen decides its position.
            "rows": [
                r.as_dict()
                for r in sorted(
                    self.rows,
                    key=lambda r: (not r.flagged, -(r.ratio or 0), -r.yesterday_gb),
                )
            ],
        }


# ── The scan ──────────────────────────────────────────────────────────────────


async def _quota_by_creator(
    session: AsyncSession, panel_id: int
) -> tuple[dict[str, float], dict[str, float]]:
    """(live sold quota, counter total) per creating admin — ONE grouped query for the whole panel.

    Grouped, never per-reseller: the CRM board's rule. ~400 resellers × a query each would be 400
    round-trips for numbers the database can produce in one. The counter sum rides along free and
    is the evidence column.
    """
    rows = (
        await session.execute(
            select(
                func.lower(EndUserSnapshot.added_by_uuid),
                func.sum(EndUserSnapshot.usage_limit_gb),
                func.sum(EndUserSnapshot.current_usage_gb),
            )
            .where(
                EndUserSnapshot.panel_id == panel_id,
                EndUserSnapshot.added_by_uuid.is_not(None),
            )
            .group_by(func.lower(EndUserSnapshot.added_by_uuid))
        )
    ).all()
    quota = {(uuid or ""): float(gb or 0) for uuid, gb, _ in rows}
    counter = {(uuid or ""): float(used or 0) for uuid, _, used in rows}
    return quota, counter


async def _quota_added_by_creator(
    session: AsyncSession, panel_id: int, period_label: str
) -> dict[str, float]:
    """Quota ADDED this month per creating admin, one grouped query.

    Renewals replace quota rather than adding to the live total, so a reseller that sells 30 GB
    twice in a month shows 30 GB live but sold 60. Taking the larger of the two keeps a legitimate
    renewal cycle from looking like abuse. `ix_usage_meters_panel_period_addedby` covers this.
    """
    rows = (
        await session.execute(
            select(func.lower(UsageMeter.added_by_uuid), func.sum(UsageMeter.quota_added_gb))
            .where(
                UsageMeter.panel_id == panel_id,
                UsageMeter.period_label == period_label,
                UsageMeter.added_by_uuid.is_not(None),
            )
            .group_by(func.lower(UsageMeter.added_by_uuid))
        )
    ).all()
    return {(uuid or ""): float(gb or 0) for uuid, gb in rows}


@dataclass
class PanelJob:
    """Everything one panel needs for its network phase — resolved while the session was open, so
    the fan-out itself touches no database."""

    panel: Panel
    owner_uuid: str
    roots: list[Reseller]
    subtree: dict[int, list[str]]
    quota_live: dict[str, float]
    quota_added: dict[str, float]
    counters: dict[str, float]


async def collect(
    session: AsyncSession, *, panel_id: int | None = None
) -> tuple[list[PanelJob], list[dict], Thresholds]:
    """Phase 1 — every database read, done up front.

    Split from the network phase on purpose: the fan-out takes 2–3 minutes, and holding a pooled
    connection idle-in-transaction for that long against a shared `max_connections` is how a
    background report starves the API that launched it.
    """
    thresholds = await load_thresholds(session)
    max_age = await pricing.get_max_snapshot_age_hours(session)
    now = dt.datetime.now(dt.timezone.utc)
    period_label = current_month().label

    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = select(Panel).where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    jobs: list[PanelJob] = []
    skipped: list[dict] = []
    for panel in panels:
        ok, reason = invoicing._panel_billable(panel, now=now, max_age_hours=max_age)
        if not ok:
            # A panel we could not measure must be named. A shorter list is not a clean bill.
            skipped.append({"panel_key": panel.key, "reason": reason})
            continue
        resellers = (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars().all()
        children = build_children_map(resellers)
        roots: list[Reseller] = []
        subtree: dict[int, list[str]] = {}
        for root in select_billable_roots(resellers):
            # Python, never SQL: the SQL twin subtracts a timedelta from a column, which SQLite
            # binds as a DATETIME and reports every removed admin as present.
            if not invoicing._reseller_present(root, panel):
                continue
            roots.append(root)
            subtree[root.id] = [
                (d.admin_uuid or "").lower() for d in collect_descendants(root, children)
            ]
        if not roots:
            continue
        quota_live, counters = await _quota_by_creator(session, panel.id)
        jobs.append(PanelJob(
            panel=panel,
            owner_uuid=panel.owner_uuid or "",
            roots=roots,
            subtree=subtree,
            quota_live=quota_live,
            quota_added=await _quota_added_by_creator(session, panel.id, period_label),
            counters=counters,
        ))
    return jobs, skipped, thresholds


async def _scan_panel(
    job: PanelJob,
    thresholds: Thresholds,
    client: AdminApiClient,
    on_progress: Any = None,
) -> list[ResellerTraffic]:
    """One panel: ask it about each of its top-level resellers, serially."""
    panel = job.panel
    out: list[ResellerTraffic] = []
    for root in job.roots:
        uuids = job.subtree.get(root.id, [])
        row = ResellerTraffic(
            panel_id=panel.id,
            panel_key=panel.key,
            reseller_id=root.id,
            reseller_name=root.name or "",
            admin_uuid=(root.admin_uuid or "").lower(),
            sub_count=max(0, len(uuids) - 1),
            # The bundle's ceiling: root + every descendant, since the panel rolls traffic up the
            # same way. `max`, not a sum: live quota is the stock every still-present config can
            # legitimately burn, `quota_added` is the flow that also catches configs created AND
            # deleted inside the window. Summing them would double-count the overlap — nearly all
            # of it — and quietly turn the ceiling into two months of sales.
            quota_gb=round(
                max(
                    sum(job.quota_live.get(u, 0.0) for u in uuids),
                    sum(job.quota_added.get(u, 0.0) for u in uuids),
                ),
                3,
            ),
            counter_gb=round(sum(job.counters.get(u, 0.0) for u in uuids), 3),
        )
        try:
            history = await client.get_server_status(panel, api_key=root.admin_uuid)
            stats = parse_usage_history(history)
        except Exception as exc:  # noqa: BLE001 — one bad reseller must not abort the panel
            log.warning("traffic_audit: %s/%s unreadable: %s", panel.key, root.name, exc)
            stats = None
        if stats is None:
            # Deleted admin, refused key, or a timeout. Left in the list as unreachable and NEVER
            # stored: a 0 GB row would read as "they went quiet", the opposite of "we could not ask".
            row.reachable = False
        else:
            row.yesterday_gb = stats.yesterday_gb
            row.yesterday_online = stats.yesterday_online
            row.last_30d_gb = stats.last_30d_gb
            row.total_users = stats.total_users
            row.ratio = compute_ratio(row.last_30d_gb, row.quota_gb)
            row.flagged = is_flagged(
                ratio=row.ratio, traffic_30d_gb=row.last_30d_gb, thresholds=thresholds
            )
        out.append(row)
        if on_progress is not None:
            on_progress(panel.key)

    # Panel share needs the WHOLE panel's traffic, which means asking as the panel owner — the
    # super-admin's sub-tree is the panel. Summing the roots instead would exclude the Owner's own
    # users and inflate every reseller's share; when the owner read fails we show nothing at all.
    panel_total = 0.0
    try:
        owner_stats = parse_usage_history(
            await client.get_server_status(panel, api_key=job.owner_uuid)
        )
        panel_total = owner_stats.yesterday_gb if owner_stats else 0.0
    except Exception as exc:  # noqa: BLE001 — share is a nicety, never a reason to fail a panel
        log.info("traffic_audit: %s panel total unavailable: %s", panel.key, exc)
    if panel_total > 0:
        for r in out:
            r.panel_share_pct = round(r.yesterday_gb / panel_total * 100, 1)
    return out


async def measure(
    jobs: list[PanelJob],
    thresholds: Thresholds,
    *,
    skipped: list[dict] | None = None,
    on_progress: Any = None,
) -> ScanResult:
    """Phase 2 — network only, panels concurrent, resellers serial inside each panel.

    An `asyncio.Semaphore` over one task per panel, exactly like `sync.sync_all`. Nothing inside a
    panel is ever gathered: measured on s1, 6 parallel calls took 9.9 s against 6 serial at 10.8 s,
    because the panel runs `cheaper = 1` and recomputes psutil stats on every request. Concurrency
    there would only risk stalling a panel that is also serving real resellers.
    """
    result = ScanResult(scanned_at=dt.datetime.now(dt.timezone.utc))
    result.skipped_panels.extend(skipped or [])
    if not jobs:
        return result

    client = AdminApiClient(timeout=SCAN_TIMEOUT_SECONDS)
    gate = asyncio.Semaphore(_PANEL_CONCURRENCY)

    async def run(job: PanelJob) -> list[ResellerTraffic]:
        async with gate:
            try:
                return await _scan_panel(job, thresholds, client, on_progress)
            except Exception as exc:  # noqa: BLE001 — one dead panel must not abort the fleet
                log.warning("traffic_audit: panel %s failed: %s", job.panel.key, exc)
                result.skipped_panels.append(
                    {"panel_key": job.panel.key, "reason": str(exc)[:200]}
                )
                return []

    for chunk in await asyncio.gather(*(run(j) for j in jobs)):
        result.rows.extend(chunk)
    return result


async def scan(
    session: AsyncSession, *, panel_id: int | None = None, on_progress: Any = None
) -> ScanResult:
    """Measure every billable top-level reseller. Pure read — writes nothing.

    Convenience wrapper for tests and one-off calls. The background runner uses `collect` and
    `measure` separately so it can close its session before the long network phase.
    """
    jobs, skipped, thresholds = await collect(session, panel_id=panel_id)
    return await measure(jobs, thresholds, skipped=skipped, on_progress=on_progress)


async def report(session: AsyncSession, *, panel_id: int | None = None) -> dict:
    """What the audit sees right now. Safe to call any time; touches nothing."""
    return (await scan(session, panel_id=panel_id)).as_dict()


# ── Persistence ───────────────────────────────────────────────────────────────


async def store(session: AsyncSession, result: ScanResult, *, day: dt.date | None = None) -> int:
    """Upsert one row per reseller per day. Unreachable resellers are NOT stored — a zero we never
    measured would read as "this reseller went quiet", which is the opposite of the truth."""
    stamp = day or today()
    stored = 0
    for row in result.rows:
        if not row.reachable:
            continue
        existing = (
            await session.execute(
                select(ResellerTrafficDaily).where(
                    ResellerTrafficDaily.panel_key == row.panel_key,
                    ResellerTrafficDaily.reseller_admin_uuid == row.admin_uuid,
                    ResellerTrafficDaily.day == stamp,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = ResellerTrafficDaily(
                panel_key=row.panel_key,
                reseller_admin_uuid=row.admin_uuid,
                day=stamp,
            )
            session.add(existing)
        existing.panel_id = row.panel_id
        existing.reseller_name = row.reseller_name
        existing.traffic_gb = row.yesterday_gb
        existing.traffic_30d_gb = row.last_30d_gb
        existing.online_users = row.yesterday_online
        existing.total_users = row.total_users
        existing.quota_gb = row.quota_gb
        existing.counter_gb = row.counter_gb
        existing.ratio = row.ratio
        existing.flagged = row.flagged
        stored += 1
    await session.commit()
    return stored


async def latest(session: AsyncSession) -> dict:
    """The most recent stored scan, read straight from history — instant, no panel calls."""
    newest = (
        await session.execute(select(func.max(ResellerTrafficDaily.day)))
    ).scalar_one_or_none()
    if newest is None:
        return {"day": None, "rows": [], "flagged": 0, "resellers_scanned": 0}
    rows = (
        await session.execute(
            select(ResellerTrafficDaily)
            .where(ResellerTrafficDaily.day == newest)
            .order_by(ResellerTrafficDaily.flagged.desc(), ResellerTrafficDaily.ratio.desc())
        )
    ).scalars().all()
    return {
        "day": newest.isoformat(),
        "resellers_scanned": len(rows),
        "flagged": sum(1 for r in rows if r.flagged),
        "total_traffic_yesterday_gb": round(sum(float(r.traffic_gb or 0) for r in rows), 2),
        "rows": [
            {
                "panel_key": r.panel_key,
                "panel_id": r.panel_id,
                "reseller_name": r.reseller_name or "—",
                "admin_uuid": (r.reseller_admin_uuid or "")[:8],
                "yesterday_gb": round(float(r.traffic_gb or 0), 2),
                "yesterday_online": int(r.online_users or 0),
                "last_30d_gb": round(float(r.traffic_30d_gb or 0), 2),
                "total_users": int(r.total_users or 0),
                "quota_gb": round(float(r.quota_gb or 0), 2),
                "counter_gb": round(float(r.counter_gb or 0), 2),
                "counter_ratio": (
                    round(float(r.traffic_30d_gb or 0) / float(r.counter_gb), 1)
                    if float(r.counter_gb or 0) > 0
                    else None
                ),
                # Postgres returns Decimal here and SQLite a float; float() at the boundary keeps
                # the flat totals from raising when the two mix.
                "ratio": float(r.ratio) if r.ratio is not None else None,
                "flagged": bool(r.flagged),
                "gb_per_user_day": gb_per_user_day(
                    float(r.traffic_gb or 0), int(r.online_users or 0)
                ),
                "reachable": True,
                "sub_count": 0,
                "panel_share_pct": None,
            }
            for r in rows
        ],
    }


async def _already_stored(session: AsyncSession, day: dt.date) -> set[tuple[str, str]]:
    """(panel_key, admin_uuid) pairs already recorded for `day`."""
    rows = (
        await session.execute(
            select(
                ResellerTrafficDaily.panel_key, ResellerTrafficDaily.reseller_admin_uuid
            ).where(ResellerTrafficDaily.day == day)
        )
    ).all()
    return {(pk or "", uuid or "") for pk, uuid in rows}


async def run_daily(session: AsyncSession) -> dict:
    """The scheduled pass: scan the fleet and record the day. Returns the same shape as `report`.

    Resellers already stored for today are dropped before the network phase, which is what makes
    the scheduler's retry hour nearly free: after a complete run the second fire makes zero panel
    calls, and after a half-finished one it measures only the remainder.
    """
    enabled = await settings_service.get(session, "traffic_audit_enabled", True)
    if not enabled:
        return {"skipped": "disabled"}
    stamp = today()
    done = await _already_stored(session, stamp)
    jobs, skipped, thresholds = await collect(session)
    for job in jobs:
        job.roots = [
            r for r in job.roots
            if (job.panel.key, (r.admin_uuid or "").lower()) not in done
        ]
    jobs = [j for j in jobs if j.roots]
    result = await measure(jobs, thresholds, skipped=skipped)
    stored = await store(session, result, day=stamp)
    payload = result.as_dict()
    payload["stored"] = stored
    log.info(
        "traffic_audit: scanned %d resellers, %d flagged, %d stored, %d panels skipped",
        payload["resellers_scanned"], payload["flagged"], stored, payload["panels_skipped"],
    )
    return payload


async def prune(session: AsyncSession, *, keep_days: int | None = None) -> int:
    """Drop history older than the retention window. Called by the daily maintenance job."""
    days = keep_days
    if days is None:
        days = int(await settings_service.get(session, "traffic_audit_retention_days", 180) or 180)
    cutoff = today() - dt.timedelta(days=max(1, days))
    rows = (
        await session.execute(
            select(ResellerTrafficDaily.id).where(ResellerTrafficDaily.day < cutoff)
        )
    ).scalars().all()
    for chunk_start in range(0, len(rows), 500):
        ids = rows[chunk_start:chunk_start + 500]
        await session.execute(delete(ResellerTrafficDaily).where(ResellerTrafficDaily.id.in_(ids)))
    await session.commit()
    return len(rows)
