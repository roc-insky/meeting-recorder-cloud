"""PostgreSQL-shared request budgets and the per-owner decode slot.

Both fences live in PostgreSQL on purpose: an in-process counter would let two
instances exceed a per-owner budget, and a process-local decode lock would let
two workers transcribe the same owner at once. The counter row contains only a
server-derived owner id, a fixed action, a window start and a count. The slot
row contains only the owner, the task it belongs to and timestamps.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, event, func, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cloud import observability
from cloud.contracts import (
    AuthContext,
    CloudError,
    ServiceUnavailableError,
)
from cloud.schema import DecodeSlot, ProcessingTask

_ACTIONS = frozenset(
    {
        "chunk_write",
        "lease_write",
        "task_enqueue",
        "ai_call",
        "playback",
        "voiceprint_enroll",
    }
)

#: action -> Settings field holding its per-window count.
_BUDGET_FIELD = {
    "chunk_write": "rate_limit_chunk_write",
    "lease_write": "rate_limit_lease_write",
    "task_enqueue": "rate_limit_task_enqueue",
    "ai_call": "rate_limit_ai_call",
    "playback": "rate_limit_playback",
    "voiceprint_enroll": "rate_limit_voiceprint_enroll",
}

#: method, path template, action. Only these surfaces consume a request budget;
#: release/heartbeat and reads stay available so a client can always recover.
_ROUTE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "PUT",
        re.compile(r"^/api/cloud/uploads/[^/]+/chunks/[^/]+/[^/]+/[0-9]+$"),
        "chunk_write",
    ),
    (
        "POST",
        re.compile(
            r"^/api/cloud/capture-lease(?:/heartbeat)?$"
        ),
        "lease_write",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/meetings/[^/]+/tasks/final$"),
        "task_enqueue",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/meetings/[^/]+/transcripts/supplement$"),
        "task_enqueue",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/meetings/[^/]+/transcripts/re-transcribe$"),
        "task_enqueue",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/meetings/[^/]+/summary$"),
        "ai_call",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/positions/[^/]+/interview$"),
        "ai_call",
    ),
    (
        "GET",
        re.compile(r"^/api/cloud/meetings/[^/]+/audio$"),
        "playback",
    ),
    (
        "POST",
        re.compile(r"^/api/cloud/voiceprints$"),
        "voiceprint_enroll",
    ),
)


class RateLimitError(CloudError):
    """HTTP 429 with a positive Retry-After measured in whole seconds."""

    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__("rate_limited")


def _window_start(now: datetime, window_s: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % window_s), tz=timezone.utc)


def _now(ctx: AuthContext) -> datetime:
    clock = getattr(ctx, "clock", None)
    if clock is None:
        return datetime.now(timezone.utc)
    value = clock.now()
    if value.tzinfo is None:
        raise ServiceUnavailableError("service_unavailable")
    return value.astimezone(timezone.utc)


def check_limit(ctx: AuthContext, action: str, cost: int) -> None:
    """Atomically admit or reject one owner/action request in a fixed window.

    The UPSERT both opens a new window and increments an existing one in a
    single statement, so two instances can never lose an increment. A database
    error is fail-closed: the request is refused with 503, never allowed.
    """
    if action not in _ACTIONS or isinstance(cost, bool) or not isinstance(cost, int):
        raise ValueError("unknown rate-limit action or invalid cost")
    if cost < 1:
        raise ValueError("rate-limit cost must be at least 1")

    db = ctx.db
    settings = ctx.settings
    if db is None or settings is None:
        raise ServiceUnavailableError("service_unavailable")
    try:
        budget = int(getattr(settings, _BUDGET_FIELD[action]))
        window_s = int(settings.rate_limit_window_s)
    except (AttributeError, TypeError, ValueError):
        raise ServiceUnavailableError("service_unavailable") from None
    if budget < 1 or window_s < 1:
        raise ServiceUnavailableError("service_unavailable")

    now = _now(ctx)
    start = _window_start(now, window_s)
    # The counter is committed on its own short session. Committing the
    # request's session here would either release the handler's locks or make a
    # later handler commit failure look like a rate-limit failure. The UPSERT is
    # still one atomic PostgreSQL statement shared by every instance.
    counter = None
    try:
        counter = Session(bind=db.get_bind(), expire_on_commit=False)
        row = counter.execute(
            text(
                """
                INSERT INTO request_rate_limits
                    (owner_user_id, action, window_start, count)
                VALUES (:owner, :action, :start, :cost)
                ON CONFLICT (owner_user_id, action) DO UPDATE
                SET count = CASE
                        WHEN request_rate_limits.window_start = EXCLUDED.window_start
                        THEN request_rate_limits.count + EXCLUDED.count
                        ELSE EXCLUDED.count
                    END,
                    window_start = EXCLUDED.window_start
                RETURNING count, window_start
                """
            ),
            {
                "owner": ctx.user_id,
                "action": action,
                "start": start,
                "cost": cost,
            },
        ).one()
        counter.commit()
    except Exception as exc:  # noqa: BLE001 - every database failure is fail-closed
        if counter is not None:
            _safe_rollback(counter)
        raise ServiceUnavailableError("service_unavailable") from exc
    finally:
        if counter is not None:
            counter.close()

    if int(row.count) <= budget:
        return
    retry_after = max(
        1,
        math.ceil((row.window_start + timedelta(seconds=window_s) - now).total_seconds()),
    )
    observability.emit_metric(
        "rate_limit_exceeded", 1, {"action": action}
    )
    raise RateLimitError(retry_after)


def enforce_request_budget(request: Any, ctx: AuthContext) -> None:
    """Apply the route table once, before the handler can mutate or call out."""
    method = request.method.upper()
    path = request.url.path
    for rule_method, pattern, action in _ROUTE_RULES:
        if method == rule_method and pattern.match(path):
            check_limit(ctx, action, 1)
            return


def acquire_decode_slot(
    ctx: AuthContext, task_id: str, *, now: datetime | None = None
) -> bool:
    """Atomically claim the owner's one decode slot; expired slots are stealable.

    Returns False when another live slot exists. The UPSERT is the race fence:
    with no row two inserters conflict and only the first gets a returning row.
    """
    db = ctx.db
    settings = ctx.settings
    if db is None or settings is None:
        raise ServiceUnavailableError("service_unavailable")
    current = (now or _now(ctx)).astimezone(timezone.utc)
    expires = current + timedelta(seconds=int(settings.decode_slot_s))
    try:
        row = db.execute(
            text(
                """
                INSERT INTO decode_slots
                    (owner_user_id, task_id, acquired_at, expires_at)
                VALUES (:owner, :task, :now, :expires)
                ON CONFLICT (owner_user_id) DO UPDATE
                SET task_id = EXCLUDED.task_id,
                    acquired_at = EXCLUDED.acquired_at,
                    expires_at = EXCLUDED.expires_at
                WHERE decode_slots.expires_at <= EXCLUDED.acquired_at
                RETURNING owner_user_id
                """
            ),
            {
                "owner": ctx.user_id,
                "task": task_id,
                "now": current,
                "expires": expires,
            },
        ).first()
        db.commit()
    except Exception as exc:  # noqa: BLE001 - no proof of a slot means no decode
        _safe_rollback(db)
        raise ServiceUnavailableError("service_unavailable") from exc
    return row is not None


def release_decode_slot(
    owner_user_id: str,
    task_id: str,
    db: Session,
    *,
    commit: bool = True,
) -> None:
    """Release the owner's slot only if it still belongs to this task."""
    try:
        db.execute(
            delete(DecodeSlot).where(
                DecodeSlot.owner_user_id == owner_user_id,
                DecodeSlot.task_id == task_id,
            )
        )
        if commit:
            db.commit()
    except Exception:  # noqa: BLE001 - expiry is the safe fallback
        _safe_rollback(db)


