"""Payment confirmation and the optional BEP-20 USDT chain check.

The reseller submits proof for one explicitly selected invoice. The owner makes the final
confirm/reject decision; for USDT/BSC they may first ask this service to verify destination,
token contract, amount, and confirmations through BscScan. A confirmed payment marks only its
linked invoice paid and restores access only when no other due debt remains.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import payment_code
from app.models import Invoice, Payment, PaymentSettlement, Reseller
from app.models.enums import (
    DeliveryKind,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import financial_archive, notifier, rates, settings_service

log = logging.getLogger("payments")

USDT_DECIMALS = 18

# Tx-hash validation, shared by the bot and the web portal so both surfaces enforce the SAME format
# (the portal previously accepted any string → overlong/garbage could 500 on Postgres / create junk
# review rows). BSC = 0x + 64 hex; TON = hex or base64, bounded to the txid column width (80).
BSC_TXID_RE = re.compile(r"0x[0-9a-fA-F]{64}")
TON_TXID_RE = re.compile(r"[A-Za-z0-9_\-/+=]{32,80}")

# Owed = delivered but not yet paid.
_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


@dataclass
class SubmitResult:
    """Outcome of a reseller payment submission. `user_message` is the plain-Persian reply to
    show the reseller (identical on the bot and the web portal). When `notify` is set the caller
    should send the owner the rich review (built by the bot's `_payment_review_html`) with
    confirm/reject buttons, prefixed by `owner_intro`."""

    status: str  # ok | reopened | invalid_txid | dup_confirmed | dup_pending | wrong_owner | not_payable | pending_exists
    user_message: str
    payment: Payment | None = None
    invoice: Invoice | None = None  # the primary/first invoice (back-compat single-invoice views)
    invoices: list[Invoice] | None = None  # the full set this payment covers (>=1)
    notify: bool = False
    owner_intro: str = ""


# A payment may now cover SEVERAL invoices (one transfer settles all of them). The set is
# stored comma-joined in settled_invoice_ids; keep this many below the String(255) column width.
_MAX_INVOICES_PER_PAYMENT = 25


async def _pending_payments_for_resellers(
    session: AsyncSession, reseller_ids: set[int] | list[int]
) -> list[Payment]:
    """All PENDING payments belonging to these resellers (so callers can expand each one's
    invoice SET — a payment can cover several invoices)."""
    if not reseller_ids:
        return []
    return list(
        (
            await session.execute(
                select(Payment).where(
                    Payment.reseller_id.in_(list(reseller_ids)),
                    Payment.status == PaymentStatus.pending,
                )
            )
        ).scalars().all()
    )


async def _sync_settlements(session: AsyncSession, payment: Payment) -> None:
    """Mirror this payment's invoice set into `payment_settlements` (I06). Dual-write: the
    comma column stays byte-equal (a rollback to the previous release keeps working); the hot
    lookups below use the indexed table instead of loading every payment into Python. Callers
    must have flushed the payment (needs `payment.id`) and commit afterwards."""
    await session.execute(
        delete(PaymentSettlement).where(PaymentSettlement.payment_id == payment.id)
    )
    for iid in sorted(set(_settled_ids(payment))):
        session.add(PaymentSettlement(payment_id=payment.id, invoice_id=iid))


async def _pending_invoice_ids_in_sets(
    session: AsyncSession, candidate_ids: set[int], reseller_ids: set[int] | list[int]
) -> set[int]:
    """Which of `candidate_ids` already belong to ANY pending payment's invoice set — used to
    block a duplicate submission. One invoice may never sit in two pending payments at once."""
    if not candidate_ids or not reseller_ids:
        return set()
    rows = (
        await session.execute(
            select(PaymentSettlement.invoice_id)
            .join(Payment, Payment.id == PaymentSettlement.payment_id)
            .where(
                Payment.status == PaymentStatus.pending,
                Payment.reseller_id.in_(list(reseller_ids)),
                PaymentSettlement.invoice_id.in_(list(candidate_ids)),
            )
        )
    ).scalars().all()
    return set(rows)


async def _pending_payment_for_invoice(session: AsyncSession, invoice_id: int | None) -> Payment | None:
    """The PENDING payment whose invoice SET contains this invoice (if any) — used to block a
    duplicate submission so one invoice never sits in several pending payments. Single indexed
    query on the settlements table (which mirrors both the primary `invoice_id` link and the
    multi-invoice `settled_invoice_ids` sets)."""
    if not invoice_id:
        return None
    return (
        await session.execute(
            select(Payment)
            .join(PaymentSettlement, PaymentSettlement.payment_id == Payment.id)
            .where(
                Payment.status == PaymentStatus.pending,
                PaymentSettlement.invoice_id == invoice_id,
            )
            .limit(1)
        )
    ).scalars().first()


async def _payment_by_txid(session: AsyncSession, txid: str) -> Payment | None:
    """The payment row holding this txid, locked FOR UPDATE (no-op on SQLite) so a concurrent
    confirm/reject/resubmit of the same row serializes against this submission — without the
    lock, the owner confirming a rejected payment at the same moment its customer resubmits the
    hash could leave a confirmed-then-silently-demoted payment with rewritten coverage."""
    return (
        await session.execute(
            select(Payment).where(Payment.txid == txid).with_for_update()
        )
    ).scalars().first()


async def submit_reseller_payment(
    session: AsyncSession,
    *,
    reseller_ids: set[int],
    invoice_ids: list[int] | None = None,
    invoice_id: int | None = None,
    txid: str | None = None,
    chain: str = "bsc",
    screenshot: bool = False,
) -> SubmitResult:
    """Validate + create a PENDING payment for one or more OWED invoices the caller owns. Shared
    by the Telegram bot and the web portal so the safety rules are identical on both surfaces:
      * a tx hash already in the system is never duplicated (confirmed/pending blocked; a
        REJECTED one is re-opened only for the reseller it belongs to, and ONLY after its
        coverage — the fresh selection, or the original set on a cold resubmit — passes the
        same re-validation as a fresh submission);
      * EVERY chosen invoice is re-checked under a row lock — must still belong to the caller, be
        OWED, and not be deferred to a future date. If ANY chosen invoice is no longer payable the
        WHOLE batch is rejected (atomic) — never silently pay a subset / mis-attribute money;
      * no invoice may already sit in another pending payment's set (one pending per invoice).
    A multi-invoice payment stores the full set in `settled_invoice_ids` (the first id is also the
    primary `invoice_id` for display/back-compat) and its amount is the SUM of the set. Never
    auto-confirms — the owner decides. The file save for a screenshot proof is done by the caller
    (it needs the new payment id). `invoice_id` is a legacy single-id alias for `invoice_ids`."""
    from app.services.periods import today as tehran_today

    # Normalize the requested ids: explicit set wins; fall back to the legacy single alias.
    raw = list(invoice_ids) if invoice_ids else ([invoice_id] if invoice_id else [])
    ids: list[int] = []
    for i in raw:  # dedup, preserve order
        if i and i not in ids:
            ids.append(i)
    if len(ids) > _MAX_INVOICES_PER_PAYMENT:
        return SubmitResult(
            "not_payable",
            f"حداکثر {_MAX_INVOICES_PER_PAYMENT} فاکتور را می‌توان یکجا پرداخت کرد؛ "
            "تعداد کمتری انتخاب کنید.",
        )

    # A rejected txid re-submitted WITH a fresh invoice selection updates that row's coverage
    # (set below); stays None for a brand-new payment or a cold resubmit with no selection.
    reopen: Payment | None = None

    if txid:
        # Canonicalize + validate on the shared path so the bot AND the portal enforce identical rules.
        # BSC hashes are matched case-insensitively on-chain, so store them lowercase — otherwise
        # 0xABC… and 0xabc… would be two rows for ONE transfer and could each settle invoices. TON
        # hashes stay case-sensitive.
        txid = txid.strip()
        # Defensive allow-list: only known chains reach the validators/method-map below. An
        # unknown chain would otherwise be validated as TON and stored as a USDT payment.
        if chain not in ("bsc", "avax", "ton"):
            return SubmitResult("invalid_txid", "شبکهٔ پرداخت نامعتبر است.")
        if chain in ("bsc", "avax"):
            # AVAX C-Chain hashes share the BSC format (0x + 64 hex) and are matched
            # case-insensitively on-chain → lowercase, same as BSC.
            txid = txid.lower()
            if not BSC_TXID_RE.fullmatch(txid):
                return SubmitResult(
                    "invalid_txid",
                    "شناسهٔ تراکنش (TXID) نامعتبر است؛ باید با 0x شروع شود و ۶۴ رقمِ هگز باشد.",
                )
        elif not TON_TXID_RE.fullmatch(txid):
            return SubmitResult("invalid_txid", "هشِ تراکنشِ TON نامعتبر است.")
        existing = await _payment_by_txid(session, txid)
        if existing:
            if existing.status == PaymentStatus.confirmed:
                return SubmitResult("dup_confirmed", "این تراکنش قبلاً ثبت و تأیید شده است.")
            if existing.status == PaymentStatus.pending:
                return SubmitResult("dup_pending", "این تراکنش قبلاً ثبت شده و در انتظار بررسی است.")
            # Rejected → re-open the SAME row (txid is unique), but only for its real owner so
            # nobody who happens to know a tx hash can resurrect/claim another's payment.
            if existing.reseller_id not in reseller_ids:
                return SubmitResult("wrong_owner", "این شناسهٔ تراکنش به حساب شما مربوط نیست.")
            if not ids:
                # Cold resubmit with NO fresh selection → re-open with the ORIGINAL coverage,
                # re-validated below EXACTLY like a fresh submission (ownership, owed, deferral,
                # one-pending-per-invoice). Without the re-validation a rejected txid could
                # resurrect coverage over invoices meanwhile paid/canceled/deferred, or stack a
                # second pending payment onto an invoice that already has one.
                ids = _settled_ids(existing)
            # A rejected payment whose customer re-selected invoices this time (e.g. now paying
            # «همهٔ بدهی» after the owner rejected a single-invoice attempt) → its coverage is NOT
            # locked. Update it to the newly-chosen set after the SAME validation as a fresh
            # payment (below). Without this, re-sending the hash forever re-opened the OLD single
            # invoice, so the customer could never make it cover both — the exact reported trap.
            reopen = existing

    if not ids:
        return SubmitResult(
            "not_payable",
            "فاکتوری برای پرداخت انتخاب نشده است؛ از منوی «💳 پرداخت فاکتور» دوباره اقدام کنید.",
        )

    # Re-validate EVERY chosen invoice under lock. Any could have been paid/canceled/reverted/
    # re-deadlined between the button tap and the proof arriving — if so, reject the whole batch.
    # Rows are locked in SORTED id order (two concurrent submissions locking overlapping sets in
    # opposite orders would deadlock on Postgres); the payment keeps the submission order.
    today = tehran_today()
    fresh_by_id: dict[int, Invoice] = {}
    for iid in sorted(ids):
        fresh = await session.get(Invoice, iid, with_for_update=True)
        if (
            fresh is None
            or fresh.reseller_id not in reseller_ids
            or fresh.status not in _OWED
            or (fresh.deferred_until and fresh.deferred_until > today)
        ):
            return SubmitResult(
                "not_payable",
                "یک یا چند فاکتورِ انتخاب‌شده دیگر قابل پرداخت نیست یا وضعیتش تغییر کرده است؛ "
                "از منوی «💳 پرداخت فاکتور» دوباره انتخاب کنید.",
            )
        fresh_by_id[iid] = fresh
    fresh_list: list[Invoice] = [fresh_by_id[iid] for iid in ids]

    held = await _pending_invoice_ids_in_sets(session, {i.id for i in fresh_list}, reseller_ids)
    if held:
        msg = (
            "برای یک یا چند فاکتورِ انتخاب‌شده قبلاً رسید فرستاده‌اید که در انتظار تأیید است؛ "
            "لطفاً منتظر بررسیِ پشتیبانی بمانید. (نیازی به ارسال دوباره نیست.)"
            if screenshot else
            "برای یک یا چند فاکتورِ انتخاب‌شده قبلاً پرداختی ثبت کرده‌اید که در انتظار تأیید است؛ "
            "لطفاً منتظر بررسیِ پشتیبانی بمانید."
        )
        return SubmitResult("pending_exists", msg)

    primary = fresh_list[0]
    settled_ids = ",".join(str(i.id) for i in fresh_list)
    total_usdt = float(sum(Decimal(str(i.amount_usdt or 0)) for i in fresh_list))
    total_toman = float(sum(Decimal(str(i.amount_toman or 0)) for i in fresh_list))
    if reopen is not None:
        # Re-open the rejected txid row with the NEW coverage (may now span several invoices).
        # method/chain are refreshed from THIS submission: a hash first sent on the wrong
        # network and rejected («شبکهٔ اشتباه») must not keep the stale chain — the owner review
        # would keep linking the wrong explorer and the deposit check would read the wrong chain.
        reopen.reseller_id = primary.reseller_id
        reopen.invoice_id = primary.id
        reopen.settled_invoice_ids = settled_ids
        reopen.amount_usdt = total_usdt
        reopen.amount_toman = total_toman
        reopen.method = {
            "ton": PaymentMethod.ton_txid,
            "avax": PaymentMethod.avax_txid,
        }.get(chain, PaymentMethod.usdt_txid)
        reopen.chain = chain
        reopen.status = PaymentStatus.pending
        if "[resubmitted]" not in (reopen.note or ""):
            reopen.note = (reopen.note or "") + " [resubmitted]"
        payment = reopen
    elif screenshot:
        payment = Payment(
            reseller_id=primary.reseller_id, invoice_id=primary.id,
            settled_invoice_ids=settled_ids, amount_usdt=total_usdt, amount_toman=total_toman,
            method=PaymentMethod.screenshot, status=PaymentStatus.pending,
            note="رسید تصویری (در انتظار بررسی مالک)",
        )
        session.add(payment)
    else:
        method = {
            "ton": PaymentMethod.ton_txid,
            "avax": PaymentMethod.avax_txid,
        }.get(chain, PaymentMethod.usdt_txid)
        payment = Payment(
            reseller_id=primary.reseller_id, invoice_id=primary.id,
            settled_invoice_ids=settled_ids, amount_usdt=total_usdt, amount_toman=total_toman,
            method=method, chain=chain, status=PaymentStatus.pending, txid=txid,
        )
        session.add(payment)
    try:
        await session.flush()
    except IntegrityError:
        # Two concurrent FIRST-TIME submissions of the same hash can both pass the
        # `_payment_by_txid` check (the row doesn't exist yet, so there is nothing to lock);
        # the loser hits the unique txid constraint here. Map it to the friendly duplicate
        # message instead of a 500 on the portal / a swallowed exception in the bot.
        await session.rollback()
        return SubmitResult("dup_pending", "این تراکنش قبلاً ثبت شده و در انتظار بررسی است.")
    await _sync_settlements(session, payment)
    await session.commit()

    n = len(fresh_list)
    scope_fa = (
        "این فاکتور" if n == 1
        else f"این {n} فاکتور (مبلغ کل: {total_toman:,.0f} تومان)"
    )
    if screenshot:
        user_message = (
            f"✅ رسید شما برای {scope_fa} دریافت شد و در انتظار تأیید پشتیبانی است.\n"
            "لطفاً منتظر بمانید؛ نتیجهٔ بررسی همین‌جا به شما اطلاع داده می‌شود. "
            "(نیازی به ارسال دوباره نیست.)\n"
            f"🔖 شمارهٔ پیگیری: #{payment_code(payment.id)}"
        )
        owner_intro = "🧾 رسید پرداخت جدید — منتظر تأیید شماست."
    else:
        label = {"ton": "GRAM", "avax": "AVAX"}.get(payment.chain or chain, "USDT")
        user_message = (
            f"✅ شناسهٔ تراکنش ({label}) برای {scope_fa} دریافت شد و در انتظار تأیید پشتیبانی است.\n"
            "نتیجهٔ بررسی همین‌جا به شما اطلاع داده می‌شود.\n"
            f"🔖 شمارهٔ پیگیری: #{payment_code(payment.id)}"
        )
        owner_intro = (
            "🔁 تراکنشِ ردشده دوباره ارسال شد و منتظر تأیید شماست."
            if reopen is not None else
            "💳 پرداخت جدید ثبت شد و منتظر تأیید شماست."
        )
    return SubmitResult(
        "reopened" if reopen is not None else "ok",
        user_message, payment=payment, invoice=primary, invoices=fresh_list,
        notify=True, owner_intro=owner_intro,
    )


async def _reseller_has_other_due(
    session: AsyncSession, reseller_id: int, exclude_invoice_ids: set[int] | None
) -> bool:
    """True if the reseller still has another OWED, non-deferred invoice OUTSIDE the excluded
    set. Used to avoid restoring a suspended reseller while they still owe on a different invoice
    — paying some invoices must not lift enforcement when other debts remain."""
    from app.services.periods import today as tehran_today

    exclude = exclude_invoice_ids or set()
    today = tehran_today()  # Tehran-local, matching enforcement/dunning deadline checks
    rows = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller_id, Invoice.status.in_(_OWED)
            )
        )
    ).scalars().all()
    for inv in rows:
        if inv.id in exclude:
            continue
        if inv.deferred_until and inv.deferred_until > today:
            continue  # deadline in the future → not currently due
        return True
    return False


async def _maybe_restore(
    session: AsyncSession,
    reseller: Reseller | None,
    *,
    exclude_invoice_ids: set[int] | None = None,
    exclude_invoice_id: int | None = None,  # legacy single-id alias
) -> None:
    if reseller is None:
        return
    if not await settings_service.get(session, "auto_restore_on_payment", True):
        return
    exclude = set(exclude_invoice_ids or set())
    if exclude_invoice_id is not None:
        exclude.add(exclude_invoice_id)
    # Only lift enforcement when NO other due (non-deferred) invoice remains for this reseller.
    if await _reseller_has_other_due(session, reseller.id, exclude):
        log.info("restore held for reseller %s: other due invoice(s) remain", reseller.id)
        return
    try:
        from app.services import enforcement

        await enforcement.queue_restore(
            session,
            reseller,
            require_no_due=True,
            reason="payment",
        )
    except Exception:  # noqa: BLE001 — enforcement module/credentials may be absent
        log.info("restore skipped/failed for reseller %s", reseller.id)


@dataclass
class PaymentResult:
    status: str             # confirmed | pending | rejected
    paid: bool
    message_fa: str
    detail: str = ""


@dataclass
class _ChainCheck:
    found: bool
    to_address: str | None
    from_address: str | None
    amount_usdt: Decimal
    confirmations: int
    error: str | None = None
    contract_address: str | None = None  # the token contract of the matched tx


def _ton_account_id(addr: str) -> str:
    """Normalize a TON address to its 32-byte account id (hex) so EQ.../UQ.../raw forms of the
    SAME wallet compare equal. Returns '' if it can't be parsed."""
    import base64

    a = (addr or "").strip()
    if not a:
        return ""
    if ":" in a:  # raw "workchain:hex"
        return a.split(":", 1)[1].strip().lower()[-64:]
    try:
        raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
        if len(raw) >= 34:  # friendly form = [tag(1)][workchain(1)][account(32)][crc(2)]
            return raw[2:34].hex()
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def _ton_received(txid: str, our_address: str, api_key: str | None = None) -> Decimal | None:
    """Best-effort: total TON credited to `our_address` by transaction `txid`, read from
    toncenter v3. Returns None on any failure / no match so the caller falls back. Network I/O is
    async (never blocks the loop); this is display-only — it NEVER auto-confirms a payment."""
    if not txid or not our_address:
        return None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as client:
            resp = await client.get(
                "https://toncenter.com/api/v3/transactions",
                params={"hash": txid, "limit": 10},
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception:  # noqa: BLE001 — toncenter down / bad txid → fall back silently
        return None
    want = _ton_account_id(our_address)
    if not want:
        return None
    book = data.get("address_book") or {}

    def _credits_us(dest: str | None) -> bool:
        if not dest:
            return False
        friendly = ((book.get(dest) or {}).get("user_friendly")) or dest
        return _ton_account_id(friendly) == want

    total = Decimal(0)
    for tx in (data.get("transactions") or []):
        # The hash a user copies from their wallet is usually the SENDER-side transaction: its
        # in_msg is the external trigger (no TON value) and the credit to us sits in its out_msgs.
        # If instead the pasted hash is the RECEIVER-side transaction (on our own account), the
        # credit is the in_msg. Scan BOTH directions and count only messages whose destination is
        # our wallet — for one transaction the deposit appears in exactly one direction, so there
        # is no double-count.
        msgs = []
        in_msg = tx.get("in_msg") or {}
        if in_msg:
            msgs.append(in_msg)
        msgs.extend(tx.get("out_msgs") or [])
        for m in msgs:
            val = m.get("value")
            if val in (None, "") or not _credits_us(m.get("destination")):
                continue
            try:
                total += Decimal(int(str(val))) / Decimal(1_000_000_000)
            except (TypeError, ValueError):
                continue
    return total if total > 0 else None


async def ton_deposit_check(session: AsyncSession, payment: Payment) -> dict:
    """Decision aid for the panel's MANUAL TON confirmation: read the actual TON deposited for this
    payment's txid, convert at the live TON→Toman rate, and compare (within tolerance) to the
    invoice amount. Display-only — never auto-confirms. Best-effort: returns {'available': False}
    if the chain can't be read, so the panel just shows the existing figures and nothing breaks."""
    if payment.chain != "ton" or not payment.txid:
        return {"available": False}
    our = await settings_service.get(session, "ton_wallet_address", "") or ""
    api_key = await settings_service.get(session, "toncenter_api_key", "") or None
    received = await _ton_received(payment.txid, our, api_key)
    if received is None:
        return {"available": False}
    ton_rate = await rates.get_ton_toman(session)  # int, 0 if unavailable
    received_toman = float(received) * ton_rate if ton_rate else 0.0
    invoice_toman = await _settled_amount_toman(session, payment)
    tol_pct = float(await settings_service.get(session, "ton_amount_tolerance_pct", 5) or 0)
    match: bool | None = None
    if invoice_toman > 0 and received_toman > 0:
        diff_pct = abs(received_toman - invoice_toman) / invoice_toman * 100
        match = diff_pct <= tol_pct
    return {
        "available": True,
        "received_ton": round(float(received), 4),
        "received_toman": round(received_toman),
        "invoice_toman": round(invoice_toman),
        "ton_rate": ton_rate,
        "tolerance_pct": tol_pct,
        "match": match,
    }


# ERC-20 `Transfer(address,address,uint256)` event signature (topics[0]).
_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


async def _usdt_received(
    txid: str, our_wallet: str, contract: str, rpc_url: str
) -> tuple[Decimal | None, int | None]:
    """Best-effort (USDT credited to our wallet by this tx, confirmations), read straight from a
    public BSC JSON-RPC node — free, no API key. Parses the tx receipt's ERC-20 Transfer logs for
    the configured token contract and sums transfers whose recipient is our wallet. Returns
    (None, None) on any failure so the caller falls back. Display-only — never auto-confirms."""
    tx = (txid or "").strip()
    if not tx or not our_wallet or not contract or not rpc_url:
        return None, None
    if not tx.startswith("0x"):
        tx = "0x" + tx
    want = our_wallet.lower()
    token = contract.lower()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx]})
            resp.raise_for_status()
            receipt = (resp.json() or {}).get("result")
            # status 0x1 = success; a reverted (0x0) transfer credited nothing.
            if not receipt or receipt.get("status") != "0x1":
                return None, None
            total = Decimal(0)
            for lg in receipt.get("logs") or []:
                if (lg.get("address") or "").lower() != token:
                    continue
                topics = lg.get("topics") or []
                if len(topics) < 3 or (topics[0] or "").lower() != _ERC20_TRANSFER_TOPIC:
                    continue
                to_addr = "0x" + (topics[2] or "")[-40:].lower()  # topic is 32-byte left-padded
                if to_addr != want:
                    continue
                try:
                    total += Decimal(int(lg.get("data") or "0x0", 16)) / (Decimal(10) ** USDT_DECIMALS)
                except (TypeError, ValueError):
                    continue
            if total <= 0:
                return None, None
            confs: int | None = None
            try:
                bn = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []})
                latest = int((bn.json() or {}).get("result") or "0x0", 16)
                txblk = int(receipt.get("blockNumber") or "0x0", 16)
                if latest and txblk:
                    confs = max(0, latest - txblk + 1)
            except Exception:  # noqa: BLE001 — confirmations are optional
                confs = None
            return total, confs
    except Exception:  # noqa: BLE001 — node down / bad txid → fall back silently
        return None, None


