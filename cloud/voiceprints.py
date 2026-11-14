"""Owner-private, versioned voiceprints (Task 13).

Boundaries kept explicit:

* a voiceprint belongs to one owner and one exact model metadata tuple, so
  equal names in different accounts never meet and same-dimension models do
  not share a profile;
* embeddings and enrolment samples are AES-GCM blobs whose associated data
  binds owner and row identity, so a copied blob cannot be replayed elsewhere;
* replacement inserts a new immutable version and revokes the old one in a
  single transaction, guarded by a partial unique active-name index;
* matching only relabels a TranscriptView and records server-only usage
  provenance; every usage is re-checked inside publish so revocation wins.

No model-specific thresholds or accuracy claims live here. Tests inject
synthetic extractors and fake metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime
from typing import Any, Protocol, Sequence
from uuid import uuid4

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloud.contracts import (
    AuthContext,
    CompatibleProfile,
    ConflictError,
    Embedding,
    InvalidRequestError,
    ModelMetadata,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
    Speaker,
    TaskView,
    TranscriptView,
    VoiceprintUsage,
    VoiceprintView,
    iso_utc,
)
from cloud.models import MODEL_METADATA_FIELDS
from cloud.schema import (
    MeetingSpeaker,
    ProcessingTask,
    VoiceprintEnrollment,
    VoiceprintProfile,
)
from cloud.settings import Settings, decode_voiceprint_key
from cloud.tasks import fail_task, read_claimed_task

VOICEPRINT_EXTRACTION_FAILED = "voiceprint_extraction_failed"
VOICEPRINT_VERSION_CHANGED = "voiceprint_version_changed"
VOICEPRINT_INVALID_EMBEDDING = "voiceprint_invalid_embedding"
VOICEPRINT_KEY_MISSING = "voiceprint_key_missing"
VOICEPRINT_SAMPLE_MISSING = "voiceprint_sample_missing"

_BLOB_MAGIC = b"VP1"
_NONCE_BYTES = 12
_TAG_BYTES = 16


class VoiceprintExtractor(Protocol):
    """Synthetic or provider-backed extractor used by workers and tests."""

    def extract(self, audio: bytes) -> Embedding: ...


def _settings(ctx: AuthContext) -> Settings:
    if ctx.settings is None:
        raise ServiceUnavailableError(VOICEPRINT_KEY_MISSING)
    return ctx.settings


def _key(settings: Settings) -> bytes:
    if settings.voiceprint_key is None:
        raise ServiceUnavailableError(VOICEPRINT_KEY_MISSING)
    try:
        raw = decode_voiceprint_key(settings.voiceprint_key)
    except ValueError as exc:
        raise ServiceUnavailableError(VOICEPRINT_KEY_MISSING) from exc
    if len(raw) != 32:
        raise ServiceUnavailableError(VOICEPRINT_KEY_MISSING)
    return raw


def _normalise_name(name: str) -> str:
    if not isinstance(name, str):
        raise InvalidRequestError("invalid_voiceprint_name")
    normalised = unicodedata.normalize("NFC", name).strip()
    if not 1 <= len(normalised) <= 100:
        raise InvalidRequestError("invalid_voiceprint_name")
    return normalised


def _normalise_consent(consent: str) -> str:
    if not isinstance(consent, str):
        raise InvalidRequestError("voiceprint_consent_required")
    normalised = consent.strip()
    if not 1 <= len(normalised) <= 500:
        raise InvalidRequestError("voiceprint_consent_required")
    return normalised


_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
)


def _looks_like_image(audio: bytes) -> bool:
    if audio.startswith(_IMAGE_SIGNATURES):
        return True
    if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WEBP":
        return True
    return False


def _validate_audio(audio: bytes, settings: Settings) -> bytes:
    if not isinstance(audio, (bytes, bytearray)) or len(audio) == 0:
        raise InvalidRequestError("invalid_voiceprint_audio")
    if len(audio) > settings.voiceprint_sample_max_bytes:
        raise PayloadTooLargeError("voiceprint_sample_too_large")
    value = bytes(audio)
    if _looks_like_image(value):
        raise UnsupportedMediaTypeError("unsupported_voiceprint_media")
    return value


def _normalise_content_type(content_type: str | None) -> str:
    if not isinstance(content_type, str) or not content_type.strip():
        return "application/octet-stream"
    normalised = content_type.split(";", 1)[0].strip().lower()
    if not normalised or len(normalised) > 100:
        return "application/octet-stream"
    return normalised


def _validate_replace_version(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError("invalid_voiceprint_version")
    return value


def _validate_embedding(embedding: Embedding) -> bool:
    if not isinstance(embedding, Embedding):
        return False
    if not embedding.vector:
        return False
    if not all(isinstance(value, float) and math.isfinite(value) for value in embedding.vector):
        return False
    return len(embedding.vector) == embedding.metadata.dimension


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = get_random_bytes(_NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return _BLOB_MAGIC + nonce + tag + ciphertext


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if not isinstance(blob, (bytes, bytearray)):
        raise ValueError("blob is not bytes")
    raw = bytes(blob)
    if len(raw) < len(_BLOB_MAGIC) + _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("blob is truncated")
    if raw[: len(_BLOB_MAGIC)] != _BLOB_MAGIC:
        raise ValueError("unknown blob version")
    offset = len(_BLOB_MAGIC)
    nonce = raw[offset : offset + _NONCE_BYTES]
    offset += _NONCE_BYTES
    tag = raw[offset : offset + _TAG_BYTES]
    offset += _TAG_BYTES
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    return cipher.decrypt_and_verify(raw[offset:], tag)


def _sample_aad(owner_user_id: str, enrollment_id: str) -> bytes:
    return b"vp-sample:" + owner_user_id.encode("utf-8") + b":" + enrollment_id.encode("utf-8")


def _embedding_aad(owner_user_id: str, profile_id: str, version: int) -> bytes:
    return (
        b"vp-embedding:"
        + owner_user_id.encode("utf-8")
        + b":"
        + profile_id.encode("utf-8")
        + b":"
        + str(version).encode("ascii")
    )


def _embedding_plaintext(embedding: Embedding) -> bytes:
    payload = {
        "vector": list(embedding.vector),
        "metadata": embedding.metadata.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _embedding_from_plaintext(raw: bytes) -> Embedding:
    payload = json.loads(raw.decode("utf-8"))
    embedding = Embedding.model_validate(payload)
    if not _validate_embedding(embedding):
        raise ValueError("invalid embedding payload")
    return embedding


def _profile_metadata(profile: VoiceprintProfile) -> ModelMetadata:
    return ModelMetadata(**{field: getattr(profile, field) for field in MODEL_METADATA_FIELDS})


def _profile_view(profile: VoiceprintProfile) -> VoiceprintView:
    return VoiceprintView(
        id=profile.id,
        name=profile.name,
        version=profile.version,
        metadata=_profile_metadata(profile),
        revoked=profile.revoked_at is not None,
        created_at=iso_utc(profile.created_at),
    )


def _task_view(task: ProcessingTask) -> TaskView:
    return TaskView(
        id=task.id,
        meeting_id=task.meeting_id,
        owner_user_id=task.owner_user_id,
        input_version=task.input_version,
        generation=task.generation,
        status=task.status,
        error_code=task.error_code,
    )


def enroll_profile(
    ctx: AuthContext,
    name: str,
    consent: str,
    audio: bytes,
    replace_version: int | None = None,
    *,
    content_type: str | None = None,
) -> TaskView:
    """Create one private enrolment task; only ciphertext is stored at rest."""
    settings = _settings(ctx)
    key = _key(settings)
    normalised_name = _normalise_name(name)
    normalised_consent = _normalise_consent(consent)
    sample = _validate_audio(audio, settings)
    media_type = _normalise_content_type(content_type)
    replace = _validate_replace_version(replace_version)

    db = ctx.require_db()
    now = ctx.clock.now()
    active = db.execute(
        select(VoiceprintProfile)
        .where(
            VoiceprintProfile.owner_user_id == ctx.user_id,
            VoiceprintProfile.name == normalised_name,
            VoiceprintProfile.revoked_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if active is None:
        if replace is not None:
            db.rollback()
            raise ConflictError("voiceprint_version_mismatch")
        target_version = 1
    else:
        if replace is None:
            db.rollback()
            raise ConflictError("voiceprint_exists")
        if replace != active.version:
            db.rollback()
            raise ConflictError("voiceprint_version_mismatch")
        target_version = active.version + 1

    enrollment_id = str(uuid4())
    task_id = str(uuid4())
    enrollment = VoiceprintEnrollment(
        owner_user_id=ctx.user_id,
        id=enrollment_id,
        name=normalised_name,
        version=target_version,
        replace_version=replace,
        consent=normalised_consent,
        sample_sha256=hashlib.sha256(sample).hexdigest(),
        sample_bytes=len(sample),
        content_type=media_type,
        sample_ciphertext=_seal(key, sample, _sample_aad(ctx.user_id, enrollment_id)),
        profile_id=None,
        error_code=None,
        created_at=now,
        captured_at=now,
        completed_at=None,
    )
    task = ProcessingTask(
        id=task_id,
        kind="voiceprint",
        owner_user_id=ctx.user_id,
        meeting_id=None,
        upload_session_id=None,
        voiceprint_ref=enrollment_id,
        input_version=0,
        generation=0,
        attempt=0,
        status="waiting",
        error_code=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    db.add_all([enrollment, task])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("voiceprint_exists")
    return _task_view(task)


def revoke_profile(ctx: AuthContext, profile_id: str) -> None:
    """Revoke one active profile; another owner's id is a 404."""
    db = ctx.require_db()
    now = ctx.clock.now()
    profile = db.execute(
        select(VoiceprintProfile)
        .where(
            VoiceprintProfile.owner_user_id == ctx.user_id,
            VoiceprintProfile.id == profile_id,
            VoiceprintProfile.revoked_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if profile is None:
        db.rollback()
        raise NotFoundError("voiceprint_not_found")
    profile.revoked_at = now
    profile.revocation_reason = "revoked_by_user"
    db.commit()


def list_profiles(ctx: AuthContext) -> list[VoiceprintView]:
    """List only active profiles; revoked versions stay immutable history."""
    db = ctx.require_db()
    rows = db.scalars(
        select(VoiceprintProfile)
        .where(
            VoiceprintProfile.owner_user_id == ctx.user_id,
            VoiceprintProfile.revoked_at.is_(None),
        )
        .order_by(VoiceprintProfile.name, VoiceprintProfile.version)
    )
    return [_profile_view(row) for row in rows]


def get_voiceprint_task(ctx: AuthContext, task_id: str) -> TaskView:
    db = ctx.require_db()
    task = db.execute(
        select(ProcessingTask).where(
            ProcessingTask.id == task_id,
            ProcessingTask.owner_user_id == ctx.user_id,
            ProcessingTask.kind == "voiceprint",
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError("task_not_found")
    return _task_view(task)


def load_compatible_profiles(
    ctx: AuthContext, metadata: ModelMetadata
) -> list[CompatibleProfile]:
    """Active profiles whose every model metadata field matches exactly."""
    settings = _settings(ctx)
    try:
        key = _key(settings)
    except ServiceUnavailableError:
        return []
    db = ctx.require_db()
    conditions = [VoiceprintProfile.owner_user_id == ctx.user_id]
    for field in MODEL_METADATA_FIELDS:
        conditions.append(getattr(VoiceprintProfile, field) == getattr(metadata, field))
    rows = db.scalars(
        select(VoiceprintProfile)
        .where(*conditions, VoiceprintProfile.revoked_at.is_(None))
        .order_by(VoiceprintProfile.created_at, VoiceprintProfile.id)
    )
    compatible: list[CompatibleProfile] = []
    for row in rows:
        try:
            plaintext = _open(
                key,
                row.embedding_ciphertext,
                _embedding_aad(row.owner_user_id, row.id, row.version),
            )
            embedding = _embedding_from_plaintext(plaintext)
        except (ValueError, TypeError, json.JSONDecodeError):
            # A corrupt row is skipped; it must never crash enrolment or a
            # transcript publish for every other row.
            continue
        if embedding.metadata != metadata:
            continue
        compatible.append(
            CompatibleProfile(
                profile_id=row.id,
                name=row.name,
                version=row.version,
                embedding=embedding,
            )
        )
    return compatible


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        if not math.isfinite(a) or not math.isfinite(b):
            return None
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return None
    score = dot / math.sqrt(left_norm * right_norm)
    if not math.isfinite(score):
        return None
    return max(-1.0, min(1.0, score))


def _speaker_metadata(row: MeetingSpeaker) -> ModelMetadata | None:
    values = {field: getattr(row, field) for field in MODEL_METADATA_FIELDS}
    if any(value is None for value in values.values()):
        return None
    try:
        return ModelMetadata(**values)
    except (TypeError, ValueError):
        return None


def match_profiles(
    ctx: AuthContext, transcript: TranscriptView, audio: Any = None
) -> TranscriptView:
    """Relabel speakers using compatible active profiles.

    The audio argument is accepted for interface stability; matching uses the
    stored meeting-speaker embeddings that produced the transcript. No meeting
    speaker row and no segment text is changed.
    """
    del audio
    settings = _settings(ctx)
    high = settings.voiceprint_high_threshold
    low = settings.voiceprint_low_threshold
    if high is None or low is None:
        return transcript

    db = ctx.require_db()
    speaker_ids = [speaker.id for speaker in transcript.speakers]
    if not speaker_ids:
        return transcript
    rows = db.scalars(
        select(MeetingSpeaker).where(
            MeetingSpeaker.owner_user_id == ctx.user_id,
            MeetingSpeaker.meeting_id == transcript.meeting_id,
            MeetingSpeaker.id.in_(speaker_ids),
        )
    )
    by_id = {row.id: row for row in rows}
    updated: list[Speaker] = []
    warnings = list(transcript.warnings)
    for speaker in transcript.speakers:
        row = by_id.get(speaker.id)
        metadata = _speaker_metadata(row) if row is not None else None
        if row is None or metadata is None or not row.embedding:
            updated.append(speaker)
            continue
        best: tuple[float, CompatibleProfile] | None = None
        for profile in load_compatible_profiles(ctx, metadata):
            score = _cosine(row.embedding, profile.embedding.vector)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, profile)
        if best is None or best[0] < low:
            updated.append(speaker)
            continue
        score, profile = best
        suspect = score < high
        updated.append(speaker.model_copy(update={"label": profile.name, "suspect": suspect}))
        transcript.record_voiceprint_usage(
            VoiceprintUsage(
                speaker_id=speaker.id,
                anonymous_label=speaker.label,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                owner_user_id=ctx.user_id,
            )
        )
        if suspect and "voiceprint_suspect_label" not in warnings:
            warnings.append("voiceprint_suspect_label")
    transcript.speakers = updated
    transcript.warnings = warnings
    if warnings and transcript.status != "basic_failed":
        transcript.status = "completed_with_warnings"
    return transcript


def fail_voiceprint_enrollment(
    db: Session,
    task: ProcessingTask,
    error_code: str,
    now: datetime,
) -> None:
    """Stage terminal cleanup in the caller transaction; never commits."""
    if task.kind != "voiceprint" or task.voiceprint_ref is None:
        return
    enrollment = db.execute(
        select(VoiceprintEnrollment)
        .where(
            VoiceprintEnrollment.owner_user_id == task.owner_user_id,
            VoiceprintEnrollment.id == task.voiceprint_ref,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if enrollment is None:
        return
    enrollment.sample_ciphertext = None
    enrollment.error_code = error_code
    enrollment.completed_at = now


def cancel_voiceprint_enrollment(db: Session, task: ProcessingTask, now: datetime) -> None:
    """Stage cancellation cleanup in the caller transaction; never commits."""
    fail_voiceprint_enrollment(db, task, "cancelled_by_user", now)


def _lease_hash(token: str) -> str:
    # Imported lazily so tasks can import this module's cleanup helpers.
    from cloud.leases import hash_lease_token

    return hash_lease_token(token)


def _settings_from_db(db: Session) -> Settings | None:
    """The publish path receives no AuthContext; workers bind settings in db.info."""
    settings = db.info.get("voiceprint_settings")
    return settings if isinstance(settings, Settings) else None


def publish_voiceprint(
    task_id: str,
    lease_token: str,
    embedding: Embedding,
    *,
    owner_user_id: str,
    now: datetime,
    db: Session,
) -> bool:
    """Atomically revoke the old version and publish one new profile version."""
    task = db.execute(
        select(ProcessingTask).where(ProcessingTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None or task.owner_user_id != owner_user_id:
        db.rollback()
        return False
    if task.kind != "voiceprint" or task.status != "running":
        db.rollback()
        return False
    if task.voiceprint_ref is None or task.lease_token_hash != _lease_hash(lease_token):
        db.rollback()
        return False
    if task.lease_deadline is None or task.lease_deadline <= now:
        db.rollback()
        return False
    if not _validate_embedding(embedding):
        db.rollback()
        return False

    enrollment = db.execute(
        select(VoiceprintEnrollment)
        .where(
            VoiceprintEnrollment.owner_user_id == owner_user_id,
            VoiceprintEnrollment.id == task.voiceprint_ref,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if enrollment is None or enrollment.profile_id is not None:
        db.rollback()
        return False

    old = db.execute(
        select(VoiceprintProfile)
        .where(
            VoiceprintProfile.owner_user_id == owner_user_id,
            VoiceprintProfile.name == enrollment.name,
            VoiceprintProfile.revoked_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if enrollment.replace_version is None:
        if old is not None:
            db.rollback()
            return False
        version = 1
    else:
        if old is None or old.version != enrollment.replace_version:
            db.rollback()
            return False
        version = old.version + 1

    settings = _settings_from_db(db)
    if settings is None or settings.voiceprint_key is None:
        db.rollback()
        return False
    key = _key(settings)
    profile_id = str(uuid4())
    ciphertext = _seal(
        key,
        _embedding_plaintext(embedding),
        _embedding_aad(owner_user_id, profile_id, version),
    )
    if old is not None:
        old.revoked_at = now
        old.revocation_reason = "replaced"
        db.flush()
    db.add(
        VoiceprintProfile(
            owner_user_id=owner_user_id,
            id=profile_id,
            name=enrollment.name,
            version=version,
            embedding_ciphertext=ciphertext,
            **embedding.metadata.model_dump(mode="python"),
            revoked_at=None,
            revocation_reason=None,
            created_at=now,
        )
    )
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    enrollment.sample_ciphertext = None
    enrollment.profile_id = profile_id
    enrollment.error_code = None
    enrollment.completed_at = now
    task.status = "completed"
    task.error_code = None
    task.completed_at = now
    task.updated_at = now
    task.lease_token_hash = None
    task.lease_worker_id = None
    task.lease_deadline = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def process_voiceprint_task(
    ctx: AuthContext,
    item: Any,
    *,
    extractor: VoiceprintExtractor,
) -> bool:
    """Extract, validate and publish one claimed voiceprint task.

    Every failure is a terminal fixed code and clears the sample in the same
    transaction as the task transition. No exception text is persisted.
    """
    db = ctx.require_db()
    now = ctx.clock.now()
    read_claimed_task(
        item.task_id,
        owner_user_id=item.owner_user_id,
        lease_token=item.lease_token,
        now=now,
        db=db,
    )
    task = db.execute(
        select(ProcessingTask).where(
            ProcessingTask.id == item.task_id,
            ProcessingTask.owner_user_id == item.owner_user_id,
        )
    ).scalar_one_or_none()
    if task is None or task.kind != "voiceprint" or task.voiceprint_ref is None:
        return False
    enrollment = db.execute(
        select(VoiceprintEnrollment).where(
            VoiceprintEnrollment.owner_user_id == item.owner_user_id,
            VoiceprintEnrollment.id == task.voiceprint_ref,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        return False

    settings = _settings(ctx)
    if settings.voiceprint_key is None:
        return _fail_voiceprint(ctx, item.task_id, item.lease_token, VOICEPRINT_KEY_MISSING, now)
    try:
        key = _key(settings)
    except ServiceUnavailableError:
        return _fail_voiceprint(ctx, item.task_id, item.lease_token, VOICEPRINT_KEY_MISSING, now)
    if enrollment.sample_ciphertext is None:
        return _fail_voiceprint(ctx, item.task_id, item.lease_token, VOICEPRINT_SAMPLE_MISSING, now)
    try:
        sample = _open(
            key,
            enrollment.sample_ciphertext,
            _sample_aad(item.owner_user_id, enrollment.id),
        )
    except (ValueError, TypeError):
        return _fail_voiceprint(ctx, item.task_id, item.lease_token, VOICEPRINT_SAMPLE_MISSING, now)

    try:
        embedding = extractor.extract(sample)
    except Exception:  # noqa: BLE001 - fixed code, no exception text persisted
        return _fail_voiceprint(
            ctx, item.task_id, item.lease_token, VOICEPRINT_EXTRACTION_FAILED, now
        )
    if not _validate_embedding(embedding):
        return _fail_voiceprint(
            ctx, item.task_id, item.lease_token, VOICEPRINT_INVALID_EMBEDDING, now
        )

    db.info["voiceprint_settings"] = settings
    published = publish_voiceprint(
        item.task_id,
        item.lease_token,
        embedding,
        owner_user_id=item.owner_user_id,
        now=now,
        db=db,
    )
    if not published:
        return _fail_voiceprint(
            ctx, item.task_id, item.lease_token, VOICEPRINT_VERSION_CHANGED, now
        )
    return True


def _fail_voiceprint(
    ctx: AuthContext,
    task_id: str,
    lease_token: str,
    error_code: str,
    now: datetime,
) -> bool:
    db = ctx.require_db()
    task = db.execute(
        select(ProcessingTask).where(ProcessingTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None:
        db.rollback()
        return False
    fail_voiceprint_enrollment(db, task, error_code, now)
    fail_task(
        task_id,
        lease_token,
        transient=False,
        error_code=error_code,
        now=now,
        db=db,
        settings=_settings(ctx),
    )
    return False


def cancel_voiceprint_task(ctx: AuthContext, task_id: str) -> None:
    """Cancel a pending/running voiceprint task and clear its sample."""
    db = ctx.require_db()
    now = ctx.clock.now()
    task = db.execute(
        select(ProcessingTask)
        .where(
            ProcessingTask.id == task_id,
            ProcessingTask.owner_user_id == ctx.user_id,
            ProcessingTask.kind == "voiceprint",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        db.rollback()
        raise NotFoundError("task_not_found")
    if task.status in {"completed", "failed", "cancelled"}:
        db.rollback()
        return
    task.status = "cancelled"
    task.error_code = "cancelled_by_user"
    task.completed_at = now
    task.updated_at = now
    task.lease_token_hash = None
    task.lease_worker_id = None
    task.lease_deadline = None
    cancel_voiceprint_enrollment(db, task, now)
    db.commit()


def recheck_voiceprint_usage(
    db: Session,
    transcript: TranscriptView,
    *,
    now: datetime,
    owner_user_id: str | None = None,
) -> TranscriptView:
    """Undo stale relabellings before a transcript becomes durable.

    Read-only and idempotent: a revoked, deleted or version-changed profile
    restores the original anonymous label and adds one warning. A basic_failed
    transcript keeps its status.
    """
    del now
    usages = transcript.voiceprint_usages
    if not usages:
        return transcript
    speakers = list(transcript.speakers)
    by_id: dict[str, Speaker] = {speaker.id: speaker for speaker in speakers}
    warnings = list(transcript.warnings)
    for usage in usages:
        scope_owner = usage.owner_user_id or owner_user_id
        conditions = [VoiceprintProfile.id == usage.profile_id]
        if scope_owner is not None:
            conditions.append(VoiceprintProfile.owner_user_id == scope_owner)
        profile = db.execute(
            select(VoiceprintProfile).where(*conditions)
        ).scalar_one_or_none()
        if (
            profile is not None
            and profile.version == usage.profile_version
            and profile.revoked_at is None
        ):
            continue
        speaker = by_id.get(usage.speaker_id)
        if speaker is None:
            continue
        by_id[usage.speaker_id] = speaker.model_copy(
            update={"label": usage.anonymous_label, "suspect": False}
        )
        if "voiceprint_revoked" not in warnings:
            warnings.append("voiceprint_revoked")
    transcript.speakers = [by_id[speaker.id] for speaker in speakers]
    transcript.warnings = warnings
    if warnings and transcript.status != "basic_failed":
        transcript.status = "completed_with_warnings"
    return transcript


# -- HTTP API ---------------------------------------------------------------

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile  # noqa: E402

from cloud.auth import require_csrf, require_user  # noqa: E402
from cloud.contracts import UnsupportedMediaTypeError  # noqa: E402

router = APIRouter(prefix="/api/cloud", tags=["voiceprints"])


def _parse_replace_version(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InvalidRequestError("invalid_voiceprint_version")
    return _validate_replace_version(parsed)


def _check_audio_content_type(content_type: str | None) -> None:
    if content_type is None or not content_type.strip():
        raise UnsupportedMediaTypeError("unsupported_voiceprint_media")
    lowered = content_type.split(";", 1)[0].strip().lower()
    if lowered.startswith("audio/") or lowered == "application/octet-stream":
        return
    raise UnsupportedMediaTypeError("unsupported_voiceprint_media")


async def enroll_profile_route(
    name: str | None = Form(default=None),
    consent: str | None = Form(default=None),
    audio: UploadFile | None = File(default=None),
    replace_version: str | None = Form(default=None),
    ctx: AuthContext = Depends(require_csrf),
) -> TaskView:
    if name is None:
        raise InvalidRequestError("invalid_voiceprint_name")
    if consent is None:
        raise InvalidRequestError("voiceprint_consent_required")
    if audio is None:
        raise InvalidRequestError("invalid_voiceprint_audio")
    _check_audio_content_type(audio.content_type)
    limit = (
        ctx.settings.voiceprint_sample_max_bytes if ctx.settings is not None else 10_000_000
    )
    payload = await audio.read(limit + 1)
    if not payload:
        raise InvalidRequestError("invalid_voiceprint_audio")
    return enroll_profile(
        ctx,
        name,
        consent,
        payload,
        replace_version=_parse_replace_version(replace_version),
        content_type=audio.content_type,
    )


def list_profiles_route(ctx: AuthContext = Depends(require_user)) -> list[VoiceprintView]:
    return list_profiles(ctx)


def get_voiceprint_task_route(
    task_id: str, ctx: AuthContext = Depends(require_user)
) -> TaskView:
    return get_voiceprint_task(ctx, task_id)


def cancel_voiceprint_task_route(
    task_id: str, ctx: AuthContext = Depends(require_csrf)
) -> Response:
    cancel_voiceprint_task(ctx, task_id)
    return Response(status_code=204)


def revoke_profile_route(
    profile_id: str, ctx: AuthContext = Depends(require_csrf)
) -> Response:
    revoke_profile(ctx, profile_id)
    return Response(status_code=204)


router.add_api_route(
    "/voiceprints",
    list_profiles_route,
    methods=["GET"],
    response_model=list[VoiceprintView],
)
router.add_api_route(
    "/voiceprints",
    enroll_profile_route,
    methods=["POST"],
    response_model=TaskView,
    status_code=201,
)
router.add_api_route(
    "/voiceprints/tasks/{task_id}",
    get_voiceprint_task_route,
    methods=["GET"],
    response_model=TaskView,
)
router.add_api_route(
    "/voiceprints/tasks/{task_id}/cancel",
    cancel_voiceprint_task_route,
    methods=["POST"],
    status_code=204,
)
router.add_api_route(
    "/voiceprints/{profile_id}",
    revoke_profile_route,
    methods=["DELETE"],
    status_code=204,
)
