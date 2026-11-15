"""Privacy deletion, replayable tombstones and cleanup for the cloud recorder.

Deletion is a two-part promise. The database marks a meeting or account
deleted, and an append-only local JSONL tombstone records enough information
to replay that deletion after a database/object-store restore. The audit lives
outside PostgreSQL and the object store on purpose: restoring a backup must not
be able to erase the record that says this was deleted.

Ordering matters. A tombstone is appended *before* the database commit. If the
commit fails, a void record is appended so a later replay skips the tombstone.
There is still an unavoidable crash window: if the process dies after the
append and before the commit, replay would delete data the user never saw
deleted. A future transaction-id handshake or two-phase protocol would close
it; this module documents the window rather than pretending it does not exist.

Tombstones carry only identifiers and scope prefixes -- never audio,
transcripts, names, embeddings or secrets. delete_meeting and revoke_account
are called from request handlers; replay_tombstones and run_cleanup are
operator functions. There is deliberately no public account-deletion route or
export: account revocation is an operator action for now.

Event vocabulary note: EVENT_NAMES has no deletion event, so neither deletion
path writes a ResourceEvent. That is a known product gap, not an oversight.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, select, update

from cloud.auth import require_csrf
from cloud.contracts import AuthContext, NotFoundError, iso_utc, parse_iso_utc
from cloud.objects import CHUNK_PREFIX, DERIVED_PREFIX
from cloud.schema import (
    AuthSession,
    CaptureLease,
    Chunk,
    DerivedAsset,
    Meeting,
    Position,
    ProcessingTask,
    UploadSession,
    User,
    VoiceprintEnrollment,
    VoiceprintProfile,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cloud", tags=["privacy"])

TOMBSTONE_SCHEMA_VERSION = 1
TOMBSTONE_KINDS = ("meeting", "account", "void", "replay")

#: Lists every record carries so replay and cleanup can be independent.
_LIST_FIELDS = (
    "object_prefixes",
    "upload_ids",
    "profile_ids",
    "position_ids",
)

_ACTIVE_TASK_STATUSES = ("waiting", "running")


class TombstoneAuditError(ValueError):
    """The audit file is corrupt, unsafe or holds an invalid record."""


def _require_str(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise TombstoneAuditError("tombstone field %r must be a non-empty string" % field)
    return value


def _require_int(record: dict[str, Any], field: str, *, minimum: int = 0) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TombstoneAuditError(
            "tombstone field %r must be an integer >= %d" % (field, minimum)
        )
    return value


def validate_record(record: Any) -> dict[str, Any]:
    """Validate one tombstone record and return it unchanged.

    Validation is strict: an unknown kind, a missing field or a malformed
    timestamp is an audit failure, never something to skip silently.
    """

    if not isinstance(record, dict):
        raise TombstoneAuditError("tombstone record must be a JSON object")
    if record.get("schema_version") != TOMBSTONE_SCHEMA_VERSION:
        raise TombstoneAuditError("unsupported tombstone schema_version")
    kind = record.get("kind")
    if kind not in TOMBSTONE_KINDS:
        raise TombstoneAuditError("unknown tombstone kind %r" % (kind,))
    _require_str(record, "tombstone_id")
    _require_str(record, "owner_user_id")
    meeting_id = record.get("meeting_id")
    if meeting_id is not None and (not isinstance(meeting_id, str) or not meeting_id):
        raise TombstoneAuditError("tombstone field 'meeting_id' must be a string or null")
    occurred_at = _require_str(record, "occurred_at")
    try:
        parse_iso_utc(occurred_at)
    except ValueError as exc:
        raise TombstoneAuditError("tombstone field 'occurred_at' is not UTC ISO-8601") from exc
    _require_int(record, "generation")
    for field in _LIST_FIELDS:
        value = record.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise TombstoneAuditError("tombstone field %r must be a list of strings" % field)
    if kind == "meeting" and not meeting_id:
        raise TombstoneAuditError("meeting tombstone requires a meeting_id")
    if kind == "void":
        _require_str(record, "voids")
    if kind == "replay":
        _require_str(record, "backup_id")
        _require_int(record, "replayed_count")
    return record


def _serialize(record: dict[str, Any]) -> bytes:
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (encoded + "\n").encode("utf-8")


class TombstoneAuditStore:
    """Append-only, fsynced JSONL tombstone audit on the local filesystem."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        missing: list[Path] = []
        current = parent
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        if current.exists() and not current.is_dir():
            raise TombstoneAuditError("tombstone audit parent is not a directory")
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                continue
            os.chmod(directory, 0o700)

    def append(self, record: dict[str, Any]) -> None:
        validate_record(record)
        self._prepare_parent()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.path.is_symlink():
                raise TombstoneAuditError("tombstone audit path must not be a symlink")
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise TombstoneAuditError("cannot open tombstone audit for append") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = _serialize(record)
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            if self.path.is_symlink():
                raise TombstoneAuditError("tombstone audit path must not be a symlink")
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise TombstoneAuditError("tombstone audit path must be a regular file")
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TombstoneAuditError("cannot read tombstone audit") from exc
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise TombstoneAuditError("tombstone audit line %d is empty" % line_number)
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TombstoneAuditError(
                    "tombstone audit line %d is not valid JSON" % line_number
                ) from exc
            validate_record(record)
            records.append(record)
        return records


