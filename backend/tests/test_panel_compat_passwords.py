import hashlib
import runpy
from pathlib import Path

import httpx
import pytest

from app.services.panel_client.admin_api import AdminApiClient
from tests.test_panel_compat_reads import panel, transport


async def test_v14_set_clear_preserves_uuid_login(monkeypatch):
    secret = ""
    def handle(request):
        nonlocal secret
        if "/admin/adminuser/" in request.url.path:
            return httpx.Response(404)
        assert "/invoice/admin-password/agent/" in request.url.path
        assert request.headers["Hiddify-API-Key"] == "owner"
        if request.method == "PATCH":
            import json
            secret = json.loads(request.content)["password"]
        return httpx.Response(200, json={"extension_version": 1, "has_password": bool(secret)})
    transport(monkeypatch, handle)
    async def login(self, panel, uid):
        return not secret
    monkeypatch.setattr(AdminApiClient, "admin_link_login_works", login)
    api = AdminApiClient()
    await api.set_admin_password(panel(), "agent", "blocked-node")
    assert secret == "blocked-node"
    await api.set_admin_password(panel(), "agent", "")
    assert secret == ""
    with pytest.raises(RuntimeError, match="owner"):
        await api.set_admin_password(panel(), "owner", "anything")


def test_installer_checks_layout_and_is_reversible(tmp_path):
    extension = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deploy/hiddify-invoice-compat.py"))
    install = extension["install"]
    package = tmp_path / "hiddifypanel"
    source_file = package / "panel/commercial/restapi/v2/admin/__init__.py"
    source_file.parent.mkdir(parents=True)
    original = 'def init_app(app):\n    app.register_blueprint(bp)\n'
    source_file.write_text(original)
    with pytest.raises(ValueError, match="Unknown panel source"):
        install(package)
    install.__globals__["EXPECTED_SHA256"] = hashlib.sha256(original.encode()).hexdigest()
    assert install(package, check=True)["installed"] is False
    assert source_file.read_text() == original
    assert install(package)["reload_required"] is True
    assert install(package)["reload_required"] is False
    assert install(package, check=True)["installed"] is True
    assert install(package, uninstall=True)["installed"] is False
    assert source_file.read_text() == original
    assert not (package / "invoice_compat.py").exists()
