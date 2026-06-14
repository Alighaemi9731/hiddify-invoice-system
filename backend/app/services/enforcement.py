"""
Enforcement: suspend a delinquent reseller (disable their + sub-resellers' users,
zero their admin limits) and restore exactly on payment.

Safety: controlled by the `enforcement_enabled` setting. When False (default), runs
in DRY-RUN — it records what it *would* do (EnforcementAction with dry_run=True) and
makes no panel writes. Set it True to perform live writes (needs panel admin API keys).
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import EndUserSnapshot, EnforcementAction, Panel, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
)
from app.services import settings_service
from app.services.invoice_engine import build_children_map, collect_descendants
from app.services.panel_client.admin_api import AdminApiClient
from app.services.periods import today as tehran_today

log = logging.getLogger("enforcement")

_MAX_RETRIES = 5


# ── low-level helpers ────────────────────────────────────────────────────────

async def _bundle(session: AsyncSession, reseller: Reseller) -> list[Reseller]:
    """The reseller + all descendant sub-resellers (same panel)."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)
    return collect_descendants(reseller, children)


async def _get_admin_limits_safe(
    client: AdminApiClient, panel, admin: Reseller
) -> tuple[int | None, int | None]:
    """Read an admin's current limits from the panel. Returns (None, None) on any error."""
    try:
        return await client.get_admin_limits(
            panel, admin.admin_uuid, api_key=admin.parent_admin_uuid
        )
    except Exception:  # noqa: BLE001
        return None, None


async def _set_admin_limits(
    client: AdminApiClient, panel, admin: Reseller, mu: int, mau: int
) -> None:
    """Set an admin's limits, trying parent-UUID auth first then falling back to panel key."""
    if admin.parent_admin_uuid:
        try:
            await client.set_admin_limits(
                panel, admin.admin_uuid, mu, mau, api_key=admin.parent_admin_uuid
            )
            return
        except Exception:  # noqa: BLE001
            pass
    await client.set_admin_limits(panel, admin.admin_uuid, mu, mau)


async def _enabled_users(
    session: AsyncSession, panel_id: int, admin_uuids: set[str]
) -> list[EndUserSnapshot]:
    rows = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == panel_id,
                EndUserSnapshot.added_by_uuid.in_(admin_uuids),
                EndUserSnapshot.enable.is_(True),
            )
        )
    ).scalars().all()
    return list(rows)


def _progress(snapshot: dict | None) -> dict:
    if snapshot is None:
        snapshot = {}
    p = snapshot.setdefault("progress", {})
    p.setdefault("users_done", [])
    p.setdefault("users_failed", {})
    p.setdefault("users_missing", [])
    p.setdefault("admins_done", [])
    p.setdefault("admins_failed", {})
    p.setdefault("admin_attempts", {})
    p.setdefault("user_attempts", {})
    p.setdefault("captured_limits", {})
    p.setdefault("admins_missing", [])
    p.setdefault("phase", "users")
    return p


async def _has_due_invoice(session: AsyncSession, reseller_id: int) -> bool:
    """Re-check debt at execution time so a stale queue item cannot suspend a paid reseller."""
    from app.models import Invoice
    from app.models.enums import InvoiceStatus

    owed = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
    today = tehran_today()
    invoices = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller_id,
                Invoice.status.in_(owed),
            )
        )
    ).scalars().all()
    return any(not (inv.deferred_until and inv.deferred_until > today) for inv in invoices)


async def _queued_snapshot(session: AsyncSession, reseller: Reseller) -> dict:
    """Build a DB-local work snapshot without writing to the panel."""
    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        raise ValueError("panel not found for reseller")
    descendants = await _bundle(session, reseller)
    admin_uuids = {d.admin_uuid for d in descendants}
    users = await _enabled_users(session, panel.id, admin_uuids)
    snapshot: dict = {
        "limits": {
            d.admin_uuid: {
                "max_users": d.panel_max_users,
                "max_active_users": d.panel_max_active_users,
            }
            for d in descendants
        },
        "admins": [d.admin_uuid for d in descendants],
        "users": {u.user_uuid: (u.added_by_uuid or "") for u in users},
    }
    _progress(snapshot)
    return snapshot


# ── inner worker helpers ─────────────────────────────────────────────────────

