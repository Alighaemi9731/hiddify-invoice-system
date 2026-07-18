"""An encrypted backup must be restorable through the bot.

The passphrase mechanism exists so a backup can be carried to a NEW server — that is its entire
point. But the bot's restore handler read the passphrase only from *this* server's
`backup_passphrase` setting, and a fresh install's copy of that setting is empty. So on the one
machine where it mattered, the owner was told «گذرواژهٔ پشتیبان را وارد کنید» with no way to enter
one. The web panel had a passphrase field all along; the bot did not.

The document's caption is now the passphrase, with the stored setting kept as a same-server
convenience. The second test matters as much as the first: a mechanism nobody is told about is not
a fix, so the encrypted-without-passphrase failure has to say how to retry.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/botrestore.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot.handlers import text_fallback  # noqa: E402
from app.core.db import Base  # noqa: E402


class _Doc:
    file_name = "invoice-backup-20260718-101500.zip"
    mime_type = "application/zip"


class _Buf:
    def __init__(self, data: bytes = b"zip-bytes") -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _Bot:
    async def download(self, doc):  # noqa: ANN001
        return _Buf()


class _Msg:
    def __init__(self, caption: str | None) -> None:
        self.caption = caption
        self.document = _Doc()
        self.bot = _Bot()
        self.from_user = type("U", (), {"id": 111, "username": "owner"})()
        self.replies: list[str] = []

    async def answer(self, text, **kw):  # noqa: ANN001
        self.replies.append(text)


class _State:
    async def get_state(self):
        return None


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await body(Session)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _patch_common(monkeypatch, Session, *, stored_passphrase: str = ""):
    """Owner-gated handler with a controllable stored setting."""
    async def is_owner(session, user):  # noqa: ANN001
        return True

    async def get_setting(session, key, default=None):  # noqa: ANN001
        if key == "backup_passphrase":
            return stored_passphrase
        return default

    monkeypatch.setattr(text_fallback.common, "_is_owner_user", is_owner)
    monkeypatch.setattr(text_fallback.common, "SessionLocal", Session)
    monkeypatch.setattr(text_fallback.settings_service, "get", get_setting)


def test_caption_is_used_as_the_passphrase(monkeypatch):
    """The cross-server case: this server's stored passphrase is empty, so only the caption can
    carry it."""
    seen: dict = {}

    def fake_restore(data, *, passphrase=None):  # noqa: ANN001
        seen["passphrase"] = passphrase
        return {"restored": True, "db_kind": "postgres", "note": "ok"}

    async def body(Session):
        _patch_common(monkeypatch, Session, stored_passphrase="")
        monkeypatch.setattr(
            "app.services.backup.restore_from_zip", fake_restore, raising=True)
        msg = _Msg(caption="  my-secret-pass  ")
        await text_fallback.on_document(msg, _State())

    _run(body)
    assert seen["passphrase"] == "my-secret-pass", "caption was ignored"


def test_stored_setting_is_the_fallback_when_there_is_no_caption(monkeypatch):
    """Same-server restores keep working exactly as before."""
    seen: dict = {}

    def fake_restore(data, *, passphrase=None):  # noqa: ANN001
        seen["passphrase"] = passphrase
        return {"restored": True, "db_kind": "postgres", "note": "ok"}

    async def body(Session):
        _patch_common(monkeypatch, Session, stored_passphrase="from-settings")
        monkeypatch.setattr(
            "app.services.backup.restore_from_zip", fake_restore, raising=True)
        await text_fallback.on_document(_Msg(caption=None), _State())

    _run(body)
    assert seen["passphrase"] == "from-settings"


def test_caption_wins_over_the_stored_setting(monkeypatch):
    """A rotated passphrase must be overridable — otherwise a stale stored value silently wins."""
    seen: dict = {}

    def fake_restore(data, *, passphrase=None):  # noqa: ANN001
        seen["passphrase"] = passphrase
        return {"restored": True, "db_kind": "postgres", "note": "ok"}

    async def body(Session):
        _patch_common(monkeypatch, Session, stored_passphrase="stale-old-value")
        monkeypatch.setattr(
            "app.services.backup.restore_from_zip", fake_restore, raising=True)
        await text_fallback.on_document(_Msg(caption="the-right-one"), _State())

    _run(body)
    assert seen["passphrase"] == "the-right-one"


def test_encrypted_without_passphrase_explains_how_to_retry(monkeypatch):
    """Discoverability IS the fix. Without this the caption mechanism is invisible and the restore
    just looks broken."""
    def fake_restore(data, *, passphrase=None):  # noqa: ANN001
        raise ValueError("این پشتیبان رمزگذاری شده است؛ گذرواژهٔ پشتیبان را وارد کنید.")

    msgs: list[_Msg] = []

    async def body(Session):
        _patch_common(monkeypatch, Session, stored_passphrase="")
        monkeypatch.setattr(
            "app.services.backup.restore_from_zip", fake_restore, raising=True)
        msg = _Msg(caption=None)
        msgs.append(msg)
        await text_fallback.on_document(msg, _State())

    _run(body)
    last = msgs[0].replies[-1]
    assert "کپشن" in last, f"the retry instruction is missing: {last}"


def test_an_unrelated_failure_is_not_dressed_up_as_a_passphrase_problem(monkeypatch):
    """Don't send the owner chasing a passphrase when the real fault is something else."""
    def fake_restore(data, *, passphrase=None):  # noqa: ANN001
        raise ValueError("فایل پشتیبان معتبر نیست (zip خراب است).")

    msgs: list[_Msg] = []

    async def body(Session):
        _patch_common(monkeypatch, Session, stored_passphrase="")
        monkeypatch.setattr(
            "app.services.backup.restore_from_zip", fake_restore, raising=True)
        msg = _Msg(caption=None)
        msgs.append(msg)
        await text_fallback.on_document(msg, _State())

    _run(body)
    assert "کپشن" not in msgs[0].replies[-1]
