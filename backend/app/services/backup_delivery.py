"""Send the system backup ZIP to the owner's Telegram PV (scheduled + on demand)."""
from __future__ import annotations

import logging

from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.services import backup, settings_service

log = logging.getLogger("backup.delivery")


async def send_backup_to_owner(session: AsyncSession) -> dict:
    chat_id = await settings_service.get(session, "owner_chat_id", "") or ""
    if not chat_id:
        # Save locally anyway so it's not lost.
        path = await backup.save_backup_to_disk(session)
        return {"status": "no_owner_chat", "saved": str(path)}

    data, name = await backup.create_backup(session)
    bot = await build_bot(session)
    if bot is None:
        path = await backup.save_backup_to_disk(session)
        return {"status": "no_bot", "saved": str(path)}
    try:
        await bot.send_document(
            int(chat_id), BufferedInputFile(data, filename=name),
            caption="🗄 پشتیبان خودکار سامانه\nبرای بازیابی، این فایل را در بخش «پشتیبان‌گیری» پنل بارگذاری کنید.",
        )
    finally:
        await bot.session.close()
    log.info("Backup sent to owner (%s bytes)", len(data))
    return {"status": "sent", "filename": name, "size": len(data)}
