"""Create submissions and documents tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

submission_status = sa.Enum(
    "uploaded",
    "extracting",
    "extraction_failed",
    "in_review",
    "query_raised",
    "approved",
    "rejected",
    name="submission_status",
)


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submitter_id", sa.Uuid(), nullable=False),
        sa.Column("status", submission_status, nullable=False),
        sa.Column("stage", sa.Integer(), nullable=True),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["submitter_id"], ["users.id"], name="fk_submissions_submitter_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submissions"),
        sa.UniqueConstraint(
            "submitter_id",
            "idempotency_key",
            name="uq_submissions_submitter_id_idempotency_key",
        ),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_documents_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("submission_id", name="uq_documents_submission_id"),
    )


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("submissions")
    submission_status.drop(op.get_bind())
