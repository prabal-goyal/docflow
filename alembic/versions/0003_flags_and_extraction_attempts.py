"""Add submission flags and the extraction_attempts table

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

attempt_outcome = sa.Enum("succeeded", "failed", name="attempt_outcome")


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "extraction_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=20), nullable=False),
        sa.Column("outcome", attempt_outcome, nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_extraction_attempts_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_attempts"),
    )
    op.create_index(
        "ix_extraction_attempts_submission_id", "extraction_attempts", ["submission_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_attempts_submission_id", table_name="extraction_attempts")
    op.drop_table("extraction_attempts")
    attempt_outcome.drop(op.get_bind())
    op.drop_column("submissions", "flags")
