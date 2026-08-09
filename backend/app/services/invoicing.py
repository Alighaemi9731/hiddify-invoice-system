"""
Invoice orchestration: run the engine over snapshot data and persist Invoice +
InvoiceLine rows. Delivery (bot) is M4; this only generates draft invoices.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    FinancialRecord,
    Invoice,
    InvoiceLine,
    Panel,
    Reseller,
)
from app.models.enums import InvoiceStatus, PanelStatus
from app.services import financial_archive, metering, pricing, storefront
from app.services.invoice_engine import BundleResult, compute_invoices
from app.services.periods import Period

log = logging.getLogger("invoicing")

# Allow for sub-second clock/reload skew when comparing a reseller's last_seen_at to the
# panel's last_synced_at (both stamped from the same value during a sync).
_PRESENCE_SKEW = dt.timedelta(seconds=2)


def _period_users_q(panel_id: int, period: Period):
    """The end-user snapshots that can possibly be billed for `period` on this panel.

    The date filter is not a heuristic — it is the engine's OWN first predicate, moved into SQL.
    `invoice_engine.billable_gb_for_user` returns None unless `period.contains(u.start_date)`,
    and `Period.contains` is exactly `d is not None and start <= d <= end`; `compute_invoices`
    reads `users` ONLY to build lines (`users_count`, `raw_gb` and `total_gb` all derive from
    them), while roots and the hierarchy come from `resellers`. So a snapshot outside the period
    can never affect any output — including the removed-from-panel branch, which is evaluated
    after the date check.

    Why it matters: only ~8% of snapshots were created in any given month, so the old
    whole-table load hydrated ~12x more ORM rows than the engine could use — measured 5.10 s /
    45 MB for 200k rows versus 0.78 s / 4 MB for the 16.5k the period actually contains
    (one `EndUserSnapshot` instance costs ~2.02 KB of Python heap). That load ran on the API
    event loop behind /reports/sales-by-day. Parity is pinned by
    tests/test_invoice_engine_prefilter.py.
    """
    return select(EndUserSnapshot).where(
        EndUserSnapshot.panel_id == panel_id,
        EndUserSnapshot.start_date.between(period.start, period.end),
    )

# Serializes all invoice generation/recompute so two concurrent runs (the monthly scheduler
# job overlapping a manual «صدور فاکتورهای دوره», or a double-click) can never race on the
# same (reseller, period): the unique constraint already blocks duplicates, but without this
# the loser would abort with an IntegrityError (rolling back the whole run) or interleave
# invoice-line writes. Adjacent to the Alembic migration lock key.
_BILLING_LOCK_KEY = 734_137_044


async def _serialize_billing(session: AsyncSession) -> None:
    """Take a transaction-level advisory lock (auto-released on commit/rollback) so only one
    billing run proceeds at a time; others wait, then see the committed invoices. No-op on
    SQLite (tests / single-writer)."""
    bind = session.get_bind()
    if getattr(bind.dialect, "name", "") == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BILLING_LOCK_KEY}
        )


def _panel_billable(
    panel: Panel, *, now: dt.datetime | None = None, max_age_hours: int = 0
) -> tuple[bool, str]:
    """A panel may only be billed from FRESH data. Never bill on a failed or absent sync — stale
    snapshots would invoice last month's reality. With `max_age_hours`>0, also skip a panel whose
    last successful sync is older than that (F9 — a scheduler outage or a re-enabled panel must not
    silently bill months-old state). Returns (ok, reason)."""
    if panel.last_synced_at is None:
        return False, "هرگز همگام‌سازی نشده"
    if panel.status == PanelStatus.error:
        return False, "آخرین همگام‌سازی ناموفق بود"
    if max_age_hours and now is not None:
        synced = _as_utc(panel.last_synced_at)
        if synced is not None and (now - synced).total_seconds() > max_age_hours * 3600:
            return False, "دادهٔ همگام‌سازی بسیار قدیمی است (لطفاً پنل را همگام‌سازی کنید)"
    return True, ""


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a possibly-naive datetime to tz-aware UTC. SQLite (and older imported rows)
    return naive datetimes even for tz-aware columns; comparing one against a tz-aware value
    raises. Treat naive as UTC (all timestamps are stamped in UTC)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _reseller_present(reseller: Reseller, panel: Panel) -> bool:
    """True if the reseller was seen in the panel's latest sync. A reseller (admin) removed
    from the panel keeps an older last_seen_at than the panel's last_synced_at, so it's no
    longer billed (instead of being invoiced forever on lingering data)."""
    seen = _as_utc(reseller.last_seen_at)
    synced = _as_utc(panel.last_synced_at)
    if seen is None or synced is None:
        return True  # not enough info → don't silently drop
    return seen >= synced - _PRESENCE_SKEW


@dataclass
class GenerationSummary:
    period: str
    created: int = 0
    updated: int = 0
    skipped_existing: int = 0
    zero_skipped: int = 0
    total_amount_toman: float = 0.0
    invoice_ids: list[int] = None  # type: ignore[assignment]
    # Panels excluded because their latest sync failed / never ran (billed on stale data is worse
    # than skipping). Each entry is "<panel key>: <reason>".
    skipped_panels: list[str] = field(default_factory=list)
    # DRAFT invoices removed because the reseller's usage dropped to zero / they were removed.
    reconciled_zero: int = 0
    # Present, non-owner, non-excluded resellers that are NEITHER a billable root NOR a
    # descendant of one (parent deleted, or a parent cycle) — so they're silently never billed.
    # Each entry is "<panel key>: <reseller name> (<admin_uuid>)".
    unbilled_subtrees: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.invoice_ids is None:
            self.invoice_ids = []


async def generate_invoices(
    session: AsyncSession,
    period: Period,
    *,
    panel_id: int | None = None,
    force: bool = False,
) -> GenerationSummary:
    """Generate draft invoices for `period`. Existing non-draft invoices are kept
    unless `force=True`. Draft invoices for the period are recomputed in place."""
    from app.services import rates, settings_service

    # In auto mode, pull a fresh live rate right before billing so the Toman→USDT figures are
    # current. Strictly best-effort — a fetch OR settings-write failure must never abort the
    # billing run; the last good rate stays in place.
    if str(await settings_service.get(session, "rate_mode", "manual")).lower() == "auto":
        try:
            await rates.refresh_auto_rate(session)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("invoicing").warning("pre-billing rate refresh failed", exc_info=True)
    # TON payment shows the customer a TON amount → keep that rate fresh at billing time too.
    if await settings_service.get(session, "pay_ton_enabled", False):
        try:
            await rates.refresh_ton_rate(session)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("invoicing").warning("pre-billing TON rate refresh failed", exc_info=True)
    # AVAX payment shows the customer an AVAX amount → keep that derived rate fresh too.
    if await settings_service.get(session, "pay_avax_enabled", False):
        try:
            await rates.refresh_avax_rate(session)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("invoicing").warning("pre-billing AVAX rate refresh failed", exc_info=True)

    # Serialize against any other concurrent generation/recompute before reading + writing.
    await _serialize_billing(session)

    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    free_threshold = await pricing.get_free_threshold_gb(session)
    deleted_over = await pricing.get_deleted_full_quota_over_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)
    rate = await pricing.get_rate(session)
    max_age = await pricing.get_max_snapshot_age_hours(session)
    now_utc = dt.datetime.now(dt.timezone.utc)

    # An explicit panel_id must NOT bypass the enabled filter — a disabled panel is
    # disabled for billing too.
    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = panel_q.where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    summary = GenerationSummary(period=period.label)
    processed_panel_ids: list[int] = []
    billed_reseller_ids: set[int] = set()

    for panel in panels:
        ok, reason = _panel_billable(panel, now=now_utc, max_age_hours=max_age)
        if not ok:
            summary.skipped_panels.append(f"{panel.key}: {reason}")
            log.warning("billing: skipping panel '%s' (%s)", panel.key, reason)
            continue
        processed_panel_ids.append(panel.id)
        resellers = (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars().all()
        users = (await session.execute(_period_users_q(panel.id, period))).scalars().all()
        # Free-trial configs are a giveaway — excluded from the reseller's invoice (base + metering).
        trial_uuids = await storefront.trial_user_uuids(session, panel.id)
        # Storefront config uuid → plan sold — caps the inflated renewal quota (used+plan_gb) so a
        # renewed config is billed the real sale, not the previous cycle's consumption re-counted.
        caps = await storefront.billing_caps(session, panel.id)

        bundles = compute_invoices(
            resellers, users, period,
            default_price_per_gb=default_price, excluded_usage_gb=excluded,
            default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
            panel_synced_at=panel.last_synced_at,
            deleted_full_quota_over_gb=deleted_over,
            exclude_user_uuids=trial_uuids,
            cap_gb_by_uuid=caps,
        )
        # Abuse-resistant extras (overage from usage resets + renew-by-edit) for ALL
        # bundles in one pass — was 2 queries per bundle (N+1 across every root).
        extras = await metering.bundle_extra_for_bundles(
            session, panel.id, [b.admin_uuids for b in bundles], period.label,
            free_threshold, exclude_user_uuids=trial_uuids,
        )
        for bundle, extra in zip(bundles, extras):
            # A reseller removed from the panel (gone in the latest sync) must not be billed
            # forever on lingering data.
            if not _reseller_present(bundle.root, panel):
                continue
            if bundle.total_gb + extra["gb"] <= 0:
                # Zero usage still yields an invoice when the flat storefront-bot fee applies —
                # otherwise a fee-only month is silently never billed AND a prior fee-only
                # draft gets reconcile-deleted below. Skip only when BOTH are zero.
                if await storefront.monthly_fee_for(session, bundle.root) <= 0:
                    summary.zero_skipped += 1
                    continue
            await _persist_bundle(session, panel, bundle, extra, period, rate, summary, force)
            billed_reseller_ids.add(bundle.root.id)

        # Detect resellers that fall through billing entirely: a non-owner, non-excluded,
        # PRESENT reseller whose admin_uuid is in NO bundle (its parent was deleted, or it sits
        # in a parent cycle) is silently never invoiced. Surface it so the owner can fix the
        # hierarchy instead of losing that revenue quietly.
        covered = {u for b in bundles for u in b.admin_uuids}
        for rr in resellers:
            if rr.is_owner or rr.exclude_from_billing:
                continue
            if rr.admin_uuid in covered or not _reseller_present(rr, panel):
                continue
            summary.unbilled_subtrees.append(
                f"{panel.key}: {rr.name or '—'} ({rr.admin_uuid})"
            )

    # Reconcile stale DRAFT invoices: a reseller that had a draft for this period from a prior
    # run but is now zero / removed gets no bundle above — its leftover draft would otherwise
    # still be sendable with last month's amount. Delete those drafts (drafts aren't in the
    # ledger; delivered/paid invoices are never touched).
    if processed_panel_ids:
        stale_q = select(Invoice.id).where(
            Invoice.period_start == period.start, Invoice.period_end == period.end,
            Invoice.panel_id.in_(processed_panel_ids), Invoice.status == InvoiceStatus.draft,
        )
        if billed_reseller_ids:
            stale_q = stale_q.where(Invoice.reseller_id.notin_(billed_reseller_ids))
        stale_ids = (await session.execute(stale_q)).scalars().all()
        if stale_ids:
            await session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(stale_ids)))
            await session.execute(
                delete(FinancialRecord).where(FinancialRecord.invoice_id.in_(stale_ids))
            )
            await session.execute(delete(Invoice).where(Invoice.id.in_(stale_ids)))
            summary.reconciled_zero = len(stale_ids)

    await session.commit()
    log.info(
        "Generated invoices for %s: created=%d updated=%d skipped=%d total=%.0f T",
        period.label, summary.created, summary.updated,
        summary.skipped_existing, summary.total_amount_toman,
    )
    return summary


async def preview_bundles(
    session: AsyncSession, period: Period, *, panel_id: int | None = None
) -> list[tuple[Panel, BundleResult]]:
    """Compute bundles for a period WITHOUT persisting (used for the zero-invoice view)."""
    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    free_threshold = await pricing.get_free_threshold_gb(session)
    deleted_over = await pricing.get_deleted_full_quota_over_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)
    max_age = await pricing.get_max_snapshot_age_hours(session)
    now_utc = dt.datetime.now(dt.timezone.utc)

    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = panel_q.where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    out: list[tuple[Panel, BundleResult]] = []
    for panel in panels:
        # Mirror generate_invoices: don't preview a panel we wouldn't bill, and skip resellers
        # no longer present so the "zero sale" view matches reality.
        ok, _ = _panel_billable(panel, now=now_utc, max_age_hours=max_age)
        if not ok:
            continue
        resellers = (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars().all()
        users = (await session.execute(_period_users_q(panel.id, period))).scalars().all()
        trial_uuids = await storefront.trial_user_uuids(session, panel.id)
        caps = await storefront.billing_caps(session, panel.id)
        preview_bundles_list = compute_invoices(
            resellers, users, period,
            default_price_per_gb=default_price, excluded_usage_gb=excluded,
            default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
            panel_synced_at=panel.last_synced_at,
            deleted_full_quota_over_gb=deleted_over,
            exclude_user_uuids=trial_uuids,
            cap_gb_by_uuid=caps,
        )
        extras = await metering.bundle_extra_for_bundles(
            session, panel.id, [b.admin_uuids for b in preview_bundles_list],
            period.label, free_threshold, exclude_user_uuids=trial_uuids,
        )
        for b, extra in zip(preview_bundles_list, extras):
            if not _reseller_present(b.root, panel):
                continue
            # Fold in the abuse-metered extra so total_gb here equals what billing would invoice
            # (a reseller with only metered overage must NOT show up as a "zero sale").
            b.total_gb = round(b.total_gb + float(extra.get("gb", 0) or 0), 3)
            out.append((panel, b))
    return out


async def recompute_invoice(
    session: AsyncSession, invoice: Invoice, *, sync_first: bool = True
) -> dict:
    """Refresh ONE invoice's figures from the panel's CURRENT data, keeping its status
    (sent/overdue/…). Used to correct an already-sent invoice after the reseller fixed
    a mistake (e.g. removed a wrong user) in the panel. Paid invoices are protected."""
    if invoice.status == InvoiceStatus.paid:
        raise ValueError("paid invoice cannot be recomputed")

    panel = await session.get(Panel, invoice.panel_id)
    if panel is None:
        raise ValueError("panel not found")

    synced = False
    if sync_first:
        from app.models.enums import SyncStatus
        from app.services import sync as sync_service

        try:
            run = await sync_service.sync_panel(session, panel)
            synced = run.status == SyncStatus.success
        except Exception:  # noqa: BLE001 — treat a sync exception like a failed sync (abort below)
            log.warning("recompute: panel sync raised", exc_info=True)
        refreshed_panel = await session.get(Panel, invoice.panel_id)
        refreshed_invoice = await session.get(Invoice, invoice.id)
        if refreshed_panel is None or refreshed_invoice is None:
            raise ValueError("invoice or panel disappeared during recompute")
        panel = refreshed_panel
        invoice = refreshed_invoice
        # F9: recompute PROMISED current data — if the requested sync did not succeed, abort WITHOUT
        # mutating the invoice rather than silently rewriting a sent invoice from stale snapshots.
        if not synced:
            raise ValueError("panel sync failed; invoice left unchanged (try again when the panel syncs)")

    # F9: also never recompute from a panel that isn't billable (stale/errored/never-synced) — this
    # path did not previously check it, so a recompute during a panel outage could rewrite from old data.
    max_age = await pricing.get_max_snapshot_age_hours(session)
    ok, reason = _panel_billable(panel, now=dt.datetime.now(dt.timezone.utc), max_age_hours=max_age)
    if not ok:
        raise ValueError(f"panel not billable ({reason}); invoice left unchanged")

    # Serialize against concurrent generation/recompute (after the optional sync's own commit,
    # so the lock is held through this recompute's single final commit).
    await _serialize_billing(session)

    period = Period(invoice.period_start, invoice.period_end)
    default_price = await pricing.get_default_price_per_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    free_threshold = await pricing.get_free_threshold_gb(session)
    deleted_over = await pricing.get_deleted_full_quota_over_gb(session)
    default_min_sale = await pricing.get_default_min_sale(session)
    rate = await pricing.get_rate(session) or int(invoice.usdt_rate or 0)

    resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
    ).scalars().all()
    users = (await session.execute(_period_users_q(panel.id, period))).scalars().all()
    trial_uuids = await storefront.trial_user_uuids(session, panel.id)
    caps = await storefront.billing_caps(session, panel.id)
    bundles = compute_invoices(
        resellers, users, period,
        default_price_per_gb=default_price, excluded_usage_gb=excluded,
        default_min_sale_toman=default_min_sale, free_threshold_gb=free_threshold,
        panel_synced_at=panel.last_synced_at,
        deleted_full_quota_over_gb=deleted_over,
        exclude_user_uuids=trial_uuids,
        cap_gb_by_uuid=caps,
    )
    bundle = next((b for b in bundles if b.root.id == invoice.reseller_id), None)

    reseller = await session.get(Reseller, invoice.reseller_id)
    base_gb = bundle.total_gb if bundle else 0.0
    base_lines = bundle.lines if bundle else []
    price = bundle.price_per_gb if bundle else invoice.price_per_gb
    if bundle:
        admin_uuids = bundle.admin_uuids
    else:
        from app.services.reseller_report import node_descendants
        admin_uuids = {d.admin_uuid for d in await node_descendants(session, reseller)} if reseller else set()
    extra = await metering.bundle_extra(
        session, panel.id, admin_uuids, period.label, free_threshold,
        exclude_user_uuids=trial_uuids,
    )

    if reseller is None:
        raise ValueError("reseller not found")
    # The SAME totals math as generation (fee + floor + rounding) — see _compute_totals.
    totals = await _compute_totals(
        session, reseller,
        base_gb=base_gb, base_users_count=(bundle.users_count if bundle else 0),
        extra=extra, price=price,
        bundle_min_sale=(bundle.min_sale_toman if bundle else 0),
        period_start=invoice.period_start,
    )

    await session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    invoice.usage_gb = totals.total_gb
    invoice.users_count = totals.users_count
    invoice.price_per_gb = price
    invoice.base_amount_toman = totals.base_amount
    invoice.min_sale_toman = totals.min_sale
    invoice.floor_applied = totals.floor_applied
    invoice.amount_toman = totals.amount_toman
    _write_lines(
        session, invoice, base_lines=base_lines, extra=extra,
        storefront_fee=totals.storefront_fee, reseller_id=reseller.id,
    )
    invoice.usdt_rate = rate
    invoice.amount_usdt = float(pricing.toman_to_usdt(invoice.amount_toman, rate))
    await financial_archive.record(session, invoice)
    await session.commit()
    return {"synced": synced, "found": bundle is not None,
            "amount_toman": float(invoice.amount_toman), "usage_gb": float(invoice.usage_gb)}


@dataclass
class InvoiceTotals:
    """One invoice's money figures, computed by the single shared `_compute_totals`."""

    total_gb: float
    base_amount: int
    min_sale: int
    floor_applied: bool
    storefront_fee: int
    amount_toman: float
    users_count: int


