"""A backup must never report success while holding nothing.

`pg_dump` of a COMPLETELY EMPTY database still emits its banner and `SET` preamble — roughly a
kilobyte of perfectly well-formed SQL. The old validator checked only `len >= 64` plus a substring,
so that preamble passed, the archive was built, `mark_backup_done` stamped it, and the owner saw a
green «آخرین پشتیبان». The failure is completely silent right up until the day the backup is needed.

The load-bearing constraint on the fix: `_validate_dump` guards the RESTORE path too, where an older
archive legitimately lacks tables added since. Strictness there would refuse a good backup — so the
strict checks are opt-in and create-only. Both directions are asserted here.
"""
from __future__ import annotations

import asyncio
import io
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/backuphonesty.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402

from app.services import backup  # noqa: E402

# What pg_dump really emits for a database with no tables at all: banner + SETs, nothing else.
EMPTY_DB_DUMP = b"""--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4
-- Dumped by pg_dump version 16.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- PostgreSQL database dump complete
--
"""


def _real_dump(tables: set[str], *, with_settings_rows: bool = True) -> bytes:
    """A structurally realistic dump: every table created, alembic_version + settings populated."""
    out = [b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n"]
    for name in sorted(tables):
        out.append(f"CREATE TABLE public.{name} (id integer);\n".encode())
    out.append(b"COPY public.alembic_version (version_num) FROM stdin;\nd4f7a2b9c1e8\n\\.\n")
    if with_settings_rows:
        out.append(b"COPY public.settings (key, value) FROM stdin;\nfoo\tbar\n\\.\n")
    return b"".join(out)


def test_empty_database_dump_is_refused_on_the_create_side():
    """The actual bug: a preamble-only dump used to sail through and be reported as a backup."""
    backup._validate_dump(EMPTY_DB_DUMP)  # loose check still passes — that is exactly the problem

    with pytest.raises(backup.BackupError) as ei:
        backup._validate_dump(
            EMPTY_DB_DUMP, strict_tables={"panels", "invoices"}, expect_settings=True
        )
    assert "پشتیبان" in str(ei.value)


def test_schema_only_dump_with_no_data_is_refused():
    """Every table present but not a single row — a dump of a freshly-migrated, empty database."""
    schema_only = b"".join(
        [b"--\n-- PostgreSQL database dump\n--\n"]
        + [f"CREATE TABLE public.{t} (id integer);\n".encode() for t in ("panels", "settings")]
    )
    with pytest.raises(backup.BackupError) as ei:
        backup._validate_dump(
            schema_only, strict_tables={"panels", "settings"}, expect_settings=True
        )
    assert "داده" in str(ei.value)


def test_dump_missing_an_application_table_is_refused():
    """Catches a pg_dump aimed at the wrong database that happens to contain unrelated tables."""
    partial = _real_dump({"panels"})
    with pytest.raises(backup.BackupError) as ei:
        backup._validate_dump(partial, strict_tables={"panels", "invoices", "payments"})
    assert "invoices" in str(ei.value) or "payments" in str(ei.value)


def test_a_genuine_dump_passes_strict_validation():
    dump = _real_dump({"panels", "invoices", "payments", "settings"})
    backup._validate_dump(
        dump,
        strict_tables={"panels", "invoices", "payments", "settings"},
        expect_settings=True,
    )


def test_settings_check_is_skipped_when_the_live_settings_table_is_empty():
    """`expect_settings` mirrors what the caller already read from the live DB — a genuinely empty
    settings table must not fail its own backup."""
    dump = _real_dump({"panels", "settings"}, with_settings_rows=False)
    backup._validate_dump(dump, strict_tables={"panels", "settings"}, expect_settings=False)


def test_restore_still_accepts_an_older_backup_missing_newer_tables():
    """ANTI-OVER-TIGHTENING. `_validate_dump` also guards restore, where an old archive legitimately
    predates tables added since. Refusing it would turn a good backup into an unusable one — a worse
    bug than the one being fixed."""
    old_backup = _real_dump({"panels", "invoices"})   # no storefront_* tables at all
    backup._validate_dump(old_backup)                 # restore path: no strict_tables → accepted


def test_truncated_or_garbage_dumps_are_still_refused():
    for bad in (b"", b"oops", b"x" * 80):
        with pytest.raises(backup.BackupError):
            backup._validate_dump(bad)


def test_real_pg_dump_of_the_live_schema_passes(tmp_path):
    """End-to-end shape check against this application's ACTUAL table set, so the strict rule can't
    drift away from the models."""
    from app.core.db import Base

    tables = set(Base.metadata.tables)
    assert "alembic_version" not in tables, "metadata should not contain alembic's bookkeeping table"
    backup._validate_dump(_real_dump(tables), strict_tables=tables, expect_settings=True)


# ── the per-member cap that never fired ───────────────────────────────────────────────────────
def _zip_with(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_per_member_cap_rejects_an_oversize_non_db_member(monkeypatch):
    """The guard compared each member against MAX_TOTAL_UNCOMPRESSED (2 GB) — the *total* budget —
    so despite its error message it could never reject a single member."""
    monkeypatch.setattr(backup, "MAX_MEMBER_UNCOMPRESSED", 1024)
    raw = _zip_with({
        "meta.json": b"{}",
        "settings.json": b"x" * 5000,   # comfortably over the cap, well under 2 GB
        "db.sqlite": backup._SQLITE_MAGIC + b"\x00" * 100,
    })
    with pytest.raises(ValueError) as ei:
        backup._open_validated_zip(raw)
    assert "اجزای پشتیبان" in str(ei.value)


def test_the_database_image_is_exempt_from_the_per_member_cap(monkeypatch):
    """The DB image is legitimately the big one; it stays bounded by the total check instead."""
    import hashlib

    monkeypatch.setattr(backup, "MAX_MEMBER_UNCOMPRESSED", 1024)
    # Incompressible payload — a run of zeros would trip the separate zip-bomb ratio guard and
    # prove nothing about the per-member cap.
    blob = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(2000))
    raw = _zip_with({
        "meta.json": b"{}",
        "settings.json": b"[]",
        "db.sqlite": backup._SQLITE_MAGIC + blob,
    })
    z = backup._open_validated_zip(raw)
    assert "db.sqlite" in z.namelist()
    z.close()


# ── end-to-end: create_backup must actually APPLY the strict rules ────────────────────────────
def test_create_backup_refuses_a_preamble_only_dump_end_to_end(monkeypatch, tmp_path):
    """Guards the WIRING, not just the validator. Testing `_validate_dump` in isolation cannot tell
    whether `create_backup` ever passes `strict_tables` — and an unwired check protects nothing.

    Simulates the real failure: Postgres reachable, pg_dump succeeds, but the database it dumped is
    empty. Previously this produced a cheerful ~1 KB archive stamped as a successful backup.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base

    monkeypatch.setattr(backup, "_sqlite_path", lambda: None)          # take the Postgres branch
    monkeypatch.setattr(backup, "_pg_dump_to_file", lambda dest: dest.write_bytes(EMPTY_DB_DUMP))

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'meta.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)
        async with Session() as s:
            with pytest.raises(backup.BackupError):
                await backup.create_backup(s)
        await engine.dispose()

    asyncio.run(run())


def test_create_backup_accepts_a_dump_that_covers_the_real_schema(monkeypatch, tmp_path):
    """…and the strict rule must not reject a legitimate dump of this application's own schema."""
    import zipfile

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import Setting

    good = _real_dump(set(Base.metadata.tables))
    monkeypatch.setattr(backup, "_sqlite_path", lambda: None)
    monkeypatch.setattr(backup, "_pg_dump_to_file", lambda dest: dest.write_bytes(good))

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'ok.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)
        async with Session() as s:
            s.add(Setting(key="foo", value="bar", is_secret=False))
            await s.commit()
            archive, filename = await backup.create_backup(s)
        await engine.dispose()

        assert filename.startswith("invoice-backup-")
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            assert {"meta.json", "settings.json", "db.sql"} <= set(z.namelist())

    asyncio.run(run())
