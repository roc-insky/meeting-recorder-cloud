"""Sealing: turn a finished upload into complete or visibly partial.

Task 7 contract (design §7.2):

* sealing is transactional: it verifies the client manifest against the rows
  the server actually ACKed, freezes input_version, and only then flips the
  upload to sealed. A refused seal leaves the upload open and appendable.
* a missing tail or a shorter track never yields complete: with
  accept_partial=False the request is refused; with accept_partial=True the
  result is partial with explicit, durable gaps.
* expiry sealing (seal_expired) only closes stale uploads. It never deletes an
  ACKed object and never claims a recording is complete without a manifest.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from cloud import uploads
from cloud.auth import require_csrf
from cloud.contracts import (
    AuthContext,
    CaptureEpoch,
    CloudError,
    ConflictError,
    Gap,
    InvalidRequestError,
    SealRequest,
    SealView,
    TrackManifest,
    TrackSpec,
)
from cloud.db import create_database_engine, make_session_factory
from cloud.leases import release_live_capture
from cloud.schema import Chunk, UploadEpoch, UploadEpochTrack, UploadSession
from cloud.settings import Settings

router = APIRouter(prefix="/api/cloud", tags=["sealing"])


def _pairs(ctx: AuthContext, upload_id: str) -> list[Any]:
    return ctx.require_db().execute(
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


def _chunks(
    db: Any, ctx: AuthContext, upload_id: str, epoch_id: str, track_id: str
) -> list[Chunk]:
    return list(
        db.scalars(
            select(Chunk)
            .where(
                Chunk.owner_user_id == ctx.user_id,
                Chunk.upload_session_id == upload_id,
                Chunk.epoch_id == epoch_id,
                Chunk.track_id == track_id,
            )
            .order_by(Chunk.sequence)
        )
    )


def _dedupe(gaps: list[Gap]) -> list[Gap]:
    seen: set[tuple[str, int, int, str]] = set()
    unique: list[Gap] = []
    for gap in gaps:
        key = (gap.track_id, gap.start_ms, gap.end_ms, gap.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(gap)
    return unique


def seal_upload(
    ctx: AuthContext, upload_session_id: str, manifest: SealRequest
) -> SealView:
    """Verify the manifest, freeze input_version and seal atomically."""
    db = ctx.require_db()
    upload = uploads._find_upload(ctx, upload_session_id)
    now = ctx.clock.now()
    uploads._require_open(ctx, upload, now)

    rows = _pairs(ctx, upload_session_id)
    registered = {(link.epoch_id, link.track_id) for link, _ in rows}

    entries: dict[tuple[str, str], TrackManifest] = {}
    for entry in manifest.tracks:
        key = (entry.capture_epoch_id, entry.track_id)
        if key in entries:
            raise InvalidRequestError("duplicate_manifest_entry")
        entries[key] = entry
    if any(key not in registered for key in entries):
        raise ConflictError("unknown_epoch_track")
    if registered - entries.keys():
        # Never let a client forget a track it registered and call it complete.
        raise ConflictError("incomplete_manifest")

    gaps: list[Gap] = []
    track_ends: dict[str, int] = {}
    overall_end = 0
    for link, epoch in rows:
        entry = entries[(link.epoch_id, link.track_id)]
        chunks = _chunks(db, ctx, upload_session_id, link.epoch_id, link.track_id)
        server_last = chunks[-1].sequence if chunks else -1
        server_end = chunks[-1].end_ms if chunks else epoch.offset_ms
        if entry.last_sequence < server_last:
            # The manifest was built before a recovered chunk landed.
            raise ConflictError("manifest_stale")
        if entry.last_sequence == server_last and entry.end_ms != server_end:
            raise ConflictError("manifest_stale")
        if entry.last_sequence > server_last:
            if entry.end_ms <= server_end:
                raise ConflictError("manifest_stale")
            # The client claims audio beyond the last ACKed sequence.
            gaps.append(
                Gap(
                    track_id=link.track_id,
                    start_ms=server_end,
                    end_ms=entry.end_ms,
                    reason="missing_chunks",
                )
            )
        _, prefix_gaps = uploads._track_prefix(
            link.track_id, chunks, start=epoch.offset_ms
        )
        gaps.extend(prefix_gaps)
        track_ends[link.track_id] = max(
            track_ends.get(link.track_id, 0), server_end
        )
        overall_end = max(overall_end, server_end, entry.end_ms)

    gaps = _dedupe(gaps)
    for track_id, track_end in track_ends.items():
        if track_end >= overall_end:
            continue
        if any(gap.track_id == track_id and gap.end_ms >= overall_end for gap in gaps):
            continue
        gaps.append(
            Gap(
                track_id=track_id,
                start_ms=track_end,
                end_ms=overall_end,
                reason="missing_track" if track_end == 0 else "missing_tail",
            )
        )
    gaps = _dedupe(gaps)

    status = "complete" if not gaps else "partial"
    if gaps and not manifest.accept_partial:
        raise ConflictError("incomplete_recording")

    input_version = int(
        db.scalar(
            select(func.count(Chunk.id)).where(
                Chunk.owner_user_id == ctx.user_id,
                Chunk.upload_session_id == upload_session_id,
            )
        )
        or 0
    )
    upload.status = "sealed"
    upload.sealed_status = status
    upload.input_version = input_version
    upload.seal_gaps = [gap.model_dump() for gap in gaps]
    upload.sealed_at = now
    upload.updated_at = now
    release_live_capture(ctx, upload.meeting_id, now)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise CloudError("commit_failed") from exc
    return SealView(input_version=input_version, status=status, gaps=gaps)


def _seal_expired(db: Any, settings: Settings, now: datetime) -> int:
    deadline = now - timedelta(seconds=settings.recovery_window_s)
    stale = list(
        db.scalars(
            select(UploadSession).where(
                UploadSession.status == "open",
                UploadSession.created_at < deadline,
            )
        )
    )
    sealed = 0
    for upload in stale:
        rows = db.execute(
            select(UploadEpochTrack, UploadEpoch)
            .join(
                UploadEpoch,
                (UploadEpoch.owner_user_id == UploadEpochTrack.owner_user_id)
                & (
                    UploadEpoch.upload_session_id
                    == UploadEpochTrack.upload_session_id
                )
                & (UploadEpoch.id == UploadEpochTrack.epoch_id),
            )
            .where(
                UploadEpochTrack.owner_user_id == upload.owner_user_id,
                UploadEpochTrack.upload_session_id == upload.id,
            )
            .order_by(UploadEpoch.created_at, UploadEpoch.id, UploadEpochTrack.track_id)
        ).all()
        gaps: list[Gap] = []
        for link, epoch in rows:
            chunks = list(
                db.scalars(
                    select(Chunk)
                    .where(
                        Chunk.owner_user_id == upload.owner_user_id,
                        Chunk.upload_session_id == upload.id,
                        Chunk.epoch_id == link.epoch_id,
                        Chunk.track_id == link.track_id,
                    )
                    .order_by(Chunk.sequence)
                )
            )
            _, prefix_gaps = uploads._track_prefix(
                link.track_id, chunks, start=epoch.offset_ms
            )
            gaps.extend(prefix_gaps)
        upload.status = "sealed"
        # Without a client manifest a timeout can never prove completeness.
        upload.sealed_status = "partial"
        upload.input_version = int(
            db.scalar(
                select(func.count(Chunk.id)).where(
                    Chunk.owner_user_id == upload.owner_user_id,
                    Chunk.upload_session_id == upload.id,
                )
            )
            or 0
        )
        upload.seal_gaps = [gap.model_dump() for gap in _dedupe(gaps)]
        upload.sealed_at = now
        upload.updated_at = now
        sealed += 1
    if sealed:
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise CloudError("commit_failed") from exc
    return sealed


def seal_expired(
    now: datetime, *, db: Any = None, settings: Settings | None = None
) -> int:
    """Seal uploads past the recovery window; ACKed objects are never deleted."""
    if db is not None:
        return _seal_expired(db, settings or Settings.from_env(), now)
    settings = settings or Settings.from_env()
    engine = create_database_engine(settings)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            return _seal_expired(session, settings, now)
    finally:
        engine.dispose()


def register_epoch(
    ctx: AuthContext,
    upload_session_id: str,
    anchor: CaptureEpoch,
    tracks: list[TrackSpec],
    capture_lease_token: str | None = None,
) -> CaptureEpoch:
    """Register a capture epoch; delegates to the uploads implementation."""
    return uploads.register_epoch(
        ctx, upload_session_id, anchor, tracks, capture_lease_token
    )


@router.post("/uploads/{upload_id}/seal", response_model=SealView)
def seal_route(
    upload_id: Annotated[str, Path(min_length=1, max_length=64)],
    payload: SealRequest,
    ctx: AuthContext = Depends(require_csrf),
) -> SealView:
    return seal_upload(ctx, upload_id, payload)
