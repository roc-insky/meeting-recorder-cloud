"""Authenticated playback of derived audio (Task 9).

The only playback surface is a private, owner-scoped route: there are no public
or signed URLs, so revoking access is immediate. Bytes are streamed from object
storage in bounded blocks; a Range request never loads the whole file.

A meeting that is still being recorded is refused with 409 rather than served,
because playing the live system-audio stream back through the speakers would
re-record it. Historical playback is only ever the sealed, derived mix, and the
client is responsible for not autoplaying it (documented in the UI, not here).
"""

from __future__ import annotations

import re
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from cloud.auth import require_user
from cloud.contracts import AuthContext, ConflictError, NotFoundError
from cloud.schema import DerivedAsset, Meeting, UploadSession

router = APIRouter(prefix="/api/cloud", tags=["playback"])

_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_NO_STORE = "private, no-store"


def _parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """Return an inclusive (start, end) window, or None when unsatisfiable.

    Only a single byte range is supported (multi-range is rejected in v1).
    """
    if header is None:
        return None
    match = _RANGE_PATTERN.match(header.strip())
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix <= 0 or total <= 0:
            return None
        start = max(0, total - suffix)
        return start, total - 1
    start = int(first)
    if total <= 0 or start >= total:
        return None
    if not last:
        return start, total - 1
    end = int(last)
    if end < start:
        return None
    return start, min(end, total - 1)


def _headers(asset: DerivedAsset, total: int) -> dict[str, str]:
    return {
        "accept-ranges": "bytes",
        "cache-control": _NO_STORE,
        "content-type": asset.content_type or "audio/wav",
        "content-disposition": f'inline; filename="meeting-{asset.meeting_id}.wav"',
        "x-content-type-options": "nosniff",
        "content-length": str(total),
    }


def _body(ctx: AuthContext, object_key: str, start: int, length: int) -> Iterator[bytes]:
    objects = ctx.require_objects()
    yield from objects.iter_range(object_key, start, length)


def stream_audio(
    ctx: AuthContext, meeting_id: str, range_header: str | None
) -> Response:
    """Serve the latest derived mix for a sealed meeting to its owner."""
    db = ctx.require_db()
    meeting = db.get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    recording = db.scalar(
        select(UploadSession.id)
        .where(
            UploadSession.owner_user_id == ctx.user_id,
            UploadSession.meeting_id == meeting_id,
            UploadSession.status == "open",
        )
        .limit(1)
    )
    if recording is not None:
        raise ConflictError("recording_in_progress")
    asset = db.scalar(
        select(DerivedAsset)
        .where(
            DerivedAsset.owner_user_id == ctx.user_id,
            DerivedAsset.meeting_id == meeting_id,
            DerivedAsset.kind == "mix",
        )
        .order_by(DerivedAsset.input_version.desc(), DerivedAsset.created_at.desc())
        .limit(1)
    )
    if asset is None:
        raise NotFoundError("audio_not_ready")
    total = asset.bytes
    if total <= 0 or ctx.require_objects().object_size(asset.object_key) is None:
        raise NotFoundError("audio_not_ready")
    headers = _headers(asset, total)
    if range_header is None:
        headers["content-length"] = str(total)
        return StreamingResponse(
            _body(ctx, asset.object_key, 0, total),
            status_code=200,
            headers=headers,
        )
    window = _parse_range(range_header, total)
    if window is None:
        return Response(
            status_code=416,
            headers={
                "accept-ranges": "bytes",
                "cache-control": _NO_STORE,
                "content-range": f"bytes */{total}",
                "content-length": "0",
            },
        )
    start, end = window
    length = end - start + 1
    headers["content-range"] = f"bytes {start}-{end}/{total}"
    headers["content-length"] = str(length)
    return StreamingResponse(
        _body(ctx, asset.object_key, start, length),
        status_code=206,
        headers=headers,
    )


@router.get("/meetings/{meeting_id}/audio")
def audio_route(
    request: Request,
    meeting_id: Annotated[str, Path(min_length=1, max_length=64)],
    ctx: AuthContext = Depends(require_user),
) -> Response:
    return stream_audio(ctx, meeting_id, request.headers.get("range"))
