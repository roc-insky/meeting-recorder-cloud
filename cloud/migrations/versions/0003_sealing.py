"""seal results and frozen input versions

Revision ID: 0003_sealing
Revises: 0002_uploads
Create Date: 2026-11-14 16:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0003_sealing'
down_revision: str | None = '0002_uploads'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('upload_sessions', sa.Column('sealed_status', sa.String(length=16), nullable=True))
    op.add_column('upload_sessions', sa.Column('input_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('upload_sessions', sa.Column('seal_gaps', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.create_check_constraint(
        'ck_upload_sessions_sealed_status',
        'upload_sessions',
        "sealed_status IS NULL OR sealed_status IN ('complete', 'partial')",
    )
    op.create_check_constraint(
        'ck_upload_sessions_input_version',
        'upload_sessions',
        'input_version >= 0',
    )
    op.create_index(
        'ix_upload_sessions_status_created',
        'upload_sessions',
        ['status', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_upload_sessions_status_created', table_name='upload_sessions')
    op.drop_constraint('ck_upload_sessions_input_version', 'upload_sessions', type_='check')
    op.drop_constraint('ck_upload_sessions_sealed_status', 'upload_sessions', type_='check')
    op.drop_column('upload_sessions', 'seal_gaps')
    op.drop_column('upload_sessions', 'input_version')
    op.drop_column('upload_sessions', 'sealed_status')
