"""add artifact schema registry fields

Revision ID: 20260920_0005
Revises: 20260918_0004
Create Date: 2026-09-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0005"
down_revision: str | None = "20260918_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("schema_key", sa.String(length=160), nullable=True))
    op.add_column("artifacts", sa.Column("producer", sa.String(length=120), nullable=True))
    op.add_column("artifacts", sa.Column("lineage", sa.JSON(), nullable=True))
    op.add_column("artifacts", sa.Column("derived_from", sa.JSON(), nullable=True))
    op.create_index("ix_artifacts_schema_key", "artifacts", ["schema_key"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_schema_key", table_name="artifacts")
    op.drop_column("artifacts", "derived_from")
    op.drop_column("artifacts", "lineage")
    op.drop_column("artifacts", "producer")
    op.drop_column("artifacts", "schema_key")
