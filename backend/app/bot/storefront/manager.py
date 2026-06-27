"""Storefront-bot manager: polls every active reseller storefront bot in this process.

ONE shared Dispatcher (the storefront router) polls ALL active bots through a SINGLE
`start_polling(*bots)` call — aiogram's `Dispatcher.start_polling` takes a `_running_lock` for its whole
lifetime, so calling it once per bot would deadlock every bot after the first. When the active set
changes (a bot added/removed, or a token changed) the manager cancels the single polling task and
restarts it with the new full set. A reconcile loop (~30s) plus a direct call from the setup wizard keep
it in sync; an invalid/revoked token is marked errored and skipped (without churning the others). The
handler resolves the tenant from `bot.id`.
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
_GETME_CONCURRENCY = 20  # bounded parallel token validation (cold start with many bots stays fast)

_dp: Dispatcher | None = None
_poll_task: asyncio.Task[None] | None = None
_polled: list[Bot] = []                 # Bot instances in the current polling task
_attempted: dict[int, str] = {}         # reseller_id → token last acted on (incl. ones that errored)
_reconcile_lock = asyncio.Lock()        # serialize reconcile (loop tick vs. setup-wizard call)


def _dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        from app.bot.storefront.handlers import storefront_router

        _dp = Dispatcher(storage=MemoryStorage())
        _dp.include_router(storefront_router)
    return _dp


async def _safe_close(bot: Bot) -> None:
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        pass


async def _stop_polling() -> None:
    """Cancel the single polling task and close its bots' sessions (idempotent)."""
    global _poll_task, _polled
    if _poll_task is not None:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — never let a dying task break reconcile
            log.warning("storefront polling task ended with error", exc_info=True)
        _poll_task = None
    for bot in _polled:
        await _safe_close(bot)
    _polled = []


async def reconcile() -> None:
    """Make the running set match the DB. Restart the single poller only when the desired token-set
    changes (so an added/removed bot or a rotated token takes effect; otherwise it's a no-op)."""
    global _poll_task, _polled, _attempted
    async with _reconcile_lock:
        async with SessionLocal() as s:
            rows = await storefront.active_bots(s)
            desired: dict[int, tuple[int, str]] = {}
            for r in rows:
                tok = storefront.bot_token(r)
                if tok:
                    desired[r.reseller_id] = (r.id, tok)

        desired_tokens = {rid: tok for rid, (_row_id, tok) in desired.items()}
        polling_alive = _poll_task is not None and not _poll_task.done()
        if desired_tokens == _attempted and (polling_alive or not desired):
            return  # nothing changed and the poller is healthy (or there's nothing to poll)
        _attempted = desired_tokens

        await _stop_polling()

        # Validate every desired bot's token in PARALLEL (bounded) — a revoked token is filtered out
        # here (marked errored, skipped) so it can't crash the shared poller; bounded concurrency keeps
        # a cold start with hundreds of bots fast instead of N sequential getMe calls.
        sem = asyncio.Semaphore(_GETME_CONCURRENCY)

        async def _validate(row_id: int, rid: int, tok: str) -> Bot | None:
            async with sem:
                bot = Bot(token=tok)
                try:
                    me = await asyncio.wait_for(bot.get_me(), timeout=20)
                except Exception as exc:  # noqa: BLE001 — invalid/revoked token → mark errored, skip
                    await _safe_close(bot)
                    async with SessionLocal() as s:
                        await storefront.mark_errored(s, row_id, f"getMe failed: {exc}")
                    log.warning("storefront bot row %s token invalid: %s", row_id, exc)
                    return None
                try:  # persist the id/username; a duplicate-token unique clash must NOT abort the fleet
                    async with SessionLocal() as s:
                        row = await s.get(StorefrontBot, row_id)
                        if row is not None and (
                            row.bot_telegram_id != me.id or row.bot_username != me.username
                        ):
                            row.bot_telegram_id = me.id
                            row.bot_username = me.username
                            await s.commit()
                except Exception as exc:  # noqa: BLE001 — e.g. two rows share one token → same me.id
                    await _safe_close(bot)
                    async with SessionLocal() as s:
                        await storefront.mark_errored(s, row_id, f"id clash: {exc}")
                    log.warning("storefront bot row %s id persist failed: %s", row_id, exc)
                    return None
                log.info("storefront bot @%s (reseller %s) ready", me.username, rid)
                return bot

        # return_exceptions so one unexpected failure can't cancel the others (belt + suspenders).
        validated = await asyncio.gather(
            *(_validate(row_id, rid, tok) for rid, (row_id, tok) in desired.items()),
            return_exceptions=True,
        )
        bots: list[Bot] = [b for b in validated if isinstance(b, Bot)]
        _polled = bots
        if bots:
            # ONE start_polling for ALL bots; we own session-closing (close_bot_session=False).
            _poll_task = asyncio.create_task(
                _dispatcher().start_polling(*bots, handle_signals=False, close_bot_session=False)
            )
            log.info("storefront polling %d bot(s)", len(bots))


async def run_manager() -> None:
    log.info("storefront manager started")
    while True:
        try:
            await reconcile()
        except Exception:  # noqa: BLE001 — the manager must never die
            log.exception("storefront reconcile failed")
        await asyncio.sleep(_RECONCILE_SECONDS)
