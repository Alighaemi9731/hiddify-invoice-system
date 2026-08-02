"""The backup must never hold the database dump in memory.

Why this file exists: `deploy-scheduler-1` was OOM-killed repeatedly on production (Jul 29-31
2026, always ~520 MB against its 512 MB cap, always on an even-hour tick — the every-2h backup).
The cause was not a leak. `_pg_dump` ran with `capture_output=True, text=True`, so the whole dump
became a Python `str`; because reseller and plan names are Persian, CPython widened the ENTIRE
string to 2 bytes per character; it was then `.encode()`d back to bytes for the archive, and the
zip was assembled in a `BytesIO` and copied out with `getvalue()`. Peak was ~3x the dump for a
job whose visible output is an ~8 MB file.

The fix is structural (pg_dump streams to a file, validation streams it back, zipfile copies it
in blocks), so the regression guard has to be structural too: assert on ALLOCATION, not on the
shape of the code. `tracemalloc` counts Python-level allocations, which is exactly the class of
allocation that caused the kills — zlib's own buffers are C-level and correctly not counted.
"""
import asyncio
import os
import tempfile
import tracemalloc
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bmem.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import Setting  # noqa: E402
from app.services import backup  # noqa: E402

# Big enough that any "whole dump in memory" implementation blows the budget by an order of
# magnitude, small enough to stay a fast unit test.
_DUMP_MB = 40
_BUDGET_MB = 6


def _fake_dump(tables: set[str]) -> bytes:
    """A dump that satisfies every honesty rule AND compresses like the real thing (Persian
    names, repetitive COPY rows) so the test exercises the real zip path."""
    head = b"-- PostgreSQL database dump\n"
    body = bytearray(head)
    for name in sorted(tables):
        body += f"CREATE TABLE public.{name} (id integer);\n".encode()
        body += f"COPY public.{name} (id) FROM stdin;\n".encode()
        body += "1\t\u0646\u0645\u0627\u06cc\u0646\u062f\u0647\u0654 \u0622\u0632\u0645\u0627\u06cc\u0634\u06cc\n".encode()
        body += b"\\.\n"
    filler = "-- \u0631\u062f\u06cc\u0641 \u062f\u0627\u062f\u0647\u0654 \u0646\u0645\u0648\u0646\u0647 padding row\n".encode()
    while len(body) < _DUMP_MB * 1024 * 1024:
        body += filler
    return bytes(body)


def test_create_backup_file_never_materialises_the_dump(tmp_path, monkeypatch):
    async def body():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mem.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)

        # `alembic_version` is Alembic's own table, not part of Base.metadata — but a real dump
        # always carries it and the validator requires its COPY block as proof of data.
        dump = _fake_dump(set(Base.metadata.tables) | {"alembic_version"})
        monkeypatch.setattr(backup, "_sqlite_path", lambda: None)          # Postgres branch
        monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(backup, "_pg_dump_to_file", lambda dest: dest.write_bytes(dump))

        async with Session() as s:
            s.add(Setting(key="foo", value="bar", is_secret=False))
            await s.commit()

            tracemalloc.start()
            path, name = await backup.create_backup_file(s)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        try:
            assert path.exists() and name.startswith("invoice-backup-")
            # The archive is real: it holds the dump, and it is far smaller than the dump.
            import zipfile

            with zipfile.ZipFile(path) as z:
                assert {"meta.json", "settings.json", "db.sql"} <= set(z.namelist())
                assert z.getinfo("db.sql").file_size == len(dump)
            assert path.stat().st_size < len(dump) // 4

            peak_mb = peak / (1024 * 1024)
            assert peak_mb < _BUDGET_MB, (
                f"backup peaked at {peak_mb:.1f} MB for a {_DUMP_MB} MB dump — the dump is being "
                "held in memory again (this is what OOM-killed the scheduler on production)"
            )
        finally:
            backup.discard_temp_archive(path)
        await engine.dispose()

    asyncio.run(body())


def test_temp_build_dir_is_removed_when_backup_dir_is_relative(tmp_path, monkeypatch):
    """PRODUCTION SHAPE. `BACKUP_DIR` is the RELATIVE `data/backups` on a real install, but
    `tempfile.mkdtemp()` absolutises its `dir` argument — so the cleanup's
    `parent.parent == BACKUP_DIR` guard never matched and every backup left an empty build
    directory behind (found on the production smoke test of v1.105.0, fixed in v1.105.1).

    Every other test here monkeypatches BACKUP_DIR to an absolute `tmp_path`, which compares
    equal and hides the bug — so this one deliberately runs with a relative path.
    """
    monkeypatch.chdir(tmp_path)
    rel = Path("data/backups")
    monkeypatch.setattr(backup, "BACKUP_DIR", rel)
    rel.mkdir(parents=True)

    workdir = Path(tempfile.mkdtemp(prefix=backup._TMP_PREFIX, dir=rel))
    archive = workdir / "invoice-backup-x.zip"
    archive.write_bytes(b"zip")

    backup.discard_temp_archive(archive)

    assert not archive.exists(), "the archive itself was not removed"
    assert not workdir.exists(), (
        "the temp build directory survived — with a relative BACKUP_DIR the cleanup guard fails "
        "and every backup leaks a directory into the data volume"
    )
    assert not list(rel.glob(f"{backup._TMP_PREFIX}*"))


def test_temp_build_dir_is_removed_even_when_the_dump_fails(tmp_path, monkeypatch):
    """A killed or failed build must not leave a full uncompressed dump on the data volume —
    that is how a repeatedly OOM-killed job silently fills the disk."""
    async def body():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)

        backups = tmp_path / "backups"
        monkeypatch.setattr(backup, "_sqlite_path", lambda: None)
        monkeypatch.setattr(backup, "BACKUP_DIR", backups)

        def _boom(dest):
            dest.write_bytes(b"-- PostgreSQL database dump\n" + b"x" * 1024)  # partial junk
            raise backup.BackupError("pg_dump failed")

        monkeypatch.setattr(backup, "_pg_dump_to_file", _boom)

        async with Session() as s:
            try:
                await backup.create_backup_file(s)
            except backup.BackupError:
                pass
            else:
                raise AssertionError("a failed dump must not produce an archive")

        assert not list(backups.glob(f"{backup._TMP_PREFIX}*")), "temp build dir was left behind"
        await engine.dispose()

    asyncio.run(body())
