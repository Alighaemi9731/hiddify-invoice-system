"""Lowercase hex-form TON txids (data-only) — hardening H06.

A TON tx hash in HEX form is case-insensitive on-chain, but payment rows stored it
case-sensitively — so one real transfer submitted as `ABC…` and `abc…` created two distinct
rows under the unique `payments.txid` index and could each settle invoices (double-credit via
a hurried manual confirm). Submission now lowercases hex-form TON hashes; this migration
canonicalizes the existing rows.

On a `lower(txid)` collision (both casings already present = the same on-chain transfer twice)
the more-settled row (confirmed > pending > rejected; tie → lower id) keeps the canonical
lowercase txid; the loser's txid is set NULL (the unique index allows multiple NULLs) and
note-tagged. A migration NEVER changes payment statuses — two already-CONFIRMED duplicates
(an already-materialized double-credit) are only tagged for the owner to resolve, with a
warning logged. Base64(url) TON forms are case-sensitive and are left untouched.

Downgrade is a documented no-op (original casing can't be recovered; rollback = DB restore).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d4f6b8a1e3"
down_revision: str | None = "b1c3e5a7f9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.h06")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_STATUS_RANK = {"confirmed": 3, "pending": 2, "rejected": 1}


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, txid, status, note FROM payments "
            "WHERE (chain = 'ton' OR method = 'ton_txid') AND txid IS NOT NULL"
        )
    ).fetchall()

    # Group hex-form TON rows by their lowercase txid.
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        txid = r[1] or ""
        if _HEX64.match(txid):
            groups[txid.lower()].append(r)

    for lower_txid, members in groups.items():
        # Already-canonical single row → nothing to do.
        if len(members) == 1 and members[0][1] == lower_txid:
            continue
        if len(members) == 1:
            conn.execute(
                sa.text("UPDATE payments SET txid = :t WHERE id = :i"),
                {"t": lower_txid, "i": members[0][0]},
            )
            continue

        # Collision: keep the most-settled row on the canonical txid, NULL the losers.
        members.sort(key=lambda m: (_STATUS_RANK.get(m[2], 0), m[0]), reverse=True)
        keeper = members[0]
        losers = members[1:]
        confirmed = [m for m in members if m[2] == "confirmed"]
        if len(confirmed) > 1:
            ids = ", ".join(f"#{m[0]}" for m in confirmed)
            log.warning(
                "H06: duplicate CONFIRMED TON txid %s across payments %s "
                "(already-materialized double-credit) — tagging for manual review, "
                "statuses left unchanged", lower_txid, ids,
            )
            for m in confirmed:
                _tag_note(conn, m[0], m[3], "[review: duplicate confirmed TON txid]")

        conn.execute(
            sa.text("UPDATE payments SET txid = :t WHERE id = :i"),
            {"t": lower_txid, "i": keeper[0]},
        )
        for m in losers:
            _tag_note(conn, m[0], m[3], f"[duplicate txid; case-merged into #{keeper[0]}]")
            conn.execute(
                sa.text("UPDATE payments SET txid = NULL WHERE id = :i"), {"i": m[0]}
            )


def _tag_note(conn, payment_id: int, note: str | None, tag: str) -> None:
    if tag in (note or ""):
        return
    conn.execute(
        sa.text("UPDATE payments SET note = :n WHERE id = :i"),
        {"n": ((note or "") + " " + tag).strip(), "i": payment_id},
    )


def downgrade() -> None:
    # Original txid casing cannot be reconstructed. Rollback = restore the DB backup.
    pass
