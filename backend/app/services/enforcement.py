"""
Enforcement: suspend a delinquent reseller (disable their + sub-resellers' users,
zero their admin limits) and restore exactly on payment.

Safety: controlled by the `enforcement_enabled` setting. When False (default), runs
in DRY-RUN — it records what it *would* do (EnforcementAction with dry_run=True) and
makes no panel writes. Set it True to perform live writes (needs panel admin API keys).
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy

from sqlalchemy import case, func, select, text
from sqlalchemy import delete as _sa_delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.core.db import SessionLocal
from app.models import EndUserSnapshot, EnforcementAction, Panel, Reseller, UsageMeter
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
)
from app.services import settings_service
from app.services.invoice_engine import build_children_map, collect_descendants
from app.services.panel_client.admin_api import AdminApiClient
from app.services.periods import today as tehran_today

log = logging.getLogger("enforcement")

_MAX_RETRIES = 5
# How many per-user id lookups to run concurrently against ONE panel — small, to stay gentle.
_ID_LOOKUP_CONCURRENCY = 8

# Advisory-lock key serializing whole enforcement-queue runs (manual endpoint vs the 5-min
# scheduler tick). Adjacent to invoicing._BILLING_LOCK_KEY (734_137_044).
_QUEUE_LOCK_KEY = 734_137_045


# ── low-level helpers ────────────────────────────────────────────────────────

async def _bundle(session: AsyncSession, reseller: Reseller) -> list[Reseller]:
    """The reseller + all descendant sub-resellers (same panel)."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)
    return collect_descendants(reseller, children)


async def _get_admin_limits_safe(
    client: AdminApiClient, panel, admin: Reseller
) -> tuple[int | None, int | None]:
    """Read an admin's current limits from the panel. Returns (None, None) on any error."""
    try:
        return await client.get_admin_limits(
            panel, admin.admin_uuid, api_key=admin.parent_admin_uuid
        )
    except Exception:  # noqa: BLE001
        return None, None


async def _set_admin_limits(
    client: AdminApiClient, panel, admin: Reseller, mu: int, mau: int
) -> None:
    """Set an admin's limits, trying parent-UUID auth first then falling back to panel key."""
    if admin.parent_admin_uuid:
        try:
            await client.set_admin_limits(
                panel, admin.admin_uuid, mu, mau, api_key=admin.parent_admin_uuid
            )
            return
        except Exception:  # noqa: BLE001
            pass
    await client.set_admin_limits(panel, admin.admin_uuid, mu, mau)


async def _enabled_users(
    session: AsyncSession, panel_id: int, admin_uuids: set[str]
) -> list[EndUserSnapshot]:
    rows = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == panel_id,
                # Case-INSENSITIVE like the delete cascade and the billing report. With a raw
                # `.in_()` a row written before uuid normalization shipped was silently skipped
                # here while still being deleted/billed — i.e. a debtor kept users online.
                func.lower(EndUserSnapshot.added_by_uuid).in_({u.lower() for u in admin_uuids}),
                EndUserSnapshot.enable.is_(True),
            )
        )
    ).scalars().all()
    return list(rows)


def _progress(snapshot: dict | None) -> dict:
    if snapshot is None:
        snapshot = {}
    p = snapshot.setdefault("progress", {})
    p.setdefault("users_done", [])
    p.setdefault("users_failed", {})
    p.setdefault("users_missing", [])
    p.setdefault("admins_done", [])
    p.setdefault("admins_failed", {})
    p.setdefault("admin_attempts", {})
    p.setdefault("user_attempts", {})
    p.setdefault("captured_limits", {})
    p.setdefault("admins_missing", [])
    p.setdefault("phase", "users")
    return p


async def _has_due_invoice(session: AsyncSession, reseller_id: int) -> bool:
    """Re-check debt at execution time so a stale queue item cannot suspend a paid reseller."""
    from app.models import Invoice
    from app.models.enums import InvoiceStatus

    owed = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
    today = tehran_today()
    invoices = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller_id,
                Invoice.status.in_(owed),
            )
        )
    ).scalars().all()
    return any(not (inv.deferred_until and inv.deferred_until > today) for inv in invoices)


async def _queued_snapshot(session: AsyncSession, reseller: Reseller) -> dict:
    """Build a DB-local work snapshot without writing to the panel."""
    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        raise ValueError("panel not found for reseller")
    descendants = await _bundle(session, reseller)
    admin_uuids = {d.admin_uuid for d in descendants}
    users = await _enabled_users(session, panel.id, admin_uuids)
    snapshot: dict = {
        "limits": {
            d.admin_uuid: {
                "max_users": d.panel_max_users,
                "max_active_users": d.panel_max_active_users,
            }
            for d in descendants
        },
        "admins": [d.admin_uuid for d in descendants],
        "users": {u.user_uuid: (u.added_by_uuid or "") for u in users},
    }
    _progress(snapshot)
    return snapshot


# ── inner worker helpers ─────────────────────────────────────────────────────

class _RevertedMidFlight(Exception):
    """Raised inside a suspend/freeze worker when the action's DB row was flipped to
    `reverted` by a concurrent payment/defer (queue_restore in another session). The worker's
    in-memory object is stale (sessions are expire_on_commit=False), so without this check it
    would keep disabling users, finalize `done` over the `reverted` status, and stamp a
    now-PAID invoice as enforced — resurrecting settled debt as overdue after the restore."""


async def _current_action_status(
    session: AsyncSession, action_id: int
) -> EnforcementActionStatus | None:
    """The action's CURRENT status as committed in the DB (core-column select, so the ORM
    identity map's stale in-memory object is bypassed)."""
    return (
        await session.execute(
            select(EnforcementAction.status).where(EnforcementAction.id == action_id)
        )
    ).scalar_one_or_none()


async def _merge_into_pending_restore(session: AsyncSession, action_id: int) -> None:
    """A suspend/freeze was reverted mid-run: the payment-triggered restore copied the
    progress it could see at that instant, but this worker may have completed one more chunk
    after the copy — those users/admins are missing from the restore's work set and would
    stay disabled/zeroed forever. Union the source's FINAL committed progress into the
    still-`planned` restore (the same panel lane processes actions serially, so the restore
    cannot have started; if it unexpectedly has, log a warning — its own retry logic covers
    partially-applied work). Takes the id (not the ORM object): the caller rolled back,
    which expires the in-memory object — re-load the committed truth."""
    action = await session.get(EnforcementAction, action_id)
    if action is None:
        return
    src_snapshot = action.snapshot or {}
    src_progress = _progress(src_snapshot)
    src_users = dict(src_snapshot.get("users") or {})
    applied_users = (
        set(src_progress.get("users_done") or []) - set(src_progress.get("users_missing") or [])
    )
    captured = dict(src_progress.get("captured_limits") or src_snapshot.get("limits") or {})
    applied_admins = {u for u in (src_progress.get("admins_done") or []) if u in captured}
    if not applied_users and not applied_admins:
        return  # nothing was applied — nothing to undo

    candidates = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == action.reseller_id,
                EnforcementAction.action == EnforcementActionType.restore,
                EnforcementAction.status.in_(
                    [EnforcementActionStatus.planned, EnforcementActionStatus.partial]
                ),
            )
            .order_by(EnforcementAction.created_at.desc(), EnforcementAction.id.desc())
            .with_for_update()
        )
    ).scalars().all()
    restore = next(
        (r for r in candidates if (r.snapshot or {}).get("source_action_id") == action.id),
        None,
    )
    if restore is None:
        # The revert landed before ANY progress had been committed, so queue_restore saw an
        # empty work set and created no restore — but this worker applied chunks right after
        # the copy. Create the restore NOW from the source's final committed progress so
        # that work is undone (otherwise those users would stay disabled forever).
        src_order = list(src_snapshot.get("admins") or [])
        admins = [u for u in src_order if u in applied_admins]
        admins += [u for u in applied_admins if u not in src_order]
        limits = {u: captured[u] for u in applied_admins}
        users = {u: src_users.get(u, "") for u in applied_users}
        restore = EnforcementAction(
            reseller_id=action.reseller_id,
            invoice_id=action.invoice_id,
            action=EnforcementActionType.restore,
            dry_run=False,
            status=EnforcementActionStatus.planned,
            snapshot={
                "limits": limits, "admins": admins, "users": users,
                "source_action_id": action.id,
                "require_no_due": False, "reason": "reverted-mid-flight",
                "progress": {
                    "phase": "limits",
                    "users_done": [], "users_missing": [], "users_failed": {},
                    "user_attempts": {}, "admins_done": [], "admins_missing": [],
                    "admins_failed": {}, "admin_attempts": {},
                    "captured_limits": limits,
                },
            },
        )
        session.add(restore)
        await session.commit()
        log.info(
            "created restore %s for mid-flight-reverted action %s (%d users, %d admins)",
            restore.id, action.id, len(users), len(admins),
        )
        return
    if restore.status != EnforcementActionStatus.planned:
        log.warning(
            "restore %s already started while merging mid-flight progress of %s; "
            "late chunks are appended and will be retried by the restore's own loop",
            restore.id, action.id,
        )

    snap = deepcopy(restore.snapshot or {})
    merged_users = dict(snap.get("users") or {})
    for uuid in applied_users:
        merged_users.setdefault(uuid, src_users.get(uuid, ""))
    merged_limits = dict(snap.get("limits") or {})
    for uuid in applied_admins:
        merged_limits.setdefault(uuid, captured[uuid])
    # Keep the restore's top-down order: rebuild from the source's root→leaf admin list.
    admin_set = set(snap.get("admins") or []) | set(merged_limits)
    src_order = list(src_snapshot.get("admins") or [])
    merged_admins = [u for u in src_order if u in admin_set]
    merged_admins += [u for u in admin_set if u not in src_order]

    snap["users"] = merged_users
    snap["limits"] = merged_limits
    snap["admins"] = merged_admins
    prog = _progress(snap)
    prog["captured_limits"] = merged_limits
    restore.snapshot = snap
    flag_modified(restore, "snapshot")
    await session.commit()
    log.info(
        "merged mid-flight progress of reverted action %s into restore %s "
        "(%d users, %d admins)", action.id, restore.id, len(merged_users), len(merged_admins),
    )


