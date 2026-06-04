"""
Enforcement: suspend a delinquent reseller (disable their + sub-resellers' users,
zero their admin limits) and restore exactly on payment.

Safety: controlled by the `enforcement_enabled` setting. When False (default), runs
in DRY-RUN — it records what it *would* do (EnforcementAction with dry_run=True) and
makes no panel writes. Set it True to perform live writes (needs panel admin API keys).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _bundle(session: AsyncSession, reseller: Reseller) -> list[Reseller]:
    """The reseller + all descendant sub-resellers (same panel)."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)
    return collect_descendants(reseller, children)


async def _set_user_enabled_uuid(
    client: AdminApiClient, panel, user_uuid: str, creator: str | None, enabled: bool
) -> None:
    """Enable/disable a user, authenticating AS its creating admin (added_by_uuid) so the
    panel's has_permission passes even when the configured key isn't the super-admin.
    Falls back to the panel's configured key."""
    if creator:
        try:
            await client.set_user_enabled(panel, user_uuid, enabled, api_key=creator)
            return
        except Exception:  # noqa: BLE001 — fall back to the panel/owner key
            pass
    await client.set_user_enabled(panel, user_uuid, enabled)


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
    users_by_admin: dict[str, list] = defaultdict(list)
    for u in users:
        users_by_admin[u.added_by_uuid or ""].append(u)

    # Walk the tree BOTTOM-UP (deepest sub-resellers before their parent — `descendants`
    # is breadth-first from the root, so reversed() yields children before parents). For
    # each admin: (1) disable ITS users, then (2) zero ITS limits. Doing users-before-
    # limits per node means a node is never capped while we still need to edit its users,
    # and processing children first means a parent's cap can't interfere with a child.
    # Users are edited AS their creator and limits AS the admin's parent, so the panel's
    # permission check passes even when the configured key isn't the super-admin.
    disabled = 0
    root_ok = False
    captured_limits: dict[str, dict] = {}
    for d in reversed(descendants):
        for u in users_by_admin.get(d.admin_uuid, []):
            try:
                await _set_user_enabled_uuid(client, panel, u.user_uuid, u.added_by_uuid, False)
                u.enable = False
                disabled += 1
            except Exception as ue:  # noqa: BLE001
                errors.append(f"user {(u.name or u.user_uuid)[:16]}: {str(ue)[:90]}")
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
    snapshot = last.snapshot if last else {"limits": {}, "users": {}}
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

    # Now re-enable users (caps are lifted), each as its creating admin.
    re_enabled = 0
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
    for uuid, creator in users_map.items():
        try:
            await _set_user_enabled_uuid(client, panel, uuid, creator or None, True)
            if uuid in rows:
                rows[uuid].enable = True
            re_enabled += 1
        except Exception as ue:  # noqa: BLE001
            errors.append(f"user {uuid[-6:]}: {str(ue)[:90]}")

    reseller.enforcement_state = EnforcementState.active
    reseller.max_users_snapshot = None
    reseller.max_active_users_snapshot = None
    if last:
        last.status = EnforcementActionStatus.reverted
    restore.status = EnforcementActionStatus.done
    restore.affected_count = re_enabled
    if errors:
        restore.error = (f"{re_enabled}/{len(users_map)} re-enabled; "
                         f"{len(errors)} issue(s): " + " | ".join(errors[:8]))[:1000]
    await session.commit()
    log.info("Restored reseller %s: re-enabled %d/%d users (%d issues)",
             reseller.name, re_enabled, len(users_map), len(errors))
    return restore
