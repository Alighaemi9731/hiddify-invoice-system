"""The shop-admin's plan-price prompt in the Telegram bot.

The whole point of this flow is that a reseller typing `50` for 50,000 Toman is caught and can fix
it in place. That makes two things load-bearing: the prompt states the unit BEFORE they type, and a
rejection leaves them IN the price state with a usable idempotency key so the corrected number is
accepted rather than answered with a stale-command conflict.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/planprice.db")
os.environ.setdefault("SECRET_KEY", "k")

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot.storefront import handlers as H  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Panel, Reseller, StorefrontBot, StorefrontPlan  # noqa: E402


class _User:
    def __init__(self, uid: int = 111) -> None:
        self.id = uid


class _Bot:
    """`_resolve` only needs the physical bot's Telegram id to find its tenant."""

    id = 900_001


class _Message:
    def __init__(self, text: str, user: _User) -> None:
        self.text = text
        self.from_user = user
        self.replies: list[str] = []

    async def answer(self, text, reply_markup=None):  # noqa: ANN001, ANN202, ARG002
        self.replies.append(text)


def _run(body, tmp_path, name):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        original = H.SessionLocal
        H.SessionLocal = factory
        try:
            async with factory() as session:
                await body(session, factory)
        finally:
            H.SessionLocal = original
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    reseller = Reseller(
        panel_id=panel.id, admin_uuid="r1", name="Owner", bot_chat_id=111,
        storefront_enabled=True, price_per_gb=2000,
    )
    session.add(reseller)
    await session.flush()
    shop = StorefrontBot(
        reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="tok",
        bot_telegram_id=_Bot.id, bot_username="shop_bot", config_version=1,
    )
    session.add(shop)
    await session.commit()
    return reseller, shop


def _state(storage: MemoryStorage) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=_Bot.id, chat_id=111, user_id=111))


def test_the_price_prompt_states_the_unit_and_this_shops_floor(tmp_path):
    async def body(session, _factory):  # noqa: ANN001
        await _seed(session)
        storage = MemoryStorage()
        state = _state(storage)
        await state.update_data(p_cost=2000, p_gb=10)

        message = _Message("30", _User())
        await H.sf_plan_days(message, state)

        prompt = message.replies[-1]
        assert "به تومان بنویسید، نه هزار تومان" in prompt
        assert "50000" in prompt              # the example to copy
        assert "20,000" in prompt             # 10 GB x 2,000 — this shop's own floor
        assert await state.get_state() == H.SF.plan_price.state

    _run(body, tmp_path, "prompt.db")


def test_a_below_cost_price_keeps_the_reseller_in_the_prompt_and_the_retype_succeeds(tmp_path):
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        storage = MemoryStorage()
        state = _state(storage)
        await state.set_state(H.SF.plan_price)
        await state.update_data(
            p_cost=2000, p_gb=10, p_days=30,
            sf_config_version=1, sf_command_key="tg-fsm:original",
        )

        rejected = _Message("50", _User())
        await H.sf_plan_price(rejected, state, _Bot())

        # Rejected with the unit lesson, and NOT dumped back to the plan list — the reseller can
        # simply retype.
        assert "50000" in rejected.replies[-1]
        assert await state.get_state() == H.SF.plan_price.state
        async with factory() as check:
            assert (await check.execute(select(StorefrontPlan))).scalars().all() == []

        # The key must have rotated: reusing the flow's original key with a DIFFERENT price hits
        # claim_command's request-hash check and would answer the fix with a conflict.
        data = await state.get_data()
        assert data["sf_command_key"] != "tg-fsm:original"
        assert data["sf_config_version"] == 1   # a cached failure never CASes

        accepted = _Message("50000", _User())
        await H.sf_plan_price(accepted, state, _Bot())
        assert "✅ پلن اضافه شد." in accepted.replies[-1]
        assert await state.get_state() is None
        async with factory() as check:
            plan = (await check.execute(select(StorefrontPlan))).scalars().one()
            assert plan.price_toman == 50_000 and plan.gb == 10
            assert plan.storefront_bot_id == shop.id

    _run(body, tmp_path, "retype.db")


def test_editing_a_plan_to_a_below_cost_price_is_refused_the_same_way(tmp_path):
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, title="", gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        session.add(plan)
        await session.commit()

        storage = MemoryStorage()
        state = _state(storage)
        await state.set_state(H.SF.edit_value)
        await state.update_data(
            edit_plan_id=plan.id, edit_field="price", e_cost=2000, e_gb=10,
            sf_config_version=1, sf_command_key="tg-fsm:edit",
        )

        rejected = _Message("60", _User())
        await H.sf_edit_value(rejected, state, _Bot())
        assert "50000" in rejected.replies[-1]
        assert await state.get_state() == H.SF.edit_value.state
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).price_toman == 50_000

        accepted = _Message("60000", _User())
        await H.sf_edit_value(accepted, state, _Bot())
        assert "✅ قیمتِ پلن تغییر کرد." in accepted.replies[-1]
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).price_toman == 60_000

    _run(body, tmp_path, "edit.db")


