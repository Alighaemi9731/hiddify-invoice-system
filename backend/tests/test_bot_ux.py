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


# ── v1.49.0: complete slash commands + owner decision keyboard + menu re-show ────

def test_command_menus_include_all_actions():
    """The `/` list must offer every action (owner /search, reseller /storefront + /register,
    both /cancel), and each new command must have a handler function."""
    from app.bot import handlers
    from app.bot.commands import OWNER_COMMANDS, RESELLER_COMMANDS

    r = {c.command for c in RESELLER_COMMANDS}
    o = {c.command for c in OWNER_COMMANDS}
    assert {"storefront", "register", "cancel"} <= r
    assert {"search", "cancel"} <= o
    assert callable(handlers.cmd_search)
    assert callable(handlers.cmd_storefront)
    assert callable(handlers.cmd_register)


def test_finalize_review_message_edits_caption_for_photo_and_text_for_text():
    """The owner review message may be a PHOTO (screenshot proof, caption) or TEXT (TXID). Use
    edit_caption for the former and edit_text for the latter — else the screenshot path silently
    fails and the buttons stay live."""
    async def go():
        from app.bot import handlers

        calls: dict = {}

        class FakeMsg:
            def __init__(self, *, is_photo: bool) -> None:
                self.caption = "cap" if is_photo else None
                self.photo = [object()] if is_photo else None
                self.html_text = "cap" if is_photo else "txt"

            async def edit_caption(self, **kw):  # noqa: ANN003
                calls["caption"] = kw

            async def edit_text(self, *a, **kw):  # noqa: ANN002, ANN003
                calls["text"] = (a, kw)

        cb = SimpleNamespace(message=FakeMsg(is_photo=True))
        assert await handlers._finalize_review_message(cb, "✅ done") is True
        assert "caption" in calls and "text" not in calls

        calls.clear()
        cb = SimpleNamespace(message=FakeMsg(is_photo=False))
        assert await handlers._finalize_review_message(cb, "✅ done") is True
        assert "text" in calls and "caption" not in calls

    asyncio.run(go())


def test_owner_payment_decided_keyboard_drops_action_buttons():
    """After a decision the تأیید/رد buttons are gone (unmistakable + un-re-tappable); the live
    keyboard still has both."""
    decided = _callbacks(keyboards.owner_payment_decided_keyboard())
    assert decided == ["owner:payments"]
    live = _callbacks(keyboards.owner_payment_detail_keyboard(5))
    assert "opok:5" in live and "opno:5" in live


def test_reshow_menu_sends_role_aware_menu(tmp_path):
    """`_reshow_menu` re-sends the inline main menu as a fresh message so it's always at hand."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot import handlers
    from app.core.db import Base
    from app.models import Panel, Reseller

    sent: list = []

    class FakeMsg:
        async def answer(self, text: str = "", **kw):  # noqa: ANN003
            sent.append((text, kw.get("reply_markup")))

    user = SimpleNamespace(id=999, first_name="Ali", username="ali")

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reshow.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                p = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
                s.add(p)
                await s.flush()
                s.add(Reseller(panel_id=p.id, admin_uuid="a", name="Ali", bot_chat_id=999))
                await s.commit()
                await handlers._reshow_menu(FakeMsg(), s, user)
            assert len(sent) == 1
            text, kb = sent[0]
            assert "منوی اصلی" in text
            assert kb is not None and hasattr(kb, "inline_keyboard")
        finally:
            await engine.dispose()

    asyncio.run(go())
