"""Send activity-log / alert messages to the owner's Telegram PV."""
from __future__ import annotations

import html
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.services import settings_service

log = logging.getLogger("owner_notify")


async def notify_owner(
    session: AsyncSession, text: str, *, html: bool = False, reply_markup=None
) -> bool:
    """Send a message to the owner's chat. Returns True if delivered.

    `html=True` allows clickable tg://user?id=... links for affected resellers.
    `reply_markup` attaches inline buttons (e.g. approve/reject under a pending payment)."""
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
            reply_markup=reply_markup,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("notify_owner failed", exc_info=True)
        return False
    finally:
        await bot.session.close()


async def notify_owner_photo(
    session: AsyncSession, photo_path: str, caption: str, *, reply_markup=None
) -> bool:
    """Send a local image (e.g. an uploaded payment receipt) to the owner's chat with an
    HTML caption + optional inline buttons. Used by the web portal, which has no Telegram
    file_id to forward (unlike the bot). Returns True if delivered."""
    from aiogram.types import FSInputFile

    owner_chat = await settings_service.get(session, "owner_chat_id", "") or ""
    if not owner_chat:
        return False
    bot = await build_bot(session)
    if bot is None:
        return False
    from app.bot.rtl import rtl

    try:
        await bot.send_photo(
            int(owner_chat), FSInputFile(photo_path), caption=rtl(caption),
            parse_mode="HTML", reply_markup=reply_markup,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("notify_owner_photo failed", exc_info=True)
        return False
    finally:
        await bot.session.close()


def user_link(reseller, *, username: str | None = None) -> str:
    """An HTML link to a reseller's Telegram profile (clickable in the owner's chat). Prefers
    t.me/<username> (more reliable) when a username is known, else tg://user?id=<bot_chat_id>; plain
    escaped name if neither is available. The name is HTML-escaped so `<`, `>`, `&` can't break the
    message or inject markup."""
    from app.core.tg_links import tg_pv_url

    label = html.escape(reseller.name or str(reseller.id))
    url = tg_pv_url(username, getattr(reseller, "bot_chat_id", None))
    return f"<a href='{url}'>{label}</a>" if url else label
