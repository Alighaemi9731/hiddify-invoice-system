"""
Bulk-reset panel admins' login passwords to a known value (default "123") via the
Hiddify Admin API, using the panel's OWNER API key. Lets the owner re-standardize
passwords after an admin changed theirs or one was set wrong.

NOTE: this depends on the Hiddify build accepting `password` on the admin_user PATCH.
Each admin's result is reported individually so a failure is visible, not silent.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Panel, Reseller
from app.services import settings_service
from app.services.panel_client.admin_api import AdminApiClient

log = logging.getLogger("admin_passwords")


async def reset_admin_passwords(
    session: AsyncSession, *, panel_id: int | None = None, password: str | None = None
) -> dict:
    """Reset every (non-owner) admin's password on one panel — or all panels — to
    `password` (defaults to the `admin_reset_password` setting, i.e. "123")."""
    if password is None:
        password = str(await settings_service.get(session, "admin_reset_password", "123") or "123")

    panel_q = select(Panel)
    if panel_id is not None:
        panel_q = panel_q.where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    client = AdminApiClient()
    ok = 0
    failed: list[dict] = []
    total = 0
    for panel in panels:
        admins = (
            await session.execute(
                select(Reseller).where(
                    Reseller.panel_id == panel.id,
                    Reseller.is_owner.is_(False),
                )
            )
        ).scalars().all()
        for a in admins:
            total += 1
            try:
                await client.set_admin_password(panel, a.admin_uuid, password)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append({"panel": panel.key, "admin": a.name, "error": str(exc)[:200]})
                log.warning("reset password failed for %s/%s: %s", panel.key, a.name, exc)

    return {
        "status": "ok" if not failed else "partial",
        "password": password,
        "total": total,
        "reset": ok,
        "failed": failed,
        "message": (
            f"رمز عبور {ok} از {total} ادمین به «{password}» تغییر کرد."
            + (f" — {len(failed)} مورد ناموفق." if failed else "")
        ),
    }
