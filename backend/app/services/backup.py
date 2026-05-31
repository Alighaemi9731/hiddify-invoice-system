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
        # The encryption key, so a restore on a DIFFERENT server can still decrypt the
        # secret settings (bot token, panel API keys, wallet xpub). Without it those
        # secrets would be unreadable after a cross-server restore. Restore writes this
        # back into .env. (The backup goes only to the owner's private Telegram.)
        "secret_key": boot.secret_key,
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


def _pg_url() -> str:
    # asyncpg DSN → libpq DSN that pg_dump/psql understand.
    return boot.sqlalchemy_url.replace("+asyncpg", "")


def _pg_dump() -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            # --clean --if-exists so the dump drops+recreates objects on restore.
            ["pg_dump", "--no-owner", "--clean", "--if-exists", "--dbname", _pg_url()],
            capture_output=True, text=True, timeout=300,
        )
        if out.returncode != 0:
            log.warning("pg_dump failed: %s", (out.stderr or "")[:300])
            return None
        return out.stdout
    except Exception:  # noqa: BLE001
        log.warning("pg_dump unavailable", exc_info=True)
        return None


_TERMINATE_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid();"
)


def _pg_restore(sql: bytes) -> bool:
    """Import a pg_dump SQL file into the live database via psql. Returns success.

    First terminates other DB connections (the bot/backend pools) so the dump's
    `DROP ... ` statements aren't blocked on locks; those services reconnect after."""
    import subprocess

    try:
        subprocess.run(
            ["psql", "--dbname", _pg_url(), "-c", _TERMINATE_SQL],
            capture_output=True, timeout=60,
        )
        out = subprocess.run(
            # ON_ERROR_STOP=1 so a failed statement aborts and is reported as failure
            # (→ caller keeps the .sql for manual import) instead of a false "ok".
            ["psql", "--dbname", _pg_url(), "-v", "ON_ERROR_STOP=1"],
            input=sql, capture_output=True, timeout=300,
        )
        if out.returncode != 0:
            log.warning("psql restore failed: %s", (out.stderr or b"")[:300])
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        log.warning("psql restore unavailable", exc_info=True)
        return False


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


_ENV_PATHS = [Path("/app/.env"), Path(__file__).resolve().parents[3] / ".env"]


def _persist_secret_key(secret_key: str) -> None:
    """Write SECRET_KEY into .env so a cross-server restore can decrypt secrets after
    the auto-restart. No-op on the original server (same value)."""
    import re

    # Restore reads this from an uploaded backup's meta.json; reject anything that
    # isn't a plain token so a tampered backup can't inject extra .env lines.
    if not secret_key or not re.fullmatch(r"[A-Za-z0-9_\-+/=]{16,128}", secret_key):
        if secret_key:
            log.warning("restore: ignoring malformed secret_key from backup meta")
        return
    for p in _ENV_PATHS:
        try:
            if not p.exists():
                continue
            text = p.read_text()
            if re.search(r"^SECRET_KEY=.*$", text, flags=re.M):
                text = re.sub(r"^SECRET_KEY=.*$", f"SECRET_KEY={secret_key}", text, flags=re.M)
            else:
                text += ("" if text.endswith("\n") else "\n") + f"SECRET_KEY={secret_key}\n"
            p.write_text(text)
        except Exception:  # noqa: BLE001
            log.warning("could not persist SECRET_KEY to %s", p, exc_info=True)


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
        # Restore the original encryption key first, so the restored encrypted
        # settings (bot token, panel API keys, wallet) decrypt after the restart.
        _persist_secret_key(meta.get("secret_key") or "")
        if sqlite and "db.sqlite" in names:
            sqlite.parent.mkdir(parents=True, exist_ok=True)
            # Back up the current DB before overwriting.
            if sqlite.exists():
                shutil.copy(sqlite, sqlite.with_suffix(".sqlite.pre-restore"))
            sqlite.write_bytes(z.read("db.sqlite"))
            return {"status": "ok", "db_kind": "sqlite", "restored": True,
                    "note": "سرویس بک‌اند باید یک‌بار ری‌استارت شود.", "meta": meta}
        if "db.sql" in names:
            sql = z.read("db.sql")
            if _pg_restore(sql):
                return {"status": "ok", "db_kind": "postgres", "restored": True,
                        "note": "بازیابی انجام شد؛ سرویس‌ها به‌صورت خودکار به دادهٔ جدید وصل می‌شوند.",
                        "meta": meta}
            # Fallback: keep the SQL on disk for a manual psql import.
            out = BACKUP_DIR / "restore.sql"
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(sql)
            return {"status": "manual", "db_kind": "postgres", "sql_path": str(out),
                    "note": "بازیابی خودکار ناموفق بود؛ این فایل را با psql وارد کنید.", "meta": meta}
    raise ValueError("محتوای پشتیبان با نوع دیتابیس فعلی سازگار نیست")
