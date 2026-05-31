"""
Write adapter: Hiddify Admin REST API (v2). Used for enforcement (disable users,
zero an admin's limits) and restore. Needs the per-panel admin API key.

Endpoints (relative to https://<host>/<proxy_path>/<owner_uuid>/api/v2/admin):
  PATCH /user/{uuid}/        body {"enable": false}
  PATCH /admin_user/{uuid}/  body {"max_users": 0, "max_active_users": 0}
Auth header: Hiddify-API-Key: <admin_api_key>
"""
from __future__ import annotations

import logging

import httpx

from app.services.panel_client.base import PanelClient, PanelData, parse_backup

log = logging.getLogger("panel.admin_api")


class AdminApiClient(PanelClient):
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _headers(self, panel) -> dict:  # noqa: ANN001
        # In Hiddify v2 the API key IS the admin uuid, so fall back to the owner uuid
        # when no separate key is configured.
        key = panel.admin_api_key or panel.owner_uuid
        if not key:
            raise RuntimeError(f"Panel '{panel.key}' has no admin API key / owner uuid")
        return {"Hiddify-API-Key": key, "Accept": "application/json"}

    async def fetch_backup(self, panel) -> PanelData:  # noqa: ANN001
        """Optional read path via the API (backup JSON remains the default)."""
        url = f"{panel.admin_api_base}/backup/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel))
            resp.raise_for_status()
            return parse_backup(resp.json())

    async def set_user_enabled(self, panel, user_uuid: str, enabled: bool) -> None:  # noqa: ANN001
        url = f"{panel.admin_api_base}/user/{user_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(url, headers=self._headers(panel), json={"enable": enabled})
            resp.raise_for_status()

    async def set_admin_limits(
        self, panel, admin_uuid: str, max_users: int, max_active_users: int  # noqa: ANN001
    ) -> None:
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        body = {"max_users": max_users, "max_active_users": max_active_users}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(url, headers=self._headers(panel), json=body)
            resp.raise_for_status()
