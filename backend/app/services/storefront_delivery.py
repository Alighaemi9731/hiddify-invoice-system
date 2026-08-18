"""Durable, resumable, tenant-scoped Telegram delivery queue for storefront broadcasts, direct
messages, and the platform's monthly free-trial notice (plan 006).

A broadcast/direct message is enqueued as a `StorefrontBroadcastJob` with a create-time SNAPSHOT of
its recipients (`StorefrontDeliveryRecipient` rows). The scheduler worker (`run_once`) then claims
pending recipients in bounded batches under `FOR UPDATE SKIP LOCKED`, leases them, sends OUTSIDE any
DB transaction (per-shop bot credential + the shared flood-control policy), and records terminal or
retry outcomes — so a crash never loses a job and never blocks the request path.

Delivery is AT-LEAST-ONCE: a crash after Telegram accepted a message but before we recorded `sent`
leaves the row leased; once the lease expires the reconciler re-queues it, so that recipient can
receive ONE duplicate. This is the accepted semantics (it mirrors the storefront operation ledger)
and is surfaced in the UI.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import (
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCustomer,
    StorefrontDeliveryRecipient,
)
from app.services import broadcast, storefront

log = logging.getLogger("bot.storefront")

_BATCH = 100                        # recipients claimed per batch
_LEASE_SECONDS = 120                # a claimed ('sending') row is owned for this long
_MAX_ATTEMPTS = 5                   # total send attempts before a recipient is terminal 'failed'
_PARK_DELAY_SECONDS = 300           # re-try delay when the shop bot is unavailable (no attempt burned)
_RETRY_LADDER = (5, 30, 120, 600, 1800)  # seconds between attempts, keyed by attempt number
_CLAIMABLE = ("pending", "retry_wait", "unknown")
_TERMINAL = ("sent", "blocked", "failed", "canceled")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _retry_delay(attempt: int) -> int:
    return _RETRY_LADDER[min(max(attempt, 1), len(_RETRY_LADDER)) - 1]


# ── create-time snapshot (called inside the entity-command transaction, NO commit) ────────────────

async def snapshot_job(
    session: AsyncSession, *, storefront_bot_id: int, kind: str, segment: str | None,
    message_text: str, actor_telegram_id: int, idempotency_key: str | None,
    customers: list[StorefrontCustomer],
) -> StorefrontBroadcastJob:
    """Insert a job + one recipient row per non-banned customer with a real chat id, all on the
    caller's session (the caller commits). Recipients are snapshotted so later customer changes never
    alter an in-flight job's audience."""
    now = _now()
    job = StorefrontBroadcastJob(
        storefront_bot_id=storefront_bot_id, actor_telegram_id=actor_telegram_id, kind=kind,
        segment=segment, message_text=message_text, status="queued", idempotency_key=idempotency_key,
        total_count=0, sent_count=0, blocked_count=0, failed_count=0, pending_count=0,
    )
    session.add(job)
    await session.flush()  # populate job.id
    seen: set[int] = set()
    rows: list[dict] = []
    for c in customers:
        if not c.telegram_id or c.id in seen:
            continue
        seen.add(c.id)
        rows.append({
            "job_id": job.id, "customer_id": c.id, "chat_id": int(c.telegram_id),
            "status": "pending", "attempt_count": 0, "next_attempt_at": now,
            "created_at": now, "updated_at": now,
        })
    if rows:
        await session.execute(insert(StorefrontDeliveryRecipient), rows)
    job.total_count = len(rows)
    job.pending_count = len(rows)
    return job


# ── worker ────────────────────────────────────────────────────────────────────

def _build_bot(token: str):  # noqa: ANN202 — returns an aiogram Bot (or a test double)
    """Build a send-only Bot for the shop credential. Monkeypatched in tests."""
    from aiogram import Bot

    from app.bot import rtl_middleware
    from app.bot.session import new_session
    # Shared TLS trust store: the delivery worker builds one Bot per shop per batch, so a
    # per-bot context re-parsed certifi's CA bundle on every send round. See app/bot/session.py.
    bot = Bot(token=token, session=new_session())
    rtl_middleware.install(bot)
    return bot


async def _safe_close(bot) -> None:  # noqa: ANN001
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class _Claim:
    recipient_id: int
    job_id: int
    chat_id: int
    attempt_count: int


