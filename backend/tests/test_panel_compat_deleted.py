import datetime as dt
from types import SimpleNamespace

from app.services.invoice_engine import billable_gb_for_user
from app.services.panel_client.base import parse_backup
from app.services.periods import month_period
from app.services.presence import snapshot_present


def test_parser_preserves_explicit_delete():
    data = parse_backup({"admin_users": [], "users": [
        {"uuid": "gone", "deleted": True}, {"uuid": "live"},
    ]})
    assert data.users[0].deleted is True
    assert data.users[1].deleted is False


def test_tombstone_is_absent_even_with_fresh_sync():
    now = dt.datetime.now(dt.timezone.utc)
    assert not snapshot_present(now, now, panel_deleted=True)
    assert snapshot_present(now, now, panel_deleted=False)


def test_billing_tombstone_uses_existing_deleted_policy():
    now = dt.datetime.now(dt.timezone.utc)
    u = SimpleNamespace(user_uuid="gone", start_date=dt.date(2026, 11, 1),
                        usage_limit_gb=50, current_usage_gb=2, last_synced_at=now,
                        panel_deleted=True)
    assert billable_gb_for_user(u, month_period(2026, 11), set(), 1, now) == (2, True)
    assert billable_gb_for_user(u, month_period(2026, 11), set(), 1, now,
                                deleted_full_quota_over_gb=2) == (50, True)


async def test_sync_tombstones_preserve_terminal_usage_and_do_not_recreate_old_rows(tmp_path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import EndUserSnapshot, Panel, UsageMeter
    from app.services import sync
    from app.services.presence import snapshot_present_filter

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.timezone.utc)
    def data(deleted, usage):
        return parse_backup({"admin_users": [{"uuid": "owner", "mode": "super_admin"}], "users": [
            {"uuid": "u", "added_by_uuid": "owner", "usage_limit_GB": 50,
             "current_usage_GB": usage, "start_date": now.date().replace(day=1).isoformat(),
             "deleted": deleted},
            {"uuid": "old", "deleted": True, "start_date": "2000-01-01"},
        ]})
    try:
        async with factory() as session:
            panel = Panel(key="p", host="example.invalid", proxy_path_enc="", owner_uuid="owner")
            session.add(panel)
            await session.commit()
            await sync.sync_panel(session, panel, data=data(False, 1))
            await sync.sync_panel(session, panel, data=data(True, 2))
            first_meter = (await session.execute(select(UsageMeter))).scalar_one()
            consumed = first_meter.consumed_gb
            await sync.sync_panel(session, panel, data=data(True, 2))
            row = (await session.execute(select(EndUserSnapshot))).scalar_one()
            assert row.panel_deleted is True
            assert row.current_usage_gb == 2
            assert (await session.execute(select(UsageMeter))).scalar_one().consumed_gb == consumed
            present = (await session.execute(select(EndUserSnapshot).join(Panel)
                       .where(snapshot_present_filter()))).scalars().all()
            assert present == []
            await sync.sync_panel(session, panel, data=data(False, 2))
            assert row.panel_deleted is False
    finally:
        await engine.dispose()
