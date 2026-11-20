"""The compact invoice page keeps its original states without resetting financial facts."""
import datetime as dt

import pytest

from app.models import EnforcementAction, Invoice
from app.models.enums import EnforcementActionType, EnforcementState, InvoiceStatus
from tests.test_invoice_ordering import _client, data  # noqa: F401, F811


@pytest.mark.parametrize('raw,delivered,blocked,registration,cause,expected', [
    ('draft', False, False, False, None, 'draft'),
    ('sent', False, False, False, None, 'draft'),
    ('overdue', False, False, False, None, 'draft'),
    ('sent', True, False, False, None, 'sent'),
    ('overdue', True, False, False, None, 'overdue'),
    ('enforced', True, True, False, 'manual', 'enforced'),
    ('enforced', True, True, False, 'financial', 'financial_blocked'),
    ('enforced', False, True, True, 'financial', 'financial_blocked'),
    ('sent', False, True, True, 'registration', 'registration_blocked'),
    ('paid', True, True, True, 'registration', 'paid'),
    ('canceled', False, True, True, 'registration', 'canceled'),
])
async def test_rows_detail_filters_and_totals_share_one_status(
    data, raw, delivered, blocked, registration, cause, expected,  # noqa: F811
):
    inv = data.invs[0]
    inv.status = InvoiceStatus(raw)
    anchor = dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)
    inv.collection_started_at = anchor
    inv.issued_at = anchor
    inv.sent_at = anchor if delivered else None
    inv.telegram_delivery_status = 'failed'  # a failed resend never undoes successful delivery
    data.a.enforcement_state = EnforcementState.enforced if blocked else EnforcementState.active
    data.a.registration_blocked = registration
    if cause:
        data.session.add(EnforcementAction(
            reseller_id=data.a.id, invoice_id=inv.id if cause == 'financial' else None,
            action=EnforcementActionType.disable_users, dry_run=False,
            snapshot={'restriction_reason': 'registration' if cause == 'registration' else 'debt'},
        ))
        # A later users-only reassert must not replace the original suspension reason.
        data.session.add(EnforcementAction(
            reseller_id=data.a.id, action=EnforcementActionType.disable_users, dry_run=False,
            snapshot={'restriction_reason': 'debt', 'reassert': True},
        ))
    await data.session.commit()
    params = {'reseller_id': data.a.id, 'period': inv.period_label, 'display_status': expected}
    async with _client(data) as client:
        result = await client.get('/api/invoices', params=params)
        assert result.status_code == 200, result.text
        assert result.headers['x-total-count'] == '1'
        assert result.headers['x-total-amount-toman'] == str(int(inv.amount_toman))
        row = result.json()[0]
        assert row['status'] == raw
        assert row['display_status'] == expected
        detail = await client.get(f'/api/invoices/{inv.id}')
        assert detail.json()['display_status'] == expected
        assert detail.json()['collection_started_at'] == row['collection_started_at']
        excluded = await client.get('/api/invoices', params={**params, 'display_status': 'paid' if expected != 'paid' else 'draft'})
        assert excluded.json() == []
        assert excluded.headers['x-total-count'] == '0'
        if raw in ('sent', 'overdue') and not delivered:
            await client.post('/api/invoices/discard-drafts', params={'period': inv.period_label})
            assert await data.session.get(Invoice, inv.id) is not None
            assert inv.collection_started_at == anchor
