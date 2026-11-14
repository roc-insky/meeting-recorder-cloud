"""Bounded worker shell for durable processing tasks (Task 8).

There is no ASR engine here: the worker claims a task, hands it to an injected
processor, and publishes the result through the fencing checks in cloud.tasks.
A processor that fails transiently requeues the task (bounded by MAX_ATTEMPTS);
any other exception is terminal. Exception text is never stored or logged as a
code, so a model or engine error cannot leak data through the queue.

run_forever is a polling shell only; it is exercised by the injectable
process_once path in tests, so no background thread is started at import time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from cloud import limits, observability
from cloud.contracts import AuthContext, ClaimedTask, NotFoundError, TranscriptView
from cloud.settings import Settings
from cloud.tasks import claim_task, fail_task, publish_result, read_claimed_task


class TransientProcessingError(Exception):
    """A retryable failure: network, engine restart, temporary storage error."""


@dataclass(frozen=True)
class WorkItem:
    """What a processor may trust: the validated lease and its frozen input."""

    task_id: str
    owner_user_id: str
    lease_token: str
    generation: int
    input_version: int
    meeting_id: str | None


def open_work(item: WorkItem, *, now: datetime, db: Session) -> ClaimedTask:
    """Validate a queue message against the durable task owner and lease."""
    claimed = read_claimed_task(
        item.task_id,
        owner_user_id=item.owner_user_id,
        lease_token=item.lease_token,
        now=now,
        db=db,
    )
    task = claimed.task
    if (
        task.generation != item.generation
        or task.input_version != item.input_version
        or task.meeting_id != item.meeting_id
    ):
        raise NotFoundError("task_not_found")
    return claimed


def process_once(
    processor: Callable[..., TranscriptView],
    *,
    worker_id: str,
    now: datetime,
    db: Session,
    settings: Settings,
    context: AuthContext | None = None,
    voiceprint_processor: Callable[..., bool] | None = None,
) -> bool:
    """Claim at most one task, run the processor, publish or record failure.

    Returns True only when a result was actually published. A task that is
    requeued, superseded or cancelled returns False, so a caller can tell a
    real publish from a consumed-but-not-published attempt.

    A real per-owner decode slot is taken in PostgreSQL after the claim. If
    another worker is already decoding for this owner the task is requeued with
    a fixed code instead of running a second decode. The slot is always
    released on the way out; expiry is the crash-safe fallback.
    """
    claimed = claim_task(worker_id, now, db=db, settings=settings)
    if claimed is None:
        observability.emit_metric("task_queue", 1, {"outcome": "empty"})
        return False
    task = claimed.task
    item = WorkItem(
        task_id=task.id,
        owner_user_id=task.owner_user_id,
        lease_token=claimed.lease_token,
        generation=task.generation,
        input_version=task.input_version,
        meeting_id=task.meeting_id,
    )
    observability.emit_metric("task_queue", 1, {"outcome": "claimed"})
    depth = limits.queue_depth(db)
    observability.emit_metric(
        "task_queue_depth", depth, {"bucket": limits.depth_bucket(depth)}
    )

    owner_ctx = (
        replace(context, db=db, settings=settings)
        if context is not None and context.user_id == task.owner_user_id
        else AuthContext(
            user_id=task.owner_user_id,
            session_id="",
            db=db,
            clock=context.clock if context is not None else (lambda: now),
            settings=settings,
            objects=None,
            audit=None,
        )
    )
    if not limits.acquire_decode_slot(owner_ctx, task.id, now=now):
        observability.emit_metric("task_queue", 1, {"outcome": "busy"})
        observability.log_event("worker_busy", request_id=None, code="decode_busy")
        fail_task(
            task.id,
            claimed.lease_token,
            transient=True,
            error_code="decode_busy",
            now=now,
            db=db,
            settings=settings,
        )
        return False
    try:
        return _run_claim(
            processor,
            claimed=claimed,
            task=task,
            item=item,
            now=now,
            db=db,
            settings=settings,
            context=context,
            voiceprint_processor=voiceprint_processor,
        )
    finally:
        limits.release_decode_slot(task.owner_user_id, task.id, db)


def _run_claim(
    processor: Callable[..., TranscriptView],
    *,
    claimed: ClaimedTask,
    task: Any,
    item: WorkItem,
    now: datetime,
    db: Session,
    settings: Settings,
    context: AuthContext | None,
    voiceprint_processor: Callable[..., bool] | None,
) -> bool:
    if claimed.kind == "voiceprint":
        if voiceprint_processor is None:
            fail_task(
                task.id,
                claimed.lease_token,
                transient=False,
                error_code="no_voiceprint_processor",
                now=now,
                db=db,
                settings=settings,
            )
            return False
        db.info["voiceprint_settings"] = settings
        try:
            if context is None:
                return bool(voiceprint_processor(item))
            return bool(voiceprint_processor(item, replace(context, db=db)))
        except TransientProcessingError:
            fail_task(
                task.id,
                claimed.lease_token,
                transient=True,
                error_code="transient_failure",
                now=now,
                db=db,
                settings=settings,
            )
            return False
        except Exception:  # noqa: BLE001 - never persist the exception text
            observability.emit_metric("worker_failure", 1, {"stage": "processing"})
            fail_task(
                task.id,
                claimed.lease_token,
                transient=False,
                error_code="processing_failed",
                now=now,
                db=db,
                settings=settings,
            )
            return False

    try:
        if context is None:
            result = processor(item)
        else:
            # Rebind the context to the worker's session so speaker rows staged
            # by the processor are flushed by the same publish transaction.
            result = processor(item, replace(context, db=db))
    except TransientProcessingError:
        observability.emit_metric("worker_failure", 1, {"stage": "decode"})
        fail_task(
            task.id,
            claimed.lease_token,
            transient=True,
            error_code="transient_failure",
            now=now,
            db=db,
            settings=settings,
        )
        return False
    except Exception:  # noqa: BLE001 - never persist the exception text
        observability.emit_metric("worker_failure", 1, {"stage": "processing"})
        fail_task(
            task.id,
            claimed.lease_token,
            transient=False,
            error_code="processing_failed",
            now=now,
            db=db,
            settings=settings,
        )
        return False
    if context is not None and claimed.kind == "final":
        # Apply compatible voiceprints exactly once, after speaker attribution
        # and before the durable publish re-checks each usage.
        from cloud.voiceprints import match_profiles

        result = match_profiles(replace(context, db=db), result, None)
    return publish_result(
        task.id,
        claimed.lease_token,
        result,
        owner_user_id=task.owner_user_id,
        now=now,
        db=db,
    )


def run_forever(
    processor: Callable[[WorkItem], TranscriptView],
    *,
    worker_id: str,
    db: Session,
    settings: Settings,
    clock: Callable[[], datetime],
    poll_seconds: float = 1.0,
) -> None:  # pragma: no cover - operational shell, not a test target
    """Poll forever; real ASR claims remain not_run for this task."""
    while True:
        worked = process_once(
            processor,
            worker_id=worker_id,
            now=clock(),
            db=db,
            settings=settings,
        )
        if not worked:
            time.sleep(poll_seconds)
