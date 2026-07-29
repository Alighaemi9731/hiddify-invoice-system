"""Deleting a high-volume end-user on the panel AND purging it from billing.

The invariant every case here defends: a local row is dropped ONLY when the panel is PROVEN to no
longer hold that user. Any panel failure — lookup, delete, or verification — must leave the snapshot
AND its meters completely untouched, because a row purged while the user is still on the panel goes
on being billed to somebody with nothing left to notice it by.

Two keys, two purposes (`admin_api._headers`: `api_key=None` == super-admin):
writes are scoped to the OWNING admin (post-2026-07-18 defence), existence checks use the
super-admin key because only it can prove absence.
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/hvdel.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    UsageMeter,
    UsageMeterEvent,
)
from app.services import end_user_delete, high_volume  # noqa: E402
from app.services.end_user_delete import DeleteStatus  # noqa: E402


def _run(body, tmp_path, name="hv.db"):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
        from app.core.db import Base

        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(s, *, gb=1000, owner="A", panel_user_id=None, panels=1):
    """One panel, one reseller, one 1000 GB snapshot with a meter + meter event."""
    for pid in range(1, panels + 1):
        s.add(Panel(id=pid, key=f"p{pid}", host=f"h{pid}", proxy_path_enc="x",
                    owner_uuid="super"))
    s.add(Reseller(panel_id=1, admin_uuid="A", name="Ali"))
    snap = EndUserSnapshot(panel_id=1, user_uuid="uuid-big-1", name="Big",
                           added_by_uuid=owner, usage_limit_gb=gb, current_usage_gb=3,
                           panel_user_id=panel_user_id)
    s.add(snap)
    s.add(UsageMeter(panel_id=1, user_uuid="uuid-big-1", period_label="2026-07"))
    s.add(UsageMeterEvent(panel_id=1, user_uuid="uuid-big-1", period_label="2026-07",
                          kind="overage", gb=1))
    await s.commit()
    return snap


class FakeClient:
    """Records every call with the api_key it was made under."""

    def __init__(self, *, resolve=None, verify=None, delete_exc=None, lookup_exc=None):
        self._resolve = resolve            # id returned to the OWNER-scoped lookup (None = 404)
        self._verify = verify              # id returned to the SUPER-ADMIN lookup (None = absent)
        self._delete_exc = delete_exc
        self._lookup_exc = lookup_exc
        self.lookups: list[tuple[str, str | None]] = []   # (uuid, api_key)
        self.deletes: list[tuple[tuple[int, ...], str | None]] = []

    async def get_user_id(self, panel, user_uuid, *, api_key=None):
        self.lookups.append((user_uuid, api_key))
        if self._lookup_exc is not None:
            raise RuntimeError(self._lookup_exc)
        if api_key is None:                                  # super-admin oracle
            v = self._verify
            return v(user_uuid, len(self.deletes)) if callable(v) else v
        r = self._resolve
        return r(user_uuid) if callable(r) else r

    async def bulk_delete_users(self, panel, user_ids, *, api_key=None):
        self.deletes.append((tuple(user_ids), api_key))
        if self._delete_exc is not None:
            raise RuntimeError(self._delete_exc)


async def _counts(s):
    snaps = (await s.execute(select(func.count()).select_from(EndUserSnapshot))).scalar_one()
    meters = (await s.execute(select(func.count()).select_from(UsageMeter))).scalar_one()
    events = (await s.execute(select(func.count()).select_from(UsageMeterEvent))).scalar_one()
    return snaps, meters, events


# ── the happy path ────────────────────────────────────────────────────────────

def test_present_user_is_deleted_then_purged(tmp_path):
    async def body(s):
        snap = await _seed(s)
        # Present for the owner (id 7); gone for the super-admin once the delete has run.
        client = FakeClient(resolve=7, verify=lambda _u, ndel: None if ndel else 7)
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert [r.status for r in res.rows] == [DeleteStatus.deleted]
        assert res.rows[0].purged is True
        assert res.deleted == 1 and res.purged == 1 and res.failed == 0
        assert client.deletes == [((7,), "A")]          # written as the OWNING admin
        assert await _counts(s) == (0, 0, 0)            # snapshot + meter + event all gone
        assert res.meters_deleted == 1

    _run(body, tmp_path)


def test_absent_user_is_purged_without_touching_the_panel(tmp_path):
    """Already deleted from Hiddify → nothing to delete, but the billing rows must still go."""

    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=None, verify=None)      # 404 under both keys
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.already_absent
        assert res.rows[0].purged is True
        assert client.deletes == []                          # no write attempted at all
        assert await _counts(s) == (0, 0, 0)

    _run(body, tmp_path)


# ── every panel failure must purge NOTHING (the owner's explicit rule) ────────

def test_panel_delete_failure_purges_nothing(tmp_path):
    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=None, delete_exc="panel unreachable")
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.delete_failed
        assert res.rows[0].purged is False
        assert "panel unreachable" in (res.rows[0].error or "")
        assert await _counts(s) == (1, 1, 1)     # snapshot AND both meter tables intact

    _run(body, tmp_path)


def test_lookup_failure_purges_nothing(tmp_path):
    async def body(s):
        snap = await _seed(s)
        client = FakeClient(lookup_exc="503 from panel")
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.lookup_failed
        assert client.deletes == []
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_verification_says_still_present_blocks_the_purge(tmp_path):
    """The 2026-07-18 lesson: a 200 from the bulk action proves nothing. If the user is still there
    afterwards we must keep billing it rather than silently lose the record."""

    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=7)      # still present after the delete
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.verify_failed
        assert res.rows[0].purged is False
        assert "still present" in (res.rows[0].error or "")
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_verification_error_is_treated_as_unverified(tmp_path):
    async def body(s):
        def verify(_uuid, _ndel):
            # The user resolved fine as the owner, so the super-admin oracle is consulted exactly
            # once — the post-delete check — and here it fails. We cannot prove the delete landed.
            raise RuntimeError("timeout")

        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=verify)
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.verify_failed
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_404_as_owner_but_present_as_super_admin_is_not_purged(tmp_path):
    """An owner-scoped 404 is ambiguous — absent, OR no longer owned by that admin. Purging on it
    alone would drop a row for a user still on the panel and still billed to somebody."""

    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=None, verify=7)     # 404 as owner, present as super-admin
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.owner_mismatch
        assert res.rows[0].purged is False
        assert client.deletes == []
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


# ── key scoping and id freshness ─────────────────────────────────────────────

def test_owning_admin_key_for_writes_super_admin_for_verification(tmp_path):
    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=lambda _u, ndel: None if ndel else 7)
        await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert client.lookups[0] == ("uuid-big-1", "A")     # resolve AS the owning admin
        assert client.deletes[0][1] == "A"                  # write AS the owning admin
        assert client.lookups[-1] == ("uuid-big-1", None)   # verify as super-admin

    _run(body, tmp_path)


def test_cached_panel_user_id_is_never_reused(tmp_path):
    """Hiddify renumbers user ids on a restore/re-import, so a cached id can address someone else's
    customer. Deletion is unrecoverable — the id must always be resolved fresh."""

    async def body(s):
        snap = await _seed(s, panel_user_id=999)      # stale cached id
        client = FakeClient(resolve=7, verify=lambda _u, ndel: None if ndel else 7)
        await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert client.deletes == [((7,), "A")]        # the freshly resolved id, not 999

    _run(body, tmp_path)


def test_deletes_are_grouped_per_owning_admin(tmp_path):
    async def body(s):
        await _seed(s)
        s.add(Reseller(panel_id=1, admin_uuid="B", name="Bob"))
        other = EndUserSnapshot(panel_id=1, user_uuid="uuid-big-2", name="Big2",
                                added_by_uuid="B", usage_limit_gb=1000)
        s.add(other)
        await s.commit()
        ids = (await s.execute(select(EndUserSnapshot.id))).scalars().all()

        client = FakeClient(resolve=lambda u: 7 if u.endswith("1") else 8,
                            verify=lambda _u, ndel: None if ndel >= 2 else 7)
        res = await end_user_delete.delete_end_users(s, list(ids), client=client)

        assert len(client.deletes) == 2                       # one call per owning admin
        assert dict((k, v) for v, k in client.deletes) == {"A": (7,), "B": (8,)}
        assert res.deleted == 2

    _run(body, tmp_path)


def test_one_failing_owner_does_not_abort_the_batch(tmp_path):
    async def body(s):
        await _seed(s)
        s.add(Reseller(panel_id=1, admin_uuid="B", name="Bob"))
        s.add(EndUserSnapshot(panel_id=1, user_uuid="uuid-big-2", name="Big2",
                              added_by_uuid="B", usage_limit_gb=1000))
        await s.commit()
        ids = (await s.execute(
            select(EndUserSnapshot.id).order_by(EndUserSnapshot.user_uuid))).scalars().all()

        class PartlyBroken(FakeClient):
            async def bulk_delete_users(self, panel, user_ids, *, api_key=None):
                self.deletes.append((tuple(user_ids), api_key))
                if api_key == "B":
                    raise RuntimeError("rejected")

        client = PartlyBroken(resolve=lambda u: 7 if u.endswith("1") else 8,
                              verify=lambda _u, ndel: None if ndel else 7)
        res = await end_user_delete.delete_end_users(s, list(ids), client=client)

        got = {r.user_uuid: r.status for r in res.rows}
        assert got["uuid-big-1"] == DeleteStatus.deleted
        assert got["uuid-big-2"] == DeleteStatus.delete_failed
        # The healthy row is purged; the failed one keeps everything.
        remaining = (await s.execute(select(EndUserSnapshot.user_uuid))).scalars().all()
        assert remaining == ["uuid-big-2"]

    _run(body, tmp_path)


# ── guards ───────────────────────────────────────────────────────────────────

def test_low_quota_row_is_refused_without_any_panel_call(tmp_path):
    async def body(s):
        snap = await _seed(s, gb=30)
        client = FakeClient(resolve=7, verify=None)
        res = await end_user_delete.delete_end_users(
            s, [snap.id], min_usage_limit_gb=1000, client=client)

        assert res.rows[0].status == DeleteStatus.skipped_low_quota
        assert client.lookups == [] and client.deletes == []
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_row_without_owning_admin_is_refused(tmp_path):
    """No `added_by_uuid` means we could only write as super-admin — the unscoped write that made a
    stale id catastrophic. Refuse rather than fall back."""

    async def body(s):
        snap = await _seed(s, owner="")
        client = FakeClient(resolve=7, verify=None)
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.skipped_no_owner
        assert client.deletes == []
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_live_storefront_config_is_refused(tmp_path):
    """That config was bought by a shop customer; deleting it here would kill a paid service and
    strand the order, bypassing the storefront's own refund/bookkeeping path."""

    async def body(s):
        snap = await _seed(s)
        bot = StorefrontBot(reseller_id=1, panel_id=1, bot_token_enc="x", bot_telegram_id=42)
        s.add(bot)
        await s.flush()
        cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=99)
        s.add(cust)
        await s.flush()
        s.add(StorefrontOrder(customer_id=cust.id, panel_id=1, gb=10, days=30,
                              price_toman=1000, status="provisioned",
                              panel_user_uuid="uuid-big-1"))
        await s.commit()

        client = FakeClient(resolve=7, verify=None)
        res = await end_user_delete.delete_end_users(s, [snap.id], client=client)

        assert res.rows[0].status == DeleteStatus.skipped_storefront
        assert client.deletes == []
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_unknown_snapshot_id_is_reported_not_fatal(tmp_path):
    async def body(s):
        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=lambda _u, ndel: None if ndel else 7)
        res = await end_user_delete.delete_end_users(s, [snap.id, 9999], client=client)

        got = {r.snapshot_id: r.status for r in res.rows}
        assert got[9999] == DeleteStatus.not_found
        assert got[snap.id] == DeleteStatus.deleted      # the real row still processed

    _run(body, tmp_path)