async def usdt_deposit_check(session: AsyncSession, payment: Payment) -> dict:
    """Decision aid for the panel's MANUAL USDT confirmation: read the actual USDT credited to our
    wallet by this payment's txid (free BSC RPC) and compare (within tolerance) to the invoice's
    USDT amount. Display-only — never auto-confirms. Best-effort: returns {'available': False}."""
    if payment.chain in ("ton", "avax") or not payment.txid:
        return {"available": False}
    cfg = await settings_service.get_many(
        session,
        ["usdt_bep20_address", "usdt_bep20_contract", "bsc_rpc_url",
         "min_confirmations", "payment_amount_tolerance_usdt"],
    )
    received, confs = await _usdt_received(
        payment.txid, cfg.get("usdt_bep20_address") or "",
        cfg.get("usdt_bep20_contract") or "", cfg.get("bsc_rpc_url") or "",
    )
    if received is None:
        return {"available": False}
    invoice_usdt = await _settled_amount_usdt(session, payment)
    tol = float(cfg.get("payment_amount_tolerance_usdt") or 0)
    match: bool | None = None
    if invoice_usdt > 0:
        match = abs(float(received) - invoice_usdt) <= tol
    return {
        "available": True,
        "received_usdt": round(float(received), 2),
        "invoice_usdt": round(invoice_usdt, 2),
        "confirmations": confs,
        "min_confirmations": int(cfg.get("min_confirmations") or 0),
        "tolerance_usdt": tol,
        "match": match,
    }