async def _run_user_chunks(
    *,
    session: AsyncSession,
    action: EnforcementAction,
    client: AdminApiClient,
    panel,
    snapshot: dict,
    progress: dict,
    users_map: dict[str, str],
    done_users: set[str],
    missing_users: set[str],
    failed_users: dict[str, str],
    user_attempts: dict[str, int],
    enable: bool,
    chunk_size: int,
) -> tuple[int, bool]:
    """Disable or enable all remaining users in a loop, committing after each chunk.

    Returns (users_patched, had_error). On error the progress is persisted so the
    next worker tick resumes from exactly where this one stopped — never repeating
    a chunk that already succeeded.
    """
    remaining = [u for u in users_map if u not in done_users and u not in missing_users]
    if not remaining:
        return 0, False

    # Resolve UUID → Hiddify numeric-ID mapping, cached in the snapshot so retries
    # don't re-fetch.
    panel_user_ids: dict[str, int] = {
        str(uuid): int(uid)
        for uuid, uid in (snapshot.get("panel_user_ids") or {}).items()
    }
    if any(uuid not in panel_user_ids for uuid in remaining):
        try:
            fetched = await client.get_user_ids(panel)
            panel_user_ids.update(
                {uuid: fetched[uuid] for uuid in users_map if uuid in fetched}
            )
            snapshot["panel_user_ids"] = panel_user_ids
        except Exception as exc:  # noqa: BLE001
            user_attempts["__lookup__"] = user_attempts.get("__lookup__", 0) + 1
            action.error = f"bulk user id lookup failed: {str(exc)[:900]}"
            progress["user_attempts"] = user_attempts
            if user_attempts["__lookup__"] >= _MAX_RETRIES:
                action.status = EnforcementActionStatus.failed
            action.snapshot = snapshot
            flag_modified(action, "snapshot")
            await session.commit()
            return 0, True

    for uuid in list(remaining):
        if uuid not in panel_user_ids:
            missing_users.add(uuid)
            done_users.add(uuid)
    remaining = [u for u in remaining if u in panel_user_ids and u not in missing_users]

    snapshot_rows: dict[str, EndUserSnapshot] = {}
    if remaining:
        snapshot_rows = {
            r.user_uuid: r
            for r in (
                await session.execute(
                    select(EndUserSnapshot).where(
                        EndUserSnapshot.panel_id == panel.id,
                        EndUserSnapshot.user_uuid.in_(remaining),
                    )
                )
            ).scalars().all()
        }

    total_patched = 0
    verb = "enable" if enable else "disable"
    while remaining:
        chunk = remaining[:max(1, chunk_size)]
        try:
            await client.bulk_set_users_enabled(
                panel, [panel_user_ids[u] for u in chunk], enable
            )
            for uuid in chunk:
                if uuid in snapshot_rows:
                    snapshot_rows[uuid].enable = enable
                done_users.add(uuid)
                failed_users.pop(uuid, None)
            total_patched += len(chunk)
            action.error = None
        except Exception as exc:  # noqa: BLE001
            for uuid in chunk:
                user_attempts[uuid] = user_attempts.get(uuid, 0) + 1
                failed_users[uuid] = str(exc)[:300]
            if any(user_attempts[uuid] >= _MAX_RETRIES for uuid in chunk):
                action.status = EnforcementActionStatus.failed
                action.error = f"bulk {verb} failed: {str(exc)[:900]}"
            else:
                action.error = f"bulk {verb} failed (will retry): {str(exc)[:600]}"
            progress["users_done"] = sorted(done_users)
            progress["users_missing"] = sorted(missing_users)
            progress["users_failed"] = failed_users
            progress["user_attempts"] = user_attempts
            action.affected_count = len(done_users - missing_users)
            action.snapshot = snapshot
            flag_modified(action, "snapshot")
            await session.commit()
            return total_patched, True

        # Commit after each successful chunk — a restart resumes from here rather than
        # re-disabling/re-enabling users that already succeeded.
        progress["users_done"] = sorted(done_users)
        progress["users_missing"] = sorted(missing_users)
        progress["users_failed"] = failed_users
        progress["user_attempts"] = user_attempts
        action.affected_count = len(done_users - missing_users)
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()
        remaining = [u for u in remaining if u not in done_users]

    return total_patched, False


