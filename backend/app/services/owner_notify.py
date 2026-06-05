"""Send activity-log / alert messages to the owner's Telegram PV."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.services import settings_service

log = logging.getLogger("owner_notify")


async def notify_owner(session: AsyncSession, text: str, *, html: bool = False) -> bool:
    """Send a message to the owner's chat. Returns True if delivered.

    `html=True` allows clickable tg://user?id=... links for affected resellers."""
    owner_chat = await settings_service.get(session, "owner_chat_id", "") or ""
    if not owner_chat:
        log.info("notify_owner skipped: owner_chat_id not set yet")
        return False
    bot = await build_bot(session)
    if bot is None:
        return False
    from app.bot.rtl import rtl

    try:
        await bot.send_message(
            int(owner_chat), rtl(text),
            parse_mode="HTML" if html else None,
            disable_web_page_preview=True,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("notify_owner failed", exc_info=True)
        return False
    finally:
        await bot.session.close()


def user_link(reseller) -> str:
    """An HTML link to a reseller's Telegram profile (clickable in the owner's chat)."""
    label = reseller.name or str(reseller.id)
    if reseller.bot_chat_id:
        return f"<a href='tg://user?id={reseller.bot_chat_id}'>{label}</a>"
    return label
