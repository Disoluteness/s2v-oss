"""add brand kits and style presets

Revision ID: 20260923_0009
Revises: 20260921_0008
Create Date: 2026-09-23 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0009"
down_revision: str | None = "20260921_0008"
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
        "brand_kits",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("logo_asset_id", sa.String(length=64), nullable=True),
        sa.Column("color_palette", sa.JSON(), nullable=False),
        sa.Column("typography", sa.JSON(), nullable=False),
        sa.Column("safe_area", sa.JSON(), nullable=False),
        sa.Column("brand_voice", sa.JSON(), nullable=False),
        sa.Column("banned_terms", sa.JSON(), nullable=False),
        sa.Column("brand_metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_kits_project_id", "brand_kits", ["project_id"])
    op.create_index("ix_brand_kits_status", "brand_kits", ["status"])
    op.create_index("ix_brand_kits_tenant_id", "brand_kits", ["tenant_id"])

    op.create_table(
        "style_presets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("brand_kit_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("style_key", sa.String(length=120), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=True),
        sa.Column("subtitle_style", sa.JSON(), nullable=False),
        sa.Column("visual_style", sa.JSON(), nullable=False),
        sa.Column("tone", sa.JSON(), nullable=False),
        sa.Column("prompt_guidelines", sa.JSON(), nullable=False),
        sa.Column("render_defaults", sa.JSON(), nullable=False),
        sa.Column("provider_preferences", sa.JSON(), nullable=False),
        sa.Column("guardrails", sa.JSON(), nullable=False),
        sa.Column("preset_metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["brand_kit_id"], ["brand_kits.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_style_presets_brand_kit_id", "style_presets", ["brand_kit_id"])
    op.create_index("ix_style_presets_is_default", "style_presets", ["is_default"])
    op.create_index("ix_style_presets_project_id", "style_presets", ["project_id"])
    op.create_index("ix_style_presets_status", "style_presets", ["status"])
    op.create_index("ix_style_presets_style_key", "style_presets", ["style_key"])
    op.create_index("ix_style_presets_tenant_id", "style_presets", ["tenant_id"])

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column("default_style_preset_id", sa.String(length=64), nullable=True)
        )
    op.create_index(
        "ix_projects_default_style_preset_id",
        "projects",
        ["default_style_preset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_default_style_preset_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("default_style_preset_id")

    op.drop_index("ix_style_presets_tenant_id", table_name="style_presets")
    op.drop_index("ix_style_presets_style_key", table_name="style_presets")
    op.drop_index("ix_style_presets_status", table_name="style_presets")
    op.drop_index("ix_style_presets_project_id", table_name="style_presets")
    op.drop_index("ix_style_presets_is_default", table_name="style_presets")
    op.drop_index("ix_style_presets_brand_kit_id", table_name="style_presets")
    op.drop_table("style_presets")

    op.drop_index("ix_brand_kits_tenant_id", table_name="brand_kits")
    op.drop_index("ix_brand_kits_status", table_name="brand_kits")
    op.drop_index("ix_brand_kits_project_id", table_name="brand_kits")
    op.drop_table("brand_kits")
