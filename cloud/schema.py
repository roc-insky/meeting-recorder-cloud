"""Cloud schema: users, sessions, meetings, uploads and capture leases.

Task 4 created users, sessions and meetings; Task 5 adds the durable upload
pipeline (upload_sessions, upload_epochs, upload_tracks, upload_epoch_tracks,
chunks) and capture_leases. Transcripts, tasks, events and annotations arrive
with later migrations.

Every child row carries ``owner_user_id`` and is linked through a composite
foreign key to its parent's (owner_user_id, id) key, so a buggy or hostile
query cannot attach a chunk to another user's upload, epoch or track even by
guessing identifiers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cloud.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)


class AuthSession(Base):
    """Server-side session; the cookie only carries an opaque random token.

    A row starts life as a pending login (user_id NULL plus state/expiry), then
    either becomes an authenticated session (token rotated) or stays pending
    until it is consumed, expired or revoked.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Meeting(Base):
    __tablename__ = "meetings"

    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    speakers: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    current_transcript_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_meetings_owner_created_id", "owner_user_id", "created_at", "id"),
        CheckConstraint("mode IN ('mic', 'dual')", name="ck_meetings_mode"),
        CheckConstraint(
            "speakers IS NULL OR (speakers >= 1 AND speakers <= 20)",
            name="ck_meetings_speakers",
        ),
        CheckConstraint("generation >= 0", name="ck_meetings_generation"),
    )


class UploadSession(Base):
    """One recording attempt for a meeting; chunks are appended while open.

    ``id`` is the client-visible upload identifier and is server generated.
    Sealing (Task 7) flips ``status`` to sealed so no further chunks land.
    """

    __tablename__ = "upload_sessions"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'open'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set with ``status='sealed'``: complete only when every track was
    #: verified against the client manifest; otherwise visibly partial.
    sealed_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Chunk count frozen at seal time (design §7.2 input_version).
    input_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Explicit gaps recorded at seal time, kept with the recording forever.
    seal_gaps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_upload_sessions_meeting",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('open', 'sealed', 'cancelled')",
            name="ck_upload_sessions_status",
        ),
        CheckConstraint(
            "sealed_status IS NULL OR sealed_status IN ('complete', 'partial')",
            name="ck_upload_sessions_sealed_status",
        ),
        CheckConstraint("input_version >= 0", name="ck_upload_sessions_input_version"),
        Index("ix_upload_sessions_owner_created", "owner_user_id", "created_at"),
        Index("ix_upload_sessions_status_created", "status", "created_at"),
    )


class UploadEpoch(Base):
    """A capture epoch: one continuous clock origin for one upload.

    A refresh/re-auth starts a new epoch instead of splicing a stale clock, so
    ``offset_ms`` and ``uncertainty_ms`` describe that origin explicitly.
    """

    __tablename__ = "upload_epochs"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upload_session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    offset_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uncertainty_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id"],
            ["upload_sessions.owner_user_id", "upload_sessions.id"],
            name="fk_upload_epochs_session",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "offset_ms >= 0 AND uncertainty_ms >= 0",
            name="ck_upload_epochs_times",
        ),
    )


class UploadTrack(Base):
    """A registered track (mic/system) for one upload session."""

    __tablename__ = "upload_tracks"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upload_session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    codec: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id"],
            ["upload_sessions.owner_user_id", "upload_sessions.id"],
            name="fk_upload_tracks_session",
            ondelete="CASCADE",
        ),
        CheckConstraint("source IN ('mic', 'system')", name="ck_upload_tracks_source"),
        CheckConstraint("sample_rate > 0", name="ck_upload_tracks_sample_rate"),
    )


class UploadEpochTrack(Base):
    """Declares which tracks an epoch contains (mic-only downgrade included)."""

    __tablename__ = "upload_epoch_tracks"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upload_session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id", "epoch_id"],
            [
                "upload_epochs.owner_user_id",
                "upload_epochs.upload_session_id",
                "upload_epochs.id",
            ],
            name="fk_upload_epoch_tracks_epoch",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id", "track_id"],
            [
                "upload_tracks.owner_user_id",
                "upload_tracks.upload_session_id",
                "upload_tracks.id",
            ],
            name="fk_upload_epoch_tracks_track",
            ondelete="CASCADE",
        ),
    )


