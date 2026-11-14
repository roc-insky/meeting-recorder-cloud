"""Capture leases: at most one live recording per user.

A lease is a server-issued token (only its SHA-256 is stored) bound to one
meeting. The recorder heartbeats it while capturing; the API requires a live
lease to start a new epoch or to append to the newest epoch. An expired lease
blocks new epochs but never deletes already ACKed chunks, and chunks of older
epochs may still be uploaded inside the upload's recovery window.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from cloud.contracts import AuthContext, ConflictError, NotFoundError
from cloud.auth import require_csrf
from cloud.schema import CaptureLease, Meeting

#: Lease lifetime; the client heartbeats well inside it (design: 60s worker
#: lease, 120s capture lease so one missed beat does not end a recording).
CAPTURE_LEASE_S = 120
#: Header carrying the lease token on mutating upload requests.
LEASE_HEADER = "X-Capture-Lease"
#: Minimum token length accepted on the wire (token_urlsafe(32) is ~43 chars).
_TOKEN_MIN = 16

router = APIRouter(prefix="/api/cloud", tags=["capture-leases"])


def hash_lease_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now(ctx: AuthContext) -> datetime:
    return ctx.clock.now()


def resolve_lease(
    ctx: AuthContext,
    token: str | None,
    *,
    meeting_id: str | None = None,
    required: bool = True,
) -> CaptureLease | None:
    """Return the caller's live lease for a mutation, or raise 4xx.

    ``meeting_id`` pins the lease to the upload's meeting: a lease held for a
    different meeting cannot authorize appending to this one.
    """
    if not token:
        if required:
            raise ConflictError("capture_lease_required")
        return None
    db = ctx.require_db()
    row = db.scalar(
        select(CaptureLease).where(
            CaptureLease.token_hash == hash_lease_token(token),
            CaptureLease.owner_user_id == ctx.user_id,
        )
    )
    if row is None:
        raise NotFoundError("capture_lease_not_found")
    now = _now(ctx)
    if row.expires_at <= now:
        raise ConflictError("capture_lease_expired")
    if row.released_at is not None:
        raise ConflictError("capture_lease_released")
    if meeting_id is not None and row.meeting_id != meeting_id:
        raise ConflictError("capture_lease_mismatch")
    return row


def enforce_capture_lease(
    ctx: AuthContext, token: str | None, *, meeting_id: str
) -> None:
    """Require the lease token only while a live capture is running here.

    With no live lease for this meeting (expired, released, or never acquired)
    the caller is in the recovery path: chunks of already-registered epochs may
    still be uploaded inside the recovery window, but no *new* epoch can be
    registered (that path always resolves a live lease).
    """
    db = ctx.require_db()
    now = _now(ctx)
    live = db.scalar(
        select(CaptureLease.id).where(
            CaptureLease.owner_user_id == ctx.user_id,
            CaptureLease.meeting_id == meeting_id,
            CaptureLease.released_at.is_(None),
            CaptureLease.expires_at > now,
        )
    )
    if live is None:
        return
    resolve_lease(ctx, token, meeting_id=meeting_id)


def acquire_capture(ctx: AuthContext, meeting_id: str) -> str:
    """Start a capture lease for one of the caller's meetings; returns the token."""
    db = ctx.require_db()
    meeting = db.get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    now = _now(ctx)

    # Sweep leases that lapsed without a heartbeat so they stop blocking the
    # partial unique index. Sweeping never touches chunks.
    db.execute(
        update(CaptureLease)
        .where(
            CaptureLease.owner_user_id == ctx.user_id,
            CaptureLease.released_at.is_(None),
            CaptureLease.expires_at <= now,
        )
        .values(released_at=now)
    )
    active = db.scalar(
        select(CaptureLease.id).where(
            CaptureLease.owner_user_id == ctx.user_id,
            CaptureLease.released_at.is_(None),
        )
    )
    if active is not None:
        raise ConflictError("capture_in_progress")

    token = secrets.token_urlsafe(32)
    db.add(
        CaptureLease(
            id=str(uuid4()),
            owner_user_id=ctx.user_id,
            meeting_id=meeting_id,
            token_hash=hash_lease_token(token),
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=CAPTURE_LEASE_S),
        )
    )
    try:
        db.commit()
    except Exception as exc:  # unique index race: another request won
        db.rollback()
        raise ConflictError("capture_in_progress") from exc
    return token


def heartbeat_capture(ctx: AuthContext, token: str) -> None:
    """Extend a live lease; expiry and release are terminal for that token."""
    row = resolve_lease(ctx, token)
    now = _now(ctx)
    row.heartbeat_at = now
    row.expires_at = now + timedelta(seconds=CAPTURE_LEASE_S)
    ctx.require_db().commit()


def release_capture(ctx: AuthContext, token: str) -> None:
    """Give up the lease (recording stopped); idempotent."""
    row = resolve_lease(ctx, token)
    if row.released_at is None:
        row.released_at = _now(ctx)
        ctx.require_db().commit()


def release_live_capture(
    ctx: AuthContext, meeting_id: str, now: datetime | None = None
) -> None:
    """Mark any live lease for this meeting released; the caller commits.

    Sealing closes the recording: a finished upload must not keep blocking the
    next capture, and the lease is bound to the meeting it was acquired for.
    """
    ctx.require_db().execute(
        update(CaptureLease)
        .where(
            CaptureLease.owner_user_id == ctx.user_id,
            CaptureLease.meeting_id == meeting_id,
            CaptureLease.released_at.is_(None),
        )
        .values(released_at=now or _now(ctx))
    )


# -- HTTP surface ----------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcquireRequest(_Body):
    meeting_id: str = Field(min_length=1, max_length=64)


class LeaseTokenRequest(_Body):
    lease_token: str = Field(min_length=_TOKEN_MIN, max_length=128)


class LeaseView(_Body):
    lease_token: str
    expires_at: datetime


class LeaseExpiryView(_Body):
    expires_at: datetime


@router.post("/capture-lease", response_model=LeaseView, status_code=201)
def acquire_route(
    payload: AcquireRequest, ctx: AuthContext = Depends(require_csrf)
) -> LeaseView:
    token = acquire_capture(ctx, payload.meeting_id)
    expires_at = _now(ctx) + timedelta(seconds=CAPTURE_LEASE_S)
    return LeaseView(lease_token=token, expires_at=expires_at)


@router.post("/capture-lease/heartbeat", response_model=LeaseExpiryView)
def heartbeat_route(
    payload: LeaseTokenRequest, ctx: AuthContext = Depends(require_csrf)
) -> LeaseExpiryView:
    heartbeat_capture(ctx, payload.lease_token)
    return LeaseExpiryView(expires_at=_now(ctx) + timedelta(seconds=CAPTURE_LEASE_S))


@router.post("/capture-lease/release", status_code=204)
def release_route(
    payload: LeaseTokenRequest, ctx: AuthContext = Depends(require_csrf)
) -> Response:
    release_capture(ctx, payload.lease_token)
    return Response(status_code=204)
