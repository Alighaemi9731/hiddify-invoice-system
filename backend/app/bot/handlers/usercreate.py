"""Create-user entry points (top-level resellers); the picker flow lives in `storefront_setup`."""
from __future__ import annotations

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers import common
from app.bot.handlers.common import router
from app.bot.handlers.views import _begin_create_user


@router.message(Command("newuser"))
async def cmd_newuser(message: Message, state: FSMContext) -> None:
    async with common.SessionLocal() as s:
        await _begin_create_user(message.answer, message.from_user.id, s, state)


@router.callback_query(F.data == "menu:newuser")
async def cb_menu_newuser(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as s:
        await _begin_create_user(cb.message.answer, cb.from_user.id, s, state)
    await cb.answer()


@router.callback_query(F.data == "cucancel")
async def cb_cu_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("لغو شد.")
    await cb.answer()
