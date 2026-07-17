"""Release A storefront portal: owner scoping, safe DTOs and bounded read-only reporting."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import portal_storefront
from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.core.storefront_access import list_owned_storefronts, require_owned_storefront
from app.main import app
from app.models import (
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCreditCode,
    StorefrontCreditRedemption,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.services import storefront_reporting

UTC = dt.timezone.utc


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                await body(session, engine)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    p1 = Panel(key="p1", name="Panel 1", host="p1.invalid", proxy_path_enc="secret-path",
               owner_uuid="owner-1")
    p2 = Panel(key="p2", name="Panel 2", host="p2.invalid", proxy_path_enc="secret-path-2",
               owner_uuid="owner-2")
    session.add_all([p1, p2])
    await session.flush()
    r1 = Reseller(panel_id=p1.id, admin_uuid="a1", name="Alpha", bot_chat_id=111)
    r2 = Reseller(panel_id=p2.id, admin_uuid="a2", name="Beta", bot_chat_id=111)
    foreign = Reseller(panel_id=p1.id, admin_uuid="b1", name="Foreign", bot_chat_id=222)
    disabled = Reseller(panel_id=p2.id, admin_uuid="a3", name="Disabled", bot_chat_id=111,
                        storefront_enabled=False)
    session.add_all([r1, r2, foreign, disabled])
    await session.flush()
    shop = StorefrontBot(
        reseller_id=r1.id, panel_id=p1.id, bot_token_enc="encrypted-secret",
        bot_username="alpha_bot", enabled=True, status="errored",
        last_error="Telegram Unauthorized: token revoked", shop_closed=False,
    )
    second = StorefrontBot(
        reseller_id=r2.id, panel_id=p2.id, bot_token_enc="other-secret",
        bot_username="beta_bot", enabled=True, status="active",
    )
    other = StorefrontBot(
        reseller_id=foreign.id, panel_id=p1.id, bot_token_enc="foreign-secret",
        bot_username="foreign_bot", enabled=True, status="active",
    )
    disabled_shop = StorefrontBot(
        reseller_id=disabled.id, panel_id=p2.id, bot_token_enc="disabled-secret",
        bot_username="disabled_bot", enabled=True, status="active",
    )
    session.add_all([shop, second, other, disabled_shop])
    await session.flush()
    return p1, r1, r2, foreign, shop, second, other, disabled_shop


def _ctx(*resellers: Reseller) -> ResellerContext:
    return ResellerContext(chat_id=111, resellers=list(resellers))


def test_owner_discovery_multi_shop_entitlement_and_redaction():
    async def body(session, _engine):  # noqa: ANN001
        _p1, r1, r2, _foreign, shop, second, _other, _disabled = await _seed(session)
        rows = await list_owned_storefronts(session, _ctx(r1, r2))
        assert [row.shop.id for row in rows] == [shop.id, second.id]

        payloads = [storefront_reporting.storefront_summary(row).model_dump() for row in rows]
        assert payloads[0]["role"] == "owner"
        assert payloads[0]["health_error_class"] == "unauthorized"
        serialized = repr(payloads)
        assert "encrypted-secret" not in serialized
        assert "token revoked" not in serialized
        assert "secret-path" not in serialized

        # A portal identity with no configured shop gets a normal empty list.
        assert await list_owned_storefronts(session, ResellerContext(333, [])) == []

    _run(body)


def test_foreign_and_disabled_entitlement_ids_are_indistinguishable_404():
    async def body(session, _engine):  # noqa: ANN001
        _p1, r1, r2, _foreign, shop, _second, other, disabled = await _seed(session)
        ctx = _ctx(r1, r2)
        assert (await require_owned_storefront(session, ctx, shop.id)).shop.id == shop.id
        for hidden_id in (other.id, disabled.id, 999_999):
            with pytest.raises(HTTPException) as exc_info:
                await require_owned_storefront(session, ctx, hidden_id)
            assert exc_info.value.status_code == 404

    _run(body)


async def _seed_dashboard(session, shop: StorefrontBot):  # noqa: ANN001, ANN202
    c1 = StorefrontCustomer(
        storefront_bot_id=shop.id, telegram_id=1001, name="Active", wallet_balance_toman=900,
        last_seen_at=dt.datetime(2026, 7, 15, tzinfo=UTC),
    )
    c2 = StorefrontCustomer(
        storefront_bot_id=shop.id, telegram_id=1002, name="Old", wallet_balance_toman=100,
        last_seen_at=dt.datetime(2026, 5, 1, tzinfo=UTC),
    )
    c3 = StorefrontCustomer(
        storefront_bot_id=shop.id, telegram_id=1003, name="Failed trial",
        wallet_balance_toman=0,
    )
    session.add_all([c1, c2, c3])
    await session.flush()
    trial = StorefrontOrder(
        customer_id=c1.id, is_trial=True, status="provisioned", gb=1, days=1,
        created_at=dt.datetime(2026, 7, 1, tzinfo=UTC),
    )
    historical_trial = StorefrontOrder(
        customer_id=c2.id, is_trial=True, status="disabled", gb=1, days=1,
        created_at=dt.datetime(2026, 7, 1, tzinfo=UTC),
    )
    failed_trial = StorefrontOrder(
        customer_id=c3.id, is_trial=True, status="failed", gb=1, days=1,
        created_at=dt.datetime(2026, 7, 1, tzinfo=UTC),
    )
    near = StorefrontOrder(
        customer_id=c1.id, is_trial=False, status="provisioned", panel_id=shop.panel_id,
        panel_user_uuid="near-user", gb=10, days=30, price_toman=100,
        created_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
    )
    failed = StorefrontOrder(
        customer_id=c3.id, is_trial=False, status="failed", gb=10, days=2, price_toman=50,
        created_at=dt.datetime(2026, 7, 16, tzinfo=UTC),
    )
    disabled = StorefrontOrder(
        customer_id=c2.id, is_trial=False, status="disabled", gb=10, days=2, price_toman=50,
        created_at=dt.datetime(2026, 7, 16, tzinfo=UTC),
    )
    session.add_all([trial, historical_trial, failed_trial, near, failed, disabled])
    await session.flush()
    session.add(EndUserSnapshot(
        panel_id=shop.panel_id, user_uuid="near-user", added_by_uuid="a1",
        start_date=dt.date(2026, 6, 18), package_days=30,
    ))  # expires 2026-07-18: two Tehran calendar days from the report date

    purchase_op = StorefrontOperation(
        op_id="purchase-op", op_type="purchase", storefront_bot_id=shop.id,
        customer_id=c1.id, order_id=near.id, status="done", price_toman=100,
    )
    renewal_op = StorefrontOperation(
        op_id="renewal-op", op_type="renewal", storefront_bot_id=shop.id,
        customer_id=c2.id, order_id=disabled.id, status="failed", price_toman=200,
    )
    session.add_all([purchase_op, renewal_op])
    await session.flush()
    sale_at = dt.datetime(2026, 7, 16, 8, tzinfo=UTC)
    session.add_all([
        StorefrontWalletTxn(
            customer_id=c1.id, storefront_bot_id=shop.id, kind="purchase", amount_toman=-100,
            status="done", operation_id=purchase_op.id, order_id=near.id, created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c2.id, storefront_bot_id=shop.id, kind="purchase", amount_toman=-200,
            status="done", operation_id=renewal_op.id, order_id=disabled.id, created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c3.id, storefront_bot_id=shop.id, kind="purchase", amount_toman=-50,
            status="done", order_id=failed.id, created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c1.id, storefront_bot_id=shop.id, kind="refund", amount_toman=40,
            status="done", order_id=None, created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c1.id, storefront_bot_id=shop.id, kind="renew_reversal", amount_toman=30,
            status="done", operation_id=renewal_op.id, order_id=near.id, created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c1.id, storefront_bot_id=shop.id, kind="topup", amount_toman=500,
            status="pending", created_at=sale_at,
        ),
        StorefrontWalletTxn(
            customer_id=c1.id, storefront_bot_id=shop.id, kind="manual_credit", amount_toman=999,
            status="done", created_at=sale_at,
        ),
    ])
    code = StorefrontCreditCode(
        storefront_bot_id=shop.id, code="GIFT", code_ci="GIFT", kind="fixed",
        amount_toman=25, is_gift=True, used_count=1,
    )
    session.add(code)
    await session.flush()
    session.add(StorefrontCreditRedemption(
        code_id=code.id, customer_id=c1.id, bonus_toman=25,
        created_at=dt.datetime(2026, 7, 1, tzinfo=UTC),   # pin in-window; else defaults to real now → flaky
    ))
    session.add(StorefrontCreditRedemption(
        code_id=code.id, customer_id=c1.id, bonus_toman=999,
        created_at=dt.datetime(2026, 6, 30, 8, tzinfo=UTC),
    ))
    await session.commit()


def test_dashboard_accounting_conversion_health_and_query_budget():
    async def body(session, engine):  # noqa: ANN001
        _p1, r1, r2, _foreign, shop, _second, _other, _disabled = await _seed(session)
        await _seed_dashboard(session, shop)
        statements: list[str] = []

        def record(_conn, _cursor, statement, _params, _context, _many):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            access = await require_owned_storefront(session, _ctx(r1, r2), shop.id)
            result = await storefront_reporting.dashboard(
                session, access, dt.date(2026, 7, 1), dt.date(2026, 7, 16),
                now=dt.datetime(2026, 7, 16, 12, tzinfo=UTC),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

        assert len(statements) <= 12
        assert not any(stmt.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                       for stmt in statements)
        sales = result.sales_range
        assert (sales.gross_sales_toman, sales.reversals_toman, sales.net_sales_toman) == (
            350, 70, 280)
        assert (sales.purchase.count, sales.purchase.amount_toman) == (1, 100)
        assert (sales.renewal.count, sales.renewal.amount_toman) == (1, 200)
        assert (sales.unknown.count, sales.unknown.amount_toman) == (1, 50)
        assert result.customers.model_dump() == {
            "total": 3, "active_30d": 1, "wallet_liability_toman": 1000}
        assert result.service_states["provisioned"] == 2
        assert result.service_states["disabled"] == 2
        assert result.service_states["failed"] == 2
        assert result.service_states["renewing"] == 0
        assert result.near_expiry == 1
        assert result.pending_topups.model_dump() == {"count": 1, "amount_toman": 500}
        assert result.credits.model_dump() == {"redemptions": 1, "bonus_toman": 25}
        assert result.operation_states["done"] == 1
        assert result.operation_states["failed"] == 1
        # Failed trial is not a denominator; a later renewal is not a first paid purchase.
        assert result.trial_conversion.trial_customers == 2
        assert result.trial_conversion.converted_customers == 1
        assert result.trial_conversion.rate == 0.5

        health = await storefront_reporting.health(session, access)
        dumped = health.model_dump()
        assert dumped["bot"]["error_class"] == "unauthorized"
        assert "last_error" not in repr(dumped)
        assert "token revoked" not in repr(dumped)

    _run(body)


@pytest.mark.parametrize(
    ("day_from", "day_to"),
    [
        (dt.date(2026, 1, 1), None),
        (dt.date(2026, 7, 2), dt.date(2026, 7, 1)),
        (dt.date(2025, 1, 1), dt.date(2026, 1, 2)),
        (dt.date(9999, 12, 1), dt.date.max),
    ],
)
def test_dashboard_rejects_partial_reversed_and_oversized_ranges(day_from, day_to):  # noqa: ANN001
    with pytest.raises(HTTPException) as exc_info:
        portal_storefront._dashboard_dates(day_from, day_to)
    assert exc_info.value.status_code == 422


def test_release_a_storefront_routes_remain_registered_as_get_only():
    storefront_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/portal/storefronts")
    }
    assert ("/api/portal/storefronts", frozenset({"GET"})) in storefront_routes
    assert ("/api/portal/storefronts/{shop_id}/dashboard", frozenset({"GET"})) in storefront_routes
    assert ("/api/portal/storefronts/{shop_id}/health", frozenset({"GET"})) in storefront_routes
    release_a_paths = {
        "/api/portal/storefronts",
        "/api/portal/storefronts/{shop_id}/dashboard",
        "/api/portal/storefronts/{shop_id}/health",
    }
    assert all(methods == {"GET"} for path, methods in storefront_routes if path in release_a_paths)


def test_storefront_http_auth_and_owner_scoping():
    async def body(session, _engine):  # noqa: ANN001
        _p1, r1, r2, _foreign, shop, _second, other, _disabled = await _seed(session)
        await _seed_dashboard(session, shop)

        async def session_override():
            yield session

        async def context_override():
            return _ctx(r1, r2)

        app.dependency_overrides[get_session] = session_override
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                unauthenticated = await client.get("/api/portal/storefronts")
                assert unauthenticated.status_code == 401

                app.dependency_overrides[get_current_reseller] = context_override
                dashboard = await client.get(
                    f"/api/portal/storefronts/{shop.id}/dashboard",
                    params={"from": "2026-07-01", "to": "2026-07-16"},
                )
                assert dashboard.status_code == 200
                assert dashboard.json()["storefront_id"] == shop.id
                health = await client.get(f"/api/portal/storefronts/{shop.id}/health")
                assert health.status_code == 200
                assert health.json()["bot"]["error_class"] == "unauthorized"

                for suffix in ("dashboard", "health"):
                    foreign = await client.get(
                        f"/api/portal/storefronts/{other.id}/{suffix}",
                        params={"from": "2026-07-01", "to": "2026-07-16"}
                        if suffix == "dashboard" else None,
                    )
                    assert foreign.status_code == 404
        finally:
            app.dependency_overrides.pop(get_current_reseller, None)
            app.dependency_overrides.pop(get_session, None)

    _run(body)
