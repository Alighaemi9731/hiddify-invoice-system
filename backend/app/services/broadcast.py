"""
Targeted broadcast to bot-registered resellers.

The audience always starts from ONE base set — the top-level resellers shown in the
«نمایندگان» main list that are NOT exempt from billing and are present on an active panel
(`reseller_stats.load_billable_roots`). Every filter only narrows that set down; it never
sends to sub-resellers, billing-exempt resellers, or removed admins.

Sending is standard-broadcast style: the request resolves the recipients fast and returns; the
actual send runs in the BACKGROUND with bounded concurrency + a global rate limit, and a summary
is pushed to the owner's Telegram at the end. Nothing is written to the database — only a tiny
in-memory snapshot of the current run (for the panel's live progress) + the server log.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html as _html
import logging
from collections.abc import Callable
from typing import Any, TypedDict

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import EndUserSnapshot, Invoice, Panel, Payment, Reseller
from app.models.enums import EnforcementState, InvoiceStatus, PaymentStatus
from app.services import owner_notify, reseller_stats
from app.services.periods import today as tehran_today

log = logging.getLogger("broadcast")

_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)

# Standard-broadcast send model. Telegram allows ~30 msgs/sec to DIFFERENT users; 25 leaves a
# safety margin. Concurrency is bounded so slow sends can't pile up unboundedly; the rate limiter
# is the real throttle. A send that keeps hitting 429 is retried up to this many times.
BROADCAST_RATE_PER_SEC = 25
BROADCAST_CONCURRENCY = 20
BROADCAST_MAX_RETRY = 3


class _RateLimiter:
    """Global send-rate cap: spaces acquisitions at least 1/rate apart (token-bucket-ish). The
    clock/sleep are injectable so it's deterministically testable without real time."""

    def __init__(self, rate_per_sec: float, *, clock=None, sleep=None) -> None:
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._clock = clock or (lambda: asyncio.get_event_loop().time())
        self._sleep = sleep or asyncio.sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._next_at > now:
                await self._sleep(self._next_at - now)
                now = self._clock()
            self._next_at = max(now, self._next_at) + self._min_interval


def rate_limiter() -> _RateLimiter:
    """A fresh limiter at the standard broadcast rate — for the OTHER fan-out senders
    (storefront notices/broadcast) so every bulk Telegram send shares one policy."""
    return _RateLimiter(BROADCAST_RATE_PER_SEC)


async def send_with_flood_control(
    bot: Any,
    chat_id: int,
    text: str,
    limiter: _RateLimiter,
    *,
    reply_markup: Any = None,
    parse_mode: str | None = None,
    max_retry: int = BROADCAST_MAX_RETRY,
) -> str:
    """Send ONE message under the shared flood-control policy and classify the outcome:
    "sent" — delivered; "blocked" — the user blocked the bot (permanent — callers must not
    retry); "failed" — transient/unknown (429 past the retry budget, network, dead bot —
    callers may retry on a later run). Every attempt first acquires the global rate
    limiter; a 429 sleeps its `retry_after` and retries the SAME recipient."""
    kwargs: dict[str, Any] = {"parse_mode": parse_mode}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    for _attempt in range(max_retry):
        await limiter.acquire()
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return "sent"
        except TelegramRetryAfter as e:               # 429 → wait then retry the same recipient
            await asyncio.sleep(e.retry_after)
            continue
        except TelegramForbiddenError:                # user blocked the bot → no retry
            return "blocked"
        except Exception:  # noqa: BLE001
            return "failed"
    return "failed"                                   # kept hitting 429 past the retry budget


# In-memory snapshot of the LAST/current run only (no DB, no per-recipient persistence) so the
# panel can show live progress while the background send runs.
_status: dict[str, Any] = {
    "running": False, "total": 0, "sent": 0, "blocked": 0, "failed": 0,
    "unregistered": 0, "started_at": None, "finished_at": None, "duration_s": None,
}


def current_status() -> dict[str, Any]:
    return dict(_status)

