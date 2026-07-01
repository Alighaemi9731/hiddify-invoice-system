"""
Bot process entrypoint:  python -m app.bot.run

Waits until a bot token is configured (in .env or the panel), then long-polls.
Runs in its own container/process; the backend's scheduler sends messages via the
notifier (it does not need this polling loop).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

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
            await asyncio.sleep(30)
            continue

        bot = Bot(token=token)
        try:
            # Default (reseller) command menu globally + owner menu scoped to the
            # owner's chat, so the owner doesn't see reseller-only commands.
            async with SessionLocal() as session:
                await apply_command_menus(bot, session)
        except Exception:  # noqa: BLE001
            log.warning("set_my_commands failed", exc_info=True)
        log.info("Bot polling started.")
        try:
            await dp.start_polling(bot)
        except Exception:  # noqa: BLE001
            log.exception("Polling stopped unexpectedly; restarting in 15s.")
            await asyncio.sleep(15)
        finally:
            await bot.session.close()


async def main() -> None:
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
