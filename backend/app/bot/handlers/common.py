"""Reseller + owner bot handlers: membership gate, menus, registration, payment.

Shared core of the handlers package: the single ``router`` (with its router-level
private-chat filter and the two membership-gate outer middlewares), the FSM States,
module-level constants/regexes, and the cross-module helper seams. Domain modules
import ``common`` and register their handlers on ``common.router``; the package
``__init__`` imports them in the original monolithic file's order, which IS the
aiogram registration (= dispatch) order.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from aiogram import Bot, F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot import keyboards, texts
from app.core.db import SessionLocal
from app.models import BotUser, Reseller
from app.models.enums import InvoiceStatus
from app.services import settings_service


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


@router.message.outer_middleware
async def _redock_menu_after_flow(handler, event, data):
    """Every flow entry docks the cancel-only `flow_cancel_kb` (hiding the menu). This wraps every
    message handler and, when a flow just ENDED (state went active → None during this message),
    re-docks the role menu — so ANY exit (success or the many early-return paths) restores the menu
    uniformly, without each handler having to remember to. A flow that stays active (invalid input
    kept in-flow) is untouched. Callback-based exits (the inline «انصراف») re-dock themselves."""
    state = data.get("state")
    before = await state.get_state() if state is not None else None
    result = await handler(event, data)
    try:
        # `/start`, `/menu` and the cancel handlers rebuild the menu themselves → skip to avoid
        # doubling it.
        text = (getattr(event, "text", "") or "").strip()
        first = (text.split(maxsplit=1) or [""])[0].lower()
        if (first not in ("/start", "/menu", "/cancel") and text != keyboards.CANCEL_LABEL
                and before is not None and state is not None
                and await state.get_state() is None):
            user = getattr(event, "from_user", None)
            if user is not None:
                async with SessionLocal() as s:
                    await _reshow_menu(event, s, user)
    except Exception:  # noqa: BLE001 — a menu re-show must never break the handler it follows
        log.warning("re-dock after flow failed", exc_info=True)
    return result


def _cb_reshows_own_view(cb_data: str) -> bool:
    """A callback that ENDS a flow but INTENTIONALLY shows its own view (a picker/list) instead of the
    menu — the middleware must not bury it. `cancel` re-docks the menu itself; the legacy inline
    `menu:*` buttons navigate (e.g. `menu:pay` clears the pay state and shows the invoice picker with
    no trailing menu). Everything else that ends a flow is a genuine completion → re-dock."""
    return cb_data == "cancel" or cb_data.startswith("menu:")


@router.callback_query.outer_middleware
async def _redock_menu_after_cb_flow(handler, event, data):
    """Companion to `_redock_menu_after_flow` for flows that COMPLETE via a CALLBACK — the GB-cap /
    capacity preset buttons (`setcap:`/`capok:`), the shop-setup confirms, `cucancel`, etc. Those clear
    the FSM state inside a callback, which the message-only middleware never sees, so the cancel-only
    keyboard used to linger. Re-dock the role menu whenever a callback takes the flow state active →
    None, except for callbacks that restore/replace the view themselves (see `_cb_reshows_own_view`)."""
    state = data.get("state")
    before = await state.get_state() if state is not None else None
    result = await handler(event, data)
    try:
        cb_data = getattr(event, "data", "") or ""
        if (not _cb_reshows_own_view(cb_data) and before is not None and state is not None
                and await state.get_state() is None):
            user = getattr(event, "from_user", None)
            msg = getattr(event, "message", None)
            if user is not None and msg is not None:
                async with SessionLocal() as s:
                    await _reshow_menu(msg, s, user)
    except Exception:  # noqa: BLE001 — a menu re-show must never break the handler
        log.warning("re-dock after callback flow failed", exc_info=True)
    return result


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


async def portal_login_url(session, chat_id: int, *, next_path: str | None = None) -> str | None:
    """Mint a short-lived one-time portal login URL for `chat_id`, or None if the site domain isn't
    configured. `next_path` (a portal deep-link) is appended ONLY when it passes the strict
    `portal_deeplink.validate_next` allowlist; an invalid/foreign path is silently dropped (never
    appended raw) so a stale notification link can't become an open redirect. The server still
    authorizes the destination before the SPA navigates. Single source for every login URL."""
    domain = (await settings_service.get(session, "server_domain", "") or "").strip()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        return None

    from urllib.parse import quote

    from app.core.portal_auth import create_portal_login_token
    from app.core.portal_deeplink import validate_next

    url = f"https://{domain}/portal/login?t={create_portal_login_token(chat_id)}"
    if next_path is not None:
        validated = validate_next(next_path)
        if validated is not None:
            url += "&next=" + quote(validated, safe="")
    return url


async def portal_stable_url(
    session, chat_id: int, *, next_path: str | None = None, with_login_token: bool = False
) -> str | None:
    """The PERMANENT portal address for this reseller: `https://<domain>/portal/u/<admin_uuid>`.

    Unlike `portal_login_url` this never expires and is never consumed, so a link sent hours ago (a
    menu message, a support notification) still works — the old 15-minute one-time links were the
    "منقضی شده" complaint. It is safe to be permanent because the uuid is only an ADDRESS: opening it
    grants nothing by itself. The page lets you in only if the browser already holds a valid session
    for that reseller, otherwise it requires proving the owning Telegram account (see
    `POST /api/portal/auth/telegram`). Returns None if the domain isn't configured or the chat has
    no reseller row.

    `with_login_token=True` additionally attaches a fresh one-time `t=` — used for links WE send
    from the bot, where the recipient's identity is already proven. It makes the link sign them in
    instantly even in a browser that has never seen the portal, and if it later goes stale the page
    simply falls back to the session / Telegram button instead of failing. Without it (the address we
    tell them to bookmark) the URL carries no credential at all.
    """
    from urllib.parse import quote

    from app.core.portal_auth import create_portal_login_token
    from app.core.portal_deeplink import validate_next

    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        return None
    uid = next((r.admin_uuid for r in resellers if r.admin_uuid), None)
    if not uid:
        return None
    domain = (await settings_service.get(session, "server_domain", "") or "").strip()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        return None
    url = f"https://{domain}/portal/u/{quote(str(uid), safe='')}"
    query: list[str] = []
    if with_login_token:
        query.append("t=" + create_portal_login_token(chat_id))
    if next_path is not None:
        validated = validate_next(next_path)
        if validated is not None:
            query.append("next=" + quote(validated, safe=""))
    return url + ("?" + "&".join(query) if query else "")


async def _portal_menu_url(session, chat_id: int) -> str | None:
    """The reseller's permanent portal link (registered resellers + configured domain only)."""
    return await portal_stable_url(session, chat_id)


