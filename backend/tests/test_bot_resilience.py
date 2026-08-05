"""I04 regressions: outgoing-rtl middleware, clean blocked-user reply errors, and
storefront polling that stops burning retries on a revoked token."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bot-resilience.db")
os.environ.setdefault("SECRET_KEY", "k")

from aiogram.exceptions import (  # noqa: E402
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto  # noqa: E402

from app.bot.rtl import rtl  # noqa: E402
from app.bot.rtl_middleware import RtlRequestMiddleware  # noqa: E402

MIXED = "مبلغ 30.86 USDT پرداخت شد"


def _mw_run(method):
    mw = RtlRequestMiddleware()
    captured: dict = {}

    async def make_request(_bot, m):
        captured["method"] = m
        return None

    asyncio.run(mw(make_request, None, method))
    return captured["method"]


# ── rtl request middleware ───────────────────────────────────────────────────

def test_middleware_rtl_wraps_send_message_text():
    out = _mw_run(SendMessage(chat_id=1, text=MIXED))
    assert out.text == rtl(MIXED)
    assert out.text != MIXED  # the Latin run actually got isolated


def test_middleware_is_idempotent():
    once = _mw_run(SendMessage(chat_id=1, text=MIXED)).text
    twice = _mw_run(SendMessage(chat_id=1, text=once)).text
    assert twice == once  # central helpers already applying rtl() stay byte-identical


def test_middleware_rtl_wraps_photo_caption():
    out = _mw_run(SendPhoto(chat_id=1, photo="file-id", caption=MIXED))
    assert out.caption == rtl(MIXED)


def test_middleware_leaves_callback_toasts_alone():
    acq = AnswerCallbackQuery(callback_query_id="q", text=MIXED)
    out = _mw_run(acq)
    assert out is acq  # excluded by design (see IMPROVEMENT_PLAN owner decisions)


def test_middleware_passes_empty_text_through():
    m = SendMessage(chat_id=1, text="")
    assert _mw_run(m) is m


# ── owner reply to a blocked user ────────────────────────────────────────────

class _ReplyMsg:
    """Fake owner message whose bot.send_message raises like a blocked user."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.text = "پاسخ من"
        self.from_user = SimpleNamespace(id=1, username=None, first_name="t")
        self.sent: list[str] = []
        self.bot = SimpleNamespace(send_message=self._raise)

    async def _raise(self, *_a, **_kw):
        raise self._exc

    async def answer(self, text: str = "", **_kw):
        self.sent.append(text)


def _fsm_with(data: dict):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    ctx = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return ctx, data


def test_owner_reply_to_blocked_user_shows_clean_persian():
    async def go():
        from app.bot import handlers

        st, data = _fsm_with({"target": "42", "reply_to": None})
        await st.set_state(handlers.OwnerReplyState.waiting)
        await st.update_data(**data)
        msg = _ReplyMsg(TelegramForbiddenError(
            method=SendMessage(chat_id=42, text="x"), message="Forbidden: bot was blocked by the user"))
        await handlers.on_owner_reply(msg, st)
        assert msg.sent, "the owner got no feedback"
        assert "مسدود" in msg.sent[-1]
        assert "Forbidden" not in msg.sent[-1]  # no raw API text

    asyncio.run(go())


# ── storefront polling: revoked token stops the loop ─────────────────────────

