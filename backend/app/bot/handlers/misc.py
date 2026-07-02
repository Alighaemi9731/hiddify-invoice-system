"""Forwarded channel post — the owner registers the announcement channel/group."""
from __future__ import annotations

from aiogram import F
from aiogram.types import Message

from app.bot.handlers import common
from app.bot.handlers.common import log, router
from app.services import settings_service


# --------------------------- forwarded channel post (owner) ---------------------------
@router.message(F.forward_origin)
async def on_forward(message: Message) -> None:
    chat = getattr(message.forward_origin, "chat", None)
    if not chat or chat.type not in ("channel", "supergroup", "group"):
        return
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, message.from_user):
            await message.answer("فقط مالک سیستم می‌تواند کانال/گروه را تنظیم کند.")
            return
        # A broadcast channel → the announcement channel; a group/supergroup → the group.
        # (A supergroup IS a group in Telegram; only a real broadcast channel has type
        # "channel", so this split is reliable.)
        if chat.type == "channel":
            await settings_service.set_value(session, "announcement_channel_id", str(chat.id))
            reply = (
                f"✅ کانال اطلاع‌رسانی ثبت شد:\n{chat.title}\nid: {chat.id}\n"
                "برای اجباری‌کردن عضویت، کلید «عضویت اجباری کانال» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین این کانال باشد."
            )
            # If this channel has a linked DISCUSSION GROUP, register it automatically — you
            # can't forward FROM a group (Telegram hides the group as the forward origin),
            # so reading the channel's linked_chat is the reliable way to capture a private
            # group. Needs the bot to be in (ideally admin of) the channel.
            try:
                full = await message.bot.get_chat(chat.id)
                linked = getattr(full, "linked_chat_id", None)
                if linked:
                    await settings_service.set_value(session, "announcement_group_id", str(linked))
                    reply += (
                        f"\n\n🔗 گروه گفتگوی متصل به این کانال هم خودکار ثبت شد (id: {linked}).\n"
                        "برای اجباری‌کردن عضویت گروه، کلید «عضویت اجباری گروه» را روشن کنید."
                    )
            except Exception:  # noqa: BLE001 — best-effort; the manual path below still works
                log.info("could not read linked_chat for channel %s", chat.id, exc_info=True)
            await message.answer(reply)
        else:
            await settings_service.set_value(session, "announcement_group_id", str(chat.id))
            await message.answer(
                f"✅ گروه ثبت شد:\n{chat.title}\nid: {chat.id}\n"
                "برای اجباری‌کردن عضویت، کلید «عضویت اجباری گروه» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین این گروه باشد."
            )
