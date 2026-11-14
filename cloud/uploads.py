"""Private chunk uploads with durable, idempotent acknowledgements.

Ordering is the contract (design §7.2):

1. validate the request (size, real SHA-256, allowed codec, ids),
2. write the object to private S3 storage,
3. commit the chunk row,
4. only then ACK.

A crash between (2) and (3) leaves an unreferenced object that is retryable
with the same idempotency key and discoverable by :func:`scan_orphans`; no
ACK is ever sent for a chunk that is not durable in Postgres.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from cloud import events
from cloud.auth import require_csrf, require_user
from cloud.live import submit_live
from cloud.contracts import (
    AckView,
    AuthContext,
    CaptureEpoch,
    ChunkMeta,
    CloudError,
    ConflictError,
    Gap,
    InvalidRequestError,
    IsoUtc,
    NotFoundError,
    PayloadTooLargeError,
    TrackSpec,
    iso_utc,
    parse_iso_utc,
)
from cloud.leases import LEASE_HEADER, enforce_capture_lease, resolve_lease
from cloud.objects import CHUNK_PREFIX, chunk_object_key
from cloud.schema import (
    Chunk,
    Meeting,
    UploadEpoch,
    UploadEpochTrack,
    UploadSession,
    UploadTrack,
)

#: Codecs a browser recorder may send (Task 2 capture contract, in priority
#: order; order is enforced client-side, the server only allow-lists).
ALLOWED_CODECS = frozenset(
    {
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
        "audio/ogg",
        "audio/mp4",
        "audio/mpeg",
    }
)
MAX_TRACKS_PER_EPOCH = 2
#: A refresh/re-auth anchor is never claimed to be exact (Task 7): a new epoch
#: carries at least this much clock uncertainty.
CROSS_EPOCH_UNCERTAINTY_MS = 1000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

router = APIRouter(prefix="/api/cloud", tags=["uploads"])


def _now(ctx: AuthContext) -> datetime:
    return ctx.clock.now()


def _find_upload(ctx: AuthContext, upload_id: str) -> UploadSession:
    upload = ctx.require_db().get(UploadSession, (ctx.user_id, upload_id))
    if upload is None:
        raise NotFoundError("upload_not_found")
    return upload


def _recovery_deadline(ctx: AuthContext, upload: UploadSession) -> datetime:
    return upload.created_at + timedelta(seconds=ctx.settings.recovery_window_s)


def _require_open(ctx: AuthContext, upload: UploadSession, now: datetime) -> None:
    if upload.status != "open":
        raise ConflictError("upload_closed")
    if now > _recovery_deadline(ctx, upload):
        raise ConflictError("recovery_window_expired")


def _newest_epoch(ctx: AuthContext, upload_id: str) -> UploadEpoch | None:
    return ctx.require_db().scalar(
        select(UploadEpoch)
        .where(
            UploadEpoch.owner_user_id == ctx.user_id,
            UploadEpoch.upload_session_id == upload_id,
        )
        .order_by(UploadEpoch.created_at.desc(), UploadEpoch.id.desc())
        .limit(1)
    )


def _get_epoch(ctx: AuthContext, upload_id: str, epoch_id: str) -> UploadEpoch:
    epoch = ctx.require_db().get(UploadEpoch, (ctx.user_id, upload_id, epoch_id))
    if epoch is None:
        raise ConflictError("unknown_epoch")
    return epoch


def _get_track(ctx: AuthContext, upload_id: str, track_id: str) -> UploadTrack:
    track = ctx.require_db().get(UploadTrack, (ctx.user_id, upload_id, track_id))
    if track is None:
        raise ConflictError("unregistered_track")
    return track


def _require_epoch_track(ctx: AuthContext, upload_id: str, epoch_id: str, track_id: str) -> None:
    link = ctx.require_db().get(
        UploadEpochTrack, (ctx.user_id, upload_id, epoch_id, track_id)
    )
    if link is None:
        raise ConflictError("unknown_epoch_track")


def _ack_view(ctx: AuthContext, upload_id: str, *, duplicate: bool) -> AckView:
    """Describe the saved position: the intersection of every epoch track.

    The saved position is the contiguous confirmed span, never the maximum
    sequence: a hole in the middle still reports the position before it. It is
    also never the newest epoch alone; a page reload that starts a mic-only
    epoch must not move the position past the last confirmed system audio.
    """
    db = ctx.require_db()
    rows = db.execute(
        select(UploadEpochTrack, UploadEpoch)
        .join(
            UploadEpoch,
            (UploadEpoch.owner_user_id == UploadEpochTrack.owner_user_id)
            & (UploadEpoch.upload_session_id == UploadEpochTrack.upload_session_id)
            & (UploadEpoch.id == UploadEpochTrack.epoch_id),
        )
        .where(
            UploadEpochTrack.owner_user_id == ctx.user_id,
            UploadEpochTrack.upload_session_id == upload_id,
        )
        .order_by(UploadEpoch.created_at, UploadEpoch.id, UploadEpochTrack.track_id)
    ).all()
    positions: list[int] = []
    missing: list[Gap] = []
    for link, epoch in rows:
        chunks = list(
            db.scalars(
                select(Chunk)
                .where(
                    Chunk.owner_user_id == ctx.user_id,
                    Chunk.upload_session_id == upload_id,
                    Chunk.epoch_id == link.epoch_id,
                    Chunk.track_id == link.track_id,
                )
                .order_by(Chunk.sequence)
            )
        )
        contiguous, gaps = _track_prefix(
            link.track_id, chunks, start=epoch.offset_ms
        )
        positions.append(contiguous)
        missing.extend(gaps)
    contiguous_ms = min(positions) if positions else 0
    return AckView(
        accepted=True,
        duplicate=duplicate,
        contiguous_ms=contiguous_ms,
        missing=missing,
    )


def _track_prefix(
    track_id: str, chunks: list[Chunk], *, start: int = 0
) -> tuple[int, list[Gap]]:
    expected = 0
    contiguous = start
    for chunk in chunks:
        if chunk.sequence != expected:
            return contiguous, [
                Gap(
                    track_id=track_id,
                    start_ms=contiguous,
                    end_ms=chunk.start_ms,
                    reason="missing_chunks",
                )
            ]
        if chunk.start_ms > contiguous:
            return contiguous, [
                Gap(
                    track_id=track_id,
                    start_ms=contiguous,
                    end_ms=chunk.start_ms,
                    reason="missing_chunks",
                )
            ]
        contiguous = chunk.end_ms
        expected += 1
    return contiguous, []


def _validate_chunk(
    ctx: AuthContext, meta: ChunkMeta, body: bytes
) -> None:
    if not body:
        raise InvalidRequestError("empty_body")
    if len(body) > ctx.settings.request_max_bytes:
        raise PayloadTooLargeError()
    digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(digest, meta.checksum):
        raise InvalidRequestError("checksum_mismatch")
    if meta.codec not in ALLOWED_CODECS:
        raise InvalidRequestError("unsupported_codec")
    if not _ID_RE.match(meta.capture_epoch_id):
        raise InvalidRequestError("invalid_epoch_id")
    if not _ID_RE.match(meta.track_id):
        raise InvalidRequestError("invalid_track_id")
    if meta.end_ms <= meta.start_ms:
        raise InvalidRequestError("invalid_time_range")


def _identical(row: Chunk, meta: ChunkMeta) -> bool:
    return (
        row.checksum == meta.checksum
        and row.start_ms == meta.start_ms
        and row.end_ms == meta.end_ms
        and row.codec == meta.codec
    )


def _existing_chunk(ctx: AuthContext, meta: ChunkMeta) -> Chunk | None:
    return ctx.require_db().scalar(
        select(Chunk).where(
            Chunk.owner_user_id == ctx.user_id,
            Chunk.upload_session_id == meta.upload_session_id,
            Chunk.epoch_id == meta.capture_epoch_id,
            Chunk.track_id == meta.track_id,
            Chunk.sequence == meta.sequence,
        )
    )


def _require_no_overlap(ctx: AuthContext, meta: ChunkMeta) -> None:
    rows = ctx.require_db().scalars(
        select(Chunk).where(
            Chunk.owner_user_id == ctx.user_id,
            Chunk.upload_session_id == meta.upload_session_id,
            Chunk.epoch_id == meta.capture_epoch_id,
            Chunk.track_id == meta.track_id,
        )
    )
    for row in rows:
        if row.start_ms < meta.end_ms and meta.start_ms < row.end_ms:
            raise ConflictError("overlapping_time")


def put_chunk(
    ctx: AuthContext,
    meta: ChunkMeta,
    body: bytes,
    capture_lease_token: str | None,
) -> AckView:
    """Persist one chunk then ACK it; safe to retry with the same identity."""
    db = ctx.require_db()
    _validate_chunk(ctx, meta, body)

    upload = _find_upload(ctx, meta.upload_session_id)
    now = _now(ctx)
    _require_open(ctx, upload, now)
    _get_epoch(ctx, meta.upload_session_id, meta.capture_epoch_id)
    _get_track(ctx, meta.upload_session_id, meta.track_id)
    _require_epoch_track(ctx, meta.upload_session_id, meta.capture_epoch_id, meta.track_id)

    # An already ACKed chunk re-ACKs (lost-ACK retry) before any lease check:
    # the row is durable, so a stale or expired lease must not fail the retry.
    existing = _existing_chunk(ctx, meta)
    if existing is not None:
        if _identical(existing, meta):
            return _ack_view(ctx, meta.upload_session_id, duplicate=True)
        raise ConflictError("chunk_conflict")

    # While a live capture runs for this meeting, only its lease token may
    # append. With no live lease (expired/released) we are in the recovery
    # path: already-registered epochs still accept chunks inside the window.
    enforce_capture_lease(ctx, capture_lease_token, meeting_id=upload.meeting_id)

    _require_no_overlap(ctx, meta)

    key = chunk_object_key(
        ctx.user_id,
        meta.upload_session_id,
        meta.capture_epoch_id,
        meta.track_id,
        meta.sequence,
    )
    store = ctx.require_objects()
    store.put_verified(key, body, meta.checksum)

    db.add(
        Chunk(
            id=str(uuid4()),
            owner_user_id=ctx.user_id,
            upload_session_id=meta.upload_session_id,
            epoch_id=meta.capture_epoch_id,
            track_id=meta.track_id,
            sequence=meta.sequence,
            start_ms=meta.start_ms,
            end_ms=meta.end_ms,
            checksum=meta.checksum,
            codec=meta.codec,
            object_key=key,
            byte_size=len(body),
            created_at=now,
        )
    )
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=upload.meeting_id,
        event="upload.progress",
        data={
            "upload_session_id": meta.upload_session_id,
            "capture_epoch_id": meta.capture_epoch_id,
            "track_id": meta.track_id,
            "sequence": meta.sequence,
            "start_ms": meta.start_ms,
            "end_ms": meta.end_ms,
            "created_at": iso_utc(now),
        },
        now=now,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raced = _existing_chunk(ctx, meta)
        if raced is not None and _identical(raced, meta):
            return _ack_view(ctx, meta.upload_session_id, duplicate=True)
        raise ConflictError("chunk_conflict") from exc
    except SQLAlchemyError as exc:
        # The object stays in place: it is unreferenced (no Chunk row), so the
        # same idempotency key can retry and the orphan scan can find it.
        db.rollback()
        raise CloudError("commit_failed") from exc
    ack = _ack_view(ctx, meta.upload_session_id, duplicate=False)
    # The chunk is durable now; live scheduling is an O(1) side channel and
    # must never delay or fail the ACK.
    try:
        submit_live(upload.meeting_id, ctx.user_id, ack.contiguous_ms)
    except Exception:
        pass
    return ack


def get_ack(ctx: AuthContext, upload_session_id: str) -> AckView:
    _find_upload(ctx, upload_session_id)
    return _ack_view(ctx, upload_session_id, duplicate=False)


def create_upload(
    ctx: AuthContext, meeting_id: str, capture_lease_token: str | None = None
) -> UploadSession:
    db = ctx.require_db()
    meeting = db.get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    if capture_lease_token:
        resolve_lease(ctx, capture_lease_token, meeting_id=meeting_id)
    now = _now(ctx)
    upload = UploadSession(
        owner_user_id=ctx.user_id,
        id=str(uuid4()),
        meeting_id=meeting_id,
        status="open",
        created_at=now,
        updated_at=now,
    )
    db.add(upload)
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=meeting_id,
        event="upload.created",
        data={
            "upload_session_id": upload.id,
            "status": upload.status,
            "created_at": iso_utc(upload.created_at),
        },
        now=now,
    )
    db.commit()
    return upload


def _with_anchor_uncertainty(
    ctx: AuthContext, upload_id: str, epoch: CaptureEpoch
) -> CaptureEpoch:
    """Never claim a refreshed clock origin is exact (Task 7, design §7.2).

    A new epoch after a refresh/re-auth keeps its real interruption. Its anchor
    also gains uncertainty: at least CROSS_EPOCH_UNCERTAINTY_MS, and more when
    the new offset would overlap audio the server already holds.
    """
    db = ctx.require_db()
    previous = db.scalar(
        select(UploadEpoch.id)
        .where(
            UploadEpoch.owner_user_id == ctx.user_id,
            UploadEpoch.upload_session_id == upload_id,
        )
        .limit(1)
    )
    if previous is None:
        return epoch
    max_end = db.scalar(
        select(func.max(Chunk.end_ms)).where(
            Chunk.owner_user_id == ctx.user_id,
            Chunk.upload_session_id == upload_id,
        )
    )
    uncertainty = max(
        epoch.uncertainty_ms,
        CROSS_EPOCH_UNCERTAINTY_MS,
        (max_end or 0) - epoch.offset_ms,
        0,
    )
    if uncertainty == epoch.uncertainty_ms:
        return epoch
    return epoch.model_copy(update={"uncertainty_ms": uncertainty})


def register_epoch(
    ctx: AuthContext,
    upload_id: str,
    epoch: CaptureEpoch,
    tracks: list[TrackSpec],
    capture_lease_token: str | None,
) -> CaptureEpoch:
    """Register a new capture epoch (new clock origin) and its tracks."""
    db = ctx.require_db()
    upload = _find_upload(ctx, upload_id)
    now = _now(ctx)
    _require_open(ctx, upload, now)
    resolve_lease(ctx, capture_lease_token, meeting_id=upload.meeting_id)

    if not _ID_RE.match(epoch.id):
        raise InvalidRequestError("invalid_epoch_id")
    if db.get(UploadEpoch, (ctx.user_id, upload_id, epoch.id)) is not None:
        raise ConflictError("epoch_exists")
    epoch = _with_anchor_uncertainty(ctx, upload_id, epoch)

    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    for spec in tracks:
        if not _ID_RE.match(spec.id):
            raise InvalidRequestError("invalid_track_id")
        if spec.codec not in ALLOWED_CODECS:
            raise InvalidRequestError("unsupported_codec")
        if spec.id in seen_ids or spec.source in seen_sources:
            raise ConflictError("track_conflict")
        seen_ids.add(spec.id)
        seen_sources.add(spec.source)
        existing = db.get(UploadTrack, (ctx.user_id, upload_id, spec.id))
        if existing is not None and (
            existing.source != spec.source
            or existing.codec != spec.codec
            or existing.sample_rate != spec.sample_rate
        ):
            raise ConflictError("track_conflict")

    db.add(
        UploadEpoch(
            owner_user_id=ctx.user_id,
            upload_session_id=upload_id,
            id=epoch.id,
            offset_ms=epoch.offset_ms,
            uncertainty_ms=epoch.uncertainty_ms,
            started_at=parse_iso_utc(epoch.started_at),
            created_at=now,
        )
    )
    for spec in tracks:
        if db.get(UploadTrack, (ctx.user_id, upload_id, spec.id)) is None:
            db.add(
                UploadTrack(
                    owner_user_id=ctx.user_id,
                    upload_session_id=upload_id,
                    id=spec.id,
                    source=spec.source,
                    codec=spec.codec,
                    sample_rate=spec.sample_rate,
                    created_at=now,
                )
            )
    # Flush tracks before the epoch/track links: the composite FK is declared on
    # the table, not via a relationship, so order it explicitly.
    db.flush()
    for spec in tracks:
        db.add(
            UploadEpochTrack(
                owner_user_id=ctx.user_id,
                upload_session_id=upload_id,
                epoch_id=epoch.id,
                track_id=spec.id,
            )
        )
    events.record(
        db,
        owner_user_id=ctx.user_id,
        meeting_id=upload.meeting_id,
        event="upload.updated",
        data={
            "upload_session_id": upload_id,
            "capture_epoch_id": epoch.id,
            "tracks": [spec.id for spec in tracks],
            "created_at": iso_utc(now),
        },
        now=now,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("epoch_conflict") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise CloudError("commit_failed") from exc
    return epoch


async def read_bounded_body(request: Any, limit: int) -> bytes:
    """Read the body, refusing to consume past ``limit`` bytes."""
    parts: list[bytes] = []
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > limit:
            raise PayloadTooLargeError()
        parts.append(part)
    return b"".join(parts)


def scan_orphans(
    db: Any,
    store: Any,
    *,
    prefix: str = CHUNK_PREFIX,
    min_age_s: int = 0,
    now: datetime | None = None,
) -> list[str]:
    """Return stored object keys under ``prefix`` that no Chunk references.

    Objects younger than ``min_age_s`` may be mid-upload (object written, row
    not committed yet) and are skipped when a clock is supplied.
    """
    referenced = set(
        db.scalars(select(Chunk.object_key).where(Chunk.object_key.like(prefix + "%")))
    )
    orphans: list[str] = []
    for key, last_modified in store.list_objects(prefix):
        if key in referenced:
            continue
        if min_age_s and now is not None and last_modified is not None:
            if (now - last_modified).total_seconds() < min_age_s:
                continue
        orphans.append(key)
    return sorted(orphans)


def delete_orphans(db: Any, store: Any, **kwargs: Any) -> list[str]:
    """Delete unreferenced objects found by :func:`scan_orphans`."""
    keys = scan_orphans(db, store, **kwargs)
    for key in keys:
        store.delete(key)
    return keys


# -- HTTP surface ----------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateUploadRequest(_Body):
    meeting_id: str = Field(min_length=1, max_length=64)


class RegisterEpochRequest(_Body):
    id: str = Field(min_length=1, max_length=64)
    offset_ms: int = Field(ge=0)
    uncertainty_ms: int = Field(ge=0)
    started_at: IsoUtc
    tracks: list[TrackSpec] = Field(min_length=1, max_length=MAX_TRACKS_PER_EPOCH)

    def epoch(self) -> CaptureEpoch:
        return CaptureEpoch(
            id=self.id,
            offset_ms=self.offset_ms,
            uncertainty_ms=self.uncertainty_ms,
            started_at=self.started_at,
        )


class UploadView(_Body):
    id: str
    meeting_id: str
    status: str
    created_at: IsoUtc


def _upload_view(upload: UploadSession) -> UploadView:
    return UploadView(
        id=upload.id,
        meeting_id=upload.meeting_id,
        status=upload.status,
        created_at=iso_utc(upload.created_at),
    )


@router.post("/uploads", response_model=UploadView, status_code=201)
def create_upload_route(
    payload: CreateUploadRequest,
    request: Request,
    ctx: AuthContext = Depends(require_csrf),
) -> UploadView:
    upload = create_upload(
        ctx, payload.meeting_id, request.headers.get(LEASE_HEADER)
    )
    return _upload_view(upload)


@router.post("/uploads/{upload_id}/epochs", response_model=CaptureEpoch)
def register_epoch_route(
    upload_id: str,
    payload: RegisterEpochRequest,
    request: Request,
    ctx: AuthContext = Depends(require_csrf),
) -> CaptureEpoch:
    return register_epoch(
        ctx,
        upload_id,
        payload.epoch(),
        payload.tracks,
        request.headers.get(LEASE_HEADER),
    )


@router.put("/uploads/{upload_id}/chunks/{epoch_id}/{track_id}/{sequence}", response_model=AckView)
async def put_chunk_route(
    request: Request,
    upload_id: str,
    epoch_id: str,
    track_id: str,
    sequence: Annotated[int, Path(ge=0)],
    start_ms: Annotated[int, Query(ge=0)],
    end_ms: Annotated[int, Query(ge=0)],
    checksum: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    codec: Annotated[str, Query(min_length=1, max_length=64)],
    ctx: AuthContext = Depends(require_csrf),
) -> AckView:
    try:
        meta = ChunkMeta(
            upload_session_id=upload_id,
            capture_epoch_id=epoch_id,
            track_id=track_id,
            sequence=sequence,
            start_ms=start_ms,
            end_ms=end_ms,
            checksum=checksum,
            codec=codec,
        )
    except ValidationError as exc:
        # Surface malformed chunk metadata like any other request validation
        # error (422), not as a domain conflict.
        raise RequestValidationError(exc.errors()) from exc

    limit = ctx.settings.request_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError()
    body = await read_bounded_body(request, limit)
    return await run_in_threadpool(
        put_chunk, ctx, meta, body, request.headers.get(LEASE_HEADER)
    )


@router.get("/uploads/{upload_id}/ack", response_model=AckView)
def get_ack_route(
    upload_id: str, ctx: AuthContext = Depends(require_user)
) -> AckView:
    return get_ack(ctx, upload_id)
