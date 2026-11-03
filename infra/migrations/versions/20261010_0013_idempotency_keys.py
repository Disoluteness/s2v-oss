"""add idempotency keys

Revision ID: 20261010_0013
Revises: 20261009_0012
Create Date: 2026-10-10 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_0013"
down_revision: str | None = "20261009_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``idempotency_key`` rather than ``key``: the latter is reserved on MySQL 8,
    # the same reason ``member_role`` / ``usage_role`` exist.
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_keys_scope_key",
        ),
    )
    op.create_index("ix_idempotency_keys_tenant_id", "idempotency_keys", ["tenant_id"])
    op.create_index("ix_idempotency_keys_actor_id", "idempotency_keys", ["actor_id"])
    op.create_index("ix_idempotency_keys_key", "idempotency_keys", ["idempotency_key"])
    op.create_index(
        "ix_idempotency_keys_fingerprint", "idempotency_keys", ["request_fingerprint"]
    )
    op.create_index("ix_idempotency_keys_state", "idempotency_keys", ["state"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_state", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_fingerprint", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_key", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_actor_id", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_tenant_id", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
