"""Storefront wallet ledger (reseller↔customer money — separate from owner↔reseller invoices).

A customer tops up (status `pending` until the reseller-admin confirms → balance credited), then buys
a plan (atomic debit). Manual admin adjustments and refunds are recorded too. The denormalized
`StorefrontCustomer.wallet_balance_toman` is the source of truth for spendable balance; every change
also writes a `StorefrontWalletTxn` row for audit.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontCustomer, StorefrontWalletTxn

# Crypto top-up methods whose txid is replay-protected (normalized + tenant-scoped unique).
_CRYPTO_METHODS = ("usdt", "ton")


class DuplicateTopupTxid(Exception):
    """A crypto top-up txid was already submitted for this storefront (replay attempt)."""


def _norm_txid(method: str | None, txid: str | None) -> str | None:
    """Normalize a crypto txid so casing variants of ONE transfer collide (BSC/hex are matched
    case-insensitively on-chain). Non-crypto references are left as-is."""
    if txid and (method or "") in _CRYPTO_METHODS:
        return txid.strip().lower()
    return txid


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def balance(customer: StorefrontCustomer) -> Decimal:
    return Decimal(str(customer.wallet_balance_toman or 0))


async def create_topup(
    session: AsyncSession,
    customer: StorefrontCustomer,
    amount_toman: int,
    *,
    method: str,
    proof_path: str | None = None,
    txid: str | None = None,
    chain: str | None = None,
    credit_code_id: int | None = None,
) -> StorefrontWalletTxn:
    """Record a PENDING wallet top-up. Balance is NOT credited until the reseller-admin confirms. A
    captured `credit_code_id` (کد شارژ) is redeemed + its bonus credited only at confirmation.

    A crypto txid is normalized (lowercased) and stamped with the customer's storefront so a
    tenant-scoped partial-unique index rejects a REPLAY of the same deposit. `DuplicateTopupTxid`
    is raised if the same normalized txid was already submitted for this shop."""
    norm_txid = _norm_txid(method, txid)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, storefront_bot_id=customer.storefront_bot_id,
        kind="topup", amount_toman=Decimal(str(int(amount_toman))),
        status="pending", method=method, proof_path=proof_path, txid=norm_txid, chain=chain,
        credit_code_id=credit_code_id,
    )
    session.add(txn)
    try:
        await session.commit()
    except IntegrityError as exc:  # tenant-scoped unique txid → this deposit was already submitted
        await session.rollback()
        raise DuplicateTopupTxid() from exc
    return txn


async def _get_for_update(session: AsyncSession, model, pk: int):  # noqa: ANN001, ANN202
    """Fetch a row by id under a write lock (Postgres `SELECT … FOR UPDATE`; a no-op on SQLite,
    where writes serialize anyway). Lets two admins decide the same top-up without a double credit."""
    stmt = select(model).where(model.id == pk)
    try:
        stmt = stmt.with_for_update()
    except Exception:  # noqa: BLE001 — dialect without row locks
        pass
    return (await session.execute(stmt)).scalar_one_or_none()


async def confirm_topup(
    session: AsyncSession, txn_id: int, *, expected_storefront_bot_id: int,
    amount_toman: int | None = None,
) -> tuple[bool, StorefrontWalletTxn | None]:
    """Confirm a pending top-up and credit the wallet. `amount_toman` lets the admin set the credited
    Toman (used for crypto deposits — manual, no rates). Atomic + idempotent: the txn row is locked
    and re-checked, so if a second admin taps «تأیید» concurrently they block until the first commits
    and then see it's no longer pending → a no-op (never a double credit). Returns (changed, txn).

    `expected_storefront_bot_id` enforces TENANT ISOLATION (F3): txn ids are a global sequence and
    the callback data is client-controllable, so an admin may only decide a top-up whose customer
    belongs to THEIR OWN storefront — otherwise a shop A admin could credit/re-amount shop B's deposit."""
    txn = await _get_for_update(session, StorefrontWalletTxn, txn_id)
    if txn is None or txn.kind != "topup":
        return False, txn
    if txn.status != "pending":
        return False, txn  # already decided — no double credit
    # Lock the customer row too, so two DIFFERENT top-ups for the same wallet can't lose an update.
    customer = await _get_for_update(session, StorefrontCustomer, txn.customer_id)
    if customer is None:
        return False, txn
    if customer.storefront_bot_id != expected_storefront_bot_id:
        return False, txn  # cross-tenant attempt — never touch another shop's money
    # The admin may override the Toman to credit (esp. for crypto deposits, where the amount is set
    # manually — the bot never depends on online rates). Otherwise credit the requested amount.
    credited = Decimal(str(int(amount_toman))) if amount_toman is not None else Decimal(
        str(txn.amount_toman or 0)
    )
    if credited < 0:
        credited = Decimal(0)
    # A captured credit code (کد شارژ) is redeemed HERE (not at request time): lock + re-validate
    # against the actually-credited base, so an expired/exhausted code between request and confirm
    # simply yields no bonus. The bonus is a SEPARATE `credit_bonus` ledger row (the top-up row stays
    # equal to the paid amount).
    from app.services import storefront_credit

    q = None
    if txn.credit_code_id:
        q = await storefront_credit.reserve_by_id(
            session, txn.credit_code_id, storefront_bot_id=customer.storefront_bot_id,
            topup_toman=int(credited), customer_id=customer.id)
    customer.wallet_balance_toman = float(balance(customer) + credited)
    txn.amount_toman = float(credited)
    txn.status = "confirmed"
    txn.decided_at = _now()
    if q is not None and q.ok and q.bonus_toman > 0 and q.code_id is not None:
        bonus_txn = StorefrontWalletTxn(
            customer_id=customer.id, kind="credit_bonus", amount_toman=Decimal(str(q.bonus_toman)),
            status="done", note="بونوسِ کدِ شارژ", decided_at=_now())
        session.add(bonus_txn)
        await session.flush()
        customer.wallet_balance_toman = float(balance(customer) + q.bonus_toman)
        await storefront_credit.record(session, q.code_id, customer.id, bonus_txn.id, q.bonus_toman)
    await session.commit()
    return True, txn


