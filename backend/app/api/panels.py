"""Panels: CRUD + sync (owner-only)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.db import SessionLocal, get_session
from app.core.security import get_current_subject
from app.models import EndUserSnapshot, Panel, Reseller, SyncRun
from app.models.enums import PanelStatus

log = logging.getLogger("api.panels")
from app.schemas.panel import (
    PanelCreate,
    PanelOut,
    PanelUpdate,
    SyncRunOut,
    SyncTestResult,
)
from app.services import sync as sync_service
from app.services.panel_client import BackupJsonClient

router = APIRouter(
    prefix="/api/panels", tags=["panels"], dependencies=[Depends(get_current_subject)]
)


async def _to_out(session: AsyncSession, panel: Panel) -> PanelOut:
    resellers_count = (
        await session.execute(
            select(func.count(Reseller.id)).where(Reseller.panel_id == panel.id)
        )
    ).scalar_one()
    users_count = (
        await session.execute(
            select(func.count(EndUserSnapshot.id)).where(
                EndUserSnapshot.panel_id == panel.id
            )
        )
    ).scalar_one()
    return PanelOut(
        id=panel.id,
        key=panel.key,
        name=panel.name,
        host=panel.host,
        owner_uuid=panel.owner_uuid,
        enabled=panel.enabled,
        status=panel.status.value,
        source=panel.source.value,
        proxy_path_masked=crypto.mask(panel.proxy_path_enc),
        has_admin_api_key=bool(panel.admin_api_key_enc),
        last_synced_at=panel.last_synced_at,
        last_error=panel.last_error,
        backup_url=panel.backup_url,
        resellers_count=resellers_count,
        end_users_count=users_count,
    )


async def _get_or_404(session: AsyncSession, panel_id: int) -> Panel:
    panel = await session.get(Panel, panel_id)
    if panel is None:
        raise HTTPException(404, "Panel not found")
    return panel


@router.get("", response_model=list[PanelOut])
async def list_panels(session: AsyncSession = Depends(get_session)) -> list[PanelOut]:
    panels = (await session.execute(select(Panel).order_by(Panel.key))).scalars().all()
    return [await _to_out(session, p) for p in panels]


@router.post("", response_model=PanelOut, status_code=201)
async def create_panel(
    body: PanelCreate, session: AsyncSession = Depends(get_session)
) -> PanelOut:
    exists = (
        await session.execute(select(Panel).where(Panel.key == body.key))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"Panel key '{body.key}' already exists")
    panel = Panel(
        key=body.key,
        name=body.name or body.key,
        host=body.host.replace("https://", "").replace("http://", "").strip("/"),
        owner_uuid=body.owner_uuid,
        enabled=body.enabled,
    )
    panel.proxy_path = body.proxy_path  # encrypts
    if body.admin_api_key:
        panel.admin_api_key = body.admin_api_key
    session.add(panel)
    await session.commit()
    await session.refresh(panel)
    return await _to_out(session, panel)


@router.get("/{panel_id}", response_model=PanelOut)
async def get_panel(panel_id: int, session: AsyncSession = Depends(get_session)) -> PanelOut:
    return await _to_out(session, await _get_or_404(session, panel_id))


@router.patch("/{panel_id}", response_model=PanelOut)
async def update_panel(
    panel_id: int, body: PanelUpdate, session: AsyncSession = Depends(get_session)
) -> PanelOut:
    panel = await _get_or_404(session, panel_id)
    if body.name is not None:
        panel.name = body.name
    if body.host is not None:
        panel.host = body.host.replace("https://", "").replace("http://", "").strip("/")
    if body.owner_uuid is not None:
        panel.owner_uuid = body.owner_uuid
    if body.proxy_path is not None:
        panel.proxy_path = body.proxy_path
    if body.admin_api_key is not None:
        panel.admin_api_key = body.admin_api_key or None
    if body.enabled is not None:
        panel.enabled = body.enabled
    await session.commit()
    await session.refresh(panel)
    return await _to_out(session, panel)


@router.delete("/{panel_id}", status_code=204)
async def delete_panel(panel_id: int, session: AsyncSession = Depends(get_session)) -> None:
    panel = await _get_or_404(session, panel_id)
    await session.delete(panel)
    await session.commit()


async def _sync_one_bg(panel_id: int) -> None:
    """Background task: sync a single panel in its own DB session."""
    try:
        async with SessionLocal() as session:
            panel = await session.get(Panel, panel_id)
            if panel is not None:
                await sync_service.sync_panel(session, panel)
    except Exception:  # noqa: BLE001
        log.exception("background sync failed for panel %s", panel_id)


async def _sync_all_bg() -> None:
    try:
        async with SessionLocal() as session:
            await sync_service.sync_all(session)
    except Exception:  # noqa: BLE001
        log.exception("background sync-all failed")


@router.post("/{panel_id}/sync")
async def sync_panel(
    panel_id: int, background: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> dict:
    """Kick off a sync in the background and return immediately. The panel's
    `status` / `last_synced_at` update when it finishes (poll the panels list)."""
    panel = await _get_or_404(session, panel_id)
    panel.status = PanelStatus.unknown  # mark "syncing…" until it finishes
    await session.commit()
    background.add_task(_sync_one_bg, panel_id)
    return {"status": "started", "panel_id": panel_id}


@router.post("/sync-all")
async def sync_all(background: BackgroundTasks, session: AsyncSession = Depends(get_session)) -> dict:
    panels = (await session.execute(select(Panel).where(Panel.enabled.is_(True)))).scalars().all()
    background.add_task(_sync_all_bg)
    return {"status": "started", "panels": len(panels)}


@router.get("/{panel_id}/sync-runs", response_model=list[SyncRunOut])
async def sync_runs(
    panel_id: int, limit: int = 20, session: AsyncSession = Depends(get_session)
) -> list[SyncRunOut]:
    await _get_or_404(session, panel_id)
    runs = (
        await session.execute(
            select(SyncRun)
            .where(SyncRun.panel_id == panel_id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [SyncRunOut.model_validate(r, from_attributes=True) for r in runs]


@router.post("/{panel_id}/test", response_model=SyncTestResult)
async def test_connection(
    panel_id: int, session: AsyncSession = Depends(get_session)
) -> SyncTestResult:
    """Fetch the backup once without persisting — verifies credentials/connectivity."""
    panel = await _get_or_404(session, panel_id)
    try:
        data = await BackupJsonClient().fetch_backup(panel)
        return SyncTestResult(
            ok=True, admin_count=len(data.admins), user_count=len(data.users)
        )
    except Exception as exc:  # noqa: BLE001
        return SyncTestResult(ok=False, error=str(exc)[:500])
