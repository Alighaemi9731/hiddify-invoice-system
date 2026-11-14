#!/usr/bin/env python3
"""Install the owner-only invoice password API on the pinned Hiddify Panel 14.2.0.

--check is read-only. No service is restarted by this program. Unknown sources fail closed.
"""
from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import os
from pathlib import Path

# Manager v14.2.0 pins Panel 9f811a25aeac8df9c9995983ff4d8d036cd0ace2.
EXPECTED_SHA256 = "de113824f3984b0d2362e2a525f935cc4a40b31cb42e878062ef64971863324e"
ANCHOR = "    app.register_blueprint(bp)"
HOOK = "    from hiddifypanel.invoice_compat import register as register_invoice_compat\n    register_invoice_compat(bp)\n"
MODULE = '''"""Owner-only password set/clear for the Hiddify invoice integration, protocol v1."""
from flask import request
from flask.views import MethodView
from apiflask import abort
from hiddifypanel import g, hutils
from hiddifypanel.auth import login_required
from hiddifypanel.models import AdminUser, Role


def register(bp):
    if getattr(bp, "_invoice_password_registered", False):
        return

    class InvoiceAdminPassword(MethodView):
        decorators = [login_required({Role.super_admin})]

        def target(self, uuid):
            # Use the actual owner identity as well as the role; never edit an owner.
            if g.account.uuid != AdminUser.get_super_admin_uuid():
                abort(403, "Only the owner can use this endpoint")
            target = AdminUser.by_uuid(str(uuid), create=False)
            if target is None:
                abort(404, "Admin not found")
            if target.id == g.account.id or target.role == Role.super_admin:
                abort(403, "The owner password cannot be changed here")
            return target

        def get(self, uuid):
            target = self.target(uuid)
            return {"extension_version": 1, "uuid": target.uuid,
                    "has_password": bool(target.password)}

        def patch(self, uuid):
            target = self.target(uuid)
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or set(body) != {"password"}:
                abort(400, "Expected exactly one password field")
            password = body["password"]
            if not isinstance(password, str) or len(password) > 100:
                abort(400, "Password must be a string of at most 100 characters")
            target.update_password(password)
            hutils.node.parent.notify_childs_users_changed()
            return {"status": 200, "extension_version": 1}

    @login_required({Role.super_admin})
    def capabilities():
        if g.account.uuid != AdminUser.get_super_admin_uuid():
            abort(403, "Only the owner can use this endpoint")
        return {"extension_version": 1, "password_set_clear": True}

    bp.add_url_rule("/invoice/capabilities/", endpoint="invoice_capabilities",
                    view_func=capabilities, methods=["GET"])
    bp.add_url_rule("/invoice/admin-password/<uuid:uuid>/",
                    view_func=InvoiceAdminPassword.as_view("invoice_admin_password"))
    bp._invoice_password_registered = True
'''


def atomic_write(path: Path, value: str, mode_from: Path) -> None:
    stat = mode_from.stat()
    temporary = path.with_suffix(path.suffix + ".invoice-new")
    temporary.write_text(value)
    os.chmod(temporary, stat.st_mode)
    if os.geteuid() == 0:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.replace(path)


def install(package: Path, *, check: bool = False, uninstall: bool = False) -> dict:
    target = package / "panel/commercial/restapi/v2/admin/__init__.py"
    source = target.read_text()
    original = source.replace(HOOK, "", 1)
    if hashlib.sha256(original.encode()).hexdigest() != EXPECTED_SHA256 or original.count(ANCHOR) != 1:
        raise ValueError("Unknown panel source; refusing to patch")
    module = package / "invoice_compat.py"
    if module.exists() and module.read_text() != MODULE:
        raise ValueError("Existing invoice extension differs; refusing to overwrite")
    installed = HOOK in source and module.exists()
    if check:
        return {"compatible": True, "installed": installed, "extension_version": 1}
    if uninstall:
        reload_required = HOOK in source or module.exists()
        atomic_write(target, original, target)
        if module.exists():
            module.unlink()
        return {"compatible": True, "installed": False, "reload_required": reload_required}
    updated = original.replace(ANCHOR, HOOK + ANCHOR, 1)
    ast.parse(updated)
    ast.parse(MODULE)
    backup = target.with_suffix(".py.invoice-original")
    if not backup.exists():
        atomic_write(backup, original, target)
    atomic_write(module, MODULE, target)
    atomic_write(target, updated, target)
    return {"compatible": True, "installed": True, "reload_required": not installed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    packages = [args.package] if args.package else [Path(p) for p in glob.glob(
        "/opt/hiddify-manager/.venv313/lib/python3.13/site-packages/hiddifypanel")]
    if len(packages) != 1:
        raise SystemExit("Exactly one Hiddify v14 package must be selected")
    print(json.dumps(install(packages[0], check=args.check, uninstall=args.uninstall)))


if __name__ == "__main__":
    main()