async def _avax_received(
    txid: str, our_wallet: str, rpc_url: str
) -> tuple[Decimal | None, int | None]:
    """Best-effort (native AVAX credited to our wallet by this tx, confirmations), read from a public
    Avalanche C-Chain JSON-RPC node — free, no API key. AVAX is a NATIVE coin, so (unlike USDT) the
    amount is the transaction's own `value` and the recipient is its `to` — no ERC-20 log parsing.
    Returns (None, None) on any failure so the caller falls back. Display-only — never auto-confirms.
    Limitation: only a top-level value transfer is read (the normal exchange→wallet case); a
    contract-internal transfer isn't visible here and just yields a fall-back 'read failed'."""
    tx = (txid or "").strip()
    if not tx or not our_wallet or not rpc_url:
        return None, None
    if not tx.startswith("0x"):
        tx = "0x" + tx
    want = our_wallet.lower()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx]})
            resp.raise_for_status()
            txn = (resp.json() or {}).get("result")
            if not txn or (txn.get("to") or "").lower() != want:
                return None, None
            try:
                value = Decimal(int(txn.get("value") or "0x0", 16)) / (Decimal(10) ** 18)
            except (TypeError, ValueError):
                return None, None
            if value <= 0:
                return None, None
            # Confirm the tx actually succeeded (a reverted tx credits nothing).
            rc = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 2, "method": "eth_getTransactionReceipt", "params": [tx]})
            receipt = (rc.json() or {}).get("result")
            if not receipt or receipt.get("status") != "0x1":
                return None, None
            confs: int | None = None
            try:
                bn = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber", "params": []})
                latest = int((bn.json() or {}).get("result") or "0x0", 16)
                txblk = int(receipt.get("blockNumber") or txn.get("blockNumber") or "0x0", 16)
                if latest and txblk:
                    confs = max(0, latest - txblk + 1)
            except Exception:  # noqa: BLE001 — confirmations are optional
                confs = None
            return value, confs
    except Exception:  # noqa: BLE001 — node down / bad txid → fall back silently
        return None, None


