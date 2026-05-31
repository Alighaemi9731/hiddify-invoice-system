"""
Write adapter: Hiddify Admin REST API (v2). Used for enforcement (disable users,
zero an admin's limits) and restore. Needs the per-panel admin API key.

Endpoints (relative to https://<host>/<proxy_path>/api/v2/admin — the admin UUID is
NOT in the path; it travels in the Hiddify-API-Key header):
  PATCH /user/{uuid}/        body {"enable": false}
  PATCH /admin_user/{uuid}/  body {"max_users": 0, "max_active_users": 0}
Auth header: Hiddify-API-Key: <admin_api_key (the admin uuid)>
"""
from __future__ import annotations

import logging

import httpx

from app.services.panel_client.base import PanelClient, PanelData, parse_backup

log = logging.getLogger("panel.admin_api")


class AdminApiClient(PanelClient):
    def __init__(self, timeout: float = 90.0) -> None:
        # Hiddify reapplies the whole proxy config on each user PATCH, which can take a
        # while on a busy panel — keep a generous timeout so disabling users doesn't fail.
        self.timeout = timeout

    def _headers(self, panel, api_key: str | None = None) -> dict:  # noqa: ANN001
        # In Hiddify v2 the API key IS an admin's uuid. `api_key` lets a caller act AS a
        # specific admin (needed because the panel only lets you edit a user if you're the
        # super-admin OR the user's own creator). Falls back to the configured key / owner.
        key = api_key or panel.admin_api_key or panel.owner_uuid
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

    async def set_user_enabled(  # noqa: ANN001
        self, panel, user_uuid: str, enabled: bool, *, api_key: str | None = None
    ) -> None:
        url = f"{panel.admin_api_base}/user/{user_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(
                url, headers=self._headers(panel, api_key), json={"enable": enabled}
            )
            # Surface the panel's actual response body on error (status code alone is
            # rarely enough to diagnose why a disable was rejected).
            if resp.status_code >= 400:
                raise RuntimeError(f"PATCH user {resp.status_code}: {resp.text[:300]}")

    async def get_admin_limits(  # noqa: ANN001
        self, panel, admin_uuid: str, *, api_key: str | None = None
    ) -> tuple[int | None, int | None]:
        """Return (max_users, max_active_users) for an admin, or (None, None)."""
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel, api_key))
            if resp.status_code >= 400:
                return (None, None)
            d = resp.json()
            return (d.get("max_users"), d.get("max_active_users"))

    async def set_admin_limits(  # noqa: ANN001
        self, panel, admin_uuid: str, max_users: int, max_active_users: int,
        *, api_key: str | None = None,
    ) -> None:
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        body = {"max_users": max_users, "max_active_users": max_active_users}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(url, headers=self._headers(panel, api_key), json=body)
            if resp.status_code < 400:
                return
            # KNOWN Hiddify v12 bug: the admin_user PATCH applies the change but then
            # crashes on `return admins` (undefined) → HTTP 500 {"msg":"name 'admins'
            # is not defined"}. Verify via GET and accept it if the change really applied.
            try:
                check = await client.get(url, headers=self._headers(panel, api_key))
                if check.status_code < 400:
                    d = check.json()
                    if (d.get("max_users") == max_users
                            and d.get("max_active_users") == max_active_users):
                        return
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"PATCH admin_user {resp.status_code}: {resp.text[:300]}")