# ── the feature wrapper's floor ──────────────────────────────────────────────

def test_high_volume_wrapper_floors_a_tiny_threshold(tmp_path):
    """The threshold box is a display filter the owner may lower; the floor is what makes deleting
    an ordinary customer through this button structurally impossible."""

    async def body(s):
        snap = await _seed(s, gb=50)                 # a normal customer
        calls = {}

        async def spy(session, ids, **kw):
            calls.update(kw)
            return end_user_delete.DeleteBatchResult()

        original = end_user_delete.delete_end_users
        end_user_delete.delete_end_users = spy       # type: ignore[assignment]
        try:
            await high_volume.delete_high_volume_users(
                s, snapshot_ids=[snap.id], threshold=1)
        finally:
            end_user_delete.delete_end_users = original  # type: ignore[assignment]

        assert calls["min_usage_limit_gb"] == high_volume.HIGH_VOLUME_DELETE_FLOOR_GB

    _run(body, tmp_path)


def test_high_volume_wrapper_keeps_a_higher_threshold(tmp_path):
    async def body(s):
        snap = await _seed(s)
        calls = {}

        async def spy(session, ids, **kw):
            calls.update(kw)
            return end_user_delete.DeleteBatchResult()

        original = end_user_delete.delete_end_users
        end_user_delete.delete_end_users = spy       # type: ignore[assignment]
        try:
            await high_volume.delete_high_volume_users(
                s, snapshot_ids=[snap.id], threshold=1000)
        finally:
            end_user_delete.delete_end_users = original  # type: ignore[assignment]

        assert calls["min_usage_limit_gb"] == 1000.0

    _run(body, tmp_path)


