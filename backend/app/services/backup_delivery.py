"""Send the system backup ZIP to the owner's Telegram PV (scheduled + on demand)."""
from __future__ import annotations

import logging
import os

from aiogram.types import FSInputFile
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

    # Built ON DISK and uploaded straight from there (`FSInputFile`), so neither the raw dump
    # nor the finished archive is ever held in this process's memory.
    built, name = await backup.create_backup_file(session)
    try:
        return await _deliver(session, built, name)
    finally:
        backup.discard_temp_archive(built)


def _keep_on_disk(built, name: str) -> str:  # noqa: ANN001
    """Move an already-built archive into the backups folder and return its path.

    Same bounded retention as `backup.save_backup_to_disk`: the newest copy replaces the
    oldest, so this folder can never grow without limit."""
    backup.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = backup.BACKUP_DIR / name
    os.replace(built, path)
    for old in sorted(backup.BACKUP_DIR.glob("invoice-backup-*.zip"))[
        : -backup._KEEP_LOCAL_BACKUPS
    ]:
        old.unlink(missing_ok=True)
    return str(path)


async def _deliver(session: AsyncSession, built, name: str) -> dict:  # noqa: ANN001
    chat_id = await settings_service.get(session, "owner_chat_id", "") or ""
    size = built.stat().st_size
    bot = await build_bot(session)
    if bot is None:
        path = _keep_on_disk(built, name)
        await backup.mark_backup_done(session)
        return {"status": "no_bot", "saved": path}

    # Telegram rejects a bot document over 50 MB. Without this check the send simply raised and
    # the ONLY copy of the backup was thrown away with the temp dir — automated off-site backup
    # would stop dead the month the archive crossed the line. Keep it on the server instead and
    # say so plainly. Deliberately NOT stamped as a completed backup: a copy that lives only on
    # the machine it is protecting is not off-site protection, and «آخرین پشتیبان» going stale
    # is the signal that surfaces this in the health panel and the daily digest.
    if size > backup.TELEGRAM_DOC_LIMIT_BYTES:
        await bot.session.close()
        path = _keep_on_disk(built, name)
        mb = size / (1024 * 1024)
        log.error("backup is %.1f MB — over Telegram's limit; kept on disk at %s", mb, path)
        from app.services import owner_notify

        await owner_notify.notify_owner(
            session,
            f"⚠️ پشتیبان امروز ({mb:.1f} مگابایت) از حدِ مجازِ تلگرام بزرگ‌تر است و ارسال نشد.\n"
            f"فایل روی سرور نگهداری شد:\n<code>{path}</code>\n\n"
            "لطفاً آن را از «پشتیبان‌گیری → دریافت فایل» در پنل دانلود و جای امن نگه دارید. "
            "برای کوچک‌تر شدنِ پشتیبان می‌توانید بازهٔ نگهداریِ گزارش‌ها را در تنظیمات کم کنید.",
            html=True,
        )
        return {"status": "too_large", "saved": path, "size": size}
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
            "\n\n⚠️ این پشتیبان رمزنگاری‌نشده است و کلیدِ رمزِ سامانه (اطلاعات محرمانه) را در خود دارد؛ "
            "لطفاً آن را در جای امن نگه دارید. برای رمزنگاری، در «تنظیمات → زمان‌بندی» یک «گذرواژهٔ رمزگذاری پشتیبان» "
            "تعیین کنید."
        )
    try:
        # FSInputFile streams the archive off disk in chunks — `BufferedInputFile` needed the
        # whole thing resident, on top of the copy the caller was already holding.
        await bot.send_document(
            int(chat_id), FSInputFile(built, filename=name), caption=caption,
        )
    finally:
        await bot.session.close()
    # Only reached when send_document didn't raise → a real successful delivery. (On failure the
    # exception propagates and the scheduler's backup_job already alerts the owner — no stamp.)
    await backup.mark_backup_done(session)
    log.info("Backup sent to owner (%s bytes)", size)
    return {"status": "sent", "filename": name, "size": size}