async def apply_gift_code(session: AsyncSession, customer_id: int, storefront_bot_id: int, code_str: str):  # noqa: ANN201
    """Standalone «کدِ هدیه» redemption (NO payment): lock the customer + code, validate, credit the
    gift + record the redemption atomically. Returns the storefront_credit.CreditQuote (q.ok/q.reason/
    q.bonus_toman). Only a code flagged `is_gift` yields credit here (a bonus code → q.reason=min_not_met)."""
    from app.services import storefront_credit

    customer = await _get_for_update(session, StorefrontCustomer, customer_id)
    if customer is None:
        return storefront_credit.CreditQuote(False, "not_found")
    q = await storefront_credit.reserve(
        session, storefront_bot_id, code_str, topup_toman=0, customer_id=customer_id)
    if not q.ok or q.bonus_toman <= 0 or q.code_id is None:
        await session.rollback()
        return q
    gift_txn = StorefrontWalletTxn(
        customer_id=customer_id, kind="credit_bonus", amount_toman=Decimal(str(q.bonus_toman)),
        status="done", note="کدِ هدیه", decided_at=_now())
    session.add(gift_txn)
    await session.flush()
    customer.wallet_balance_toman = float(balance(customer) + q.bonus_toman)
    await storefront_credit.record(session, q.code_id, customer_id, gift_txn.id, q.bonus_toman)
    await session.commit()
    return q


