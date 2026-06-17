"""Standard background broadcast: rate-limit spacing, bounded concurrency, per-recipient error
handling (blocked / failed / retry-on-429), owner summary, and no DB persistence."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bcast.db")
os.environ.setdefault("SECRET_KEY", "k")

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.services.broadcast as B  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import DeliveryLog  # noqa: E402


def _m():
    return SendMessage(chat_id=1, text="x")


class _Sess:
    async def close(self):
        return None


class _FakeBot:
    def __init__(self, behavior):
        self.behavior = behavior          # chat_id -> ok|blocked|fail|retry_then_ok
        self.concurrent = 0
        self.max_concurrent = 0
        self.session = _Sess()
        self._retried: set[int] = set()
        self.sent_bodies: list = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent_bodies.append((chat_id, text))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)        # yield so concurrent sends actually overlap
            b = self.behavior.get(chat_id, "ok")
            if b == "blocked":
                raise TelegramForbiddenError(method=_m(), message="blocked")
            if b == "fail":
                raise RuntimeError("boom")
            if b == "retry_then_ok" and chat_id not in self._retried:
                self._retried.add(chat_id)
                raise TelegramRetryAfter(method=_m(), message="flood", retry_after=1)
            return None
        finally:
            self.concurrent -= 1


def _recipients(chat_ids):
    return [{"reseller_id": i, "name": f"r{i}", "panel": "p", "chat_id": c, "status": "pending"}
            for i, c in enumerate(chat_ids)]


def _session():
    async def make():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)
    return make


def test_rate_limiter_spaces_sends():
    t = [0.0]
    sleeps: list[float] = []

    async def fsleep(d):
        sleeps.append(round(d, 6))
        t[0] += d

    rl = B._RateLimiter(25, clock=lambda: t[0], sleep=fsleep)

    async def run():
        for _ in range(5):
            await rl.acquire()

    asyncio.run(run())
    # 5 acquisitions at 25/sec → first immediate, then 4 waits of 1/25 = 0.04s.
    assert sleeps == [0.04, 0.04, 0.04, 0.04]


def test_concurrency_never_exceeds_limit_and_counts_sent(monkeypatch):
    async def body():
        engine, Session = await _session()()
        try:
            async with Session() as s:
                bot = _FakeBot({})  # everyone ok
                monkeypatch.setattr(B, "build_bot", lambda _s: _coro(bot))
                monkeypatch.setattr(B.owner_notify, "notify_owner",
                                    lambda *a, **k: _coro(True))
                counts = await B.run_broadcast(
                    s, "hi", _recipients(range(40)),
                    rate_per_sec=1_000_000, concurrency=5)
                assert counts == {"sent": 40, "blocked": 0, "failed": 0}
                assert bot.max_concurrent <= 5
                assert bot.max_concurrent > 1   # it really did run concurrently
                # No per-recipient rows were written anywhere.
                assert (await s.execute(select(func.count(DeliveryLog.id)))).scalar_one() == 0
        finally:
            await engine.dispose()
    asyncio.run(body())


def test_error_handling_and_retry(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(_d):
        await real_sleep(0)

    async def body():
        engine, Session = await _session()()
        try:
            async with Session() as s:
                behavior = {0: "ok", 1: "blocked", 2: "fail", 3: "retry_then_ok", 4: "ok"}
                bot = _FakeBot(behavior)
                monkeypatch.setattr(B, "build_bot", lambda _s: _coro(bot))
                monkeypatch.setattr(asyncio, "sleep", fast_sleep)
                summaries: list[str] = []
                monkeypatch.setattr(B.owner_notify, "notify_owner",
                                    lambda _s, text, **k: _record(summaries, text))
                counts = await B.run_broadcast(
                    s, "hi", _recipients(range(5)), unregistered=2,
                    rate_per_sec=1_000_000, concurrency=10)
                assert counts == {"sent": 3, "blocked": 1, "failed": 1}  # 0,3,4 ok; 1 blocked; 2 fail
                assert len(summaries) == 1
                assert "✅ 3" in summaries[0] and "🚫 1" in summaries[0] and "❌ 1" in summaries[0]
                assert "📵 2" in summaries[0]
        finally:
            await engine.dispose()
    asyncio.run(body())


def test_render_sends_per_recipient_body(monkeypatch):
    """The panel-migration path reuses run_broadcast with a per-recipient `render` — each recipient
    must receive its OWN message, not a shared one."""
    async def body():
        engine, Session = await _session()()
        try:
            async with Session() as s:
                bot = _FakeBot({})
                monkeypatch.setattr(B, "build_bot", lambda _s: _coro(bot))
                monkeypatch.setattr(B.owner_notify, "notify_owner", lambda *a, **k: _coro(True))
                recips = _recipients([10, 20, 30])
                counts = await B.run_broadcast(
                    s, "", recips, render=lambda rec: f"link-for-{rec['chat_id']}",
                    parse_mode="HTML", rate_per_sec=1_000_000, concurrency=5)
                assert counts["sent"] == 3
                assert sorted(bot.sent_bodies) == [
                    (10, "link-for-10"), (20, "link-for-20"), (30, "link-for-30")]
        finally:
            await engine.dispose()
    asyncio.run(body())


async def _coro(value):
    return value


async def _record(bucket, text):
    bucket.append(text)
    return True
