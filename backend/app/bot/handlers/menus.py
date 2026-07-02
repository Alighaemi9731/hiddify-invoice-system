"""Persistent docked main menu (reply keyboard): the label router + role dispatchers."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import (
    _OWNER_TERMINAL,
    _RESELLER_TERMINAL,
    OwnerSearchState,
    SupportState,
    _reshow_menu,
    _track_user,
    router,
)
from app.bot.handlers.views import (
    _begin_create_user,
    _begin_storefront_setup,
    _dispatch_owner,
    _send_invoices,
    _send_panels,
    _send_pay,
    _send_portal_link,
    _send_removelink,
    _send_self_interim,
    _send_sub_panels,
)


# --------------------------- persistent docked main menu (reply keyboard) ---------------------------
# Registered BEFORE every FSM text handler so a tap on a docked main-menu button ALWAYS works — it
# clears any in-progress flow and navigates, acting as a universal escape (the user can never get stuck).
@router.message(F.text.in_(keyboards.ALL_MENU_LABELS))
async def on_menu_label(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    async with common.SessionLocal() as s:
        await _track_user(s, message.from_user)
        is_owner = await common._is_owner_user(s, message.from_user)
        # A non-owner must still be a member; the message middleware already gates this.
        await state.clear()
        if is_owner:
            action = keyboards.OWNER_LABEL_TO_ACTION.get(text)
            if action:
                await _do_owner_menu(action, message, state, s)
                return
        action = keyboards.RESELLER_LABEL_TO_ACTION.get(text)
        if action:
            await _do_reseller_menu(action, message, state, bot, s)
            return
        # A label that doesn't apply to this role → just (re)show their own menu.
        await common._send_menu(message.answer, s, message.from_user, bot=bot)

async def _do_reseller_menu(action: str, message: Message, state: FSMContext, bot: Bot, s) -> None:  # noqa: ANN001
    ans, cid = message.answer, message.from_user.id
    if action == "invoices":
        await _send_invoices(ans, cid, s)
    elif action == "pay":
        await _send_pay(ans, cid, s)
    elif action == "interim":
        await _send_self_interim(ans, cid, s, bot=bot)
    elif action == "panels":
        await _send_panels(ans, cid, s)
    elif action == "subs":
        await _send_sub_panels(ans, cid, s)
    elif action == "portal":
        await _send_portal_link(ans, cid, s)
    elif action == "storefront":
        await _begin_storefront_setup(ans, cid, s, state)
    elif action == "removelink":
        await _send_removelink(ans, cid, s)
    elif action == "support":
        await state.set_state(SupportState.waiting)
        await ans("پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.cancel_keyboard())
    elif action == "register":
        await ans(
            "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
            reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
        )
    elif action == "newuser":
        await _begin_create_user(ans, cid, s, state)
    # Keep the menu at hand after a completed (non-flow) action.
    if action in _RESELLER_TERMINAL:
        await _reshow_menu(message, s, message.from_user)


async def _do_owner_menu(action: str, message: Message, state: FSMContext, s) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "search":
        await state.set_state(OwnerSearchState.waiting)
        await ans(
            "🔎 نام یا شناسهٔ نماینده را بفرستید.",
            reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
        )
        return
    await _dispatch_owner(action, ans, s)
    if action in _OWNER_TERMINAL:
        await _reshow_menu(message, s, message.from_user)
