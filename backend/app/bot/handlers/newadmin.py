"""Owner-only «➕ نمایندهٔ جدید» — create a reseller (a Hiddify admin) in two taps + a name.

Panel picker → name → the reseller's panel link. The row is written locally at creation time
(`services/resellercreate.py`), so the reseller can register in the bot immediately instead of
waiting for the next panel sync.
"""
from __future__ import annotations

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import NewResellerState, _reshow_menu, router
from app.bot.handlers.views import _begin_new_reseller, _send_new_reseller
from app.bot.rtl import rtl
from app.models import Panel
from app.services import resellercreate


@router.message(Command("newadmin"))
async def cmd_new_admin(message: Message, command: CommandObject, state: FSMContext) -> None:
    """`/newadmin` — the panel is always asked, so a name given inline is kept for the last step."""
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return  # owner-only; resellers don't see this command
        await _begin_new_reseller(message.answer, s, state)
    name = resellercreate.clean_name(command.args)
    if name:
        await state.update_data(na_name=name)


@router.callback_query(F.data.startswith("napanel:"))
async def cb_new_admin_panel(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        panel_id = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        await cb.answer()
        return
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        panel = await s.get(Panel, panel_id)
        if panel is None or not panel.enabled:
            await cb.answer("این پنل در دسترس نیست.", show_alert=True)
            return
        # A name typed on the /newadmin line skips the last step entirely.
        data = await state.get_data()
        pending = data.get("na_name")
        if pending:
            await state.clear()
            await cb.answer()
            await _send_new_reseller(cb.message.answer, s, panel, pending)
            await _reshow_menu(cb.message, s, cb.from_user)
            return
        panel_key = panel.key
        await state.set_state(NewResellerState.name)
        await state.update_data(na_panel_id=panel_id)
    await cb.message.answer(
        rtl(f"✍️ نامِ نمایندهٔ جدید روی پنلِ {panel_key} را بفرستید:"),
        reply_markup=keyboards.flow_cancel_kb(),
    )
    await cb.answer()


@router.message(NewResellerState.name)
async def on_new_admin_name(message: Message, state: FSMContext) -> None:
    name = resellercreate.clean_name(message.text)
    if not name:
        await message.answer(rtl("نامِ معتبر بفرستید (یا «✖️ انصراف» را بزنید)."))
        return
    data = await state.get_data()
    panel_id = int(data.get("na_panel_id") or 0)
    await state.clear()
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return
        panel = await s.get(Panel, panel_id)
        if panel is None or not panel.enabled:
            await message.answer(rtl("این پنل دیگر در دسترس نیست؛ دوباره شروع کنید."))
        else:
            await _send_new_reseller(message.answer, s, panel, name)
        await _reshow_menu(message, s, message.from_user)
