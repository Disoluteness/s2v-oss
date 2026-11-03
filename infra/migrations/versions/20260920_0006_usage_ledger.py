"""add usage ledger

Revision ID: 20260920_0006
Revises: 20260920_0005
Create Date: 2026-09-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0006"
down_revision: str | None = "20260920_0005"
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
        "usage_ledger_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=True),
        sa.Column("tool_key", sa.String(length=120), nullable=True),
        sa.Column("operation", sa.String(length=160), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("actual_cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("asset_bytes", sa.Integer(), nullable=True),
        sa.Column("usage_unit", sa.String(length=64), nullable=True),
        sa.Column("usage_quantity", sa.Numeric(16, 4), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_ledger_entries_tenant_id", "usage_ledger_entries", ["tenant_id"])
    op.create_index("ix_usage_ledger_entries_run_id", "usage_ledger_entries", ["run_id"])
    op.create_index("ix_usage_ledger_entries_stage_run_id", "usage_ledger_entries", ["stage_run_id"])
    op.create_index("ix_usage_ledger_entries_source_type", "usage_ledger_entries", ["source_type"])
    op.create_index("ix_usage_ledger_entries_source_id", "usage_ledger_entries", ["source_id"])
    op.create_index("ix_usage_ledger_entries_provider", "usage_ledger_entries", ["provider"])
    op.create_index("ix_usage_ledger_entries_tool_key", "usage_ledger_entries", ["tool_key"])
    op.create_index("ix_usage_ledger_entries_operation", "usage_ledger_entries", ["operation"])


def downgrade() -> None:
    op.drop_index("ix_usage_ledger_entries_operation", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_tool_key", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_provider", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_source_id", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_source_type", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_stage_run_id", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_run_id", table_name="usage_ledger_entries")
    op.drop_index("ix_usage_ledger_entries_tenant_id", table_name="usage_ledger_entries")
    op.drop_table("usage_ledger_entries")