class _CallbackMessage(_Message):
    def __init__(self) -> None:
        super().__init__("", _User())
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None):  # noqa: ANN001, ANN202
        self.replies.append(text)
        self.markups.append(reply_markup)


class _Callback:
    def __init__(self, data: str) -> None:
        self.id = "cb-1"
        self.data = data
        self.from_user = _User()
        self.message = _CallbackMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False):  # noqa: ANN202, ARG002
        self.answers.append(text)


def test_editing_one_field_leaves_the_other_two_exactly_as_they_were(tmp_path):
    """The whole point of the field picker: correcting a price must not re-ask (and so must not
    silently rewrite) the plan's volume and duration."""
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        session.add(plan)
        await session.commit()

        state = _state(MemoryStorage())
        picker = _Callback(f"sfplanedit:{plan.id}")
        await H.sf_plan_edit(picker, state, _Bot())
        # Nothing is asked yet — the admin picks WHICH field first, and no FSM is entered.
        assert "کدام مورد را تغییر می‌دهید؟" in picker.message.replies[-1]
        assert await state.get_state() is None

        chosen = _Callback(f"sfplanfield:{plan.id}:price")
        await H.sf_plan_field(chosen, state, _Bot())
        assert await state.get_state() == H.SF.edit_value.state
        prompt = chosen.message.replies[-1]
        assert "قیمتِ فعلی" in prompt
        assert "20,000" in prompt          # the floor, from the plan's OWN 10 GB

        await H.sf_edit_value(_Message("90000", _User()), state, _Bot())
        async with factory() as check:
            after = await check.get(StorefrontPlan, plan.id)
            assert (after.gb, after.days, after.price_toman) == (10, 30, 90_000)

    _run(body, tmp_path, "one_field.db")


def test_an_out_of_range_value_is_retryable_in_place(tmp_path):
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        session.add(plan)
        await session.commit()

        state = _state(MemoryStorage())
        await H.sf_plan_field(_Callback(f"sfplanfield:{plan.id}:days"), state, _Bot())

        rejected = _Message("9999", _User())          # over the 3650-day ceiling
        await H.sf_edit_value(rejected, state, _Bot())
        assert "۳۶۵۰" in rejected.replies[-1]
        assert await state.get_state() == H.SF.edit_value.state

        await H.sf_edit_value(_Message("60", _User()), state, _Bot())
        async with factory() as check:
            after = await check.get(StorefrontPlan, plan.id)
            assert (after.gb, after.days, after.price_toman) == (10, 60, 50_000)

    _run(body, tmp_path, "range.db")


# ── the optional plan NAME (v1.122.0) ─────────────────────────────────────────
# A plan's name is free text in a flow whose other three fields are strictly numeric, and it is the
# only plan field that can be set back to "nothing". Both properties are easy to break by widening
# the wrong validation, so they are pinned here.

def test_creating_a_plan_can_skip_the_name_in_one_tap(tmp_path):
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        state = _state(MemoryStorage())
        adder = _Callback("sfplanadd")
        await H.sf_plan_add(adder, state, _Bot())
        assert await state.get_state() == H.SF.plan_title.state
        assert "نامِ پلن (اختیاری)" in adder.message.replies[-1]

        # The docked «بدون نام» button — one tap, and the wizard moves on to the volume.
        await H.sf_plan_title(_Message(H.kb.PLAN_NO_TITLE, _User()), state)
        assert await state.get_state() == H.SF.plan_gb.state
        await H.sf_plan_gb(_Message("10", _User()), state)
        await H.sf_plan_days(_Message("30", _User()), state)
        await H.sf_plan_price(_Message("90000", _User()), state, _Bot())

        async with factory() as check:
            plan = (await check.execute(select(StorefrontPlan))).scalar_one()
            assert plan.title == ""
            assert (plan.gb, plan.days, plan.price_toman) == (10, 30, 90_000)

    _run(body, tmp_path, "create_skip_name.db")


def test_a_name_typed_at_the_start_survives_a_below_cost_retry(tmp_path):
    """The name is collected FIRST precisely so `sf_plan_price` keeps owning the below-cost retry.
    If that ordering is ever flipped, the retype would lose the name (or lose the retry)."""
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        state = _state(MemoryStorage())
        await H.sf_plan_add(_Callback("sfplanadd"), state, _Bot())
        await H.sf_plan_title(_Message("  طلایی   ویژه ", _User()), state)   # whitespace collapsed
        await H.sf_plan_gb(_Message("10", _User()), state)
        await H.sf_plan_days(_Message("30", _User()), state)

        rejected = _Message("50", _User())        # 50 Toman for 10 GB — below this shop's cost
        await H.sf_plan_price(rejected, state, _Bot())
        assert await state.get_state() == H.SF.plan_price.state
        async with factory() as check:
            assert (await check.execute(select(StorefrontPlan))).first() is None

        await H.sf_plan_price(_Message("90000", _User()), state, _Bot())
        async with factory() as check:
            plan = (await check.execute(select(StorefrontPlan))).scalar_one()
            assert plan.title == "طلایی ویژه"
            assert plan.price_toman == 90_000

    _run(body, tmp_path, "create_name_retry.db")