class MemoryTombstoneStore:
    """In-process audit for development and test defaults only.

    The restore test must not use this: durability is the whole point of the
    real store. This class lets a local development app run without a
    configured audit path.
    """

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        validate_record(record)
        with self._lock:
            self._records.append(json.loads(_serialize(record)))

    def read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(_serialize(record)) for record in self._records]


@dataclass(frozen=True)
class PrivacyRuntime:
    session_factory: Any
    settings: Any
    object_store: Any
    audit_store: Any


_runtime: PrivacyRuntime | None = None


def configure_runtime(
    session_factory: Any,
    settings: Any,
    object_store: Any,
    audit_store: Any,
) -> PrivacyRuntime:
    global _runtime
    _runtime = PrivacyRuntime(
        session_factory=session_factory,
        settings=settings,
        object_store=object_store,
        audit_store=audit_store,
    )
    return _runtime


def reset_runtime() -> None:
    global _runtime
    _runtime = None


def _require_runtime() -> PrivacyRuntime:
    if _runtime is None:
        raise RuntimeError("privacy runtime is not configured")
    return _runtime


def _audit_for(ctx: AuthContext) -> Any:
    store = ctx.audit
    if store is None and _runtime is not None:
        store = _runtime.audit_store
    if store is None:
        raise RuntimeError("tombstone audit is not configured")
    return store


def _record_base(
    *,
    kind: str,
    owner_user_id: str,
    meeting_id: str | None,
    occurred_at: datetime,
    generation: int,
    object_prefixes: list[str],
    upload_ids: list[str],
    profile_ids: list[str],
    position_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": TOMBSTONE_SCHEMA_VERSION,
        "kind": kind,
        "tombstone_id": str(uuid4()),
        "owner_user_id": owner_user_id,
        "meeting_id": meeting_id,
        "occurred_at": iso_utc(occurred_at),
        "generation": generation,
        "object_prefixes": object_prefixes,
        "upload_ids": upload_ids,
        "profile_ids": profile_ids,
        "position_ids": position_ids,
    }


def _void_record(record: dict[str, Any], occurred_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": TOMBSTONE_SCHEMA_VERSION,
        "kind": "void",
        "tombstone_id": str(uuid4()),
        "owner_user_id": record["owner_user_id"],
        "meeting_id": record["meeting_id"],
        "occurred_at": iso_utc(occurred_at),
        "generation": record["generation"],
        "object_prefixes": [],
        "upload_ids": [],
        "profile_ids": [],
        "position_ids": [],
        "voids": record["tombstone_id"],
    }


