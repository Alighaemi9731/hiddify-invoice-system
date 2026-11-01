"""Real Postgres contracts: lock survives commits, duplicate workers and sends serialize."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app.models import DeliveryLog, Invoice, MonthlyBillingRun, Panel, Reseller
from app.models.enums import DeliveryKind, DeliveryStatus, InvoiceStatus
from app.scheduler import jobs
from app.services import delivery, monthly_billing
from app.services.invoicing import GenerationSummary
from tests.pg_barrier import make_engine, requires_pg


@pytest.mark.pg_contract
@requires_pg
def test_monthly_workers_serialize_across_business_commits(monkeypatch):
    async def run():
        engine, factory = make_engine()
        reached = asyncio.Event()
        release = asyncio.Event()
        now = dt.datetime(2091, 7, 2, 12, tzinfo=dt.timezone.utc)
        period_start = dt.date(2091, 6, 1)
        async def sync(session):
            await session.commit()  # Session-level guard must remain held after this.
            reached.set()
            await release.wait()
        monkeypatch.setattr(jobs, "load_config", AsyncMock(return_value=jobs.ScheduleConfig()))
        monkeypatch.setattr(monthly_billing.sync_service, "sync_all", sync)
        generate = AsyncMock(return_value=GenerationSummary(period="2091-06"))
        send = AsyncMock(return_value={"sent": 2, "failed": 0})
        monkeypatch.setattr(monthly_billing.invoicing, "generate_invoices", generate)
        monkeypatch.setattr(monthly_billing.delivery, "send_period", send)
        monkeypatch.setattr(monthly_billing.owner_notify, "notify_owner", AsyncMock())
        async def worker():
            async with factory() as session:
                return await monthly_billing.run_due(session, now=now)
        first = None
        try:
            async with factory() as session:
                await session.execute(delete(MonthlyBillingRun).where(MonthlyBillingRun.period_start == period_start))
                await session.commit()
            first = asyncio.create_task(worker())
            await asyncio.wait_for(reached.wait(), 10)
            assert (await worker())["status"] == "already_running"
            release.set()
            assert (await first)["status"] == "completed"
            assert (await worker())["status"] == "completed"
            assert generate.await_count == send.await_count == 1
        finally:
            release.set()
            if first is not None:
                await first
            async with factory() as session:
                await session.execute(delete(MonthlyBillingRun).where(MonthlyBillingRun.period_start == period_start))
                await session.commit()
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.pg_contract
@requires_pg
def test_two_draft_senders_only_deliver_once(monkeypatch):
    async def run():
        engine, factory = make_engine()
        suffix = uuid.uuid4().hex[:12]
        calls = []
        panel_id = invoice_id = reseller_id = None
        async def fake_send(session, invoice_id, *, bot=None):
            calls.append(invoice_id)
            await asyncio.sleep(0.05)
            inv = await session.get(Invoice, invoice_id)
            inv.status = InvoiceStatus.sent
            inv.sent_at = dt.datetime.now(dt.timezone.utc)
            dl = DeliveryLog(invoice_id=invoice_id, reseller_id=inv.reseller_id,
                             kind=DeliveryKind.invoice, status=DeliveryStatus.sent)
            session.add(dl)
            await session.commit()
            return dl
        monkeypatch.setattr(delivery, "_send_invoice", fake_send)
        try:
            async with factory() as session:
                panel = Panel(key=f"scheduler-{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x", owner_uuid=suffix)
                session.add(panel)
                await session.flush()
                panel_id = panel.id
                reseller = Reseller(panel_id=panel_id, admin_uuid=suffix, name="race")
                session.add(reseller)
                await session.flush()
                reseller_id = reseller.id
                inv = Invoice(reseller_id=reseller_id, panel_id=panel_id, period_label="2091-06",
                              period_start=dt.date(2091, 6, 1), period_end=dt.date(2091, 6, 30))
                session.add(inv)
                await session.commit()
                invoice_id = inv.id
            async def worker():
                async with factory() as session:
                    # Prime the identity map BEFORE either worker finishes.
                    await session.get(Invoice, invoice_id)
                    return await delivery.send_draft_invoice(session, invoice_id)
            results = await asyncio.gather(worker(), worker())
            assert len(calls) == 1
            assert sum(r is None for r in results) == 1
        finally:
            async with factory() as session:
                if invoice_id is not None:
                    await session.execute(delete(DeliveryLog).where(DeliveryLog.invoice_id == invoice_id))
                    await session.execute(delete(Invoice).where(Invoice.id == invoice_id))
                if reseller_id is not None:
                    await session.execute(delete(Reseller).where(Reseller.id == reseller_id))
                if panel_id is not None:
                    await session.execute(delete(Panel).where(Panel.id == panel_id))
                await session.commit()
            await engine.dispose()
    asyncio.run(run())