async def _reclaim_expired(worker_id: str) -> int:
    """Reconcile rows stuck in 'sending' past their lease (a crashed/slow worker). Under the max
    attempt budget they become 'unknown' (claimable once more — the at-least-once retry); once the
    budget is spent they are terminal 'failed'."""
    async with SessionLocal() as session:
        now = _now()
        stmt = select(StorefrontDeliveryRecipient).where(
            StorefrontDeliveryRecipient.status == "sending",
            StorefrontDeliveryRecipient.lease_expires_at < now,
        ).order_by(StorefrontDeliveryRecipient.id).limit(_BATCH)
        try:
            stmt = stmt.with_for_update(skip_locked=True)
        except Exception:  # noqa: BLE001 — sqlite has no row locks
            pass
        rows = list((await session.execute(stmt)).scalars().all())
        affected: set[int] = set()
        for r in rows:
            affected.add(r.job_id)
            r.lease_owner = None
            r.lease_expires_at = None
            r.last_error_class = "lease_expired"
            if int(r.attempt_count or 0) >= _MAX_ATTEMPTS:
                r.status = "failed"
                r.next_attempt_at = None
            else:
                r.status = "unknown"
                r.next_attempt_at = now
        if rows:
            await session.flush()
            await _recompute_jobs(session, affected)
            await session.commit()
        return len(rows)


async def _claim_batch(worker_id: str) -> tuple[list[_Claim], dict[int, tuple[int, str, str]]]:
    """Atomically claim up to `_BATCH` due recipients: stamp 'sending' + lease + attempt++, commit,
    and return plain claim tuples plus a {job_id: (shop_id, message_text, kind)} map (extracted
    BEFORE the commit expires the ORM rows)."""
    async with SessionLocal() as session:
        now = _now()
        stmt = select(StorefrontDeliveryRecipient).where(
            StorefrontDeliveryRecipient.status.in_(_CLAIMABLE),
            StorefrontDeliveryRecipient.next_attempt_at <= now,
        ).order_by(
            StorefrontDeliveryRecipient.next_attempt_at, StorefrontDeliveryRecipient.id
        ).limit(_BATCH)
        try:
            stmt = stmt.with_for_update(skip_locked=True)
        except Exception:  # noqa: BLE001
            pass
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return [], {}
        lease_until = now + dt.timedelta(seconds=_LEASE_SECONDS)
        claims: list[_Claim] = []
        job_ids = {r.job_id for r in rows}
        jobs = {j.id: j for j in (await session.execute(
            select(StorefrontBroadcastJob).where(StorefrontBroadcastJob.id.in_(job_ids)))).scalars().all()}
        job_meta: dict[int, tuple[int, str, str]] = {}
        for r in rows:
            job = jobs.get(r.job_id)
            if job is None:
                continue
            r.status = "sending"
            r.lease_owner = worker_id
            r.lease_expires_at = lease_until
            r.attempt_count = int(r.attempt_count or 0) + 1
            if job.status == "queued":
                job.status = "running"
            job_meta[r.job_id] = (job.storefront_bot_id, job.message_text, str(job.kind or ""))
            claims.append(_Claim(r.id, r.job_id, int(r.chat_id), int(r.attempt_count)))
        await session.commit()
        return claims, job_meta


async def _shop_tokens(shop_ids: set[int]) -> dict[int, str | None]:
    """Fetch + decrypt each shop's bot credential in one brief session (closed BEFORE any send)."""
    async with SessionLocal() as session:
        shops = (await session.execute(
            select(StorefrontBot).where(StorefrontBot.id.in_(shop_ids)))).scalars().all()
        out: dict[int, str | None] = {}
        for shop in shops:
            try:
                out[shop.id] = storefront.bot_token(shop)
            except Exception:  # noqa: BLE001
                out[shop.id] = None
        return out


def _menu_markup(kind: str):  # noqa: ANN202 — returns a ReplyKeyboardMarkup or None
    """The reply keyboard to dock along with a message, or None to leave the customer's keyboard as
    it is (the normal case for a broadcast or a direct message).

    Only the platform's monthly free-trial notice docks one. A reply keyboard lives on the
    customer's phone until something replaces it, so a customer whose menu was last rendered while
    «🎁 تست رایگان» was still hidden would read "press the free-trial button" next to a menu that
    has no such button. Docking it with the announcement is the one moment we know the trial is
    available. Safe mid-flow: the FSM lock lives server-side (`handlers.sf_menu`), so a fresh
    keyboard cannot let a customer fire a command inside an active flow."""
    if kind != "trial_reset":
        return None
    from app.bot.storefront import keyboards as kb

    return kb.customer_reply_kb(show_free_trial=True)


