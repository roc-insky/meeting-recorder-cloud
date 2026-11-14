"""ASGI application factory for the cloud recorder.

The factory takes explicit dependencies so tests (and later workers) can inject
the engine, clock and identity provider without environment variables. The app
never imports the local recorder controller or web_server.py.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud import (
    ai,
    annotations,
    auth,
    events,
    leases,
    meetings,
    observability,
    playback,
    positions,
    privacy,
    sealing,
    tasks,
    transcripts,
    uploads,
    voiceprints,
)
from cloud.contracts import CloudError
from cloud.db import create_database_engine, make_session_factory
from cloud.limits import RateLimitError
from cloud.objects import object_store_from_env
from cloud.settings import Settings

#: Raised by the body-limit receive wrapper. It must be a distinct type so the
#: request validation and generic exception handlers cannot turn it into a 500.
class BodyTooLarge(Exception):
    pass


_HTTP_CODES = {
    400: "invalid_request",
    401: "authentication_required",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    502: "ai_failed",
    503: "service_unavailable",
}


class RequestIdMiddleware:
    """Assign a server id and attach it to every cloud response.

    Pure ASGI (not BaseHTTPMiddleware) so event streams are never buffered and
    an unhandled failure can still be answered with the fixed error shape.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(
            "/api/cloud"
        ):
            await self.app(scope, receive, send)
            return
        request_id = observability.new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def send_with_id(message: dict) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:  # noqa: BLE001 - fixed envelope, never exception text
            observability.log_event("http_unhandled", request_id=request_id)
            if started:
                raise
            response = JSONResponse(
                observability.error_payload("internal_error", request_id),
                status_code=500,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send_with_id)


class BodyLimitMiddleware:
    """Refuse an oversized cloud body before any handler can act on it.

    A declared Content-Length over the budget is rejected without receiving a
    single body message. A chunked body is counted as it arrives and rejected
    the instant it crosses the budget; exactly the budget is allowed.
    """

    _METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app: Any, limit: int) -> None:
        self.app = app
        self.limit = int(limit)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() not in self._METHODS
            or not str(scope.get("path", "")).startswith("/api/cloud")
        ):
            await self.app(scope, receive, send)
            return

        request_id = scope.get("state", {}).get("request_id")
        declared = _declared_length(scope)
        if declared is not None and declared > self.limit:
            await _send_too_large(scope, receive, send, request_id)
            return

        # Read the body here, not inside the application. FastAPI wraps an
        # exception raised by receive() into a generic 400, which would hide
        # the fixed 413 from the client. Buffering at most limit+1 bytes also
        # guarantees a chunked body is never fully drained once it is too big.
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                await _send_too_large(scope, receive, send, request_id)
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.limit:
                await _send_too_large(scope, receive, send, request_id)
                return
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def _declared_length(scope: dict) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None


