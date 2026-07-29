"""Delete end-users on the Hiddify panel AND purge their local billing rows, in one operation.

Exists because the two halves were disconnected: `AdminApiClient.delete_user` removes the user from
the panel but leaves our snapshot billing it (sync never hard-deletes end-user snapshots), while
`POST /api/tools/end-users/{id}/remove` drops the local rows but the next sync re-adds them from the
panel. Removing a mistakenly-created user therefore took two manual steps in two places.

ORDER IS FIXED AND NOT NEGOTIABLE: panel first, verify, DB last. Purging first would destroy the
`user_uuid`/`added_by_uuid` needed to address the panel — and the evidence needed to notice a
failure. A crash between the panel delete and the commit leaves the local row intact, which is the
safe direction (over-billing that a re-run fixes, never lost data); the operation is idempotent, so
the re-run sees `already_absent` and completes the purge.

TWO KEYS, TWO PURPOSES (`admin_api._headers`: `api_key=None` falls back to the panel's super-admin):
  * WRITES always use the OWNING admin's uuid, so Hiddify itself refuses a rowid that isn't theirs —
    the second line of defence after the 2026-07-18 incident, where a stale numeric id executed as
    super-admin hit 305 users belonging to ~20 other resellers.
  * EXISTENCE CHECKS use the super-admin key, because only it can prove a user is really gone. A 404
    under the owning admin's key is ambiguous — "absent" OR "no longer owned by that admin" — and
    purging on that alone would drop a row for a user still on the panel, still billed to somebody.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    Panel,
    StorefrontOrder,
    UsageMeter,
    UsageMeterEvent,
)
from app.services.panel_client.admin_api import AdminApiClient

log = logging.getLogger("service.end_user_delete")

_ID_LOOKUP_CONCURRENCY = 8
# Rowids per Flask-Admin bulk POST. Do NOT widen casually: `_user_bulk_action` GETs the panel's whole
# `/admin/user/` HTML page on every call just to scrape a CSRF token, so each call is expensive.
_BULK_CHUNK = 200
# Storefront order statuses that represent a live, paid service (mirrors storefront_subscription).
_LIVE_ORDER_STATUSES = ("provisioned", "disabled", "renewing")


class DeleteStatus(str, Enum):
    deleted = "deleted"                        # was present, delete VERIFIED, purged locally
    already_absent = "already_absent"          # absent for BOTH keys → purged locally
    owner_mismatch = "owner_mismatch"          # 404 as owner but PRESENT as super-admin → kept
    lookup_failed = "lookup_failed"            # panel error resolving the id → nothing touched
    delete_failed = "delete_failed"            # the bulk action raised → nothing touched
    verify_failed = "verify_failed"            # still present after the delete, or verify errored
    skipped_low_quota = "skipped_low_quota"    # below the caller's quota guard
    skipped_no_owner = "skipped_no_owner"      # no added_by_uuid → refuse (never write as super-admin)
    skipped_storefront = "skipped_storefront"  # belongs to a live, paid storefront order
    panel_unavailable = "panel_unavailable"    # the snapshot's panel row is gone
    not_found = "not_found"                    # snapshot_id no longer exists


_PURGEABLE = (DeleteStatus.deleted, DeleteStatus.already_absent)
_FAILED = (
    DeleteStatus.owner_mismatch, DeleteStatus.lookup_failed,
    DeleteStatus.delete_failed, DeleteStatus.verify_failed,
    DeleteStatus.panel_unavailable, DeleteStatus.not_found,
)
_SKIPPED = (
    DeleteStatus.skipped_low_quota, DeleteStatus.skipped_no_owner,
    DeleteStatus.skipped_storefront,
)


@dataclass(slots=True)
class EndUserDeleteRow:
    snapshot_id: int
    user_uuid: str          # FULL uuid — internal; the API truncates it like the list does
    name: str
    panel_key: str
    usage_limit_gb: float
    status: DeleteStatus
    purged: bool = False    # local rows actually deleted
    error: str | None = None


@dataclass(slots=True)
class DeleteBatchResult:
    rows: list[EndUserDeleteRow] = field(default_factory=list)
    meters_deleted: int = 0

    def _count(self, *statuses: DeleteStatus) -> int:
        return sum(1 for r in self.rows if r.status in statuses)

    @property
    def deleted(self) -> int:
        return self._count(DeleteStatus.deleted)

    @property
    def already_absent(self) -> int:
        return self._count(DeleteStatus.already_absent)

    @property
    def purged(self) -> int:
        return sum(1 for r in self.rows if r.purged)

    @property
    def skipped(self) -> int:
        return self._count(*_SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(*_FAILED)


async def _live_storefront_uuids(
    session: AsyncSession, panel_id: int, user_uuids: list[str]
) -> set[str]:
    """Lowercased panel-user uuids on this panel that back a LIVE, paid storefront order.

    Those configs were bought by a shop customer; deleting one here would kill a paying end-user's
    service and strand the order, bypassing the refund/bookkeeping in `storefront_subscription`.
    Refuse them and let the owner handle it in the storefront flow."""
    if not user_uuids:
        return set()
    rows = (
        await session.execute(
            select(StorefrontOrder.panel_user_uuid).where(
                StorefrontOrder.panel_id == panel_id,
                StorefrontOrder.panel_user_uuid.in_(user_uuids),
                StorefrontOrder.status.in_(_LIVE_ORDER_STATUSES),
            )
        )
    ).scalars().all()
    return {(u or "").lower() for u in rows if u}


async def delete_end_users(
    session: AsyncSession,
    snapshot_ids: Sequence[int],
    *,
    min_usage_limit_gb: float | None = None,
    client: AdminApiClient | None = None,
    concurrency: int = _ID_LOOKUP_CONCURRENCY,
) -> DeleteBatchResult:
    """Delete each snapshot's end-user on its panel, then purge the local billing rows.

    A local row is dropped ONLY when the panel is proven to no longer hold that user. Any panel
    failure leaves that row completely untouched — including its meters — so a failed run never
    hides a user that is still billing. Errors are isolated per row (and per owning admin), so one
    unreachable panel or one rejected delete never aborts the batch.

    `min_usage_limit_gb` refuses snapshots below that sold quota. The caller passes the same
    threshold the list was built with: the ids are client-supplied, and this confines an
    irreversible operation to the class of users it is meant for.
    """
    client = client or AdminApiClient()
    result = DeleteBatchResult()
    if not snapshot_ids:
        return result

    unique_ids = list(dict.fromkeys(int(i) for i in snapshot_ids))
    snapshots = (
        await session.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.id.in_(unique_ids))
        )
    ).scalars().all()
    by_id = {s.id: s for s in snapshots}

    rows: dict[int, EndUserDeleteRow] = {}
    panels: dict[int, Panel] = {}
    work: dict[int, list[EndUserSnapshot]] = defaultdict(list)

    for sid in unique_ids:
        snap = by_id.get(sid)
        if snap is None:
            rows[sid] = EndUserDeleteRow(sid, "", "", "", 0.0, DeleteStatus.not_found)
            continue
        panel = panels.get(snap.panel_id)
        if panel is None:
            panel = await session.get(Panel, snap.panel_id)
            if panel is not None:
                panels[snap.panel_id] = panel
        gb = float(snap.usage_limit_gb or 0)
        row = EndUserDeleteRow(
            snapshot_id=sid, user_uuid=snap.user_uuid or "", name=snap.name or "",
            panel_key=(panel.key if panel else ""), usage_limit_gb=gb,
            status=DeleteStatus.not_found,
        )
        rows[sid] = row
        if min_usage_limit_gb is not None and gb < float(min_usage_limit_gb):
            row.status = DeleteStatus.skipped_low_quota
            continue
        if not (snap.added_by_uuid or "").strip():
            # Without an owning admin we could only write as super-admin — exactly the unscoped
            # write that made a stale id catastrophic in 2026-07-18. Refuse instead.
            row.status = DeleteStatus.skipped_no_owner
            continue
        if panel is None:
            row.status = DeleteStatus.panel_unavailable
            continue
        work[snap.panel_id].append(snap)

    for panel_id, snaps in work.items():
        panel = panels[panel_id]
        protected = await _live_storefront_uuids(
            session, panel_id, [s.user_uuid for s in snaps if s.user_uuid]
        )
        pending = []
        for snap in snaps:
            if (snap.user_uuid or "").lower() in protected:
                rows[snap.id].status = DeleteStatus.skipped_storefront
                continue
            pending.append(snap)
        if not pending:
            continue

        sem = asyncio.Semaphore(max(1, concurrency))

        # ── A. Resolve ids FRESH and owner-scoped ─────────────────────────────
        # Never reuse the cached `EndUserSnapshot.panel_user_id`: Hiddify renumbers user ids on a
        # panel restore/re-import, and a stale id here would delete somebody else's customer.
        async def _resolve(snap: EndUserSnapshot) -> tuple[int, int | None, DeleteStatus | None, str | None]:
            async with sem:
                owner = snap.added_by_uuid
                try:
                    uid = await client.get_user_id(panel, snap.user_uuid, api_key=owner)
                except Exception as exc:  # noqa: BLE001 — panel I/O; isolate this row
                    return snap.id, None, DeleteStatus.lookup_failed, str(exc)[:300]
                if uid is not None:
                    return snap.id, uid, None, None
                # Ambiguous 404: absent, or simply not owned by that admin any more. Only the
                # super-admin key can tell those apart, and purging the wrong one loses revenue.
                try:
                    confirm = await client.get_user_id(panel, snap.user_uuid)
                except Exception as exc:  # noqa: BLE001
                    return snap.id, None, DeleteStatus.lookup_failed, str(exc)[:300]
                if confirm is None:
                    return snap.id, None, DeleteStatus.already_absent, None
                return snap.id, None, DeleteStatus.owner_mismatch, (
                    "user exists on the panel but is no longer owned by this admin"
                )

        resolved = await asyncio.gather(*[_resolve(s) for s in pending])
        by_sid = {s.id: s for s in pending}
        ids_by_owner: dict[str, list[tuple[int, int]]] = defaultdict(list)  # owner → [(sid, rowid)]
        for sid, uid, status, err in resolved:
            if status is not None:
                rows[sid].status = status
                rows[sid].error = err
                continue
            ids_by_owner[by_sid[sid].added_by_uuid or ""].append((sid, cast(int, uid)))

        # ── B. Delete, grouped per owning admin ───────────────────────────────
        deleted_sids: list[int] = []
        for owner, pairs in ids_by_owner.items():
            for start in range(0, len(pairs), _BULK_CHUNK):
                chunk = pairs[start:start + _BULK_CHUNK]
                try:
                    await client.bulk_delete_users(
                        panel, [uid for _sid, uid in chunk], api_key=owner
                    )
                except Exception as exc:  # noqa: BLE001 — only this owner's chunk is affected
                    for sid, _uid in chunk:
                        rows[sid].status = DeleteStatus.delete_failed
                        rows[sid].error = str(exc)[:300]
                    continue
                deleted_sids.extend(sid for sid, _uid in chunk)

        # ── C. Verify EVERY delete (super-admin key) ──────────────────────────
        # A 200 from the bulk action proves nothing — rowids that match no row also return 200.
        # Batches here are small and each delete is individually targeted, so per-row proof is cheap
        # and makes "purge nothing on failure" exact rather than statistical.
        async def _verify(sid: int) -> tuple[int, DeleteStatus, str | None]:
            async with sem:
                try:
                    still = await client.get_user_id(panel, by_sid[sid].user_uuid)
                except Exception as exc:  # noqa: BLE001
                    return sid, DeleteStatus.verify_failed, f"unverified: {str(exc)[:280]}"
                if still is None:
                    return sid, DeleteStatus.deleted, None
                return sid, DeleteStatus.verify_failed, "still present on the panel after delete"

        for sid, status, err in await asyncio.gather(*[_verify(s) for s in deleted_sids]):
            rows[sid].status = status
            rows[sid].error = err

        # ── D. Purge the proven-gone rows ─────────────────────────────────────
        purge = [
            by_sid[sid] for sid in by_sid
            if rows[sid].status in _PURGEABLE
        ]
        if purge:
            uuids = [s.user_uuid for s in purge]
            # All three tables: the snapshot feeds the sold-quota base, the meters feed the
            # metering extras. Dropping only the snapshot leaves the user billing forever.
            res = await session.execute(
                sa_delete(UsageMeter).where(
                    UsageMeter.panel_id == panel_id, UsageMeter.user_uuid.in_(uuids)
                )
            )
            result.meters_deleted += cast("CursorResult[Any]", res).rowcount or 0
            await session.execute(
                sa_delete(UsageMeterEvent).where(
                    UsageMeterEvent.panel_id == panel_id, UsageMeterEvent.user_uuid.in_(uuids)
                )
            )
            await session.execute(
                sa_delete(EndUserSnapshot).where(
                    EndUserSnapshot.id.in_([s.id for s in purge])
                )
            )
            for snap in purge:
                rows[snap.id].purged = True
                # The only forensic record of an irreversible owner action.
                log.warning(
                    "end-user purged: snapshot=%s panel=%s uuid=%s owner=%s quota=%sGB status=%s",
                    snap.id, panel.key, snap.user_uuid, snap.added_by_uuid,
                    snap.usage_limit_gb, rows[snap.id].status.value,
                )

    await session.commit()
    result.rows = [rows[sid] for sid in unique_ids]
    return result
