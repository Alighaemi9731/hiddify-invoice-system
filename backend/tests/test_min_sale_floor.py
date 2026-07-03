"""Minimum-sale floor: first-invoiced-month exemption (applies from the 2nd month) + the
transparent floor-explanation text on the delivered invoice."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/floor.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus  # noqa: E402
from app.services import delivery, invoicing  # noqa: E402


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _invoice(reseller_id, panel_id, label, status):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(reseller_id=reseller_id, panel_id=panel_id, period_start=start,
                   period_end=end, period_label=label, usage_gb=5, amount_toman=5000,
                   base_amount_toman=5000, amount_usdt=0, status=status)


async def _seed_reseller(s):
    p = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    return p, r


FLOOR = 300_000


def test_first_invoiced_month_is_exempt(tmp_path):
    async def body(s):
        p, r = await _seed_reseller(s)
        # No prior invoice → first month → floor disabled (returns 0).
        eff = await invoicing._effective_min_sale(s, r.id, dt.date(2026, 6, 1), FLOOR)
        assert eff == 0
    _run(body, tmp_path, "f1.db")


def test_floor_applies_from_second_month(tmp_path):
    async def body(s):
        p, r = await _seed_reseller(s)
        # A DELIVERED (sent) invoice exists for an earlier month → not the first → floor applies.
        s.add(_invoice(r.id, p.id, "2026-05", InvoiceStatus.sent))
        await s.commit()
        eff = await invoicing._effective_min_sale(s, r.id, dt.date(2026, 6, 1), FLOOR)
        assert eff == FLOOR
    _run(body, tmp_path, "f2.db")


def test_prior_draft_or_canceled_does_not_count(tmp_path):
    async def body(s):
        p, r = await _seed_reseller(s)
        # A prior DRAFT (never billed) and a prior CANCELED must NOT count as a first invoice.
        s.add(_invoice(r.id, p.id, "2026-04", InvoiceStatus.draft))
        s.add(_invoice(r.id, p.id, "2026-05", InvoiceStatus.canceled))
        await s.commit()
        eff = await invoicing._effective_min_sale(s, r.id, dt.date(2026, 6, 1), FLOOR)
        assert eff == 0
    _run(body, tmp_path, "f3.db")


def test_zero_floor_stays_zero(tmp_path):
    async def body(s):
        p, r = await _seed_reseller(s)
        s.add(_invoice(r.id, p.id, "2026-05", InvoiceStatus.sent))
        await s.commit()
        assert await invoicing._effective_min_sale(s, r.id, dt.date(2026, 6, 1), 0) == 0
    _run(body, tmp_path, "f4.db")


def test_floor_text_shows_real_floor_and_final(tmp_path):
    async def body(s):
        p, r = await _seed_reseller(s)
        inv = Invoice(reseller_id=r.id, panel_id=p.id, period_start=dt.date(2026, 6, 1),
                      period_end=dt.date(2026, 6, 30), period_label="2026-06",
                      usage_gb=120, base_amount_toman=120_000, min_sale_toman=300_000,
                      amount_toman=300_000, amount_usdt=0, floor_applied=True,
                      status=InvoiceStatus.sent)
        s.add(inv)
        await s.commit()
        text = await delivery.build_invoice_text(s, inv, r)
        assert "حداقل فروش ماهانه" in text
        assert "120,000" in text        # real sale
        assert "300,000" in text        # floor + final
        assert "PDF" in text            # points the reseller to the accurate PDF
    _run(body, tmp_path, "f5.db")
