from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.panel_client import admin_api


class _Response:
    def __init__(
        self, status_code: int, *, json_data=None, text: str = "", headers: dict | None = None
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _panel():
    return SimpleNamespace(
        key="test",
        owner_uuid="owner-key",
        admin_api_key=None,
        admin_api_base="https://panel.example/proxy/api/v2/admin",
        proxy_base="https://panel.example/proxy",
    )


def test_get_user_ids_maps_only_valid_rows(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            assert url.endswith("/api/v2/admin/user/")
            assert headers["Hiddify-API-Key"] == "owner-key"
            return _Response(200, json_data=[
                {"uuid": "u1", "id": 11},
                {"uuid": "u2", "id": 12},
                {"uuid": "missing-id"},
            ])

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(admin_api.AdminApiClient().get_user_ids(_panel()))
    assert result == {"u1": 11, "u2": 12}


def test_get_user_id_single(monkeypatch):
    """Per-user id lookup: returns the int id on 200, None on 404 (absent), used by enforcement
    instead of fetching the whole panel."""
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers):
            if url.endswith("/user/present-uuid/"):
                return _Response(200, json_data={"uuid": "present-uuid", "id": 77})
            return _Response(404, text="not found")

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    c = admin_api.AdminApiClient()
    assert asyncio.run(c.get_user_id(_panel(), "present-uuid")) == 77
    assert asyncio.run(c.get_user_id(_panel(), "gone-uuid")) is None


def test_bulk_set_users_enabled_posts_native_hiddify_action(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            captured["get"] = url
            return _Response(
                200,
                text='<form><input name="csrf_token" value="token&amp;value"></form>',
            )

        async def post(self, url, data):
            captured["post"] = (url, data)
            return _Response(302)

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    asyncio.run(
        admin_api.AdminApiClient().bulk_set_users_enabled(_panel(), [11, 12], False)
    )

    assert captured["get"] == "https://panel.example/proxy/admin/user/"
    url, data = captured["post"]
    assert url == "https://panel.example/proxy/admin/user/action/"
    assert data["csrf_token"] == "token&value"
    assert data["action"] == "disable"
    assert data["rowid"] == ["11", "12"]
    assert captured["init"]["headers"]["Hiddify-API-Key"] == "owner-key"


def test_bulk_delete_users_posts_delete_action(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url):
            return _Response(200, text='<input name="csrf_token" value="tok">')
        async def post(self, url, data):
            captured["post"] = (url, data)
            return _Response(302)

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    asyncio.run(admin_api.AdminApiClient().bulk_delete_users(_panel(), [21, 22]))
    url, data = captured["post"]
    assert url == "https://panel.example/proxy/admin/user/action/"
    assert data["action"] == "delete"
    assert data["rowid"] == ["21", "22"]


def test_delete_user_acts_as_the_owning_admin_end_to_end(monkeypatch):
    """A single-user delete must run under the OWNING admin's key, not the super-admin's.

    `delete_user` resolved the numeric id as the owning admin but then called the destructive
    bulk action with no key, so the POST went out as the panel super-admin. That discards the
    panel-side scoping which is our second line of defence: under the owner's key Hiddify simply
    does not match a rowid that isn't theirs, so a stale/foreign id fails closed. As the
    super-admin the panel has no reason to refuse — the exact shape of the 2026-07-18 incident,
    except deletion is unrecoverable.
    """
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.setdefault("init_headers", []).append(kwargs.get("headers", {}))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers=None):
            if headers is not None:          # id resolution (REST)
                seen["resolve_key"] = headers["Hiddify-API-Key"]
                return _Response(200, json_data={"uuid": "u1", "id": 77})
            return _Response(200, text='<input name="csrf_token" value="tok">')
        async def post(self, url, data):
            seen["rowid"] = data["rowid"]
            seen["action"] = data["action"]
            return _Response(302)

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    asyncio.run(admin_api.AdminApiClient().delete_user(_panel(), "u1", api_key="OWNER"))

    assert seen["resolve_key"] == "OWNER"          # id resolved as the owning admin
    assert seen["action"] == "delete" and seen["rowid"] == ["77"]
    # …and the destructive POST carried the SAME credential, not the panel super-admin's.
    bulk_headers = seen["init_headers"][-1]
    assert bulk_headers["Hiddify-API-Key"] == "OWNER", (
        "bulk delete fell back to the super-admin key — panel-side scoping lost"
    )


def test_delete_user_absent_on_panel_deletes_nothing(monkeypatch):
    """A 404 during id resolution must short-circuit — never a keyless bulk call with no rowids."""
    posted = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers=None):
            return _Response(404)
        async def post(self, url, data):
            posted.append(data)
            return _Response(302)

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    asyncio.run(admin_api.AdminApiClient().delete_user(_panel(), "gone", api_key="OWNER"))
    assert posted == []


