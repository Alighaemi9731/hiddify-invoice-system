"""Is an end-user snapshot still present on the panel?

A user deleted from Hiddify keeps its `EndUserSnapshot` row on purpose: billing must still
charge for a service that was sold and then removed mid-month, and the financial history has
to stay reconstructable. That retention is deliberate and must never be traded away to fix an
operational question (see `maintenance.prune_stale_snapshots` for when the row finally goes).

The cost is that "row exists" stops meaning "user exists on the panel". Anything asking an
OPERATIONAL question — how much creation capacity is left, which users still need disabling —
must therefore filter on presence, while BILLING keeps reading every retained row.

The predicate: sync stamps ONE `now` onto the panel and onto every user it saw, in a single
transaction (`sync.py`), so a user present in the latest sync has
`snapshot.last_synced_at == panel.last_synced_at` exactly. A row left behind by a deletion keeps
an older stamp. Hence strict `<` means removed — and `>=` means present.

  Do NOT relax `<` to `<=`: because the two timestamps are equal for present users, `<=` would
  classify every user on the panel as deleted.

Unlike the reseller-level filter in `api/resellers.py`, this needs no skew tolerance — the two
timestamps come from the same variable, not from two different clocks.

Fails OPEN (treats the user as present) when either timestamp is NULL or the panel has no
successful sync: with no trustworthy sync we cannot claim anything was deleted, and guessing
"deleted" would wrongly free up capacity and silently drop users from enforcement.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import or_

from app.models import EndUserSnapshot, Panel


def snapshot_present_filter():
    """SQLAlchemy WHERE clause: the snapshot was seen in the panel's latest sync.

    Requires `Panel` to be joined in the query. Written as an explicit `or_` because the naive
    `NOT (snap < panel)` yields NULL — not TRUE — when either side is NULL, which would silently
    DROP never-synced rows instead of keeping them.
    """
    return or_(
        Panel.last_synced_at.is_(None),
        EndUserSnapshot.last_synced_at.is_(None),
        EndUserSnapshot.last_synced_at >= Panel.last_synced_at,
    )


def snapshot_present(
    user_synced: dt.datetime | None, panel_synced: dt.datetime | None
) -> bool:
    """In-Python twin of `snapshot_present_filter` (naive datetimes are read as UTC)."""
    if not user_synced or not panel_synced:
        return True
    if user_synced.tzinfo is None:
        user_synced = user_synced.replace(tzinfo=dt.timezone.utc)
    if panel_synced.tzinfo is None:
        panel_synced = panel_synced.replace(tzinfo=dt.timezone.utc)
    return user_synced >= panel_synced
