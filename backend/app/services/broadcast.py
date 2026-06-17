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
import logging
from typing import Any, TypedDict

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import EndUserSnapshot, Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import owner_notify, reseller_stats

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


# In-memory snapshot of the LAST/current run only (no DB, no per-recipient persistence) so the
# panel can show live progress while the background send runs.
_status: dict[str, Any] = {
    "running": False, "total": 0, "sent": 0, "blocked": 0, "failed": 0,
    "unregistered": 0, "started_at": None, "finished_at": None, "duration_s": None,
}


def current_status() -> dict[str, Any]:
    return dict(_status)

# Audience filters (each applied ON TOP of the base set). `panel_id` is an independent,
# combinable restriction (one panel, or all panels when None).
AUDIENCES = ("all", "debtors", "zero_sale", "few_active", "invoice_below")


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


async def _matching_roots(
    session: AsyncSession, audience: str, panel_id: int | None, threshold: float | None
) -> list[Reseller]:
    """The base set narrowed by the chosen filter (and the optional single-panel restriction)."""
    roots = await reseller_stats.load_billable_roots(session, panel_id)
    if audience == "debtors":
        owed = set((await session.execute(
            select(Invoice.reseller_id).where(Invoice.status.in_(_OWED)).distinct()
        )).scalars().all())
        return [r for r in roots if r.id in owed]
    if audience == "few_active":
        limit = int(threshold or 0)
        active = await _active_counts(session)
        return [r for r in roots if active.get((r.panel_id, r.admin_uuid), 0) < limit]
    if audience in ("zero_sale", "invoice_below"):
        amounts = await _bundle_amounts(session, panel_id)
        if audience == "zero_sale":
            return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[0] <= 0]
        below = float(threshold or 0)
        return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[1] < below]
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
    unregistered: int = 0,
    rate_per_sec: float = BROADCAST_RATE_PER_SEC, concurrency: int = BROADCAST_CONCURRENCY,
) -> dict[str, int]:
    """Send `text` to the already-resolved reachable recipients with bounded concurrency + a global
    rate limit, then push a summary to the owner's Telegram. Runs in the background (its own session
    + bot). Nothing is persisted; only the in-memory `_status` snapshot is updated for live progress."""
    total = len(reachable)
    started = dt.datetime.now(dt.timezone.utc)
    counts = {"sent": 0, "blocked": 0, "failed": 0}
    _status.update({
        "running": True, "total": total, "sent": 0, "blocked": 0, "failed": 0,
        "unregistered": unregistered, "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None, "duration_s": None,
    })
    if not text.strip() or not reachable:
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
        async with sem:
            for _attempt in range(BROADCAST_MAX_RETRY):
                await limiter.acquire()
                try:
                    await bot.send_message(cid, text)
                    counts["sent"] += 1
                    _status["sent"] = counts["sent"]
                    return
                except TelegramRetryAfter as e:           # 429 → wait then retry the same recipient
                    await asyncio.sleep(e.retry_after)
                    continue
                except TelegramForbiddenError:            # user blocked the bot → no retry
                    counts["blocked"] += 1
                    _status["blocked"] = counts["blocked"]
                    return
                except Exception:  # noqa: BLE001
                    counts["failed"] += 1
                    _status["failed"] = counts["failed"]
                    return
            counts["failed"] += 1                          # kept hitting 429 past the retry budget
            _status["failed"] = counts["failed"]

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