async def reject_topup(
    session: AsyncSession, txn_id: int, *, expected_storefront_bot_id: int
) -> tuple[bool, StorefrontWalletTxn | None]:
    """Reject a pending top-up (no credit). Atomic + idempotent (locks + re-checks the txn row so a
    concurrent confirm/reject by another admin can't both act). `expected_storefront_bot_id` enforces
    tenant isolation (F3), same as confirm_topup. Returns (changed, txn)."""
    txn = await _get_for_update(session, StorefrontWalletTxn, txn_id)
    if txn is None or txn.kind != "topup":
        return False, txn
    if txn.status != "pending":
        return False, txn
    customer = await _get_for_update(session, StorefrontCustomer, txn.customer_id)
    if customer is None or customer.storefront_bot_id != expected_storefront_bot_id:
        return False, txn  # missing / cross-tenant — refuse
    txn.status = "rejected"
    txn.decided_at = _now()
    await session.commit()
    return True, txn


async def manual_adjust(
    session: AsyncSession, customer: StorefrontCustomer, amount_toman_signed: int, *, note: str = ""
) -> StorefrontWalletTxn:
    """Admin manually credits (+) or debits (−) a customer's wallet. Never drives the balance below 0.

    Re-reads the customer under a row lock before the read-modify-write so two admins adjusting
    the same wallet at once (realistic now that co-admins share the panel) can't lose an update."""
    locked = await _get_for_update(session, StorefrontCustomer, customer.id)
    if locked is not None:
        customer = locked
    delta = Decimal(str(int(amount_toman_signed)))
    new_balance = balance(customer) + delta
    if new_balance < 0:
        new_balance = Decimal(0)
        delta = new_balance - balance(customer)
    customer.wallet_balance_toman = float(new_balance)
    txn = StorefrontWalletTxn(
        customer_id=customer.id,
        kind="manual_credit" if delta >= 0 else "manual_debit",
        amount_toman=delta, status="done", method="manual", note=(note or "")[:255],
        decided_at=_now(),
    )
    session.add(txn)
    await session.commit()
    return txn


async def charge_purchase(
    session: AsyncSession, customer_id: int, price_toman: int, *, order_id: int | None = None,
    operation_id: int | None = None,
) -> tuple[bool, StorefrontWalletTxn | None]:
    """Atomically debit the wallet for a purchase. Re-reads the customer under a row lock (Postgres)
    so two concurrent buys can't overspend. Returns (ok, debit_txn). ok=False if the balance is short.
    Does NOT commit — the caller commits the debit together with the order it belongs to.

    `operation_id` links the debit to its durable StorefrontOperation (F4/F11) so the reconciler can
    tell whether a crashed renewal actually charged and reverse it exactly once."""
    price = Decimal(str(int(price_toman)))
    stmt = select(StorefrontCustomer).where(StorefrontCustomer.id == customer_id)
    try:
        stmt = stmt.with_for_update()
    except Exception:  # noqa: BLE001 — sqlite has no row locks; the test path is single-threaded
        pass
    customer = (await session.execute(stmt)).scalar_one_or_none()
    if customer is None or balance(customer) < price:
        return False, None
    customer.wallet_balance_toman = float(balance(customer) - price)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, kind="purchase", amount_toman=-price, status="done",
        order_id=order_id, operation_id=operation_id, decided_at=_now(),
    )
    session.add(txn)
    await session.flush()
    return True, txn


async def refund(
    session: AsyncSession, customer_id: int, amount_toman: int, *, order_id: int | None = None,
    note: str = "",
) -> StorefrontWalletTxn | None:
    """Credit a refund back to the wallet (e.g. provisioning failed after the debit). Does NOT commit.

    Locks the customer row (F5) so a refund can't lose a concurrent debit's update; a partial-unique
    index on (order_id) WHERE kind='refund' is the durable backstop against a double refund per order."""
    customer = await _get_for_update(session, StorefrontCustomer, customer_id)
    if customer is None:
        return None
    amt = Decimal(str(int(amount_toman)))
    customer.wallet_balance_toman = float(balance(customer) + amt)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, storefront_bot_id=customer.storefront_bot_id,
        kind="refund", amount_toman=amt, status="done",
        order_id=order_id, note=(note or "")[:255], decided_at=_now(),
    )
    session.add(txn)
    await session.flush()
    return txn


