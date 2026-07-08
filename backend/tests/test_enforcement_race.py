"""H02 — enforcement mid-payment race, restore-source retention, queue serialization.

- A payment confirmed while a suspend's chunks are flying reverts the action; the worker
  must NOT overwrite `reverted` with `done`, must NOT stamp the (now paid) invoice as
  enforced, and the chunks that landed after the restore's progress-copy must be merged
  into the pending restore so those users are re-enabled too.
- The finalize invoice stamp only touches a still-OWED invoice.
- prune_old_logs keeps the newest live disable/freeze action of a reseller who is still
  enforced/frozen (it is the restore source; deleting it made restore impossible forever).
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfrace.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
)


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s, Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _invoice(reseller_id, *, label, status=InvoiceStatus.sent, sent_days_ago=5):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=start, period_end=end,
        period_label=label, usage_gb=10, amount_toman=10000, amount_usdt=1, status=status,
        sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sent_days_ago),
    )


async def _seed_suspend(s, *, n_users=4):
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                 panel_max_active_users=10, enforcement_state=EnforcementState.active)
    s.add(r)
    await s.flush()
    for i in range(n_users):
        s.add(EndUserSnapshot(panel_id=1, user_uuid=f"u{i}", name=f"u{i}",
                              added_by_uuid="A", enable=True))
    inv = _invoice(r.id, label="2026-03")
    s.add(inv)
    await s.commit()
    return r, inv


def test_midflight_payment_reverts_suspend_without_done_overwrite(tmp_path, monkeypatch):
    """Payment lands between chunk 1 and chunk 2 (chunk_size=2, 4 users): queue_restore
    reverts the action mid-run. Old behavior: the worker finalized `done` over `reverted`
    and stamped the PAID invoice `enforced`. New behavior: the action stays `reverted`, the
    invoice stays `paid`, and chunk-2's users are merged into the pending restore."""
    from app.services import enforcement

    async def body(s, Session):
        r, inv = await _seed_suspend(s)
        action = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)
        assert action.status == EnforcementActionStatus.planned

        bulk_calls: list[tuple] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return {"u0": 10, "u1": 11, "u2": 12, "u3": 13}.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled):
            bulk_calls.append((tuple(sorted(user_ids)), enabled))
            if len(bulk_calls) == 1 and not enabled:
                # Chunk 1 just landed on the panel; the customer's payment is confirmed NOW
                # in another session → _maybe_restore → queue_restore reverts the action.
                async with Session() as s2:
                    inv2 = await s2.get(Invoice, inv.id)
                    inv2.status = InvoiceStatus.paid
                    inv2.paid_at = dt.datetime.now(dt.timezone.utc)
                    r2 = await s2.get(Reseller, r.id)
                    await enforcement.queue_restore(
                        s2, r2, require_no_due=True, reason="payment")

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        res = await enforcement.process_enforcement_queue(
            s, action_limit=5, user_chunk_size=2)
        assert res.get("reverted_midflight") == 1, f"got: {res}"

        await s.refresh(action)
        assert action.status == EnforcementActionStatus.reverted  # not overwritten by done
        await s.refresh(inv)
        assert inv.status == InvoiceStatus.paid                   # never stamped enforced

        # The restore exists and its work set covers BOTH chunks (u0..u3): chunk 2 landed
        # after queue_restore copied progress, so only the merge makes it whole.
        restore = (
            await s.execute(
                select(EnforcementAction).where(
                    EnforcementAction.action == EnforcementActionType.restore)
            )
        ).scalars().one()
        assert restore.status == EnforcementActionStatus.planned
        restore_users = set((restore.snapshot or {}).get("users") or {})
        id2u = {10: "u0", 11: "u1", 12: "u2", 13: "u3"}
        disabled = {u for ids, en in bulk_calls if not en for u in ids}
        # EVERY user actually disabled on the panel must be in the restore's work set —
        # including any chunk that landed after queue_restore copied progress.
        assert disabled, "suspend disabled nothing?"
        assert {id2u[i] for i in disabled} <= restore_users

        # Run the queue again: the restore re-enables everything that was disabled.
        res2 = await enforcement.process_enforcement_queue(
            s, action_limit=5, user_chunk_size=10)
        assert res2["done"] == 1
        enabled = {u for ids, en in bulk_calls if en for u in ids}
        assert enabled >= disabled  # everything disabled was re-enabled
        await s.refresh(inv)
        assert inv.status == InvoiceStatus.paid                   # restore didn't flip it
        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.active

    _run(body, tmp_path, "race1.db")


def test_finalize_skips_enforced_stamp_when_invoice_paid(tmp_path, monkeypatch):
    """Even without a queued restore (auto-restore path unavailable), the finalize must not
    stamp a paid invoice `enforced` — it re-checks debt and queues a restore instead."""
    from app.services import enforcement

    async def body(s, Session):
        r, inv = await _seed_suspend(s, n_users=2)
        await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return {"u0": 10, "u1": 11}.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled):
            if not enabled:
                # Payment lands mid-run but nothing reverts the action (e.g. auto-restore
                # couldn't run) — the old finalize would stamp the paid invoice enforced.
                async with Session() as s2:
                    inv2 = await s2.get(Invoice, inv.id)
                    if inv2.status != InvoiceStatus.paid:
                        inv2.status = InvoiceStatus.paid
                        inv2.paid_at = dt.datetime.now(dt.timezone.utc)
                        await s2.commit()

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        res = await enforcement.process_enforcement_queue(
            s, action_limit=5, user_chunk_size=10)
        # The finalize re-checked debt: no due invoice → the applied work went to a restore.
        assert res.get("restore_queued") == 1, f"got: {res}"
        await s.refresh(inv)
        assert inv.status == InvoiceStatus.paid

    _run(body, tmp_path, "race2.db")


def test_prune_keeps_restore_source_for_enforced_reseller(tmp_path):
    """An aged `done` suspend of a STILL-enforced reseller must survive the retention sweep —
    it is the snapshot queue_restore needs; without it a payment after 90 days restores
    nothing, silently."""
    from app.services import enforcement, maintenance

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)
        source = EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done, created_at=old,
            snapshot={"users": {"u0": "A"}, "admins": ["A"],
                      "limits": {"A": {"max_users": 10, "max_active_users": 10}},
                      "progress": {"phase": "done", "users_done": ["u0"],
                                   "users_missing": [], "users_failed": {},
                                   "user_attempts": {}, "admins_done": ["A"],
                                   "admins_missing": [], "admins_failed": {},
                                   "admin_attempts": {},
                                   "captured_limits": {"A": {"max_users": 10,
                                                             "max_active_users": 10}}}},
        )
        # An aged unrelated terminal row of an ACTIVE reseller — must still be pruned.
        r2 = Reseller(panel_id=1, admin_uuid="B", name="R2",
                      enforcement_state=EnforcementState.active)
        s.add(r2)
        await s.flush()
        other = EnforcementAction(
            reseller_id=r2.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done, created_at=old, snapshot={},
        )
        s.add_all([source, other])
        await s.commit()

        counts = await maintenance.prune_old_logs(s)
        assert counts["enforcement_actions"] == 1  # only the active reseller's row aged out

        kept = await s.get(EnforcementAction, source.id)
        assert kept is not None

        # …and the restore source still works: a payment restore finds it.
        restore = await enforcement.queue_restore(s, r, require_no_due=False, reason="payment")
        assert restore is not None
        assert set((restore.snapshot or {}).get("users") or {}) == {"u0"}

    _run(body, tmp_path, "prune1.db")
