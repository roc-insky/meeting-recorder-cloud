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
from fastapi.responses import JSONResponse

from cloud import (
    ai,
    annotations,
    auth,
    events,
    leases,
    meetings,
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
from cloud.objects import object_store_from_env
from cloud.settings import Settings


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

    @app.exception_handler(CloudError)
    async def _cloud_error_handler(_: Request, exc: CloudError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)

    @app.middleware("http")
    async def _private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/cloud"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Registered last so it runs outermost: during a restore no /api/cloud
    # request may touch the database or object store at all.
    @app.middleware("http")
    async def _maintenance(request: Request, call_next):
        if settings.privacy_maintenance and (
            request.url.path == "/api/cloud"
            or request.url.path.startswith("/api/cloud/")
        ):
            return JSONResponse(
                {"error": "maintenance"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)

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
