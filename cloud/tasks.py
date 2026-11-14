"""Durable, owner-scoped processing tasks (Task 8).

A worker cannot be trusted to stay alive: laptops sleep, deployments restart,
and a stalled process may wake up minutes later holding a result. The queue is
therefore fenced per task, not per user:

* enqueue freezes the sealed input version and the meeting generation it may
  publish into; a duplicate submit returns the existing row;
* claim takes exactly one row with SELECT ... FOR UPDATE SKIP LOCKED, sets a
  server-generated lease token (stored only as a SHA-256 hash) and a deadline;
* publish re-checks owner, kind, input version, meeting generation, deletion
  and lease inside one transaction, and inserts at most one transcript version;
* cancel and delete are terminal; only transient failures are retried, and at
  most MAX_ATTEMPTS times.

No lock here is user-wide or global: enqueue locks one meeting row, claim locks
one task row, and fairness is computed by ordering owners by when they were
last served.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloud import events
from cloud.auth import require_csrf
from cloud.contracts import (
    AuthContext,
    ClaimedTask,
    ConflictError,
    NotFoundError,
    TaskView,
    TranscriptView,
    iso_utc,
)
from cloud.leases import hash_lease_token
from cloud.schema import (
    Meeting,
    MeetingSpeaker,
    ProcessingTask,
    TranscriptSegment,
    TranscriptSpeaker,
    TranscriptVersion,
    UploadSession,
)
from cloud.settings import Settings

router = APIRouter(prefix="/api/cloud", tags=["tasks"])

#: A task may be leased this many times before a transient failure is final.
MAX_ATTEMPTS = 3
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: One candidate per owner, ordered by the least recently served owner.
#: The aggregate reads rows without locking them, so it never blocks a worker
#: that is finishing another owner's task.
_CANDIDATE_SQL = text(
    "WITH ranked AS ("
    " SELECT t.id, t.created_at,"
    " row_number() OVER (PARTITION BY t.owner_user_id"
    " ORDER BY t.created_at ASC, t.id ASC) AS rn,"
    " COALESCE((SELECT MAX(p.lease_acquired_at) FROM processing_tasks p"
    " WHERE p.owner_user_id = t.owner_user_id),"
    " '-infinity'::timestamptz) AS served_at"
    " FROM processing_tasks t"
    " WHERE t.status = 'waiting'"
    ") SELECT id FROM ranked WHERE rn = 1"
    " ORDER BY served_at ASC, created_at ASC, id ASC"
)


class EnqueueFinalRequest(BaseModel):
    input_version: int = Field(ge=0)
    force: bool = False


def _view(task: ProcessingTask) -> TaskView:
    return TaskView(
        id=task.id,
        meeting_id=task.meeting_id,
        owner_user_id=task.owner_user_id,
        input_version=task.input_version,
        generation=task.generation,
        status=task.status,
    )


def _claimed(task: ProcessingTask, lease_token: str) -> ClaimedTask:
    assert task.lease_deadline is not None
    return ClaimedTask(
        task=_view(task),
        kind=task.kind,
        lease_token=lease_token,
        lease_deadline=iso_utc(task.lease_deadline),
    )


def _clear_lease(task: ProcessingTask) -> None:
    task.lease_token_hash = None
    task.lease_worker_id = None
    task.lease_deadline = None


def enqueue_final(
    ctx: AuthContext,
    meeting_id: str,
    input_version: int,
    *,
    force: bool = False,
) -> TaskView:
    """Queue one final transcription of a sealed input version.

    Idempotent for (meeting, input_version, generation): a duplicate submit,
    forced or not, returns the same row. A terminal row is only replaced when
    the caller explicitly forces a re-transcribe, which bumps the meeting
    generation so any still-running older worker can no longer publish.
    """
    db = ctx.db
    now = ctx.clock.now()
    meeting = db.execute(
        select(Meeting)
        .where(Meeting.owner_user_id == ctx.user_id, Meeting.id == meeting_id)
        .with_for_update()
    ).scalar_one_or_none()
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")

    upload = db.execute(
        select(UploadSession)
        .where(
            UploadSession.owner_user_id == ctx.user_id,
            UploadSession.meeting_id == meeting_id,
            UploadSession.status == "sealed",
            UploadSession.input_version == input_version,
        )
        .order_by(UploadSession.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if upload is None:
        raise ConflictError("input_not_sealed")

    latest = db.execute(
        select(ProcessingTask)
        .where(
            ProcessingTask.owner_user_id == ctx.user_id,
            ProcessingTask.meeting_id == meeting_id,
            ProcessingTask.kind == "final",
        )
        .order_by(
            ProcessingTask.generation.desc(),
            ProcessingTask.created_at.desc(),
            ProcessingTask.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()

    same_input = (
        latest is not None
        and latest.input_version == input_version
        and latest.generation == meeting.generation
    )
    # A waiting row is always reused: the worker has not started yet.  A forced
    # submit supersedes a terminal result or a running attempt (the old worker's
    # publish is fenced by the generation bump below).
    if same_input and (not force or latest.status == "waiting"):
        db.commit()
        return _view(latest)

    supersede = latest is not None
    generation = meeting.generation
    superseded_rows: list[ProcessingTask] = []
    if supersede:
        generation = meeting.generation + 1
        meeting.generation = generation
        meeting.updated_at = now
        # Load the rows before the bulk update so each resulting cancellation
        # has a durable event in this same transaction.
        superseded_rows = list(
            db.scalars(
                select(ProcessingTask).where(
                    ProcessingTask.owner_user_id == ctx.user_id,
                    ProcessingTask.meeting_id == meeting_id,
                    ProcessingTask.status.in_(("waiting", "running")),
                )
            )
        )
        # Only this meeting's open tasks are superseded; no other user waits.
        db.execute(
            update(ProcessingTask)
            .where(
                ProcessingTask.owner_user_id == ctx.user_id,
                ProcessingTask.meeting_id == meeting_id,
                ProcessingTask.status.in_(("waiting", "running")),
            )
            .values(
                status="cancelled",
                error_code="superseded",
                lease_token_hash=None,
                lease_worker_id=None,
                lease_deadline=None,
                completed_at=now,
                updated_at=now,
            )
        )

    task = ProcessingTask(
        id=str(uuid4()),
        kind="final",
        owner_user_id=ctx.user_id,
        meeting_id=meeting_id,
        upload_session_id=upload.id,
        input_version=input_version,
        generation=generation,
        attempt=0,
        status="waiting",
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    for row in superseded_rows:
        events.record(
            db,
            owner_user_id=row.owner_user_id,
            meeting_id=meeting_id,
            event="task.updated",
            data={
                "task_id": row.id,
                "status": "cancelled",
                "error_code": "superseded",
                "input_version": row.input_version,
                "generation": row.generation,
                "updated_at": iso_utc(now),
            },
            now=now,
        )
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=meeting_id,
        event="task.updated",
        data={
            "task_id": task.id,
            "status": task.status,
            "input_version": task.input_version,
            "generation": task.generation,
            "created_at": iso_utc(now),
            "updated_at": iso_utc(now),
        },
        now=now,
    )
    db.commit()
    return _view(task)


def claim_task(
    worker_id: str, now: datetime, *, db: Session, settings: Settings
) -> ClaimedTask | None:
    """Lease the oldest waiting task of the least recently served owner.

    Expired leases are requeued first (or failed after MAX_ATTEMPTS), then the
    chosen row is locked with SKIP LOCKED so two workers can never both claim
    it. A lost race simply returns None instead of waiting.
    """
    lease_s = settings.worker_lease_s
    expired = (
        ProcessingTask.status == "running",
        ProcessingTask.lease_deadline < now,
    )
    # Lock the rows before changing them so two concurrent claims cannot both
    # record the same requeue/failure transition as an event.
    expired_rows = list(
        db.scalars(select(ProcessingTask).where(*expired).with_for_update())
    )
    expired_states = [
        (row, "failed" if row.attempt >= MAX_ATTEMPTS else "waiting")
        for row in expired_rows
    ]
    db.execute(
        update(ProcessingTask)
        .where(*expired, ProcessingTask.attempt >= MAX_ATTEMPTS)
        .values(
            status="failed",
            error_code="lease_expired",
            lease_token_hash=None,
            lease_worker_id=None,
            lease_deadline=None,
            completed_at=now,
            updated_at=now,
        )
    )
    db.execute(
        update(ProcessingTask)
        .where(*expired, ProcessingTask.attempt < MAX_ATTEMPTS)
        .values(
            status="waiting",
            lease_token_hash=None,
            lease_worker_id=None,
            lease_deadline=None,
            updated_at=now,
        )
    )
    for row, status in expired_states:
        if row.kind == "voiceprint" and status == "failed":
            from cloud.voiceprints import fail_voiceprint_enrollment

            fail_voiceprint_enrollment(db, row, "lease_expired", now)
        if row.meeting_id is None:
            continue
        events.record(
            db,
            owner_user_id=row.owner_user_id,
            meeting_id=row.meeting_id,
            event="task.updated",
            data={
                "task_id": row.id,
                "status": status,
                "error_code": "lease_expired" if status == "failed" else row.error_code,
                "input_version": row.input_version,
                "generation": row.generation,
                "updated_at": iso_utc(now),
            },
            now=now,
        )
    db.commit()

    candidates = [row[0] for row in db.execute(_CANDIDATE_SQL).all()]
    for task_id in candidates:
        task = db.execute(
            select(ProcessingTask)
            .where(
                ProcessingTask.id == task_id,
                ProcessingTask.status == "waiting",
            )
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if task is None:
            continue
        lease_token = secrets.token_urlsafe(32)
        deadline = now + timedelta(seconds=lease_s)
        task.status = "running"
        task.attempt += 1
        task.error_code = None
        task.lease_token_hash = hash_lease_token(lease_token)
        task.lease_worker_id = worker_id[:64]
        task.lease_acquired_at = now
        task.lease_deadline = deadline
        task.updated_at = now
        if task.meeting_id is not None:
            events.record(
                db,
                owner_user_id=task.owner_user_id,
                meeting_id=task.meeting_id,
                event="task.updated",
                data={
                    "task_id": task.id,
                    "status": task.status,
                    "input_version": task.input_version,
                    "generation": task.generation,
                    "updated_at": iso_utc(now),
                },
                now=now,
            )
        db.commit()
        return _claimed(task, lease_token)

    db.rollback()
    return None


def renew_task(
    task_id: str,
    lease_token: str,
    *,
    now: datetime,
    db: Session,
    settings: Settings,
) -> None:
    """Extend only the matching, still-live lease. A stale token never revives."""
    task = db.execute(
        select(ProcessingTask)
        .where(ProcessingTask.id == task_id)
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        db.rollback()
        raise NotFoundError("task_not_found")
    if task.status != "running" or task.lease_token_hash != hash_lease_token(lease_token):
        db.rollback()
        raise ConflictError("lease_lost")
    if task.lease_deadline is None or task.lease_deadline <= now:
        db.rollback()
        raise ConflictError("lease_expired")
    task.lease_deadline = now + timedelta(seconds=settings.worker_lease_s)
    task.updated_at = now
    db.commit()


def publish_result(
    task_id: str,
    lease_token: str,
    result: TranscriptView,
    *,
    owner_user_id: str | None = None,
    now: datetime,
    db: Session,
) -> bool:
    """Publish exactly once, only from the live lease of the current input.

    Every rejection returns False and writes nothing: a stale, forged, expired
    or superseded worker cannot create a transcript version, and a repeat
    publish cannot duplicate one.
    """
    # Lock order is meeting then task, matching enqueue (meeting -> tasks), so a
    # publish and a superseding enqueue cannot deadlock each other.
    probe = db.execute(
        select(ProcessingTask).where(ProcessingTask.id == task_id)
    ).scalar_one_or_none()
    if probe is None:
        db.rollback()
        return False
    if owner_user_id is not None and probe.owner_user_id != owner_user_id:
        db.rollback()
        return False
    if probe.meeting_id is None:
        db.rollback()
        return False

    meeting = db.execute(
        select(Meeting)
        .where(
            Meeting.owner_user_id == probe.owner_user_id,
            Meeting.id == probe.meeting_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(ProcessingTask)
        .where(ProcessingTask.id == task_id)
        .with_for_update()
    ).scalar_one_or_none()
    if task is None or meeting is None or meeting.deleted_at is not None:
        db.rollback()
        return False
    if owner_user_id is not None and task.owner_user_id != owner_user_id:
        db.rollback()
        return False
    if task.status != "running" or task.kind != "final":
        db.rollback()
        return False
    if task.lease_token_hash != hash_lease_token(lease_token):
        db.rollback()
        return False
    if task.lease_deadline is None or task.lease_deadline <= now:
        db.rollback()
        return False
    if task.meeting_id is None or task.upload_session_id is None:
        db.rollback()
        return False
    if result.meeting_id != task.meeting_id:
        db.rollback()
        return False
    if meeting.generation != task.generation:
        db.rollback()
        return False

    upload = db.execute(
        select(UploadSession)
        .where(
            UploadSession.owner_user_id == task.owner_user_id,
            UploadSession.id == task.upload_session_id,
            UploadSession.meeting_id == task.meeting_id,
            UploadSession.status == "sealed",
            UploadSession.input_version == task.input_version,
        )
    ).scalar_one_or_none()
    if upload is None:
        db.rollback()
        return False

    speaker_ids = {speaker.id for speaker in result.speakers}
    segment_ids = {segment.id for segment in result.segments}
    if len(speaker_ids) != len(result.speakers):
        db.rollback()
        return False
    if len(segment_ids) != len(result.segments):
        db.rollback()
        return False
    if result.empty != (len(result.segments) == 0):
        db.rollback()
        return False
    for segment in result.segments:
        if segment.end_ms < segment.start_ms:
            db.rollback()
            return False
        if segment.speaker_id is not None and segment.speaker_id not in speaker_ids:
            db.rollback()
            return False

    # A voiceprint usage may have gone stale while the worker was busy. Undo
    # those labels inside this publish transaction so a revoked profile never
    # becomes durable as a real name. Read-only and idempotent.
    from cloud.voiceprints import recheck_voiceprint_usage

    result = recheck_voiceprint_usage(
        db, result, now=now, owner_user_id=probe.owner_user_id
    )

    # Speaker rows created by the enhancement pipeline are pending in this same
    # session. Flush them so the child foreign keys below can see them; any
    # failure rolls the whole publish back, including the meeting pointer.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False

    db.add(
        TranscriptVersion(
            owner_user_id=task.owner_user_id,
            id=result.id,
            meeting_id=task.meeting_id,
            task_id=task.id,
            input_version=task.input_version,
            generation=task.generation,
            payload=result.model_dump(mode="json"),
            status=result.status,
            empty=result.empty,
            warnings=list(result.warnings),
            created_at=now,
        )
    )
    for speaker in result.speakers:
        identity = db.get(MeetingSpeaker, (task.owner_user_id, speaker.id))
        if identity is None:
            # A hand-built or legacy view may name a speaker that the pipeline
            # never stored. Keep a meeting-scoped identity without an embedding
            # so the version remains self-describing.
            db.add(
                MeetingSpeaker(
                    owner_user_id=task.owner_user_id,
                    id=speaker.id,
                    meeting_id=task.meeting_id,
                    label=speaker.label,
                    created_at=now,
                )
            )
        elif identity.meeting_id != task.meeting_id:
            # A speaker id belongs to exactly one meeting per owner.
            db.rollback()
            return False
    # SQLAlchemy orders inserts by relationships, not by raw composite foreign
    # keys, so flush the version and any legacy identity rows before the child
    # rows that reference them.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    for speaker in result.speakers:
        db.add(
            TranscriptSpeaker(
                owner_user_id=task.owner_user_id,
                version_id=result.id,
                speaker_id=speaker.id,
                meeting_id=task.meeting_id,
                label=speaker.label,
                suspect=speaker.suspect,
            )
        )
    for ordinal, segment in enumerate(result.segments):
        db.add(
            TranscriptSegment(
                owner_user_id=task.owner_user_id,
                version_id=result.id,
                id=segment.id,
                ordinal=ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=segment.speaker_id,
                track_id=segment.track_id,
            )
        )
    task.status = "completed"
    task.completed_at = now
    task.error_code = None
    task.lease_token_hash = None
    task.lease_deadline = None
    task.updated_at = now
    meeting.current_transcript_id = result.id
    meeting.updated_at = now
    # Both the transcript pointer and the terminal task state are durable in
    # this commit; a rejected publish returned before reaching this point.
    events.record(
        db,
        owner_user_id=task.owner_user_id,
        meeting_id=task.meeting_id,
        event="transcript.published",
        data={
            "version_id": result.id,
            "task_id": task.id,
            "status": result.status,
            "empty": result.empty,
            "warnings": list(result.warnings),
            "created_at": iso_utc(now),
        },
        now=now,
    )
    events.record(
        db,
        owner_user_id=task.owner_user_id,
        meeting_id=task.meeting_id,
        event="task.updated",
        data={
            "task_id": task.id,
            "status": task.status,
            "input_version": task.input_version,
            "generation": task.generation,
            "updated_at": iso_utc(now),
        },
        now=now,
    )
    try:
        db.commit()
    except IntegrityError:
        # The unique task_id (or version id) already exists: consume once only.
        db.rollback()
        return False
    return True


def fail_task(
    task_id: str,
    lease_token: str,
    *,
    transient: bool,
    error_code: str,
    now: datetime,
    db: Session,
    settings: Settings,
) -> bool:
    """Record a safe error code; retry only transient failures, at most 3 times.

    The exception message is never persisted: callers pass a fixed code.
    """
    del settings  # bound is the module constant; kept for a uniform interface
    task = db.execute(
        select(ProcessingTask)
        .where(ProcessingTask.id == task_id)
        .with_for_update()
    ).scalar_one_or_none()
    if task is None or task.status != "running":
        db.rollback()
        return False
    if task.lease_token_hash != hash_lease_token(lease_token):
        db.rollback()
        return False

    task.error_code = error_code
    task.updated_at = now
    _clear_lease(task)
    if transient and task.attempt < MAX_ATTEMPTS:
        task.status = "waiting"
    else:
        task.status = "failed"
        task.completed_at = now
        if task.kind == "voiceprint":
            from cloud.voiceprints import fail_voiceprint_enrollment

            fail_voiceprint_enrollment(db, task, error_code, now)
    if task.meeting_id is not None:
        events.record(
            db,
            owner_user_id=task.owner_user_id,
            meeting_id=task.meeting_id,
            event="task.updated",
            data={
                "task_id": task.id,
                "status": task.status,
                "error_code": task.error_code,
                "input_version": task.input_version,
                "generation": task.generation,
                "updated_at": iso_utc(now),
            },
            now=now,
        )
    db.commit()
    return True


def cancel_task(ctx: AuthContext, task_id: str) -> None:
    """Cancel one of the caller's tasks; another user's id is a 404, not a 403."""
    db = ctx.db
    now = ctx.clock.now()
    task = db.execute(
        select(ProcessingTask)
        .where(
            ProcessingTask.id == task_id,
            ProcessingTask.owner_user_id == ctx.user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        db.rollback()
        raise NotFoundError("task_not_found")
    if task.status in TERMINAL_STATUSES:
        db.rollback()
        return
    task.status = "cancelled"
    task.error_code = "cancelled_by_user"
    task.completed_at = now
    task.updated_at = now
    _clear_lease(task)
    if task.kind == "voiceprint":
        from cloud.voiceprints import cancel_voiceprint_enrollment

        cancel_voiceprint_enrollment(db, task, now)
    if task.meeting_id is not None:
        events.record(
            db,
            owner_user_id=task.owner_user_id,
            meeting_id=task.meeting_id,
            event="task.updated",
            data={
                "task_id": task.id,
                "status": task.status,
                "error_code": task.error_code,
                "input_version": task.input_version,
                "generation": task.generation,
                "updated_at": iso_utc(now),
            },
            now=now,
        )
    db.commit()


def read_claimed_task(
    task_id: str,
    *,
    owner_user_id: str,
    lease_token: str,
    now: datetime,
    db: Session,
) -> ClaimedTask:
    """Validate a queue message against the durable lease before doing work.

    A message naming another owner is a plain 404: the worker learns nothing
    about the row it tried to read, and no payload is returned.
    """
    task = db.execute(
        select(ProcessingTask)
        .where(
            ProcessingTask.id == task_id,
            ProcessingTask.owner_user_id == owner_user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        db.rollback()
        raise NotFoundError("task_not_found")
    if task.status != "running" or task.lease_token_hash != hash_lease_token(lease_token):
        db.rollback()
        raise ConflictError("lease_lost")
    if task.lease_deadline is None or task.lease_deadline <= now:
        db.rollback()
        raise ConflictError("lease_expired")
    claimed = _claimed(task, lease_token)
    db.rollback()
    return claimed


# -- routes ----------------------------------------------------------------


@router.post("/meetings/{meeting_id}/tasks/final", response_model=TaskView)
def enqueue_final_route(
    meeting_id: str,
    payload: EnqueueFinalRequest,
    ctx: AuthContext = Depends(require_csrf),
) -> TaskView:
    return enqueue_final(
        ctx, meeting_id, payload.input_version, force=payload.force
    )


@router.post("/tasks/{task_id}/cancel", status_code=204)
def cancel_task_route(
    task_id: str, ctx: AuthContext = Depends(require_csrf)
) -> Response:
    cancel_task(ctx, task_id)
    return Response(status_code=204)