# Audience filters (each applied ON TOP of the base set). `panel_id` is an independent,
# combinable restriction (one panel, or all panels when None). Debt-lifecycle filters:
#   debtors         — ANY owed invoice (sent/overdue/enforced)
#   overdue         — an invoice dunning already marked overdue/enforced AND due now
#                     (no future payment deadline shields it)
#   deferred        — an owed invoice with a payment deadline still in the FUTURE
#                     (the owner granted a مهلت that hasn't arrived yet)
#   pending_payment — a payment awaiting the owner's confirm (در انتظارِ تأیید)
#   paid_up         — NO owed invoices at all (خوش‌حساب — thanks/promos)
# Access-state filters: enforced (معلق), frozen (توقفِ ساختِ کاربر).
# Sales/activity: zero_sale, few_active(<N), invoice_below(<X), invoice_above(≥X),
# new_resellers (created within N days).
AUDIENCES = (
    "all", "debtors", "overdue", "deferred", "pending_payment", "paid_up",
    "enforced", "frozen",
    "zero_sale", "few_active", "invoice_below", "invoice_above", "new_resellers",
)


class Recipient(TypedDict):
    reseller_id: int
    name: str
    panel: str
    chat_id: int | None
    status: str  # pending | sent | blocked | failed | unregistered


class BroadcastResult(TypedDict):
    audience: str
    panel_id: int | None
    threshold: float | None
    matched: int        # resellers matching the filter (reachable + unregistered)
    total: int          # reachable recipients (registered in the bot)
    unregistered: int   # matched the filter but never started the bot → cannot be reached
    sent: int
    blocked: int
    failed: int
    recipients: list[Recipient]  # reachable recipients with their final status
    skipped: list[Recipient]     # matched but unregistered (chat_id is None)


async def _active_counts(session: AsyncSession) -> dict[tuple[int, str], int]:
    """ACTIVE end-users each admin created, keyed by (panel_id, admin_uuid) — the same metric the
    Resellers tab's capacity column shows (this admin's own users, enabled AND is_active)."""
    rows = await session.execute(
        select(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid, func.count())
        .where(
            EndUserSnapshot.added_by_uuid.is_not(None),
            EndUserSnapshot.enable.is_(True),
            EndUserSnapshot.is_active.is_(True),
        )
        .group_by(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid)
    )
    return {(pid, uuid): int(n) for pid, uuid, n in rows.all()}


async def _bundle_amounts(
    session: AsyncSession, panel_id: int | None
) -> dict[int, tuple[float, float]]:
    """Current-month (so far) {root_id: (billable_gb, amount_toman)} for every billable bundle —
    the same numbers the invoice would carry. Used by the zero-sale and invoice-below filters."""
    from app.services import invoicing
    from app.services.periods import current_month

    out: dict[int, tuple[float, float]] = {}
    for _panel, b in await invoicing.preview_bundles(session, current_month(), panel_id=panel_id):
        base = round(b.total_gb * b.price_per_gb)
        amount = float(b.min_sale_toman) if (0 < base < (b.min_sale_toman or 0)) else float(base)
        out[b.root.id] = (b.total_gb, amount)
    return out


async def _owed_ids(session: AsyncSession) -> set[int]:
    """reseller_ids with at least one owed (sent/overdue/enforced) invoice."""
    return set((await session.execute(
        select(Invoice.reseller_id).where(Invoice.status.in_(_OWED)).distinct()
    )).scalars().all())


