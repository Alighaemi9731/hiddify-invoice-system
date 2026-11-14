"""Exercise real host files/SQLite; fake only systemd and authenticated HTTP."""

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "invoice_panel_addon_test", ROOT / "deploy/panel_addon.py"
)
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
ORIGINAL12 = "class View:\n    def update(self, model):\n        if model.new_password:\n            model.password = model.new_password\n"
ORIGINAL14 = "def init_app(app):\n    app.register_blueprint(bp)\n"


class FakeService:
    def __init__(self, paths, python):
        self.paths, self.python = paths, python
        self.restarts, self.loaded, self.start = 0, False, 0
        self.fail_restart_once = self.fail_verification = False
        self.installer = None

    def active(self):
        return True

    def reload(self):
        pass

    def started_at(self):
        return self.start

    def value(self, key):
        if key == "ExecStart":
            return str(self.python) + " -m hiddifypanel"
        if key != "ExecStartPre":
            raise AssertionError(key)
        commands = ["/usr/bin/true"]
        for file in sorted(self.paths.dropins.glob("*.conf")):
            for line in file.read_text().splitlines():
                if not line.startswith("ExecStartPre="):
                    continue
                command = line.split("=", 1)[1]
                if not command:
                    commands = []
                else:
                    commands.append(command.lstrip("-+!:@").split()[0])
        return " ".join("{ path=" + c + " ; argv[]=... ; }" for c in commands)

    def restart(self):
        self.restarts += 1
        if self.fail_restart_once:
            self.fail_restart_once = False
            raise addon.AddonError("injected restart failure")
        if str(self.paths.wrapper) in self.value("ExecStartPre"):
            self.installer.apply()  # Simulate ExecStartPre; no DB/HTTP is used here.
        self.loaded = self.installer.source_status(addon.discover(self.paths, self))
        self.start = time.time() + 2


@pytest.fixture
def host(tmp_path):
    def make(version="14.2.0"):
        paths = addon.Paths(tmp_path / "host")
        package = paths.manager / ".venv313/lib/python3.13/site-packages/hiddifypanel"
        package.mkdir(parents=True)
        meta = package.parent / ("hiddifypanel-" + version + ".dist-info/METADATA")
        meta.parent.mkdir()
        meta.write_text("Name: hiddifypanel\nVersion: " + version + "\n\n")
        python = paths.manager / ".venv313/bin/python"
        python.parent.mkdir()
        python.touch()
        (paths.manager / "VERSION").write_text(version)
        cfg = paths.manager / (
            "hiddify-panel/app.cfg"
            if version == "12.0.0"
            else "data/hiddify-panel/app.cfg"
        )
        cfg.parent.mkdir(parents=True)
        cfg.write_text("private-config")
        rel = (
            "panel/admin/AdminstratorAdmin.py"
            if version == "12.0.0"
            else "panel/commercial/restapi/v2/admin/__init__.py"
        )
        target = package / rel
        target.parent.mkdir(parents=True)
        original = ORIGINAL12 if version == "12.0.0" else ORIGINAL14
        target.write_text(original)
        paths.dropins.mkdir(parents=True)
        (paths.dropins / "20-custom.conf").write_text(
            "[Service]\nExecStartPre=/usr/bin/logger before-panel\nLimitNOFILE=12345\n"
        )
        (paths.dropins / "invoice-password-clear.conf").write_text(
            "[Service]\nExecStartPre=-+/usr/local/sbin/invoice-hiddify-password-clear\n"
        )
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        for name in addon.PAYLOAD:
            if name == "VERSION":
                (bundle / name).write_text("1.128.0\n")
            else:
                shutil.copyfile(ROOT / "deploy" / name, bundle / name)
        db = tmp_path / "panel.sqlite3"
        with sqlite3.connect(db) as c:
            c.executescript(
                "CREATE TABLE sentinel(value TEXT);INSERT INTO sentinel VALUES('keep-data');"
            )
        settings = {
            "owner": "owner-private-uuid",
            "owner_id": 1,
            "path": "private-path",
            "backend": "sqlite",
            "database": str(db),
        }
        service = FakeService(paths, python)

        def getter(_settings, suffix):
            if "/admin_user/" in suffix:
                return 200, json.dumps({"uuid": settings["owner"]}).encode()
            if "capabilities" in suffix:
                if service.loaded and not service.fail_verification:
                    return 200, b'{"extension_version":1,"password_set_clear":true}'
                return 404, b"{}"
            return (
                200,
                b"csrf_token new_password"
                if not service.fail_verification
                else b"not ready",
            )

        inst = addon.Installer(
            bundle, paths, service, lambda panel: settings, getter, wait_seconds=0
        )
        inst.native["install"].__globals__["EXPECTED_SHA256"] = hashlib.sha256(
            ORIGINAL14.encode()
        ).hexdigest()
        service.installer = inst
        return inst, paths, service, target, original, db

    return make


