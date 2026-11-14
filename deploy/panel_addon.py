#!/usr/bin/env python3
"""Install/check/remove the invoice login extension on an existing Hiddify host."""

from __future__ import annotations

import argparse
import ast
import http.client
import json
import os
import re
import runpy
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

SUPPORTED = {"12.0.0": 12, "14.2.0": 14}
PAYLOAD = (
    "panel_addon.py",
    "hiddify-password-clear.py",
    "hiddify-invoice-compat.py",
    "VERSION",
)
LEGACY = (
    "/usr/local/sbin/invoice-hiddify-password-clear",
    "/usr/local/sbin/invoice-hiddify-compat-v14",
)
MARKER = "# invoice-panel-addon managed"


class AddonError(RuntimeError):
    pass


def run(args, **kwargs):
    try:
        return subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=30, **kwargs
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        # Native DB/HTTP errors can include credentials. Never forward their stderr or URLs.
        raise AddonError(
            "Local panel command failed; no credentials were printed"
        ) from None


def atomic(path: Path, data: str, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".invoice-addon-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(name, mode)
        if path.exists() and os.geteuid() == 0:
            st = path.stat()
            os.chown(name, st.st_uid, st.st_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class Paths:
    root: Path = Path("/")

    def at(self, path):
        return self.root / path.lstrip("/")

    @property
    def manager(self):
        return self.at("/opt/hiddify-manager")

    @property
    def library(self):
        return self.at("/usr/local/lib/invoice-panel-addon")

    @property
    def wrapper(self):
        return self.at("/usr/local/sbin/invoice-panel-addon")

    @property
    def dropins(self):
        return self.at("/etc/systemd/system/hiddify-panel.service.d")

    @property
    def hook(self):
        return self.dropins / "90-invoice-panel-addon.conf"

    @property
    def backups(self):
        return self.at("/var/backups/invoice-panel-addon")

    def hook_text(self):
        return f"[Service]\nExecStartPre=+{self.wrapper} --apply\n"


class Service:
    def value(self, key):
        return run(["systemctl", "show", "hiddify-panel.service", "-p", key, "--value"])

    def active(self):
        return self.value("ActiveState") == "active"

    def reload(self):
        run(["systemctl", "daemon-reload"])

    def restart(self):
        try:
            subprocess.run(
                ["systemctl", "restart", "hiddify-panel.service"],
                check=True,
                capture_output=True,
                timeout=240,
            )
        except (subprocess.SubprocessError, OSError):
            raise AddonError("hiddify-panel restart failed") from None

    def started_at(self):
        return (
            time.time()
            - time.monotonic()
            + int(self.value("ExecMainStartTimestampMonotonic")) / 1_000_000
        )


@dataclass
class Panel:
    package: Path
    python: Path
    version: str
    config: Path

    @property
    def generation(self):
        return SUPPORTED[self.version]

    def source_files(self):
        if self.generation == 12:
            return [self.package / "panel/admin/AdminstratorAdmin.py"]
        return [
            self.package / "panel/commercial/restapi/v2/admin/__init__.py",
            self.package / "invoice_compat.py",
        ]


def discover(paths: Paths, service: Service):
    candidates = []
    for package in paths.manager.glob(".venv*/lib/python*/site-packages/hiddifypanel"):
        versions = []
        for meta in package.parent.glob("*.dist-info/METADATA"):
            headers = meta.read_text(errors="replace").split("\n\n", 1)[0].splitlines()
            values = dict(line.split(": ", 1) for line in headers if ": " in line)
            if values.get("Name", "").lower().replace("-", "_") == "hiddifypanel":
                versions.append(values.get("Version"))
        if len(versions) != 1:
            raise AddonError("Cannot identify the installed Hiddify package version")
        venv = package.parents[3]
        python = venv / "bin/python"
        if not python.is_file():
            raise AddonError("Hiddify virtual environment is incomplete")
        candidates.append((package, python, versions[0]))
    active = service.value("ExecStart")
    selected = [p for p in candidates if str(p[1].parent.parent) + "/" in active]
    if not selected and len(candidates) == 1:
        selected = candidates
    if len(selected) != 1:
        raise AddonError(
            "Cannot uniquely select the active Hiddify virtual environment"
        )
    package, python, version = selected[0]
    if version not in SUPPORTED:
        raise AddonError("Unsupported panel version; supported: 12.0.0, 14.2.0")
    manager_version = paths.manager / "VERSION"
    if manager_version.exists() and manager_version.read_text().strip() != version:
        raise AddonError(
            "Manager and Panel versions disagree; finish the panel upgrade first"
        )
    configs = [
        paths.manager / "data/hiddify-panel/app.cfg",
        paths.manager / "hiddify-panel/app.cfg",
    ]
    if SUPPORTED[version] == 12:
        configs.reverse()
    config = next((p for p in configs if p.is_file()), configs[0])
    return Panel(package, python, version, config)


SETTINGS_READER = r"""
import json,sys
from dotenv import dotenv_values
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.engine import make_url
cfg=dotenv_values(sys.argv[1]);uri=cfg.get("SQLALCHEMY_DATABASE_URI")
if not uri: raise RuntimeError("missing DB setting")
url=make_url(uri)
engine=create_engine(uri,connect_args={"connect_timeout":10} if url.get_backend_name()=="mysql" else {})
with engine.connect() as c:
 if url.get_backend_name()=="mysql": c.exec_driver_sql("SET TRANSACTION READ ONLY")
 columns={col["name"] for col in inspect(c).get_columns("admin_user")}
 extra=" AND deleted=0" if "deleted" in columns else ""
 owner=c.execute(text("SELECT id,uuid FROM admin_user WHERE mode='super_admin'"+extra+" ORDER BY id LIMIT 1")).first()
 path=c.execute(text("SELECT value FROM str_config WHERE `key`='proxy_path_admin' AND child_id=0 LIMIT 1")).scalar_one()
 if not owner or not path: raise RuntimeError("missing panel identity")
 print(json.dumps({"owner":owner[1],"owner_id":owner[0],"path":path,"database":url.database,"backend":url.get_backend_name(),"host":url.host,"port":url.port,"username":url.username,"password":url.password}))
engine.dispose()
"""


def read_settings(panel):
    try:
        return json.loads(
            run([str(panel.python), "-c", SETTINGS_READER, str(panel.config)])
        )
    except (ValueError, KeyError):
        raise AddonError("Could not read the local panel identity") from None


def local_get(settings, suffix):
    conn = http.client.HTTPConnection("127.0.0.1", 9000, timeout=5)
    try:
        # No redirects, public network request or credential in the command line.
        conn.request(
            "GET",
            "/" + quote(settings["path"].strip("/"), safe="") + suffix,
            headers={
                "Hiddify-API-Key": settings["owner"],
                "X-Forwarded-Proto": "https",
            },
        )
        response = conn.getresponse()
        body = response.read(2_000_000)
        return response.status, body
    except (OSError, http.client.HTTPException):
        raise AddonError("Local panel HTTP check failed") from None
    finally:
        conn.close()


def database_backup(settings, target):
    target.touch(mode=0o600, exist_ok=False)
    if settings["backend"] == "sqlite":
        source = Path(settings["database"])
        if not source.is_absolute():
            raise AddonError("Relative SQLite database path is unsupported")
        with sqlite3.connect(
            "file:" + quote(str(source)) + "?mode=ro", uri=True
        ) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
        return
    if settings["backend"] != "mysql":
        raise AddonError("Unsupported panel database backend")
    binary = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not binary or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_$-]*", settings["database"] or ""
    ):
        raise AddonError(
            "A supported SQL dump tool/database is required before installation"
        )
    args = [
        binary,
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--host=" + (settings["host"] or "localhost"),
        "--port=" + str(settings["port"] or 3306),
        "--user=" + (settings["username"] or "root"),
        "--databases",
        settings["database"],
    ]
    try:
        with target.open("wb") as stream:
            subprocess.run(
                args,
                env={**os.environ, "MYSQL_PWD": settings["password"] or ""},
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=180,
                check=True,
            )
    except (OSError, subprocess.SubprocessError):
        raise AddonError(
            "Database backup failed; panel files were not changed"
        ) from None
    with target.open("rb") as stream:
        if target.stat().st_size < 1000 or b"dump" not in stream.read(4096).lower():
            raise AddonError(
                "Database backup is incomplete; panel files were not changed"
            )


