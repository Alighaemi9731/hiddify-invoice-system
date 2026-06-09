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

Safety invariants (B02):
  • A backup is only ever returned/reported as successful when it actually contains a
    usable DB image — a valid SQLite file or a non-empty, validated pg_dump. A failed or
    empty dump raises `BackupError` instead of producing a useless "successful" archive.
  • Restore is atomic: the Postgres import runs in a single transaction, so a mid-restore
    failure rolls back and the live DB is left untouched (a pre-restore safety dump is
    also kept on disk). The restored SECRET_KEY is written to .env ONLY after the DB
    restore succeeds.
  • Uploaded archives are validated (size cap, member allowlist, decompression-bomb
    guard, metadata shape) before anything is read out of them.
  • Optional password-protected export: when `backup_passphrase` is configured the whole
    archive is encrypted (PBKDF2 → Fernet envelope); restore needs the same passphrase.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import settings as boot
from app.models import Setting
from app.services import restart_signal, settings_service

log = logging.getLogger("backup")

BACKUP_DIR = Path("data/backups")

# Upload / decompression guards (the owner is authenticated, but a corrupt or malicious
# archive must not exhaust memory/disk). Backups are normally a few MB.
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024          # reject an uploaded archive bigger than this
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # total decompressed size guard
MAX_COMPRESSION_RATIO = 500                      # per-member ratio guard (zip bomb)
_ALLOWED_MEMBERS = {"meta.json", "settings.json", "db.sqlite", "db.sql"}

_SQLITE_MAGIC = b"SQLite format 3\x00"
# Envelope for an encrypted (passphrase-protected) archive: magic + 16-byte salt + token.
_ENC_MAGIC = b"HINVENC1\n"
_ENC_SALT_LEN = 16
_KDF_ITERATIONS = 200_000


class BackupError(RuntimeError):
    """A backup could not be produced with a usable database image."""


def _sqlite_path() -> Path | None:
    url = boot.sqlalchemy_url
    if not url.startswith("sqlite"):
        return None
    # sqlite+aiosqlite:///./data/app.db  -> ./data/app.db
    tail = url.split("///", 1)[-1]
    return Path(tail)


# ------------------------------- passphrase encryption -------------------------------
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_archive(zip_bytes: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_ENC_SALT_LEN)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(zip_bytes)
    return _ENC_MAGIC + salt + token


def _is_encrypted(data: bytes) -> bool:
    return data[: len(_ENC_MAGIC)] == _ENC_MAGIC


def _decrypt_archive(data: bytes, passphrase: str | None) -> bytes:
    if not _is_encrypted(data):
        return data
    if not passphrase:
        raise ValueError("این پشتیبان رمزگذاری شده است؛ گذرواژهٔ پشتیبان را وارد کنید.")
    salt = data[len(_ENC_MAGIC):len(_ENC_MAGIC) + _ENC_SALT_LEN]
    token = data[len(_ENC_MAGIC) + _ENC_SALT_LEN:]
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("گذرواژهٔ پشتیبان نادرست است.") from exc


# ------------------------------- create -------------------------------
def _validate_dump(sql: bytes) -> None:
    """A pg_dump we are about to ship/restore must look like a real, non-empty dump."""
    if not sql or len(sql) < 64:
        raise BackupError("pg_dump خروجی خالی یا ناقص تولید کرد؛ پشتیبان معتبر ساخته نشد.")
    head = sql[:4096]
    if b"PostgreSQL database dump" not in head and b"CREATE TABLE" not in sql[:200_000]:
        raise BackupError("خروجی pg_dump ساختار معتبری ندارد؛ پشتیبان لغو شد.")


def _validate_sqlite(data: bytes) -> None:
    if not data.startswith(_SQLITE_MAGIC):
        raise BackupError("فایل دیتابیس SQLite معتبر نیست؛ پشتیبان لغو شد.")


