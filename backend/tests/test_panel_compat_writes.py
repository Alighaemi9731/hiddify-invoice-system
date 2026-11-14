from types import SimpleNamespace

import httpx
import pytest

from app.services.panel_client.admin_api import AdminApiClient
from tests.test_panel_compat_reads import panel, transport


@pytest.mark.parametrize("action", ["enable", "disable", "delete"])
async def test_json_bulk_uses_fresh_scoped_uuid_and_verifies(monkeypatch, action):
    state = {"id": 5, "uuid": "u", "added_by_uuid": "reseller", "enable": action == "disable"}
    posts = []
    def handle(request):
        path = request.url.path
        if path.endswith("/admin/user/"):
            return httpx.Response(404)
        if path.endswith("/user/u/"):
            return httpx.Response(200, json=state) if state else httpx.Response(404)
        if path.endswith("/users/u/"):
            return httpx.Response(200, json={"user": state})
        if path.endswith("/users/bulk/"):
            import json
            body = json.loads(request.content)
            assert body == {"action": action, "uuids": ["u"]}
            assert request.headers["Hiddify-API-Key"] == "reseller"
            posts.append(body)
            if action == "delete":
                state.clear()
            else:
                state["enable"] = action == "enable"
            return httpx.Response(200, json={"count": 1})
        raise AssertionError(path)
    transport(monkeypatch, handle)
    api = AdminApiClient()
    await api.get_user_identity(panel(), "u", api_key="reseller")
    if action == "delete":
        await api.bulk_delete_users(panel(), [5], api_key="reseller")
    else:
        await api.bulk_set_users_enabled(panel(), [5], action == "enable", api_key="reseller")
    assert len(posts) == 1


@pytest.mark.parametrize("changed", ["owner", "id"])
async def test_identity_change_never_mutates(monkeypatch, changed):
    reads = 0
    def handle(request):
        nonlocal reads
        assert request.method == "GET"
        if request.url.path.endswith("/admin/user/"):
            return httpx.Response(404)
        reads += 1
        return httpx.Response(200, json={"id": 6 if changed == "id" and reads > 1 else 5,
            "uuid": "u", "added_by_uuid": "foreign" if changed == "owner" and reads > 1 else "reseller"})
    transport(monkeypatch, handle)
    api = AdminApiClient()
    await api.get_user_identity(panel(), "u")
    with pytest.raises(RuntimeError, match="identity changed|owner mismatch"):
        await api.bulk_delete_users(panel(), [5], api_key="reseller")


async def test_bulk_auth_failure_is_not_version_fallback(monkeypatch):
    seen = []
    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(401)
    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError):
        await AdminApiClient().bulk_delete_users(panel(), [5], api_key="reseller")
    assert len(seen) == 1


async def test_admin_capacity_checks_live_parent_and_uses_owner(monkeypatch):
    from app.services import admin_capacity
    p = panel()
    p.id = 1
    reseller = SimpleNamespace(panel_id=1, admin_uuid="child", parent_admin_uuid="parent")
    async def get_admin(self, panel, uid, *, api_key=None):
        assert api_key == "owner"
        return {"parent_admin_uuid": "parent"}
    monkeypatch.setattr(AdminApiClient, "get_admin", get_admin)
    assert await admin_capacity._authorized_key(None, p, reseller) == "owner"
    reseller.parent_admin_uuid = "other-parent"
    with pytest.raises(RuntimeError, match="parent changed"):
        await admin_capacity._authorized_key(None, p, reseller)


async def test_mixed_fleet_target_maps_do_not_leak_between_panels(monkeypatch):
    old, new = panel(), panel()
    new.proxy_base = "https://new.example/secret"
    new.admin_api_base = new.proxy_base + "/api/v2/admin"
    states = {"panel.example": {"id": 5, "uuid": "old-user", "added_by_uuid": "r", "enable": True},
              "new.example": {"id": 5, "uuid": "new-user", "added_by_uuid": "r", "enable": True}}
    def handle(request):
        state = states[request.url.host]
        path = request.url.path
        if "/api/v2/admin/user/" in path or "/api/v2/admin/users/" in path and request.method == "GET":
            return httpx.Response(200, json={"user": state} if "/users/" in path else state)
        if path.endswith("/admin/user/"):
            return httpx.Response(404) if request.url.host == "new.example" else httpx.Response(
                200, text='<input name="csrf_token" value="token">')
        if path.endswith("/admin/user/action/"):
            state["enable"] = False
            return httpx.Response(302, headers={"Location": "/secret/admin/user/"})
        if path.endswith("/users/bulk/"):
            import json
            assert json.loads(request.content)["uuids"] == ["new-user"]
            state["enable"] = False
            return httpx.Response(200, json={"count": 1})
        raise AssertionError(path)
    transport(monkeypatch, handle)
    api = AdminApiClient()
    await api.get_user_identity(old, "old-user", api_key="r")
    await api.get_user_identity(new, "new-user", api_key="r")
    await api.bulk_set_users_enabled(old, [5], False, api_key="r")
    await api.bulk_set_users_enabled(new, [5], False, api_key="r")
    assert all(not row["enable"] for row in states.values())


@pytest.mark.parametrize("generation", [12, 14])
async def test_partial_date_patch_preserves_legacy_duration_without_changing_intent(monkeypatch, generation):
    import json
    intent = {"start_date": "2026-11-01"}
    def handle(request):
        if request.method == "GET":
            row = {"uuid": "u", "package_days": 30}
            if generation == 14:
                row["deleted"] = False
            return httpx.Response(200, json=row)
        payload = json.loads(request.content)
        assert payload == ({**intent, "package_days": 30} if generation == 12 else intent)
        return httpx.Response(200, json={})
    transport(monkeypatch, handle)
    await AdminApiClient().patch_user(panel(), "u", intent, api_key="r")
    assert intent == {"start_date": "2026-11-01"}
