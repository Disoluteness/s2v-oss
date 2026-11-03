"""initial schema

Revision ID: 20260915_0001
Revises:
Create Date: 2026-09-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0001"
down_revision: str | None = None
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
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("plan", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("default_language", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])

    op.create_table(
        "production_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("pipeline_key", sa.String(length=120), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("current_stage_key", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("target_platform", sa.String(length=64), nullable=True),
        sa.Column("style_key", sa.String(length=120), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(10, 4), nullable=True),
        sa.Column("actual_cost", sa.Numeric(10, 4), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_production_runs_pipeline_key", "production_runs", ["pipeline_key"])
    op.create_index("ix_production_runs_project_id", "production_runs", ["project_id"])
    op.create_index("ix_production_runs_status", "production_runs", ["status"])
    op.create_index("ix_production_runs_tenant_id", "production_runs", ["tenant_id"])

    op.create_table(
        "stage_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=120), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False),
        sa.Column("output_artifact_types", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stage_runs_run_id", "stage_runs", ["run_id"])
    op.create_index("ix_stage_runs_stage_key", "stage_runs", ["stage_key"])
    op.create_index("ix_stage_runs_status", "stage_runs", ["status"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("stage_key", sa.String(length=120), nullable=False),
        sa.Column("artifact_type", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_artifact_type", "artifacts", ["artifact_type"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_index("ix_artifacts_stage_key", "artifacts", ["stage_key"])

    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("asset_type", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=True),
        sa.Column("tool_key", sa.String(length=120), nullable=True),
        sa.Column("storage_url", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("asset_metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assets_asset_type", "assets", ["asset_type"])
    op.create_index("ix_assets_run_id", "assets", ["run_id"])

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("tool_key", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cost", sa.Numeric(10, 4), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("external_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_invocations_provider", "tool_invocations", ["provider"])
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"])
    op.create_index("ix_tool_invocations_tool_key", "tool_invocations", ["tool_key"])


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_tool_key", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_id", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_provider", table_name="tool_invocations")
    op.drop_table("tool_invocations")

    op.drop_index("ix_assets_run_id", table_name="assets")
    op.drop_index("ix_assets_asset_type", table_name="assets")
    op.drop_table("assets")

    op.drop_index("ix_artifacts_stage_key", table_name="artifacts")
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_index("ix_artifacts_artifact_type", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_stage_runs_status", table_name="stage_runs")
    op.drop_index("ix_stage_runs_stage_key", table_name="stage_runs")
    op.drop_index("ix_stage_runs_run_id", table_name="stage_runs")
    op.drop_table("stage_runs")

    op.drop_index("ix_production_runs_tenant_id", table_name="production_runs")
    op.drop_index("ix_production_runs_status", table_name="production_runs")
    op.drop_index("ix_production_runs_project_id", table_name="production_runs")
    op.drop_index("ix_production_runs_pipeline_key", table_name="production_runs")
    op.drop_table("production_runs")

    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_table("projects")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_table("tenants")
