"""add event log

Revision ID: 20260916_0002
Revises: 20260915_0001
Create Date: 2026-09-16 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0002"
down_revision: str | None = "20260915_0001"
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
        "event_logs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column("stage_key", sa.String(length=120), nullable=True),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("tool_invocation_id", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_logs_event_type", "event_logs", ["event_type"])
    op.create_index("ix_event_logs_run_id", "event_logs", ["run_id"])
    op.create_index("ix_event_logs_run_id_sequence", "event_logs", ["run_id", "sequence"])
    op.create_index("ix_event_logs_severity", "event_logs", ["severity"])
    op.create_index("ix_event_logs_source_id", "event_logs", ["source_id"])
    op.create_index("ix_event_logs_source_type", "event_logs", ["source_type"])
    op.create_index("ix_event_logs_stage_key", "event_logs", ["stage_key"])
    op.create_index("ix_event_logs_stage_run_id", "event_logs", ["stage_run_id"])
    op.create_index("ix_event_logs_tenant_id", "event_logs", ["tenant_id"])
    op.create_index("ix_event_logs_tool_invocation_id", "event_logs", ["tool_invocation_id"])


def downgrade() -> None:
    op.drop_index("ix_event_logs_tool_invocation_id", table_name="event_logs")
    op.drop_index("ix_event_logs_tenant_id", table_name="event_logs")
    op.drop_index("ix_event_logs_stage_run_id", table_name="event_logs")
    op.drop_index("ix_event_logs_stage_key", table_name="event_logs")
    op.drop_index("ix_event_logs_source_type", table_name="event_logs")
    op.drop_index("ix_event_logs_source_id", table_name="event_logs")
    op.drop_index("ix_event_logs_severity", table_name="event_logs")
    op.drop_index("ix_event_logs_run_id_sequence", table_name="event_logs")
    op.drop_index("ix_event_logs_run_id", table_name="event_logs")
    op.drop_index("ix_event_logs_event_type", table_name="event_logs")
    op.drop_table("event_logs")