def _append_void_after_failure(store: Any, record: dict[str, Any], now: datetime) -> None:
    try:
        store.append(_void_record(record, now))
    except Exception:  # pragma: no cover - only the failure path reaches this
        log.exception("could not append void tombstone for %s", record.get("tombstone_id"))


def _delete_prefixes(objects: Any, prefixes: list[str]) -> int:
    """Best-effort object-prefix deletion; every failure is logged, not raised."""

    if objects is None:
        if prefixes:
            log.warning("object store unavailable; prefix deletion deferred")
        return 0
    deleted = 0
    for prefix in prefixes:
        try:
            keys = objects.list_keys(prefix)
        except Exception:
            log.warning("could not list object prefix %s; cleanup will retry", prefix, exc_info=True)
            continue
        for key in keys:
            try:
                objects.delete(key)
                deleted += 1
            except Exception:
                log.warning("could not delete object %s; cleanup will retry", key, exc_info=True)
    return deleted


def _cancel_task_row(task: ProcessingTask, now: datetime, error_code: str) -> None:
    task.status = "cancelled"
    task.error_code = error_code
    task.lease_token_hash = None
    task.lease_worker_id = None
    task.lease_deadline = None
    task.lease_acquired_at = None
    task.updated_at = now
    task.completed_at = now


def _release_meeting_leases(db: Any, owner_user_id: str, meeting_id: str, now: datetime) -> None:
    db.execute(
        update(CaptureLease)
        .where(
            CaptureLease.owner_user_id == owner_user_id,
            CaptureLease.meeting_id == meeting_id,
            CaptureLease.released_at.is_(None),
        )
        .values(released_at=now)
    )


def _cancel_meeting_tasks(db: Any, owner_user_id: str, meeting_id: str, now: datetime) -> None:
    tasks = db.scalars(
        select(ProcessingTask)
        .where(
            ProcessingTask.owner_user_id == owner_user_id,
            ProcessingTask.meeting_id == meeting_id,
            ProcessingTask.status.in_(_ACTIVE_TASK_STATUSES),
        )
        .with_for_update()
    ).all()
    for task in tasks:
        _cancel_task_row(task, now, "meeting_deleted")


def delete_meeting(ctx: AuthContext, meeting_id: str) -> None:
    """Soft-delete one owner meeting, cancel work and queue object removal."""

    db = ctx.require_db()
    now = ctx.clock.now()
    meeting = db.scalars(
        select(Meeting)
        .where(Meeting.owner_user_id == ctx.user_id, Meeting.id == meeting_id)
        .with_for_update()
    ).one_or_none()
    if meeting is None or meeting.deleted_at is not None:
        db.rollback()
        raise NotFoundError("meeting_not_found")
    upload_ids = list(
        db.scalars(
            select(UploadSession.id)
            .where(
                UploadSession.owner_user_id == ctx.user_id,
                UploadSession.meeting_id == meeting_id,
            )
            .order_by(UploadSession.id)
        ).all()
    )
    record = _record_base(
        kind="meeting",
        owner_user_id=ctx.user_id,
        meeting_id=meeting_id,
        occurred_at=now,
        generation=meeting.generation + 1,
        object_prefixes=[
            CHUNK_PREFIX + ctx.user_id + "/" + upload_id + "/" for upload_id in upload_ids
        ]
        + [DERIVED_PREFIX + ctx.user_id + "/" + meeting_id + "/"],
        upload_ids=upload_ids,
        profile_ids=[],
        position_ids=[],
    )
    store = _audit_for(ctx)
    store.append(record)
    try:
        meeting.deleted_at = now
        meeting.updated_at = now
        meeting.generation = meeting.generation + 1
        _release_meeting_leases(db, ctx.user_id, meeting_id, now)
        _cancel_meeting_tasks(db, ctx.user_id, meeting_id, now)
        db.commit()
    except Exception:
        db.rollback()
        _append_void_after_failure(store, record, now)
        raise
    _delete_prefixes(ctx.objects, record["object_prefixes"])


