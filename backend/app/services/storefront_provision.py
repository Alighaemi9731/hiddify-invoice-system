"""Provision a VPN config for a storefront purchase.

Reuses `usercreate.create_for_reseller`, which creates the user on the reseller's panel AS the
reseller (`api_key = reseller.admin_uuid`) — so the config counts toward the reseller's own usage that
the owner bills. Returns the subscription link + uuid, or a typed failure the caller turns into a
wallet refund + admin nudge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller, StorefrontBot, StorefrontCustomer, StorefrontOrder
from app.services import usercreate

log = logging.getLogger("bot.storefront")


@dataclass
class ProvisionResult:
    ok: bool
    sub_link: str | None = None
    uuid: str | None = None
    reason: str | None = None      # "capacity" | "limit" | "error"
    message: str | None = None


@dataclass
class LiveStatus:
    ok: bool                       # False → the panel read failed; show the stored plan only
    used_gb: float = 0.0
    limit_gb: float = 0.0
    remaining_days: int | None = None


async def provision(
    session: AsyncSession,
    bot: StorefrontBot,
    customer: StorefrontCustomer,
    *,
    gb: int,
    days: int,
    label: str | None = None,
) -> ProvisionResult:
    """Create ONE config for this purchase on the reseller's chosen panel. Each purchase → a fresh uuid.

    `label` is the customer-chosen name: it becomes the config's name on the panel AND the sub-link
    `#slug`, so a customer who buys several services can tell them apart. Falls back to a
    name+telegram-id base when no label is supplied."""
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return ProvisionResult(False, reason="error", message="reseller not found")
    base = (label or "").strip() or f"{(customer.name or 'cust')[:16]}-{customer.telegram_id}"
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


def _to_float(value) -> float:  # noqa: ANN001
    try:
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


async def live_status(
    session: AsyncSession, bot: StorefrontBot, order: StorefrontOrder
) -> LiveStatus:
    """Read this order's config LIVE from the panel (used GB, limit GB, remaining days). Best-effort:
    any failure (no uuid, panel error, missing user) returns ok=False so the caller falls back to the
    stored plan figures."""
    if not order.panel_user_uuid:
        return LiveStatus(False)
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return LiveStatus(False)
    from app.models import Panel
    from app.services.panel_client.admin_api import AdminApiClient

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        return LiveStatus(False)
    try:
        data = await AdminApiClient().get_user(
            panel, order.panel_user_uuid, api_key=reseller.admin_uuid
        )
    except Exception:  # noqa: BLE001 — live read is best-effort; caller shows the stored plan
        log.warning("storefront live_status read failed", exc_info=True)
        return LiveStatus(False)
    if not data:
        return LiveStatus(False)
    remaining = data.get("remaining_day")
    try:
        remaining_days = int(remaining) if remaining is not None else None
    except (TypeError, ValueError):
        remaining_days = None
    return LiveStatus(
        True,
        used_gb=_to_float(data.get("current_usage_GB")),
        limit_gb=_to_float(data.get("usage_limit_GB")) or float(order.gb or 0),
        remaining_days=remaining_days,
    )
