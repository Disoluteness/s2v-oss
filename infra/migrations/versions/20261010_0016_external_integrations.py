"""add external integrations

Revision ID: 20261010_0016
Revises: 20261010_0015
Create Date: 2026-10-10 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_0016"
down_revision: str | None = "20261010_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One table is all V7.4 needs: an integration is a *registration* (who may
    # embed us, from which origins), while the embed session itself is a
    # stateless signed token — no rows to write, nothing to clean up.
    op.create_table(
        "external_integrations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # JSON array of origins ("https://portal.example.com"), turned into the
        # embed page's CSP ``frame-ancestors``. Stored rather than configured in
        # the environment because it is per-partner data, not deployment config.
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_external_integrations_tenant_slug"),
    )
    op.create_index("ix_external_integrations_tenant_id", "external_integrations", ["tenant_id"])
    op.create_index("ix_external_integrations_status", "external_integrations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_external_integrations_status", table_name="external_integrations")
    op.drop_index("ix_external_integrations_tenant_id", table_name="external_integrations")
    op.drop_table("external_integrations")
