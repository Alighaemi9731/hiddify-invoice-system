"""Cross-invoice/month regressions and preview confirmation; never contacts Telegram."""
import datetime as dt

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.operations import BroadcastBody, broadcast
from app.models import EnforcementAction, Invoice, Payment, PaymentSettlement, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PaymentStatus,
)
from app.schemas.broadcast import BroadcastFilters
from app.services import broadcast as bc
from app.services import periods
from tests.test_broadcast_filters import _run, _seed


def _invoice(root, month, status=InvoiceStatus.sent, amount=90000, age=52, deferred=None):
    period = periods.parse_period(month)
    return Invoice(reseller_id=root.id, panel_id=root.panel_id, period_start=period.start,
                   period_end=period.end, period_label=month, status=status, amount_toman=amount,
                   sent_at=dt.datetime.combine(periods.today() - dt.timedelta(days=age), dt.time(12),
                                               tzinfo=dt.timezone.utc), deferred_until=deferred)


async def _preview(s, audience="debtors", **filters):
    return await bc.preview(s, audience=audience, filters=BroadcastFilters(**filters))


def _ids(result):
    return {a["reseller_id"] for r in result["recipients"] + result["skipped"] for a in r["accounts"]}


def test_month_filters_do_not_mix_invoices(tmp_path):
    async def body(s):
        a, b, *_ = await _seed(s)
        # A owes June but already paid July. B owes July; paid/canceled/drafts never create debt.
        s.add_all([_invoice(a, "2026-06"), _invoice(a, "2026-07", InvoiceStatus.paid),
                   _invoice(b, "2026-07"), _invoice(a, "2026-08", InvoiceStatus.draft)])
        await s.commit()
        result = await _preview(s, period_from="2026-07", period_to="2026-07")
        assert _ids(result) == {b.id}
        assert result["debt_toman"] == 90000
        assert result["recipients"][0]["periods"] == ["2026-07"]
        assert not _ids(await _preview(s, period_from="2026-08", period_to="2026-08"))
        assert not _ids(await _preview(s, "paid_up", period_from="2026-07", period_to="2026-07"))
    _run(body, tmp_path)


def test_overdue_age_and_deferral_apply_to_same_invoice(tmp_path):
    async def body(s):
        a, *_ = await _seed(s)
        s.add_all([_invoice(a, "2026-07", InvoiceStatus.overdue, age=5),
                   _invoice(a, "2026-08", InvoiceStatus.sent, age=80)])
        await s.commit()
        assert a.id not in _ids(await _preview(s, "overdue", min_age_days=50))
        assert a.id in _ids(await _preview(s, "debtors", min_age_days=50))
        overdue = (await s.execute(select(Invoice).where(Invoice.reseller_id == a.id,
                                                       Invoice.period_label == "2026-07"))).scalar_one()
        overdue.deferred_until = periods.today() + dt.timedelta(days=4)
        await s.commit()
        assert a.id not in _ids(await _preview(s, "overdue"))
        assert a.id in _ids(await _preview(s, "deferred"))
        assert a.id not in _ids(await _preview(s, "deferred", min_age_days=1))
    _run(body, tmp_path)


def test_enforced_month_is_linked_to_actual_non_reverted_action(tmp_path):
    async def body(s):
        a, b, *_ = await _seed(s)
        a.enforcement_state = b.enforcement_state = EnforcementState.enforced
        july = _invoice(a, "2026-07", InvoiceStatus.enforced)
        s.add_all([july, _invoice(b, "2026-07", InvoiceStatus.paid)])
        await s.flush()
        action = EnforcementAction(reseller_id=a.id, invoice_id=july.id,
                                   action=EnforcementActionType.disable_users,
                                   status=EnforcementActionStatus.done, dry_run=False)
        # B was blocked manually. It must not sneak in via an unrelated invoice for July.
        s.add_all([action, EnforcementAction(reseller_id=b.id, action=EnforcementActionType.disable_users,
                                            status=EnforcementActionStatus.done, dry_run=False)])
        await s.commit()
        assert _ids(await _preview(s, "enforced", period_from="2026-07", period_to="2026-07")) == {a.id}
        action.status = EnforcementActionStatus.reverted
        await s.commit()
        assert not _ids(await _preview(s, "enforced", period_from="2026-07", period_to="2026-07"))
        assert _ids(await _preview(s, "enforced")) == {a.id, b.id}
    _run(body, tmp_path)


