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
from dataclasses import dataclass

import httpx

from app.services.panel_client.base import PanelClient, PanelData, parse_backup

log = logging.getLogger("panel.admin_api")

# Flask-Admin mounts each model view under the LOWERCASED MODEL CLASS NAME, and Hiddify registers
# the admin view as `flaskadmin.add_view(AdminstratorAdmin(AdminUser, db.session))` — so the admin
# list lives at `/admin/adminuser/`, NOT at `/admin/admin/` (that path 404s with a JSON body, which
# is exactly how the lockout silently failed for every admin until 2026-07-31). The user list, by the
# same rule, is `/admin/user/`. Verified against a live v12 panel + the upstream source.
ADMIN_LIST_PATH = "/admin/adminuser/"


class UserLimitError(RuntimeError):
    """Raised when the panel rejects a create because the admin's max_users is reached."""


def _tag_attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def _scrape_edit_form_fields(html_text: str) -> dict[str, str]:
    """Mirror a Flask-Admin edit <form>: return every current field value so we can resend it
    verbatim. Isolates the form block that carries `new_password` (the admin edit form), then reads
    inputs (skipping submit/button/file; a checkbox/radio only when `checked`), the selected <option>
    of each <select>, and each <textarea>'s body. Injecting one field into this mirror is the safest
    way to submit the form without inventing values that could overwrite name/limits/uuid."""
    block = None
    for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", html_text, re.S | re.I):
        if "new_password" in m.group(1):
            block = m.group(1)
            break
    if block is None:
        return {}
    fields: dict[str, str] = {}
    for tag in re.finditer(r"<input\b[^>]*?>", block, re.I):
        t = tag.group(0)
        name = _tag_attr(t, "name")
        if not name:
            continue
        typ = (_tag_attr(t, "type") or "text").lower()
        if typ in ("submit", "button", "image", "file", "reset"):
            continue
        if typ in ("checkbox", "radio"):
            if re.search(r"\bchecked\b", t, re.I):
                fields[name] = html.unescape(_tag_attr(t, "value") or "y")
            continue
        fields[name] = html.unescape(_tag_attr(t, "value") or "")
    for sm in re.finditer(
        r'<select\b[^>]*?\bname\s*=\s*["\']([^"\']+)["\'][^>]*?>(.*?)</select>', block, re.S | re.I
    ):
        name, body = sm.group(1), sm.group(2)
        opt = re.search(
            r'<option\b[^>]*?\bselected\b[^>]*?\bvalue\s*=\s*["\']([^"\']*)["\']', body, re.I
        ) or re.search(
            r'<option\b[^>]*?\bvalue\s*=\s*["\']([^"\']*)["\'][^>]*?\bselected\b', body, re.I
        )
        if opt is None:
            opt = re.search(r'<option\b[^>]*?\bvalue\s*=\s*["\']([^"\']*)["\']', body, re.I)
        if opt is not None:
            fields[name] = html.unescape(opt.group(1))
    for tm in re.finditer(
        r'<textarea\b[^>]*?\bname\s*=\s*["\']([^"\']+)["\'][^>]*?>(.*?)</textarea>',
        block,
        re.S | re.I,
    ):
        fields[tm.group(1)] = html.unescape(tm.group(2))
    return fields


