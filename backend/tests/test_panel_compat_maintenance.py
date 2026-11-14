from types import SimpleNamespace

import pytest

from app.services.panel_client.admin_api import AdminApiClient
from app.services.panel_maintenance import PanelMaintenanceError
from tests.pg_barrier import requires_pg
from tests.test_panel_compat_reads import transport


@pytest.mark.parametrize("method,args,kwargs", [
    ("create_user", (), {"name": "u", "gb": 2, "days": 7}),
    ("set_user_enabled", ("u", False), {}),
    ("patch_user", ("u", {"enable": True}), {}),
    ("bulk_set_users_enabled", ([1], False), {"api_key": "r"}),
    ("bulk_delete_users", ([1],), {"api_key": "r"}),
    ("set_admin_limits", ("r", 0, 0), {}),
    ("set_admin_password", ("r", ""), {}),
    ("delete_admin", ("r",), {}),
])
async def test_maintenance_blocks_all_panel_writes_before_network(monkeypatch, method, args, kwargs):
    def network(request):
        raise AssertionError("maintenance must block before network I/O")
    transport(monkeypatch, network)
    with pytest.raises(PanelMaintenanceError):
        await getattr(AdminApiClient(), method)(SimpleNamespace(maintenance=True), *args, **kwargs)


async def test_creation_guard_reports_maintenance_before_capacity():
    from app.services.usercreate import creation_blocked
    class Session:
        async def get(self, model, pk):
            return SimpleNamespace(maintenance=True)
    assert await creation_blocked(Session(), SimpleNamespace(panel_id=1)) == "maintenance"


def test_billing_refuses_maintenance_without_calling_it_zero_sales():
    from app.services.invoicing import _panel_billable
    ok, reason = _panel_billable(SimpleNamespace(maintenance=True))
    assert not ok
    assert "به‌روزرسانی" in reason