async def avax_deposit_check(session: AsyncSession, payment: Payment) -> dict:
    """Decision aid for the panel's MANUAL AVAX confirmation: read the actual native AVAX deposited
    for this payment's txid (free Avalanche C-Chain RPC), convert at the derived AVAX→Toman rate, and
    compare (within tolerance) to the invoice amount. Display-only — never auto-confirms. Best-effort:
    returns {'available': False} if the chain can't be read, so the panel just shows the link."""
    if payment.chain != "avax" or not payment.txid:
        return {"available": False}
    cfg = await settings_service.get_many(
        session, ["avax_address", "avalanche_rpc_url", "avax_amount_tolerance_pct"])
    received, confs = await _avax_received(
        payment.txid, cfg.get("avax_address") or "", cfg.get("avalanche_rpc_url") or "")
    if received is None:
        return {"available": False}
    avax_rate = await rates.get_avax_toman(session)  # int, 0 if unavailable
    received_toman = float(received) * avax_rate if avax_rate else 0.0
    invoice_toman = await _settled_amount_toman(session, payment)
    tol_pct = float(cfg.get("avax_amount_tolerance_pct") or 0)
    match: bool | None = None
    if invoice_toman > 0 and received_toman > 0:
        diff_pct = abs(received_toman - invoice_toman) / invoice_toman * 100
        match = diff_pct <= tol_pct
    return {
        "available": True,
        "received_avax": round(float(received), 4),
        "received_toman": round(received_toman),
        "invoice_toman": round(invoice_toman),
        "avax_rate": avax_rate,
        "tolerance_pct": tol_pct,
        "confirmations": confs,
        "match": match,
    }