async def _dispatch(
    claims: list[_Claim], job_meta: dict[int, tuple[int, str, str]]
) -> dict[int, tuple[str, str | None]]:
    """Send each claimed recipient with NO DB connection held. Groups by shop, builds one temp Bot per
    shop (shared flood-control limiter), and classifies each outcome. Returns
    {recipient_id: (outcome, error_class)} where outcome ∈ sent|blocked|failed|park."""
    by_shop: dict[int, list[tuple[_Claim, str, str]]] = defaultdict(list)
    for c in claims:
        shop_id, text, kind = job_meta[c.job_id]
        by_shop[shop_id].append((c, text, kind))
    tokens = await _shop_tokens(set(by_shop))
    outcomes: dict[int, tuple[str, str | None]] = {}
    for shop_id, items in by_shop.items():
        token = tokens.get(shop_id)
        bot = None
        if token:
            try:
                bot = _build_bot(token)
            except Exception as exc:  # noqa: BLE001 — malformed token: park, don't burn attempts
                await _mark_shop_revoked(shop_id, f"malformed token: {exc}")
                bot = None
        if bot is None:
            for c, _t, _k in items:
                outcomes[c.recipient_id] = ("park", "bot_unavailable")
            continue
        limiter = broadcast.rate_limiter()
        try:
            for c, text, kind in items:
                res = await broadcast.send_with_flood_control(
                    bot, c.chat_id, text, limiter, max_retry=1,
                    reply_markup=_menu_markup(kind))
                outcomes[c.recipient_id] = (res, None if res == "sent" else res)
        finally:
            await _safe_close(bot)
    return outcomes


async def _mark_shop_revoked(shop_id: int, error: str) -> None:
    """A stored token we can no longer even construct a Bot from is permanently dead — clear it
    and tell the reseller, rather than parking every future broadcast forever."""
    try:
        async with SessionLocal() as session:
            await storefront.mark_revoked(session, shop_id, error)
    except Exception:  # noqa: BLE001
        log.warning("failed to mark storefront %s revoked", shop_id, exc_info=True)


async def _apply_outcomes(
    worker_id: str, claims: list[_Claim], outcomes: dict[int, tuple[str, str | None]]
) -> dict[str, int]:
    """Record each recipient's outcome in a short txn (never holding a DB conn during a send), then
    recompute the affected jobs' aggregate counts. Rows that a reconciler already reclaimed (lease
    lost) are skipped, preserving at-least-once."""
    tallies = {"sent": 0, "blocked": 0, "failed": 0, "retry": 0, "park": 0}
    async with SessionLocal() as session:
        now = _now()
        affected: set[int] = set()
        for c in claims:
            r = await session.get(StorefrontDeliveryRecipient, c.recipient_id)
            if r is None or r.status != "sending" or r.lease_owner != worker_id:
                continue  # taken over / reconciled → do not overwrite
            affected.add(r.job_id)
            outcome, err = outcomes.get(c.recipient_id, ("failed", "missing"))
            r.lease_owner = None
            r.lease_expires_at = None
            if outcome == "sent":
                r.status, r.last_error_class, r.next_attempt_at = "sent", None, None
                tallies["sent"] += 1
            elif outcome == "blocked":
                r.status, r.last_error_class, r.next_attempt_at = "blocked", "blocked", None
                tallies["blocked"] += 1
            elif outcome == "park":
                # Shop bot unavailable: retry later WITHOUT consuming an attempt.
                r.status = "retry_wait"
                r.last_error_class = err
                r.attempt_count = max(0, int(r.attempt_count or 0) - 1)
                r.next_attempt_at = now + dt.timedelta(seconds=_PARK_DELAY_SECONDS)
                tallies["park"] += 1
            else:  # failed / transient
                if int(r.attempt_count or 0) >= _MAX_ATTEMPTS:
                    r.status, r.last_error_class, r.next_attempt_at = "failed", err or "failed", None
                    tallies["failed"] += 1
                else:
                    r.status = "retry_wait"
                    r.last_error_class = err or "failed"
                    r.next_attempt_at = now + dt.timedelta(seconds=_retry_delay(int(r.attempt_count or 0)))
                    tallies["retry"] += 1
        await session.flush()
        await _recompute_jobs(session, affected)
        await session.commit()
    return tallies