# ── endpoint level ───────────────────────────────────────────────────────────

def test_endpoint_uses_the_settings_threshold_as_the_guard(tmp_path):
    """With no threshold in the body the server falls back to `high_volume_gb_threshold`, so a
    500 GB row is refused even though the caller named no limit at all."""

    async def body(s):
        from app.api.reports import HighVolumeDeleteBody, high_volume_users_delete
        from app.services import settings_service

        snap = await _seed(s, gb=500)
        await settings_service.set_value(s, "high_volume_gb_threshold", 1000)

        called = {"n": 0}

        class Guard(FakeClient):
            async def get_user_id(self, *a, **kw):
                called["n"] += 1
                raise AssertionError("a refused row must never reach the panel")

        original = end_user_delete.AdminApiClient
        end_user_delete.AdminApiClient = Guard      # type: ignore[misc]
        try:
            res = await high_volume_users_delete(
                HighVolumeDeleteBody(snapshot_ids=[snap.id]), session=s)
        finally:
            end_user_delete.AdminApiClient = original  # type: ignore[misc]

        assert res.skipped == 1 and res.purged == 0
        assert res.rows[0].status == DeleteStatus.skipped_low_quota.value
        assert called["n"] == 0
        assert await _counts(s) == (1, 1, 1)

    _run(body, tmp_path)


