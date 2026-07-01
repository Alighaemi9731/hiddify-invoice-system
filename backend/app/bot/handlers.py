"""Reseller + owner bot handlers: membership gate, menus, registration, payment."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import func, or_, select

from app.bot import keyboards, texts
from app.bot.matching import normalize_host, normalize_path, parse_link
from app.bot.rtl import rtl
from app.core.codes import payment_code
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
from app.services.periods import today as tehran_today


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


class OwnerCapBumpState(StatesGroup):
    """The owner is entering a custom capacity-increase amount for a reseller that requested
    more capacity from the web portal (the reseller id is held in FSM data)."""

    waiting = State()


class PayState(StatesGroup):
    """A reseller chose one or more invoices to pay and is now sending the TXID / receipt photo
    (the chosen invoice ids are held in FSM data as `pay_invoice_ids`, a list)."""

    waiting = State()


class OwnerSearchState(StatesGroup):
    """The owner is typing a reseller name/uuid to look up (admin-bot search)."""

    waiting = State()


class CreateUserState(StatesGroup):
    """A top-level reseller is creating end-user(s). Choices (reseller_id, mode, count, gb, days)
    are collected via inline buttons into FSM data; `name` waits for the typed user/base name."""

    name = State()


class StorefrontSetupState(StatesGroup):
    """A reseller is setting up their VPN storefront bot — `token` waits for the BotFather token."""

    token = State()


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
_GATE_EXEMPT_COMMANDS = {"start", "cancel"}


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
        await event.answer(
            "بررسی عضویت موقتاً ممکن نیست؛ لطفاً کمی بعد دوباره تلاش کنید.",
            show_alert=True,
        )
        return
    return await handler(event, data)


def _message_command(text: str | None) -> str | None:
    """Return a normalized Telegram slash command, excluding any @bot suffix."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    first = parts[0]
    if not first.startswith("/"):
        return None
    return first[1:].split("@", 1)[0].lower()


@router.message.outer_middleware
async def _membership_gate_message_mw(handler, event, data):
    """Apply forced membership to direct commands and FSM messages as well as callbacks.

    `/start` must remain reachable so a non-member can obtain join links, and `/cancel`
    remains reachable so an in-progress FSM can always be exited. Owners are exempt.
    """
    chat = getattr(event, "chat", None)
    if getattr(chat, "type", None) != "private":
        return await handler(event, data)
    if _message_command(getattr(event, "text", None)) in _GATE_EXEMPT_COMMANDS:
        return await handler(event, data)
    try:
        bot = data.get("bot")
        user = getattr(event, "from_user", None)
        if bot is not None and user is not None:
            async with SessionLocal() as session:
                if not await _is_owner_user(session, user):
                    missing = await _missing_gates(bot, session, user.id)
                    if missing:
                        names = " و ".join(g["label"] for g in missing)
                        await event.answer(
                            f"برای استفاده از ربات باید عضو {names} باشید.\n"
                            "ابتدا /start را بزنید تا لینک عضویت برایتان ارسال شود."
                        )
                        return
    except Exception:  # noqa: BLE001 — gate failures must not grant access
        log.warning("message membership gate middleware failed", exc_info=True)
        await event.answer("بررسی عضویت موقتاً ممکن نیست؛ لطفاً کمی بعد دوباره تلاش کنید.")
        return
    return await handler(event, data)


_TXID_RE = re.compile(r"0x[0-9a-fA-F]{64}")          # BEP-20 (BSC) / Avalanche C-Chain tx hash
_TON_EXPLORERS = ("tonscan.org", "tonviewer.com", "ton.cx", "dton.io", "toncoin.org")


def _parse_txid(text: str, *, usdt: bool, ton: bool, avax: bool) -> tuple[str, str] | None:
    """Extract (chain, txid) from raw text OR a pasted explorer URL. chain ∈ {'bsc','ton','avax'},
    or the sentinel 'ambiguous' when a BARE 0x hash arrives and BOTH USDT(BSC) and AVAX are enabled
    (they share the 0x+64hex format) — the caller then asks which network. Classification honors
    which methods are enabled (so a hash maps to an offered chain). No on-chain check here — the
    owner verifies via the clickable link in the panel."""
    t = (text or "").strip()
    # An explorer URL is AUTHORITATIVE about the chain: resolve to that chain when it's enabled,
    # else REJECT (return None) — never fall through to the bare-0x scanner below, which would
    # mis-attribute the hash to the *other* 0x-chain (a snowtrace link → bsc, or a bscscan link →
    # avax) and produce a dead/wrong review link in the panel.
    m = re.search(r"snowtrace\.io/tx/(0x[0-9a-fA-F]{64})", t, re.I)
    if m:
        return ("avax", m.group(1).lower()) if avax else None
    m = re.search(r"bscscan\.com/tx/(0x[0-9a-fA-F]{64})", t, re.I)
    if m:
        return ("bsc", m.group(1)) if usdt else None
    if ton:
        explorers = "|".join(re.escape(host) for host in _TON_EXPLORERS)
        m = re.search(rf"(?:{explorers})/\S+", t, re.I)
        if m:
            seg = m.group(0).rstrip("/").split("?")[0].split("/")[-1]
            if len(seg) >= 40:
                return ("ton", seg)
    # Bare 0x+64hex txid — identical format on BSC and Avalanche C-Chain. Map to whichever of those
    # two is enabled; if BOTH, it's genuinely ambiguous → let the caller ask which network.
    m = _TXID_RE.search(t)
    if m:
        hsh = m.group(0)
        if usdt and avax:
            return ("ambiguous", hsh)
        if avax:
            return ("avax", hsh.lower())
        if usdt:
            return ("bsc", hsh)
    # Bare TON hash: a base64/base64url token (43–44 chars). A bare 64-hex is only treated as TON
    # when neither 0x-chain (USDT/AVAX) is enabled — otherwise it's almost certainly such a hash
    # pasted without its 0x prefix, and classifying it as TON would produce a dead tonscan link.
    if ton and not re.search(r"\s", t):
        if re.fullmatch(r"[A-Za-z0-9+/_-]{43,48}={0,2}", t):
            return ("ton", t)
        if not usdt and not avax and re.fullmatch(r"[0-9a-fA-F]{64}", t):
            return ("ton", t)
    return None