class Snapshot:
    def __init__(self, directory, files):
        self.directory = directory
        self.entries = []
        for index, path in enumerate(dict.fromkeys(files)):
            if path.is_symlink():
                entry = (path, "link", os.readlink(path))
            elif path.exists():
                if not path.is_file():
                    raise AddonError("An installation target is not a regular file")
                backup = directory / str(index)
                shutil.copy2(path, backup)
                backup.chmod(0o600)
                st = path.stat()
                entry = (
                    path,
                    "file",
                    (backup, st.st_mode & 0o777, st.st_uid, st.st_gid),
                )
            else:
                entry = (path, "absent", None)
            self.entries.append(entry)
        (directory / "files.json").write_text(
            json.dumps([{"path": str(p), "kind": k} for p, k, _ in self.entries])
        )

    def restore(self):
        for path, kind, value in reversed(self.entries):
            if kind == "absent":
                path.unlink(missing_ok=True)
            elif kind == "link":
                path.unlink(missing_ok=True)
                path.symlink_to(value)
            else:
                backup, mode, uid, gid = value
                # Use bytes: a previous wrapper/config need not be UTF-8.
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".addon-restore")
                shutil.copyfile(backup, tmp)
                tmp.chmod(mode)
                if os.geteuid() == 0:
                    os.chown(tmp, uid, gid)
                os.replace(tmp, path)


