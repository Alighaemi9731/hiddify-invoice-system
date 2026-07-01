"""PostgreSQL smoke tests (I02) — run ONLY when DATABASE_URL points at Postgres.

The regular suite runs on SQLite; production runs PostgreSQL 16. The CI
`backend-postgres` job applies every Alembic migration to a real postgres:16
service and then runs this module against the migrated schema — catching the
dialect differences (boolean server defaults, constraint enforcement, PG-only
statements) that SQLite can't. Locally this module auto-skips.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest

os.environ.setdefault("SECRET_KEY", "ci-only-not-a-secret-key")

DB_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="requires a PostgreSQL DATABASE_URL (CI backend-postgres job)",
)


def _factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DB_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_settings_roundtrip_on_postgres():
    """Settings write/read (incl. an encrypted secret) against the migrated table."""
    from app.services import settings_service

    async def run():
        engine, factory = _factory()
        try:
            async with factory() as s:
                await settings_service.set_value(s, "toman_per_usdt", 123_456)
                assert int(await settings_service.get(s, "toman_per_usdt")) == 123_456
                await settings_service.set_value(s, "telegram_bot_token", "tok-ci-smoke")
                assert await settings_service.get(s, "telegram_bot_token") == "tok-ci-smoke"
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _seed(factory):
    from app.models import Invoice, Panel, Payment, Reseller
    from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus

    tag = uuid.uuid4().hex[:10]
    async with factory() as s:
        panel = Panel(key=f"smoke-{tag}", host=f"{tag}.invalid", proxy_path_enc="x",
                      owner_uuid=f"o-{tag}")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid=f"a-{tag}", name="smoke")
        s.add(r)
        await s.flush()
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id, period_start=dt.date(2026, 6, 1),
            period_end=dt.date(2026, 6, 30), period_label="2026-06",
            usage_gb=10, amount_toman=500_000, amount_usdt=5, status=InvoiceStatus.sent,
        )
        s.add(inv)
        await s.flush()
        pay = Payment(
            reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.usdt_txid,
            status=PaymentStatus.pending, txid=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"[:66],
        )
        s.add(pay)
        await s.commit()
        return panel.id, r.id, inv.id, pay.id


def test_money_rows_and_status_filters_on_postgres():
    """Insert the core money entities and run the production-style status-filtered query."""
    from sqlalchemy import select

    from app.models import Invoice, Panel
    from app.models.enums import InvoiceStatus

    async def run():
        engine, factory = _factory()
        try:
            panel_id, _rid, inv_id, _pid = await _seed(factory)
            async with factory() as s:
                owed = (
                    await s.execute(
                        select(Invoice.id).where(
                            Invoice.status.in_(
                                (InvoiceStatus.sent, InvoiceStatus.overdue,
                                 InvoiceStatus.enforced)),
                            Invoice.panel_id == panel_id,
                        )
                    )
                ).scalars().all()
                assert inv_id in owed
                # cleanup (FK CASCADE removes reseller/invoice/payment rows)
                panel = await s.get(Panel, panel_id)
                await s.delete(panel)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonnegative_constraint_enforced_on_postgres():
    """The B07 CHECK constraints must actually reject bad money on PG (SQLite also enforces
    CHECK, but this proves the migration carried them to the production dialect)."""
    from sqlalchemy.exc import IntegrityError

    from app.models import Invoice, Panel, Reseller
    from app.models.enums import InvoiceStatus

    async def run():
        engine, factory = _factory()
        try:
            tag = uuid.uuid4().hex[:10]
            # Commit the fixtures FIRST — the constraint violation below rolls back its
            # whole transaction, and the cleanup needs the panel row to survive that.
            async with factory() as s:
                panel = Panel(key=f"neg-{tag}", host=f"{tag}.invalid", proxy_path_enc="x",
                              owner_uuid=f"o-{tag}")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid=f"a-{tag}", name="neg")
                s.add(r)
                await s.commit()
                panel_id, reseller_id = panel.id, r.id
            async with factory() as s:
                s.add(Invoice(
                    reseller_id=reseller_id, panel_id=panel_id,
                    period_start=dt.date(2026, 6, 1), period_end=dt.date(2026, 6, 30),
                    period_label="2026-06",
                    usage_gb=1, amount_toman=-1, amount_usdt=0, status=InvoiceStatus.draft,
                ))
                with pytest.raises(IntegrityError):
                    await s.commit()
                await s.rollback()
            async with factory() as s:
                panel = await s.get(Panel, panel_id)
                assert panel is not None
                await s.delete(panel)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
