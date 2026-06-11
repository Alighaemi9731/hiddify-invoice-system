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