async def _run_admin_limits(
    *,
    session: AsyncSession,
    action: EnforcementAction,
    client: AdminApiClient,
    panel,
    snapshot: dict,
    progress: dict,
    by_uuid: dict[str, Reseller],
    admin_order: list[str],
    done_admins: set[str],
    failed_admins: dict[str, str],
    admin_attempts: dict[str, int],
    captured_limits: dict[str, dict],
    is_suspend: bool,
    parallelism: int,
) -> tuple[int, bool]:
    """Patch all remaining admin limits in parallel (bounded by parallelism).

    For suspend: captures real current limits then zeros them.
    For restore: reads saved limits from snapshot then restores them.
    Returns (admins_patched, had_error). Commits progress on any error so the next
    tick retries only the failed admins.
    """
    remaining = [u for u in admin_order if u not in done_admins]
    if not remaining:
        return 0, False

    sem = asyncio.Semaphore(max(1, parallelism))

    async def _patch_one(admin_uuid: str) -> tuple[str, str | None, dict | None]:
        """Returns (uuid, error_or_None, limits_dict_or_None)."""
        async with sem:
            admin = by_uuid.get(admin_uuid)
            if admin is None:
                return admin_uuid, "__missing__", None

            if is_suspend:
                real_mu, real_mau = await _get_admin_limits_safe(client, panel, admin)
                if real_mu is None:
                    real_mu = admin.panel_max_users
                if real_mau is None:
                    real_mau = admin.panel_max_active_users
                if not real_mu and admin.max_users_snapshot:
                    real_mu = admin.max_users_snapshot
                if not real_mau and admin.max_active_users_snapshot:
                    real_mau = admin.max_active_users_snapshot
                if real_mu is None or real_mau is None:
                    return admin_uuid, "current admin limits could not be captured", None
                lim: dict = {"max_users": real_mu, "max_active_users": real_mau}
                try:
                    await _set_admin_limits(client, panel, admin, 0, 0)
                    return admin_uuid, None, lim
                except Exception as exc:  # noqa: BLE001
                    return admin_uuid, str(exc)[:300], None
            else:
                lim = captured_limits.get(admin_uuid) or {}
                mu = lim.get("max_users") or admin.max_users_snapshot
                mau = lim.get("max_active_users") or admin.max_active_users_snapshot
                if mu is None or mau is None:
                    return admin_uuid, "saved admin limits are incomplete", None
                try:
                    await _set_admin_limits(client, panel, admin, int(mu), int(mau))
                    return admin_uuid, None, lim
                except Exception as exc:  # noqa: BLE001
                    return admin_uuid, str(exc)[:300], None

    outcomes = await asyncio.gather(*[_patch_one(u) for u in remaining])

    any_hard_fail = False
    patched = 0
    for admin_uuid, error, lim in outcomes:
        if error is None:
            if is_suspend and lim is not None:
                admin = by_uuid.get(admin_uuid)
                if admin is not None:
                    admin.max_users_snapshot = lim["max_users"]
                    admin.max_active_users_snapshot = lim["max_active_users"]
                captured_limits[admin_uuid] = lim
            done_admins.add(admin_uuid)
            failed_admins.pop(admin_uuid, None)
            patched += 1
        elif error == "__missing__":
            done_admins.add(admin_uuid)
            if admin_uuid not in progress["admins_missing"]:
                progress["admins_missing"].append(admin_uuid)
        else:
            admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
            failed_admins[admin_uuid] = error
            if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                any_hard_fail = True

    progress["admins_done"] = sorted(done_admins)
    progress["admins_failed"] = failed_admins
    progress["admin_attempts"] = admin_attempts
    if is_suspend:
        progress["captured_limits"] = captured_limits
        action.snapshot = {**snapshot, "limits": captured_limits or snapshot.get("limits", {})}
    else:
        action.snapshot = snapshot
    flag_modified(action, "snapshot")

    if any_hard_fail:
        action.status = EnforcementActionStatus.failed
        action.error = (
            f"admin limit failed for: {', '.join(list(failed_admins)[:10])}"[:1000]
        )
        await session.commit()
        return patched, True

    if failed_admins:
        action.status = EnforcementActionStatus.partial
        action.error = f"{len(failed_admins)} admin limit failure(s), will retry"
        await session.commit()
        return patched, True

    await session.commit()
    return patched, False


# ── queue API ────────────────────────────────────────────────────────────────

