"""Top-level reseller counting — shared by the panel list + the bot «آمار کلی»."""
from types import SimpleNamespace

from app.services.reseller_stats import RootStats, top_level_roots


def R(uuid, parent, *, owner=False, exempt=False, chat=None, panel=1):
    return SimpleNamespace(
        panel_id=panel, admin_uuid=uuid, parent_admin_uuid=parent, is_owner=owner,
        exclude_from_billing=exempt, bot_chat_id=chat, name=uuid,
    )


def _fixture():
    return [
        R("owner", None, owner=True),         # owner — never a root
        R("r1", "owner", chat=111),           # main, billable, connected
        R("r2", "owner"),                     # main, billable, NOT connected
        R("r3", "owner", exempt=True, chat=9),  # main but EXEMPT (excluded from billable)
        R("r1a", "r1", chat=222),             # sub of r1 — not a root
        R("r1a1", "r1a"),                     # deeper sub — not a root
        R("orphan", "ghost", chat=333),       # parent missing → structural root, billable
    ]


def test_top_level_roots_excludes_owner_and_subs():
    roots = {r.admin_uuid for r in top_level_roots(_fixture())}
    assert roots == {"r1", "r2", "r3", "orphan"}  # subs r1a/r1a1 and the owner are excluded


def test_root_counts_split_billable_exempt_connected():
    roots = top_level_roots(_fixture())
    billable = [r for r in roots if not r.exclude_from_billing]
    exempt = [r for r in roots if r.exclude_from_billing]
    connected = sum(1 for r in billable if r.bot_chat_id is not None)
    s = RootStats(total=len(roots), billable=len(billable), exempt=len(exempt), connected=connected)
    assert s.total == 4
    assert s.billable == 3          # r1, r2, orphan (r3 is exempt)
    assert s.exempt == 1            # r3
    assert s.connected == 2         # r1 (111) + orphan (333); r2 not connected, r3 exempt not counted


def test_same_uuid_on_two_panels_is_independent():
    # A sub on panel 1 whose uuid happens to match an owner on panel 2 must NOT be treated
    # as a root via the wrong panel's owner set.
    res = [
        R("owner", None, owner=True, panel=2),
        R("x", "owner", panel=1),       # panel 1 has no owner row → structural root
        R("y", "x", panel=1),           # sub of x on panel 1
    ]
    roots = {(r.panel_id, r.admin_uuid) for r in top_level_roots(res)}
    assert roots == {(1, "x")}          # y is a sub; the panel-2 owner is excluded
