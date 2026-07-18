from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.panel_client import admin_api


class _Response:
    def __init__(self, status_code: int, *, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

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
