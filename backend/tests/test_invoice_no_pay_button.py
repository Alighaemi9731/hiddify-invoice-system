"""The sent invoice no longer carries an inline «💳 پرداخت فاکتور» button: paying is ONLY via the
menu pay flow, whose first option settles all outstanding debt at once. So `send_invoice_content`
must call `bot.send_message` WITHOUT a `reply_markup`, and the removed keyboard helper is gone."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/nopaybtn.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.services import delivery  # noqa: E402


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, chat_id, text, **kw):  # noqa: ANN001, ANN003
        self.calls.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=len(self.calls))


def test_send_invoice_content_has_no_pay_button(monkeypatch):
    async def _no_breakdown(*_a, **_k):
        raise RuntimeError("breakdown skipped in test")

    async def _no_pdfs(*_a, **_k):
        return []

    monkeypatch.setattr(delivery.invoice_pdf, "invoice_node_breakdown", _no_breakdown)
    monkeypatch.setattr(delivery, "_render_invoice_pdfs", _no_pdfs)

    bot = _FakeBot()
    inv = SimpleNamespace(id=7, status="sent")
    reseller = SimpleNamespace(name="R")

    async def go():
        ids = await delivery.send_invoice_content(
            session=None, bot=bot, chat_id=123, inv=inv, reseller=reseller, text="TXT"
        )
        return ids

    ids = asyncio.run(go())

    assert ids == [1]
    assert len(bot.calls) == 1
    # The invoice message carries NO inline keyboard (paying is menu-only now).
    assert bot.calls[0].get("reply_markup") is None
    assert "TXT" in bot.calls[0]["text"]


def test_pay_invoice_button_helper_removed():
    from app.bot import keyboards
    assert not hasattr(keyboards, "pay_invoice_button")
