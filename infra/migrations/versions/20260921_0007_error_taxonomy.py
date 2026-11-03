"""add error taxonomy fields

Revision ID: 20260921_0007
Revises: 20260920_0006
Create Date: 2026-09-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0007"
down_revision: str | None = "20260920_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("tool_invocations", "background_jobs"):
        op.add_column(table_name, sa.Column("error_category", sa.String(length=64), nullable=True))
        op.add_column(table_name, sa.Column("error_taxonomy_code", sa.String(length=120), nullable=True))
        op.add_column(table_name, sa.Column("error_retryable", sa.Boolean(), nullable=True))
        op.add_column(table_name, sa.Column("error_user_message", sa.Text(), nullable=True))
        op.add_column(table_name, sa.Column("error_remediation", sa.Text(), nullable=True))
        op.create_index(f"ix_{table_name}_error_category", table_name, ["error_category"])
        op.create_index(f"ix_{table_name}_error_taxonomy_code", table_name, ["error_taxonomy_code"])


def downgrade() -> None:
    for table_name in ("background_jobs", "tool_invocations"):
        op.drop_index(f"ix_{table_name}_error_taxonomy_code", table_name=table_name)
        op.drop_index(f"ix_{table_name}_error_category", table_name=table_name)
        op.drop_column(table_name, "error_remediation")
        op.drop_column(table_name, "error_user_message")
        op.drop_column(table_name, "error_retryable")
        op.drop_column(table_name, "error_taxonomy_code")
        op.drop_column(table_name, "error_category")
