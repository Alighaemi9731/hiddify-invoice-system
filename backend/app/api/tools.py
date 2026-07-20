"""Owner "Tools" — rare/special operations kept off the always-visible pages.

Currently: search end-users (by name or uuid) and remove a mistaken one from billing. Removing
deletes the user's EndUserSnapshot AND its UsageMeter rows, so it drops out of BOTH the sold-quota
base and the metering (overage/consumption) extras — the same rows `delete_absent_reseller` clears.
DB-only: nothing on the Hiddify panel, sent invoices, or the financial ledger changes (regenerate the
period's draft / recompute a sent invoice to reflect the removal). Owner-authenticated.
"""
from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import CursorResult, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import EndUserSnapshot, Panel, Reseller, UsageMeter, UsageMeterEvent
from app.services import enforcement
from app.services.reseller_report import node_descendants

router = APIRouter(
    prefix="/api/tools", tags=["tools"], dependencies=[Depends(get_current_subject)]
)


@router.get("/end-users")
async def search_end_users(
    q: str = Query(..., min_length=1, description="search by user name or uuid"),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Find end-users by name or uuid (case-insensitive), with the context the owner needs to
    identify the right one before removing it."""
    like = f"%{q.strip()}%"
    rows = (await session.execute(
        select(EndUserSnapshot)
        .where(or_(EndUserSnapshot.name.ilike(like), EndUserSnapshot.user_uuid.ilike(like)))
        .order_by(EndUserSnapshot.start_date.desc().nullslast(), EndUserSnapshot.id.desc())
        .limit(50)
    )).scalars().all()
    if not rows:
        return []

    panel_ids = {r.panel_id for r in rows}
    panels = {
        p.id: p for p in (await session.execute(
            select(Panel).where(Panel.id.in_(panel_ids))
        )).scalars().all()
    }
    # Map (panel_id, lower admin_uuid) → reseller name for the "created by" column.
    adders = {(r.panel_id, (r.added_by_uuid or "").lower()) for r in rows if r.added_by_uuid}
    reseller_names: dict[tuple[int, str], str] = {}
    if adders:
        for rs in (await session.execute(
            select(Reseller).where(Reseller.panel_id.in_(panel_ids))
        )).scalars().all():
            reseller_names[(rs.panel_id, (rs.admin_uuid or "").lower())] = rs.name

    out: list[dict] = []
    for r in rows:
        panel = panels.get(r.panel_id)
        present = bool(
            r.last_synced_at and panel and panel.last_synced_at
            and r.last_synced_at >= panel.last_synced_at
        )
        out.append({
            "id": r.id,
            "panel_id": r.panel_id,
            "panel_key": panel.key if panel else "?",
            "user_uuid": r.user_uuid,
            "name": r.name,
            "added_by_uuid": r.added_by_uuid,
            "reseller_name": reseller_names.get(
                (r.panel_id, (r.added_by_uuid or "").lower()), ""),
            "usage_limit_gb": float(r.usage_limit_gb or 0),
            "current_usage_gb": float(r.current_usage_gb or 0),
            "start_date": r.start_date.isoformat() if r.start_date else None,
            "enable": r.enable,
            "present_on_panel": present,
        })
    return out


@router.post("/end-users/{snapshot_id}/remove")
async def remove_end_user(
    snapshot_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Purge ONE end-user from billing: delete its snapshot + all its usage_meters (so it drops out
    of the sold-quota base AND the metering extras). DB-only; the Hiddify panel, sent invoices, and
    the financial ledger are untouched. Regenerate the period's draft (or «بازمحاسبه» a sent invoice)
    to reflect it. If the user is still on the panel, the next sync re-adds it."""
    s = await session.get(EndUserSnapshot, snapshot_id)
    if s is None:
        raise HTTPException(404, "End-user not found")
    panel_id, user_uuid, name = s.panel_id, s.user_uuid, s.name
    res = await session.execute(
        sa_delete(UsageMeter).where(
            UsageMeter.panel_id == panel_id, UsageMeter.user_uuid == user_uuid
        )
    )
    meters = cast("CursorResult[Any]", res).rowcount or 0
    await session.execute(
        sa_delete(UsageMeterEvent).where(
            UsageMeterEvent.panel_id == panel_id, UsageMeterEvent.user_uuid == user_uuid
        )
    )
    await session.execute(
        sa_delete(EndUserSnapshot).where(EndUserSnapshot.id == snapshot_id)
    )
    await session.commit()
    return {"user_uuid": user_uuid, "name": name, "meters_deleted": int(meters)}


async def _delete_scope(session: AsyncSession, reseller: Reseller) -> dict:
    """The cascade footprint of deleting `reseller`: its whole sub-tree of admins + the count of
    end-users created across that sub-tree (what would be removed from the panel + our DB)."""
    descendants = await node_descendants(session, reseller)
    uuids = [(d.admin_uuid or "").lower() for d in descendants if d.admin_uuid]
    user_count = 0
    if uuids:
        user_count = int((await session.execute(
            select(func.count(EndUserSnapshot.id)).where(
                EndUserSnapshot.panel_id == reseller.panel_id,
                func.lower(EndUserSnapshot.added_by_uuid).in_(uuids),
            )
        )).scalar_one() or 0)
    panel = await session.get(Panel, reseller.panel_id)
    return {
        "reseller_id": reseller.id,
        "name": reseller.name,
        "admin_uuid": reseller.admin_uuid,
        "panel_key": panel.key if panel else "?",
        "is_owner": bool(getattr(reseller, "is_owner", False)),
        "sub_reseller_count": max(0, len(descendants) - 1),
        "user_count": user_count,
    }


@router.get("/admin/{reseller_id}/delete-preview")
async def delete_admin_preview(
    reseller_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """How much deleting this admin would remove (sub-resellers + users) — for the confirm dialog."""
    reseller = await session.get(Reseller, reseller_id)
    if reseller is None:
        raise HTTPException(404, "Reseller not found")
    return await _delete_scope(session, reseller)


@router.post("/admin/{reseller_id}/delete")
async def delete_admin_cascade(
    reseller_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Queue a full cascade deletion of this admin + its sub-tree (users + sub-admins) from the
    Hiddify panel AND our DB. Runs in the background (queued, chunked, panel-paced, resumable) like
    suspend/restore. The financial ledger is kept. Refuses the panel owner."""
    reseller = await session.get(Reseller, reseller_id)
    if reseller is None:
        raise HTTPException(404, "Reseller not found")
    scope = await _delete_scope(session, reseller)
    try:
        action = await enforcement.queue_admin_deletion(session, reseller)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"queued": True, "action_id": action.id, **scope}