def test_the_name_can_be_edited_and_cleared_without_touching_the_numbers(tmp_path):
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, title="", gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        session.add(plan)
        await session.commit()

        state = _state(MemoryStorage())
        chosen = _Callback(f"sfplanfield:{plan.id}:title")
        await H.sf_plan_field(chosen, state, _Bot())
        assert await state.get_state() == H.SF.edit_value.state
        assert "نامِ جدیدِ پلن" in chosen.message.replies[-1]

        await H.sf_edit_value(_Message("نقره‌ای", _User()), state, _Bot())
        async with factory() as check:
            after = await check.get(StorefrontPlan, plan.id)
            assert after.title == "نقره‌ای"
            assert (after.gb, after.days, after.price_toman) == (10, 30, 50_000)

        # …and back to unnamed. A name is the one plan field that can be removed.
        state2 = _state(MemoryStorage())
        await H.sf_plan_field(_Callback(f"sfplanfield:{plan.id}:title"), state2, _Bot())
        await H.sf_edit_value(_Message(H.kb.PLAN_NO_TITLE, _User()), state2, _Bot())
        async with factory() as check:
            after = await check.get(StorefrontPlan, plan.id)
            assert after.title == ""
            assert (after.gb, after.days, after.price_toman) == (10, 30, 50_000)

    _run(body, tmp_path, "edit_name.db")


def test_free_text_is_accepted_for_the_name_and_still_refused_for_the_numbers(tmp_path):
    """`sf_edit_value` had a single `isdigit()` gate. Branching it per field is what lets a name
    through — without letting «abc» through as a volume."""
    async def body(session, factory):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, title="", gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        session.add(plan)
        await session.commit()

        state = _state(MemoryStorage())
        await H.sf_plan_field(_Callback(f"sfplanfield:{plan.id}:gb"), state, _Bot())
        rejected = _Message("abc", _User())
        await H.sf_edit_value(rejected, state, _Bot())
        assert "عددِ معتبر" in rejected.replies[-1]
        assert await state.get_state() == H.SF.edit_value.state
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).gb == 10

        # An over-long name is likewise retryable in place, not a dead end.
        state2 = _state(MemoryStorage())
        await H.sf_plan_field(_Callback(f"sfplanfield:{plan.id}:title"), state2, _Bot())
        too_long = _Message("ن" * 65, _User())
        await H.sf_edit_value(too_long, state2, _Bot())
        assert "۶۴ نویسه" in too_long.replies[-1] or "64" in too_long.replies[-1]
        assert await state2.get_state() == H.SF.edit_value.state
        await H.sf_edit_value(_Message("ن" * 64, _User()), state2, _Bot())
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).title == "ن" * 64

    _run(body, tmp_path, "name_free_text.db")


# ── the customer's support message actually reaches someone (v1.122.0) ────────

def test_a_customer_support_message_is_relayed_to_the_shop_admins(tmp_path):
    """The bot has always promised «…تا مستقیم به پشتیبانیِ فروشگاه برسد و پاسخ بگیرید», but the
    customer «💬 پشتیبانی» branch set no state and nothing ever called `_relay_to_admins` — the
    message went nowhere and neither side knew. Relaying only from the deliberate state keeps stray
    chatter out of the shop admin's inbox, which is why the blanket relay was removed in the first
    place."""
    async def body(session, factory):  # noqa: ANN001
        reseller, shop = await _seed(session)
        shop.co_admin_ids = "222"
        await session.commit()

        sent: list[tuple[int, str]] = []

        class _RelayBot:
            id = _Bot.id

            async def send_message(self, chat_id, text, **kw):  # noqa: ANN001, ANN003
                sent.append((chat_id, text))

        state = _state(MemoryStorage())
        customer = _User(555)
        bot = _RelayBot()

        # Entering support docks a cancel-only keyboard and ARMS the relay.
        await H._customer_action(
            "support", _Message("", customer), state, session, shop,
            await __import__("app.services.storefront", fromlist=["storefront"])
            .get_or_create_customer(session, shop.id, customer), bot)
        assert await state.get_state() == H.SF.cust_support.state

        message = _Message("سلام، سرویسم وصل نمی‌شود", customer)
        message.photo = None
        message.caption = None
        await H.sf_customer_support(message, state, bot)

        # Owner (111) and co-admin (222) both got it; the customer got a confirmation.
        assert {chat_id for chat_id, _ in sent} >= {111, 222}
        assert any("سلام، سرویسم وصل نمی‌شود" in text for _cid, text in sent)
        # (the confirmation, then the re-docked customer menu)
        assert any("به پشتیبانیِ فروشگاه رسید" in reply for reply in message.replies)
        # …and the flow is over, so the NEXT stray message is not a support ticket.
        assert await state.get_state() is None

    _run(body, tmp_path, "cust_support.db")
