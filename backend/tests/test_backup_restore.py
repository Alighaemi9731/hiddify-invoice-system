"""Backup/restore safety (B02).

Covers: PG17 SET stripping (original), passphrase encryption round-trip, dump/sqlite
validation, archive (zip-bomb / stray-member / size) validation, the invariant that a
restored SECRET_KEY is persisted ONLY after the DB restore succeeds, encrypted-archive
restore needing a passphrase, that a dump-less backup is refused, and the cross-process
restart-signal logic (provably loop-free)."""
import io
import os
import zipfile

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bk.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402

from app.services import backup  # noqa: E402
from app.services.backup import (  # noqa: E402
    BackupError,
    _decrypt_archive,
    _encrypt_archive,
    _open_validated_zip,
    _restore_sql,
    _strip_incompatible_sets,
    _validate_dump,
    _validate_sqlite,
)

_DUMP = b"""--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';

CREATE TABLE public.t (id integer);
"""

_SQLITE = b"SQLite format 3\x00" + b"\x00" * 100


# ---------------------------------------------------------------- PG17 strip
def test_strips_pg17_transaction_timeout():
    out = _strip_incompatible_sets(_DUMP)
    assert b"transaction_timeout" not in out, "PG17-only SET must be removed"
    assert b"SET statement_timeout = 0;" in out
    assert b"SET client_encoding = 'UTF8';" in out
    assert b"CREATE TABLE public.t (id integer);" in out


def test_no_transaction_timeout_is_noop():
    sql = b"SET statement_timeout = 0;\nCREATE TABLE x(i int);\n"
    assert _strip_incompatible_sets(sql) == sql


def test_restore_sql_resets_schema_first():
    # The restore must clean-slate the schema BEFORE the dump, so the dump's --clean
    # drop-ordering can't fail on FK→PK deps (the bug a real restore test surfaced).
    out = _restore_sql(_DUMP)
    assert out.startswith(b"DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n")
    assert b"transaction_timeout" not in out          # still strips PG17-only SETs
    assert b"CREATE TABLE public.t (id integer);" in out  # dump body preserved
    # reset must come before any dump statement
    assert out.index(b"CREATE SCHEMA public;") < out.index(b"CREATE TABLE public.t")


# ---------------------------------------------------------------- validation
def test_validate_dump_accepts_real_dump_and_rejects_garbage():
    _validate_dump(_DUMP)  # ok, no raise
    for bad in (b"", b"oops", b"x" * 80):
        with pytest.raises(BackupError):
            _validate_dump(bad)


def test_validate_sqlite_checks_magic():
    _validate_sqlite(_SQLITE)  # ok
    with pytest.raises(BackupError):
        _validate_sqlite(b"not a sqlite file")


# ---------------------------------------------------------------- encryption
def test_encrypt_decrypt_roundtrip():
    plain = b"PK\x03\x04 pretend zip bytes"
    blob = _encrypt_archive(plain, "hunter2")
    assert blob != plain and backup._is_encrypted(blob)
    assert _decrypt_archive(blob, "hunter2") == plain
    # plaintext archive passes straight through regardless of passphrase
    assert _decrypt_archive(plain, None) == plain


def test_decrypt_requires_correct_passphrase():
    blob = _encrypt_archive(b"secret-archive", "right")
    with pytest.raises(ValueError):
        _decrypt_archive(blob, "wrong")
    with pytest.raises(ValueError):
        _decrypt_archive(blob, None)  # encrypted but no passphrase given


