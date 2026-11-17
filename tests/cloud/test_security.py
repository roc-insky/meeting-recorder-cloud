"""Task 16: owner isolation, bounded bodies and private processing.

The tests exercise real PostgreSQL and a real local object-store root (a
temporary directory), not an in-memory fake: ownership fences, shared
fixed-window budgets and the concurrent decode slot are only meaningful when
two independent sessions really race. Browser checks live in
tests/e2e/cloud/isolation.spec.mjs; this module never claims a real browser,
provider or AWS deployment.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from cloud import limits, observability
from cloud.app import create_app
from cloud.auth import hash_session_token
from cloud.contracts import AuthContext, ServiceUnavailableError
from cloud.limits import RateLimitError
from cloud.schema import ProcessingTask
from cloud.settings import Settings
from cloud.worker import process_once
from tests.cloud.support import A_EMAIL, CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"
REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")
SENTINEL = "SENSITIVE-9f4e7d-sentinel"
WAV_SAMPLE = (
    b"RIFF\x24\x00\x00\x00WAVEfmt "
    b"\x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80\x3e\x00\x00\x00\x7d\x00\x00"
    b"\x02\x00\x10\x00data\x00\x00\x00\x00"
)


class SecurityCase(CloudCase):
    """Owner-isolation, limits, body bounds and sanitised failure surfaces."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.settings = cls.settings.model_copy(
            update={
                "voiceprint_key": base64.b64encode(
                    b"0123456789abcdef0123456789abcdef"
                ).decode("ascii")
            }
        )
        cls._build_app()

    def expect(
        self, response: Any, status: int = 200, code: str | None = None
    ) -> Any:
        self.assertEqual(
            response.status_code,
            status,
            msg=f"unexpected status {response.status_code}: {response.text[:800]}",
        )
        self.assert_request_id(response)
        if code is not None:
            self.assertEqual(response.json().get("error"), code)
        return response

    def assert_request_id(self, response: Any) -> None:
        header = response.headers.get("x-request-id", "")
        self.assertRegex(header, REQUEST_ID)
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            self.assertEqual(body.get("request_id"), header)
            self.assertRegex(body["request_id"], REQUEST_ID)

    def context(self, user_id: str) -> tuple[Session, AuthContext]:
        db = self.session_factory()
        self.addCleanup(db.close)
        session_id = self.query_value(
            "SELECT id FROM sessions WHERE user_id = :u ORDER BY created_at DESC LIMIT 1",
            u=user_id,
        )
        ctx = AuthContext(
            user_id=user_id,
            session_id=session_id or "security-test-session",
            db=db,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )
        return db, ctx

    def open_upload(self, client: Any, meeting_id: str) -> tuple[str, str, dict[str, str]]:
        csrf = self.csrf_headers(client)
        lease = self.expect(
            client.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting_id},
                headers=csrf,
            ),
            201,
        ).json()["lease_token"]
        headers = {**csrf, "X-Capture-Lease": lease}
        upload = self.expect(
            client.post(
                "/api/cloud/uploads",
                json={"meeting_id": meeting_id},
                headers=headers,
            ),
            201,
        ).json()["id"]
        self.expect(
            client.post(
                f"/api/cloud/uploads/{upload}/epochs",
                json={
                    "id": "e1",
                    "offset_ms": 0,
                    "uncertainty_ms": 0,
                    "started_at": STARTED_AT,
                    "tracks": [dict(MIC)],
                },
                headers=headers,
            )
        )
        return lease, upload, headers

    def put_chunk(
        self,
        client: Any,
        upload_id: str,
        headers: dict[str, str],
        sequence: int = 0,
        body: bytes = b"bounded-chunk",
        start_ms: int = 0,
        end_ms: int = 3000,
    ) -> Any:
        return client.put(
            f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/{sequence}",
            params={
                "start_ms": start_ms,
                "end_ms": end_ms,
                "checksum": hashlib.sha256(body).hexdigest(),
                "codec": CODEC,
            },
            content=body,
            headers=headers,
        )

    def insert_transcript(
        self, meeting_id: str, owner: str | None = None, transcript_text: str = "私密转写内容"
    ) -> str:
        owner = owner or self.a_user_id
        version_id = str(uuid4())
        segment_id = str(uuid4())
        now = self.clock.now()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO transcript_versions
                        (owner_user_id, id, meeting_id, task_id, input_version,
                         generation, payload, status, empty, warnings, created_at)
                    VALUES
                        (:owner, :id, :meeting, NULL, 1, 0, '{}'::jsonb,
                         'complete', false, '[]'::jsonb, :now)
                    """
                ),
                {
                    "owner": owner,
                    "id": version_id,
                    "meeting": meeting_id,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO transcript_segments
                        (owner_user_id, version_id, id, ordinal, start_ms, end_ms,
                         text, speaker_id, track_id)
                    VALUES
                        (:owner, :version, :segment, 0, 0, 3000,
                         :text, NULL, 'mic')
                    """
                ),
                {
                    "owner": owner,
                    "version": version_id,
                    "segment": segment_id,
                    "text": transcript_text,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE meetings SET current_transcript_id = :version
                    WHERE owner_user_id = :owner AND id = :meeting
                    """
                ),
                {"owner": owner, "version": version_id, "meeting": meeting_id},
            )
        return version_id

    def insert_profile(
        self,
        owner: str | None = None,
        name: str = "历史声纹",
        ciphertext: bytes = b"ciphertext",
    ) -> str:
        owner = owner or self.a_user_id
        profile_id = str(uuid4())
        now = self.clock.now()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO voiceprint_profiles
                        (owner_user_id, id, name, version, embedding_ciphertext,
                         model_id, model_version, weights_sha256, dimension,
                         sample_rate, preprocess_version, normalization_version,
                         calibration_version, revoked_at, revocation_reason,
                         created_at)
                    VALUES
                        (:owner, :id, :name, 1, :ciphertext,
                         'test-model', '1', :hash, 192, 16000, 'p1', 'n1', 'c1',
                         NULL, NULL, :now)
                    """
                ),
                {
                    "owner": owner,
                    "id": profile_id,
                    "name": name,
                    "ciphertext": ciphertext,
                    "hash": "0" * 64,
                    "now": now,
                },
            )
        return profile_id

    def create_position(self, client: Any, title: str = "后端工程师") -> str:
        return self.expect(
            client.post(
                "/api/cloud/positions",
                json={
                    "type": "后端",
                    "title": title,
                    "requirements": "熟悉 Python 与 PostgreSQL",
                },
                headers=self.csrf_headers(client),
            ),
            201,
        ).json()["id"]

    def enroll_voiceprint(self, client: Any, name: str = "主持人") -> str:
        return self.expect(
            client.post(
                "/api/cloud/voiceprints",
                data={"name": name, "consent": "已获得明确同意"},
                files={"audio": ("sample.wav", WAV_SAMPLE, "audio/wav")},
                headers=self.csrf_headers(client),
            ),
            201,
        ).json()["id"]

    def app_for(self, **deps: Any) -> TestClient:
        return self.app_client(
            raise_server_exceptions=False, extra_deps=deps
        )

    # -- 1. every private resource is owner-scoped ------------------------

    def test_all_private_resource_operations(self) -> None:
        meeting = self.create_meeting(self.a)
        lease, upload, headers = self.open_upload(self.a, meeting)
        self.expect(self.put_chunk(self.a, upload, headers), 200)
        self.expect(
            self.a.post(
                f"/api/cloud/meetings/{meeting}/annotations",
                json={"kind": "note", "offset_ms": 0, "text": "私有批注"},
                headers=self.csrf_headers(self.a),
            ),
            201,
        )
        position = self.create_position(self.a)
        version = self.insert_transcript(meeting)
        profile = self.insert_profile()
        voice_task = self.enroll_voiceprint(self.a)

        # Anonymous requests never reach a private operation.
        for path in (
            f"/api/cloud/meetings/{meeting}",
            f"/api/cloud/meetings/{meeting}/transcripts",
            f"/api/cloud/meetings/{meeting}/audio",
        ):
            self.expect(self.anon.get(path), 401, "authentication_required")

        # Every B request is either owner-scoped 404 or a scoped empty list.
        not_found = (
            ("get", f"/api/cloud/meetings/{meeting}", None),
            ("patch", f"/api/cloud/meetings/{meeting}", {"title": "stolen"}),
            ("delete", f"/api/cloud/meetings/{meeting}", None),
            ("post", "/api/cloud/capture-lease", {"meeting_id": meeting}),
            ("post", "/api/cloud/uploads", {"meeting_id": meeting}),
            (
                "post",
                f"/api/cloud/uploads/{upload}/epochs",
                {
                    "id": "e2",
                    "offset_ms": 0,
                    "uncertainty_ms": 0,
                    "started_at": STARTED_AT,
                    "tracks": [dict(MIC)],
                },
            ),
            ("get", f"/api/cloud/uploads/{upload}/ack", None),
            (
                "post",
                f"/api/cloud/uploads/{upload}/seal",
                {"tracks": [], "accept_partial": True},
            ),
            ("post", f"/api/cloud/meetings/{meeting}/tasks/final", {"input_version": 1}),
            ("post", f"/api/cloud/tasks/{voice_task}/cancel", None),
            ("get", f"/api/cloud/meetings/{meeting}/transcripts", None),
            ("get", f"/api/cloud/meetings/{meeting}/transcripts/{version}", None),
            ("post", f"/api/cloud/meetings/{meeting}/transcripts/supplement", None),
            ("post", f"/api/cloud/meetings/{meeting}/transcripts/re-transcribe", None),
            ("get", f"/api/cloud/meetings/{meeting}/annotations", None),
            (
                "post",
                f"/api/cloud/meetings/{meeting}/annotations",
                {"kind": "note", "offset_ms": 1, "text": "stolen"},
            ),
            ("patch", f"/api/cloud/positions/{position}", {"title": "stolen"}),
            ("delete", f"/api/cloud/positions/{position}", None),
            ("post", f"/api/cloud/positions/{position}/interview", None),
            (
                "post",
                f"/api/cloud/meetings/{meeting}/summary",
                {"transcript_version_id": version},
            ),
            ("get", f"/api/cloud/meetings/{meeting}/events", None),
            ("get", f"/api/cloud/meetings/{meeting}/events/stream", None),
            ("get", f"/api/cloud/voiceprints/tasks/{voice_task}", None),
            ("post", f"/api/cloud/voiceprints/tasks/{voice_task}/cancel", None),
            ("delete", f"/api/cloud/voiceprints/{profile}", None),
        )
        for method, path, payload in not_found:
            with self.subTest(method=method, path=path):
                kwargs: dict[str, Any] = {"headers": self.csrf_headers(self.b)}
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(self.b, method)(path, **kwargs)
                self.expect(response, 404)
                self.assertTrue(
                    response.json()["error"].endswith("not_found"), response.text
                )
        foreign_chunk = self.put_chunk(
            self.b,
            upload,
            {**self.csrf_headers(self.b), "X-Capture-Lease": lease},
        )
        self.expect(foreign_chunk, 404)
        self.assertTrue(foreign_chunk.json()["error"].endswith("not_found"))
        for path in (
            "/api/cloud/positions",
            "/api/cloud/voiceprints",
            "/api/cloud/meetings",
        ):
            response = self.b.get(path)
            self.expect(response, 200)
            items = response.json()
            if isinstance(items, dict):
                items = items.get("items", [])
            ids = {item.get("id") for item in items}
            self.assertNotIn(profile, ids)
            self.assertNotIn(voice_task, ids)
            self.assertNotIn(meeting, ids)

        # The owner still reads their own private state.
        for method, path in (
            ("get", f"/api/cloud/meetings/{meeting}"),
            ("get", f"/api/cloud/meetings/{meeting}/transcripts"),
            ("get", f"/api/cloud/meetings/{meeting}/transcripts/{version}"),
            ("get", f"/api/cloud/meetings/{meeting}/annotations"),
            ("get", "/api/cloud/positions"),
            ("get", "/api/cloud/voiceprints"),
        ):
            self.expect(getattr(self.a, method)(path), 200)

        # Unknown identifiers are indistinguishable from another owner's rows:
        # 404 before any row is created or mutated.
        unknown = "00000000-0000-4000-8000-000000000000"
        before_unknown = {
            table: self.query_count(table)
            for table in (
                "meetings",
                "upload_sessions",
                "chunks",
                "processing_tasks",
                "transcript_versions",
                "annotations",
                "positions",
                "voiceprint_profiles",
            )
        }
        unknown_operations = (
            ("get", f"/api/cloud/meetings/{unknown}", None),
            ("patch", f"/api/cloud/meetings/{unknown}", {"title": "x"}),
            ("delete", f"/api/cloud/meetings/{unknown}", None),
            ("get", f"/api/cloud/meetings/{unknown}/audio", None),
            ("post", "/api/cloud/capture-lease", {"meeting_id": unknown}),
            ("post", "/api/cloud/uploads", {"meeting_id": unknown}),
            (
                "post",
                f"/api/cloud/uploads/{unknown}/epochs",
                {
                    "id": "e1",
                    "offset_ms": 0,
                    "uncertainty_ms": 0,
                    "started_at": STARTED_AT,
                    "tracks": [dict(MIC)],
                },
            ),
            ("get", f"/api/cloud/uploads/{unknown}/ack", None),
            (
                "post",
                f"/api/cloud/uploads/{unknown}/seal",
                {"tracks": [], "accept_partial": True},
            ),
            ("post", f"/api/cloud/meetings/{unknown}/tasks/final", {"input_version": 1}),
            ("post", f"/api/cloud/tasks/{unknown}/cancel", None),
            ("get", f"/api/cloud/meetings/{unknown}/transcripts", None),
            (
                "get",
                f"/api/cloud/meetings/{unknown}/transcripts/{unknown}",
                None,
            ),
            ("post", f"/api/cloud/meetings/{unknown}/transcripts/supplement", None),
            (
                "post",
                f"/api/cloud/meetings/{unknown}/transcripts/re-transcribe",
                None,
            ),
            ("get", f"/api/cloud/meetings/{unknown}/annotations", None),
            (
                "post",
                f"/api/cloud/meetings/{unknown}/annotations",
                {"kind": "note", "offset_ms": 0, "text": "x"},
            ),
            ("patch", f"/api/cloud/positions/{unknown}", {"title": "x"}),
            ("delete", f"/api/cloud/positions/{unknown}", None),
            ("post", f"/api/cloud/positions/{unknown}/interview", {"resume": "x"}),
            (
                "post",
                f"/api/cloud/meetings/{unknown}/summary",
                {"transcript_version_id": unknown},
            ),
            ("get", f"/api/cloud/meetings/{unknown}/events", None),
            ("get", f"/api/cloud/meetings/{unknown}/events/stream", None),
            ("get", f"/api/cloud/voiceprints/tasks/{unknown}", None),
            ("post", f"/api/cloud/voiceprints/tasks/{unknown}/cancel", None),
            ("delete", f"/api/cloud/voiceprints/{unknown}", None),
        )
        for method, path, payload in unknown_operations:
            with self.subTest(scope="unknown", method=method, path=path):
                kwargs: dict[str, Any] = {"headers": self.csrf_headers(self.a)}
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(self.a, method)(path, **kwargs)
                self.assertEqual(
                    response.status_code,
                    404,
                    f"{method} {path}: {response.status_code} {response.text[:400]}",
                )
                self.assert_request_id(response)
        self.assertEqual(
            {
                table: self.query_count(table)
                for table in before_unknown
            },
            before_unknown,
        )

        # Release the first lease so the deleted-meeting fixture can acquire
        # its own; releasing never deletes the already-ACKed chunk.
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease/release",
                json={"lease_token": lease},
                headers=self.csrf_headers(self.a),
            ),
            204,
        )

        # A deleted meeting fences every meeting-scoped operation, including
        # Range playback, chunk writes and task/transcript mutation. Owner-level
        # positions and voiceprints deliberately survive this deletion.
        deleted = self.create_meeting(self.a, title="Deleted matrix")
        deleted_lease, deleted_upload, deleted_headers = self.open_upload(
            self.a, deleted
        )
        self.expect(self.put_chunk(self.a, deleted_upload, deleted_headers), 200)
        deleted_annotation = self.expect(
            self.a.post(
                f"/api/cloud/meetings/{deleted}/annotations",
                json={"kind": "note", "offset_ms": 0, "text": "删除前批注"},
                headers=self.csrf_headers(self.a),
            ),
            201,
        ).json()["id"]
        deleted_version = self.insert_transcript(deleted)
        self.expect(
            self.a.delete(
                f"/api/cloud/meetings/{deleted}",
                headers=self.csrf_headers(self.a),
            ),
            204,
        )
        before_deleted = {
            table: self.query_count(table)
            for table in (
                "meetings",
                "upload_sessions",
                "chunks",
                "processing_tasks",
                "transcript_versions",
                "annotations",
            )
        }
        deleted_operations = (
            ("get", f"/api/cloud/meetings/{deleted}", None),
            ("patch", f"/api/cloud/meetings/{deleted}", {"title": "x"}),
            ("delete", f"/api/cloud/meetings/{deleted}", None),
            ("get", f"/api/cloud/meetings/{deleted}/audio", None),
            ("post", "/api/cloud/capture-lease", {"meeting_id": deleted}),
            ("post", "/api/cloud/uploads", {"meeting_id": deleted}),
            (
                "post",
                f"/api/cloud/uploads/{deleted_upload}/epochs",
                {
                    "id": "e2",
                    "offset_ms": 0,
                    "uncertainty_ms": 0,
                    "started_at": STARTED_AT,
                    "tracks": [dict(MIC)],
                },
            ),
            ("get", f"/api/cloud/uploads/{deleted_upload}/ack", None),
            (
                "post",
                f"/api/cloud/uploads/{deleted_upload}/seal",
                {"tracks": [], "accept_partial": True},
            ),
            (
                "post",
                f"/api/cloud/meetings/{deleted}/tasks/final",
                {"input_version": 1},
            ),
            ("get", f"/api/cloud/meetings/{deleted}/transcripts", None),
            (
                "get",
                f"/api/cloud/meetings/{deleted}/transcripts/{deleted_version}",
                None,
            ),
            (
                "post",
                f"/api/cloud/meetings/{deleted}/transcripts/supplement",
                None,
            ),
            (
                "post",
                f"/api/cloud/meetings/{deleted}/transcripts/re-transcribe",
                None,
            ),
            ("get", f"/api/cloud/meetings/{deleted}/annotations", None),
            (
                "post",
                f"/api/cloud/meetings/{deleted}/annotations",
                {"kind": "note", "offset_ms": 0, "text": "x"},
            ),
            (
                "post",
                f"/api/cloud/meetings/{deleted}/summary",
                {"transcript_version_id": deleted_version},
            ),
            ("get", f"/api/cloud/meetings/{deleted}/events", None),
            ("get", f"/api/cloud/meetings/{deleted}/events/stream", None),
        )
        for method, path, payload in deleted_operations:
            with self.subTest(scope="deleted", method=method, path=path):
                kwargs: dict[str, Any] = {"headers": self.csrf_headers(self.a)}
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(self.a, method)(path, **kwargs)
                self.assertEqual(
                    response.status_code,
                    404,
                    f"{method} {path}: {response.status_code} {response.text[:400]}",
                )
                self.assert_request_id(response)
        ranged = self.a.get(
            f"/api/cloud/meetings/{deleted}/audio", headers={"Range": "bytes=0-3"}
        )
        self.assertEqual(ranged.status_code, 404, ranged.text)
        stale_chunk = self.put_chunk(self.a, deleted_upload, deleted_headers)
        self.assertEqual(stale_chunk.status_code, 404, stale_chunk.text)
        self.assertIsNotNone(deleted_annotation)
        self.assertEqual(
            {table: self.query_count(table) for table in before_deleted},
            before_deleted,
        )

    # -- 2. body bound before a complete read -----------------------------

    def test_size_limit_before_reading_full_body(self) -> None:
        settings = self.settings.model_copy(update={"request_max_bytes": 1024})
        client = self.app_client(settings=settings, raise_server_exceptions=False)
        app = client.app
        raw_session = self.a.cookies.get("cloud_session")
        raw_csrf_cookie = self.a.cookies.get("cloud_csrf")
        csrf_header = self.csrf_headers(self.a)["X-CSRF-Token"]
        self.assertIsNotNone(raw_session)
        base_headers = [
            (b"content-type", b"application/json"),
            (
                b"cookie",
                f"cloud_session={raw_session}; cloud_csrf={raw_csrf_cookie}".encode(),
            ),
            (b"x-csrf-token", csrf_header.encode()),
        ]

        calls = {"n": 0}

        async def receive_chunked() -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] > 100:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"x" * 512, "more_body": True}

        status, body, headers = self._run_asgi(
            app,
            {
                "type": "http",
                "method": "POST",
                "path": "/api/cloud/uploads",
                "raw_path": b"/api/cloud/uploads",
                "query_string": b"",
                "headers": base_headers,
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 123),
                "root_path": "",
                "http_version": "1.1",
            },
            receive_chunked,
        )
        self.assertEqual(status, 413, body)
        self.assertEqual(body["error"], "request_too_large")
        self.assertRegex(body["request_id"], REQUEST_ID)
        self.assertRegex(headers.get(b"x-request-id", b"").decode(), REQUEST_ID)
        self.assertLessEqual(calls["n"], 4)
        self.assertEqual(self.query_count("upload_sessions"), 0)

        declared_calls = {"n": 0}

        async def receive_never() -> dict[str, Any]:
            declared_calls["n"] += 1
            raise AssertionError("declared oversized body must not be received")

        status, body, _ = self._run_asgi(
            app,
            {
                "type": "http",
                "method": "POST",
                "path": "/api/cloud/uploads",
                "raw_path": b"/api/cloud/uploads",
                "query_string": b"",
                "headers": base_headers + [(b"content-length", b"999999")],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 123),
                "root_path": "",
                "http_version": "1.1",
            },
            receive_never,
        )
        self.assertEqual(status, 413, body)
        self.assertEqual(body["error"], "request_too_large")
        self.assertEqual(declared_calls["n"], 0)

        exact = b"{" + b" " * 1022 + b"}"
        self.assertEqual(len(exact), 1024)

        async def receive_exact() -> dict[str, Any]:
            return {"type": "http.request", "body": exact, "more_body": False}

        status, body, _ = self._run_asgi(
            app,
            {
                "type": "http",
                "method": "POST",
                "path": "/api/cloud/uploads",
                "raw_path": b"/api/cloud/uploads",
                "query_string": b"",
                "headers": base_headers + [(b"content-length", b"1024")],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 123),
                "root_path": "",
                "http_version": "1.1",
            },
            receive_exact,
        )
        self.assertNotEqual(status, 413, body)
        self.assertEqual(status, 422, body)

    def _run_asgi(
        self, app: Any, scope: dict[str, Any], receive: Any
    ) -> tuple[int, dict[str, Any], dict[bytes, bytes]]:
        import json

        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        asyncio.run(app(scope, receive, send))
        start = next(m for m in messages if m["type"] == "http.response.start")
        payload = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return (
            int(start["status"]),
            json.loads(payload.decode("utf-8")),
            {k.lower(): v for k, v in start.get("headers", [])},
        )

    # -- 3. no sensitive data in errors or logs ---------------------------

    def test_no_sensitive_error_or_log(self) -> None:
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        handler = Capture()
        # Attach to the emitting loggers themselves: an in-process Alembic
        # run disables pre-existing loggers, so relying on the parent logger
        # would make this test pass or fail depending on test order.
        targets = [
            logging.getLogger("cloud.metrics"),
            logging.getLogger("cloud.events"),
        ]
        states = [(target, target.disabled, target.level) for target in targets]
        for target in targets:
            target.disabled = False
            target.setLevel(logging.DEBUG)
            target.addHandler(handler)

        def restore_logging() -> None:
            for target, disabled, level in states:
                target.removeHandler(handler)
                target.disabled = disabled
                target.setLevel(level)

        self.addCleanup(restore_logging)

        bad = self.app_client(raise_server_exceptions=False)
        response = bad.post(
            "/api/cloud/meetings",
            json={"title": SENTINEL, "mode": "mic", "speakers": "many"},
            headers=self.csrf_headers(self.a),
        )
        self.expect(response, 422, "invalid_request")
        self.assertNotIn(SENTINEL, response.text)

        # Secrets carried in cookies, CSRF headers and provider codes are never
        # echoed in an error body, response header or application log.
        # A freshly built app_client copies account A's real cookies, so a
        # forged cookie could ride along next to a valid one. Use a bare client
        # to prove the forged token on its own is rejected.
        raw_cookie = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(raw_cookie.close)
        raw_cookie.cookies.set("cloud_session", SENTINEL, path="/")
        response = raw_cookie.get("/api/cloud/meetings")
        self.expect(response, 401, "authentication_required")
        self.assertNotIn(SENTINEL, response.text)
        self.assertNotIn(SENTINEL, response.headers.get("x-request-id", ""))

        bad_csrf = self.app_client(raise_server_exceptions=False)
        response = bad_csrf.post(
            "/api/cloud/meetings",
            json={"title": "x", "mode": "mic"},
            headers={"X-CSRF-Token": SENTINEL},
        )
        self.expect(response, 403, "csrf_failed")
        self.assertNotIn(SENTINEL, response.text)

        login_client = TestClient(self.app, follow_redirects=False)
        start = login_client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        self.assertEqual(start.status_code, 303, start.text)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        response = login_client.get(
            "/api/cloud/auth/callback",
            params={"code": SENTINEL, "state": state},
        )
        self.expect(response, 401, "login_failed")
        self.assertNotIn(SENTINEL, response.text)
        login_client.close()

        sentinel_meeting = self.create_meeting(self.a)
        sentinel_version = self.insert_transcript(sentinel_meeting, transcript_text=SENTINEL)
        response = self.a.get(
            f"/api/cloud/meetings/{sentinel_meeting}/transcripts/{sentinel_version}"
        )
        self.expect(response, 200)
        self.assertIn(SENTINEL, response.text)
        self.assertNotIn(SENTINEL, "\n".join(records))

        sentinel_profile = self.insert_profile(ciphertext=SENTINEL.encode())
        response = self.a.get("/api/cloud/voiceprints")
        self.expect(response, 200)
        self.assertNotIn(SENTINEL, response.text)
        self.assertNotIn(SENTINEL, "\n".join(records))
        self.assertIsNotNone(sentinel_profile)

        position = self.create_position(self.a)
        response = self.a.post(
            f"/api/cloud/positions/{position}/interview",
            data={"resume_text": SENTINEL},
            headers=self.csrf_headers(self.a),
        )
        self.assertIn(response.status_code, (502, 503), response.text)
        self.assertNotIn(SENTINEL, response.text)
        self.assertNotIn(SENTINEL, "\n".join(records))

        enroll = self.a.post(
            "/api/cloud/voiceprints",
            data={"name": "Sentinel sample", "consent": "explicit consent"},
            files={
                "audio": (
                    "sample.wav",
                    WAV_SAMPLE + SENTINEL.encode("utf-8"),
                    "audio/wav",
                )
            },
            headers=self.csrf_headers(self.a),
        )
        self.expect(enroll, 201)
        self.assertNotIn(SENTINEL, enroll.text)
        self.assertNotIn(SENTINEL, "\n".join(records))

        class ExplodingFactory:
            def __call__(self) -> Any:
                raise RuntimeError(SENTINEL)

        broken = self.app_client(
            session_factory=ExplodingFactory(), raise_server_exceptions=False
        )
        response = broken.get("/api/cloud/meetings")
        self.expect(response, 500, "internal_error")
        self.assertNotIn(SENTINEL, response.text)
        self.assertNotIn(SENTINEL, response.headers.get("x-request-id", ""))

        meeting = self.create_meeting(self.a)
        _, upload, headers = self.open_upload(self.a, meeting)

        class ExplodingStore:
            def __getattr__(self, _name: str) -> Any:
                raise RuntimeError(SENTINEL)

        broken = self.app_client(
            object_store=ExplodingStore(), raise_server_exceptions=False
        )
        for cookie in self.a.cookies.jar:
            broken.cookies.jar.set_cookie(cookie)
        response = self.put_chunk(broken, upload, headers)
        self.expect(response, 500, "internal_error")
        self.assertNotIn(SENTINEL, response.text)
        self.assertEqual(self.query_count("chunks"), 0)

        version = self.insert_transcript(meeting)

        class ExplodingProvider:
            def generate(self, *_args: Any, **_kwargs: Any) -> str:
                raise RuntimeError(SENTINEL)

        broken = self.app_client(
            extra_deps={"ai_provider": ExplodingProvider()},
            raise_server_exceptions=False,
        )
        response = broken.post(
            f"/api/cloud/meetings/{meeting}/summary",
            json={"transcript_version_id": version},
            headers=self.csrf_headers(self.a),
        )
        self.expect(response, 502, "ai_failed")
        self.assertNotIn(SENTINEL, response.text)

        self.assertEqual(observability.safe_error(RuntimeError(SENTINEL)), (500, "internal_error"))
        observability.emit_metric("worker_failure", 1, {"stage": SENTINEL})
        observability.emit_metric(SENTINEL, 1)
        observability.log_event("http_rejected", code=SENTINEL, route=SENTINEL)
        joined = "\n".join(records)
        self.assertNotIn(SENTINEL, joined)
        self.assertIn('"name":"ai_provider"', joined)
        self.assertIn('"state":"degraded"', joined)
        self.assertIn('"event":"metric_dropped"', joined)

    # -- 4. server-side revocation and SSRF-shaped configuration ----------

    def test_session_revocation_and_ssrf(self) -> None:
        raw = self.a.cookies.get("cloud_session")
        self.assertIsNotNone(raw)
        sid = hash_session_token(raw)
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE sessions SET revoked_at = :now WHERE id = :sid"),
                {"now": self.clock.now(), "sid": sid},
            )
        self.expect(self.a.get("/api/cloud/meetings"), 401, "authentication_required")
        self.expect(self.a.get("/api/cloud/auth/session"), 401, "authentication_required")
        self.expect(self.b.get("/api/cloud/meetings"), 200)

        with self.assertRaises(ValidationError):
            Settings(
                **(
                    self.settings.model_dump()
                    | {"ai_provider": "https://evil.example/hook"}
                )
            )
        with self.assertRaises(ValidationError):
            Settings(
                **(
                    self.settings.model_dump()
                    | {"public_base_url": "https://example.test/redirect"}
                )
            )

        meeting = self.create_meeting(self.b)
        version = self.insert_transcript(meeting, owner=self.b_user_id)

        class SpyProvider:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, *_args: Any, **_kwargs: Any) -> str:
                self.calls += 1
                return "ok"

        spy = SpyProvider()
        app = self.app_client(
            extra_deps={"ai_provider": spy}, raise_server_exceptions=False
        )
        for cookie in self.b.cookies.jar:
            app.cookies.jar.set_cookie(cookie)
        response = app.post(
            f"/api/cloud/meetings/{meeting}/summary",
            json={
                "transcript_version_id": version,
                "provider_url": "https://evil.example/hook",
                "callback_url": "https://evil.example/callback",
            },
            headers=self.csrf_headers(self.b),
        )
        self.expect(response, 422, "invalid_request")
        self.assertEqual(spy.calls, 0)

    # -- 5. shared fixed-window budgets ----------------------------------

    def test_rate_limit_is_shared_and_fails_closed(self) -> None:
        settings = self.settings.model_copy(update={"rate_limit_chunk_write": 1})
        _, ctx1 = self.context(self.a_user_id)
        _, ctx2 = self.context(self.a_user_id)
        ctx1 = replace(ctx1, settings=settings)
        ctx2 = replace(ctx2, settings=settings)

        barrier = threading.Barrier(2)
        outcomes: list[Any] = []

        def hit(ctx: AuthContext) -> None:
            barrier.wait(timeout=10)
            try:
                limits.check_limit(ctx, "chunk_write", 1)
                outcomes.append(None)
            except RateLimitError as exc:
                outcomes.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(hit, (ctx1, ctx2)))
        self.assertEqual(sum(item is None for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, RateLimitError) for item in outcomes), 1)

        for action, cost in (("unknown", 1), ("chunk_write", 0), ("chunk_write", True)):
            with self.assertRaises(ValueError):
                limits.check_limit(ctx1, action, cost)

        zero = settings.model_copy(update={"rate_limit_chunk_write": 0})
        with self.assertRaises(ServiceUnavailableError):
            limits.check_limit(replace(ctx1, settings=zero), "chunk_write", 1)

        class BrokenBind:
            def get_bind(self) -> Any:
                raise RuntimeError("database unavailable")

        broken_ctx = replace(ctx1, db=BrokenBind())
        with self.assertRaises(ServiceUnavailableError):
            limits.check_limit(broken_ctx, "chunk_write", 1)

        columns = self._columns("request_rate_limits")
        self.assertEqual(
            columns, {"owner_user_id", "action", "window_start", "count"}
        )

    def _columns(self, table: str) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :table
                    """
                ),
                {"table": table},
            ).scalars()
            return set(rows)

    def test_rate_limit_retry_after_and_no_success(self) -> None:
        settings = self.settings.model_copy(update={"rate_limit_chunk_write": 1})
        client = self.app_client(settings=settings, raise_server_exceptions=False)
        meeting = self.create_meeting(client)
        _, upload, headers = self.open_upload(client, meeting)
        self.expect(self.put_chunk(client, upload, headers, 0), 200)

        response = self.put_chunk(client, upload, headers, 1, b"second", 3000, 6000)
        self.expect(response, 429, "rate_limited")
        retry_after = int(response.headers.get("retry-after", "0"))
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, 60)
        self.assertEqual(self.query_count("chunks"), 1)

        self.clock.advance(seconds=61)
        self.expect(self.put_chunk(client, upload, headers, 1, b"second", 3000, 6000), 200)
        self.assertEqual(self.query_count("chunks"), 2)

    def test_transcript_enqueue_routes_share_task_budget(self) -> None:
        from tests.cloud import test_transcripts

        settings = self.settings.model_copy(update={"rate_limit_task_enqueue": 2})
        client = self.app_client(settings=settings, raise_server_exceptions=False)
        first_meeting, _ = test_transcripts.TranscriptsCase.sealed_media(
            self, client=client, duration_ms=600
        )
        supplement = client.post(
            f"/api/cloud/meetings/{first_meeting}/transcripts/supplement",
            headers=self.csrf_headers(client),
        )
        self.expect(supplement, 200)
        self.assertEqual(self.query_count("processing_tasks"), 1)

        second_meeting, _ = test_transcripts.TranscriptsCase.sealed_media(
            self, client=client, duration_ms=600
        )
        self.insert_transcript(second_meeting)
        retranscribe = client.post(
            f"/api/cloud/meetings/{second_meeting}/transcripts/re-transcribe",
            headers=self.csrf_headers(client),
        )
        self.expect(retranscribe, 200)
        self.assertEqual(self.query_count("processing_tasks"), 2)

        throttled = client.post(
            f"/api/cloud/meetings/{second_meeting}/transcripts/re-transcribe",
            headers=self.csrf_headers(client),
        )
        self.expect(throttled, 429, "rate_limited")
        self.assertGreaterEqual(int(throttled.headers.get("retry-after", "0")), 1)
        self.assertEqual(self.query_count("processing_tasks"), 2)

    # -- 6. one real decode slot per owner --------------------------------

    def test_concurrent_decode_is_limited(self) -> None:
        _, ctx1 = self.context(self.a_user_id)
        _, ctx2 = self.context(self.a_user_id)
        _, ctxb = self.context(self.b_user_id)

        self.assertTrue(limits.acquire_decode_slot(ctx1, "task-a1"))
        self.assertFalse(limits.acquire_decode_slot(ctx2, "task-a2"))
        self.assertTrue(limits.acquire_decode_slot(ctxb, "task-b1"))
        limits.release_decode_slot(self.a_user_id, "task-a1", ctx1.db)
        self.assertTrue(limits.acquire_decode_slot(ctx2, "task-a2"))
        limits.release_decode_slot(self.a_user_id, "task-a2", ctx2.db)
        limits.release_decode_slot(self.b_user_id, "task-b1", ctxb.db)

        later = self.clock.now() + timedelta(seconds=181)
        self.assertTrue(limits.acquire_decode_slot(ctx1, "task-a3", now=later))
        self.assertEqual(self.query_count("decode_slots"), 1)
        limits.release_decode_slot(self.a_user_id, "task-a3", ctx1.db)

        for status in ("failed", "cancelled", "completed"):
            task_id = str(uuid4())
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO processing_tasks
                            (id, kind, owner_user_id, meeting_id,
                             upload_session_id, voiceprint_ref, input_version,
                             generation, attempt, status, created_at, updated_at)
                        VALUES
                            (:id, 'voiceprint', :owner, NULL, NULL, :ref, 0, 0, 0,
                             'waiting', :now, :now)
                        """
                    ),
                    {
                        "id": task_id,
                        "owner": self.a_user_id,
                        "ref": str(uuid4()),
                        "now": self.clock.now(),
                    },
                )
            db, ctx = self.context(self.a_user_id)
            self.assertTrue(limits.acquire_decode_slot(ctx, task_id))
            task = db.get(ProcessingTask, task_id)
            self.assertIsNotNone(task)
            task.status = status
            db.commit()
            self.assertEqual(self.query_count("decode_slots"), 0)

        # A busy slot requeues the task without consuming its retry budget:
        # three busy claims in a row must leave it waiting, not failed.
        busy_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO processing_tasks
                        (id, kind, owner_user_id, meeting_id, upload_session_id,
                         voiceprint_ref, input_version, generation, attempt,
                         status, created_at, updated_at)
                    VALUES
                        (:id, 'voiceprint', :owner, NULL, NULL, :ref, 0, 0, 0,
                         'waiting', :now, :now)
                    """
                ),
                {
                    "id": busy_id,
                    "owner": self.a_user_id,
                    "ref": str(uuid4()),
                    "now": self.clock.now(),
                },
            )
        held_db, held_ctx = self.context(self.a_user_id)
        self.assertTrue(limits.acquire_decode_slot(held_ctx, "held-slot"))
        for _ in range(3):
            worker_db = self.session_factory()
            try:
                self.assertFalse(
                    process_once(
                        lambda *_args, **_kwargs: None,
                        worker_id="busy-worker",
                        now=self.clock.now(),
                        db=worker_db,
                        settings=self.settings,
                    )
                )
            finally:
                worker_db.close()
            state = self.query_value(
                "SELECT status || '|' || coalesce(error_code, '') || '|' || attempt "
                "FROM processing_tasks WHERE id = :id",
                id=busy_id,
            )
            self.assertEqual(state, "waiting|decode_busy|0")
        limits.release_decode_slot(self.a_user_id, "held-slot", held_db)
        self.assertEqual(self.query_count("decode_slots"), 0)

    # -- 7. CSRF, XSS, traversal, spoofing and expiry ---------------------

    def test_csrf_xss_path_traversal_file_spoof_and_expiry(self) -> None:
        payload = {"title": "CSRF", "mode": "mic", "speakers": None}
        self.expect(self.a.post("/api/cloud/meetings", json=payload), 403, "csrf_failed")
        self.expect(
            self.a.post(
                "/api/cloud/meetings",
                json=payload,
                headers={"X-CSRF-Token": "wrong", "Origin": "https://app.test"},
            ),
            403,
            "csrf_failed",
        )
        self.expect(
            self.a.post(
                "/api/cloud/meetings",
                json=payload,
                headers={**self.csrf_headers(self.a), "Origin": "https://evil.test"},
            ),
            403,
            "csrf_failed",
        )

        xss = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
        meeting = self.expect(
            self.a.post(
                "/api/cloud/meetings",
                json={"title": xss, "mode": "mic", "speakers": None},
                headers=self.csrf_headers(self.a),
            ),
            201,
        ).json()["id"]
        for response in (
            self.a.get("/api/cloud/meetings"),
            self.a.get(f"/api/cloud/meetings/{meeting}/annotations"),
        ):
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.expect(
            self.a.post(
                f"/api/cloud/meetings/{meeting}/annotations",
                json={"kind": "note", "offset_ms": 0, "text": xss},
                headers=self.csrf_headers(self.a),
            ),
            201,
        )

        lease, upload, headers = self.open_upload(self.a, meeting)
        for path in (
            "/api/cloud/uploads/..%2F..%2Fetc%2Fpasswd/ack",
            f"/api/cloud/uploads/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd/chunks/e1/mic/0",
        ):
            response = self.a.get(path, headers=headers)
            self.assertEqual(response.status_code, 404, response.text)
        self.expect(self.put_chunk(self.a, upload, headers), 200)
        key = self.query_value(
            "SELECT object_key FROM chunks WHERE owner_user_id = :o AND upload_session_id = :u",
            o=self.a_user_id,
            u=upload,
        )
        self.assertTrue(key.startswith(f"chunks/{self.a_user_id}/"))
        self.assertNotIn("..", key)

        before = self.query_count("voiceprint_enrollments")
        self.expect(
            self.a.post(
                "/api/cloud/voiceprints",
                data={"name": "主持人", "consent": "明确同意"},
                files={
                    "audio": (
                        "../../evil.wav",
                        WAV_SAMPLE,
                        "audio/wav",
                    )
                },
                headers=self.csrf_headers(self.a),
            ),
            201,
        )
        self.assertEqual(self.query_count("voiceprint_enrollments"), before + 1)

        before = self.query_count("voiceprint_enrollments")
        self.expect(
            self.a.post(
                "/api/cloud/voiceprints",
                data={"name": "伪装图片", "consent": "明确同意"},
                files={
                    "audio": (
                        "spoof.wav",
                        b"\x89PNG\r\n\x1a\n" + b"0" * 32,
                        "audio/webm",
                    )
                },
                headers=self.csrf_headers(self.a),
            ),
            415,
            "unsupported_voiceprint_media",
        )
        self.assertEqual(self.query_count("voiceprint_enrollments"), before)

        position = self.create_position(self.a)

        class SpyProvider:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, *_args: Any, **_kwargs: Any) -> str:
                self.calls += 1
                return "ok"

        spy = SpyProvider()
        app = self.app_client(
            extra_deps={"ai_provider": spy, "ai_ocr_provider": spy},
            raise_server_exceptions=False,
        )
        response = app.post(
            f"/api/cloud/positions/{position}/interview",
            data={"resume_text": "候选人简历"},
            files={"images": ("photo.jpg", b"not-an-image", "image/jpeg")},
            headers=self.csrf_headers(self.a),
        )
        self.expect(response, 415, "unsupported_media_type")
        self.assertEqual(spy.calls, 0)

        self.clock.advance(seconds=121)
        self.expect(
            self.put_chunk(self.a, upload, headers, 2, b"expired", 6000, 9000),
            409,
            "capture_lease_expired",
        )
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease/heartbeat",
                json={"lease_token": lease},
                headers=self.csrf_headers(self.a),
            ),
            409,
            "capture_lease_expired",
        )
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting},
                headers=self.csrf_headers(self.a),
            ),
            201,
        )

        profile = self.insert_profile()
        self.expect(
            self.a.delete(
                f"/api/cloud/voiceprints/{profile}",
                headers=self.csrf_headers(self.a),
            ),
            204,
        )
        self.expect(
            self.a.delete(
                f"/api/cloud/voiceprints/{profile}",
                headers=self.csrf_headers(self.a),
            ),
            404,
            "voiceprint_not_found",
        )
        listed = self.a.get("/api/cloud/voiceprints")
        self.expect(listed, 200)
        self.assertNotIn(profile, {item["id"] for item in listed.json()})

    # -- 8. no default administration surface ----------------------------

    def test_admin_has_no_default_business_browsing_interface(self) -> None:
        for route in self.app.routes:
            path = getattr(route, "path", "")
            self.assertNotIn("/admin", path)
            self.assertNotIn("/users", path)

        for path in (
            "/api/cloud/admin/meetings",
            "/api/cloud/admin/users",
            "/api/cloud/users",
        ):
            self.expect(self.a.get(path), 404, "not_found")
        for path in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(self.a.get(path).status_code, 404)

        a_meeting = self.create_meeting(self.a, title="A only")
        b_meeting = self.create_meeting(self.b, title="B only")
        a_items = self.a.get("/api/cloud/meetings").json()["items"]
        b_items = self.b.get("/api/cloud/meetings").json()["items"]
        self.assertEqual({item["id"] for item in a_items}, {a_meeting})
        self.assertEqual({item["id"] for item in b_items}, {b_meeting})
        filtered = self.a.get(
            "/api/cloud/meetings", params={"owner_user_id": self.b_user_id}
        ).json()["items"]
        self.assertEqual({item["id"] for item in filtered}, {a_meeting})


if __name__ == "__main__":
    unittest.main()
