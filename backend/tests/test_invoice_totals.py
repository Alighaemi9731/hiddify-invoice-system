"""H03 — billing totals unification.

Generation (`_persist_bundle`) and «بازمحاسبه از روی پنل» (`recompute_invoice`) share ONE
totals computation (`_compute_totals`/`_write_lines`):
- recompute keeps the storefront monthly fee AND its invoice line (it used to silently
  drop both, discounting the invoice);
- a zero-usage month still bills the flat storefront fee (fee-only invoice) and that
  fee-only draft survives regeneration's reconciliation;
- persist and recompute produce identical figures for identical inputs;
- zero usage + zero fee is still skipped.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/invtotals.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Invoice,
    InvoiceLine,
    Panel,
    Reseller,
    StorefrontBot,
)
from app.services import invoicing  # noqa: E402
from app.services.periods import Period  # noqa: E402

PERIOD = Period(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
FEE = 200_000


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
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


async def _seed(s, *, users_gb=(), fee=FEE, with_bot=True):
    now = dt.datetime.now(dt.timezone.utc)
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc=crypto.encrypt("x"),
              owner_uuid="o", last_synced_at=now)
    s.add(p)
    await s.flush()
    r = Reseller(
        panel_id=p.id, admin_uuid="A1", name="Ali", last_seen_at=now,
        storefront_enabled=with_bot, storefront_monthly_fee_toman=fee,
    )
    s.add(r)
    await s.flush()
    if with_bot:
        s.add(StorefrontBot(
            reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("123:abc") or "",
            bot_telegram_id=991, enabled=True,
        ))
    for i, gb in enumerate(users_gb):
        s.add(EndUserSnapshot(
            panel_id=p.id, user_uuid=f"u{i}", name=f"user{i}", added_by_uuid="A1",
            usage_limit_gb=gb, start_date=dt.date(2026, 6, 10), enable=True,
            last_synced_at=now,
        ))
    await s.commit()
    return p, r


async def _invoice_for(s, reseller_id):
    return (
        await s.execute(select(Invoice).where(Invoice.reseller_id == reseller_id))
    ).scalars().one_or_none()


async def _lines_for(s, invoice_id):
    return (
        await s.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id))
    ).scalars().all()


def test_recompute_keeps_storefront_fee_and_fee_line(tmp_path):
    async def body(s):
        p, r = await _seed(s, users_gb=(10,))
        summary = await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        assert summary.created == 1
        inv = await _invoice_for(s, r.id)
        amount_before = float(inv.amount_toman)
        assert amount_before == 10 * 1000 + FEE  # usage + fee
        fee_lines = [ln for ln in await _lines_for(s, inv.id)
                     if str(ln.end_user_uuid).startswith("storefront_fee_")]
        assert len(fee_lines) == 1

        result = await invoicing.recompute_invoice(s, inv, sync_first=False)
        assert result["found"] is True
        await s.refresh(inv)
        assert float(inv.amount_toman) == amount_before          # fee NOT dropped
        fee_lines = [ln for ln in await _lines_for(s, inv.id)
                     if str(ln.end_user_uuid).startswith("storefront_fee_")]
        assert len(fee_lines) == 1                               # fee line re-written

    _run(body, tmp_path, "t1.db")


def test_zero_usage_month_generates_fee_only_invoice(tmp_path):
    async def body(s):
        p, r = await _seed(s, users_gb=())
        summary = await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        assert summary.created == 1 and summary.zero_skipped == 0
        inv = await _invoice_for(s, r.id)
        assert inv is not None
        assert float(inv.amount_toman) == FEE
        assert float(inv.usage_gb) == 0
        assert inv.users_count == 0                              # fee line isn't a user
        lines = await _lines_for(s, inv.id)
        assert len(lines) == 1
        assert str(lines[0].end_user_uuid).startswith("storefront_fee_")

    _run(body, tmp_path, "t2.db")


def test_reconcile_keeps_fee_only_draft(tmp_path):
    async def body(s):
        p, r = await _seed(s, users_gb=())
        await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        summary2 = await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        assert summary2.reconciled_zero == 0                     # not reconcile-deleted
        assert summary2.updated == 1
        inv = await _invoice_for(s, r.id)
        assert inv is not None and float(inv.amount_toman) == FEE

    _run(body, tmp_path, "t3.db")


def test_persist_and_recompute_agree(tmp_path):
    async def body(s):
        p, r = await _seed(s, users_gb=(10, 5))
        await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        inv = await _invoice_for(s, r.id)
        before = (float(inv.amount_toman), float(inv.usage_gb), inv.users_count,
                  float(inv.base_amount_toman), len(await _lines_for(s, inv.id)))
        await invoicing.recompute_invoice(s, inv, sync_first=False)
        await s.refresh(inv)
        after = (float(inv.amount_toman), float(inv.usage_gb), inv.users_count,
                 float(inv.base_amount_toman), len(await _lines_for(s, inv.id)))
        assert before == after

    _run(body, tmp_path, "t4.db")


def test_zero_usage_zero_fee_still_skipped(tmp_path):
    async def body(s):
        p, r = await _seed(s, users_gb=(), with_bot=False, fee=None)
        summary = await invoicing.generate_invoices(s, PERIOD, panel_id=p.id)
        assert summary.created == 0 and summary.zero_skipped == 1
        assert await _invoice_for(s, r.id) is None

    _run(body, tmp_path, "t5.db")
