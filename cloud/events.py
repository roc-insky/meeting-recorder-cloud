"""Durable, owner-scoped resource events and the SSE replay stream.

The cursor is the ResourceEvent BigInteger identity id, not a per-meeting
counter and not a timestamp: it is assigned by PostgreSQL inside the same
transaction as the state change, so a restart replays exactly the durable feed
and a rolled-back change never leaves an event behind.

Two reads exist:

* GET /meetings/{id}/events returns a bounded EventPage. A missing or invalid
  cursor returns the caller's snapshot, and a cursor that belongs to another
  owner or meeting also returns a snapshot: it can never be used to read a
  foreign event.
* GET /meetings/{id}/events/stream is text/event-stream. Each poll opens a
  fresh database session, revalidates the server-side AuthSession and the
  meeting, then reads new events. The request-scoped session is closed before
  streaming starts, so a long-lived stream never holds a pool connection.

This module must stay out of the auth import path (cloud.auth is imported
lazily inside the dependency below) to keep the module graph acyclic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cloud import live
from cloud.contracts import (
    AuthContext,
    EventPage,
    EventView,
    NotFoundError,
    iso_utc,
)
from cloud.db import get_db
from cloud.schema import (
    AuthSession,
    Meeting,
    ProcessingTask,
    ResourceEvent,
    TranscriptVersion,
    UploadSession,
)

router = APIRouter(prefix="/api/cloud", tags=["events"])

#: A snapshot instead of a replay when more than this many events are needed.
MAX_REPLAY = 1000
SNAPSHOT_ROWS = 20


def _require_user(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Resolve the authenticated context without a module-level auth import."""
    from cloud.auth import require_user

    return require_user(request, db)