async def queue_enforcement(
    session: AsyncSession,
    reseller: Reseller,
    *,
    invoice_id: int | None = None,
    dry_run: bool | None = None,
) -> EnforcementAction:
    """Plan an enforcement action without doing panel writes.

    Dry-run actions are finalized immediately. Live actions are durable queue items
    that the enforcement worker processes in resumable chunks.
    """
    enabled = await settings_service.get(session, "enforcement_enabled", False)
    is_dry = (not enabled) if dry_run is None else dry_run

    if invoice_id is not None:
        criteria = [
            EnforcementAction.invoice_id == invoice_id,
            EnforcementAction.action == EnforcementActionType.disable_users,
        ]
        if not is_dry:
            # A previous dry-run is only an audit record. It must not block the first
            # real queued enforcement after the operator enables enforcement.
            criteria.append(EnforcementAction.dry_run.is_(False))
            criteria.append(
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.done,
                        EnforcementActionStatus.failed,
                    ]
                )
            )
        else:
            criteria.append(EnforcementAction.status == EnforcementActionStatus.dry_run)
        existing = (
            await session.execute(
                select(EnforcementAction)
                .where(*criteria)
                .order_by(EnforcementAction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if not is_dry and reseller.enforcement_state == EnforcementState.enforced:
        prior = (
            await session.execute(
                select(EnforcementAction)
                .where(
                    EnforcementAction.reseller_id == reseller.id,
                    EnforcementAction.action == EnforcementActionType.disable_users,
                    EnforcementAction.status == EnforcementActionStatus.done,
                )
                .order_by(EnforcementAction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior is not None:
            return prior

    snapshot = await _queued_snapshot(session, reseller)
    action = EnforcementAction(
        reseller_id=reseller.id,
        invoice_id=invoice_id,
        action=EnforcementActionType.disable_users,
        dry_run=is_dry,
        affected_count=len(snapshot.get("users") or {}),
        snapshot=snapshot,
        status=EnforcementActionStatus.dry_run if is_dry else EnforcementActionStatus.planned,
    )
    session.add(action)
    await session.commit()
    if is_dry:
        log.info(
            "[dry-run] queued enforcement intent for reseller %s: %d users, %d admins",
            reseller.name,
            len(snapshot.get("users") or {}),
            len(snapshot.get("admins") or []),
        )
    return action


async def queue_restore(
    session: AsyncSession,
    reseller: Reseller,
    *,
    require_no_due: bool = False,
    reason: str = "manual",
) -> EnforcementAction | None:
    """Queue an exact, resumable restore and cancel any still-running suspension."""
    existing = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == reseller.id,
                EnforcementAction.action == EnforcementActionType.restore,
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.failed,
                    ]
                ),
            )
            .order_by(EnforcementAction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == EnforcementActionStatus.failed:
            snap = existing.snapshot or {}
            prog = _progress(snap)
            prog["users_failed"] = {}
            prog["user_attempts"] = {}
            prog["admins_failed"] = {}
            prog["admin_attempts"] = {}
            snap["require_no_due"] = require_no_due
            snap["reason"] = reason
            existing.snapshot = snap
            existing.status = EnforcementActionStatus.planned
            existing.error = None
            flag_modified(existing, "snapshot")
            await session.commit()
        return existing

    source = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == reseller.id,
                EnforcementAction.action == EnforcementActionType.disable_users,
                EnforcementAction.dry_run.is_(False),
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.done,
                        EnforcementActionStatus.failed,
                    ]
                ),
            )
            .order_by(EnforcementAction.created_at.desc(), EnforcementAction.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if source is None:
        return None

    source_snapshot = deepcopy(source.snapshot or {})
    source_progress = _progress(source_snapshot)

    if source.status == EnforcementActionStatus.planned:
        source.status = EnforcementActionStatus.reverted
        await session.commit()
        return None

    users_map: dict[str, str] = dict(source_snapshot.get("users") or {})
    admins = list(source_snapshot.get("admins") or [])
    limits = dict(
        source_progress.get("captured_limits") or source_snapshot.get("limits") or {}
    )

    if source.status in (
        EnforcementActionStatus.partial,
        EnforcementActionStatus.failed,
    ):
        completed_users = set(source_progress.get("users_done") or [])
        missing_users_set = set(source_progress.get("users_missing") or [])
        completed_admins = set(source_progress.get("admins_done") or [])
        users_map = {
            uuid: owner
            for uuid, owner in users_map.items()
            if uuid in completed_users and uuid not in missing_users_set
        }
        admins = [uuid for uuid in admins if uuid in completed_admins]
        limits = {uuid: limits[uuid] for uuid in admins if uuid in limits}
        source.status = EnforcementActionStatus.reverted
        if not users_map and not admins:
            await session.commit()
            return None
    elif not admins:
        descendants = await _bundle(session, reseller)
        admins = [d.admin_uuid for d in descendants if d.admin_uuid in limits]

    snapshot: dict = {
        "limits": limits,
        "admins": admins,
        "users": users_map,
        "source_action_id": source.id,
        "require_no_due": require_no_due,
        "reason": reason,
        "progress": {
            "phase": "limits",
            "users_done": [],
            "users_missing": [],
            "users_failed": {},
            "user_attempts": {},
            "admins_done": [],
            "admins_missing": [],
            "admins_failed": {},
            "admin_attempts": {},
            "captured_limits": limits,
        },
    }
    restore = EnforcementAction(
        reseller_id=reseller.id,
        invoice_id=source.invoice_id,
        action=EnforcementActionType.restore,
        dry_run=False,
        snapshot=snapshot,
        status=EnforcementActionStatus.planned,
    )
    session.add(restore)
    reseller.enforcement_state = EnforcementState.enforced
    await session.commit()
    return restore


# ── worker actions ───────────────────────────────────────────────────────────

async def _process_enforcement_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
    admin_parallelism: int,
) -> dict:
    """Process one queued live enforcement (suspend) action.

    Phase 1 — users: all remaining chunks processed in a loop, commit after each.
    Phase 2 — admin limits: all remaining admins patched in parallel.
    A failure in either phase commits progress and returns partial/failed so the
    next worker tick can resume exactly where this one stopped.
    """
    if action.dry_run or action.action != EnforcementActionType.disable_users:
        return {"skipped": 1}
    if action.status == EnforcementActionStatus.done:
        return {"done": 1}

    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        action.status = EnforcementActionStatus.failed
        action.error = "reseller not found"
        await session.commit()
        return {"failed": 1}

    if action.invoice_id is not None and not await _has_due_invoice(session, reseller.id):
        restore = await queue_restore(
            session, reseller, require_no_due=False, reason="disable-canceled-no-debt"
        )
        if restore is None:
            action.status = EnforcementActionStatus.reverted
            await session.commit()
            return {"skipped": 1}
        return {"restore_queued": 1}

    if reseller.enforcement_state == EnforcementState.enforced:
        action.status = EnforcementActionStatus.done
        await session.commit()
        return {"done": 1}

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        return {"failed": 1}

    snapshot = action.snapshot or await _queued_snapshot(session, reseller)
    progress = _progress(snapshot)
    users_map: dict[str, str] = dict(snapshot.get("users") or {})
    done_users: set[str] = set(progress.get("users_done") or [])
    missing_users: set[str] = set(progress.get("users_missing") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})
    client = AdminApiClient()

    action.status = EnforcementActionStatus.partial
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()

    result: dict = {"patched_users": 0, "patched_admins": 0}

    # ── Phase 1: disable users ────────────────────────────────────────────────
    if progress.get("phase") in ("users", None):
        patched_u, had_error = await _run_user_chunks(
            session=session, action=action, client=client, panel=panel,
            snapshot=snapshot, progress=progress,
            users_map=users_map, done_users=done_users, missing_users=missing_users,
            failed_users=failed_users, user_attempts=user_attempts,
            enable=False, chunk_size=user_chunk_size,
        )
        result["patched_users"] = patched_u
        if had_error:
            result["partial"] = int(action.status == EnforcementActionStatus.partial)
            result["failed"] = int(action.status == EnforcementActionStatus.failed)
            return result

        progress["phase"] = "limits"
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()

    # ── Phase 2: zero admin limits (parallel) ────────────────────────────────
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    # Bottom-up (leaf → root): children lose quota first so they can't create new
    # users while the parent still has capacity.
    admin_order = list(
        reversed(snapshot.get("admins") or [d.admin_uuid for d in descendants])
    )
    done_admins: set[str] = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or {})

    patched_a, had_error = await _run_admin_limits(
        session=session, action=action, client=client, panel=panel,
        snapshot=snapshot, progress=progress,
        by_uuid=by_uuid, admin_order=admin_order,
        done_admins=done_admins, failed_admins=failed_admins,
        admin_attempts=admin_attempts, captured_limits=captured_limits,
        is_suspend=True, parallelism=admin_parallelism,
    )
    result["patched_admins"] = patched_a
    if had_error:
        result["partial"] = int(action.status == EnforcementActionStatus.partial)
        result["failed"] = int(action.status == EnforcementActionStatus.failed)
        return result

    # ── Finalize ─────────────────────────────────────────────────────────────
    if not done_users and not done_admins:
        action.status = EnforcementActionStatus.failed
        action.error = "enforcement did nothing"
        await session.commit()
        return {"failed": 1}

    reseller.enforcement_state = EnforcementState.enforced
    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.affected_count = len(done_users - missing_users)
    # panel_user_ids is only a retry cache; strip it from the stored snapshot so the row
    # doesn't hold ~100 KB of integer-ID mappings that have no audit value after completion.
    snapshot.pop("panel_user_ids", None)
    action.snapshot = snapshot
    flag_modified(action, "snapshot")

    if action.invoice_id:
        from app.models import Invoice
        from app.models.enums import InvoiceStatus

        inv = await session.get(Invoice, action.invoice_id)
        if inv is not None:
            inv.status = InvoiceStatus.enforced

    await session.commit()
    log.info(
        "Enforcement done for reseller %s: %d users disabled, %d admins zeroed",
        reseller.name,
        len(done_users - missing_users),
        len(done_admins),
    )
    result["done"] = 1
    return result