async def reverse_charge(
    session: AsyncSession, customer_id: int, amount_toman: int, *, order_id: int | None = None,
    operation_id: int | None = None, note: str = "",
) -> StorefrontWalletTxn | None:
    """Credit back a renewal charge whose panel write FAILED or crashed (F11 compensation). Uses a
    DISTINCT kind ('renew_reversal') — NOT 'refund' — so it never collides with the one-refund-per-order
    index. EXACTLY-ONCE per operation: locks the customer, checks `operation_has_reversal` first, and the
    partial-unique `uq_sfwallet_reversal_per_operation` (WHERE kind='renew_reversal') is the durable
    backstop so the inline handler and the reconciler can't both credit. Returns None if already reversed.
    Does NOT commit."""
    customer = await _get_for_update(session, StorefrontCustomer, customer_id)
    if customer is None:
        return None
    if operation_id is not None and await operation_has_reversal(session, operation_id):
        return None  # already compensated — idempotent
    amt = Decimal(str(int(amount_toman)))
    customer.wallet_balance_toman = float(balance(customer) + amt)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, storefront_bot_id=customer.storefront_bot_id,
        kind="renew_reversal", amount_toman=amt, status="done",
        order_id=order_id, operation_id=operation_id,
        note=(note or "بازگشتِ وجهِ تمدیدِ ناموفق")[:255], decided_at=_now(),
    )
    session.add(txn)
    await session.flush()
    return txn


async def operation_has_charge(session: AsyncSession, operation_id: int) -> bool:
    """True if this operation's wallet was debited (a `purchase` row carries its operation_id) — so the
    reconciler knows a crashed renewal actually charged and must be reversed."""
    row = (
        await session.execute(
            select(StorefrontWalletTxn.id).where(
                StorefrontWalletTxn.operation_id == operation_id,
                StorefrontWalletTxn.kind == "purchase",
            ).limit(1)
        )
    ).first()
    return row is not None


async def operation_has_reversal(session: AsyncSession, operation_id: int) -> bool:
    """True if this operation's charge was already reversed — so a reversal is never applied twice."""
    row = (
        await session.execute(
            select(StorefrontWalletTxn.id).where(
                StorefrontWalletTxn.operation_id == operation_id,
                StorefrontWalletTxn.kind == "renew_reversal",
            ).limit(1)
        )
    ).first()
    return row is not None


async def order_has_refund(session: AsyncSession, order_id: int) -> bool:
    """True if this order's purchase was already refunded — so the reaper never double-refunds."""
    row = (
        await session.execute(
            select(StorefrontWalletTxn.id).where(
                StorefrontWalletTxn.order_id == order_id,
                StorefrontWalletTxn.kind == "refund",
            ).limit(1)
        )
    ).first()
    return row is not None


async def pending_topups_for_bot(
    session: AsyncSession, storefront_bot_id: int
) -> list[StorefrontWalletTxn]:
    """All pending top-ups across this storefront's customers (for the admin review queue)."""
    rows = (
        await session.execute(
            select(StorefrontWalletTxn)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .where(
                StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                StorefrontWalletTxn.kind == "topup",
                StorefrontWalletTxn.status == "pending",
            )
            .order_by(StorefrontWalletTxn.created_at)
        )
    ).scalars().all()
    return list(rows)


async def pending_topups_for_customer(
    session: AsyncSession, customer_id: int
) -> list[StorefrontWalletTxn]:
    """This customer's own pending top-ups (shown on their wallet screen)."""
    rows = (
        await session.execute(
            select(StorefrontWalletTxn).where(
                StorefrontWalletTxn.customer_id == customer_id,
                StorefrontWalletTxn.kind == "topup",
                StorefrontWalletTxn.status == "pending",
            ).order_by(StorefrontWalletTxn.created_at)
        )
    ).scalars().all()
    return list(rows)
