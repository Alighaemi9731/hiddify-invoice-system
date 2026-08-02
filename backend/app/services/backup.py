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
    also kept on disk). A cross-server restore (backup encrypted under a DIFFERENT
    SECRET_KEY) is made self-sufficient by RE-ENCRYPTING every secret to the running
    server's key right after the import — so restoring via the bot needs no .env edit and
    no container recreate. This runs ONLY after the DB restore succeeds.
  • Uploaded archives are validated (size cap, member allowlist, decompression-bomb
    guard, metadata shape) before anything is read out of them.
  • Optional password-protected export: when `backup_passphrase` is configured the whole
    archive is encrypted (PBKDF2 → Fernet envelope); restore needs the same passphrase.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
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
# Archives are BUILT in a temp dir under BACKUP_DIR (same filesystem → atomic rename) and the
# dir is removed as soon as the archive is delivered. The prefix is what identifies a dir left
# behind by a process that died mid-build, so `_sweep_stale_temp_dirs` can reclaim it.
_TMP_PREFIX = ".building-"
# How many artifacts may sit on the data volume. Each local backup is a compressed zip (single
# -digit MB); each pre-restore dump is UNCOMPRESSED SQL (~10x that). Both are kept deliberately
# small: the newest plus ONE predecessor, so a fresh copy replaces the old one instead of the
# folder growing forever, while a fallback still exists if the newest turns out to be unusable.
_KEEP_LOCAL_BACKUPS = 2
_KEEP_PRE_RESTORE_DUMPS = 2
# Telegram refuses a bot document over 50 MB. Stay clear of the edge (multipart overhead, and
# a backup that only just fits today would start failing silently next month).
TELEGRAM_DOC_LIMIT_BYTES = 45 * 1024 * 1024

# Upload / decompression guards (the owner is authenticated, but a corrupt or malicious
# archive must not exhaust memory/disk). Backups are normally a few MB.
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024          # reject an uploaded archive bigger than this
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # total decompressed size guard
MAX_MEMBER_UNCOMPRESSED = 64 * 1024 * 1024       # per-member cap (the DB image is exempt)
MAX_COMPRESSION_RATIO = 500                      # per-member ratio guard (zip bomb)
_ALLOWED_MEMBERS = {"meta.json", "settings.json", "db.sqlite", "db.sql"}
# The database image is the one member allowed to be arbitrarily large (bounded by the total).
_DB_MEMBERS = {"db.sqlite", "db.sql"}

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
        raise ValueError("این پشتیبان رمزگذاری شده است؛ لطفاً گذرواژهٔ پشتیبان را وارد کنید.")
    salt = data[len(_ENC_MAGIC):len(_ENC_MAGIC) + _ENC_SALT_LEN]
    token = data[len(_ENC_MAGIC) + _ENC_SALT_LEN:]
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("گذرواژهٔ پشتیبان نادرست است.") from exc


# ------------------------------- create -------------------------------
# A plain-format pg_dump names every object with these two prefixes. Scanning for the PREFIX
# and reading the identifier that follows means ONE pass over the dump regardless of how many
# tables we need to prove — the previous per-table `needle in sql` loop re-scanned the whole
# dump ~40 times (O(tables x dump); seconds of CPU on a 100 MB dump, every 2 hours).
_CREATE_PREFIX = b"CREATE TABLE public."
_COPY_PREFIX = b"COPY public."
# Longest prefix + Postgres' 63-byte identifier cap, rounded up: how much of the previous chunk
# must be replayed so a marker straddling a chunk boundary is still seen exactly once.
_SCAN_OVERLAP = 256
_DUMP_CHUNK = 1 << 20


def _collect_markers(chunk: bytes, created: set[str], copied: set[str]) -> None:
    """Record every `CREATE TABLE public.<name>` / `COPY public.<name>` seen in one chunk."""
    for prefix, sink in ((_CREATE_PREFIX, created), (_COPY_PREFIX, copied)):
        pos = chunk.find(prefix)
        while pos != -1:
            start = pos + len(prefix)
            end = start
            while end < len(chunk) and (chunk[end] in _IDENT_BYTES):
                end += 1
            # A name that runs to the very end of the chunk may be truncated; the overlap replay
            # re-reads it in full on the next chunk, so skipping it here loses nothing.
            if end > start and end < len(chunk):
                sink.add(chunk[start:end].decode("ascii", "replace"))
            pos = chunk.find(prefix, max(end, pos + 1))


