"""Owner-scoped meeting operations.

Every query carries owner_user_id == ctx.user_id: there is no path that
looks a row up by id alone. Inaccessible rows are reported as 404 (never 403),
so a caller cannot probe for someone else's meeting ids.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select

from cloud import events
from cloud.auth import require_csrf, require_user
from cloud.contracts import (
    AuthContext,
    CreateMeetingRequest,
    InvalidRequestError,
    MeetingPage,
    MeetingView,
    NotFoundError,
    UpdateMeetingRequest,
    b64url_decode,
    b64url_encode,
    iso_utc,
    parse_iso_utc,
)
from cloud.schema import Meeting

router = APIRouter(prefix="/api/cloud", tags=["meetings"])

_MAX_CURSOR_LENGTH = 512
_MAX_TITLE_LENGTH = 200


def _utcnow(ctx: AuthContext) -> datetime:
    return ctx.clock.now()


def _clean_title(title: str) -> str:
    cleaned = title.strip()
    if not 1 <= len(cleaned) <= _MAX_TITLE_LENGTH:
        raise InvalidRequestError("invalid_title")
    return cleaned


def _clean_mode(mode: str) -> str:
    if mode not in ("mic", "dual"):
        raise InvalidRequestError("invalid_mode")
    return mode


def _clean_speakers(speakers: int | None) -> int | None:
    if speakers is None:
        return None
    if isinstance(speakers, bool) or not isinstance(speakers, int):
        raise InvalidRequestError("invalid_speakers")
    if not 1 <= speakers <= 20:
        raise InvalidRequestError("invalid_speakers")
    return speakers


def _view(meeting: Meeting) -> MeetingView:
    return MeetingView(
        id=meeting.id,
        title=meeting.title,
        mode=meeting.mode,
        generation=meeting.generation,
        current_transcript_id=meeting.current_transcript_id,
        deleted_at=iso_utc(meeting.deleted_at) if meeting.deleted_at is not None else None,
    )


def encode_cursor(created_at: datetime, meeting_id: str, secret_key: str) -> str:
    payload = json.dumps(
        {"created_at": iso_utc(created_at), "id": meeting_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    body = b64url_encode(payload)
    signature = b64url_encode(
        hmac.new(secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def decode_cursor(cursor: str, secret_key: str) -> tuple[datetime, str]:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidRequestError("invalid_cursor")
    try:
        body, signature = cursor.split(".", 1)
        expected = b64url_encode(
            hmac.new(secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not secrets.compare_digest(signature, expected):
            raise InvalidRequestError("invalid_cursor")
        payload = json.loads(b64url_decode(body))
        created_at = parse_iso_utc(str(payload["created_at"]))
        meeting_id = str(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidRequestError("invalid_cursor") from exc
    if not meeting_id or len(meeting_id) > 64:
        raise InvalidRequestError("invalid_cursor")
    return created_at, meeting_id


def create_meeting(
    ctx: AuthContext, title: str, mode: str = "mic", speakers: int | None = None
) -> MeetingView:
    now = _utcnow(ctx)
    meeting = Meeting(
        owner_user_id=ctx.user_id,
        id=str(uuid4()),
        title=_clean_title(title),
        mode=_clean_mode(mode),
        speakers=_clean_speakers(speakers),
        generation=0,
        created_at=now,
        updated_at=now,
    )
    db = ctx.require_db()
    db.add(meeting)
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=meeting.id,
        event="meeting.created",
        data={
            "meeting_id": meeting.id,
            "mode": meeting.mode,
            "generation": meeting.generation,
            "created_at": iso_utc(meeting.created_at),
        },
        now=now,
    )
    db.commit()
    return _view(meeting)


def get_meeting(ctx: AuthContext, meeting_id: str) -> MeetingView:
    meeting = _find_meeting(ctx, meeting_id)
    return _view(meeting)


def rename_meeting(ctx: AuthContext, meeting_id: str, title: str) -> MeetingView:
    meeting = _find_meeting(ctx, meeting_id)
    meeting.title = _clean_title(title)
    meeting.updated_at = _utcnow(ctx)
    db = ctx.require_db()
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=meeting.id,
        event="meeting.updated",
        data={
            "meeting_id": meeting.id,
            "generation": meeting.generation,
            "updated_at": iso_utc(meeting.updated_at),
        },
        now=meeting.updated_at,
    )
    db.commit()
    return _view(meeting)


def list_meetings(
    ctx: AuthContext, cursor: str | None = None, limit: int = 20
) -> MeetingPage:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InvalidRequestError("invalid_limit")
    db = ctx.require_db()
    statement = select(Meeting).where(
        Meeting.owner_user_id == ctx.user_id, Meeting.deleted_at.is_(None)
    )
    if cursor is not None:
        created_at, meeting_id = decode_cursor(cursor, ctx.settings.secret_key)
        statement = statement.where(
            or_(
                Meeting.created_at < created_at,
                and_(Meeting.created_at == created_at, Meeting.id < meeting_id),
            )
        )
    statement = statement.order_by(Meeting.created_at.desc(), Meeting.id.desc()).limit(
        limit + 1
    )
    rows = list(db.scalars(statement).all())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id, ctx.settings.secret_key)
    return MeetingPage(items=[_view(row) for row in rows], next_cursor=next_cursor)


def _find_meeting(ctx: AuthContext, meeting_id: str) -> Meeting:
    meeting = ctx.require_db().get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError()
    return meeting


# -- routes ----------------------------------------------------------------


@router.post("/meetings", response_model=MeetingView, status_code=201)
def create_meeting_route(
    payload: CreateMeetingRequest, ctx: AuthContext = Depends(require_csrf)
) -> MeetingView:
    return create_meeting(
        ctx, title=payload.title, mode=payload.mode, speakers=payload.speakers
    )


@router.get("/meetings", response_model=MeetingPage)
def list_meetings_route(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(require_user),
) -> MeetingPage:
    return list_meetings(ctx, cursor=cursor, limit=limit)


@router.get("/meetings/{meeting_id}", response_model=MeetingView)
def get_meeting_route(
    meeting_id: str, ctx: AuthContext = Depends(require_user)
) -> MeetingView:
    return get_meeting(ctx, meeting_id)


@router.patch("/meetings/{meeting_id}", response_model=MeetingView)
def rename_meeting_route(
    meeting_id: str,
    payload: UpdateMeetingRequest,
    ctx: AuthContext = Depends(require_csrf),
) -> MeetingView:
    return rename_meeting(ctx, meeting_id, payload.title)
