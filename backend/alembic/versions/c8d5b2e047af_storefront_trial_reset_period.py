"""storefront_bots.trial_reset_period — the monthly free-trial re-arm stamp

A storefront's free trial was one-per-customer FOR LIFE: `StorefrontCustomer.free_trial_used`
was set on claim and nothing ever cleared it, so a shop had no way to win a lapsed customer
back. Shop admins may now re-arm every customer's trial, but at most ONCE PER GREGORIAN
CALENDAR MONTH — this column records the `YYYY-MM` in which that last happened (NULL = never).

A string period rather than a timestamp, matching `Reseller.gb_cap_alerted_period`: the limit
is expressed in billing months, which are the same months the invoices use, and comparing two
`YYYY-MM` strings cannot drift with timezones the way a rolling-window timestamp can.

Additive and backward compatible: NULL means "never reset", which is exactly the state every
existing shop is in.

Revision ID: c8d5b2e047af
Revises: b3f6a1d94c27
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c8d5b2e047af"
down_revision = "b3f6a1d94c27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_bots",
        sa.Column("trial_reset_period", sa.String(length=7), nullable=True),
    )


def downgrade() -> None:
    # Fully reversible: the column only gates the once-a-month reset button, and the older build
    # ships no reset path at all, so dropping it returns the shops to "trial is lifetime-once".
    op.drop_column("storefront_bots", "trial_reset_period")
