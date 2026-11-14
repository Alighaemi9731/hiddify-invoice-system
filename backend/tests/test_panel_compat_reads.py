"""Real HTTP transport contracts for the two supported panel generations."""
from types import SimpleNamespace

import httpx
import pytest

from app.services.panel_client.admin_api import AdminApiClient
from app.services.panel_client.backup_json import BackupJsonClient


def panel():
    return SimpleNamespace(
        key="test", owner_uuid="owner", admin_api_key="other-admin",
        proxy_base="https://panel.example/secret",
        base_secret_url="https://panel.example/secret/owner",
        backup_url="https://panel.example/secret/admin/backup/backupfile/",
        admin_api_base="https://panel.example/secret/api/v2/admin",
    )


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("version", [12, 14])
@pytest.mark.parametrize("adapter", [BackupJsonClient, AdminApiClient])
async def test_backup_both_versions(monkeypatch, version, adapter):
    def handle(request):
        valid = request.url.path.endswith(
            "/backup/download/" if version == 14 else "/admin/backup/backupfile/")
        if valid:
            if version == 14:
                assert request.headers["Hiddify-API-Key"] == "owner"
            return httpx.Response(200, json={
                "admin_users": [{"uuid": "owner", "mode": "super_admin"}], "users": [],
                "hconfigs": [{"key": "proxy_path_client", "value": "client"}],
            })
        if request.url.path.endswith("/api/v2/admin/backup/"):
            return httpx.Response(200, json={"current": {}, "files": []})
        return httpx.Response(404)
    transport(monkeypatch, handle)
    result = await adapter().fetch_backup(panel())
    assert len(result.admins) == 1
    assert result.users == []
    assert result.client_proxy_path == "client"


@pytest.mark.parametrize("payload", [{"current": {}, "files": []}, {"admin_users": [], "users": []},
                                     {"admin_users": [{"uuid": "owner"}]}])
async def test_api_never_accepts_metadata_as_empty_backup(monkeypatch, payload):
    transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(RuntimeError):
        await AdminApiClient().fetch_backup(panel())


async def test_backup_never_follows_secret_to_other_origin(monkeypatch):
    visited = []
    def handle(request):
        visited.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://foreign.example/"})
    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError):
        await BackupJsonClient().fetch_backup(panel())
    assert set(visited) == {"panel.example"}