async def _process_restore_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
    admin_parallelism: int,
) -> dict:
    """Process one queued restore action.

    Phase 1 — admin limits: all admins restored in parallel (bounded by admin_parallelism).
    Phase 2 — users: all remaining chunks in a loop, commit after each.
    The reseller is only flipped to active once ALL users are re-enabled — a partial
    restore leaves enforcement_state=enforced so the next trigger retries cleanly.
    """
    if action.dry_run or action.action != EnforcementActionType.restore:
        return {"skipped": 1}

    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        action.status = EnforcementActionStatus.failed
        action.error = "reseller not found"
        await session.commit()
        return {"failed": 1}

    snapshot = action.snapshot or {}
    if snapshot.get("require_no_due") and await _has_due_invoice(session, reseller.id):
        action.status = EnforcementActionStatus.failed
        action.error = "restore canceled: reseller still has a due invoice"
        await session.commit()
        return {"failed": 1}

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        return {"failed": 1}

    progress = _progress(snapshot)
    action.status = EnforcementActionStatus.partial
    client = AdminApiClient()
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    limits: dict[str, dict] = dict(snapshot.get("limits") or {})
    # Top-down (root → leaf): parent quotas restored first so children's quota
    # is meaningful as soon as they get it back.
    admins = list(snapshot.get("admins") or limits)
    done_admins: set[str] = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or limits)

    result: dict = {"restored_users": 0, "restored_admins": 0}

    # ── Phase 1: restore admin limits (parallel) ─────────────────────────────
    if progress.get("phase") == "limits":
        patched_a, had_error = await _run_admin_limits(
            session=session, action=action, client=client, panel=panel,
            snapshot=snapshot, progress=progress,
            by_uuid=by_uuid, admin_order=admins,
            done_admins=done_admins, failed_admins=failed_admins,
            admin_attempts=admin_attempts, captured_limits=captured_limits,
            is_suspend=False, parallelism=admin_parallelism,
        )
        result["restored_admins"] = patched_a
        if had_error:
            result["partial"] = int(action.status == EnforcementActionStatus.partial)
            result["failed"] = int(action.status == EnforcementActionStatus.failed)
            return result

        progress["phase"] = "users"
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()

    # ── Phase 2: re-enable users ──────────────────────────────────────────────
    users_map: dict[str, str] = dict(snapshot.get("users") or {})
    done_users: set[str] = set(progress.get("users_done") or [])
    missing_users: set[str] = set(progress.get("users_missing") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})

    patched_u, had_error = await _run_user_chunks(
        session=session, action=action, client=client, panel=panel,
        snapshot=snapshot, progress=progress,
        users_map=users_map, done_users=done_users, missing_users=missing_users,
        failed_users=failed_users, user_attempts=user_attempts,
        enable=True, chunk_size=user_chunk_size,
    )
    result["restored_users"] = patched_u
    if had_error:
        result["partial"] = int(action.status == EnforcementActionStatus.partial)
        result["failed"] = int(action.status == EnforcementActionStatus.failed)
        return result

    # ── Finalize ─────────────────────────────────────────────────────────────
    reseller.enforcement_state = EnforcementState.active
    for descendant in descendants:
        descendant.max_users_snapshot = None
        descendant.max_active_users_snapshot = None

    from app.models import Invoice
    from app.models.enums import InvoiceStatus

    enforced_invoices = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller.id,
                Invoice.status == InvoiceStatus.enforced,
            )
        )
    ).scalars().all()
    for invoice in enforced_invoices:
        invoice.status = InvoiceStatus.overdue

    source_id = snapshot.get("source_action_id")
    if source_id:
        src = await session.get(EnforcementAction, int(source_id))
        if src is not None:
            src.status = EnforcementActionStatus.reverted

    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.affected_count = len(done_users - missing_users)
    snapshot.pop("panel_user_ids", None)
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()
    log.info(
        "Restore done for reseller %s: %d users enabled, %d admins restored",
        reseller.name,
        patched_u,
        result["restored_admins"],
    )
    result["done"] = 1
    return result