# ── the deleted-admin failure mode (2026-08-06) ──────────────────────────────
#
# A reseller asked us to suspend a sub-reseller and then deleted that sub-admin from the panel
# before the queue got to it. Hiddify answers a page request carrying an unknown admin key with
# `302 → /<proxy>/?force=1&next=…` (auth_before_request → logout_redirect), not a 4xx. The old
# `status >= 400` check let that through, the empty redirect body had no CSRF token, and the
# owner was alerted five times about a CSRF problem that never existed.

_LOGIN_REDIRECT = {"location": "/proxy/?force=1&next=/proxy/admin/user/"}


def _login_redirect_client(*, on_get=True):
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers=None):
            if on_get:
                return _Response(302, headers=_LOGIN_REDIRECT)
            return _Response(200, text='<input name="csrf_token" value="tok">')
        async def post(self, url, data):
            return _Response(302, headers=_LOGIN_REDIRECT)
    return FakeClient


def test_bulk_page_login_redirect_is_an_auth_error_not_a_csrf_error(monkeypatch):
    monkeypatch.setattr(admin_api.httpx, "AsyncClient", _login_redirect_client())
    try:
        asyncio.run(
            admin_api.AdminApiClient().bulk_set_users_enabled(
                _panel(), [11], False, api_key="deadbeef-0000-0000-0000-000000000000")
        )
    except admin_api.PanelAuthError as exc:
        message = str(exc)
    else:
        raise AssertionError("a login redirect must not pass as a scrapeable page")
    assert "CSRF" not in message
    assert "deadbeef…" in message, "the error must name the key the panel refused"
    assert "302" in message


def test_bulk_action_post_bounced_to_login_is_not_reported_as_success(monkeypatch):
    """Flask-Admin redirects to the list on success, so a 3xx POST normally means done — but a
    3xx to the LOGIN page means the write never ran. Accepting it would record an untouched
    batch as disabled, the silent-miss mode the whole verification layer exists to prevent."""
    monkeypatch.setattr(admin_api.httpx, "AsyncClient", _login_redirect_client(on_get=False))
    try:
        asyncio.run(admin_api.AdminApiClient().bulk_set_users_enabled(_panel(), [11], False))
    except admin_api.PanelAuthError:
        return
    raise AssertionError("a POST bounced to the login page must not count as a successful write")


def test_admin_exists_is_three_valued(monkeypatch):
    """404 = definitively gone; anything else unhappy = unknown. "Gone" cancels a suspension, so
    it may never be inferred from a timeout or a 5xx."""
    codes = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers=None):
            if codes["code"] == 0:
                raise RuntimeError("connection reset")
            return _Response(codes["code"], json_data={"uuid": "a"})

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    c = admin_api.AdminApiClient()
    for code, expected in ((200, True), (404, False), (500, None), (403, None), (0, None)):
        codes["code"] = code
        assert asyncio.run(c.admin_exists(_panel(), "some-admin")) is expected, code


def test_get_user_identity_reports_the_live_owner(monkeypatch):
    """The id AND the current `added_by_uuid`, in the one call enforcement already makes — the
    frozen user→owner map of a queued action cannot be trusted once an admin is deleted."""
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, headers=None):
            if url.endswith("/user/moved/"):
                return _Response(200, json_data={"id": 5, "added_by_uuid": "AAAA1111-BBBB"})
            return _Response(404)

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    c = admin_api.AdminApiClient()
    identity = asyncio.run(c.get_user_identity(_panel(), "moved"))
    assert identity.panel_user_id == 5
    assert identity.added_by_uuid == "aaaa1111-bbbb", "owner uuids are compared lowercased"
    assert asyncio.run(c.get_user_identity(_panel(), "gone")) is None
