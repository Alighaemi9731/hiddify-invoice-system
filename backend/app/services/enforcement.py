"""
Enforcement: suspend a delinquent reseller (disable their + sub-resellers' users,
zero their admin limits) and restore exactly on payment.

Safety: controlled by the `enforcement_enabled` setting. When False (default), runs
in DRY-RUN — it records what it *would* do (EnforcementAction with dry_run=True) and
makes no panel writes. Set it True to perform live writes (needs panel admin API keys).
"""
from __future__ import annotations

import logging
from copy import deepcopy

from sqlalchemy import case, select
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


async def _bundle(session: AsyncSession, reseller: Reseller) -> list[Reseller]:
    """The reseller + all descendant sub-resellers (same panel)."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)
    return collect_descendants(reseller, children)


async def _set_admin_limits(client: AdminApiClient, panel, admin: Reseller, mu: int, mau: int) -> None:
    """Set an admin's limits, authenticating AS that admin's PARENT (has_permission for an
    AdminUser passes when parent_admin_id == the acting account). Falls back to panel key."""
    parent = admin.parent_admin_uuid
    if parent:
        try:
            await client.set_admin_limits(panel, admin.admin_uuid, mu, mau, api_key=parent)
            return
        except Exception:  # noqa: BLE001
            pass
    await client.set_admin_limits(panel, admin.admin_uuid, mu, mau)


async def _enabled_users(session: AsyncSession, panel_id: int, admin_uuids: set[str]) -> list[EndUserSnapshot]:
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
    progress = snapshot.setdefault("progress", {})
    progress.setdefault("users_done", [])
    progress.setdefault("users_failed", {})
    progress.setdefault("users_missing", [])
    progress.setdefault("admins_done", [])
    progress.setdefault("admins_failed", {})
    progress.setdefault("admin_attempts", {})
    progress.setdefault("user_attempts", {})
    progress.setdefault("captured_limits", {})
    progress.setdefault("phase", "users")
    return progress


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
    """Build a DB-local work snapshot without writing to the panel.

    It captures the exact bundle and enabled users at planning time. Live limit values are
    still re-read right before zeroing each admin, because sync data can be stale.
    """
    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        raise ValueError("panel not found for reseller")
    descendants = await _bundle(session, reseller)
    admin_uuids = {d.admin_uuid for d in descendants}
    users = await _enabled_users(session, panel.id, admin_uuids)
    snapshot = {
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


async def queue_enforcement(
    session: AsyncSession,
    reseller: Reseller,
    *,
    invoice_id: int | None = None,
    dry_run: bool | None = None,
) -> EnforcementAction:
    """Plan an enforcement action without doing panel writes.

    Dry-run actions are finalized immediately. Live actions are durable queue items that the
    enforcement worker processes in small, resumable chunks.
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
            criteria.append(
                EnforcementAction.status == EnforcementActionStatus.dry_run
            )
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
            reseller.name, len(snapshot.get("users") or {}), len(snapshot.get("admins") or []),
        )
    return action