# ---------------------------------------------------------------- archive guards
def _zip_with(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_open_validated_zip_rejects_stray_member():
    blob = _zip_with({"meta.json": b"{}", "evil.sh": b"rm -rf /"})
    with pytest.raises(ValueError):
        _open_validated_zip(blob)


def test_open_validated_zip_rejects_oversize_upload(monkeypatch):
    monkeypatch.setattr(backup, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError):
        _open_validated_zip(_zip_with({"meta.json": b"{}"}))


def test_open_validated_zip_rejects_zip_bomb(monkeypatch):
    monkeypatch.setattr(backup, "MAX_TOTAL_UNCOMPRESSED", 1000)
    # 1 MB of zeros compresses to almost nothing → trips the total/ratio guard.
    blob = _zip_with({"db.sql": b"\x00" * (1024 * 1024)})
    with pytest.raises(ValueError):
        _open_validated_zip(blob)


def test_open_validated_zip_accepts_normal_archive():
    blob = _zip_with({"meta.json": b"{}", "settings.json": b"[]", "db.sqlite": _SQLITE})
    with _open_validated_zip(blob) as z:
        assert set(z.namelist()) == {"meta.json", "settings.json", "db.sqlite"}


# ---------------------------------------------------------------- restore ordering
def _backup_zip(secret_key: str, db_bytes: bytes, member: str = "db.sqlite") -> bytes:
    import json

    return _zip_with({
        "meta.json": json.dumps({"app_version": "t", "db_kind": "sqlite",
                                 "secret_key": secret_key}).encode(),
        "settings.json": b"[]",
        member: db_bytes,
    })


def test_restore_persists_key_only_after_success(monkeypatch, tmp_path):
    sqlite_path = tmp_path / "app.db"
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_KEY=oldkey0000000000\n")
    monkeypatch.setattr(backup, "_sqlite_path", lambda: sqlite_path)
    monkeypatch.setattr(backup, "_ENV_PATHS", [env_path])
    monkeypatch.setattr(backup.restart_signal, "_MARKER", tmp_path / ".restart")
    monkeypatch.setattr(backup.restart_signal, "_inited", False)
    monkeypatch.setattr(backup.restart_signal, "_startup_token", None)

    # 1) A bad DB image fails validation and does NOT touch the live DB or the key.
    with pytest.raises(BackupError):
        backup.restore_from_zip(_backup_zip("newkey1111111111", b"corrupt-not-sqlite"))
    assert "oldkey0000000000" in env_path.read_text(), "key must NOT change on failed restore"
    assert not sqlite_path.exists(), "DB must NOT be written on failed restore"

    # 2) A valid restore writes the DB and then finalizes the key. The DB image here is a
    #    stub sqlite header (no real tables), so re-encryption can't run and the code FALLS
    #    BACK to the legacy .env key-swap — which must still happen only after the DB write.
    res = backup.restore_from_zip(_backup_zip("newkey1111111111", _SQLITE))
    assert res["restored"] is True
    assert sqlite_path.read_bytes() == _SQLITE
    assert "SECRET_KEY=newkey1111111111" in env_path.read_text()
    # ...and it signalled a peer restart.
    assert (tmp_path / ".restart").exists()


def test_cross_server_restore_reencrypts_secrets_to_current_key(monkeypatch, tmp_path):
    """A backup encrypted under a DIFFERENT SECRET_KEY (cross-server migration) is made
    self-sufficient: on restore every Fernet secret is re-encrypted to THIS server's key,
    so it is readable immediately — WITHOUT any .env change or container recreate."""
    import asyncio
    import json

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core import crypto
    from app.core.db import Base
    from app.models import Panel, Setting

    OLD = "oldkeyAAAAAAAAAA"   # the backup's key (meta.json)
    NEW = "newkeyBBBBBBBBBB"   # this server's key

    src = tmp_path / "src.db"

    # Seed a real DB with secrets encrypted under the OLD (backup) key.
    monkeypatch.setattr(backup.boot, "secret_key", OLD)

    async def _seed():
        eng = create_async_engine(f"sqlite+aiosqlite:///{src}")
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as s:
            s.add(Setting(key="telegram_bot_token", value=crypto.encrypt("8873:TOKEN"), is_secret=True))
            s.add(Setting(key="usdt_bep20_address", value="0xPLAINTEXT", is_secret=False))
            p = Panel(key="p1", host="h.example.com", owner_uuid="u-1")
            p.proxy_path = "sSECRETpath"          # setter encrypts under OLD
            s.add(p)
            await s.commit()
        await eng.dispose()

    asyncio.run(_seed())
    db_bytes = src.read_bytes()

    # The running server now uses a DIFFERENT key.
    monkeypatch.setattr(backup.boot, "secret_key", NEW)

    target = tmp_path / "restored.db"
    env_path = tmp_path / ".env"
    env_path.write_text(f"SECRET_KEY={NEW}\n")
    monkeypatch.setattr(backup, "_sqlite_path", lambda: target)
    monkeypatch.setattr(backup, "_ENV_PATHS", [env_path])
    monkeypatch.setattr(backup.restart_signal, "_MARKER", tmp_path / ".restart")
    monkeypatch.setattr(backup.restart_signal, "_inited", False)
    monkeypatch.setattr(backup.restart_signal, "_startup_token", None)

    zip_bytes = _zip_with({
        "meta.json": json.dumps({"app_version": "t", "db_kind": "sqlite", "secret_key": OLD}).encode(),
        "settings.json": b"[]",
        "db.sqlite": db_bytes,
    })
    res = backup.restore_from_zip(zip_bytes)
    assert res["restored"] is True

    # .env key is UNCHANGED — no swap needed, secrets were migrated in-DB instead.
    txt = env_path.read_text()
    assert f"SECRET_KEY={NEW}" in txt
    assert OLD not in txt

    # The restored secrets now decrypt under the CURRENT key; plaintext is untouched.
    async def _verify():
        eng = create_async_engine(f"sqlite+aiosqlite:///{target}")
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as s:
            tok = (await s.execute(
                select(Setting).where(Setting.key == "telegram_bot_token"))).scalar_one()
            assert tok.value.startswith(crypto._PREFIX)
            assert crypto.decrypt(tok.value) == "8873:TOKEN"
            plain = (await s.execute(
                select(Setting).where(Setting.key == "usdt_bep20_address"))).scalar_one()
            assert plain.value == "0xPLAINTEXT"
            p = (await s.execute(select(Panel))).scalar_one()
            assert crypto.decrypt(p.proxy_path_enc) == "sSECRETpath"
        await eng.dispose()

    asyncio.run(_verify())


def test_restore_rejects_encrypted_archive_without_passphrase(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "_sqlite_path", lambda: tmp_path / "app.db")
    enc = _encrypt_archive(_backup_zip("k" * 16, _SQLITE), "the-pass")
    with pytest.raises(ValueError):
        backup.restore_from_zip(enc)  # no passphrase
    # with the right passphrase it restores
    res = backup.restore_from_zip(enc, passphrase="the-pass")
    assert res["restored"] is True


# ---------------------------------------------------------------- create refuses empty
def test_create_backup_refuses_when_db_image_missing(monkeypatch, tmp_path):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base

    monkeypatch.setattr(backup, "_sqlite_path", lambda: tmp_path / "does-not-exist.db")

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'meta.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)
        async with Session() as s:
            with pytest.raises(BackupError):
                await backup.create_backup(s)
        await engine.dispose()

    asyncio.run(run())


# ---------------------------------------------------------------- restart signal
def test_restart_signal_is_loop_free(monkeypatch, tmp_path):
    from app.services import restart_signal

    marker = tmp_path / ".restart-requested"
    monkeypatch.setattr(restart_signal, "_MARKER", marker)
    monkeypatch.setattr(restart_signal, "_inited", False)
    monkeypatch.setattr(restart_signal, "_startup_token", None)

    restart_signal.init()                       # no marker yet
    assert restart_signal.peer_restart_requested() is False
    restart_signal.request_restart("tok-1")     # a peer restore happened
    assert restart_signal.peer_restart_requested() is True
    # After "restarting", the process re-reads the marker as its startup token → stable.
    monkeypatch.setattr(restart_signal, "_inited", False)
    monkeypatch.setattr(restart_signal, "_startup_token", None)
    restart_signal.init()
    assert restart_signal.peer_restart_requested() is False


def test_restart_marker_lives_on_the_shared_volume_not_the_update_bind_mount(monkeypatch):
    """The backend and the bot must watch the SAME file, or a restore only restarts one of them.

    The marker used to key off UPDATE_DIR — which is the panel self-update watcher's HOST bind
    mount and is set only on the backend (`/app/data/update`). The bot, with no UPDATE_DIR, fell
    back to `/app/data`. Two different files: after a restore the backend restarted itself while
    the bot kept serving from the pre-restore database with the old SECRET_KEY. The marker must
    depend only on the `app_data` volume both containers mount at /app/data.
    """
    import importlib

    from app.services import restart_signal

    monkeypatch.setenv("UPDATE_DIR", "/app/data/update")   # as the backend has it in compose
    monkeypatch.delenv("RESTART_SIGNAL_DIR", raising=False)
    reloaded = importlib.reload(restart_signal)
    try:
        assert str(reloaded._MARKER) == "/app/data/.restart-requested", (
            "the restart marker followed UPDATE_DIR again — backend and bot would diverge")
    finally:
        monkeypatch.delenv("UPDATE_DIR", raising=False)
        importlib.reload(restart_signal)
