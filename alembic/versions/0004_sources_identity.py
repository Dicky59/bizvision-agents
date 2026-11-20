"""sources: one row per (tenant, type, uri-or-filename)

Source identity for re-ingest: web pages and FAQ entries are keyed by `uri`, files by
`filename`. The index is partial: rows with neither (not produced by ingestion) are unconstrained.
Removed sources keep their row, so a page that comes back is reactivated instead of duplicated.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_sources_identity ON sources "
        "(tenant_id, type, (coalesce(uri, filename))) "
        "WHERE coalesce(uri, filename) IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_sources_identity")
