"""Opt-in live adapter acceptance probe. Defaults to read-only preflight.

Connection JSON (mode 0600): host, proxy_path, owner_uuid. Never pass credentials
on the command line. Write mode additionally requires an exact --allow-host.
Only disposable UUIDs minted by this process are mutated; cleanup always runs.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import secrets
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.panel_client import BackupJsonClient
from app.services.panel_client.admin_api import AdminApiClient
from app.services.panel_preflight import inspect_panel


def connection(path: Path):
    if path.stat().st_mode & 0o077:
        raise ValueError("Connection file must have mode 0600")
    raw = json.loads(path.read_text())
    host, secret_path = raw["host"], raw["proxy_path"]
    if not isinstance(host, str) or any(c in host for c in "/@?#"):
        raise ValueError("Expected a host, not a URL")
    if not isinstance(secret_path, str) or any(c in secret_path for c in "/?#"):
        raise ValueError("Expected one proxy path segment")
    owner = str(uuid.UUID(raw["owner_uuid"]))
    base = f"https://{host}/{secret_path}"
    return SimpleNamespace(
        key="compat-probe", host=host, owner_uuid=owner, admin_api_key=owner,
        proxy_base=base, base_secret_url=f"{base}/{owner}",
        backup_url=f"{base}/admin/backup/backupfile/", admin_api_base=f"{base}/api/v2/admin",
        admin_link=lambda uid: f"{base}/{uid}/", maintenance=False,
    )


def business_state(data):
    # Background usage and login timestamps may legitimately advance during observation.
    return {
        "admins": {a.uuid: {k: v for k, v in asdict(a).items()} for a in data.admins},
        "users": {u.uuid: {k: v for k, v in asdict(u).items()
                           if k not in {"current_usage_gb", "last_online", "is_active"}}
                  for u in data.users},
    }


def private_json(path: Path, value) -> None:  # noqa: ANN001
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, default=str)


async def run_probe(panel, *, write: bool = False, journal: Path | None = None) -> dict:
    checks: list[dict[str, Any]] = []
    async def check(name, operation):
        try:
            ok = await operation()
            checks.append({"name": name, "ok": bool(ok)})
            print(json.dumps({"check": name, "ok": bool(ok)}), flush=True)
            if not ok:
                raise RuntimeError("Readback mismatch")
        except Exception as exc:
            if not checks or checks[-1]["name"] != name:
                checks.append({"name": name, "ok": False, "error": type(exc).__name__})
            raise
    report: dict[str, Any] = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": checks}
    try:
        report["preflight"] = await inspect_panel(panel)
        if not write:
            report["ok"] = report["preflight"]["ready"]
            return report
        if not report["preflight"]["ready"]:
            raise RuntimeError("Preflight incomplete")
        client = AdminApiClient()
        before = business_state(await BackupJsonClient().fetch_backup(panel))
        admin_ids = [str(uuid.uuid4()) for _ in range(3)]
        user_ids = [str(uuid.uuid4()) for _ in range(4)]
        label = "invoice-compat-" + secrets.token_hex(4)
        journal_state = {"label": label, "admin_ids": admin_ids, "user_ids": user_ids, "before": before}
        if journal is not None:
            private_json(journal, journal_state)
        try:
            for index, aid in enumerate(admin_ids):
                parent = admin_ids[0] if index == 2 else panel.owner_uuid
                await client.create_admin(panel, name=f"{label}-admin-{index}", api_key=panel.owner_uuid,
                                          parent_admin_uuid=parent, admin_uuid=aid)
                async def parent_ok(aid=aid, parent=parent):
                    row = await client.get_admin(panel, aid, api_key=panel.owner_uuid)
                    return row is not None and row.get("parent_admin_uuid") == parent
                await check(f"admin_parent_{index}", parent_ok)
            for index, uid in enumerate(user_ids):
                owner = admin_ids[[0, 0, 1, 2][index]]
                await client.create_user(panel, name=f"{label}-user-{index}", gb=2, days=7,
                                         api_key=owner, user_uuid=uid)
                async def owner_ok(uid=uid, owner=owner):
                    row = await client.get_user(panel, uid, api_key=owner)
                    return row is not None and row.get("added_by_uuid") == owner
                await check(f"user_owner_{index}", owner_ok)
            ids = [await client.get_user_id(panel, uid, api_key=admin_ids[0]) for uid in user_ids[:2]]
            if any(i is None for i in ids):
                raise RuntimeError("Fixture identity missing")
            for enabled in [False, True]:
                await client.bulk_set_users_enabled(panel, [int(i) for i in ids if i is not None],
                                                    enabled, api_key=admin_ids[0])
                async def bulk_ok(enabled=enabled):
                    own = await client.get_user(panel, user_ids[0], api_key=admin_ids[0])
                    foreign = await client.get_user(panel, user_ids[2], api_key=admin_ids[1])
                    return own is not None and own.get("enable") is enabled and foreign is not None and foreign.get("enable") is True
                await check(f"bulk_{enabled}_and_foreign_untouched", bulk_ok)
            await client.renew_user(panel, user_ids[0], gb=4, days=12, api_key=admin_ids[0])
            async def renewed():
                row = await client.get_user(panel, user_ids[0], api_key=admin_ids[0])
                return row is not None and row.get("usage_limit_GB") == 4 and row.get("current_usage_GB") == 0 and row.get("start_date") is None and row.get("package_days") == 12
            await check("renewal_readback", renewed)
            for enabled in [False, True]:
                await client.set_user_enabled(panel, user_ids[0], enabled, api_key=admin_ids[0])
            for limit in [0, 100]:
                await client.set_admin_limits(panel, admin_ids[0], limit, limit, api_key=panel.owner_uuid)
                async def limits_ok(limit=limit):
                    return await client.get_admin_limits(panel, admin_ids[0], api_key=panel.owner_uuid) == (limit, limit)
                await check(f"capacity_{limit}", limits_ok)
            for allowed in [True, False]:
                await client.set_can_add_admin(panel, admin_ids[0], allowed, api_key=panel.owner_uuid)
                async def allowed_ok(allowed=allowed):
                    row = await client.get_admin(panel, admin_ids[0], api_key=panel.owner_uuid)
                    return row is not None and row.get("can_add_admin") is allowed
                await check(f"can_add_admin_{allowed}", allowed_ok)
            for password in ["Lock-" + secrets.token_urlsafe(16), ""]:
                await client.set_admin_password(panel, admin_ids[0], password, api_key=panel.owner_uuid)
                async def login_ok(password=password):
                    return await client.admin_link_login_works(panel, admin_ids[0]) == (not password)
                await check("uuid_login_locked" if password else "uuid_login_restored", login_ok)
            async def traffic():
                history = await client.get_server_status(panel, api_key=admin_ids[0])
                return isinstance(history, dict) and all(k in history for k in ["yesterday", "last_30_days", "total"])
            await check("traffic_history", traffic)
            await client.delete_user(panel, user_ids[1], api_key=admin_ids[0])
            async def deleted():
                return await client.get_user(panel, user_ids[1], api_key=panel.owner_uuid) is None
            await check("delete_user", deleted)
        finally:
            # Even a timeout may have applied. Cleanup every minted UUID, not just acknowledged creates.
            for uid in user_ids:
                try:
                    row = await client.get_user(panel, uid, api_key=panel.owner_uuid)
                    if row:
                        await client.delete_user(panel, uid, api_key=row["added_by_uuid"])
                except Exception as exc:
                    checks.append({"name": "cleanup_user", "ok": False, "error": type(exc).__name__})
            for aid in reversed(admin_ids):
                try:
                    await client.delete_admin(panel, aid, api_key=panel.owner_uuid)
                except Exception as exc:
                    checks.append({"name": "cleanup_admin", "ok": False, "error": type(exc).__name__})
            after_data = await BackupJsonClient().fetch_backup(panel)
            active = not any(a.uuid in admin_ids for a in after_data.admins) and not any(
                u.uuid in user_ids and not u.deleted for u in after_data.users)
            checks.append({"name": "no_active_fixtures", "ok": active})
            after = business_state(after_data)
            unchanged = all(after[k].get(uid) == row for k in before for uid, row in before[k].items())
            checks.append({"name": "preexisting_business_fields_unchanged", "ok": unchanged})
            if journal is not None:
                journal_state.update(after=after, cleanup_complete=active, checks=checks)
                private_json(journal, journal_state)
        report["ok"] = all(c["ok"] for c in checks)
    except Exception as exc:
        report["ok"] = False
        report["error"] = type(exc).__name__
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--allow-host")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    panel = connection(args.connection_file)
    if args.write and args.allow_host != panel.host:
        parser.error("Write mode requires --allow-host matching the connection host exactly")
    journal = args.output.with_suffix(".private.json") if args.output and args.write else None
    if args.write and journal is None:
        parser.error("Write mode requires --output for a durable private recovery journal")
    report = asyncio.run(run_probe(panel, write=args.write, journal=journal))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(encoded)
    print(encoded, end="")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
