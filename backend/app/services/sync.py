"""
Sync a panel's backup into our DB: upsert resellers (admins) and end-user snapshots.

Idempotent: existing rows are updated in place, new ones inserted. `exclude_from_billing`
is seeded once (from a "-" comment) on insert and never overwritten afterwards, so the
owner's manual toggle is preserved across syncs.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Panel, Reseller, SyncRun
from app.models.enums import PanelStatus, SyncSource, SyncStatus
from app.services.panel_client import BackupJsonClient, PanelClient, PanelData

log = logging.getLogger("sync")


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
    for a in data.admins:
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
        r.panel_max_users = a.max_users
        r.panel_max_active_users = a.max_active_users
        r.can_add_admin = a.can_add_admin
        r.last_seen_at = now


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
                log.warning("metering.apply failed for user %s", u.uuid, exc_info=True)

        s.name = u.name
        s.added_by_uuid = u.added_by_uuid
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