def _iso(value) -> str:
    """Wrap a value in a Unicode First-Strong Isolate (U+2068 … U+2069) so it renders cleanly
    inside a mixed Persian/English Telegram line: the segment keeps its own auto-detected
    direction and does NOT reorder the surrounding RTL text. Use around panel keys, reseller
    names, link tags, uuids — anything that may be English/Latin and sits inside an RTL line."""
    return f"⁨{value}⁩"


def iso_html(value) -> str:
    """Like `_iso`, but HTML-ESCAPES the value first. Use for panel/user-sourced strings that
    land in a `parse_mode="HTML"` message — a reseller name / panel key / link tag containing
    `<`, `>` or `&` would otherwise break Telegram's entity parsing and the whole message would
    fail to send (the flow appears dead)."""
    import html as _html

    return _iso(_html.escape(str(value)))


def _safe_int(data: str | None, idx: int = 1, sep: str = ":") -> int | None:
    """Parse `int(data.split(sep)[idx])` from callback data, returning None instead of raising
    on forged/malformed data (callback data is client-controllable). Callers answer with a
    graceful notice instead of crashing the handler."""
    if not data:
        return None
    parts = data.split(sep)
    if idx >= len(parts):
        return None
    try:
        return int(parts[idx])
    except (TypeError, ValueError):
        return None


