"""Tiny process-local TTL caches, namespaced per SQLAlchemy engine.

Used for read-mostly lookups that were measured as repeated per-request round-trips in
the 2026-07-22 performance audit (settings rows + the owner AppUser row + computed
report previews). Values live only in this process's memory for a few seconds — the
database stays the source of truth, and writers invalidate through the helpers below.

Why the engine namespace: the test suite runs hundreds of tests in ONE process, each
against its OWN temporary database (often with identical URLs for in-memory SQLite). A
cache keyed by plain setting-key would leak values across tests/databases. Keying every
entry by a per-engine namespace id makes each engine's cache world independent — and in
production (one global engine per process) it is a no-op distinction.
"""
from __future__ import annotations

import itertools
import time
import weakref
from typing import Any

_ns_by_engine: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_counter = itertools.count(1)

#: Sentinel distinguishing "not cached" from a cached None/falsy value.
MISS = object()

# Safety valve: test runs create a namespace per engine; entries are tiny but unbounded
# growth is unbounded — reset wholesale past this size (never reached in production).
_MAX_ENTRIES = 4096


def engine_ns(session: Any) -> int:
    """Stable namespace id for the engine behind an (Async)Session."""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    ns = _ns_by_engine.get(engine)
    if ns is None:
        ns = next(_counter)
        _ns_by_engine[engine] = ns
    return ns


class TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._d: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        entry = self._d.get(key)
        if entry is None:
            return MISS
        expires, value = entry
        if expires <= time.monotonic():
            self._d.pop(key, None)
            return MISS
        return value

    def put(self, key: Any, value: Any) -> None:
        if len(self._d) >= _MAX_ENTRIES:
            self._d.clear()
        self._d[key] = (time.monotonic() + self.ttl, value)

    def drop(self, key: Any) -> None:
        self._d.pop(key, None)

    def clear(self) -> None:
        self._d.clear()
