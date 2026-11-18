"""Review workflow: field queries, append-only audit log, edit tracking

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

query_status = sa.Enum("open", "answered", name="query_status")

# Enforced in the database, not only in application code: no session, bug or ad-hoc SQL can
# rewrite history. Row-level for UPDATE/DELETE, statement-level for TRUNCATE.
APPEND_ONLY_FUNCTION = """
CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "human_edited_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("documents", sa.Column("text_content", sa.Text(), nullable=True))
    op.create_index("ix_submissions_status_stage", "submissions", ["status", "stage"])

    op.create_table(
        "field_queries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=100), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", query_status, nullable=False),
        sa.Column("raised_by", sa.Uuid(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.Uuid(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_field_queries_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raised_by"], ["users.id"], name="fk_field_queries_raised_by_users"
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"], ["users.id"], name="fk_field_queries_answered_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_field_queries"),
    )
    op.create_index("ix_field_queries_submission_id", "field_queries", ["submission_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], name="fk_audit_events_submission_id_submissions"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_audit_events_actor_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint("seq", name="uq_audit_events_seq"),
    )
    op.create_index("ix_audit_events_submission_id", "audit_events", ["submission_id"])

    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER audit_events_no_update_or_delete BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER audit_events_no_update_or_delete ON audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.drop_index("ix_audit_events_submission_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_field_queries_submission_id", table_name="field_queries")
    op.drop_table("field_queries")
    query_status.drop(op.get_bind())
    op.drop_index("ix_submissions_status_stage", table_name="submissions")
    op.drop_column("documents", "text_content")
    op.drop_column("submissions", "human_edited_fields")
