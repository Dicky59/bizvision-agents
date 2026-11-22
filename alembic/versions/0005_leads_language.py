"""leads: language of the visitor (notification language) and a per-tenant email index

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("language", sa.String(length=8), nullable=True))
    op.execute("CREATE INDEX ix_leads_tenant_email ON leads (tenant_id, lower(email))")


def downgrade() -> None:
    op.execute("DROP INDEX ix_leads_tenant_email")
    op.drop_column("leads", "language")
