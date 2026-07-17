"""Opaque keyset cursor codec: round-trip, tamper detection, endpoint binding."""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/cursor.db")
os.environ.setdefault("SECRET_KEY", "cursor-test-key")

import pytest  # noqa: E402

from app.services import storefront_cursor as sc  # noqa: E402

_TS = dt.datetime(2026, 7, 17, 12, 30, 45, tzinfo=dt.timezone.utc)


def test_round_trip_preserves_created_at_and_id():
    cur = sc.encode_cursor(endpoint="customers", created_at=_TS, row_id=42)
    ts, rid = sc.decode_cursor("customers", cur)
    assert ts == _TS and rid == 42


def test_naive_created_at_is_treated_as_utc():
    naive = dt.datetime(2026, 7, 17, 12, 30, 45)
    cur = sc.encode_cursor(endpoint="customers", created_at=naive, row_id=7)
    ts, rid = sc.decode_cursor("customers", cur)
    assert ts == _TS.replace(microsecond=0) or ts == naive.replace(tzinfo=dt.timezone.utc)
    assert rid == 7


def test_wrong_endpoint_fails_signature():
    cur = sc.encode_cursor(endpoint="customers", created_at=_TS, row_id=42)
    with pytest.raises(sc.CursorError):
        sc.decode_cursor("ledger:42", cur)  # a customers cursor replayed on the ledger endpoint


def test_tampered_body_or_sig_rejected():
    cur = sc.encode_cursor(endpoint="customers", created_at=_TS, row_id=42)
    body, sig = cur.split(".", 1)
    with pytest.raises(sc.CursorError):
        sc.decode_cursor("customers", body + "x." + sig)         # mutated body
    with pytest.raises(sc.CursorError):
        sc.decode_cursor("customers", body + "." + sig[:-1] + "A")  # mutated signature


def test_malformed_shapes_rejected():
    for bad in ("", "nodot", "a.b.c", "!!!.###"):
        with pytest.raises(sc.CursorError):
            sc.decode_cursor("customers", bad)