async def _run_user_chunks(
    *,
    session: AsyncSession,
    action: EnforcementAction,
    client: AdminApiClient,
    panel,
    snapshot: dict,
    progress: dict,
    users_map: dict[str, str],
    done_users: set[str],
    missing_users: set[str],
    failed_users: dict[str, str],
    user_attempts: dict[str, int],
    enable: bool,
    chunk_size: int,
) -> tuple[int, bool]:
    """Disable or enable all remaining users in a loop, committing after each chunk.

    Returns (users_patched, had_error). On error the progress is persisted so the
    next worker tick resumes from exactly where this one stopped — never repeating
    a chunk that already succeeded.
    """
    remaining = [u for u in users_map if u not in done_users and u not in missing_users]
    if not remaining:
        return 0, False

    # Snapshot rows for all targets up front: they hold the cached numeric id (panel_user_id) and
    # we flip their `enable` flag after a successful chunk.
    snapshot_rows: dict[str, EndUserSnapshot] = {
        r.user_uuid: r
        for r in (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.panel_id == panel.id,
                    EndUserSnapshot.user_uuid.in_(remaining),
                )
            )
        ).scalars().all()
    }

    # Resolve UUID → Hiddify numeric id for ONLY the target users (NEVER the whole panel list,
    # which 503s on large panels).
    #
    # CRITICAL — the numeric id is VOLATILE; only the uuid is a stable identity. Hiddify renumbers
    # its user table whenever the panel is restored/re-imported, so a numeric id captured on an
    # earlier run can later belong to a COMPLETELY DIFFERENT user (possibly another reseller's).
    # A durable cross-run cache once caused a suspension to disable 305 innocent users of ~20 other
    # resellers while leaving the target's own users enabled — and the panel still answered 200, so
    # it was recorded as success. Therefore we resolve every id FRESH from the panel each run and
    # NEVER seed from the durable `EndUserSnapshot.panel_user_id` (kept only as a forensic
    # last-resolved value). Nothing is cached between passes at all — see below.
    # NOT seeded from, and NOT persisted into, the action snapshot either. It used to be kept there
    # as a crash-resume cache and only stripped on SUCCESSFUL finalize — so an action that ended
    # `partial`/`failed` (panel down, retry-exhausted lookup, worker restart) kept the ids in the DB
    # and reseeded from them on the next tick, hours or days later. That is the original incident by
    # another route. Ids live for exactly one pass now; a resume re-resolves.
    panel_user_ids: dict[str, int] = {}

    to_lookup = [u for u in remaining if u not in panel_user_ids]
    lookup_failed = False
    if to_lookup:
        sem = asyncio.Semaphore(_ID_LOOKUP_CONCURRENCY)

        async def _resolve(uuid: str) -> tuple[str, int | None, str | None]:
            async with sem:
                try:
                    return uuid, await client.get_user_id(panel, uuid), None
                except Exception as exc:  # noqa: BLE001 — a single user's lookup failed
                    return uuid, None, str(exc)[:300]

        for uuid, uid, err in await asyncio.gather(*[_resolve(u) for u in to_lookup]):
            if err is not None:
                user_attempts[uuid] = user_attempts.get(uuid, 0) + 1
                failed_users[uuid] = err
                action.error = f"user id lookup failed (will retry): {err}"
                if user_attempts[uuid] >= _MAX_RETRIES:
                    # Give up on this one user (skip) so it can't block the batch forever.
                    missing_users.add(uuid)
                    done_users.add(uuid)
                else:
                    lookup_failed = True
                continue
            failed_users.pop(uuid, None)
            if uid is None:
                # 404 → user absent on the panel; skip it (we only act on present users).
                missing_users.add(uuid)
                done_users.add(uuid)
            else:
                panel_user_ids[uuid] = uid
                row = snapshot_rows.get(uuid)
                if row is not None:
                    row.panel_user_id = uid  # cache durably → next time zero lookups
        # Persist resolved ids + progress so a restart resumes without re-looking-up.
        progress["users_done"] = sorted(done_users)
        progress["users_missing"] = sorted(missing_users)
        progress["users_failed"] = failed_users
        progress["user_attempts"] = user_attempts
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()

    # Act only on users we resolved an id for; 404-missing and not-yet-retried lookup errors are
    # skipped this run (the latter keep the action partial → retried next tick).
    remaining = [u for u in remaining if u in panel_user_ids and u not in missing_users]

    total_patched = 0
    verb = "enable" if enable else "disable"
    while remaining:
        chunk = remaining[:max(1, chunk_size)]
        try:
            await client.bulk_set_users_enabled(
                panel, [panel_user_ids[u] for u in chunk], enable
            )
            for uuid in chunk:
                if uuid in snapshot_rows:
                    snapshot_rows[uuid].enable = enable
                done_users.add(uuid)
                failed_users.pop(uuid, None)
            total_patched += len(chunk)
            action.error = None
        except Exception as exc:  # noqa: BLE001
            for uuid in chunk:
                user_attempts[uuid] = user_attempts.get(uuid, 0) + 1
                failed_users[uuid] = str(exc)[:300]
            if any(user_attempts[uuid] >= _MAX_RETRIES for uuid in chunk):
                action.status = EnforcementActionStatus.failed
                action.error = f"bulk {verb} failed: {str(exc)[:900]}"
            else:
                action.error = f"bulk {verb} failed (will retry): {str(exc)[:600]}"
            progress["users_done"] = sorted(done_users)
            progress["users_missing"] = sorted(missing_users)
            progress["users_failed"] = failed_users
            progress["user_attempts"] = user_attempts
            action.affected_count = len(done_users - missing_users)
            action.snapshot = snapshot
            flag_modified(action, "snapshot")
            await session.commit()
            return total_patched, True

        # Commit after each successful chunk — a restart resumes from here rather than
        # re-disabling/re-enabling users that already succeeded.
        progress["users_done"] = sorted(done_users)
        progress["users_missing"] = sorted(missing_users)
        progress["users_failed"] = failed_users
        progress["user_attempts"] = user_attempts
        action.affected_count = len(done_users - missing_users)
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()
        remaining = [u for u in remaining if u not in done_users]

        # A payment can land while chunks are flying: queue_restore (another session) flips a
        # SUSPEND action to `reverted`. Notice it before the next panel write — a reverted
        # suspension must stop disabling users. (Restores never self-abort; the last chunk is
        # covered by the caller's pre-finalize check.)
        if not enable and action.action == EnforcementActionType.disable_users and remaining:
            current = await _current_action_status(session, action.id)
            if current == EnforcementActionStatus.reverted:
                raise _RevertedMidFlight

    # `lookup_failed` (a transient per-user id lookup error, not yet retry-exhausted) keeps the
    # action partial so the unresolved users are retried next tick.
    return total_patched, lookup_failed


