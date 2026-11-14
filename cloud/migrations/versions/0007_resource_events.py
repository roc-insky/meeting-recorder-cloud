"""durable owner-scoped resource events

Revision ID: 0007_resource_events
Revises: 0006_transcripts
Create Date: 2026-11-14 17:36:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_resource_events"
down_revision: str | None = "0006_transcripts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_resource_events_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_resource_events_meeting",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resource_events"),
        sa.CheckConstraint(
            "event IN ('snapshot', 'meeting.created', 'meeting.updated',"
            " 'upload.created', 'upload.updated', 'upload.progress',"
            " 'upload.sealed', 'task.updated', 'transcript.published')",
            name="ck_resource_events_event",
        ),
    )
    op.create_index(
        "ix_resource_events_owner_meeting_id",
        "resource_events",
        ["owner_user_id", "meeting_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resource_events_owner_meeting_id",
        table_name="resource_events",
    )
    op.drop_table("resource_events")
