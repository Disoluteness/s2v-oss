"""worker scaling: queue, priority, lease, started_at, next_run_at

Revision ID: 20261010_0018
Revises: 20261010_0017
Create Date: 2026-10-10 22:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_0018"
down_revision: str | None = "20261010_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # V8.1 worker scaling keeps the single background_jobs table: a queue is a
    # column (which worker role claims the row), not an entity of its own.
    # - queue: role affinity, derived from job_type + capability at enqueue time.
    # - priority: smaller runs first; FIFO (created_at) only breaks ties.
    # - lease_expires_at: a real lease. NULL rows (pre-migration) count as
    #   expired so old rows are claimable without a backfill.
    # - started_at: lets the reaper find jobs stuck in `running` past the
    #   timeout. waiting_provider is exempt by design (provider polls are slow).
    # - next_run_at: exponential-backoff retry scheduling; NULL means claimable.
    op.add_column(
        "background_jobs",
        sa.Column(
            "queue",
            sa.String(length=40),
            nullable=False,
            server_default="media",
        ),
    )
    op.add_column(
        "background_jobs",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "background_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_jobs",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_background_jobs_queue", "background_jobs", ["queue"])
    # Backfill role affinity for paid provider jobs. stage_tool_step rows keep
    # the `media` default: their capability lives in a JSON payload whose
    # extraction is not portable across MySQL/SQLite, and historical rows are
    # mostly terminal states the claimer never touches.
    op.execute(
        "UPDATE background_jobs SET queue = 'provider' WHERE job_type = 'avatar_from_assets'"
    )


def downgrade() -> None:
    op.drop_index("ix_background_jobs_queue", table_name="background_jobs")
    op.drop_column("background_jobs", "next_run_at")
    op.drop_column("background_jobs", "started_at")
    op.drop_column("background_jobs", "lease_expires_at")
    op.drop_column("background_jobs", "priority")
    op.drop_column("background_jobs", "queue")
