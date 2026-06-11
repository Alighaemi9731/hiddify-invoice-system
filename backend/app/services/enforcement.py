"""
Enforcement: suspend a delinquent reseller (disable their + sub-resellers' users,
zero their admin limits) and restore exactly on payment.

Safety: controlled by the `enforcement_enabled` setting. When False (default), runs
in DRY-RUN — it records what it *would* do (EnforcementAction with dry_run=True) and
makes no panel writes. Set it True to perform live writes (needs panel admin API keys).
"""
from __future__ import annotations

import logging

from sqlalchemy import select
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

log = logging.getLogger("enforcement")

_LIVE_QUEUE_STATUSES = (
    EnforcementActionStatus.planned,
    EnforcementActionStatus.running,
    EnforcementActionStatus.partial,
    EnforcementActionStatus.done,
)


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


async def _bulk_set_user_uuids(
    client: AdminApiClient,
    panel,
    user_uuids: list[str],
    enabled: bool,
    *,
    batch_size: int,
    missing_ok: bool,
) -> tuple[set[str], list[str]]:
    """Apply Hiddify's native bulk user action in bounded batches."""
    if not user_uuids:
        return set(), []
    user_ids = await client.get_user_ids(panel)
    completed: set[str] = set()
    errors: list[str] = []
    available = [uuid for uuid in user_uuids if uuid in user_ids]
    missing = [uuid for uuid in user_uuids if uuid not in user_ids]
    if missing_ok:
        completed.update(missing)
    else:
        errors.extend(f"user {uuid[-6:]}: not found on panel" for uuid in missing)
    size = max(1, batch_size)
    for offset in range(0, len(available), size):
        chunk = available[offset:offset + size]
        try:
            await client.bulk_set_users_enabled(
                panel, [user_ids[uuid] for uuid in chunk], enabled
            )
            completed.update(chunk)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"bulk users {offset + 1}-{offset + len(chunk)}: {str(exc)[:300]}"
            )
            break
    return completed, errors


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
    progress.setdefault("captured_limits", {})
    progress.setdefault("phase", "users")
    return progress


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
    client = AdminApiClient()
    action.status = EnforcementActionStatus.partial
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()

    remaining_users = [uuid for uuid in users_map if uuid not in done_users and uuid not in failed_users]
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
                action.status = EnforcementActionStatus.partial
                action.error = f"bulk user id lookup failed: {str(exc)[:900]}"
                action.snapshot = snapshot
                flag_modified(action, "snapshot")
                await session.commit()
                return {"failed": 1, "partial": 1}

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
                    patched += 1
                action.error = None
            except Exception as exc:  # noqa: BLE001
                # Keep every UUID pending. The next scheduler pass retries this same batch;
                # never fall back to per-user PATCHes and accidentally overload the panel.
                action.status = EnforcementActionStatus.partial
                action.error = f"bulk disable failed: {str(exc)[:900]}"
                action.snapshot = snapshot
                flag_modified(action, "snapshot")
                await session.commit()
                return {"failed": 1, "partial": 1}
        for uuid in missing_users:
            if uuid in rows:
                rows[uuid].enable = False
        progress["users_done"] = sorted(done_users)
        progress["users_failed"] = failed_users
        progress["phase"] = "users" if len(done_users) + len(failed_users) < len(users_map) else "limits"
        action.status = EnforcementActionStatus.partial
        action.affected_count = len(done_users)
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
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or {})
    for admin_uuid in admin_order:
        if admin_uuid in done_admins or admin_uuid in failed_admins:
            continue
        admin = by_uuid.get(admin_uuid)
        if admin is None:
            failed_admins[admin_uuid] = "admin row not found"
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
        admin.max_users_snapshot = real_mu
        admin.max_active_users_snapshot = real_mau
        captured_limits[admin_uuid] = {"max_users": real_mu, "max_active_users": real_mau}
        try:
            await _set_admin_limits(client, panel, admin, 0, 0)
            done_admins.add(admin_uuid)
        except Exception as exc:  # noqa: BLE001
            failed_admins[admin_uuid] = str(exc)[:300]
        break

    progress["admins_done"] = sorted(done_admins)
    progress["admins_failed"] = failed_admins
    progress["captured_limits"] = captured_limits
    action.snapshot = {**snapshot, "limits": captured_limits or snapshot.get("limits", {})}
    flag_modified(action, "snapshot")
    if len(done_admins) + len(failed_admins) < len(admin_order):
        action.status = EnforcementActionStatus.partial
        action.affected_count = len(done_users)
        await session.commit()
        return {"patched_admins": 1 if done_admins else 0, "partial": 1}

    if len(done_users) == 0 and reseller.admin_uuid not in done_admins:
        action.status = EnforcementActionStatus.failed
        action.error = "enforcement did nothing"
        await session.commit()
        return {"failed": 1}

    action.affected_count = len(done_users)
    if failed_users or failed_admins:
        action.status = EnforcementActionStatus.failed
        action.error = (
            f"{len(failed_users)} user failure(s), {len(failed_admins)} admin failure(s)"
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
    return {"done": 1, "patched_users": 0}


async def process_enforcement_queue(
    session: AsyncSession,
    *,
    action_limit: int | None = None,
    user_chunk_size: int | None = None,
) -> dict:
    cfg = await settings_service.get_many(
        session, ["enforcement_action_batch_limit", "enforcement_user_chunk_size"]
    )
    limit = max(1, int(action_limit or cfg.get("enforcement_action_batch_limit") or 1))
    chunk = max(1, int(user_chunk_size or cfg.get("enforcement_user_chunk_size") or 100))
    actions = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.action == EnforcementActionType.disable_users,
                EnforcementAction.dry_run.is_(False),
                EnforcementAction.status.in_([
                    EnforcementActionStatus.planned,
                    EnforcementActionStatus.partial,
                ]),
            )
            .order_by(EnforcementAction.created_at, EnforcementAction.id)
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
        "skipped": 0,
    }
    for action in actions:
        step = await _process_enforcement_action(session, action, user_chunk_size=chunk)
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
    """Disable the reseller's + sub-resellers' users and zero their admin limits."""
    enabled = await settings_service.get(session, "enforcement_enabled", False)
    is_dry = (not enabled) if dry_run is None else dry_run

    # Idempotency guard (live path only): NEVER re-enforce a reseller already enforced.
    # On a re-enforce its on-panel limits are already 0, so reading them back would overwrite
    # the saved restore snapshot with 0/0 and permanently destroy the real max_users. Dunning
    # already guards on state==active; this protects the manual panel/bot suspend paths
    # (double-submit, a stale view, a rapid double-tap). Return the prior action unchanged.
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
        log.info("enforce_reseller: %s already enforced — skipping (idempotent)", reseller.name)
        if prior is not None:
            return prior
        # State says enforced but no 'done' action on record: record a no-op rather than
        # re-reading/zeroing limits (which is the corrupting path).
        noop = EnforcementAction(
            reseller_id=reseller.id, invoice_id=invoice_id,
            action=EnforcementActionType.disable_users, dry_run=False, affected_count=0,
            snapshot={"limits": {}, "users": {}}, status=EnforcementActionStatus.done,
        )
        session.add(noop)
        await session.commit()
        return noop

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
        # user_uuid → creating admin (added_by), so restore can re-enable each user
        # authenticated as the same admin.
        "users": {u.user_uuid: (u.added_by_uuid or "") for u in users},
    }

    action = EnforcementAction(
        reseller_id=reseller.id, invoice_id=invoice_id,
        action=EnforcementActionType.disable_users, dry_run=is_dry,
        affected_count=len(users), snapshot=snapshot,
        status=EnforcementActionStatus.dry_run if is_dry else EnforcementActionStatus.planned,
    )
    session.add(action)

    if is_dry:
        # Record intent only — no panel writes, no local state change.
        await session.commit()
        log.info("[dry-run] would enforce reseller %s: %d users, %d admins",
                 reseller.name, len(users), len(descendants))
        return action

    client = AdminApiClient()
    errors: list[str] = []
    batch_size = int(
        await settings_service.get(session, "enforcement_user_chunk_size", 100) or 100
    )
    try:
        disabled_uuids, user_errors = await _bulk_set_user_uuids(
            client,
            panel,
            [u.user_uuid for u in users],
            False,
            batch_size=batch_size,
            missing_ok=True,
        )
        errors.extend(user_errors)
    except Exception as exc:  # noqa: BLE001
        disabled_uuids = set()
        errors.append(f"bulk user lookup: {str(exc)[:300]}")
    for u in users:
        if u.user_uuid in disabled_uuids:
            u.enable = False

    # Walk the tree BOTTOM-UP (deepest sub-resellers before their parent — `descendants`
    # is breadth-first from the root, so reversed() yields children before parents). Users
    # were disabled above through Hiddify's native bulk action before any limit is zeroed.
    disabled = len(disabled_uuids)
    root_ok = False
    captured_limits: dict[str, dict] = {}
    for d in reversed(descendants):
        # Read the admin's REAL current limits from the API (the backup sync may not
        # carry max_users), so restore puts back the true values — not a stale 0/None.
        try:
            real_mu, real_mau = await client.get_admin_limits(
                panel, d.admin_uuid, api_key=d.parent_admin_uuid
            )
        except Exception:  # noqa: BLE001
            real_mu = real_mau = None
        if real_mu is None:
            real_mu = d.panel_max_users
        if real_mau is None:
            real_mau = d.panel_max_active_users
        # Defense-in-depth: never overwrite a previously-saved real limit with a 0 we just
        # read (would happen if the admin is somehow already capped) — keep the good value.
        if not real_mu and d.max_users_snapshot:
            real_mu = d.max_users_snapshot
        if not real_mau and d.max_active_users_snapshot:
            real_mau = d.max_active_users_snapshot
        d.max_users_snapshot = real_mu
        d.max_active_users_snapshot = real_mau
        captured_limits[d.admin_uuid] = {"max_users": real_mu, "max_active_users": real_mau}
        try:
            await _set_admin_limits(client, panel, d, 0, 0)
            if d.admin_uuid == reseller.admin_uuid:
                root_ok = True
        except Exception as le:  # noqa: BLE001
            errors.append(f"limit {(d.name or d.admin_uuid)[:16]}: {str(le)[:90]}")
    # Persist the accurate prior limits into the action snapshot for restore.
    action.snapshot = {"limits": captured_limits, "users": snapshot["users"]}

    # Enforcement counts as done if we made real progress: users disabled (existing
    # connections cut) OR the reseller's own limits zeroed. Only a total no-op is a failure.
    if disabled == 0 and not root_ok:
        action.status = EnforcementActionStatus.failed
        action.error = ("enforcement did nothing — " + " | ".join(errors))[:1000]
        await session.commit()
        log.warning("Enforcement failed for reseller %s: %s", reseller.name, action.error)
        return action

    reseller.enforcement_state = EnforcementState.enforced
    action.status = EnforcementActionStatus.done
    action.affected_count = disabled
    if errors:
        action.error = (f"{disabled}/{len(users)} users disabled; root_limits={root_ok}; "
                        f"{len(errors)} issue(s): " + " | ".join(errors[:8]))[:1000]
    await session.commit()
    log.info("Enforced reseller %s: disabled %d/%d users, root_limits=%s (%d issues)",
             reseller.name, disabled, len(users), root_ok, len(errors))
    return action