def migrate_hooks(paths):
    changes = {}
    for file in paths.dropins.glob("*.conf"):
        if file == paths.hook:
            if file.read_text() != paths.hook_text():
                raise AddonError(
                    "Existing addon drop-in differs; refusing to overwrite it"
                )
            continue
        lines = file.read_text().splitlines(keepends=True)
        owned = [
            line
            for line in lines
            if line.strip().startswith("ExecStartPre=")
            and any(name in line for name in LEGACY)
        ]
        if not owned:
            continue
        for line in owned:
            command = line.strip().split("=", 1)[1]
            if command.lstrip("-+!:@") not in LEGACY:
                raise AddonError("A custom legacy startup command needs manual review")
        others = [
            line
            for line in lines
            if line.strip().startswith("ExecStartPre=")
            and line not in owned
            and line.strip() != "ExecStartPre="
        ]
        if others and any(line.strip() == "ExecStartPre=" for line in lines):
            raise AddonError(
                "Ambiguous startup override; no service files were changed"
            )
        kept = [
            line
            for line in lines
            if line not in owned and line.strip() != "ExecStartPre="
        ]
        if not any(
            line.strip() and not line.lstrip().startswith(("#", ";", "["))
            for line in kept
        ):
            changes[file] = None
        else:
            changes[file] = "".join(kept)
    return changes


