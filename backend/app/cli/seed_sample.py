"""
Seed the DB from a Hiddify backup JSON file (the owner's real sample, or any backup).

Usage:
    python -m app.cli.seed_sample [PATH_TO_BACKUP.json] [--key sample] [--reset]

If PATH is omitted, looks for a *.json under "../sample backup panel/" (gitignored).
The created panel is marked source=sample and is NOT contacted over the network
(data is loaded straight from the file), so it works fully offline for demos.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Panel
from app.models.enums import PanelStatus, SyncSource
from app.services import sync as sync_service
from app.services.bootstrap import run_bootstrap
from app.services.panel_client import parse_backup

DEFAULT_GLOBS = [
    os.path.join(os.getcwd(), "..", "sample backup panel", "*.json"),
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "sample backup panel", "*.json"),
]


def _find_default() -> str | None:
    for pattern in DEFAULT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def _pick_owner_uuid(payload: dict) -> str:
    for a in payload.get("admin_users", []):
        if a.get("mode") == "super_admin" or (a.get("name") or "").strip().lower() == "owner":
            return a.get("uuid", "")
    admins = payload.get("admin_users", [])
    return admins[0].get("uuid", "sample-owner") if admins else "sample-owner"


async def _run(path: str, key: str, reset: bool) -> None:
    await run_bootstrap()
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    data = parse_backup(payload)

    async with SessionLocal() as session:
        panel = (
            await session.execute(select(Panel).where(Panel.key == key))
        ).scalar_one_or_none()
        if panel and reset:
            await session.delete(panel)
            await session.commit()
            panel = None
        if panel is None:
            panel = Panel(
                key=key,
                name=f"Sample ({os.path.basename(path)[:40]})",
                host="sample.local",
                owner_uuid=_pick_owner_uuid(payload),
                enabled=True,
                source=SyncSource.sample,
                status=PanelStatus.ok,
            )
            panel.proxy_path = "sample-offline"
            session.add(panel)
            await session.commit()
            await session.refresh(panel)

        run = await sync_service.sync_panel(
            session, panel, data=data, source=SyncSource.sample
        )
        print(
            f"Seeded panel '{key}' from {path}\n"
            f"  status={run.status.value} admins={run.admin_count} users={run.user_count}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed DB from a Hiddify backup JSON.")
    parser.add_argument("path", nargs="?", default=None)
    parser.add_argument("--key", default="sample")
    parser.add_argument("--reset", action="store_true", help="delete + re-create the panel")
    args = parser.parse_args()

    path = args.path or _find_default()
    if not path or not os.path.exists(path):
        print(
            "No backup file found. Pass a path:\n"
            "  python -m app.cli.seed_sample /path/to/backup.json",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(_run(path, args.key, args.reset))


if __name__ == "__main__":
    main()
