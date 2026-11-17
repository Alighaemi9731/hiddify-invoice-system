"""No Telegram route may exempt debt, reset its clock, or bypass another restriction."""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    DeliveryLog,
    EnforcementAction,
    FinancialRecord,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import (
    DeliveryKind,
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PanelStatus,
)
from app.services import (
    billing_recovery,
    delivery,
    dunning,
    enforcement,
    issuance,
    periods,
    registration_policy,
    settings_service,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 17, 12, tzinfo=UTC)


def run_db(tmp_path, body):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False, autoflush=False)() as s:
                panel = Panel(id=1, key="p", host="test.invalid", proxy_path_enc="x", owner_uuid="owner",
                              status=PanelStatus.ok, last_synced_at=NOW)
                r = Reseller(panel_id=1, admin_uuid="r", name="R", panel_max_users=10,
                             panel_max_active_users=5, last_seen_at=NOW)
                s.add_all([panel, r])
                await s.commit()
                await body(s, r)
        finally:
            await engine.dispose()
    asyncio.run(run())


def invoice(r, month=1, **kwargs):
    return Invoice(reseller_id=r.id, panel_id=r.panel_id, period_label=f"2026-{month:02}",
                   period_start=dt.date(2026, month, 1), period_end=periods.month_period(2026, month).end,
                   usage_gb=10, amount_toman=10000, amount_usdt=1, **kwargs)


def test_unmatched_invoice_is_issued_and_dunned_without_sent_at(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "build_bot", AsyncMock(return_value=None))
    async def body(s, r):
        inv = invoice(r)
        s.add(inv)
        await s.commit()
        dl = await delivery.send_invoice(s, inv.id)
        assert dl.status == DeliveryStatus.unmatched
        assert inv.status == InvoiceStatus.sent
        assert inv.issued_at is not None and inv.sent_at is None
        assert inv.collection_started_at == inv.issued_at
        assert inv.telegram_delivery_status == "unmatched"
        assert await s.scalar(select(FinancialRecord.id).where(FinancialRecord.invoice_id == inv.id))
        res = await dunning.run_dunning(s, now=inv.issued_at + dt.timedelta(days=31))
        assert res["enforcement_queued"] == 1
        assert inv.status == InvoiceStatus.overdue
        action = await s.scalar(select(EnforcementAction).where(EnforcementAction.invoice_id == inv.id))
        assert action and not action.dry_run
    run_db(tmp_path, body)


def test_delivery_failure_then_success_and_resend_never_reage_debt(tmp_path, monkeypatch):
    async def body(s, r):
        r.bot_chat_id = 12
        inv = invoice(r)
        s.add(inv)
        await s.commit()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        failed = await delivery.send_invoice(s, inv.id, bot=bot)
        assert failed.status == DeliveryStatus.failed and inv.sent_at is None
        first_issue = inv.issued_at
        assert first_issue is not None
        monkeypatch.setattr(delivery, "send_invoice_content", AsyncMock(return_value=[123]))
        bot.delete_message = AsyncMock()
        await delivery.send_invoice(s, inv.id, bot=bot)
        first_delivery = inv.sent_at
        await delivery.send_invoice(s, inv.id, bot=bot)
        assert inv.issued_at.replace(tzinfo=UTC) == first_issue.replace(tzinfo=UTC)
        assert inv.collection_started_at.replace(tzinfo=UTC) == first_issue.replace(tzinfo=UTC)
        assert inv.sent_at.replace(tzinfo=UTC) == first_delivery.replace(tzinfo=UTC)
        assert inv.telegram_delivery_status == "sent"
    run_db(tmp_path, body)


