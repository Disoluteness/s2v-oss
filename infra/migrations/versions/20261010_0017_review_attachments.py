"""add review attachments

Revision ID: 20261010_0017
Revises: 20261010_0016
Create Date: 2026-10-10 16:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_0017"
down_revision: str | None = "20261010_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One table is all V6.6 needs: an attachment is a *mount* (review request ↔
    # asset evidence). The row references the original asset instead of copying
    # it — the evidence must point at the same storage the decision saw. The
    # unique pair makes idempotent mounting a database guarantee, not just an
    # application-level lookup.
    op.create_table(
        "review_attachments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_request_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("added_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("added_by_display_name", sa.String(length=160), nullable=True),
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
        sa.ForeignKeyConstraint(["review_request_id"], ["review_requests.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"]),
        sa.UniqueConstraint(
            "review_request_id",
            "asset_id",
            name="uq_review_attachments_request_asset",
        ),
    )
    op.create_index("ix_review_attachments_tenant_id", "review_attachments", ["tenant_id"])
    op.create_index("ix_review_attachments_review_request_id", "review_attachments", ["review_request_id"])
    op.create_index("ix_review_attachments_run_id", "review_attachments", ["run_id"])
    op.create_index("ix_review_attachments_asset_id", "review_attachments", ["asset_id"])
    op.create_index("ix_review_attachments_added_by_user_id", "review_attachments", ["added_by_user_id"])


def downgrade() -> None:
    op.drop_index("ix_review_attachments_added_by_user_id", table_name="review_attachments")
    op.drop_index("ix_review_attachments_asset_id", table_name="review_attachments")
    op.drop_index("ix_review_attachments_run_id", table_name="review_attachments")
    op.drop_index("ix_review_attachments_review_request_id", table_name="review_attachments")
    op.drop_index("ix_review_attachments_tenant_id", table_name="review_attachments")
    op.drop_table("review_attachments")
