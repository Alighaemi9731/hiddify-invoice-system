"""Deleting a panel is a large, irreversible-feeling action — it must state its scope first.

Panel deletion already WORKS (SQLAlchemy's `all, delete-orphan` cascade on `Panel.resellers`
unwinds resellers → invoices → lines, and the database FK cascades cover snapshots/meters/payments)
— this was verified directly against PostgreSQL 16, correcting an earlier belief that a billed panel
could not be deleted at all. The durable ledger (`financial_records`, FK-free) survives, so the
accounting history is never lost.

What was missing is FRICTION proportional to the blast radius: deleting a panel that has been billed
permanently removes hundreds of invoice rows behind a generic «delete?» prompt. These tests pin the
confirmation — real invoices (or a cross-panel payment) require an explicit `force`, while a
drafts-only or empty panel still deletes freely. The mechanism itself is deliberately NOT re-tested
here beyond "the panel and its invoices are gone"; the `pg_contract` case proves the full cascade on
the engine that actually enforces it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/paneldel.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import panels as panels_api  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Invoice,
    InvoiceLine,
    Panel,
    Reseller,
)
from app.models.enums import InvoiceStatus  # noqa: E402


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _invoice(s, reseller_id, panel_id, status, month):
    inv = Invoice(
        reseller_id=reseller_id, panel_id=panel_id,
        period_start=dt.date(2026, month, 1), period_end=dt.date(2026, month, 28),
        period_label=f"2026-{month:02d}", usage_gb=10, amount_toman=100_000,
        amount_usdt=1, status=status,
    )
    s.add(inv)
    await s.flush()
    s.add(InvoiceLine(invoice_id=inv.id, end_user_uuid=f"u{inv.id}", name="u",
                      usage_gb=10, sub_reseller_name="R"))
    return inv


async def _seed(s, *, drafts=0, sent=0, paid=0):
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    r = Reseller(panel_id=panel.id, admin_uuid="A", name="R", bot_chat_id=111)
    s.add(r)
    await s.flush()
    s.add(EndUserSnapshot(panel_id=panel.id, user_uuid="u-1", name="u",
                          added_by_uuid="A", enable=True, is_active=True))
    month = 1
    for _ in range(drafts):
        await _invoice(s, r.id, panel.id, InvoiceStatus.draft, month)
        month += 1
    for _ in range(sent):
        await _invoice(s, r.id, panel.id, InvoiceStatus.sent, month)
        month += 1
    for _ in range(paid):
        await _invoice(s, r.id, panel.id, InvoiceStatus.paid, month)
        month += 1
    await s.commit()
    return panel, r


def test_a_drafts_only_panel_deletes_without_a_prompt():
    """Drafts are throwaway (never sent, no money, discarded routinely) — they must not add
    friction. This was the real production case: a panel whose only invoices were 10 drafts."""
    async def body(s):
        panel, _r = await _seed(s, drafts=10)
        await panels_api.delete_panel(panel.id, force=False, session=s)
        assert await s.get(Panel, panel.id) is None

    _run(body)


def test_an_empty_panel_deletes_without_a_prompt():
    async def body(s):
        panel, _r = await _seed(s)
        await panels_api.delete_panel(panel.id, force=False, session=s)
        assert await s.get(Panel, panel.id) is None

    _run(body)


def test_real_invoices_require_confirmation_and_the_message_states_the_scope():
    async def body(s):
        panel, _r = await _seed(s, drafts=3, sent=2, paid=4)
        with pytest.raises(HTTPException) as ei:
            await panels_api.delete_panel(panel.id, force=False, session=s)
        assert ei.value.status_code == 409
        detail = ei.value.detail
        assert "6" in detail          # 6 real invoices (drafts excluded)
        assert "4" in detail and "2" in detail   # paid / unpaid split
        assert "تاریخچهٔ مالی" in detail          # says the ledger survives
        # Refusing changed nothing.
        assert await s.get(Panel, panel.id) is not None
        assert len((await s.execute(
            select(Invoice.id).where(Invoice.panel_id == panel.id))).all()) == 9

    _run(body)


def test_force_deletes_a_billed_panel_and_its_invoices():
    async def body(s):
        panel, _r = await _seed(s, drafts=2, sent=1, paid=3)
        await panels_api.delete_panel(panel.id, force=True, session=s)
        assert await s.get(Panel, panel.id) is None
        assert len((await s.execute(
            select(Invoice.id).where(Invoice.panel_id == panel.id))).all()) == 0

    _run(body)


def test_a_cross_panel_payment_still_blocks_and_names_the_stranded_invoice():
    """The v1.93.0 guard must keep firing: a payment settling ANOTHER panel's invoice is a distinct
    reason to confirm, because that invoice would be left `paid` with no evidence behind it."""
    async def body(s):
        from app.services import payments

        pa = Panel(key="pa", host="a.invalid", proxy_path_enc="x", owner_uuid="o")
        pb = Panel(key="pb", host="b.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add_all([pa, pb])
        await s.flush()
        ra = Reseller(panel_id=pa.id, admin_uuid="A", name="RA", bot_chat_id=555)
        rb = Reseller(panel_id=pb.id, admin_uuid="B", name="RB", bot_chat_id=555)
        s.add_all([ra, rb])
        await s.flush()
        ia = await _invoice(s, ra.id, pa.id, InvoiceStatus.sent, 3)
        ib = await _invoice(s, rb.id, pb.id, InvoiceStatus.sent, 3)
        await s.commit()

        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid="0x" + "ab" * 32)
        assert res.status == "ok", res.user_message

        with pytest.raises(HTTPException) as ei:
            await panels_api.delete_panel(pa.id, force=False, session=s)
        assert ei.value.status_code == 409
        assert str(ib.id) in ei.value.detail

    _run(body)


def test_a_forced_cross_panel_delete_archives_the_survivor_first():
    """When forced, the surviving panel's invoice keeps its money facts in the FK-free ledger."""
    async def body(s):
        from app.models import FinancialRecord
        from app.services import payments

        pa = Panel(key="pa", host="a.invalid", proxy_path_enc="x", owner_uuid="o")
        pb = Panel(key="pb", host="b.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add_all([pa, pb])
        await s.flush()
        ra = Reseller(panel_id=pa.id, admin_uuid="A", name="RA", bot_chat_id=555)
        rb = Reseller(panel_id=pb.id, admin_uuid="B", name="RB", bot_chat_id=555)
        s.add_all([ra, rb])
        await s.flush()
        ia = await _invoice(s, ra.id, pa.id, InvoiceStatus.sent, 3)
        ib = await _invoice(s, rb.id, pb.id, InvoiceStatus.sent, 3)
        await s.commit()
        await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid="0x" + "cd" * 32)

        await panels_api.delete_panel(pa.id, force=True, session=s)

        assert await s.get(Panel, pa.id) is None
        row = (await s.execute(select(FinancialRecord).where(
            FinancialRecord.invoice_id == ib.id))).scalar_one_or_none()
        assert row is not None, "the surviving invoice's money facts were not archived"

    _run(body)


