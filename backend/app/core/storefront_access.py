"""Owner-only tenant boundary for storefront portal routes."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.models import Panel, Reseller, StorefrontBot


@dataclass(frozen=True)
class StorefrontAccess:
    """A storefront proven to belong to the authenticated reseller identity."""

    shop: StorefrontBot
    reseller: Reseller
    panel: Panel
    role: str = "owner"


def _owned_query(reseller_ids: list[int]):
    return (
        select(StorefrontBot, Reseller, Panel)
        .join(Reseller, Reseller.id == StorefrontBot.reseller_id)
        .join(Panel, Panel.id == StorefrontBot.panel_id)
        .where(
            StorefrontBot.reseller_id.in_(reseller_ids),
            Reseller.storefront_enabled.is_(True),
        )
    )


async def list_owned_storefronts(
    session: AsyncSession, ctx: ResellerContext
) -> list[StorefrontAccess]:
    """Return configured, entitled storefronts owned by this portal identity."""
    rows = (
        await session.execute(_owned_query(ctx.ids).order_by(Reseller.name, StorefrontBot.id))
    ).all()
    return [StorefrontAccess(shop=shop, reseller=reseller, panel=panel) for shop, reseller, panel in rows]


async def require_owned_storefront(
    session: AsyncSession, ctx: ResellerContext, shop_id: int
) -> StorefrontAccess:
    """Resolve one owned shop; absent, disabled-entitlement and foreign ids are all 404."""
    row = (
        await session.execute(_owned_query(ctx.ids).where(StorefrontBot.id == shop_id).limit(1))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Storefront not found")
    shop, reseller, panel = row
    return StorefrontAccess(shop=shop, reseller=reseller, panel=panel)


async def get_storefront_access(
    shop_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontAccess:
    """FastAPI dependency for all shop-scoped portal endpoints."""
    return await require_owned_storefront(session, ctx, shop_id)
