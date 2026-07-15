"""Storefront-bot manager: polls every active reseller storefront bot in this process.

ONE shared Dispatcher (the storefront router) handles updates, but each bot has its OWN independent,
cancellable polling loop (`bot.get_updates` → `dp.feed_update`). This is deliberate:

  • aiogram's `Dispatcher.start_polling` holds a per-dispatcher `_running_lock` for its whole lifetime,
    so it can't be called once-per-bot on a shared dispatcher (deadlocks all but the first), and calling
    it ONCE for all bots forces a full-fleet stop+restart on every change — which, with many bots and
    frequent setups, left overlapping `getUpdates` loops (Telegram 409 «terminated by other getUpdates»)
    that delivered each update TWICE. Per-bot `feed_update` loops avoid the lock AND the fleet restart.

A reconcile loop (~30s) + a direct call from the setup wizard keep the running set in sync INCREMENTALLY:
only added / removed / token-changed (or crashed) bots are touched; the rest keep polling undisturbed.
An invalid/revoked token is marked errored and skipped. The handler resolves the tenant from `bot.id`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import rtl_middleware
from app.core.db import SessionLocal
from app.models import StorefrontBot
from app.services import storefront

log = logging.getLogger("bot.storefront")

_RECONCILE_SECONDS = 30
_GETME_CONCURRENCY = 20   # bounded parallel token validation (cold start with many bots stays fast)
_LONGPOLL_TIMEOUT = 25    # getUpdates long-poll seconds
_POLL_BACKOFF = 3         # seconds to wait after a getUpdates error (e.g. a brief 409 on token change)
_UNAUTHORIZED_LIMIT = 3   # consecutive 401s before a mid-run revoked token is marked errored
_HANDLER_CONCURRENCY = 32  # max in-flight update handlers PER storefront bot (backpressure)


@dataclass
class _Runner:
    bot: Bot
    task: asyncio.Task[None]
    token: str
    sem: asyncio.Semaphore                    # caps concurrent update handlers for this bot
    tasks: set[asyncio.Task[None]]            # live handler tasks (drained on stop)


_dp: Dispatcher | None = None
_active: dict[int, _Runner] = {}          # reseller_id → its running poller
_reconcile_lock = asyncio.Lock()          # serialize reconcile (loop tick vs. setup-wizard call)


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


async def _feed(dp: Dispatcher, bot: Bot, update) -> None:  # noqa: ANN001
    try:
        await dp.feed_update(bot, update)
    except Exception:  # noqa: BLE001 — one bad update must never kill the bot's poll loop
        log.warning("storefront update handling failed", exc_info=True)


async def _feed_and_release(sem: asyncio.Semaphore, dp: Dispatcher, bot: Bot, update) -> None:  # noqa: ANN001
    """Handle one update, then release the per-bot concurrency slot the poll loop acquired for it.
    F13: the slot is acquired BEFORE the task is created (in `_poll_one`), so the number of LIVE task
    objects — not just the number of actively-running handlers — is bounded. Released in `finally` so a
    handler error or cancellation always frees the slot."""
    try:
        await _feed(dp, bot, update)
    finally:
        sem.release()


async def _poll_one(
    bot: Bot, row_id: int, sem: asyncio.Semaphore, tasks: set[asyncio.Task[None]]
) -> None:
    """One bot's dedicated long-poll loop. Each update is dispatched as its own task so a slow handler
    (panel/Telegram I/O) doesn't stall polling. Exits cleanly on cancel; backs off on transient errors.
    A token revoked MID-RUN (consecutive 401s) marks the row errored and ends the loop — otherwise the
    dead bot would burn a retry every few seconds forever until someone re-ran the setup wizard."""
    dp = _dispatcher()
    allowed = dp.resolve_used_update_types()
    offset: int | None = None
    unauthorized = 0
    while True:
        try:
            updates = await bot.get_updates(
                offset=offset, timeout=_LONGPOLL_TIMEOUT, allowed_updates=allowed)
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError as exc:
            unauthorized += 1
            if unauthorized >= _UNAUTHORIZED_LIMIT:
                log.warning("storefront bot row %s token revoked — marking errored: %s", row_id, exc)
                try:
                    async with SessionLocal() as s:
                        await storefront.mark_errored(s, row_id, f"token revoked: {exc}")
                except Exception:  # noqa: BLE001
                    log.exception("mark_errored failed for storefront bot row %s", row_id)
                return  # reconcile reaps the done task; errored rows are excluded from active_bots
            await asyncio.sleep(_POLL_BACKOFF)
            continue
        except Exception as exc:  # noqa: BLE001 — 409 during a token-change overlap, network blips, …
            log.debug("storefront getUpdates error: %s", exc)
            await asyncio.sleep(_POLL_BACKOFF)
            continue
        unauthorized = 0
        for update in updates:
            offset = update.update_id + 1
            # F13: acquire a concurrency slot BEFORE creating the task. When all _HANDLER_CONCURRENCY
            # slots are busy this blocks the poll loop (so get_updates isn't called again either) —
            # applying backpressure at the SOURCE so queued task objects can't grow without bound.
            await sem.acquire()
            t = asyncio.create_task(_feed_and_release(sem, dp, bot, update))
            tasks.add(t)              # keep a strong ref (avoid GC mid-flight) + enable drain-on-stop
            t.add_done_callback(tasks.discard)


async def _stop_runner(reseller_id: int) -> None:
    runner = _active.pop(reseller_id, None)
    if runner is None:
        return
    runner.task.cancel()
    try:
        await runner.task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.warning("storefront poll task for reseller %s ended with error", reseller_id, exc_info=True)
    # Drain in-flight handler tasks BEFORE closing the session — otherwise an orphaned handler would
    # keep running against a closed bot (raising mid-I/O). Bounded by the per-bot semaphore.
    for t in list(runner.tasks):
        t.cancel()
    if runner.tasks:
        await asyncio.gather(*runner.tasks, return_exceptions=True)
    await _safe_close(runner.bot)   # closing the session aborts any in-flight getUpdates immediately


async def _start_runner(reseller_id: int, row_id: int, token: str, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            # The Bot constructor validates the token FORMAT synchronously; a malformed stored
            # token must be marked errored like a revoked one, not re-raised every reconcile.
            bot = Bot(token=token)
        except Exception as exc:  # noqa: BLE001
            async with SessionLocal() as s:
                await storefront.mark_errored(s, row_id, f"malformed token: {exc}")
            log.warning("storefront bot row %s token malformed: %s", row_id, exc)
            return
        rtl_middleware.install(bot)  # storefront replies are Persian+Latin mixes too
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=20)
        except Exception as exc:  # noqa: BLE001 — invalid/revoked token → mark errored, don't poll
            await _safe_close(bot)
            async with SessionLocal() as s:
                await storefront.mark_errored(s, row_id, f"getMe failed: {exc}")
            log.warning("storefront bot row %s token invalid: %s", row_id, exc)
            return
        try:  # persist id/username; a duplicate-token unique clash must NOT abort the fleet
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
            return
    sem = asyncio.Semaphore(_HANDLER_CONCURRENCY)
    handler_tasks: set[asyncio.Task[None]] = set()
    task = asyncio.create_task(_poll_one(bot, row_id, sem, handler_tasks))
    _active[reseller_id] = _Runner(bot=bot, task=task, token=token, sem=sem, tasks=handler_tasks)
    log.info("storefront bot @%s (reseller %s) polling", me.username, reseller_id)


async def reconcile() -> None:
    """Make the running pollers match the DB, INCREMENTALLY: stop removed/token-changed/dead runners,
    start newly-added ones. Unchanged bots are left polling undisturbed (no fleet-wide restart)."""
    async with _reconcile_lock:
        async with SessionLocal() as s:
            rows = await storefront.active_bots(s)
            desired: dict[int, tuple[int, str]] = {}
            for r in rows:
                tok = storefront.bot_token(r)
                if tok:
                    desired[r.reseller_id] = (r.id, tok)

        # Stop runners that are gone, had their token rotated, or whose task died.
        for rid in list(_active):
            runner = _active[rid]
            if rid not in desired or desired[rid][1] != runner.token or runner.task.done():
                await _stop_runner(rid)

        # Start runners for everything desired that isn't already running (bounded-parallel getMe).
        to_start = [(rid, row_id, tok) for rid, (row_id, tok) in desired.items() if rid not in _active]
        if to_start:
            sem = asyncio.Semaphore(_GETME_CONCURRENCY)
            await asyncio.gather(
                *(_start_runner(rid, row_id, tok, sem) for rid, row_id, tok in to_start),
                return_exceptions=True,
            )
            log.info("storefront manager: %d running, %d started", len(_active), len(to_start))


async def run_manager() -> None:
    log.info("storefront manager started")
    while True:
        try:
            await reconcile()
        except Exception:  # noqa: BLE001 — the manager must never die
            log.exception("storefront reconcile failed")
        await asyncio.sleep(_RECONCILE_SECONDS)
