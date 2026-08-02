"""The health report's «آخرین پشتیبان» reads a stamped last_backup_at (set on every successful
backup), not the disk folder — because the auto-backup streams to Telegram and writes no zip."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bstamp.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.services import backup_delivery, owner_report, settings_service  # noqa: E402
from app.services.owner_report import _format_backup_stamp  # noqa: E402


def _factory(tmp_path):
    async def make():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)
    return make


class _Sess:
    async def close(self):
        return None


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.session = _Sess()

    async def send_document(self, *a, **k):
        if self.fail:
            raise RuntimeError("telegram down")


def test_format_backup_stamp_tehran():
    assert _format_backup_stamp("") is None
    assert _format_backup_stamp("not-a-date") is None
    # Tehran is a fixed UTC+3:30 (no DST). A naive stamp is treated as UTC.
    assert _format_backup_stamp("2026-06-17T00:00:00+00:00") == "2026-06-17 03:30"
    assert _format_backup_stamp("2026-06-17T00:00:00") == "2026-06-17 03:30"


def _stub_built_archive(monkeypatch, tmp_path, *, size: int = 3):
    """Stand in for `create_backup_file`, which now hands the delivery path a real FILE (the
    archive is built on disk and uploaded from there — it is never a bytes blob any more)."""
    from pathlib import Path

    def _make(_s):
        workdir = Path(tmp_path) / "build"
        workdir.mkdir(exist_ok=True)
        path = workdir / "backup.zip"
        path.write_bytes(b"z" * size)
        return _coro((path, "backup.zip"))

    monkeypatch.setattr(backup_delivery.backup, "create_backup_file", _make)


def test_successful_send_stamps_last_backup(tmp_path, monkeypatch):
    async def body(monkeypatch):
        engine, Session = await _factory(tmp_path)()
        try:
            async with Session() as s:
                await settings_service.set_value(s, "owner_chat_id", "123")
                _stub_built_archive(monkeypatch, tmp_path)
                monkeypatch.setattr(backup_delivery, "build_bot", lambda _s: _coro(_Bot()))
                res = await backup_delivery.send_backup_to_owner(s)
                assert res["status"] == "sent"
                stamp = await settings_service.get(s, "last_backup_at", "")
                assert stamp  # a real ISO timestamp was recorded
                assert _format_backup_stamp(str(stamp)) is not None
        finally:
            await engine.dispose()
    asyncio.run(body(monkeypatch))


def test_failed_send_does_not_stamp(tmp_path, monkeypatch):
    async def body(monkeypatch):
        engine, Session = await _factory(tmp_path)()
        try:
            async with Session() as s:
                await settings_service.set_value(s, "owner_chat_id", "123")
                _stub_built_archive(monkeypatch, tmp_path)
                monkeypatch.setattr(backup_delivery, "build_bot", lambda _s: _coro(_Bot(fail=True)))
                with pytest.raises(RuntimeError):
                    await backup_delivery.send_backup_to_owner(s)
                assert not (await settings_service.get(s, "last_backup_at", ""))  # never stamped
        finally:
            await engine.dispose()
    asyncio.run(body(monkeypatch))


def test_oversize_backup_is_kept_on_disk_and_not_stamped(tmp_path, monkeypatch):
    """Telegram refuses a bot document over 50 MB. The send used to just raise, and the only copy
    of the backup went out with the temp dir — automated off-site backup would stop dead the month
    the archive crossed the line. Now it is kept on the server, the owner is told, and the stamp is
    deliberately NOT set: a copy living on the machine it protects is not off-site protection, so
    «آخرین پشتیبان» must go stale and surface in the health panel."""
    from pathlib import Path

    async def body(monkeypatch):
        engine, Session = await _factory(tmp_path)()
        try:
            async with Session() as s:
                await settings_service.set_value(s, "owner_chat_id", "123")
                monkeypatch.setattr(backup_delivery.backup, "BACKUP_DIR", Path(tmp_path) / "kept")
                monkeypatch.setattr(backup_delivery.backup, "TELEGRAM_DOC_LIMIT_BYTES", 10)
                _stub_built_archive(monkeypatch, tmp_path, size=64)  # over the patched limit
                monkeypatch.setattr(backup_delivery, "build_bot", lambda _s: _coro(_Bot()))

                notes: list[str] = []
                from app.services import owner_notify

                monkeypatch.setattr(
                    owner_notify, "notify_owner",
                    lambda _s, text, **k: _coro(notes.append(text) or True))

                res = await backup_delivery.send_backup_to_owner(s)

                assert res["status"] == "too_large"
                assert Path(res["saved"]).exists()          # the backup survived
                assert notes and "تلگرام" in notes[0]        # the owner was told why
                assert not (await settings_service.get(s, "last_backup_at", ""))
        finally:
            await engine.dispose()
    asyncio.run(body(monkeypatch))


def test_local_backup_folder_does_not_grow_without_bound(tmp_path, monkeypatch):
    """Each new local copy must REPLACE the oldest rather than pile up on the data volume."""
    from pathlib import Path

    from app.services import backup as backup_service

    kept = Path(tmp_path) / "kept"
    kept.mkdir()
    monkeypatch.setattr(backup_service, "BACKUP_DIR", kept)
    for i in range(5):
        src = Path(tmp_path) / f"src{i}.zip"
        src.write_bytes(b"z")
        backup_delivery._keep_on_disk(src, f"invoice-backup-2026010{i}-000000.zip")

    remaining = sorted(p.name for p in kept.glob("invoice-backup-*.zip"))
    assert len(remaining) == backup_service._KEEP_LOCAL_BACKUPS
    assert remaining[-1] == "invoice-backup-20260104-000000.zip"  # the newest is the one kept


def test_health_reads_backup_stamp(tmp_path):
    async def body():
        engine, Session = await _factory(tmp_path)()
        try:
            async with Session() as s:
                # No stamp yet, no disk zip → «—» (None).
                h0 = await owner_report.health(s)
                assert h0.last_backup is None
                # With a stamp → Tehran-formatted.
                await settings_service.set_value(s, "last_backup_at", "2026-06-17T00:00:00+00:00")
                h1 = await owner_report.health(s)
                assert h1.last_backup == "2026-06-17 03:30"
        finally:
            await engine.dispose()
    asyncio.run(body())


async def _coro(value):
    return value
