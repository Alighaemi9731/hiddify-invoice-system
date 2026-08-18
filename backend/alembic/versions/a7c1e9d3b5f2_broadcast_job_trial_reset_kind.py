"""storefront_broadcast_jobs.kind gains 'trial_reset'

The monthly free-trial announcement is no longer a broadcast a shop admin wrote — the platform
sends it on a schedule, to every shop at once. It rides the same durable delivery job as a normal
broadcast, but it needs to be TELLABLE from one, for two reasons:

  * the delivery worker attaches the customer reply keyboard to a `trial_reset` job, which is what
    puts «🎁 تست رایگان» back on the phone of a customer whose menu was rendered before the button
    became permanent (`storefront_delivery._dispatch`);
  * in the reseller's own campaign history, a notice they did not write must not read as one they
    did.

Widening a CHECK is backward compatible in the direction that matters: every existing row is
'broadcast' or 'direct' and stays valid. The DOWNGRADE is the narrow one — it re-tightens the
constraint, so it first rewrites any `trial_reset` row back to 'broadcast' (the older build renders
it as a segment broadcast to «همه», which is exactly what it is minus the keyboard).

Revision ID: a7c1e9d3b5f2
Revises: c8d5b2e047af
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a7c1e9d3b5f2"
down_revision = "c8d5b2e047af"
branch_labels = None
depends_on = None

_TABLE = "storefront_broadcast_jobs"
_NAME = "ck_sfbjob_kind"
_WIDE = "kind in ('broadcast','direct','trial_reset')"
_NARROW = "kind in ('broadcast','direct')"
# The table's OTHER check. SQLite cannot alter a constraint, so batch mode rebuilds the table from
# what SQLAlchemy reflects — and reflected CHECKs come back unnamed, which would silently drop this
# one. Restating it here keeps the rebuilt table identical to the model.
_STATUS = sa.CheckConstraint(
    "status in ('queued','running','completed','canceled')", name="ck_sfbjob_status")


def _replace(expression: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(_NAME, _TABLE, type_="check")
        op.create_check_constraint(_NAME, _TABLE, expression)
        return
    with op.batch_alter_table(_TABLE, table_args=(_STATUS,)) as batch_op:
        batch_op.drop_constraint(_NAME, type_="check")
        batch_op.create_check_constraint(_NAME, expression)


def upgrade() -> None:
    _replace(_WIDE)


def downgrade() -> None:
    op.execute(
        sa.text(f"UPDATE {_TABLE} SET kind = 'broadcast' WHERE kind = 'trial_reset'")
    )
    _replace(_NARROW)
