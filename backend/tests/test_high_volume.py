"""High-volume users (the 1000 GB mistake): creator→responsible-root mapping, threshold +
this-month filter, present-filter parity with billing, would-be amount, and the warn grouping."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/hv.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import DeliveryLog, EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import PanelStatus  # noqa: E402
from app.services import high_volume as hv  # noqa: E402
from app.services.periods import current_month  # noqa: E402

# Naive datetimes: on SQLite the DateTime columns round-trip naive, so present-filter comparisons
# stay naive-vs-naive (prod is Postgres + tz-aware; the comparison logic is identical).
NOW = dt.datetime.now()  # noqa: DTZ005
OLD = NOW - dt.timedelta(days=3)
THIS_MONTH = current_month().start
LAST_MONTH = current_month().start - dt.timedelta(days=1)


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _user(panel_id, added_by, gb, start, name="u", *, user_synced=NOW, consumed=0.0):
    return EndUserSnapshot(panel_id=panel_id, user_uuid=f"{added_by}-{gb}-{name}",
                           added_by_uuid=added_by, usage_limit_gb=gb, current_usage_gb=consumed,
                           start_date=start, name=name, enable=True, is_active=True,
                           last_synced_at=user_synced)


async def _seed(s, *, panel_status=PanelStatus.ok, root_seen=NOW):
    p = Panel(key="p1", host="p1", proxy_path_enc="x", owner_uuid="OWNER",
              status=panel_status, last_synced_at=NOW)
    s.add(p)
    await s.flush()
    s.add_all([
        Reseller(panel_id=p.id, admin_uuid="OWNER", name="Owner", is_owner=True, last_seen_at=NOW),
        Reseller(panel_id=p.id, admin_uuid="ROOT", parent_admin_uuid="OWNER", name="RootAdmin",
                 price_per_gb=2000, bot_chat_id=111, last_seen_at=root_seen),
        Reseller(panel_id=p.id, admin_uuid="SUB", parent_admin_uuid="ROOT", name="SubAdmin",
                 last_seen_at=NOW),
        Reseller(panel_id=p.id, admin_uuid="GS", parent_admin_uuid="SUB", name="DeepSub",
                 last_seen_at=NOW),
    ])
    return p


def test_mapping_threshold_month_and_amount():
    async def body(s):
        p = await _seed(s)
        s.add_all([
            _user(p.id, "ROOT", 1000, THIS_MONTH, "byroot"),     # root creator
            _user(p.id, "GS", 1500, THIS_MONTH, "bydeepsub"),    # deep-sub creator
            _user(p.id, "ROOT", 500, THIS_MONTH, "small"),       # below threshold → excluded
            _user(p.id, "ROOT", 1000, LAST_MONTH, "oldmonth"),   # not this month → excluded
            _user(p.id, "OWNER", 1000, THIS_MONTH, "byowner"),   # creator not billable → excluded
        ])
        await s.commit()

        rows = await hv.high_volume_users(s, threshold=1000, this_month_only=True)
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"byroot", "bydeepsub"}
        # responsible root is always the top-level admin, even for a deep sub's user
        assert by_name["bydeepsub"]["root_name"] == "RootAdmin"
        assert by_name["bydeepsub"]["creator_is_sub"] is True
        assert by_name["bydeepsub"]["creator_name"] == "DeepSub"
        assert by_name["byroot"]["creator_is_sub"] is False
        # would_be = gb × root price (override 2000)
        assert by_name["byroot"]["would_be_toman"] == 2_000_000
        assert by_name["bydeepsub"]["would_be_toman"] == 3_000_000
        assert by_name["byroot"]["root_bot_chat_id"] == 111

        # this_month_only=False also surfaces last-month high-volume users
        rows_all = await hv.high_volume_users(s, threshold=1000, this_month_only=False)
        assert "oldmonth" in {r["name"] for r in rows_all}
    _run(body)


def test_removed_users_billed_on_consumption_are_excluded():
    async def body(s):
        p = await _seed(s)
        # All three have a huge SOLD quota, but were REMOVED from Hiddify (stale last_synced_at).
        s.add_all([
            _user(p.id, "ROOT", 1_000_000, THIS_MONTH, "out_low",   # consumed < free → dropped
                  user_synced=OLD, consumed=0.8),
            _user(p.id, "ROOT", 1000, THIS_MONTH, "out_mid",        # 1..5 GB → billed on consumption
                  user_synced=OLD, consumed=3),
            _user(p.id, "ROOT", 1000, THIS_MONTH, "out_high",       # >=5 GB → billed FULL quota
                  user_synced=OLD, consumed=20),
        ])
        await s.commit()
        rows = await hv.high_volume_users(s, threshold=1000, this_month_only=True)
        names = {r["name"] for r in rows}
        # The 1-billion-GB "out_low" (deleted, barely used) must NOT appear — it's billed ~0.
        assert "out_low" not in names
        assert "out_mid" not in names          # billed on its small consumption, not the quota
        assert "out_high" in names             # deleted but consumed a lot → billed full quota
        assert next(r for r in rows if r["name"] == "out_high")["from_deleted"] is True
    _run(body)


def test_present_filter_excludes_failed_panel_and_removed_root():
    async def body(s):
        p = await _seed(s, panel_status=PanelStatus.error)   # failed sync → not billable
        s.add(_user(p.id, "ROOT", 1000, THIS_MONTH))
        await s.commit()
        assert await hv.high_volume_users(s, threshold=1000, this_month_only=True) == []
    _run(body)

    async def body2(s):
        p = await _seed(s, root_seen=OLD)   # root removed from panel (stale last_seen)
        s.add(_user(p.id, "ROOT", 1000, THIS_MONTH))
        await s.commit()
        assert await hv.high_volume_users(s, threshold=1000, this_month_only=True) == []
    _run(body2)


def test_build_warnings_groups_per_root_and_counts_unregistered():
    async def body(s):
        p = await _seed(s)
        # add a SECOND root that is NOT registered in the bot
        s.add(Reseller(panel_id=p.id, admin_uuid="ROOT2", parent_admin_uuid="OWNER",
                       name="UnregRoot", price_per_gb=1000, bot_chat_id=None, last_seen_at=NOW))
        s.add_all([
            _user(p.id, "ROOT", 1000, THIS_MONTH, "a"),
            _user(p.id, "GS", 2000, THIS_MONTH, "b"),     # deep sub under ROOT
            _user(p.id, "ROOT2", 1000, THIS_MONTH, "c"),  # under the unregistered root
        ])
        await s.commit()

        reachable, texts, unregistered = await hv.build_warnings(
            s, threshold=1000, panel_id=None, this_month_only=True, root_reseller_ids=None)
        assert len(reachable) == 1 and reachable[0]["chat_id"] == 111   # only the registered root
        assert unregistered == 1                                        # ROOT2 can't be DMed
        msg = texts[reachable[0]["reseller_id"]]
        assert "ساختهٔ زیرمجموعهٔ شما: ⁨DeepSub⁩" in msg                 # ↳ line only for the deep sub
        assert msg.count("👤") == 2                                     # both high-volume users listed
        # nothing persisted
        assert (await s.execute(select(func.count(DeliveryLog.id)))).scalar_one() == 0
    _run(body)


def test_rows_expose_snapshot_id_and_keep_the_uuid_truncated():
    """The delete action addresses rows by `snapshot_id`: the displayed uuid is deliberately cut to
    8 chars, so it cannot identify a user and must never become the handle."""
    async def body(s):
        p = await _seed(s)
        s.add(_user(p.id, "ROOT", 1000, THIS_MONTH, "byroot"))
        await s.commit()

        rows = await hv.high_volume_users(s, threshold=1000, this_month_only=True)
        assert rows, "expected at least one high-volume row"
        ids = set((await s.execute(select(EndUserSnapshot.id))).scalars().all())
        for r in rows:
            assert r["snapshot_id"] in ids
            assert r["panel_id"] == p.id
            assert len(r["user_uuid"]) <= 8

    _run(body)
