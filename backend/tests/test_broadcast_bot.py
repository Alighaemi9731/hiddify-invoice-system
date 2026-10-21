"""Owner bot segmentation and confirmation with real FSM storage; no Telegram network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.handlers import broadcast as bot
from app.bot.handlers import common
from app.bot.handlers.common import BroadcastState
from app.bot.handlers.support import cmd_broadcast, on_broadcast_text
from tests.test_bot_ux import _fsm


def _message(text="test"):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock())


def _cb(data):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=1), message=_message(), answer=AsyncMock())


def _patch_session(monkeypatch, owner=True):
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(common, "SessionLocal", Session)
    monkeypatch.setattr(common, "_is_owner_user", AsyncMock(return_value=owner))


def _result(token="same"):
    return {"total": 1, "unregistered": 0, "matched": 1, "matched_accounts": 1,
            "duplicate_chats": 0, "debt_toman": 50, "recipients": [], "skipped": [], "preview_token": token}


def test_bot_month_and_panel_filters_reach_shared_resolver(monkeypatch):
    async def go():
        _patch_session(monkeypatch)
        state = _fsm()
        await state.set_state(BroadcastState.configuring)
        await state.update_data(audience="overdue", filters={"panel_ids": [1, 3]})
        await bot.cb_broadcast_filters(_cb("bcf:monthset:single:2026-07"), state)
        data = await state.get_data()
        assert data["filters"]["period_from"] == data["filters"]["period_to"] == "2026-07"
        assert data["filters"]["panel_ids"] == [1, 3]
        resolver = AsyncMock(return_value=_result())
        monkeypatch.setattr(bot.bc, "preview", resolver)
        await bot.cb_broadcast_filters(_cb("bcf:compose"), state)
        assert await state.get_state() == BroadcastState.waiting.state
        assert resolver.call_args.kwargs["audience"] == "overdue"
        assert resolver.call_args.kwargs["filters"].panel_ids == [1, 3]
    asyncio.run(go())


def test_text_and_inline_command_require_confirmation(monkeypatch):
    async def go():
        _patch_session(monkeypatch)
        state = _fsm()
        monkeypatch.setattr(bot.bc, "preview", AsyncMock(return_value=_result()))
        sender = AsyncMock()
        monkeypatch.setattr(bot, "_bot_broadcast_bg", sender)
        await cmd_broadcast(_message("/broadcast hello"), state, SimpleNamespace(args="hello"))
        assert await state.get_state() == BroadcastState.confirming.state
        assert (await state.get_data())["text"] == "hello"
        sender.assert_not_called()
        await state.set_state(BroadcastState.waiting)
        await on_broadcast_text(_message("different text"), state)
        assert (await state.get_data())["text"] == "different text"
        sender.assert_not_called()
    asyncio.run(go())


def test_confirm_sends_once_and_rejects_replayed_button(monkeypatch):
    async def go():
        _patch_session(monkeypatch)
        state = _fsm()
        await state.set_state(BroadcastState.confirming)
        await state.update_data(text="hello", preview_token="same", confirm_nonce="abc")
        monkeypatch.setattr(bot.bc, "preview", AsyncMock(return_value=_result()))
        sender = AsyncMock()
        monkeypatch.setattr(bot, "_bot_broadcast_bg", sender)
        cb = _cb("bcconfirm:abc")
        await bot.cb_broadcast_confirm(cb, state)
        await asyncio.sleep(0)
        await bot.cb_broadcast_confirm(cb, state)
        sender.assert_awaited_once()
        assert await state.get_state() is None
    asyncio.run(go())


def test_changed_snapshot_and_non_owner_cannot_send(monkeypatch):
    async def go():
        _patch_session(monkeypatch)
        state = _fsm()
        await state.set_state(BroadcastState.confirming)
        await state.update_data(text="hello", preview_token="same", confirm_nonce="abc")
        monkeypatch.setattr(bot.bc, "preview", AsyncMock(return_value=_result("changed")))
        sender = AsyncMock()
        monkeypatch.setattr(bot, "_bot_broadcast_bg", sender)
        await bot.cb_broadcast_confirm(_cb("bcconfirm:abc"), state)
        sender.assert_not_called()
        assert await state.get_state() == BroadcastState.configuring.state
        _patch_session(monkeypatch, owner=False)
        await bot.cb_broadcast_audience(_cb("bcaud:all"), state)
        assert await state.get_state() == BroadcastState.configuring.state
    asyncio.run(go())


def test_persian_range_and_invalid_input_preserve_flow(monkeypatch):
    async def go():
        _patch_session(monkeypatch)
        state = _fsm()
        await state.set_state(BroadcastState.filter_value)
        await state.update_data(filter_field="age", filters={})
        await bot.on_broadcast_filter_value(_message("۵۰:۵۵"), state)
        filters = (await state.get_data())["filters"]
        assert filters["min_age_days"] == 50 and filters["max_age_days"] == 55
        await state.set_state(BroadcastState.filter_value)
        await bot.on_broadcast_filter_value(_message("۶۰:۱۰"), state)
        assert await state.get_state() == BroadcastState.filter_value.state
        assert (await state.get_data())["filters"] == filters
    asyncio.run(go())
