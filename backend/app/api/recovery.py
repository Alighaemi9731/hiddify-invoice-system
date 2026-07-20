"""Owner-only: recover end-users a panel backup-rollback removed (preview + restore).

`GET /api/recovery/candidates` previews the losses (grouped per panel, clustered by the drop moment);
`POST /api/recovery/restore` re-creates the chosen users on the panel under their original admin with
their original uuid. Preview-then-confirm; the restore re-derives every detail from the snapshot, so
the client only ever chooses WHICH users, never their data.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal, get_session
from app.core.security import get_current_subject
from app.services import user_recovery

router = APIRouter(
    prefix="/api/recovery", tags=["recovery"], dependencies=[Depends(get_current_subject)]
)


def _parse_ids(panel_ids: str) -> list[int]:
    try:
        ids = [int(x) for x in panel_ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(400, "panel_ids must be a comma-separated list of integers") from exc
    if not ids:
        raise HTTPException(400, "select at least one panel")
    if len(ids) > 50:
        raise HTTPException(400, "too many panels selected")
    return ids


@router.get("/candidates")
async def candidates(
    panel_ids: str = Query(..., description="comma-separated panel ids"),
    lookback_days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Preview the lost users for the selected panels, clustered by the drop moment."""
    return await user_recovery.detect(session, _parse_ids(panel_ids), lookback_days=lookback_days)


class RestoreUser(BaseModel):
    panel_id: int
    user_uuid: str


class RestoreBody(BaseModel):
    users: list[RestoreUser]
    dry_run: bool = False


@router.post("/restore")
async def restore(body: RestoreBody) -> dict:
    """Re-create the chosen users on their panel under their original admin (same uuid). `dry_run`
    runs every check (incl. the live already-present pre-check) without creating anything."""
    if not body.users:
        raise HTTPException(400, "no users selected")
    if len(body.users) > 2000:
        raise HTTPException(400, "too many users in one request")
    pairs = [(u.panel_id, u.user_uuid) for u in body.users]
    return await user_recovery.restore(SessionLocal, pairs, dry_run=body.dry_run)
