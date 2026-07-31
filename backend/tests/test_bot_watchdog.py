"""Telegram-liveness watchdog: the bot must report unhealthy — and eventually restart itself — when
its polling session stops reaching Telegram. Long-polling hides this failure (no exception, no
updates), and the old beacon touched the heartbeat as long as the event loop ran, so a mute bot
stayed «healthy» for hours. Regression cover for the 2026-07-31 fleet outage."""
from __future__ import annotations

import asyncio

import pytest

from app.bot import run


def test_marking_ok_resets_the_silence_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(run.time, "monotonic", lambda: clock["t"])
    run.mark_telegram_ok()
    clock["t"] += 240.0
    assert run.telegram_silent_for() == pytest.approx(240.0)
    run.mark_telegram_ok()
    assert run.telegram_silent_for() == pytest.approx(0.0)


def test_watchdog_returns_only_after_consecutive_failures(monkeypatch):
    """One flaky getMe must not tear down a healthy session; `_WATCH_STRIKES` in a row must."""
    calls = {"n": 0}

    class FakeBot:
        async def get_me(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return object()            # healthy
            if calls["n"] == 2:
                raise TimeoutError("blip")  # single failure → forgiven
            if calls["n"] == 3:
                return object()            # recovered → strikes reset
            raise TimeoutError("wedged")    # from here on, permanently unreachable

    real_sleep = asyncio.sleep
    monkeypatch.setattr(run.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    monkeypatch.setattr(run, "_WATCH_INTERVAL_SECONDS", 0)
    asyncio.run(run._watch_session(FakeBot()))

    # 3 good/forgiven calls, then _WATCH_STRIKES consecutive failures before it gives up.
    assert calls["n"] == 3 + run._WATCH_STRIKES


def test_beacon_stops_touching_when_telegram_is_silent(monkeypatch, tmp_path):
    """The heartbeat is the container's only liveness signal — it must follow Telegram, not the loop."""
    beat = tmp_path / "bot.heartbeat"
    monkeypatch.setattr(run, "_HEARTBEAT_FILE", beat)
    silence = {"s": 0.0}
    monkeypatch.setattr(run, "telegram_silent_for", lambda: silence["s"])

    async def one_pass():
        # Run a single beacon iteration by making the trailing sleep end the loop.
        async def stop(*_a, **_k):
            raise asyncio.CancelledError

        monkeypatch.setattr(run.asyncio, "sleep", stop)
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await run._liveness_beacon()

    asyncio.run(one_pass())
    assert beat.exists()                      # reachable → heartbeat written

    beat.unlink()
    silence["s"] = run._STALE_SECONDS + 1     # silent → no heartbeat → container goes unhealthy
    asyncio.run(one_pass())
    assert not beat.exists()


def test_total_blackout_exits_the_process(monkeypatch, tmp_path):
    """Rebuilding the session is not always enough; after `_HARD_EXIT_SECONDS` the process must die so
    Docker recreates the container — that is what brings all 151 storefront bots back too."""
    monkeypatch.setattr(run, "_HEARTBEAT_FILE", tmp_path / "hb")
    monkeypatch.setattr(run, "telegram_silent_for", lambda: run._HARD_EXIT_SECONDS + 1)
    exited = {}

    def fake_exit(code):
        exited["code"] = code
        raise asyncio.CancelledError  # stand in for the process ending

    monkeypatch.setattr(run.os, "_exit", fake_exit)

    async def one_pass():
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await run._liveness_beacon()

    asyncio.run(one_pass())
    assert exited == {"code": 1}
