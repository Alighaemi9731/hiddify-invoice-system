"""Once a reseller is in the pay flow, tapping an invoice button again must NOT re-send the
payment-details message (the duplicate-spam bug). The flow stays locked until they send a
proof or /cancel. The guard runs before any DB access, so this test needs no database."""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/pay-lock.db")
os.environ.setdefault("SECRET_KEY", "k")


class _State:
    def __init__(self, current):
        self._cur = current

    async def get_state(self):
        return self._cur


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, *a, **k):
        self.sent.append((a, k))


class _CB:
    def __init__(self):
        self.data = "payinv:5"
        self.from_user = SimpleNamespace(id=123)
        self.message = _Msg()
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


def test_pay_invoice_button_locked_while_in_pay_state():
    from app.bot import handlers

    cb = _CB()
    asyncio.run(handlers.cb_pay_invoice(cb, _State("PayState:waiting")))

    # No new payment-details message was sent (the duplicate the user reported).
    assert cb.message.sent == []
    # Instead the user got a single alert telling them they're mid-payment.
    assert len(cb.answers) == 1
    assert cb.answers[0][1] is True  # show_alert
    assert "/cancel" in cb.answers[0][0]
