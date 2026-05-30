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
    try:
        for d in descendants:
            d.max_users_snapshot = d.panel_max_users
            d.max_active_users_snapshot = d.panel_max_active_users
            await client.set_admin_limits(panel, d.admin_uuid, 0, 0)
        for u in users:
            await client.set_user_enabled(panel, u.user_uuid, False)
            u.enable = False
        reseller.enforcement_state = EnforcementState.enforced
        action.status = EnforcementActionStatus.done
        await session.commit()
        log.info("Enforced reseller %s: disabled %d users, zeroed %d admins",
                 reseller.name, len(users), len(descendants))
    except Exception as exc:  # noqa: BLE001
        action.status = EnforcementActionStatus.failed
        action.error = str(exc)[:1000]
        await session.commit()
        log.exception("Enforcement failed for reseller %s", reseller.name)
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
    try:
        for admin_uuid, lim in (snapshot.get("limits") or {}).items():
            mu = lim.get("max_users")
            mau = lim.get("max_active_users")
            if mu is None and mau is None:
                continue
            await client.set_admin_limits(panel, admin_uuid, mu or 0, mau or 0)
        user_uuids = list((snapshot.get("users") or {}).keys())
        for uuid in user_uuids:
            await client.set_user_enabled(panel, uuid, True)
        # Reflect locally.
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
                r.enable = True
        reseller.enforcement_state = EnforcementState.active
        reseller.max_users_snapshot = None
        reseller.max_active_users_snapshot = None
        if last:
            last.status = EnforcementActionStatus.reverted
        restore.status = EnforcementActionStatus.done
        restore.affected_count = len(user_uuids)
        await session.commit()
        log.info("Restored reseller %s: re-enabled %d users", reseller.name, len(user_uuids))
    except Exception as exc:  # noqa: BLE001
        restore.status = EnforcementActionStatus.failed
        restore.error = str(exc)[:1000]
        await session.commit()
        log.exception("Restore failed for reseller %s", reseller.name)
    return restore
