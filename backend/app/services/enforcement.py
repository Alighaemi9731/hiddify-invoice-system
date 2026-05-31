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


async def _set_user_enabled(client: AdminApiClient, panel, snapshot, enabled: bool) -> None:
    """Enable/disable a user, authenticating AS its creating admin (added_by_uuid) so the
    panel's has_permission passes even when the configured key isn't the super-admin.
    Falls back to the panel's configured key."""
    creator = getattr(snapshot, "added_by_uuid", None)
    if creator:
        try:
            await client.set_user_enabled(panel, snapshot.user_uuid, enabled, api_key=creator)
            return
        except Exception:  # noqa: BLE001 — fall back to the panel/owner key
            pass
    await client.set_user_enabled(panel, snapshot.user_uuid, enabled)


async def _disable_user(client: AdminApiClient, panel, snapshot) -> None:
    await _set_user_enabled(client, panel, snapshot, False)


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
        "users": {u.user_uuid: True for u in users},  # prior enable state (all True here)
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

    # 1) Disable end-users FIRST — while every admin still has its full limits, so a
    # just-capped admin can never be the reason a user-disable is rejected. Each user is
    # disabled authenticated AS its creating admin (added_by). BEST-EFFORT: a slow/failed
    # per-user PATCH (the panel reapplies the whole proxy each time) must not abort.
    # Disabling the users is what actually cuts off existing connections.
    disabled = 0
    for u in users:
        try:
            await _disable_user(client, panel, u)
            u.enable = False
            disabled += 1
        except Exception as ue:  # noqa: BLE001
            errors.append(f"{(u.name or u.user_uuid)[:18]}: {str(ue)[:100]}")

    # 2) THEN zero every admin's limits (prevents creating/re-activating users). Snapshot
    # prior values for exact restore. BEST-EFFORT: a stale sub-admin must not abort.
    root_ok = False
    for d in descendants:
        d.max_users_snapshot = d.panel_max_users
        d.max_active_users_snapshot = d.panel_max_active_users
        try:
            await client.set_admin_limits(panel, d.admin_uuid, 0, 0)
            if d.admin_uuid == reseller.admin_uuid:
                root_ok = True
        except Exception as le:  # noqa: BLE001
            errors.append(f"limit {(d.name or d.admin_uuid)[:18]}: {str(le)[:100]}")

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

    restore = EnforcementAction(
        reseller_id=reseller.id, action=EnforcementActionType.restore,
        dry_run=False, snapshot=snapshot, status=EnforcementActionStatus.planned,
    )
    session.add(restore)
    errors: list[str] = []
    try:
        for admin_uuid, lim in (snapshot.get("limits") or {}).items():
            mu = lim.get("max_users")
            mau = lim.get("max_active_users")
            if mu is None and mau is None:
                continue
            await client.set_admin_limits(panel, admin_uuid, mu or 0, mau or 0)
    except Exception as exc:  # noqa: BLE001 — restoring limits is the critical step
        restore.status = EnforcementActionStatus.failed
        restore.error = f"restore admin limits failed: {str(exc)[:900]}"
        await session.commit()
        log.exception("Restore (admin limits) failed for reseller %s", reseller.name)
        return restore

    # Re-enable users BEST-EFFORT, authenticated as each user's creator (added_by) so the
    # panel permits it; one failure must not leave a paid reseller stuck enforced. We work
    # off the live snapshot rows (which carry added_by_uuid).
    user_uuids = list((snapshot.get("users") or {}).keys())
    re_enabled = 0
    rows = []
    if user_uuids:
        rows = (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.panel_id == panel.id,
                    EndUserSnapshot.user_uuid.in_(user_uuids),
                )
            )
        ).scalars().all()
    for r in rows:
        try:
            await _set_user_enabled(client, panel, r, True)
            r.enable = True
            re_enabled += 1
        except Exception as ue:  # noqa: BLE001
            errors.append(f"{r.user_uuid[-6:]}: {str(ue)[:120]}")
    reseller.enforcement_state = EnforcementState.active
    reseller.max_users_snapshot = None
    reseller.max_active_users_snapshot = None
    if last:
        last.status = EnforcementActionStatus.reverted
    restore.status = EnforcementActionStatus.done
    restore.affected_count = re_enabled
    if errors:
        restore.error = (f"{re_enabled}/{len(user_uuids)} re-enabled; "
                         f"{len(errors)} failed: " + " | ".join(errors[:8]))[:1000]
    await session.commit()
    log.info("Restored reseller %s: re-enabled %d/%d users (%d errors)",
             reseller.name, re_enabled, len(user_uuids), len(errors))
    return restore