def queue_depth(db: Session) -> int:
    """Count waiting tasks without exposing any task id in metrics."""
    try:
        return int(
            db.scalar(
                select(func.count())
                .select_from(ProcessingTask)
                .where(ProcessingTask.status == "waiting")
            )
            or 0
        )
    except Exception:  # noqa: BLE001 - metrics must not break a worker
        return 0


def depth_bucket(depth: int) -> str:
    if depth <= 0:
        return "0"
    if depth <= 10:
        return "1-10"
    if depth <= 100:
        return "11-100"
    return "100+"


def _safe_rollback(db: Any) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - preserving the original failure is enough
        pass


# -- terminal task transitions release the slot ----------------------------
# publish_result, fail_task and cancel_task all change an owner's processing
# task to a terminal state. Collecting those transitions in a flush hook keeps
# the slot lifecycle next to the table that defines it without duplicating the
# release call in every task path. The explicit worker release remains for a
# no-op (fenced) publish and for immediate hand-back.

_SLOT_RELEASES = "_cloud_decode_slot_releases"
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


@event.listens_for(Session, "before_flush")
def _collect_terminal_tasks(session: Session, _flush_context: Any, _instances: Any) -> None:
    from sqlalchemy import inspect as sa_inspect

    releases: list[tuple[str, str]] = []
    for obj in session.dirty:
        if not isinstance(obj, ProcessingTask):
            continue
        history = sa_inspect(obj).attrs.status.history
        if history.has_changes() and obj.status in _TERMINAL:
            releases.append((obj.owner_user_id, obj.id))
    if releases:
        session.info.setdefault(_SLOT_RELEASES, []).extend(releases)


@event.listens_for(Session, "after_flush")
def _release_terminal_slots(session: Session, _flush_context: Any) -> None:
    releases = session.info.pop(_SLOT_RELEASES, None)
    if not releases:
        return
    try:
        session.connection().execute(
            delete(DecodeSlot).where(
                tuple_(DecodeSlot.owner_user_id, DecodeSlot.task_id).in_(releases)
            )
        )
    except SQLAlchemyError:
        # The task transaction must still be able to finish; expiry reclaims
        # the slot if this narrow delete fails.
        pass
