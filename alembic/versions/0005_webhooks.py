"""Webhooks: endpoints, the event outbox and deliveries

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

delivery_status = sa.Enum("pending", "delivering", "succeeded", "failed", name="delivery_status")


def timestamp(name: str, default: sa.sql.functions.Function[Any]) -> sa.Column[datetime]:
    """A non-null timezone-aware timestamp with a database-side default."""
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=False)


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        timestamp("created_at", sa.func.now()),
        timestamp("updated_at", sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_endpoints"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        timestamp("created_at", sa.func.clock_timestamp()),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("status", delivery_status, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        timestamp("next_attempt_at", sa.func.now()),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        timestamp("created_at", sa.func.clock_timestamp()),
        timestamp("updated_at", sa.func.now()),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["webhook_events.id"],
            name="fk_webhook_deliveries_event_id_webhook_events",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            name="fk_webhook_deliveries_endpoint_id_webhook_endpoints",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_deliveries"),
    )
    op.create_index(
        "ix_webhook_deliveries_due", "webhook_deliveries", ["status", "next_attempt_at"]
    )
    op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_endpoint_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_events")
    op.drop_table("webhook_endpoints")
    delivery_status.drop(op.get_bind())
