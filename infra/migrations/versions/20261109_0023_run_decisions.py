"""run-scoped decision log

Revision ID: 20261109_0023
Revises: 20261012_0022
Create Date: 2026-11-09 12:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0023"
down_revision: str | None = "20261012_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``audit_logs`` already records privileged actions, but it answers a
    # different question: who changed what. This table records the reasoning
    # behind a choice — the winner, the alternatives with their scores, and the
    # actor — so a run can be audited without replaying the selector.
    #
    # No unique constraint on (run_id, sequence): sequences are allocated from
    # ``max(sequence) + 1`` inside the writing transaction, exactly as
    # ``event_logs`` does, and a hard constraint would turn an interleaved
    # re-plan into a 500 instead of an out-of-order row the reader sorts anyway.
    op.create_table(
        "run_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("stage_key", sa.String(length=120), nullable=True),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("chosen", sa.JSON(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("confidence_basis", sa.String(length=64), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("actor_display_name", sa.String(length=160), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=64), nullable=True),
        *[
            sa.Column(
                name,
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            )
            for name in ("created_at", "updated_at")
        ],
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
    )
    op.create_index("ix_run_decisions_tenant_id", "run_decisions", ["tenant_id"])
    op.create_index("ix_run_decisions_project_id", "run_decisions", ["project_id"])
    op.create_index("ix_run_decisions_run_id", "run_decisions", ["run_id"])
    op.create_index("ix_run_decisions_category", "run_decisions", ["category"])
    op.create_index("ix_run_decisions_stage_key", "run_decisions", ["stage_key"])
    op.create_index("ix_run_decisions_decision", "run_decisions", ["decision"])
    op.create_index("ix_run_decisions_actor_id", "run_decisions", ["actor_id"])


def downgrade() -> None:
    # Dropping the table drops its rows. A decision log is derived data: every
    # row can be rebuilt by re-planning the run, so there is nothing to preserve
    # across a downgrade — and keeping it would leave a table the ORM of the
    # older revision cannot read.
    for index_name in (
        "ix_run_decisions_actor_id",
        "ix_run_decisions_decision",
        "ix_run_decisions_stage_key",
        "ix_run_decisions_category",
        "ix_run_decisions_run_id",
        "ix_run_decisions_project_id",
        "ix_run_decisions_tenant_id",
    ):
        op.drop_index(index_name, table_name="run_decisions")
    op.drop_table("run_decisions")
