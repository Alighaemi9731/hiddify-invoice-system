"""Shared test fixtures.

Payment methods default to ENABLED + CONFIGURED for every test, mirroring a live install
(`seed_defaults` turns USDT/screenshot on and the owner configures destinations). The ad-hoc
per-test SQLite databases never run `seed_defaults`, so without this the F7 policy check in
`submit_reseller_payment` (added in the security remediation) would reject every payment and
break the whole payment suite.

Tests that specifically exercise the policy (a disabled or unconfigured method must be rejected)
mark themselves `@pytest.mark.real_pay_options`; the autouse patch then steps aside so the real
`payment_methods.load_options` reads whatever settings that test seeds.
"""
from __future__ import annotations

import pytest

from app.services import payment_methods


def _all_methods_on() -> payment_methods.PaymentOptions:
    return payment_methods.PaymentOptions(
        usdt=True, screenshot=True, card=True, ton=True, avax=True,
        wallet="0xwallet", card_number="6037000000000000", card_holder="Owner",
        ton_address="UQtonaddress", avax_address="0xavaxaddress",
    )


@pytest.fixture(autouse=True)
def _enable_payment_methods(request, monkeypatch):
    """Make every payment method available by default (see module docstring)."""
    if request.node.get_closest_marker("real_pay_options"):
        return

    async def fake_load_options(_session):  # noqa: ANN001
        return _all_methods_on()

    monkeypatch.setattr(payment_methods, "load_options", fake_load_options)
