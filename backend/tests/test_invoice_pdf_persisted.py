"""Invoice PDFs/breakdown must come from the PERSISTED invoice lines, not a live recompute.

Regression for the bug where a customer's sent PDF showed less GB than the invoice text: the
per-node PDFs re-computed GB from current panel snapshots, so a user deleted AFTER the invoice was
issued silently shrank the PDF (e.g. text 85 GB, PDF 50 GB). The persisted InvoiceLine rows are
authoritative; the breakdown/PDF must total to the locked `inv.usage_gb` regardless of snapshots.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Invoice, InvoiceLine, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import invoice_pdf


def _run(coro_fn, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'pdf.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):
    panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    own = Reseller(panel_id=panel.id, admin_uuid="own-uuid", name="hosein")
    sub = Reseller(panel_id=panel.id, admin_uuid="sub-uuid", name="SubA",
                   parent_admin_uuid="own-uuid")
    s.add_all([own, sub])
    await s.flush()
    inv = Invoice(
        reseller_id=own.id, panel_id=panel.id,
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31), period_label="2026-05",
        usage_gb=84.567, amount_toman=169134, base_amount_toman=169134,
        status=InvoiceStatus.sent, sent_at=dt.datetime.now(dt.timezone.utc),
    )
    s.add(inv)
    await s.flush()
    # 2 "own" lines (creator = own), 1 sub line, 1 metered-extra line (no sub_reseller_name, but
    # added_by = the sub). NONE of these have a matching end_user_snapshot → a live recompute would
    # drop them; the persisted lines must still total 84.567.
    s.add_all([
        InvoiceLine(invoice_id=inv.id, end_user_uuid="u1", name="u1", usage_gb=40.0,
                    added_by_uuid="own-uuid", sub_reseller_name="hosein"),
        InvoiceLine(invoice_id=inv.id, end_user_uuid="u2", name="u2", usage_gb=10.567,
                    added_by_uuid="own-uuid", sub_reseller_name="hosein"),
        InvoiceLine(invoice_id=inv.id, end_user_uuid="u3", name="u3", usage_gb=30.0,
                    added_by_uuid="sub-uuid", sub_reseller_name="SubA"),
        InvoiceLine(invoice_id=inv.id, end_user_uuid="u4", name="u4 — مصرف اضافه/تمدید",
                    usage_gb=4.0, added_by_uuid="sub-uuid", sub_reseller_name=""),
    ])
    await s.commit()
    return own, inv


def test_breakdown_totals_from_persisted_lines(tmp_path):
    async def body(s):
        own, inv = await _seed(s)
        bd = await invoice_pdf.invoice_node_breakdown(s, inv, own)
        assert bd is not None
        # total equals the locked invoice usage_gb (not a snapshot recompute → would be 0 here)
        assert round(bd["total_gb"], 3) == 84.567
        assert round(bd["own"]["gb"], 3) == 50.567 and bd["own"]["users"] == 2
        subs = {x["name"]: x for x in bd["subs"]}
        # the sub group gets its base line + the metered-extra line (grouped by added_by_uuid)
        assert round(subs["SubA"]["gb"], 3) == 34.0 and subs["SubA"]["users"] == 2

    _run(body, tmp_path)


def test_node_pdfs_render_own_plus_each_sub_from_persisted_lines(tmp_path):
    async def body(s):
        own, inv = await _seed(s)
        docs = await invoice_pdf.render_invoice_node_pdfs(s, inv, own)
        # one PDF for own users + one per sub-reseller
        assert len(docs) == 2
        captions = " | ".join(c for _, c in docs)
        assert "کاربران خودتان" in captions and "SubA" in captions

    _run(body, tmp_path)


def test_sub_sharing_parent_name_is_not_merged(tmp_path):
    """Grouping is by the unique creator uuid, so a sub-reseller with the SAME name as its parent
    still gets its own group/PDF (not swallowed into 'own')."""
    async def body(s):
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        own = Reseller(panel_id=panel.id, admin_uuid="own-uuid", name="CompanyA")
        sub = Reseller(panel_id=panel.id, admin_uuid="sub-uuid", name="CompanyA",
                       parent_admin_uuid="own-uuid")
        s.add_all([own, sub])
        await s.flush()
        inv = Invoice(reseller_id=own.id, panel_id=panel.id,
                      period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
                      period_label="2026-05", usage_gb=30.0, amount_toman=1, base_amount_toman=1,
                      status=InvoiceStatus.sent)
        s.add(inv)
        await s.flush()
        s.add_all([
            InvoiceLine(invoice_id=inv.id, end_user_uuid="a", name="a", usage_gb=20.0,
                        added_by_uuid="own-uuid", sub_reseller_name="CompanyA"),
            InvoiceLine(invoice_id=inv.id, end_user_uuid="b", name="b", usage_gb=10.0,
                        added_by_uuid="sub-uuid", sub_reseller_name="CompanyA"),
        ])
        await s.commit()
        bd = await invoice_pdf.invoice_node_breakdown(s, inv, own)
        assert round(bd["own"]["gb"], 3) == 20.0        # only the parent's own line
        assert len(bd["subs"]) == 1 and round(bd["subs"][0]["gb"], 3) == 10.0
        assert round(bd["total_gb"], 3) == 30.0
        docs = await invoice_pdf.render_invoice_node_pdfs(s, inv, own)
        assert len(docs) == 2  # own + the same-named sub, separate

    _run(body, tmp_path)


def test_reconciles_to_locked_usage_gb_after_manual_edit(tmp_path):
    """A manual usage_gb edit that didn't touch lines is reconciled with an adjustment line, so the
    breakdown/PDF total equals the locked invoice amount shown in the text."""
    async def body(s):
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        own = Reseller(panel_id=panel.id, admin_uuid="own-uuid", name="R")
        s.add(own)
        await s.flush()
        inv = Invoice(reseller_id=own.id, panel_id=panel.id,
                      period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
                      period_label="2026-05", usage_gb=120.0, amount_toman=1, base_amount_toman=1,
                      status=InvoiceStatus.sent)  # usage_gb manually bumped to 120
        s.add(inv)
        await s.flush()
        # lines still total only 100 (5 × 20)
        for i in range(5):
            s.add(InvoiceLine(invoice_id=inv.id, end_user_uuid=f"u{i}", name=f"u{i}",
                              usage_gb=20.0, added_by_uuid="own-uuid", sub_reseller_name="R"))
        await s.commit()
        bd = await invoice_pdf.invoice_node_breakdown(s, inv, own)
        assert round(bd["total_gb"], 3) == 120.0        # reconciled to the locked usage_gb
        assert round(bd["own"]["gb"], 3) == 120.0        # +20 adjustment folded into own

    _run(body, tmp_path)
