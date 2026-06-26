"""Provision a VPN config for a storefront purchase.

Reuses `usercreate.create_for_reseller`, which creates the user on the reseller's panel AS the
reseller (`api_key = reseller.admin_uuid`) — so the config counts toward the reseller's own usage that
the owner bills. Returns the subscription link + uuid, or a typed failure the caller turns into a
wallet refund + admin nudge.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller, StorefrontBot, StorefrontCustomer
from app.services import usercreate


@dataclass
class ProvisionResult:
    ok: bool
    sub_link: str | None = None
    uuid: str | None = None
    reason: str | None = None      # "capacity" | "limit" | "error"
    message: str | None = None


async def provision(
    session: AsyncSession,
    bot: StorefrontBot,
    customer: StorefrontCustomer,
    *,
    gb: int,
    days: int,
) -> ProvisionResult:
    """Create ONE config for this purchase on the reseller's chosen panel. Each purchase → a fresh uuid."""
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return ProvisionResult(False, reason="error", message="reseller not found")
    base = f"{(customer.name or 'cust')[:16]}-{customer.telegram_id}"
    try:
        result = await usercreate.create_for_reseller(
            session, reseller, count=1, gb=int(gb), days=int(days), base_name=base
        )
    except Exception as exc:  # noqa: BLE001 — surface any panel error as a typed failure (caller refunds)
        return ProvisionResult(False, reason="error", message=str(exc)[:300])
    if getattr(result, "error", None):
        return ProvisionResult(False, reason="error", message=str(result.error)[:300])
    if getattr(result, "capacity_blocked", False):
        return ProvisionResult(False, reason="capacity", message="capacity reached")
    if getattr(result, "limit_hit", False) or not result.created:
        return ProvisionResult(False, reason="limit", message="panel user limit hit")
    u = result.created[0]
    return ProvisionResult(True, sub_link=u.sub_link, uuid=u.uuid)
