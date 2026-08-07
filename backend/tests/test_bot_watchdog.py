"""Telegram-liveness watchdog: the bot must report unhealthy — and eventually restart itself — when
its polling session stops reaching Telegram. Long-polling hides this failure (no exception, no
updates), and the old beacon touched the heartbeat as long as the event loop ran, so a mute bot
stayed «healthy» for hours. Regression cover for the 2026-07-31 fleet outage.

The second half covers the same failure one layer down: the watchdog above proves only the MAIN
bot, so every storefront bot could be mute behind it while the container reported healthy. The
fleet now stamps its own heartbeat, and /health reports it."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.bot import run
from app.bot.storefront import manager


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


# ── storefront fleet liveness ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_fleet():
    """`_active` is module state shared with the other manager tests."""
    manager._active.clear()
    manager.mark_fleet_ok()
    manager._last_fleet_alert = None
    yield
    manager._active.clear()
    manager._last_fleet_alert = None


def test_fleet_marking_ok_resets_the_silence_clock(monkeypatch):
    clock = {"t": 500.0}
    monkeypatch.setattr(manager.time, "monotonic", lambda: clock["t"])
    manager.mark_fleet_ok()
    clock["t"] += 120.0
    assert manager.fleet_silent_for() == pytest.approx(120.0)
    manager.mark_fleet_ok()
    assert manager.fleet_silent_for() == pytest.approx(0.0)


def test_a_shopless_install_is_live_not_stale(monkeypatch):
    """The most important case: no storefront bots at all is HEALTHY. Treating it as stale would
    alarm on every fresh deployment and on every install whose resellers have no shops."""
    monkeypatch.setattr(manager, "fleet_silent_for", lambda: manager._FLEET_STALE_SECONDS * 10)
    assert manager._active == {}
    assert manager.fleet_is_live() is True


def test_running_bots_gone_silent_are_stale(monkeypatch):
    manager._active[1] = object()   # a runner exists, so silence is real evidence of a mute fleet
    monkeypatch.setattr(manager, "fleet_silent_for", lambda: manager._FLEET_STALE_SECONDS + 1)
    assert manager.fleet_is_live() is False
    monkeypatch.setattr(manager, "fleet_silent_for", lambda: manager._FLEET_STALE_SECONDS - 1)
    assert manager.fleet_is_live() is True


def _run_beacon_once(monkeypatch):
    """One `fleet_beacon` iteration, ended by its trailing sleep. Returns the stamped keys."""
    stamped: list[str] = []

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    async def _set_value(_session, key, _value, **_kw):
        stamped.append(key)

    monkeypatch.setattr(manager, "SessionLocal", _S)
    monkeypatch.setattr(manager.settings_service, "set_value", _set_value)

    async def stop(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager.asyncio, "sleep", stop)

    async def go():
        with contextlib.suppress(asyncio.CancelledError):
            await manager.fleet_beacon()

    asyncio.run(go())
    return stamped


def test_beacon_stamps_only_while_the_fleet_is_live(monkeypatch):
    monkeypatch.setattr(manager, "fleet_is_live", lambda: True)
    assert _run_beacon_once(monkeypatch) == [manager.FLEET_HEARTBEAT_KEY]

    # Mute fleet → no stamp at all, so the settings row goes cold and /health reports stale.
    monkeypatch.setattr(manager, "fleet_is_live", lambda: False)
    assert _run_beacon_once(monkeypatch) == []


def test_mute_fleet_alert_is_throttled(monkeypatch, caplog):
    """A day-long outage must not write 1440 identical ERROR rows into errortrack and the digest."""
    monkeypatch.setattr(manager, "fleet_is_live", lambda: False)
    clock = {"t": 0.0}
    monkeypatch.setattr(manager.time, "monotonic", lambda: clock["t"])

    with caplog.at_level("ERROR", logger="bot.storefront"):
        _run_beacon_once(monkeypatch)                       # first detection → alert
        clock["t"] += manager._FLEET_ALERT_EVERY / 2
        _run_beacon_once(monkeypatch)                       # too soon → silent
        assert len(caplog.records) == 1
        clock["t"] += manager._FLEET_ALERT_EVERY
        _run_beacon_once(monkeypatch)                       # window elapsed → alert again
        assert len(caplog.records) == 2

    # Recovery re-arms the alert, so the NEXT outage is reported immediately.
    monkeypatch.setattr(manager, "fleet_is_live", lambda: True)
    _run_beacon_once(monkeypatch)
    assert manager._last_fleet_alert is None


def test_beacon_survives_a_database_failure(monkeypatch):
    """Liveness reporting must never be able to kill the manager it reports on."""
    monkeypatch.setattr(manager, "fleet_is_live", lambda: True)

    class _S:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(manager, "SessionLocal", _S)

    async def stop(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager.asyncio, "sleep", stop)

    async def go():
        with contextlib.suppress(asyncio.CancelledError):
            await manager.fleet_beacon()

    asyncio.run(go())   # must not raise


def test_successful_long_poll_marks_the_fleet_live(monkeypatch):
    """An EMPTY long-poll still proves reachability — a quiet shop must not read as a dead one."""
    monkeypatch.setattr(manager, "_LONGPOLL_TIMEOUT", 0)
    marks = {"n": 0}
    monkeypatch.setattr(manager, "mark_fleet_ok", lambda: marks.__setitem__("n", marks["n"] + 1))

    class FakeBot:
        async def get_updates(self, **_kw):
            if marks["n"] >= 2:
                raise asyncio.CancelledError
            return []                      # empty, but a completed round-trip

    class FakeDp:
        def resolve_used_update_types(self):
            return []

    monkeypatch.setattr(manager, "_dispatcher", lambda: FakeDp())

    async def go():
        with contextlib.suppress(asyncio.CancelledError):
            await manager._poll_one(FakeBot(), 1, asyncio.Semaphore(1), set())

    asyncio.run(go())
    assert marks["n"] >= 2


def test_persistent_poll_failure_is_logged_once(monkeypatch, caplog):
    """A one-off getUpdates error stays at debug; a bot stuck failing must say so exactly once, so
    it reaches errors_24h and the daily digest instead of vanishing into an unprinted debug line."""
    monkeypatch.setattr(manager, "_POLL_BACKOFF", 0)
    calls = {"n": 0}

    class FakeBot:
        async def get_updates(self, **_kw):
            calls["n"] += 1
            if calls["n"] > manager._POLL_FAIL_ALERT_AFTER + 3:
                raise asyncio.CancelledError
            raise RuntimeError("network down")

    class FakeDp:
        def resolve_used_update_types(self):
            return []

    monkeypatch.setattr(manager, "_dispatcher", lambda: FakeDp())

    async def go():
        with contextlib.suppress(asyncio.CancelledError):
            await manager._poll_one(FakeBot(), 7, asyncio.Semaphore(1), set())

    with caplog.at_level("WARNING", logger="bot.storefront"):
        asyncio.run(go())

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "consecutive getUpdates failures" in warnings[0].getMessage()
