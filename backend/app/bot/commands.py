"""
Telegram `/` command menus.

Scopes:
  * PRIVATE CHATS — reseller commands, shown only in private chats with the bot
                    (BotCommandScopeAllPrivateChats), so the `/` menu does NOT pop up
                    in GROUPS where the bot is a member/admin.
  * OWNER         — admin commands, scoped to the owner's private chat, so the owner
                    does NOT see reseller-only commands like /pay or /invoices.
The GROUP-CHATS scope is explicitly CLEARED so any commands previously registered
under the default scope stop appearing in groups.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

log = logging.getLogger("bot.commands")

# Mirrors the reseller inline menu (reseller_menu_keyboard) so the `/` list and the menu match.
RESELLER_COMMANDS = [
    BotCommand(command="menu", description="🏠 منوی اصلی"),
    BotCommand(command="invoices", description="🧾 فاکتورهای پرداخت‌نشده"),
    BotCommand(command="pay", description="💳 پرداخت فاکتور"),
    BotCommand(command="interim", description="📄 فاکتور علی‌الحساب (ماه جاری)"),
    BotCommand(command="panels", description="🖥 پنل‌های من"),
    BotCommand(command="subs", description="👥 زیرمجموعه‌ها"),
    BotCommand(command="support", description="💬 پیام به پشتیبانی"),
    BotCommand(command="removelink", description="🗑 حذف لینک‌ها"),
    BotCommand(command="help", description="❓ راهنما"),
]

# Mirrors the owner inline menu (owner_menu_keyboard).
OWNER_COMMANDS = [
    BotCommand(command="menu", description="🏠 منوی مدیریت"),
    BotCommand(command="stats", description="📊 آمار کلی"),
    BotCommand(command="debtors", description="💰 بدهکاران"),
    BotCommand(command="broadcast", description="📢 پیام همگانی"),
    BotCommand(command="sync", description="🔄 همگام‌سازی پنل‌ها"),
    BotCommand(command="backup", description="🗄 پشتیبان‌گیری اکنون"),
    BotCommand(command="help", description="❓ راهنما"),
]


async def apply_command_menus(bot: Bot, session: AsyncSession) -> None:
    """Show the reseller `/` menu ONLY in private chats, clear it in groups, and apply the
    owner menu in the owner's private chat."""
    # Reseller menu only in private chats (the bot is a PRIVATE-chat assistant).
    await bot.set_my_commands(RESELLER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    # Remove any `/` menu in GROUP chats so it doesn't pop up where the bot is just a
    # member/admin for the membership gate. Also clear the legacy default scope (older
    # installs registered the reseller menu there, which leaks into groups).
    for scope in (BotCommandScopeAllGroupChats(), BotCommandScopeDefault()):
        try:
            await bot.delete_my_commands(scope=scope)
        except Exception:  # noqa: BLE001
            log.warning("clearing group/default command menu failed", exc_info=True)

    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()
    if owner_chat.lstrip("-").isdigit():
        try:
            await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=int(owner_chat)))
            log.info("Owner command menu applied for chat %s", owner_chat)
        except Exception:  # noqa: BLE001
            log.warning("setting owner command menu failed", exc_info=True)


async def apply_owner_menu(bot: Bot, owner_chat_id: int) -> None:
    """Apply the owner-scoped menu for a freshly-identified owner chat."""
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_chat_id))
    except Exception:  # noqa: BLE001
        log.warning("apply_owner_menu failed", exc_info=True)
