"""Regression tests for the audit fixes (pure, no DB/network)."""
import datetime as dt

from app.core import loginsec


def test_loginsec_per_ip_and_global_lockout():
    loginsec._buckets.clear()
    # 5 fails from one IP locks that IP only.
    for _ in range(loginsec.MAX_ATTEMPTS):
        loginsec.record_failure("admin", "1.1.1.1")
    assert loginsec.is_locked("admin", "1.1.1.1") > 0
    assert loginsec.is_locked("admin", "2.2.2.2") == 0  # a fresh IP is NOT locked
    # A successful login clears both buckets for that IP.
    loginsec.reset("admin", "1.1.1.1")
    assert loginsec.is_locked("admin", "1.1.1.1") == 0


def test_loginsec_global_backstop_across_ips():
    loginsec._buckets.clear()
    # Distinct IP every attempt (the spoofed-XFF scenario) still trips the username backstop.
    for i in range(loginsec.GLOBAL_MAX_ATTEMPTS):
        loginsec.record_failure("admin", f"10.0.0.{i}")
    assert loginsec.is_locked("admin", "9.9.9.9") > 0  # brand-new IP is locked by the global cap


def test_crypto_mask_reveals_nothing():
    from app.core import crypto

    masked = crypto.mask("enc::supersecretvalue1234")
    assert set(masked) <= {"•"} and masked != ""      # only bullets
    assert "1234" not in masked and "value" not in masked  # no plaintext, not even the tail
    assert crypto.mask("") == "" and crypto.mask(None) == ""


def test_to_float_sanitizes_nan_inf():
    from app.services.panel_client.base import _to_float, _to_int

    assert _to_float(10.5) == 10.5
    assert _to_float("1.3") == 1.3
    assert _to_float("nan") == 0.0           # NaN → default (would corrupt billing)
    assert _to_float("inf") == 0.0
    assert _to_float(float("nan")) == 0.0
    assert _to_int("nan") is None
    assert _to_int(float("inf")) is None


def test_parse_backup_skips_bad_entries_not_crashes():
    from app.services.panel_client.base import parse_backup

    # Non-dict payload → empty, never raises.
    assert parse_backup(None).users == []
    payload = {
        "admin_users": [{"uuid": "a1", "name": "A"}, "garbage", {"name": "no-uuid"}],
        "users": [
            {"uuid": "u1", "usage_limit_GB": "5"},
            None,
            {"uuid": "u2", "usage_limit_GB": "nan"},  # bad value sanitized, row kept
        ],
    }
    data = parse_backup(payload)
    assert {a.uuid for a in data.admins} == {"a1"}
    assert {u.uuid for u in data.users} == {"u1", "u2"}
    assert next(u for u in data.users if u.uuid == "u2").usage_limit_gb == 0.0


def test_periods_use_local_timezone_today():
    from app.services import periods

    # today() must return a real date (Tehran-local) and previous/current must be consistent.
    t = periods.today()
    assert isinstance(t, dt.date)
    assert periods.current_month().label == t.strftime("%Y-%m")
    # Explicit-date overrides still work and are inclusive.
    p = periods.month_period(2026, 2)
    assert p.contains(dt.date(2026, 2, 15)) and not p.contains(dt.date(2026, 3, 1))


def test_reseller_report_applies_excluded_sizes():
    from types import SimpleNamespace

    from app.services.periods import month_period
    from app.services.reseller_report import _billable_gb_for_period

    P = month_period(2026, 2)

    def U(gb):
        return SimpleNamespace(start_date=dt.date(2026, 2, 10), usage_limit_gb=gb)

    users = [U(0.5), U(1), U(5), U(10)]  # free<=1; 5 is an extra excluded size
    # free threshold 1 excludes 0.5 and 1; excluded {5} also drops the 5 → only 10 remains.
    gb, cnt = _billable_gb_for_period(users, P, free_threshold=1.0, excluded={5})
    assert gb == 10.0 and cnt == 1


def test_owner_notify_escapes_reseller_name():
    """A reseller name with HTML metachars must be escaped so it can't break/inject the
    owner's HTML notification (finding #5)."""
    from types import SimpleNamespace

    from app.services.owner_notify import user_link

    link = user_link(SimpleNamespace(name="A & B <x>", id=1, bot_chat_id=123))
    assert "&amp;" in link and "&lt;x&gt;" in link  # name escaped
    assert link.startswith("<a href") and "</a>" in link  # the anchor markup itself is intact


def test_get_current_subject_rejects_unknown_user(tmp_path):
    """A token whose user no longer exists (e.g. username changed) is rejected, not trusted —
    only a DB ERROR falls back to trusting the token (finding #2)."""
    import asyncio

    import pytest
    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.core.security import create_access_token, get_current_subject

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'s.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine)
        token = create_access_token("ghost", {"role": "owner", "epoch": 0})
        async with Session() as s:
            with pytest.raises(HTTPException) as ei:
                await get_current_subject(token=token, session=s)
            assert ei.value.status_code == 401
        await engine.dispose()

    asyncio.run(run())
