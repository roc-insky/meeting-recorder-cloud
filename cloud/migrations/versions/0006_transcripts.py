"""versioned transcript rows, speakers and segments

Revision ID: 0006_transcripts
Revises: 0005_derived_assets
Create Date: 2026-11-14 17:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0006_transcripts'
down_revision: str | None = '0005_derived_assets'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'transcript_versions',
        sa.Column(
            'status',
            sa.String(length=32),
            server_default='complete',
            nullable=False,
        ),
    )
    op.add_column(
        'transcript_versions',
        sa.Column(
            'empty',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )
    op.add_column(
        'transcript_versions',
        sa.Column(
            'warnings',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        'ck_transcript_versions_status',
        'transcript_versions',
        "status IN ('complete', 'completed_with_warnings', 'basic_failed')",
    )

    op.create_table(
        'meeting_speakers',
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column(
            'embedding', postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column('model_id', sa.String(length=200), nullable=True),
        sa.Column('model_version', sa.String(length=200), nullable=True),
        sa.Column('weights_sha256', sa.String(length=64), nullable=True),
        sa.Column('dimension', sa.Integer(), nullable=True),
        sa.Column('sample_rate', sa.Integer(), nullable=True),
        sa.Column('preprocess_version', sa.String(length=100), nullable=True),
        sa.Column('normalization_version', sa.String(length=100), nullable=True),
        sa.Column('calibration_version', sa.String(length=100), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'meeting_id'],
            ['meetings.owner_user_id', 'meetings.id'],
            name='fk_meeting_speakers_meeting',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'owner_user_id', 'id', name='pk_meeting_speakers'
        ),
        sa.UniqueConstraint(
            'owner_user_id',
            'meeting_id',
            'id',
            name='uq_meeting_speakers_owner_meeting_id',
        ),
    )
    op.create_index(
        'ix_meeting_speakers_owner_meeting_created',
        'meeting_speakers',
        ['owner_user_id', 'meeting_id', 'created_at'],
    )

    op.create_table(
        'transcript_speakers',
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('version_id', sa.String(length=36), nullable=False),
        sa.Column('speaker_id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('suspect', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'version_id'],
            ['transcript_versions.owner_user_id', 'transcript_versions.id'],
            name='fk_transcript_speakers_version',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'meeting_id', 'speaker_id'],
            [
                'meeting_speakers.owner_user_id',
                'meeting_speakers.meeting_id',
                'meeting_speakers.id',
            ],
            name='fk_transcript_speakers_speaker',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'owner_user_id',
            'version_id',
            'speaker_id',
            name='pk_transcript_speakers',
        ),
    )
    op.create_index(
        'ix_transcript_speakers_owner_version',
        'transcript_speakers',
        ['owner_user_id', 'version_id'],
    )

    op.create_table(
        'transcript_segments',
        sa.Column('owner_user_id', sa.String(length=36), nullable=False),
        sa.Column('version_id', sa.String(length=36), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('start_ms', sa.Integer(), nullable=False),
        sa.Column('end_ms', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('speaker_id', sa.String(length=36), nullable=True),
        sa.Column('track_id', sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ['owner_user_id', 'version_id'],
            ['transcript_versions.owner_user_id', 'transcript_versions.id'],
            name='fk_transcript_segments_version',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'owner_user_id', 'version_id', 'id', name='pk_transcript_segments'
        ),
        sa.CheckConstraint(
            'ordinal >= 0 AND start_ms >= 0 AND end_ms >= start_ms',
            name='ck_transcript_segments_order',
        ),
    )
    op.create_index(
        'ix_transcript_segments_owner_version_ordinal',
        'transcript_segments',
        ['owner_user_id', 'version_id', 'ordinal'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_transcript_segments_owner_version_ordinal',
        table_name='transcript_segments',
    )
    op.drop_table('transcript_segments')
    op.drop_index(
        'ix_transcript_speakers_owner_version',
        table_name='transcript_speakers',
    )
    op.drop_table('transcript_speakers')
    op.drop_index(
        'ix_meeting_speakers_owner_meeting_created',
        table_name='meeting_speakers',
    )
    op.drop_table('meeting_speakers')
    op.drop_constraint(
        'ck_transcript_versions_status',
        'transcript_versions',
        type_='check',
    )
    op.drop_column('transcript_versions', 'warnings')
    op.drop_column('transcript_versions', 'empty')
    op.drop_column('transcript_versions', 'status')