async def _compute_totals(
    session: AsyncSession,
    reseller: Reseller,
    *,
    base_gb: float,
    base_users_count: int,
    extra: dict,
    price: int,
    bundle_min_sale: int,
    period_start: dt.date,
) -> InvoiceTotals:
    """SINGLE source of truth for an invoice's money figures: usage+metering GB rounding,
    the first-month-exempt min-sale floor, and the flat storefront-bot monthly fee.

    BOTH generation (`_persist_bundle`) and «بازمحاسبه از روی پنل» (`recompute_invoice`)
    must go through here — recompute used to rebuild the amount WITHOUT the storefront fee,
    silently discounting the invoice and dropping its fee line. Note the fee is evaluated
    at call time (active-bot-only billing): recomputing after the reseller's bot was
    disabled intentionally drops the fee."""
    from app.services import storefront as _storefront

    total_gb = round(base_gb + float(extra.get("gb", 0) or 0), 3)
    base_amount = int(round(total_gb * price))
    min_sale = await _effective_min_sale(session, reseller.id, period_start, bundle_min_sale)
    floor_applied = base_amount > 0 and min_sale > 0 and base_amount < min_sale
    storefront_fee = int(await _storefront.monthly_fee_for(session, reseller))
    amount_toman = (float(min_sale) if floor_applied else float(base_amount)) + float(storefront_fee)
    return InvoiceTotals(
        total_gb=total_gb,
        base_amount=base_amount,
        min_sale=min_sale,
        floor_applied=floor_applied,
        storefront_fee=storefront_fee,
        amount_toman=amount_toman,
        # Extras are split into one line per component (renewal #1/#2, edit top-up, overage),
        # so count the DISTINCT metered users — the semantics users_count always had.
        users_count=base_users_count
        + len({el["user_uuid"] for el in (extra.get("lines", []) or [])}),
    )


