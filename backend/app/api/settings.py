"""Runtime settings: read (masked) + update. Owner-only."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.schemas.setting import SettingOut, SettingsBulkUpdate, SettingUpdate
from app.services import settings_service

router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_subject)]
)

# Keys whose change must re-program the running scheduler's cron triggers immediately.
_SCHEDULE_KEYS = {
    "invoice_day_of_month", "invoice_hour", "dunning_hour",
    "sync_interval_hours", "guard_interval_minutes", "backup_interval_hours",
}


async def _reschedule_if_needed(session: AsyncSession, changed_keys: set[str]) -> None:
    """Live-apply scheduler timing changes so the owner doesn't need a restart."""
    if not (changed_keys & _SCHEDULE_KEYS):
        return
    try:
        from app.scheduler import scheduler

        await scheduler.apply_settings(session)
    except Exception:  # noqa: BLE001 - a reschedule hiccup must never fail the save
        import logging

        logging.getLogger("api.settings").warning("live reschedule failed", exc_info=True)


@router.get("", response_model=list[SettingOut])
async def list_settings(session: AsyncSession = Depends(get_session)) -> list[SettingOut]:
    return [SettingOut(**row) for row in await settings_service.all_for_api(session)]


@router.put("", response_model=dict)
async def update_one(body: SettingUpdate, session: AsyncSession = Depends(get_session)) -> dict:
    # Skip masked secret values that weren't changed (frontend sends the mask back).
    if isinstance(body.value, str) and set(body.value) <= {"•"} and body.value:
        return {"status": "unchanged", "key": body.key}
    await settings_service.set_value(session, body.key, body.value)
    await _reschedule_if_needed(session, {body.key})
    return {"status": "ok", "key": body.key}


@router.patch("", response_model=dict)
async def update_bulk(
    body: SettingsBulkUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    updated = 0
    changed: set[str] = set()
    for item in body.items:
        if isinstance(item.value, str) and item.value and set(item.value) <= {"•"}:
            continue  # untouched masked secret
        await settings_service.set_value(session, item.key, item.value)
        changed.add(item.key)
        updated += 1
    await _reschedule_if_needed(session, changed)
    return {"status": "ok", "updated": updated}
