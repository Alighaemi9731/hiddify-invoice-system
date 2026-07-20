"""The SENT invoice text is lean: amount + a CTA to the «💳 پرداخت فاکتور» button — it must NOT
embed the wallet/card/USDT/TON details (those live on the pay-button path, with a live rate)."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/invtext.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus  # noqa: E402
from app.services import delivery, settings_service  # noqa: E402


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


def test_sent_invoice_text_is_lean(tmp_path):
    async def body(s):
        await settings_service.seed_defaults(s)
        # Enable USDT + a wallet, so if details were embedded the wallet WOULD appear.
        await settings_service.set_value(s, "pay_usdt_enabled", True)
        await settings_service.set_value(s, "usdt_bep20_address", "0xWALLETADDR1234")
        await settings_service.set_value(s, "pay_ton_enabled", True)
        await settings_service.set_value(s, "ton_wallet_address", "UQ_ton_addr")
        r = Reseller(panel_id=1, admin_uuid="A", name="R")
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        s.add(r)
        await s.flush()
        inv = Invoice(reseller_id=r.id, panel_id=1, period_start=dt.date(2026, 6, 1),
                      period_end=dt.date(2026, 6, 30), period_label="2026-06",
                      usage_gb=20, amount_toman=200000, amount_usdt=4, status=InvoiceStatus.sent)
        s.add(inv)
        await s.flush()

        text = await delivery.build_invoice_text(s, inv, r)

        # present: amount, the CTA to the pay button, and the invoice number header
        assert "مبلغ قابل پرداخت" in text and "200,000" in text
        assert "💳 پرداخت فاکتور" in text
        assert "🔢 شمارهٔ فاکتور" in text
        # absent: no embedded payment details
        assert "0xWALLETADDR1234" not in text
        assert "UQ_ton_addr" not in text
        assert "BEP-20" not in text
        assert "USDT" not in text
        assert "TON" not in text
        assert "{payment_instructions}" not in text  # placeholder fully replaced

    _run(body, tmp_path, "invtext.db")