def _coerce_after(value: Any) -> int | None:
    """Return a non-negative integer cursor, or None for the snapshot path."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def record(
    db: Any,
    *,
    owner_user_id: str,
    meeting_id: str,
    event: str,
    data: dict[str, Any],
    now: datetime,
) -> ResourceEvent:
    """Add one pending event row; the caller commits with its state change."""
    row = ResourceEvent(
        owner_user_id=owner_user_id,
        meeting_id=meeting_id,
        event=event,
        data=dict(data),
        created_at=now,
    )
    db.add(row)
    return row


def _latest_cursor(db: Any, owner_user_id: str, meeting_id: str) -> int:
    value = db.scalar(
        select(func.max(ResourceEvent.id)).where(
            ResourceEvent.owner_user_id == owner_user_id,
            ResourceEvent.meeting_id == meeting_id,
        )
    )
    return int(value or 0)


def _require_meeting(ctx: AuthContext, meeting_id: str) -> Meeting:
    meeting = ctx.require_db().get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    return meeting


def _iso_or_none(value: datetime | None) -> str | None:
    return iso_utc(value) if value is not None else None


def _upload_data(upload: UploadSession) -> dict[str, Any]:
    return {
        "id": upload.id,
        "status": upload.status,
        "sealed_status": upload.sealed_status,
        "input_version": upload.input_version,
        "created_at": iso_utc(upload.created_at),
        "sealed_at": _iso_or_none(upload.sealed_at),
    }


def _task_data(task: ProcessingTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "input_version": task.input_version,
        "generation": task.generation,
        "error_code": task.error_code,
        "created_at": iso_utc(task.created_at),
        "updated_at": iso_utc(task.updated_at),
    }


def _transcript_data(
    version: TranscriptVersion | None,
) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "status": version.status,
        "empty": version.empty,
        "warnings": list(version.warnings),
        "created_at": iso_utc(version.created_at),
    }


def get_snapshot(ctx: AuthContext, meeting_id: str) -> dict[str, Any]:
    """Return the caller's current owner-scoped view for meeting_id.

    The snapshot is the recovery surface: a client that missed events gets
    current state, never another owner's event. Rows carry ids, statuses,
    counters and timestamps only - no transcript text, audio, speaker label or
    object key.
    """
    db = ctx.require_db()
    meeting = _require_meeting(ctx, meeting_id)
    latest = _latest_cursor(db, ctx.user_id, meeting_id)

    uploads = list(
        db.scalars(
            select(UploadSession)
            .where(
                UploadSession.owner_user_id == ctx.user_id,
                UploadSession.meeting_id == meeting_id,
            )
            .order_by(UploadSession.created_at.desc(), UploadSession.id.desc())
            .limit(SNAPSHOT_ROWS)
        )
    )
    tasks = list(
        db.scalars(
            select(ProcessingTask)
            .where(
                ProcessingTask.owner_user_id == ctx.user_id,
                ProcessingTask.meeting_id == meeting_id,
            )
            .order_by(
                ProcessingTask.created_at.desc(), ProcessingTask.id.desc()
            )
            .limit(SNAPSHOT_ROWS)
        )
    )
    transcript = None
    if meeting.current_transcript_id is not None:
        transcript = db.get(
            TranscriptVersion, (ctx.user_id, meeting.current_transcript_id)
        )
        if transcript is not None and transcript.meeting_id != meeting.id:
            # A stale/corrupt pointer must not expose another meeting's state.
            transcript = None

    return {
        "cursor": latest,
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "mode": meeting.mode,
            "generation": meeting.generation,
            "current_transcript_id": meeting.current_transcript_id,
            "deleted_at": _iso_or_none(meeting.deleted_at),
        },
        "uploads": [_upload_data(row) for row in uploads],
        "tasks": [_task_data(row) for row in tasks],
        "transcript": _transcript_data(transcript),
        "live": live.snapshot(ctx.user_id, meeting_id),
    }


def _snapshot_event(ctx: AuthContext, meeting_id: str) -> EventView:
    return EventView(
        cursor=_latest_cursor(ctx.require_db(), ctx.user_id, meeting_id),
        event="snapshot",
        data=get_snapshot(ctx, meeting_id),
        created_at=iso_utc(ctx.clock.now()),
    )


def _event_view(row: ResourceEvent) -> EventView:
    return EventView(
        cursor=row.id,
        event=row.event,
        data=dict(row.data or {}),
        created_at=iso_utc(row.created_at),
    )


def read_events(
    ctx: AuthContext, meeting_id: str, after: int | None
) -> list[EventView]:
    """Replay events after a durable cursor, or the caller's snapshot.

    Rules, in order: a missing/invalid/negative cursor, a cursor beyond the
    latest event, and a cursor that is not an event of this owner+meeting all
    return one snapshot event. A cursor equal to the latest event returns an
    empty page. Replays are capped; more than MAX_REPLAY rows also snapshots.
    """
    db = ctx.require_db()
    _require_meeting(ctx, meeting_id)
    latest = _latest_cursor(db, ctx.user_id, meeting_id)

    if after is None:
        return [_snapshot_event(ctx, meeting_id)]
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        return [_snapshot_event(ctx, meeting_id)]
    if after > latest:
        return [_snapshot_event(ctx, meeting_id)]
    if after == latest:
        return []

    anchor = None
    if after != 0:
        anchor = db.get(ResourceEvent, after)
        if (
            anchor is None
            or anchor.owner_user_id != ctx.user_id
            or anchor.meeting_id != meeting_id
        ):
            return [_snapshot_event(ctx, meeting_id)]

    rows = list(
        db.scalars(
            select(ResourceEvent)
            .where(
                ResourceEvent.owner_user_id == ctx.user_id,
                ResourceEvent.meeting_id == meeting_id,
                ResourceEvent.id > after,
            )
            .order_by(ResourceEvent.id.asc())
            .limit(MAX_REPLAY + 1)
        )
    )
    if len(rows) > MAX_REPLAY:
        return [_snapshot_event(ctx, meeting_id)]
    return [_event_view(row) for row in rows]


def sse_frame(event: EventView) -> str:
    """Encode one SSE frame; data is compact JSON and ids are cursors."""
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.cursor}\nevent: {event.event}\ndata: {payload}\n\n"


def _poll_once(
    *,
    session_factory: Any,
    clock: Any,
    user_id: str,
    session_id: str,
    meeting_id: str,
    after: int | None,
) -> list[EventView] | None:
    """Open a fresh session, revalidate auth + meeting, then read events.

    None means the stream must close; an empty list means "nothing new".
    """
    now = clock.now()
    with session_factory() as db:
        auth = db.get(AuthSession, session_id)
        if (
            auth is None
            or auth.user_id != user_id
            or auth.consumed_at is not None
            or auth.revoked_at is not None
            or auth.expires_at <= now
        ):
            return None
        meeting = db.get(Meeting, (user_id, meeting_id))
        if meeting is None or meeting.deleted_at is not None:
            return None
        ctx = AuthContext(
            user_id=user_id,
            session_id=session_id,
            db=db,
            clock=clock,
        )
        return read_events(ctx, meeting_id, after)


async def stream_events(
    *,
    session_factory: Any,
    clock: Any,
    settings: Any,
    user_id: str,
    session_id: str,
    meeting_id: str,
    after: int | None,
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Yield SSE frames until auth or the meeting is gone.

    The last fully emitted cursor is the resume point; Last-Event-ID (parsed
    by the route) is applied only on entry. Each iteration opens and closes
    its own session and does the sync SQL work in a worker thread, so no
    request-scoped session or event-loop thread is held.
    """
    del settings  # a uniform injectable signature; no tuning lives here yet
    cursor = after
    last_beat = clock.now()
    while True:
        events = await run_in_threadpool(
            _poll_once,
            session_factory=session_factory,
            clock=clock,
            user_id=user_id,
            session_id=session_id,
            meeting_id=meeting_id,
            after=cursor,
        )
        if events is None:
            return
        for event in events:
            yield sse_frame(event)
            cursor = event.cursor
            last_beat = clock.now()
        now = clock.now()
        if (
            heartbeat_seconds > 0
            and (now - last_beat).total_seconds() >= heartbeat_seconds
        ):
            yield ": keep-alive\n\n"
            last_beat = now
        if poll_seconds > 0:
            await asyncio.sleep(poll_seconds)
        else:
            await asyncio.sleep(0)