async def deposit_check(session: AsyncSession, payment: Payment) -> dict:
    """Unified on-chain deposit read for the panel + bot. Dispatches by the payment's chain to the
    free TON (toncenter), AVAX (Avalanche RPC), or USDT (BSC RPC) reader and tags the result with
    `kind`. Display-only — never auto-confirms. {'available': False, 'kind': 'none'} when there's
    nothing to read."""
    if not payment.txid:
        return {"available": False, "kind": "none"}
    if payment.chain == "ton":
        d = await ton_deposit_check(session, payment)
        d["kind"] = "ton" if d.get("available") else "none"
        return d
    if payment.chain == "avax":
        d = await avax_deposit_check(session, payment)
        d["kind"] = "avax" if d.get("available") else "none"
        return d
    # chain "bsc" or legacy "" with a txid → USDT/BEP-20
    d = await usdt_deposit_check(session, payment)
    d["kind"] = "usdt" if d.get("available") else "none"
    return d


async def _bscscan_tokentx(
    api_url: str, api_key: str, wallet: str, contract: str, txid: str
) -> _ChainCheck:
    """Look up the USDT token transfers for our wallet and find the matching tx."""
    params: dict[str, str | int] = {
        "module": "account",
        "action": "tokentx",
        "address": wallet,
        "contractaddress": contract,
        "page": 1,
        "offset": 100,
        "sort": "desc",
        "apikey": api_key,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, params=params)
        resp.raise_for_status()
        data = resp.json()
    if str(data.get("status")) != "1" and not isinstance(data.get("result"), list):
        return _ChainCheck(False, None, None, Decimal(0), 0, error=str(data.get("message") or data.get("result")))
    for tx in data.get("result", []):
        if (tx.get("hash") or "").lower() == txid.lower():
            raw = Decimal(str(tx.get("value", "0")))
            amount = raw / (Decimal(10) ** USDT_DECIMALS)
            return _ChainCheck(
                found=True,
                to_address=(tx.get("to") or "").lower(),
                from_address=(tx.get("from") or "").lower(),
                amount_usdt=amount,
                confirmations=int(tx.get("confirmations", 0) or 0),
                contract_address=(tx.get("contractAddress") or "").lower(),
            )
    return _ChainCheck(False, None, None, Decimal(0), 0, error="transaction not found for this wallet")


