"""Opaque, tamper-evident keyset pagination cursors for the storefront portal.

A cursor encodes the `(created_at, id)` of the last row on the current page and is signed with
an HMAC over the *endpoint tag* + payload, keyed by the server `SECRET_KEY`. Folding the endpoint
into the signature means a cursor minted for one list (e.g. `customers`) fails verification if
replayed on another (e.g. `ledger:42`). Any malformed, tampered, or wrong-endpoint cursor raises
`CursorError`, which the routes map to HTTP 422. Cursors are ephemeral: rotating `SECRET_KEY`
invalidates outstanding cursors (acceptable — the client simply restarts from page 1).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

from app.core.config import settings


class CursorError(ValueError):
    """A cursor is malformed, tampered, or was minted for a different endpoint."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError) as exc:  # binascii.Error is a ValueError subclass
        raise CursorError("cursor is not valid base64") from exc


def _sign(endpoint: str, body: str) -> str:
    mac = hmac.new(
        settings.secret_key.encode("utf-8"),
        f"{endpoint}|{body}".encode(),
        hashlib.sha256,
    ).digest()
    return _b64e(mac)


def encode_cursor(*, endpoint: str, created_at: dt.datetime, row_id: int) -> str:
    """Encode a keyset position for `endpoint`. `created_at` is normalized to UTC."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    payload = json.dumps(
        {"t": created_at.astimezone(dt.timezone.utc).isoformat(), "i": int(row_id)},
        separators=(",", ":"), sort_keys=True,
    )
    body = _b64e(payload.encode("utf-8"))
    return f"{body}.{_sign(endpoint, body)}"


def decode_cursor(endpoint: str, cursor: str) -> tuple[dt.datetime, int]:
    """Decode + verify a cursor for `endpoint`. Raises CursorError on any problem."""
    if not cursor or cursor.count(".") != 1:
        raise CursorError("cursor is malformed")
    body, sig = cursor.split(".", 1)
    if not hmac.compare_digest(_sign(endpoint, body), sig):
        raise CursorError("cursor signature mismatch")
    try:
        data = json.loads(_b64d(body).decode("utf-8"))
        created_at = dt.datetime.fromisoformat(data["t"])
        row_id = int(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CursorError("cursor payload is invalid") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    return created_at, row_id
