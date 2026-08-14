"""Server-side pagination contracts (2026-07-22 audit, B4 / Wave 3).

Every owner list endpoint must: report the FULL filtered count in `X-Total-Count`,
honor limit/offset windows with a deterministic tiebreaker (no duplicated/dropped rows
across pages), and filter/search on the server. `top_level_only` reseller filtering must
happen in SQL — the old post-filter shrank pages unpredictably.
"""
import asyncio
import datetime as dt

from fastapi import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.invoices import list_invoices
from app.api.payments import list_payments
from app.api.reports import financial_history
from app.api.resellers import list_resellers
from app.core.db import Base
from app.models import FinancialRecord, Invoice, Panel, Payment, Reseller
from app.models.enums import InvoiceStatus, PanelStatus, PaymentStatus


async def _mk(tmp_path, name):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session):
    now = dt.datetime.now(dt.timezone.utc)
    panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="own",
                  enabled=True, status=PanelStatus.ok, last_synced_at=now)
    session.add(panel)
    await session.flush()
    owner = Reseller(panel_id=panel.id, admin_uuid="own", name="Owner", is_owner=True,
                     last_seen_at=now)
    roots = [
        Reseller(panel_id=panel.id, admin_uuid=f"r{i}", parent_admin_uuid="own",
                 name=f"Root{i:02d}", last_seen_at=now)
        for i in range(7)
    ]
    subs = [
        Reseller(panel_id=panel.id, admin_uuid=f"s{i}", parent_admin_uuid="r0",
                 name=f"Sub{i}", last_seen_at=now)
        for i in range(3)
    ]
    session.add_all([owner, *roots, *subs])
    await session.flush()
    # one invoice per (reseller, period): 7 in July + 5 in June = 12 total
    invoices = [
        Invoice(
            panel_id=panel.id, reseller_id=roots[i % 7].id,
            period_label="2026-07" if i < 7 else "2026-06",
            period_start=dt.date(2026, 7 if i < 7 else 6, 1),
            period_end=dt.date(2026, 7 if i < 7 else 6, 30), usage_gb=10 + i,
            amount_toman=1000 * (i + 1), status=InvoiceStatus.sent,
        )
        for i in range(12)
    ]
    session.add_all(invoices)
    await session.flush()
    # Distinctive per-payment hashes (BSC shape, lowercase as submit stores them) so a search
    # for one fragment can only hit one row; payment 1 gets a mixed-case TON-style hash.
    txids = ["0x" + f"{i:02d}" * 32 for i in range(9)]
    txids[1] = "AbCdEf0123456789AbCdEf0123456789AbCd"
    session.add_all([
        Payment(reseller_id=roots[i % 7].id, invoice_id=invoices[i].id,
                status=PaymentStatus.pending if i % 2 else PaymentStatus.confirmed,
                amount_usdt=0, amount_toman=500 * (i + 1), txid=txids[i])
        for i in range(9)
    ])
    session.add_all([
        FinancialRecord(invoice_id=1000 + i, panel_key="p", reseller_name=f"Root{i % 7:02d}",
                        reseller_admin_uuid=f"r{i % 7}", period_label="2026-07",
                        usage_gb=1, price_per_gb=1000, amount_toman=100 * (i + 1),
                        status="sent")
        for i in range(11)
    ])
    await session.commit()
    return panel, roots


