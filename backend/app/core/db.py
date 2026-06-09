"""Async SQLAlchemy engine, session factory, and declarative base."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from alembic import command
from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_connect_args: dict = {}
if settings.is_sqlite:
    # aiosqlite: allow use across the single asyncio loop.
    _connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.sqlalchemy_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
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
    command.upgrade(cfg, "head")
