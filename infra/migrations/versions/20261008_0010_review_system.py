"""add review system tables and stage invalidation markers

Revision ID: 20261008_0010
Revises: 20260923_0009
Create Date: 2026-10-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0010"
down_revision: str | None = "20260923_0009"
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
        "review_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=120), nullable=False),
        sa.Column("pipeline_key", sa.String(length=120), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_decision", sa.String(length=32), nullable=True),
        sa.Column("gate_type", sa.String(length=32), nullable=False),
        sa.Column("gates_stage_key", sa.String(length=120), nullable=True),
        sa.Column("artifact_scope", sa.JSON(), nullable=False),
        sa.Column("review_focus", sa.JSON(), nullable=False),
        sa.Column("success_criteria", sa.JSON(), nullable=False),
        sa.Column("assignee_user_id", sa.String(length=64), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.ForeignKeyConstraint(["stage_run_id"], ["stage_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_requests_tenant_id", "review_requests", ["tenant_id"])
    op.create_index("ix_review_requests_project_id", "review_requests", ["project_id"])
    op.create_index("ix_review_requests_run_id", "review_requests", ["run_id"])
    op.create_index("ix_review_requests_stage_run_id", "review_requests", ["stage_run_id"])
    op.create_index("ix_review_requests_stage_key", "review_requests", ["stage_key"])
    op.create_index("ix_review_requests_pipeline_key", "review_requests", ["pipeline_key"])
    op.create_index("ix_review_requests_status", "review_requests", ["status"])
    op.create_index("ix_review_requests_current_decision", "review_requests", ["current_decision"])
    op.create_index("ix_review_requests_assignee_user_id", "review_requests", ["assignee_user_id"])

    op.create_table(
        "review_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_request_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_display_name", sa.String(length=160), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=48), nullable=True),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["review_request_id"], ["review_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_decisions_tenant_id", "review_decisions", ["tenant_id"])
    op.create_index("ix_review_decisions_review_request_id", "review_decisions", ["review_request_id"])
    op.create_index("ix_review_decisions_run_id", "review_decisions", ["run_id"])
    op.create_index("ix_review_decisions_decision", "review_decisions", ["decision"])
    op.create_index("ix_review_decisions_actor_user_id", "review_decisions", ["actor_user_id"])
    op.create_index("ix_review_decisions_reason_code", "review_decisions", ["reason_code"])

    op.create_table(
        "review_comments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_request_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=120), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_version", sa.Integer(), nullable=True),
        sa.Column("author_user_id", sa.String(length=64), nullable=True),
        sa.Column("author_display_name", sa.String(length=160), nullable=True),
        sa.Column("severity", sa.String(length=24), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("resolved_by_decision_id", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["review_request_id"], ["review_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_comments_tenant_id", "review_comments", ["tenant_id"])
    op.create_index("ix_review_comments_review_request_id", "review_comments", ["review_request_id"])
    op.create_index("ix_review_comments_run_id", "review_comments", ["run_id"])
    op.create_index("ix_review_comments_stage_key", "review_comments", ["stage_key"])
    op.create_index("ix_review_comments_artifact_id", "review_comments", ["artifact_id"])
    op.create_index("ix_review_comments_author_user_id", "review_comments", ["author_user_id"])
    op.create_index("ix_review_comments_severity", "review_comments", ["severity"])
    op.create_index("ix_review_comments_status", "review_comments", ["status"])

    op.create_table(
        "review_action_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_request_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("stage_key", sa.String(length=120), nullable=True),
        sa.Column("artifact_type", sa.String(length=120), nullable=True),
        sa.Column("stage_run_id", sa.String(length=64), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("linked_comment_ids", sa.JSON(), nullable=False),
        sa.Column("result_action", sa.String(length=48), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("produced_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["decision_id"], ["review_decisions.id"]),
        sa.ForeignKeyConstraint(["review_request_id"], ["review_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_action_items_tenant_id", "review_action_items", ["tenant_id"])
    op.create_index(
        "ix_review_action_items_review_request_id",
        "review_action_items",
        ["review_request_id"],
    )
    op.create_index("ix_review_action_items_decision_id", "review_action_items", ["decision_id"])
    op.create_index("ix_review_action_items_run_id", "review_action_items", ["run_id"])
    op.create_index("ix_review_action_items_kind", "review_action_items", ["kind"])
    op.create_index("ix_review_action_items_stage_key", "review_action_items", ["stage_key"])
    op.create_index("ix_review_action_items_status", "review_action_items", ["status"])

    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.add_column(
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("invalidated_by_stage_key", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(sa.Column("invalidation_reason", sa.Text(), nullable=True))
    op.create_index("ix_stage_runs_invalidated_at", "stage_runs", ["invalidated_at"])


def downgrade() -> None:
    op.drop_index("ix_stage_runs_invalidated_at", table_name="stage_runs")
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.drop_column("invalidation_reason")
        batch_op.drop_column("invalidated_by_stage_key")
        batch_op.drop_column("invalidated_at")

    op.drop_index("ix_review_action_items_status", table_name="review_action_items")
    op.drop_index("ix_review_action_items_stage_key", table_name="review_action_items")
    op.drop_index("ix_review_action_items_kind", table_name="review_action_items")
    op.drop_index("ix_review_action_items_run_id", table_name="review_action_items")
    op.drop_index("ix_review_action_items_decision_id", table_name="review_action_items")
    op.drop_index("ix_review_action_items_review_request_id", table_name="review_action_items")
    op.drop_index("ix_review_action_items_tenant_id", table_name="review_action_items")
    op.drop_table("review_action_items")

    op.drop_index("ix_review_comments_status", table_name="review_comments")
    op.drop_index("ix_review_comments_severity", table_name="review_comments")
    op.drop_index("ix_review_comments_author_user_id", table_name="review_comments")
    op.drop_index("ix_review_comments_artifact_id", table_name="review_comments")
    op.drop_index("ix_review_comments_stage_key", table_name="review_comments")
    op.drop_index("ix_review_comments_run_id", table_name="review_comments")
    op.drop_index("ix_review_comments_review_request_id", table_name="review_comments")
    op.drop_index("ix_review_comments_tenant_id", table_name="review_comments")
    op.drop_table("review_comments")

    op.drop_index("ix_review_decisions_reason_code", table_name="review_decisions")
    op.drop_index("ix_review_decisions_actor_user_id", table_name="review_decisions")
    op.drop_index("ix_review_decisions_decision", table_name="review_decisions")
    op.drop_index("ix_review_decisions_run_id", table_name="review_decisions")
    op.drop_index("ix_review_decisions_review_request_id", table_name="review_decisions")
    op.drop_index("ix_review_decisions_tenant_id", table_name="review_decisions")
    op.drop_table("review_decisions")

    op.drop_index("ix_review_requests_assignee_user_id", table_name="review_requests")
    op.drop_index("ix_review_requests_current_decision", table_name="review_requests")
    op.drop_index("ix_review_requests_status", table_name="review_requests")
    op.drop_index("ix_review_requests_pipeline_key", table_name="review_requests")
    op.drop_index("ix_review_requests_stage_key", table_name="review_requests")
    op.drop_index("ix_review_requests_stage_run_id", table_name="review_requests")
    op.drop_index("ix_review_requests_run_id", table_name="review_requests")
    op.drop_index("ix_review_requests_project_id", table_name="review_requests")
    op.drop_index("ix_review_requests_tenant_id", table_name="review_requests")
    op.drop_table("review_requests")