def test_multi_panel_and_shared_chat_preserve_all_matched_accounts(tmp_path):
    from app.models import Panel
    from app.models.enums import PanelStatus
    async def body(s):
        a, b, *_ = await _seed(s)
        p = Panel(key="second", host="second.invalid", proxy_path_enc="x", owner_uuid="owner2",
                  status=PanelStatus.ok)
        s.add(p)
        await s.flush()
        x = Reseller(panel_id=p.id, admin_uuid="x", name="Other account", bot_chat_id=a.bot_chat_id)
        s.add(x)
        await s.flush()
        s.add_all([_invoice(a, "2026-07", amount=100), _invoice(x, "2026-07", amount=200)])
        await s.commit()
        result = await _preview(s, panel_ids=[a.panel_id, p.id], period_from="2026-07", period_to="2026-07")
        assert result["total"] == 1 and result["matched_accounts"] == 2
        assert result["duplicate_chats"] == 1 and result["debt_toman"] == 300
        assert _ids(result) == {a.id, x.id}
        assert _ids(await _preview(s, panel_ids=[p.id])) == {x.id}
        assert _ids(await _preview(s, panel_ids=[p.id], excluded_reseller_ids=[x.id])) == set()
        assert _ids(await _preview(s, search="Other account")) == {x.id}
        assert b.id not in _ids(result)
    _run(body, tmp_path)


def test_pending_receipts_multi_invoice_coverage_and_exclusion(tmp_path):
    async def body(s):
        a, b, *_ = await _seed(s)
        july = _invoice(a, "2026-07")
        august = _invoice(a, "2026-08")
        s.add_all([july, august])
        await s.flush()
        payment = Payment(reseller_id=a.id, invoice_id=july.id, status=PaymentStatus.pending)
        s.add_all([payment, Payment(reseller_id=b.id, status=PaymentStatus.pending)])
        await s.flush()
        s.add(PaymentSettlement(payment_id=payment.id, invoice_id=august.id))
        await s.commit()
        result = await _preview(s, "pending_payment", period_from="2026-08", period_to="2026-08")
        assert _ids(result) == {a.id}
        assert not _ids(await _preview(s, period_from="2026-08", period_to="2026-08", exclude_pending=True))
        assert b.id not in _ids(await _preview(s, exclude_pending=True))
    _run(body, tmp_path)


def test_debt_bounds_sum_only_selected_periods_and_age_boundaries(tmp_path):
    async def body(s):
        a, *_ = await _seed(s)
        s.add_all([_invoice(a, "2026-07", amount=100, age=50), _invoice(a, "2026-08", amount=200, age=55)])
        await s.commit()
        assert _ids(await _preview(s, period_from="2026-07", period_to="2026-08", min_debt=300, max_debt=300,
                                  min_age_days=50, max_age_days=50)) == {a.id}
        assert not _ids(await _preview(s, period_from="2026-07", period_to="2026-07", min_debt=200))
        assert not _ids(await _preview(s, period_from="2026-07", min_age_days=56))
        assert not _ids(await _preview(s, access_state="enforced", min_debt=1))
    _run(body, tmp_path)


def test_preview_token_rejects_changed_debt_and_recipient_without_sending(tmp_path):
    async def body(s):
        _a, b, *_ = await _seed(s)
        first = await _preview(s)
        tasks = BackgroundTasks()
        inv = (await s.execute(select(Invoice).where(Invoice.reseller_id == b.id))).scalar_one()
        inv.status = InvoiceStatus.paid
        await s.commit()
        with pytest.raises(HTTPException) as exc:
            await broadcast(BroadcastBody(text="Never delivered", audience="debtors",
                                          preview_token=first["preview_token"]), tasks, s)
        assert exc.value.status_code == 409 and not tasks.tasks
    _run(body, tmp_path)


@pytest.mark.parametrize("values", [
    {"period_from": "2026-13"}, {"period_from": "2026-07", "period_to": "2026-06"},
    {"min_debt": -1}, {"max_debt": float('inf')}, {"min_debt": 9, "max_debt": 8},
    {"min_age_days": 56, "max_age_days": 55}, {"panel_ids": [0]}, {"access_state": "typo"},
    {"period_typo": "2026-07"},
])
def test_invalid_filters_never_widen_audience(values):
    with pytest.raises(ValidationError):
        BroadcastFilters(**values)


def test_pending_exclusion_covers_same_person_other_panel_account(tmp_path):
    async def body(s):
        a, b, *_ = await _seed(s)
        b.bot_chat_id = a.bot_chat_id
        s.add_all([_invoice(a, "2026-07"), Payment(reseller_id=b.id, status=PaymentStatus.pending)])
        await s.commit()
        assert a.id in _ids(await _preview(s, period_from="2026-07", period_to="2026-07"))
        assert not _ids(await _preview(s, period_from="2026-07", period_to="2026-07", exclude_pending=True))
    _run(body, tmp_path)


def test_disabled_panel_excluded_and_nan_threshold_fails_closed(tmp_path):
    from app.models import Panel
    async def body(s):
        a, *_ = await _seed(s)
        panel = await s.get(Panel, a.panel_id)
        panel.enabled = False
        await s.commit()
        assert not _ids(await _preview(s, "all"))
        assert await bc.resolve_recipients(s, "new_resellers", None, float('inf')) == ([], [])
    _run(body, tmp_path)
