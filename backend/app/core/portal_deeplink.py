"""Airtight allowlist for the portal login `next` deep-link parameter (plan 007).

A storefront-owner Telegram notification can carry a one-time login URL with a `next` that lands the
owner directly on the relevant portal page (a top-up queue, a customer, a campaign). `next` is
attacker-influenceable (it rides in a URL), so it is validated **default-deny**: only an exact
`/portal/storefront/{numeric_shop_id}` prefix followed by a REGISTERED SPA route suffix passes; every
scheme, `//`, backslash, control char, `..`, whitespace, or double-encoding is rejected. Passing
validation is necessary but NOT sufficient — the server still authorizes the shop_id against the
caller's owned storefronts (`/api/portal/authorize-next`) before the SPA navigates, so a valid-but-
foreign path degrades to the owner's dashboard with no existence leak.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

_PREFIX = "/portal/storefront/"
_MAX_LEN = 512
_PERCENT_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}")
# The path AFTER `/portal/storefront/{shop_id}` — must be one of the SPA's registered storefront
# routes. Deliberately NO `/topups/{id}`, `/orders/{id}`, `/broadcasts/{id}`, `/credits/{id}`: the
# SPA has no such route, so allowing them would be a dead (and unvalidated) landing.
_SUFFIX = re.compile(
    r"(?:/|/plans|/customers|/customers/[0-9]+|/topups|/credits|/campaigns"
    r"|/settings|/managers|/preview|/health)?"
)
_SHOP_SEGMENT = re.compile(r"[0-9]+")


def _has_control_char(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def validate_next(raw: str | None) -> str | None:
    """Return the canonical relative `next` path if it is a safe, registered storefront route, else
    `None`. Default-deny: anything unrecognized returns `None`."""
    if not raw:
        return None
    if len(raw) > _MAX_LEN:
        return None
    # Decode exactly once, then refuse anything still percent-encoded (blocks double-encoding like
    # `%252f` that would decode to `/` only after a second pass on the client).
    decoded = unquote(raw)
    if _PERCENT_ENCODED.search(decoded):
        return None
    if any(ch.isspace() for ch in decoded):
        return None
    if _has_control_char(decoded):
        return None
    if "\\" in decoded or ".." in decoded:
        return None
    # A scheme marker anywhere (`:`) blocks http:/https:/javascript:/data: and protocol-relative tricks.
    if ":" in decoded:
        return None
    if not decoded.startswith(_PREFIX):
        return None
    # `//` anywhere would let `/portal/storefront//evil` or similar slip a second authority segment.
    if "//" in decoded:
        return None
    rest = decoded[len(_PREFIX):]
    # Split off the shop-id segment; the remainder must match a registered suffix.
    slash = rest.find("/")
    shop_seg = rest if slash == -1 else rest[:slash]
    suffix = "" if slash == -1 else rest[slash:]
    if not _SHOP_SEGMENT.fullmatch(shop_seg):
        return None
    if not _SUFFIX.fullmatch(suffix):
        return None
    # Canonical form: drop a bare trailing slash (the SPA index is `/portal/storefront/{id}`).
    canonical = f"{_PREFIX}{int(shop_seg)}{suffix}"
    if canonical.endswith("/") and canonical != _PREFIX:
        canonical = canonical.rstrip("/")
    return canonical


def parse_shop_id(next_path: str) -> int:
    """Extract the numeric shop id from a path already validated by `validate_next`."""
    rest = next_path[len(_PREFIX):]
    slash = rest.find("/")
    shop_seg = rest if slash == -1 else rest[:slash]
    return int(shop_seg)