async def _recompute_jobs(session: AsyncSession, job_ids: set[int]) -> None:
    """Recompute a job's aggregate counts from its recipient rows (idempotent, race-safe: every
    worker reads committed recipient states). Completes a job when nothing is left pending."""
    for jid in job_ids:
        job = await session.get(StorefrontBroadcastJob, jid)
        if job is None:
            continue
        rows = (await session.execute(
            select(StorefrontDeliveryRecipient.status, func.count())
            .where(StorefrontDeliveryRecipient.job_id == jid)
            .group_by(StorefrontDeliveryRecipient.status))).all()
        counts = {str(status): int(n) for status, n in rows}
        total = sum(counts.values())
        sent = counts.get("sent", 0)
        blocked = counts.get("blocked", 0)
        failed = counts.get("failed", 0)
        canceled = counts.get("canceled", 0)
        pending = total - sent - blocked - failed - canceled
        job.total_count = total
        job.sent_count = sent
        job.blocked_count = blocked
        job.failed_count = failed
        job.pending_count = pending
        if job.status != "canceled":
            if total > 0 and pending == 0:
                job.status = "completed"
            elif job.status == "queued":
                job.status = "running"


async def run_once(*, worker_id: str | None = None, max_batches: int = 100) -> dict[str, int]:
    """One worker pass: reconcile expired leases, then claim → send → record in bounded batches until
    the queue is drained (or `max_batches`). Never raises out (the scheduler wraps it too)."""
    worker_id = worker_id or f"w-{uuid.uuid4().hex[:12]}"
    summary = {"claimed": 0, "sent": 0, "blocked": 0, "failed": 0, "retry": 0, "park": 0, "reclaimed": 0}
    try:
        summary["reclaimed"] = await _reclaim_expired(worker_id)
    except Exception:  # noqa: BLE001
        log.warning("delivery reclaim pass failed", exc_info=True)
    for _ in range(max_batches):
        claims, job_meta = await _claim_batch(worker_id)
        if not claims:
            break
        summary["claimed"] += len(claims)
        outcomes = await _dispatch(claims, job_meta)
        tallies = await _apply_outcomes(worker_id, claims, outcomes)
        for k, v in tallies.items():
            summary[k] += v
    return summary


# ── cancellation + retention ──────────────────────────────────────────────────

async def cancel_unsent(session: AsyncSession, job_id: int) -> None:
    """Flip a job's still-unsent recipients (pending/retry_wait/unknown) to 'canceled'. Rows already
    claimed ('sending') may finish. Caller recomputes counts + commits."""
    now = _now()
    rows = (await session.execute(
        select(StorefrontDeliveryRecipient).where(
            StorefrontDeliveryRecipient.job_id == job_id,
            StorefrontDeliveryRecipient.status.in_(_CLAIMABLE),
        ))).scalars().all()
    for r in rows:
        r.status = "canceled"
        r.lease_owner = None
        r.lease_expires_at = None
        r.next_attempt_at = None
    job = await session.get(StorefrontBroadcastJob, job_id)
    if job is not None:
        job.status = "canceled"
        job.canceled_at = now
    await session.flush()
    await _recompute_jobs(session, {job_id})
    # _recompute_jobs leaves status='canceled' intact (guarded), so keep it canceled explicitly.
    if job is not None:
        job.status = "canceled"


async def prune_recipients(session: AsyncSession, retention_days: int) -> int:
    """Delete recipient DETAIL rows older than `retention_days` for TERMINAL jobs (completed/canceled),
    preserving the job aggregate totals + audit forever. 0 (or negative) disables pruning."""
    if retention_days <= 0:
        return 0
    cutoff = _now() - dt.timedelta(days=retention_days)
    terminal_jobs = select(StorefrontBroadcastJob.id).where(
        StorefrontBroadcastJob.status.in_(("completed", "canceled")))
    res = await session.execute(
        delete(StorefrontDeliveryRecipient).where(
            StorefrontDeliveryRecipient.created_at < cutoff,
            StorefrontDeliveryRecipient.job_id.in_(terminal_jobs),
        ).execution_options(synchronize_session=False))
    return int(cast("CursorResult[Any]", res).rowcount or 0)
