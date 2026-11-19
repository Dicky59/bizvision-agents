"""usage_events.cost_eur precision: Numeric(18, 10)

Short embedding calls cost ~1e-7 EUR and were rounded to 0 by Numeric(12, 6).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "usage_events",
        "cost_eur",
        existing_type=sa.Numeric(12, 6),
        type_=sa.Numeric(18, 10),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "usage_events",
        "cost_eur",
        existing_type=sa.Numeric(18, 10),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
    )
