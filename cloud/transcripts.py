"""Transcript production, speaker identity and read APIs.

The worker hands a claimed final task to transcribe_task, which decodes the
sealed input version, asks the configured adapter for a draft transcript,
optionally removes cross-track duplicates, and optionally attributes speakers
with embeddings.  The resulting TranscriptView is published by
cloud.tasks.publish_result in the task's fenced transaction; speaker rows
created here are flushed by that transaction, never committed independently.

Identity and duplicate rules follow the evidence contract: the strategy stays
unselected until the contract is approved, and the parameters are copied from
the contract rather than tuned here.
"""

from __future__ import annotations

import difflib
import math
import sys
import uuid
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloud.audio import prepare_audio
from cloud.auth import require_csrf, require_user
from cloud.contracts import (
    AudioInput,
    AuthContext,
    ConflictError,
    Embedding,
    ModelMetadata,
    NotFoundError,
    Segment,
    Speaker,
    TaskView,
    TranscriptView,
)
from cloud.models import (
    MODEL_METADATA_FIELDS,
    EnhancementError,
    ModelAdapter,
    RecognitionParams,
)
from cloud.schema import (
    Meeting,
    MeetingSpeaker,
    TranscriptSegment,
    TranscriptSpeaker,
    TranscriptVersion,
    UploadSession,
)
from cloud.tasks import enqueue_final
from cloud.worker import WorkItem

router = APIRouter(prefix="/api/cloud", tags=["transcripts"])

#: Metadata fields that must agree for two embeddings to be comparable.
COMPARABLE_METADATA = MODEL_METADATA_FIELDS

SPEAKER_LABEL_PREFIX = "说话人 "


# -- duplicate removal -----------------------------------------------------


def _normalise_text(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _is_cross_track_duplicate(
    left: Segment, right: Segment, params: RecognitionParams
) -> bool:
    """Return True when two segments are the same utterance picked up twice."""

    if left.track_id is None or right.track_id is None:
        return False
    if left.track_id == right.track_id:
        return False
    if abs(left.start_ms - right.start_ms) > params.dedup_window_ms:
        return False
    overlap = min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)
    if overlap <= 0:
        return False
    left_text = _normalise_text(left.text)
    right_text = _normalise_text(right.text)
    if not left_text or not right_text:
        return False
    similarity = difflib.SequenceMatcher(None, left_text, right_text).ratio()
    return similarity >= params.dedup_min_text_similarity


def _deduplicate(
    segments: Sequence[Segment], params: RecognitionParams
) -> list[Segment]:
    """Keep the earlier of two cross-track copies of the same sentence."""

    ordered = sorted(
        enumerate(segments), key=lambda pair: (pair[1].start_ms, pair[0])
    )
    kept: list[Segment] = []
    for _, segment in ordered:
        if any(
            _is_cross_track_duplicate(other, segment, params) for other in kept
        ):
            continue
        kept.append(segment)
    return kept


# -- audio slicing ---------------------------------------------------------


