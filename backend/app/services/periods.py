"""Gregorian-month billing periods (inclusive of both the first and last day)."""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass


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


def previous_month(today: dt.date | None = None) -> Period:
    today = today or dt.date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    return month_period(last_prev.year, last_prev.month)


def current_month(today: dt.date | None = None) -> Period:
    today = today or dt.date.today()
    return month_period(today.year, today.month)


def parse_period(value: str) -> Period:
    """Accept 'YYYY-MM' (or 'YYYY/MM')."""
    value = value.strip().replace("/", "-")
    parts = value.split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid period '{value}', expected YYYY-MM")
    return month_period(int(parts[0]), int(parts[1]))
