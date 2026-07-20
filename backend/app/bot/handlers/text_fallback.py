"""Free text (link / txid) + cold photo/document fallback.

MUST be imported (= registered) LAST: `on_text` is the catch-all that every other
message handler must out-rank.
"""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import _SETCHAT_RE, _parse_txid, _track_user, router
from app.bot.handlers.intake import _handle_link
from app.bot.matching import parse_link
from app.bot.rtl import rtl
from app.services import settings_service


async def _reprompt_if_in_flow(message: Message, state: FSMContext) -> bool:
    """Flows are LOCKED: a stray message (photo/file/text not matched by the flow's own handler)
    while a flow is active must NOT do its cold-path thing — re-prompt the docked cancel instead.
    Returns True if handled (caller should return)."""
    if await state.get_state() is not None:
        await message.answer(
            "شما در حال یک عملیات هستید؛ ورودیِ خواسته‌شده را بفرستید یا «✖️ انصراف» را بزنید.",
            reply_markup=keyboards.flow_cancel_kb(),
        )
        return True
    return False


# --------------------------- free text (link / txid) ---------------------------
@router.message(F.document)
async def on_document(message: Message, state: FSMContext) -> None:
    """Owner sends a backup .zip → restore the system from it."""
    if await _reprompt_if_in_flow(message, state):
        return
    async with common.SessionLocal() as session:
        if not await common._is_owner_user(session, message.from_user):
            # A reseller who sent a screenshot as an uncompressed FILE → nudge them to send
            # it as a photo after choosing the invoice in the locked payment flow.
            d = message.document
            fn = (d.file_name or "").lower()
            if (d.mime_type or "").startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp")):
                await message.answer(
                    "برای ثبت رسید پرداخت، لطفاً تصویر را به‌صورت «عکس» ارسال کنید (نه فایل)."
                )
            return
    doc = message.document
    if not (doc.file_name or "").endswith(".zip"):
        await message.answer("برای بازیابی، فایل پشتیبان با پسوند .zip ارسال کنید.")
        return
    await message.answer("⏳ در حال بازیابی از فایل پشتیبان...")
    try:
        import asyncio

        from app.services import backup as backup_service

        # The document's CAPTION is the passphrase. This is the only way to restore an encrypted
        # backup onto a NEW server — the flagship use case — because a fresh install's own
        # `backup_passphrase` setting is empty, so the stored value below is useless there. The
        # stored setting stays as a convenience fallback for restoring onto the same server.
        caption = (message.caption or "").strip()
        passphrase = caption or None
        if passphrase is None:
            async with common.SessionLocal() as session:
                passphrase = await settings_service.get(session, "backup_passphrase", "") or None
        buf = await message.bot.download(doc)
        # Blocking dump/restore subprocess work runs off the event loop.
        result = await asyncio.to_thread(
            backup_service.restore_from_zip, buf.read(), passphrase=passphrase
        )
        if result.get("restored"):
            # Drop the bot's pooled connections so it reconnects to the restored data; the
            # restore wrote a restart marker, so this process (and the backend) self-restart
            # shortly via the restart watcher to pick up the restored data / SECRET_KEY.
            from app.core.db import engine

            await engine.dispose()
        await message.answer(
            f"✅ بازیابی انجام شد ({result.get('db_kind')}).\n{result.get('note', '')}"
        )
    except Exception as exc:  # noqa: BLE001
        # An encrypted archive with no usable passphrase is the ONE failure the owner can fix
        # themselves — but only if they're told how. Without this hint the caption mechanism is
        # undiscoverable and the restore looks simply broken.
        text = str(exc)
        if "رمزگذاری شده" in text or "گذرواژه" in text:
            await message.answer(rtl(
                f"❌ بازیابی ناموفق بود: {text}\n\n"
                "🔑 این فایل پشتیبان رمزگذاری شده است. همان فایل را دوباره بفرستید و "
                "گذرواژه را در «کپشن» (متن همراه فایل) بنویسید."
            ))
            return
        await message.answer(f"❌ بازیابی ناموفق بود: {exc}")


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    """A cold photo (NOT inside the pay flow) is NOT a payment — it would create stray,
    unattributed payments. Guide the customer to the «فاکتور و پرداخت» menu option instead. (Inside
    PayState, the receipt is handled by pay_state_photo.)"""
    if await _reprompt_if_in_flow(message, state):
        return
    async with common.SessionLocal() as session:
        await _track_user(session, message.from_user)
    await message.answer(
        "📸 برای ثبتِ پرداخت، ابتدا از منو روی «🧾 فاکتور و پرداخت» بزنید و فاکتور(ها) را انتخاب کنید، "
        "سپس تصویرِ رسید را بفرستید."
    )

@router.message(F.text)
async def on_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if await _reprompt_if_in_flow(message, state):
        return
    text = (message.text or "").strip()
    async with common.SessionLocal() as session:
        await _track_user(session, message.from_user)
        # Owner manual fallback for a PRIVATE GROUP that can't be forwarded: «group -100…»
        # (or «channel -100…»). Telegram hides a group as the forward origin, so this lets
        # the owner paste the numeric id directly.
        m = _SETCHAT_RE.match(text)
        if m and await common._is_owner_user(session, message.from_user):
            kind = m.group(1).lower()
            chat_id = m.group(2)
            is_channel = kind in ("channel", "کانال")
            key = "announcement_channel_id" if is_channel else "announcement_group_id"
            await settings_service.set_value(session, key, chat_id)
            label = "کانال" if is_channel else "گروه"
            await message.answer(
                f"✅ {label} با شناسهٔ {chat_id} ثبت شد.\n"
                f"برای اجباری‌کردن عضویت، کلید «عضویت اجباری {label}» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین آن باشد."
            )
            return
        from app.services import payment_methods

        opts = await payment_methods.load_options(session)
        # A cold TXID/link (NOT inside the pay flow) is NOT a payment — guide them to the button
        # instead of silently recording a stray payment. (Inside PayState, pay_state_text handles it.)
        if _parse_txid(text, usdt=opts.usdt, ton=opts.ton, avax=opts.avax):
            await message.answer(
                "برای ثبتِ پرداخت، ابتدا از منو روی «🧾 فاکتور و پرداخت» بزنید و فاکتور(ها) را انتخاب کنید، "
                "سپس شناسهٔ تراکنش را بفرستید."
            )
            return
        parsed = parse_link(text)
        if parsed:
            await _handle_link(message, session, parsed)
            return
        # Any other text / mistyped command → show the right main menu (owner vs reseller),
        # but a non-member gets the join prompt (with a clickable /start), not the menu.
        await common._send_menu(message.answer, session, message.from_user, bot=bot)