def _proof_wanted_fa(opts) -> str:
    """What proof the customer should send, given the enabled methods — for the prompt/error."""
    wants = []
    if opts.usdt or opts.ton or opts.avax:
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

    `owner_telegram` is the owner identity the admin sets in Settings; `owner_chat_id` is the
    pinned numeric chat the bot reaches for menus/alerts. Rules:
      * If `owner_telegram` is a NUMERIC id, it is AUTHORITATIVE — the owner is exactly that id, and
        we (re-)pin `owner_chat_id` to it. So editing it in Settings to a new id takes effect on the
        new owner's next interaction, and a stale pin never wins (fixes "changed the id but the bot
        still knows the old owner").
      * Otherwise (an @username, or unset): once `owner_chat_id` is pinned we trust ONLY that id (a
        reassigned @username can't impersonate the owner); before pinning, a first match by the
        configured @username pins the id."""
    owner_setting = str(await settings_service.get(session, "owner_telegram", "") or "").strip()
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()

    # Explicit numeric owner id is the source of truth (Settings change applies immediately).
    if owner_setting.isdigit():
        is_owner = str(user.id) == owner_setting
        if is_owner and owner_chat != owner_setting:
            await settings_service.set_value(session, "owner_chat_id", owner_setting)
        return is_owner

    if owner_chat:
        # Pinned (username-based identity): numeric id is the sole source of truth.
        return str(user.id) == owner_chat

    # Not yet pinned — allow a first-time match by the configured @username.
    uname = (user.username or "").lstrip("@").lower()
    owner_name = owner_setting.lstrip("@").lower()
    is_owner = bool(owner_name and uname and uname == owner_name)
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
        if member.status in ("member", "administrator", "creator"):
            return True
        # In a supergroup a user under ANY restriction reports status `restricted` but is still IN the
        # group (Telegram flags this with is_member=True). Channels never report `restricted`, so this
        # only matters for the group gate — matching channel_guard, which already counts `restricted`.
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False  # left / kicked
    except Exception as exc:  # noqa: BLE001 — fail closed (treat as non-member) on API errors
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
        # Menu is inline again; clear any lingering docked reply keyboard (from the brief reply-menu
        # era), then show the inline menu.
        await answer("👑 پنل مدیریت", reply_markup=ReplyKeyboardRemove())
        await answer(
            "یک گزینه را انتخاب کنید:",
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
    can_create = await _can_create_users(session, user.id)
    can_storefront = await _can_setup_storefront(session, user.id)
    # Clear any lingering docked reply keyboard, then show the inline menu (the docked menu didn't fit).
    await answer(welcome, reply_markup=ReplyKeyboardRemove())
    await answer(
        menu,
        reply_markup=keyboards.reseller_menu_keyboard(
            show_create_user=can_create, show_storefront=can_storefront),
    )


async def _reshow_menu(message: Message, session, user) -> None:  # noqa: ANN001
    """Re-send the role-aware main menu as the LAST message so it's always at hand after a completed
    action (the menu is inline, so it scrolls up as the chat fills with messages). Compact — just the
    menu keyboard, no welcome text. Call this only at the END of an action/flow, never when entering
    an FSM prompt (that would put the menu below the prompt mid-flow)."""
    try:
        if await _is_owner_user(session, user):
            await message.answer("📋 منوی مدیریت:", reply_markup=keyboards.owner_menu_keyboard())
            return
        can_create = await _can_create_users(session, user.id)
        can_storefront = await _can_setup_storefront(session, user.id)
        await message.answer(
            "📋 منوی اصلی:",
            reply_markup=keyboards.reseller_menu_keyboard(
                show_create_user=can_create, show_storefront=can_storefront),
        )
    except Exception:  # noqa: BLE001 — a menu re-show must never break the action it follows
        pass


async def _top_level_resellers(session, chat_id: int) -> list[Reseller]:
    """The chat's reseller rows that are TOP-LEVEL on their panel (eligible to create users)."""
    out = []
    for r in await _resellers_for_chat(session, chat_id):
        if await _is_top_level_reseller(session, r):
            out.append(r)
    return out


async def _can_create_users(session, chat_id: int) -> bool:
    """True if the user-creation feature is on AND this chat has at least one top-level reseller."""
    from app.services import usercreate

    opts = await usercreate.load_options(session)
    if not opts.enabled or not (opts.gb and opts.days):
        return False
    return bool(await _top_level_resellers(session, chat_id))


async def _can_setup_storefront(session, chat_id: int) -> bool:
    """True if the owner enabled the storefront feature for at least one of this chat's top-level
    resellers."""
    return any(
        getattr(r, "storefront_enabled", False)
        for r in await _top_level_resellers(session, chat_id)
    )


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
                "/health — سلامت سامانه\n"
                "/payments — پرداخت‌های در انتظار\n"
                "/debtors — بدهکاران\n"
                "/search — جستجوی نماینده\n"
                "/broadcast — پیام همگانی به نمایندگان\n"
                "/sync — همگام‌سازی پنل‌ها\n"
                "/backup — پشتیبان‌گیری اکنون\n"
                "/cancel — لغو عملیات جاری\n\n"
                "• ثبت کانال/گروه: یک پیام از آن را برای ربات فوروارد کنید.\n"
                "• بازیابی: فایل پشتیبان (zip) را برای ربات بفرستید.\n"
                "• پاسخ به پشتیبانی: روی پیام کاربر «ریپلای» کنید."
            )
        else:
            await message.answer(
                "📖 راهنما\n\n"
                "/menu — منوی اصلی\n"
                "/invoices — فاکتورهای پرداخت‌نشده\n"
                "/pay — پرداخت فاکتور (می‌توانید همهٔ بدهی را یکجا پرداخت کنید)\n"
                "/interim — فاکتور علی‌الحساب (ماه جاری)\n"
                "/panels — پنل‌های من\n"
                "/subs — مدیریت زیرمجموعه‌ها\n"
                "/newuser — ساخت کاربر (نماینده‌های اصلی)\n"
                "/storefront — راه‌اندازی ربات فروشگاهی\n"
                "/portal — ورود به پنلِ تحتِ وب\n"
                "/register — ثبت لینک پنل من\n"
                "/support — پیام به پشتیبانی\n"
                "/removelink — حذف لینک‌ها\n"
                "/cancel — لغو عملیات جاری\n\n"
                "برای ثبت‌نام، کافی است لینک پنل خود را همین‌جا ارسال کنید.\n"
                "پرداخت با رمزارز (USDT/گرام/AVAX)، کارت‌به‌کارت یا ارسال تصویر رسید انجام می‌شود "
                "(روش‌های فعال هنگام پرداخت نمایش داده می‌شوند)."
            )


