"""One TLS trust store for every aiogram Bot in this process.

aiogram's `AiohttpSession.__init__` builds `ssl.create_default_context(cafile=certifi.where())`
per session, which re-parses certifi's whole CA bundle into a fresh OpenSSL trust store for
EVERY bot. Measured (`bench_fleet_one.py`, one process per variant because `ru_maxrss` is a
high-water mark):

    per-bot context   151 bots -> 101.8 MB RSS   (690 KB/bot)
                      400 bots -> 270.0 MB RSS
    shared context    151 bots ->   5.8 MB RSS   ( 39 KB/bot)
                      400 bots ->   7.0 MB RSS   ( 18 KB/bot)

That memory is allocated by OpenSSL, not CPython, so `tracemalloc` cannot see it — which is
why it went unnoticed until the fleet reached ~151 shops and the bot container's cap had to be
raised twice.

The session object itself stays PER-BOT on purpose. `storefront/manager._stop_runner` closes
`runner.bot.session` to abort an in-flight `getUpdates` immediately; that is what stops a
token-rotation restart from overlapping its own outstanding long-poll and triggering Telegram
409 «terminated by other getUpdates», which once double-delivered every update. A single
fleet-wide session measures better still (~5 KB/bot) but would tear down every bot's polling
on any single stop, so it is deliberately NOT used — see plans/PERF_AUDIT_2026-08.md.
"""
from __future__ import annotations

import ssl

import certifi
from aiogram.client.session.aiohttp import AiohttpSession

#: Process-wide TLS trust store. Immutable after creation and safe to share: aiohttp only
#: reads it when opening a connection, and `make_request` takes the token from the `Bot`
#: argument, so nothing bot-specific lives here.
SHARED_SSL_CONTEXT: ssl.SSLContext = ssl.create_default_context(cafile=certifi.where())


def new_session(**kwargs) -> AiohttpSession:
    """A fresh per-bot `AiohttpSession` that reuses the process-wide TLS context."""
    session = AiohttpSession(**kwargs)
    # aiogram exposes no public hook for the connector's ssl argument (only `limit`), so the
    # context is swapped in the connector kwargs it will pass to TCPConnector. `create_session`
    # has not run yet, so no connector exists to invalidate.
    session._connector_init["ssl"] = SHARED_SSL_CONTEXT
    return session
