"""User-recovery tool: detect lost-user clusters + restore each under the correct admin."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/recov.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.services import user_recovery  # noqa: E402
from app.services.panel_client import admin_api  # noqa: E402


def _run(body, tmp_path, name):  # noqa: ANN001
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s, Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s):  # noqa: ANN001
    now = dt.datetime.now(dt.timezone.utc)
    latest = now - dt.timedelta(minutes=5)          # the panel's latest sync
    drop = latest - dt.timedelta(hours=1)           # the rollback "drop" moment (lost cluster)
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o",
              last_synced_at=latest)
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="ADMIN-A", name="Ali")
    s.add(r)
    await s.flush()

    def snap(uuid, added, lsa, name, gb=10, days=30, sd=None):  # noqa: ANN001, ANN202
        return EndUserSnapshot(
            panel_id=p.id, user_uuid=uuid, name=name, added_by_uuid=added, usage_limit_gb=gb,
            current_usage_gb=1, package_days=days, start_date=sd, enable=True, is_active=True,
            last_synced_at=lsa, meter_provisioned_gb=0, meter_consumed_gb=0, meter_init=True)

    s.add_all([
        snap("u-present", "ADMIN-A", latest, "Present"),                    # seen in latest → not lost
        snap("u-lost1", "ADMIN-A", drop, "Lost1", sd=dt.date(2026, 7, 20)),  # lost, admin resolves
        snap("u-lost2", "ADMIN-A", drop, "Lost2"),                          # lost, same cluster
        snap("u-noadmin", "UNKNOWN-UUID", drop, "NoAdmin"),                 # lost, admin unresolved
        snap("u-old", "ADMIN-A", latest - dt.timedelta(days=30), "Old"),    # stale but beyond lookback
    ])
    await s.commit()
    return p, r


def test_detect_finds_lost_clusters_only(tmp_path):
    async def body(s, _Session):  # noqa: ANN001
        p, _r = await _seed(s)
        res = await user_recovery.detect(s, [p.id], lookback_days=7)
        assert len(res) == 1
        panel = res[0]
        assert panel["total_lost"] == 3            # lost1, lost2, noadmin (present + old excluded)
        assert len(panel["clusters"]) == 1         # all three share the drop instant
        c = panel["clusters"][0]
        assert c["count"] == 3
        by = {u["name"]: u for u in c["users"]}
        assert by["Lost1"]["has_admin"] and by["Lost1"]["admin_name"] == "Ali"
        assert by["Lost1"]["start_date"] == "2026-07-20"   # start_date surfaced for the owner
        assert by["NoAdmin"]["has_admin"] is False         # unresolved admin flagged, not hidden
        uuids = {u["user_uuid"] for u in c["users"]}
        assert "u-present" not in uuids and "u-old" not in uuids
        # No sync_runs seeded → the hints are empty (owner still decides; nothing auto-classified).
        assert c["drop_size"] == 0 and c["had_failure"] is False
    _run(body, tmp_path, "d1.db")


def test_detect_annotates_drop_and_failure_hints(tmp_path):
    async def body(s, _Session):  # noqa: ANN001
        from app.models import SyncRun
        p, _r = await _seed(s)
        latest = p.last_synced_at
        drop_t = latest - dt.timedelta(hours=1)          # == the lost cluster's last-seen instant
        mid = drop_t + dt.timedelta(minutes=20)
        # success(100) at drop_t → a FAILED sync (panel down) → success(90) at latest: a drop of 10
        # with an outage in between — the migration tell. These are HINTS on the cluster, not a verdict.
        s.add_all([
            SyncRun(panel_id=p.id, source="backup_json", status="success", admin_count=1,
                    user_count=100, started_at=drop_t, finished_at=drop_t),
            SyncRun(panel_id=p.id, source="backup_json", status="failed", admin_count=0,
                    user_count=0, started_at=mid, finished_at=mid),
            SyncRun(panel_id=p.id, source="backup_json", status="success", admin_count=1,
                    user_count=90, started_at=latest, finished_at=latest),
        ])
        await s.commit()
        res = await user_recovery.detect(s, [p.id], lookback_days=7)
        c = res[0]["clusters"][0]
        assert c["drop_size"] == 10 and c["had_failure"] is True
    _run(body, tmp_path, "d3.db")


def test_old_losses_excluded_by_lookback(tmp_path):
    async def body(s, _Session):  # noqa: ANN001
        p, _r = await _seed(s)
        res = await user_recovery.detect(s, [p.id], lookback_days=1)   # 30-day-old still excluded
        assert res[0]["total_lost"] == 3
    _run(body, tmp_path, "d2.db")


def test_restore_creates_under_correct_admin_same_uuid(tmp_path, monkeypatch):
    calls = []

    async def fake_get(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return None   # absent → proceed to create

    async def fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):  # noqa: ANN001, ANN002
        calls.append((user_uuid, api_key, name, gb, days))
        return user_uuid

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get)
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", fake_create)

    async def body(s, Session):  # noqa: ANN001
        p, _r = await _seed(s)
        out = await user_recovery.restore(Session, [(p.id, "u-lost1")])
        assert out["counts"]["created"] == 1
        uuid, key, name, gb, days = calls[0]
        # SAME uuid, the ORIGINAL admin's key (→ correct added_by), original plan.
        assert uuid == "u-lost1" and key == "ADMIN-A" and name == "Lost1" and gb == 10 and days == 30
    _run(body, tmp_path, "r1.db")


def test_restore_skips_present_and_errors_when_admin_missing(tmp_path, monkeypatch):
    created = []

    async def fake_get(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"uuid": uuid} if uuid == "u-lost1" else None   # lost1 already back on the panel

    async def fake_create(self, panel, *, name, gb, days, api_key=None, user_uuid=None):  # noqa: ANN001, ANN002
        created.append(user_uuid)
        return user_uuid

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get)
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", fake_create)

    async def body(s, Session):  # noqa: ANN001
        p, _r = await _seed(s)
        out = await user_recovery.restore(
            Session, [(p.id, "u-lost1"), (p.id, "u-noadmin"), (p.id, "u-lost2")])
        assert out["counts"]["skipped"] == 1   # lost1 already present → not duplicated
        assert out["counts"]["errors"] == 1    # noadmin → refused (can't place safely)
        assert out["counts"]["created"] == 1   # lost2 created
        assert created == ["u-lost2"]
    _run(body, tmp_path, "r2.db")


def test_restore_dry_run_creates_nothing(tmp_path, monkeypatch):
    created = []

    async def fake_get(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return None

    async def fake_create(self, panel, **kw):  # noqa: ANN001, ANN003
        created.append(kw.get("user_uuid"))
        return "x"

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get)
    monkeypatch.setattr(admin_api.AdminApiClient, "create_user", fake_create)

    async def body(s, Session):  # noqa: ANN001
        p, _r = await _seed(s)
        out = await user_recovery.restore(Session, [(p.id, "u-lost1")], dry_run=True)
        assert out["counts"]["created"] == 1   # reported as "would create"
        assert created == []                   # but nothing actually created
    _run(body, tmp_path, "r3.db")