async def create_backup(
    session: AsyncSession, *, passphrase: str | None = None
) -> tuple[bytes, str]:
    """Build the backup ZIP in memory. Returns (zip_bytes, filename).

    Raises `BackupError` if a usable DB image cannot be produced (so a caller can never
    report a dump-less archive as a successful backup). When a `backup_passphrase` is
    configured (or passed in) the archive is encrypted."""
    if passphrase is None:
        passphrase = await settings_service.get(session, "backup_passphrase", "") or ""

    settings_rows = (await session.execute(select(Setting))).scalars().all()
    settings_dump = [
        {"key": s.key, "value": s.value, "is_secret": s.is_secret} for s in settings_rows
    ]

    db_kind = "sqlite" if boot.is_sqlite else "postgres"
    meta = {
        "app_version": __version__,
        "db_kind": db_kind,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        # The encryption key, so a restore on a DIFFERENT server can still decrypt the
        # secret settings (bot token, panel API keys, wallet xpub). Without it those
        # secrets would be unreadable after a cross-server restore. Restore writes this
        # back into .env. (The backup goes only to the owner's private Telegram, and may
        # additionally be passphrase-encrypted via the envelope above.)
        "secret_key": boot.secret_key,
        "encrypted": bool(passphrase),
    }

    # Resolve the DB image FIRST and fail loudly if it's unusable — never ship an archive
    # whose only contents are meta + settings.
    sqlite = _sqlite_path()
    db_member: tuple[str, bytes] | None = None
    if sqlite is not None:
        if not sqlite.exists():
            raise BackupError("فایل دیتابیس یافت نشد؛ پشتیبان ساخته نشد.")
        data = sqlite.read_bytes()
        _validate_sqlite(data)
        db_member = ("db.sqlite", data)
    else:
        dump = (await asyncio.to_thread(_pg_dump)).encode("utf-8")
        _validate_dump(dump)
        db_member = ("db.sql", dump)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.writestr("settings.json", json.dumps(settings_dump, ensure_ascii=False, indent=2))
        z.writestr(db_member[0], db_member[1])
    raw = buf.getvalue()
    if passphrase:
        raw = _encrypt_archive(raw, passphrase)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return raw, f"invoice-backup-{stamp}.zip"


def _pg_url() -> str:
    # asyncpg DSN → libpq DSN that pg_dump/psql understand.
    return boot.sqlalchemy_url.replace("+asyncpg", "")


def _pg_dump() -> str:
    """Run pg_dump and return the SQL text. Raises `BackupError` on any failure so the
    caller never builds a dump-less archive."""
    import subprocess

    try:
        out = subprocess.run(
            # --clean --if-exists so the dump drops+recreates objects on restore.
            ["pg_dump", "--no-owner", "--clean", "--if-exists", "--dbname", _pg_url()],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError as exc:
        raise BackupError("ابزار pg_dump روی سرور یافت نشد؛ پشتیبان‌گیری ممکن نیست.") from exc
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"اجرای pg_dump ناموفق بود: {exc}") from exc
    if out.returncode != 0:
        log.warning("pg_dump failed: %s", (out.stderr or "")[:300])
        raise BackupError("pg_dump با خطا متوقف شد؛ پشتیبان ساخته نشد.")
    return out.stdout


_TERMINATE_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid();"
)


# pg_dump 17 writes `SET transaction_timeout = 0;` in its preamble — a GUC that only exists
# from PostgreSQL 17. The backend image bundles postgresql-client 17, but the DB server is 16,
# which rejects that parameter («unrecognized configuration parameter "transaction_timeout"»);
# with ON_ERROR_STOP that aborts the entire restore. It's the ONLY 17-only statement pg_dump
# emits for a 16 schema, so dropping it lets a newer client's dump restore into an older server.
_PG17_ONLY_SET = re.compile(rb"(?im)^[ \t]*SET[ \t]+transaction_timeout\b[^;\n]*;[ \t]*\r?\n?")


def _strip_incompatible_sets(sql: bytes) -> bytes:
    return _PG17_ONLY_SET.sub(b"", sql)


def _save_pre_restore_dump() -> Path | None:
    """Best-effort safety dump of the CURRENT database before a restore overwrites it,
    so the prior state can be recovered manually if needed."""
    try:
        sql = _pg_dump()
    except BackupError:
        log.warning("could not take a pre-restore safety dump", exc_info=True)
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"pre-restore-{stamp}.sql"
    out.write_text(sql, encoding="utf-8")
    # Keep only the latest few safety dumps.
    for old in sorted(BACKUP_DIR.glob("pre-restore-*.sql"))[:-5]:
        old.unlink(missing_ok=True)
    return out


