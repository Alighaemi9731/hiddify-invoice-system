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


def _labels(kb):
    return [b.text for row in kb.keyboard for b in row]


def test_reply_menu_keyboards_and_label_maps_are_consistent():
    """The lean reply-keyboard menus (≤5) map every label to an action; «ساخت سرویس» appears only
    when enabled; a «⋯ بیشتر» / «« بازگشت» pair drives the drill-down; the first-timer menu leads with
    «ثبت پنل»."""
    with_cu = _labels(keyboards.reseller_main_reply_kb(show_create_user=True))
    without_cu = _labels(keyboards.reseller_main_reply_kb(show_create_user=False))
    assert "➕ ساخت سرویس" in with_cu and "➕ ساخت سرویس" not in without_cu
    assert keyboards.MORE_LABEL in with_cu           # «بیشتر» is always present
    assert len(with_cu) <= 5                          # lean: ≤5 top-level options

    for label in with_cu:
        assert label in keyboards.RESELLER_LABEL_TO_ACTION or label in keyboards._NAV_LABELS
    for label in _labels(keyboards.owner_main_reply_kb()):
        assert label in keyboards.OWNER_LABEL_TO_ACTION or label in keyboards._NAV_LABELS

    # «بیشتر» drill-downs carry the back button; the first-timer menu leads with «ثبت پنل».
    assert keyboards.BACK_LABEL in _labels(keyboards.reseller_more_reply_kb())
    assert keyboards.BACK_LABEL in _labels(keyboards.owner_more_reply_kb())
    first = _labels(keyboards.first_timer_reply_kb())
    assert first[0] == keyboards.REGISTER_LABEL and "💬 پشتیبانی" in first

    # ALL_MENU_LABELS (what the label router listens for) includes the nav labels.
    assert keyboards._NAV_LABELS <= keyboards.ALL_MENU_LABELS
    kb = keyboards.owner_main_reply_kb()
    assert kb.is_persistent and kb.resize_keyboard    # always docked at the bottom


def test_flow_cancel_keyboard_is_cancel_only():
    """A flow docks a cancel-ONLY reply keyboard so the menu is hidden and only «انصراف» is tappable."""
    kb = keyboards.flow_cancel_kb()
    assert _labels(kb) == [keyboards.CANCEL_LABEL]
    assert kb.is_persistent and kb.resize_keyboard


def test_menu_label_during_a_flow_is_locked_not_a_universal_escape():
    """FSM-LOCK: tapping a menu label while a flow is active must NOT cancel/navigate — it re-prompts
    the docked cancel. (The old behavior cleared the flow; now only «انصراف»/start exits.)"""
    from app.bot.handlers import menus

    class FakeState:
        def __init__(self):
            self.cleared = False
            self._st = "PayState:waiting"

        async def get_state(self):
            return self._st

        async def clear(self):
            self.cleared = True
            self._st = None

    sent: list = []

    class FakeMsg:
        text = "🧾 فاکتور و پرداخت"
        from_user = SimpleNamespace(id=1, first_name="A", username=None)

        async def answer(self, t="", **kw):  # noqa: ANN001, ANN003
            sent.append((t, kw.get("reply_markup")))

    st = FakeState()
    asyncio.run(menus.on_menu_label(FakeMsg(), st, bot=None))
    assert not st.cleared                              # the flow is NOT canceled by a menu tap
    assert sent and "در حال یک عملیات" in sent[0][0]   # re-prompted to cancel first
    assert isinstance(sent[0][1], type(keyboards.flow_cancel_kb()))  # docked cancel keyboard