async def test_storefront_maintenance_never_claims_or_charges(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import Panel, Reseller, StorefrontBot
    from app.services import storefront_provision as sp
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'maintenance.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def forbidden(*args, **kwargs):
        raise AssertionError("maintenance must return before an operation or charge")
    monkeypatch.setattr(sp.storefront_ops, "claim", forbidden)
    try:
        async with factory() as session:
            from app.core import crypto
            p = Panel(key="p", host="h", owner_uuid="owner", proxy_path_enc="", maintenance=True)
            session.add(p)
            await session.flush()
            r = Reseller(panel_id=p.id, admin_uuid="r", name="r")
            session.add(r)
            await session.flush()
            sf = StorefrontBot(reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("test-token"),
                               bot_username="compatbot", status="active")
            session.add(sf)
            await session.commit()
            sid = sf.id
        result = await sp.purchase(factory, sf_id=sid, customer_id=1, plan_id=1, label="test")
        assert not result.ok and result.reason == "maintenance"
    finally:
        await engine.dispose()


async def test_monthly_maintenance_does_not_consume_attempts(monkeypatch, tmp_path):
    import datetime as dt
    from unittest.mock import AsyncMock

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import MonthlyBillingRun, Panel
    from app.scheduler import jobs
    from app.services import monthly_billing
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'monthly.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "load_config", AsyncMock(return_value=jobs.ScheduleConfig()))
    from app.services.invoicing import GenerationSummary
    monkeypatch.setattr(monthly_billing.sync_service, "sync_all", AsyncMock())
    monkeypatch.setattr(monthly_billing.invoicing, "generate_invoices", AsyncMock(
        return_value=GenerationSummary(period="2090-07", skipped_panels=["p: maintenance"])))
    monkeypatch.setattr(monthly_billing.delivery, "send_period", AsyncMock(return_value={"sent": 1}))
    monkeypatch.setattr(monthly_billing.owner_notify, "notify_owner", AsyncMock())
    try:
        async with factory() as session:
            session.add(Panel(key="p", host="h", proxy_path_enc="", owner_uuid="o", maintenance=True))
            await session.commit()
            result = await monthly_billing.run_due(session, now=dt.datetime(2090, 8, 2, 12, tzinfo=dt.timezone.utc))
            assert result["status"] == "retry"
            run = (await session.execute(select(MonthlyBillingRun))).scalar_one()
            assert run.attempts == 0
            monthly_billing.delivery.send_period.assert_awaited_once()
    finally:
        await engine.dispose()


@pytest.mark.pg_contract
@requires_pg
async def test_pg_maintenance_drains_writers_and_checks_stale_objects():
    import uuid

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Panel
    from app.services.panel_maintenance import panel_operation, set_maintenance
    from tests.pg_barrier import DB_URL
    engine = create_async_engine(DB_URL, pool_size=1, max_overflow=0, pool_timeout=2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import asyncio
    release_child = asyncio.Event()
    child_task = None
    pid = None
    try:
        async with factory() as s:
            p = Panel(key=uuid.uuid4().hex[:20], host="compat.invalid", proxy_path_enc="", owner_uuid="o")
            s.add(p)
            await s.commit()
            pid = p.id
            async with panel_operation(p, s):
                async with factory() as other:
                    same = await other.get(Panel, pid)
                    with pytest.raises(RuntimeError, match="عملیات"):
                        await set_maintenance(other, same, True)
                    await other.rollback()
                async def child():
                    await release_child.wait()
                    try:
                        async with panel_operation(p, s):
                            return False
                    except PanelMaintenanceError:
                        return True
                child_task = asyncio.create_task(child())
            async with factory() as other:
                same = await other.get(Panel, pid)
                await set_maintenance(other, same, True)
            release_child.set()
            assert await child_task is True  # A child must not inherit a released parent lease.
            assert p.maintenance is False  # Deliberately stale identity map.
            with pytest.raises(PanelMaintenanceError):
                async with panel_operation(p, s):
                    raise AssertionError("stale object bypassed persisted maintenance")
    finally:
        release_child.set()
        if child_task is not None and not child_task.done():
            await child_task
        if pid is not None:
            async with factory() as s:
                await s.execute(delete(Panel).where(Panel.id == pid))
                await s.commit()
        await engine.dispose()


async def test_tenant_check_precedes_maintenance_result(monkeypatch):
    from app.services.panel_maintenance import storefront_write
    class Session:
        bind = None
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, model, pk):
            from app.models import StorefrontBot, StorefrontCustomer, StorefrontOrder
            if model is StorefrontOrder:
                return SimpleNamespace(customer_id=1)
            if model is StorefrontCustomer:
                return SimpleNamespace(storefront_bot_id=2)
            if model is StorefrontBot:
                return SimpleNamespace(id=2)
            raise AssertionError("A foreign tenant must be rejected before resolving its panel")
    @storefront_write(lambda ok, **kw: kw)
    async def operation(session_factory, *, order_id, expected_sf_id):
        raise AssertionError("foreign order")
    assert await operation(Session, order_id=1, expected_sf_id=3) == {"reason": "not_found"}


@pytest.mark.pg_contract
@requires_pg
@pytest.mark.parametrize("operation", ["sync", "billing"])
async def test_pg_maintenance_drains_complete_sync_and_billing(monkeypatch, operation):
    import asyncio
    import uuid

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Panel
    from app.services import invoicing, sync
    from app.services.panel_maintenance import set_maintenance
    from app.services.periods import current_month
    from tests.pg_barrier import DB_URL

    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return invoicing.GenerationSummary(period=current_month().label)
    monkeypatch.setattr(sync if operation == "sync" else invoicing,
                        "_sync_panel" if operation == "sync" else "_generate_invoices", held)
    pid = None
    task = None
    try:
        async with factory() as s:
            panel = Panel(key=uuid.uuid4().hex[:20], host="compat.invalid", proxy_path_enc="", owner_uuid="o")
            s.add(panel)
            await s.commit()
            pid = panel.id
        async def writer():
            async with factory() as s:
                if operation == "sync":
                    await sync.sync_panel(s, await s.get(Panel, pid))
                else:
                    await invoicing.generate_invoices(s, current_month(), panel_id=pid)
        task = asyncio.create_task(writer())
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with factory() as other:
            panel = await other.get(Panel, pid)
            with pytest.raises(RuntimeError, match="عملیات"):
                await set_maintenance(other, panel, True)
        release.set()
        await task
        async with factory() as other:
            await set_maintenance(other, await other.get(Panel, pid), True)
    finally:
        release.set()
        if task is not None:
            await task
        if pid is not None:
            async with factory() as s:
                await s.execute(delete(Panel).where(Panel.id == pid))
                await s.commit()
        await engine.dispose()
