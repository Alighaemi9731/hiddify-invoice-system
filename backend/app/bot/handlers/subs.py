"""Sub-reseller management: list/detail, enforce/freeze/restore, invoice, monthly GB cap."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import OwnerCapBumpState, SubCapState, router
from app.bot.handlers.views import (
    _owns_sub,
    _send_sub_detail,
    _send_sub_invoice,
    _send_sub_list,
    _send_sub_panels,
)
from app.models import Reseller
from app.models.enums import EnforcementActionStatus, EnforcementState


# --------------------------- sub-reseller management ---------------------------
@router.callback_query(F.data == "menu:subs")
async def cb_subs(cb: CallbackQuery) -> None:
    async with common.SessionLocal() as s:
        await _send_sub_panels(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: sub-panel picker (tap to manage) — a trailing menu would bury it.
    await cb.answer()


@router.callback_query(F.data.startswith("subp:"))
async def cb_sub_panel(cb: CallbackQuery) -> None:
    parent_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        await _send_sub_list(cb.message.answer, cb.from_user.id, parent_id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("subv:"))
async def cb_sub_view(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        await _send_sub_detail(cb.message.answer, cb.from_user.id, sub_id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("subx:"))
async def cb_sub_enforce(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.message.answer(f"⏳ مسدودسازی «{sub.name}» در صف قرار می‌گیرد...")
        from app.services import enforcement

        # Reseller-initiated manual action → force the real write (dry_run=False),
        # independent of the global automatic-dunning enforcement switch.
        action = await enforcement.enforce_reseller(s, sub, dry_run=False)
        if action.status in (
            EnforcementActionStatus.planned,
            EnforcementActionStatus.partial,
        ):
            msg = (
                f"⏳ مسدودسازی «{sub.name}» در صف ثبت شد و به‌صورت مرحله‌ای انجام می‌شود."
            )
            await cb.message.answer(msg)
        elif action.status == EnforcementActionStatus.done:
            await cb.message.answer(f"⛔️ «{sub.name}» از قبل مسدود است.")
        else:
            await cb.message.answer(
                "❌ مسدودسازی ناموفق بود. مطمئن شوید کلید API پنل در تنظیمات ثبت شده است.\n"
                f"{action.error or ''}"
            )
    await cb.answer()


@router.callback_query(F.data.startswith("subf:"))
async def cb_sub_freeze(cb: CallbackQuery) -> None:
    """Limits-only freeze: block new-user creation (max_users→0) WITHOUT disabling existing users."""
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.message.answer(f"⏳ توقف ساخت سرویس برای «{sub.name}» در صف قرار می‌گیرد...")
        from app.services import enforcement

        # Reseller-initiated manual action → always a live write.
        action = await enforcement.freeze_reseller(s, sub)
        if action is None:
            await cb.message.answer("این زیرمجموعه از قبل محدود یا مسدود است.")
        elif action.status in (
            EnforcementActionStatus.planned,
            EnforcementActionStatus.partial,
        ):
            await cb.message.answer(
                f"⏳ «{sub.name}»: توقف ساخت سرویس در صف ثبت شد. سرویس‌های فعلیِ او قطع نمی‌شوند؛ "
                "فقط ساخت سرویس جدید و افزایش ظرفیت متوقف می‌شود."
            )
        elif action.status == EnforcementActionStatus.done:
            await cb.message.answer(f"🚫 «{sub.name}» از قبل محدود است.")
        else:
            await cb.message.answer(
                "❌ عملیات ناموفق بود. مطمئن شوید کلید API پنل در تنظیمات ثبت شده است.\n"
                f"{action.error or ''}"
            )
    await cb.answer()


@router.callback_query(F.data.startswith("subr:"))
async def cb_sub_restore(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        # State-aware wording: lifting a freeze vs lifting a full suspension.
        verb = (
            "رفع توقف ساخت سرویس"
            if sub.enforcement_state == EnforcementState.frozen
            else "آزادسازی"
        )
        await cb.message.answer(f"⏳ {verb} «{sub.name}» در صف قرار می‌گیرد...")
        from app.services import enforcement

        action = await enforcement.queue_restore(s, sub, reason="bot")
        if action is None:
            await cb.message.answer("این زیرمجموعه محدود یا مسدود نیست.")
        elif action.status in (
            EnforcementActionStatus.planned,
            EnforcementActionStatus.partial,
        ):
            await cb.message.answer(
                f"⏳ {verb} «{sub.name}» در صف ثبت شد و به‌صورت مرحله‌ای انجام می‌شود."
            )
        elif action.status == EnforcementActionStatus.done:
            await cb.message.answer(f"✅ «{sub.name}» از قبل آزاد شده است.")
        else:
            await cb.message.answer(f"❌ {verb} ناموفق بود.\n{action.error or ''}")
    await cb.answer()


@router.callback_query(F.data.startswith("subinv:"))
async def cb_sub_invoice(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    sub_id, period_label = int(parts[1]), parts[2]
    await cb.answer("در حال ساخت فاکتور…")
    async with common.SessionLocal() as s:
        await _send_sub_invoice(cb.message.answer, cb.from_user.id, sub_id, period_label, s, bot=bot)


@router.callback_query(F.data.startswith("subcap:"))
async def cb_sub_cap(cb: CallbackQuery) -> None:
    """Show preset GB-cap buttons for this sub-reseller (no typing needed; «مقدار دلخواه» for custom)."""
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        cur = int(sub.gb_cap or 0)
        name = sub.name
    cur_txt = f"سقف فعلی: {cur:g} گیگابایت" if cur > 0 else "سقف فعلی: تعیین‌نشده"
    await cb.message.answer(
        f"🎯 سقف حجم ماهانه برای «{name}»\n{cur_txt}\n\n"
        "یک مقدار را انتخاب کنید (یا «مقدار دلخواه»). این سقف فقط برای هشدار است و هر ماه بازنشانی می‌شود.",
        reply_markup=keyboards.sub_cap_keyboard(sub_id),
    )
    await cb.answer()


# Postgres INTEGER max is 2^31-1; clamp the monthly GB cap well under it so a forged/typo'd
# huge number can't overflow the column (an unhandled DB error with no reply to the user).
_MAX_GB_CAP = 1_000_000_000


async def _apply_sub_cap(answer, session, chat_id: int, sub_id: int | None, gb: int) -> None:
    sub = await session.get(Reseller, sub_id) if sub_id else None
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    gb = max(0, min(int(gb), _MAX_GB_CAP))
    sub.gb_cap = gb or None
    sub.gb_cap_alerted_period = None  # re-arm the alert for the new ceiling
    await session.commit()
    if gb > 0:
        await answer(f"✅ سقف حجم ماهانهٔ «{sub.name}» روی {gb:g} گیگابایت تنظیم شد.")
    else:
        await answer(f"✅ سقف حجم «{sub.name}» حذف شد (بدون محدودیت).")


@router.callback_query(F.data.startswith("setcap:"))
async def cb_set_cap(cb: CallbackQuery) -> None:
    """A preset GB-cap tap → set it directly, no typing (gb=0 clears the cap)."""
    sub_id = common._safe_int(cb.data, 1)
    gb = common._safe_int(cb.data, 2)
    if sub_id is None or gb is None:
        await cb.answer("دادهٔ نامعتبر است.", show_alert=True)
        return
    async with common.SessionLocal() as s:
        await _apply_sub_cap(cb.message.answer, s, cb.from_user.id, sub_id, gb)
    await cb.answer()


@router.callback_query(F.data.startswith("capcustom:"))
async def cb_cap_custom(cb: CallbackQuery, state: FSMContext) -> None:
    """«مقدار دلخواه» → enter the text path, with a tappable «انصراف»."""
    sub_id = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await state.set_state(SubCapState.waiting)
    await state.update_data(sub_id=sub_id)
    await cb.message.answer(
        "عدد سقف را به گیگابایت بفرستید (مثلاً 500). برای حذف سقف، عدد 0 را بفرستید.",
        reply_markup=keyboards.flow_cancel_kb(),
    )
    await cb.answer()


@router.message(SubCapState.waiting)
async def on_sub_cap_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace("٬", "").replace(",", "")
    # Accept Persian digits too.
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit():
        # Invalid → stay in the state with a tappable exit (never a button-less dead-end).
        await message.answer(
            "عدد واردشده نامعتبر است؛ لطفاً یک عددِ صحیح (به گیگابایت) بفرستید.",
            reply_markup=keyboards.cancel_keyboard(),
        )
        return
    data = await state.get_data()
    sub_id = data.get("sub_id")
    await state.clear()
    async with common.SessionLocal() as s:
        await _apply_sub_cap(message.answer, s, message.from_user.id, sub_id, int(raw))


@router.message(OwnerCapBumpState.waiting)
async def on_owner_cap_bump_text(message: Message, state: FSMContext) -> None:
    """Owner typed a custom capacity-increase amount for a reseller that requested more from the
    web portal. Apply the bump and notify the reseller."""
    raw = (message.text or "").strip().replace("٬", "").replace(",", "")
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit() or not (0 < int(raw) <= 5000):
        # Invalid → stay in the state with a tappable exit (never a button-less dead-end).
        await message.answer(
            "عدد واردشده نامعتبر است؛ لطفاً یک عددِ صحیح بین ۱ تا ۵۰۰۰ بفرستید.",
            reply_markup=keyboards.cancel_keyboard(),
        )
        return
    amount = int(raw)
    data = await state.get_data()
    rid = data.get("cap_rid")
    await state.clear()
    async with common.SessionLocal() as s:
        from app.services import admin_capacity, notifier

        r = await s.get(Reseller, rid) if rid else None
        if r is None:
            await message.answer("نماینده پیدا نشد.")
            return
        rname = r.name
        try:
            new_mu, _new_mau = await admin_capacity.bump_limits(s, r, amount)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ افزایش ظرفیت ناموفق بود: {exc}")
            return
        await notifier.send_to_reseller(
            s, r,
            f"✅ درخواستِ افزایشِ ظرفیتِ شما تأیید شد.\nسقفِ جدیدِ کاربران: {new_mu}",
            bot=message.bot,
        )
    await message.answer(f"✅ ظرفیت «{rname}» به‌اندازهٔ {amount} افزایش یافت (سقف جدید: {new_mu}).")