_IDENT_BYTES = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _scan_dump(chunks) -> tuple[bytes, int, set[str], set[str]]:  # noqa: ANN001
    """One streaming pass over a dump. Returns (head, total_bytes, created, copied)."""
    head = b""
    total = 0
    created: set[str] = set()
    copied: set[str] = set()
    tail = b""
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if len(head) < 200_000:
            head += chunk[: 200_000 - len(head)]
        _collect_markers(tail + chunk, created, copied)
        tail = chunk[-_SCAN_OVERLAP:]
    return head, total, created, copied


def _validate_scan(
    head: bytes,
    total: int,
    created: set[str],
    copied: set[str],
    *,
    strict_tables: set[str] | None,
    expect_settings: bool,
) -> None:
    """Apply the backup-honesty rules to the result of one `_scan_dump` pass. Shared by the
    in-memory (restore) and streaming (create) validators so the two can never diverge."""
    if total < 64:
        raise BackupError("pg_dump خروجی خالی یا ناقص تولید کرد؛ پشتیبان معتبر ساخته نشد.")
    if b"PostgreSQL database dump" not in head[:4096] and b"CREATE TABLE" not in head:
        raise BackupError("خروجی pg_dump ساختار معتبری ندارد؛ پشتیبان لغو شد.")

    if strict_tables:
        missing = sorted(strict_tables - created)
        if missing:
            raise BackupError(
                "پشتیبان ناقص است: جدول‌های زیر در خروجی pg_dump نیستند — "
                f"{'، '.join(missing[:5])}"
                + (f" و {len(missing) - 5} مورد دیگر" if len(missing) > 5 else "")
                + ". پشتیبان لغو شد."
            )
        # alembic_version always has exactly one row, so its COPY block is the single most reliable
        # proof that this dump carries DATA and not just a schema skeleton.
        if "alembic_version" not in copied:
            raise BackupError(
                "پشتیبان بدون داده است (نسخهٔ مهاجرت دیتابیس در خروجی نیست)؛ پشتیبان لغو شد."
            )
        if expect_settings and "settings" not in copied:
            raise BackupError(
                "پشتیبان بدون داده است (تنظیمات در خروجی pg_dump نیست)؛ پشتیبان لغو شد."
            )


def _validate_dump_file(
    path: Path,
    *,
    strict_tables: set[str] | None = None,
    expect_settings: bool = False,
) -> None:
    """Same rules as `_validate_dump`, but streamed from disk so a multi-hundred-MB dump is
    never held in memory. Used by the CREATE path (the dump is written straight to a file)."""

    def _chunks():  # noqa: ANN202
        with path.open("rb") as fh:
            while True:
                block = fh.read(_DUMP_CHUNK)
                if not block:
                    return
                yield block

    _validate_scan(
        *_scan_dump(_chunks()), strict_tables=strict_tables, expect_settings=expect_settings
    )


def _validate_dump(
    sql: bytes,
    *,
    strict_tables: set[str] | None = None,
    expect_settings: bool = False,
) -> None:
    """A pg_dump we are about to ship/restore must look like a real, non-empty dump.

    The structural checks below are deliberately loose because they ALSO guard the restore path,
    where an older backup legitimately lacks tables that have been added since. Strictness on
    restore is wrong in principle: it would refuse a perfectly good archive.

    `strict_tables` turns on the CREATE-side checks. Without them, `pg_dump` of a completely EMPTY
    database still emits its ~1 KB banner and SET preamble — which passed every check here, so the
    archive was built, the backup was stamped successful, and the owner saw a green «آخرین پشتیبان»
    while holding nothing. That failure is silent until the day you actually need the backup.

    Uses only bytes already in hand (no extra I/O): a plain-format dump emits one
    `CREATE TABLE public.<name>` per table and one `COPY public.<name>` block per NON-EMPTY table.
    We do not require COPY for every table — an empty table is perfectly legal — only for
    `alembic_version`, which by construction always holds exactly one row, and for `settings` when
    the caller has already read a non-empty settings table from the live database.

    This is the in-memory form, kept for the RESTORE path (which already holds the uploaded
    archive's bytes). The create path uses `_validate_dump_file`; both share `_validate_scan`,
    so the rules cannot drift apart.
    """
    _validate_scan(
        *_scan_dump([sql]), strict_tables=strict_tables, expect_settings=expect_settings
    )


