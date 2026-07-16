"""Read-only storefront management endpoints for the reseller web portal."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.core.storefront_access import (
    StorefrontAccess,
    get_storefront_access,
    list_owned_storefronts,
)
from app.schemas.portal_storefront import (
    StorefrontDashboardOut,
    StorefrontHealthOut,
    StorefrontSummaryOut,
)
from app.services import storefront_reporting

router = APIRouter(prefix="/api/portal/storefronts", tags=["portal-storefront"])


@router.get("", response_model=list[StorefrontSummaryOut])
async def storefronts(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[StorefrontSummaryOut]:
    """List configured shops owned by the authenticated Telegram identity."""
    owned = await list_owned_storefronts(session, ctx)
    return [storefront_reporting.storefront_summary(access) for access in owned]


def _dashboard_dates(
    from_date: dt.date | None, to_date: dt.date | None
) -> tuple[dt.date, dt.date]:
    if (from_date is None) != (to_date is None):
        raise HTTPException(status_code=422, detail="from and to must be supplied together")
    if from_date is None or to_date is None:
        today = dt.datetime.now(storefront_reporting.TEHRAN).date()
        return today.replace(day=1), today
    if to_date == dt.date.max:
        raise HTTPException(status_code=422, detail="to date is outside the supported range")
    span = (to_date - from_date).days + 1
    if span <= 0:
        raise HTTPException(status_code=422, detail="from must be on or before to")
    if span > 366:
        raise HTTPException(status_code=422, detail="date range cannot exceed 366 days")
    return from_date, to_date


@router.get("/{shop_id}/dashboard", response_model=StorefrontDashboardOut)
async def storefront_dashboard(
    from_date: dt.date | None = Query(default=None, alias="from"),
    to_date: dt.date | None = Query(default=None, alias="to"),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StorefrontDashboardOut:
    day_from, day_to = _dashboard_dates(from_date, to_date)
    return await storefront_reporting.dashboard(session, access, day_from, day_to)


@router.get("/{shop_id}/health", response_model=StorefrontHealthOut)
async def storefront_health(
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StorefrontHealthOut:
    return await storefront_reporting.health(session, access)
