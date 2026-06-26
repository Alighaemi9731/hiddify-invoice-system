"""Bulletproof-bot-UX regressions: every FSM-entry keyboard offers a tappable exit, the preset
keyboards avoid typing, and the locked pay flow always lets the user out by tap."""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bot-ux.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.bot import keyboards  # noqa: E402


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


# ── exit-everywhere keyboard contracts ───────────────────────────────────────

def test_cancel_keyboard_defaults_to_global_cancel():
    cb = _callbacks(keyboards.cancel_keyboard())
    assert cb == ["cancel"]
    # A custom target (e.g. the create-user flow's own cucancel) is honored.
    assert _callbacks(keyboards.cancel_keyboard("« انصراف", "cucancel")) == ["cucancel"]


def test_with_cancel_appends_an_exit_row():
    base = [[keyboards.InlineKeyboardButton(text="x", callback_data="x")]]
    cb = _callbacks(keyboards.with_cancel(base))
    assert cb == ["x", "cancel"]


def test_sub_cap_keyboard_has_presets_unlimited_custom_and_back():
    cb = _callbacks(keyboards.sub_cap_keyboard(7))
    # every preset sets the cap with no typing …
    for gb in keyboards.SUB_CAP_PRESETS:
        assert f"setcap:7:{gb}" in cb
    assert "setcap:7:0" in cb          # «نامحدود» clears the cap
    assert "capcustom:7" in cb         # custom-typing path exists
    assert "subv:7" in cb              # context-correct «بازگشت» to the sub's detail


def test_cap_bump_keyboard_has_presets_custom_and_cancel():
    cb = _callbacks(keyboards.cap_bump_keyboard(9))
    for n in keyboards.CAP_BUMP_PRESETS:
        assert f"capok:9:{n}" in cb    # preset → existing approve+notify path
    assert "bumptype:9" in cb          # custom-typing path
    assert "cancel" in cb              # always an exit


def test_every_fsm_entry_keyboard_offers_an_exit():
    """The keyboards attached to the prompts that put a user into an FSM state must always carry a
    way out by tap (so the user is never forced to remember /cancel)."""
    fsm_prompt_keyboards = [
        keyboards.cancel_keyboard(),                  # support / broadcast / owner-reply / register / pay
        keyboards.cancel_keyboard("« بازگشت به منو"),  # owner search / register
        keyboards.sub_cap_keyboard(1),                # GB-cap custom prompt parent
        keyboards.cap_bump_keyboard(1),               # capacity-bump prompt parent
    ]
    exits = {"cancel", "cucancel"}
    for kb in fsm_prompt_keyboards:
        cbs = set(_callbacks(kb))
        assert cbs & (exits | {c for c in cbs if c.startswith(("subv:",))}), kb


# ── the locked pay flow always lets the user out ─────────────────────────────

def _fsm():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class _Msg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=1, username=None, first_name="t")
        self.bot = SimpleNamespace(id=1)
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text: str = "", **kw):  # noqa: ANN003
        self.sent.append((text, kw))


def test_reply_menu_keyboards_and_label_maps_are_consistent():
    """The docked main menu (reply keyboard) labels must each map to an action, and «ساخت کاربر»
    appears only when enabled."""
    def labels(kb):
        return [b.text for row in kb.keyboard for b in row]

    with_cu = labels(keyboards.reseller_reply_keyboard(show_create_user=True))
    without_cu = labels(keyboards.reseller_reply_keyboard(show_create_user=False))
    assert "➕ ساخت کاربر" in with_cu and "➕ ساخت کاربر" not in without_cu
    for label in with_cu:
        assert label in keyboards.RESELLER_LABEL_TO_ACTION
    for label in labels(keyboards.owner_reply_keyboard()):
        assert label in keyboards.OWNER_LABEL_TO_ACTION
    assert keyboards.ALL_MENU_LABELS == (
        set(keyboards.RESELLER_LABEL_TO_ACTION) | set(keyboards.OWNER_LABEL_TO_ACTION)
    )
    kb = keyboards.owner_reply_keyboard()
    assert kb.is_persistent and kb.resize_keyboard  # always docked at the bottom


def test_paystate_cancel_text_exits_cleanly():
    """Typing «cancel»/«لغو» in the locked pay flow clears the state (no DB needed)."""
    async def go():
        from app.bot import handlers

        st = _fsm()
        await st.set_state(handlers.PayState.waiting)
        msg = _Msg("لغو")
        await handlers.pay_state_text(msg, st)
        assert await st.get_state() is None
        assert msg.sent  # the user got an acknowledgement

    asyncio.run(go())