@dataclass(frozen=True)
class RenewUserTarget:
    """Absolute, replayable panel state for one storefront renewal."""

    usage_limit_gb: float
    package_days: int
    prior_start_date: str | None

    @property
    def body(self) -> dict:
        return {
            "usage_limit_GB": self.usage_limit_gb,
            # CLEAN RESET: zero the consumed counter so a renewal is a FRESH plan (limit = the plan
            # sold, remaining = the whole plan) instead of the old cumulative `used + gb`, which
            # inflated the quota every cycle and over-billed the reseller. Auto-renew fires at
            # near-exhaustion, so resetting forfeits ~no volume. Hiddify accepts + persists this
            # (verified on prod). `start_date: None` restarts the day-clock on first connect.
            "current_usage_GB": 0,
            "package_days": self.package_days,
            "start_date": None,
            "enable": True,
        }


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

    async def get_user(  # noqa: ANN001
        self, panel, user_uuid: str, *, api_key: str | None = None
    ) -> dict | None:
        """Live read of ONE end-user via `GET /user/{uuid}/` — the full record (used by the storefront
        «my services» live view: current_usage_GB, usage_limit_GB, remaining_day, …). Returns the parsed
        dict, or None if the user is absent (404). Other HTTP errors raise."""
        url = f"{panel.admin_api_base}/user/{user_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers(panel, api_key))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"GET user {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data if isinstance(data, dict) else None

    async def patch_user(  # noqa: ANN001
        self, panel, user_uuid: str, body: dict, *, api_key: str | None = None
    ) -> None:
        """PATCH one user (usage_limit_GB / package_days / start_date / enable / …) via
        `PATCH /user/{uuid}/`. Tolerates the Hiddify v12 quirk where the PATCH applies but returns a
        non-2xx: re-GET and accept if every field we sent actually took effect."""
        url = f"{panel.admin_api_base}/user/{user_uuid}/"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.patch(url, headers=self._headers(panel, api_key), json=body)
            if resp.status_code < 400:
                return
            try:
                check = await client.get(url, headers=self._headers(panel, api_key))
                if check.status_code < 400:
                    d = check.json()
                    if isinstance(d, dict) and all(d.get(k) == v for k, v in body.items()):
                        return
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"PATCH user {resp.status_code}: {resp.text[:300]}")

    async def delete_user(  # noqa: ANN001
        self, panel, user_uuid: str, *, api_key: str | None = None
    ) -> None:
        """Delete ONE end-user. Hiddify v2 has no single-user DELETE, so resolve the numeric id (as the
        owning admin) and use the native bulk action. A missing user (already gone) is a no-op.

        The SAME `api_key` must travel to BOTH calls. Resolving the id as the owning admin but then
        deleting as the super-admin would throw away the panel-side scoping that is our second line
        of defence against a stale/foreign numeric id — the exact hole that let one reseller's
        suspension disable 305 other resellers' users (M-incident 2026-07-18). Deletion is
        unrecoverable, so here the id must fail closed, not merely be believed."""
        uid = await self.get_user_id(panel, user_uuid, api_key=api_key)
        if uid is None:
            return
        await self.bulk_delete_users(panel, [uid], api_key=api_key)

    async def prepare_renew_user(  # noqa: ANN001
        self, panel, user_uuid: str, *, gb: int, days: int, api_key: str | None = None
    ) -> RenewUserTarget:
        """Read and calculate the absolute renewal target without mutating the panel.

        CLEAN RESET: the target quota is exactly the plan `gb` (from ZERO usage — see
        `RenewUserTarget.body`), NOT the old cumulative `used + gb`. So a renewed config reads as the
        plan it sold, the quota never compounds across renewals, and the reseller invoice is correct
        at the source (the billing cap then becomes a harmless no-op for cleanly-renewed configs).
        The panel row is still read here only to capture `prior_start_date` for the durable
        reconciler's applied-check."""
        data = await self.get_user(panel, user_uuid, api_key=api_key)
        if data is None:
            raise RuntimeError("panel user not found")
        raw_start = data.get("start_date")
        return RenewUserTarget(
            usage_limit_gb=round(float(gb), 3),
            package_days=int(days),
            prior_start_date=(str(raw_start)[:32] if raw_start not in (None, "") else None),
        )

    async def apply_renew_user_target(  # noqa: ANN001
        self, panel, user_uuid: str, target: RenewUserTarget, *, api_key: str | None = None
    ) -> None:
        """Apply a previously persisted absolute target; safe to retry during crash recovery."""
        await self.patch_user(panel, user_uuid, target.body, api_key=api_key)

    async def renew_user(  # noqa: ANN001
        self, panel, user_uuid: str, *, gb: int, days: int, api_key: str | None = None
    ) -> None:
        """Backward-compatible prepare+apply wrapper for non-durable callers and adapter tests."""
        target = await self.prepare_renew_user(
            panel, user_uuid, gb=gb, days=days, api_key=api_key)
        await self.apply_renew_user_target(panel, user_uuid, target, api_key=api_key)

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

    async def _user_bulk_action(  # noqa: ANN001
        self, panel, user_ids: list[int], action: str, *, api_key: str | None = None
    ) -> None:
        """Run Hiddify's native Flask-Admin user-list bulk `action` (enable/disable/delete) on the
        given numeric rowids in ONE POST.

        Hiddify has no bulk endpoint in its public v2 REST API. Its own user-list UI does provide
        bulk actions that update all selected rows in one SQL statement, update the core clients
        server-side, and invoke quick_apply_users only once per batch — so this is far gentler on
        the panel than per-user REST calls (each of which quick_applies).

        ALWAYS pass `api_key` = the uuid of the admin that OWNS these users. Hiddify then applies
        the action within that admin's own scope, so a rowid that is not theirs simply does not
        match and nothing happens to it. Running this as the super-admin (the old behaviour) is what
        turned a wrong-id bug into 305 disabled users belonging to ~20 other resellers: the panel
        had no reason to refuse. With the owning admin's key the panel itself is the second line of
        defence, independent of any id correctness on our side."""
        if not user_ids:
            return
        list_url = f"{panel.proxy_base}/admin/user/"
        action_url = f"{panel.proxy_base}/admin/user/action/"
        headers = self._headers(panel, api_key)
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
                    "action": action,
                    "rowid": [str(user_id) for user_id in user_ids],
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Hiddify bulk user action {response.status_code}: {response.text[:300]}"
                )

    async def bulk_set_users_enabled(  # noqa: ANN001
        self, panel, user_ids: list[int], enabled: bool, *, api_key: str | None = None
    ) -> None:
        """Bulk enable/disable users via Hiddify's native Flask-Admin action (one apply per batch).
        Pass `api_key` = the OWNING admin's uuid so the panel confines the action to their users."""
        await self._user_bulk_action(
            panel, user_ids, "enable" if enabled else "disable", api_key=api_key)

    async def bulk_delete_users(  # noqa: ANN001
        self, panel, user_ids: list[int], *, api_key: str | None = None
    ) -> None:
        """Bulk DELETE users via Hiddify's native Flask-Admin action (one quick_apply per batch).
        Used by the cascade admin-deletion; far gentler than per-user REST deletes. Pass `api_key`
        = the OWNING admin's uuid — deletion is unrecoverable, so panel-side scoping matters most
        here."""
        await self._user_bulk_action(panel, user_ids, "delete", api_key=api_key)

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

    async def set_admin_password(  # noqa: ANN001
        self, panel, admin_uuid: str, password: str, *, api_key: str | None = None
    ) -> None:
        """Set an admin's LOGIN password via Hiddify's own Flask-Admin edit form.

        Hiddify's v2 REST API exposes NO password field (verified against `PatchAdminSchema`), so —
        exactly like `_user_bulk_action` does for bulk enable/disable — we drive the panel's own
        authenticated web form. Setting a non-empty password matters for enforcement: Hiddify's auth
        rejects UUID-link login when `account.password != ""`, so a suspended reseller can no longer
        open their panel via their UUID link and re-enable their own users. Restore sets it back.

        The form is submitted by MIRRORING it: we GET the edit page and resend every field the panel
        rendered verbatim, injecting only `new_password`. This never invents a value, so it cannot
        clobber the admin's name / limits / uuid / mode even if Hiddify reshuffles the form. Auth uses
        the panel's super-admin key (default header) so it can edit any sub-admin. Best-effort — the
        caller wraps this; a failure is logged, never fatal to the suspend/restore."""
        headers = self._headers(panel, api_key)
        headers["User-Agent"] = "invoice-system-admin-lockout/1"
        list_url = f"{panel.proxy_base}{ADMIN_LIST_PATH}"
        async with httpx.AsyncClient(
            timeout=max(self.timeout, 120.0), follow_redirects=False, headers=headers
        ) as client:
            # 1) Resolve the Flask-Admin numeric row id by searching the admin list by uuid (the
            #    list is `column_searchable_list = ["name", "uuid"]`, so uuid search is exact).
            search = await client.get(list_url, params={"search": admin_uuid})
            if search.status_code >= 400:
                raise RuntimeError(f"admin list {search.status_code}: {search.text[:200]}")
            ids = re.findall(rf'{ADMIN_LIST_PATH}edit/\?[^"\']*?\bid=(\d+)', search.text)
            unique_ids = sorted(set(ids))
            if not unique_ids:
                raise RuntimeError(f"admin {admin_uuid} not found in list")
            if len(unique_ids) > 1:
                # Ambiguous → fail closed rather than risk editing the wrong admin.
                raise RuntimeError(f"admin {admin_uuid} search returned {len(unique_ids)} rows")
            row_id = unique_ids[0]

            # 2) GET the edit form and mirror every field, then inject the new password.
            edit_url = f"{list_url}edit/"
            page = await client.get(edit_url, params={"id": row_id})
            if page.status_code >= 400:
                raise RuntimeError(f"admin edit page {page.status_code}: {page.text[:200]}")
            fields = _scrape_edit_form_fields(page.text)
            if "new_password" not in fields and "csrf_token" not in fields:
                raise RuntimeError("admin edit form not recognized (no csrf/new_password)")
            # The form carries the admin's own uuid — the one identity Hiddify never renumbers. Refuse
            # to POST a form that belongs to a DIFFERENT admin: a row id resolved from a list page is
            # exactly the kind of indirection that once disabled 305 other resellers' users.
            form_uuid = (fields.get("uuid") or "").strip().lower()
            if form_uuid and form_uuid != admin_uuid.strip().lower():
                raise RuntimeError(f"admin edit form is for another admin (row {row_id})")
            fields["new_password"] = password

            resp = await client.post(edit_url, params={"id": row_id}, data=fields)
            # Flask-Admin redirects (3xx) to the list on success; a validation error re-renders the
            # form (200) with an error alert. Treat a 200 carrying an error marker as a failure.
            if resp.status_code >= 400:
                raise RuntimeError(f"admin edit POST {resp.status_code}: {resp.text[:200]}")
            if resp.status_code < 300 and re.search(
                r'alert-danger|is-invalid|has-error', resp.text, re.I
            ):
                raise RuntimeError("admin edit POST rejected (form validation error)")

    async def delete_admin(  # noqa: ANN001
        self, panel, admin_uuid: str, *, api_key: str | None = None
    ) -> None:
        """Delete ONE admin via `DELETE /api/v2/admin/admin_user/{uuid}/`.

        This does NOT cascade: Hiddify answers 500 with MySQL 1451
        (`FOREIGN KEY (parent_admin_id) REFERENCES admin_user(id)`) while the admin still has
        sub-admins, so callers must delete a subtree deepest-first (see
        `enforcement._delete_order`). Leftover users are reassigned to the caller, so the cascade
        deletes all subtree users FIRST. 404 (already gone) is tolerated, which makes retries
        idempotent; the panel refuses to delete the super-admin/self (we never target those)."""
        url = f"{panel.admin_api_base}/admin_user/{admin_uuid}/"
        async with httpx.AsyncClient(timeout=max(self.timeout, 120.0)) as client:
            resp = await client.delete(url, headers=self._headers(panel, api_key))
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            raise RuntimeError(f"DELETE admin_user {resp.status_code}: {resp.text[:300]}")
