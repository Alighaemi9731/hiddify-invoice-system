"""H11 — bot state hygiene & UX correctness.

- clear_stale_flow drops an in-progress PayState so a later txid can't attach to a stale
  invoice selection;
- _safe_int tolerates forged/malformed callback data;
- iso_html HTML-escapes a panel/user string (a `<` can't break entity parsing);
- the sub GB-cap is clamped so a huge value can't overflow the Integer column.
"""
import asyncio

from app.bot.handlers import common
from app.bot.handlers.subs import _MAX_GB_CAP


def _fsm():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def test_clear_stale_flow_drops_pay_selection():
    async def go():
        state = _fsm()
        await state.set_state(common.PayState.waiting)
        await state.update_data(pay_invoice_ids=[7, 8])
        await common.clear_stale_flow(state)
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(go())


def test_clear_stale_flow_is_noop_without_state():
    async def go():
        state = _fsm()
        # No active state → nothing happens (and no crash).
        await common.clear_stale_flow(state)
        assert await state.get_state() is None

    asyncio.run(go())


def test_safe_int_parses_and_tolerates_garbage():
    assert common._safe_int("setcap:12:500", 1) == 12
    assert common._safe_int("setcap:12:500", 2) == 500
    assert common._safe_int("rm:9") == 9
    assert common._safe_int("rm:notanumber") is None
    assert common._safe_int("rm") is None          # missing index
    assert common._safe_int(None) is None
    assert common._safe_int("x:5", 9) is None       # index out of range


def test_iso_html_escapes_and_isolates():
    out = common.iso_html("<b>Ali & Co</b>")
    assert "&lt;b&gt;" in out and "&amp;" in out
    assert "<b>" not in out                          # raw tag never survives
    assert out.startswith("⁨") and out.endswith("⁩")  # FSI…PDI


def test_gb_cap_clamp_bound_is_under_int32():
    assert _MAX_GB_CAP < 2**31 - 1