def test_portal_menu_url_requires_a_registered_reseller(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot.handlers import common
    from app.core.db import Base

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-reseller.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                assert await common._portal_menu_url(session, 999) is None
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_portal_menu_url_requires_a_configured_domain(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot.handlers import common
    from app.core.db import Base
    from app.models import Panel, Reseller

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-domain.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
                session.add(panel)
                await session.flush()
                session.add(
                    Reseller(
                        panel_id=panel.id,
                        admin_uuid="a",
                        name="Ali",
                        bot_chat_id=999,
                    )
                )
                await session.commit()

                assert await common._portal_menu_url(session, 999) is None
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_portal_menu_url_normalizes_domain_and_mints_valid_token(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot.handlers import common
    from app.core.db import Base
    from app.models import Panel, Reseller
    from app.services import settings_service

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portal-url.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                panel = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
                session.add(panel)
                await session.flush()
                session.add(
                    Reseller(
                        panel_id=panel.id,
                        admin_uuid="a",
                        name="Ali",
                        bot_chat_id=999,
                    )
                )
                await settings_service.set_value(
                    session,
                    "server_domain",
                    "  https://portal.example.test///  ",
                )

                # The reseller's portal address is now PERMANENT (/portal/u/<admin_uuid>) instead
                # of a 15-minute one-time /portal/login?t=… link, which constantly expired before
                # the reseller got round to tapping it. It carries no credential at all.
                url = await common._portal_menu_url(session, 999)
                assert url is not None
                parsed = urlsplit(url)
                assert parsed.scheme == "https"
                assert parsed.netloc == "portal.example.test"
                assert parsed.path == "/portal/u/a"          # the reseller's own admin_uuid
                assert "t=" not in (parsed.query or "")      # no token rides in the URL
                assert parse_qs(parsed.query) in ({}, {})    # nothing but an optional &next

                # A deep-link destination is appended only when it passes the allowlist.
                deep = await common.portal_stable_url(
                    session, 999, next_path="/portal/storefront/3/topups")
                assert deep is not None and deep.endswith(
                    "?next=%2Fportal%2Fstorefront%2F3%2Ftopups")
                bad = await common.portal_stable_url(session, 999, next_path="//evil.com")
                assert bad is not None and "next=" not in bad
        finally:
            await engine.dispose()

    asyncio.run(go())


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


def _reply_labels(kb):
    return [b.text for row in getattr(kb, "keyboard", []) for b in row]


def test_reshow_menu_docks_role_reply_menu(tmp_path):
    """`_reshow_menu` re-docks the role reply keyboard (a registered reseller sees the reseller menu)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot import handlers, keyboards
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
            _text, kb = sent[0]
            labels = _reply_labels(kb)
            assert keyboards.MORE_LABEL in labels and "🧾 فاکتور و پرداخت" in labels
            assert kb.is_persistent
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_send_menu_tiers_first_timer_vs_registered_with_inline_portal(tmp_path):
    """`_send_menu`: a first-timer with NO registered panel gets «ثبت پنل» front-and-center; a
    registered reseller gets the reseller reply keyboard with «🌐 پنل تحت وب» as a normal menu item.

    The portal used to ride its own extra inline message above every menu, which looked bolted-on;
    it is now just another button in the keyboard like the rest."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot import handlers, keyboards
    from app.core.db import Base
    from app.models import Panel, Reseller
    from app.services import settings_service

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tiers.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await settings_service.set_value(s, "server_domain", "portal.example.test")
                await s.commit()

                # First-timer (no reseller row) → «ثبت پنل» docked, no portal button.
                first: list = []

                class M1:
                    async def answer(self, text="", **kw):  # noqa: ANN001, ANN003
                        first.append((text, kw.get("reply_markup")))
                await handlers._send_menu(M1().answer, s, SimpleNamespace(id=555, first_name="New", username=None))
                ft_kbs = [_reply_labels(kb) for _t, kb in first if hasattr(kb, "keyboard")]
                assert any(keyboards.REGISTER_LABEL in labels for labels in ft_kbs)

                # Registered reseller → the reseller reply keyboard, portal INSIDE it (no extra
                # inline message).
                p = Panel(key="p", host="p.invalid", proxy_path_enc="x", owner_uuid="o")
                s.add(p)
                await s.flush()
                s.add(Reseller(panel_id=p.id, admin_uuid="a", name="Reg", bot_chat_id=777))
                await s.commit()
                out: list = []

                class M2:
                    async def answer(self, text="", **kw):  # noqa: ANN001, ANN003
                        out.append((text, kw.get("reply_markup")))
                await handlers._send_menu(M2().answer, s, SimpleNamespace(id=777, first_name="Reg", username=None))
                # No extra inline message any more — the menu is a single docked keyboard…
                assert not [kb for _t, kb in out if hasattr(kb, "inline_keyboard")]
                docked = [_reply_labels(kb) for _t, kb in out if hasattr(kb, "keyboard")]
                assert any("🧾 فاکتور و پرداخت" in labels for labels in docked)
                # …and the portal is one of its normal buttons.
                assert any("🌐 پنل تحت وب" in labels for labels in docked)
        finally:
            await engine.dispose()

    asyncio.run(go())
