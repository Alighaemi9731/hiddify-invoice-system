"""#18 — payment receipt PDF: renders a valid Persian PDF from a confirmed payment, and is sent
(best-effort) to the reseller exactly ONCE on confirm (not again on re-confirm)."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/receipt.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus  # noqa: E402
from app.services import payments, receipt_pdf  # noqa: E402

S = InvoiceStatus
TX = "0x" + "b2" * 32


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


def _invoice(reseller_id, label="2026-01", amount=250000):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(reseller_id=reseller_id, panel_id=1, period_start=start, period_end=end,
                   period_label=label, usage_gb=10, amount_toman=amount, amount_usdt=5,
                   status=S.sent, sent_at=dt.datetime.now(dt.timezone.utc))


def test_render_payment_receipt_pdf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # receipt writes under data/receipts/ relative to cwd

    async def _noop(*a, **k):  # keep confirm's own receipt hook from rendering twice
        return None
    monkeypatch.setattr(payments, "_send_receipt", _noop)

    async def body(s):
        r = Reseller(panel_id=1, admin_uuid="u1", name="نمایندهٔ تست", bot_chat_id=555)
        s.add(r)
        await s.flush()
        a, b = _invoice(r.id, "2026-01", 250000), _invoice(r.id, "2026-02", 120000)
        s.add_all([a, b])
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[a.id, b.id], txid=TX, chain="bsc")
        await payments.confirm_manually(s, res.payment.id)
        await s.refresh(res.payment)

        path, name = await receipt_pdf.render_payment_receipt_pdf(s, res.payment)
        assert os.path.exists(path) and os.path.getsize(path) > 1500   # a real PDF
        with open(path, "rb") as fh:
            assert fh.read(5) == b"%PDF-"                              # valid header
        assert name.endswith(".pdf") and "رسید" in name

    _run(body, tmp_path, "receipt.db")


def test_receipt_sent_once_on_confirm(tmp_path, monkeypatch):
    calls: list[int] = []

    async def fake_send_receipt(session, reseller, payment):
        calls.append(payment.id)
    monkeypatch.setattr(payments, "_send_receipt", fake_send_receipt)

    async def body(s):
        r = Reseller(panel_id=1, admin_uuid="u1", name="R", bot_chat_id=555)
        s.add(r)
        await s.flush()
        inv = _invoice(r.id)
        s.add(inv)
        await s.commit()
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=TX, chain="bsc")
        await payments.confirm_manually(s, res.payment.id)
        assert calls == [res.payment.id]                    # sent once on first confirm
        await payments.confirm_manually(s, res.payment.id)  # re-confirm (already confirmed)
        assert calls == [res.payment.id]                    # NOT sent again

    _run(body, tmp_path, "receipt2.db")