def test_invoices_pagination_totals_and_stability(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "inv.db")
        try:
            async with Session() as s:
                await _seed(s)
                resp = Response()
                page1 = await list_invoices(resp, period="2026-07", limit=3, offset=0,
                                            sort="amount_toman", order="asc", session=s)
                assert resp.headers["x-total-count"] == "7"
                assert len(page1) == 3
                page2 = await list_invoices(Response(), period="2026-07", limit=3, offset=3,
                                            sort="amount_toman", order="asc", session=s)
                page3 = await list_invoices(Response(), period="2026-07", limit=3, offset=6,
                                            sort="amount_toman", order="asc", session=s)
                ids = [i.id for i in page1 + page2 + page3]
                assert len(ids) == 7 and len(set(ids)) == 7  # no dup/drop across pages
                amounts = [i.amount_toman for i in page1 + page2 + page3]
                assert amounts == sorted(amounts)
                # unfiltered total spans both periods
                r_all = Response()
                await list_invoices(r_all, limit=100, offset=0, session=s)
                assert r_all.headers["x-total-count"] == "12"
                # server search decodes the public 8-digit invoice number (Persian digits ok)
                from app.core.codes import invoice_code
                target = page1[0]
                code_fa = "".join("۰۱۲۳۴۵۶۷۸۹"[int(c)] for c in invoice_code(target.id))
                r = Response()
                found = await list_invoices(r, q=code_fa, limit=100, offset=0, session=s)
                assert [i.id for i in found] == [target.id]
                r = Response()
                by_name = await list_invoices(r, q="Root01", limit=100, offset=0, session=s)
                assert int(r.headers["x-total-count"]) >= 1
                assert all(i.reseller_name == "Root01" for i in by_name)
                # offset beyond the end: empty page, total intact
                r = Response()
                empty = await list_invoices(r, period="2026-07", limit=5, offset=50, session=s)
                assert empty == [] and r.headers["x-total-count"] == "7"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_payments_pagination_and_search(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "pay.db")
        try:
            async with Session() as s:
                await _seed(s)
                r = Response()
                rows = await list_payments(r, limit=4, offset=0, session=s)
                assert r.headers["x-total-count"] == "9" and len(rows) == 4
                r2 = Response()
                rest = await list_payments(r2, limit=100, offset=4, session=s)
                assert len(rest) == 5
                assert len({p.id for p in rows} | {p.id for p in rest}) == 9
                # tracking-number search (#id with Persian digits) and name search
                from app.core.codes import payment_code
                target = rows[0]
                r3 = Response()
                hit = await list_payments(r3, q=f"#{payment_code(target.id)}", limit=100, offset=0, session=s)
                assert [p.id for p in hit] == [target.id]
                # raw internal id also works (short numeric)
                r3b = Response()
                hit2 = await list_payments(r3b, q=str(target.id), limit=100, offset=0, session=s)
                assert target.id in [p.id for p in hit2]
                r4 = Response()
                await list_payments(r4, q="Root02", limit=100, offset=0, session=s)
                assert int(r4.headers["x-total-count"]) >= 1
                # status filter + total reflects the filter
                r5 = Response()
                pend = await list_payments(r5, status=PaymentStatus.pending, limit=100, offset=0, session=s)
                assert int(r5.headers["x-total-count"]) == len(pend)
                # txid search: the full hash, and the 14-char prefix the table shows
                r6 = Response()
                full = await list_payments(r6, q="0x" + "03" * 32, limit=100, offset=0, session=s)
                assert [p.txid for p in full] == ["0x" + "03" * 32]
                r7 = Response()
                frag = await list_payments(r7, q="0x040404040404", limit=100, offset=0, session=s)
                assert [p.txid for p in frag] == ["0x" + "04" * 32]
                # case-insensitive: BSC hashes are stored lowercased, TON base64 keeps its case
                r8 = Response()
                ton = await list_payments(r8, q="abcdef0123456789", limit=100, offset=0, session=s)
                assert [p.txid for p in ton] == ["AbCdEf0123456789AbCdEf0123456789AbCd"]
                # a term shorter than the 6-char floor never probes the hash column, so a short
                # name/number search can't drag in unrelated payments
                r9 = Response()
                short = await list_payments(r9, q="0505", limit=100, offset=0, session=s)
                assert short == [] and r9.headers["x-total-count"] == "0"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_financial_history_offset_total(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "fin.db")
        try:
            async with Session() as s:
                await _seed(s)
                r = Response()
                page1 = await financial_history(r, limit=4, offset=0, session=s)
                assert r.headers["x-total-count"] == "11" and len(page1) == 4
                page2 = await financial_history(Response(), limit=4, offset=4, session=s)
                page3 = await financial_history(Response(), limit=4, offset=8, session=s)
                ids = [x["id"] for x in page1 + page2 + page3]
                assert len(ids) == 11 and len(set(ids)) == 11
                r2 = Response()
                filtered = await financial_history(r2, q="Root03", limit=100, offset=0, session=s)
                assert int(r2.headers["x-total-count"]) == len(filtered) > 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_resellers_top_level_pagination_in_sql(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "res.db")
        try:
            async with Session() as s:
                await _seed(s)  # 7 roots + 3 subs (+1 owner)
                r = Response()
                page1 = await list_resellers(r, top_level_only=True, limit=4, offset=0,
                                             session=s)
                assert r.headers["x-total-count"] == "7"
                assert len(page1) == 4  # the OLD post-filter made this window shrink
                page2 = await list_resellers(Response(), top_level_only=True, limit=4,
                                             offset=4, session=s)
                assert len(page2) == 3
                names = {x.name for x in page1} | {x.name for x in page2}
                assert names == {f"Root{i:02d}" for i in range(7)}  # subs excluded, no drops
        finally:
            await engine.dispose()
    asyncio.run(run())