async def verify_payment(
    session: AsyncSession, payment_id: int, *, notify_reseller: bool = False
) -> PaymentResult:
    """Verify a pending TXID payment on-chain and apply it. Idempotent-ish.
    `notify_reseller=True` (panel-triggered) sends the confirmation to the reseller's
    Telegram; the bot path leaves it False because it answers the chat inline."""
    # Lock the row (Postgres) and re-check status under the lock so a concurrent
    # verify/confirm can't double-settle the same payment. No-op on SQLite (tests).
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "پرداخت یافت نشد.")
    if payment.status == PaymentStatus.confirmed:
        return PaymentResult("confirmed", True, "این پرداخت قبلاً تأیید شده است.")

    # On-chain verify is BSC/USDT-only. A non-BSC (TON / AVAX) hash must never be looked up on
    # BscScan — it would never be found and the message would be misleading. Hold for manual review.
    if payment.chain and payment.chain not in ("bsc", ""):
        return PaymentResult(
            "pending", False,
            "بررسی خودکار فقط برای USDT است؛ این پرداخت را به‌صورت دستی بررسی و تأیید کنید.",
        )

    cfg = await settings_service.get_many(
        session,
        ["bscscan_api_key", "bscscan_api_url", "usdt_bep20_address", "usdt_bep20_contract",
         "min_confirmations", "payment_amount_tolerance_usdt"],
    )
    api_key = cfg.get("bscscan_api_key") or ""
    wallet = (cfg.get("usdt_bep20_address") or "").lower()
    contract = cfg.get("usdt_bep20_contract") or ""
    min_conf = int(cfg.get("min_confirmations") or 0)
    tolerance = Decimal(str(cfg.get("payment_amount_tolerance_usdt") or 0))

    if not api_key or not wallet or not contract:
        # Can't safely auto-verify without all three (a blank token contract would let a
        # worthless-token transfer to our wallet pass as USDT) — leave pending for manual review.
        return PaymentResult(
            "pending", False,
            "✅ شناسه تراکنش دریافت شد و پس از بررسی توسط پشتیبانی تأیید می‌شود.",
            detail="bscscan api key, wallet, or USDT contract not configured",
        )

    try:
        api_url = str(cfg.get("bscscan_api_url") or "")
        if not payment.txid:
            return PaymentResult("rejected", False, "شناسه تراکنش ثبت نشده است.")
        check = await _bscscan_tokentx(api_url, api_key, wallet, contract, payment.txid)
    except Exception as exc:  # noqa: BLE001
        log.exception("on-chain lookup failed")
        return PaymentResult("pending", False,
                             "✅ شناسه تراکنش دریافت شد و در حال بررسی است.",
                             detail=f"lookup error: {exc}")

    payment.raw_json = json.dumps(check.__dict__, default=str)[:4000]

    if not check.found:
        payment.status = PaymentStatus.pending
        payment.note = check.error
        await session.commit()
        return PaymentResult("pending", False,
                             "تراکنش هنوز روی شبکه پیدا نشد. لطفاً چند دقیقه بعد دوباره تلاش کنید.",
                             detail=check.error or "")

    payment.from_address = check.from_address
    payment.to_address = check.to_address
    payment.confirmations = check.confirmations
    # Deliberately NOT overwriting payment.amount_usdt with the on-chain deposit: the payment's
    # amounts are the submission-time invoice-set sums (Toman/USDT pair must keep corresponding
    # in the panel). The deposit figure lives in raw_json and the deposit_check endpoint.

    if check.to_address != wallet:
        payment.status = PaymentStatus.rejected
        payment.note = "destination address mismatch"
        await session.commit()
        return PaymentResult("rejected", False, "❌ آدرس مقصد تراکنش با کیف پول ما مطابقت ندارد.")

    # The matched tx must be for the configured USDT token contract — otherwise a transfer of
    # some other (worthless) token to our wallet, with the same nominal value, would pass.
    if (check.contract_address or "") != contract.lower():
        payment.status = PaymentStatus.rejected
        payment.note = f"token contract mismatch: {check.contract_address}"
        await session.commit()
        return PaymentResult("rejected", False, "❌ توکن این تراکنش با USDT موردنظر مطابقت ندارد.")

    if check.confirmations < min_conf:
        payment.status = PaymentStatus.pending
        await session.commit()
        return PaymentResult("pending", False,
                             f"تراکنش یافت شد اما هنوز تأییدیه کافی ندارد ({check.confirmations}/{min_conf}).")

    # Settle the WHOLE set this payment covers — one transfer can pay several invoices. The
    # deposit must cover the SUM of the still-owed invoices in the set.
    set_ids = _settled_ids(payment) or ([payment.invoice_id] if payment.invoice_id else [])
    all_in_set = (
        (await session.execute(select(Invoice).where(Invoice.id.in_(set_ids)))).scalars().all()
        if set_ids else []
    )
    targets = [t for t in all_in_set if t.status in _OWED]
    if not targets:
        # No owed member. Auto-closing is only safe when the WHOLE set demonstrably exists and
        # is already PAID (settled meanwhile — e.g. by a manual confirm). Anything else — an id
        # missing from the DB (invoice deleted), or a member reverted to draft / canceled —
        # must go to MANUAL review: auto-confirming would burn the unique txid on a payment
        # that settled nothing, and the customer could never resubmit that hash against the
        # re-issued invoice (dup_confirmed blocks it forever).
        found_ids = {t.id for t in all_in_set}
        all_paid = (
            bool(all_in_set)
            and all(t.status == InvoiceStatus.paid for t in all_in_set)
            and not [i for i in set_ids if i not in found_ids]
        )
        if not all_paid:
            payment.status = PaymentStatus.pending
            marker = "[needs manual review: invoice unpayable]"
            if marker not in (payment.note or ""):
                payment.note = ((payment.note or "") + " " + marker).strip()
            await session.commit()
            return PaymentResult(
                "pending", False,
                "فاکتورهای این پرداخت اکنون قابل تسویه نیستند (پیش‌نویس/لغو/حذف‌شده)؛ "
                "پرداخت برای بررسیِ دستی نگه داشته شد." + _ref_line(payment.id),
            )
        payment.status = PaymentStatus.confirmed
        payment.verified_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
        return PaymentResult("confirmed", True,
                             "✅ پرداخت دریافت شد؛ بدهی فعالی برای این فاکتور(ها) نبود."
                             + _ref_line(payment.id))

    target_amt = sum((Decimal(str(t.amount_usdt or 0)) for t in targets), Decimal(0))
    # Safety net: never AUTO-confirm when the total is zero. If the conversion rate was 0 when
    # the invoice was generated (e.g. auto mode before a live rate was fetched), amount_usdt is
    # 0 and the "amount too low" floor below (anything < 0) could never fire — so a dust
    # transfer would clear the whole Toman debt. Hold it for the owner's manual review.
    if target_amt <= 0:
        payment.status = PaymentStatus.pending
        if "[needs manual review: zero invoice amount]" not in (payment.note or ""):
            payment.note = (payment.note or "") + " [needs manual review: zero invoice amount]"
        await session.commit()
        return PaymentResult(
            "pending", False,
            "مبلغ این فاکتور(ها) نامشخص است؛ پرداخت برای بررسیِ دستی ثبت شد.",
        )
    if check.amount_usdt + tolerance < target_amt:
        payment.status = PaymentStatus.rejected
        payment.note = f"amount too low: {check.amount_usdt} < {target_amt}"
        await session.commit()
        return PaymentResult(
            "rejected", False,
            f"❌ مبلغ واریزی ({check.amount_usdt:.2f} USDT) کمتر از مبلغ کلِ فاکتورها "
            f"({target_amt:.2f} USDT) است.",
        )

    await _mark_invoices_paid(session, targets, payment)
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    # Keep the full submission-time set in settled_invoice_ids (don't narrow it to the still-owed
    # subset); just keep the primary id pointing at a covered invoice.
    if set_ids:
        payment.invoice_id = set_ids[0]
        payment.settled_invoice_ids = ",".join(str(i) for i in set_ids)
        await _sync_settlements(session, payment)
    await session.commit()
    await _maybe_restore(
        session, await session.get(Reseller, targets[0].reseller_id),
        exclude_invoice_ids={t.id for t in targets},
    )

    period = _periods_label([t.period_label for t in targets])
    msg = await _payment_received_text(session, period, payment.id)
    if notify_reseller:
        r = await session.get(Reseller, targets[0].reseller_id)
        if r is not None:
            await notifier.send_to_reseller(session, r, msg, kind=DeliveryKind.payment_ack)
    return PaymentResult("confirmed", True, msg)


