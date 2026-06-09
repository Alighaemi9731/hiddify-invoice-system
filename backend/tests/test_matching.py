"""Tests for parsing a reseller's pasted panel link."""
from app.bot.matching import normalize_host, normalize_path, parse_link


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


def test_parse_uses_url_containing_uuid_and_complete_proxy_path():
    uuid = "11111111-2222-3333-4444-555555555555"
    p = parse_link(
        "راهنما https://wrong.example/help و لینک من "
        f"https://Panel.Example.COM.:443/a%20b/secret/{uuid}/#نام"
    )
    assert p is not None
    assert p.host == "panel.example.com"
    assert p.path == "a b/secret"
    assert p.tag == "نام"


def test_identity_normalization_preserves_nondefault_port_and_path_case():
    assert normalize_host("HTTPS://Panel.Example.COM.:443/anything") == "panel.example.com"
    assert normalize_host("panel.example.com:8443") == "panel.example.com:8443"
    assert normalize_path("//Secret/%70ath/") == "Secret/path"
