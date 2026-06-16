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


def test_successful_send_stamps_last_backup(tmp_path, monkeypatch):
    async def body(monkeypatch):
        engine, Session = await _factory(tmp_path)()
        try:
            async with Session() as s:
                await settings_service.set_value(s, "owner_chat_id", "123")
                monkeypatch.setattr(backup_delivery.backup, "create_backup",
                                    lambda _s: _coro((b"zip", "backup.zip")))
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
                monkeypatch.setattr(backup_delivery.backup, "create_backup",
                                    lambda _s: _coro((b"zip", "backup.zip")))
                monkeypatch.setattr(backup_delivery, "build_bot", lambda _s: _coro(_Bot(fail=True)))
                with pytest.raises(RuntimeError):
                    await backup_delivery.send_backup_to_owner(s)
                assert not (await settings_service.get(s, "last_backup_at", ""))  # never stamped
        finally:
            await engine.dispose()
    asyncio.run(body(monkeypatch))


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
