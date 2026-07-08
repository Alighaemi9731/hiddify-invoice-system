"""H04 — owner payment-review delivery via the shared send_owner_review helper.

- A photo caption over Telegram's 1024-char cap is truncated at a line boundary and the
  FULL review follows as a second message (old code: send_photo raised → owner got
  nothing).
- ANY photo-send failure falls back to the text review — even when the proof file was
  saved to disk (old code keyed the fallback on the disk-save flag).
- The caption/message split never breaks an <a> tag.
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/ownerreview.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot.handlers.intake import _split_caption, send_owner_review  # noqa: E402
from app.services import settings_service  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await settings_service.set_value(s, "owner_chat_id", "12345")
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


class _FakeBot:
    """Records calls; `photo_fails` makes send_photo raise (caption too long / forward fail)."""

    def __init__(self, *, photo_fails=False, enforce_caption_cap=False):
        self.photo_fails = photo_fails
        self.enforce_caption_cap = enforce_caption_cap
        self.photos: list[dict] = []
        self.messages: list[str] = []

    async def send_photo(self, chat_id, photo, *, caption=None, parse_mode=None, reply_markup=None):
        if self.photo_fails:
            raise RuntimeError("Bad Request: message caption is too long")
        if self.enforce_caption_cap and caption is not None and len(caption) > 1024:
            raise RuntimeError("Bad Request: message caption is too long")
        self.photos.append({"caption": caption})

    async def send_message(self, chat_id, text, *, parse_mode=None,
                           disable_web_page_preview=None, reply_markup=None):
        self.messages.append(text)


def test_split_caption_short_stays_whole():
    cap, follow = _split_caption("short line\nsecond")
    assert follow is None and cap == "short line\nsecond"


def test_split_caption_long_cuts_at_line_and_follows_up():
    body = "\n".join(f"line {i} " + "x" * 40 for i in range(60))
    cap, follow = _split_caption(body)
    assert len(cap) <= 1024
    assert cap.endswith("…")
    assert follow is not None and follow.startswith("line 0")


def test_split_caption_never_breaks_anchor_tag():
    # Force a cut point right after a '<a' with no closing tag before the cap.
    body = "head\n" + "y" * 1000 + "\n<a href='tg://user?id=1'>Name</a>\nmore"
    cap, follow = _split_caption(body)
    assert cap.count("<a") == cap.count("</a>")  # no dangling open tag
    assert follow is not None


def test_long_caption_truncated_with_followup(tmp_path):
    async def body(s):
        bot = _FakeBot(enforce_caption_cap=True)
        review = "\n".join(f"🧾 فاکتور دوره 2026-{i:02d}: مبلغ" for i in range(1, 60))
        ok = await send_owner_review(
            s, bot, intro="رسید جدید", review_html=review, photo="file123")
        assert ok is True
        assert len(bot.photos) == 1
        assert len(bot.photos[0]["caption"]) <= 1024
        assert len(bot.messages) == 1                 # full review followed up

    _run(body, tmp_path, "d1.db")


def test_photo_failure_falls_back_to_text(tmp_path):
    """Even with a saved proof file, a forward failure must reach the owner as text."""
    async def body(s):
        bot = _FakeBot(photo_fails=True)
        ok = await send_owner_review(
            s, bot, intro="رسید جدید", review_html="🧾 فاکتور دوره 2026-06", photo="file123")
        assert ok is True
        assert bot.photos == []                       # photo send raised
        assert len(bot.messages) == 1
        assert "ناموفق بود" in bot.messages[0]         # fallback note present

    _run(body, tmp_path, "d2.db")


def test_text_only_review_when_no_photo(tmp_path):
    async def body(s):
        bot = _FakeBot()
        ok = await send_owner_review(
            s, bot, intro="", review_html="🧾 فاکتور دوره 2026-06", photo=None)
        assert ok is True
        assert bot.photos == [] and len(bot.messages) == 1

    _run(body, tmp_path, "d3.db")


def test_no_owner_chat_returns_false(tmp_path):
    async def body(s):
        await settings_service.set_value(s, "owner_chat_id", "")
        bot = _FakeBot()
        ok = await send_owner_review(s, bot, intro="", review_html="x", photo=None)
        assert ok is False and bot.messages == []

    _run(body, tmp_path, "d4.db")