def test_poll_one_marks_revoked_after_consecutive_unauthorized(monkeypatch):
    from app.bot.storefront import manager

    class _FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def get_updates(self, **_kw):
            self.calls += 1
            raise TelegramUnauthorizedError(
                method=SendMessage(chat_id=1, text="x"), message="Unauthorized")

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    recorded: list[tuple[int, str]] = []

    async def fake_mark_revoked(_s, row_id, error):
        recorded.append((row_id, error))

    monkeypatch.setattr(manager, "_POLL_BACKOFF", 0)
    monkeypatch.setattr(manager, "SessionLocal", lambda: _S())
    monkeypatch.setattr(manager.storefront, "mark_revoked", fake_mark_revoked)
    # Don't build the REAL dispatcher: attaching storefront_router to a throwaway Dispatcher
    # here would poison later tests (an aiogram router attaches to exactly one dispatcher).
    monkeypatch.setattr(
        manager, "_dispatcher",
        lambda: SimpleNamespace(resolve_used_update_types=lambda: []))

    bot = _FakeBot()
    asyncio.run(manager._poll_one(  # returns instead of looping forever
        bot, 7, asyncio.Semaphore(manager._HANDLER_CONCURRENCY), set()))
    assert bot.calls == manager._UNAUTHORIZED_LIMIT
    assert recorded and recorded[0][0] == 7
    assert "revoked" in recorded[0][1]


def test_active_bots_only_polls_active(tmp_path):
    """Neither `errored` (ambiguous) nor `revoked` (dead credential) may be polled."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Panel, Reseller, StorefrontBot
    from app.services import storefront

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'sfa.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
                s.add(panel)
                await s.flush()
                r1 = Reseller(panel_id=panel.id, admin_uuid="a1", name="r1")
                r2 = Reseller(panel_id=panel.id, admin_uuid="a2", name="r2")
                r3 = Reseller(panel_id=panel.id, admin_uuid="a3", name="r3")
                s.add_all([r1, r2, r3])
                await s.flush()
                s.add_all([
                    StorefrontBot(reseller_id=r1.id, panel_id=panel.id, bot_token_enc="t1",
                                  enabled=True, status="active"),
                    StorefrontBot(reseller_id=r2.id, panel_id=panel.id, bot_token_enc="t2",
                                  enabled=True, status="errored", last_error="id clash"),
                    StorefrontBot(reseller_id=r3.id, panel_id=panel.id, bot_token_enc="",
                                  enabled=True, status="revoked", last_error="token revoked"),
                ])
                await s.commit()
                rows = await storefront.active_bots(s)
                assert [b.reseller_id for b in rows] == [r1.id]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_mark_revoked_clears_token_and_notifies_reseller_once(tmp_path, monkeypatch):
    """The dead secret must not be retained, and the reseller is told exactly once — over the
    MAIN bot, since their own bot is precisely what stopped working."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Panel, Reseller, StorefrontBot
    from app.services import notifier, storefront

    sent: list[tuple[int, str]] = []

    async def fake_send(_s, reseller, text, **_kw):
        sent.append((reseller.id, text))

    monkeypatch.setattr(notifier, "send_to_reseller", fake_send)

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'sfrev.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid="a1", name="r1", bot_chat_id=555)
                s.add(r)
                await s.flush()
                bot = StorefrontBot(reseller_id=r.id, panel_id=panel.id,
                                    bot_token_enc="enc::secret", bot_username="shop_bot",
                                    enabled=True, status="active")
                s.add(bot)
                await s.commit()

                await storefront.mark_revoked(s, bot.id, "token revoked: Unauthorized")
                await s.refresh(bot)
                assert bot.status == "revoked"
                assert bot.bot_token_enc == ""          # dead secret dropped, NOT kept
                assert bot.last_error.startswith("token revoked")
                assert len(sent) == 1 and sent[0][0] == r.id
                assert "@shop_bot" in sent[0][1]

                # The shop row and its data survive so a new token recovers it in place.
                assert await storefront.get_bot_for_reseller(s, r.id) is not None

                # Re-marking must NOT re-notify (the poll loop can hit this more than once).
                await storefront.mark_revoked(s, bot.id, "token revoked: Unauthorized")
                assert len(sent) == 1
        finally:
            await engine.dispose()

    asyncio.run(go())


# ── malformed BotFather token (seen live 2026-07-02) ─────────────────────────