class Chunk(Base):
    """One ACKed audio chunk; the row is the durable acknowledgement.

    The identity (owner, upload, epoch, track, sequence) is unique and doubles
    as the idempotency key: retrying the same chunk re-ACKs the existing row
    instead of writing a second copy. ``object_key`` is server generated from
    that identity, never supplied by a client.
    """

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    upload_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    epoch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    track_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    codec: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id", "epoch_id", "track_id"],
            [
                "upload_epoch_tracks.owner_user_id",
                "upload_epoch_tracks.upload_session_id",
                "upload_epoch_tracks.epoch_id",
                "upload_epoch_tracks.track_id",
            ],
            name="fk_chunks_epoch_track",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "owner_user_id",
            "upload_session_id",
            "epoch_id",
            "track_id",
            "sequence",
            name="uq_chunks_identity",
        ),
        UniqueConstraint("object_key", name="uq_chunks_object_key"),
        CheckConstraint("sequence >= 0", name="ck_chunks_sequence"),
        CheckConstraint("start_ms >= 0 AND end_ms > start_ms", name="ck_chunks_time"),
        CheckConstraint("byte_size > 0", name="ck_chunks_byte_size"),
    )


class CaptureLease(Base):
    """A server-issued capture lease; at most one unreleased lease per user."""

    __tablename__ = "capture_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_capture_leases_meeting",
            ondelete="CASCADE",
        ),
        UniqueConstraint("token_hash", name="uq_capture_leases_token_hash"),
        CheckConstraint("expires_at > acquired_at", name="ck_capture_leases_window"),
        Index(
            "uq_capture_leases_active_owner",
            "owner_user_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_capture_leases_owner_expires", "owner_user_id", "expires_at"),
    )

class ProcessingTask(Base):
    """A durable unit of deferred work, claimed under a fenced lease.

    Task 8 created final transcription tasks; live transcription and voiceprint
    enrolment reuse the same queue with their own link. owner_user_id is part
    of every lookup and of the composite keys, so a forged task id or queue
    message cannot reach another user's payload. lease_token_hash stores only
    the SHA-256 of the server-generated token, like capture leases.
    """

    __tablename__ = "processing_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", name="fk_processing_tasks_owner", ondelete="CASCADE"),
        nullable=False,
    )
    meeting_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    upload_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: Voiceprint enrolment links its own sample/template, never a meeting.
    voiceprint_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Chunk count frozen when the input was sealed (design section 7.2).
    input_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Meeting generation this result may claim; superseding bumps it.
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Number of leases handed out so far (bounded retry fence).
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'waiting'")
    )
    lease_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Safe, fixed error code only; never an exception message.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_processing_tasks_meeting",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "upload_session_id"],
            ["upload_sessions.owner_user_id", "upload_sessions.id"],
            name="fk_processing_tasks_upload",
            ondelete="CASCADE",
        ),
        # Composite key target for owner-scoped children (transcript versions).
        UniqueConstraint("owner_user_id", "id", name="uq_processing_tasks_owner_id"),
        CheckConstraint(
            "kind IN ('final', 'live', 'voiceprint')",
            name="ck_processing_tasks_kind",
        ),
        CheckConstraint(
            "(kind = 'voiceprint' AND meeting_id IS NULL"
            " AND upload_session_id IS NULL AND voiceprint_ref IS NOT NULL)"
            " OR (kind IN ('final', 'live') AND meeting_id IS NOT NULL"
            " AND upload_session_id IS NOT NULL AND voiceprint_ref IS NULL)",
            name="ck_processing_tasks_link",
        ),
        CheckConstraint(
            "status IN ('waiting', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_processing_tasks_status",
        ),
        CheckConstraint(
            "input_version >= 0 AND generation >= 0 AND attempt >= 0",
            name="ck_processing_tasks_counters",
        ),
        CheckConstraint(
            "status <> 'running' OR (lease_token_hash IS NOT NULL"
            " AND lease_worker_id IS NOT NULL AND lease_deadline IS NOT NULL)",
            name="ck_processing_tasks_lease",
        ),
        Index("ix_processing_tasks_status_created", "status", "created_at"),
        Index("ix_processing_tasks_owner_status", "owner_user_id", "status"),
        Index(
            "ix_processing_tasks_meeting_generation", "meeting_id", "generation"
        ),
    )


