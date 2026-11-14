"""Owner-private interview positions.

A position is a small reusable description of the job a candidate is being
interviewed for. It is the only persistent row Task 14 adds: resumes, OCR
text, prompts, images and generated questions are never stored.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloud.auth import require_csrf, require_user
from cloud.contracts import (
    AuthContext,
    InvalidRequestError,
    NotFoundError,
    PositionCreate,
    PositionUpdate,
    PositionView,
)
from cloud.schema import Position

router = APIRouter(prefix="/api/cloud", tags=["positions"])

POSITION_TYPES = ("前端", "后端", "客户端", "测试")
TITLE_MAX_CHARS = 200
REQUIREMENTS_MAX_CHARS = 20000


def _clean_type(value: Any) -> str:
    if not isinstance(value, str) or value not in POSITION_TYPES:
        raise InvalidRequestError("invalid_position")
    return value


def _clean_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError("invalid_position")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= max_chars:
        raise InvalidRequestError("invalid_position")
    return cleaned


def _require_position(ctx: AuthContext, position_id: str) -> Position:
    row = ctx.require_db().get(Position, (ctx.user_id, position_id))
    if row is None:
        raise NotFoundError()
    return row


def _view(row: Position) -> PositionView:
    return PositionView(
        id=row.id,
        type=row.type,
        title=row.title,
        requirements=row.requirements,
    )


def create_position(
    ctx: AuthContext,
    type: Any,
    title: Any,
    requirements: Any,
    position_id: str | None = None,
) -> PositionView:
    """Create one position owned by the session user."""

    cleaned_type = _clean_type(type)
    cleaned_title = _clean_text(title, field="title", max_chars=TITLE_MAX_CHARS)
    cleaned_requirements = _clean_text(
        requirements, field="requirements", max_chars=REQUIREMENTS_MAX_CHARS
    )
    now = ctx.clock.now()
    row = Position(
        owner_user_id=ctx.user_id,
        id=position_id or _new_id(),
        type=cleaned_type,
        title=cleaned_title,
        requirements=cleaned_requirements,
        created_at=now,
        updated_at=now,
    )
    db = ctx.require_db()
    db.add(row)
    db.commit()
    return _view(row)


def list_positions(ctx: AuthContext) -> list[PositionView]:
    """Return the caller's positions, newest first."""

    rows = (
        ctx.require_db()
        .execute(
            select(Position)
            .where(Position.owner_user_id == ctx.user_id)
            .order_by(Position.created_at.desc(), Position.id.desc())
        )
        .scalars()
        .all()
    )
    return [_view(row) for row in rows]


def update_position(
    ctx: AuthContext,
    position_id: str,
    type: Any = None,
    title: Any = None,
    requirements: Any = None,
) -> PositionView:
    """Patch the supplied fields; another user's position is always a 404."""

    row = _require_position(ctx, position_id)
    if type is None and title is None and requirements is None:
        raise InvalidRequestError("invalid_position")
    if type is not None:
        row.type = _clean_type(type)
    if title is not None:
        row.title = _clean_text(title, field="title", max_chars=TITLE_MAX_CHARS)
    if requirements is not None:
        row.requirements = _clean_text(
            requirements, field="requirements", max_chars=REQUIREMENTS_MAX_CHARS
        )
    row.updated_at = ctx.clock.now()
    ctx.require_db().commit()
    return _view(row)


def delete_position(ctx: AuthContext, position_id: str) -> None:
    """Hard-delete the caller's position; there is no tombstone or history."""

    row = _require_position(ctx, position_id)
    db: Session = ctx.require_db()
    db.delete(row)
    db.commit()


def _new_id() -> str:
    from uuid import uuid4

    return str(uuid4())


def create_position_route(
    payload: PositionCreate, ctx: AuthContext = Depends(require_csrf)
) -> PositionView:
    return create_position(
        ctx,
        type=payload.type,
        title=payload.title,
        requirements=payload.requirements,
    )


def list_positions_route(ctx: AuthContext = Depends(require_user)) -> list[PositionView]:
    return list_positions(ctx)


def update_position_route(
    position_id: str,
    payload: PositionUpdate,
    ctx: AuthContext = Depends(require_csrf),
) -> PositionView:
    return update_position(
        ctx,
        position_id,
        type=payload.type,
        title=payload.title,
        requirements=payload.requirements,
    )


def delete_position_route(
    position_id: str, ctx: AuthContext = Depends(require_csrf)
) -> None:
    delete_position(ctx, position_id)


router.add_api_route(
    "/positions",
    list_positions_route,
    methods=["GET"],
    response_model=list[PositionView],
)
router.add_api_route(
    "/positions",
    create_position_route,
    methods=["POST"],
    response_model=PositionView,
    status_code=201,
)
router.add_api_route(
    "/positions/{position_id}",
    update_position_route,
    methods=["PATCH"],
    response_model=PositionView,
)
router.add_api_route(
    "/positions/{position_id}",
    delete_position_route,
    methods=["DELETE"],
    status_code=204,
)
