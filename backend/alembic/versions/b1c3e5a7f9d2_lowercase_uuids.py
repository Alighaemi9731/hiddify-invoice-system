"""Lowercase all admin/user uuids (data-only) — hardening H05.

Billing compared uuids case-SENSITIVELY (`invoice_engine.build_children_map`/
`select_billable_roots`/`users_by_adder`, `metering`, `reseller_stats`, `gb_cap`) while the
resellers tree (M54) and the persisted-line PDF paths compare lowercase. A case-mismatched
`parent_admin_uuid` / `added_by_uuid` therefore detached a subtree from its billing bundle and
it was silently never billed. Ingest now lowercases uuids at the `parse_backup` choke point;
this migration canonicalizes the EXISTING rows so the two layers agree immediately.

Steps (data only — no schema change):
  1. Merge case-duplicate resellers (`(panel_id, LOWER(admin_uuid))` collisions — the unique
     constraint is case-sensitive so `ABC`/`abc` could coexist): keeper = more invoices →
     non-null bot_chat_id → newest last_seen_at → lowest id; repoint FK rows; copy override
     fields onto the keeper where NULL; delete the loser. Aborts loudly if two NON-draft
     invoices collide on one period (settled money is never silently merged).
  2. Dedup end_user_snapshots (keep latest last_synced_at) and usage_meters (keep the larger
     meter — never sum) on `(panel_id, LOWER(uuid))`.
  3. Lowercase the uuid columns on resellers / end_user_snapshots / usage_meters /
     invoice_lines.
  4. Rewrite the uuid keys inside live enforcement_actions.snapshot JSON.

Downgrade is a documented no-op (original casing can't be recovered; rollback = DB restore).
Rehearse against a restored clone of production Postgres before release.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c3e5a7f9d2"
down_revision: str | None = "a3c5e7b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables whose reseller_id points at resellers.id and must follow a merge.
_RESELLER_FK_TABLES = ("invoices", "payments", "delivery_log", "enforcement_actions")


def _ts(value) -> str:
    """A comparable string form of a timestamp that may be a datetime (Postgres) or an
    ISO string (SQLite). Empty string sorts oldest."""
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _invoice_is_nondraft(conn, invoice_id: int) -> bool:
    row = conn.execute(
        sa.text("SELECT status FROM invoices WHERE id = :i"), {"i": invoice_id}
    ).fetchone()
    return bool(row) and row[0] != "draft"


def _merge_resellers(conn) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT id, panel_id, admin_uuid, bot_chat_id, last_seen_at, "
            "price_per_gb, min_sale_toman, storefront_monthly_fee_toman "
            "FROM resellers"
        )
    ).fetchall()
    groups: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        groups[(r[1], (r[2] or "").lower())].append(r)

    for (panel_id, _lower), members in groups.items():
        if len(members) < 2:
            continue

        def _invoice_count(rid: int) -> int:
            return int(
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM invoices WHERE reseller_id = :r"),
                    {"r": rid},
                ).scalar()
                or 0
            )

        # Keeper: most invoices → has a bot_chat_id → newest last_seen_at → lowest id.
        def _key(m):
            return (
                _invoice_count(m[0]),
                1 if m[3] is not None else 0,
                _ts(m[4]),
                -m[0],
            )

        keeper = max(members, key=_key)
        keeper_id = keeper[0]
        for loser in members:
            if loser[0] == keeper_id:
                continue
            loser_id = loser[0]
            # Invoice period collisions: repointing would violate uq_invoice_period.
            keeper_periods = {
                (row[0], row[1]): row[2]
                for row in conn.execute(
                    sa.text(
                        "SELECT period_start, period_end, id FROM invoices "
                        "WHERE reseller_id = :r"
                    ),
                    {"r": keeper_id},
                ).fetchall()
            }
            for row in conn.execute(
                sa.text(
                    "SELECT period_start, period_end, id, status FROM invoices "
                    "WHERE reseller_id = :r"
                ),
                {"r": loser_id},
            ).fetchall():
                period = (row[0], row[1])
                loser_inv_id, loser_status = row[2], row[3]
                keeper_inv_id = keeper_periods.get(period)
                if keeper_inv_id is None:
                    continue
                keeper_nondraft = _invoice_is_nondraft(conn, keeper_inv_id)
                loser_nondraft = loser_status != "draft"
                if keeper_nondraft and loser_nondraft:
                    raise RuntimeError(
                        "H05 migration: reseller case-duplicates "
                        f"(panel {panel_id}, uuid {loser[2]!r}) both have a non-draft invoice "
                        f"for period {period[0]}..{period[1]} (keeper #{keeper_inv_id}, "
                        f"loser #{loser_inv_id}). Resolve by hand before upgrading."
                    )
                # Delete the redundant loser (or keeper draft) invoice + its lines/ledger.
                drop_id = loser_inv_id if not loser_nondraft else keeper_inv_id
                if drop_id == keeper_inv_id:
                    # Loser is authoritative: drop the keeper's draft so the loser can repoint.
                    del keeper_periods[period]
                conn.execute(
                    sa.text("DELETE FROM invoice_lines WHERE invoice_id = :i"), {"i": drop_id}
                )
                conn.execute(
                    sa.text("DELETE FROM financial_records WHERE invoice_id = :i"),
                    {"i": drop_id},
                )
                conn.execute(sa.text("DELETE FROM invoices WHERE id = :i"), {"i": drop_id})

            # Repoint FK rows to the keeper.
            for table in _RESELLER_FK_TABLES:
                conn.execute(
                    sa.text(
                        f"UPDATE {table} SET reseller_id = :k WHERE reseller_id = :l"  # noqa: S608
                    ),
                    {"k": keeper_id, "l": loser_id},
                )
            # storefront_bots has a UNIQUE(reseller_id): repoint only if the keeper has none.
            keeper_has_bot = conn.execute(
                sa.text("SELECT 1 FROM storefront_bots WHERE reseller_id = :r LIMIT 1"),
                {"r": keeper_id},
            ).fetchone()
            loser_bot = conn.execute(
                sa.text("SELECT id FROM storefront_bots WHERE reseller_id = :r"),
                {"r": loser_id},
            ).fetchone()
            if loser_bot is not None:
                if keeper_has_bot is None:
                    conn.execute(
                        sa.text(
                            "UPDATE storefront_bots SET reseller_id = :k WHERE reseller_id = :l"
                        ),
                        {"k": keeper_id, "l": loser_id},
                    )
                else:
                    raise RuntimeError(
                        "H05 migration: reseller case-duplicates "
                        f"(panel {panel_id}, uuid {loser[2]!r}) BOTH run a storefront bot. "
                        "Resolve by hand before upgrading."
                    )
            # Copy override fields onto the keeper where the keeper's is NULL.
            keeper_over = conn.execute(
                sa.text(
                    "SELECT price_per_gb, min_sale_toman, storefront_monthly_fee_toman, "
                    "bot_chat_id FROM resellers WHERE id = :r"
                ),
                {"r": keeper_id},
            ).fetchone()
            sets, params = [], {"r": keeper_id}
            for idx, col in enumerate(
                ("price_per_gb", "min_sale_toman", "storefront_monthly_fee_toman", "bot_chat_id")
            ):
                loser_val = loser[5 + idx] if idx < 3 else loser[3]
                if keeper_over[idx] is None and loser_val is not None:
                    sets.append(f"{col} = :{col}")
                    params[col] = loser_val
            if sets:
                conn.execute(
                    sa.text(f"UPDATE resellers SET {', '.join(sets)} WHERE id = :r"), params
                )
            conn.execute(sa.text("DELETE FROM resellers WHERE id = :l"), {"l": loser_id})


def _dedup_snapshots_and_meters(conn) -> None:
    # end_user_snapshots — keep latest last_synced_at per (panel_id, LOWER(user_uuid)).
    rows = conn.execute(
        sa.text("SELECT id, panel_id, user_uuid, last_synced_at FROM end_user_snapshots")
    ).fetchall()
    groups: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        groups[(r[1], (r[2] or "").lower())].append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=lambda m: (_ts(m[3]), m[0]))
        for m in members:
            if m[0] != keeper[0]:
                conn.execute(
                    sa.text("DELETE FROM end_user_snapshots WHERE id = :i"), {"i": m[0]}
                )

    # usage_meters — keep the larger meter (never sum) per (panel_id, LOWER(uuid), period).
    # "Larger" = the most billable extra it contributes (overage + edit_renewal + renew_used).
    rows = conn.execute(
        sa.text(
            "SELECT id, panel_id, user_uuid, period_label, "
            "COALESCE(overage_gb, 0) + COALESCE(edit_renewal_gb, 0) + COALESCE(renew_used_gb, 0) "
            "FROM usage_meters"
        )
    ).fetchall()
    mgroups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        mgroups[(r[1], (r[2] or "").lower(), r[3])].append(r)
    for members in mgroups.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=lambda m: (float(m[4] or 0), m[0]))
        for m in members:
            if m[0] != keeper[0]:
                conn.execute(sa.text("DELETE FROM usage_meters WHERE id = :i"), {"i": m[0]})


def _lowercase_columns(conn) -> None:
    conn.execute(
        sa.text(
            "UPDATE resellers SET admin_uuid = LOWER(admin_uuid) "
            "WHERE admin_uuid <> LOWER(admin_uuid)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE resellers SET parent_admin_uuid = LOWER(parent_admin_uuid) "
            "WHERE parent_admin_uuid IS NOT NULL AND parent_admin_uuid <> LOWER(parent_admin_uuid)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE end_user_snapshots SET user_uuid = LOWER(user_uuid) "
            "WHERE user_uuid <> LOWER(user_uuid)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE end_user_snapshots SET added_by_uuid = LOWER(added_by_uuid) "
            "WHERE added_by_uuid IS NOT NULL AND added_by_uuid <> LOWER(added_by_uuid)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE usage_meters SET user_uuid = LOWER(user_uuid) "
            "WHERE user_uuid <> LOWER(user_uuid)"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE invoice_lines SET end_user_uuid = LOWER(end_user_uuid) "
            "WHERE end_user_uuid <> LOWER(end_user_uuid) "
            "AND end_user_uuid NOT LIKE 'storefront_fee_%'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE invoice_lines SET added_by_uuid = LOWER(added_by_uuid) "
            "WHERE added_by_uuid IS NOT NULL AND added_by_uuid <> LOWER(added_by_uuid)"
        )
    )


def _lower_keys(d: dict | None) -> dict | None:
    if not isinstance(d, dict):
        return d
    return {(k or "").lower(): v for k, v in d.items()}


def _rewrite_enforcement_snapshots(conn) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT id, snapshot FROM enforcement_actions "
            "WHERE snapshot IS NOT NULL AND status NOT IN ('reverted', 'dry_run')"
        )
    ).fetchall()
    for row in rows:
        raw = row[1]
        if raw is None:
            continue
        snap = raw if isinstance(raw, dict) else json.loads(raw)
        if not isinstance(snap, dict):
            continue
        # users: {uuid: owner_uuid}; limits/captured_limits: {uuid: {...}}; admins: [uuid].
        snap["users"] = {
            (u or "").lower(): (v or "").lower() if isinstance(v, str) else v
            for u, v in (snap.get("users") or {}).items()
        }
        if isinstance(snap.get("admins"), list):
            snap["admins"] = [(u or "").lower() for u in snap["admins"]]
        snap["limits"] = _lower_keys(snap.get("limits"))
        prog = snap.get("progress")
        if isinstance(prog, dict):
            for key in ("users_done", "users_missing", "admins_done", "admins_missing"):
                if isinstance(prog.get(key), list):
                    prog[key] = [(u or "").lower() for u in prog[key]]
            for key in ("users_failed", "user_attempts", "admins_failed", "admin_attempts"):
                prog[key] = _lower_keys(prog.get(key)) or {}
            prog["captured_limits"] = _lower_keys(prog.get("captured_limits"))
            snap["progress"] = prog
        conn.execute(
            sa.text("UPDATE enforcement_actions SET snapshot = :s WHERE id = :i"),
            {"s": json.dumps(snap), "i": row[0]},
        )


def upgrade() -> None:
    conn = op.get_bind()
    _merge_resellers(conn)
    _dedup_snapshots_and_meters(conn)
    _lowercase_columns(conn)
    _rewrite_enforcement_snapshots(conn)


def downgrade() -> None:
    # Original uuid casing cannot be reconstructed. To roll back, restore the pre-upgrade
    # database backup (deploy/rollback.sh + the safety pg_dump).
    pass