@router.get("/meetings/{meeting_id}/events", response_model=EventPage)
def read_events_route(
    meeting_id: str,
    after: str | None = Query(default=None),
    ctx: AuthContext = Depends(_require_user),
) -> EventPage:
    parsed = _coerce_after(after)
    items = read_events(ctx, meeting_id, parsed)
    latest = _latest_cursor(ctx.require_db(), ctx.user_id, meeting_id)
    next_cursor = items[-1].cursor if items else max(parsed or 0, latest)
    return EventPage(items=items, next_cursor=next_cursor)


@router.get("/meetings/{meeting_id}/events/stream")
async def stream_events_route(
    meeting_id: str,
    request: Request,
    after: str | None = Query(default=None),
    ctx: AuthContext = Depends(_require_user),
) -> StreamingResponse:
    _require_meeting(ctx, meeting_id)
    parsed = _coerce_after(after)
    header = request.headers.get("last-event-id")
    if header is not None:
        # A reconnecting EventSource sends its durable cursor here; it wins
        # over the query string. A malformed value falls back to a snapshot.
        parsed = _coerce_after(header)
    session_id = ctx.session_id
    user_id = ctx.user_id
    # Never hold the request-scoped session for the lifetime of the stream.
    ctx.require_db().close()
    factory = request.app.state.session_factory
    clock = request.app.state.clock
    settings = request.app.state.settings
    generator = stream_events(
        session_factory=factory,
        clock=clock,
        settings=settings,
        user_id=user_id,
        session_id=session_id,
        meeting_id=meeting_id,
        after=parsed,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
