"""
Sync a panel's backup into our DB: upsert resellers (admins) and end-user snapshots.

Idempotent: existing rows are updated in place, new ones inserted. `exclude_from_billing`
is seeded once (from a "-" comment) on insert and never overwritten afterwards, so the
owner's manual toggle is preserved across syncs.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Panel, Reseller, SyncRun
from app.models.enums import EnforcementState, PanelStatus, SyncSource, SyncStatus
from app.services.panel_client import BackupJsonClient, PanelClient, PanelData

log = logging.getLogger("sync")

# Two-key advisory-lock namespace for serializing a SINGLE panel's sync (F12) — distinct key-space
# from the billing lock (invoicing._BILLING_LOCK_KEY, single-key) so they never collide.
_SYNC_LOCK_NS = 0x53594E43  # "SYNC"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def sync_panel(
    session: AsyncSession,
    panel: Panel,
    *,
    data: PanelData | None = None,
    client: PanelClient | None = None,
    source: SyncSource = SyncSource.backup_json,
) -> SyncRun:
    """Sync one panel. Tests may inject parsed `data` directly."""
    # Capture the id NOW: session.rollback() in the except block expires every attribute,
    # so reading panel.id afterwards would trigger a sync lazy-load (MissingGreenlet) and
    # mask the real error / abort the whole run.
    panel_id = panel.id
    # Serialize concurrent syncs of THIS panel (F12): scheduler tick, sync-all, a manual sync click,
    # create/update-panel background sync, and recompute can all target one panel at once — without a
    # lease, a reverse-order finish overwrites newer data or two first-time inserts collide on
    # uq_enduser_panel_uuid and spuriously mark the panel errored. Blocking (not try) + per-panel key
    # so different panels never wait on each other; released on commit/rollback. No-op on SQLite.
    bind = session.get_bind()
    if getattr(bind.dialect, "name", "") == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :pid)"),
            {"ns": _SYNC_LOCK_NS, "pid": panel_id},
        )
    run = SyncRun(panel_id=panel_id, source=source, status=SyncStatus.running)
    session.add(run)
    await session.flush()

    try:
        if data is None:
            client = client or BackupJsonClient()
            data = await client.fetch_backup(panel)

        now = _now()
        await _upsert_resellers(session, panel, data, now)
        await _upsert_users(session, panel, data, now)

        # Capture/refresh the end-user (client) secret path used to build customers' sub links
        # (differs from the admin proxy path in Hiddify v12).
        if data.client_proxy_path and data.client_proxy_path != panel.client_proxy_path:
            panel.client_proxy_path = data.client_proxy_path

        panel.last_synced_at = now
        panel.status = PanelStatus.ok
        panel.last_error = None

        run.status = SyncStatus.success
        run.admin_count = len(data.admins)
        run.user_count = len(data.users)
        run.finished_at = now
        await session.commit()
        log.info(
            "Synced panel '%s': %d admins, %d users", panel.key, run.admin_count, run.user_count
        )
    except Exception as exc:  # noqa: BLE001
        # The flushed run row is gone after rollback; record a fresh failure row.
        await session.rollback()
        err = str(exc)[:1000]
        current_panel = await session.get(Panel, panel_id)  # re-attach after rollback
        if current_panel is not None:
            current_panel.status = PanelStatus.error
            current_panel.last_error = err
        run = SyncRun(
            panel_id=panel_id,
            source=source,
            status=SyncStatus.failed,
            error=err,
            finished_at=_now(),
        )
        session.add(run)
        await session.commit()
        log.exception("Sync failed for panel '%s'", getattr(current_panel, "key", "?"))

    return run


async def _upsert_resellers(
    session: AsyncSession, panel: Panel, data: PanelData, now: dt.datetime
) -> None:
    existing = {
        r.admin_uuid: r
        for r in (
            await session.execute(select(Reseller).where(Reseller.panel_id == panel.id))
        ).scalars()
    }
    seen_uuids: set[str] = set()
    for a in data.admins:
        seen_uuids.add(a.uuid)
        r = existing.get(a.uuid)
        if r is None:
            r = Reseller(
                panel_id=panel.id,
                admin_uuid=a.uuid,
                exclude_from_billing=((a.comment or "").strip() == "-"),
            )
            session.add(r)
        r.name = a.name
        r.parent_admin_uuid = a.parent_admin_uuid
        r.mode = a.mode
        r.comment = a.comment
        r.is_owner = a.is_owner
        r.panel_telegram_id = a.telegram_id
        # While a reseller is suspended/frozen its ON-PANEL limits are the ZEROED enforcement
        # values, not its real quota — recording them would make the capacity UI lie and, worse,
        # feed a restore-from-DB fallback the zeros (restore-zeros, the M38 bug class). Only
        # refresh panel_max_* while the reseller is active; a real quota change during suspension
        # lands on the next sync after restore. (A brand-new row's state is not yet defaulted →
        # treat None as active so its first limits ARE recorded.)
        if r.enforcement_state in (None, EnforcementState.active):
            r.panel_max_users = a.max_users
            r.panel_max_active_users = a.max_active_users
        r.can_add_admin = a.can_add_admin
        r.last_seen_at = now

    # If the panel's owner UUID changed (e.g. restored backup on a new server), the old
    # owner row is no longer in data.admins.  Delete it — owner rows are never billed and
    # have no invoices/payments/enforcement actions, so there is nothing to orphan.
    for uuid, r in existing.items():
        if r.is_owner and uuid not in seen_uuids:
            await session.delete(r)


async def _upsert_users(
    session: AsyncSession, panel: Panel, data: PanelData, now: dt.datetime
) -> None:
    from app.models import UsageMeter
    from app.services import metering

    existing = {
        s.user_uuid: s
        for s in (
            await session.execute(
                select(EndUserSnapshot).where(EndUserSnapshot.panel_id == panel.id)
            )
        ).scalars()
    }
    period_label = now.strftime("%Y-%m")
    metering_on = await metering.is_enabled(session)
    meters = await metering.load_period_meters(session, panel.id, period_label) if metering_on else {}

    for u in data.users:
        s = existing.get(u.uuid)
        if s is None:
            s = EndUserSnapshot(panel_id=panel.id, user_uuid=u.uuid)
            session.add(s)

        # Meter from the DELTA between the stored snapshot (prev) and the new values —
        # must run BEFORE we overwrite the snapshot's usage fields below.
        meter_ok = True
        if metering_on:
            try:
                meter = meters.get(u.uuid)
                if meter is None:
                    meter = UsageMeter(panel_id=panel.id, user_uuid=u.uuid, period_label=period_label)
                    session.add(meter)
                    meters[u.uuid] = meter
                metering.apply(
                    snapshot=s, meter=meter,
                    prev_limit=float(s.usage_limit_gb or 0), prev_used=float(s.current_usage_gb or 0),
                    new_limit=float(u.usage_limit_gb or 0), new_used=float(u.current_usage_gb or 0),
                    start_date=u.start_date, added_by_uuid=u.added_by_uuid, name=u.name,
                    period_label=period_label,
                )
            except Exception:  # noqa: BLE001 — metering must never break a sync
                meter_ok = False
                log.warning("metering.apply failed for user %s", u.uuid, exc_info=True)

        s.name = u.name
        s.added_by_uuid = u.added_by_uuid
        # F10: only advance the metering BASELINE (usage/limit) when metering succeeded (or is off).
        # If metering.apply raised, keeping the prev values means the NEXT sync recomputes the full
        # combined delta — otherwise the failed interval's usage/overage would be lost forever.
        if meter_ok:
            s.usage_limit_gb = u.usage_limit_gb
            s.current_usage_gb = u.current_usage_gb
        s.start_date = u.start_date
        s.package_days = u.package_days
        s.enable = u.enable
        s.is_active = u.is_active
        s.mode = u.mode
        s.last_online = u.last_online
        s.comment = u.comment
        s.last_synced_at = now


async def sync_all(session: AsyncSession) -> list[SyncRun]:
    panels = (
        await session.execute(select(Panel).where(Panel.enabled.is_(True)))
    ).scalars().all()
    runs: list[SyncRun] = []
    for panel in panels:
        try:
            runs.append(await sync_panel(session, panel))
        except Exception:  # noqa: BLE001 — one bad panel must not abort the rest
            log.exception("sync_all: panel %s failed", getattr(panel, "key", "?"))
            await session.rollback()
    return runs
