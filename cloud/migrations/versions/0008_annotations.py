"""meeting-relative annotations

Revision ID: 0008_annotations
Revises: 0007_resource_events
Create Date: 2026-11-14 17:55:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_annotations"
down_revision: str | None = "0007_resource_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("offset_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_annotations_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_annotations_meeting",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotations"),
        sa.CheckConstraint(
            "kind IN ('note', 'highlight', 'comment')",
            name="ck_annotations_kind",
        ),
        sa.CheckConstraint("offset_ms >= 0", name="ck_annotations_offset"),
        sa.CheckConstraint(
            "length(btrim(text)) > 0", name="ck_annotations_text"
        ),
    )
    op.create_index(
        "ix_annotations_owner_meeting_offset",
        "annotations",
        ["owner_user_id", "meeting_id", "offset_ms", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_annotations_owner_meeting_offset",
        table_name="annotations",
    )
    op.drop_table("annotations")
