"""perf audit 2026-08: billing-period index on end_user_snapshots

Revision ID: a4e7c2b9f1d6
Revises: d7f3a9c1e5b8
Create Date: 2026-08-09

Billing now asks the database for one period's services — `panel_id = ? AND start_date BETWEEN
? AND ?` (`app/services/invoicing._period_users_q`) — instead of loading every snapshot a panel
has ever synced and discarding ~92% of them in Python. This composite index serves that range
directly.

Measured on a seeded PostgreSQL 16 with 200k snapshots across 10 panels (16,969 rows in the
period):

    with this index      Bitmap Index Scan ix_enduser_panel_start_date   794 buffers, 1.91 ms
    without it           BitmapAnd of ix_end_user_snapshots_start_date
                         + ix_enduser_panel_addedby_lower                826 buffers, 1.97 ms
    the whole-panel load it replaced                                   4,443 buffers, 5.68 ms

So the index itself is a ~4% read win: the planner could already bitmap-AND two existing
indexes. It is kept for plan STABILITY — one index scan whose cost estimate is 41.6 instead of
a two-index AND estimated at 601.9, which degrades less as the table grows or the month gets
less selective. The large win of that change is Python-side, not in the database: 20,000
hydrated ORM rows per panel became 1,642, and one `EndUserSnapshot` instance costs ~2.02 KB of
heap.

A pg_trgm GIN index on `name` for the owner's end-user search was drafted here and REMOVED
after measurement: the search is a 45 ms parallel seq scan at this size, and a GIN index would
add write amplification to the most heavily UPSERTed table in the system (every user of every
panel, on every sync) to save ~40 ms on an occasional manual lookup. See
plans/PERF_AUDIT_2026-08.md → "do not do this".

Additive, portable DDL; instant at current sizes on both PostgreSQL and SQLite.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a4e7c2b9f1d6"
down_revision: str | None = "d7f3a9c1e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERIOD_INDEX = "ix_enduser_panel_start_date"


def upgrade() -> None:
    op.create_index(_PERIOD_INDEX, "end_user_snapshots", ["panel_id", "start_date"])


def downgrade() -> None:
    op.drop_index(_PERIOD_INDEX, table_name="end_user_snapshots")
