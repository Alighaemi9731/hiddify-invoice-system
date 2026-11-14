"""Read-only compatibility report; explicit maintenance switches require an operator."""
from __future__ import annotations

import argparse
import asyncio
import json

import httpx

from app.core.db import SessionLocal
from app.models import Panel
from app.models.enums import SyncStatus
from app.services.panel_client import BackupJsonClient
from app.services.panel_maintenance import set_maintenance


async def inspect_panel(panel) -> dict:  # noqa: ANN001
    data = await BackupJsonClient().fetch_backup(panel)
    headers = {"Hiddify-API-Key": panel.owner_uuid}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        legacy = await client.get(f"{panel.proxy_base}/admin/user/", headers=headers)
        if legacy.status_code == 200 and "csrf_token" in legacy.text:
            return {"generation": 12, "backup": True, "bulk": True,
                    "password": "legacy_hook_required", "ready": True,
                    "admins": len(data.admins), "users": sum(not u.deleted for u in data.users)}
        if legacy.status_code != 404:
            raise RuntimeError("Legacy capability probe failed; not a version downgrade")
        native = await client.get(f"{panel.admin_api_base}/users/", headers=headers)
        bulk = native.status_code == 200 and isinstance(native.json().get("users"), list)
        extension = await client.get(f"{panel.admin_api_base}/invoice/capabilities/", headers=headers)
        password = (extension.status_code == 200 and extension.json().get("extension_version") == 1
                    and extension.json().get("password_set_clear") is True)
        return {"generation": 14, "backup": True, "bulk": bulk,
                "password": password, "ready": bulk and password,
                "admins": len(data.admins), "users": sum(not u.deleted for u in data.users),
                "deleted_users": sum(u.deleted for u in data.users)}


async def change_maintenance(session, panel: Panel, enabled: bool) -> dict:  # noqa: ANN001
    if enabled:
        await set_maintenance(session, panel, True)
        return {"maintenance": True}
    # Keep the flag on while validating and refreshing local accounting data.
    report = await inspect_panel(panel)
    if not report["ready"]:
        raise RuntimeError("Panel compatibility preflight is incomplete; maintenance remains enabled")
    from app.services.sync import sync_panel

    run = await sync_panel(session, panel, allow_maintenance=True)
    if run.status != SyncStatus.success:
        raise RuntimeError("Validation sync failed; maintenance remains enabled")
    await set_maintenance(session, panel, False)
    return {"maintenance": False, "preflight": report}


async def _main(args) -> int:  # noqa: ANN001
    async with SessionLocal() as session:
        panel = await session.get(Panel, args.panel_id)
        if panel is None:
            raise RuntimeError("Panel not found")
        if args.maintenance:
            report = await change_maintenance(session, panel, args.maintenance == "on")
        else:
            report = await inspect_panel(panel)
        print(json.dumps(report))
        return 0 if report.get("ready", True) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-id", type=int, required=True)
    parser.add_argument("--maintenance", choices=["on", "off"])
    args = parser.parse_args()
    try:
        code = asyncio.run(_main(args))
    except Exception as exc:
        # URLs and UUIDs are credentials; do not print arbitrary network exception strings.
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
