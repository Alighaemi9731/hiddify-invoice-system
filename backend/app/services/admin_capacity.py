"""
Admin (reseller) capacity helpers: how full an admin's user quota is, bumping their
`max_users`/`max_active_users`, and toggling their `can_add_admin` permission — all via
the Hiddify Admin REST API. The panel does NOT store a current user count on the admin
object, so "used" is counted from the end-user snapshots whose `added_by_uuid` is the
admin (this admin only, not its sub-resellers — it mirrors what the panel caps).
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Panel, Reseller
from app.services.panel_client.admin_api import AdminApiClient

log = logging.getLogger("admin_capacity")


async def bump_limits(
    session: AsyncSession, reseller: Reseller, amount: int
) -> tuple[int, int]:
    """Add `amount` to the admin's max_users AND max_active_users on the panel, then
    persist the new values locally. Reads the CURRENT limits from the API first (the
    backup sync may lag), so repeated clicks compound correctly. Returns the new
    (max_users, max_active_users). Raises on API failure."""
    if amount <= 0:
        raise ValueError("amount must be a positive number")  # «bump» only ever increases capacity
    panel = await session.get(Panel, reseller.panel_id)
    client = AdminApiClient()
    # Authenticate as the admin's parent (Hiddify permits editing an admin_user when the
    # acting key is its parent), falling back to the panel/owner key inside the client.
    parent = reseller.parent_admin_uuid
    cur_mu, cur_mau = await client.get_admin_limits(panel, reseller.admin_uuid, api_key=parent)
    if cur_mu is None:
        cur_mu = reseller.panel_max_users or 0
    if cur_mau is None:
        cur_mau = reseller.panel_max_active_users or 0
    new_mu = max(0, int(cur_mu) + amount)
    new_mau = max(0, int(cur_mau) + amount)
    await client.set_admin_limits(panel, reseller.admin_uuid, new_mu, new_mau, api_key=parent)
    reseller.panel_max_users = new_mu
    reseller.panel_max_active_users = new_mau
    await session.commit()
    log.info("Bumped %s limits by %d → mu=%d mau=%d", reseller.name, amount, new_mu, new_mau)
    return new_mu, new_mau


async def set_can_add_admin(
    session: AsyncSession, reseller: Reseller, enabled: bool
) -> None:
    """Toggle the admin's ability to create sub-admins on the panel + persist locally."""
    panel = await session.get(Panel, reseller.panel_id)
    client = AdminApiClient()
    await client.set_can_add_admin(
        panel, reseller.admin_uuid, enabled, api_key=reseller.parent_admin_uuid
    )
    reseller.can_add_admin = enabled
    await session.commit()
    log.info("Set can_add_admin=%s for %s", enabled, reseller.name)