async def clear_stale_flow(state) -> None:  # noqa: ANN001 — FSMContext
    """Clear any in-progress FSM flow (e.g. PayState's chosen invoice ids) when the user starts
    a NEW, unrelated interaction. Without this, a stale `pay_invoice_ids` selection leaks: a
    txid/receipt the user sends later attaches to the OLD invoice set. Call at every non-flow
    entry point (terminal slash commands, menu/invoice-view callbacks). MUST NOT be called from
    flow-continuation handlers (the pay selection/network callbacks, the SF.* setup wizard,
    support/broadcast/owner-reply text handlers, capacity/cap pickers) — they need the state.
    No-op when there is no active state."""
    if await state.get_state() is not None:
        await state.clear()


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
    try:
        await session.commit()
    except IntegrityError:
        # aiogram 3 processes updates concurrently and MemoryStorage has no per-user lock,
        # so two near-simultaneous first messages from the same user can both insert the
        # unique telegram_id. The loser's row is already tracked — swallow it.
        await session.rollback()


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
    """Show the role-and-state-aware lean menu: a persistent reply keyboard (≤5) + a one-tap inline
    portal button. Owner → owner menu; a registered reseller → the reseller menu; a first-time user
    with NO registered panel → «ثبت پنل» front-and-center."""
    if await _is_owner_user(session, user):
        await answer("👑 پنل مدیریت — یک گزینه را انتخاب کنید:",
                     reply_markup=keyboards.owner_main_reply_kb())
        return
    # A non-owner must pass the membership gate. If `bot` is available we re-check here too, so a
    # stray message from a non-member shows the JOIN prompt instead of leaking the reseller menu.
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
    # First-timer with no registered panel → register-first menu (not buried in «بیشتر»).
    if not await _resellers_for_chat(session, user.id):
        await answer(
            welcome + "\n\nبرای شروع، لینکِ پنلِ خود را همین‌جا بفرستید (یا دکمهٔ «🔗 ثبت پنل»).",
            reply_markup=keyboards.first_timer_reply_kb(),
        )
        return
    can_create = await _can_create_users(session, user.id)
    # The portal is a normal «🌐 پنل تحت وب» menu button now (it used to ride its own inline message
    # above the menu, which looked bolted-on). Tapping it replies with the permanent link.
    await answer(welcome + "\n\nیک گزینه را انتخاب کنید:",
                 reply_markup=keyboards.reseller_main_reply_kb(show_create_user=can_create))


async def _reshow_menu(message: Message, session, user) -> None:  # noqa: ANN001
    """Re-dock the role-aware main reply keyboard after a completed action. Compact (no portal
    button — that was shown at /start). Call only at the END of an action/flow, never mid-flow."""
    try:
        if await _is_owner_user(session, user):
            await message.answer("📋 منوی مدیریت:", reply_markup=keyboards.owner_main_reply_kb())
            return
        if not await _resellers_for_chat(session, user.id):
            await message.answer("📋 منو:", reply_markup=keyboards.first_timer_reply_kb())
            return
        can_create = await _can_create_users(session, user.id)
        await message.answer("📋 منوی اصلی:",
                             reply_markup=keyboards.reseller_main_reply_kb(show_create_user=can_create))
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

# NOTE: there used to be _RESELLER_TERMINAL/_OWNER_TERMINAL sets that appended a «📋 منوی اصلی:»
# message after every read-only action. That made sense when the menu was INLINE in the chat and
# scrolled away. The menu is a docked reply keyboard now — always visible — so the trailing message
# was pure clutter after every single tap. Flows still get their menu back via the re-dock
# middleware, which is the only case where the keyboard actually changed.

_SETCHAT_RE = re.compile(r"^(channel|group|کانال|گروه)\s+(-?\d{5,})$", re.IGNORECASE)

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