def test_recovery_preserves_late_success_and_only_issues_failed_drafts_once(tmp_path):
    async def body(s, r):
        invs = [invoice(r, month=m) for m in range(1, 6)]
        late = invoice(r, month=6, status=InvoiceStatus.sent, sent_at=NOW)
        s.add_all([*invs, late])
        await s.flush()
        for inv, status in zip(invs[:4], [DeliveryStatus.unmatched, DeliveryStatus.blocked,
                                         DeliveryStatus.failed, DeliveryStatus.sent], strict=True):
            s.add(DeliveryLog(invoice_id=inv.id, reseller_id=r.id, kind=DeliveryKind.invoice, status=status))
        await s.commit()
        report = await billing_recovery.recover(s, now=NOW)
        assert set(report["recovered_ids"]) == {i.id for i in invs[:3]}
        assert {i["id"] for i in report["review"]} == {i.id for i in invs[3:]}
        for inv in invs[:3]:
            await s.refresh(inv)
            assert inv.status == InvoiceStatus.sent and inv.sent_at is None
            assert issuance.collection_anchor(inv) == NOW.date()
            assert inv.amount_toman == 10000
            assert issuance.dates(inv, {"warning_day": 10, "enforcement_day": 30})["enforcement_on"] == dt.date(2026, 12, 17)
        await s.refresh(late)
        assert issuance.collection_anchor(late) == NOW.date()
        assert late.sent_at.replace(tzinfo=UTC) == NOW
        assert r.registration_deadline_at == NOW + dt.timedelta(days=30)
        again = await billing_recovery.recover(s, now=NOW + dt.timedelta(days=60))
        assert again == report
        assert issuance.collection_anchor(invs[0]) == NOW.date()
    run_db(tmp_path, body)


def test_recovery_does_not_issue_exempt_or_owner_drafts(tmp_path):
    async def body(s, r):
        r.exclude_from_billing = True
        inv = invoice(r)
        s.add(inv)
        await s.flush()
        s.add(DeliveryLog(invoice_id=inv.id, reseller_id=r.id, kind=DeliveryKind.invoice,
                          status=DeliveryStatus.unmatched))
        await s.commit()
        report = await billing_recovery.recover(s, now=NOW)
        assert report["recovered_ids"] == [] and len(report["review"]) == 1
        assert inv.status == InvoiceStatus.draft and r.registration_deadline_at is None
    run_db(tmp_path, body)


def test_interrupted_recovery_gives_not_yet_processed_drafts_a_full_fresh_cycle(tmp_path):
    async def body(s, r):
        inv = invoice(r)
        s.add(inv)
        await s.flush()
        s.add(DeliveryLog(invoice_id=inv.id, reseller_id=r.id, kind=DeliveryKind.invoice,
                          status=DeliveryStatus.unmatched))
        await s.commit()
        await settings_service.set_value(s, billing_recovery.REPORT_KEY, {
            "started_at": (NOW - dt.timedelta(days=60)).isoformat(), "recovered_ids": [], "review": [],
        })
        await billing_recovery.recover(s, now=NOW)
        await s.refresh(inv)
        assert issuance.collection_anchor(inv) == NOW.date()
        assert r.registration_deadline_at == NOW + dt.timedelta(days=30)
    run_db(tmp_path, body)


def test_existing_roots_get_full_cycle_new_roots_get_72h_not_subs(tmp_path, monkeypatch):
    monkeypatch.setattr(enforcement, "reassert_enforced", AsyncMock(return_value={}))
    async def body(s, r):
        await billing_recovery.recover(s, now=NOW)
        child = Reseller(panel_id=1, admin_uuid="sub", parent_admin_uuid="r", last_seen_at=NOW)
        new = Reseller(panel_id=1, admin_uuid="new", last_seen_at=NOW)
        s.add_all([child, new])
        await s.commit()
        await registration_policy.run(s, now=NOW)
        assert r.registration_deadline_at == NOW + dt.timedelta(days=30)
        assert new.registration_deadline_at == NOW + dt.timedelta(hours=72)
        assert child.registration_deadline_at is None
        await registration_policy.run(s, now=NOW + dt.timedelta(hours=73))
        assert new.registration_blocked and not r.registration_blocked
        actions = list((await s.scalars(select(EnforcementAction))).all())
        assert len(actions) == 1 and actions[0].snapshot["restriction_reason"] == "registration"
        await registration_policy.run(s, now=NOW + dt.timedelta(hours=74))
        assert len(list((await s.scalars(select(EnforcementAction))).all())) == 1
    run_db(tmp_path, body)


