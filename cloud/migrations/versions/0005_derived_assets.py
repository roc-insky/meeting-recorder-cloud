"""durable derived audio assets for one frozen input version

Revision ID: 0005_derived_assets
Revises: 0004_processing_tasks
Create Date: 2026-11-14 16:52:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0005_derived_assets'
down_revision: str | None = '0004_processing_tasks'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'derived_assets',
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('input_version', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('track_id', sa.String(length=64), nullable=True),
        sa.Column('object_key', sa.String(length=512), nullable=False),
        sa.Column('content_type', sa.String(length=64), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('duration_ms', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'meeting_id'],
            ['meetings.owner_user_id', 'meetings.id'],
            name='fk_derived_assets_meeting',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('owner_user_id', 'id', name='pk_derived_assets'),
        sa.UniqueConstraint('object_key', name='uq_derived_assets_object_key'),
        sa.CheckConstraint(
            "kind IN ('mix', 'track')",
            name='ck_derived_assets_kind',
        ),
        sa.CheckConstraint(
            "(kind = 'mix' AND track_id IS NULL)"
            " OR (kind = 'track' AND track_id IS NOT NULL)",
            name='ck_derived_assets_track',
        ),
        sa.CheckConstraint(
            'input_version >= 0 AND bytes > 0 AND duration_ms >= 0',
            name='ck_derived_assets_metrics',
        ),
    )
    op.create_index(
        'uq_derived_assets_mix',
        'derived_assets',
        ['owner_user_id', 'meeting_id', 'input_version'],
        unique=True,
        postgresql_where=sa.text("kind = 'mix'"),
    )
    op.create_index(
        'uq_derived_assets_track',
        'derived_assets',
        ['owner_user_id', 'meeting_id', 'input_version', 'track_id'],
        unique=True,
        postgresql_where=sa.text("kind = 'track'"),
    )
    op.create_index(
        'ix_derived_assets_owner_meeting',
        'derived_assets',
        ['owner_user_id', 'meeting_id', 'input_version'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_derived_assets_owner_meeting',
        table_name='derived_assets',
    )
    op.drop_index(
        'uq_derived_assets_track',
        table_name='derived_assets',
    )
    op.drop_index(
        'uq_derived_assets_mix',
        table_name='derived_assets',
    )
    op.drop_table('derived_assets')
