"""In-memory, TTL'd WebAuthn challenge store. A challenge only needs to survive the brief
begin→complete round-trip; the backend runs a single uvicorn worker, so an in-process dict
is safe (a server restart just makes the user retry — no security impact)."""
from __future__ import annotations

import base64
import os
import time

_TTL = 300.0  # seconds
_store: dict[str, tuple[bytes, float, str | None]] = {}


def _gc() -> None:
    now = time.time()
    for k in [k for k, (_, exp, _) in _store.items() if exp < now]:
        _store.pop(k, None)


def put(challenge: bytes, username: str | None = None) -> str:
    """Stash a challenge, return a random handle the client echoes back on complete."""
    _gc()
    handle = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    _store[handle] = (challenge, time.time() + _TTL, username)
    return handle


def take(handle: str) -> tuple[bytes, str | None] | None:
    """Pop + return (challenge, username) for a handle, or None if missing/expired. Single-use."""
    _gc()
    v = _store.pop(handle, None)
    if not v:
        return None
    challenge, exp, username = v
    if exp < time.time():
        return None
    return challenge, username