def _ref_line(payment_id: int | None) -> str:
    """Tracking-number footer so the customer can quote «شمارهٔ پیگیری #N» to support and the
    owner can find that exact payment in the panel. Shows the public 8-digit code (not the raw
    sequential id), so the payment count isn't leaked."""
    from app.core.codes import payment_code

    return f"\n🔖 شمارهٔ پیگیری: #{payment_code(payment_id)}" if payment_id else ""


def _periods_label(periods: list[str]) -> str:
    """A readable label for the period(s) a payment settled: a single «2026-02», or
    «2026-01، 2026-02» for several — used in the customer's confirm/reject acknowledgements."""
    clean = [p.strip() for p in periods if p and p.strip()]
    return "، ".join(clean)


async def _payment_received_text(session: AsyncSession, period: str, code: int | None = None) -> str:
    from app.bot import texts

    # «—» when there's no linked invoice, so the template never renders «فاکتور دوره  …» (a
    # dangling double space).
    period = (period or "").strip() or "—"
    return await texts.render(session, "tpl_payment_received", period=period) + _ref_line(code)


async def _payment_rejected_text(session: AsyncSession, period: str, code: int | None = None) -> str:
    from app.bot import texts

    period = (period or "").strip() or "—"
    return await texts.render(session, "tpl_payment_rejected", period=period) + _ref_line(code)


def _settled_ids(payment: Payment) -> list[int]:
    """The invoice ids a payment covers/has settled (from settled_invoice_ids, else invoice_id)."""
    if payment.settled_invoice_ids:
        return [int(x) for x in payment.settled_invoice_ids.split(",") if x.strip().isdigit()]
    return [payment.invoice_id] if payment.invoice_id else []


async def _settled_amount_toman(session: AsyncSession, payment: Payment) -> float:
    """Sum of Toman across every invoice this payment covers (for the on-chain decision aid)."""
    ids = _settled_ids(payment)
    if not ids:
        return 0.0
    rows = (await session.execute(select(Invoice).where(Invoice.id.in_(ids)))).scalars().all()
    return float(sum(Decimal(str(inv.amount_toman or 0)) for inv in rows))


async def _settled_amount_usdt(session: AsyncSession, payment: Payment) -> float:
    """Sum of USDT across every invoice this payment covers (for the on-chain decision aid)."""
    ids = _settled_ids(payment)
    if not ids:
        return 0.0
    rows = (await session.execute(select(Invoice).where(Invoice.id.in_(ids)))).scalars().all()
    return float(sum(Decimal(str(inv.amount_usdt or 0)) for inv in rows))


async def _settled_by_other_confirmed(
    session: AsyncSession, invoice_id: int, exclude_payment_id: int
) -> bool:
    """True if a DIFFERENT confirmed payment also settled this invoice. Reversing/deleting one
    payment must not un-pay an invoice that another confirmed payment still settles — otherwise
    rejecting a duplicate/mis-clicked payment would wrongly mark a genuinely-paid invoice owed.
    Single indexed EXISTS-style query on the settlements table."""
    row = (
        await session.execute(
            select(PaymentSettlement.payment_id)
            .join(Payment, Payment.id == PaymentSettlement.payment_id)
            .where(
                Payment.status == PaymentStatus.confirmed,
                Payment.id != exclude_payment_id,
                PaymentSettlement.invoice_id == invoice_id,
            )
            .limit(1)
        )
    ).scalars().first()
    return row is not None


async def _revert_settled_invoices(
    session: AsyncSession, payment: Payment
) -> None:
    """Revert the invoices a (confirmed) payment settled back to owed — UNLESS another
    confirmed payment still settles them. Reverted invoices get a fresh dunning cycle and the
    stale txid cleared from the ledger."""
    from app.services import dunning

    ids = _settled_ids(payment)
    if not ids:
        return
    rows = (
        await session.execute(select(Invoice).where(Invoice.id.in_(ids)))
    ).scalars().all()
    for inv in rows:
        if inv.status != InvoiceStatus.paid:
            continue
        if await _settled_by_other_confirmed(session, inv.id, payment.id):
            continue  # still settled elsewhere — leave it paid
        inv.status = InvoiceStatus.sent
        inv.paid_at = None
        await dunning.reset_cycle(session, inv, restamp_sent_at=True)
        r = await session.get(Reseller, inv.reseller_id)
        # record() clears the stale txid because the invoice is no longer paid.
        await financial_archive.record(session, inv, reseller=r)


