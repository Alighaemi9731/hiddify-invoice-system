"""B08 workflow gate: billing -> payment -> backup in one durable scenario."""
import asyncio
import datetime as dt
import io
import sqlite3
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    EndUserSnapshot,
    FinancialRecord,
    Invoice,
    Panel,
    Payment,
    Reseller,
)
from app.models.enums import InvoiceStatus, PanelStatus, PaymentMethod, PaymentStatus
from app.services import backup, invoicing, payments, settings_service
from app.services.periods import month_period


def test_billing_payment_backup_workflow(monkeypatch, tmp_path):
    db_path = tmp_path / "workflow.db"
    extracted_path = tmp_path / "restored-copy.db"

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        period = month_period(2026, 5)
        now = dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)
        async with session_factory() as session:
            await settings_service.seed_defaults(session)
            await settings_service.set_value(session, "default_price_per_gb", 10_000)
            await settings_service.set_value(session, "toman_per_usdt", 100_000)
            await settings_service.set_value(session, "rate_mode", "manual")
            await settings_service.set_value(session, "auto_restore_on_payment", False)

            panel = Panel(
                key="stage",
                name="Staging",
                host="stage.invalid",
                proxy_path_enc="path",
                owner_uuid="owner",
                status=PanelStatus.ok,
                last_synced_at=now,
            )
            session.add(panel)
            await session.flush()
            reseller = Reseller(
                panel_id=panel.id,
                admin_uuid="reseller",
                name="Workflow Reseller",
                last_seen_at=now,
            )
            session.add(reseller)
            await session.flush()
            session.add(
                EndUserSnapshot(
                    panel_id=panel.id,
                    user_uuid="user-1",
                    name="Workflow User",
                    added_by_uuid=reseller.admin_uuid,
                    usage_limit_gb=10,
                    current_usage_gb=2,
                    start_date=dt.date(2026, 5, 10),
                    last_synced_at=now,
                )
            )
            await session.commit()

            summary = await invoicing.generate_invoices(session, period)
            assert summary.created == 1
            invoice = (await session.execute(select(Invoice))).scalar_one()
            assert invoice.status == InvoiceStatus.draft
            assert float(invoice.amount_toman) == 100_000

            invoice.status = InvoiceStatus.sent
            invoice.sent_at = now
            payment = Payment(
                reseller_id=reseller.id,
                invoice_id=invoice.id,
                method=PaymentMethod.manual,
                status=PaymentStatus.pending,
                amount_toman=invoice.amount_toman,
            )
            session.add(payment)
            await session.commit()

            result = await payments.confirm_manually(session, payment.id)
            await session.refresh(invoice)
            await session.refresh(payment)
            assert result.paid is True
            assert invoice.status == InvoiceStatus.paid
            assert payment.status == PaymentStatus.confirmed
            ledger = (
                await session.execute(
                    select(FinancialRecord).where(
                        FinancialRecord.invoice_id == invoice.id
                    )
                )
            ).scalar_one()
            assert float(ledger.amount_toman) == 100_000

            monkeypatch.setattr(backup, "_sqlite_path", lambda: db_path)
            archive, filename = await backup.create_backup(session)
            assert filename.startswith("invoice-backup-")
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                assert {"meta.json", "settings.json", "db.sqlite"} <= set(
                    zipped.namelist()
                )
                extracted_path.write_bytes(zipped.read("db.sqlite"))

        await engine.dispose()

    asyncio.run(run())

    restored = sqlite3.connect(extracted_path)
    try:
        assert restored.execute(
            "SELECT status FROM invoices"
        ).fetchone() == (InvoiceStatus.paid.value,)
        assert restored.execute(
            "SELECT status FROM payments"
        ).fetchone() == (PaymentStatus.confirmed.value,)
        assert restored.execute("SELECT count(*) FROM financial_records").fetchone() == (1,)
    finally:
        restored.close()
