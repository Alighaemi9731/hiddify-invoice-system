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
from app.bot.rtl import rtl
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


class PayState(StatesGroup):
    """A reseller chose ONE invoice to pay and is now sending its TXID / receipt photo
    (the chosen invoice id is held in FSM data as `pay_invoice_id`)."""

    waiting = State()


log = logging.getLogger("bot.handlers")
router = Router()

# The bot is a PRIVATE-CHAT assistant. When it's an admin of the announcement channel/
# group (needed for the membership gate + guard), Telegram delivers every group message to
# it — but it must NOT react there. Restrict ALL message handlers to private chats; group/
# channel/supergroup messages are ignored. Membership checks use the get_chat_member API,
# not message handlers, so the gate still works. Callback queries (button taps) are
# unaffected — they only occur on messages the bot itself sent in a private chat.
router.message.filter(F.chat.type == "private")

# Callbacks that must work even for a NON-member (so they can pass the gate or are inert).
_GATE_EXEMPT_CALLBACKS = {"check_membership", "noop"}


@router.callback_query.outer_middleware
async def _membership_gate_mw(handler, event, data):
    """Re-check forced-membership on EVERY button tap, not just /start.

    Without this, a user who already has the menu in their chat history (or left the
    channel/group afterwards) could keep using old buttons without being a member. The
    owner is exempt; `check_membership` is always allowed so they can re-verify."""
    try:
        cb_data = getattr(event, "data", "") or ""
        if cb_data not in _GATE_EXEMPT_CALLBACKS:
            bot = data.get("bot")
            user = getattr(event, "from_user", None)
            if bot is not None and user is not None:
                async with SessionLocal() as session:
                    if not await _is_owner_user(session, user):
                        missing = await _missing_gates(bot, session, user.id)
                        if missing:
                            names = " و ".join(g["label"] for g in missing)
                            await event.answer(
                                f"برای استفاده از ربات باید عضو {names} باشید. ابتدا /start را بزنید.",
                                show_alert=True,
                            )
                            return  # block the real handler
    except Exception:  # noqa: BLE001 — a gate error must never break the bot
        log.warning("membership gate middleware failed", exc_info=True)
    return await handler(event, data)


_TXID_RE = re.compile(r"0x[0-9a-fA-F]{64}")          # BEP-20 (BSC) tx hash
_TON_EXPLORERS = ("tonscan.org", "tonviewer.com", "ton.cx", "dton.io", "toncoin.org")


def _parse_txid(text: str, *, usdt: bool, ton: bool) -> tuple[str, str] | None:
    """Extract (chain, txid) from raw text OR a pasted explorer URL. chain ∈ {'bsc','ton'}.
    Classification honors which methods are enabled (so a hash maps to an offered chain). No
    on-chain check here — the owner verifies via the clickable link in the panel."""
    t = (text or "").strip()
    # Explorer URL → pull the hash out of the path.
    m = re.search(r"bscscan\.com/tx/(0x[0-9a-fA-F]{64})", t, re.I)
    if m and usdt:
        return ("bsc", m.group(1))
    if ton:
        m = re.search(r"(?:%s)/\S+" % "|".join(re.escape(h) for h in _TON_EXPLORERS), t, re.I)
        if m:
            seg = m.group(0).rstrip("/").split("?")[0].split("/")[-1]
            if len(seg) >= 40:
                return ("ton", seg)
    # Bare BEP-20 txid (must carry the 0x prefix).
    m = _TXID_RE.search(t)
    if m and usdt:
        return ("bsc", m.group(0))
    # Bare TON hash: a base64/base64url token (43–44 chars). A bare 64-hex is only treated as
    # TON when USDT is NOT enabled — otherwise it's almost certainly a BSC hash pasted without
    # its 0x prefix, and classifying it as TON would produce a dead tonscan link.
    if ton and not re.search(r"\s", t):
        if re.fullmatch(r"[A-Za-z0-9+/_-]{43,48}={0,2}", t):
            return ("ton", t)
        if not usdt and re.fullmatch(r"[0-9a-fA-F]{64}", t):
            return ("ton", t)
    return None


def _proof_wanted_fa(opts) -> str:
    """What proof the customer should send, given the enabled methods — for the prompt/error."""
    wants = []
    if opts.usdt or opts.ton:
        wants.append("«شناسهٔ تراکنش (TXID)» یا لینکِ آن")
    if opts.card or opts.screenshot:
        wants.append("«تصویر رسید»")
    return " یا ".join(wants) if wants else "رسید پرداخت"
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


def _iso(value) -> str:
    """Wrap a value in a Unicode First-Strong Isolate (U+2068 … U+2069) so it renders cleanly
    inside a mixed Persian/English Telegram line: the segment keeps its own auto-detected
    direction and does NOT reorder the surrounding RTL text. Use around panel keys, reseller
    names, link tags, uuids — anything that may be English/Latin and sits inside an RTL line."""
    return f"⁨{value}⁩"


