"""add asset library collections and items

Revision ID: 20261009_0011
Revises: 20261008_0010
Create Date: 2026-10-09 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_0011"
down_revision: str | None = "20261008_0010"
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
        "asset_collections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_run_id", sa.String(length=64), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("collection_metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_collections_tenant_id", "asset_collections", ["tenant_id"])
    op.create_index("ix_asset_collections_project_id", "asset_collections", ["project_id"])
    op.create_index("ix_asset_collections_kind", "asset_collections", ["kind"])
    op.create_index("ix_asset_collections_status", "asset_collections", ["status"])
    op.create_index("ix_asset_collections_source_run_id", "asset_collections", ["source_run_id"])
    op.create_index("ix_asset_collections_is_default", "asset_collections", ["is_default"])

    # ``usage_role`` / ``license_terms`` avoid the MySQL reserved word ``role`` and
    # keep the licensing payload clearly separate from the asset's own metadata.
    op.create_table(
        "asset_collection_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("collection_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("usage_role", sa.String(length=120), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("license_terms", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["collection_id"], ["asset_collections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id",
            "asset_id",
            name="uq_asset_collection_items_collection_asset",
        ),
    )
    op.create_index("ix_asset_collection_items_tenant_id", "asset_collection_items", ["tenant_id"])
    op.create_index(
        "ix_asset_collection_items_collection_id", "asset_collection_items", ["collection_id"]
    )
    op.create_index("ix_asset_collection_items_asset_id", "asset_collection_items", ["asset_id"])
    op.create_index("ix_asset_collection_items_usage_role", "asset_collection_items", ["usage_role"])
    op.create_index("ix_asset_collection_items_status", "asset_collection_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_asset_collection_items_status", table_name="asset_collection_items")
    op.drop_index("ix_asset_collection_items_usage_role", table_name="asset_collection_items")
    op.drop_index("ix_asset_collection_items_asset_id", table_name="asset_collection_items")
    op.drop_index("ix_asset_collection_items_collection_id", table_name="asset_collection_items")
    op.drop_index("ix_asset_collection_items_tenant_id", table_name="asset_collection_items")
    op.drop_table("asset_collection_items")

    op.drop_index("ix_asset_collections_is_default", table_name="asset_collections")
    op.drop_index("ix_asset_collections_source_run_id", table_name="asset_collections")
    op.drop_index("ix_asset_collections_status", table_name="asset_collections")
    op.drop_index("ix_asset_collections_kind", table_name="asset_collections")
    op.drop_index("ix_asset_collections_project_id", table_name="asset_collections")
    op.drop_index("ix_asset_collections_tenant_id", table_name="asset_collections")
    op.drop_table("asset_collections")