def _pg_restore(sql: bytes) -> bool:
    """Import a pg_dump SQL file into the live database via psql, atomically. Returns success.

    First takes a pre-restore safety dump, then terminates other DB connections (the
    bot/backend pools) so the dump's `DROP ...` statements aren't blocked on locks; those
    services reconnect after. The import runs in a SINGLE transaction (--single-transaction)
    with ON_ERROR_STOP, so any failed statement rolls the whole thing back and the live DB
    is left exactly as it was (never half-dropped)."""
    import subprocess

    sql = _strip_incompatible_sets(sql)
    _save_pre_restore_dump()
    try:
        subprocess.run(
            ["psql", "--dbname", _pg_url(), "-c", _TERMINATE_SQL],
            capture_output=True, timeout=60,
        )
        out = subprocess.run(
            # --single-transaction: the entire dump is one transaction → all-or-nothing.
            # ON_ERROR_STOP=1: a failed statement aborts (→ rollback) and is reported as
            # failure (caller keeps the .sql for manual import) instead of a false "ok".
            ["psql", "--dbname", _pg_url(), "--single-transaction",
             "-v", "ON_ERROR_STOP=1"],
            input=sql, capture_output=True, timeout=300,
        )
        if out.returncode != 0:
            log.warning("psql restore failed (rolled back): %s", (out.stderr or b"")[:300])
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
    the auto-restart. No-op on the original server (same value). Called ONLY after the
    DB restore has succeeded."""
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


# ------------------------------- restore -------------------------------
def _open_validated_zip(zip_bytes: bytes) -> zipfile.ZipFile:
    """Open the archive after enforcing size/member/decompression-bomb limits."""
    if len(zip_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("حجم فایل پشتیبان بیش از حد مجاز است.")
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("فایل پشتیبان معتبر نیست (zip خراب است).") from exc

    total = 0
    for info in z.infolist():
        name = info.filename
        # Reject anything outside the known, flat member set (also blocks zip-slip paths).
        if name not in _ALLOWED_MEMBERS:
            z.close()
            raise ValueError(f"فایل پشتیبان عضو غیرمجاز دارد: {name}")
        total += info.file_size
        if info.file_size > MAX_TOTAL_UNCOMPRESSED:
            z.close()
            raise ValueError("اندازهٔ یکی از اجزای پشتیبان بیش از حد مجاز است.")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            z.close()
            raise ValueError("نسبت فشرده‌سازی غیرعادی است (احتمال فایل مخرب).")
    if total > MAX_TOTAL_UNCOMPRESSED:
        z.close()
        raise ValueError("حجم بازشدهٔ پشتیبان بیش از حد مجاز است.")
    return z


def restore_from_zip(zip_bytes: bytes, *, passphrase: str | None = None) -> dict:
    """Restore the DB from a backup ZIP. Returns a summary.

    The new DB file is written in place (SQLite) or imported atomically (Postgres); the
    app must be restarted to pick up a SQLite swap / a changed SECRET_KEY cleanly — the
    restore writes a restart marker so the peer process restarts too. The restored
    SECRET_KEY is persisted ONLY after the DB restore succeeds, so a failed restore never
    leaves a new key against an unchanged database."""
    zip_bytes = _decrypt_archive(zip_bytes, passphrase)
    sqlite = _sqlite_path()
    with _open_validated_zip(zip_bytes) as z:
        names = z.namelist()
        if "meta.json" not in names:
            raise ValueError("فایل پشتیبان معتبر نیست (meta.json یافت نشد)")
        meta = json.loads(z.read("meta.json"))
        if not isinstance(meta, dict):
            raise ValueError("ساختار meta.json پشتیبان نامعتبر است")

        if sqlite is not None and "db.sqlite" in names:
            data = z.read("db.sqlite")
            _validate_sqlite(data)
            sqlite.parent.mkdir(parents=True, exist_ok=True)
            # Back up the current DB before overwriting (rollback artifact).
            if sqlite.exists():
                shutil.copy(sqlite, sqlite.with_suffix(".sqlite.pre-restore"))
            sqlite.write_bytes(data)
            # Persist the key + signal peers ONLY after the DB swap succeeded.
            _persist_secret_key(meta.get("secret_key") or "")
            restart_signal.request_restart(dt.datetime.now(dt.timezone.utc).isoformat())
            return {"status": "ok", "db_kind": "sqlite", "restored": True,
                    "note": "سرویس بک‌اند باید یک‌بار ری‌استارت شود.", "meta": meta}

        if "db.sql" in names:
            sql = z.read("db.sql")
            try:
                _validate_dump(sql)
            except BackupError as exc:
                raise ValueError(str(exc)) from exc
            if _pg_restore(sql):
                # DB import committed — now it's safe to persist the key + signal peers.
                _persist_secret_key(meta.get("secret_key") or "")
                restart_signal.request_restart(dt.datetime.now(dt.timezone.utc).isoformat())
                return {"status": "ok", "db_kind": "postgres", "restored": True,
                        "note": "بازیابی انجام شد؛ سرویس‌ها به‌صورت خودکار به دادهٔ جدید وصل می‌شوند.",
                        "meta": meta}
            # The single-transaction import rolled back: the live DB is unchanged and the
            # original SECRET_KEY is intact. Keep the SQL on disk for a manual psql import.
            out = BACKUP_DIR / "restore.sql"
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(sql)
            return {"status": "manual", "db_kind": "postgres", "sql_path": str(out),
                    "note": "بازیابی خودکار ناموفق بود (دیتابیس بدون تغییر ماند)؛ این فایل را با psql وارد کنید.",
                    "meta": meta}
    raise ValueError("محتوای پشتیبان با نوع دیتابیس فعلی سازگار نیست")