async def _is_owner_user(session, user) -> bool:
    """Owner identification, hardened against @username takeover.

    Once the owner's numeric chat id has been pinned (`owner_chat_id`), we trust ONLY that id
    — a Telegram @username can be reassigned to someone else, so matching by username after
    pinning would let an attacker who grabs the owner's old handle impersonate them. Username
    (or a configured numeric id) is used ONLY for the first-ever match, which then pins the id."""
    owner_setting = str(await settings_service.get(session, "owner_telegram", "") or "").strip()
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()

    if owner_chat:
        # Pinned: numeric id is the sole source of truth.
        return str(user.id) == owner_chat

    # Not yet pinned — allow a first-time match by the configured numeric id or @username.
    uname = (user.username or "").lstrip("@").lower()
    owner_name = owner_setting.lstrip("@").lower()
    is_owner = False
    if owner_setting.isdigit() and str(user.id) == owner_setting:
        is_owner = True
    elif owner_name and uname and uname == owner_name:
        is_owner = True

    if is_owner:
        # Pin the owner's chat id so scheduled backups/alerts/logs can reach them, and so all
        # subsequent checks are id-only.
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


async def _send_menu(answer, session, user, *, bot: Bot | None = None) -> None:
    if await _is_owner_user(session, user):
        await answer(
            "👑 پنل مدیریت\nیک گزینه را انتخاب کنید:",
            reply_markup=keyboards.owner_menu_keyboard(),
        )
        return
    # A non-owner must pass the membership gate to see the menu. If `bot` is available we
    # re-check here too, so a stray message from a non-member shows the JOIN prompt (with a
    # clickable /start) instead of leaking the reseller menu.
    if bot is not None:
        missing = await _missing_gates(bot, session, user.id)
        if missing:
            names = " و ".join(g["label"] for g in missing)
            await answer(
                f"برای استفاده از ربات باید عضو {names} ما باشید.\n"
                "ابتدا /start را بزنید تا لینک عضویت برایتان ارسال شود."
            )
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
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()  # leaving any in-progress pay flow resets it (no stale invoice binding)
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
        await _sync_command_menu(bot, session, message.from_user)
        await _gate_or_menu(message.answer, bot, session, message.from_user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with SessionLocal() as session:
        await _sync_command_menu(bot, session, message.from_user)
        await _send_menu(message.answer, session, message.from_user, bot=bot)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    async with SessionLocal() as session:
        if await _is_owner_user(session, message.from_user):
            await message.answer(
                "📖 راهنمای مدیر\n\n"
                "/menu — منوی مدیریت\n"
                "/stats — آمار کلی\n"
                "/debtors — بدهکاران\n"
                "/broadcast — پیام همگانی به نمایندگان\n"
                "/sync — همگام‌سازی پنل‌ها\n"
                "/backup — پشتیبان‌گیری اکنون\n\n"
                "• ثبت کانال/گروه: یک پیام از آن را برای ربات فوروارد کنید.\n"
                "• بازیابی: فایل پشتیبان (zip) را برای ربات بفرستید.\n"
                "• پاسخ به پشتیبانی: روی پیام کاربر «ریپلای» کنید."
            )
        else:
            await message.answer(
                "📖 راهنما\n\n"
                "/menu — منوی اصلی\n"
                "/invoices — فاکتورهای پرداخت‌نشده\n"
                "/pay — پرداخت فاکتور (هر فاکتور جداگانه)\n"
                "/interim — فاکتور علی‌الحساب (ماه جاری)\n"
                "/panels — پنل‌های من\n"
                "/subs — مدیریت زیرمجموعه‌ها\n"
                "/support — پیام به پشتیبانی\n"
                "/removelink — حذف لینک‌ها\n\n"
                "برای ثبت‌نام، کافی است لینک پنل خود را همین‌جا ارسال کنید.\n"
                "پرداخت با USDT (شبکهٔ BEP-20)، کارت‌به‌کارت یا ارسال تصویر رسید انجام می‌شود."
            )


@router.message(Command("invoices"))
async def cmd_invoices(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_invoices(message.answer, message.from_user.id, s)


@router.message(Command("pay"))
async def cmd_pay(message: Message, state: FSMContext) -> None:
    await state.clear()  # re-opening the pay list resets any prior invoice selection
    async with SessionLocal() as s:
        await _send_pay(message.answer, message.from_user.id, s)


@router.message(Command("panels"))
async def cmd_panels(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_panels(message.answer, message.from_user.id, s)


@router.message(Command("interim"))
async def cmd_interim(message: Message, bot: Bot) -> None:
    async with SessionLocal() as s:
        await _send_self_interim(message.answer, message.from_user.id, s, bot=bot)


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await message.answer("پیام خود را برای پشتیبانی بنویسید (یا /cancel برای لغو):")


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


@router.callback_query(F.data.startswith("inv:"))
async def cb_invoice_view(cb: CallbackQuery, bot: Bot) -> None:
    """Re-send the full content of one of the caller's invoices (text + per-node PDFs)."""
    try:
        inv_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    await cb.answer("در حال ارسال فاکتور…")
    async with SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        owned_ids = {r.id for r in resellers}
        inv = await s.get(Invoice, inv_id)
        # Ownership check: a reseller may only view invoices of their own reseller rows.
        if inv is None or inv.reseller_id not in owned_ids:
            await cb.message.answer("این فاکتور در دسترس شما نیست.")
            return
        reseller = await s.get(Reseller, inv.reseller_id)
        from app.services import delivery

        try:
            await delivery.send_invoice_content(s, bot, cb.from_user.id, inv, reseller)
        except Exception:  # noqa: BLE001
            log.warning("invoice view failed for %s", inv_id, exc_info=True)
            await cb.message.answer("ارسال فاکتور با خطا مواجه شد؛ بعداً دوباره تلاش کنید.")


@router.callback_query(F.data == "menu:interim")
async def cb_interim(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer("در حال ساخت فاکتور علی‌الحساب…")
    async with SessionLocal() as s:
        await _send_self_interim(cb.message.answer, cb.from_user.id, s, bot=bot)


@router.callback_query(F.data == "menu:pay")
async def cb_pay(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # tapping «پرداخت فاکتور» again resets any prior invoice selection
    async with SessionLocal() as s:
        await _send_pay(cb.message.answer, cb.from_user.id, s)
    await cb.answer()


@router.callback_query(F.data.startswith("payinv:"))
async def cb_pay_invoice(cb: CallbackQuery, state: FSMContext) -> None:
    """Start paying ONE chosen invoice: show its amount + instructions and wait for the
    customer's TXID / receipt photo, which is then attributed to exactly this invoice."""
    from app.services import payment_methods

    try:
        inv_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    async with SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        owned = {r.id for r in resellers}
        inv = await s.get(Invoice, inv_id)
        # Not payable if: not the caller's, not owed, OR deferred to a future date (a payment
        # deadline the owner granted) — `_send_pay` excludes deferred invoices, so a stale
        # button must not let one be paid early.
        deferred = inv is not None and inv.deferred_until and inv.deferred_until > dt.date.today()
        if inv is None or inv.reseller_id not in owned or inv.status not in _OWED or deferred:
            await cb.answer("این فاکتور در حال حاضر قابل پرداخت نیست.", show_alert=True)
            return
        # Already submitted a payment for this invoice → don't let them pay again (one pending
        # per invoice); tell them it's under review.
        if await _pending_payment_for_invoice(s, inv.id) is not None:
            await cb.answer(
                "برای این فاکتور قبلاً رسید فرستاده‌اید و در انتظار تأیید است؛ لطفاً منتظر بمانید.",
                show_alert=True,
            )
            return
        opts = await payment_methods.load_options(s)
        amount_ton = None
        if opts.ton:
            from app.services import rates
            ton_rate = await rates.get_ton_toman(s)
            if ton_rate:
                amount_ton = f"{float(inv.amount_toman) / ton_rate:,.2f}"
        text = (
            f"💳 پرداخت فاکتور دوره {inv.period_label}\n"
            f"مبلغ: {float(inv.amount_toman):,.0f} تومان ({float(inv.amount_usdt):,.2f} USDT)\n\n"
            + payment_methods.instructions_text(
                opts, amount_usdt=f"{float(inv.amount_usdt):,.2f}",
                amount_toman=f"{float(inv.amount_toman):,.0f}", amount_ton=amount_ton, html=True)
            + "\n\nℹ️ این مبلغ فقط برای همین فاکتور است. پس از واریز، شناسهٔ تراکنش (TXID) یا "
              "تصویر رسید را همین‌جا بفرستید (برای لغو: /cancel)."
        )
        await state.set_state(PayState.waiting)
        await state.update_data(pay_invoice_id=inv_id)
        await cb.message.answer(rtl(text), parse_mode="HTML")
    await cb.answer()


@router.message(PayState.waiting, F.photo)
async def pay_state_photo(message: Message, state: FSMContext) -> None:
    # A photo is always a valid receipt (manual review), regardless of method.
    data = await state.get_data()
    inv_id = data.get("pay_invoice_id")
    await state.clear()
    async with SessionLocal() as s:
        await _track_user(s, message.from_user)
        inv = await s.get(Invoice, int(inv_id)) if inv_id else None
        await _handle_payment_proof(message, s, invoice=inv)


@router.message(PayState.waiting, F.text)
async def pay_state_text(message: Message, state: FSMContext) -> None:
    """The pay flow is LOCKED: the customer must send a valid txid/receipt (per the enabled
    methods) or /cancel. Any other text gets a clear error and keeps them in the flow — it does
    NOT fall through to the menu."""
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel", "لغو"):
        await state.clear()
        await message.answer("پرداخت لغو شد. هر وقت خواستی، دوباره از «💳 پرداخت فاکتور» اقدام کن.")
        return
    from app.services import payment_methods

    async with SessionLocal() as s:
        await _track_user(s, message.from_user)
        opts = await payment_methods.load_options(s)
        parsed = _parse_txid(text, usdt=opts.usdt, ton=opts.ton)
        if parsed is None:
            # Invalid input → explain what's needed and STAY in the pay flow (no menu).
            await message.answer(rtl(
                "❌ ورودی نامعتبر است.\n"
                f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید.\n"
                "اگر نمی‌خواهی پرداخت کنی، /cancel را بزن."
            ))
            return
        chain, txid = parsed
        data = await state.get_data()
        inv = await s.get(Invoice, int(data["pay_invoice_id"])) if data.get("pay_invoice_id") else None
        await state.clear()
        await _handle_txid(message, s, txid, invoice=inv, chain=chain)


@router.message(PayState.waiting)
async def pay_state_other(message: Message) -> None:
    """Any other content (document, sticker, …) while paying → keep them in the locked flow."""
    from app.services import payment_methods

    async with SessionLocal() as s:
        opts = await payment_methods.load_options(s)
    await message.answer(rtl(
        f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید.\n"
        "اگر نمی‌خواهی پرداخت کنی، /cancel را بزن."
    ))


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
async def _dispatch_owner(action: str, answer, session) -> None:
    """Run an owner action. Shared by the menu buttons (cb_owner) AND the owner `/` commands,
    so the slash-command list and the inline menu always do the exact same thing."""
    if action == "stats":
        await _owner_stats(answer, session)
    elif action == "debtors":
        await _owner_debtors(answer, session)
    elif action == "broadcast":
        await answer("📢 گیرندگان پیام همگانی را انتخاب کنید:",
                     reply_markup=keyboards.broadcast_audience_keyboard())
    elif action == "sync":
        from app.services import sync as sync_service

        await answer("⏳ در حال همگام‌سازی پنل‌ها…")
        res = await sync_service.sync_all(session)
        ok = sum(1 for r in res if r.status.value == "success")
        await answer(f"🔄 همگام‌سازی انجام شد: {ok}/{len(res)} پنل موفق.")
    elif action == "backup":
        from app.services import backup_delivery

        await answer("⏳ در حال تهیهٔ پشتیبان…")
        r = await backup_delivery.send_backup_to_owner(session)
        if r.get("status") == "sent":
            await answer("🗄 پشتیبان تهیه و برای شما ارسال شد.")
        elif r.get("status") in ("no_owner_chat", "no_bot"):
            await answer(f"⚠️ پشتیبان روی سرور ذخیره شد ولی ارسال نشد ({r.get('status')}).")
        else:
            await answer("❌ ارسال پشتیبان ناموفق بود.")
    elif action == "monthly":
        from app.services import delivery, invoicing, sync as sync_service
        from app.services.periods import previous_month

        await answer("⏳ در حال همگام‌سازی، صدور و ارسال ماه قبل...")
        await sync_service.sync_all(session)
        p = previous_month()
        g = await invoicing.generate_invoices(session, p)
        d = await delivery.send_period(session, p.label)
        await answer(
            f"✅ دوره {p.label}: {g.created} فاکتور ساخته شد، "
            f"{d.get('sent', 0)} ارسال موفق، {d.get('unmatched', 0)} بدون ربات."
        )


@router.callback_query(F.data.startswith("owner:"))
async def cb_owner(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await _dispatch_owner(action, cb.message.answer, s)
    await cb.answer()


# Owner `/` commands — mirror the owner menu buttons exactly (see commands.OWNER_COMMANDS).
# `/broadcast` is handled by its own dedicated command handler above (it also accepts inline
# text), so it's intentionally not duplicated here.
_OWNER_CMD_ACTION = {
    "stats": "stats", "debtors": "debtors", "sync": "sync", "backup": "backup",
}


@router.message(Command(commands=list(_OWNER_CMD_ACTION)))
async def cmd_owner_action(message: Message, command: CommandObject) -> None:
    action = _OWNER_CMD_ACTION.get(command.command)
    if not action:
        return
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
            return  # owner-only; resellers don't see these commands
        await _dispatch_owner(action, message.answer, s)


async def _owner_stats(answer, session) -> None:
    from app.services.periods import current_month
    from app.services.reseller_stats import load_root_stats

    panels = (await session.execute(select(func.count(Panel.id)))).scalar_one()
    # Count only MAIN (top-level) resellers that are billable — not their sub-resellers,
    # not the exempt ones — and how many of those are connected to the bot.
    stats = await load_root_stats(session)
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
    exempt_note = f" + {stats.exempt} معاف" if stats.exempt else ""
    await answer(
        f"📊 آمار کلی\n"
        f"پنل‌ها: {panels}\n"
        f"نمایندگان اصلی: {stats.billable} ({stats.connected} متصل به ربات){exempt_note}\n"
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
    # Each row starts with a right-to-left mark (‏) so a line that begins with an
    # English reseller name still renders right-aligned in Telegram (otherwise the first
    # Latin character flips the whole line to LTR and it reads garbled).
    lines = ["💰 بدهکاران برتر:\n"]
    for i, (name, total) in enumerate(rows, 1):
        lines.append(f"‏{i}. {_iso(name)}: {float(total):,.0f} تومان")
    await answer("\n".join(lines))


# --------------------------- shared reseller views ---------------------------
async def _pending_payment_for_invoice(session, invoice_id: int | None):
    """The PENDING payment already submitted for this invoice (if any) — used to block a
    duplicate submission so one invoice never spawns several pending payments."""
    if not invoice_id:
        return None
    return (
        await session.execute(
            select(Payment).where(
                Payment.invoice_id == invoice_id, Payment.status == PaymentStatus.pending
            ).limit(1)
        )
    ).scalar_one_or_none()


async def _pending_invoice_ids(session, reseller_ids: list[int]) -> set[int]:
    """Invoice ids that already have a PENDING payment awaiting the owner's review — so the bot
    shows «در انتظار تأیید» and blocks a duplicate submission for the same invoice."""
    if not reseller_ids:
        return set()
    rows = (
        await session.execute(
            select(Payment.invoice_id).where(
                Payment.reseller_id.in_(reseller_ids),
                Payment.status == PaymentStatus.pending,
                Payment.invoice_id.is_not(None),
            )
        )
    ).scalars().all()
    return {i for i in rows if i}


async def _send_invoices(answer, chat_id: int, session) -> None:
    """«فاکتورهای پرداخت‌نشده» — the reseller's UNPAID, already-issued invoices, each as a
    button; tapping re-sends the full invoice (text + PDFs). An invoice with a payment awaiting
    review is marked «در انتظار تأیید» so the customer knows not to pay again."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    invoices = (
        await session.execute(
            select(Invoice)
            .where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.desc()).limit(12)
        )
    ).scalars().all()
    if not invoices:
        await answer("فاکتور پرداخت‌نشده‌ای ندارید. 🎉")
        return
    pending = await _pending_invoice_ids(session, ids)
    items = []
    for inv in invoices:
        toman = f"{float(inv.amount_toman):,.0f} ت"
        if inv.id in pending:
            items.append((inv.id, f"⏳ دوره {inv.period_label} — {toman} (در انتظار تأیید)"))
        else:
            status = _STATUS_FA.get(inv.status.value, inv.status.value)
            items.append((inv.id, f"🧾 دوره {inv.period_label} — {toman} ({status})"))
    note = ""
    if pending:
        note = "\n⏳ فاکتورهای «در انتظار تأیید» را فرستاده‌اید؛ تا بررسیِ پشتیبانی منتظر بمانید."
    await answer(
        "🧾 فاکتورهای پرداخت‌نشدهٔ شما — برای دیدن کاملِ هر فاکتور (متن + PDF) روی آن بزنید.\n"
        "برای پرداخت، از «💳 پرداخت فاکتور» استفاده کنید." + note,
        reply_markup=keyboards.my_invoices_keyboard(items),
    )


async def _send_self_interim(answer, chat_id: int, session, *, bot=None) -> None:
    """A reseller's OWN interim invoice for the CURRENT month so far — same SCOPE as the real
    end-of-month invoice (their own users + all sub-resellers), but marked interim. Sends a
    text breakdown (own + each sub + Rial total) plus volume-only PDFs split per node: ONE PDF
    for the reseller's own users and ONE PDF per sub-reseller (its subtree), so each can be
    handed to the matching sub without exposing the others. The grand total stays text-only."""
    from app.services import invoice_pdf, reseller_report
    from app.services.periods import current_month

    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    period = current_month()
    sent_any = False
    for r in resellers:
        # Only TOP-LEVEL resellers get a bundled invoice (a sub is billed via its parent).
        if not await _is_top_level_reseller(session, r):
            continue
        bd = await reseller_report.interim_breakdown(session, r, period)
        if bd["total_gb"] <= 0:
            await answer(f"«{r.name}»: در دورهٔ جاری ({period.label}) هنوز مصرفی ثبت نشده است.")
            sent_any = True
            continue

        # --- text breakdown ---
        # Each line starts with a Persian word so Telegram renders the WHOLE line RTL even
        # when the (sub-)reseller's name is English — otherwise a line beginning with a
        # Latin name gets left-aligned and reads garbled.
        price = bd["price"]
        lines = [
            f"📄 فاکتور علی‌الحساب — «{r.name}»",
            f"دوره: {period.label} (تا امروز)",
            f"قیمت هر گیگ: {price:,} تومان",
            "",
            "🟦 مصرف خودتان:",
            f"• حجم {bd['own']['gb']:g} گیگ ({bd['own']['users']} سرویس) — {bd['own']['amount']:,} تومان",
        ]
        if bd["subs"]:
            lines.append("\n🟨 زیرمجموعه‌های شما:")
            for s in bd["subs"]:
                # Isolate the (possibly English) name so the GB/Toman after it don't reorder.
                lines.append(
                    f"• نماینده {_iso(s['name'])}: حجم {s['gb']:g} گیگ "
                    f"({s['users']} سرویس) — {s['amount']:,} تومان"
                )
        lines += [
            "",
            "➖➖➖➖➖➖➖➖",
            f"📊 مجموع حجم: {bd['total_gb']:g} گیگ ({bd['total_users']} سرویس)",
            f"💰 مجموع مبلغ: {bd['total_amount']:,} تومان",
            "",
            "ℹ️ این فاکتور علی‌الحساب است؛ اول ماه آینده فاکتور کامل و واقعیِ قابل پرداخت برایتان ارسال می‌شود.",
        ]
        if bd["subs"]:
            lines.append("\n📎 در ادامه، یک PDF جدا برای خودتان و هر زیرمجموعه ارسال می‌شود تا بتوانید به هرکدام بدهید.")
        text = "\n".join(lines)

        owner_name = await settings_service.get(session, "owner_name", "") or ""
        # Send the text breakdown first (it's the summary), then the PDFs.
        await answer(text)

        if bot is None:
            sent_any = True
            continue
        from aiogram.types import FSInputFile

        # (1) The admin's OWN invoice — ONLY their own users (not the subtree), exactly
        #     like each sub gets its own PDF; own + each sub cover everyone once.
        try:
            res = await invoice_pdf.render_own_usage_pdf(
                session, r, period, title="فاکتور علی الحساب", issuer_name=owner_name
            )
            if res:
                path, fname = res
                await bot.send_document(
                    chat_id, FSInputFile(path, filename=fname),
                    caption=f"📄 فاکتور علی‌الحساب شما «{r.name}» (فقط کاربران خودتان)",
                )
        except Exception:  # noqa: BLE001
            log.warning("interim own pdf failed", exc_info=True)

        # (2) A separate PDF per sub-reseller, so the admin can forward each to that sub.
        for s in bd["subs"]:
            sub = await session.get(Reseller, s["id"])
            if sub is None:
                continue
            try:
                sres = await invoice_pdf.render_node_usage_pdf(
                    session, sub, period, title="فاکتور علی الحساب", issuer_name=r.name
                )
                if sres:
                    spath, sfname = sres
                    await bot.send_document(
                        chat_id, FSInputFile(spath, filename=sfname),
                        caption=f"📄 فاکتور علی‌الحساب زیرمجموعه «{sub.name}» — {s['gb']:g} گیگ",
                    )
            except Exception:  # noqa: BLE001
                log.warning("interim sub pdf failed for %s", s.get("id"), exc_info=True)
        sent_any = True
    if not sent_any:
        await answer("شما نمایندهٔ اصلی نیستید؛ فاکتور شما از طریق نمایندهٔ بالادستی صادر می‌شود.")


async def _send_pay(answer, chat_id: int, session) -> None:
    """«پرداخت فاکتور» — list each UNPAID, due-now invoice as its OWN button so the customer
    pays them SEPARATELY (no lumping into one transfer). Tapping a button (payinv:<id>) starts
    paying just that invoice."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    owed = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.desc())
        )
    ).scalars().all()
    today = dt.date.today()
    due = [i for i in owed if not (i.deferred_until and i.deferred_until > today)]
    deferred = [i for i in owed if i.deferred_until and i.deferred_until > today]
    # An invoice with a payment already awaiting review is NOT offered for payment again —
    # the customer is told it's under review (one pending payment per invoice).
    pending = await _pending_invoice_ids(session, ids)
    payable = [i for i in due if i.id not in pending]
    in_review = [i for i in due if i.id in pending]

    if not payable:
        if in_review:
            await answer(
                f"⏳ {len(in_review)} فاکتور فرستاده‌اید و در انتظار تأیید پشتیبانی است؛ "
                "لطفاً منتظر بمانید. (لازم نیست دوباره بفرستید.)"
            )
        elif deferred:
            await answer("فاکتورِ سررسیدشده‌ای برای پرداخت ندارید؛ فاکتورهای شما مهلت‌دار هستند. ⏳")
        else:
            await answer("بدهی فعالی برای پرداخت ندارید. 🎉")
        return

    items = [
        (i.id,
         f"💳 دوره {i.period_label} — {float(i.amount_toman):,.0f} ت ({float(i.amount_usdt):,.2f} USDT)")
        for i in payable
    ]
    msg = "💳 کدام فاکتور را می‌خواهید پرداخت کنید؟\nهر فاکتور را جداگانه پرداخت می‌کنید — روی آن بزنید:"
    if in_review:
        msg += f"\n\n⏳ {len(in_review)} فاکتور دیگر در انتظار تأیید است (لازم نیست دوباره بفرستید)."
    if deferred:
        msg += f"\n\n📅 {len(deferred)} فاکتور مهلت‌دار فعلاً لازم نیست پرداخت شود."
    await answer(msg, reply_markup=keyboards.pay_invoices_keyboard(items))


async def _send_removelink(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer("لینکی برای حذف ندارید.")
        return
    items = [(r.id, _iso(f"{r.name} (…{r.admin_uuid[-6:]})")) for r in resellers]
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
        # Isolate the panel label so the English key + (#tag) render as one clean LTR chunk
        # inside the RTL line instead of reordering with the trailing count.
        lines.append(f"‏• پنل {_iso((panel.name or panel.key) + tag)} — زیرمجموعه‌ها: {subs}")
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
            items.append((r.id, f"پنل {_iso((panel.name or panel.key))} — {_iso(r.name)} ({subs})"))
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
            reply = (
                f"✅ کانال اطلاع‌رسانی ثبت شد:\n{chat.title}\nid: {chat.id}\n"
                "برای اجباری‌کردن عضویت، کلید «عضویت اجباری کانال» را در تنظیمات روشن کنید.\n"
                "توجه: ربات باید ادمین این کانال باشد."
            )
            # If this channel has a linked DISCUSSION GROUP, register it automatically — you
            # can't forward FROM a group (Telegram hides the group as the forward origin),
            # so reading the channel's linked_chat is the reliable way to capture a private
            # group. Needs the bot to be in (ideally admin of) the channel.
            try:
                full = await message.bot.get_chat(chat.id)
                linked = getattr(full, "linked_chat_id", None)
                if linked:
                    await settings_service.set_value(session, "announcement_group_id", str(linked))
                    reply += (
                        f"\n\n🔗 گروه گفتگوی متصل به این کانال هم خودکار ثبت شد (id: {linked}).\n"
                        "برای اجباری‌کردن عضویت گروه، کلید «عضویت اجباری گروه» را روشن کنید."
                    )
            except Exception:  # noqa: BLE001 — best-effort; the manual path below still works
                log.info("could not read linked_chat for channel %s", chat.id, exc_info=True)
            await message.answer(reply)
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


_SETCHAT_RE = re.compile(r"^(channel|group|کانال|گروه)\s+(-?\d{5,})$", re.IGNORECASE)


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    text = (message.text or "").strip()
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
        # Owner manual fallback for a PRIVATE GROUP that can't be forwarded: «group -100…»
        # (or «channel -100…»). Telegram hides a group as the forward origin, so this lets
        # the owner paste the numeric id directly.
        m = _SETCHAT_RE.match(text)
        if m and await _is_owner_user(session, message.from_user):
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
        txp = _parse_txid(text, usdt=opts.usdt, ton=opts.ton)
        if txp:
            await _handle_txid(message, session, txp[1], chain=txp[0])
            return
        parsed = parse_link(text)
        if parsed:
            await _handle_link(message, session, parsed)
            return
        # Any other text / mistyped command → show the right main menu (owner vs reseller),
        # but a non-member gets the join prompt (with a clickable /start), not the menu.
        await _send_menu(message.answer, session, message.from_user, bot=bot)


async def _is_top_level_reseller(session, reseller: Reseller) -> bool:
    """True only for a TOP-LEVEL reseller — a direct child of the panel's Owner. Mirrors the
    billing engine's `select_billable_roots` rule so "who may register in the bot" matches
    "who gets billed". A sub-reseller (its parent is another reseller, not the Owner) is NOT
    top-level: it's managed/billed through its parent, so it must not self-register."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    owner_uuids = {r.admin_uuid for r in panel_resellers if r.is_owner}
    all_uuids = {r.admin_uuid for r in panel_resellers}
    if owner_uuids:
        return reseller.parent_admin_uuid in owner_uuids
    # No Owner row in the data → fall back to structural roots (orphans / no parent).
    return reseller.parent_admin_uuid is None or reseller.parent_admin_uuid not in all_uuids


async def _handle_link(message: Message, session, parsed) -> None:
    # The same admin_uuid can exist on more than one panel, so a uuid-only lookup may return
    # several rows — `scalar_one_or_none()` would raise MultipleResultsFound and crash the
    # handler (no reply). Fetch all and disambiguate by the link's host (the documented
    # "host + UUID identifies the panel" rule); fall back to the first if host can't resolve.
    candidates = (
        await session.execute(select(Reseller).where(Reseller.admin_uuid == parsed.uuid))
    ).scalars().all()
    if not candidates:
        reseller = None
    elif len(candidates) == 1:
        reseller = candidates[0]
    else:
        reseller = None
        host = (parsed.host or "").lower()
        if host:
            for c in candidates:
                p = await session.get(Panel, c.panel_id)
                if p and (p.host or "").lower() == host:
                    reseller = c
                    break
        reseller = reseller or candidates[0]
    # Must be a real, non-owner reseller that came from one of the registered panels.
    if reseller is None or reseller.is_owner:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Only TOP-LEVEL resellers may register. A sub-reseller is handled by its parent (the
    # parent issues its invoices + manages it from «مدیریت زیرمجموعه‌ها»), so block it.
    if not await _is_top_level_reseller(session, reseller):
        await message.answer(
            "این لینک متعلق به یک زیرمجموعه است.\n"
            "زیرمجموعه‌ها مستقیماً در ربات ثبت نمی‌شوند؛ مدیریت و صورتحساب شما از طریق "
            "نمایندهٔ بالادستی‌تان انجام می‌شود. لطفاً با او هماهنگ کنید."
        )
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


def _invoice_amount_fa(invoice) -> str:
    """«X تومان (Y USDT)» for an owner-facing receipt; «نامشخص» when there's no invoice."""
    if invoice is None:
        return "نامشخص"
    return f"{float(invoice.amount_toman):,.0f} تومان ({float(invoice.amount_usdt):,.2f} USDT)"


async def _invoice_amount_for_chain(session, invoice, chain: str) -> str:
    """Like _invoice_amount_fa but shows the equivalent in the PAID currency: TON for a TON
    payment, USDT otherwise — so a TON payment's owner message doesn't show a USDT figure."""
    if invoice is None:
        return "نامشخص"
    toman = f"{float(invoice.amount_toman):,.0f} تومان"
    if chain == "ton":
        from app.services import rates
        rate = await rates.get_ton_toman(session)
        if rate:
            return f"{toman} (≈ {float(invoice.amount_toman) / rate:,.2f} TON)"
        return toman
    return f"{toman} ({float(invoice.amount_usdt):,.2f} USDT)"


async def _oldest_due_invoice(session, resellers: list[Reseller]) -> Invoice | None:
    """The customer's oldest PAYABLE invoice (owed, not deferred, and NOT already awaiting
    review) — what a cold-path payment (a TXID/photo sent without picking a button) attaches to.

    Excluding invoices that already have a pending payment is essential: otherwise a customer
    who paid invoice B while invoice A (older) is still under review would have B's cold-path
    payment attributed to A and then blocked as a duplicate — silently dropping B's transfer."""
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
    pending = await _pending_invoice_ids(session, ids)
    due = [
        i for i in owed
        if not (i.deferred_until and i.deferred_until > today) and i.id not in pending
    ]
    return due[0] if due else None


async def _handle_txid(message: Message, session, txid: str, *, invoice=None, chain: str = "bsc") -> None:
    """Record a submitted tx hash (USDT/BSC or TON) as a PENDING payment for MANUAL review —
    no on-chain auto-verify. The owner opens the clickable explorer link in the panel and
    confirms/rejects."""
    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    existing = (
        await session.execute(select(Payment).where(Payment.txid == txid))
    ).scalars().first()
    if existing:
        if existing.status == PaymentStatus.confirmed:
            await message.answer("این تراکنش قبلاً ثبت و تأیید شده است.")
            return
        if existing.status == PaymentStatus.pending:
            await message.answer("این تراکنش قبلاً ثبت شده و در انتظار بررسی است.")
            return
        # Rejected → the reject notice told them «دوباره ارسال کنید», and the txid is unique, so
        # RE-OPEN the same row for another manual review instead of dead-ending them.
        existing.status = PaymentStatus.pending
        if "[resubmitted]" not in (existing.note or ""):
            existing.note = (existing.note or "") + " [resubmitted]"
        await session.commit()
        await message.answer(rtl(
            "✅ شناسهٔ تراکنش دوباره برای بررسی ثبت شد؛ منتظر تأیید پشتیبانی بمانید.\n"
            f"🔖 شمارهٔ پیگیری: #{existing.id}"
        ))
        inv2 = await session.get(Invoice, existing.invoice_id) if existing.invoice_id else None
        await owner_notify.notify_owner(
            session,
            f"🔁 نمایندهٔ «{resellers[0].name}» تراکنشِ ردشده را دوباره فرستاد (#{existing.id}).\n"
            f"دوره: {inv2.period_label if inv2 else '—'} | مبلغ فاکتور: {_invoice_amount_fa(inv2)}\n"
            "برای تأیید/رد به «پرداخت‌ها» در پنل بروید.")
        return
    # Link to the chosen invoice (from «پرداخت فاکتور») if given; otherwise the oldest payable.
    if invoice is None:
        invoice = await _oldest_due_invoice(session, resellers)
    # One pending payment per invoice → don't create a duplicate for an invoice under review.
    if invoice is not None and await _pending_payment_for_invoice(session, invoice.id) is not None:
        await message.answer(
            "برای این فاکتور قبلاً پرداختی ثبت کرده‌اید که در انتظار تأیید است؛ "
            "لطفاً منتظر بررسیِ پشتیبانی بمانید."
        )
        return
    method = PaymentMethod.ton_txid if chain == "ton" else PaymentMethod.usdt_txid
    payment = Payment(
        reseller_id=invoice.reseller_id if invoice else resellers[0].id,
        invoice_id=invoice.id if invoice else None,
        method=method, chain=chain, status=PaymentStatus.pending, txid=txid,
    )
    session.add(payment)
    await session.commit()

    label = "TON" if chain == "ton" else "USDT"
    await message.answer(rtl(
        f"✅ شناسهٔ تراکنش ({label}) دریافت شد و در انتظار تأیید پشتیبانی است.\n"
        "نتیجهٔ بررسی همین‌جا به شما اطلاع داده می‌شود.\n"
        f"🔖 شمارهٔ پیگیری: #{payment.id}"
    ))
    name = resellers[0].name
    period = invoice.period_label if invoice else "—"
    amount = await _invoice_amount_for_chain(session, invoice, chain)
    await owner_notify.notify_owner(
        session, f"💳 پرداخت جدید ({label} TXID) از «{name}» ثبت شد و منتظر تأیید شماست.\n"
        f"دوره: {period} | مبلغ فاکتور: {amount}\n"
        f"شناسهٔ پرداخت در پنل: #{payment.id}\nبرای تأیید/رد به «پرداخت‌ها» در پنل بروید.")


async def _handle_payment_proof(message: Message, session, *, invoice=None) -> None:
    """A reseller sent a deposit screenshot as proof of payment. Store it, link it to the
    chosen invoice (from «پرداخت فاکتور») or their oldest due one as a PENDING payment, and
    forward it to the owner for manual confirm. The easy path for customers without a TXID."""
    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        # Not a registered reseller → can't attribute the payment.
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    if invoice is None:
        invoice = await _oldest_due_invoice(session, resellers)
    # One pending payment per invoice — block a duplicate receipt for an invoice already
    # awaiting review (so a customer who sends 2–3 receipts doesn't spawn 2–3 payments).
    if invoice is not None and await _pending_payment_for_invoice(session, invoice.id) is not None:
        await message.answer(
            "برای این فاکتور قبلاً رسید فرستاده‌اید که در انتظار تأیید است؛ "
            "لطفاً منتظر بررسیِ پشتیبانی بمانید. (نیازی به ارسال دوباره نیست.)"
        )
        return
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

    await message.answer(rtl(
        "✅ رسید شما دریافت شد و در انتظار تأیید پشتیبانی است.\n"
        "لطفاً منتظر بمانید؛ نتیجهٔ بررسی همین‌جا به شما اطلاع داده می‌شود. (نیازی به ارسال دوباره نیست.)\n"
        f"🔖 شمارهٔ پیگیری: #{payment.id}"
    ))

    # Forward the screenshot to the owner so they can confirm from Telegram + the panel.
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()
    if owner_chat:
        r = resellers[0]
        period = invoice.period_label if invoice else "—"
        amount = _invoice_amount_fa(invoice)
        caption = rtl(
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
                    rtl(f"🧾 رسید پرداخت از «{resellers[0].name}» ثبت شد (#{payment.id})، "
                        "اما ارسال تصویر ناموفق بود. در پنل بررسی کنید."),
                )
