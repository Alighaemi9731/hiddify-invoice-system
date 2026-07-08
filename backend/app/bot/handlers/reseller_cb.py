"""Reseller menu callbacks: membership re-check, register, panels, portal."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import _reshow_menu, router
from app.bot.handlers.views import _send_panels, _send_portal_link


# --------------------------- reseller callbacks ---------------------------
@router.callback_query(F.data == "check_membership")
async def cb_check_membership(cb: CallbackQuery, bot: Bot) -> None:
    async with common.SessionLocal() as session:
        missing = (
            [] if await common._is_owner_user(session, cb.from_user)
            else await common._missing_gates(bot, session, cb.from_user.id)
        )
        if not missing:
            # edit_text raises "message is not modified" on a double-tap — tolerate it.
            try:
                await cb.message.edit_text("✅ عضویت شما تأیید شد.")
            except TelegramBadRequest:
                pass
            await common._send_menu(cb.message.answer, session, cb.from_user)
            await cb.answer()
        else:
            names = " و ".join(g["label"] for g in missing)
            await cb.answer(f"هنوز عضو {names} نیستید.", show_alert=True)


@router.callback_query(F.data == "menu:register")
async def cb_register(cb: CallbackQuery, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    await cb.message.answer(
        "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
        reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
    )
    await cb.answer()


@router.callback_query(F.data == "menu:panels")
async def cb_panels(cb: CallbackQuery, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_panels(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


@router.callback_query(F.data == "menu:portal")
async def cb_portal(cb: CallbackQuery, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_portal_link(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()
