"""durable processing tasks and publish-once transcript versions

Revision ID: 0004_processing_tasks
Revises: 0003_sealing
Create Date: 2026-11-14 16:40:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0004_processing_tasks'
down_revision: str | None = '0003_sealing'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'processing_tasks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=True),
        sa.Column('upload_session_id', sa.String(length=36), nullable=True),
        sa.Column('voiceprint_ref', sa.String(length=64), nullable=True),
        sa.Column('input_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('generation', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('attempt', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('status', sa.String(length=16), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column('lease_token_hash', sa.String(length=64), nullable=True),
        sa.Column('lease_worker_id', sa.String(length=64), nullable=True),
        sa.Column('lease_deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_acquired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name='fk_processing_tasks_owner', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'meeting_id'],
            ['meetings.owner_user_id', 'meetings.id'],
            name='fk_processing_tasks_meeting',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'upload_session_id'],
            ['upload_sessions.owner_user_id', 'upload_sessions.id'],
            name='fk_processing_tasks_upload',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name='pk_processing_tasks'),
        sa.UniqueConstraint('owner_user_id', 'id', name='uq_processing_tasks_owner_id'),
        sa.CheckConstraint(
            "kind IN ('final', 'live', 'voiceprint')",
            name='ck_processing_tasks_kind',
        ),
        sa.CheckConstraint(
            "(kind = 'voiceprint' AND meeting_id IS NULL"
            ' AND upload_session_id IS NULL AND voiceprint_ref IS NOT NULL)'
            " OR (kind IN ('final', 'live') AND meeting_id IS NOT NULL"
            ' AND upload_session_id IS NOT NULL AND voiceprint_ref IS NULL)',
            name='ck_processing_tasks_link',
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'running', 'completed', 'failed', 'cancelled')",
            name='ck_processing_tasks_status',
        ),
        sa.CheckConstraint(
            'input_version >= 0 AND generation >= 0 AND attempt >= 0',
            name='ck_processing_tasks_counters',
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (lease_token_hash IS NOT NULL"
            ' AND lease_worker_id IS NOT NULL AND lease_deadline IS NOT NULL)',
            name='ck_processing_tasks_lease',
        ),
    )
    op.create_index(
        'ix_processing_tasks_status_created',
        'processing_tasks',
        ['status', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_processing_tasks_owner_status',
        'processing_tasks',
        ['owner_user_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_processing_tasks_meeting_generation',
        'processing_tasks',
        ['meeting_id', 'generation'],
        unique=False,
    )

    op.create_table(
        'transcript_versions',
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=True),
        sa.Column('input_version', sa.Integer(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'meeting_id'],
            ['meetings.owner_user_id', 'meetings.id'],
            name='fk_transcript_versions_meeting',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'task_id'],
            ['processing_tasks.owner_user_id', 'processing_tasks.id'],
            name='fk_transcript_versions_task',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('owner_user_id', 'id', name='pk_transcript_versions'),
        sa.UniqueConstraint('task_id', name='uq_transcript_versions_task'),
        sa.CheckConstraint(
            'input_version >= 0 AND generation >= 0',
            name='ck_transcript_versions_counters',
        ),
    )
    op.create_index(
        'ix_transcript_versions_owner_meeting_created',
        'transcript_versions',
        ['owner_user_id', 'meeting_id', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_transcript_versions_owner_meeting_created',
        table_name='transcript_versions',
    )
    op.drop_table('transcript_versions')
    op.drop_index(
        'ix_processing_tasks_meeting_generation',
        table_name='processing_tasks',
    )
    op.drop_index(
        'ix_processing_tasks_owner_status',
        table_name='processing_tasks',
    )
    op.drop_index(
        'ix_processing_tasks_status_created',
        table_name='processing_tasks',
    )
    op.drop_table('processing_tasks')
