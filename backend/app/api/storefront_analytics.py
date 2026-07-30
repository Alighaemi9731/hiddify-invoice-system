"""Owner-side analytics over EVERY reseller's storefront bot."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import proccache
from app.core.db import get_session
from app.core.security import get_current_subject
from app.schemas.storefront_analytics import StorefrontAnalyticsOut
from app.services import storefront_analytics
from app.services.periods import current_month, parse_period

router = APIRouter(
    prefix="/api/storefront-analytics",
    tags=["storefront-analytics"],
    dependencies=[Depends(get_current_subject)],
)

# The report fans out into ~15 aggregate queries plus one pass over the live services. Nothing on
# the page is second-sensitive, so a short TTL absorbs the SPA's refetch-on-focus/period-toggle
# bursts. Keyed by period AND by the current Tehran day so «today» rolls over on its own.
_cache = proccache.TTLCache(ttl_seconds=60.0)


@router.get("", response_model=StorefrontAnalyticsOut)
async def analytics(
    period: str | None = None,
    refresh: bool = Query(False, description="bypass the 60s report cache"),
    session: AsyncSession = Depends(get_session),
) -> StorefrontAnalyticsOut:
    """Fleet-wide storefront analytics for one billing month (defaults to the current one)."""
    try:
        p = parse_period(period) if period else current_month()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from app.services.periods import today as local_today

    key = (proccache.engine_ns(session), "storefront-analytics", p.label, local_today().isoformat())
    if not refresh:
        hit = _cache.get(key)
        if hit is not proccache.MISS:
            return hit
    out = await storefront_analytics.analytics(session, p)
    _cache.put(key, out)
    return out
