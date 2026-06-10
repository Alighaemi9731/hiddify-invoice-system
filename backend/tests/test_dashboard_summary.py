"""Dashboard metrics stay consistent across periods and outstanding invoices."""
import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.reports import dashboard
from app.core.db import Base
from app.models import Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus, PanelStatus


def test_dashboard_summary_metrics(tmp_path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dashboard.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with session_factory() as session:
                healthy = Panel(
                    key="healthy", host="healthy.invalid", proxy_path_enc="x",
                    owner_uuid="owner-1", enabled=True, status=PanelStatus.ok,
                )
                offline = Panel(
                    key="offline", host="offline.invalid", proxy_path_enc="x",
                    owner_uuid="owner-2", enabled=False, status=PanelStatus.disabled,
                )
                session.add_all([healthy, offline])
                await session.flush()

                owner = Reseller(
                    panel_id=healthy.id, admin_uuid="owner-1", name="Owner", is_owner=True,
                )
                first = Reseller(
                    panel_id=healthy.id, admin_uuid="first", parent_admin_uuid="owner-1",
                    name="First", bot_chat_id=1001,
                )
                second = Reseller(
                    panel_id=healthy.id, admin_uuid="second", parent_admin_uuid="owner-1",
                    name="Second",
                )
                session.add_all([owner, first, second])
                await session.flush()

                def invoice(
                    reseller: Reseller,
                    label: str,
                    amount: int,
                    status: InvoiceStatus,
                ) -> Invoice:
                    year, month = (int(part) for part in label.split("-"))
                    start = dt.date(year, month, 1)
                    end = dt.date(year, month, 28)
                    return Invoice(
                        reseller_id=reseller.id, panel_id=healthy.id,
                        period_start=start, period_end=end, period_label=label,
                        usage_gb=10, amount_toman=amount, status=status,
                    )

                session.add_all([
                    invoice(first, "2026-06", 1_000_000, InvoiceStatus.paid),
                    invoice(second, "2026-06", 500_000, InvoiceStatus.overdue),
                    invoice(first, "2026-05", 750_000, InvoiceStatus.sent),
                ])
                await session.commit()

                summary = await dashboard("2026-06", session)

                assert summary.period == "2026-06"
                assert summary.previous_period == "2026-05"
                assert summary.panels == 2
                assert summary.active_panels == 1
                assert summary.healthy_panels == 1
                assert summary.resellers == 2
                assert summary.registered_resellers == 1
                assert summary.period_invoices == 2
                assert summary.period_billed_toman == 1_500_000
                assert summary.previous_period_billed_toman == 750_000
                assert summary.period_paid_toman == 1_000_000
                assert summary.outstanding_toman == 1_250_000
                assert summary.outstanding_resellers == 2
                assert [row.panel_key for row in summary.sales_by_panel] == ["healthy"]
                assert [row.reseller_name for row in summary.top_resellers] == [
                    "First", "Second",
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())
