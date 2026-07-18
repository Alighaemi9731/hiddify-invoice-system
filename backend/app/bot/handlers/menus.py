"""Persistent docked main menu (reply keyboard): the label router + role dispatchers."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import (
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


# --------------------------- lean reply-keyboard menu: cancel + label routers ---------------------------
# The «✖️ انصراف» handler is registered FIRST so cancel always wins — it exits any active flow and
# re-docks the role menu. This is the ONLY way (besides /start) to leave a locked flow.
@router.message(F.text == keyboards.CANCEL_LABEL)
async def on_cancel_label(message: Message, state: FSMContext) -> None:
    """«✖️ انصراف» must ALWAYS hand the menu back — never only when a flow happened to be active.

    Relying on the re-dock middleware left anyone whose flow state had vanished (a bot restart wipes
    the in-memory FSM) stuck with the cancel-only keyboard: every tap answered «لغو شد.» and restored
    nothing, with no way out but /start."""
    await state.clear()
    await message.answer("لغو شد.")
    async with common.SessionLocal() as s:
        await _reshow_menu(message, s, message.from_user)


# Registered before every FSM text handler. UNLIKE before, a menu-label tap DURING a flow no longer
# cancels it — the flow is LOCKED: only «✖️ انصراف» exits. A stray label mid-flow re-prompts cancel.
@router.message(F.text.in_(keyboards.ALL_MENU_LABELS))
async def on_menu_label(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    # LOCK: while a flow is active, refuse menu navigation and re-prompt the docked cancel button.
    if await state.get_state() is not None:
        await message.answer(
            "شما در حال یک عملیات هستید؛ برای خروج «✖️ انصراف» را بزنید و سپس گزینهٔ دیگری را انتخاب کنید.",
            reply_markup=keyboards.flow_cancel_kb(),
        )
        return
    async with common.SessionLocal() as s:
        await _track_user(s, message.from_user)
        is_owner = await common._is_owner_user(s, message.from_user)
        if text == keyboards.MORE_LABEL:
            await _show_more(message, s, is_owner)
            return
        if text == keyboards.BACK_LABEL:
            await common._send_menu(message.answer, s, message.from_user, bot=bot)
            return
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


async def _show_more(message: Message, s, is_owner: bool) -> None:  # noqa: ANN001
    """«⋯ بیشتر» → swap the docked keyboard to the role's «بیشتر» keyboard (+ «بازگشت»)."""
    if is_owner:
        await message.answer("گزینه‌های بیشتر:", reply_markup=keyboards.owner_more_reply_kb())
        return
    can_storefront = await common._can_setup_storefront(s, message.from_user.id)
    await message.answer("گزینه‌های بیشتر:",
                         reply_markup=keyboards.reseller_more_reply_kb(show_storefront=can_storefront))


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
    elif action == "storefront":
        await _begin_storefront_setup(ans, cid, s, state)
    elif action == "portal":
        await _send_portal_link(ans, cid, s)
    elif action == "removelink":
        await _send_removelink(ans, cid, s)
    elif action == "support":
        await state.set_state(SupportState.waiting)
        await ans("پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.flow_cancel_kb())
    elif action == "register":
        await ans(
            "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
            reply_markup=keyboards.flow_cancel_kb(),
        )
    elif action == "newuser":
        await _begin_create_user(ans, cid, s, state)


async def _do_owner_menu(action: str, message: Message, state: FSMContext, s) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "search":
        await state.set_state(OwnerSearchState.waiting)
        await ans("🔎 نام یا شناسهٔ نماینده را بفرستید.", reply_markup=keyboards.flow_cancel_kb())
        return
    await _dispatch_owner(action, ans, s)
