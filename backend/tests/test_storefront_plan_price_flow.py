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
        await state.set_state(H.SF.edit_price)
        await state.update_data(
            edit_plan_id=plan.id, e_cost=2000, e_gb=10, e_days=30,
            sf_config_version=1, sf_command_key="tg-fsm:edit",
        )

        rejected = _Message("60", _User())
        await H.sf_edit_price(rejected, state, _Bot())
        assert "50000" in rejected.replies[-1]
        assert await state.get_state() == H.SF.edit_price.state
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).price_toman == 50_000

        accepted = _Message("60000", _User())
        await H.sf_edit_price(accepted, state, _Bot())
        assert "✅ پلن ویرایش شد." in accepted.replies[-1]
        async with factory() as check:
            assert (await check.get(StorefrontPlan, plan.id)).price_toman == 60_000

    _run(body, tmp_path, "edit.db")
