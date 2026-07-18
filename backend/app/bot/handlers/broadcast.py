"""Owner broadcast (audience pick + background send) and the global cancel command/callback."""
from __future__ import annotations

import asyncio

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import BroadcastState, log, router
from app.bot.rtl import rtl
from app.models import Panel

# --------------------------- broadcast (owner) ---------------------------
_AUDIENCE_FA = {
    "all": "همه نمایندگان",
    "debtors": "بدهکاران",
    "overdue": "سررسیدگذشته‌ها",
    "deferred": "مهلت‌دارها (هنوز به مهلت نرسیده)",
    "zero_sale": "فروش صفر این ماه",
}


async def _audience_label(session, audience: str, panel_id: int | None) -> str:
    if audience == "panel" and panel_id is not None:
        panel = await session.get(Panel, panel_id)
        return f"نمایندگان پنل {panel.key}" if panel else "نمایندگان یک پنل"
    return _AUDIENCE_FA.get(audience, audience)


async def _bot_broadcast_bg(text: str, reachable: list, unregistered: int) -> None:
    """Background send for an owner bot-triggered broadcast — its own session/bot (the handler's
    session is already closed). The final summary reaches the owner via owner_notify.notify_owner."""
    try:
        async with common.SessionLocal() as session:
            from app.services import broadcast as bc

            await bc.run_broadcast(session, text, reachable, unregistered=unregistered)
    except Exception:  # noqa: BLE001
        log.exception("bot background broadcast failed")


async def _do_broadcast(
    message: Message, session, text: str, audience: str = "all", panel_id: int | None = None
) -> None:
    from app.services import broadcast as bc

    reachable, unregistered = await bc.resolve_recipients(session, audience, panel_id, None)
    label = await _audience_label(session, audience, panel_id)
    if not text.strip() or not reachable:
        await message.answer(rtl(f"📢 «{label}»: گیرنده‌ای برای ارسال نبود."))
        return
    # Send in the background (bounded concurrency + rate limit); reply immediately. The summary
    # arrives here when it finishes (notify_owner targets the owner's PV = this chat).
    asyncio.create_task(_bot_broadcast_bg(text, reachable, len(unregistered)))
    note = [f"📣 ارسال به «{label}» در پس‌زمینه شروع شد — {len(reachable)} گیرنده."]
    if unregistered:
        note.append(f"ℹ️ {len(unregistered)} نماینده در ربات ثبت‌نام نکرده‌اند (پیام نمی‌گیرند).")
    note.append("خلاصهٔ نتیجه پس از پایان، همین‌جا ارسال می‌شود.")
    await message.answer(rtl("\n".join(note)))


@router.callback_query(F.data.startswith("bcaud:"))
async def cb_broadcast_audience(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")  # bcaud:all | bcaud:panel | bcaud:panel:<id>
    audience = parts[1]
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        # "panel" with no id → show a panel picker first.
        if audience == "panel" and len(parts) < 3:
            panels = (
                await s.execute(select(Panel.id, Panel.key).where(Panel.enabled.is_(True)).order_by(Panel.key))
            ).all()
            await cb.message.answer(
                "🖥 پیام به نمایندگانِ کدام پنل ارسال شود؟",
                reply_markup=keyboards.broadcast_panel_keyboard([(pid, key) for pid, key in panels]),
            )
            await cb.answer()
            return
        panel_id = int(parts[2]) if (audience == "panel" and len(parts) >= 3) else None
        label = await _audience_label(s, audience, panel_id)
    await state.set_state(BroadcastState.waiting)
    await state.update_data(audience=audience, panel_id=panel_id)
    await cb.message.answer(
        f"📢 گیرنده: «{label}»\n"
        f"اکنون متن پیام را ارسال کنید:",
        reply_markup=keyboards.flow_cancel_kb(),
    )
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Always restore the menu, even when no flow was active (see on_cancel_label)."""
    await state.clear()
    await message.answer("لغو شد.")
    async with common.SessionLocal() as s:
        await common._reshow_menu(message, s, message.from_user)


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Universal exit button carried by every FSM prompt: clear the state and return the role-aware
    main menu, so a user can ALWAYS tap their way out (never forced to remember /cancel)."""
    await state.clear()
    async with common.SessionLocal() as s:
        await common._send_menu(cb.message.answer, s, cb.from_user, bot=bot)
    await cb.answer("لغو شد.")
