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

# Columns ADDED by migrations after the baseline (v1.37.43). A genuine pre-Alembic database is at
# the baseline schema and legitimately lacks these — they're created by `upgrade` right after the
# baseline is stamped — so the adoption validator must not treat them as "missing". Append the
# fully-qualified "table.column" for every post-baseline column-add migration.
_POST_BASELINE_COLUMNS = {
    "usage_meters.renew_used_gb", "panels.host_aliases", "panels.client_proxy_path_enc",
    "end_user_snapshots.panel_user_id",
    "resellers.storefront_enabled", "resellers.storefront_monthly_fee_toman",
    "storefront_orders.label",
    "storefront_bots.free_trial_enabled", "storefront_bots.free_trial_gb",
    "storefront_bots.free_trial_days", "storefront_customers.free_trial_used",
    "storefront_orders.panel_id", "storefront_orders.last_renewed_at",
    "storefront_wallet_txns.order_id", "storefront_customers.last_seen_at",
    "storefront_orders.expiry_alerted_at", "storefront_orders.is_trial",
    "storefront_bots.co_admin_ids",
    "storefront_bots.shop_closed", "storefront_bots.closed_text",
    "storefront_orders.trial_ended_alerted_at", "storefront_orders.usage_alerted_at",
    "storefront_orders.expired_alerted_at",
    "storefront_wallet_txns.credit_code_id",
    "storefront_orders.lease_expires_at", "storefront_wallet_txns.operation_id",
    "storefront_bots.config_version",
    "storefront_bots.channel_verified_at", "storefront_bots.channel_verification_error",
}
# Whole TABLES introduced by a post-baseline migration. A pre-Alembic (baseline-era) database
# legitimately lacks these — they're created by `upgrade` right after the baseline is stamped — so
# the adoption validator must not treat them as "missing".
_POST_BASELINE_TABLES = {
    "portal_login_nonce",
    "storefront_bots", "storefront_plans", "storefront_customers",
    "storefront_wallet_txns", "storefront_orders",
    "storefront_credit_codes", "storefront_credit_redemptions",
    "storefront_operations",
    "storefront_audit_events", "storefront_api_commands",
    "storefront_broadcast_jobs", "storefront_delivery_recipients",
    "payment_settlements",
}


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

    missing_tables = sorted(expected_tables - existing_tables - _POST_BASELINE_TABLES)
    missing_columns: list[str] = []
    for table in target_metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            if column.name not in actual and qualified not in _POST_BASELINE_COLUMNS:
                missing_columns.append(qualified)
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
