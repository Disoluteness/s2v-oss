"""add provider credential manager

Revision ID: 20260921_0008
Revises: 20260921_0007
Create Date: 2026-09-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0008"
down_revision: str | None = "20260921_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=True),
        sa.Column("credential_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("secret_values", sa.JSON(), nullable=False),
        sa.Column("quota", sa.JSON(), nullable=False),
        sa.Column("credential_metadata", sa.JSON(), nullable=False),
        sa.Column("last_check_status", sa.String(length=32), nullable=True),
        sa.Column("last_check_message", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_credentials_tenant_id", "provider_credentials", ["tenant_id"])
    op.create_index("ix_provider_credentials_project_id", "provider_credentials", ["project_id"])
    op.create_index("ix_provider_credentials_provider", "provider_credentials", ["provider"])
    op.create_index("ix_provider_credentials_status", "provider_credentials", ["status"])


def downgrade() -> None:
    op.drop_index("ix_provider_credentials_status", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_provider", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_project_id", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_tenant_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
