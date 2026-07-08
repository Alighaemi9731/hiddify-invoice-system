"""H12 — login-security memory bounds + captcha throttle.

- the rate-limit bucket map is bounded (an attacker sending a unique username per request
  can't grow it without limit);
- the unauthenticated captcha endpoint is throttled per IP.
"""
from app.core import loginsec


def test_buckets_are_evicted_after_window(monkeypatch):
    loginsec._buckets.clear()
    t = [1000.0]
    monkeypatch.setattr(loginsec.time, "time", lambda: t[0])
    # A failed attempt for a unique username creates buckets.
    loginsec.record_failure("attacker1", "1.1.1.1")
    assert len(loginsec._buckets) >= 1
    # Advance past the window + lockout; the next insert evicts the stale ones.
    t[0] += loginsec.WINDOW_SECONDS + loginsec.LOCKOUT_SECONDS + 1
    loginsec.record_failure("attacker2", "2.2.2.2")
    # attacker1's buckets are gone; only attacker2's remain.
    keys = list(loginsec._buckets)
    assert not any("attacker1" in k for k in keys)
    assert any("attacker2" in k for k in keys)


def test_bucket_map_hard_capped(monkeypatch):
    loginsec._buckets.clear()
    t = [1000.0]
    monkeypatch.setattr(loginsec.time, "time", lambda: t[0])
    monkeypatch.setattr(loginsec, "_MAX_BUCKETS", 100)
    for i in range(300):
        t[0] += 0.001  # all within the window (not evictable by age)
        loginsec.record_failure(f"u{i}", f"{i}.0.0.0")
    # Even though nothing aged out, the map is bounded near the cap (×2 for ip+user keys +slack).
    assert len(loginsec._buckets) <= 100 + 4


def test_captcha_throttle_per_ip(monkeypatch):
    loginsec._captcha_hits.clear()
    t = [1000.0]
    monkeypatch.setattr(loginsec.time, "time", lambda: t[0])
    ip = "9.9.9.9"
    allowed = sum(1 for _ in range(100) if loginsec.captcha_allowed(ip))
    assert allowed == loginsec._CAPTCHA_MAX_PER_WINDOW
    # A different IP is independent.
    assert loginsec.captcha_allowed("8.8.8.8") is True
    # After the window elapses, the first IP is allowed again.
    t[0] += loginsec._CAPTCHA_WINDOW + 1
    assert loginsec.captcha_allowed(ip) is True
