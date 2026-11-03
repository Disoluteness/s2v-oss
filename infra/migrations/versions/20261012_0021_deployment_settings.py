"""deployment settings

Revision ID: 20261012_0021
Revises: 20261012_0020
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_0021"
down_revision: str | None = "20261012_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The row's fixed primary key. Deployment settings describe *the install*, not
#: a tenant, so there is exactly one row and no ``tenant_id`` column — the first
#: table in this schema that is not tenant-scoped, which is why it is called out
#: here rather than discovered later. Access is still tenant-gated by the API
#: (``deployment:read`` / ``deployment:manage``), because "who may reconfigure
#: the install" is answered by the permission matrix, not by the row's shape.
DEPLOYMENT_SETTINGS_ID = "default"


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
    # V8.5 keeps the two things an operator needs to change *while the
    # deployment is being repaired* — the maintenance switch and the backup
    # bookkeeping — in the database rather than in a file, because the moment
    # you need them is exactly the moment you cannot edit ``.env`` and restart.
    # Everything else about a deployment stays in configuration, where it
    # belongs: a value that has to be the same on every replica is not a runtime
    # setting, it is a build input.
    op.create_table(
        "deployment_settings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("deployment_name", sa.String(length=200), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=True),
        sa.Column("maintenance_mode", sa.Boolean(), nullable=False),
        sa.Column("maintenance_message", sa.Text(), nullable=True),
        sa.Column("maintenance_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("maintenance_updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "backup_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="14",
        ),
        sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_backup_id", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    # Seed the singleton here rather than lazily on first read. A GET that
    # inserts would give a read-only endpoint a side effect (the same reason
    # V8.4 refused to migrate secrets from a GET), and it would also make two
    # replicas racing on the first request a real possibility. The migration
    # runs once, under a lock, before either replica serves traffic.
    deployment_settings = sa.table(
        "deployment_settings",
        sa.column("id", sa.String),
        sa.column("maintenance_mode", sa.Boolean),
        sa.column("backup_retention_days", sa.Integer),
    )
    op.bulk_insert(
        deployment_settings,
        [
            {
                "id": DEPLOYMENT_SETTINGS_ID,
                "maintenance_mode": False,
                "backup_retention_days": 14,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("deployment_settings")