def test_registration_suspension_uses_existing_worker_and_restores_exact_limits(tmp_path, monkeypatch):
    writes = []
    async def limits(_self, _panel, _uuid, mu, mau, api_key=None):
        writes.append((mu, mau))
    monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", AsyncMock(return_value=(10, 5)))
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_password", AsyncMock())
    async def body(s, r):
        await settings_service.set_value(s, "enforcement_admin_lockout_enabled", False)
        r.registration_deadline_at = dt.datetime.now(UTC) - dt.timedelta(hours=1)
        r.registration_blocked = True
        inv = invoice(r, status=InvoiceStatus.sent, issued_at=dt.datetime.now(UTC),
                      collection_started_at=dt.datetime.now(UTC))
        s.add(inv)
        await s.commit()
        action = await enforcement.queue_enforcement(s, r, reason="registration", dry_run=False)
        result = await enforcement.process_enforcement_queue(s)
        assert result["done"] == 1
        await s.refresh(r)
        await s.refresh(inv)
        assert r.enforcement_state == EnforcementState.enforced
        assert inv.status == InvoiceStatus.sent  # no false overdue debt from registration
        assert (0, 0) in writes
        assert await enforcement.queue_restore(s, r, reason="payment") is None
        r.bot_chat_id = 123
        await s.commit()
        await registration_policy.completed(s, r)
        # Daily debt reconciliation may overlap the queued restore. It must not turn
        # this registration-only suspension into a new financial reason that blocks restore.
        assert await enforcement._stamp_invoices_enforced(s, r) == 0
        result = await enforcement.process_enforcement_queue(s)
        assert result["done"] == 1
        await s.refresh(r)
        await s.refresh(action)
        assert r.enforcement_state == EnforcementState.active
        assert action.status == EnforcementActionStatus.reverted
        assert (10, 5) in writes
    run_db(tmp_path, body)


def test_registration_completion_keeps_financial_or_manual_suspension(tmp_path):
    async def body(s, r):
        r.enforcement_state = EnforcementState.enforced
        r.registration_blocked = True
        r.registration_deadline_at = NOW - dt.timedelta(days=1)
        inv = invoice(r, status=InvoiceStatus.enforced, issued_at=NOW - dt.timedelta(days=90))
        s.add_all([inv, EnforcementAction(reseller_id=r.id, action=EnforcementActionType.disable_users,
                    status=EnforcementActionStatus.done, dry_run=False,
                    snapshot={"restriction_reason": "registration", "limits": {"r": {"max_users": 10}}})])
        await s.commit()
        r.bot_chat_id = 123
        await registration_policy.completed(s, r)
        assert r.enforcement_state == EnforcementState.enforced
        assert not r.registration_blocked
        assert not list((await s.scalars(select(EnforcementAction).where(
            EnforcementAction.action == EnforcementActionType.restore))).all())
    run_db(tmp_path, body)


def test_pending_restore_cannot_bypass_registration_deadline(tmp_path):
    async def body(s, r):
        r.enforcement_state = EnforcementState.enforced
        r.registration_deadline_at = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
        action = EnforcementAction(reseller_id=r.id, action=EnforcementActionType.restore,
                                   dry_run=False, status=EnforcementActionStatus.planned, snapshot={})
        s.add(action)
        await s.commit()
        result = await enforcement._process_restore_action(s, action, user_chunk_size=10, admin_parallelism=1)
        assert result["failed"] == 1 and r.enforcement_state == EnforcementState.enforced
    run_db(tmp_path, body)


def test_recovery_midnight_uses_tehran_not_invoice_month():
    inv = Invoice(status=InvoiceStatus.sent, issued_at=dt.datetime(2026, 11, 16, 22, tzinfo=UTC))
    assert issuance.collection_anchor(inv) == dt.date(2026, 11, 17)


def test_unmark_paid_never_returns_an_issued_undelivered_invoice_to_draft(tmp_path):
    from app.api import invoices as invoice_api

    async def body(s, r):
        inv = invoice(r)
        s.add(inv)
        await s.commit()
        await issuance.issue_invoice(s, inv.id)
        await invoice_api.mark_paid(inv.id, session=s)
        assert inv.status == InvoiceStatus.paid and inv.sent_at is None
        await invoice_api.unmark_paid(inv.id, session=s)
        assert inv.status == InvoiceStatus.sent and inv.sent_at is None
        assert inv.collection_started_at is not None
        assert await s.scalar(select(FinancialRecord.id).where(FinancialRecord.invoice_id == inv.id))
    run_db(tmp_path, body)


@pytest.mark.parametrize("day", [0, 7, 30])
def test_configured_schedule_zero_and_custom_days_are_respected(tmp_path, day):
    async def body(s, _r):
        await settings_service.set_value(s, "warning_day", day)
        await settings_service.set_value(s, "enforcement_day", day)
        cfg = await issuance.schedule_days(s)
        inv = Invoice(status=InvoiceStatus.sent, issued_at=NOW)
        assert issuance.dates(inv, cfg)["enforcement_on"] == NOW.date() + dt.timedelta(days=day)
    run_db(tmp_path, body)