def test_a_missing_panel_is_a_404():
    async def body(s):
        with pytest.raises(HTTPException) as ei:
            await panels_api.delete_panel(99999, force=True, session=s)
        assert ei.value.status_code == 404

    _run(body)


# ── PG contract: the cascade this endpoint relies on must actually clean everything ────────────
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


@pytest.mark.pg_contract
@requires_pg
def test_pg_force_delete_cleans_the_whole_subtree():
    """SQLite does not enforce the database FK cascades, so PostgreSQL is the only place the full
    cleanup (snapshots, meters, payments) can be proven — and the only place the delete could ever
    have failed. Seeds the production shape: a billed panel with a user and a confirmed payment."""
    async def run():
        from app.models import Payment, PaymentSettlement
        from app.models.enums import PaymentMethod, PaymentStatus

        engine, Session = make_engine()
        try:
            async with Session() as s:
                await s.execute(Panel.__table__.delete().where(Panel.key == "pgpdel"))
                await s.commit()
            async with Session() as s:
                p = Panel(key="pgpdel", host="pgpdel.invalid", proxy_path_enc="x", owner_uuid="o")
                s.add(p)
                await s.flush()
                r = Reseller(panel_id=p.id, admin_uuid="PGPDEL-A", name="R")
                s.add(r)
                await s.flush()
                s.add(EndUserSnapshot(panel_id=p.id, user_uuid="pgpdel-u", name="u",
                                      added_by_uuid="PGPDEL-A", enable=True, is_active=True))
                inv = await _invoice(s, r.id, p.id, InvoiceStatus.paid, 3)
                pay = Payment(reseller_id=r.id, invoice_id=inv.id,
                              method=PaymentMethod.usdt_txid, status=PaymentStatus.confirmed,
                              chain="bsc", settled_invoice_ids=str(inv.id), amount_usdt=1)
                s.add(pay)
                await s.flush()
                s.add(PaymentSettlement(payment_id=pay.id, invoice_id=inv.id))
                await s.commit()
                pid, rid = p.id, r.id

            async with Session() as s:
                await panels_api.delete_panel(pid, force=True, session=s)

            async with Session() as s:
                assert await s.get(Panel, pid) is None
                for model, col in ((Reseller, Reseller.panel_id), (Invoice, Invoice.panel_id),
                                   (EndUserSnapshot, EndUserSnapshot.panel_id)):
                    left = len((await s.execute(select(model.id).where(col == pid))).all())
                    assert left == 0, f"{model.__name__} left {left} orphan(s)"
                assert len((await s.execute(
                    select(Payment.id).where(Payment.reseller_id == rid))).all()) == 0
                # The money fact survives in the FK-free ledger.
                from app.models import FinancialRecord
                await s.execute(FinancialRecord.__table__.delete().where(
                    FinancialRecord.panel_key == "pgpdel"))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
