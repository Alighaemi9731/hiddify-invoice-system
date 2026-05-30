"""Tests for parsing a reseller's pasted panel link."""
from app.bot.matching import parse_link


def test_parse_full_link():
    p = parse_link("https://panel-01.sidin.com.de/sXDm8ZxnkWl5kI4RppukoBGLx8E/"
                   "e5ed5732-1b80-489d-ac17-269d254e5c49/#PH")
    assert p is not None
    assert p.uuid == "e5ed5732-1b80-489d-ac17-269d254e5c49"
    assert p.host == "panel-01.sidin.com.de"
    assert p.path == "sXDm8ZxnkWl5kI4RppukoBGLx8E"
    assert p.tag == "PH"


def test_parse_with_surrounding_text():
    p = parse_link("سلام این لینک منه: https://x.example.com/abc/"
                   "11111111-2222-3333-4444-555555555555/#name لطفا ثبت کن")
    assert p.uuid == "11111111-2222-3333-4444-555555555555"
    assert p.tag == "name"


def test_parse_uuid_only():
    p = parse_link("e5ed5732-1b80-489d-ac17-269d254e5c49")
    assert p is not None
    assert p.uuid == "e5ed5732-1b80-489d-ac17-269d254e5c49"


def test_parse_no_uuid_returns_none():
    assert parse_link("just some text without an id") is None
    assert parse_link("") is None
