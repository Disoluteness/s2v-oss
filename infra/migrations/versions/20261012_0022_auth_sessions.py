"""user password login and sessions

Revision ID: 20261012_0022
Revises: 20261012_0021
Create Date: 2026-10-12 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_0022"
down_revision: str | None = "20261012_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # V1.1 username-password login. Both columns are nullable on purpose: every
    # identity that predates the login feature keeps working unchanged, and
    # "no hash" simply means "this user cannot log in yet".
    op.add_column("users", sa.Column("password_hash", sa.String(length=512), nullable=True))
    op.add_column(
        "users", sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_tenant_id", "user_sessions", ["tenant_id"])
    op.create_index("ix_user_sessions_token_prefix", "user_sessions", ["token_prefix"])


def downgrade() -> None:
    # Logout-then-drop: a downgrade that strands rows the ORM can no longer read
    # is worse than one that simply removes the feature's data.
    op.drop_index("ix_user_sessions_token_prefix", table_name="user_sessions")
    op.drop_index("ix_user_sessions_tenant_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_column("users", "password_updated_at")
    op.drop_column("users", "password_hash")
