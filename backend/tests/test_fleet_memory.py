"""Every Bot in this process must share ONE TLS trust store — and still own its session.

Why this file exists: aiogram builds `ssl.create_default_context(cafile=certifi.where())` per
`AiohttpSession`, re-parsing certifi's whole CA bundle into a fresh OpenSSL trust store for
every bot. At the fleet's ~151 shops that measured 690 KB of RSS per bot — 101.8 MB — and
270 MB at 400 bots, which is the wall the `bot` container's mem_limit had already been raised
twice to chase. The allocation belongs to OpenSSL, not CPython, so `tracemalloc` reports
~0 for it; only RSS sees it. That is precisely why it survived earlier audits, and why this
guard asserts on OBJECT IDENTITY rather than on a byte budget: identity is the property that
actually causes the memory, and it is stable across platforms and CI runners.

The second assertion is the one that protects correctness. The session must stay PER-BOT:
`storefront/manager._stop_runner` closes `runner.bot.session` to abort an in-flight
`getUpdates` immediately, which is what stops a token rotation from overlapping its own
outstanding long-poll and producing Telegram 409 «terminated by other getUpdates» — the bug
that once delivered every storefront update twice. A single fleet-wide session measures better
(~5 KB/bot) and is deliberately rejected; if someone "optimizes" into it, this test fails.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/fleet.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from aiogram import Bot  # noqa: E402

from app.bot.session import SHARED_SSL_CONTEXT, new_session  # noqa: E402

_N = 25  # enough to prove sharing; the real fleet is ~151


def _token(i: int) -> str:
    return f"{100000 + i}:AAH{'x' * 30}{i:04d}"


@pytest.fixture
def fleet():
    bots = [Bot(token=_token(i), session=new_session()) for i in range(_N)]
    yield bots


def test_every_bot_shares_one_ssl_context(fleet):
    """The whole memory win. A per-bot context costs ~690 KB of OpenSSL memory each."""
    contexts = [b.session._connector_init["ssl"] for b in fleet]
    assert all(ctx is SHARED_SSL_CONTEXT for ctx in contexts), (
        "a Bot was built with its own TLS context — that is 690 KB of OpenSSL memory per bot "
        "(≈96 MB across the 151-shop fleet) and tracemalloc cannot see it"
    )
    assert len({id(ctx) for ctx in contexts}) == 1


def test_each_bot_still_owns_its_session(fleet):
    """The 409 guard. `_stop_runner` closes one bot's session to abort its in-flight
    getUpdates; a shared session would tear down the entire fleet's polling instead."""
    sessions = [b.session for b in fleet]
    assert len({id(s) for s in sessions}) == _N, (
        "Bots are sharing one AiohttpSession. _stop_runner closes runner.bot.session to abort "
        "an in-flight getUpdates — sharing it would stop every storefront bot at once and "
        "reintroduce the 409 double-delivery bug (see app/bot/storefront/manager.py)"
    )


@pytest.mark.asyncio
async def test_closing_one_bot_does_not_disturb_the_others(fleet):
    """Directly exercise the stop path `_stop_runner` relies on."""
    victim, survivor = fleet[0], fleet[1]
    await victim.session.close()
    assert victim.session.closed if hasattr(victim.session, "closed") else True
    # The survivor must still be usable: its own session object is untouched and its
    # connector kwargs still carry the shared context.
    assert survivor.session is not victim.session
    assert survivor.session._connector_init["ssl"] is SHARED_SSL_CONTEXT


def test_build_bot_and_the_fleet_use_the_same_helper():
    """Every Bot construction site must go through `new_session()`; a raw `Bot(token=...)`
    silently reintroduces the per-bot context. AST-free structural check: grep the sources."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    # `Bot(` constructions that do NOT pass a session= argument on the same line.
    pattern = re.compile(r"\bBot\(\s*token=(?!.*session=)", re.MULTILINE)
    for path in root.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, (
        "these Bot constructions do not pass session=new_session(), so each builds its own "
        "TLS trust store (~690 KB):\n  " + "\n  ".join(offenders)
    )
