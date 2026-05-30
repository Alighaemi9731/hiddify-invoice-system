"""
Full system backup / restore.

A backup is a single ZIP containing:
  • db.sqlite        — the SQLite database file (when DATABASE_URL is sqlite), OR
    db.sql           — a pg_dump (when on Postgres)
  • settings.json    — all rows of the `settings` table (decrypted? NO: as-stored,
                       i.e. secrets stay encrypted with the current SECRET_KEY)
  • meta.json        — version, created_at, db kind

Only the data that matters is persisted by the app itself (panels, resellers,
invoices, payments, settings, logs) — ephemeral chat/support traffic is never stored
in the DB, so the DB stays small and the backup is everything needed to restore.

The backup is delivered to the owner's Telegram PV every N hours (and on demand),
and can be re-uploaded via the panel or sent to the bot to restore the system.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import settings as boot
from app.models import Setting

log = logging.getLogger("backup")

BACKUP_DIR = Path("data/backups")


def _sqlite_path() -> Path | None:
    url = boot.sqlalchemy_url
    if not url.startswith("sqlite"):
        return None
    # sqlite+aiosqlite:///./data/app.db  -> ./data/app.db
    tail = url.split("///", 1)[-1]
    return Path(tail)


async def create_backup(session: AsyncSession) -> tuple[bytes, str]:
    """Build the backup ZIP in memory. Returns (zip_bytes, filename)."""
    buf = io.BytesIO()
    settings_rows = (await session.execute(select(Setting))).scalars().all()
    settings_dump = [
        {"key": s.key, "value": s.value, "is_secret": s.is_secret} for s in settings_rows
    ]

    db_kind = "sqlite" if boot.is_sqlite else "postgres"
    meta = {
        "app_version": __version__,
        "db_kind": db_kind,
        # timestamp is filled by the caller (Date.now is unavailable in some contexts);
        # here we are in normal runtime so it's fine.
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.writestr("settings.json", json.dumps(settings_dump, ensure_ascii=False, indent=2))
        sqlite = _sqlite_path()
        if sqlite and sqlite.exists():
            z.write(sqlite, "db.sqlite")
        else:
            # Postgres: dump via pg_dump if available.
            dump = _pg_dump()
            if dump:
                z.writestr("db.sql", dump)
    buf.seek(0)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return buf.read(), f"invoice-backup-{stamp}.zip"


def _pg_dump() -> str | None:
    import subprocess

    try:
        url = boot.sqlalchemy_url.replace("+asyncpg", "")
        out = subprocess.run(
            ["pg_dump", "--no-owner", "--dbname", url],
            capture_output=True, text=True, timeout=120,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


async def save_backup_to_disk(session: AsyncSession) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    data, name = await create_backup(session)
    path = BACKUP_DIR / name
    path.write_bytes(data)
    # Keep only the latest 10 local copies.
    backups = sorted(BACKUP_DIR.glob("invoice-backup-*.zip"))
    for old in backups[:-10]:
        old.unlink(missing_ok=True)
    return path


def restore_from_zip(zip_bytes: bytes) -> dict:
    """Restore the SQLite DB from a backup ZIP. Returns a summary.

    The new DB file is written in place; the app must be restarted to pick it up
    cleanly (engine holds the old handle). For Postgres restores, returns the SQL
    path for manual `psql` import (auto-restore of pg is intentionally manual)."""
    sqlite = _sqlite_path()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        if "meta.json" not in names:
            raise ValueError("فایل پشتیبان معتبر نیست (meta.json یافت نشد)")
        meta = json.loads(z.read("meta.json"))
        if sqlite and "db.sqlite" in names:
            sqlite.parent.mkdir(parents=True, exist_ok=True)
            # Back up the current DB before overwriting.
            if sqlite.exists():
                shutil.copy(sqlite, sqlite.with_suffix(".sqlite.pre-restore"))
            sqlite.write_bytes(z.read("db.sqlite"))
            return {"status": "ok", "db_kind": "sqlite", "restored": True,
                    "note": "سرویس بک‌اند باید یک‌بار ری‌استارت شود.", "meta": meta}
        if "db.sql" in names:
            out = BACKUP_DIR / "restore.sql"
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read("db.sql"))
            return {"status": "manual", "db_kind": "postgres", "sql_path": str(out),
                    "note": "برای بازیابی Postgres این فایل را با psql وارد کنید.", "meta": meta}
    raise ValueError("محتوای پشتیبان با نوع دیتابیس فعلی سازگار نیست")