async def _verify_user_states(
    client: AdminApiClient, panel, uuids: list[str], *, expect_enabled: bool,  # noqa: ANN001
    sample: int = 20,
) -> tuple[int, int]:
    """Read a spread-out sample of the users we just wrote back from the panel.

    Hiddify's bulk action answers 200 even when it changed nothing we intended — if the rowids we
    sent no longer belong to our users, the write "succeeds" while missing every target (and hitting
    somebody else). Reading back by UUID is the only way to turn that silent failure into a loud
    one. Returns (in_expected_state, wrong). Users absent from the panel are ignored.
    """
    if not uuids:
        return 0, 0
    step = max(1, len(uuids) // sample)
    ok = wrong = 0
    for uu in uuids[::step][:sample]:
        try:
            data = await client.get_user(panel, uu)
        except Exception:  # noqa: BLE001 — a flaky read must not fail the action by itself
            continue
        if data is None:
            continue
        if bool(data.get("enable")) == expect_enabled:
            ok += 1
        else:
            wrong += 1
    return ok, wrong


async def _fail_if_writes_missed(
    session: AsyncSession, action: EnforcementAction, client: AdminApiClient, panel,  # noqa: ANN001
    written_uuids: list[str], *, expect_enabled: bool,
) -> bool:
    """Verify a sample actually reached the intended state; fail the action loudly if it didn't.

    Guards the catastrophic silent-miss mode (stale panel ids → the bulk write lands on the wrong
    users). A MAJORITY-wrong sample means a systemic miss, not a one-off drift, so we refuse to
    record success. Returns True when the action was failed."""
    ok, wrong = await _verify_user_states(
        client, panel, written_uuids, expect_enabled=expect_enabled)
    # Fail on ANY meaningful drift, not just a majority. The guarded failure is catastrophic and
    # asymmetric (a mis-addressed bulk write hits other tenants), so a "majority wrong" bar was far
    # too lenient — a 40%-wrong sample would still have been recorded as success. One mismatch is
    # tolerated because a reseller can legitimately flip a single user between our write and this
    # read; two independent mismatches in a small sample is a systemic miss.
    if wrong < 2 and not (wrong and ok == 0):
        return False
    verb = "enabled" if expect_enabled else "disabled"
    action.status = EnforcementActionStatus.failed
    action.error = (
        f"verification failed: {wrong} of {wrong + ok} sampled users are NOT {verb} after the bulk "
        "write. The panel's user ids may have been renumbered (restore/re-import). Refusing to "
        "report success — re-run after a fresh sync."
    )
    log.error("enforcement action %s verification failed: %s", action.id, action.error)
    await session.commit()
    return True


async def _run_admin_limits(
    *,
    session: AsyncSession,
    action: EnforcementAction,
    client: AdminApiClient,
    panel,
    snapshot: dict,
    progress: dict,
    by_uuid: dict[str, Reseller],
    admin_order: list[str],
    done_admins: set[str],
    failed_admins: dict[str, str],
    admin_attempts: dict[str, int],
    captured_limits: dict[str, dict],
    is_suspend: bool,
    parallelism: int,
    freeze: bool = False,
) -> tuple[int, bool]:
    """Patch all remaining admin limits in parallel (bounded by parallelism).

    For suspend: captures real current limits then zeros them (both, unless `freeze`).
    For freeze (a kind of suspend): zeros ONLY max_users and keeps max_active_users, so existing
    users stay online while new-user creation is blocked.
    For restore: reads saved limits from snapshot then restores them.
    Returns (admins_patched, had_error). Commits progress on any error so the next
    tick retries only the failed admins.
    """
    remaining = [u for u in admin_order if u not in done_admins]
    if not remaining:
        return 0, False

    # Suspend/freeze only: abort before the parallel writes if the action was reverted by a
    # concurrent payment/defer (see _RevertedMidFlight). A restore never self-aborts.
    if is_suspend:
        current = await _current_action_status(session, action.id)
        if current == EnforcementActionStatus.reverted:
            raise _RevertedMidFlight

    sem = asyncio.Semaphore(max(1, parallelism))

    async def _patch_one(admin_uuid: str) -> tuple[str, str | None, dict | None]:
        """Returns (uuid, error_or_None, limits_dict_or_None)."""
        async with sem:
            admin = by_uuid.get(admin_uuid)
            if admin is None:
                return admin_uuid, "__missing__", None

            if is_suspend:
                real_mu, real_mau = await _get_admin_limits_safe(client, panel, admin)
                if real_mu is None:
                    real_mu = admin.panel_max_users
                if real_mau is None:
                    real_mau = admin.panel_max_active_users
                if not real_mu and admin.max_users_snapshot:
                    real_mu = admin.max_users_snapshot
                if not real_mau and admin.max_active_users_snapshot:
                    real_mau = admin.max_active_users_snapshot
                if real_mu is None or real_mau is None:
                    return admin_uuid, "current admin limits could not be captured", None
                lim: dict = {"max_users": real_mu, "max_active_users": real_mau}
                try:
                    # Freeze keeps max_active_users (existing users stay online); full suspend zeros both.
                    await _set_admin_limits(client, panel, admin, 0, real_mau if freeze else 0)
                    return admin_uuid, None, lim
                except Exception as exc:  # noqa: BLE001
                    return admin_uuid, str(exc)[:300], None
            else:
                lim = captured_limits.get(admin_uuid) or {}
                # A captured limit of 0 is a REAL value (an admin legitimately at max_users=0),
                # not "missing" — `x or snapshot` wrongly overrode it with the snapshot. Use an
                # explicit None check so a genuine 0 is restored as 0.
                mu = lim.get("max_users")
                if mu is None:
                    mu = admin.max_users_snapshot
                mau = lim.get("max_active_users")
                if mau is None:
                    mau = admin.max_active_users_snapshot
                if mu is None or mau is None:
                    return admin_uuid, "saved admin limits are incomplete", None
                try:
                    await _set_admin_limits(client, panel, admin, int(mu), int(mau))
                    return admin_uuid, None, lim
                except Exception as exc:  # noqa: BLE001
                    return admin_uuid, str(exc)[:300], None

    outcomes = await asyncio.gather(*[_patch_one(u) for u in remaining])

    any_hard_fail = False
    patched = 0
    for admin_uuid, error, lim in outcomes:
        if error is None:
            if is_suspend and lim is not None:
                admin = by_uuid.get(admin_uuid)
                if admin is not None:
                    admin.max_users_snapshot = lim["max_users"]
                    admin.max_active_users_snapshot = lim["max_active_users"]
                captured_limits[admin_uuid] = lim
            done_admins.add(admin_uuid)
            failed_admins.pop(admin_uuid, None)
            patched += 1
        elif error == "__missing__":
            done_admins.add(admin_uuid)
            if admin_uuid not in progress["admins_missing"]:
                progress["admins_missing"].append(admin_uuid)
        else:
            admin_attempts[admin_uuid] = admin_attempts.get(admin_uuid, 0) + 1
            failed_admins[admin_uuid] = error
            if admin_attempts[admin_uuid] >= _MAX_RETRIES:
                any_hard_fail = True

    progress["admins_done"] = sorted(done_admins)
    progress["admins_failed"] = failed_admins
    progress["admin_attempts"] = admin_attempts
    if is_suspend:
        progress["captured_limits"] = captured_limits
        action.snapshot = {**snapshot, "limits": captured_limits or snapshot.get("limits", {})}
    else:
        action.snapshot = snapshot
    flag_modified(action, "snapshot")

    if any_hard_fail:
        action.status = EnforcementActionStatus.failed
        action.error = (
            f"admin limit failed for: {', '.join(list(failed_admins)[:10])}"[:1000]
        )
        await session.commit()
        return patched, True

    if failed_admins:
        action.status = EnforcementActionStatus.partial
        action.error = f"{len(failed_admins)} admin limit failure(s), will retry"
        await session.commit()
        return patched, True

    await session.commit()
    return patched, False


# ── queue API ────────────────────────────────────────────────────────────────

async def queue_enforcement(
    session: AsyncSession,
    reseller: Reseller,
    *,
    invoice_id: int | None = None,
    dry_run: bool | None = None,
) -> EnforcementAction:
    """Plan an enforcement action without doing panel writes.

    Dry-run actions are finalized immediately. Live actions are durable queue items
    that the enforcement worker processes in resumable chunks.
    """
    enabled = await settings_service.get(session, "enforcement_enabled", False)
    is_dry = (not enabled) if dry_run is None else dry_run

    if invoice_id is not None:
        criteria = [
            EnforcementAction.invoice_id == invoice_id,
            EnforcementAction.action == EnforcementActionType.disable_users,
        ]
        if not is_dry:
            # A previous dry-run is only an audit record. It must not block the first
            # real queued enforcement after the operator enables enforcement.
            criteria.append(EnforcementAction.dry_run.is_(False))
            criteria.append(
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.done,
                        EnforcementActionStatus.failed,
                    ]
                )
            )
        else:
            criteria.append(EnforcementAction.status == EnforcementActionStatus.dry_run)
        existing = (
            await session.execute(
                select(EnforcementAction)
                .where(*criteria)
                .order_by(EnforcementAction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # A hard-`failed` suspend (retries exhausted, e.g. the panel was unreachable) would
            # otherwise be returned forever while the worker only executes planned/partial — so
            # dunning re-requests it daily and it never runs again. Reset it to planned with
            # cleared attempt counters, exactly like queue_restore does for a failed restore.
            if existing.status == EnforcementActionStatus.failed:
                snap = existing.snapshot or {}
                prog = _progress(snap)
                prog["users_failed"] = {}
                prog["user_attempts"] = {}
                prog["admins_failed"] = {}
                prog["admin_attempts"] = {}
                existing.snapshot = snap
                existing.status = EnforcementActionStatus.planned
                existing.error = None
                flag_modified(existing, "snapshot")
                await session.commit()
            return existing

    if not is_dry and reseller.enforcement_state == EnforcementState.enforced:
        prior = (
            await session.execute(
                select(EnforcementAction)
                .where(
                    EnforcementAction.reseller_id == reseller.id,
                    EnforcementAction.action == EnforcementActionType.disable_users,
                    EnforcementAction.status == EnforcementActionStatus.done,
                )
                .order_by(EnforcementAction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior is not None:
            return prior

    snapshot = await _queued_snapshot(session, reseller)
    action = EnforcementAction(
        reseller_id=reseller.id,
        invoice_id=invoice_id,
        action=EnforcementActionType.disable_users,
        dry_run=is_dry,
        affected_count=len(snapshot.get("users") or {}),
        snapshot=snapshot,
        status=EnforcementActionStatus.dry_run if is_dry else EnforcementActionStatus.planned,
    )
    session.add(action)
    await session.commit()
    if is_dry:
        log.info(
            "[dry-run] queued enforcement intent for reseller %s: %d users, %d admins",
            reseller.name,
            len(snapshot.get("users") or {}),
            len(snapshot.get("admins") or []),
        )
    return action


async def queue_restore(
    session: AsyncSession,
    reseller: Reseller,
    *,
    require_no_due: bool = False,
    reason: str = "manual",
) -> EnforcementAction | None:
    """Queue an exact, resumable restore and cancel any still-running suspension."""
    existing = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == reseller.id,
                EnforcementAction.action == EnforcementActionType.restore,
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.failed,
                    ]
                ),
            )
            .order_by(EnforcementAction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == EnforcementActionStatus.failed:
            snap = existing.snapshot or {}
            prog = _progress(snap)
            prog["users_failed"] = {}
            prog["user_attempts"] = {}
            prog["admins_failed"] = {}
            prog["admin_attempts"] = {}
            snap["require_no_due"] = require_no_due
            snap["reason"] = reason
            existing.snapshot = snap
            existing.status = EnforcementActionStatus.planned
            existing.error = None
            flag_modified(existing, "snapshot")
            await session.commit()
        return existing

    source = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == reseller.id,
                # A restore reverts the latest live suspend OR freeze (unfreeze == restore: it
                # re-applies the captured max_users and re-enables the — for freeze, empty — user set).
                EnforcementAction.action.in_(
                    [EnforcementActionType.disable_users, EnforcementActionType.freeze]
                ),
                EnforcementAction.dry_run.is_(False),
                EnforcementAction.status.in_(
                    [
                        EnforcementActionStatus.planned,
                        EnforcementActionStatus.partial,
                        EnforcementActionStatus.done,
                        EnforcementActionStatus.failed,
                    ]
                ),
            )
            .order_by(EnforcementAction.created_at.desc(), EnforcementAction.id.desc())
            .limit(1)
            # Serialize against the queue worker: its row lock is held between chunk commits,
            # so this read waits for the freshest committed progress before copying it (the
            # residual one-chunk window is handled by _merge_into_pending_restore).
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        return None

    source_snapshot = deepcopy(source.snapshot or {})
    source_progress = _progress(source_snapshot)

    if source.status == EnforcementActionStatus.planned:
        source.status = EnforcementActionStatus.reverted
        await session.commit()
        return None

    users_map: dict[str, str] = dict(source_snapshot.get("users") or {})
    admins = list(source_snapshot.get("admins") or [])
    limits = dict(
        source_progress.get("captured_limits") or source_snapshot.get("limits") or {}
    )

    if source.status in (
        EnforcementActionStatus.partial,
        EnforcementActionStatus.failed,
    ):
        completed_users = set(source_progress.get("users_done") or [])
        missing_users_set = set(source_progress.get("users_missing") or [])
        completed_admins = set(source_progress.get("admins_done") or [])
        users_map = {
            uuid: owner
            for uuid, owner in users_map.items()
            if uuid in completed_users and uuid not in missing_users_set
        }
        admins = [uuid for uuid in admins if uuid in completed_admins]
        limits = {uuid: limits[uuid] for uuid in admins if uuid in limits}
        source.status = EnforcementActionStatus.reverted
        if not users_map and not admins:
            await session.commit()
            return None
    elif not admins:
        descendants = await _bundle(session, reseller)
        admins = [d.admin_uuid for d in descendants if d.admin_uuid in limits]

    snapshot: dict = {
        "limits": limits,
        "admins": admins,
        "users": users_map,
        "source_action_id": source.id,
        "require_no_due": require_no_due,
        "reason": reason,
        "progress": {
            "phase": "limits",
            "users_done": [],
            "users_missing": [],
            "users_failed": {},
            "user_attempts": {},
            "admins_done": [],
            "admins_missing": [],
            "admins_failed": {},
            "admin_attempts": {},
            "captured_limits": limits,
        },
    }
    restore = EnforcementAction(
        reseller_id=reseller.id,
        invoice_id=source.invoice_id,
        action=EnforcementActionType.restore,
        dry_run=False,
        snapshot=snapshot,
        status=EnforcementActionStatus.planned,
    )
    session.add(restore)
    # Keep a frozen reseller `frozen` while the unfreeze is queued (restore completion flips it to
    # active); a suspended reseller stays `enforced` as before.
    if reseller.enforcement_state != EnforcementState.frozen:
        reseller.enforcement_state = EnforcementState.enforced
    await session.commit()
    return restore


async def queue_freeze(session: AsyncSession, reseller: Reseller) -> EnforcementAction | None:
    """Queue a limits-only «freeze»: zero the reseller subtree's `max_users` so they can't create new
    users / expand, WITHOUT disabling existing users (they stay online). Reversible via `queue_restore`
    (unfreeze == restore). Always live — an explicit, parent-initiated action. Returns None when the
    reseller is not `active` (already frozen/enforced → the UI hides the button). Idempotent: returns an
    in-flight freeze action if one is already queued for this reseller."""
    if getattr(reseller, "is_owner", False):
        raise ValueError("cannot freeze the panel owner")
    if reseller.enforcement_state != EnforcementState.active:
        return None

    existing = (
        await session.execute(
            select(EnforcementAction)
            .where(
                EnforcementAction.reseller_id == reseller.id,
                EnforcementAction.action == EnforcementActionType.freeze,
                EnforcementAction.status.in_(
                    [EnforcementActionStatus.planned, EnforcementActionStatus.partial]
                ),
            )
            .order_by(EnforcementAction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    descendants = await _bundle(session, reseller)
    snapshot: dict = {
        "limits": {},
        "admins": [d.admin_uuid for d in descendants],
        "users": {},
    }
    _progress(snapshot)["phase"] = "limits"
    action = EnforcementAction(
        reseller_id=reseller.id,
        action=EnforcementActionType.freeze,
        dry_run=False,
        affected_count=0,
        snapshot=snapshot,
        status=EnforcementActionStatus.planned,
    )
    session.add(action)
    await session.commit()
    log.info("queued freeze for reseller %s (%d admins in subtree)", reseller.name, len(descendants))
    return action


async def _notify_owner_failed(session: AsyncSession, action: EnforcementAction) -> None:
    """Best-effort: tell the owner a queued suspension/restore failed after its retries, so a
    stuck action (bad API key, panel unreachable) doesn't silently leave debt uncollected or a
    paid reseller suspended. Never raises into the worker."""
    try:
        from app.services import owner_notify

        reseller = await session.get(Reseller, action.reseller_id)
        who = owner_notify.user_link(reseller) if reseller else f"#{action.reseller_id}"
        kind = "بازگردانی" if action.action == EnforcementActionType.restore else "مسدودسازی"
        await owner_notify.notify_owner(
            session,
            f"⛔️ {kind} خودکار برای نماینده {who} پس از چند تلاش ناموفق ماند و متوقف شد.\n"
            f"خطا: {(action.error or '—')[:300]}\n"
            f"لطفاً اتصال/کلید API پنل را بررسی کنید و در صورت نیاز دستی اقدام کنید.",
            html=bool(reseller),
        )
    except Exception:  # noqa: BLE001
        log.warning("owner failure notification failed for action %s", action.id, exc_info=True)


# ── worker actions ───────────────────────────────────────────────────────────

async def _process_enforcement_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
    admin_parallelism: int,
) -> dict:
    """Process one queued live enforcement (suspend) action.

    Phase 1 — users: all remaining chunks processed in a loop, commit after each.
    Phase 2 — admin limits: all remaining admins patched in parallel.
    A failure in either phase commits progress and returns partial/failed so the
    next worker tick can resume exactly where this one stopped.
    """
    if action.dry_run or action.action != EnforcementActionType.disable_users:
        return {"skipped": 1}
    if action.status == EnforcementActionStatus.done:
        return {"done": 1}

    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        action.status = EnforcementActionStatus.failed
        action.error = "reseller not found"
        await session.commit()
        return {"failed": 1}

    if action.invoice_id is not None and not await _has_due_invoice(session, reseller.id):
        restore = await queue_restore(
            session, reseller, require_no_due=False, reason="disable-canceled-no-debt"
        )
        if restore is None:
            action.status = EnforcementActionStatus.reverted
            await session.commit()
            return {"skipped": 1}
        return {"restore_queued": 1}

    if reseller.enforcement_state == EnforcementState.enforced:
        action.status = EnforcementActionStatus.done
        await session.commit()
        return {"done": 1}

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        return {"failed": 1}

    snapshot = action.snapshot or await _queued_snapshot(session, reseller)
    progress = _progress(snapshot)
    users_map: dict[str, str] = dict(snapshot.get("users") or {})
    done_users: set[str] = set(progress.get("users_done") or [])
    missing_users: set[str] = set(progress.get("users_missing") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})
    client = AdminApiClient()

    action.status = EnforcementActionStatus.partial
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()

    result: dict = {"patched_users": 0, "patched_admins": 0}

    # ── Phase 1: disable users ────────────────────────────────────────────────
    if progress.get("phase") in ("users", None):
        patched_u, had_error = await _run_user_chunks(
            session=session, action=action, client=client, panel=panel,
            snapshot=snapshot, progress=progress,
            users_map=users_map, done_users=done_users, missing_users=missing_users,
            failed_users=failed_users, user_attempts=user_attempts,
            enable=False, chunk_size=user_chunk_size,
        )
        result["patched_users"] = patched_u
        if had_error:
            result["partial"] = int(action.status == EnforcementActionStatus.partial)
            result["failed"] = int(action.status == EnforcementActionStatus.failed)
            return result

        # The bulk write reported success — prove it actually landed on OUR users before we zero
        # any admin limits or call this action done.
        if patched_u and await _fail_if_writes_missed(
            session, action, client, panel,
            sorted(done_users - missing_users), expect_enabled=False,
        ):
            result["failed"] = 1
            return result

        progress["phase"] = "limits"
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()

    # ── Phase 2: zero admin limits (parallel) ────────────────────────────────
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    # Bottom-up (leaf → root): children lose quota first so they can't create new
    # users while the parent still has capacity.
    admin_order = list(
        reversed(snapshot.get("admins") or [d.admin_uuid for d in descendants])
    )
    done_admins: set[str] = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or {})

    patched_a, had_error = await _run_admin_limits(
        session=session, action=action, client=client, panel=panel,
        snapshot=snapshot, progress=progress,
        by_uuid=by_uuid, admin_order=admin_order,
        done_admins=done_admins, failed_admins=failed_admins,
        admin_attempts=admin_attempts, captured_limits=captured_limits,
        is_suspend=True, parallelism=admin_parallelism,
    )
    result["patched_admins"] = patched_a
    if had_error:
        result["partial"] = int(action.status == EnforcementActionStatus.partial)
        result["failed"] = int(action.status == EnforcementActionStatus.failed)
        return result

    # ── Finalize ─────────────────────────────────────────────────────────────
    if not done_users and not done_admins:
        action.status = EnforcementActionStatus.failed
        action.error = "enforcement did nothing"
        await session.commit()
        return {"failed": 1}

    # The last chunk isn't covered by the in-loop revert checks — re-read the DB status once
    # more so a `done` write can never overwrite a concurrent `reverted`.
    if await _current_action_status(session, action.id) == EnforcementActionStatus.reverted:
        raise _RevertedMidFlight
    # Debt can also vanish mid-run WITHOUT a queued restore (auto_restore_on_payment off, or
    # the payment landed between the head check and here) — mirror the head check: hand the
    # applied work to a restore instead of finalizing a suspension nobody owes for.
    if action.invoice_id is not None and not await _has_due_invoice(session, reseller.id):
        restore = await queue_restore(
            session, reseller, require_no_due=False, reason="paid-mid-suspend"
        )
        if restore is None:
            action.status = EnforcementActionStatus.reverted
            await session.commit()
            return {"skipped": 1}
        return {"restore_queued": 1, "patched_users": result["patched_users"],
                "patched_admins": result["patched_admins"]}

    reseller.enforcement_state = EnforcementState.enforced
    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.affected_count = len(done_users - missing_users)
    # Legacy cleanup: older rows may still carry a persisted panel_user_ids cache; strip it so the row
    # doesn't hold ~100 KB of integer-ID mappings that have no audit value after completion.
    snapshot.pop("panel_user_ids", None)
    action.snapshot = snapshot
    flag_modified(action, "snapshot")

    if action.invoice_id:
        from app.models import Invoice
        from app.models.enums import InvoiceStatus
        from app.services import invoice_state

        inv = await session.get(Invoice, action.invoice_id)
        # Stamp only a still-OWED invoice: a payment can land while chunks are flying, and
        # flipping a PAID invoice to enforced would resurrect it as overdue on restore —
        # dunning would then chase settled debt.
        if inv is not None and inv.status in invoice_state.OWED:
            inv.status = InvoiceStatus.enforced
            # Mirror the flip into the ledger so «تاریخچهٔ مالی» reflects the real state.
            from app.services import financial_archive

            await financial_archive.record(session, inv, reseller=reseller)

    await session.commit()
    log.info(
        "Enforcement done for reseller %s: %d users disabled, %d admins zeroed",
        reseller.name,
        len(done_users - missing_users),
        len(done_admins),
    )
    result["done"] = 1
    return result


async def _process_restore_action(
    session: AsyncSession,
    action: EnforcementAction,
    *,
    user_chunk_size: int,
    admin_parallelism: int,
) -> dict:
    """Process one queued restore action.

    Phase 1 — admin limits: all admins restored in parallel (bounded by admin_parallelism).
    Phase 2 — users: all remaining chunks in a loop, commit after each.
    The reseller is only flipped to active once ALL users are re-enabled — a partial
    restore leaves enforcement_state=enforced so the next trigger retries cleanly.
    """
    if action.dry_run or action.action != EnforcementActionType.restore:
        return {"skipped": 1}

    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        action.status = EnforcementActionStatus.failed
        action.error = "reseller not found"
        await session.commit()
        return {"failed": 1}

    snapshot = action.snapshot or {}
    if snapshot.get("require_no_due") and await _has_due_invoice(session, reseller.id):
        action.status = EnforcementActionStatus.failed
        action.error = "restore canceled: reseller still has a due invoice"
        await session.commit()
        return {"failed": 1}

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        return {"failed": 1}

    progress = _progress(snapshot)
    action.status = EnforcementActionStatus.partial
    client = AdminApiClient()
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    # A descendant that is INDEPENDENTLY frozen/suspended (its own enforcement_state != active,
    # set by a separate action — e.g. its parent froze it) must NOT be re-opened by THIS
    # reseller's restore: doing so would lift a limit the descendant is meant to keep and would
    # destroy its recovery snapshot. Exclude such descendants from the limit-restore order and
    # preserve their snapshots at finalize. (The root itself is always restored.)
    independently_enforced = {
        d.admin_uuid for d in descendants
        if d.id != reseller.id and d.enforcement_state != EnforcementState.active
    }
    limits: dict[str, dict] = dict(snapshot.get("limits") or {})
    # Top-down (root → leaf): parent quotas restored first so children's quota
    # is meaningful as soon as they get it back.
    admins = [u for u in (snapshot.get("admins") or limits) if u not in independently_enforced]
    done_admins: set[str] = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or limits)

    result: dict = {"restored_users": 0, "restored_admins": 0}

    # ── Phase 1: restore admin limits (parallel) ─────────────────────────────
    if progress.get("phase") == "limits":
        patched_a, had_error = await _run_admin_limits(
            session=session, action=action, client=client, panel=panel,
            snapshot=snapshot, progress=progress,
            by_uuid=by_uuid, admin_order=admins,
            done_admins=done_admins, failed_admins=failed_admins,
            admin_attempts=admin_attempts, captured_limits=captured_limits,
            is_suspend=False, parallelism=admin_parallelism,
        )
        result["restored_admins"] = patched_a
        if had_error:
            result["partial"] = int(action.status == EnforcementActionStatus.partial)
            result["failed"] = int(action.status == EnforcementActionStatus.failed)
            return result

        progress["phase"] = "users"
        action.snapshot = snapshot
        flag_modified(action, "snapshot")
        await session.commit()

    # ── Phase 2: re-enable users ──────────────────────────────────────────────
    users_map: dict[str, str] = dict(snapshot.get("users") or {})
    done_users: set[str] = set(progress.get("users_done") or [])
    missing_users: set[str] = set(progress.get("users_missing") or [])
    failed_users: dict[str, str] = dict(progress.get("users_failed") or {})
    user_attempts: dict[str, int] = dict(progress.get("user_attempts") or {})

    patched_u, had_error = await _run_user_chunks(
        session=session, action=action, client=client, panel=panel,
        snapshot=snapshot, progress=progress,
        users_map=users_map, done_users=done_users, missing_users=missing_users,
        failed_users=failed_users, user_attempts=user_attempts,
        enable=True, chunk_size=user_chunk_size,
    )
    result["restored_users"] = patched_u
    if had_error:
        result["partial"] = int(action.status == EnforcementActionStatus.partial)
        result["failed"] = int(action.status == EnforcementActionStatus.failed)
        return result

    # Prove the re-enable actually landed on OUR users before declaring the reseller active —
    # a silent miss here would leave a paying customer's users offline while we report success.
    if patched_u and await _fail_if_writes_missed(
        session, action, client, panel,
        sorted(done_users - missing_users), expect_enabled=True,
    ):
        result["failed"] = 1
        return result

    # ── Finalize ─────────────────────────────────────────────────────────────
    reseller.enforcement_state = EnforcementState.active
    for descendant in descendants:
        # Keep the recovery snapshot of an independently frozen/suspended descendant — it was
        # NOT restored above and still needs its snapshot to be lifted by its own action.
        if descendant.admin_uuid in independently_enforced:
            continue
        descendant.max_users_snapshot = None
        descendant.max_active_users_snapshot = None

    from app.models import Invoice
    from app.models.enums import InvoiceStatus

    enforced_invoices = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller.id,
                Invoice.status == InvoiceStatus.enforced,
            )
        )
    ).scalars().all()
    for invoice in enforced_invoices:
        invoice.status = InvoiceStatus.overdue

    source_id = snapshot.get("source_action_id")
    if source_id:
        src = await session.get(EnforcementAction, int(source_id))
        if src is not None:
            src.status = EnforcementActionStatus.reverted

    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.affected_count = len(done_users - missing_users)
    snapshot.pop("panel_user_ids", None)
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()
    log.info(
        "Restore done for reseller %s: %d users enabled, %d admins restored",
        reseller.name,
        patched_u,
        result["restored_admins"],
    )
    result["done"] = 1
    return result


