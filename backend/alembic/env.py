"""Alembic environment with safe adoption of the pre-Alembic production schema."""
from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (register all tables on Base.metadata)
from alembic import context
from app.core.config import settings
from app.core.db import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
log = logging.getLogger("alembic.env")
BASELINE_REVISION = "18a3b4fd6e33"
_MIGRATION_LOCK = 734_137_043


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    is_postgres = connection.dialect.name == "postgresql"
    if is_postgres:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK})
        connection.commit()  # session lock survives; leave no ambient transaction for Alembic
    try:
        _adopt_existing_schema(connection)
        # Inspector/schema-adoption queries autobegin a SQLAlchemy transaction. Alembic must
        # start and own its migration transaction, otherwise connection close rolls back the
        # version row (and transactional DDL on PostgreSQL).
        connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if is_postgres:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK}
                )
                connection.commit()
            except Exception:  # noqa: BLE001 - connection close also releases session locks
                log.warning("Could not explicitly release migration advisory lock", exc_info=True)


def _adopt_existing_schema(connection) -> None:
    """Stamp a compatible pre-Alembic database at the baseline revision.

    Existing installations already have the v1.37.43 tables. We validate every expected
    table/column before stamping, so an incomplete schema fails startup instead of being
    silently treated as current. Fresh databases have no app tables and run the baseline
    migration normally.
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    if "alembic_version" in existing_tables:
        return
    expected_tables = {table.name for table in target_metadata.sorted_tables}
    present_app_tables = existing_tables & expected_tables
    if not present_app_tables:
        return

    missing_tables = sorted(expected_tables - existing_tables)
    missing_columns: list[str] = []
    for table in target_metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in actual:
                missing_columns.append(f"{table.name}.{column.name}")
    if missing_tables or missing_columns:
        details = []
        if missing_tables:
            details.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            details.append("missing columns: " + ", ".join(missing_columns))
        raise RuntimeError(
            "Refusing to baseline an incomplete existing database (" + "; ".join(details) + ")"
        )

    migration_context = MigrationContext.configure(connection)
    migration_context.stamp(ScriptDirectory.from_config(config), BASELINE_REVISION)
    connection.commit()
    log.info("Validated existing schema and stamped baseline revision %s", BASELINE_REVISION)


async def run_migrations_online() -> None:
    engine = create_async_engine(settings.sqlalchemy_url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
