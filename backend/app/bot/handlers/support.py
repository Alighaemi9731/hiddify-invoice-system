"""Support chat (no DB storage), owner reply, and the broadcast command/text intake."""
from __future__ import annotations

import html

from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.broadcast import _do_broadcast
from app.bot.handlers.common import (
    BroadcastState,
    OwnerReplyState,
    SupportState,
    _reshow_menu,
    router,
)
from app.services import settings_service


# --------------------------- support chat (no DB storage) ---------------------------
@router.message(SupportState.waiting)
async def on_support_text(message: Message, state: FSMContext) -> None:
    """Relay a reseller's support message to the owner. Nothing is stored in the DB —
    the owner replies live via the inline button, which carries the user id."""
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("پیام خالی بود؛ لغو شد.")
        return
    async with common.SessionLocal() as s:
        owner_chat = await settings_service.get(s, "owner_chat_id", "") or ""
        bot = message.bot
        u = message.from_user
        if not owner_chat:
            await message.answer("در حال حاضر پشتیبانی در دسترس نیست. بعداً تلاش کنید.")
            return
        await bot.send_message(
            int(owner_chat),
            _support_message_html(u, text),
            reply_markup=keyboards.support_reply_keyboard(u.id, message.message_id),
            parse_mode="HTML",
        )
        await message.answer("✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ می‌گیرید.")
        await _reshow_menu(message, s, message.from_user)


def _support_message_html(user, text: str) -> str:
    """Build owner-facing support HTML with every Telegram/user value escaped."""
    handle = (
        f"@{html.escape(user.username)}"
        if user.username
        else f"<a href='tg://user?id={user.id}'>{html.escape(str(user.first_name or user.id))}</a>"
    )
    return (
        f"💬 پیام پشتیبانی\nاز: {handle} (id: <code>{user.id}</code>)\n\n"
        f"{html.escape(text)}"
    )


@router.callback_query(F.data.startswith("sup:"))
async def cb_support_reply(cb: CallbackQuery, state: FSMContext) -> None:
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    parts = cb.data.split(":")
    target = int(parts[1])
    reply_to = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    await state.set_state(OwnerReplyState.waiting)
    await state.update_data(target=target, reply_to=reply_to)
    await cb.message.answer(f"پاسخ خود را برای کاربر <code>{target}</code> بنویسید:",
                            parse_mode="HTML", reply_markup=keyboards.cancel_keyboard())
    await cb.answer()


@router.message(OwnerReplyState.waiting)
async def on_owner_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    target = data.get("target")
    reply_to = data.get("reply_to")
    text = (message.text or "").strip()
    if not target or not text:
        await message.answer("پاسخ ارسال نشد.")
        return
    body = f"💬 پاسخ پشتیبانی:\n\n{text}"
    try:
        if reply_to:
            # Quote the user's original message. If it was deleted, Telegram errors,
            # so fall back to a plain message.
            try:
                await message.bot.send_message(int(target), body, reply_to_message_id=int(reply_to))
            except TelegramForbiddenError:
                raise
            except Exception:  # noqa: BLE001
                await message.bot.send_message(int(target), body)
        else:
            await message.bot.send_message(int(target), body)
        await message.answer("✅ پاسخ ارسال شد.")
    except TelegramForbiddenError:
        # The user blocked the bot (or deleted their account) — say that plainly instead of
        # dumping the raw English API error on the owner.
        await message.answer("⛔️ ارسال نشد: این کاربر ربات را مسدود کرده یا حسابش حذف شده است.")
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            await message.answer("⛔️ ارسال نشد: گفتگویی با این کاربر پیدا نشد (شاید هرگز ربات را استارت نکرده است).")
        else:
            await message.answer(f"ارسال پاسخ ناموفق بود: {exc}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"ارسال پاسخ ناموفق بود: {exc}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, command: CommandObject) -> None:
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return
        if command.args:
            await _do_broadcast(message, s, command.args, "all")
        else:
            await message.answer("📢 گیرندگان پیام همگانی را انتخاب کنید:",
                                 reply_markup=keyboards.broadcast_audience_keyboard())


@router.message(BroadcastState.waiting)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    audience = data.get("audience", "all")
    panel_id = data.get("panel_id")
    await state.clear()
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return
        if not (message.text or "").strip():
            await message.answer("متن خالی بود؛ لغو شد.")
            return
        await _do_broadcast(message, s, message.text, audience, panel_id)
        await _reshow_menu(message, s, message.from_user)
