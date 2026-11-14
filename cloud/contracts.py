"""Wire contracts shared by the cloud API, workers and web client.

Every DTO named in plan section 2.1 lives here so producers and consumers can
reference one definition instead of ad-hoc dictionaries. Conventions:

* ids are UUID strings unless the plan says otherwise (cursor tokens, opaque);
* timestamps are ISO-8601 UTC strings;
* durations/offsets are non-negative integer milliseconds;
* models forbid unknown fields, so a client cannot smuggle extra state
  (notably owner_user_id) into a request body.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from cloud.settings import IMAGE_MAX_BYTES, RESUME_MAX_CHARS


def iso_utc(value: datetime) -> str:
    """Render an aware datetime as ISO-8601 UTC with a trailing Z."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and require an explicit UTC offset."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be ISO-8601 UTC with an explicit offset")
    return parsed.astimezone(timezone.utc)


def _valid_iso_utc(value: str) -> str:
    parse_iso_utc(value)
    return value


IsoUtc = Annotated[str, AfterValidator(_valid_iso_utc)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SpeakerHint = Annotated[StrictInt, Field(ge=1, le=20)]


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sniff_image(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


@dataclass
class AuthContext:
    """Server-derived identity for one request; never built from request data."""

    user_id: str
    session_id: str
    db: Any = field(default=None, repr=False, compare=False)
    clock: Any = field(default=None, repr=False, compare=False)
    settings: Any = field(default=None, repr=False, compare=False)
    objects: Any = field(default=None, repr=False, compare=False)

    def require_db(self) -> Any:
        if self.db is None:
            raise RuntimeError("AuthContext has no database session")
        return self.db

    def require_objects(self) -> Any:
        if self.objects is None:
            raise ServiceUnavailableError("object_store_unavailable")
        return self.objects


# -- meetings --------------------------------------------------------------


class MeetingView(_Contract):
    id: str
    title: str = Field(min_length=1, max_length=200)
    mode: Literal["mic", "dual"]
    generation: int = Field(ge=0)
    current_transcript_id: str | None = None
    deleted_at: IsoUtc | None = None


class MeetingPage(_Contract):
    items: list[MeetingView] = Field(default_factory=list)
    next_cursor: str | None = None


class CreateMeetingRequest(_Contract):
    title: str = Field(min_length=1, max_length=200)
    mode: Literal["mic", "dual"] = "mic"
    speakers: SpeakerHint | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class UpdateMeetingRequest(_Contract):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


# -- auth ------------------------------------------------------------------


class SessionView(_Contract):
    user_id: str
    csrf_token: str
    expires_at: IsoUtc


# -- capture ---------------------------------------------------------------


class ChunkMeta(_Contract):
    upload_session_id: str
    capture_epoch_id: str
    track_id: str
    sequence: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    checksum: Sha256Hex
    codec: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be >= start_ms")
        return self


class CaptureEpoch(_Contract):
    id: str
    offset_ms: int = Field(ge=0)
    uncertainty_ms: int = Field(ge=0)
    started_at: IsoUtc


class TrackSpec(_Contract):
    id: str
    source: Literal["mic", "system"]
    codec: str = Field(min_length=1)
    sample_rate: int = Field(ge=1)


class Gap(_Contract):
    track_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be >= start_ms")
        return self


class AckView(_Contract):
    accepted: StrictBool
    duplicate: StrictBool
    contiguous_ms: int = Field(ge=0)
    missing: list[Gap] = Field(default_factory=list)


class TrackManifest(_Contract):
    capture_epoch_id: str
    track_id: str
    last_sequence: int = Field(ge=-1)
    end_ms: int = Field(ge=0)


class SealRequest(_Contract):
    tracks: list[TrackManifest] = Field(default_factory=list)
    accept_partial: StrictBool = False


class SealView(_Contract):
    input_version: int = Field(ge=0)
    status: Literal["complete", "partial"]
    gaps: list[Gap] = Field(default_factory=list)


# -- tasks -----------------------------------------------------------------


class TaskView(_Contract):
    id: str
    meeting_id: str | None = None
    owner_user_id: str
    input_version: int = Field(ge=0)
    generation: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=32)


class ClaimedTask(_Contract):
    task: TaskView
    lease_token: str = Field(min_length=1)
    lease_deadline: IsoUtc


# -- transcripts and annotations ------------------------------------------


class Segment(_Contract):
    id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    speaker_id: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be >= start_ms")
        return self


class Speaker(_Contract):
    id: str
    label: str = Field(min_length=1)
    suspect: StrictBool


class TranscriptView(_Contract):
    id: str
    meeting_id: str
    segments: list[Segment] = Field(default_factory=list)
    speakers: list[Speaker] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    empty: StrictBool


class AnnotationView(_Contract):
    id: str
    meeting_id: str
    offset_ms: int = Field(ge=0)
    kind: Literal["note", "highlight", "comment"]
    text: str = Field(min_length=1)


class EventView(_Contract):
    cursor: int = Field(ge=0)
    meeting_id: str
    generation: int = Field(ge=0)
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


# -- models, embeddings, voiceprints --------------------------------------


class ModelMetadata(_Contract):
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    weights_sha256: Sha256Hex
    dimension: int = Field(ge=1)
    sample_rate: int = Field(ge=1)
    preprocess_version: str = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    calibration_version: str = Field(min_length=1)


class Embedding(_Contract):
    vector: list[float]
    metadata: ModelMetadata

    @model_validator(mode="after")
    def _dimension_matches(self) -> Self:
        if len(self.vector) != self.metadata.dimension:
            raise ValueError("vector length must equal metadata.dimension")
        return self


class VoiceprintView(_Contract):
    id: str
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    metadata: ModelMetadata
    revoked: StrictBool


# -- interview -------------------------------------------------------------


class PositionView(_Contract):
    id: str
    type: Literal["前端", "后端", "客户端", "测试"]
    title: str = Field(min_length=1)
    requirements: str = Field(min_length=1)


class AIResult(_Contract):
    markdown: str
    transcript_version_id: str | None = None


class InterviewInput(_Contract):
    resume_text: str = Field(max_length=RESUME_MAX_CHARS)
    images: list[bytes] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _images_are_supported(self) -> Self:
        for image in self.images:
            if not image:
                raise ValueError("images must not be empty")
            if len(image) > IMAGE_MAX_BYTES:
                raise ValueError(f"images must be at most {IMAGE_MAX_BYTES} bytes")
            if _sniff_image(image) is None:
                raise ValueError("images must be png, jpeg, webp or gif")
        return self


# -- HTTP error contract ---------------------------------------------------
# 401 unauthenticated; 403 CSRF/origin failure; 404 inaccessible resource
# (never 403 for another user's resource, so existence does not leak).


class CloudError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class UnauthorizedError(CloudError):
    status_code = 401
    code = "authentication_required"


class ForbiddenError(CloudError):
    status_code = 403
    code = "forbidden"


class NotFoundError(CloudError):
    status_code = 404
    code = "not_found"


class InvalidRequestError(CloudError):
    status_code = 400
    code = "invalid_request"


class ConflictError(CloudError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(CloudError):
    status_code = 413
    code = "request_too_large"


class ServiceUnavailableError(CloudError):
    status_code = 503
    code = "service_unavailable"
