"""add pipeline versions

Revision ID: 20260918_0004
Revises: 20260917_0003
Create Date: 2026-09-18 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0004"
down_revision: str | None = "20260917_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "pipeline_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("pipeline_key", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("dsl_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("definition_snapshot", sa.JSON(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_versions_content_hash", "pipeline_versions", ["content_hash"])
    op.create_index("ix_pipeline_versions_is_current", "pipeline_versions", ["is_current"])
    op.create_index("ix_pipeline_versions_pipeline_key", "pipeline_versions", ["pipeline_key"])
    op.create_index("ix_pipeline_versions_status", "pipeline_versions", ["status"])
    op.create_index("ix_pipeline_versions_version", "pipeline_versions", ["version"])

    with op.batch_alter_table("production_runs") as batch_op:
        batch_op.add_column(sa.Column("pipeline_version_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("pipeline_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("pipeline_dsl_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("pipeline_definition_snapshot", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            "fk_production_runs_pipeline_version_id",
            "pipeline_versions",
            ["pipeline_version_id"],
            ["id"],
        )
    op.create_index(
        "ix_production_runs_pipeline_version_id",
        "production_runs",
        ["pipeline_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_production_runs_pipeline_version_id", table_name="production_runs")
    with op.batch_alter_table("production_runs") as batch_op:
        batch_op.drop_constraint(
            "fk_production_runs_pipeline_version_id",
            type_="foreignkey",
        )
        batch_op.drop_column("pipeline_definition_snapshot")
        batch_op.drop_column("pipeline_dsl_version")
        batch_op.drop_column("pipeline_version")
        batch_op.drop_column("pipeline_version_id")

    op.drop_index("ix_pipeline_versions_version", table_name="pipeline_versions")
    op.drop_index("ix_pipeline_versions_status", table_name="pipeline_versions")
    op.drop_index("ix_pipeline_versions_pipeline_key", table_name="pipeline_versions")
    op.drop_index("ix_pipeline_versions_is_current", table_name="pipeline_versions")
    op.drop_index("ix_pipeline_versions_content_hash", table_name="pipeline_versions")
    op.drop_table("pipeline_versions")
