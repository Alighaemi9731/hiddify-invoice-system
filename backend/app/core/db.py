"""Async SQLAlchemy engine, session factory, and declarative base."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from alembic import command
from app.core.config import settings

log = logging.getLogger("db")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_connect_args: dict = {}
if settings.is_sqlite:
    # aiosqlite: allow use across the single asyncio loop.
    _connect_args = {"check_same_thread": False}

# Postgres gets an explicit, larger pool: the bot process shares one pool across the main bot, every
# storefront bot, and concurrent buyers; the SQLAlchemy default (5 + 10 overflow) is tight. The
# storefront keeps transactions short and does network I/O outside sessions, so this comfortably covers
# many simultaneous customers. SQLite (tests/local) ignores pool sizing, so only pass it for Postgres.
_pool_args: dict = {}
if not settings.is_sqlite:
    _pool_args = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }

engine = create_async_engine(
    settings.sqlalchemy_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
    **_pool_args,
)

SessionLocal = async_sessionmaker(
    bind=engine, expire_on_commit=False, autoflush=False
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a request-scoped session."""
    async with SessionLocal() as session:
        yield session


async def init_models() -> None:
    """Upgrade the database to the current versioned Alembic head."""
    await asyncio.to_thread(_upgrade_schema)


def _upgrade_schema() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.attributes["configure_logger"] = False
    try:
        command.upgrade(cfg, "head")
    except CommandError as exc:
        # Almost always: a rollback left the DATABASE ahead of the CODE. `alembic_version` names a
        # revision this (older) release does not ship, so Alembic cannot resolve the chain — and it
        # fails here, before any DDL, which is why even a purely additive migration blocks the boot.
        # Nothing above catches this, and `restart: unless-stopped` turns it into a silent restart
        # loop on both the backend and the bot. Name the mismatch and the way out, then FAIL —
        # serving on an unknown schema would be far worse than not starting.
        if "locate revision" in str(exc).lower():
            log.critical(
                "DATABASE SCHEMA IS AHEAD OF THIS RELEASE — refusing to start.\n"
                "  %s\n"
                "  This build's migrations do not contain the revision recorded in the database,\n"
                "  which normally means the code was rolled back without downgrading the schema.\n"
                "  Fix it with:  sudo ALLOW_DOWNGRADE=1 deploy/rollback.sh <this release's tag>\n"
                "  (that downgrades the schema first, from the newer image, then swaps the code)\n"
                "  — or redeploy the newer release to get back to a matching pair.",
                exc,
            )
        raise
