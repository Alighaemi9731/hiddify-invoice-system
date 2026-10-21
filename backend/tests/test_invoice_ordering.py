"""Invoice displays follow billing months even when ids, amounts and status disagree."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import invoices, portal, reports
from app.api import payments as payment_api
from app.bot.handlers import common, intake, owner, storefront_setup, views
from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.core.security import get_current_subject
from app.models import FinancialRecord, Invoice, Panel, Payment, Reseller
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus
from app.services import payments, receipt_pdf


@pytest.fixture
async def data(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ordering.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
        session.add(panel)
        await session.flush()
        a = Reseller(panel_id=panel.id, admin_uuid="a", name="A", bot_chat_id=111)
        b = Reseller(panel_id=panel.id, admin_uuid="b", name="B", bot_chat_id=111)
        other = Reseller(panel_id=panel.id, admin_uuid="other", name="Other", bot_chat_id=222)
        session.add_all([a, b, other])
        await session.flush()
        specs = [
            (a, "2026-02", 10, InvoiceStatus.sent),
            (a, "2025-12", 900, InvoiceStatus.paid),
            (a, "2026-01", 500, InvoiceStatus.overdue),
            (b, "2026-01", 1000, InvoiceStatus.enforced),
            (b, "2025-11", 1, InvoiceStatus.canceled),
            (b, "2025-10", 1, InvoiceStatus.draft),
            (other, "2025-09", 1, InvoiceStatus.sent),
        ]
        invs = []
        for reseller, label, amount, status in specs:
            year, month = map(int, label.split("-"))
            inv = Invoice(
                panel_id=panel.id, reseller_id=reseller.id, period_label=label,
                period_start=dt.date(year, month, 1), period_end=dt.date(year, month, 28),
                amount_toman=amount, amount_usdt=1, status=status,
            )
            session.add(inv)
            await session.flush()
            invs.append(inv)
            session.add(FinancialRecord(
                invoice_id=inv.id, panel_key="p", reseller_name=reseller.name,
                period_label=label, amount_toman=amount, status=status.value,
            ))
        pay = Payment(
            reseller_id=a.id, invoice_id=invs[0].id,
            settled_invoice_ids=",".join(str(i.id) for i in invs[:4]),
            method=PaymentMethod.manual, status=PaymentStatus.confirmed, amount_toman=2410,
        )
        session.add(pay)
        await session.commit()
        monkeypatch.setattr(common, "SessionLocal", factory)
        yield SimpleNamespace(session=session, a=a, b=b, invs=invs, pay=pay,
                              ctx=ResellerContext(chat_id=111, resellers=[a, b]))
    await engine.dispose()


def _client(data):
    app = FastAPI()
    for router in (invoices.router, reports.router, portal.router, payment_api.router):
        app.include_router(router)
    app.dependency_overrides[get_session] = lambda: data.session
    app.dependency_overrides[get_current_subject] = lambda: "owner"
    app.dependency_overrides[get_current_reseller] = lambda: data.ctx
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_owner_defaults_pagination_filters_and_manual_sort(data):
    async with _client(data) as client:
        pages = [await client.get("/api/invoices", params={"limit": 2, "offset": n})
                 for n in range(0, 7, 2)]
        assert all(p.status_code == 200 and p.headers["x-total-count"] == "7" for p in pages)
        rows = [r for p in pages for r in p.json()]
        expected = sorted(data.invs, key=lambda i: (i.period_start, i.id))
        assert [r["id"] for r in rows] == [i.id for i in expected]
        filtered = await client.get("/api/invoices", params={"reseller_id": data.a.id})
        assert [r["period_label"] for r in filtered.json()] == ["2025-12", "2026-01", "2026-02"]
        manual = await client.get("/api/invoices", params={"sort": "amount", "order": "desc"})
        amounts = [r["amount_toman"] for r in manual.json()]
        assert amounts == sorted(amounts, reverse=True)
        fallback = await client.get("/api/invoices", params={"sort": "unknown"})
        assert [r["id"] for r in fallback.json()] == [i.id for i in expected]


async def test_financial_history_orders_month_then_id_across_pages(data):
    async with _client(data) as client:
        rows = []
        for offset in range(0, 7, 2):
            response = await client.get("/api/reports/financial-history",
                                        params={"limit": 2, "offset": offset})
            assert response.status_code == 200
            rows.extend(response.json())
        assert len(rows) == len({r["id"] for r in rows}) == 7
        assert [(r["period_label"], r["id"]) for r in rows] == sorted(
            (r["period_label"], r["id"]) for r in rows)


async def test_portal_history_and_pay_all_keep_month_order_and_scope(data):
    # Pending payment hides just one owed invoice from the pay-all set.
    pending = Payment(reseller_id=data.a.id, invoice_id=data.invs[0].id,
                      status=PaymentStatus.pending)
    data.session.add(pending)
    await data.session.flush()
    await payments._sync_settlements(data.session, pending)
    await data.session.commit()
    async with _client(data) as client:
        response = await client.get("/api/portal/invoices")
        assert response.status_code == 200
        assert [i["id"] for i in response.json()] == [data.invs[n].id for n in [4, 1, 2, 3, 0]]
        options = await client.get("/api/portal/pay/options-all")
        assert options.status_code == 200
        assert options.json()["invoice_ids"] == [data.invs[n].id for n in [2, 3]]


async def test_bot_invoice_and_payment_buttons_follow_month_order(data):
    expected = [data.invs[n].id for n in [2, 3, 0]]
    for handler, prefix in [(views._send_invoices, "inv:"), (views._send_pay, "payinv:")]:
        answer = AsyncMock()
        await handler(answer, 111, data.session)
        kb = answer.call_args.kwargs["reply_markup"]
        ids = [int(b.callback_data.split(":")[1]) for row in kb.inline_keyboard for b in row
               if b.callback_data and b.callback_data.startswith(prefix)]
        assert ids == expected
    selected = await storefront_setup._load_pay_invoices(
        data.session, {"pay_invoice_ids": [i.id for i in data.invs[:4]]})
    assert [i.id for i in selected] == [data.invs[n].id for n in [1, 2, 3, 0]]


async def test_owner_bot_history_and_payment_review_follow_month_order(data, monkeypatch):
    monkeypatch.setattr(common, "_is_owner_user", AsyncMock(return_value=True))
    cb = SimpleNamespace(data=f"orcinv:{data.a.id}", from_user=SimpleNamespace(id=999),
                         message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    await owner.cb_owner_reseller_invoices(cb)
    text = cb.message.answer.call_args.args[0]
    assert text.index("2025-12") < text.index("2026-01") < text.index("2026-02")
    for passed in ({}, {"invs": data.invs[:4]}):
        text = await intake._payment_review_html(data.session, data.pay, **passed)
        assert text.index("2025-12") < text.index("2026-01") < text.index("2026-02")


async def test_payment_details_receipt_and_labels_are_chronological(data, monkeypatch):
    expected = [data.invs[n].id for n in [1, 2, 3, 0]]
    async with _client(data) as client:
        response = await client.get("/api/payments")
        assert response.status_code == 200
        payment = response.json()[0]
        assert payment["invoice_ids"] == expected
        # Display sorting must preserve the actual primary invoice's summary/identity.
        assert payment["invoice_id"] == data.invs[0].id
        assert payment["invoice_period"] == "2026-02"
        assert payment["invoice_amount_toman"] == 10
        response = await client.get("/api/portal/payments")
        assert response.status_code == 200
        assert response.json()[0]["invoice_period"] == "2025-12، 2026-01، 2026-01، 2026-02"
    captured = {}

    def render(_path, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(receipt_pdf.pdf_service, "build_receipt_pdf", render)
    await receipt_pdf.render_payment_receipt_pdf(data.session, data.pay)
    assert [i["period"] for i in captured["invoices"]] == ["2025-12", "2026-01", "2026-01", "2026-02"]
    assert captured["amount_toman"] == 2410
    assert payments._periods_label(["2026-02", "2025-12", "2026-01"]) == "2025-12، 2026-01، 2026-02"
    assert data.pay.settled_invoice_ids == ",".join(str(i.id) for i in data.invs[:4])


async def test_long_history_keeps_every_month_reachable(data, monkeypatch):
    # Previously the portal silently stopped at 60, and bot history at 12. An ascending
    # sort must not replace those cutoffs with a permanent blind spot for newer debts.
    added = []
    for n in reversed(range(65)):
        year, month = 2019 + n // 12, n % 12 + 1
        inv = Invoice(
            panel_id=data.a.panel_id, reseller_id=data.a.id,
            period_label=f"{year}-{month:02d}", period_start=dt.date(year, month, 1),
            period_end=dt.date(year, month, 28), amount_toman=100, status=InvoiceStatus.sent,
        )
        data.session.add(inv)
        added.append(inv)
    await data.session.commit()
    async with _client(data) as client:
        response = await client.get("/api/portal/invoices")
        assert response.status_code == 200
        assert len(response.json()) == 70
        assert response.json()[-1]["id"] == data.invs[0].id
    answer = AsyncMock()
    await views._send_invoices(answer, 111, data.session)
    button_ids = [
        int(b.callback_data.split(":")[1]) for call in answer.call_args_list
        for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert button_ids == [i.id for i in reversed(added)] + [data.invs[n].id for n in [2, 3, 0]]
    assert all(len(c.kwargs["reply_markup"].inline_keyboard) <= 12 for c in answer.call_args_list)
    monkeypatch.setattr(common, "_is_owner_user", AsyncMock(return_value=True))
    cb = SimpleNamespace(data=f"orcinv:{data.a.id}", from_user=SimpleNamespace(id=999),
                         message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    await owner.cb_owner_reseller_invoices(cb)
    messages = [c.args[0] for c in cb.message.answer.call_args_list]
    assert sum(text.count("•") for text in messages) == 68
    assert "2019-01" in messages[0] and "2026-02" in messages[-1]
