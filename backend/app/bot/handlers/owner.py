"""Owner (admin-bot) callbacks: stats, payment review, reseller search/card/actions,
capacity requests, and the owner `/` commands."""
from __future__ import annotations

import os

from aiogram import Bot, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import (
    _OWED,
    _OWNER_TERMINAL,
    OwnerCapBumpState,
    OwnerSearchState,
    _iso,
    _reshow_menu,
    router,
)
from app.bot.handlers.intake import _payment_review_html, send_owner_review
from app.bot.handlers.views import _dispatch_owner, _owner_stats
from app.models import BotUser, Invoice, Panel, Payment, Reseller
from app.models.enums import EnforcementState, PaymentStatus


@router.callback_query(F.data.startswith("owner:"))
async def cb_owner(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if action == "search":
            await state.set_state(OwnerSearchState.waiting)
            await cb.message.answer(
                "🔎 نام یا شناسهٔ نماینده را بفرستید.",
                reply_markup=keyboards.flow_cancel_kb(),
            )
            await cb.answer()
            return
        await _dispatch_owner(action, cb.message.answer, s)
        if action in _OWNER_TERMINAL:
            await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


# ── admin-bot: period stats switch + per-panel breakdown ─────────────────────
@router.callback_query(F.data.startswith("ostat:"))
async def cb_owner_stats_period(cb: CallbackQuery) -> None:
    label = cb.data.split(":", 1)[1]
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await _owner_stats(cb.message.answer, s, label)
    await cb.answer()


@router.callback_query(F.data.startswith("opanel:"))
async def cb_owner_per_panel(cb: CallbackQuery) -> None:
    label = cb.data.split(":", 1)[1]
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import owner_report

        rows = await owner_report.per_panel(s, label)
        await cb.message.answer(owner_report.render_per_panel(label, rows))
    await cb.answer()


# ── admin-bot: pending-payment review (proof + confirm/reject) ───────────────
@router.callback_query(F.data.startswith("opv:"))
async def cb_owner_payment_view(cb: CallbackQuery, bot: Bot) -> None:
    pid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        pay = await s.get(Payment, pid)
        if pay is None or pay.status != PaymentStatus.pending:
            await cb.message.answer("این پرداخت دیگر در انتظار نیست (شاید قبلاً رسیدگی شده).")
            await cb.answer()
            return
        review = await _payment_review_html(s, pay)
        kb = keyboards.owner_payment_detail_keyboard(pay.id)
        # If there's a proof image, send it with the detail as caption; else just the text.
        # send_owner_review truncates a >1024-char caption (big «پرداخت همهٔ بدهی» sets used
        # to make this raise → the payment was unreviewable from the bot) and falls back to
        # text on any photo failure.
        proof = pay.proof_path
        photo = None
        if proof and os.path.exists(proof):
            from aiogram.types import FSInputFile

            photo = FSInputFile(proof)
        await send_owner_review(
            s, bot, intro="", review_html=review, photo=photo, reply_markup=kb,
        )
    await cb.answer()


async def _finalize_review_message(cb: CallbackQuery, status_line: str) -> bool:
    """Edit the owner's payment-review message in place — append the decision and drop the تأیید/رد
    buttons — so it's obvious which payment was already handled and it can't be re-tapped. Returns
    True if the message was edited. Screenshot proofs are PHOTO messages (caption, no text), so use
    edit_caption there and edit_text for a plain-text (TXID) notification. Best-effort: a too-old /
    unmodifiable message returns False and the caller sends a plain confirmation instead."""
    msg = cb.message
    if msg is None:
        return False
    kb = keyboards.owner_payment_decided_keyboard()
    try:
        # Inside the try: an InaccessibleMessage (review too old) has NO html_text — the
        # attribute read used to raise AFTER confirm/reject had committed, so the owner got
        # no ✅/❌ ack and the live buttons invited a re-tap.
        base = msg.html_text or ""
        body = f"{base}\n\n{status_line}"
        if msg.caption is not None or msg.photo:
            await msg.edit_caption(caption=body[:1024], reply_markup=kb, parse_mode="HTML")
        else:
            await msg.edit_text(body, reply_markup=kb, parse_mode="HTML")
        return True
    except Exception:  # noqa: BLE001 — message too old / not modified → caller falls back
        return False


@router.callback_query(F.data.startswith("opok:"))
async def cb_owner_payment_confirm(cb: CallbackQuery) -> None:
    pid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import payments

        try:
            res = await payments.confirm_manually(s, pid)
        except Exception as exc:  # noqa: BLE001
            await cb.message.answer(f"❌ تأیید ناموفق بود: {exc}")
            await cb.answer()
            return
        if res.paid:
            edited = await _finalize_review_message(
                cb, "✅ این پرداخت تأیید شد و به نماینده اطلاع داده شد.")
            if not edited:  # couldn't update the original in place → send a plain confirmation
                await cb.message.answer("✅ پرداخت تأیید شد و به نماینده اطلاع داده شد.")
        else:
            await cb.message.answer(f"⚠️ {res.message_fa}")
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer("تأیید شد")


@router.callback_query(F.data.startswith("opno:"))
async def cb_owner_payment_reject(cb: CallbackQuery) -> None:
    pid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import payments

        try:
            await payments.reject_payment(s, pid)
        except Exception as exc:  # noqa: BLE001
            await cb.message.answer(f"❌ رد ناموفق بود: {exc}")
            await cb.answer()
            return
        edited = await _finalize_review_message(cb, "❌ این پرداخت رد شد و به نماینده اطلاع داده شد.")
        if not edited:
            await cb.message.answer("❌ پرداخت رد شد و به نماینده اطلاع داده شد.")
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer("رد شد")


# ── admin-bot: reseller search → card → quick actions ────────────────────────
@router.message(OwnerSearchState.waiting)
async def on_owner_search(message: Message, state: FSMContext) -> None:
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            await state.clear()
            return
        q = (message.text or "").strip()
        if len(q) < 2:
            # Stay in the state with a tappable exit instead of a dead error.
            await message.answer(
                "حداقل ۲ نویسه بفرستید.",
                reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
            )
            return
        rows = (
            await s.execute(
                select(Reseller)
                .where(
                    Reseller.is_owner.is_(False),
                    or_(Reseller.name.ilike(f"%{q}%"), Reseller.admin_uuid.ilike(f"%{q}%")),
                )
                .order_by(Reseller.name)
                .limit(20)
            )
        ).scalars().all()
        if not rows:
            await message.answer(
                "نماینده‌ای پیدا نشد. نام یا UUID دیگری بفرستید.",
                reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
            )
            return
        await state.clear()
        if len(rows) == 1:
            # A reseller card with its own action buttons — no trailing menu (would bury the actions).
            await _send_reseller_card(message.answer, s, rows[0].id)
            return
        items = [(r.id, f"{(r.name or '—')[:24]}") for r in rows]
        await message.answer(
            f"🔎 {len(rows)} نتیجه — یکی را انتخاب کنید:",
            reply_markup=keyboards.owner_reseller_results_keyboard(items),
        )
        # No menu re-show: the results are a picker (tap a reseller) — a trailing menu would bury it.


@router.callback_query(F.data.startswith("orc:"))
async def cb_owner_reseller_card(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await _send_reseller_card(cb.message.answer, s, rid)
    await cb.answer()


async def _send_reseller_card(answer, session, reseller_id: int) -> None:
    from app.services import owner_report, pricing, reseller_report
    from app.services.periods import current_month

    r = await session.get(Reseller, reseller_id)
    if r is None:
        await answer("نماینده پیدا نشد.")
        return
    panel = await session.get(Panel, r.panel_id)
    label = current_month().label
    # Current-month sales for this node (own + subs), matching the real invoice.
    gb = await reseller_report.current_billable_gb(session, r)
    price = int(r.price_per_gb or await pricing.get_default_price_per_gb(session))
    # Outstanding debt for this reseller.
    owed = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(Invoice.amount_toman), 0)).where(
                    Invoice.reseller_id == r.id, Invoice.status.in_(_OWED)
                )
            )
        ).scalar_one()
        or 0
    )
    cap = (r.panel_max_users or 0)
    enforced = r.enforcement_state == EnforcementState.enforced
    username = None
    if r.bot_chat_id:
        bu = (
            await session.execute(select(BotUser).where(BotUser.telegram_id == r.bot_chat_id))
        ).scalar_one_or_none()
        username = bu.username if bu else None
    tg_href = (
        f"https://t.me/{username}" if username
        else (f"tg://user?id={r.bot_chat_id}" if r.bot_chat_id else None)
    )
    lines = [
        f"👤 {_iso(r.name or '—')}",
        _iso(f"پنل: {panel.key if panel else '—'}"),
        f"وضعیت: {'⛔️ مسدود' if enforced else '✅ فعال'} | "
        f"{'متصل به ربات' if r.bot_chat_id else 'بدون ربات'}",
        f"فروشِ ماهِ جاری ({label}): {gb:g} گیگ ≈ {owner_report._toman(gb * price)} ت",
        f"بدهیِ معوق: {owner_report._toman(owed)} ت",
        f"سقفِ کاربر: {cap or '—'}",
    ]
    try:
        await answer(
            "\n".join(lines),
            reply_markup=keyboards.owner_reseller_card_keyboard(
                r.id, enforced=enforced, tg_href=tg_href
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # Telegram rejects a tg://user?id= BUTTON when the reseller's privacy settings disallow it
        # (BUTTON_USER_PRIVACY_RESTRICTED) — resend the card without the chat button so it always opens.
        if "BUTTON" not in str(exc).upper():
            raise
        await answer(
            "\n".join(lines),
            reply_markup=keyboards.owner_reseller_card_keyboard(
                r.id, enforced=enforced, tg_href=None
            ),
        )


@router.callback_query(F.data.startswith("oenf:"))
async def cb_owner_enforce(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import enforcement

        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        await enforcement.enforce_reseller(s, r, dry_run=False)
        await cb.message.answer(f"⏳ مسدودسازی «{r.name}» در صف ثبت شد و مرحله‌ای انجام می‌شود.")
    await cb.answer("در صف ثبت شد")


@router.callback_query(F.data.startswith("ores:"))
async def cb_owner_restore(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import enforcement

        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        action = await enforcement.queue_restore(s, r, reason="bot-owner")
        await cb.message.answer(
            "این نماینده مسدود نیست." if action is None
            else f"⏳ آزادسازی «{r.name}» در صف ثبت شد و مرحله‌ای انجام می‌شود."
        )
    await cb.answer("در صف ثبت شد")


@router.callback_query(F.data.startswith("obump:"))
async def cb_owner_bump(cb: CallbackQuery) -> None:
    _, rid_s, amount_s = cb.data.split(":")
    rid, amount = int(rid_s), int(amount_s)
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import admin_capacity

        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        try:
            new_mu, _new_mau = await admin_capacity.bump_limits(s, r, amount)
        except Exception as exc:  # noqa: BLE001
            await cb.message.answer(f"❌ افزایش ظرفیت ناموفق بود: {exc}")
            await cb.answer()
            return
        await cb.message.answer(f"✅ ظرفیت «{r.name}» {amount}+ شد (سقف جدید: {new_mu}).")
    await cb.answer("انجام شد")


@router.callback_query(F.data.startswith("capok:"))
async def cb_cap_approve(cb: CallbackQuery) -> None:
    """Owner approved a reseller's web-portal capacity-increase request: apply the requested bump."""
    _, rid_s, amount_s = cb.data.split(":")
    rid, amount = int(rid_s), int(amount_s)
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import admin_capacity, notifier

        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        rname = r.name
        try:
            new_mu, _new_mau = await admin_capacity.bump_limits(s, r, amount)
        except Exception as exc:  # noqa: BLE001
            await cb.answer(f"ناموفق: {exc}", show_alert=True)
            return
        await notifier.send_to_reseller(
            s, r,
            f"✅ درخواستِ افزایشِ ظرفیتِ شما تأیید شد.\nسقفِ جدیدِ کاربران: {new_mu}",
            bot=cb.message.bot,
        )
    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # prevent double-tap
    except Exception:  # noqa: BLE001
        pass
    await cb.message.answer(f"✅ ظرفیت «{rname}» {amount}+ شد (سقف جدید: {new_mu}) و به نماینده اطلاع داده شد.")
    await cb.answer("اعمال شد")


@router.callback_query(F.data.startswith("capno:"))
async def cb_cap_reject(cb: CallbackQuery) -> None:
    """Owner rejected a reseller's capacity-increase request."""
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        from app.services import notifier

        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        rname = r.name
        await notifier.send_to_reseller(
            s, r,
            "❌ درخواستِ افزایشِ ظرفیتِ شما در حالِ حاضر تأیید نشد. برای جزئیات با پشتیبانی در تماس باشید.",
            bot=cb.message.bot,
        )
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await cb.message.answer(f"❌ درخواستِ «{rname}» رد شد و به او اطلاع داده شد.")
    await cb.answer("رد شد")


@router.callback_query(F.data.startswith("capmore:"))
async def cb_cap_more(cb: CallbackQuery) -> None:
    """Owner wants a different capacity-increase amount → show preset buttons (no typing)."""
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        r = await s.get(Reseller, rid)
        if r is None:
            await cb.answer("نماینده پیدا نشد.", show_alert=True)
            return
        rname = r.name
    await cb.message.answer(
        f"➕ مقدارِ افزایشِ ظرفیت برای «{rname}» را انتخاب کنید (یا «مقدار دلخواه»):",
        reply_markup=keyboards.cap_bump_keyboard(rid),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("bumptype:"))
async def cb_bump_type(cb: CallbackQuery, state: FSMContext) -> None:
    """«مقدار دلخواه» for a capacity bump → enter the text path with a tappable «انصراف»."""
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await state.set_state(OwnerCapBumpState.waiting)
    await state.update_data(cap_rid=rid)
    await cb.message.answer(
        "مقدارِ افزایش را به‌صورتِ عدد بفرستید (۱ تا ۵۰۰۰).",
        reply_markup=keyboards.flow_cancel_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("orcinv:"))
async def cb_owner_reseller_invoices(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        rows = (
            await s.execute(
                select(Invoice.period_label, Invoice.amount_toman, Invoice.status)
                .where(Invoice.reseller_id == rid)
                .order_by(Invoice.period_start.desc())
                .limit(12)
            )
        ).all()
        if not rows:
            await cb.message.answer("فاکتوری برای این نماینده ثبت نشده است.")
            await cb.answer()
            return
        status_fa = {"draft": "پیش‌نویس", "sent": "ارسال‌شده", "paid": "پرداخت‌شده",
                     "overdue": "معوق", "enforced": "مسدود", "canceled": "لغو"}
        lines = ["🧾 فاکتورهای اخیر:"]
        for period, toman, status in rows:
            lines.append(
                f"‏• {period}: {float(toman or 0):,.0f} ت — {status_fa.get(status.value, status.value)}"
            )
        await cb.message.answer("\n".join(lines))
    await cb.answer()


# Owner `/` commands — mirror the owner menu buttons exactly (see commands.OWNER_COMMANDS).
# `/broadcast` is handled by its own dedicated command handler above (it also accepts inline
# text), so it's intentionally not duplicated here.
_OWNER_CMD_ACTION = {
    "stats": "stats", "health": "health", "payments": "payments",
    "debtors": "debtors", "sync": "sync", "backup": "backup",
}


@router.message(Command(commands=list(_OWNER_CMD_ACTION)))
async def cmd_owner_action(message: Message, command: CommandObject) -> None:
    action = _OWNER_CMD_ACTION.get(command.command)
    if not action:
        return
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return  # owner-only; resellers don't see these commands
        await _dispatch_owner(action, message.answer, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    """Owner-only reseller search (same as the «🔎 جستجوی نماینده» menu action)."""
    async with common.SessionLocal() as s:
        if not await common._is_owner_user(s, message.from_user):
            return
    await state.set_state(OwnerSearchState.waiting)
    await message.answer(
        "🔎 نام یا شناسهٔ نماینده را بفرستید.",
        reply_markup=keyboards.flow_cancel_kb(),
    )
