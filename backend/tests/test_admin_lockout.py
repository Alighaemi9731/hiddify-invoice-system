"""Admin-login lockout: setting a Hiddify admin's panel password via the Flask-Admin edit form
(the REST API has no password field), and the enforcement helper that applies it to a subtree while
never touching the panel owner. See M-note: password lockout closes the "suspended reseller re-enables
their own users via their still-valid UUID link" hole."""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from app.services import enforcement
from app.services.panel_client import admin_api


class _Response:
    def __init__(self, status_code: int, *, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _panel():
    return SimpleNamespace(
        key="test",
        owner_uuid="owner-key",
        admin_api_key=None,
        admin_api_base="https://panel.example/proxy/api/v2/admin",
        proxy_base="https://panel.example/proxy",
    )


_EDIT_FORM = """
<html><body>
<form method="get" action="/proxy/admin/adminuser/"><input name="search" value=""></form>
<form method="post" action="/proxy/admin/adminuser/edit/?id=42">
  <input name="csrf_token" value="tok&amp;123">
  <input name="name" value="NOrouzi">
  <input name="uuid" value="5836db89-8bcd-4c9c-8e2b-cbc484696382">
  <input name="max_users" value="500">
  <input name="max_active_users" value="0">
  <input type="checkbox" name="can_add_admin" value="y" checked>
  <select name="mode">
    <option value="super_admin">super</option>
    <option value="admin" selected>admin</option>
    <option value="agent">agent</option>
  </select>
  <textarea name="comment">hello</textarea>
  <input type="password" name="new_password" value="">
  <input type="submit" value="Save">
</form>
</body></html>
"""


# The list page exactly as a live Hiddify v12 panel renders one search hit: the edit link carries
# HTML-escaped `&amp;url=…&amp;modal=True` after the id, and the row id is Flask-Admin's numeric PK.
_LIST_PAGE = (
    '<table><tr><td><a href="/proxy/admin/adminuser/edit/?id=42&amp;url=/proxy/admin/adminuser/'
    '?search%3D5836db89-8bcd-4c9c-8e2b-cbc484696382&amp;modal=True">Edit</a></td></tr></table>'
)


def test_edit_link_id_is_read_from_the_real_list_markup():
    ids = re.findall(rf'{admin_api.ADMIN_LIST_PATH}edit/\?[^"\']*?\bid=(\d+)', _LIST_PAGE)
    assert ids == ["42"]


def test_scrape_edit_form_mirrors_every_field():
    f = admin_api._scrape_edit_form_fields(_EDIT_FORM)
    assert f["csrf_token"] == "tok&123"          # HTML-unescaped
    assert f["name"] == "NOrouzi"
    assert f["uuid"] == "5836db89-8bcd-4c9c-8e2b-cbc484696382"
    assert f["max_users"] == "500"
    assert f["max_active_users"] == "0"
    assert f["can_add_admin"] == "y"             # checked → included
    assert f["mode"] == "admin"                  # the selected option
    assert f["comment"] == "hello"
    assert "new_password" in f


def test_scrape_omits_unchecked_checkbox_and_submit():
    html = (
        '<form><input name="csrf_token" value="t">'
        '<input type="checkbox" name="can_add_admin" value="y">'  # unchecked
        '<input type="submit" name="save" value="Save">'
        '<input name="new_password" value=""></form>'
    )
    f = admin_api._scrape_edit_form_fields(html)
    assert "can_add_admin" not in f              # unchecked → omitted (would flip it off if sent "")
    assert "save" not in f                       # submit button never resent
    assert f["csrf_token"] == "t"


def test_set_admin_password_mirrors_form_and_injects_password(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            if url.endswith("/adminuser/"):              # search
                captured["search_url"] = url
                captured["search"] = params
                return _Response(200, text=_LIST_PAGE)
            captured["edit_get"] = (url, params)         # edit form GET
            return _Response(200, text=_EDIT_FORM)

        async def post(self, url, params=None, data=None):
            captured["post"] = (url, params, data)
            return _Response(302)                        # success → redirect to list

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    asyncio.run(
        admin_api.AdminApiClient().set_admin_password(
            _panel(), "5836db89-8bcd-4c9c-8e2b-cbc484696382", "blocked-node"
        )
    )

    # The admin list is `/admin/adminuser/` (Flask-Admin names the view after the MODEL class).
    # `/admin/admin/` — the path this shipped with — returns a JSON 404 on a real panel, so every
    # lockout failed while the suspension itself looked complete.
    assert captured["search_url"] == "https://panel.example/proxy/admin/adminuser/"
    assert captured["search"] == {"search": "5836db89-8bcd-4c9c-8e2b-cbc484696382"}
    url, params, data = captured["post"]
    assert url == "https://panel.example/proxy/admin/adminuser/edit/"
    assert params == {"id": "42"}
    # The password is injected, and the rest of the form is echoed verbatim (no clobber).
    assert data["new_password"] == "blocked-node"
    assert data["name"] == "NOrouzi" and data["max_users"] == "500"
    assert data["csrf_token"] == "tok&123"
    assert captured["headers"]["Hiddify-API-Key"] == "owner-key"   # super-admin edits any sub-admin


def test_set_admin_password_ambiguous_search_fails_closed(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, params=None):
            return _Response(
                200,
                text='<a href="/proxy/admin/adminuser/edit/?id=42">a</a>'
                     '<a href="/proxy/admin/adminuser/edit/?id=43">b</a>',
            )
        async def post(self, url, params=None, data=None):
            raise AssertionError("must not POST when the target admin is ambiguous")

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    err = None
    try:
        asyncio.run(admin_api.AdminApiClient().set_admin_password(_panel(), "u", "pw"))
    except RuntimeError as exc:
        err = exc
    assert err is not None and "returned 2 rows" in str(err)


def test_set_admin_password_form_validation_error_raises(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, params=None):
            if url.endswith("/admin/adminuser/"):
                return _Response(200, text='<a href="/proxy/admin/adminuser/edit/?id=42">e</a>')
            return _Response(200, text=_EDIT_FORM)
        async def post(self, url, params=None, data=None):
            return _Response(200, text='<div class="alert-danger">Should be a valid uuid</div>')

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    err = None
    try:
        asyncio.run(
            admin_api.AdminApiClient().set_admin_password(
                _panel(), "5836db89-8bcd-4c9c-8e2b-cbc484696382", "pw"
            )
        )
    except RuntimeError as exc:
        err = exc
    assert err is not None and "validation" in str(err)


def test_set_admin_password_refuses_a_form_belonging_to_another_admin(monkeypatch):
    """A row id is an indirection; the uuid in the mirrored form is the identity. If they disagree
    (stale/renumbered list page), locking the wrong admin out of their panel is unacceptable."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, params=None):
            if url.endswith("/adminuser/"):
                return _Response(200, text=_LIST_PAGE)
            return _Response(200, text=_EDIT_FORM)       # form carries a DIFFERENT uuid
        async def post(self, url, params=None, data=None):
            raise AssertionError("must not POST a form that belongs to another admin")

    monkeypatch.setattr(admin_api.httpx, "AsyncClient", FakeClient)
    err = None
    try:
        asyncio.run(
            admin_api.AdminApiClient().set_admin_password(
                _panel(), "11111111-2222-3333-4444-555555555555", "blocked-node"
            )
        )
    except RuntimeError as exc:
        err = exc
    assert err is not None and "another admin" in str(err)


def test_apply_admin_passwords_skips_owner_and_survives_failures(monkeypatch):
    calls = []

    class FakePw:
        async def set_admin_password(self, panel, admin_uuid, password, *, api_key=None):
            calls.append((admin_uuid, password))
            if admin_uuid == "boom":
                raise RuntimeError("panel form changed")

    monkeypatch.setattr(enforcement, "AdminApiClient", lambda *a, **k: FakePw())

    panel = SimpleNamespace(owner_uuid="OWNER", admin_api_key=None)
    admins = [
        SimpleNamespace(admin_uuid="OWNER", is_owner=True, name="owner"),  # super-admin → untouched
        SimpleNamespace(admin_uuid="sub1", is_owner=False, name="Sub One"),
        SimpleNamespace(admin_uuid="boom", is_owner=False, name="Boom"),   # failure must not abort
        SimpleNamespace(admin_uuid="sub2", is_owner=False, name="Sub Two"),
        SimpleNamespace(admin_uuid="sub1", is_owner=False, name="Sub One"),  # duplicate → once
    ]
    res = asyncio.run(enforcement._apply_admin_passwords(None, panel, admins, "123"))

    touched = [u for u, _ in calls]
    assert "OWNER" not in touched                 # owner login never changed
    assert touched.count("sub1") == 1             # deduped
    assert res["ok"] == 2 and res["failed"] == 1  # sub1, sub2 ok; boom failed
    # The failing admin is NAMED so the owner alert can say who kept their panel login.
    assert res["failed_names"] == ["Boom"]