async def _process_freeze_action(
    session: AsyncSession, action: EnforcementAction, *, admin_parallelism: int
) -> dict:
    """Process one queued «freeze» (limits-only) action: zero the subtree's max_users (keep
    max_active_users → existing users stay ONLINE), then mark the reseller `frozen`. No user phase.
    Resumable: admin progress is committed so a restart resumes with only the unfinished admins."""
    if action.dry_run or action.action != EnforcementActionType.freeze:
        return {"skipped": 1}
    if action.status == EnforcementActionStatus.done:
        return {"done": 1}

    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        action.status = EnforcementActionStatus.failed
        action.error = "reseller not found"
        await session.commit()
        return {"failed": 1}
    if reseller.enforcement_state == EnforcementState.enforced:
        # A full suspension already covers (and supersedes) a freeze — nothing to do.
        action.status = EnforcementActionStatus.done
        await session.commit()
        return {"done": 1}

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        return {"failed": 1}

    snapshot = action.snapshot or {}
    progress = _progress(snapshot)
    client = AdminApiClient()
    descendants = await _bundle(session, reseller)
    by_uuid = {d.admin_uuid: d for d in descendants}
    # Bottom-up (leaf → root) so a child loses new-user capacity before its parent.
    admin_order = list(reversed(snapshot.get("admins") or [d.admin_uuid for d in descendants]))
    done_admins: set[str] = set(progress.get("admins_done") or [])
    failed_admins: dict[str, str] = dict(progress.get("admins_failed") or {})
    admin_attempts: dict[str, int] = dict(progress.get("admin_attempts") or {})
    captured_limits: dict[str, dict] = dict(progress.get("captured_limits") or {})

    action.status = EnforcementActionStatus.partial
    flag_modified(action, "snapshot")
    await session.commit()

    patched_a, had_error = await _run_admin_limits(
        session=session, action=action, client=client, panel=panel,
        snapshot=snapshot, progress=progress,
        by_uuid=by_uuid, admin_order=admin_order,
        done_admins=done_admins, failed_admins=failed_admins,
        admin_attempts=admin_attempts, captured_limits=captured_limits,
        is_suspend=True, freeze=True, parallelism=admin_parallelism,
    )
    result = _empty_queue_result()
    result["patched_admins"] = patched_a
    if had_error:
        result["partial"] = int(action.status == EnforcementActionStatus.partial)
        result["failed"] = int(action.status == EnforcementActionStatus.failed)
        return result

    # An unfreeze (queue_restore) can revert this action while the limit writes were flying —
    # never overwrite `reverted` with `done` (see _RevertedMidFlight).
    if await _current_action_status(session, action.id) == EnforcementActionStatus.reverted:
        raise _RevertedMidFlight

    reseller.enforcement_state = EnforcementState.frozen
    action.status = EnforcementActionStatus.done
    action.error = None
    progress["phase"] = "done"
    action.affected_count = len(done_admins)
    action.snapshot = snapshot
    flag_modified(action, "snapshot")
    await session.commit()
    log.info(
        "Freeze done for reseller %s: %d admins capped to max_users=0", reseller.name, len(done_admins)
    )
    result["done"] = 1
    return result


