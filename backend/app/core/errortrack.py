"""Lightweight in-app error tracking (no external service).

A ``logging.Handler`` (level >= ERROR) installed on the root logger of BOTH processes
(backend ``app/main.py`` and bot ``app/bot/run.py``). Every ERROR/exception record is
fingerprinted and appended as one JSON line to a small per-process rotating file under
``data/logs/`` on the shared ``app_data`` volume:

    errors-backend.jsonl / errors-bot.jsonl

Per-process files mean the two containers never contend on rotation. Read side:
``summary(since)`` aggregates both files (counts + top fingerprints) for ``/health``
(``errors_24h``, via the cached ``recent_total()``) and the owner's daily digest.

Every path here swallows its own failures — error TRACKING must never become an
error SOURCE.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("data/logs")
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3
_PROCS = ("backend", "bot")
_CACHE_TTL = 60.0  # /health is polled every ~10s; re-scan the files at most once a minute

_lock = threading.Lock()
_installed: str | None = None
_cache: tuple[float, int] | None = None


def _path_for(proc: str) -> Path:
    # Reads the module attribute at call time so tests can point LOG_DIR elsewhere.
    return Path(LOG_DIR) / f"errors-{proc}.jsonl"


def parse_ts(raw: str) -> dt.datetime | None:
    """Parse an ISO timestamp defensively (None on junk); naive values are treated as UTC."""
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=dt.timezone.utc) if ts.tzinfo is None else ts


def _last_app_frame(tb) -> str:  # noqa: ANN001 - traceback object
    """`file:func:line` of the LAST in-app frame (falls back to the innermost frame),
    so the fingerprint points at our code, not the library frame that finally raised."""
    best = ""
    last = ""
    while tb is not None:
        frame = tb.tb_frame
        fname = frame.f_code.co_filename
        label = f"{Path(fname).name}:{frame.f_code.co_name}:{tb.tb_lineno}"
        last = label
        if "/app/" in fname.replace("\\", "/"):
            best = label
        tb = tb.tb_next
    return best or last


def _fingerprint(logger_name: str, exc_type: str, where: str, template: str) -> str:
    """Stable id for "the same error": logger + exception type + in-app frame + the
    UNFORMATTED message template (so varying arguments don't split one bug into many)."""
    raw = f"{logger_name}|{exc_type}|{where}|{template[:120]}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


class _JsonFormatter(logging.Formatter):
    def __init__(self, proc: str):
        super().__init__()
        self.proc = proc

    def format(self, record: logging.LogRecord) -> str:
        exc = ""
        where = ""
        if record.exc_info and record.exc_info[0] is not None:
            exc = getattr(record.exc_info[0], "__name__", str(record.exc_info[0]))
            where = _last_app_frame(record.exc_info[2])
        if not where:
            where = f"{Path(record.pathname).name}:{record.funcName}:{record.lineno}"
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad %-template must not kill tracking
            msg = str(record.msg)
        event = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "proc": self.proc,
            "logger": record.name,
            "fp": _fingerprint(record.name, exc, where, str(record.msg)),
            "exc": exc,
            "where": where,
            "msg": msg[:300],
        }
        return json.dumps(event, ensure_ascii=False)


def install(proc: str) -> None:
    """Attach the JSONL error handler to the root logger. Idempotent; never raises.
    ``delay=True`` → the file is only created when the first error actually occurs."""
    global _installed
    try:
        with _lock:
            if _installed is not None:
                return
            path = _path_for(proc)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True
            )
            handler.setLevel(logging.ERROR)
            handler.setFormatter(_JsonFormatter(proc))
            logging.getLogger().addHandler(handler)
            _installed = proc
    except Exception:  # noqa: BLE001 - tracking must never break the app
        logging.getLogger(__name__).warning("errortrack install failed", exc_info=True)


def summary(since: dt.datetime | None = None) -> dict:
    """Aggregate tracked errors from every per-process file (current + first rotation).

    Returns ``{"total", "by_proc": {proc: n}, "top": [{fp, exc, msg, where, count,
    last_ts}, ...]}`` — ``top`` sorted by count, capped at 10. Never raises."""
    out: dict = {"total": 0, "by_proc": {}, "top": []}
    try:
        groups: dict[str, dict] = {}
        for proc in _PROCS:
            base = _path_for(proc)
            for path in (Path(f"{base}.1"), base):
                if not path.exists():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                ev = json.loads(line)
                            except Exception:  # noqa: BLE001 - skip torn/corrupt lines
                                continue
                            ts = parse_ts(str(ev.get("ts") or ""))
                            if since is not None and (ts is None or ts < since):
                                continue
                            out["total"] += 1
                            out["by_proc"][proc] = out["by_proc"].get(proc, 0) + 1
                            fp = str(ev.get("fp") or "")
                            g = groups.setdefault(fp, {
                                "fp": fp,
                                "exc": str(ev.get("exc") or ""),
                                "msg": str(ev.get("msg") or ""),
                                "where": str(ev.get("where") or ""),
                                "count": 0,
                                "last_ts": "",
                            })
                            g["count"] += 1
                            g["last_ts"] = max(g["last_ts"], str(ev.get("ts") or ""))
                except Exception:  # noqa: BLE001 - a broken file never breaks the summary
                    continue
        out["top"] = sorted(groups.values(), key=lambda g: -g["count"])[:10]
    except Exception:  # noqa: BLE001
        pass
    return out


def recent_total(hours: int = 24) -> int:
    """Total tracked errors in the last N hours, cached ~60s (cheap enough for /health)."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and _cache[0] > now:
            return _cache[1]
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    total = int(summary(since=since).get("total") or 0)
    with _lock:
        _cache = (now + _CACHE_TTL, total)
    return total


def _reset_for_tests() -> None:
    """Detach our handler and clear module state (test isolation only)."""
    global _installed, _cache
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler) and isinstance(h.formatter, _JsonFormatter):
            root.removeHandler(h)
            h.close()
    _installed = None
    _cache = None