async def _matching_roots(
    session: AsyncSession, audience: str, panel_id: int | None, threshold: float | None
) -> list[Reseller]:
    """The base set narrowed by the chosen filter (and the optional single-panel restriction)."""
    roots = await reseller_stats.load_billable_roots(session, panel_id)
    today = tehran_today()
    if audience == "debtors":
        owed = await _owed_ids(session)
        return [r for r in roots if r.id in owed]
    if audience == "paid_up":
        owed = await _owed_ids(session)
        return [r for r in roots if r.id not in owed]
    if audience == "overdue":
        # Dunning already flagged it overdue/enforced AND it's due NOW — a payment deadline set
        # in the future shields the invoice (the whole cycle restarts from that deadline).
        rows = (await session.execute(
            select(Invoice.reseller_id, Invoice.deferred_until).where(
                Invoice.status.in_((InvoiceStatus.overdue, InvoiceStatus.enforced)))
        )).all()
        ids = {rid for rid, deadline in rows if deadline is None or deadline <= today}
        return [r for r in roots if r.id in ids]
    if audience == "deferred":
        # Owed with a payment deadline still in the FUTURE («هنوز به مهلتشان نرسیده‌ایم»).
        ids = set((await session.execute(
            select(Invoice.reseller_id).where(
                Invoice.status.in_(_OWED), Invoice.deferred_until > today).distinct()
        )).scalars().all())
        return [r for r in roots if r.id in ids]
    if audience == "pending_payment":
        ids = set((await session.execute(
            select(Payment.reseller_id).where(Payment.status == PaymentStatus.pending).distinct()
        )).scalars().all())
        return [r for r in roots if r.id in ids]
    if audience == "enforced":
        return [r for r in roots if r.enforcement_state == EnforcementState.enforced]
    if audience == "frozen":
        return [r for r in roots if r.enforcement_state == EnforcementState.frozen]
    if audience == "new_resellers":
        days = int(threshold) if threshold is not None else 30
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

        def _created(r: Reseller) -> dt.datetime | None:
            c = r.created_at
            return c.replace(tzinfo=dt.timezone.utc) if (c is not None and c.tzinfo is None) else c

        # Unknown creation date ≠ new — exclude rather than guess.
        return [r for r in roots
                if (created := _created(r)) is not None and created >= cutoff]
    if audience == "few_active":
        limit = int(threshold or 0)
        active = await _active_counts(session)
        return [r for r in roots if active.get((r.panel_id, r.admin_uuid), 0) < limit]
    if audience in ("zero_sale", "invoice_below", "invoice_above"):
        amounts = await _bundle_amounts(session, panel_id)
        if audience == "zero_sale":
            return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[0] <= 0]
        toman = float(threshold or 0)
        if audience == "invoice_above":
            return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[1] >= toman]
        return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[1] < toman]
    return roots  # "all"


async def resolve_recipients(
    session: AsyncSession, audience: str, panel_id: int | None, threshold: float | None
) -> tuple[list[Recipient], list[Recipient]]:
    """(reachable, unregistered): the matched roots split by whether they can be messaged.
    Reachable recipients are de-duplicated by chat_id (one person on two panels → one message)."""
    roots = await _matching_roots(session, audience, panel_id, threshold)
    panel_keys = {p.id: p.key for p in (await session.execute(select(Panel))).scalars().all()}
    reachable: dict[int, Recipient] = {}
    unregistered: list[Recipient] = []
    for r in roots:
        rec = Recipient(
            reseller_id=r.id, name=r.name or "—",
            panel=panel_keys.get(r.panel_id, "?"), chat_id=r.bot_chat_id, status="pending",
        )
        if r.bot_chat_id:
            reachable.setdefault(r.bot_chat_id, rec)
        else:
            rec["status"] = "unregistered"
            unregistered.append(rec)
    return list(reachable.values()), unregistered


def _empty(audience: str, panel_id: int | None, threshold: float | None) -> BroadcastResult:
    return BroadcastResult(
        audience=audience, panel_id=panel_id, threshold=threshold,
        matched=0, total=0, unregistered=0, sent=0, blocked=0, failed=0,
        recipients=[], skipped=[],
    )


async def preview(
    session: AsyncSession, *, audience: str = "all",
    panel_id: int | None = None, threshold: float | None = None,
) -> BroadcastResult:
    """Resolve the recipient list WITHOUT sending, so the owner can verify the filter first."""
    reachable, unregistered = await resolve_recipients(session, audience, panel_id, threshold)
    res = _empty(audience, panel_id, threshold)
    res["recipients"] = reachable
    res["skipped"] = unregistered
    res["total"] = len(reachable)
    res["unregistered"] = len(unregistered)
    res["matched"] = len(reachable) + len(unregistered)
    return res


