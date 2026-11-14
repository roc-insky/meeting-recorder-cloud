"""Non-blocking live scheduling seam.

Task 11 intentionally ships only the *schedule and backpressure* boundary of
live transcription. There is no live ASR, no model call, no live transcript
text and no speaker label in this module: those are a later task with a real
model behind them. What exists here is the O(1) hand-off an upload uses after a
chunk is durable, plus a bounded queue a future worker can drain.

Design rules:

* submit_live is called after a chunk commit and must never wait on a
  database, model or ffmpeg. It only takes a short in-process lock, so a
  backlogged live worker cannot delay an upload ACK.
* At most one pending request exists per (owner, meeting): a newer cursor
  replaces the queued cursor instead of growing the queue.
* The pending FIFO is bounded (MAX_PENDING). When it is full a new meeting
  is dropped and its state becomes "paused": dropping is honest, blocking
  the API is not.
* A cursor that does not advance is ignored, so a duplicate ACK never queues
  a second live request.

No event is emitted here: this task deliberately does not add live.updated
and does not invent partial transcript text. A consumer may expose
LiveScheduler.snapshot as project state.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

#: Maximum number of distinct meetings with a pending live request.
MAX_PENDING = 8
#: States reported by LiveScheduler.snapshot.
IDLE = 'idle'
QUEUED = 'queued'
RUNNING = 'running'
PAUSED = 'paused'
_STATES = frozenset({IDLE, QUEUED, RUNNING, PAUSED})


@dataclass(frozen=True)
class LiveRequest:
    """One coalesced request to run live work up to input_cursor."""

    owner_user_id: str
    meeting_id: str
    input_cursor: int


@dataclass
class _Entry:
    cursor: int = 0
    state: str = IDLE


class LiveScheduler:
    """Bounded, coalescing, in-process queue for degraded live work."""

    def __init__(self, capacity: int = MAX_PENDING) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._lock = Lock()
        self._pending: list[LiveRequest] = []
        self._entries: dict[tuple[str, str], _Entry] = {}

    def submit(self, meeting_id: str, owner_user_id: str, input_cursor: int) -> None:
        """Record the latest cursor for a meeting; never blocks or raises."""
        try:
            if isinstance(input_cursor, bool) or not isinstance(input_cursor, int):
                return
            if input_cursor < 0:
                return
            key = (str(owner_user_id), str(meeting_id))
            with self._lock:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _Entry()
                    self._entries[key] = entry
                if input_cursor <= entry.cursor:
                    # Duplicate or stale ACK: nothing new to schedule.
                    return
                entry.cursor = input_cursor
                for index, request in enumerate(self._pending):
                    if (
                        request.owner_user_id == key[0]
                        and request.meeting_id == key[1]
                    ):
                        # Coalesce in place: keep one pending request per key.
                        self._pending[index] = LiveRequest(
                            key[0], key[1], input_cursor
                        )
                        entry.state = QUEUED
                        return
                if len(self._pending) >= self._capacity:
                    # Shed load instead of making the upload path wait.
                    entry.state = PAUSED
                    return
                self._pending.append(LiveRequest(key[0], key[1], input_cursor))
                entry.state = QUEUED
        except Exception:
            # Backpressure is never allowed to fail the durable upload path.
            return

    def take(self) -> LiveRequest | None:
        """Return the oldest pending request and mark that meeting running."""
        with self._lock:
            if not self._pending:
                return None
            request = self._pending.pop(0)
            entry = self._entries.get((request.owner_user_id, request.meeting_id))
            if entry is None:
                entry = _Entry(cursor=request.input_cursor)
                self._entries[(request.owner_user_id, request.meeting_id)] = entry
            entry.state = RUNNING
            return request

    def complete(self, owner_user_id: str, meeting_id: str) -> None:
        """Mark one meeting's live work finished; its cursor is retained."""
        key = (str(owner_user_id), str(meeting_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            # A newer request queued while this run was in flight still needs
            # its own turn, so finishing one run must not hide that backlog.
            still_pending = any(
                request.owner_user_id == key[0] and request.meeting_id == key[1]
                for request in self._pending
            )
            entry.state = QUEUED if still_pending else IDLE

    def snapshot(self, owner_user_id: str, meeting_id: str) -> dict[str, object]:
        """Return the degraded live state without touching a database."""
        with self._lock:
            entry = self._entries.get((str(owner_user_id), str(meeting_id)))
            if entry is None:
                return {"state": IDLE, "cursor": 0}
            return {"state": entry.state, "cursor": entry.cursor}

    def pending(self) -> int:
        """Number of queued meetings (diagnostics and tests only)."""
        with self._lock:
            return len(self._pending)


_scheduler = LiveScheduler()


def get_live_scheduler() -> LiveScheduler:
    return _scheduler


def reset_live_scheduler() -> LiveScheduler:
    """Replace the process-wide scheduler (tests and worker restarts)."""
    global _scheduler
    _scheduler = LiveScheduler()
    return _scheduler


def submit_live(meeting_id: str, owner_user_id: str, input_cursor: int) -> None:
    """Non-blocking hand-off used by the upload path after a chunk commit."""
    get_live_scheduler().submit(meeting_id, owner_user_id, input_cursor)


def snapshot(owner_user_id: str, meeting_id: str) -> dict[str, object]:
    """Read the degraded live state for one meeting without any I/O."""
    return get_live_scheduler().snapshot(owner_user_id, meeting_id)
