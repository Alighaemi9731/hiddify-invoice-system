"""Session advisory locks survive business-service commits, across API/worker processes."""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def session_lock(session: AsyncSession, key: int, *, wait: bool = False):
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        yield True
        return
    async with async_sessionmaker(bind, expire_on_commit=False)() as guard:
        acquired = False
        try:
            if wait:
                await guard.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                acquired = True
            else:
                acquired = bool((await guard.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                )).scalar())
            yield acquired
        finally:
            if acquired:
                await guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
