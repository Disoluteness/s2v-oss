"""encrypted secrets

Revision ID: 20261012_0020
Revises: 20261012_0019
Create Date: 2026-10-12 11:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_0020"
down_revision: str | None = "20261012_0019"
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
    # V8.4 stores secret material a service has to be able to *recover*, which is
    # the opposite of the API key (hashed, and deliberately unrecoverable). The
    # ciphertext lives here rather than inline on ``provider_credentials`` for two
    # reasons: listing credentials no longer loads secret material at all, and a
    # secret can be re-encrypted (or its key version bumped) without rewriting the
    # row that references it.
    #
    # ``algorithm`` and ``key_version`` are stamped per row so a future key
    # rotation is a data migration over this table alone; nothing else needs to
    # know which key encrypted a given blob.
    op.create_table(
        "encrypted_secrets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "algorithm",
            sa.String(length=32),
            nullable=False,
            server_default="AESGCM-256",
        ),
        sa.Column(
            "key_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("secret_metadata", sa.JSON(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_encrypted_secrets_tenant_id", "encrypted_secrets", ["tenant_id"])
    op.create_index("ix_encrypted_secrets_name", "encrypted_secrets", ["name"])
    # Lookups are always "the secret called X in this tenant" (the reference path
    # resolves by primary key, the audit path by name), so the composite index is
    # the one that matters.
    op.create_index(
        "ix_encrypted_secrets_tenant_name",
        "encrypted_secrets",
        ["tenant_id", "name"],
    )


def downgrade() -> None:
    op.drop_index("ix_encrypted_secrets_tenant_name", table_name="encrypted_secrets")
    op.drop_index("ix_encrypted_secrets_name", table_name="encrypted_secrets")
    op.drop_index("ix_encrypted_secrets_tenant_id", table_name="encrypted_secrets")
    op.drop_table("encrypted_secrets")
