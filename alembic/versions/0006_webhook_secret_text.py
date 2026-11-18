"""Widen webhook_endpoints.secret for KMS ciphertext

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "webhook_endpoints", "secret", type_=sa.Text(), existing_type=sa.String(length=128)
    )


def downgrade() -> None:
    # Fails if any stored value is longer than 128 characters (i.e. KMS-encrypted secrets exist).
    op.alter_column(
        "webhook_endpoints", "secret", type_=sa.String(length=128), existing_type=sa.Text()
    )
