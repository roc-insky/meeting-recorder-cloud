"""Owner-scoped, meeting-relative annotations.

An annotation marks a point on the meeting timeline (offset_ms) rather than a
transcript version, so a re-transcription or a version switch never moves or
drops it. Routes require the session user and derive ownership from it; every
lookup carries owner_user_id and another user's (or a deleted) meeting is a
404, never a 403, so ids cannot be probed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from cloud.auth import require_csrf, require_user
from cloud.contracts import (
    AnnotationView,
    AuthContext,
    InvalidRequestError,
    NotFoundError,
)
from cloud.schema import Annotation, Meeting

router = APIRouter(prefix="/api/cloud", tags=["annotations"])

KINDS = ("note", "highlight", "comment")
MAX_TEXT_LENGTH = 2000


class AddAnnotationRequest(BaseModel):
    """Request body for one annotation.

    Fields are declared as Any on purpose: the service, not FastAPI's schema
    validation, decides what is invalid and reports it as 400
    invalid_annotation (a bool offset or a non-literal kind must not turn into
    a 422 that the client contract does not define).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Any
    offset_ms: Any
    text: Any


def _clean_kind(kind: Any) -> str:
    if not isinstance(kind, str) or kind not in KINDS:
        raise InvalidRequestError("invalid_annotation")
    return kind


def _clean_offset(offset_ms: Any) -> int:
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int):
        raise InvalidRequestError("invalid_annotation")
    if offset_ms < 0:
        raise InvalidRequestError("invalid_annotation")
    return offset_ms


def _clean_text(text: Any) -> str:
    if not isinstance(text, str):
        raise InvalidRequestError("invalid_annotation")
    cleaned = text.strip()
    if not 1 <= len(cleaned) <= MAX_TEXT_LENGTH:
        raise InvalidRequestError("invalid_annotation")
    return cleaned


def _require_meeting(ctx: AuthContext, meeting_id: str) -> Meeting:
    meeting = ctx.require_db().get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError()
    return meeting


def _view(row: Annotation) -> AnnotationView:
    return AnnotationView(
        id=row.id,
        meeting_id=row.meeting_id,
        offset_ms=row.offset_ms,
        kind=row.kind,
        text=row.text,
    )


def add_annotation(
    ctx: AuthContext, meeting_id: str, kind: str, offset_ms: int, text: str
) -> AnnotationView:
    """Store one trimmed, validated note on the caller's meeting timeline."""

    # Resolve access first: a foreign, deleted or unknown meeting is always a
    # 404, never a validation response that could confirm the resource exists.
    meeting = _require_meeting(ctx, meeting_id)
    cleaned_kind = _clean_kind(kind)
    cleaned_offset = _clean_offset(offset_ms)
    cleaned_text = _clean_text(text)
    db = ctx.require_db()
    row = Annotation(
        id=str(uuid4()),
        owner_user_id=ctx.user_id,
        meeting_id=meeting.id,
        kind=cleaned_kind,
        offset_ms=cleaned_offset,
        text=cleaned_text,
        created_at=ctx.clock.now(),
    )
    db.add(row)
    db.commit()
    return _view(row)


def list_annotations(ctx: AuthContext, meeting_id: str) -> list[AnnotationView]:
    """Return the caller's annotations ordered by timeline position."""

    meeting = _require_meeting(ctx, meeting_id)
    db = ctx.require_db()
    rows = (
        db.execute(
            select(Annotation)
            .where(
                Annotation.owner_user_id == ctx.user_id,
                Annotation.meeting_id == meeting.id,
            )
            .order_by(
                Annotation.offset_ms,
                Annotation.created_at,
                Annotation.id,
            )
        )
        .scalars()
        .all()
    )
    return [_view(row) for row in rows]


def add_annotation_route(
    meeting_id: str,
    payload: AddAnnotationRequest,
    ctx: AuthContext = Depends(require_csrf),
) -> AnnotationView:
    return add_annotation(
        ctx,
        meeting_id,
        kind=payload.kind,
        offset_ms=payload.offset_ms,
        text=payload.text,
    )


def list_annotations_route(
    meeting_id: str, ctx: AuthContext = Depends(require_user)
) -> list[AnnotationView]:
    return list_annotations(ctx, meeting_id)


router.add_api_route(
    "/meetings/{meeting_id}/annotations",
    add_annotation_route,
    methods=["POST"],
    response_model=AnnotationView,
    status_code=201,
)

router.add_api_route(
    "/meetings/{meeting_id}/annotations",
    list_annotations_route,
    methods=["GET"],
    response_model=list[AnnotationView],
)