def _write_lines(
    session: AsyncSession,
    invoice: Invoice,
    *,
    base_lines: list,
    extra: dict,
    storefront_fee: int,
    reseller_id: int,
) -> None:
    """Persist ALL of an invoice's lines (the caller has already deleted old ones): the
    0-GB storefront-fee line, the engine's base lines (deleted users name-flagged), and the
    metering «مصرف اضافه/تمدید» extras — identical for generation and recompute."""
    if storefront_fee > 0:
        session.add(InvoiceLine(
            invoice_id=invoice.id, end_user_uuid=f"storefront_fee_{reseller_id}",
            name="🏪 هزینهٔ ماهانهٔ ربات فروشگاهی", start_date=None, usage_gb=0,
            added_by_uuid=None, sub_reseller_name="",
        ))
    for line in base_lines:
        # A user removed from the panel is billed on consumption and flagged in its name (same
        # convention as the metering "extra" lines below) so the reseller sees why.
        nm = ((line.name or "")[:235] + " — مصرف حذف‌شده از پنل") if line.from_deleted else line.name[:255]
        session.add(InvoiceLine(
            invoice_id=invoice.id,
            end_user_uuid=line.user_uuid,
            name=nm,
            start_date=line.start_date,
            usage_gb=line.usage_gb,
            added_by_uuid=line.added_by_uuid,
            sub_reseller_name=(line.sub_reseller_name or "")[:255],
        ))
    for el in extra.get("lines", []) or []:
        # `display_name` is the typed label built by metering (« — تمدید اول/دوم»,
        # « — افزایش حجم با ویرایش», « — مصرف مازاد بر بسته») — one line per component,
        # so the invoice says exactly what each item is.
        session.add(InvoiceLine(
            invoice_id=invoice.id,
            end_user_uuid=el["user_uuid"],
            name=(el.get("display_name") or el.get("name") or "")[:255],
            start_date=None,
            usage_gb=el["usage_gb"],
            added_by_uuid=el.get("added_by_uuid"),
            sub_reseller_name="",
        ))


