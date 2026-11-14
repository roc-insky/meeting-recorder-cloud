"""First cloud schema: users, server-side sessions and meetings.

Only the three entities Task 4 needs are created here; transcripts, tasks,
events and annotations arrive with later migrations. A meeting is keyed by
(owner_user_id, id) so even a buggy query cannot load another user's row by id
alone.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
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