def test_endpoint_truncates_the_uuid_like_the_list_does(tmp_path):
    async def body(s):
        from app.api.reports import HighVolumeDeleteBody, high_volume_users_delete

        snap = await _seed(s)
        client = FakeClient(resolve=7, verify=lambda _u, ndel: None if ndel else 7)
        original = end_user_delete.AdminApiClient
        end_user_delete.AdminApiClient = lambda *a, **kw: client  # type: ignore[misc]
        try:
            res = await high_volume_users_delete(
                HighVolumeDeleteBody(snapshot_ids=[snap.id]), session=s)
        finally:
            end_user_delete.AdminApiClient = original  # type: ignore[misc]

        assert res.deleted == 1 and res.purged == 1
        assert res.rows[0].user_uuid == "uuid-big"      # 8 chars, never the full uuid
        assert res.meters_deleted == 1

    _run(body, tmp_path)


def test_endpoint_rejects_an_empty_or_oversized_batch(tmp_path):
    import pytest
    from pydantic import ValidationError

    from app.api.reports import HighVolumeDeleteBody

    with pytest.raises(ValidationError):
        HighVolumeDeleteBody(snapshot_ids=[])
    with pytest.raises(ValidationError):
        HighVolumeDeleteBody(snapshot_ids=list(range(201)))
    assert HighVolumeDeleteBody(snapshot_ids=[1, 2]).threshold is None