async def process_enforcement_queue(
    session: AsyncSession,
    *,
    action_limit: int | None = None,
    user_chunk_size: int | None = None,
    admin_chunk_size: int | None = None,
) -> dict:
    """Pick up to `action_limit` pending enforcement/restore actions and process each.

    `admin_chunk_size` controls the maximum number of concurrent admin-limit API calls
    (semaphore size). All remaining admins for an action are attempted in one worker
    invocation — this parameter only bounds parallelism, not batch count.
    """
    cfg = await settings_service.get_many(
        session,
        [
            "enforcement_action_batch_limit",
            "enforcement_user_chunk_size",
            "enforcement_admin_chunk_size",
        ],
    )
    limit = max(1, int(action_limit or cfg.get("enforcement_action_batch_limit") or 1))
    chunk = max(1, int(user_chunk_size or cfg.get("enforcement_user_chunk_size") or 500))
    para = max(1, int(admin_chunk_size or cfg.get("enforcement_admin_chunk_size") or 10))

    # Prune terminal enforcement_action rows older than 30 days to keep the table lean.
    # Rows still hold full user-UUID lists; after 30 days they have no operational value.
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    await session.execute(
        delete(EnforcementAction)
        .where(
            EnforcementAction.status.in_(
                [EnforcementActionStatus.done, EnforcementActionStatus.reverted]
            ),
            EnforcementAction.created_at < cutoff,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    actions = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.action.in_(
                    [EnforcementActionType.disable_users, EnforcementActionType.restore]
                ),
                EnforcementAction.dry_run.is_(False),
                EnforcementAction.status.in_(
                    [EnforcementActionStatus.planned, EnforcementActionStatus.partial]
                ),
            )
            .order_by(
                # Restores first so paying customers are un-blocked before new suspensions run.
                case(
                    (EnforcementAction.action == EnforcementActionType.restore, 0),
                    else_=1,
                ),
                EnforcementAction.created_at,
                EnforcementAction.id,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()

    result: dict = {
        "picked": len(actions),
        "done": 0, "partial": 0, "failed": 0, "skipped": 0,
        "patched_users": 0, "failed_users": 0, "patched_admins": 0,
        "restored_users": 0, "restored_admins": 0, "restore_queued": 0,
    }
    for action in actions:
        if action.action == EnforcementActionType.restore:
            step = await _process_restore_action(
                session, action,
                user_chunk_size=chunk,
                admin_parallelism=para,
            )
        else:
            step = await _process_enforcement_action(
                session, action,
                user_chunk_size=chunk,
                admin_parallelism=para,
            )
        for key in result:
            if key != "picked":
                result[key] += int(step.get(key, 0) or 0)
    return result


# ── public API (thin wrappers) ───────────────────────────────────────────────

async def enforce_reseller(
    session: AsyncSession,
    reseller: Reseller,
    *,
    dry_run: bool | None = None,
    invoice_id: int | None = None,
) -> EnforcementAction:
    """Queue a suspension so API and bot requests never wait for panel writes."""
    return await queue_enforcement(
        session, reseller, invoice_id=invoice_id, dry_run=dry_run
    )


async def restore_reseller(
    session: AsyncSession, reseller: Reseller
) -> EnforcementAction | None:
    """Compatibility wrapper: restore is always durable and asynchronous."""
    return await queue_restore(session, reseller)
