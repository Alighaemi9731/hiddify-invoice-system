"""Async SQLAlchemy engine, session factory, and declarative base."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

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
    """Create all tables, then add any missing columns (lightweight auto-migrate).

    Alembic handles real migrations in Phase 2; for the SQLite MVP this keeps an
    existing DB (with the owner's real config/data) in sync as the schema evolves,
    without dropping anything."""
    # Import models so they are registered on Base.metadata before create_all.
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_missing_columns)


def _sync_missing_columns(sync_conn) -> None:
    """Add columns present in the ORM models but missing from the live tables.

    Only ADD COLUMN (safe, non-destructive). Columns are added as NULLable with a
    sensible DEFAULT so SQLite accepts the ALTER on a populated table."""
    import logging

    from sqlalchemy import Boolean, Integer, Numeric
    from sqlalchemy import inspect as sa_inspect

    log = logging.getLogger("db.migrate")
    inspector = sa_inspect(sync_conn)
    dialect = sync_conn.dialect

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            try:
                type_sql = column.type.compile(dialect=dialect)
            except Exception:  # noqa: BLE001
                type_sql = "VARCHAR"
            # Default so existing rows get a value (and NOT NULL stays satisfiable).
            if isinstance(column.type, (Integer, Numeric)):
                default = " DEFAULT 0"
            elif isinstance(column.type, Boolean):
                default = " DEFAULT 0"
            else:
                default = ""
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_sql}{default}'
            try:
                sync_conn.exec_driver_sql(ddl)
                log.info("auto-migrate: added %s.%s", table.name, column.name)
            except Exception:  # noqa: BLE001
                log.warning("auto-migrate: could not add %s.%s", table.name, column.name,
                            exc_info=True)