async def queue_admin_deletion(session: AsyncSession, reseller: Reseller) -> EnforcementAction:
    """Queue a CASCADE deletion of a reseller (admin) + its whole sub-tree from the panel and our
    DB. Always live (dry_run=False) — it's an explicit, confirmation-gated owner action, not the
    automatic dunning path. Refuses the panel owner / an `is_owner` reseller. Idempotent: returns
    an existing not-yet-finished delete action for the same reseller if one is queued."""
    if getattr(reseller, "is_owner", False):
        raise ValueError("cannot delete the panel owner")
    panel = await session.get(Panel, reseller.panel_id)
    if panel is not None and reseller.admin_uuid and panel.owner_uuid and \
            reseller.admin_uuid.lower() == str(panel.owner_uuid).lower():
        raise ValueError("cannot delete the panel owner")

    existing = (
        await session.execute(
            select(EnforcementAction).where(
                EnforcementAction.reseller_id == reseller.id,
                EnforcementAction.action == EnforcementActionType.delete_admin,
                EnforcementAction.status.in_(
                    [EnforcementActionStatus.planned, EnforcementActionStatus.partial]
                ),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    descendants = await _bundle(session, reseller)
    snapshot: dict = {
        "panel_id": reseller.panel_id,
        "root_uuid": reseller.admin_uuid,
        "admins": [d.admin_uuid for d in descendants],
        "deleted_users": 0,
    }
    _progress(snapshot)
    snapshot["progress"]["phase"] = "users"
    action = EnforcementAction(
        reseller_id=reseller.id,
        action=EnforcementActionType.delete_admin,
        dry_run=False,
        affected_count=0,
        snapshot=snapshot,
        status=EnforcementActionStatus.planned,
    )
    session.add(action)
    await session.commit()
    log.info("queued cascade delete for reseller %s (%d admins in subtree)",
             reseller.name, len(descendants))
    return action


async def _process_delete_action(
    session: AsyncSession, action: EnforcementAction, *, user_chunk_size: int
) -> dict:
    """Resumable cascade deletion: (1) delete the subtree's users on the panel in bounded chunks
    via the native bulk delete (one quick_apply per batch) and drop their DB rows as we go; then
    (2) delete the admin on the panel (Hiddify cascades sub-admins); then (3) purge the subtree
    from our DB (ledger kept). Progress phase is committed so a restart resumes mid-cascade."""
    result = _empty_queue_result()
    snapshot = action.snapshot or {}
    progress = _progress(snapshot)
    reseller = await session.get(Reseller, action.reseller_id)
    if reseller is None:
        # DB rows already gone (purge done) → finalize.
        action.status = EnforcementActionStatus.done
        progress["phase"] = "done"
        flag_modified(action, "snapshot")
        await session.commit()
        result["done"] = 1
        return result
    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        action.status = EnforcementActionStatus.failed
        action.error = "panel not found"
        await session.commit()
        result["failed"] = 1
        return result
    client = AdminApiClient()
    chunk = max(1, int(user_chunk_size or 500))

    # ---- Phase 1: delete the subtree's users on the panel (bounded, resumable) ----
    if progress.get("phase", "users") == "users":
        descendants = await _bundle(session, reseller)
        admin_uuids_lower = [(d.admin_uuid or "").lower() for d in descendants]
        deleted_total = int(progress.get("deleted_users") or snapshot.get("deleted_users") or 0)
        sem = asyncio.Semaphore(_ID_LOOKUP_CONCURRENCY)
        while True:
            batch = (
                await session.execute(
                    select(EndUserSnapshot).where(
                        EndUserSnapshot.panel_id == panel.id,
                        func.lower(EndUserSnapshot.added_by_uuid).in_(admin_uuids_lower),
                    ).limit(chunk)
                )
            ).scalars().all()
            if not batch:
                break

            async def _resolve(row):  # noqa: ANN001, ANN202
                # ALWAYS resolve fresh — never reuse the durable `panel_user_id`. Hiddify renumbers
                # user ids on a panel restore/re-import, so a stale id can point at another
                # reseller's user. Here that would DELETE the wrong user (unrecoverable), so this
                # path must never trade correctness for a saved lookup. See _run_user_chunks.
                async with sem:
                    try:
                        return row, await client.get_user_id(panel, row.user_uuid), None
                    except Exception as exc:  # noqa: BLE001
                        return row, None, str(exc)[:200]

            resolved = await asyncio.gather(*[_resolve(r) for r in batch])
            ids_on_panel: list[int] = []
            removable_uuids: list[str] = []  # rows safe to drop from our DB this chunk
            last_err: str | None = None
            for row, uid, err in resolved:
                if err is not None:
                    last_err = err
                    continue          # transient lookup error → leave for retry
                if uid is not None:
                    ids_on_panel.append(uid)
                removable_uuids.append(row.user_uuid)   # resolved OR 404 (absent) → removable

            if not removable_uuids:
                # Whole batch errored (panel down) → stop; the worker retries next tick.
                action.error = f"user id lookup failed (will retry): {last_err or ''}"
                action.status = EnforcementActionStatus.partial
                snapshot["deleted_users"] = deleted_total
                progress["deleted_users"] = deleted_total
                action.affected_count = deleted_total
                flag_modified(action, "snapshot")
                await session.commit()
                result["partial"] = 1
                result["failed_users"] = len(batch)
                return result

            try:
                if ids_on_panel:
                    await client.bulk_delete_users(panel, ids_on_panel)
            except Exception as exc:  # noqa: BLE001
                action.error = f"bulk delete failed (will retry): {str(exc)[:300]}"
                action.status = EnforcementActionStatus.partial
                snapshot["deleted_users"] = deleted_total
                progress["deleted_users"] = deleted_total
                flag_modified(action, "snapshot")
                await session.commit()
                result["partial"] = 1
                return result

            # VERIFY before we forget who they were. The panel answers 200 for a bulk action even
            # when the rowids matched nobody we meant, and the very next statements delete our own
            # snapshot rows — i.e. the evidence needed to notice. Deletion is unrecoverable, so this
            # path (unlike suspend/restore) must never take success on trust. A user that is really
            # gone reads back as absent (get_user_id → None).
            if ids_on_panel:
                still_present = 0
                for uu in removable_uuids[: max(1, len(removable_uuids) // 20)][:20]:
                    try:
                        if await client.get_user_id(panel, uu) is not None:
                            still_present += 1
                    except Exception:  # noqa: BLE001 — a flaky read must not fail the action alone
                        continue
                if still_present >= 2:
                    action.status = EnforcementActionStatus.failed
                    action.error = (
                        f"delete verification failed: {still_present} sampled users still exist on "
                        "the panel after the bulk delete. The panel's user ids may have been "
                        "renumbered; refusing to drop local records."
                    )
                    log.error("cascade delete verification failed for action %s", action.id)
                    snapshot["deleted_users"] = deleted_total
                    progress["deleted_users"] = deleted_total
                    flag_modified(action, "snapshot")
                    await session.commit()
                    result["failed"] = 1
                    return result

            # Drop the just-deleted (or absent) users from our DB so the next batch shrinks.
            await session.execute(
                _sa_delete(UsageMeter).where(
                    UsageMeter.panel_id == panel.id, UsageMeter.user_uuid.in_(removable_uuids)
                )
            )
            await session.execute(
                _sa_delete(EndUserSnapshot).where(
                    EndUserSnapshot.panel_id == panel.id,
                    EndUserSnapshot.user_uuid.in_(removable_uuids),
                )
            )
            deleted_total += len(removable_uuids)
            snapshot["deleted_users"] = deleted_total
            progress["deleted_users"] = deleted_total
            action.affected_count = deleted_total
            action.error = None
            flag_modified(action, "snapshot")
            await session.commit()
            result["patched_users"] += len(removable_uuids)
        progress["phase"] = "panel_admin"
        flag_modified(action, "snapshot")
        await session.commit()

    # ---- Phase 2: delete the admin on the panel (Hiddify cascades sub-admins) ----
    if progress.get("phase") == "panel_admin":
        try:
            await client.delete_admin(panel, reseller.admin_uuid)
        except Exception as exc:  # noqa: BLE001
            action.error = f"delete admin failed (will retry): {str(exc)[:300]}"
            action.status = EnforcementActionStatus.partial
            await session.commit()
            result["partial"] = 1
            return result
        progress["phase"] = "db"
        action.error = None
        flag_modified(action, "snapshot")
        await session.commit()

    # ---- Phase 3: purge the subtree from our DB (financial ledger kept) ----
    if progress.get("phase") == "db":
        descendants = await _bundle(session, reseller)
        purge_ids = [d.id for d in descendants]
        purge_uuids = [d.admin_uuid for d in descendants]
        # Mark done FIRST while the action row is still valid: purging deletes the reseller, and
        # the action's reseller_id FK is ondelete=CASCADE, so the action row itself disappears on
        # Postgres once we purge — we must not try to update it afterwards.
        progress["phase"] = "done"
        action.status = EnforcementActionStatus.done
        flag_modified(action, "snapshot")
        await session.commit()
        try:
            from app.services import reseller_purge

            await reseller_purge.purge_subtree(
                session, panel_id=panel.id, reseller_ids=purge_ids, admin_uuids=purge_uuids,
            )
        except Exception:  # noqa: BLE001
            # Panel deletion already succeeded; the DB rows are now "absent" and get cleaned by
            # the daily prune / re-running the tool. Don't fail the action over a DB-purge hiccup.
            log.warning("cascade DB purge failed for reseller %s", action.reseller_id, exc_info=True)
        result["done"] = 1
    return result


def _empty_queue_result() -> dict:
    return {
        "picked": 0,
        "done": 0, "partial": 0, "failed": 0, "skipped": 0,
        "patched_users": 0, "failed_users": 0, "patched_admins": 0,
        "restored_users": 0, "restored_admins": 0, "restore_queued": 0,
        "reverted_midflight": 0,
    }


_PENDING_ACTION_FILTER = (
    EnforcementAction.action.in_(
        [
            EnforcementActionType.disable_users,
            EnforcementActionType.restore,
            EnforcementActionType.delete_admin,
            EnforcementActionType.freeze,
        ]
    ),
    EnforcementAction.dry_run.is_(False),
    EnforcementAction.status.in_(
        [EnforcementActionStatus.planned, EnforcementActionStatus.partial]
    ),
)


async def _process_panel_queue(
    session: AsyncSession, panel_id: int, *, limit: int, chunk: int, para: int
) -> dict:
    """Process up to `limit` pending actions for ONE panel, sequentially (restores first), on a
    dedicated session. One lane per panel; lanes run concurrently (see process_enforcement_queue).
    Each panel is processed serially so we never hammer a single panel's API in parallel."""
    actions = (
        await session.execute(
            select(EnforcementAction)
            .where(
                *_PENDING_ACTION_FILTER,
                # Scope to this panel only. A scalar subquery keeps the row-lock on
                # EnforcementAction (not the joined Reseller rows).
                EnforcementAction.reseller_id.in_(
                    select(Reseller.id).where(Reseller.panel_id == panel_id)
                ),
            )
            .order_by(
                # Restores first so paying customers are un-blocked before new suspensions run.
                case(
                    (EnforcementAction.action == EnforcementActionType.restore, 0),
                    else_=1,
                ),
                EnforcementAction.created_at,
                EnforcementAction.id,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()

    result = _empty_queue_result()
    result["picked"] = len(actions)
    for action in actions:
        action_id = action.id  # read while fresh — a rollback below expires the object
        try:
            if action.action == EnforcementActionType.restore:
                step = await _process_restore_action(
                    session, action, user_chunk_size=chunk, admin_parallelism=para,
                )
            elif action.action == EnforcementActionType.delete_admin:
                step = await _process_delete_action(session, action, user_chunk_size=chunk)
            elif action.action == EnforcementActionType.freeze:
                step = await _process_freeze_action(session, action, admin_parallelism=para)
            else:
                step = await _process_enforcement_action(
                    session, action, user_chunk_size=chunk, admin_parallelism=para,
                )
        except _RevertedMidFlight:
            # A payment/defer reverted this suspend/freeze while its chunks were flying.
            # Never write status/snapshot over the `reverted` row; hand the chunks that
            # landed after the restore's copy over to that pending restore instead.
            await session.rollback()
            await _merge_into_pending_restore(session, action_id)
            step = {"skipped": 1, "reverted_midflight": 1}
        # A hard failure (exhausted retries) is no longer silent: ping the owner so a stuck
        # suspension/restore is visible instead of leaving debt uncollected.
        if step.get("failed"):
            await _notify_owner_failed(session, action)
        for key in result:
            if key != "picked":
                result[key] += int(step.get(key, 0) or 0)
    return result


async def process_enforcement_queue(
    session: AsyncSession,
    *,
    action_limit: int | None = None,
    user_chunk_size: int | None = None,
    admin_chunk_size: int | None = None,
) -> dict:
    """Process pending enforcement/restore actions, **one lane per panel running in parallel** so
    suspensions/restores on different panels progress simultaneously instead of one-at-a-time.

    Each panel lane gets its OWN session (an AsyncSession is not concurrency-safe) and processes up
    to `enforcement_action_batch_limit` of that panel's actions sequentially (restores first). The
    number of panels processed concurrently is bounded by `enforcement_panel_concurrency`. Per-action
    chunking/resumability is unchanged, so a large reseller still resumes across ticks — now without
    blocking other panels. `admin_chunk_size` bounds concurrent admin-limit calls within one action.
    """
    cfg = await settings_service.get_many(
        session,
        [
            "enforcement_action_batch_limit",
            "enforcement_user_chunk_size",
            "enforcement_admin_chunk_size",
            "enforcement_panel_concurrency",
        ],
    )
    limit = max(1, int(action_limit or cfg.get("enforcement_action_batch_limit") or 1))
    chunk = max(1, int(user_chunk_size or cfg.get("enforcement_user_chunk_size") or 500))
    para = max(1, int(admin_chunk_size or cfg.get("enforcement_admin_chunk_size") or 10))
    panel_para = max(1, int(cfg.get("enforcement_panel_concurrency") or 6))

    # Each lane needs its OWN session (an AsyncSession is NOT safe to share across tasks). Derive the
    # factory from the caller's bind so it uses the same engine in production AND in tests; fall back
    # to the app-wide SessionLocal if the session isn't engine-bound.
    bind = session.bind
    lane_factory = (
        async_sessionmaker(bind, expire_on_commit=False, autoflush=False) if bind is not None
        else SessionLocal
    )

    # Serialize WHOLE queue runs: the per-row FOR UPDATE skip_locked evaporates at the first
    # mid-action commit, so a manual «اجرای صف» overlapping the 5-min scheduler tick could
    # re-pick a partial action and (worst case) re-capture already-zeroed limits into the
    # restore snapshot (the M38 restore-zeros class). A transaction-level advisory lock on a
    # DEDICATED session (its transaction stays open for the whole run; xact locks would die
    # at the first chunk commit on a shared session) makes runs mutually exclusive; the loser
    # returns an "already_running" result instead of double-processing. No-op on SQLite.
    lock_session = None
    try:
        sync_bind = session.get_bind()
    except Exception:  # noqa: BLE001 — unbound session (tests) → skip locking
        sync_bind = None
    if sync_bind is not None and getattr(sync_bind.dialect, "name", "") == "postgresql":
        lock_session = lane_factory()
        got = (
            await lock_session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _QUEUE_LOCK_KEY}
            )
        ).scalar()
        if not got:
            await lock_session.close()
            agg = _empty_queue_result()
            agg["panels"] = 0
            agg["already_running"] = 1
            return agg

    try:
        # Retention of terminal enforcement_action rows (incl. their large JSON snapshots) is
        # handled centrally by the daily maintenance job — see app.services.maintenance.
        panel_ids = list((
            await session.execute(
                select(Reseller.panel_id)
                .join(EnforcementAction, EnforcementAction.reseller_id == Reseller.id)
                .where(*_PENDING_ACTION_FILTER)
                .distinct()
            )
        ).scalars().all())
        panel_ids = [p for p in panel_ids if p is not None]

        agg = _empty_queue_result()
        agg["panels"] = len(panel_ids)
        if not panel_ids:
            return agg

        sem = asyncio.Semaphore(panel_para)

        async def _lane(pid: int) -> dict:
            async with sem:
                try:
                    async with lane_factory() as lane_session:
                        return await _process_panel_queue(
                            lane_session, pid, limit=limit, chunk=chunk, para=para
                        )
                except Exception:  # noqa: BLE001 — one panel's failure must not abort the others
                    log.exception("enforcement queue lane failed for panel %s", pid)
                    return {}

        steps = await asyncio.gather(*[_lane(pid) for pid in panel_ids])
        for step in steps:
            for key, val in step.items():
                agg[key] = agg.get(key, 0) + int(val or 0)
        return agg
    finally:
        if lock_session is not None:
            await lock_session.rollback()
            await lock_session.close()


# ── public API (thin wrappers) ───────────────────────────────────────────────

async def enforce_reseller(
    session: AsyncSession,
    reseller: Reseller,
    *,
    dry_run: bool | None = None,
    invoice_id: int | None = None,
) -> EnforcementAction:
    """Queue a suspension so API and bot requests never wait for panel writes."""
    return await queue_enforcement(
        session, reseller, invoice_id=invoice_id, dry_run=dry_run
    )


async def freeze_reseller(
    session: AsyncSession, reseller: Reseller
) -> EnforcementAction | None:
    """Queue a limits-only freeze (block new-user creation; existing users stay online). Returns None
    if the reseller isn't `active` (already frozen/enforced)."""
    return await queue_freeze(session, reseller)