@pytest.mark.parametrize("version", ["12.0.0", "14.2.0"])
def test_install_repeat_check_uninstall_preserve_data(host, version):
    inst, paths, service, target, original, db = host(version)
    result = inst.execute()
    assert result["ready"] and service.restarts == 1
    backup = Path(result["backup"])
    with sqlite3.connect(backup / "panel.sqlite3") as c:
        assert c.execute("SELECT value FROM sentinel").fetchone()[0] == "keep-data"
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "panel.sqlite3").stat().st_mode & 0o077 == 0
    assert not (paths.dropins / "invoice-password-clear.conf").exists()
    assert "LimitNOFILE=12345" in (paths.dropins / "20-custom.conf").read_text()
    assert "/usr/bin/logger" in service.value("ExecStartPre")
    assert inst.check()["ready"]
    assert inst.execute()["changed"] is False and service.restarts == 1
    assert inst.execute(uninstall=True)["installed"] is False
    assert target.read_text() == original and not paths.hook.exists()
    assert inst.execute(uninstall=True)["changed"] is False and service.restarts == 2
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM sentinel").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["restart", "verification"])
def test_failed_install_restores_files_and_startup_commands(host, failure):
    inst, paths, service, target, original, _db = host()
    old = (paths.dropins / "invoice-password-clear.conf").read_text()
    if failure == "restart":
        service.fail_restart_once = True
    else:
        service.fail_verification = True
    with pytest.raises(
        addon.AddonError, match="previous files and service were restored"
    ):
        inst.execute()
    assert target.read_text() == original
    assert (paths.dropins / "invoice-password-clear.conf").read_text() == old
    assert not inst.source_status(addon.discover(paths, service))
    assert (
        not paths.wrapper.exists() and not paths.hook.exists() and service.restarts == 2
    )


def test_backup_failure_is_before_panel_mutations(host):
    inst, paths, service, target, original, _db = host()

    def failed(*args):
        raise addon.AddonError("backup failed")

    inst.backup = failed
    with pytest.raises(addon.AddonError, match="backup failed"):
        inst.execute()
    assert target.read_text() == original and service.restarts == 0
    assert (
        not paths.wrapper.exists()
        and (paths.dropins / "invoice-password-clear.conf").exists()
    )


@pytest.mark.parametrize("reason", ["source", "version", "manager"])
def test_unknown_or_mixed_source_is_rejected_before_changes(host, reason):
    inst, paths, service, target, original, _db = host(
        "14.3.0" if reason == "version" else "14.2.0"
    )
    if reason == "source":
        target.write_text("unrecognized source")
    if reason == "manager":
        (paths.manager / "VERSION").write_text("12.0.0")
    before = target.read_bytes()
    with pytest.raises((addon.AddonError, ValueError)):
        inst.execute()
    assert (
        target.read_bytes() == before
        and not paths.backups.exists()
        and not paths.library.exists()
    )
    assert service.restarts == 0


def test_legacy_reset_cannot_silently_revive_unrelated_prestart(host):
    inst, paths, service, _target, _original, _db = host()
    file = paths.dropins / "90-invoice-compat-v14.conf"
    file.write_text(
        "[Service]\nExecStartPre=\nExecStartPre=+/usr/local/sbin/invoice-hiddify-compat-v14\n"
    )
    with pytest.raises(
        addon.AddonError, match="previous files and service were restored"
    ):
        inst.execute()
    assert file.exists() and service.restarts == 1  # rollback only; no bad new restart


def test_boot_selects_new_generation_without_db_or_http(host):
    inst, paths, service, _target, _original, _db = host("12.0.0")
    inst.execute()
    package = addon.discover(paths, service).package
    next(package.parent.glob("hiddifypanel-*/METADATA")).write_text(
        "Name: hiddifypanel\nVersion: 14.2.0\n\n"
    )
    (paths.manager / "VERSION").write_text("14.2.0")
    file = package / "panel/commercial/restapi/v2/admin/__init__.py"
    file.parent.mkdir(parents=True)
    file.write_text(ORIGINAL14)

    def forbidden(*args):
        raise AssertionError("boot must not query DB/HTTP")

    inst.settings_reader = inst.getter = forbidden
    assert inst.apply()["panel_version"] == "14.2.0"
    assert "register_invoice_compat" in file.read_text()
    assert inst.apply()["source_changed"] is False


def test_source_installed_with_old_worker_is_not_ready(host):
    inst, _paths, service, _target, _original, _db = host()
    inst.execute()
    service.start = 0
    assert inst.check()["ready"] is False


def test_settings_reader_reads_sqlite_without_loading_hiddify(tmp_path):
    db = tmp_path / "panel.db"
    with sqlite3.connect(db) as c:
        c.executescript(
            "CREATE TABLE admin_user(id INTEGER,uuid TEXT,mode TEXT);INSERT INTO admin_user VALUES(1,'private-owner','super_admin');CREATE TABLE str_config(`key` TEXT,value TEXT,child_id INTEGER);INSERT INTO str_config VALUES('proxy_path_admin','private-path',0);"
        )
    cfg = tmp_path / "app.cfg"
    cfg.write_text('SQLALCHEMY_DATABASE_URI="sqlite:///' + str(db) + '"\n')
    data = addon.read_settings(
        addon.Panel(tmp_path, Path(sys.executable), "12.0.0", cfg)
    )
    assert (
        data["owner"] == "private-owner"
        and data["path"] == "private-path"
        and data["database"] == str(db)
    )
