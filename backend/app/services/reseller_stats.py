"""
Top-level ("main") reseller helpers — the single source of truth for "how many
resellers do I have" shared by the panel Resellers list and the bot's «آمار کلی».

A "top-level" reseller is a non-owner whose parent is the panel Owner, or which has
no parent in the set (structural-root fallback). This mirrors EXACTLY the tree-view
root logic in app.api.resellers.reseller_tree and invoice_engine.select_billable_roots,
so the panel count and the bot count always agree. Sub-resellers are NOT counted here
(they show under their parent in the tree view).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller


def top_level_roots(resellers: Iterable[Reseller]) -> list[Reseller]:
    """All top-level resellers (non-owner roots), INCLUDING billing-exempt ones.

    Keyed by (panel_id, admin_uuid) so the same uuid on two panels is handled
    independently — identical to the tree view's grouping."""
    resellers = list(resellers)
    owner_keys = {(r.panel_id, r.admin_uuid) for r in resellers if r.is_owner}
    all_keys = {(r.panel_id, r.admin_uuid) for r in resellers}
    roots: list[Reseller] = []
    for r in resellers:
        if r.is_owner:
            continue
        parent_key = (r.panel_id, r.parent_admin_uuid)
        if r.parent_admin_uuid is None or parent_key in owner_keys or parent_key not in all_keys:
            roots.append(r)
    return roots


@dataclass(frozen=True)
class RootStats:
    total: int      # all top-level roots (billable + exempt)
    billable: int   # top-level roots NOT exempt from billing
    exempt: int     # top-level roots exempt from billing
    connected: int  # billable roots registered in the bot (bot_chat_id set)


async def load_root_stats(session: AsyncSession, panel_id: int | None = None) -> RootStats:
    """Compute top-level reseller counts (optionally for one panel)."""
    q = select(Reseller)
    if panel_id is not None:
        q = q.where(Reseller.panel_id == panel_id)
    roots = top_level_roots((await session.execute(q)).scalars().all())
    billable = [r for r in roots if not r.exclude_from_billing]
    exempt = [r for r in roots if r.exclude_from_billing]
    connected = sum(1 for r in billable if r.bot_chat_id is not None)
    return RootStats(
        total=len(roots), billable=len(billable), exempt=len(exempt), connected=connected
    )