def _owned_uploads(db: Any, owner_user_id: str) -> list[str]:
    return list(
        db.scalars(
            select(UploadSession.id)
            .where(UploadSession.owner_user_id == owner_user_id)
            .order_by(UploadSession.id)
        ).all()
    )


def _active_profile_ids(db: Any, owner_user_id: str) -> list[str]:
    return list(
        db.scalars(
            select(VoiceprintProfile.id)
            .where(
                VoiceprintProfile.owner_user_id == owner_user_id,
                VoiceprintProfile.revoked_at.is_(None),
            )
            .order_by(VoiceprintProfile.id)
        ).all()
    )


def _owned_position_ids(db: Any, owner_user_id: str) -> list[str]:
    return list(
        db.scalars(
            select(Position.id)
            .where(Position.owner_user_id == owner_user_id)
            .order_by(Position.id)
        ).all()
    )


def _apply_account_effects(db: Any, owner_user_id: str, now: datetime) -> None:
    """Apply every account-revocation effect inside the caller's transaction."""

    user = db.get(User, owner_user_id)
    if user is not None and user.deleted_at is None:
        user.deleted_at = now
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == owner_user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.execute(
        update(CaptureLease)
        .where(
            CaptureLease.owner_user_id == owner_user_id,
            CaptureLease.released_at.is_(None),
        )
        .values(released_at=now)
    )
    tasks = db.scalars(
        select(ProcessingTask)
        .where(
            ProcessingTask.owner_user_id == owner_user_id,
            ProcessingTask.status.in_(_ACTIVE_TASK_STATUSES),
        )
        .with_for_update()
    ).all()
    for task in tasks:
        _cancel_task_row(task, now, "account_deleted")
    db.execute(
        update(VoiceprintProfile)
        .where(
            VoiceprintProfile.owner_user_id == owner_user_id,
            VoiceprintProfile.revoked_at.is_(None),
        )
        .values(revoked_at=now, revocation_reason="account_deleted")
    )
    db.execute(
        update(VoiceprintEnrollment)
        .where(VoiceprintEnrollment.owner_user_id == owner_user_id)
        .values(sample_ciphertext=None)
    )
    db.execute(delete(Position).where(Position.owner_user_id == owner_user_id))
    meetings = db.scalars(
        select(Meeting)
        .where(
            Meeting.owner_user_id == owner_user_id,
            Meeting.deleted_at.is_(None),
        )
        .with_for_update()
    ).all()
    for meeting in meetings:
        meeting.deleted_at = now
        meeting.updated_at = now
        meeting.generation = meeting.generation + 1


def revoke_account(ctx: AuthContext) -> None:
    """Revoke every trace of the calling account; operator-facing helper."""

    db = ctx.require_db()
    now = ctx.clock.now()
    user = db.scalars(select(User).where(User.id == ctx.user_id).with_for_update()).one_or_none()
    if user is None or user.deleted_at is not None:
        db.rollback()
        raise NotFoundError("user_not_found")
    upload_ids = _owned_uploads(db, ctx.user_id)
    profile_ids = _active_profile_ids(db, ctx.user_id)
    position_ids = _owned_position_ids(db, ctx.user_id)
    record = _record_base(
        kind="account",
        owner_user_id=ctx.user_id,
        meeting_id=None,
        occurred_at=now,
        generation=0,
        object_prefixes=[
            CHUNK_PREFIX + ctx.user_id + "/",
            DERIVED_PREFIX + ctx.user_id + "/",
        ],
        upload_ids=upload_ids,
        profile_ids=profile_ids,
        position_ids=position_ids,
    )
    store = _audit_for(ctx)
    store.append(record)
    try:
        _apply_account_effects(db, ctx.user_id, now)
        db.commit()
    except Exception:
        db.rollback()
        _append_void_after_failure(store, record, now)
        raise
    _delete_prefixes(ctx.objects, record["object_prefixes"])