async def _mark_invoices_paid(
    session: AsyncSession, invoices: list[Invoice], payment: Payment
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for inv in invoices:
        # Only an OWED invoice may be marked paid. Guarding here (not just in the caller)
        # protects EVERY settlement path: confirming a stale payment whose linked invoice was
        # meanwhile reverted to draft / canceled / already paid must NOT resurrect it as paid
        # or write a duplicate ledger row. (verify_payment also guards before calling.)
        if inv.status not in _OWED:
            continue
        inv.status = InvoiceStatus.paid
        inv.paid_at = now
        reseller = await session.get(Reseller, inv.reseller_id)
        await financial_archive.record(session, inv, reseller=reseller, txid=payment.txid)


async def record_manual_payment(session: AsyncSession, invoice: Invoice) -> Payment:
    """Create the confirmed `manual` Payment row behind a panel «ثبت پرداخت» (mark-paid).

    Marking an invoice paid by hand used to leave NO payment row, so
    `_settled_by_other_confirmed` couldn't see the settlement — rejecting an unrelated pending
    payment that also covered the invoice would wrongly un-pay it. The row makes the manual
    settlement first-class: visible in the panel's payments list and protective on revert.
    Does not commit; the caller owns the transaction."""
    payment = Payment(
        reseller_id=invoice.reseller_id,
        invoice_id=invoice.id,
        settled_invoice_ids=str(invoice.id),
        amount_toman=float(invoice.amount_toman or 0),
        amount_usdt=float(invoice.amount_usdt or 0),
        method=PaymentMethod.manual,
        status=PaymentStatus.confirmed,
        verified_at=dt.datetime.now(dt.timezone.utc),
        note="ثبت دستی از پنل",
    )
    session.add(payment)
    await session.flush()
    await _sync_settlements(session, payment)
    return payment


async def retire_manual_payments(session: AsyncSession, invoice: Invoice) -> int:
    """Reject the confirmed single-invoice `manual` rows covering this invoice (used by
    unmark-paid). A confirmed manual row must not outlive its un-paid invoice — it would
    wrongly shield the invoice from a later revert via `_settled_by_other_confirmed`.
    Rows covering OTHER invoices too are left alone (the owner manages those explicitly).
    Does not commit; returns the number of rows retired."""
    rows = (
        await session.execute(
            select(Payment).where(
                Payment.method == PaymentMethod.manual,
                Payment.status == PaymentStatus.confirmed,
                Payment.invoice_id == invoice.id,
            )
        )
    ).scalars().all()
    n = 0
    for p in rows:
        if set(_settled_ids(p)) != {invoice.id}:
            continue
        p.status = PaymentStatus.rejected
        p.verified_at = None
        if "[unmarked from panel]" not in (p.note or ""):
            p.note = ((p.note or "") + " [unmarked from panel]").strip()
        n += 1
    return n


async def confirm_manually(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Owner override: mark a payment confirmed (without on-chain verification) for ALL the
    invoices it covers — a payment may settle several invoices (one transfer for several debts).

    Reversible: works on a previously rejected payment too (recovers a mis-click). The reseller
    is notified only when the status actually CHANGES to confirmed (re-confirming an already-
    confirmed payment is silent), so a double-click doesn't spam them.
    """
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    was_confirmed = payment.status == PaymentStatus.confirmed
    reseller = await session.get(Reseller, payment.reseller_id)
    set_ids = _settled_ids(payment)
    rows = (
        (await session.execute(select(Invoice).where(Invoice.id.in_(set_ids)))).scalars().all()
        if set_ids else []
    )
    # Keep the STORED set order (the IN(...) select returns DB order): the first id is the
    # primary invoice shown in the panel, and a manual confirm must not silently reshuffle it.
    _by_id = {inv.id: inv for inv in rows}
    all_in_set = [_by_id[i] for i in set_ids if i in _by_id]
    # Don't "confirm" a payment whose invoices can't actually be settled (any reverted to draft
    # or canceled) — that would leave the payment marked confirmed while an invoice stays unpaid,
    # misleading the owner. Tell them to fix the invoice first; leave the payment pending.
    if any(inv.status in (InvoiceStatus.draft, InvoiceStatus.canceled) for inv in all_in_set):
        return PaymentResult(
            "pending", False,
            "یک یا چند فاکتورِ مرتبط در وضعیتِ پیش‌نویس/لغوشده است؛ ابتدا آن را صادر یا اصلاح کنید.",
        )
    targets = [inv for inv in all_in_set if inv.status in _OWED]

    await _mark_invoices_paid(session, targets, payment)
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    if all_in_set:
        payment.invoice_id = all_in_set[0].id
        payment.settled_invoice_ids = ",".join(str(inv.id) for inv in all_in_set)
        await _sync_settlements(session, payment)
        if not payment.amount_usdt:
            payment.amount_usdt = float(
                sum(Decimal(str(inv.amount_usdt or 0)) for inv in all_in_set)
            )
    if "[manually confirmed]" not in (payment.note or ""):
        payment.note = (payment.note or "") + " [manually confirmed]"
    await session.commit()

    if all_in_set:
        await _maybe_restore(
            session, await session.get(Reseller, all_in_set[0].reseller_id),
            exclude_invoice_ids={inv.id for inv in all_in_set},
        )

    if reseller is not None and not was_confirmed:
        period = _periods_label([inv.period_label for inv in all_in_set])
        await notifier.send_to_reseller(
            session, reseller, await _payment_received_text(session, period, payment.id),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("confirmed", True, "Confirmed")


async def reject_payment(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Owner rejects a payment. Reversible: if this payment had previously CONFIRMED one or
    more invoices (a mis-click, or a change of mind), EVERY invoice it settled is reverted to
    owed (unpaid) and the ledger updated, so the accounting stays consistent. The reseller is
    notified that their payment wasn't accepted — but only on a real state CHANGE to rejected
    (re-rejecting is silent), so toggling/double-clicks don't spam them. An already-enforced
    reseller is NOT re-suspended automatically — dunning re-escalates on its normal timeline,
    or the owner suspends manually."""
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    was_rejected = payment.status == PaymentStatus.rejected
    was_confirmed = payment.status == PaymentStatus.confirmed
    reseller = await session.get(Reseller, payment.reseller_id)
    set_ids = _settled_ids(payment)
    invoices = (
        (await session.execute(select(Invoice).where(Invoice.id.in_(set_ids)))).scalars().all()
        if set_ids else []
    )
    payment.status = PaymentStatus.rejected
    payment.verified_at = None
    if was_confirmed:
        await _revert_settled_invoices(session, payment)
    await session.commit()
    # Tell the customer their payment wasn't accepted — but only on a real state change
    # (so toggling reject→confirm→reject, or a double-click, doesn't spam them).
    if reseller is not None and not was_rejected:
        period = _periods_label([inv.period_label for inv in invoices])
        await notifier.send_to_reseller(
            session, reseller, await _payment_rejected_text(session, period, payment.id),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("rejected", False, "Rejected")


async def delete_payment(session: AsyncSession, payment_id: int) -> bool:
    """Delete a payment row entirely (e.g. to clean up test data).

    If the payment had CONFIRMED an invoice, that invoice is first reverted to owed (and the
    ledger updated) so we never leave a 'paid' invoice with no payment behind it. The proof
    image file, if any, is removed too. Returns False if the payment doesn't exist.
    """
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return False
    if payment.status == PaymentStatus.confirmed:
        await _revert_settled_invoices(session, payment)
    if payment.proof_path and os.path.exists(payment.proof_path):
        try:
            os.remove(payment.proof_path)
        except OSError:
            log.warning("failed to remove proof file %s", payment.proof_path, exc_info=True)
    # Postgres also removes these via ON DELETE CASCADE; delete explicitly so the mirror
    # is correct on every backend (SQLite test runs don't enforce FK cascades).
    await session.execute(
        delete(PaymentSettlement).where(PaymentSettlement.payment_id == payment.id)
    )
    await session.delete(payment)
    await session.commit()
    return True
