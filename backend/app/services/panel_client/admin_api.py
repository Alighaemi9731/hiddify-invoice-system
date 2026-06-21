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

import html
import logging
import re
import uuid as uuidlib

import httpx

from app.services.panel_client.base import PanelClient, PanelData, parse_backup

log = logging.getLogger("panel.admin_api")


class UserLimitError(RuntimeError):
    """Raised when the panel rejects a create because the admin's max_users is reached."""


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

    async def create_user(  # noqa: ANN001
        self, panel, *, name: str, gb: float, days: int, api_key: str | None = None,
        user_uuid: str | None = None,
    ) -> str:
        """Create ONE end-user via the v2 admin API and return its uuid.

        Authenticate AS the reseller (api_key = their admin_uuid): the panel then sets the new
        user's `added_by_uuid` to that admin automatically, so it's billed/owned correctly. The
        POST runs `quick_apply_users` server-side, so the user works immediately (no manual apply).
        We supply our own uuid4 so we know the sub-link without parsing the response. A panel
        rejection for the admin's max_users is surfaced as `UserLimitError`."""
        uid = user_uuid or str(uuidlib.uuid4())
        url = f"{panel.admin_api_base}/user/"
        body = {
            "uuid": uid,
            "name": name,
            "usage_limit_GB": float(gb),
            "package_days": int(days),
            "enable": True,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(panel, api_key), json=body)
        if resp.status_code < 400:
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("uuid"):
                    return str(data["uuid"])
            except Exception:  # noqa: BLE001 — response may not be JSON; our uuid is authoritative
                pass
            return uid
        text = resp.text[:300]
        low = text.lower()
        if resp.status_code in (400, 403) and any(
            k in low for k in ("max", "limit", "quota", "exceed", "ظرفیت", "حداکثر")
        ):
            raise UserLimitError(text)
        raise RuntimeError(f"POST user {resp.status_code}: {text}")

    async def get_user_id(  # noqa: ANN001
        self, panel, user_uuid: str, *, api_key: str | None = None
    ) -> int | None:
        """Hiddify's numeric id for ONE user via `GET /user/{uuid}/`.

        Used by enforcement to resolve only the reseller's TARGET users' ids (the bulk action
        needs numeric rowids) instead of downloading the entire panel user list — far gentler on
        large panels. Returns the int id, or None if the user is absent on the panel (404) so the
        caller skips it. Other HTTP errors raise so the caller's retry path handles them."""
        url = f"{panel.admin_api_base}/user/{user_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel, api_key))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"GET user {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        uid = data.get("id") if isinstance(data, dict) else None
        return int(uid) if isinstance(uid, int) else None

    async def get_user_ids(self, panel) -> dict[str, int]:  # noqa: ANN001
        """Return Hiddify's internal numeric id for every visible user (WHOLE panel — heavy).

        NOTE: no longer on the enforcement hot path (it 503s on large panels); enforcement now
        resolves ids per-target via `get_user_id`. Kept for ad-hoc use / fallback.
        The numeric ids are required by Hiddify's own Flask-Admin bulk action endpoint.
        """
        url = f"{panel.admin_api_base}/user/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel))
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError("Hiddify user list returned a non-list response")
        result: dict[str, int] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            uuid = row.get("uuid")
            user_id = row.get("id")
            if uuid and isinstance(user_id, int):
                result[str(uuid)] = user_id
        return result

    async def bulk_set_users_enabled(  # noqa: ANN001
        self, panel, user_ids: list[int], enabled: bool
    ) -> None:
        """Use Hiddify's native Flask-Admin bulk Enable/Disable action.

        Hiddify has no bulk endpoint in its public v2 REST API. Its own user-list UI does
        provide a bulk action that updates all selected rows in one SQL statement, updates
        the core clients server-side, and invokes quick_apply_users only once per batch.
        """
        if not user_ids:
            return
        list_url = f"{panel.proxy_base}/admin/user/"
        action_url = f"{panel.proxy_base}/admin/user/action/"
        headers = self._headers(panel)
        headers["User-Agent"] = "invoice-system-bulk-enforcement/1"
        async with httpx.AsyncClient(
            timeout=max(self.timeout, 300.0),
            follow_redirects=False,
            headers=headers,
        ) as client:
            page = await client.get(list_url)
            if page.status_code >= 400:
                raise RuntimeError(
                    f"Hiddify bulk user page {page.status_code}: {page.text[:300]}"
                )
            match = re.search(
                r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
                page.text,
            )
            if match is None:
                raise RuntimeError("Hiddify bulk user action has no CSRF token")
            csrf_token = html.unescape(match.group(1))
            response = await client.post(
                action_url,
                data={
                    "csrf_token": csrf_token,
                    "url": list_url,
                    "action": "enable" if enabled else "disable",
                    "rowid": [str(user_id) for user_id in user_ids],
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Hiddify bulk user action {response.status_code}: {response.text[:300]}"
                )

    async def get_admin(  # noqa: ANN001
        self, panel, admin_uuid: str, *, api_key: str | None = None
    ) -> dict | None:
        """Return the full admin_user object, or None on error."""
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel, api_key))
            if resp.status_code >= 400:
                return None
            return resp.json()

    async def get_admin_limits(  # noqa: ANN001
        self, panel, admin_uuid: str, *, api_key: str | None = None
    ) -> tuple[int | None, int | None]:
        """Return (max_users, max_active_users) for an admin, or (None, None)."""
        d = await self.get_admin(panel, admin_uuid, api_key=api_key)
        if d is None:
            return (None, None)
        return (d.get("max_users"), d.get("max_active_users"))

    async def _patch_admin(  # noqa: ANN001
        self, panel, admin_uuid: str, body: dict, *, api_key: str | None = None
    ) -> None:
        """PATCH an admin_user with arbitrary fields, tolerating the Hiddify v12 bug where
        the PATCH applies but returns HTTP 500 ("name 'admins' is not defined"). On a non-2xx
        we re-GET and accept the change if every field we sent actually took effect."""
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(url, headers=self._headers(panel, api_key), json=body)
            if resp.status_code < 400:
                return
            try:
                check = await client.get(url, headers=self._headers(panel, api_key))
                if check.status_code < 400:
                    d = check.json()
                    if all(d.get(k) == v for k, v in body.items()):
                        return
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"PATCH admin_user {resp.status_code}: {resp.text[:300]}")

    async def set_can_add_admin(  # noqa: ANN001
        self, panel, admin_uuid: str, can_add_admin: bool, *, api_key: str | None = None
    ) -> None:
        """Turn an admin's ability to create sub-admins on/off (Hiddify `can_add_admin`)."""
        await self._patch_admin(panel, admin_uuid, {"can_add_admin": can_add_admin}, api_key=api_key)

    async def set_admin_limits(  # noqa: ANN001
        self, panel, admin_uuid: str, max_users: int, max_active_users: int,
        *, api_key: str | None = None,
    ) -> None:
        # KNOWN Hiddify v12 bug: the admin_user PATCH applies the change but then crashes on
        # `return admins` (undefined) → HTTP 500. _patch_admin verifies via GET and accepts it.
        await self._patch_admin(
            panel, admin_uuid,
            {"max_users": max_users, "max_active_users": max_active_users},
            api_key=api_key,
        )