async def _effective_min_sale(
    session: AsyncSession, reseller_id: int, period_start: dt.date, min_sale: int
) -> int:
    """The minimum-sale floor to actually apply to this reseller's invoice this month.

    The floor is skipped on the reseller's FIRST invoiced month — a reseller who bought a panel
    mid-month and is billed for a short partial period shouldn't be forced up to the minimum. It
    applies from the SECOND invoiced month onward. "First invoiced month" = no earlier
    (period_start < this one) delivered invoice exists — draft/canceled don't count, since a
    draft was never billed. Returns the effective floor (0 = no floor)."""
    if min_sale <= 0:
        return 0
    prior = (
        await session.execute(
            select(Invoice.id).where(
                Invoice.reseller_id == reseller_id,
                Invoice.period_start < period_start,
                Invoice.status.notin_((InvoiceStatus.draft, InvoiceStatus.canceled)),
            ).limit(1)
        )
    ).first()
    return min_sale if prior is not None else 0


async def _persist_bundle(
    session: AsyncSession,
    panel: Panel,
    bundle: BundleResult,
    extra: dict,
    period: Period,
    rate: int,
    summary: GenerationSummary,
    force: bool,
) -> None:
    reseller: Reseller = bundle.root
    # Usage + metering extra, min-sale floor, and the flat storefront-bot fee — all computed
    # by the shared _compute_totals so generation and recompute can never diverge.
    price = bundle.price_per_gb
    totals = await _compute_totals(
        session, reseller,
        base_gb=bundle.total_gb, base_users_count=bundle.users_count,
        extra=extra, price=price, bundle_min_sale=bundle.min_sale_toman,
        period_start=period.start,
    )
    amount_usdt = float(pricing.toman_to_usdt(totals.amount_toman, rate))

    existing = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller.id,
                Invoice.period_start == period.start,
                Invoice.period_end == period.end,
            )
        )
    ).scalar_one_or_none()

    # A PAID invoice is settled accounting — NEVER recompute it, even with force.
    if existing and existing.status == InvoiceStatus.paid:
        summary.skipped_existing += 1
        return
    # Other already-delivered invoices (sent/overdue/enforced) are left as-is unless
    # the caller explicitly forces a recompute.
    if existing and existing.status != InvoiceStatus.draft and not force:
        summary.skipped_existing += 1
        return

    if existing:
        invoice = existing
        # Clear old lines before recomputation (explicit delete avoids a lazy load).
        await session.execute(
            delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
        )
        summary.updated += 1
    else:
        invoice = Invoice(reseller_id=reseller.id, panel_id=panel.id)
        session.add(invoice)
        summary.created += 1

    invoice.period_start = period.start
    invoice.period_end = period.end
    invoice.period_label = period.label
    invoice.usage_gb = totals.total_gb
    invoice.users_count = totals.users_count
    invoice.price_per_gb = price
    invoice.amount_toman = totals.amount_toman
    invoice.base_amount_toman = totals.base_amount
    invoice.min_sale_toman = totals.min_sale
    invoice.floor_applied = totals.floor_applied
    invoice.usdt_rate = rate
    invoice.amount_usdt = amount_usdt
    if existing is None or existing.status == InvoiceStatus.draft:
        invoice.status = InvoiceStatus.draft
    await session.flush()

    _write_lines(
        session, invoice, base_lines=bundle.lines, extra=extra,
        storefront_fee=totals.storefront_fee, reseller_id=reseller.id,
    )

    # Mirror into the durable financial ledger (survives wipes / panel removal).
    await financial_archive.record(session, invoice, panel=panel, reseller=reseller)

    summary.total_amount_toman += totals.amount_toman
    summary.invoice_ids.append(invoice.id)
