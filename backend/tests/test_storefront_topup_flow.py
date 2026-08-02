"""The wallet top-up must not greet the customer in the middle of itself.

Reported from production: a customer taps «شارژ کیف پول», sends the amount — and the shop's
WELCOME message appears, mid-flow, before they have paid anything.

Cause: `sf_topup_amount` parked the flow with `state.set_state(None)` while waiting for the
customer to tap an inline button (credit code? which payment method?). Both re-dock middlewares
(`_sf_redock_after_flow`, `_sf_redock_after_cb_flow`) treat a state going active → None as "the
flow finished" and re-show the customer menu — and that menu opens with `welcome_text`. The flow
had not finished at all; it was parked.

Parking now uses a REAL state (`SF.topup_choice`), which also keeps the flow FSM-LOCKED so a stray
message gets the «✖️ انصراف» re-prompt instead of navigating away mid-payment.

These tests assert the CONDITION the middlewares actually branch on, so they fail again if any
future step in the flow goes back to parking on `None`.
"""
import asyncio

import pytest

from app.bot.storefront import handlers as H


def _fsm():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class _User:
    id = 555
    username = "cust"
    first_name = "Cust"
    is_bot = False


class _Msg:
    """Minimal stand-in for the pieces of `Message` the handler touches."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = _User()
        self.sent: list[str] = []

    async def answer(self, text, **kw):  # noqa: ANN001, ANN202, ARG002
        self.sent.append(text)


class _NullSession:
    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *a):  # noqa: ANN002, ANN204
        return False


def _patch_shop(monkeypatch, *, has_codes: bool):
    """Neutralise everything the handler needs from the DB / keyboards."""
    shop = type("SF", (), {"id": 1, "welcome_text": "به فروشگاه ما خوش آمدید"})()
    monkeypatch.setattr(H, "SessionLocal", lambda: _NullSession())
    monkeypatch.setattr(H, "_resolve", lambda *a, **k: _coro((shop, None, False)))
    monkeypatch.setattr(
        H.storefront_credit, "has_enabled_codes", lambda *a, **k: _coro(has_codes))
    monkeypatch.setattr(H.kb, "pay_methods_kb", lambda *a, **k: None)
    monkeypatch.setattr(H.kb, "topup_code_prompt_kb", lambda *a, **k: None)
    monkeypatch.setattr(H.kb, "flow_cancel_kb", lambda *a, **k: None)


async def _coro(value):
    return value


def _would_redock(before, after) -> bool:
    """Exactly the condition both re-dock middlewares branch on."""
    return before is not None and after is None


@pytest.mark.parametrize("has_codes", [True, False])
def test_entering_the_amount_never_looks_like_a_finished_flow(monkeypatch, has_codes):
    async def go():
        _patch_shop(monkeypatch, has_codes=has_codes)
        state = _fsm()
        await state.set_state(H.SF.topup_amount)
        before = await state.get_state()

        msg = _Msg("50000")
        await H.sf_topup_amount(msg, state, bot=None)

        after = await state.get_state()
        assert not _would_redock(before, after), (
            "the top-up parked on a cleared state — the re-dock middleware will greet the customer "
            "with the shop welcome text in the middle of their payment"
        )
        assert after == H.SF.topup_choice.state
        # The amount really was captured, so the flow can continue on the inline buttons.
        assert (await state.get_data())["topup_amount"] == 50000

    asyncio.run(go())


def test_a_bad_amount_keeps_the_flow_open(monkeypatch):
    """A non-numeric amount must re-prompt, not park and not end the flow."""
    async def go():
        _patch_shop(monkeypatch, has_codes=False)
        state = _fsm()
        await state.set_state(H.SF.topup_amount)

        msg = _Msg("سلام")
        await H.sf_topup_amount(msg, state, bot=None)

        assert await state.get_state() == H.SF.topup_amount.state
        assert msg.sent and "معتبر" in msg.sent[0]

    asyncio.run(go())


def test_amount_can_be_corrected_while_parked(monkeypatch):
    """Sending a new number while parked updates the amount instead of falling through to the
    catch-all (which would have re-shown the menu)."""
    async def go():
        _patch_shop(monkeypatch, has_codes=False)
        state = _fsm()
        await state.set_state(H.SF.topup_amount)
        await H.sf_topup_amount(_Msg("50000"), state, bot=None)
        assert await state.get_state() == H.SF.topup_choice.state

        await H.sf_topup_amount(_Msg("70000"), state, bot=None)
        assert (await state.get_data())["topup_amount"] == 70000
        assert await state.get_state() == H.SF.topup_choice.state

    asyncio.run(go())


def test_no_storefront_flow_parks_on_a_cleared_state():
    """Structural guard: `set_state(None)` inside a multi-step flow is the bug itself. Parking must
    use a real state so the re-dock middlewares can keep trusting active → None as 'flow ended'."""
    import inspect

    # Comments explaining the bug legitimately mention the call, so look at CODE only.
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(inspect.getsource(H).splitlines(), 1)
        if "set_state(None)" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "a flow parks on a cleared FSM state — the re-dock middleware will treat it as finished "
        f"and re-show the menu (welcome text) in the middle of the flow: {offenders}"
    )