def test_sf_setup_malformed_token_gets_friendly_reply():
    """A pasted token that passes the cheap pre-check (has ':' and length) but fails
    aiogram's constructor validation must produce the same «توکن نامعتبر است» reply the
    obviously-bad input gets — not an unhandled TokenValidationError."""
    async def go():
        from app.bot import handlers

        st, _ = _fsm_with({})
        await st.set_state(handlers.StorefrontSetupState.token)
        await st.update_data(sf_reseller_id=1)

        class _M:
            text = "1234567:invalid token with spaces and نviz"
            from_user = SimpleNamespace(id=1, username=None, first_name="t")
            sent: list[str] = []

            async def answer(self, text: str = "", **_kw):
                self.sent.append(text)

        msg = _M()
        await handlers.on_sf_setup_token(msg, st)  # must not raise
        assert msg.sent and "نامعتبر" in msg.sent[-1]

    asyncio.run(go())


def test_start_runner_marks_malformed_stored_token_revoked(monkeypatch):
    from app.bot.storefront import manager

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    recorded: list[tuple[int, str]] = []

    async def fake_mark_revoked(_s, row_id, error):
        recorded.append((row_id, error))

    monkeypatch.setattr(manager, "SessionLocal", lambda: _S())
    monkeypatch.setattr(manager.storefront, "mark_revoked", fake_mark_revoked)

    async def go():
        sem = asyncio.Semaphore(1)
        await manager._start_runner(1, 9, "totally not a token", sem)

    asyncio.run(go())
    assert recorded and recorded[0][0] == 9
    assert "malformed" in recorded[0][1]
    assert 1 not in manager._active


def _start_runner_with_getme(monkeypatch, exc: Exception) -> list[tuple[int, str]]:
    """Drive `_start_runner` against a fake Bot whose get_me() raises `exc`; return mark_revoked calls."""
    from app.bot.storefront import manager

    class _FakeBot:
        session = SimpleNamespace(close=_noop_close)

        def __init__(self, **_kw) -> None:
            pass

        async def get_me(self):
            raise exc

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    recorded: list[tuple[int, str]] = []

    async def fake_mark_revoked(_s, row_id, error):
        recorded.append((row_id, error))

    monkeypatch.setattr(manager, "Bot", _FakeBot)
    monkeypatch.setattr(manager.rtl_middleware, "install", lambda _b: None)
    monkeypatch.setattr(manager, "SessionLocal", lambda: _S())
    monkeypatch.setattr(manager.storefront, "mark_revoked", fake_mark_revoked)

    asyncio.run(manager._start_runner(1, 9, "123456:AAquiteplausiblelookingtokenvalue", asyncio.Semaphore(1)))
    assert 1 not in manager._active
    return recorded


async def _noop_close():
    return None


def test_start_runner_does_not_error_a_bot_on_a_transient_network_failure(monkeypatch):
    """PROD REGRESSION: on 2026-07-14 a momentary DNS/TCP blip reaching api.telegram.org made
    `_start_runner`'s getMe fail for three healthy storefront bots. Because ANY exception marked the
    row errored — and `active_bots` excludes errored rows — those bots were permanently taken offline
    and never retried; @Ir90_2bot stayed dead for four days with a perfectly valid token. A transient
    failure must leave the row active so the next reconcile brings the bot back by itself."""
    recorded = _start_runner_with_getme(
        monkeypatch, ConnectionError("Cannot connect to host api.telegram.org:443"))
    assert recorded == []


def test_start_runner_does_not_error_a_bot_on_a_getme_timeout(monkeypatch):
    """`asyncio.wait_for` raises a bare TimeoutError (empty str) — also transient, also must not stick."""
    assert _start_runner_with_getme(monkeypatch, TimeoutError()) == []


def test_start_runner_marks_revoked_token_revoked(monkeypatch):
    """The one case that IS permanent: a 401 must still stop the fleet re-validating a dead token."""
    recorded = _start_runner_with_getme(monkeypatch, TelegramUnauthorizedError(
        method=SendMessage(chat_id=1, text="x"), message="Unauthorized"))
    assert recorded and recorded[0][0] == 9 and "getMe failed" in recorded[0][1]

