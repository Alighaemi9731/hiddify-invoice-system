"""Helpers for real-Postgres concurrency ("barrier") tests.

The regular suite runs on SQLite, where `FOR UPDATE`, `pg_advisory_xact_lock`, `SKIP LOCKED`,
and partial-unique constraints are no-ops or absent — so a locking bug passes there. These
helpers drive TWO independent asyncpg connections against the same migrated Postgres DB and let
a test interleave them deterministically to assert an invariant (no double-settle, one refund,
serialized sync, txid replay rejected).

Usage: mark the test `@pytest.mark.pg_contract` and gate it with `requires_pg`. The CI
`backend-postgres` job applies every Alembic migration to a real postgres:16 and runs
`pytest -m pg_contract`; locally these auto-skip.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("SECRET_KEY", "ci-only-not-a-secret-key")

DB_URL = os.environ.get("DATABASE_URL", "")

#: Decorator: skip a test unless DATABASE_URL is Postgres (mirrors test_postgres_smoke.py).
requires_pg = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="requires a PostgreSQL DATABASE_URL (CI backend-postgres job)",
)


def make_engine():
    """A fresh async engine bound to the CI Postgres DB. Caller disposes it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DB_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class Barrier:
    """A one-shot rendezvous: coro A `arrive()`s (signalling it holds its lock), coro B `wait()`s
    for that signal before racing the conflicting op. Avoids sleeps/flakiness."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def arrive(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


async def run_two(coro_a, coro_b):  # noqa: ANN001
    """Run two coroutines concurrently on independent connections; return (result_a, result_b).
    Exceptions are returned (not raised) so a test can assert which side won/failed."""
    return await asyncio.gather(coro_a, coro_b, return_exceptions=True)