async def _send_too_large(
    scope: dict, receive: Any, send: Any, request_id: str | None
) -> None:
    observability.log_event("body_too_large", request_id=request_id)
    response = JSONResponse(
        observability.error_payload("request_too_large", request_id),
        status_code=413,
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def create_app(settings: Settings, dependencies: dict[str, Any] | None = None) -> FastAPI:
    settings.validate_for_startup()

    deps = dict(dependencies or {})
    engine = deps.get("engine")
    owns_engine = engine is None
    if engine is None:
        engine = create_database_engine(settings)
    session_factory = deps.get("session_factory") or make_session_factory(engine)
    clock = deps.get("clock") or SystemClock()
    identity_provider = deps.get("identity_provider") or auth.build_identity_provider(settings)
    if "object_store" in deps:
        object_store = deps["object_store"]
    else:
        object_store = object_store_from_env(os.environ)
    # The audit is deliberately outside PostgreSQL and the object store. A
    # configured path is mandatory for real use; test/development defaults to
    # an in-process store so the app still starts without filesystem setup.
    if "audit_store" in deps:
        audit_store = deps["audit_store"]
    elif settings.privacy_audit_path:
        audit_store = privacy.TombstoneAuditStore(settings.privacy_audit_path)
    else:
        audit_store = privacy.MemoryTombstoneStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Cloud meeting recorder",
        version="0.5.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.identity_provider = identity_provider
    app.state.object_store = object_store
    app.state.audit_store = audit_store
    privacy.configure_runtime(session_factory, settings, object_store, audit_store)
    # Injected AI/OCR engines. None means the endpoint answers 503
    # ai_not_configured; settings never name a URL or endpoint.
    app.state.ai_provider = deps.get("ai_provider")
    app.state.ai_ocr_provider = deps.get("ai_ocr_provider")
    app.state.ai_executor = deps.get("ai_executor")
    app.state.ai_ocr_executor = deps.get("ai_ocr_executor")
    app.state.dependencies = deps

    def _payload(request: Request, code: str) -> dict[str, str]:
        return observability.error_payload(code, observability.get_request_id(request))

    @app.exception_handler(CloudError)
    async def _cloud_error_handler(request: Request, exc: CloudError) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after)
        elif exc.status_code in (401, 403):
            observability.emit_metric(
                "access_rejection", 1, {"reason": "forbidden"}
            )
            observability.log_event(
                "http_rejected",
                request_id=observability.get_request_id(request),
                code=exc.code,
            )
        elif exc.status_code == 404:
            observability.emit_metric(
                "access_rejection", 1, {"reason": "not_found"}
            )
        return JSONResponse(
            _payload(request, exc.code), status_code=exc.status_code, headers=headers
        )

    @app.exception_handler(BodyTooLarge)
    async def _body_too_large_handler(
        request: Request, _exc: BodyTooLarge
    ) -> JSONResponse:
        observability.log_event(
            "body_too_large", request_id=observability.get_request_id(request)
        )
        return JSONResponse(
            _payload(request, "request_too_large"),
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            _payload(request, "invalid_request"),
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code)
        if code is None:
            code = "internal_error" if exc.status_code >= 500 else "invalid_request"
        headers = {"Cache-Control": "no-store"}
        if exc.status_code == 405 and exc.headers:
            allow = exc.headers.get("allow")
            if allow:
                headers["Allow"] = allow
        return JSONResponse(
            _payload(request, code), status_code=exc.status_code, headers=headers
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = observability.get_request_id(request)
        status, code = observability.safe_error(exc)
        observability.log_event("http_unhandled", request_id=request_id, code=code)
        return JSONResponse(
            observability.error_payload(code, request_id),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    # Registration order matters: the last added middleware runs outermost.
    # Request id wraps maintenance so even a maintenance response carries the
    # header; body limiting is innermost so it sees the real receive stream.
    app.add_middleware(BodyLimitMiddleware, limit=settings.request_max_bytes)

    @app.middleware("http")
    async def _private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/cloud"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Registered after private responses so it runs outside them: during a
    # restore no /api/cloud request may touch the database or object store.
    @app.middleware("http")
    async def _maintenance(request: Request, call_next):
        if settings.privacy_maintenance and (
            request.url.path == "/api/cloud"
            or request.url.path.startswith("/api/cloud/")
        ):
            return JSONResponse(
                _payload(request, "maintenance"),
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)

    app.add_middleware(RequestIdMiddleware)

    app.include_router(auth.router)
    app.include_router(meetings.router)
    app.include_router(privacy.router)
    app.include_router(annotations.router)
    app.include_router(positions.router)
    app.include_router(ai.router)
    app.include_router(events.router)
    app.include_router(uploads.router)
    app.include_router(leases.router)
    app.include_router(sealing.router)
    app.include_router(tasks.router)
    app.include_router(transcripts.router)
    app.include_router(playback.router)
    app.include_router(voiceprints.router)
    return app
