"""Owner-only «🧪 کانفیگ تست» — one tap builds a test config on the configured panel.

No FSM: every entry point ends in `_send_test_config`, which either builds immediately or shows the
panel picker once. The picker SAVES the choice (`test_config_panel_id`), so the next tap is silent.
"""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import router
from app.bot.handlers.views import _send_test_config
from app.bot.rtl import rtl


@router.message(Command("test"))
async def cmd_test(message: Message, command: CommandObject, bot: Bot) -> None:
    """`/test` (optionally `/test <name>` to name the config for a specific customer)."""
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return  # owner-only; resellers don't see this command
        await _send_test_config(message.answer, message.chat.id, s, bot=bot,
                                name=(command.args or "").strip() or None)


@router.callback_query(F.data == "tcnew")
async def cb_tc_new(cb: CallbackQuery, bot: Bot) -> None:
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.answer()
        await _send_test_config(cb.message.answer, cb.message.chat.id, s, bot=bot)


@router.callback_query(F.data == "tcpick")
async def cb_tc_pick(cb: CallbackQuery) -> None:
    """Open the panel picker on demand (the choice is a setting, so this changes it for good)."""
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import testconfig

        items = await testconfig.panel_choices(s)
    if not items:
        await cb.message.answer(rtl("هیچ پنلِ فعالی ثبت نشده است."))
    else:
        await cb.message.answer(rtl("🖥 کانفیگ‌های تست از کدام پنل ساخته شوند؟"),
                                reply_markup=keyboards.test_config_panels_keyboard(items))
    await cb.answer()


@router.callback_query(F.data.startswith("tcpanel:"))
async def cb_tc_panel(cb: CallbackQuery, bot: Bot) -> None:
    """Save the picked panel, then build a config right away — picking is never a dead end."""
    try:
        panel_id = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        await cb.answer()
        return
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.models import Panel
        from app.services import testconfig

        panel = await s.get(Panel, panel_id)
        if panel is None or not panel.enabled:
            await cb.answer("این پنل در دسترس نیست.", show_alert=True)
            return
        await testconfig.set_panel(s, panel_id)
        await cb.answer("پنلِ تست ذخیره شد.")
        await _send_test_config(cb.message.answer, cb.message.chat.id, s, bot=bot)
