"""Use the real aiogram dispatcher; no Telegram calls or production credentials."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher

from app.bot import run


@pytest.mark.parametrize("ending", ["watch_return", "watch_error", "cancel", "poll_error"])
def test_real_dispatcher_drains_child_before_session_closes(monkeypatch, ending):
    async def scenario():
        active = set()
        started = asyncio.Event()
        stop_watch = asyncio.Event()
        session_closed = []

        class Session:
            async def close(self):
                assert not active, "A leaked poller can reopen the session after close"
                session_closed.append(True)

        class TestDispatcher(Dispatcher):
            async def start_polling(self, bot):
                return await super().start_polling(bot, handle_signals=False)

            async def _polling(self, **kwargs):
                task = asyncio.current_task()
                active.add(task)
                started.set()
                try:
                    if ending == "poll_error":
                        raise ConnectionError("simulated polling failure")
                    await asyncio.Event().wait()
                finally:
                    active.remove(task)

        async def watchdog(bot):
            await stop_watch.wait()
            if ending == "watch_error":
                raise ConnectionError("simulated watchdog failure")

        monkeypatch.setattr(run, "_watch_session", watchdog)
        worker = asyncio.create_task(
            run._poll_with_watchdog(TestDispatcher(), SimpleNamespace(session=Session()))
        )
        await asyncio.wait_for(started.wait(), 2)
        if ending == "cancel":
            worker.cancel()
        else:
            stop_watch.set()
        result = (await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2))[0]
        assert not active
        assert session_closed
        if ending == "cancel":
            assert isinstance(result, asyncio.CancelledError)
        elif ending in ("watch_error", "poll_error"):
            assert isinstance(result, ConnectionError)
        else:
            assert result is None

    asyncio.run(scenario())


def test_reconnect_reuses_dispatcher_without_overlapping_pollers(monkeypatch):
    async def scenario():
        active = set()
        peak = 0
        started = asyncio.Event()

        class Session:
            async def close(self):
                assert not active

        class TestDispatcher(Dispatcher):
            async def start_polling(self, bot):
                return await super().start_polling(bot, handle_signals=False)

            async def _polling(self, **kwargs):
                nonlocal peak
                task = asyncio.current_task()
                active.add(task)
                peak = max(peak, len(active))
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active.remove(task)

        async def watchdog(bot):
            await started.wait()

        monkeypatch.setattr(run, "_watch_session", watchdog)
        dispatcher = TestDispatcher()
        for _ in range(3):
            started.clear()
            await asyncio.wait_for(
                run._poll_with_watchdog(dispatcher, SimpleNamespace(session=Session())), 2
            )
            assert not active
        assert peak == 1

    asyncio.run(scenario())