async def _process_enforcement_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
) -> dict:
    """Process one queued live enforcement action.

    The function commits after each chunk, so a restart or timeout resumes from
    `snapshot.progress` instead of repeating the whole bundle.
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
            session,
            reseller,
            require_no_due=False,
            reason="disable-canceled-no-debt",
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
    done_users = set(progress.get("users_done") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})
    client = AdminApiClient()
    action.status = EnforcementActionStatus.partial
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()

    remaining_users = [uuid for uuid in users_map if uuid not in done_users]
    patched = 0
    rows = {}
    if remaining_users:
        panel_user_ids: dict[str, int] = {
            str(uuid): int(user_id)
            for uuid, user_id in (snapshot.get("panel_user_ids") or {}).items()
        }
        if any(uuid not in panel_user_ids for uuid in remaining_users):
            try:
                current_user_ids = await client.get_user_ids(panel)
                panel_user_ids.update(
                    {
                        uuid: current_user_ids[uuid]
                        for uuid in users_map
                        if uuid in current_user_ids
                    }
                )
                snapshot["panel_user_ids"] = panel_user_ids
            except Exception as exc:  # noqa: BLE001
                user_attempts["__lookup__"] = user_attempts.get("__lookup__", 0) + 1
                action.status = EnforcementActionStatus.partial
                action.error = f"bulk user id lookup failed: {str(exc)[:900]}"
                progress["user_attempts"] = user_attempts
                if user_attempts["__lookup__"] >= _MAX_RETRIES:
                    action.status = EnforcementActionStatus.failed
                action.snapshot = snapshot
                flag_modified(action, "snapshot")
                await session.commit()
                return {
                    "failed": 1,
                    "partial": int(action.status == EnforcementActionStatus.partial),
                }

        missing_users = set(progress.get("users_missing") or [])
        for uuid in remaining_users:
            if uuid not in panel_user_ids:
                # The local sync snapshot can contain a user deleted from Hiddify after
                # planning. There is nothing left to disable, so record and skip it.
                missing_users.add(uuid)
                done_users.add(uuid)
        progress["users_missing"] = sorted(missing_users)
        remaining_users = [uuid for uuid in remaining_users if uuid in panel_user_ids]
        chunk = remaining_users[:max(1, user_chunk_size)]
        rows = {
            r.user_uuid: r for r in (
                await session.execute(
                    select(EndUserSnapshot).where(
                        EndUserSnapshot.panel_id == panel.id,
                        EndUserSnapshot.user_uuid.in_(chunk),
                    )
                )
            ).scalars().all()
        }
        if chunk:
            try:
                await client.bulk_set_users_enabled(
                    panel, [panel_user_ids[uuid] for uuid in chunk], False
                )
                for uuid in chunk:
                    if uuid in rows:
                        rows[uuid].enable = False
                    done_users.add(uuid)
                    failed_users.pop(uuid, None)
                    patched += 1
                action.error = None
            except Exception as exc:  # noqa: BLE001
                # Keep every UUID pending. The next scheduler pass retries this same batch;
                # never fall back to per-user PATCHes and accidentally overload the panel.
                for uuid in chunk:
                    user_attempts[uuid] = user_attempts.get(uuid, 0) + 1
                    failed_users[uuid] = str(exc)[:300]
                action.status = EnforcementActionStatus.partial
                action.error = f"bulk disable failed: {str(exc)[:900]}"
                progress["users_failed"] = failed_users
                progress["user_attempts"] = user_attempts
                if any(user_attempts[uuid] >= _MAX_RETRIES for uuid in chunk):
                    action.status = EnforcementActionStatus.failed
                action.snapshot = snapshot
                flag_modified(action, "snapshot")
                await session.commit()
                return {
                    "failed": 1,
                    "partial": int(action.status == EnforcementActionStatus.partial),
                }
        for uuid in missing_users:
            if uuid in rows:
                rows[uuid].enable = False
        progress["users_done"] = sorted(done_users)
        progress["users_failed"] = failed_users
        progress["user_attempts"] = user_attempts
        progress["phase"] = "users" if len(done_users) < len(users_map) else "limits"
        action.status = EnforcementActionStatus.partial
        action.affected_count = len(done_users - missing_users)
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()
        return {"patched_users": patched, "failed_users": len(failed_users), "partial": 1}

    # User phase complete. Zero admin limits bottom-up, one admin per worker tick. This keeps
    # large hierarchies resumable and avoids one long request storm against a panel.
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    admin_order = list(reversed(snapshot.get("admins") or [d.admin_uuid for d in descendants]))
    done_admins = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or {})
    patched_admins = 0
    for admin_uuid in admin_order:
        if admin_uuid in done_admins:
            continue
        admin = by_uuid.get(admin_uuid)
        if admin is None:
            failed_admins[admin_uuid] = "admin row not found"
            action.status = EnforcementActionStatus.failed
            action.error = "admin row not found while applying limits"
            break
        try:
            real_mu, real_mau = await client.get_admin_limits(
                panel, admin.admin_uuid, api_key=admin.parent_admin_uuid
            )
        except Exception:  # noqa: BLE001
            real_mu = real_mau = None
        if real_mu is None:
            real_mu = admin.panel_max_users
        if real_mau is None:
            real_mau = admin.panel_max_active_users
        if not real_mu and admin.max_users_snapshot:
            real_mu = admin.max_users_snapshot
        if not real_mau and admin.max_active_users_snapshot:
            real_mau = admin.max_active_users_snapshot
        if real_mu is None or real_mau is None:
            admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
            failed_admins[admin_uuid] = "current admin limits could not be captured"
            if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                action.status = EnforcementActionStatus.failed
                action.error = (
                    f"admin limits could not be captured after {_MAX_RETRIES} attempts"
                )
            break
        admin.max_users_snapshot = real_mu
        admin.max_active_users_snapshot = real_mau
        captured_limits[admin_uuid] = {"max_users": real_mu, "max_active_users": real_mau}
        try:
            await _set_admin_limits(client, panel, admin, 0, 0)
            done_admins.add(admin_uuid)
            failed_admins.pop(admin_uuid, None)
            patched_admins = 1
        except Exception as exc:  # noqa: BLE001
            admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
            failed_admins[admin_uuid] = str(exc)[:300]
            if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                action.status = EnforcementActionStatus.failed
                action.error = (
                    f"admin limit enforcement failed after {_MAX_RETRIES} attempts"
                )
        break

    progress["admins_done"] = sorted(done_admins)
    progress["admins_failed"] = failed_admins
    progress["admin_attempts"] = admin_attempts
    progress["captured_limits"] = captured_limits
    action.snapshot = {**snapshot, "limits": captured_limits or snapshot.get("limits", {})}
    flag_modified(action, "snapshot")
    if action.status == EnforcementActionStatus.failed:
        await session.commit()
        return {"failed": 1}
    if len(done_admins) < len(admin_order):
        action.status = EnforcementActionStatus.partial
        action.affected_count = len(done_users)
        await session.commit()
        return {"patched_admins": patched_admins, "partial": 1}

    if len(done_users) == 0 and reseller.admin_uuid not in done_admins:
        action.status = EnforcementActionStatus.failed
        action.error = "enforcement did nothing"
        await session.commit()
        return {"failed": 1}

    missing_users = set(progress.get("users_missing") or [])
    action.affected_count = len(done_users - missing_users)
    if failed_users:
        action.status = EnforcementActionStatus.failed
        action.error = (
            f"{len(failed_users)} user failure(s)"
        )[:1000]
        await session.commit()
        return {"failed": 1}

    reseller.enforcement_state = EnforcementState.enforced
    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    if action.invoice_id:
        from app.models import Invoice
        from app.models.enums import InvoiceStatus

        inv = await session.get(Invoice, action.invoice_id)
        if inv is not None:
            inv.status = InvoiceStatus.enforced
    await session.commit()
    log.info("Queued enforcement done for reseller %s: %d users", reseller.name, len(done_users))
    return {"done": 1, "patched_users": 0, "patched_admins": patched_admins}


async def process_enforcement_queue(
    session: AsyncSession,
    *,
    action_limit: int | None = None,
    user_chunk_size: int | None = None,
    admin_chunk_size: int | None = None,
) -> dict:
    cfg = await settings_service.get_many(
        session,
        [
            "enforcement_action_batch_limit",
            "enforcement_user_chunk_size",
            "enforcement_admin_chunk_size",
        ],
    )
    limit = max(1, int(action_limit or cfg.get("enforcement_action_batch_limit") or 1))
    chunk = max(1, int(user_chunk_size or cfg.get("enforcement_user_chunk_size") or 100))
    admin_chunk = max(
        1, int(admin_chunk_size or cfg.get("enforcement_admin_chunk_size") or 10)
    )
    actions = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.action.in_(
                    [EnforcementActionType.disable_users, EnforcementActionType.restore]
                ),
                EnforcementAction.dry_run.is_(False),
                EnforcementAction.status.in_([
                    EnforcementActionStatus.planned,
                    EnforcementActionStatus.partial,
                ]),
            )
            .order_by(
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
    result = {
        "picked": len(actions),
        "done": 0,
        "partial": 0,
        "failed": 0,
        "patched_users": 0,
        "failed_users": 0,
        "patched_admins": 0,
        "restored_users": 0,
        "restored_admins": 0,
        "restore_queued": 0,
        "skipped": 0,
    }
    for action in actions:
        if action.action == EnforcementActionType.restore:
            step = await _process_restore_action(
                session,
                action,
                user_chunk_size=chunk,
                admin_chunk_size=admin_chunk,
            )
        else:
            step = await _process_enforcement_action(
                session, action, user_chunk_size=chunk
            )
        for key in result:
            if key != "picked":
                result[key] += int(step.get(key, 0) or 0)
    return result


async def enforce_reseller(
    session: AsyncSession,
    reseller: Reseller,
    *,
    dry_run: bool | None = None,
    invoice_id: int | None = None,
) -> EnforcementAction:
    """Queue a suspension so API and bot requests never wait for panel writes."""
    return await queue_enforcement(
        session,
        reseller,
        invoice_id=invoice_id,
        dry_run=dry_run,
    )


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
            snapshot = existing.snapshot or {}
            progress = _progress(snapshot)
            progress["users_failed"] = {}
            progress["user_attempts"] = {}
            progress["admins_failed"] = {}
            progress["admin_attempts"] = {}
            snapshot["require_no_due"] = require_no_due
            snapshot["reason"] = reason
            existing.snapshot = snapshot
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
    limits = dict(source_progress.get("captured_limits") or source_snapshot.get("limits") or {})
    if source.status in (
        EnforcementActionStatus.partial,
        EnforcementActionStatus.failed,
    ):
        completed_users = set(source_progress.get("users_done") or [])
        missing_users = set(source_progress.get("users_missing") or [])
        completed_admins = set(source_progress.get("admins_done") or [])
        users_map = {
            uuid: owner
            for uuid, owner in users_map.items()
            if uuid in completed_users and uuid not in missing_users
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

    snapshot = {
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


async def _process_restore_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
    admin_chunk_size: int,
) -> dict:
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
    progress.setdefault("user_attempts", {})
    progress.setdefault("admin_attempts", {})
    progress.setdefault("admins_missing", [])
    action.status = EnforcementActionStatus.partial
    client = AdminApiClient()
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    limits: dict[str, dict] = dict(snapshot.get("limits") or {})
    admins = list(snapshot.get("admins") or limits)
    done_admins = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    restored_admins = 0

    if progress.get("phase") == "limits":
        for admin_uuid in admins:
            if admin_uuid in done_admins:
                continue
            admin = by_uuid.get(admin_uuid)
            if admin is None:
                done_admins.add(admin_uuid)
                progress["admins_missing"] = sorted(
                    set(progress.get("admins_missing") or []) | {admin_uuid}
                )
                continue
            lim = limits.get(admin_uuid) or {}
            max_users = lim.get("max_users")
            max_active_users = lim.get("max_active_users")
            if max_users is None:
                max_users = admin.max_users_snapshot
            if max_active_users is None:
                max_active_users = admin.max_active_users_snapshot
            if max_users is None or max_active_users is None:
                admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
                failed_admins[admin_uuid] = "saved admin limits are incomplete"
                if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                    action.status = EnforcementActionStatus.failed
                    action.error = "saved admin limits are incomplete"
                break
            try:
                await _set_admin_limits(
                    client,
                    panel,
                    admin,
                    int(max_users),
                    int(max_active_users),
                )
                done_admins.add(admin_uuid)
                failed_admins.pop(admin_uuid, None)
                restored_admins += 1
            except Exception as exc:  # noqa: BLE001
                admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
                failed_admins[admin_uuid] = str(exc)[:300]
                if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                    action.status = EnforcementActionStatus.failed
                    action.error = f"admin limit restore failed after {_MAX_RETRIES} attempts"
                break
            if restored_admins >= max(1, admin_chunk_size):
                break
        progress["admins_done"] = sorted(done_admins)
        progress["admins_failed"] = failed_admins
        progress["admin_attempts"] = admin_attempts
        if action.status == EnforcementActionStatus.failed:
            action.snapshot = snapshot
            flag_modified(action, "snapshot")
            await session.commit()
            return {"failed": 1, "restored_admins": restored_admins}
        if len(done_admins) < len(admins):
            action.snapshot = snapshot
            flag_modified(action, "snapshot")
            await session.commit()
            return {"partial": 1, "restored_admins": restored_admins}
        progress["phase"] = "users"

    users_map: dict[str, str] = dict(snapshot.get("users") or {})
    done_users = set(progress.get("users_done") or [])
    missing_users = set(progress.get("users_missing") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})
    remaining = [uuid for uuid in users_map if uuid not in done_users]
    restored_users = 0
    if remaining:
        panel_user_ids: dict[str, int] = {
            str(uuid): int(user_id)
            for uuid, user_id in (snapshot.get("panel_user_ids") or {}).items()
        }
        if any(uuid not in panel_user_ids for uuid in remaining):
            try:
                current_ids = await client.get_user_ids(panel)
                panel_user_ids.update(
                    {uuid: current_ids[uuid] for uuid in remaining if uuid in current_ids}
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
                return {
                    "failed": 1,
                    "partial": int(action.status == EnforcementActionStatus.partial),
                }
        for uuid in remaining:
            if uuid not in panel_user_ids:
                missing_users.add(uuid)
                done_users.add(uuid)
        chunk = [
            uuid for uuid in remaining
            if uuid in panel_user_ids and uuid not in done_users
        ][:max(1, user_chunk_size)]
        if chunk:
            try:
                await client.bulk_set_users_enabled(
                    panel, [panel_user_ids[uuid] for uuid in chunk], True
                )
                rows = {
                    row.user_uuid: row
                    for row in (
                        await session.execute(
                            select(EndUserSnapshot).where(
                                EndUserSnapshot.panel_id == panel.id,
                                EndUserSnapshot.user_uuid.in_(chunk),
                            )
                        )
                    ).scalars().all()
                }
                for uuid in chunk:
                    done_users.add(uuid)
                    failed_users.pop(uuid, None)
                    if uuid in rows:
                        rows[uuid].enable = True
                restored_users = len(chunk)
                action.error = None
            except Exception as exc:  # noqa: BLE001
                for uuid in chunk:
                    user_attempts[uuid] = user_attempts.get(uuid, 0) + 1
                    failed_users[uuid] = str(exc)[:300]
                if any(user_attempts[uuid] >= _MAX_RETRIES for uuid in chunk):
                    action.status = EnforcementActionStatus.failed
                    action.error = f"bulk user restore failed after {_MAX_RETRIES} attempts"
        progress["users_done"] = sorted(done_users)
        progress["users_missing"] = sorted(missing_users)
        progress["users_failed"] = failed_users
        progress["user_attempts"] = user_attempts
        action.affected_count = len(done_users - missing_users)
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        if action.status == EnforcementActionStatus.failed:
            await session.commit()
            return {"failed": 1}
        if len(done_users) < len(users_map):
            await session.commit()
            return {"partial": 1, "restored_users": restored_users}

    reseller.enforcement_state = EnforcementState.active
    for descendant in descendants:
        descendant.max_users_snapshot = None
        descendant.max_active_users_snapshot = None
    source_id = snapshot.get("source_action_id")
    if source_id:
        source = await session.get(EnforcementAction, int(source_id))
        if source is not None:
            source.status = EnforcementActionStatus.reverted
    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()
    log.info(
        "Queued restore done for reseller %s: %d users",
        reseller.name,
        action.affected_count,
    )
    return {
        "done": 1,
        "restored_users": restored_users,
        "restored_admins": restored_admins,
    }


async def restore_reseller(
    session: AsyncSession, reseller: Reseller
) -> EnforcementAction | None:
    """Compatibility wrapper: restore is now always durable and asynchronous."""
    return await queue_restore(session, reseller)