@router.message(Command("invoices"))
async def cmd_invoices(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_invoices(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("pay"))
async def cmd_pay(message: Message, state: FSMContext) -> None:
    await state.clear()  # re-opening the pay list resets any prior invoice selection
    async with SessionLocal() as s:
        await _send_pay(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a payable-invoice picker; a trailing menu would bury it.


@router.message(Command("panels"))
async def cmd_panels(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_panels(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("portal"))
async def cmd_portal(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_portal_link(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("interim"))
async def cmd_interim(message: Message, bot: Bot) -> None:
    async with SessionLocal() as s:
        await _send_self_interim(message.answer, message.from_user.id, s, bot=bot)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await message.answer(
        "پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.cancel_keyboard()
    )


@router.message(Command("removelink"))
async def cmd_removelink(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_removelink(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a link picker (tap to remove) — a menu would bury it.


@router.message(Command("subs"))
async def cmd_subs(message: Message) -> None:
    async with SessionLocal() as s:
        await _send_sub_panels(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a sub-panel picker — a menu would bury it.


@router.message(Command("storefront"))
async def cmd_storefront(message: Message, state: FSMContext) -> None:
    """Start the storefront-bot setup (self-gated: only top-level resellers with the capability)."""
    async with SessionLocal() as s:
        await _begin_storefront_setup(message.answer, message.from_user.id, s, state)


@router.message(Command("register"))
async def cmd_register(message: Message) -> None:
    """Prompt for the panel link (same as the «🔗 ثبت لینک پنل من» menu action)."""
    await message.answer(
        "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
        reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
    )


# --------------------------- broadcast (owner) ---------------------------
_AUDIENCE_FA = {"all": "همه نمایندگان", "debtors": "بدهکاران", "zero_sale": "فروش صفر این ماه"}


async def _audience_label(session, audience: str, panel_id: int | None) -> str:
    if audience == "panel" and panel_id is not None:
        panel = await session.get(Panel, panel_id)
        return f"نمایندگان پنل {panel.key}" if panel else "نمایندگان یک پنل"
    return _AUDIENCE_FA.get(audience, audience)


async def _bot_broadcast_bg(text: str, reachable: list, unregistered: int) -> None:
    """Background send for an owner bot-triggered broadcast — its own session/bot (the handler's
    session is already closed). The final summary reaches the owner via owner_notify.notify_owner."""
    try:
        async with SessionLocal() as session:
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
        f"اکنون متن پیام را ارسال کنید:",
        reply_markup=keyboards.cancel_keyboard(),
    )
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("لغو شد.")


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Universal exit button carried by every FSM prompt: clear the state and return the role-aware
    main menu, so a user can ALWAYS tap their way out (never forced to remember /cancel)."""
    await state.clear()
    async with SessionLocal() as s:
        await _send_menu(cb.message.answer, s, cb.from_user, bot=bot)
    await cb.answer("لغو شد.")


# --------------------------- persistent docked main menu (reply keyboard) ---------------------------
# Registered BEFORE every FSM text handler so a tap on a docked main-menu button ALWAYS works — it
# clears any in-progress flow and navigates, acting as a universal escape (the user can never get stuck).
@router.message(F.text.in_(keyboards.ALL_MENU_LABELS))
async def on_menu_label(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    async with SessionLocal() as s:
        await _track_user(s, message.from_user)
        is_owner = await _is_owner_user(s, message.from_user)
        # A non-owner must still be a member; the message middleware already gates this.
        await state.clear()
        if is_owner:
            action = keyboards.OWNER_LABEL_TO_ACTION.get(text)
            if action:
                await _do_owner_menu(action, message, state, s)
                return
        action = keyboards.RESELLER_LABEL_TO_ACTION.get(text)
        if action:
            await _do_reseller_menu(action, message, state, bot, s)
            return
        # A label that doesn't apply to this role → just (re)show their own menu.
        await _send_menu(message.answer, s, message.from_user, bot=bot)


# Terminal reseller actions whose result is complete info the user just reads → re-show the menu
# after them. EXCLUDED: selection-list actions (pay/subs/removelink) whose result is a picker the
# user must tap next — a trailing menu would bury those buttons; the menu re-appears when the
# sub-action completes. FSM-entering actions (storefront/support/register/newuser) re-show on
# completion. (Matches the owner's «اگه تموم شد» — only after a completed action.)
_RESELLER_TERMINAL = {"invoices", "interim", "panels", "portal"}
# Terminal owner actions. EXCLUDED: payments (a picker) + broadcast/search (open a flow).
_OWNER_TERMINAL = {"stats", "health", "debtors", "sync", "backup"}


async def _do_reseller_menu(action: str, message: Message, state: FSMContext, bot: Bot, s) -> None:  # noqa: ANN001
    ans, cid = message.answer, message.from_user.id
    if action == "invoices":
        await _send_invoices(ans, cid, s)
    elif action == "pay":
        await _send_pay(ans, cid, s)
    elif action == "interim":
        await _send_self_interim(ans, cid, s, bot=bot)
    elif action == "panels":
        await _send_panels(ans, cid, s)
    elif action == "subs":
        await _send_sub_panels(ans, cid, s)
    elif action == "portal":
        await _send_portal_link(ans, cid, s)
    elif action == "storefront":
        await _begin_storefront_setup(ans, cid, s, state)
    elif action == "removelink":
        await _send_removelink(ans, cid, s)
    elif action == "support":
        await state.set_state(SupportState.waiting)
        await ans("پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.cancel_keyboard())
    elif action == "register":
        await ans(
            "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
            reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
        )
    elif action == "newuser":
        await _begin_create_user(ans, cid, s, state)
    # Keep the menu at hand after a completed (non-flow) action.
    if action in _RESELLER_TERMINAL:
        await _reshow_menu(message, s, message.from_user)


async def _do_owner_menu(action: str, message: Message, state: FSMContext, s) -> None:  # noqa: ANN001
    ans = message.answer
    if action == "search":
        await state.set_state(OwnerSearchState.waiting)
        await ans(
            "🔎 نام یا شناسهٔ نماینده را بفرستید.",
            reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
        )
        return
    await _dispatch_owner(action, ans, s)
    if action in _OWNER_TERMINAL:
        await _reshow_menu(message, s, message.from_user)


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
        await bot.send_message(
            int(owner_chat),
            _support_message_html(u, text),
            reply_markup=keyboards.support_reply_keyboard(u.id, message.message_id),
            parse_mode="HTML",
        )
        await message.answer("✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ می‌گیرید.")
        await _reshow_menu(message, s, message.from_user)


def _support_message_html(user, text: str) -> str:
    """Build owner-facing support HTML with every Telegram/user value escaped."""
    handle = (
        f"@{html.escape(user.username)}"
        if user.username
        else f"<a href='tg://user?id={user.id}'>{html.escape(str(user.first_name or user.id))}</a>"
    )
    return (
        f"💬 پیام پشتیبانی\nاز: {handle} (id: <code>{user.id}</code>)\n\n"
        f"{html.escape(text)}"
    )


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
    await cb.message.answer(f"پاسخ خود را برای کاربر <code>{target}</code> بنویسید:",
                            parse_mode="HTML", reply_markup=keyboards.cancel_keyboard())
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
            except TelegramForbiddenError:
                raise
            except Exception:  # noqa: BLE001
                await message.bot.send_message(int(target), body)
        else:
            await message.bot.send_message(int(target), body)
        await message.answer("✅ پاسخ ارسال شد.")
    except TelegramForbiddenError:
        # The user blocked the bot (or deleted their account) — say that plainly instead of
        # dumping the raw English API error on the owner.
        await message.answer("⛔️ ارسال نشد: این کاربر ربات را مسدود کرده یا حسابش حذف شده است.")
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            await message.answer("⛔️ ارسال نشد: گفتگویی با این کاربر پیدا نشد (شاید هرگز ربات را استارت نکرده است).")
        else:
            await message.answer(f"ارسال پاسخ ناموفق بود: {exc}")
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
        await _reshow_menu(message, s, message.from_user)


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
    await cb.message.answer(
        "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
        reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
    )
    await cb.answer()


@router.callback_query(F.data == "menu:panels")
async def cb_panels(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_panels(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


@router.callback_query(F.data == "menu:portal")
async def cb_portal(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_portal_link(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


# --------------------------- sub-reseller management ---------------------------
@router.callback_query(F.data == "menu:subs")
async def cb_subs(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_sub_panels(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: sub-panel picker (tap to manage) — a trailing menu would bury it.
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
                f"⏳ مسدودسازی «{sub.name}» در صف ثبت شد و مرحله‌ای انجام می‌شود."
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
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await cb.message.answer(f"⏳ توقف ساخت کاربر برای «{sub.name}» در صف قرار می‌گیرد...")
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
                f"⏳ «{sub.name}»: توقف ساخت کاربر در صف ثبت شد. کاربرانِ فعلیِ او قطع نمی‌شوند؛ "
                "فقط ساخت کاربرِ جدید و افزایش ظرفیت متوقف می‌شود."
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
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        # State-aware wording: lifting a freeze vs lifting a full suspension.
        verb = (
            "رفع توقف ساخت کاربر"
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
                f"⏳ {verb} «{sub.name}» در صف ثبت شد و مرحله‌ای انجام می‌شود."
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
    async with SessionLocal() as s:
        await _send_sub_invoice(cb.message.answer, cb.from_user.id, sub_id, period_label, s, bot=bot)


@router.callback_query(F.data.startswith("subcap:"))
async def cb_sub_cap(cb: CallbackQuery) -> None:
    """Show preset GB-cap buttons for this sub-reseller (no typing needed; «مقدار دلخواه» for custom)."""
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        cur = int(sub.gb_cap or 0)
        name = sub.name
    cur_txt = f"سقف فعلی: {cur:g} گیگ" if cur > 0 else "سقف فعلی: تعیین‌نشده"
    await cb.message.answer(
        f"🎯 سقف حجم ماهانه برای «{name}»\n{cur_txt}\n\n"
        "یک مقدار را انتخاب کنید (یا «مقدار دلخواه»). این سقف فقط برای هشدار است و هر ماه ریست می‌شود.",
        reply_markup=keyboards.sub_cap_keyboard(sub_id),
    )
    await cb.answer()


async def _apply_sub_cap(answer, session, chat_id: int, sub_id: int | None, gb: int) -> None:
    sub = await session.get(Reseller, sub_id) if sub_id else None
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    sub.gb_cap = gb or None
    sub.gb_cap_alerted_period = None  # re-arm the alert for the new ceiling
    await session.commit()
    if gb > 0:
        await answer(f"✅ سقف حجم ماهانهٔ «{sub.name}» روی {gb:g} گیگ تنظیم شد.")
    else:
        await answer(f"✅ سقف حجم «{sub.name}» حذف شد (بدون محدودیت).")


@router.callback_query(F.data.startswith("setcap:"))
async def cb_set_cap(cb: CallbackQuery) -> None:
    """A preset GB-cap tap → set it directly, no typing (gb=0 clears the cap)."""
    parts = cb.data.split(":")
    sub_id, gb = int(parts[1]), int(parts[2])
    async with SessionLocal() as s:
        await _apply_sub_cap(cb.message.answer, s, cb.from_user.id, sub_id, gb)
    await cb.answer()


@router.callback_query(F.data.startswith("capcustom:"))
async def cb_cap_custom(cb: CallbackQuery, state: FSMContext) -> None:
    """«مقدار دلخواه» → enter the text path, with a tappable «انصراف»."""
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        sub = await s.get(Reseller, sub_id)
        if not sub or not await _owns_sub(s, cb.from_user.id, sub):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await state.set_state(SubCapState.waiting)
    await state.update_data(sub_id=sub_id)
    await cb.message.answer(
        "عدد سقف را به گیگابایت بفرستید (مثلاً 500). برای حذف سقف، عدد 0 را بفرستید.",
        reply_markup=keyboards.cancel_keyboard(),
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
            "عدد نامعتبر بود. یک عددِ صحیح (گیگابایت) بفرستید.",
            reply_markup=keyboards.cancel_keyboard(),
        )
        return
    data = await state.get_data()
    sub_id = data.get("sub_id")
    await state.clear()
    async with SessionLocal() as s:
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
            "عدد نامعتبر بود. یک عددِ صحیح بین ۱ تا ۵۰۰۰ بفرستید.",
            reply_markup=keyboards.cancel_keyboard(),
        )
        return
    amount = int(raw)
    data = await state.get_data()
    rid = data.get("cap_rid")
    await state.clear()
    async with SessionLocal() as s:
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
    await message.answer(f"✅ ظرفیت «{rname}» {amount}+ شد (سقف جدید: {new_mu}).")


# --------------------------- create user (top-level resellers) ---------------------------
async def _begin_create_user(answer, chat_id: int, session, state: FSMContext) -> None:
    """Entry point for «ساخت کاربر»: validate eligibility, then ask for the panel (if >1) or the mode."""
    from app.services import usercreate

    await state.clear()
    opts = await usercreate.load_options(session)
    if not opts.enabled:
        await answer("ساخت کاربر در حال حاضر غیرفعال است.")
        return
    if not (opts.gb and opts.days):
        await answer("گزینه‌های حجم/روز پیکربندی نشده‌اند؛ به پشتیبانی اطلاع دهید.")
        return
    roots = await _top_level_resellers(session, chat_id)
    if not roots:
        await answer("فقط نماینده‌های اصلی می‌توانند کاربر بسازند.")
        return
    if len(roots) == 1:
        await state.update_data(cu_reseller_id=roots[0].id)
        await answer(
            f"➕ ساخت کاربر روی پنلِ «{_iso(roots[0].name)}»\nنوعِ ساخت را انتخاب کنید:",
            reply_markup=keyboards.create_user_mode_keyboard(),
        )
        return
    items = []
    for r in roots:
        panel = await session.get(Panel, r.panel_id)
        items.append((r.id, _iso(f"{(panel.name or panel.key) if panel else '?'} — {r.name}")))
    await answer("➕ ساخت کاربر\nروی کدام پنل؟", reply_markup=keyboards.create_user_panels_keyboard(items))


@router.message(Command("newuser"))
async def cmd_newuser(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as s:
        await _begin_create_user(message.answer, message.from_user.id, s, state)


@router.callback_query(F.data == "menu:newuser")
async def cb_menu_newuser(cb: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as s:
        await _begin_create_user(cb.message.answer, cb.from_user.id, s, state)
    await cb.answer()


@router.callback_query(F.data == "cucancel")
async def cb_cu_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("لغو شد.")
    await cb.answer()


# --------------------------- storefront-bot setup wizard ---------------------------
_BOTFATHER_GUIDE = (
    "🏪 راه‌اندازی ربات فروشگاهی\n\n"
    "۱) در تلگرام به @BotFather بروید.\n"
    "۲) دستور /newbot را بفرستید؛ یک نام و سپس یک یوزرنیم (که به bot ختم می‌شود) انتخاب کنید.\n"
    "۳) توکنی که می‌دهد (مثلِ <code>123456789:AA...</code>) را کپی کنید.\n"
    "۴) همان توکن را همین‌جا بفرستید تا رباتِ فروشگاهیِ شما ساخته و فعال شود.\n\n"
    "برای لغو، «انصراف» را بزنید."
)


async def _storefront_target_line(session, r: Reseller) -> str:  # noqa: ANN001
    """A one-liner naming which account/panel this storefront bot will sell from — always shown so the
    owner knows the target before sending the token."""
    panel = await session.get(Panel, r.panel_id)
    return (f"🏪 رباتِ فروشگاهی برای حسابِ «{r.name or '—'}» روی پنلِ "
            f"«{panel.key if panel else '?'}» راه‌اندازی می‌شود.\n\n")


async def _begin_storefront_setup(answer, chat_id: int, session, state: FSMContext) -> None:  # noqa: ANN001
    from app.services import storefront

    # One storefront bot PER PANEL (per reseller row): a person who is top-level on several panels can
    # run a separate bot for each. Re-running setup on a panel that already has a bot just replaces its
    # token (data preserved). Only top-level resellers reach here.
    roots = [
        r for r in await _top_level_resellers(session, chat_id)
        if getattr(r, "storefront_enabled", False)
    ]
    if not roots:
        await answer(rtl("این قابلیت برای شما فعال نیست."))
        return
    if len(roots) == 1:
        r = roots[0]
        target = await _storefront_target_line(session, r)
        if await storefront.get_bot_for_reseller(session, r.id) is not None:
            target += ("این پنل هم‌اکنون یک ربات دارد — توکنِ جدید جایگزین می‌شود و همهٔ داده‌ها "
                       "(پلن‌ها/مشتری‌ها/سرویس‌ها/کیفِ پول) حفظ می‌شوند.\n\n")
        await state.set_state(StorefrontSetupState.token)
        await state.update_data(sf_reseller_id=r.id)
        await answer(rtl(target + _BOTFATHER_GUIDE), parse_mode="HTML",
                     reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    items = []
    for r in roots:
        panel = await session.get(Panel, r.panel_id)
        bot = await storefront.get_bot_for_reseller(session, r.id)
        status = f"ربات فعلی: @{bot.bot_username or '—'}" if bot is not None else "بدون ربات"
        items.append((r.id, f"{r.name or '—'} — {panel.key if panel else '?'} ({status})"))
    await answer(rtl("🏪 برای کدام حساب/پنل؟ (هر پنل یک ربات جداگانه)"),
                 reply_markup=keyboards.storefront_setup_panels_keyboard(items))


@router.callback_query(F.data == "menu:storefront")
async def cb_menu_storefront(cb: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as s:
        await _begin_storefront_setup(cb.message.answer, cb.from_user.id, s, state)
    await cb.answer()


@router.callback_query(F.data.startswith("sfsetup:"))
async def cb_sf_setup_panel(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        r = await s.get(Reseller, rid)
        # Defensive: must be this person's OWN, storefront-enabled, TOP-LEVEL reseller (never a sub).
        if r is None or r.bot_chat_id != cb.from_user.id \
                or not getattr(r, "storefront_enabled", False) \
                or not await _is_top_level_reseller(s, r):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        target = await _storefront_target_line(s, r)
    await state.set_state(StorefrontSetupState.token)
    await state.update_data(sf_reseller_id=rid)
    await cb.message.answer(rtl(target + _BOTFATHER_GUIDE), parse_mode="HTML",
                            reply_markup=keyboards.cancel_keyboard("« انصراف"))
    await cb.answer()


@router.message(StorefrontSetupState.token, F.text)
async def on_sf_setup_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if ":" not in token or len(token) < 20:
        await message.answer(rtl("توکن نامعتبر است؛ توکنِ BotFather را بفرستید."),
                             reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    probe = Bot(token=token)
    try:
        me = await probe.get_me()
    except Exception:  # noqa: BLE001
        await message.answer(rtl("این توکن کار نکرد؛ مطمئن شوید درست کپی شده و دوباره بفرستید."),
                             reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    finally:
        await probe.session.close()
    data = await state.get_data()
    rid = int(data.get("sf_reseller_id") or 0)
    await state.clear()
    from app.services import storefront

    async with SessionLocal() as s:
        r = await s.get(Reseller, rid)
        # Must be this person's OWN, storefront-enabled, TOP-LEVEL reseller (never a sub-reseller).
        if r is None or r.bot_chat_id != message.from_user.id \
                or not getattr(r, "storefront_enabled", False) \
                or not await _is_top_level_reseller(s, r):
            await message.answer(rtl("دسترسی ندارید."))
            return
        # One bot per panel: if THIS panel already has a bot, the token replaces it (data preserved).
        replaced = await storefront.get_bot_for_reseller(s, rid) is not None
        # Each panel needs its OWN Telegram bot/token — reject reusing one already bound elsewhere.
        by_tgid = await storefront.get_bot_by_telegram_id(s, me.id)
        if by_tgid is not None and by_tgid.reseller_id != rid:
            await message.answer(rtl(
                "این توکن قبلاً برای پنلِ دیگری ثبت شده است؛ برای هر پنل یک ربات/توکنِ جداگانه لازم است."))
            return
        await storefront.upsert_bot(s, reseller_id=rid, panel_id=r.panel_id, token=token,
                                    bot_username=me.username, bot_telegram_id=me.id)
    try:  # start it now (the storefront manager runs in this same process)
        from app.bot.storefront import manager

        await manager.reconcile()
    except Exception:  # noqa: BLE001
        log.warning("storefront reconcile after setup failed", exc_info=True)
    if replaced:
        await message.answer(rtl(
            f"✅ رباتِ فروشگاهیِ شما به توکنِ جدید منتقل شد: @{me.username}\n"
            "همهٔ داده‌ها (پلن‌ها، مشتری‌ها، سرویس‌ها، کیفِ پول) حفظ شد و ربات قبلی از کار افتاد.\n"
            "واردِ رباتِ جدید شوید و /start را بزنید."))
    else:
        await message.answer(rtl(
            f"✅ رباتِ فروشگاهیِ شما آماده شد: @{me.username}\n"
            "اکنون واردِ رباتِ خودتان شوید و /start را بزنید تا پنلِ مدیریت را ببینید."))


@router.callback_query(F.data.startswith("cupanel:"))
async def cb_cu_panel(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        r = await s.get(Reseller, rid)
        if r is None or r.bot_chat_id != cb.from_user.id or not await _is_top_level_reseller(s, r):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        name = r.name
    await state.update_data(cu_reseller_id=rid)
    await cb.message.answer(
        f"➕ ساخت کاربر روی پنلِ «{_iso(name)}»\nنوعِ ساخت را انتخاب کنید:",
        reply_markup=keyboards.create_user_mode_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cumode:"))
async def cb_cu_mode(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":")[1]
    data = await state.get_data()
    if "cu_reseller_id" not in data:
        await cb.answer("از ابتدا شروع کنید.", show_alert=True)
        return
    async with SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if mode == "bulk":
        await state.update_data(cu_mode="bulk")
        await cb.message.answer(
            "تعدادِ کاربر را انتخاب کنید:",
            reply_markup=keyboards.create_user_count_keyboard(opts.counts),
        )
    else:
        await state.update_data(cu_mode="single", cu_count=1)
        await cb.message.answer(
            "حجم را انتخاب کنید:", reply_markup=keyboards.create_user_gb_keyboard(opts.gb)
        )
    await cb.answer()


@router.callback_query(F.data.startswith("cucount:"))
async def cb_cu_count(cb: CallbackQuery, state: FSMContext) -> None:
    count = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if count not in opts.counts:
        await cb.answer("نامعتبر.", show_alert=True)
        return
    await state.update_data(cu_count=count)
    await cb.message.answer(
        "حجم را انتخاب کنید:", reply_markup=keyboards.create_user_gb_keyboard(opts.gb)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cugb:"))
async def cb_cu_gb(cb: CallbackQuery, state: FSMContext) -> None:
    gb = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if gb not in opts.gb:
        await cb.answer("نامعتبر.", show_alert=True)
        return
    await state.update_data(cu_gb=gb)
    await cb.message.answer(
        "مدت را انتخاب کنید:", reply_markup=keyboards.create_user_days_keyboard(opts.days)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cudays:"))
async def cb_cu_days(cb: CallbackQuery, state: FSMContext) -> None:
    days = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        from app.services import usercreate
        opts = await usercreate.load_options(s)
    if days not in opts.days:
        await cb.answer("نامعتبر.", show_alert=True)
        return
    data = await state.get_data()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_count")):
        await cb.answer("از ابتدا شروع کنید.", show_alert=True)
        return
    await state.update_data(cu_days=days)
    await state.set_state(CreateUserState.name)
    is_bulk = data.get("cu_mode") == "bulk"
    prompt = (
        "یک نامِ پایه برای کاربران بفرستید (به انتهای آن شماره اضافه می‌شود):"
        if is_bulk else "یک نام برای کاربر بفرستید:"
    )
    await cb.message.answer(
        prompt, reply_markup=keyboards.cancel_keyboard("« انصراف", "cucancel")
    )
    await cb.answer()


@router.message(CreateUserState.name)
async def on_cu_name(message: Message, state: FSMContext) -> None:
    name = " ".join((message.text or "").split())  # collapse whitespace
    if not name or len(name) > 40:
        await message.answer(
            "نام نامعتبر است (۱ تا ۴۰ نویسه). دوباره بفرستید.",
            reply_markup=keyboards.cancel_keyboard("« انصراف", "cucancel"),
        )
        return
    await state.update_data(cu_name=name)
    data = await state.get_data()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_days", "cu_count")):
        await state.clear()
        await message.answer("اطلاعات ناقص است؛ از «➕ ساخت کاربر» دوباره شروع کنید.")
        return
    count = int(data["cu_count"])
    gb = int(data["cu_gb"])
    days = int(data["cu_days"])
    if data.get("cu_mode") == "bulk":
        summary = (
            "تأیید ساخت کاربرِ گروهی؟\n"
            f"• تعداد: {count} (با نام {_iso(f'{name}1')} تا {_iso(f'{name}{count}')})\n"
            f"• حجم: {gb} گیگ\n• مدت: {days} روز"
        )
    else:
        summary = f"تأیید ساخت کاربر؟\n• نام: {_iso(name)}\n• حجم: {gb} گیگ\n• مدت: {days} روز"
    await message.answer(summary, reply_markup=keyboards.create_user_confirm_keyboard())


@router.callback_query(F.data == "cuok")
async def cb_cu_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if not all(k in data for k in ("cu_reseller_id", "cu_gb", "cu_days", "cu_name", "cu_count")):
        await cb.answer("اطلاعات ناقص است؛ از ابتدا شروع کنید.", show_alert=True)
        return
    await cb.answer("در حال ساخت…")
    await _finish_create_user(cb.message, cb.from_user.id, data, user=cb.from_user)


async def _finish_create_user(message: Message, chat_id: int, data: dict, *, user=None) -> None:  # noqa: ANN001
    # `message` is cb.message (bot-authored), so its from_user is the BOT — use the passed real
    # `user` (the reseller) for the menu re-show, falling back to message.from_user defensively.
    menu_user = user or message.from_user
    from app.services import usercreate

    rid = int(data["cu_reseller_id"])
    gb = int(data["cu_gb"])
    days = int(data["cu_days"])
    count = int(data.get("cu_count", 1))
    base = str(data["cu_name"])
    async with SessionLocal() as s:
        reseller = await s.get(Reseller, rid)
        if reseller is None or reseller.bot_chat_id != chat_id or not await _is_top_level_reseller(s, reseller):
            await message.answer("دسترسی ندارید.")
            return
        result = await usercreate.create_for_reseller(
            s, reseller, count=count, gb=gb, days=days, base_name=base
        )
        rname = reseller.name
        panel = await s.get(Panel, reseller.panel_id)
        pname = panel.key if panel else "?"

    if result.capacity_blocked:
        remaining = result.remaining if result.remaining is not None else 0
        await message.answer(
            f"⛔️ ظرفیتِ شما اجازهٔ ساختِ {count} کاربر را نمی‌دهد "
            f"(باقی‌مانده: {remaining} از {result.max_users}).\n"
            "از «👥 زیرمجموعه‌ها» یا پشتیبانی، افزایشِ ظرفیت درخواست کنید."
        )
        async with SessionLocal() as s:
            await _reshow_menu(message, s, menu_user)
        return

    for u in result.created:
        link_attr = html.escape(u.sub_link, quote=True)
        link_text = html.escape(u.sub_link)
        caption = rtl(
            f"✅ کاربر «{html.escape(u.name)}» ساخته شد — {gb} گیگ · {days} روز\n\n"
            f"🔗 لینکِ اشتراک (auto):\n<a href=\"{link_attr}\">{link_text}</a>\n<code>{link_text}</code>"
        )
        try:
            png = usercreate.qr_png(u.sub_link)
            await message.bot.send_photo(
                chat_id, BufferedInputFile(png, filename="sub.png"),
                caption=caption, parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001 — fall back to a text message if the photo fails
            log.warning("failed to send created-user QR", exc_info=True)
            await message.bot.send_message(
                chat_id, caption, parse_mode="HTML", disable_web_page_preview=True
            )
        await asyncio.sleep(0.4)  # gentle pacing for Telegram's per-chat rate limit

    made = len(result.created)
    if result.limit_hit:
        await message.answer(
            f"⚠️ {made} از {count} کاربر ساخته شد؛ بقیه به‌خاطرِ سقفِ ظرفیتِ پنل ناموفق بود. "
            "افزایشِ ظرفیت درخواست کنید."
        )
    elif result.error:
        await message.answer(f"⚠️ {made} از {count} کاربر ساخته شد؛ ادامه با خطا متوقف شد.")
    elif made:
        await message.answer(f"✅ {made} کاربر ساخته شد.")

    if made:
        async with SessionLocal() as s:
            await owner_notify.notify_owner(
                s, f"➕ نمایندهٔ «{rname}» روی پنل {pname} تعداد {made} کاربر ساخت "
                   f"({gb} گیگ، {days} روز)."
            )
    async with SessionLocal() as s:
        await _reshow_menu(message, s, menu_user)


@router.callback_query(F.data == "menu:support")
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await cb.message.answer(
        "پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.cancel_keyboard()
    )
    await cb.answer()


@router.callback_query(F.data == "menu:invoices")
async def cb_invoices(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_invoices(cb.message.answer, cb.from_user.id, s)
        await _reshow_menu(cb.message, s, cb.from_user)
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
        await _reshow_menu(cb.message, s, cb.from_user)


@router.callback_query(F.data == "menu:pay")
async def cb_pay(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # tapping «پرداخت فاکتور» again resets any prior invoice selection
    async with SessionLocal() as s:
        await _send_pay(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: payable-invoice picker — a trailing menu would bury it.
    await cb.answer()


async def _load_pay_invoices(session, data: dict) -> list[Invoice]:
    """The Invoice rows chosen in the pay flow, from FSM data. Reads the new `pay_invoice_ids`
    list and falls back to the legacy single `pay_invoice_id` (for any in-flight state)."""
    ids = list(data.get("pay_invoice_ids") or [])
    if not ids and data.get("pay_invoice_id"):
        ids = [int(data["pay_invoice_id"])]
    invs: list[Invoice] = []
    for iid in ids:
        inv = await session.get(Invoice, int(iid))
        if inv is not None:
            invs.append(inv)
    return invs


async def _start_pay_flow(cb: CallbackQuery, state: FSMContext, invoices: list[Invoice]) -> None:
    """Show the payable total + tap-to-copy instructions for the chosen invoice(s) and lock the
    customer into PayState.waiting with the chosen ids — the TXID/receipt they send next is
    attributed to exactly this set."""
    from app.services import payment_methods

    total_toman = float(sum(float(i.amount_toman or 0) for i in invoices))
    total_usdt = float(sum(float(i.amount_usdt or 0) for i in invoices))
    n = len(invoices)
    async with SessionLocal() as s:
        opts = await payment_methods.load_options(s)
        amount_ton = None
        amount_avax = None
        if opts.ton or opts.avax:
            from app.services import rates
            if opts.ton:
                ton_rate = await rates.get_ton_toman(s)
                if ton_rate:
                    amount_ton = f"{total_toman / ton_rate:,.2f}"
            if opts.avax:
                avax_rate = await rates.get_avax_toman(s)
                if avax_rate:
                    amount_avax = f"{total_toman / avax_rate:,.4f}"
    if n == 1:
        header = f"💳 پرداخت فاکتور دوره {invoices[0].period_label}\n"
        footer = ("\n\nℹ️ این مبلغ فقط برای همین فاکتور است. پس از واریز، شناسهٔ تراکنش (TXID) یا "
                  "تصویر رسید را همین‌جا بفرستید (برای لغو: /cancel).")
    else:
        periods = "، ".join(i.period_label for i in invoices)
        header = f"💳 پرداخت {n} فاکتور (دوره‌های {periods})\n"
        footer = (f"\n\nℹ️ این مبلغ برای {n} فاکتورِ انتخاب‌شده است. پس از واریز، شناسهٔ تراکنش "
                  "(TXID) یا تصویر رسید را همین‌جا بفرستید (برای لغو: /cancel).")
    text = (
        header
        + f"مبلغ کل: {total_toman:,.0f} تومان\n\n"
        + payment_methods.instructions_text(
            opts, amount_usdt=f"{total_usdt:,.2f}",
            amount_toman=f"{total_toman:,.0f}", amount_ton=amount_ton,
            amount_avax=amount_avax, html=True)
        + footer
    )
    await state.set_state(PayState.waiting)
    await state.update_data(pay_invoice_ids=[i.id for i in invoices])
    await cb.message.answer(
        rtl(text), parse_mode="HTML",
        reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
    )


@router.callback_query(F.data.startswith("payinv:"))
async def cb_pay_invoice(cb: CallbackQuery, state: FSMContext) -> None:
    """Start paying ONE chosen invoice: show its amount + instructions and wait for the
    customer's TXID / receipt photo, which is then attributed to exactly this invoice."""
    # Already mid-payment → don't start another flow. The flow stays locked on the chosen
    # invoice(s) until they send a proof or /cancel; to switch they re-open «💳 پرداخت فاکتور».
    if await state.get_state() == PayState.waiting.state:
        await cb.answer(
            "شما در حال پرداخت هستید. رسید یا شناسهٔ تراکنش (TXID) را بفرستید، "
            "یا برای انتخاب دوباره ابتدا /cancel را بزنید.",
            show_alert=True,
        )
        return

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
        deferred = inv is not None and inv.deferred_until and inv.deferred_until > tehran_today()
        if inv is None or inv.reseller_id not in owned or inv.status not in _OWED or deferred:
            await cb.answer("این فاکتور در حال حاضر قابل پرداخت نیست.", show_alert=True)
            return
        # Already submitted a payment covering this invoice → don't let them pay again (one
        # pending per invoice); tell them it's under review.
        if await _pending_payment_for_invoice(s, inv.id) is not None:
            await cb.answer(
                "برای این فاکتور قبلاً رسید فرستاده‌اید و در انتظار تأیید است؛ لطفاً منتظر بمانید.",
                show_alert=True,
            )
            return
        await _start_pay_flow(cb, state, [inv])
    await cb.answer()


@router.callback_query(F.data == "payall")
async def cb_pay_all(cb: CallbackQuery, state: FSMContext) -> None:
    """Pay ALL the customer's payable invoices with one transfer. Gathers every owed, due-now
    invoice not already inside a pending payment and locks the customer into one combined pay
    flow whose proof settles the whole set."""
    if await state.get_state() == PayState.waiting.state:
        await cb.answer(
            "شما در حال پرداخت هستید. رسید یا شناسهٔ تراکنش (TXID) را بفرستید، "
            "یا برای انتخاب دوباره ابتدا /cancel را بزنید.",
            show_alert=True,
        )
        return
    async with SessionLocal() as s:
        resellers = await _resellers_for_chat(s, cb.from_user.id)
        if not resellers:
            await cb.answer("این فاکتورها در حال حاضر قابل پرداخت نیستند.", show_alert=True)
            return
        ids = [r.id for r in resellers]
        owed = (
            await s.execute(
                select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
                .order_by(Invoice.period_start.asc())
            )
        ).scalars().all()
        today = tehran_today()
        pending = await _pending_invoice_ids(s, ids)
        payable = [
            i for i in owed
            if not (i.deferred_until and i.deferred_until > today) and i.id not in pending
        ]
        if not payable:
            await cb.answer("بدهی قابل پرداختی ندارید.", show_alert=True)
            return
        await _start_pay_flow(cb, state, payable)
    await cb.answer()


@router.message(PayState.waiting, F.photo)
async def pay_state_photo(message: Message, state: FSMContext) -> None:
    from app.services import payment_methods

    async with SessionLocal() as s:
        opts = await payment_methods.load_options(s)
        # A photo is a valid proof ONLY when a photo-based method (card / receipt) is enabled.
        # If the owner turned those off (e.g. only USDT/TON TXID), a photo isn't accepted —
        # stay locked and ask for the right proof.
        if not (opts.card or opts.screenshot):
            await message.answer(
                "❌ پرداخت با «تصویر رسید» فعال نیست.\n"
                f"لطفاً {_proof_wanted_fa(opts)} را بفرستید.",
                reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
            )
            return
        data = await state.get_data()
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _track_user(s, message.from_user)
        await _handle_payment_proof(message, s, invoices=invs)
        await _reshow_menu(message, s, message.from_user)


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
        parsed = _parse_txid(text, usdt=opts.usdt, ton=opts.ton, avax=opts.avax)
        if parsed is None:
            # Invalid input → explain what's needed and STAY in the pay flow (no menu), with a
            # tappable exit so the user is never stuck guessing /cancel.
            await message.answer(
                rtl(
                    "❌ ورودی نامعتبر است.\n"
                    f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید."
                ),
                reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
            )
            return
        chain, txid = parsed
        if chain == "ambiguous":
            # A bare 0x hash with BOTH USDT(BSC) and AVAX enabled → ask which network. Hold the
            # hash in FSM and stay in PayState; the paychain:* buttons resolve it.
            await state.update_data(pay_txid=txid)
            await message.answer(
                rtl("این تراکنش روی کدام شبکه انجام شده است؟"),
                reply_markup=keyboards.pay_chain_keyboard(),
            )
            return
        data = await state.get_data()
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _handle_txid(message, s, txid, invoices=invs, chain=chain)
        await _reshow_menu(message, s, message.from_user)


@router.message(PayState.waiting)
async def pay_state_other(message: Message) -> None:
    """Any other content (document, sticker, …) while paying → keep them in the locked flow."""
    from app.services import payment_methods

    async with SessionLocal() as s:
        opts = await payment_methods.load_options(s)
    await message.answer(
        rtl(f"لطفاً {_proof_wanted_fa(opts)} را همین‌جا بفرستید."),
        reply_markup=keyboards.cancel_keyboard("✖️ انصراف از پرداخت"),
    )


@router.callback_query(F.data.startswith("paychain:"))
async def cb_pay_chain(cb: CallbackQuery, state: FSMContext) -> None:
    """Resolve the «which network?» ask for an ambiguous bare 0x hash: attribute the pending hash
    (held in FSM by `pay_state_text`) to the chosen chain and record it."""
    if await state.get_state() != PayState.waiting.state:
        await cb.answer()
        return
    chain = cb.data.split(":", 1)[1]
    if chain not in ("avax", "bsc"):
        await cb.answer()
        return
    data = await state.get_data()
    txid = (data.get("pay_txid") or "").strip()
    if not txid:
        await state.clear()
        await cb.answer("نشست منقضی شد؛ دوباره از «💳 پرداخت فاکتور» اقدام کنید.", show_alert=True)
        return
    if chain == "avax":
        txid = txid.lower()
    async with SessionLocal() as s:
        invs = await _load_pay_invoices(s, data)
        await state.clear()
        await _track_user(s, cb.from_user)
        await _handle_txid(cb.message, s, txid, invoices=invs, chain=chain, from_user=cb.from_user)
        await _reshow_menu(cb.message, s, cb.from_user)
    await cb.answer()


@router.callback_query(F.data == "menu:removelink")
async def cb_removelink(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await _send_removelink(cb.message.answer, cb.from_user.id, s)
        # No menu re-show: link picker (tap to remove) — a trailing menu would bury it.
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
    elif action == "health":
        from app.services import owner_report

        await answer(owner_report.render_health(await owner_report.health(session)))
    elif action == "payments":
        await _owner_pending_payments(answer, session)
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
        from app.services import delivery, invoicing
        from app.services import sync as sync_service
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
        if action == "search":
            await state.set_state(OwnerSearchState.waiting)
            await cb.message.answer(
                "🔎 نام یا شناسهٔ نماینده را بفرستید.",
                reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        await _owner_stats(cb.message.answer, s, label)
    await cb.answer()


@router.callback_query(F.data.startswith("opanel:"))
async def cb_owner_per_panel(cb: CallbackQuery) -> None:
    label = cb.data.split(":", 1)[1]
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
        pay = await s.get(Payment, pid)
        if pay is None or pay.status != PaymentStatus.pending:
            await cb.message.answer("این پرداخت دیگر در انتظار نیست (شاید قبلاً رسیدگی شده).")
            await cb.answer()
            return
        text = rtl(await _payment_review_html(s, pay))
        kb = keyboards.owner_payment_detail_keyboard(pay.id)
        # If there's a proof image, send it with the detail as caption; else just the text.
        proof = pay.proof_path
        if proof and os.path.exists(proof):
            from aiogram.types import FSInputFile

            await cb.message.answer_photo(
                FSInputFile(proof), caption=text, parse_mode="HTML", reply_markup=kb
            )
        else:
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb,
                                    disable_web_page_preview=True)
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
    base = msg.html_text or ""
    body = f"{base}\n\n{status_line}"
    kb = keyboards.owner_payment_decided_keyboard()
    try:
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    await answer(
        "\n".join(lines),
        reply_markup=keyboards.owner_reseller_card_keyboard(
            r.id, enforced=enforced, tg_href=tg_href
        ),
    )


@router.callback_query(F.data.startswith("oenf:"))
async def cb_owner_enforce(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
            await cb.answer("دسترسی ندارید.", show_alert=True)
            return
    await state.set_state(OwnerCapBumpState.waiting)
    await state.update_data(cap_rid=rid)
    await cb.message.answer(
        "مقدارِ افزایش را به‌صورتِ عدد بفرستید (۱ تا ۵۰۰۰).",
        reply_markup=keyboards.cancel_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("orcinv:"))
async def cb_owner_reseller_invoices(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":")[1])
    async with SessionLocal() as s:
        if not await _is_owner_user(s, cb.from_user):
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
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
            return  # owner-only; resellers don't see these commands
        await _dispatch_owner(action, message.answer, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    """Owner-only reseller search (same as the «🔎 جستجوی نماینده» menu action)."""
    async with SessionLocal() as s:
        if not await _is_owner_user(s, message.from_user):
            return
    await state.set_state(OwnerSearchState.waiting)
    await message.answer(
        "🔎 نام یا شناسهٔ نماینده را بفرستید.",
        reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
    )


async def _owner_stats(answer, session, label: str | None = None) -> None:
    """Period KPI dashboard with a month switch + a per-panel breakdown button."""
    from app.services import owner_report

    label = label or owner_report.current_period_label()
    stats = await owner_report.period_stats(session, label)
    await answer(
        owner_report.render_period_stats(stats),
        reply_markup=keyboards.owner_stats_keyboard(label, owner_report.period_choices()),
    )


async def _owner_debtors(answer, session) -> None:
    from app.services import owner_report

    rows = await owner_report.top_debtors(session, limit=10)
    if not rows:
        await answer("بدهکاری وجود ندارد.")
        return
    # Each row starts with a right-to-left mark (‏) so a line that begins with an
    # English reseller name still renders right-aligned in Telegram, and links to the card.
    lines = ["💰 بدهکاران برتر — برای کارت/اقدام روی «🔎 جستجوی نماینده» بزنید:\n"]
    for i, d in enumerate(rows, 1):
        lines.append(f"‏{i}. {_iso(d.name)}: {float(d.total):,.0f} تومان")
    await answer("\n".join(lines))


async def _owner_pending_payments(answer, session) -> None:
    """List PENDING payments as buttons → tap to see proof + confirm/reject."""
    rows = (
        await session.execute(
            select(Payment.id, Reseller.name, Payment.amount_toman,
                   Invoice.period_label, Invoice.amount_toman)
            .join(Reseller, Payment.reseller_id == Reseller.id)
            .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
            .where(Payment.status == PaymentStatus.pending)
            .order_by(Payment.created_at)
            .limit(30)
        )
    ).all()
    if not rows:
        await answer("✅ پرداختِ در انتظارِ تأییدی وجود ندارد.")
        return
    items: list[tuple[int, str]] = []
    for pid, name, toman, period, inv_toman in rows:
        # A TON/screenshot payment rarely stores its own amount; fall back to the invoice amount
        # so the list never shows a bare «—».
        shown = float(toman or 0) or float(inv_toman or 0)
        amt = f"{shown:,.0f}ت" if shown else "—"
        items.append((pid, f"#{payment_code(pid)} · {(name or '—')[:18]} · {amt} · {period or '—'}"))
    await answer(
        f"💳 {len(rows)} پرداختِ در انتظارِ تأیید:",
        reply_markup=keyboards.owner_pending_payments_keyboard(items),
    )


# --------------------------- shared reseller views ---------------------------
async def _pending_payment_for_invoice(session, invoice_id: int | None):
    """The PENDING payment whose invoice SET contains this invoice (if any) — used to block a
    duplicate submission so one invoice never sits in several pending payments. Delegates to the
    service helper so the bot and the web portal share identical rules."""
    from app.services import payments

    return await payments._pending_payment_for_invoice(session, invoice_id)


async def _pending_invoice_ids(session, reseller_ids: list[int]) -> set[int]:
    """Invoice ids that already belong to a PENDING payment's set (awaiting the owner's review) —
    so the bot shows «در انتظار تأیید» and blocks a duplicate submission. Expands each pending
    payment's invoice SET (a payment may cover several invoices)."""
    from app.services import payments

    if not reseller_ids:
        return set()
    held: set[int] = set()
    for p in await payments._pending_payments_for_resellers(session, set(reseller_ids)):
        held.update(payments._settled_ids(p))
    return held


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
        toman = f"{float(inv.amount_toman):,.0f} تومان"
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
    """«پرداخت فاکتور» — list each UNPAID, due-now invoice as its OWN button, plus a «پرداخت
    همهٔ بدهی» button (when 2+ are payable) that settles them all with one transfer. Tapping a
    button starts the locked pay flow for that invoice (payinv:<id>) or all of them (payall)."""
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
    today = tehran_today()
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
         f"💳 دوره {i.period_label} — {float(i.amount_toman):,.0f} تومان")
        for i in payable
    ]
    pay_all_label = None
    if len(payable) > 1:
        total_toman = float(sum(float(i.amount_toman or 0) for i in payable))
        pay_all_label = f"✅ پرداخت همهٔ بدهی ({len(payable)} فاکتور — {total_toman:,.0f} تومان)"
        msg = (
            "💳 می‌توانید همهٔ فاکتورها را یکجا پرداخت کنید (دکمهٔ بالا)، "
            "یا هر فاکتور را جداگانه با زدن روی آن:"
        )
    else:
        msg = "💳 کدام فاکتور را می‌خواهید پرداخت کنید؟ روی آن بزنید:"
    if in_review:
        msg += f"\n\n⏳ {len(in_review)} فاکتور دیگر در انتظار تأیید است (لازم نیست دوباره بفرستید)."
    if deferred:
        msg += f"\n\n📅 {len(deferred)} فاکتور مهلت‌دار فعلاً لازم نیست پرداخت شود."
    await answer(msg, reply_markup=keyboards.pay_invoices_keyboard(items, pay_all_label=pay_all_label))


async def _send_removelink(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer("لینکی برای حذف ندارید.")
        return
    items = [(r.id, _iso(f"{r.name} (…{r.admin_uuid[-6:]})")) for r in resellers]
    await answer("لینک‌های ثبت‌شدهٔ شما — برای حذف انتخاب کنید:",
                 reply_markup=keyboards.remove_links_keyboard(items))


async def _send_panels(answer, chat_id: int, session) -> None:
    """Show a reseller the list of panels they're registered on (with sub-counts + their own
    tap-to-copy panel link). The link is built from the panel's CURRENT host, so it auto-updates
    after a domain move."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    lines = ["🖥 پنل‌های شما:"]
    for r in resellers:
        panel = await session.get(Panel, r.panel_id)
        subs = (
            await session.execute(
                select(func.count(Reseller.id)).where(
                    Reseller.panel_id == r.panel_id,
                    Reseller.parent_admin_uuid == r.admin_uuid,
                )
            )
        ).scalar_one()
        tag = f" (#{r.link_tag})" if r.link_tag else ""
        link = panel.admin_link(r.admin_uuid, tag=r.link_tag)
        lines.append("")  # blank line between panels for readability
        lines.append(f"‏• پنل {_iso((panel.name or panel.key) + tag)} — زیرمجموعه‌ها: {subs}")
        lines.append(f"‏🔗 <code>{html.escape(link)}</code>")
    await answer(rtl("\n".join(lines)), parse_mode="HTML")


async def _send_portal_link(answer, chat_id: int, session) -> None:
    """Give the reseller a one-time link that opens the standalone web portal already logged in
    (no password). The link carries a short-lived signed token the site exchanges for a session."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    domain = (await settings_service.get(session, "server_domain", "") or "").strip()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        await answer(rtl("🌐 پنلِ تحتِ وب هنوز پیکربندی نشده است؛ لطفاً به پشتیبانی اطلاع دهید."))
        return
    from app.core.portal_auth import create_portal_login_token

    url = f"https://{domain}/portal/login?t={create_portal_login_token(chat_id)}"
    msg = (
        "🌐 ورود به پنلِ تحتِ وب\n\n"
        "برای دیدنِ فاکتورها، پرداخت، آمار و مدیریتِ زیرمجموعه‌ها در سایت، روی لینکِ زیر بزنید "
        "(این لینک تا ۱۵ دقیقه معتبر است):\n\n"
        f"<a href=\"{html.escape(url, quote=True)}\">باز کردنِ پنلِ من</a>\n\n"
        "یا این آدرس را کپی کنید:\n"
        f"<code>{html.escape(url)}</code>"
    )
    await answer(rtl(msg), parse_mode="HTML", disable_web_page_preview=True)


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
            items.append((r.id, f"پنل {_iso(panel.name or panel.key)} — {_iso(r.name)} ({subs})"))
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
    state = sub.enforcement_state.value
    status_txt = (
        "⛔️ مسدود" if state == "enforced"
        else "🚫 ساخت کاربر متوقف (کاربرانِ فعلی آنلاین)" if state == "frozen"
        else "🟢 فعال"
    )
    lines = [
        f"👤 زیرمجموعه: {rep['name']}",
        f"وضعیت: {status_txt}",
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
        reply_markup=keyboards.sub_detail_keyboard(sub.id, state, months, has_cap=cap > 0),
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

        async with SessionLocal() as session:
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
        await message.answer(f"❌ بازیابی ناموفق بود: {exc}")


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    """A cold photo (NOT inside the pay flow) is NOT a payment — it would create stray,
    unattributed payments. Guide the customer to the «پرداخت فاکتور» button instead. (Inside
    PayState, the receipt is handled by pay_state_photo.)"""
    async with SessionLocal() as session:
        await _track_user(session, message.from_user)
    await message.answer(
        "📸 برای ثبتِ پرداخت، اول روی دکمهٔ «💳 پرداخت فاکتور» (زیرِ فاکتور یا در منو) بزنید، "
        "سپس تصویرِ رسید را بفرستید."
    )


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
        # A cold TXID/link (NOT inside the pay flow) is NOT a payment — guide them to the button
        # instead of silently recording a stray payment. (Inside PayState, pay_state_text handles it.)
        if _parse_txid(text, usdt=opts.usdt, ton=opts.ton, avax=opts.avax):
            await message.answer(
                "برای ثبتِ پرداخت، اول روی دکمهٔ «💳 پرداخت فاکتور» (زیرِ فاکتور یا در منو) بزنید، "
                "سپس شناسهٔ تراکنش را بفرستید."
            )
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
    reseller = await _registration_candidate(session, parsed)
    # A registration identity is the normalized host + complete proxy path + UUID. Missing,
    # mismatched, or ambiguous identities are rejected rather than selecting an arbitrary row.
    if reseller is None:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Must be a real, non-owner reseller that came from one of the registered panels.
    if reseller.is_owner:
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
    # Now a registered reseller → keep the menu at hand.
    await _reshow_menu(message, session, message.from_user)


async def _registration_candidate(session, parsed) -> Reseller | None:
    """Return the unique reseller matching normalized host + proxy path + UUID."""
    expected_host = normalize_host(parsed.host)
    expected_path = normalize_path(parsed.path)
    if not expected_host or not expected_path:
        return None
    candidates = (
        await session.execute(
            select(Reseller).where(func.lower(Reseller.admin_uuid) == parsed.uuid.lower())
        )
    ).scalars().all()
    matches: list[Reseller] = []
    for candidate in candidates:
        panel = await session.get(Panel, candidate.panel_id)
        if panel is None:
            continue
        # The link's host may be the panel's CURRENT host or one of its old/alternate hosts
        # (after a domain move). Path + UUID identity is otherwise unchanged.
        host_ok = expected_host == normalize_host(panel.host) or expected_host in {
            normalize_host(a) for a in panel.host_alias_list
        }
        if host_ok and normalize_path(panel.proxy_path) == expected_path:
            matches.append(candidate)
    # Fail-closed: a unique match or nothing. Never guess / never take the first of several.
    return matches[0] if len(matches) == 1 else None


async def _invoice_amount_for_chain(session, invoice, chain: str) -> str:
    """The invoice amount in Toman plus its equivalent in the PAID currency: TON for a TON
    payment, USDT otherwise — so a TON payment's owner message doesn't show a USDT figure."""
    if invoice is None:
        return "نامشخص"
    toman = f"{float(invoice.amount_toman):,.0f} تومان"
    if chain == "ton":
        from app.services import rates
        rate = await rates.get_ton_toman(session)
        if rate:
            return f"{toman} (≈ {float(invoice.amount_toman) / rate:,.2f} GRAM)"
        return toman
    if chain == "avax":
        from app.services import rates
        rate = await rates.get_avax_toman(session)
        if rate:
            return f"{toman} (≈ {float(invoice.amount_toman) / rate:,.4f} AVAX)"
        return toman
    return f"{toman} ({float(invoice.amount_usdt):,.2f} USDT)"


_PAYMENT_METHOD_FA = {
    "usdt_txid": "تتر (USDT)", "ton_txid": "گرام (GRAM)", "avax_txid": "اوالانچ (AVAX)",
    "screenshot": "رسید تصویری", "manual": "دستی",
}


def _explorer_link(chain: str, txid: str) -> str:
    """An HTML link to the matching block explorer so the owner can open the tx in one tap."""
    if chain == "ton":
        return f"<a href='https://tonscan.org/tx/{txid}'>مشاهده در tonscan</a>"
    if chain == "avax":
        return f"<a href='https://snowtrace.io/tx/{txid}'>مشاهده در snowtrace</a>"
    return f"<a href='https://bscscan.com/tx/{txid}'>مشاهده در bscscan</a>"


def _network_status_fa(chk: dict) -> str:
    """One-line on-chain status from a `deposit_check` result, for any chain (TON/AVAX/USDT)."""
    if not chk.get("available"):
        return "⚪️ از زنجیره خوانده نشد — تراکنش را از روی لینک بررسی کنید."
    if chk.get("kind") == "ton":
        recv = f"{chk['received_ton']} GRAM ≈ {chk['received_toman']:,.0f} تومان"
        tol = f"±{chk['tolerance_pct']:.0f}٪"
    elif chk.get("kind") == "avax":
        recv = f"{chk['received_avax']} AVAX ≈ {chk['received_toman']:,.0f} تومان"
        conf = chk.get("confirmations")
        if conf is not None:
            recv += f" ({conf} تأیید)"
        tol = f"±{chk['tolerance_pct']:.0f}٪"
    else:  # usdt
        recv = f"{chk['received_usdt']:,.2f} USDT"
        conf = chk.get("confirmations")
        if conf is not None:
            recv += f" ({conf} تأیید)"
        tol = f"±{chk['tolerance_usdt']:g} USDT"
    if chk.get("match") is True:
        return f"✅ واریزی یافت شد: {recv} — مطابق فاکتور ({tol})"
    if chk.get("match") is False:
        return f"⚠️ واریزی یافت شد: {recv} — مغایر با فاکتور (خارج از {tol})"
    return f"🟢 واریزی یافت شد: {recv}"


async def _payment_review_html(session, pay, *, reseller=None, inv=None, invs=None) -> str:
    """Rich, owner-facing HTML summary of a pending payment — shared by the submit notification
    and the «پرداخت‌های در انتظار» detail view so both are complete and identical. Includes the
    tracking number, a CLICKABLE reseller name (opens their Telegram profile), method, EVERY
    invoice the payment covers (a payment may settle several) with its paid-currency equivalent
    plus a grand total, a clickable explorer link, and — for TON — a best-effort on-chain deposit
    read (actual received ≈ Toman, matched against the total) so the owner can decide."""
    from app.services import payments as _payments

    if reseller is None:
        reseller = await session.get(Reseller, pay.reseller_id)
    # The full set this payment covers. Prefer an explicitly passed list; else load from the
    # payment's stored set (settled_invoice_ids → invoice_id fallback).
    invoices = list(invs) if invs else ([inv] if inv else [])
    if not invoices:
        set_ids = _payments._settled_ids(pay)
        if set_ids:
            invoices = list(
                (await session.execute(select(Invoice).where(Invoice.id.in_(set_ids))))
                .scalars().all()
            )
    chain = pay.chain or ("ton" if pay.method == PaymentMethod.ton_txid else "bsc")
    name_link = owner_notify.user_link(reseller) if reseller else "—"
    lines = [
        f"💳 پرداخت — شمارهٔ پیگیری #{payment_code(pay.id)}",
        f"👤 نماینده: {name_link}",
        f"📤 روش: {_PAYMENT_METHOD_FA.get(pay.method.value, pay.method.value)}",
    ]
    if not invoices:
        lines.append("🧾 فاکتور: —")
    elif len(invoices) == 1:
        only = invoices[0]
        lines.append(f"🧾 فاکتور: دورهٔ {only.period_label}")
        lines.append(f"💰 مبلغ فاکتور: {await _invoice_amount_for_chain(session, only, chain)}")
    else:
        lines.append(f"🧾 فاکتورها ({len(invoices)}):")
        for one in invoices:
            amt = await _invoice_amount_for_chain(session, one, chain)
            lines.append(_iso(f"• دورهٔ {one.period_label}: {amt}"))
        total_toman = float(sum(float(i.amount_toman or 0) for i in invoices))
        lines.append(f"💰 مبلغ کل: {total_toman:,.0f} تومان")
    if pay.txid:
        lines.append(f"🔗 تراکنش: {_explorer_link(chain, pay.txid)}")
        lines.append(_iso(f"TXID: {pay.txid}"))
        # Best-effort on-chain read (free): TON via toncenter, USDT via a public BSC RPC node.
        chk = await _payments.deposit_check(session, pay)
        lines.append(f"🔍 وضعیت شبکه: {_network_status_fa(chk)}")
    return "\n".join(lines)


async def _handle_txid(
    message: Message,
    session,
    txid: str,
    *,
    invoices: list[Invoice] | None,
    chain: str = "bsc",
    from_user=None,  # noqa: ANN001 — when called from a callback, cb.message.from_user is the bot
) -> None:
    """Record a submitted tx hash (USDT/BSC, TON, or AVAX) as a PENDING payment for MANUAL review —
    no on-chain auto-verify. The owner opens the clickable explorer link in the panel and
    confirms/rejects. A payment may cover several invoices (one transfer for several debts)."""
    from app.services import payments

    actor = from_user or message.from_user
    resellers = await _resellers_for_chat(session, actor.id)
    if not resellers:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Shared validation + creation (identical rules on the bot and the web portal).
    result = await payments.submit_reseller_payment(
        session, reseller_ids={r.id for r in resellers},
        invoice_ids=[i.id for i in (invoices or [])], txid=txid, chain=chain,
    )
    await message.answer(rtl(result.user_message))
    if result.notify and result.payment is not None:
        review = await _payment_review_html(
            session, result.payment, reseller=resellers[0], invs=result.invoices)
        await owner_notify.notify_owner(
            session, result.owner_intro + "\n\n" + review,
            html=True, reply_markup=keyboards.owner_payment_detail_keyboard(result.payment.id))


async def _handle_payment_proof(
    message: Message, session, *, invoices: list[Invoice] | None
) -> None:
    """A reseller sent a deposit screenshot as proof of payment. Store it, link it to the
    exact invoice(s) chosen in «پرداخت فاکتور» as a PENDING payment, and forward it to the
    owner for manual confirmation. A payment may cover several invoices."""
    from app.services import payments

    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        # Not a registered reseller → can't attribute the payment.
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Shared validation + creation (identical rules on the bot and the web portal).
    result = await payments.submit_reseller_payment(
        session, reseller_ids={r.id for r in resellers},
        invoice_ids=[i.id for i in (invoices or [])], screenshot=True,
    )
    if result.status != "ok" or result.payment is None:
        await message.answer(rtl(result.user_message))
        return
    payment = result.payment
    result_invoices = result.invoices

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

    await message.answer(rtl(result.user_message))

    # Forward the screenshot to the owner so they can confirm from Telegram + the panel.
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()
    if owner_chat:
        review = await _payment_review_html(
            session, payment, reseller=resellers[0], invs=result_invoices)
        caption = rtl("🧾 رسید پرداخت جدید — منتظر تأیید شماست.\n\n" + review)
        kb = keyboards.owner_payment_detail_keyboard(payment.id)
        try:
            await message.bot.send_photo(
                int(owner_chat), photo.file_id, caption=caption,
                parse_mode="HTML", reply_markup=kb,
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to forward payment proof to owner", exc_info=True)
            if not saved:
                await message.bot.send_message(
                    int(owner_chat),
                    rtl(review + "\n\n(ارسال تصویرِ رسید ناموفق بود؛ آن را در پنل ببینید.)"),
                    parse_mode="HTML", reply_markup=kb,
                )