def _read_samples(
    path: Path, start_ms: int, end_ms: int, rate: int
) -> list[float]:
    """Read a mono 16-bit slice as floats in [-1, 1)."""

    with wave.open(str(path), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise EnhancementError("working audio is not mono 16-bit PCM")
        total = reader.getnframes()
        start = max(0, min(total, round(start_ms * rate / 1000)))
        end = max(start, min(total, round(end_ms * rate / 1000)))
        if end == start:
            return []
        reader.setpos(start)
        raw = reader.readframes(end - start)
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return [value / 32768.0 for value in samples]


# -- speaker identity ------------------------------------------------------


def _metadata_tuple(metadata: ModelMetadata) -> tuple[object, ...]:
    return tuple(getattr(metadata, field) for field in COMPARABLE_METADATA)


def _row_metadata_tuple(row: MeetingSpeaker) -> tuple[object, ...]:
    return tuple(getattr(row, field) for field in COMPARABLE_METADATA)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    try:
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(left_norm) or not math.isfinite(right_norm):
        return None
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    score = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    if not math.isfinite(score):
        return None
    return score


@dataclass
class _Identity:
    speaker_id: str
    vector: Sequence[float] | None


def _next_label(used: set[str]) -> str:
    number = 1
    while f"{SPEAKER_LABEL_PREFIX}{number}" in used:
        number += 1
    label = f"{SPEAKER_LABEL_PREFIX}{number}"
    used.add(label)
    return label


def _create_speaker(
    ctx: AuthContext,
    meeting: Meeting,
    embedding: Embedding,
    used_labels: set[str],
    labels: dict[str, str],
    candidates: list[_Identity],
) -> str:
    metadata = embedding.metadata
    speaker_id = str(uuid.uuid4())
    label = _next_label(used_labels)
    row = MeetingSpeaker(
        owner_user_id=ctx.user_id,
        id=speaker_id,
        meeting_id=meeting.id,
        label=label,
        embedding=list(embedding.vector),
        model_id=metadata.model_id,
        model_version=metadata.model_version,
        weights_sha256=metadata.weights_sha256,
        dimension=metadata.dimension,
        sample_rate=metadata.sample_rate,
        preprocess_version=metadata.preprocess_version,
        normalization_version=metadata.normalization_version,
        calibration_version=metadata.calibration_version,
        created_at=ctx.clock.now(),
    )
    ctx.require_db().add(row)
    labels[speaker_id] = label
    candidates.append(_Identity(speaker_id, list(embedding.vector)))
    return speaker_id


def _attribute_speakers(
    ctx: AuthContext,
    meeting: Meeting,
    audio: AudioInput,
    segments: Sequence[Segment],
    adapter: ModelAdapter,
    params: RecognitionParams,
) -> tuple[list[Segment], list[Speaker], list[str]]:
    """Embed each segment and map it to a stable meeting speaker.

    The caller catches EnhancementError, OSError, wave.Error and ValueError
    and falls back to an unattributed transcript.  Database failures and
    programming errors are deliberately not caught here.
    """

    db = ctx.require_db()
    rows = list(
        db.execute(
            select(MeetingSpeaker).where(
                MeetingSpeaker.owner_user_id == ctx.user_id,
                MeetingSpeaker.meeting_id == meeting.id,
            )
        )
        .scalars()
        .all()
    )
    used_labels = {row.label for row in rows}
    labels = {row.id: row.label for row in rows}
    candidates: list[_Identity] = []
    warnings: list[str] = []
    expected_metadata: tuple[object, ...] | None = None
    speakers: dict[str, Speaker] = {}

    # Embed everything before creating any identity: a failure part-way through
    # must not leave half-attributed speakers staged for the publish commit.
    embedded: list[tuple[Segment, Embedding]] = []
    for segment in segments:
        if segment.end_ms <= segment.start_ms:
            continue
        track_path = (
            audio.tracks.get(segment.track_id)
            if segment.track_id is not None
            else None
        )
        path = Path(track_path) if track_path is not None else Path(audio.mix_path)
        samples = _read_samples(path, segment.start_ms, segment.end_ms, audio.rate)
        if not samples:
            continue
        embedding: Embedding = adapter.embed(samples)
        metadata = embedding.metadata
        if len(embedding.vector) != metadata.dimension:
            raise EnhancementError("embedding dimension mismatch")
        if not all(math.isfinite(value) for value in embedding.vector):
            raise EnhancementError("embedding contains non-finite values")
        metadata_tuple = _metadata_tuple(metadata)
        if expected_metadata is None:
            expected_metadata = metadata_tuple
            for row in rows:
                if row.embedding is None:
                    continue
                if _row_metadata_tuple(row) != expected_metadata:
                    continue
                candidates.append(_Identity(row.id, row.embedding))
        elif metadata_tuple != expected_metadata:
            raise EnhancementError("embedding metadata changed between segments")
        embedded.append((segment, embedding))

    for segment, embedding in embedded:
        best: _Identity | None = None
        best_score: float | None = None
        for candidate in candidates:
            if candidate.vector is None:
                continue
            score = _cosine(candidate.vector, embedding.vector)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best = candidate
                best_score = score

        if best is not None and best_score is not None:
            if best_score >= params.cluster_similarity_threshold:
                speaker_id = best.speaker_id
            elif len(candidates) >= params.max_identities:
                speaker_id = best.speaker_id
                if "identity_cap_reached" not in warnings:
                    warnings.append("identity_cap_reached")
            else:
                speaker_id = _create_speaker(
                    ctx, meeting, embedding, used_labels, labels, candidates
                )
        elif len(candidates) >= params.max_identities:
            raise EnhancementError("identity cap reached without a comparable speaker")
        else:
            speaker_id = _create_speaker(
                ctx, meeting, embedding, used_labels, labels, candidates
            )

        if speaker_id not in speakers:
            speakers[speaker_id] = Speaker(
                id=speaker_id, label=labels[speaker_id], suspect=False
            )
        segment.speaker_id = speaker_id

    return list(segments), list(speakers.values()), warnings


# -- task entry point ------------------------------------------------------


def _merged_warnings(warnings: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(warning) for warning in warnings))


