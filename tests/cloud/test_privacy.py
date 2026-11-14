"""Privacy deletion, replayable tombstones and cleanup (Task 15).

The deletable surface is deliberately narrow: an owner can delete one meeting
(DELETE /api/cloud/meetings/{meeting_id}, CSRF protected, 204). Account
revocation and restore replay are operational functions, not public routes,
and there is no export route.

Every deletion first appends a durable, append-only JSONL tombstone and only
then commits the database change; a commit failure appends a void record and
re-raises. Tests use a real file-backed TombstoneAuditStore because the
in-memory store used by the rest of the suite cannot prove a restarted process
still remembers a deletion.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from cloud.contracts import AuthContext, NotFoundError, TranscriptView
from cloud.objects import chunk_object_key
from cloud.privacy import (
    TombstoneAuditError,
    TombstoneAuditStore,
    delete_meeting,
    replay_tombstones,
    revoke_account,
    run_cleanup,
)
from cloud.schema import Position, VoiceprintEnrollment, VoiceprintProfile
from cloud.tasks import claim_task, enqueue_final, fail_task, publish_result
from tests.cloud.support import BASE_TIME, A_EMAIL, CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"


class PrivacyCase(CloudCase):
    audit_store: Any = None
    ephemeral_root: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._audit_dir = tempfile.TemporaryDirectory(prefix="cloud-audit-")
        cls._ephemeral_root = tempfile.mkdtemp(prefix="cloud-ephemeral-")
        cls.audit_store = TombstoneAuditStore(
            Path(cls._audit_dir.name) / "audit" / "tombstones.jsonl"
        )
        super().setUpClass()
        cls.settings = cls.settings.model_copy(
            update={
                "ephemeral_root": cls._ephemeral_root,
                "privacy_audit_path": str(cls.audit_store.path),
            }
        )
        cls._build_app()
        cls.addClassCleanup(shutil.rmtree, cls._ephemeral_root, True)
        cls.addClassCleanup(cls._audit_dir.cleanup)

    def setUp(self) -> None:
        super().setUp()
        self.audit_store = type(self).audit_store
        self.audit_store.path.unlink(missing_ok=True)
        for prefix in ("chunks/", "derived/"):
            for key in self.object_store.list_keys(prefix):
                self.object_store.delete(key)
        self.ephemeral_root = type(self)._ephemeral_root
        root = Path(self.ephemeral_root)
        for child in root.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)

    # -- helpers ----------------------------------------------------------

    def expect(self, response: Any, status: int = 200, code: str | None = None) -> Any:
        self.assertEqual(
            response.status_code,
            status,
            msg=f"unexpected status {response.status_code}: {response.text}",
        )
        if code is not None:
            payload = response.json()
            self.assertEqual(payload.get("error"), code)
            self.assertRegex(payload.get("request_id", ""), r"^req_[0-9a-f]{32}$")
        return response

    def context(self, user_id: str) -> tuple[Any, AuthContext]:
        db = self.session_factory()
        session_id = self.query_value(
            "SELECT id FROM sessions WHERE user_id = :u ORDER BY created_at DESC LIMIT 1",
            u=user_id,
        )
        ctx = AuthContext(
            user_id=user_id,
            session_id=session_id,
            db=db,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
            audit=self.audit_store,
        )
        return db, ctx

    def delete_http(self, client: Any, meeting_id: str) -> Any:
        return client.delete(
            f"/api/cloud/meetings/{meeting_id}", headers=self.csrf_headers(client)
        )

    def put_chunk(
        self, client: Any, *, meeting_id: str | None = None
    ) -> tuple[str, str, str]:
        """Create one open upload with one durable chunk; returns ids and key."""
        if meeting_id is None:
            meeting_id = self.create_meeting(client)
        lease = self.expect(
            client.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting_id},
                headers=self.csrf_headers(client),
            ),
            201,
        ).json()["lease_token"]
        headers = {**self.csrf_headers(client), "X-Capture-Lease": lease}
        upload_id = self.expect(
            client.post(
                "/api/cloud/uploads",
                json={"meeting_id": meeting_id},
                headers=headers,
            ),
            201,
        ).json()["id"]
        self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/epochs",
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
        body = b"privacy-chunk"
        key = chunk_object_key(self.a_user_id, upload_id, "e1", "mic", 0)
        self.expect(
            client.put(
                f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/0",
                params={
                    "start_ms": 0,
                    "end_ms": 3000,
                    "checksum": hashlib.sha256(body).hexdigest(),
                    "codec": CODEC,
                },
                content=body,
                headers=headers,
            )
        )
        # One live capture lease per owner: give it back so later captures work.
        self.expect(
            client.post(
                "/api/cloud/capture-lease/release",
                json={"lease_token": lease},
                headers=self.csrf_headers(client),
            ),
            204,
        )
        return meeting_id, upload_id, key

    def sealed_upload(
        self, client: Any, *, meeting_id: str | None = None, chunks: int = 1
    ) -> tuple[str, str, int]:
        if meeting_id is None:
            meeting_id = self.create_meeting(client)
        lease = self.expect(
            client.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting_id},
                headers=self.csrf_headers(client),
            ),
            201,
        ).json()["lease_token"]
        headers = {**self.csrf_headers(client), "X-Capture-Lease": lease}
        upload_id = self.expect(
            client.post(
                "/api/cloud/uploads",
                json={"meeting_id": meeting_id},
                headers=headers,
            ),
            201,
        ).json()["id"]
        self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/epochs",
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
        for sequence in range(chunks):
            body = f"privacy-sealed-{sequence}".encode()
            self.expect(
                client.put(
                    f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/{sequence}",
                    params={
                        "start_ms": sequence * 3000,
                        "end_ms": (sequence + 1) * 3000,
                        "checksum": hashlib.sha256(body).hexdigest(),
                        "codec": CODEC,
                    },
                    content=body,
                    headers=headers,
                )
            )
        sealed = self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/seal",
                json={
                    "tracks": [
                        {
                            "capture_epoch_id": "e1",
                            "track_id": "mic",
                            "last_sequence": chunks - 1,
                            "end_ms": chunks * 3000,
                        }
                    ],
                    "accept_partial": False,
                },
                headers=self.csrf_headers(client),
            )
        )
        return meeting_id, upload_id, int(sealed.json()["input_version"])

    def transcript(self, meeting_id: str) -> TranscriptView:
        return TranscriptView(
            id=str(uuid4()),
            meeting_id=meeting_id,
            segments=[],
            speakers=[],
            warnings=[],
            empty=True,
        )

    def claim(self) -> Any:
        db = self.session_factory()
        try:
            return claim_task(
                "privacy-worker",
                self.clock.now(),
                db=db,
                settings=self.settings,
            )
        finally:
            db.close()

    def insert_active_lease(self, meeting_id: str, token: str = "lease-after-delete") -> str:
        now = self.clock.now()
        lease_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO capture_leases (id, owner_user_id, meeting_id,"
                    " token_hash, acquired_at, heartbeat_at, expires_at)"
                    " VALUES (:id, :owner, :meeting, :hash, :now, :now, :expires)"
                ),
                {
                    "id": lease_id,
                    "owner": self.a_user_id,
                    "meeting": meeting_id,
                    "hash": hashlib.sha256(token.encode()).hexdigest(),
                    "now": now,
                    "expires": now + timedelta(seconds=120),
                },
            )
        return lease_id

    def insert_running_task(
        self,
        meeting_id: str,
        upload_id: str,
        token: str = "task-after-delete",
    ) -> str:
        now = self.clock.now()
        task_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO processing_tasks (id, kind, owner_user_id, meeting_id,"
                    " upload_session_id, input_version, generation, attempt, status,"
                    " lease_token_hash, lease_worker_id, lease_deadline,"
                    " lease_acquired_at, created_at, updated_at)"
                    " VALUES (:id, 'final', :owner, :meeting, :upload, 0, 0, 1,"
                    " 'running', :hash, 'w-after-delete', :deadline, :now, :now, :now)"
                ),
                {
                    "id": task_id,
                    "owner": self.a_user_id,
                    "meeting": meeting_id,
                    "upload": upload_id,
                    "hash": hashlib.sha256(token.encode()).hexdigest(),
                    "deadline": now + timedelta(seconds=300),
                    "now": now,
                },
            )
        return task_id

    def insert_private_rows(self) -> tuple[str, str]:
        now = self.clock.now()
        profile_id = str(uuid4())
        with self.session_factory() as db:
            db.add(
                VoiceprintProfile(
                    owner_user_id=self.a_user_id,
                    id=profile_id,
                    name="me",
                    version=1,
                    embedding_ciphertext=b"ciphertext",
                    model_id="test-model",
                    model_version="1",
                    weights_sha256="0" * 64,
                    dimension=192,
                    sample_rate=16000,
                    preprocess_version="1",
                    normalization_version="1",
                    calibration_version="1",
                    created_at=now,
                )
            )
            db.add(
                VoiceprintEnrollment(
                    owner_user_id=self.a_user_id,
                    id=str(uuid4()),
                    name="me",
                    version=1,
                    consent="explicit",
                    sample_sha256="0" * 64,
                    sample_bytes=8,
                    content_type="audio/wav",
                    sample_ciphertext=b"sample",
                    profile_id=profile_id,
                    created_at=now,
                    captured_at=now,
                )
            )
            db.add(
                Position(
                    owner_user_id=self.a_user_id,
                    id=str(uuid4()),
                    type="后端",
                    title="Role",
                    requirements="Requirements",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()
        return profile_id, ""

    # -- required cases ---------------------------------------------------

    def test_delete_meeting_204_requires_csrf_and_scopes_to_owner(self) -> None:
        meeting_id = self.create_meeting(self.a)

        self.expect(
            self.a.delete(f"/api/cloud/meetings/{meeting_id}"), 403, "csrf_failed"
        )
        self.expect(self.a.get(f"/api/cloud/meetings/{meeting_id}"))
        self.expect(self.delete_http(self.b, meeting_id), 404, "meeting_not_found")
        self.expect(self.a.get(f"/api/cloud/meetings/{meeting_id}"))

        response = self.expect(self.delete_http(self.a, meeting_id), 204)
        self.assertEqual(response.content, b"")
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}"), 404, "not_found"
        )
        self.expect(
            self.a.patch(
                f"/api/cloud/meetings/{meeting_id}",
                json={"title": "renamed"},
                headers=self.csrf_headers(self.a),
            ),
            404,
            "not_found",
        )
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}/events"),
            404,
            "meeting_not_found",
        )
        listed = self.expect(self.a.get("/api/cloud/meetings")).json()
        self.assertEqual(listed["items"], [])

        records = self.audit_store.read_all()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["kind"], "meeting")
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["owner_user_id"], self.a_user_id)
        self.assertEqual(record["meeting_id"], meeting_id)
        self.assertEqual(record["generation"], 1)
        self.assertEqual(
            record["object_prefixes"],
            [f"derived/{self.a_user_id}/{meeting_id}/"],
        )
        self.assertEqual(record["upload_ids"], [])
        self.assertTrue(record["occurred_at"].endswith("Z"))

    def test_delete_fences_inflight_publish(self) -> None:
        meeting_id, upload_id, version = self.sealed_upload(self.a)
        enqueued = self.expect(
            self.a.post(
                f"/api/cloud/meetings/{meeting_id}/tasks/final",
                json={"input_version": version, "force": False},
                headers=self.csrf_headers(self.a),
            ),
            200,
        ).json()
        claimed = self.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.task.id, enqueued["id"])

        self.expect(self.delete_http(self.a, meeting_id), 204)

        db = self.session_factory()
        try:
            published = publish_result(
                claimed.task.id,
                claimed.lease_token,
                self.transcript(meeting_id),
                now=self.clock.now(),
                db=db,
            )
        finally:
            db.close()
        self.assertFalse(published)
        self.assertEqual(self.query_count("transcript_versions"), 0)
        row = self.query_value(
            "SELECT status || '|' || coalesce(error_code, '') || '|' || generation"
            " FROM processing_tasks WHERE id = :id",
            id=claimed.task.id,
        )
        self.assertEqual(row, "cancelled|meeting_deleted|0")
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM capture_leases WHERE released_at IS NULL"
            ),
            0,
        )
        self.expect(
            self.a.get(f"/api/cloud/uploads/{upload_id}/ack"),
            404,
            "upload_not_found",
        )
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}/events"),
            404,
            "meeting_not_found",
        )

    def test_deleted_meeting_rejects_lease_and_task_writes(self) -> None:
        meeting_id, upload_id, _ = self.put_chunk(self.a)
        self.expect(self.delete_http(self.a, meeting_id), 204)

        lease_token = "lease-after-delete"
        lease_id = self.insert_active_lease(meeting_id, lease_token)
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease/heartbeat",
                json={"lease_token": lease_token},
                headers=self.csrf_headers(self.a),
            ),
            404,
            "meeting_not_found",
        )
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease/release",
                json={"lease_token": lease_token},
                headers=self.csrf_headers(self.a),
            ),
            404,
            "meeting_not_found",
        )
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting_id},
                headers=self.csrf_headers(self.a),
            ),
            404,
            "meeting_not_found",
        )
        self.assertIsNone(
            self.query_value(
                "SELECT released_at FROM capture_leases WHERE id = :id", id=lease_id
            )
        )

        task_token = "task-after-delete"
        task_id = self.insert_running_task(meeting_id, upload_id, task_token)
        db = self.session_factory()
        try:
            progressed = fail_task(
                task_id,
                task_token,
                transient=False,
                error_code="test_error",
                now=self.clock.now(),
                db=db,
                settings=self.settings,
            )
        finally:
            db.close()
        self.assertFalse(progressed)
        row = self.query_value(
            "SELECT status || '|' || coalesce(error_code, '') FROM processing_tasks"
            " WHERE id = :id",
            id=task_id,
        )
        self.assertEqual(row, "running|")

        self.expect(
            self.a.post(
                f"/api/cloud/tasks/{task_id}/cancel",
                headers=self.csrf_headers(self.a),
            ),
            404,
            "task_not_found",
        )
        self.assertEqual(
            self.query_value(
                "SELECT status FROM processing_tasks WHERE id = :id", id=task_id
            ),
            "running",
        )
        db, ctx = self.context(self.a_user_id)
        try:
            from cloud.tasks import cancel_task

            with self.assertRaises(NotFoundError) as caught:
                cancel_task(ctx, task_id)
            self.assertEqual(caught.exception.code, "task_not_found")
        finally:
            db.close()

    def test_account_revocation_erases_identity_and_private_data(self) -> None:
        meeting_id, upload_id, key = self.put_chunk(self.a)
        self.assertTrue(self.object_store.exists(key))
        profile_id, _ = self.insert_private_rows()
        sealed_meeting, _, version = self.sealed_upload(self.a)
        enqueued = self.expect(
            self.a.post(
                f"/api/cloud/meetings/{sealed_meeting}/tasks/final",
                json={"input_version": version, "force": False},
                headers=self.csrf_headers(self.a),
            ),
            200,
        ).json()

        db, ctx = self.context(self.a_user_id)
        try:
            revoke_account(ctx)
        finally:
            db.close()

        self.assertIsNotNone(
            self.query_value(
                "SELECT deleted_at FROM users WHERE id = :id", id=self.a_user_id
            )
        )
        self.expect(self.a.get("/api/cloud/auth/session"), 401)
        fresh = TestClient(self.app, follow_redirects=True)
        try:
            response = fresh.get("/api/cloud/auth/login", params={"email": A_EMAIL})
            self.assertNotEqual(response.status_code, 200, response.text)
            self.assertNotIn("csrf_token", response.text)
        finally:
            fresh.close()

        self.assertFalse(self.object_store.exists(key))
        self.assertEqual(self.query_count("chunks"), 2)  # rows remain until purge
        self.assertEqual(
            self.query_value(
                "SELECT revocation_reason FROM voiceprint_profiles WHERE id = :id",
                id=profile_id,
            ),
            "account_deleted",
        )
        self.assertIsNone(
            self.query_value("SELECT sample_ciphertext FROM voiceprint_enrollments LIMIT 1")
        )
        self.assertEqual(self.query_count("positions"), 0)
        self.assertEqual(
            self.query_value(
                "SELECT deleted_at IS NOT NULL FROM meetings WHERE id = :id",
                id=meeting_id,
            ),
            True,
        )
        self.assertEqual(
            self.query_value(
                "SELECT generation FROM meetings WHERE id = :id", id=meeting_id
            ),
            1,
        )
        self.assertEqual(
            self.query_value(
                "SELECT status FROM processing_tasks WHERE id = :id",
                id=enqueued["id"],
            ),
            "cancelled",
        )
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM sessions WHERE user_id = :u AND revoked_at IS NULL",
                u=self.a_user_id,
            ),
            0,
        )
        records = self.audit_store.read_all()
        self.assertEqual([r["kind"] for r in records], ["account"])
        self.assertEqual(
            records[0]["object_prefixes"],
            [f"chunks/{self.a_user_id}/", f"derived/{self.a_user_id}/"],
        )
        self.assertEqual(records[0]["profile_ids"], [profile_id])

    def test_restore_does_not_resurrect(self) -> None:
        meeting_id, upload_id, key = self.put_chunk(self.a)
        body = b"privacy-chunk"
        self.assertTrue(self.object_store.exists(key))
        self.expect(self.delete_http(self.a, meeting_id), 204)
        self.assertFalse(self.object_store.exists(key))

        # Simulate a database+object restore that predates the deletion.
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE meetings SET deleted_at = NULL WHERE id = :id"),
                {"id": meeting_id},
            )
        self.object_store.put_verified(key, body, hashlib.sha256(body).hexdigest())
        self.expect(self.a.get(f"/api/cloud/meetings/{meeting_id}"))

        self.assertEqual(replay_tombstones("backup-2026-11-14"), 1)
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}"), 404, "not_found"
        )
        self.expect(
            self.a.get(f"/api/cloud/uploads/{upload_id}/ack"),
            404,
            "upload_not_found",
        )
        self.assertFalse(self.object_store.exists(key))
        self.assertEqual(
            self.query_value(
                "SELECT generation FROM meetings WHERE id = :id", id=meeting_id
            ),
            1,
        )

        # Replaying the same backup twice is safe: same state, no resurrection.
        self.assertEqual(replay_tombstones("backup-2026-11-14"), 1)
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}"), 404, "not_found"
        )
        self.assertFalse(self.object_store.exists(key))
        receipts = [r for r in self.audit_store.read_all() if r["kind"] == "replay"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["backup_id"], "backup-2026-11-14")
        self.assertEqual(receipts[0]["replayed_count"], 1)

    def test_voided_tombstone_is_not_replayed(self) -> None:
        meeting_id = self.create_meeting(self.a)
        now = self.clock.now()
        record = {
            "schema_version": 1,
            "kind": "meeting",
            "tombstone_id": str(uuid4()),
            "owner_user_id": self.a_user_id,
            "meeting_id": meeting_id,
            "occurred_at": now.isoformat().replace("+00:00", "Z"),
            "generation": 7,
            "object_prefixes": [],
            "upload_ids": [],
            "profile_ids": [],
            "position_ids": [],
        }
        self.audit_store.append(record)
        void = {
            "schema_version": 1,
            "kind": "void",
            "tombstone_id": str(uuid4()),
            "owner_user_id": self.a_user_id,
            "meeting_id": meeting_id,
            "occurred_at": now.isoformat().replace("+00:00", "Z"),
            "generation": 0,
            "object_prefixes": [],
            "upload_ids": [],
            "profile_ids": [],
            "position_ids": [],
            "voids": record["tombstone_id"],
        }
        self.audit_store.append(void)

        self.assertEqual(replay_tombstones("backup-voided"), 0)
        self.expect(self.a.get(f"/api/cloud/meetings/{meeting_id}"))
        self.assertIsNone(
            self.query_value(
                "SELECT deleted_at FROM meetings WHERE id = :id", id=meeting_id
            )
        )

    def test_voided_tombstone_does_not_delete_live_objects(self) -> None:
        meeting_id, upload_id, key = self.put_chunk(self.a)
        now = self.clock.now()

        def meeting_record(
            meeting: str, upload: str, prefix: str
        ) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "kind": "meeting",
                "tombstone_id": str(uuid4()),
                "owner_user_id": self.a_user_id,
                "meeting_id": meeting,
                "occurred_at": now.isoformat().replace("+00:00", "Z"),
                "generation": 1,
                "object_prefixes": [prefix],
                "upload_ids": [upload],
                "profile_ids": [],
                "position_ids": [],
            }

        # A commit failure appends a void; its object prefix is still live data.
        voided = meeting_record(
            meeting_id, upload_id, f"chunks/{self.a_user_id}/{upload_id}/"
        )
        self.audit_store.append(voided)
        self.audit_store.append(
            {
                "schema_version": 1,
                "kind": "void",
                "tombstone_id": str(uuid4()),
                "owner_user_id": self.a_user_id,
                "meeting_id": meeting_id,
                "occurred_at": now.isoformat().replace("+00:00", "Z"),
                "generation": 0,
                "object_prefixes": [],
                "upload_ids": [],
                "profile_ids": [],
                "position_ids": [],
                "voids": voided["tombstone_id"],
            }
        )

        counts = run_cleanup(now)
        self.assertEqual(counts["tombstone_objects_deleted"], 0)
        self.assertTrue(self.object_store.exists(key))
        self.assertEqual(self.query_count("chunks"), 1)

        # A genuinely committed tombstone still retries and deletes its prefix.
        other_meeting, other_upload, other_key = self.put_chunk(self.a)
        self.audit_store.append(
            meeting_record(
                other_meeting,
                other_upload,
                f"chunks/{self.a_user_id}/{other_upload}/",
            )
        )
        counts = run_cleanup(now)
        self.assertEqual(counts["tombstone_objects_deleted"], 1)
        self.assertFalse(self.object_store.exists(other_key))
        self.assertTrue(self.object_store.exists(key))
        self.assertEqual(self.query_count("chunks"), 2)

    def test_audit_store_is_durable_and_rejects_tampering(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.expect(self.delete_http(self.a, meeting_id), 204)

        reopened = TombstoneAuditStore(self.audit_store.path)
        records = reopened.read_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["meeting_id"], meeting_id)

        link = Path(self.ephemeral_root) / "audit-link"
        target = Path(self.ephemeral_root) / "outside.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        link.symlink_to(target)
        with self.assertRaises(TombstoneAuditError):
            TombstoneAuditStore(link).append(records[0])
        with self.assertRaises(TombstoneAuditError):
            TombstoneAuditStore(link).read_all()

        corrupt = Path(self.ephemeral_root) / "corrupt.jsonl"
        corrupt.write_text("not-json\n", encoding="utf-8")
        with self.assertRaises(TombstoneAuditError):
            TombstoneAuditStore(corrupt).read_all()

    def test_cleanup_retries_tombstones_and_purges_aged_rows(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.expect(self.delete_http(self.a, meeting_id), 204)

        b_meeting = self.create_meeting(self.b)
        db, ctx = self.context(self.b_user_id)
        try:
            revoke_account(ctx)
        finally:
            db.close()

        now = BASE_TIME + timedelta(
            seconds=max(
                self.settings.cleanup_target_s,
                self.settings.account_revocation_wait_s,
            )
            + 1
        )
        counts = run_cleanup(now)
        for key in (
            "tombstone_objects_deleted",
            "orphan_objects_deleted",
            "meetings_purged",
            "users_purged",
            "ephemeral_removed",
            "errors",
        ):
            self.assertIn(key, counts)
        self.assertEqual(counts["meetings_purged"], 2)
        self.assertEqual(counts["users_purged"], 1)
        self.assertEqual(self.query_count("meetings"), 0)
        self.assertIsNone(
            self.query_value("SELECT id FROM users WHERE id = :id", id=self.b_user_id)
        )
        self.assertIsNotNone(
            self.query_value("SELECT id FROM users WHERE id = :id", id=self.a_user_id)
        )

        again = run_cleanup(now)
        self.assertEqual(again["meetings_purged"], 0)
        self.assertEqual(again["users_purged"], 0)
        self.assertEqual(again["orphan_objects_deleted"], 0)
        self.assertEqual(again["errors"], 0)

    def test_ack_reference_not_orphan(self) -> None:
        meeting_id, upload_id, key = self.put_chunk(self.a)
        self.expect(self.a.get(f"/api/cloud/uploads/{upload_id}/ack"))
        self.assertTrue(self.object_store.exists(key))
        counts = run_cleanup(BASE_TIME + timedelta(days=365))
        self.assertEqual(counts["orphan_objects_deleted"], 0)
        self.assertEqual(self.query_count("chunks"), 1)
        self.assertTrue(self.object_store.exists(key))
        self.assertEqual(
            self.query_value("SELECT object_key FROM chunks LIMIT 1"), key
        )

    def test_cancelled_ephemeral_cleanup(self) -> None:
        root = Path(self.ephemeral_root)
        old_dir = root / "old" / "nested"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "capture.wav"
        old_file.write_bytes(b"old")
        fresh = root / "fresh.wav"
        fresh.write_bytes(b"fresh")
        outside = root.parent / f"outside-{uuid4().hex}.txt"
        outside.write_text("keep", encoding="utf-8")
        self.addCleanup(outside.unlink, True)
        link = root / "old-link"
        link.symlink_to(outside)
        old_ts = (BASE_TIME - timedelta(hours=2)).timestamp()
        os.utime(old_file, (old_ts, old_ts))
        os.utime(old_dir, (old_ts, old_ts))
        os.utime(link, (old_ts, old_ts), follow_symlinks=False)

        counts = run_cleanup(BASE_TIME)
        self.assertGreaterEqual(counts["ephemeral_removed"], 2)
        self.assertFalse(old_file.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(link.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(root.exists())

    def test_privacy_maintenance_blocks_only_cloud_api(self) -> None:
        settings = self.settings.model_copy(update={"privacy_maintenance": True})
        client = self.app_client(settings=settings)
        blocked = client.get("/api/cloud/auth/session")
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.json()["error"], "maintenance")
        self.assertRegex(blocked.json()["request_id"], r"^req_[0-9a-f]{32}$")
        self.assertEqual(blocked.headers.get("cache-control"), "no-store")
        self.assertEqual(client.get("/").status_code, 404)
        self.assertEqual(client.get("/api/cloudish").status_code, 404)
        self.assertEqual(self.a.get("/api/cloud/auth/session").status_code, 200)

    def test_no_public_account_deletion_or_export_route(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.assertEqual(
            self.a.delete(
                "/api/cloud/account", headers=self.csrf_headers(self.a)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.a.get(f"/api/cloud/meetings/{meeting_id}/export").status_code,
            404,
        )
        paths = [getattr(route, "path", "") for route in self.app.routes]
        self.assertFalse(any("export" in path for path in paths))
        self.assertNotIn("/api/cloud/account", paths)


if __name__ == "__main__":
    unittest.main()