def _voided_tombstone_ids(records: list[dict[str, Any]]) -> set[str]:
    """Tombstone ids explicitly voided by a later failed commit."""

    return {
        record["voids"]
        for record in records
        if record["kind"] == "void" and isinstance(record.get("voids"), str)
    }


def _replay_meeting(db: Any, record: dict[str, Any], now: datetime) -> None:
    owner = record["owner_user_id"]
    meeting = db.scalars(
        select(Meeting)
        .where(Meeting.owner_user_id == owner, Meeting.id == record["meeting_id"])
        .with_for_update()
    ).one_or_none()
    if meeting is not None:
        if meeting.deleted_at is None:
            meeting.deleted_at = now
            meeting.updated_at = now
        meeting.generation = max(meeting.generation, record["generation"])
        _release_meeting_leases(db, owner, meeting.id, now)
        _cancel_meeting_tasks(db, owner, meeting.id, now)


def _replay_account(db: Any, record: dict[str, Any], now: datetime) -> None:
    _apply_account_effects(db, record["owner_user_id"], now)


def replay_tombstones(backup_id: str) -> int:
    """Re-apply every non-voided tombstone over a restored database.

    Returns the number of meeting/account tombstones applied. Rows that were
    hard-purged are skipped, but their object prefixes are still deleted.
    The receipt appended at the end records the restore for later audits.
    """

    runtime = _require_runtime()
    records = runtime.audit_store.read_all()
    voided = _voided_tombstone_ids(records)
    ordered = sorted(
        (
            record
            for record in records
            if record["kind"] in ("meeting", "account")
            and record["tombstone_id"] not in voided
        ),
        key=lambda record: (parse_iso_utc(record["occurred_at"]), record["tombstone_id"]),
    )
    applied = 0
    for record in ordered:
        now = datetime.now(timezone.utc)
        with runtime.session_factory() as db:
            if record["kind"] == "meeting":
                _replay_meeting(db, record, now)
            else:
                _replay_account(db, record, now)
            db.commit()
        _delete_prefixes(runtime.object_store, record["object_prefixes"])
        applied += 1
    receipt = {
        "schema_version": TOMBSTONE_SCHEMA_VERSION,
        "kind": "replay",
        "tombstone_id": str(uuid4()),
        "owner_user_id": "system",
        "meeting_id": None,
        "occurred_at": iso_utc(datetime.now(timezone.utc)),
        "generation": 0,
        "object_prefixes": [],
        "upload_ids": [],
        "profile_ids": [],
        "position_ids": [],
        "backup_id": backup_id,
        "replayed_count": applied,
    }
    runtime.audit_store.append(receipt)
    return applied


def _purge_ephemeral(root: Path, cutoff: datetime) -> tuple[int, int]:
    removed = 0
    errors = 0
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1
    for entry in entries:
        try:
            stat = entry.stat(follow_symlinks=False)
            old = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) <= cutoff
            if entry.is_symlink():
                # Never follow a link out of the ephemeral root; delete the link.
                if old:
                    os.unlink(entry.path)
                    removed += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                nested_removed, nested_errors = _purge_ephemeral(Path(entry.path), cutoff)
                removed += nested_removed
                errors += nested_errors
                if old:
                    try:
                        os.rmdir(entry.path)
                        removed += 1
                    except OSError:
                        pass
                continue
            if old:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            errors += 1
    return removed, errors


