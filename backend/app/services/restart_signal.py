"""
Cross-process restart coordination for restores.

The backend (API + scheduler) and the bot run as SEPARATE processes/containers but
share the `app_data` volume. A restore changes the database and may change SECRET_KEY
in `.env`; the process that did NOT perform the restore still holds the old key and a
pooled handle to the pre-restore DB, so it must restart too.

Mechanism: the restoring code writes a token into a marker file on the shared volume.
Every long-running process records the token it saw at startup and, while running, polls
the marker; if the token changes it self-exits (Docker's `restart: unless-stopped` brings
it back). The startup-token comparison makes this provably loop-free — after a restart the
process reads the new token as its own startup token and stays up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

log = logging.getLogger("restart")

# Same shared dir the self-update watcher uses (host-mounted at the repo data dir).
_DIR = os.environ.get("UPDATE_DIR", "/app/data")
_MARKER = Path(_DIR) / ".restart-requested"

# Token observed when this process started watching. None until init() runs.
_startup_token: str | None = None
_inited = False


def _read_token() -> str | None:
    try:
        return _MARKER.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def init() -> None:
    """Capture the marker token present at startup. Idempotent."""
    global _startup_token, _inited
    if _inited:
        return
    _startup_token = _read_token()
    _inited = True


def request_restart(token: str) -> None:
    """Record that a restore happened, so peer processes self-restart."""
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(token, encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.warning("could not write restart marker", exc_info=True)


def peer_restart_requested() -> bool:
    """True when the marker token differs from the one seen at startup."""
    cur = _read_token()
    return cur is not None and cur != _startup_token


async def watch_loop(interval: float = 5.0) -> None:
    """Self-exit when a peer process signals a restart (after a restore)."""
    init()
    while True:
        await asyncio.sleep(interval)
        if peer_restart_requested():
            log.info("restart marker changed (peer restore) — self-restarting")
            os.kill(os.getpid(), signal.SIGTERM)
            return


def start_watcher() -> asyncio.Task | None:
    """Start the watch loop as a background task (best-effort)."""
    init()
    try:
        return asyncio.get_event_loop().create_task(watch_loop())
    except Exception:  # noqa: BLE001
        log.warning("could not start restart watcher", exc_info=True)
        return None
