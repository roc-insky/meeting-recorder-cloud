"""versioned, owner-private voiceprints

Revision ID: 0009_voiceprints
Revises: 0008_annotations
Create Date: 2026-11-14 18:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_voiceprints"
down_revision: str | None = "0008_annotations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voiceprint_profiles",
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("embedding_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=200), nullable=False),
        sa.Column("weights_sha256", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("preprocess_version", sa.String(length=100), nullable=False),
        sa.Column("normalization_version", sa.String(length=100), nullable=False),
        sa.Column("calibration_version", sa.String(length=100), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_voiceprint_profiles_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id", "id", name="pk_voiceprint_profiles"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "name",
            "version",
            name="uq_voiceprint_profiles_owner_name_version",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_voiceprint_profiles_version"
        ),
        sa.CheckConstraint(
            "dimension >= 1 AND sample_rate >= 1",
            name="ck_voiceprint_profiles_metadata",
        ),
    )
    op.create_index(
        "uq_voiceprint_profiles_active_name",
        "voiceprint_profiles",
        ["owner_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_voiceprint_profiles_owner_revoked",
        "voiceprint_profiles",
        ["owner_user_id", "revoked_at"],
    )

    op.create_table(
        "voiceprint_enrollments",
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("replace_version", sa.Integer(), nullable=True),
        sa.Column("consent", sa.Text(), nullable=False),
        sa.Column("sample_sha256", sa.String(length=64), nullable=False),
        sa.Column("sample_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("sample_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("profile_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_voiceprint_enrollments_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id", "id", name="pk_voiceprint_enrollments"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_voiceprint_enrollments_version"
        ),
        sa.CheckConstraint(
            "replace_version IS NULL OR replace_version >= 1",
            name="ck_voiceprint_enrollments_replace",
        ),
        sa.CheckConstraint(
            "sample_bytes >= 0", name="ck_voiceprint_enrollments_sample_bytes"
        ),
    )
    op.create_index(
        "ix_voiceprint_enrollments_owner_created",
        "voiceprint_enrollments",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voiceprint_enrollments_owner_created",
        table_name="voiceprint_enrollments",
    )
    op.drop_table("voiceprint_enrollments")
    op.drop_index(
        "ix_voiceprint_profiles_owner_revoked",
        table_name="voiceprint_profiles",
    )
    op.drop_index(
        "uq_voiceprint_profiles_active_name",
        table_name="voiceprint_profiles",
    )
    op.drop_table("voiceprint_profiles")
