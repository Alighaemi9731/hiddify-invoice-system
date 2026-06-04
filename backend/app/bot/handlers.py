"""Reseller + owner bot handlers: membership gate, menus, registration, payment."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import keyboards, texts
from app.bot.matching import parse_link
from app.core.db import SessionLocal
from app.models import BotUser, Invoice, Panel, Payment, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementState,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import owner_notify, settings_service


class BroadcastState(StatesGroup):
    waiting = State()


class SupportState(StatesGroup):
    """A reseller is composing a message to support."""

    waiting = State()


class OwnerReplyState(StatesGroup):
    """The owner is composing a reply to a specific user (target id in FSM data)."""

    waiting = State()


class SubCapState(StatesGroup):
    """A reseller is entering the monthly GB cap for one of their sub-resellers
    (the sub's id is held in FSM data)."""

    waiting = State()


log = logging.getLogger("bot.handlers")
router = Router()

_TXID_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_UNPAID = (InvoiceStatus.draft, InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
_STATUS_FA = {"draft": "پیش‌نویس", "sent": "ارسال‌شده", "paid": "پرداخت‌شده",
              "overdue": "سررسید گذشته", "enforced": "مسدود", "canceled": "لغو"}


# --------------------------- helpers ---------------------------
async def _resellers_for_chat(session, chat_id: int) -> list[Reseller]:
    return list(
        (await session.execute(select(Reseller).where(Reseller.bot_chat_id == chat_id)))
        .scalars().all()
    )


async def _is_owner_user(session, user) -> bool:
    """Owner = matches the configured @username OR the numeric owner_telegram OR the
    already-captured owner_chat_id. First owner match also pins the chat id."""
    owner_setting = str(await settings_service.get(session, "owner_telegram", "") or "").strip()
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()

    uname = (user.username or "").lstrip("@").lower()
    owner_name = owner_setting.lstrip("@").lower()

    is_owner = False
    if owner_name and uname and uname == owner_name:
        is_owner = True
    elif owner_setting.isdigit() and str(user.id) == owner_setting:
        is_owner = True
    elif owner_chat and str(user.id) == owner_chat:
        is_owner = True

    if is_owner and owner_chat != str(user.id):
        # Pin the owner's chat id so scheduled backups/alerts/logs can reach them.
        await settings_service.set_value(session, "owner_chat_id", str(user.id))
    return is_owner


async def _track_user(session, user) -> None:
    """Record everyone who interacts with the bot (used by the channel guard)."""
    row = (
        await session.execute(select(BotUser).where(BotUser.telegram_id == user.id))
    ).scalar_one_or_none()
    now = dt.datetime.now(dt.timezone.utc)
    if row is None:
        session.add(BotUser(telegram_id=user.id, username=user.username,
                            first_name=user.first_name, last_seen_at=now))
    else:
        row.username = user.username
        row.first_name = user.first_name
        row.last_seen_at = now
    await session.commit()


async def _join_link(bot: Bot, chat_id: str, static_link: str, one_time: bool) -> str | None:
    """A per-user single-use invite link so the chat's real link isn't shared.
    Falls back to the static link if the bot can't create one (needs invite rights)."""
    if chat_id and one_time:
        try:
            link = await bot.create_chat_invite_link(chat_id, member_limit=1)
            return link.invite_link
        except Exception:  # noqa: BLE001
            log.warning("create_chat_invite_link failed (need invite rights?)", exc_info=True)
    return static_link or None


async def _is_member(bot: Bot, chat_id: str, user_id: int) -> bool:
    if not chat_id:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("member", "administrator", "creator", "owner")
    except Exception as exc:  # noqa: BLE001
        log.warning("membership check failed for %s: %s", user_id, exc)
        return False


async def _required_gates(session) -> list[dict]:
    """The enabled forced-membership targets (channel and/or group). Each: id, link, label.
    A gate counts only when its toggle is on AND a chat id is configured."""
    cfg = await settings_service.get_many(session, [
        "channel_membership_required", "announcement_channel_id", "announcement_channel_link",
        "group_membership_required", "announcement_group_id", "announcement_group_link",
    ])
    gates: list[dict] = []
    if cfg.get("channel_membership_required") and (cfg.get("announcement_channel_id") or ""):
        gates.append({"id": str(cfg["announcement_channel_id"]),
                      "link": cfg.get("announcement_channel_link") or "", "label": "کانال"})
    if cfg.get("group_membership_required") and (cfg.get("announcement_group_id") or ""):
        gates.append({"id": str(cfg["announcement_group_id"]),
                      "link": cfg.get("announcement_group_link") or "", "label": "گروه"})
    return gates


async def _missing_gates(bot: Bot, session, user_id: int) -> list[dict]:
    """Of the enabled gates, the ones the user is NOT a member of."""
    return [g for g in await _required_gates(session) if not await _is_member(bot, g["id"], user_id)]


async def _gate_or_menu(answer, bot: Bot, session, user) -> None:
    """Show the main menu if the user is the owner or passes every enabled gate; otherwise
    show the join prompt with a button per chat they still need to join."""
    if await _is_owner_user(session, user):
        await _send_menu(answer, session, user)
        return
    missing = await _missing_gates(bot, session, user.id)
    if not missing:
        await _send_menu(answer, session, user)
        return
    one_time = bool(await settings_service.get(session, "one_time_invite_links", True))
    targets = []
    for g in missing:
        link = await _join_link(bot, g["id"], g["link"], one_time)
        targets.append({"label": g["label"], "link": link})
    text = await texts.render(session, "tpl_membership")
    await answer(text, reply_markup=keyboards.membership_keyboard(targets))


async def _send_menu(answer, session, user) -> None:
    if await _is_owner_user(session, user):
        await answer("👑 منوی مدیریت سیستم:", reply_markup=keyboards.owner_menu_keyboard())
        return
    name = user.first_name or user.username or ""
    welcome = await texts.render(session, "tpl_welcome", name=name)
    menu = await texts.render(session, "tpl_menu")
    await answer(f"{welcome}\n\n{menu}", reply_markup=keyboards.reseller_menu_keyboard())


# --------------------------- /commands ---------------------------
async def _sync_command_menu(bot: Bot, session, user) -> None:
    """Make sure this user's `/` command list matches their role (owner vs reseller)."""
    from app.bot import commands as bot_commands

    try:
        if await _is_owner_user(session, user):
            await bot_commands.apply_owner_menu(bot, user.id)
    except Exception:  # noqa: BLE001
        log.warning("sync command menu failed", exc_info=True)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
        await _sync_command_menu(bot, session, message.from_user)
        await _gate_or_menu(message.answer, bot, session, message.from_user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot) -> None:
    async with SessionLocal() as session:
        await _sync_command_menu(bot, session, message.from_user)
        await _send_menu(message.answer, session, message.from_user)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "راهنما:\n"
        "/start یا /menu — منوی اصلی\n"
        "/invoices — فاکتورهای من\n"
        "/pay — پرداخت\n"
        "/debt — بدهی من\n"
        "/removelink — حذف لینک‌های ثبت‌شده\n\n"
        "برای ثبت، کافیست لینک پنل خود را ارسال کنید."
    )


@router.message(Command("invoices"))
async def cmd_invoices(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_invoices(message.answer, message.from_user.id, s)


@router.message(Command("pay"))
async def cmd_pay(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_pay(message.answer, message.from_user.id, s)


@router.message(Command("debt"))
async def cmd_debt(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_debt(message.answer, message.from_user.id, s)


@router.message(Command("removelink"))
async def cmd_removelink(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_removelink(message.answer, message.from_user.id, s)


@router.message(Command("subs"))
async def cmd_subs(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_sub_panels(message.answer, message.from_user.id, s)


# --------------------------- broadcast (owner) ---------------------------
_AUDIENCE_FA = {"all": "همه نمایندگان", "debtors": "بدهکاران", "zero_sale": "فروش صفر این ماه"}


async def _audience_label(session, audience: str, panel_id: int | None) -> str:
    if audience == "panel" and panel_id is not None:
        panel = await session.get(Panel, panel_id)
        return f"نمایندگان پنل {panel.key}" if panel else "نمایندگان یک پنل"
    return _AUDIENCE_FA.get(audience, audience)


async def _do_broadcast(
    message: Message, session, text: str, audience: str = "all", panel_id: int | None = None
) -> None:
    from app.services import broadcast as bc

    counts = await bc.broadcast(session, text, audience=audience, panel_id=panel_id)
    label = await _audience_label(session, audience, panel_id)
    await message.answer(
        f"📢 ارسال به «{label}»:\n"
        f"{counts['sent']} موفق، {counts['blocked']} مسدود، "
        f"{counts['failed']} ناموفق (از {counts['total']} گیرنده)"
    )


@router.callback_query(F.data.startswith("bcaud:"))
async def cb_broadcast_audience(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")  # bcaud:all | bcaud:panel | bcaud:panel:<id>
    audience = parts[1]
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
        f"اکنون متن پیام را ارسال کنید (یا /cancel برای لغو):"
    )
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("لغو شد.")


# --------------------------- support chat (no DB storage) ---------------------------
@router.message(SupportState.waiting)
async def on_support_text(message: Message, state: FSMContext) -> None:
    """Relay a reseller's support message to the owner. Nothing is stored in the DB —
    the owner replies live via the inline button, which carries the user id."""
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("پیام خالی بود؛ لغو شد.")
        return
    async with SessionLocal() as s:
        owner_chat = await settings_service.get(s, "owner_chat_id", "") or ""
        bot = message.bot
        u = message.from_user
        if not owner_chat:
            await message.answer("در حال حاضر پشتیبانی در دسترس نیست. بعداً تلاش کنید.")
            return
        handle = f"@{u.username}" if u.username else f"<a href='tg://user?id={u.id}'>{u.first_name or u.id}</a>"
        await bot.send_message(
            int(owner_chat),
            f"💬 پیام پشتیبانی\nاز: {handle} (id: <code>{u.id}</code>)\n\n{text}",
            reply_markup=keyboards.support_reply_keyboard(u.id, message.message_id),
            parse_mode="HTML",
        )
        await message.answer("✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ می‌گیرید.")


@router.callback_query(F.data.startswith("sup:"))
async def cb_support_reply(cb: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    parts = cb.data.split(":")
    target = int(parts[1])
    reply_to = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    await state.set_state(OwnerReplyState.waiting)
    await state.update_data(target=target, reply_to=reply_to)
    await cb.message.answer(f"پاسخ خود را برای کاربر <code>{target}</code> بنویسید (یا /cancel):",
                            parse_mode="HTML")
    await cb.answer()


@router.message(OwnerReplyState.waiting)
async def on_owner_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    target = data.get("target")
    reply_to = data.get("reply_to")
    text = (message.text or "").strip()
    if not target or not text:
        await message.answer("پاسخ ارسال نشد.")
        return
    body = f"💬 پاسخ پشتیبانی:\n\n{text}"
    try:
        if reply_to:
            # Quote the user's original message. If it was deleted, Telegram errors,
            # so fall back to a plain message.
            try:
                await message.bot.send_message(int(target), body, reply_to_message_id=int(reply_to))
            except Exception:  # noqa: BLE001
                await message.bot.send_message(int(target), body)
        else:
            await message.bot.send_message(int(target), body)
        await message.answer("✅ پاسخ ارسال شد.")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"ارسال پاسخ ناموفق بود: {exc}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, command: CommandObject) -> None:
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
            return
        if command.args:
            await _do_broadcast(message, s, command.args, "all")
        else:
            await message.answer("📢 گیرندگان پیام همگانی را انتخاب کنید:",
                                 reply_markup=keyboards.broadcast_audience_keyboard())


@router.message(BroadcastState.waiting)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    audience = data.get("audience", "all")
    panel_id = data.get("panel_id")
    await state.clear()
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
            return
        if not (message.text or "").strip():
            await message.answer("متن خالی بود؛ لغو شد.")
            return
        await _do_broadcast(message, s, message.text, audience, panel_id)


# --------------------------- reseller callbacks ---------------------------
@router.callback_query(F.data == "check_membership")
async def cb_check_membership(cb: CallbackQuery, bot: Bot) -> None:
    async with SessionLocal() as session:
        missing = (
            [] if await _is_owner_user(session, cb.from_user)
            else await _missing_gates(bot, session, cb.from_user.id)
        )
        if not missing:
            await cb.message.edit_text("✅ عضویت شما تأیید شد.")
            await _send_menu(cb.message.answer, session, cb.from_user)
        else:
            names = " و ".join(g["label"] for g in missing)
            await cb.answer(f"هنوز عضو {names} نیستید.", show_alert=True)


@router.callback_query(F.data == "menu:register")
async def cb_register(cb: CallbackQuery) -> None:
    await cb.message.answer("لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).")
    await cb.answer()


@router.callback_query(F.data == "menu:panels")
async def cb_panels(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_panels(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


# --------------------------- sub-reseller management ---------------------------
@router.callback_query(F.data == "menu:subs")
async def cb_subs(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_sub_panels(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("subp:"))
async def cb_sub_panel(cb: CallbackQuery) -> None:
    parent_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        await _send_sub_list(cb.message.answer, cb.from_user.id, parent_id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("subv:"))
async def cb_sub_view(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        await _send_sub_detail(cb.message.answer, cb.from_user.id, sub_id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("subx:"))
async def cb_sub_enforce(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.message.answer(f"⏳ در حال مسدودسازی «{sub.name}»...")
        from app.services import enforcement

        # Reseller-initiated manual action → force the real write (dry_run=False),
        # independent of the global automatic-dunning enforcement switch.
        action = await enforcement.enforce_reseller(s, sub, dry_run=False)
        if action.status == EnforcementActionStatus.done:
            msg = (
                f"⛔️ «{sub.name}» مسدود شد: {action.affected_count} کاربر غیرفعال و "
                f"سقف کاربران (max users / max active users) صفر شد."
            )
            if action.error:  # some users couldn't be disabled (logged for the owner)
                msg += "\n⚠️ برخی کاربران غیرفعال نشدند؛ دوباره تلاش کنید یا گزارش را ببینید."
            await cb.message.answer(msg)
        else:
            await cb.message.answer(
                "❌ مسدودسازی ناموفق بود. مطمئن شوید کلید API پنل در تنظیمات ثبت شده است.\n"
                f"{action.error or ''}"
            )
    await cb.answer()


@router.callback_query(F.data.startswith("subr:"))
async def cb_sub_restore(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.message.answer(f"⏳ در حال آزادسازی «{sub.name}»...")
        from app.services import enforcement

        action = await enforcement.restore_reseller(s, sub)
        if action is None:
            await cb.message.answer("این زیرمجموعه مسدود نیست.")
        elif action.status == EnforcementActionStatus.done:
            await cb.message.answer(
                f"✅ «{sub.name}» آزاد شد: {action.affected_count} کاربر دوباره فعال و "
                f"سقف‌ها به حالت قبل برگشت."
            )
        else:
            await cb.message.answer(f"❌ آزادسازی ناموفق بود.\n{action.error or ''}")
    await cb.answer()


@router.callback_query(F.data.startswith("subinv:"))
async def cb_sub_invoice(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    sub_id, period_label = int(parts[1]), parts[2]
    await cb.answer("در حال ساخت فاکتور…")
    async with SessionLocal() as s:
        await _send_sub_invoice(cb.message.answer, cb.from_user.id, sub_id, period_label, s, bot=bot)


@router.callback_query(F.data.startswith("subcap:"))
async def cb_sub_cap(cb: CallbackQuery, state: FSMContext) -> None:
    """Ask the reseller for a new monthly GB cap for this sub-reseller."""
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        cur = int(sub.gb_cap or 0)
        name = sub.name
    await state.set_state(SubCapState.waiting)
    await state.update_data(sub_id=sub_id)
    cur_txt = f"سقف فعلی: {cur:g} گیگ\n" if cur > 0 else "سقف فعلی: تعیین‌نشده\n"
    await cb.message.answer(
        f"🎯 تعیین سقف حجم ماهانه برای «{name}»\n{cur_txt}"
        "عدد سقف را به گیگابایت بفرستید (مثلاً 500). برای حذف سقف، عدد 0 را بفرستید.\n"
        "این سقف هر ماه ریست می‌شود و فقط برای هشدار است (مسدودسازی خودکار نمی‌کند).\n"
        "برای لغو: /cancel"
    )
    await cb.answer()


@router.message(SubCapState.waiting)
async def on_sub_cap_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    sub_id = data.get("sub_id")
    await state.clear()
    raw = (message.text or "").strip().replace("٬", "").replace(",", "")
    # Accept Persian digits too.
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit():
        await message.answer("عدد نامعتبر بود. یک عدد صحیح (گیگابایت) بفرستید یا /cancel.")
        return
    gb = int(raw)
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id) if sub_id else None
        if not sub or not await _owns_sub(s, message.from_user.id, sub):
            await message.answer("دسترسی ندارید.")
            return
        sub.gb_cap = gb or None
        sub.gb_cap_alerted_period = None  # re-arm the alert for the new ceiling
        await s.commit()
        if gb > 0:
            await message.answer(
                f"✅ سقف حجم ماهانهٔ «{sub.name}» روی {gb:g} گیگ تنظیم شد.\n"
                "از "
                "«مدیریت زیرمجموعه‌ها» می‌توانید میزان مصرف این ماه را ببینید."
            )
        else:
            await message.answer(f"✅ سقف حجم «{sub.name}» حذف شد (بدون محدودیت).")


@router.callback_query(F.data == "menu:support")
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await cb.message.answer("پیام خود را برای پشتیبانی بنویسید (یا /cancel برای لغو):")
    await cb.answer()


@router.callback_query(F.data == "menu:invoices")
async def cb_invoices(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_invoices(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data == "menu:debt")
async def cb_debt(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_debt(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data == "menu:pay")
async def cb_pay(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_pay(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data == "menu:removelink")
async def cb_removelink(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_removelink(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("rm:"))
async def cb_rm(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        r = await s.get(Reseller, rid)
        if r and r.bot_chat_id == cb.from_user.id:
            r.bot_chat_id = None
            r.link_tag = None
            r.registered_at = None
            await s.commit()
            await cb.message.answer(f"✅ لینک «{r.name}» حذف شد.")
        else:
            await cb.answer("یافت نشد.", show_alert=True)
    await cb.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# --------------------------- owner callbacks ---------------------------
@router.callback_query(F.data.startswith("owner:"))
async def cb_owner(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        if action == "stats":
            await _owner_stats(cb.message.answer, s)
        elif action == "debtors":
            await _owner_debtors(cb.message.answer, s)
        elif action == "zerosale":
            await _owner_zerosale(cb.message.answer, s)
        elif action == "broadcast":
            await cb.message.answer("📢 گیرندگان پیام همگانی را انتخاب کنید:",
                                    reply_markup=keyboards.broadcast_audience_keyboard())
        elif action == "sync":
            from app.services import sync as sync_service

            await cb.message.answer("⏳ در حال همگام‌سازی پنل‌ها…")
            res = await sync_service.sync_all(s)
            ok = sum(1 for r in res if r.status.value == "success")
            await cb.message.answer(f"🔄 همگام‌سازی انجام شد: {ok}/{len(res)} پنل موفق.")
        elif action == "backup":
            from app.services import backup_delivery

            await cb.message.answer("⏳ در حال تهیهٔ پشتیبان…")
            r = await backup_delivery.send_backup_to_owner(s)
            if r.get("status") == "sent":
                await cb.message.answer("🗄 پشتیبان تهیه و برای شما ارسال شد.")
            elif r.get("status") in ("no_owner_chat", "no_bot"):
                await cb.message.answer(f"⚠️ پشتیبان روی سرور ذخیره شد ولی ارسال نشد ({r.get('status')}).")
            else:
                await cb.message.answer("❌ ارسال پشتیبان ناموفق بود.")
        elif action == "dunning":
            from app.services import dunning

            res = await dunning.run_dunning(s)
            await cb.message.answer(
                f"🔔 یادآوری‌ها اجرا شد:\n"
                f"یادآوری ۱: {res['reminder1']} | یادآوری ۲: {res['reminder2']} | "
                f"اخطار: {res['warning']} | مسدودسازی: {res['enforced']} (آزمایشی: {res['enforced_dry']})"
            )
        elif action == "monthly":
            from app.services import delivery, invoicing, sync as sync_service
            from app.services.periods import previous_month

            await cb.message.answer("⏳ در حال همگام‌سازی، صدور و ارسال ماه قبل...")
            await sync_service.sync_all(s)
            p = previous_month()
            g = await invoicing.generate_invoices(s, p)
            d = await delivery.send_period(s, p.label)
            await cb.message.answer(
                f"✅ دوره {p.label}: {g.created} فاکتور ساخته شد، "
                f"{d.get('sent', 0)} ارسال موفق، {d.get('unmatched', 0)} بدون ربات."
            )
    await cb.answer()


async def _owner_stats(answer, session) -> None:
    from app.services.periods import current_month

    panels = (await session.execute(select(func.count(Panel.id)))).scalar_one()
    resellers = (
        await session.execute(select(func.count(Reseller.id)).where(Reseller.is_owner.is_(False)))
    ).scalar_one()
    registered = (
        await session.execute(select(func.count(Reseller.id)).where(Reseller.bot_chat_id.is_not(None)))
    ).scalar_one()
    label = current_month().label
    sent_rows = (
        await session.execute(
            select(Invoice.amount_toman).where(
                Invoice.period_label == label,
                Invoice.status.in_((InvoiceStatus.sent, InvoiceStatus.overdue,
                                    InvoiceStatus.enforced, InvoiceStatus.paid)),
            )
        )
    ).scalars().all()
    owed_rows = (
        await session.execute(select(Invoice.amount_toman).where(Invoice.status.in_(_OWED)))
    ).scalars().all()
    await answer(
        f"📊 آمار کلی\n"
        f"پنل‌ها: {panels}\n"
        f"نمایندگان: {resellers} ({registered} متصل به ربات)\n"
        f"فروش دورهٔ جاری ({label}): {sum(float(x) for x in sent_rows):,.0f} تومان\n"
        f"بدهی معوق: {sum(float(x) for x in owed_rows):,.0f} تومان"
    )


async def _owner_debtors(answer, session) -> None:
    rows = (
        await session.execute(
            select(Reseller.name, func.sum(Invoice.amount_toman))
            .join(Reseller, Invoice.reseller_id == Reseller.id)
            .where(Invoice.status.in_(_OWED))
            .group_by(Reseller.id, Reseller.name)
            .order_by(func.sum(Invoice.amount_toman).desc())
            .limit(10)
        )
    ).all()
    if not rows:
        await answer("بدهکاری وجود ندارد.")
        return
    lines = ["💰 بدهکاران برتر:\n"]
    for i, (name, total) in enumerate(rows, 1):
        lines.append(f"{i}. {name}: {float(total):,.0f} تومان")
    await answer("\n".join(lines))


async def _owner_zerosale(answer, session) -> None:
    """Bot-registered resellers whose bundle sells nothing this month (idle customers)."""
    from app.services import invoicing
    from app.services.periods import current_month

    pairs = await invoicing.preview_bundles(session, current_month())
    idle = [
        b.root.name for _panel, b in pairs
        if b.total_gb <= 0 and b.root.bot_chat_id is not None
    ]
    if not idle:
        await answer(f"🟡 فروش صفر ({current_month().label}): همهٔ نمایندگانِ متصل فروش داشته‌اند.")
        return
    lines = [f"🟡 فروش صفر این ماه ({current_month().label}) — {len(idle)} نماینده:\n"]
    lines += [f"• {n}" for n in idle[:40]]
    if len(idle) > 40:
        lines.append(f"… و {len(idle) - 40} نمایندهٔ دیگر")
    await answer("\n".join(lines))


# --------------------------- shared reseller views ---------------------------
async def _send_invoices(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    invoices = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids))
            .order_by(Invoice.period_start.desc()).limit(8)
        )
    ).scalars().all()
    if not invoices:
        await answer("فاکتوری برای شما ثبت نشده است.")
        return
    lines = ["🧾 فاکتورهای اخیر شما:\n"]
    for inv in invoices:
        lines.append(
            f"• دوره {inv.period_label}: {float(inv.amount_toman):,.0f} تومان "
            f"({float(inv.amount_usdt):,.2f} USDT) — {_STATUS_FA.get(inv.status.value, inv.status.value)}"
        )
    await answer("\n".join(lines))


async def _send_debt(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    invoices = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
        )
    ).scalars().all()
    total_t = sum(float(i.amount_toman) for i in invoices)
    total_u = sum(float(i.amount_usdt) for i in invoices)
    await answer(
        f"📊 بدهی فعلی شما: {total_t:,.0f} تومان ({total_u:,.2f} USDT)\n"
        f"تعداد فاکتورهای پرداخت‌نشده: {len(invoices)}"
    )


async def _send_pay(answer, chat_id: int, session) -> None:
    from app.services import payment_methods

    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    owed = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
        )
    ).scalars().all()
    today = dt.date.today()
    due = [i for i in owed if not (i.deferred_until and i.deferred_until > today)]
    deferred = [i for i in owed if i.deferred_until and i.deferred_until > today]

    if not due:
        if deferred:
            await answer("در حال حاضر مبلغی برای پرداخت ندارید؛ فاکتورهای شما مهلت‌دار هستند. ⏳")
        else:
            await answer("بدهی فعالی برای پرداخت ندارید. 🎉")
        return

    total_u = sum(float(i.amount_usdt) for i in due)
    total_t = sum(float(i.amount_toman) for i in due)
    lines = [
        "💳 پرداخت",
        f"مبلغ قابل پرداخت (هم‌اکنون): {total_u:,.2f} USDT ({total_t:,.0f} تومان)",
    ]
    if len(due) > 1:
        lines.append(f"(مجموع {len(due)} فاکتور — با یک پرداخت همه تسویه می‌شوند)")
    opts = await payment_methods.load_options(session)
    lines.append("\n" + payment_methods.instructions_text(opts, amount_usdt=f"{total_u:,.2f}"))
    if deferred:
        dsum = sum(float(i.amount_usdt) for i in deferred)
        lines.append(f"\n⏳ {len(deferred)} فاکتور مهلت‌دار ({dsum:,.2f} USDT) فعلاً لازم نیست پرداخت شود.")
    await answer("\n".join(lines))


async def _send_removelink(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer("لینکی برای حذف ندارید.")
        return
    items = [(r.id, f"{r.name} (…{r.admin_uuid[-6:]})") for r in resellers]
    await answer("لینک‌های ثبت‌شدهٔ شما — برای حذف انتخاب کنید:",
                 reply_markup=keyboards.remove_links_keyboard(items))


async def _send_panels(answer, chat_id: int, session) -> None:
    """Show a reseller the list of panels they're registered on (with sub-counts)."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    lines = ["🖥 پنل‌های شما:\n"]
    for r in resellers:
        panel = await session.get(Panel, r.panel_id)
        # count sub-resellers (descendants) on the same panel
        subs = (
            await session.execute(
                select(func.count(Reseller.id)).where(
                    Reseller.panel_id == r.panel_id,
                    Reseller.parent_admin_uuid == r.admin_uuid,
                )
            )
        ).scalar_one()
        tag = f" (#{r.link_tag})" if r.link_tag else ""
        lines.append(f"• {panel.name or panel.key}{tag} — زیرمجموعه‌ها: {subs}")
    await answer("\n".join(lines))


# --------------------------- sub-reseller management helpers ---------------------------
async def _owns_sub(session, chat_id: int, sub: Reseller) -> bool:
    """True if `sub` is a descendant of one of the chat's own resellers (same panel).
    Guards every management action so a reseller can only touch their own subtree."""
    from app.services.reseller_report import node_descendants

    mine = [r for r in await _resellers_for_chat(session, chat_id) if r.panel_id == sub.panel_id]
    for r in mine:
        if r.id == sub.id:
            continue
        if any(d.id == sub.id for d in await node_descendants(session, r)):
            return True
    return False


async def _send_sub_panels(answer, chat_id: int, session) -> None:
    mine = await _resellers_for_chat(session, chat_id)
    if not mine:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    items: list[tuple[int, str]] = []
    for r in mine:
        subs = (
            await session.execute(
                select(func.count(Reseller.id)).where(
                    Reseller.panel_id == r.panel_id,
                    Reseller.parent_admin_uuid == r.admin_uuid,
                )
            )
        ).scalar_one()
        if subs > 0:
            panel = await session.get(Panel, r.panel_id)
            items.append((r.id, f"{panel.name or panel.key} — {r.name} ({subs})"))
    if not items:
        await answer("شما زیرمجموعه‌ای ندارید.")
        return
    await answer(
        "👥 مدیریت زیرمجموعه‌ها\nیک پنل را انتخاب کنید:",
        reply_markup=keyboards.sub_panels_keyboard(items),
    )


async def _send_sub_list(answer, chat_id: int, parent_id: int, session) -> None:
    parent = await session.get(Reseller, parent_id)
    if not parent or parent.bot_chat_id != chat_id:
        await answer("دسترسی ندارید.")
        return
    subs = (
        await session.execute(
            select(Reseller)
            .where(
                Reseller.panel_id == parent.panel_id,
                Reseller.parent_admin_uuid == parent.admin_uuid,
            )
            .order_by(Reseller.name)
        )
    ).scalars().all()
    if not subs:
        await answer("زیرمجموعه‌ای ندارید.")
        return
    items = [
        (s.id, f"{'⛔️' if s.enforcement_state == EnforcementState.enforced else '🟢'} {s.name}")
        for s in subs
    ]
    await answer(
        f"زیرمجموعه‌های «{parent.name}» — یکی را برای مشاهده/مدیریت انتخاب کنید:",
        reply_markup=keyboards.sub_list_keyboard(items),
    )


async def _send_sub_detail(answer, chat_id: int, sub_id: int, session) -> None:
    sub = await session.get(Reseller, sub_id)
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    from app.services import reseller_report

    rep = await reseller_report.node_report(session, sub, months=3)
    enforced = sub.enforcement_state == EnforcementState.enforced
    lines = [
        f"👤 زیرمجموعه: {rep['name']}",
        f"وضعیت: {'⛔️ مسدود' if enforced else '🟢 فعال'}",
        f"تعداد کاربران: {rep['total_users']} (فعال: {rep['enabled_users']})",
    ]
    if rep["sub_count"]:
        lines.append(f"زیرمجموعه‌های این نماینده: {rep['sub_count']}")
    lines.append(f"قیمت هر گیگ: {rep['price_per_gb']:,} تومان")
    # Monthly GB-cap progress (the Hiddify-missing volume limit, simulated by us).
    cap = rep.get("gb_cap") or 0
    used = rep.get("current_gb") or 0
    if cap > 0:
        pct = rep.get("cap_pct") or 0
        bar = _cap_bar(pct)
        remaining = rep.get("cap_remaining_gb")
        status = "⛔️ به سقف رسید" if used >= cap else f"باقی‌مانده: {remaining:g} گیگ"
        lines.append(
            f"\n🎯 سقف حجم ماهانه ({rep['current_period']}):\n"
            f"{bar} {used:g}/{cap:g} گیگ ({pct}%) — {status}"
        )
    else:
        lines.append(
            f"\n🎯 سقف حجم ماهانه: تعیین نشده "
            f"(این ماه تا الان: {used:g} گیگ ساخته شده)"
        )
    lines.append("\n📊 فروش ماهانه (سهمیهٔ فروخته‌شده):")
    for m in rep["months"]:
        lines.append(
            f"• {m['label']}: {m['gb']:g} گیگ — {m['amount_toman']:,} تومان "
            f"({m['new_services']} سرویس جدید)"
        )
    lines.append("\n📄 برای دریافت فاکتور این زیرمجموعه (برای ارسال به خودش) دکمهٔ ماه را بزنید.")
    months = [m["label"] for m in rep["months"]]
    await answer(
        "\n".join(lines),
        reply_markup=keyboards.sub_detail_keyboard(sub.id, enforced, months, has_cap=cap > 0),
    )


def _cap_bar(pct: int, width: int = 10) -> str:
    """A small text progress bar for the GB cap (🟩 under 70%, 🟧 70–89%, 🟥 90%+)."""
    pct = max(0, min(100, int(pct)))
    filled = round(pct / 100 * width)
    block = "🟥" if pct >= 90 else ("🟧" if pct >= 70 else "🟩")
    return block * filled + "⬜️" * (width - filled)


async def _send_sub_invoice(answer, chat_id: int, sub_id: int, period_label: str, session, *, bot=None) -> None:
    """Generate + send a PDF invoice for ONE sub-reseller (for the reseller to bill it)."""
    sub = await session.get(Reseller, sub_id)
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    from app.services import invoice_pdf
    from app.services.periods import parse_period

    try:
        period = parse_period(period_label)
    except Exception:  # noqa: BLE001
        await answer("دورهٔ نامعتبر.")
        return
    # The issuer is the chat's own reseller on this panel (the parent billing the sub).
    mine = [r for r in await _resellers_for_chat(session, chat_id) if r.panel_id == sub.panel_id]
    issuer = mine[0].name if mine else ""
    try:
        res = await invoice_pdf.render_sub_invoice_pdf(session, sub, period, issuer_name=issuer)
    except Exception:  # noqa: BLE001
        log.warning("sub invoice pdf failed", exc_info=True)
        res = None
    if res is None:
        await answer(f"«{sub.name}» در دوره {period_label} فروشی نداشته است.")
        return
    path, fname = res
    if bot is not None:
        from aiogram.types import FSInputFile

        await bot.send_document(chat_id, FSInputFile(path, filename=fname),
                                caption=f"📄 فاکتور «{sub.name}» — دوره {period_label}")
    else:
        await answer(f"فاکتور ساخته شد: {fname}")


# --------------------------- forwarded channel post (owner) ---------------------------
@router.message(F.forward_origin)
async def on_forward(message: Message) -> None:
    chat = getattr(message.forward_origin, "chat", None)
    if not chat or chat.type not in ("channel", "supergroup", "group"):
        return
    async with SessionLocal() as session:
        if not await _is_owner_user(session, message.from_user):
            await message.answer("فقط مالک سیستم می‌تواند کانال/گروه را تنظیم کند.")
            return
        # A broadcast channel → the announcement channel; a group/supergroup → the group.
        # (A supergroup IS a group in Telegram; only a real broadcast channel has type
        # "channel", so this split is reliable.)
        if chat.type == "channel":
            await settings_service.set_value(session, "announcement_channel_id", str(chat.id))
            await message.answer(
                f"✅ کانال اطلاع‌رسانی ثبت شد:\n{chat.title}\nid: {chat.id}\n"
                "برای اجباری‌کردن عضویت، کلید «عضویت اجباری کانال» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین این کانال باشد."
            )
        else:
            await settings_service.set_value(session, "announcement_group_id", str(chat.id))
            await message.answer(
                f"✅ گروه ثبت شد:\n{chat.title}\nid: {chat.id}\n"
                "برای اجباری‌کردن عضویت، کلید «عضویت اجباری گروه» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین این گروه باشد."
            )


# --------------------------- free text (link / txid) ---------------------------
@router.message(F.document)
async def on_document(message: Message) -> None:
    """Owner sends a backup .zip → restore the system from it."""
    async with SessionLocal() as session:
        if not await _is_owner_user(session, message.from_user):
            # A reseller who sent a screenshot as an uncompressed FILE → nudge them to send
            # it as a photo (which on_photo records as a payment proof).
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
        from app.services import backup as backup_service

        buf = await message.bot.download(doc)
        result = backup_service.restore_from_zip(buf.read())
        if result.get("restored"):
            # Drop the bot's pooled connections so it reconnects to the restored data.
            from app.core.db import engine

            await engine.dispose()
        await message.answer(
            f"✅ بازیابی انجام شد ({result.get('db_kind')}).\n{result.get('note', '')}"
        )
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"❌ بازیابی ناموفق بود: {exc}")


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    """A photo from a reseller is treated as a deposit screenshot (payment proof)."""
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
        await _handle_payment_proof(message, session)


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
        txm = _TXID_RE.search(text)
        if txm:
            await _handle_txid(message, session, txm.group(0))
            return
        parsed = parse_link(text)
        if parsed:
            await _handle_link(message, session, parsed)
            return
        # Any other text / mistyped command → show the right main menu (owner vs reseller)
        # instead of a dead-end hint, so the user always sees their options.
        await _send_menu(message.answer, session, message.from_user)


async def _handle_link(message: Message, session, parsed) -> None:
    reseller = (
        await session.execute(select(Reseller).where(Reseller.admin_uuid == parsed.uuid))
    ).scalar_one_or_none()
    # Must be a real, non-owner reseller that came from one of the registered panels.
    if reseller is None or reseller.is_owner:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    if parsed.host:
        hosts = {
            (h or "").lower()
            for h in (await session.execute(select(Panel.host))).scalars().all()
        }
        if parsed.host.lower() not in hosts:
            await message.answer("این لینک به هیچ‌کدام از پنل‌های ثبت‌شدهٔ شما تعلق ندارد.")
            return
    # Prevent duplicate / takeover: if bound to another account, refuse.
    if reseller.bot_chat_id and reseller.bot_chat_id != message.from_user.id:
        await message.answer("این نماینده قبلاً توسط حساب دیگری ثبت شده است.")
        return
    already = reseller.bot_chat_id == message.from_user.id
    reseller.bot_chat_id = message.from_user.id
    reseller.link_tag = parsed.tag or reseller.link_tag
    reseller.registered_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    if already:
        await message.answer("این لینک قبلاً ثبت شده بود و اطلاعاتش به‌روزرسانی شد.")
    else:
        await message.answer(await texts.render(session, "tpl_link_matched", name=reseller.name))
        panel = await session.get(Panel, reseller.panel_id)
        await owner_notify.notify_owner(
            session, f"🔗 نمایندهٔ جدید در ربات ثبت شد: «{reseller.name}»"
            + (f" (پنل {panel.key})" if panel else ""))


async def _oldest_due_invoice(session, resellers: list[Reseller]) -> Invoice | None:
    """The customer's oldest DUE-NOW invoice (owed, not a draft, not deferred to the
    future) — what a submitted payment should be attached to."""
    ids = [r.id for r in resellers]
    if not ids:
        return None
    today = dt.date.today()
    owed = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.asc())
        )
    ).scalars().all()
    due = [i for i in owed if not (i.deferred_until and i.deferred_until > today)]
    return due[0] if due else None


async def _handle_txid(message: Message, session, txid: str) -> None:
    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    existing = (
        await session.execute(select(Payment).where(Payment.txid == txid))
    ).scalar_one_or_none()
    if existing:
        await message.answer("این تراکنش قبلاً ثبت شده است.")
        return
    # Link to the oldest DUE-NOW invoice; verification then settles across all of the
    # customer's due-now invoices.
    invoice = await _oldest_due_invoice(session, resellers)
    payment = Payment(
        reseller_id=invoice.reseller_id if invoice else resellers[0].id,
        invoice_id=invoice.id if invoice else None,
        method=PaymentMethod.usdt_txid, status=PaymentStatus.pending, txid=txid,
    )
    session.add(payment)
    await session.commit()
    from app.services.payments import verify_payment

    result = await verify_payment(session, payment.id)
    await message.answer(result.message_fa)

    # Keep the owner in the loop: a new payment either needs their manual confirm, or was
    # auto-confirmed on-chain (money arrived).
    name = resellers[0].name
    period = invoice.period_label if invoice else "—"
    amount = f"{float(invoice.amount_usdt):,.2f} USDT" if invoice else "نامشخص"
    if result.status == "confirmed":
        await owner_notify.notify_owner(
            session, f"✅ پرداخت «{name}» به‌صورت خودکار روی زنجیره تأیید شد.\n"
            f"دوره: {period} | مبلغ فاکتور: {amount}")
    else:
        await owner_notify.notify_owner(
            session, f"💳 پرداخت جدید (TXID) از «{name}» ثبت شد و منتظر تأیید شماست.\n"
            f"دوره: {period} | مبلغ فاکتور: {amount}\n"
            f"شناسهٔ پرداخت در پنل: #{payment.id}\nبرای تأیید/رد به «پرداخت‌ها» در پنل بروید.")


async def _handle_payment_proof(message: Message, session) -> None:
    """A reseller sent a deposit screenshot as proof of payment. Store it, link it to their
    oldest due invoice as a PENDING payment, and forward it to the owner for manual confirm.
    This is the easy path for customers who can't extract a TXID."""
    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        # Not a registered reseller → can't attribute the payment.
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    invoice = await _oldest_due_invoice(session, resellers)
    payment = Payment(
        reseller_id=invoice.reseller_id if invoice else resellers[0].id,
        invoice_id=invoice.id if invoice else None,
        method=PaymentMethod.screenshot, status=PaymentStatus.pending,
        note="رسید تصویری (در انتظار بررسی مالک)",
    )
    session.add(payment)
    await session.commit()

    # Download the largest rendition of the photo to disk for the panel to display.
    photo = message.photo[-1]
    proof_dir = "data/payment_proofs"
    os.makedirs(proof_dir, exist_ok=True)
    proof_path = f"{proof_dir}/payment_{payment.id}.jpg"
    saved = False
    try:
        await message.bot.download(photo, destination=proof_path)
        payment.proof_path = proof_path
        await session.commit()
        saved = True
    except Exception:  # noqa: BLE001 — keep the pending payment even if the file fails
        log.warning("failed to save payment proof for payment %s", payment.id, exc_info=True)

    await message.answer(
        "✅ رسید شما دریافت شد و پس از بررسی توسط پشتیبانی تأیید می‌شود.\n"
        "اگر شناسهٔ تراکنش (TXID) هم دارید، ارسالش کنید تا سریع‌تر بررسی شود."
    )

    # Forward the screenshot to the owner so they can confirm from Telegram + the panel.
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()
    if owner_chat:
        r = resellers[0]
        period = invoice.period_label if invoice else "—"
        amount = f"{float(invoice.amount_usdt):,.2f} USDT" if invoice else "نامشخص"
        caption = (
            f"🧾 رسید پرداخت از «{r.name}»\n"
            f"دوره: {period}\nمبلغ فاکتور: {amount}\n"
            f"شناسهٔ پرداخت در پنل: #{payment.id}\n"
            "برای تأیید/رد به بخش «پرداخت‌ها» در پنل بروید."
        )
        try:
            await message.bot.send_photo(int(owner_chat), photo.file_id, caption=caption)
        except Exception:  # noqa: BLE001
            log.warning("failed to forward payment proof to owner", exc_info=True)
            if not saved:
                await message.bot.send_message(
                    int(owner_chat),
                    f"🧾 رسید پرداخت از «{resellers[0].name}» ثبت شد (#{payment.id})، "
                    "اما ارسال تصویر ناموفق بود. در پنل بررسی کنید.",
                )
