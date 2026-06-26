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
        await backup.mark_backup_done(session)
        return {"status": "no_owner_chat", "saved": str(path)}

    data, name = await backup.create_backup(session)
    bot = await build_bot(session)
    if bot is None:
        path = await backup.save_backup_to_disk(session)
        await backup.mark_backup_done(session)
        return {"status": "no_bot", "saved": str(path)}
    # The backup embeds the system's SECRET_KEY (so it can be restored on another server). When no
    # backup passphrase is set the archive is UNENCRYPTED, so anyone who gets the file can read every
    # secret. Nudge the owner to set a passphrase (we don't block — it would disable auto-backup).
    passphrase = (await settings_service.get(session, "backup_passphrase", "") or "").strip()
    caption = (
        "🗄 پشتیبان خودکار سامانه\n"
        "برای بازیابی، این فایل را در بخش «پشتیبان‌گیری» پنل بارگذاری کنید."
    )
    if not passphrase:
        log.warning("auto-backup is delivered UNENCRYPTED (no backup_passphrase set)")
        caption += (
            "\n\n⚠️ این پشتیبان رمزنگاری‌نشده است و شاملِ کلیدِ رمزِ سامانه (رازها) می‌شود؛ "
            "آن را جای امن نگه دارید. برای رمزنگاری، در «تنظیمات → زمان‌بندی» یک «عبارت عبور پشتیبان» "
            "تعیین کنید."
        )
    try:
        await bot.send_document(
            int(chat_id), BufferedInputFile(data, filename=name), caption=caption,
        )
    finally:
        await bot.session.close()
    # Only reached when send_document didn't raise → a real successful delivery. (On failure the
    # exception propagates and the scheduler's backup_job already alerts the owner — no stamp.)
    await backup.mark_backup_done(session)
    log.info("Backup sent to owner (%s bytes)", len(data))
    return {"status": "sent", "filename": name, "size": len(data)}