def migration_text(panel: Panel, previous_host: str, admin_uuid: str, tag: str | None) -> str:
    """Per-reseller «new panel address» message (HTML, bidi-safe via rtl). Shows the reseller's own
    admin link on the panel's CURRENT host (recommended) and on the chosen previous host."""
    from app.bot.rtl import rtl

    new_link = panel.admin_link(admin_uuid, tag=tag)
    prev_link = panel.admin_link(admin_uuid, tag=tag, host=previous_host)
    # Each Persian label sits on its OWN line and each (English) URL alone on the NEXT line inside
    # <code> — so no line mixes Persian + Latin and the bidi layout stays clean (rtl() also
    # isolates runs). Keep it short and unambiguous.
    body = (
        "🔔 آدرسِ جدیدِ پنلِ شما\n"
        "—\n"
        "نمایندهٔ گرامی، دامنهٔ پنل عوض شده است. اگر آدرسِ قبلی باز نمی‌شود، با آدرسِ جدیدِ زیر "
        "وارد پنل شوید (بدونِ نیاز به فیلترشکن).\n\n"
        "🟢 آدرسِ جدید (برای کپی، روی آن بزنید):\n"
        f"<code>{_html.escape(new_link)}</code>\n\n"
        "🔵 آدرسِ قبلی (فعلاً کار می‌کند):\n"
        f"<code>{_html.escape(prev_link)}</code>\n\n"
        "ℹ️ هر دو آدرس به همان پنلِ شما وصل‌اند؛ لطفاً آدرسِ جدید را ذخیره کنید."
    )
    return rtl(body)


def _finish(counts: dict[str, int], started: dt.datetime, *, no_bot: bool = False) -> float:
    finished = dt.datetime.now(dt.timezone.utc)
    dur = (finished - started).total_seconds()
    _status.update({
        "running": False, "sent": counts["sent"], "blocked": counts["blocked"],
        "failed": counts["failed"], "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round(dur, 1), "no_bot": no_bot,
    })
    return dur


async def run_broadcast(
    session: AsyncSession, text: str, reachable: list[Recipient], *,
    unregistered: int = 0, render: Callable[[Recipient], str] | None = None,
    parse_mode: str | None = None,
    rate_per_sec: float = BROADCAST_RATE_PER_SEC, concurrency: int = BROADCAST_CONCURRENCY,
) -> dict[str, int]:
    """Send to the already-resolved reachable recipients with bounded concurrency + a global rate
    limit, then push a summary to the owner's Telegram. Runs in the background (its own session +
    bot). Nothing is persisted; only the in-memory `_status` snapshot is updated for live progress.

    `text` is the same message for everyone; pass `render(rec)` instead for a PER-RECIPIENT message
    (e.g. the panel-migration links). `parse_mode` is forwarded to send_message (e.g. "HTML")."""
    total = len(reachable)
    started = dt.datetime.now(dt.timezone.utc)
    counts = {"sent": 0, "blocked": 0, "failed": 0}
    _status.update({
        "running": True, "total": total, "sent": 0, "blocked": 0, "failed": 0,
        "unregistered": unregistered, "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None, "duration_s": None,
    })
    if not reachable or (render is None and not text.strip()):
        _finish(counts, started)
        return counts

    bot = await build_bot(session)
    if bot is None:
        log.warning("broadcast: no bot token configured; %s recipients not sent", total)
        _finish(counts, started, no_bot=True)
        return counts

    limiter = _RateLimiter(rate_per_sec)
    sem = asyncio.Semaphore(concurrency)

    async def _send(rec: Recipient) -> None:
        cid = rec["chat_id"]
        if cid is None:  # reachable always has a chat_id; satisfies the type checker
            return
        body = render(rec) if render is not None else text
        async with sem:
            outcome = await send_with_flood_control(
                bot, cid, body, limiter, parse_mode=parse_mode)
        counts[outcome] += 1
        _status[outcome] = counts[outcome]

    try:
        await asyncio.gather(*[_send(r) for r in reachable])
    finally:
        await bot.session.close()

    dur = _finish(counts, started)
    await owner_notify.notify_owner(
        session,
        f"📣 پیام همگانی تمام شد — ✅ {counts['sent']} موفق، 🚫 {counts['blocked']} مسدود، "
        f"❌ {counts['failed']} ناموفق، 📵 {unregistered} بدونِ‌ربات (از مجموعِ {total} گیرنده) "
        f"• مدت: ~{int(dur)} ثانیه",
    )
    log.info("Broadcast done: sent=%s blocked=%s failed=%s unreg=%s total=%s in %.1fs",
             counts["sent"], counts["blocked"], counts["failed"], unregistered, total, dur)
    return counts
