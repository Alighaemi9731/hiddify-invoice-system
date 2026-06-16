"""
Public-facing display codes.

A row's primary key is a small sequential integer (1, 2, 3 …). Showing it to resellers
would leak how many invoices/payments the business has ever produced. `invoice_code`
maps the id to a stable **8-digit** number that is unique but NOT obviously sequential,
so adjacent invoices don't get adjacent numbers and the absolute count is hidden.

It's a pure, reversible bijection (no DB column, no migration): with a multiplier that is
coprime to the 8-digit span, `id -> base + (id*mult) mod span` never collides for any id in
the range, and the inverse exists if we ever need to decode.
"""
from __future__ import annotations

_BASE = 10_000_000
_SPAN = 90_000_000  # 10,000,000 .. 99,999,999  (= 2^7 · 3^2 · 5^7)
# Coprime to _SPAN (odd, digit-sum not divisible by 3, not ending in 0/5) → the map is a
# bijection over the whole 8-digit space, so codes are unique with zero collisions.
_MULT_INVOICE = 73_939_133


def _code(n: int, mult: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return _BASE
    return _BASE + (n * mult) % _SPAN


def invoice_code(invoice_id: int) -> str:
    """Stable 8-digit public invoice number for a given invoice id."""
    return str(_code(invoice_id, _MULT_INVOICE))
