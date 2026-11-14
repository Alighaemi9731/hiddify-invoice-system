import datetime as dt

import pytest

from app.services import storefront_provision


@pytest.mark.parametrize("data,expected", [
    ({"package_days": 30, "start_date": "2026-10-16"}, 1),
    ({"package_days": 30, "start_date": "2026-10-15"}, 0),
    ({"package_days": 30, "start_date": "2026-10-14"}, -1),
    ({"package_days": 30, "start_date": None}, 30),
    ({"package_days": 0, "start_date": None}, 0),
    ({"package_days": None}, -1),
    ({"package_days": 20000, "start_date": None}, 10000),
    ({"package_days": "30", "start_date": "2026-10-16T00:00:00"}, 1),
    ({"remaining_day": "7", "package_days": 30, "start_date": "2026-10-16"}, 7),
    ({"remaining_day": 0, "package_days": 30}, 0),
    ({"remaining_days": -2}, -2),
    ({}, None),
    ({"package_days": "unknown"}, None),
    ({"package_days": True}, None),
    ({"package_days": 1.5}, None),
    ({"package_days": 30, "start_date": "invalid"}, None),
])
def test_live_expiry_matches_package_calendar(monkeypatch, data, expected):
    monkeypatch.setattr(storefront_provision, "tehran_today", lambda: dt.date(2026, 11, 14))
    assert storefront_provision._remaining_days(data) == expected
