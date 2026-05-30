"""Unit tests for the invoice formula (the project's source-of-truth rules)."""
import datetime as dt
from types import SimpleNamespace

from app.services.invoice_engine import compute_invoices, select_billable_roots
from app.services.periods import month_period


def R(uuid, parent, *, owner=False, exclude=False, price=None, name=""):
    return SimpleNamespace(
        admin_uuid=uuid, parent_admin_uuid=parent, is_owner=owner,
        exclude_from_billing=exclude, price_per_gb=price, name=name or uuid, id=hash(uuid) & 0xffff,
    )


def U(uuid, added_by, day, gb):
    return SimpleNamespace(
        user_uuid=uuid, added_by_uuid=added_by, name=uuid,
        start_date=dt.date(2026, 2, day), usage_limit_gb=gb,
    )


def _fixture():
    owner = R("owner", None, owner=True)
    r1 = R("r1", "owner", name="R1")          # billable root
    r1a = R("r1a", "r1", name="R1a")          # sub-reseller of R1
    r2 = R("r2", "owner", exclude=True)        # excluded top-level
    resellers = [owner, r1, r1a, r2]
    u5_out_of_period = SimpleNamespace(
        user_uuid="u5", added_by_uuid="r1", name="u5",
        start_date=dt.date(2026, 1, 15), usage_limit_gb=40,
    )
    users = [
        U("u1", "r1", 15, 10),    # billed
        U("u2", "r1a", 15, 20),   # billed under R1 bundle (sub-reseller)
        U("u3", "r1", 15, 1),     # excluded: 1 GB test config
        U("u4", "r1", 15, 5),     # billed: 5 GB now counts
        u5_out_of_period,          # out of period (January) -> excluded
        U("u6", "owner", 15, 50), # not billed: owner-created
        U("u7", "r2", 15, 30),    # not billed: excluded subtree
    ]
    return resellers, users


def test_billable_roots_excludes_owner_and_excluded():
    resellers, _ = _fixture()
    roots = {r.admin_uuid for r in select_billable_roots(resellers)}
    assert roots == {"r1"}  # owner, r1a (sub), r2 (excluded) are not roots


def test_formula_bundles_subresellers_and_applies_exclusions():
    resellers, users = _fixture()
    bundles = compute_invoices(
        resellers, users, month_period(2026, 2),
        default_price_per_gb=1000, excluded_usage_gb={1},
    )
    assert len(bundles) == 1
    b = bundles[0]
    assert b.root.admin_uuid == "r1"
    # 10 (u1) + 20 (u2, sub) + 5 (u4, 5GB included) = 35 ; u3=1GB excluded, u5 out of period,
    # u6 owner-created, u7 excluded subtree -> all skipped
    assert b.total_gb == 35
    assert b.users_count == 3
    assert b.amount_toman == 35_000


def test_per_reseller_price_override():
    resellers, users = _fixture()
    for r in resellers:
        if r.admin_uuid == "r1":
            r.price_per_gb = 800
    b = compute_invoices(resellers, users, month_period(2026, 2),
                         default_price_per_gb=1000, excluded_usage_gb={1})[0]
    assert b.price_per_gb == 800
    assert b.amount_toman == 35 * 800


def test_no_carryover_each_period_standalone():
    # March has no in-period users -> zero usage (no implicit carry of Feb).
    resellers, users = _fixture()
    bundles = compute_invoices(resellers, users, month_period(2026, 3),
                               default_price_per_gb=1000, excluded_usage_gb={1})
    assert all(b.total_gb == 0 for b in bundles)
