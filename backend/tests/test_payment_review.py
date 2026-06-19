"""The owner-facing payment-review summary (bot) is complete: clickable reseller name, exact
invoice amount, explorer link, and a TON on-chain status line. The chain read is mocked."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/pay-review.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402

TXID = "2d4a27438f75a48db76cab554cfad0036db33b650f12f6a52c88dca8d23db326"


def test_payment_review_html_is_complete(tmp_path, monkeypatch):
    from app.bot import handlers
    from app.services import payments as payments_service

    async def fake_check(session, payment):
        return {
            "available": True, "received_ton": 17.4, "received_toman": 4_490_000,
            "invoice_toman": 4_480_000, "ton_rate": 258_000, "tolerance_pct": 5.0, "match": True,
        }

    monkeypatch.setattr(payments_service, "ton_deposit_check", fake_check)

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pr.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid="a", name="ali", bot_chat_id=999)
                s.add(r)
                await s.flush()
                inv = Invoice(
                    reseller_id=r.id, panel_id=panel.id, period_start=dt.date(2026, 6, 1),
                    period_end=dt.date(2026, 6, 30), period_label="2026-06",
                    usage_gb=10, amount_toman=4_480_000, status=InvoiceStatus.sent,
                )
                s.add(inv)
                await s.flush()
                pay = Payment(
                    reseller_id=r.id, invoice_id=inv.id, method=PaymentMethod.ton_txid,
                    chain="ton", status=PaymentStatus.pending, txid=TXID,
                )
                s.add(pay)
                await s.flush()

                html = await handlers._payment_review_html(s, pay)

                # Clickable reseller name → opens their Telegram profile.
                assert "tg://user?id=999" in html and "ali" in html
                # Exact invoice amount + the period.
                assert "4,480,000" in html and "2026-06" in html
                # Clickable explorer link to the right chain.
                assert f"https://tonscan.org/tx/{TXID}" in html
                # Gram (TON network) on-chain status with the matched verdict.
                assert "وضعیت شبکه" in html and "17.4 GRAM" in html and "مطابق فاکتور" in html
                # Tracking number present.
                assert "شمارهٔ پیگیری" in html
        finally:
            await engine.dispose()

    asyncio.run(run())
