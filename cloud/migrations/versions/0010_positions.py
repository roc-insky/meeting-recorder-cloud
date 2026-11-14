"""owner-private interview positions

Revision ID: 0010_positions
Revises: 0009_voiceprints
Create Date: 2026-11-14 19:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_positions"
down_revision: str | None = "0009_voiceprints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_positions_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_user_id", "id", name="pk_positions"),
        sa.CheckConstraint(
            "type IN ('前端', '后端', '客户端', '测试')",
            name="ck_positions_type",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0 AND length(title) <= 200",
            name="ck_positions_title",
        ),
        sa.CheckConstraint(
            "length(btrim(requirements)) > 0"
            " AND length(requirements) <= 20000",
            name="ck_positions_requirements",
        ),
    )
    op.create_index(
        "ix_positions_owner_created",
        "positions",
        ["owner_user_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_positions_owner_created", table_name="positions")
    op.drop_table("positions")
