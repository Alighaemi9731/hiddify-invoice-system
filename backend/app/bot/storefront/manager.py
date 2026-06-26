"""Storefront-bot manager: polls every active reseller storefront bot in this process.

ONE shared Dispatcher (the storefront router) drives all of them; each bot gets its own cancellable
polling task. A reconcile loop (~30s) starts newly-enabled bots, restarts a bot whose token changed,
stops disabled/removed ones, and marks a bot errored if its token is revoked. Runs alongside the main
bot inside the existing `bot` container. The handler resolves the tenant from `bot.id`.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.core.db import SessionLocal
from app.models import StorefrontBot
from app.services import storefront

log = logging.getLogger("bot.storefront")

_RECONCILE_SECONDS = 30

# reseller_id → (Bot, polling task, token-in-use)
_active: dict[int, tuple[Bot, asyncio.Task[None], str]] = {}
_dp: Dispatcher | None = None


def _dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        from app.bot.storefront.handlers import storefront_router

        _dp = Dispatcher(storage=MemoryStorage())
        _dp.include_router(storefront_router)
    return _dp


async def _start_one(bot_row_id: int, reseller_id: int, token: str) -> None:
    bot = Bot(token=token)
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=20)
    except Exception as exc:  # noqa: BLE001 — invalid/revoked token → mark errored, don't poll
        await _safe_close(bot)
        async with SessionLocal() as s:
            await storefront.mark_errored(s, bot_row_id, f"getMe failed: {exc}")
        log.warning("storefront bot row %s token invalid: %s", bot_row_id, exc)
        return
    async with SessionLocal() as s:
        row = await s.get(StorefrontBot, bot_row_id)
        if row is not None and (row.bot_telegram_id != me.id or row.bot_username != me.username):
            row.bot_telegram_id = me.id
            row.bot_username = me.username
            await s.commit()
    task = asyncio.create_task(_dispatcher().start_polling(bot, handle_signals=False))
    _active[reseller_id] = (bot, task, token)
    log.info("storefront bot @%s (reseller %s) polling", me.username, reseller_id)


async def _stop_one(reseller_id: int) -> None:
    entry = _active.pop(reseller_id, None)
    if not entry:
        return
    bot, task, _ = entry
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.warning("storefront bot %s polling task ended with error", reseller_id, exc_info=True)
    await _safe_close(bot)


async def _safe_close(bot: Bot) -> None:
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        pass


async def reconcile() -> None:
    """Make the running set match the DB: start new, restart on token change, stop disabled/removed."""
    async with SessionLocal() as s:
        rows = await storefront.active_bots(s)
        desired: dict[int, tuple[int, str]] = {}
        for r in rows:
            tok = storefront.bot_token(r)
            if tok:
                desired[r.reseller_id] = (r.id, tok)

    for rid in list(_active):
        if rid not in desired:
            await _stop_one(rid)
        elif _active[rid][2] != desired[rid][1]:  # token changed → restart
            await _stop_one(rid)

    for rid, (row_id, tok) in desired.items():
        if rid not in _active:
            await _start_one(row_id, rid, tok)


async def run_manager() -> None:
    log.info("storefront manager started")
    while True:
        try:
            await reconcile()
        except Exception:  # noqa: BLE001 — the manager must never die
            log.exception("storefront reconcile failed")
        await asyncio.sleep(_RECONCILE_SECONDS)
