"""Gregorian-month billing periods (inclusive of both the first and last day)."""
from __future__ import annotations

import calendar
import datetime as dt
import os
from dataclasses import dataclass

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Billing months are anchored to the OWNER's clock (same tz the scheduler fires on), not the
# container's UTC. Otherwise, near midnight, `dt.date.today()` (UTC, +3:30 behind Tehran)
# can land in the wrong month — e.g. an invoice_hour of 0 would bill the wrong period.
_TZ_NAME = os.getenv("SCHEDULER_TIMEZONE", "Asia/Tehran")


def today() -> dt.date:
    """The current date in the configured local timezone (Asia/Tehran by default)."""
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo(_TZ_NAME)).date()
        except Exception:  # noqa: BLE001
            pass
    return dt.date.today()


def to_local_date(ts: dt.datetime) -> dt.date:
    """The LOCAL (Asia/Tehran) calendar date a stored instant fell on. Naive values are
    treated as UTC (how timestamps are stored). Raw `.date()` on a UTC instant extracts the
    UTC day — for events in the Tehran 00:00–03:29 window that's the PREVIOUS day, which
    made day-count anchors (e.g. dunning's `sent_at`) fire a day early."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if ZoneInfo is not None:
        try:
            return ts.astimezone(ZoneInfo(_TZ_NAME)).date()
        except Exception:  # noqa: BLE001
            pass
    return ts.date()


@dataclass(frozen=True)
class Period:
    start: dt.date
    end: dt.date

    @property
    def label(self) -> str:
        return self.start.strftime("%Y-%m")

    def contains(self, d: dt.date | None) -> bool:
        return d is not None and self.start <= d <= self.end


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(dt.date(year, month, 1), dt.date(year, month, last))


def previous_month(today_: dt.date | None = None) -> Period:
    today_ = today_ or today()
    first_this = today_.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    return month_period(last_prev.year, last_prev.month)


def current_month(today_: dt.date | None = None) -> Period:
    today_ = today_ or today()
    return month_period(today_.year, today_.month)


def parse_period(value: str) -> Period:
    """Accept 'YYYY-MM' (or 'YYYY/MM')."""
    value = value.strip().replace("/", "-")
    parts = value.split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid period '{value}', expected YYYY-MM")
    return month_period(int(parts[0]), int(parts[1]))
