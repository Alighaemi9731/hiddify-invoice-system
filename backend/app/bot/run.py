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


async def _current_token() -> str | None:
    async with SessionLocal() as session:
        return await get_token(session)


async def main() -> None:
    await run_bootstrap()
    # Self-restart if a restore (here or in the backend) changed the DB / SECRET_KEY, so the
    # bot never keeps a stale key or a pooled handle to the pre-restore database.
    from app.services import restart_signal

    restart_signal.start_watcher()
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


if __name__ == "__main__":
    asyncio.run(main())