async def restore_reseller(session: AsyncSession, reseller: Reseller) -> EnforcementAction | None:
    """Undo the most recent live enforcement: re-enable users + restore limits."""
    if reseller.enforcement_state != EnforcementState.enforced:
        return None

    last = (
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

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        raise ValueError("panel not found for reseller")
    snapshot = last.snapshot if last and last.snapshot else {"limits": {}, "users": {}}
    client = AdminApiClient()
    descendants = await _bundle(session, reseller)
    limits_map = snapshot.get("limits") or {}
    users_map = snapshot.get("users") or {}  # {user_uuid: creating admin uuid}

    restore = EnforcementAction(
        reseller_id=reseller.id, action=EnforcementActionType.restore,
        dry_run=False, snapshot=snapshot, status=EnforcementActionStatus.planned,
    )
    session.add(restore)
    errors: list[str] = []

    # Reverse of enforce: TOP-DOWN (root first). For each admin restore ITS limits FIRST,
    # then re-enable ITS users — because the panel REJECTS enabling a user while its
    # admin's max is still 0 (an active user can't exceed the cap). Limits are set AS the
    # admin's parent, users AS their creator, so permission passes without the super key.
    root_failed = False
    for d in descendants:  # breadth-first = root before children
        lim = limits_map.get(d.admin_uuid)
        if lim and not (lim.get("max_users") is None and lim.get("max_active_users") is None):
            try:
                await _set_admin_limits(client, panel, d,
                                        lim.get("max_users") or 0, lim.get("max_active_users") or 0)
            except Exception as le:  # noqa: BLE001
                errors.append(f"limit {(d.name or d.admin_uuid)[:16]}: {str(le)[:90]}")
                if d.admin_uuid == reseller.admin_uuid:
                    root_failed = True
    if root_failed:
        # Couldn't lift the reseller's own cap → users can't be re-enabled; report failure.
        restore.status = EnforcementActionStatus.failed
        restore.error = ("could not restore the reseller's own limits — " + " | ".join(errors))[:1000]
        await session.commit()
        log.warning("Restore failed for reseller %s: %s", reseller.name, restore.error)
        return restore

    # Now re-enable users in bounded native Hiddify batches (caps are lifted).
    rows = {}
    if users_map:
        rows = {
            r.user_uuid: r for r in (
                await session.execute(
                    select(EndUserSnapshot).where(
                        EndUserSnapshot.panel_id == panel.id,
                        EndUserSnapshot.user_uuid.in_(list(users_map.keys())),
                    )
                )
            ).scalars().all()
        }
    batch_size = int(
        await settings_service.get(session, "enforcement_user_chunk_size", 100) or 100
    )
    try:
        re_enabled_uuids, user_errors = await _bulk_set_user_uuids(
            client,
            panel,
            list(users_map),
            True,
            batch_size=batch_size,
            missing_ok=False,
        )
        errors.extend(user_errors)
    except Exception as exc:  # noqa: BLE001
        re_enabled_uuids = set()
        errors.append(f"bulk user lookup: {str(exc)[:300]}")
    for uuid in re_enabled_uuids:
        if uuid in rows:
            rows[uuid].enable = True
    re_enabled = len(re_enabled_uuids)

    # Only declare the reseller restored when EVERY required user re-enable succeeded. A
    # partial restore must stay `enforced` so the next trigger (payment confirm, manual
    # restore) RETRIES the still-disabled users — otherwise flipping to `active` here would
    # strand them disabled forever (a future restore is a no-op once state != enforced).
    fully_restored = (re_enabled == len(users_map)) and not errors
    restore.affected_count = re_enabled
    if fully_restored:
        reseller.enforcement_state = EnforcementState.active
        reseller.max_users_snapshot = None
        reseller.max_active_users_snapshot = None
        if last:
            last.status = EnforcementActionStatus.reverted
        restore.status = EnforcementActionStatus.done
        log.info("Restored reseller %s: re-enabled %d/%d users", reseller.name,
                 re_enabled, len(users_map))
    else:
        # Leave state=enforced and KEEP the enforce snapshot (last stays `done`) so a retry
        # re-runs the full set (re-enabling an already-enabled user is harmless/idempotent).
        restore.status = EnforcementActionStatus.failed
        restore.error = (f"partial restore: {re_enabled}/{len(users_map)} users re-enabled; "
                         f"{len(errors)} issue(s): " + " | ".join(errors[:8]))[:1000]
        log.warning("Partial restore for reseller %s: %d/%d users; staying enforced for retry",
                    reseller.name, re_enabled, len(users_map))
    await session.commit()
    return restore