class TranscriptVersion(Base):
    """One published transcript result; a task can be consumed exactly once.

    Task 8 needs a durable publish target so publish_result can prove that a
    stale, repeated or forged publish writes nothing. Task 10 extends this row
    with segment/speaker tables; the payload keeps the task result as it was
    published. task_id is unique, so a second publish for one task can never
    create a duplicate transcript version.
    """

    __tablename__ = "transcript_versions"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="complete"
    )
    empty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_transcript_versions_meeting",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "task_id"],
            ["processing_tasks.owner_user_id", "processing_tasks.id"],
            name="fk_transcript_versions_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint("task_id", name="uq_transcript_versions_task"),
        CheckConstraint(
            "input_version >= 0 AND generation >= 0",
            name="ck_transcript_versions_counters",
        ),
        CheckConstraint(
            "status IN ('complete', 'completed_with_warnings', 'basic_failed')",
            name="ck_transcript_versions_status",
        ),
        Index(
            "ix_transcript_versions_owner_meeting_created",
            "owner_user_id",
            "meeting_id",
            "created_at",
        ),
    )


class MeetingSpeaker(Base):
    """A meeting-scoped speaker identity, optionally with an embedding.

    Rows are created by the enhancement pipeline (with model metadata and a
    vector) or by an older hand-published transcript (without them).  Identity
    is per meeting and owner; ids are never stable across meetings.
    """

    __tablename__ = "meeting_speakers"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weights_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preprocess_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalization_version: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    calibration_version: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_meeting_speakers_meeting",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "owner_user_id",
            "meeting_id",
            "id",
            name="uq_meeting_speakers_owner_meeting_id",
        ),
        Index(
            "ix_meeting_speakers_owner_meeting_created",
            "owner_user_id",
            "meeting_id",
            "created_at",
        ),
    )


class TranscriptSpeaker(Base):
    """A speaker identity as published in one transcript version."""

    __tablename__ = "transcript_speakers"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    speaker_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    suspect: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "version_id"],
            ["transcript_versions.owner_user_id", "transcript_versions.id"],
            name="fk_transcript_speakers_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id", "speaker_id"],
            [
                "meeting_speakers.owner_user_id",
                "meeting_speakers.meeting_id",
                "meeting_speakers.id",
            ],
            name="fk_transcript_speakers_speaker",
            ondelete="CASCADE",
        ),
        Index(
            "ix_transcript_speakers_owner_version",
            "owner_user_id",
            "version_id",
        ),
    )


class TranscriptSegment(Base):
    """One ordered segment of a published transcript version."""

    __tablename__ = "transcript_segments"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    speaker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    track_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "version_id"],
            ["transcript_versions.owner_user_id", "transcript_versions.id"],
            name="fk_transcript_segments_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "ordinal >= 0 AND start_ms >= 0 AND end_ms >= start_ms",
            name="ck_transcript_segments_order",
        ),
        Index(
            "ix_transcript_segments_owner_version_ordinal",
            "owner_user_id",
            "version_id",
            "ordinal",
        ),
    )


class DerivedAsset(Base):
    """A durable, owner-scoped decode product for one frozen input version.

    Task 9 decodes the sealed recording once per input_version into 16 kHz mono
    WAV work assets. The row records the private object, its hash and its
    timeline duration, so a worker restart reuses bytes instead of re-encoding,
    and so playback can serve exactly the asset the transcript was built from.
    Sources (chunks) are never replaced by these rows.
    """

    __tablename__ = "derived_assets"

    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False)
    #: Chunk count frozen at seal time; one asset set per version.
    input_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Track id for kind='track'; NULL for the single mix of a version.
    track_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "meeting_id"],
            ["meetings.owner_user_id", "meetings.id"],
            name="fk_derived_assets_meeting",
            ondelete="CASCADE",
        ),
        UniqueConstraint("object_key", name="uq_derived_assets_object_key"),
        CheckConstraint("kind IN ('mix', 'track')", name="ck_derived_assets_kind"),
        CheckConstraint(
            "(kind = 'mix' AND track_id IS NULL)"
            " OR (kind = 'track' AND track_id IS NOT NULL)",
            name="ck_derived_assets_track",
        ),
        CheckConstraint(
            "input_version >= 0 AND bytes > 0 AND duration_ms >= 0",
            name="ck_derived_assets_metrics",
        ),
        # Postgres treats NULLs as distinct, so the mix needs its own partial
        # unique index; both together express one set per input version.
        Index(
            "uq_derived_assets_mix",
            "owner_user_id",
            "meeting_id",
            "input_version",
            unique=True,
            postgresql_where=text("kind = 'mix'"),
        ),
        Index(
            "uq_derived_assets_track",
            "owner_user_id",
            "meeting_id",
            "input_version",
            "track_id",
            unique=True,
            postgresql_where=text("kind = 'track'"),
        ),
        Index(
            "ix_derived_assets_owner_meeting",
            "owner_user_id",
            "meeting_id",
            "input_version",
        ),
    )
