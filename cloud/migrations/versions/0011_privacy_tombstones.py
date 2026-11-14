"""account deletion and tombstone replay support

Revision ID: 0011_privacy_tombstones
Revises: 0010_positions
Create Date: 2026-11-14 19:45:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_privacy_tombstones"
down_revision: str | None = "0010_positions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])
    op.create_index("ix_meetings_deleted_at", "meetings", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_meetings_deleted_at", table_name="meetings")
    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")
