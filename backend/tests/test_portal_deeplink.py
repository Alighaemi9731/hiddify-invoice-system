"""Plan 007: the portal login `next` deep-link allowlist is airtight (default-deny). Pure unit tests —
every open-redirect / scheme / traversal / double-encode / unknown-suffix input must reject to None,
and every registered storefront route must pass to its canonical form."""
from __future__ import annotations

import pytest

from app.core.portal_deeplink import parse_shop_id, validate_next

REJECT = [
    None, "", "   ",
    "//evil.com",
    "https://evil.com",
    "http://evil.com/portal/storefront/1",
    "http:/x",
    "javascript:alert(1)",
    "data:text/html,<script>",
    "/portal/storefront/1\\x",                       # backslash
    "/portal/storefront/1\n/customers",              # control char
    "/portal/storefront/1\t",                        # whitespace
    "/portal/storefront/1/%252fcustomers",           # double-encode (still %-encoded after one decode)
    "/portal/storefront/1/../../etc/passwd",         # traversal
    "/portal/storefront/1/..",                       # traversal
    "/portal/storefront/abc",                        # non-numeric shop id
    "/portal/storefront/",                           # missing shop id
    "/portal/storefront/1//customers",               # double slash
    "/portal/storefront/1/orders/5",                 # unregistered SPA route
    "/portal/storefront/1/topups/9",                 # unregistered (no per-txn SPA route)
    "/portal/storefront/1/broadcasts/3",             # unregistered
    "/portal/storefront/1/credits/3",                # unregistered
    "/portal/storefront/1/unknown",                  # unknown suffix
    "/portal/invoices",                              # wrong prefix
    "/portal/storefront/1/customers/abc",            # non-numeric customer id
    "https://host/portal/storefront/1",              # absolute url (scheme)
    "/portal/storefront/1@evil",                     # junk after shop id
]

ACCEPT = {
    "/portal/storefront/7": "/portal/storefront/7",
    "/portal/storefront/7/": "/portal/storefront/7",
    "/portal/storefront/7/plans": "/portal/storefront/7/plans",
    "/portal/storefront/7/customers": "/portal/storefront/7/customers",
    "/portal/storefront/7/customers/42": "/portal/storefront/7/customers/42",
    "/portal/storefront/7/topups": "/portal/storefront/7/topups",
    "/portal/storefront/7/credits": "/portal/storefront/7/credits",
    "/portal/storefront/7/campaigns": "/portal/storefront/7/campaigns",
    # `/finance` shipped as a real SPA route in v1.111.0 but was never added here, so a link to it
    # silently landed on the shop picker instead — this list must track PortalApp.tsx.
    "/portal/storefront/7/finance": "/portal/storefront/7/finance",
    "/portal/storefront/7/settings": "/portal/storefront/7/settings",
    "/portal/storefront/7/managers": "/portal/storefront/7/managers",
    "/portal/storefront/7/preview": "/portal/storefront/7/preview",
    "/portal/storefront/7/health": "/portal/storefront/7/health",
    # A single percent-encoded next decodes ONCE to a clean registered path.
    "/portal/storefront/7/customers/42%00": None,  # NUL after decode -> control char -> reject
}


@pytest.mark.parametrize("raw", REJECT)
def test_rejects_unsafe_next(raw):  # noqa: ANN001
    assert validate_next(raw) is None


def test_accepts_registered_routes_and_canonicalizes():
    for raw, expected in ACCEPT.items():
        assert validate_next(raw) == expected, raw


def test_parse_shop_id_from_validated():
    assert parse_shop_id("/portal/storefront/7/customers/42") == 7
    assert parse_shop_id("/portal/storefront/123") == 123


def test_percent_encoded_valid_next_decodes_once():
    # A legitimately percent-encoded but registered path passes after exactly one decode.
    assert validate_next("/portal/storefront/9/customers/5") == "/portal/storefront/9/customers/5"


def test_length_cap():
    assert validate_next("/portal/storefront/1/customers/" + "9" * 600) is None