class Installer:
    def __init__(
        self,
        bundle,
        paths=None,
        service=None,
        settings_reader=read_settings,
        getter=local_get,
        backup=database_backup,
        wait_seconds=60,
    ):
        self.bundle = Path(bundle).resolve()
        self.paths = paths or Paths()
        self.service = service or Service()
        self.settings_reader, self.getter, self.backup = settings_reader, getter, backup
        self.wait_seconds = wait_seconds
        self.version = (self.bundle / "VERSION").read_text().strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            raise AddonError("Invalid addon release version")
        self.legacy = runpy.run_path(str(self.bundle / "hiddify-password-clear.py"))
        self.native = runpy.run_path(str(self.bundle / "hiddify-invoice-compat.py"))

    def source_status(self, panel):
        if panel.generation == 14:
            return self.native["install"](panel.package, check=True)["installed"]
        source = panel.source_files()[0].read_text()
        return self.legacy["patched_source"](source) == source

    def apply(self, panel=None):
        panel = panel or discover(self.paths, self.service)
        installed = self.source_status(panel)
        if not installed:
            if panel.generation == 14:
                self.native["install"](panel.package)
            else:
                file = panel.source_files()[0]
                atomic(
                    file,
                    self.legacy["patched_source"](file.read_text()),
                    file.stat().st_mode & 0o777,
                )
        return {
            "panel_version": panel.version,
            "installed": True,
            "source_changed": not installed,
        }

    def ready(self, panel, settings):
        if not self.source_status(panel) or not self.service.active():
            return False
        files = [p for p in panel.source_files() if p.exists()]
        if max(p.stat().st_mtime for p in files) > self.service.started_at() + 1:
            return False
        if panel.generation == 14:
            status, body = self.getter(settings, "/api/v2/admin/invoice/capabilities/")
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                return False
            return (
                status == 200
                and data.get("extension_version") == 1
                and data.get("password_set_clear") is True
            )
        status, body = self.getter(
            settings, f"/admin/adminuser/edit/?id={int(settings['owner_id'])}"
        )
        return status == 200 and b"csrf_token" in body and b"new_password" in body

    def check(self):
        panel = discover(self.paths, self.service)
        installed = self.source_status(panel)
        settings = self.settings_reader(panel)
        hook = (
            self.paths.hook.is_file()
            and self.paths.hook.read_text() == self.paths.hook_text()
        )
        return {
            "release": self.version,
            "panel_version": panel.version,
            "installed": installed,
            "startup_hook": hook,
            "ready": hook and self.ready(panel, settings),
        }

    def payload_current(self):
        current = self.paths.library / "current"
        return current.is_dir() and all(
            (current / n).is_file()
            and (current / n).read_bytes() == (self.bundle / n).read_bytes()
            for n in PAYLOAD
        )

    def install_payload(self):
        release = self.paths.library / "releases" / ("v" + self.version)
        if release.exists():
            if not all(
                (release / n).is_file()
                and (release / n).read_bytes() == (self.bundle / n).read_bytes()
                for n in PAYLOAD
            ):
                raise AddonError(
                    "Cached addon release differs; refusing to overwrite it"
                )
        else:
            release.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".addon-", dir=release.parent))
            try:
                for name in PAYLOAD:
                    shutil.copyfile(self.bundle / name, tmp / name)
                    (tmp / name).chmod(0o644)
                tmp.chmod(0o755)
                tmp.rename(release)
            finally:
                if tmp.exists():
                    shutil.rmtree(tmp)
        current = self.paths.library / "current"
        if current.exists() and not current.is_symlink():
            raise AddonError("Addon current selector is not a symlink")
        temp = self.paths.library / (".current-" + str(os.getpid()))
        temp.unlink(missing_ok=True)
        temp.symlink_to(release)
        os.replace(temp, current)
        wrapper = f'#!/bin/sh\n{MARKER}\nexec /usr/bin/python3 {shlex.quote(str(current / "panel_addon.py"))} "$@"\n'
        atomic(self.paths.wrapper, wrapper, 0o755)

    def execute(self, uninstall=False):
        paths, service = self.paths, self.service
        panel = discover(paths, service)
        installed = self.source_status(
            panel
        )  # Validate source BEFORE touching the host.
        if not service.active():
            raise AddonError(
                "Start a healthy Hiddify panel before running this installer"
            )
        changes = migrate_hooks(paths)
        if paths.wrapper.exists() and MARKER not in paths.wrapper.read_text():
            raise AddonError("Existing addon command is not managed by this installer")
        settings = self.settings_reader(panel)
        status, body = self.getter(
            settings, f"/api/v2/admin/admin_user/{settings['owner']}/"
        )
        try:
            identity = json.loads(body)
        except (ValueError, UnicodeError):
            identity = {}
        if status != 200 or identity.get("uuid") != settings["owner"]:
            raise AddonError(
                "The local panel is not healthy/authenticated; installation cancelled"
            )
        if uninstall and not installed and not changes and not paths.hook.exists():
            return {
                "release": self.version,
                "panel_version": panel.version,
                "installed": False,
                "ready": False,
                "changed": False,
            }
        if (
            not uninstall
            and installed
            and not changes
            and self.payload_current()
            and paths.hook.exists()
            and self.ready(panel, settings)
        ):
            return {
                "release": self.version,
                "panel_version": panel.version,
                "ready": True,
                "changed": False,
            }
        paths.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(
            tempfile.mkdtemp(
                prefix=time.strftime("%Y%m%dT%H%M%SZ-", time.gmtime()),
                dir=paths.backups,
            )
        )
        snapshot = Snapshot(
            directory,
            [
                *panel.source_files(),
                panel.config,
                *changes,
                paths.hook,
                paths.wrapper,
                paths.library / "current",
            ],
        )
        self.backup(
            settings,
            directory
            / ("panel.sqlite3" if settings["backend"] == "sqlite" else "panel.sql"),
        )
        original_pre = service.value("ExecStartPre")

        def other_commands(value):
            return [
                p
                for p in re.findall(r"path=([^ ;]+)", value)
                if p not in (*LEGACY, str(paths.wrapper))
            ]

        try:
            if uninstall:
                if panel.generation == 14:
                    self.native["install"](panel.package, uninstall=True)
                elif installed:
                    file = panel.source_files()[0]
                    source = file.read_text().replace(self.legacy["BLOCK"], "", 1)
                    ast.parse(source)
                    atomic(file, source, file.stat().st_mode & 0o777)
                paths.hook.unlink(missing_ok=True)
            else:
                self.install_payload()
                self.apply(panel)
                atomic(paths.hook, paths.hook_text())
            for file, content in changes.items():
                if content is None:
                    file.unlink()
                else:
                    atomic(file, content, file.stat().st_mode & 0o777)
            service.reload()
            effective = service.value("ExecStartPre")
            if other_commands(effective) != other_commands(original_pre):
                raise AddonError("Unrelated service startup commands would change")
            if not uninstall and str(paths.wrapper) not in effective:
                raise AddonError(
                    "Another service override masks the addon startup hook"
                )
            if any(name in effective for name in LEGACY):
                raise AddonError(
                    "A legacy hook outside the managed drop-ins is still active"
                )
            service.restart()
            deadline = time.monotonic() + self.wait_seconds
            while True:
                try:
                    if uninstall:
                        valid = service.active() and not self.source_status(panel)
                        if panel.generation == 14:
                            status, _ = self.getter(
                                settings, "/api/v2/admin/invoice/capabilities/"
                            )
                            valid = valid and status == 404
                    else:
                        valid = self.ready(panel, settings)
                except AddonError:
                    valid = False
                if valid:
                    break
                if time.monotonic() >= deadline:
                    raise AddonError("Loaded addon verification failed after restart")
                time.sleep(1)
        except Exception:
            snapshot.restore()
            try:
                service.reload()
                service.restart()
            except AddonError:
                raise AddonError(
                    f"Installation failed and panel restart after file rollback failed; backup: {directory}"
                ) from None
            raise AddonError(
                f"Installation failed; previous files and service were restored. Backup: {directory}"
            ) from None
        return {
            "release": self.version,
            "panel_version": panel.version,
            "installed": not uninstall,
            "ready": not uninstall,
            "changed": True,
            "backup": str(directory),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check",
        action="store_true",
        help="read-only installed/loaded readiness check",
    )
    modes.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the extension/hook and restart only the panel",
    )
    modes.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root (sudo)")
    bundle = Path(__file__).resolve().parent
    try:
        installer = Installer(bundle)
        result = (
            installer.apply()
            if args.apply
            else installer.check()
            if args.check
            else installer.execute(args.uninstall)
        )
        print(json.dumps(result))
        return 0 if not args.check or result["ready"] else 1
    except Exception as exc:
        # Only our explicit messages are safe to print. No DB/auth/HTTP exception details.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc)
                    if isinstance(exc, AddonError)
                    else "Panel source or configuration is unsupported; no credentials printed",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
