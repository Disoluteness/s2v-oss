"""team quotas and project-to-team attribution

Revision ID: 20261012_0019
Revises: 20261010_0018
Create Date: 2026-10-12 09:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_0019"
down_revision: str | None = "20261010_0018"
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
    # V8.3 keeps one row per team: a quota is a *setting*, not a ledger. Who
    # changed it and when belongs in ``audit_logs`` (V6.5), because reading the
    # currently-effective quota sits on the hot path of every paid call and must
    # not become an ``ORDER BY created_at DESC LIMIT 1``.
    #
    # The window is derived from ``period`` at read time rather than stored: a
    # stored start/end would need a job to roll it over, and every deployment
    # without that job would silently stop enforcing at the month boundary.
    op.create_table(
        "team_quotas",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("team_id", sa.String(length=64), nullable=False),
        sa.Column(
            "period",
            sa.String(length=32),
            nullable=False,
            server_default="calendar_month",
        ),
        sa.Column("limit_cost", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("limit_runs", sa.Integer(), nullable=True),
        sa.Column("limit_tokens", sa.BigInteger(), nullable=True),
        sa.Column(
            "enforcement",
            sa.String(length=16),
            nullable=False,
            server_default="hard",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.UniqueConstraint("team_id", name="uq_team_quotas_team_id"),
    )
    op.create_index("ix_team_quotas_tenant_id", "team_quotas", ["tenant_id"])
    op.create_index("ix_team_quotas_team_id", "team_quotas", ["team_id"])
    op.create_index("ix_team_quotas_status", "team_quotas", ["status"])

    # Attribution edge: until V8.3 a project belonged to a tenant but to no
    # team, so "the team's spend" was unanswerable. NULL stays legal and means
    # *the default team* — the migration runs before anyone may have seated an
    # identity, so the default team row need not exist yet and a backfill would
    # either write a dangling reference or a value that changes meaning later.
    # Deliberately no FK constraint, matching ``projects.default_style_preset_id``
    # (0009): a nullable attribution hint, not a lifecycle owner.
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("team_id", sa.String(length=64), nullable=True))
    op.create_index("ix_projects_team_id", "projects", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_team_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("team_id")

    op.drop_index("ix_team_quotas_status", table_name="team_quotas")
    op.drop_index("ix_team_quotas_team_id", table_name="team_quotas")
    op.drop_index("ix_team_quotas_tenant_id", table_name="team_quotas")
    op.drop_table("team_quotas")