def transcribe_task(
    ctx: AuthContext,
    item: WorkItem,
    *,
    adapter: ModelAdapter,
    params: RecognitionParams,
    workspace: Path,
) -> TranscriptView:
    """Produce one transcript view for a claimed final task."""

    if item.owner_user_id != ctx.user_id:
        raise NotFoundError("task_not_found")
    if item.meeting_id is None:
        raise NotFoundError("meeting_not_found")
    db = ctx.require_db()
    meeting = db.get(Meeting, (ctx.user_id, item.meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")

    audio = prepare_audio(ctx, meeting.id, item.input_version, workspace)
    draft = adapter.transcribe(audio, meeting.speakers)
    version_id = str(uuid.uuid4())
    warnings = _merged_warnings(draft.warnings)

    if not any(segment.text.strip() for segment in draft.segments):
        return TranscriptView(
            id=version_id,
            meeting_id=meeting.id,
            segments=[],
            speakers=[],
            warnings=warnings,
            empty=True,
            status="complete",
        )

    segments = [segment for segment in draft.segments if segment.text.strip()]
    if params.strategy == "separate_merge":
        segments = _deduplicate(segments, params)

    extra_warnings: list[str] = []
    if segments:
        try:
            segments, speakers, extra_warnings = _attribute_speakers(
                ctx, meeting, audio, segments, adapter, params
            )
        except (EnhancementError, OSError, wave.Error, ValueError):
            segments = [
                segment.model_copy(update={"speaker_id": None})
                for segment in segments
            ]
            speakers = []
            extra_warnings = ["speaker_enhancement_failed"]
    else:
        speakers = []

    warnings = _merged_warnings([*warnings, *extra_warnings])
    return TranscriptView(
        id=version_id,
        meeting_id=meeting.id,
        segments=segments,
        speakers=speakers,
        warnings=warnings,
        empty=not segments,
        status="completed_with_warnings" if warnings else "complete",
    )


# -- retrieval -------------------------------------------------------------


def _require_meeting(ctx: AuthContext, meeting_id: str) -> Meeting:
    db = ctx.require_db()
    meeting = db.get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    return meeting


def _version_view(
    db: Session, ctx: AuthContext, version: TranscriptVersion
) -> TranscriptView:
    segments = list(
        db.execute(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.owner_user_id == ctx.user_id,
                TranscriptSegment.version_id == version.id,
            )
            .order_by(TranscriptSegment.ordinal)
        )
        .scalars()
        .all()
    )
    speaker_rows = list(
        db.execute(
            select(TranscriptSpeaker).where(
                TranscriptSpeaker.owner_user_id == ctx.user_id,
                TranscriptSpeaker.version_id == version.id,
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.speaker_id: row for row in speaker_rows}
    ordered_ids: list[str] = []
    for segment in segments:
        if segment.speaker_id is not None and segment.speaker_id in by_id:
            if segment.speaker_id not in ordered_ids:
                ordered_ids.append(segment.speaker_id)
    for row in speaker_rows:
        if row.speaker_id not in ordered_ids:
            ordered_ids.append(row.speaker_id)

    return TranscriptView(
        id=version.id,
        meeting_id=version.meeting_id,
        segments=[
            Segment(
                id=segment.id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                speaker_id=segment.speaker_id,
                track_id=segment.track_id,
            )
            for segment in segments
        ],
        speakers=[
            Speaker(
                id=speaker_id,
                label=by_id[speaker_id].label,
                suspect=by_id[speaker_id].suspect,
            )
            for speaker_id in ordered_ids
        ],
        warnings=list(version.warnings or []),
        empty=bool(version.empty),
        status=version.status,
    )


def get_transcript(
    ctx: AuthContext, meeting_id: str, version_id: str | None = None
) -> TranscriptView:
    meeting = _require_meeting(ctx, meeting_id)
    db = ctx.require_db()
    target = version_id if version_id is not None else meeting.current_transcript_id
    if target is None:
        raise NotFoundError("transcript_not_found")
    version = db.get(TranscriptVersion, (ctx.user_id, target))
    if version is None or version.meeting_id != meeting.id:
        raise NotFoundError("transcript_not_found")
    return _version_view(db, ctx, version)


def list_versions(ctx: AuthContext, meeting_id: str) -> list[TranscriptView]:
    meeting = _require_meeting(ctx, meeting_id)
    db = ctx.require_db()
    versions = list(
        db.execute(
            select(TranscriptVersion)
            .where(
                TranscriptVersion.owner_user_id == ctx.user_id,
                TranscriptVersion.meeting_id == meeting.id,
            )
            .order_by(
                TranscriptVersion.created_at.desc(), TranscriptVersion.id.desc()
            )
        )
        .scalars()
        .all()
    )
    return [_version_view(db, ctx, version) for version in versions]


# -- supplement / re-transcribe -------------------------------------------


def _latest_sealed_upload(ctx: AuthContext, meeting_id: str) -> UploadSession:
    db = ctx.require_db()
    upload = (
        db.execute(
            select(UploadSession)
            .where(
                UploadSession.owner_user_id == ctx.user_id,
                UploadSession.meeting_id == meeting_id,
                UploadSession.status == "sealed",
            )
            .order_by(UploadSession.created_at.desc(), UploadSession.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if upload is None:
        raise NotFoundError("upload_not_found")
    return upload


def supplement(ctx: AuthContext, meeting_id: str) -> TaskView:
    """Transcribe the newest sealed upload, refusing an already-covered one."""

    meeting = _require_meeting(ctx, meeting_id)
    upload = _latest_sealed_upload(ctx, meeting.id)
    db = ctx.require_db()
    existing = (
        db.execute(
            select(TranscriptVersion.id)
            .where(
                TranscriptVersion.owner_user_id == ctx.user_id,
                TranscriptVersion.meeting_id == meeting.id,
                TranscriptVersion.input_version == upload.input_version,
            )
            .limit(1)
        )
        .first()
    )
    if existing is not None:
        raise ConflictError("transcript_already_exists")
    return enqueue_final(ctx, meeting.id, upload.input_version, force=True)


def re_transcribe(ctx: AuthContext, meeting_id: str) -> TaskView:
    """Queue a fresh transcription of the input behind the newest transcript."""

    meeting = _require_meeting(ctx, meeting_id)
    db = ctx.require_db()
    latest = (
        db.execute(
            select(TranscriptVersion)
            .where(
                TranscriptVersion.owner_user_id == ctx.user_id,
                TranscriptVersion.meeting_id == meeting.id,
            )
            .order_by(
                TranscriptVersion.created_at.desc(), TranscriptVersion.id.desc()
            )
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if latest is None:
        raise ConflictError("transcript_not_found")
    return enqueue_final(ctx, meeting.id, latest.input_version, force=True)


# -- routes ----------------------------------------------------------------


def list_transcripts(
    meeting_id: str, ctx: AuthContext = Depends(require_user)
) -> list[TranscriptView]:
    return list_versions(ctx, meeting_id)


def read_transcript(
    meeting_id: str, version_id: str, ctx: AuthContext = Depends(require_user)
) -> TranscriptView:
    return get_transcript(ctx, meeting_id, version_id)


def supplement_transcript(
    meeting_id: str, ctx: AuthContext = Depends(require_csrf)
) -> TaskView:
    return supplement(ctx, meeting_id)


def retranscribe_transcript(
    meeting_id: str, ctx: AuthContext = Depends(require_csrf)
) -> TaskView:
    return re_transcribe(ctx, meeting_id)


router.add_api_route(
    "/meetings/{meeting_id}/transcripts",
    list_transcripts,
    methods=["GET"],
    response_model=list[TranscriptView],
)
router.add_api_route(
    "/meetings/{meeting_id}/transcripts/{version_id}",
    read_transcript,
    methods=["GET"],
    response_model=TranscriptView,
)
router.add_api_route(
    "/meetings/{meeting_id}/transcripts/supplement",
    supplement_transcript,
    methods=["POST"],
    response_model=TaskView,
)
router.add_api_route(
    "/meetings/{meeting_id}/transcripts/re-transcribe",
    retranscribe_transcript,
    methods=["POST"],
    response_model=TaskView,
)