def _validate_sqlite(data: bytes) -> None:
    if not data.startswith(_SQLITE_MAGIC):
        raise BackupError("فایل دیتابیس SQLite معتبر نیست؛ پشتیبان لغو شد.")


def discard_temp_archive(path: Path) -> None:
    """Delete an archive returned by `create_backup_file` together with its temp directory.
    Safe to call twice, and never raises — cleanup must not mask the caller's own error."""
    try:
        parent = path.parent
        if parent.name.startswith(_TMP_PREFIX) and parent.parent == BACKUP_DIR:
            shutil.rmtree(parent, ignore_errors=True)
        else:  # a caller passed a path we did not create — remove just the file
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.debug("temp archive cleanup failed for %s", path, exc_info=True)


def _sweep_stale_temp_dirs() -> None:
    """Remove temp build dirs left behind by a process killed mid-backup (the OOM case).
    Without this the data volume would accumulate a full uncompressed dump per kill."""
    cutoff = dt.datetime.now().timestamp() - 6 * 3600
    for leftover in BACKUP_DIR.glob(f"{_TMP_PREFIX}*"):
        try:
            if leftover.is_dir() and leftover.stat().st_mtime < cutoff:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:  # noqa: PERF203 - a vanished dir is exactly what we wanted
            continue


async def create_backup_file(
    session: AsyncSession, *, passphrase: str | None = None
) -> tuple[Path, str]:
    """Build the backup archive ON DISK. Returns (path, filename).

    The caller OWNS the returned file and must hand it to `discard_temp_archive` (or move it)
    when finished. Nothing bigger than the COMPRESSED archive ever enters this process's
    memory: pg_dump streams into a file, validation streams that file back in 1 MB blocks, and
    zipfile copies it into the archive in small blocks. Only the optional passphrase envelope
    (Fernet, which has no streaming API) loads the finished archive — which is the ~8 MB
    compressed artifact, not the ~100 MB raw dump.

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
    # Everything below (file read / pg_dump, validation, DEFLATE zip assembly, optional
    # Fernet encryption) is CPU/blocking work on multi-MB payloads. It runs in ONE worker
    # thread so the single-worker event loop keeps serving requests during the (scheduled
    # every-2h and manual) backup — previously this stalled every concurrent request.
    sqlite = _sqlite_path()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"invoice-backup-{stamp}.zip"

    def _assemble() -> Path:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_stale_temp_dirs()
        # The temp dir lives INSIDE BACKUP_DIR (the mounted data volume) on purpose: the
        # container's own filesystem is small, and building here means the finished archive can
        # be moved to its final name with an atomic same-filesystem rename.
        workdir = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=BACKUP_DIR))
        archive = workdir / name
        try:
            if sqlite is not None:
                if not sqlite.exists():
                    raise BackupError("فایل دیتابیس یافت نشد؛ پشتیبان ساخته نشد.")
                # SQLite is the test/dev path only; the file is small and must be validated
                # by magic bytes, so reading it is fine.
                _validate_sqlite(sqlite.read_bytes()[: len(_SQLITE_MAGIC)])
                db_source, arcname = sqlite, "db.sqlite"
            else:
                dump = workdir / "db.sql"
                _pg_dump_to_file(dump)
                # STRICT on the create side only: prove the dump really covers this application's
                # schema and carries data. `settings_rows` was already read from the live DB above,
                # so if it is non-empty the dump must contain those rows too — that is what catches
                # a pg_dump pointed at the wrong (or a freshly-created, empty) database.
                from app.core.db import Base

                _validate_dump_file(
                    dump,
                    strict_tables=set(Base.metadata.tables),
                    expect_settings=bool(settings_rows),
                )
                db_source, arcname = dump, "db.sql"

            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
                z.writestr(
                    "settings.json", json.dumps(settings_dump, ensure_ascii=False, indent=2)
                )
                z.write(db_source, arcname)   # streamed by zipfile; never fully in memory
            if db_source.parent == workdir:
                db_source.unlink(missing_ok=True)  # the dump is inside the zip now — free the disk
            if passphrase:
                # Fernet has no streaming API, so this DOES load the archive. That is the
                # compressed artifact (single-digit MB), not the raw dump.
                archive.write_bytes(_encrypt_archive(archive.read_bytes(), passphrase))
            return archive
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    path = await asyncio.to_thread(_assemble)
    return path, name


async def create_backup(
    session: AsyncSession, *, passphrase: str | None = None
) -> tuple[bytes, str]:
    """Convenience wrapper that returns the archive's BYTES — for callers that genuinely need
    them (the panel's direct download, tests). Bounded by the compressed archive size.

    Prefer `create_backup_file` anywhere the archive is only going to be written or uploaded:
    it never materialises the archive in memory at all."""
    path, name = await create_backup_file(session, passphrase=passphrase)
    try:
        return path.read_bytes(), name
    finally:
        discard_temp_archive(path)


def _pg_url() -> str:
    # asyncpg DSN → libpq DSN that pg_dump/psql understand.
    return boot.sqlalchemy_url.replace("+asyncpg", "")


def _pg_dump_to_file(dest: Path) -> None:
    """Run pg_dump writing its SQL DIRECTLY into `dest`. Raises `BackupError` on any failure so
    the caller never builds a dump-less archive.

    Streaming to a file instead of returning the text is what keeps this process alive. The old
    form used `capture_output=True, text=True`, which meant the whole dump existed as a Python
    `str` — and because reseller/plan names are Persian, CPython widened the ENTIRE string to
    2 bytes per character — and was then `.encode()`d back to bytes for the archive, so peak RSS
    was ~3x the dump. On prod that repeatedly pushed the scheduler past its container memory cap
    and the kernel OOM-killed it mid-job (Jul 29-31 2026, always on an even-hour tick, i.e. this
    every-2h job). Nothing here now holds more than pg_dump's own pipe buffer.
    """
    import subprocess

    try:
        with dest.open("wb") as fh:
            out = subprocess.run(
                # --clean --if-exists so the dump drops+recreates objects on restore.
                ["pg_dump", "--no-owner", "--clean", "--if-exists", "--dbname", _pg_url()],
                stdout=fh, stderr=subprocess.PIPE, timeout=300,
            )
    except FileNotFoundError as exc:
        raise BackupError("ابزار pg_dump روی سرور یافت نشد؛ پشتیبان‌گیری ممکن نیست.") from exc
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"اجرای pg_dump ناموفق بود: {exc}") from exc
    if out.returncode != 0:
        log.warning("pg_dump failed: %s", (out.stderr or b"")[:300])
        raise BackupError("pg_dump با خطا متوقف شد؛ پشتیبان ساخته نشد.")


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


# pg_dump's `--clean` emits per-object DROPs, but their ordering can fail when a foreign key in
# one table depends on another table's primary-key index (e.g. webauthn_credentials → app_users):
# "cannot drop constraint app_users_pkey ... because ... _fkey depends on it". Restoring is always
# onto a DB that already has the schema (the backend migrates on boot), so we reset the schema to a
# clean slate FIRST. It runs inside the same --single-transaction as the import, so a failure still
# rolls everything back (the live DB is never left with an empty schema).
_RESET_SCHEMA = b"DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n"


def _restore_sql(sql: bytes) -> bytes:
    """The exact SQL piped to psql on restore: a clean-slate schema reset, then the dump with
    PG17-only statements stripped. Pure (testable) so the assembly can't silently regress."""
    return _RESET_SCHEMA + _strip_incompatible_sets(sql)


def _save_pre_restore_dump() -> Path | None:
    """Best-effort safety dump of the CURRENT database before a restore overwrites it,
    so the prior state can be recovered manually if needed.

    Streamed straight to disk (never through memory) for the same reason as the backup path.
    These are UNCOMPRESSED SQL — one is roughly ten times the size of a backup zip — so only
    the latest `_KEEP_PRE_RESTORE_DUMPS` are kept; the old limit of 5 could sit on the data
    volume indefinitely after a couple of restores."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"pre-restore-{stamp}.sql"
    try:
        _pg_dump_to_file(out)
    except BackupError:
        out.unlink(missing_ok=True)  # never leave a truncated dump that looks like a safety net
        log.warning("could not take a pre-restore safety dump", exc_info=True)
        return None
    # Prune only AFTER a successful write, so a failed attempt never costs us an older good dump.
    for old in sorted(BACKUP_DIR.glob("pre-restore-*.sql"))[:-_KEEP_PRE_RESTORE_DUMPS]:
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

    # Clean-slate the schema first (atomic, within the same transaction as the import), so the
    # dump's --clean drop-ordering can't fail on cross-table FK→PK dependencies.
    sql = _restore_sql(sql)
    _save_pre_restore_dump()
    try:
        subprocess.run(
            ["psql", "--dbname", _pg_url(), "-c", _TERMINATE_SQL],
            capture_output=True, timeout=60,
        )
        out = subprocess.run(
            # --single-transaction: the entire dump (incl. the schema reset above) is one
            # transaction → all-or-nothing. ON_ERROR_STOP=1: a failed statement aborts (→ rollback)
            # and is reported as failure (caller keeps the .sql for manual import), never a false
            # "ok" or a half-dropped DB.
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


async def mark_backup_done(session: AsyncSession) -> None:
    """Record the time of a SUCCESSFUL backup (read by the health report's «آخرین پشتیبان»).

    The normal auto-backup streams straight to the owner's Telegram and never writes a zip to
    disk, so the health label can't be derived from the disk folder — this timestamp is the
    source of truth. Follows the `toman_per_usdt_auto_at` precedent (ISO, UTC, seconds)."""
    await settings_service.set_value(
        session, "last_backup_at",
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )


async def save_backup_to_disk(session: AsyncSession) -> Path:
    """Keep the backup on the server's data volume. Used when Telegram delivery is impossible
    (no owner chat, no bot) or refuses the file (too large).

    The archive is built in a temp dir and MOVED into place, so a half-written file is never
    visible under the real name. Only `_KEEP_LOCAL_BACKUPS` copies are retained — each new one
    effectively replaces its predecessor rather than the folder growing without bound."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    built, name = await create_backup_file(session)
    path = BACKUP_DIR / name
    try:
        os.replace(built, path)          # atomic: same filesystem by construction
    finally:
        discard_temp_archive(built)      # removes the now-empty temp dir (no-op if moved)
    for old in sorted(BACKUP_DIR.glob("invoice-backup-*.zip"))[:-_KEEP_LOCAL_BACKUPS]:
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


# ------------------------- cross-server key assimilation -------------------------
# A backup carries its own SECRET_KEY in meta.json. On a CROSS-SERVER restore the running
# server has a DIFFERENT key, so every Fernet secret in the restored DB (bot tokens, panel
# credentials, TOTP secrets, encrypted settings) would be undecryptable. Rather than swap
# SECRET_KEY in .env — which only takes effect on a full container RECREATE that a sandboxed
# container cannot trigger, and which the bot-restore path can't even write to the host —
# we RE-ENCRYPT every secret from the backup's key to THIS server's key right after the DB
# import. The running process already holds this server's key, so the data is readable
# immediately; no .env change, no recreate, no host-watcher dependency.


def _fernet_for_key(secret_key: str) -> Fernet:
    """Build the Fernet used for `secret_key` (mirrors app.core.crypto._fernet)."""
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _recrypt_value(value: object, old_fernet: Fernet) -> str | None:
    """Decrypt `value` with the backup key and re-encrypt with the CURRENT key.

    Returns the new ciphertext, or None when there is nothing to migrate: a non-string,
    a plaintext/empty value, or a value that does not decrypt under the backup key (already
    under the current key, or corrupt — left untouched either way)."""
    from app.core import crypto

    if not isinstance(value, str) or not value.startswith(crypto._PREFIX):
        return None
    raw = value[len(crypto._PREFIX):]
    try:
        plaintext = old_fernet.decrypt(raw.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
    return crypto.encrypt(plaintext)  # under the current SECRET_KEY


async def _reencrypt_async(old_key: str, db_url: str) -> dict:
    """Re-encrypt every Fernet secret in the (just-restored) DB at `db_url` to the current key."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import AppUser, Panel, StorefrontBot

    new_key = boot.secret_key
    if not old_key or old_key == new_key:
        return {"migrated": 0, "same_key": True}
    old_fernet = _fernet_for_key(old_key)

    eng = create_async_engine(db_url, pool_pre_ping=True)
    migrated = 0
    try:
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session:
            for st in (await session.execute(select(Setting))).scalars().all():
                nv = _recrypt_value(st.value, old_fernet)
                if nv is not None:
                    st.value = nv
                    migrated += 1
            for p in (await session.execute(select(Panel))).scalars().all():
                for col in ("proxy_path_enc", "client_proxy_path_enc", "admin_api_key_enc"):
                    nv = _recrypt_value(getattr(p, col), old_fernet)
                    if nv is not None:
                        setattr(p, col, nv)
                        migrated += 1
            for b in (await session.execute(select(StorefrontBot))).scalars().all():
                nv = _recrypt_value(b.bot_token_enc, old_fernet)
                if nv is not None:
                    b.bot_token_enc = nv
                    migrated += 1
            for u in (await session.execute(select(AppUser))).scalars().all():
                for col in ("totp_secret_enc", "totp_pending_secret_enc"):
                    nv = _recrypt_value(getattr(u, col), old_fernet)
                    if nv is not None:
                        setattr(u, col, nv)
                        migrated += 1
            await session.commit()
    finally:
        await eng.dispose()
    return {"migrated": migrated, "same_key": False}


def _reencrypt_after_restore(old_key: str, db_url: str) -> dict:
    """Run the async re-encryption in a dedicated thread with its own event loop, so it is
    safe whether `restore_from_zip` is called from a worker thread (bot / API `to_thread`)
    or directly inside a running loop (tests)."""
    box: dict = {}

    def _worker() -> None:
        try:
            box["res"] = asyncio.run(_reencrypt_async(old_key, db_url))
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    t = __import__("threading").Thread(target=_worker, name="restore-reencrypt")
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box["res"]


def _finalize_restore_key(old_key: str, db_url: str) -> str:
    """After a successful DB restore, make the restored secrets readable under THIS server's
    key by re-encrypting them (no SECRET_KEY change). Falls back to the legacy .env key-swap
    only if re-encryption fails. Returns a short Persian status note for the owner."""
    try:
        res = _reencrypt_after_restore(old_key, db_url)
        if res.get("same_key"):
            return "کلید رمز یکسان بود؛ نیازی به تبدیل نبود."
        n = res.get("migrated", 0)
        log.info("restore: re-encrypted %d secret(s) to this server's key", n)
        return f"{n} مقدار رمزنگاری‌شده به کلید این سرور منتقل شد."
    except Exception:  # noqa: BLE001
        log.warning("restore: re-encrypt failed; falling back to .env key swap", exc_info=True)
        _persist_secret_key(old_key)
        return "انتقال کلید ناموفق بود؛ کلید در .env نوشته شد (نیازمند بازسازی سرویس)."


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
        # Per-MEMBER cap. This compared against MAX_TOTAL_UNCOMPRESSED (2 GB) — the *total* budget —
        # so despite its message it never rejected anything a single member could realistically be.
        # The DB image is exempt: it is legitimately the large one, and it is still bounded by the
        # total check below.
        if name not in _DB_MEMBERS and info.file_size > MAX_MEMBER_UNCOMPRESSED:
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
            # Assimilate the backup's secrets to THIS server's key, then signal peers —
            # ONLY after the DB swap succeeded.
            key_note = _finalize_restore_key(
                meta.get("secret_key") or "", f"sqlite+aiosqlite:///{sqlite}"
            )
            restart_signal.request_restart(dt.datetime.now(dt.timezone.utc).isoformat())
            return {"status": "ok", "db_kind": "sqlite", "restored": True,
                    "note": "سرویس بک‌اند باید یک‌بار مجدداً راه‌اندازی شود. " + key_note, "meta": meta}

        if "db.sql" in names:
            sql = z.read("db.sql")
            try:
                _validate_dump(sql)
            except BackupError as exc:
                raise ValueError(str(exc)) from exc
            if _pg_restore(sql):
                # DB import committed — assimilate the backup's secrets to this server's key,
                # then signal peers.
                key_note = _finalize_restore_key(meta.get("secret_key") or "", boot.sqlalchemy_url)
                restart_signal.request_restart(dt.datetime.now(dt.timezone.utc).isoformat())
                return {"status": "ok", "db_kind": "postgres", "restored": True,
                        "note": "بازیابی انجام شد؛ سرویس‌ها به‌صورت خودکار به دادهٔ جدید متصل می‌شوند. "
                                + key_note,
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
