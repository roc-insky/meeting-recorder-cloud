"""postgres-shared request budgets and one decode slot per owner

Revision ID: 0012_rate_limits
Revises: 0011_privacy_tombstones
Create Date: 2026-11-14 20:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_rate_limits"
down_revision: str | None = "0011_privacy_tombstones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_rate_limits",
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('chunk_write', 'lease_write', 'task_enqueue',"
            " 'ai_call', 'playback', 'voiceprint_enroll')",
            name="ck_request_rate_limits_action",
        ),
        sa.CheckConstraint("count >= 0", name="ck_request_rate_limits_count"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_request_rate_limits_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_user_id", "action"),
    )
    op.create_table(
        "decode_slots",
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > acquired_at", name="ck_decode_slots_window"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_decode_slots_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )


def downgrade() -> None:
    op.drop_table("decode_slots")
    op.drop_table("request_rate_limits")
