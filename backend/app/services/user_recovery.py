"""Recover end-users that a panel backup-rollback removed but the invoice DB still remembers.

When a Hiddify panel is rolled back to an OLDER backup (server migration, ban, disaster restore),
every user an admin created between that backup and the rollback vanishes from the panel. The sync
never HARD-deletes `end_user_snapshots`, so those users survive here as STALE rows — their
`last_synced_at` frozen at their last pre-rollback sighting while the panel's own `last_synced_at`
moved on. This module:

  * `detect(...)` — finds those losses for the chosen panels (recent + gone-from-panel), CLUSTERED by
    the moment they were last seen. A rollback drops MANY users at the SAME instant (one big cluster),
    while ordinary admin deletions are scattered (tiny clusters) — so a real rollback stands out and
    the owner isn't tricked into resurrecting intentionally-deleted users.
  * `restore(...)` — re-creates the chosen users on the panel under their ORIGINAL admin
    (`added_by_uuid` → that reseller's key, so the panel files them under the right admin) with their
    ORIGINAL uuid (so the customer's existing config/sub-link keeps working). A live pre-check skips
    any user already back, and the `(panel_id, user_uuid)` unique constraint makes a duplicate
    impossible.

Owner-triggered, preview-then-confirm — NEVER automatic (stale rows also include deliberate deletions).
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EndUserSnapshot, Panel, Reseller
from app.services.panel_client.admin_api import AdminApiClient

log = logging.getLogger("user_recovery")

# A user whose last sighting predates the panel's latest sync by more than this is "gone from the
# panel" (not merely a mid-sync timing skew — one sync stamps every present user with the SAME time).
_GONE_MARGIN = dt.timedelta(minutes=2)


def _iso(v) -> str | None:  # noqa: ANN001
    return v.isoformat() if v is not None else None


def _aware(v):  # noqa: ANN001, ANN202
    """Coerce a possibly-naive timestamp (SQLite drops tz) to aware UTC for safe comparison."""
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=dt.timezone.utc)
    return v


async def _admin_names(session: AsyncSession, panel_ids: list[int]) -> dict[tuple[int, str], str]:
    """(panel_id, lower(admin_uuid)) → reseller name, for owner resolution."""
    rows = (await session.execute(
        select(Reseller.panel_id, Reseller.admin_uuid, Reseller.name).where(
            Reseller.panel_id.in_(panel_ids))
    )).all()
    return {(pid, (uuid or "").lower()): name for pid, uuid, name in rows}


def _user_dict(s: EndUserSnapshot, admin_name: str | None) -> dict:
    return {
        "panel_id": s.panel_id,
        "user_uuid": s.user_uuid,
        "name": s.name or "",
        "added_by_uuid": s.added_by_uuid,
        "admin_name": admin_name,
        "has_admin": admin_name is not None,
        "gb": float(s.usage_limit_gb or 0),
        "days": int(s.package_days or 0),
        "start_date": _iso(s.start_date),
        "current_usage_gb": float(s.current_usage_gb or 0),
        "enable": bool(s.enable),
        "last_seen_at": _iso(s.last_synced_at),
        "created_at": _iso(s.created_at),
    }


async def _sync_events(
    session: AsyncSession, panel_ids: list[int], cutoff: dt.datetime
) -> dict[int, dict[str, dict]]:
    """Per panel, minute-key of each SUCCESSFUL sync → context for the loss that followed it:
    `{"drop": users lost by the next successful sync, "failure": a sync FAILED in between}`.

    These are HINTS, not a classifier: a rollback/migration typically shows a count drop and often a
    sync failure (the panel was briefly unreachable while it was moved/restored) — but ordinary
    expiry churn also drops users, and network blips also fail, so the owner still decides. Because
    sync stamps `last_synced_at`, `panel.last_synced_at` and `sync_run.finished_at` with the SAME
    instant, a cluster's last-seen minute equals its sync's minute here — the match is exact."""
    from app.models import SyncRun

    out: dict[int, dict[str, dict]] = {pid: {} for pid in panel_ids}
    rows = (await session.execute(
        select(SyncRun).where(
            SyncRun.panel_id.in_(panel_ids),
            SyncRun.started_at >= cutoff,
        ).order_by(SyncRun.panel_id, SyncRun.started_at)
    )).scalars().all()
    by_panel: dict[int, list] = {}
    for r in rows:
        if r.panel_id is not None:
            by_panel.setdefault(r.panel_id, []).append(r)
    for pid, runs in by_panel.items():
        for i, r in enumerate(runs):
            if r.status != "success":
                continue
            failure = False
            nxt = None
            for j in range(i + 1, len(runs)):
                if runs[j].status == "failed":
                    failure = True
                elif runs[j].status == "success":
                    nxt = runs[j]
                    break
            if nxt is None:
                continue
            t = _aware(r.finished_at or r.started_at)
            if t is not None:
                drop = max(0, int(r.user_count or 0) - int(nxt.user_count or 0))
                out[pid][t.replace(second=0, microsecond=0).isoformat()] = {
                    "drop": drop, "failure": failure}
    return out


async def detect(
    session: AsyncSession, panel_ids: list[int], *, lookback_days: int = 7
) -> list[dict]:
    """Lost users for `panel_ids`, grouped per panel and clustered by the moment they vanished.

    A rollback loses only RECENTLY-CREATED users (an old user was in the restored backup too, so it's
    still on the panel). So `lookback_days` filters on when the user was ADDED to the system
    (`created_at`) and requires a recent-or-empty `start_date` (empty = never connected yet) — NOT on
    when they were last seen. That's the difference that stops a month-old user who merely disappeared
    today from showing up under a 1-day window."""
    if not panel_ids:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max(1, lookback_days))
    cutoff_date = cutoff.date()
    panels = {p.id: p for p in (await session.execute(
        select(Panel).where(Panel.id.in_(panel_ids)))).scalars().all()}
    names = await _admin_names(session, panel_ids)

    rows = (await session.execute(
        select(EndUserSnapshot).where(
            EndUserSnapshot.panel_id.in_(panel_ids),
            EndUserSnapshot.created_at >= cutoff,   # ADDED to the system within the window
        ).order_by(EndUserSnapshot.panel_id, EndUserSnapshot.last_synced_at)
    )).scalars().all()

    # panel_id -> cluster_key(minute) -> {"last_seen_at", "users"}
    per_panel: dict[int, dict[str, dict]] = {}
    for s in rows:
        p = panels.get(s.panel_id)
        if p is None or p.last_synced_at is None or s.last_synced_at is None:
            continue
        created = _aware(s.created_at)
        if created is None or created < cutoff:
            continue  # not recently added (SQLite-safe backstop for the created_at filter)
        # A recent user's start_date is recent OR empty (never connected). An OLD start_date means an
        # old config that was in the backup — not a rollback casualty.
        if s.start_date is not None and s.start_date < cutoff_date:
            continue
        s_seen, p_seen = _aware(s.last_synced_at), _aware(p.last_synced_at)
        if s_seen >= p_seen - _GONE_MARGIN:
            continue  # still present in the latest sync — not lost
        key = s_seen.replace(second=0, microsecond=0).isoformat()
        bucket = per_panel.setdefault(s.panel_id, {})
        cluster = bucket.setdefault(
            key, {"key": key, "last_seen_at": _iso(s.last_synced_at), "users": []})
        cluster["users"].append(_user_dict(s, names.get((s.panel_id, (s.added_by_uuid or "").lower()))))

    events = await _sync_events(session, panel_ids, cutoff)
    out: list[dict] = []
    for pid, buckets in per_panel.items():
        p = panels[pid]
        clusters = list(buckets.values())
        for c in clusters:
            c["users"].sort(key=lambda u: ((u["admin_name"] or "~"), u["name"]))
            c["count"] = len(c["users"])
            ev = events.get(pid, {}).get(c["key"], {})
            # HINTS only (the owner decides which cluster is their rollback): how many the panel's
            # count dropped at that instant, and whether a sync FAILED then (panel was down = a
            # migration/restore tell). Deliberately NOT auto-classified — on a busy panel the
            # migration loss is the same size as ordinary expiry churn, so a guess would be wrong.
            c["drop_size"] = int(ev.get("drop", 0))
            c["had_failure"] = bool(ev.get("failure", False))
        # Biggest clusters first (the rollback is usually a bunch lost at once), then most recent.
        clusters.sort(key=lambda c: (c["count"], c["last_seen_at"] or ""), reverse=True)
        out.append({
            "panel_id": pid,
            "panel_key": p.key,
            "panel_last_synced_at": _iso(p.last_synced_at),
            "total_lost": sum(c["count"] for c in clusters),
            "clusters": clusters,
        })
    out.sort(key=lambda x: x["panel_id"])
    return out


async def restore(
    session_factory: async_sessionmaker[AsyncSession],
    users: list[tuple[int, str]],
    *,
    dry_run: bool = False,
) -> dict:
    """Re-create each (panel_id, user_uuid) on its panel under its ORIGINAL admin, same uuid. Details
    are re-read from the snapshot (the client only chooses WHICH users), so nothing is injectable. A
    live pre-check skips a user already back. Returns per-user created / skipped / errors."""
    client = AdminApiClient()
    created: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for panel_id, user_uuid in users:
        async with session_factory() as s:
            snap = (await s.execute(select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == panel_id,
                EndUserSnapshot.user_uuid == user_uuid,
            ))).scalars().first()
            panel = await s.get(Panel, panel_id) if snap is not None else None
            reseller = None
            if snap is not None:
                reseller = (await s.execute(select(Reseller).where(
                    Reseller.panel_id == panel_id,
                    func.lower(Reseller.admin_uuid) == (snap.added_by_uuid or "").lower(),
                ))).scalars().first()
            # Detach the values we need before the session closes.
            name = snap.name if snap else None
            gb = float(snap.usage_limit_gb or 0) if snap else 0.0
            days = int(snap.package_days or 30) if snap else 30
            admin_key = reseller.admin_uuid if reseller else None
            admin_name = reseller.name if reseller else None
        label = {"panel_id": panel_id, "user_uuid": user_uuid, "name": name or "",
                 "admin_name": admin_name}
        if snap is None or panel is None:
            errors.append({**label, "reason": "not found in the invoice database"})
            continue
        if reseller is None or not admin_key:
            errors.append({**label, "reason": "its admin no longer exists — can't place it safely"})
            continue
        # LIVE pre-check — already back on the panel? (admin may have re-made it) → skip, never dup.
        try:
            existing = await client.get_user(panel, user_uuid, api_key=admin_key)
        except Exception:  # noqa: BLE001 — a read error is treated as unknown; be safe and skip
            errors.append({**label, "reason": "panel read failed — try again"})
            continue
        if existing:
            skipped.append({**label, "reason": "already present on the panel"})
            continue
        if dry_run:
            created.append({**label, "reason": "would be created"})
            continue
        try:
            await client.create_user(panel, name=name or "user", gb=gb, days=days,
                                     api_key=admin_key, user_uuid=user_uuid)
            created.append(label)
        except Exception as exc:  # noqa: BLE001
            errors.append({**label, "reason": type(exc).__name__ + ": " + str(exc)[:160]})
    if not dry_run:
        log.info("user recovery: created=%d skipped=%d errors=%d",
                 len(created), len(skipped), len(errors))
    return {"created": created, "skipped": skipped, "errors": errors,
            "counts": {"created": len(created), "skipped": len(skipped), "errors": len(errors)}}