def run_cleanup(now: datetime) -> dict[str, int]:
    """Retry pending object deletion, purge aged rows and expired scratch data.

    Idempotent and scheduler-free: call it from an operator cron. Every failure
    is counted in errors instead of aborting the remaining work.
    """

    runtime = _require_runtime()
    settings = runtime.settings
    counts = {
        "tombstone_objects_deleted": 0,
        "orphan_objects_deleted": 0,
        "meetings_purged": 0,
        "users_purged": 0,
        "ephemeral_removed": 0,
        "scratch_removed": 0,
        "errors": 0,
    }
    records = runtime.audit_store.read_all()
    # A void means the database deletion never committed, so its prefixes are
    # still live data. Replay skips these records; cleanup must skip them too.
    voided = _voided_tombstone_ids(records)
    prefixes: list[str] = []
    for record in records:
        if (
            record["kind"] in ("meeting", "account")
            and record["tombstone_id"] not in voided
        ):
            prefixes.extend(record["object_prefixes"])

    objects = runtime.object_store
    # Abandoned .put-* staging files are not live objects; sweep them before the
    # orphan pass so a crashed write can never look like a chunk to keep.
    sweeper = getattr(objects, "sweep_stale_temp", None)
    if callable(sweeper):
        try:
            counts["scratch_removed"] = sweeper(
                max_age_s=max(3600, settings.ephemeral_cleanup_s)
            )
        except Exception as exc:  # pragma: no cover - defensive
            counts["errors"] += 1
            log.warning("scratch_sweep: %s", exc)
    if objects is not None:
        for prefix in prefixes:
            try:
                keys = objects.list_keys(prefix)
            except Exception:
                counts["errors"] += 1
                continue
            for key in keys:
                try:
                    objects.delete(key)
                    counts["tombstone_objects_deleted"] += 1
                except Exception:
                    counts["errors"] += 1

        referenced: set[str] = set()
        references_loaded = False
        try:
            with runtime.session_factory() as db:
                referenced.update(db.scalars(select(Chunk.object_key)).all())
                referenced.update(db.scalars(select(DerivedAsset.object_key)).all())
            references_loaded = True
        except Exception:
            counts["errors"] += 1
        # Without a complete reference set, orphan deletion could destroy live
        # chunks, so it is skipped rather than guessed.
        if references_loaded:
            threshold = now - timedelta(seconds=max(3600, settings.ephemeral_cleanup_s))
            for prefix in (CHUNK_PREFIX, DERIVED_PREFIX):
                try:
                    listed = objects.list_objects(prefix)
                except Exception:
                    counts["errors"] += 1
                    continue
                for key, last_modified in listed:
                    if key in referenced:
                        continue
                    if last_modified > threshold:
                        continue
                    try:
                        objects.delete(key)
                        counts["orphan_objects_deleted"] += 1
                    except Exception:
                        counts["errors"] += 1

    try:
        with runtime.session_factory() as db:
            meeting_cutoff = now - timedelta(seconds=settings.cleanup_target_s)
            user_cutoff = now - timedelta(seconds=settings.account_revocation_wait_s)
            result = db.execute(
                delete(Meeting).where(
                    Meeting.deleted_at.is_not(None),
                    Meeting.deleted_at <= meeting_cutoff,
                )
            )
            counts["meetings_purged"] = int(result.rowcount or 0)
            result = db.execute(
                delete(User).where(
                    User.deleted_at.is_not(None),
                    User.deleted_at <= user_cutoff,
                )
            )
            counts["users_purged"] = int(result.rowcount or 0)
            db.commit()
    except Exception:
        counts["errors"] += 1

    if settings.ephemeral_root:
        removed, errors = _purge_ephemeral(
            Path(settings.ephemeral_root),
            now - timedelta(seconds=settings.ephemeral_cleanup_s),
        )
        counts["ephemeral_removed"] = removed
        counts["errors"] += errors
    return counts


@router.delete("/meetings/{meeting_id}", status_code=204)
def delete_meeting_route(
    meeting_id: str,
    ctx: AuthContext = Depends(require_csrf),
) -> Response:
    delete_meeting(ctx, meeting_id)
    return Response(status_code=204)
