"""add non-negative financial constraints

Revision ID: 6a9c7f21d4e0
Revises: 18a3b4fd6e33
Create Date: 2026-06-09
"""
from collections.abc import Sequence

from alembic import op

revision: str = "6a9c7f21d4e0"
down_revision: str | None = "18a3b4fd6e33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINTS = {
    "financial_records": [
        ("ck_financial_records_usage_nonnegative", "usage_gb >= 0"),
        ("ck_financial_records_price_nonnegative", "price_per_gb >= 0"),
        ("ck_financial_records_toman_nonnegative", "amount_toman >= 0"),
        ("ck_financial_records_usdt_nonnegative", "amount_usdt >= 0"),
    ],
    "invoices": [
        ("ck_invoices_usage_nonnegative", "usage_gb >= 0"),
        ("ck_invoices_price_nonnegative", "price_per_gb >= 0"),
        ("ck_invoices_toman_nonnegative", "amount_toman >= 0"),
        ("ck_invoices_base_toman_nonnegative", "base_amount_toman >= 0"),
        ("ck_invoices_min_sale_nonnegative", "min_sale_toman >= 0"),
        ("ck_invoices_rate_nonnegative", "usdt_rate >= 0"),
        ("ck_invoices_usdt_nonnegative", "amount_usdt >= 0"),
    ],
    "invoice_lines": [
        ("ck_invoice_lines_usage_nonnegative", "usage_gb >= 0"),
    ],
    "payments": [
        ("ck_payments_confirmations_nonnegative", "confirmations >= 0"),
        ("ck_payments_usdt_nonnegative", "amount_usdt >= 0"),
        ("ck_payments_toman_nonnegative", "amount_toman IS NULL OR amount_toman >= 0"),
    ],
    "resellers": [
        ("ck_resellers_price_nonnegative", "price_per_gb IS NULL OR price_per_gb >= 0"),
        ("ck_resellers_min_sale_nonnegative", "min_sale_toman IS NULL OR min_sale_toman >= 0"),
        ("ck_resellers_gb_cap_nonnegative", "gb_cap IS NULL OR gb_cap >= 0"),
    ],
    "usage_meters": [
        ("ck_usage_meters_quota_nonnegative", "quota_added_gb >= 0"),
        ("ck_usage_meters_consumed_nonnegative", "consumed_gb >= 0"),
        ("ck_usage_meters_overage_nonnegative", "overage_gb >= 0"),
        ("ck_usage_meters_edit_renewal_nonnegative", "edit_renewal_gb >= 0"),
        ("ck_usage_meters_reset_count_nonnegative", "reset_count >= 0"),
    ],
}


def upgrade() -> None:
    for table, constraints in _CONSTRAINTS.items():
        with op.batch_alter_table(table) as batch_op:
            for name, expression in constraints:
                batch_op.create_check_constraint(name, expression)


def downgrade() -> None:
    for table, constraints in reversed(list(_CONSTRAINTS.items())):
        with op.batch_alter_table(table) as batch_op:
            for name, _expression in reversed(constraints):
                batch_op.drop_constraint(name, type_="check")
