"""
Invoice engine — pure, deterministic computation of reseller invoices.

Rewritten from scratch for correctness and clarity. No DB or I/O here, so the rules
are trivially unit-testable. The orchestration layer (invoicing.py) feeds it ORM rows
and persists the result.

THE RULES (confirmed with the owner — source of truth)
------------------------------------------------------
A "bundle" is a top-level reseller (a non-owner, non-excluded admin whose parent is
the panel Owner) together with ALL of its descendant sub-resellers, to any depth.

For each bundle, over a Gregorian billing month, sum the package size SOLD
(`usage_limit_gb`, NOT consumed traffic) of every end-user where:
  • the user's `added_by_uuid` is the bundle's reseller or one of its descendants, AND
  • the user's `start_date` (creation date) falls inside the month, AND
  • the package size is not an excluded "test" size (default {1} GB).
    (5 GB counts as normal traffic and IS billed.)

amount = total_gb × price_per_gb
  price_per_gb = the top reseller's override, else the global default.
Then apply the MINIMUM-SALE FLOOR: if 0 < amount < min_sale, charge `min_sale`.
  min_sale = the top reseller's override, else the global default (0 = disabled).
  The floor is on the WHOLE bundle, so two sub-resellers each below the floor but
  together above it are fine.

No prior-unpaid carry-over. The Owner and excluded resellers are never billed.
Bundles with zero usage are still returned (so the owner can see who billed nothing).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.periods import Period


@dataclass
class LineResult:
    user_uuid: str
    name: str
    start_date: dt.date | None
    usage_gb: float
    added_by_uuid: str | None
    sub_reseller_name: str = ""  # which (sub-)reseller created this service
    from_deleted: bool = False   # user removed from the panel → billed on consumption, not quota


@dataclass
class BundleResult:
    root: Any                       # the billed top-level reseller (ORM row / object)
    admin_uuids: set[str]           # root + every descendant uuid
    descendant_names: dict[str, str]
    lines: list[LineResult] = field(default_factory=list)
    raw_gb: float = 0.0             # summed package sizes (informational)
    total_gb: float = 0.0           # billable gb (same as raw here; kept explicit)
    users_count: int = 0
    price_per_gb: int = 0
    base_amount_toman: float = 0.0  # total_gb * price (before floor)
    min_sale_toman: int = 0
    floor_applied: bool = False

    @property
    def amount_toman(self) -> float:
        """Final amount: base, raised to the floor when 0 < base < floor."""
        if self.base_amount_toman > 0 and self.min_sale_toman > 0 \
                and self.base_amount_toman < self.min_sale_toman:
            return float(self.min_sale_toman)
        return self.base_amount_toman


# ----------------------------- hierarchy -----------------------------
def build_children_map(resellers: Iterable[Any]) -> dict[str, list[Any]]:
    children: dict[str, list[Any]] = defaultdict(list)
    for r in resellers:
        if r.parent_admin_uuid:
            children[r.parent_admin_uuid].append(r)
    return children


def collect_descendants(root: Any, children_map: dict[str, list[Any]]) -> list[Any]:
    """Root + all descendants (cycle-safe, breadth-first)."""
    out: list[Any] = []
    seen: set[str] = set()
    queue = [root]
    while queue:
        cur = queue.pop()
        if cur.admin_uuid in seen:
            continue
        seen.add(cur.admin_uuid)
        out.append(cur)
        queue.extend(children_map.get(cur.admin_uuid, []))
    return out


def select_billable_roots(resellers: list[Any]) -> list[Any]:
    """Top-level resellers to bill: non-owner, not excluded, direct child of an Owner.

    Falls back to structural roots when the dataset has no Owner row."""
    owner_uuids = {r.admin_uuid for r in resellers if getattr(r, "is_owner", False)}
    all_uuids = {r.admin_uuid for r in resellers}
    roots: list[Any] = []
    for r in resellers:
        if getattr(r, "is_owner", False) or getattr(r, "exclude_from_billing", False):
            continue
        if owner_uuids:
            if r.parent_admin_uuid in owner_uuids:
                roots.append(r)
        elif r.parent_admin_uuid is None or r.parent_admin_uuid not in all_uuids:
            roots.append(r)
    return roots


# ----------------------------- billing -----------------------------
def _excluded(usage_gb: float, excluded: set[int], free_threshold_gb: float) -> bool:
    """A config is a free test config when its quota is <= the free threshold
    (default 1 GB → 0.5 GB and 1 GB are free), OR it exactly matches an extra
    excluded size. Uses exact (not rounded) comparison so a real 1.3 GB package
    is NOT mistaken for a 1 GB test config."""
    try:
        gb = float(usage_gb or 0)
    except (TypeError, ValueError):
        return False
    if gb <= free_threshold_gb + 1e-9:
        return True
    return any(abs(gb - e) < 1e-9 for e in excluded)


def billable_gb_for_user(
    u: Any,
    period: Period,
    excluded_usage_gb: set[int],
    free_threshold_gb: float,
    panel_synced_at: dt.datetime | None = None,
) -> tuple[float, bool] | None:
    """The single source of truth for one user's billable GB in a period.

    Returns `(gb, from_deleted)` or `None` if the user isn't billed. A user the panel no
    longer has (its snapshot predates the panel's latest sync) is billed on what it actually
    CONSUMED before deletion (`current_usage_gb`); everyone still on the panel is billed on the
    SOLD quota (`usage_limit_gb`). The "is it a test config?" exclusion uses the SOLD quota (so a
    deleted real config isn't mistaken for a test one); a removed config whose CONSUMPTION is
    below the free threshold is also dropped as negligible. `panel_synced_at=None` disables
    deletion detection (everyone billed on sold quota — the legacy behaviour)."""
    if not period.contains(u.start_date):
        return None
    gb_sold = float(u.usage_limit_gb or 0)
    if _excluded(gb_sold, excluded_usage_gb, free_threshold_gb):
        return None
    deleted = bool(
        panel_synced_at and getattr(u, "last_synced_at", None)
        and u.last_synced_at < panel_synced_at
    )
    if deleted:
        gb = round(float(getattr(u, "current_usage_gb", 0) or 0), 3)
        # A removed config whose CONSUMPTION is below the free threshold is negligible (e.g. a
        # config renewed by delete+recreate that was barely used, or one that used a few MB) →
        # not billed, just like a test config. Real removed usage (above the threshold) is billed.
        if gb <= free_threshold_gb:
            return None
        return gb, True
    return gb_sold, False


def compute_invoices(
    resellers: list[Any],
    users: list[Any],
    period: Period,
    *,
    default_price_per_gb: int,
    excluded_usage_gb: set[int],
    default_min_sale_toman: int = 0,
    free_threshold_gb: float = 1.0,
    panel_synced_at: dt.datetime | None = None,
) -> list[BundleResult]:
    """Return one BundleResult per billable top-level reseller (including zero ones)."""
    children_map = build_children_map(resellers)
    roots = select_billable_roots(resellers)

    users_by_adder: dict[str, list[Any]] = defaultdict(list)
    for u in users:
        if u.added_by_uuid:
            users_by_adder[u.added_by_uuid].append(u)

    bundles: list[BundleResult] = []
    for root in roots:
        descendants = collect_descendants(root, children_map)
        names = {d.admin_uuid: (d.name or "") for d in descendants}
        admin_uuids = set(names)

        lines: list[LineResult] = []
        for uuid in admin_uuids:
            for u in users_by_adder.get(uuid, []):
                res = billable_gb_for_user(
                    u, period, excluded_usage_gb, free_threshold_gb, panel_synced_at
                )
                if res is None:
                    continue
                gb, deleted = res
                lines.append(LineResult(
                    user_uuid=u.user_uuid, name=u.name or "", start_date=u.start_date,
                    usage_gb=gb, added_by_uuid=u.added_by_uuid,
                    sub_reseller_name=names.get(u.added_by_uuid or "", ""),
                    from_deleted=deleted,
                ))

        # Sort lines newest-first within the month, biggest package first.
        lines.sort(key=lambda ln: (ln.start_date or period.start, ln.usage_gb), reverse=True)

        total_gb = round(sum(ln.usage_gb for ln in lines), 3)
        price = int(getattr(root, "price_per_gb", None) or default_price_per_gb)
        min_sale = int(
            getattr(root, "min_sale_toman", None)
            if getattr(root, "min_sale_toman", None) is not None
            else default_min_sale_toman
        )
        base = round(total_gb * price)

        b = BundleResult(
            root=root, admin_uuids=admin_uuids, descendant_names=names, lines=lines,
            raw_gb=total_gb, total_gb=total_gb, users_count=len(lines),
            price_per_gb=price, base_amount_toman=base, min_sale_toman=min_sale,
        )
        b.floor_applied = base > 0 and min_sale > 0 and base < min_sale
        bundles.append(b)

    return bundles
