"""In-app error tracking (I01): capture, fingerprinting, summary, and failure safety."""
from __future__ import annotations

import datetime as dt
import json
import logging

from app.core import errortrack
from app.services import owner_report


def _setup(tmp_path, monkeypatch, proc: str = "backend") -> None:
    errortrack._reset_for_tests()
    monkeypatch.setattr(errortrack, "LOG_DIR", tmp_path)
    errortrack.install(proc)


def _events(tmp_path, proc: str = "backend") -> list[dict]:
    path = tmp_path / f"errors-{proc}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_capture_exception_event(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        log = logging.getLogger("payments.test")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("confirm failed")
        events = _events(tmp_path)
        assert len(events) == 1
        ev = events[0]
        assert ev["proc"] == "backend"
        assert ev["logger"] == "payments.test"
        assert ev["exc"] == "ValueError"
        assert ev["msg"] == "confirm failed"
        assert ev["fp"]
        assert errortrack.parse_ts(ev["ts"]) is not None
    finally:
        errortrack._reset_for_tests()


def test_below_error_level_is_ignored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        log = logging.getLogger("svc.quiet")
        log.warning("just a warning")
        log.info("just info")
        assert _events(tmp_path) == []
        log.error("a plain error with no exception")
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["exc"] == ""
        assert events[0]["where"]  # falls back to the log call site
    finally:
        errortrack._reset_for_tests()


def test_fingerprint_groups_same_error_and_splits_different(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        log = logging.getLogger("svc.fp")
        for _ in range(2):
            try:
                raise RuntimeError("x")
            except RuntimeError:
                log.exception("op failed")
        try:
            raise KeyError("y")
        except KeyError:
            log.exception("other op failed")
        fps = [e["fp"] for e in _events(tmp_path)]
        assert len(fps) == 3
        assert fps[0] == fps[1]
        assert fps[2] != fps[0]
    finally:
        errortrack._reset_for_tests()


def test_summary_counts_groups_and_since_filter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        log = logging.getLogger("svc.sum")
        for _ in range(2):
            try:
                raise RuntimeError("x")
            except RuntimeError:
                log.exception("op failed")
        s = errortrack.summary()
        assert s["total"] == 2
        assert s["by_proc"] == {"backend": 2}
        assert s["top"][0]["count"] == 2
        assert s["top"][0]["exc"] == "RuntimeError"
        # A future cutoff excludes everything.
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
        assert errortrack.summary(since=future)["total"] == 0
        # recent_total sees them too (fresh cache).
        assert errortrack.recent_total() == 2
    finally:
        errortrack._reset_for_tests()


def test_summary_skips_corrupt_lines(tmp_path, monkeypatch):
    errortrack._reset_for_tests()
    monkeypatch.setattr(errortrack, "LOG_DIR", tmp_path)
    good = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "proc": "backend", "logger": "x", "fp": "abc123", "exc": "ValueError",
        "where": "f.py:g:1", "msg": "m",
    }
    (tmp_path / "errors-backend.jsonl").write_text(
        "not json at all\n" + json.dumps(good) + "\n", encoding="utf-8")
    s = errortrack.summary()
    assert s["total"] == 1
    assert s["top"][0]["fp"] == "abc123"


def test_install_failure_is_swallowed(tmp_path, monkeypatch):
    errortrack._reset_for_tests()
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a directory should be", encoding="utf-8")
    monkeypatch.setattr(errortrack, "LOG_DIR", blocker / "sub")
    errortrack.install("backend")  # must not raise even though mkdir fails
    assert errortrack._installed is None
    errortrack._reset_for_tests()


def test_install_is_idempotent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        errortrack.install("backend")  # second install is a no-op
        log = logging.getLogger("svc.once")
        log.error("only once")
        assert len(_events(tmp_path)) == 1
    finally:
        errortrack._reset_for_tests()


def test_parse_ts_defensive():
    assert errortrack.parse_ts("") is None
    assert errortrack.parse_ts("garbage") is None
    naive = errortrack.parse_ts("2026-07-02T10:00:00")
    assert naive is not None and naive.tzinfo is not None


def test_render_errors_section():
    assert owner_report.render_errors({"total": 0}) is None
    text = owner_report.render_errors({
        "total": 3,
        "top": [{"exc": "ValueError", "where": "payments.py:confirm:12", "count": 2}],
    })
    assert text is not None
    assert "3" in text
    assert "ValueError" in text
    assert "×2" in text
