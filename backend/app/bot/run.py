"""
Bot process entrypoint:  python -m app.bot.run

Waits until a bot token is configured (in .env or the panel), then long-polls.
Runs in its own container/process; the backend's scheduler sends messages via the
notifier (it does not need this polling loop).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import rtl_middleware
from app.bot.commands import apply_command_menus
from app.bot.handlers import router
from app.bot.telegram import get_token
from app.core.db import SessionLocal
from app.services.bootstrap import run_bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# In-app error tracking: fingerprint + persist every ERROR/exception (shared data volume,
# own per-process file) so the backend's /health and daily digest see bot errors too.
from app.core import errortrack  # noqa: E402

errortrack.install("bot")


async def _current_token() -> str | None:
    async with SessionLocal() as session:
        return await get_token(session)


async def _main_bot_loop() -> None:
    # Build the Dispatcher ONCE — a router can only be attached to one dispatcher,
    # so we reuse it across reconnects (only the Bot/session is recreated).
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    while True:
        token = await _current_token()
        if not token:
            log.info("No Telegram bot token configured yet — retrying in 30s "
                     "(set it in .env or the panel Settings tab).")
            # A pre-setup install has nothing to reach Telegram WITH; keep the beacon honest so the
            # watchdog never reads «no token» as «bot went silent» and restarts the container forever.
            mark_telegram_ok()
            await asyncio.sleep(30)
            continue

        bot = Bot(token=token)
        rtl_middleware.install(bot)  # bidi-safe outgoing text on every handler reply
        try:
            # Default (reseller) command menu globally + owner menu scoped to the
            # owner's chat, so the owner doesn't see reseller-only commands.
            async with SessionLocal() as session:
                await apply_command_menus(bot, session)
        except Exception:  # noqa: BLE001
            log.warning("set_my_commands failed", exc_info=True)
        try:
            # Cache our @username: the portal's stable-link page renders Telegram's official login
            # button, which needs the bot username. Cheap (once per bot start) and self-healing if
            # the token is ever swapped for a different bot.
            me = await bot.get_me()
            if me.username:
                async with SessionLocal() as session:
                    from app.services import settings_service

                    if (await settings_service.get(
                            session, "telegram_bot_username", "")) != me.username:
                        await settings_service.set_value(
                            session, "telegram_bot_username", me.username)
        except Exception:  # noqa: BLE001 — a transient getMe must never block polling
            log.warning("caching bot username failed", exc_info=True)
        log.info("Bot polling started.")
        mark_telegram_ok()  # a fresh session that just answered getMe counts as proof of life
        try:
            # Poll and watch the SAME session side by side. `start_polling` can keep looping forever
            # on a session that no longer reaches Telegram (no exception, no updates, container stays
            # «healthy») — that silent wedge is what left the fleet mute on 2026-07-31. Whichever task
            # finishes first ends this round; the `while` then rebuilds the Bot with a new session.
            poll = asyncio.create_task(dp.start_polling(bot), name="polling")
            watch = asyncio.create_task(_watch_session(bot), name="session-watchdog")
            done, pending = await asyncio.wait(
                {poll, watch}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                task.result()  # re-raise a real failure into the handler below
        except Exception:  # noqa: BLE001
            log.exception("Polling stopped unexpectedly; restarting in 15s.")
            await asyncio.sleep(15)
        finally:
            with contextlib.suppress(Exception):
                await bot.session.close()


# Container-LOCAL on purpose (not the shared data volume): liveness is per-container.
# The compose healthcheck probes this file's mtime (stale >120s → unhealthy).
_HEARTBEAT_FILE = Path("/tmp/bot.heartbeat")

# How often the watchdog proves the polling session can still reach Telegram, how many misses in a
# row condemn the session, and how long a total blackout may last before the PROCESS gives up and
# lets Docker recreate the container (which also re-polls every storefront bot).
_WATCH_INTERVAL_SECONDS = 60.0
_WATCH_TIMEOUT_SECONDS = 20.0
_WATCH_STRIKES = 3
_STALE_SECONDS = 300.0
_HARD_EXIT_SECONDS = 900.0

# Monotonic stamp of the last PROVEN Telegram round-trip. A live event loop is not proof of a live
# bot: the beacon used to touch the heartbeat unconditionally, so a wedged poller kept reporting
# «healthy» forever and nothing ever restarted it.
_last_telegram_ok = time.monotonic()


def mark_telegram_ok() -> None:
    """Record a successful Telegram round-trip (called by the watchdog and on each fresh session)."""
    global _last_telegram_ok
    _last_telegram_ok = time.monotonic()


def telegram_silent_for() -> float:
    """Seconds since the last proven Telegram round-trip."""
    return time.monotonic() - _last_telegram_ok


async def _watch_session(bot: Bot) -> None:
    """Return as soon as this bot's session stops reaching Telegram, so the caller can rebuild it.

    `getMe` is the cheapest call that exercises the very session the poller uses; a hung session
    fails the timeout rather than raising, which is exactly the failure mode long-polling hides.
    """
    strikes = 0
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
        try:
            await asyncio.wait_for(bot.get_me(), timeout=_WATCH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure is a strike, none may crash the loop
            strikes += 1
            log.warning("Telegram watchdog strike %d/%d: %r", strikes, _WATCH_STRIKES, exc)
            if strikes >= _WATCH_STRIKES:
                log.error("Telegram unreachable on this session — rebuilding it.")
                return
        else:
            strikes = 0
            mark_telegram_ok()


async def _liveness_beacon() -> None:
    """Touch the heartbeat file every 30s — but ONLY while Telegram is provably reachable.

    A silent bot must show up as `unhealthy` in `docker ps` instead of hiding behind a live event
    loop, and a blackout that outlives `_HARD_EXIT_SECONDS` ends the process so `restart:
    unless-stopped` recreates the container (the storefront manager comes back with it).
    """
    while True:
        silent = telegram_silent_for()
        if silent < _STALE_SECONDS:
            try:
                _HEARTBEAT_FILE.touch()
            except Exception:  # noqa: BLE001 - liveness must never crash the bot
                pass
        elif silent >= _HARD_EXIT_SECONDS:
            log.error(
                "No Telegram contact for %.0fs — exiting so the container is recreated.", silent
            )
            os._exit(1)
        else:
            log.warning("No Telegram contact for %.0fs — reporting unhealthy.", silent)
        await asyncio.sleep(30)


async def main() -> None:
    try:
        _HEARTBEAT_FILE.touch()  # first stamp before bootstrap, which can take a while
    except Exception:  # noqa: BLE001
        pass
    await run_bootstrap()
    # Self-restart if a restore (here or in the backend) changed the DB / SECRET_KEY, so the
    # bot never keeps a stale key or a pooled handle to the pre-restore database.
    from app.services import restart_signal

    restart_signal.start_watcher()

    # Run the owner/reseller main bot AND the per-reseller storefront-bot manager (which polls every
    # active reseller storefront bot) concurrently in this one process. Each runs under a supervisor so
    # an unexpected crash in one self-restarts WITHOUT cancelling the other.
    from app.bot.storefront.manager import run_manager

    await asyncio.gather(
        _supervise(_main_bot_loop, "main-bot-loop"),
        _supervise(run_manager, "storefront-manager"),
        _supervise(_liveness_beacon, "liveness-beacon"),
    )


async def _supervise(factory, name: str) -> None:  # noqa: ANN001
    """Keep a long-running loop alive: restart it on any unexpected exit, never let it cancel siblings."""
    while True:
        try:
            await factory()
            log.warning("%s exited; restarting in 15s", name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s crashed; restarting in 15s", name)
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
