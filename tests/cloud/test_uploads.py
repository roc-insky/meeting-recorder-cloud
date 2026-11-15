"""Task 5: durable private audio chunks and capture leases.

Every test runs against the real PostgreSQL database and the per-class local
object-store root (no fakes for storage). The suite pins the contract that
matters for data safety:

* write-before-ACK — a chunk row only exists after the object is stored, so an
  ACK never refers to bytes that cannot be recovered;
* a lost ACK plus retry with the same chunk identity is idempotent, while a
  different hash under the same identity is a conflict;
* object-store failure leaves zero chunk rows; a database failure after the
  object landed leaves an unreferenced object that the retry reuses and the
  orphan scan can find;
* owner isolation, one active capture per user, epoch/session recovery rules
  and the 24h recovery window;
* ACKs survive an API restart and the object is not anonymously readable.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from cloud.app import create_app
from cloud.contracts import PayloadTooLargeError
from cloud.leases import CAPTURE_LEASE_S
from cloud.objects import (
    CHUNK_PREFIX,
    LocalObjectStore,
    ObjectStoreError,
    chunk_object_key,
)
from cloud.uploads import read_bounded_body, scan_orphans
from tests.cloud.support import A_EMAIL, CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
SYSTEM = {"id": "system", "source": "system", "codec": CODEC, "sample_rate": 48000}
KEY_SHAPE = re.compile(r"^chunks/[0-9a-f-]{36}/[0-9a-f-]{36}/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/\d{10}\.bin$")


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class FailingObjectStore:
    """Object store whose writes always fail, counting attempts."""

    def __init__(self) -> None:
        self.attempts = 0

    def put_verified(self, key: str, body: bytes, sha256: str) -> None:
        self.attempts += 1
        raise ObjectStoreError("simulated object store failure")

    def get_bytes(self, key: str) -> bytes:
        raise ObjectStoreError("unavailable")

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        raise ObjectStoreError("unavailable")

    def list_keys(self, prefix: str) -> list[str]:
        return []

    def list_objects(self, prefix: str) -> list[tuple[str, object]]:
        return []

    def clear_prefix(self, prefix: str) -> int:
        return 0


class _FlakySession(Session):
    """Session that fails the next commit once, simulating a DB outage."""

    fail_next_commit = False

    def commit(self) -> None:
        if type(self).fail_next_commit:
            type(self).fail_next_commit = False
            raise OperationalError(
                "simulated commit failure", {}, Exception("commit failed")
            )
        super().commit()


class UploadsCase(CloudCase):
    """Chunk ACK, idempotency, ownership and capture-lease behaviour."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Start from an empty prefix in the shared local object store root.
        cls.object_store.clear_prefix(CHUNK_PREFIX)

    def setUp(self) -> None:
        super().setUp()
        self.object_store.clear_prefix(CHUNK_PREFIX)

    # -- helpers ---------------------------------------------------------

    def expect(self, response, status: int = 200, code: str | None = None):
        if response.status_code != status:
            raise AssertionError(
                f"{response.request.method} {response.request.url} expected "
                f"{status}, got {response.status_code}: {response.text}"
            )
        if code is not None:
            payload = response.json()
            if payload.get("error") != code:
                raise AssertionError(f"expected error {code!r}, got {payload!r}")
        return response

    def lease(self, client: TestClient, meeting_id: str) -> str:
        response = client.post(
            "/api/cloud/capture-lease",
            json={"meeting_id": meeting_id},
            headers=self.csrf_headers(client),
        )
        self.expect(response, 201)
        return response.json()["lease_token"]

    def open_upload(self, client: TestClient, meeting_id: str, token: str | None = None) -> str:
        headers = self.csrf_headers(client)
        if token is not None:
            headers["X-Capture-Lease"] = token
        response = client.post(
            "/api/cloud/uploads",
            json={"meeting_id": meeting_id},
            headers=headers,
        )
        self.expect(response, 201)
        return response.json()["id"]

    def epoch_payload(
        self,
        epoch_id: str = "e1",
        *,
        tracks: list[dict] | None = None,
        offset_ms: int = 0,
        uncertainty_ms: int = 0,
        started_at: str = "2026-11-14T07:00:00Z",
    ) -> dict:
        return {
            "id": epoch_id,
            "offset_ms": offset_ms,
            "uncertainty_ms": uncertainty_ms,
            "started_at": started_at,
            "tracks": [dict(MIC)] if tracks is None else tracks,
        }

    def register_epoch(
        self,
        client: TestClient,
        upload_id: str,
        token: str | None = None,
        epoch_id: str = "e1",
        **payload_kwargs,
    ):
        headers = self.csrf_headers(client)
        if token is not None:
            headers["X-Capture-Lease"] = token
        return client.post(
            f"/api/cloud/uploads/{upload_id}/epochs",
            json=self.epoch_payload(epoch_id, **payload_kwargs),
            headers=headers,
        )

    def put_chunk(
        self,
        client: TestClient,
        upload_id: str,
        epoch_id: str,
        track_id: str,
        sequence: int,
        body: bytes,
        *,
        start_ms: int,
        end_ms: int,
        token: str | None = None,
        codec: str = CODEC,
        checksum: str | None = None,
    ):
        headers = self.csrf_headers(client)
        if token is not None:
            headers["X-Capture-Lease"] = token
        return client.put(
            f"/api/cloud/uploads/{upload_id}/chunks/{epoch_id}/{track_id}/{sequence}",
            params={
                "start_ms": start_ms,
                "end_ms": end_ms,
                "checksum": sha(body) if checksum is None else checksum,
                "codec": codec,
            },
            content=body,
            headers=headers,
        )

    def ack(self, client: TestClient, upload_id: str):
        return client.get(f"/api/cloud/uploads/{upload_id}/ack")

    def object_keys(self) -> list[str]:
        return self.object_store.list_keys(CHUNK_PREFIX)

    def recorded_upload(self, *, tracks: list[dict] | None = None):
        """Meeting + live lease + upload session + registered epoch e1."""
        meeting_id = self.create_meeting(self.a)
        token = self.lease(self.a, meeting_id)
        upload_id = self.open_upload(self.a, meeting_id, token)
        self.expect(self.register_epoch(self.a, upload_id, token, tracks=tracks), 200)
        return meeting_id, upload_id, token

    # -- ACK idempotency -------------------------------------------------

    def test_ack_loss_retry_is_idempotent(self) -> None:
        _, upload_id, token = self.recorded_upload()
        body = b"\x1aE\xdf\xa3" + b"opus-frame" * 64

        first = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=3000, token=token,
            )
        )
        self.assertEqual(
            first.json(),
            {"accepted": True, "duplicate": False, "contiguous_ms": 3000, "missing": []},
        )
        self.assertEqual(self.query_count("chunks"), 1)

        # The ACK was lost on the wire: the recorder retries the same identity.
        second = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=3000, token=token,
            )
        )
        self.assertEqual(
            second.json(),
            {"accepted": True, "duplicate": True, "contiguous_ms": 3000, "missing": []},
        )
        self.assertEqual(self.query_count("chunks"), 1)
        self.assertEqual(len(self.object_keys()), 1)

        # After a lost ACK the canonical answer is what GET ack returns.
        ack = self.expect(self.ack(self.a, upload_id))
        self.assertEqual(ack.json()["accepted"], True)
        self.assertEqual(ack.json()["contiguous_ms"], 3000)

    def test_hash_conflict(self) -> None:
        _, upload_id, token = self.recorded_upload()
        first_body = b"first-take"
        second_body = b"second-take"

        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, first_body,
                start_ms=0, end_ms=1000, token=token,
            )
        )
        conflict = self.put_chunk(
            self.a, upload_id, "e1", "mic", 0, second_body,
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(conflict, 409, "chunk_conflict")
        self.assertEqual(self.query_count("chunks"), 1)

        key = self.query_value("SELECT object_key FROM chunks")
        self.assertEqual(self.object_store.get_bytes(key), first_body)

        # Claiming the accepted checksum while sending different bytes is a
        # body-integrity failure, not a conflict, and stores nothing.
        forged = self.put_chunk(
            self.a, upload_id, "e1", "mic", 0, second_body,
            start_ms=0, end_ms=1000, token=token, checksum=sha(first_body),
        )
        self.expect(forged, 400, "checksum_mismatch")
        self.assertEqual(self.query_count("chunks"), 1)
        self.assertEqual(self.object_store.get_bytes(key), first_body)

    # -- failure ordering ------------------------------------------------

    def test_object_failure_never_acks(self) -> None:
        _, upload_id, token = self.recorded_upload()
        failing = FailingObjectStore()
        client = self.app_client(object_store=failing, raise_server_exceptions=False)

        response = self.put_chunk(
            client, upload_id, "e1", "mic", 0, b"frames",
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(response, 500, "object_store_failure")
        self.assertEqual(failing.attempts, 1)
        self.assertEqual(self.query_count("chunks"), 0)
        self.assertEqual(self.object_keys(), [])

    def test_real_object_store_write_failure_rolls_back(self) -> None:
        _, upload_id, token = self.recorded_upload()
        # A real store on its own root, made unusable by removing the root after
        # construction: the write must fail before any chunk row is committed.
        root = Path(tempfile.mkdtemp(prefix="uploads-fault-"))
        self.addCleanup(shutil.rmtree, root, True)
        store = LocalObjectStore(root, min_free_bytes=0)
        client = self.app_client(object_store=store, raise_server_exceptions=False)
        shutil.rmtree(root)
        response = self.put_chunk(
            client, upload_id, "e1", "mic", 0, b"frames",
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(response, 500, "object_store_failure")
        self.assertEqual(self.query_count("chunks"), 0)

    def test_db_commit_failure_then_retry(self) -> None:
        _, upload_id, token = self.recorded_upload()
        body = b"commit-failure-body"
        flaky_factory = sessionmaker(
            bind=self.engine,
            class_=_FlakySession,
            autoflush=False,
            expire_on_commit=False,
        )
        client = self.app_client(
            session_factory=flaky_factory, raise_server_exceptions=False
        )
        _FlakySession.fail_next_commit = True
        try:
            response = self.put_chunk(
                client, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=1000, token=token,
            )
        finally:
            _FlakySession.fail_next_commit = False

        self.expect(response, 500, "commit_failed")
        self.assertEqual(self.query_count("chunks"), 0)
        keys = self.object_keys()
        self.assertEqual(len(keys), 1, "object must stay after the DB failure")

        # The orphan scan sees the object no row references yet...
        with self.session_factory() as db:
            self.assertEqual(scan_orphans(db, self.object_store), keys)

        # ...and retrying the same idempotency key associates those bytes.
        retry = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=1000, token=token,
            )
        )
        self.assertEqual(retry.json()["duplicate"], False)
        self.assertEqual(retry.json()["contiguous_ms"], 1000)
        self.assertEqual(self.query_count("chunks"), 1)
        self.assertEqual(self.object_keys(), keys, "retry reuses the stored object")

        with self.session_factory() as db:
            self.assertEqual(scan_orphans(db, self.object_store), [])

    # -- ownership and capture lease -------------------------------------

    def test_owner_and_one_capture(self) -> None:
        mid_a = self.create_meeting(self.a)
        mid_b = self.create_meeting(self.b)
        token_a = self.lease(self.a, mid_a)
        token_b = self.lease(self.b, mid_b)  # independent user, independent lease
        self.assertNotEqual(token_a, token_b)

        # One active capture per user: a second lease conflicts.
        mid_a2 = self.create_meeting(self.a)
        second = self.a.post(
            "/api/cloud/capture-lease",
            json={"meeting_id": mid_a2},
            headers=self.csrf_headers(self.a),
        )
        self.expect(second, 409, "capture_in_progress")

        # B cannot start a capture on A's meeting (404: no existence leak).
        foreign = self.b.post(
            "/api/cloud/capture-lease",
            json={"meeting_id": mid_a},
            headers=self.csrf_headers(self.b),
        )
        self.expect(foreign, 404)

        upload_a = self.open_upload(self.a, mid_a, token_a)
        upload_b = self.open_upload(self.b, mid_b, token_b)
        self.expect(self.register_epoch(self.a, upload_a, token_a), 200)
        self.expect(self.register_epoch(self.b, upload_b, token_b), 200)

        # A lease is bound to its meeting: it cannot open epochs on another one.
        upload_a2 = self.open_upload(self.a, mid_a2)
        self.expect(
            self.register_epoch(self.a, upload_a2, token_a),
            409,
            "capture_lease_mismatch",
        )

        # B cannot attach a new upload session to A's meeting...
        foreign_upload = self.b.post(
            "/api/cloud/uploads",
            json={"meeting_id": mid_a},
            headers=self.csrf_headers(self.b),
        )
        self.expect(foreign_upload, 404)

        # ...write into A's upload (even while presenting A's lease token)...
        write = self.put_chunk(
            self.b, upload_a, "e1", "mic", 0, b"stolen",
            start_ms=0, end_ms=1000, token=token_a,
        )
        self.expect(write, 404)
        self.assertEqual(self.query_count("chunks"), 0)

        # ...or read A's acknowledged position.
        self.expect(self.ack(self.b, upload_a), 404)
        self.expect(self.ack(self.a, upload_b), 404)

        # Releasing makes the user capturable again and never deletes chunks.
        self.expect(
            self.put_chunk(
                self.a, upload_a, "e1", "mic", 0, b"kept",
                start_ms=0, end_ms=1000, token=token_a,
            )
        )
        release = self.a.post(
            "/api/cloud/capture-lease/release",
            json={"lease_token": token_a},
            headers=self.csrf_headers(self.a),
        )
        self.expect(release, 204)
        token_a2 = self.lease(self.a, mid_a)
        self.assertNotEqual(token_a, token_a2)
        self.assertEqual(self.query_count("chunks"), 1)
        ack = self.expect(self.ack(self.a, upload_a))
        self.assertEqual(ack.json()["contiguous_ms"], 1000)

    # -- validation ------------------------------------------------------

    def test_unregistered_track_and_codec_rejected(self) -> None:
        _, upload_id, token = self.recorded_upload()

        unknown_track = self.put_chunk(
            self.a, upload_id, "e1", "aux", 0, b"frames",
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(unknown_track, 409, "unregistered_track")

        bad_codec = self.put_chunk(
            self.a, upload_id, "e1", "mic", 0, b"frames",
            start_ms=0, end_ms=1000, token=token, codec="audio/wav",
        )
        self.expect(bad_codec, 400, "unsupported_codec")

        unknown_epoch = self.put_chunk(
            self.a, upload_id, "e9", "mic", 0, b"frames",
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(unknown_epoch, 409, "unknown_epoch")

        empty = self.put_chunk(
            self.a, upload_id, "e1", "mic", 0, b"",
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(empty, 400, "empty_body")

        self.assertEqual(self.query_count("chunks"), 0)
        self.assertEqual(self.object_keys(), [])

    def test_negative_or_overlapping_times_rejected(self) -> None:
        _, upload_id, token = self.recorded_upload()

        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"a",
                start_ms=-1, end_ms=1000, token=token,
            ),
            422,
        )
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"a",
                start_ms=2000, end_ms=1000, token=token,
            ),
            422,
        )
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"a",
                start_ms=0, end_ms=3000, token=token,
            )
        )
        overlap = self.put_chunk(
            self.a, upload_id, "e1", "mic", 1, b"b",
            start_ms=1000, end_ms=4000, token=token,
        )
        self.expect(overlap, 409, "overlapping_time")
        self.assertEqual(self.query_count("chunks"), 1)

        # Adjacent (end == next start) is a normal split, not an overlap.
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 1, b"b",
                start_ms=3000, end_ms=6000, token=token,
            )
        )
        self.assertEqual(self.query_count("chunks"), 2)

    # -- ACK position ----------------------------------------------------

    def test_ack_contiguous_prefix_and_gaps(self) -> None:
        _, upload_id, token = self.recorded_upload()

        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"a",
                start_ms=0, end_ms=1000, token=token,
            )
        )
        # Sequence 1 is missing: the saved position must stop at the hole even
        # though a later sequence exists (max sequence would be wrong).
        late = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 2, b"c",
                start_ms=2000, end_ms=3000, token=token,
            )
        )
        self.assertEqual(late.json()["contiguous_ms"], 1000)
        self.assertEqual(
            late.json()["missing"],
            [
                {
                    "track_id": "mic",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "reason": "missing_chunks",
                }
            ],
        )
        ack = self.expect(self.ack(self.a, upload_id))
        self.assertEqual(ack.json()["contiguous_ms"], 1000)

        filled = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 1, b"b",
                start_ms=1000, end_ms=2000, token=token,
            )
        )
        self.assertEqual(filled.json()["contiguous_ms"], 3000)
        self.assertEqual(filled.json()["missing"], [])

    def test_ack_minimum_across_tracks(self) -> None:
        _, upload_id, token = self.recorded_upload(tracks=[dict(MIC), dict(SYSTEM)])
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"mic",
                start_ms=0, end_ms=3000, token=token,
            )
        )
        lagging = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "system", 0, b"sys",
                start_ms=0, end_ms=1000, token=token,
            )
        )
        # The position is only as good as the slower track.
        self.assertEqual(lagging.json()["contiguous_ms"], 1000)
        self.assertEqual(lagging.json()["missing"], [])

    # -- epochs and the recovery window ----------------------------------

    def test_lease_heartbeat_expiry_and_new_epoch(self) -> None:
        meeting_id = self.create_meeting(self.a)
        token = self.lease(self.a, meeting_id)
        upload_id = self.open_upload(self.a, meeting_id, token)
        self.expect(self.register_epoch(self.a, upload_id, token), 200)
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"one",
                start_ms=0, end_ms=1000, token=token,
            )
        )

        self.clock.advance(seconds=60)
        heartbeat = self.a.post(
            "/api/cloud/capture-lease/heartbeat",
            json={"lease_token": token},
            headers=self.csrf_headers(self.a),
        )
        self.expect(heartbeat, 200)
        extended = heartbeat.json()["expires_at"]
        self.assertGreater(
            datetime.fromisoformat(extended.replace("Z", "+00:00")),
            self.clock.now() + timedelta(seconds=CAPTURE_LEASE_S - 1),
        )

        # Expiry: no NEW epoch may start, heartbeat is refused...
        self.clock.advance(seconds=CAPTURE_LEASE_S + 1)
        self.expect(
            self.a.post(
                "/api/cloud/capture-lease/heartbeat",
                json={"lease_token": token},
                headers=self.csrf_headers(self.a),
            ),
            409,
            "capture_lease_expired",
        )
        self.expect(
            self.register_epoch(self.a, upload_id, token, epoch_id="e2"),
            409,
            "capture_lease_expired",
        )
        # ...but already registered epochs stay writable inside the window and
        # the expiry never deleted an ACKed chunk.
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 1, b"two",
                start_ms=1000, end_ms=2000,
            )
        )
        self.assertEqual(self.query_count("chunks"), 2)

        # A fresh lease allows the recorder to continue in a new epoch.
        token2 = self.lease(self.a, meeting_id)
        self.expect(self.register_epoch(self.a, upload_id, token2, epoch_id="e2"), 200)
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e2", "mic", 0, b"three",
                start_ms=2000, end_ms=3000, token=token2,
            )
        )
        # The newest epoch requires a live lease for every chunk.
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e2", "mic", 1, b"four",
                start_ms=3000, end_ms=4000,
            ),
            409,
            "capture_lease_required",
        )

    def test_recovery_window_expiry_rejects_chunks(self) -> None:
        _, upload_id, token = self.recorded_upload()
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"one",
                start_ms=0, end_ms=1000, token=token,
            )
        )
        window = self.settings.recovery_window_s
        self.clock.advance(seconds=window - 60)
        client = self.login(A_EMAIL)  # the 12h session expired meanwhile

        # Same account resumes the unfinished upload with no lease at all: an
        # old epoch may still be extended inside the recovery window even
        # though the original lease is long expired.
        self.expect(
            self.put_chunk(
                client, upload_id, "e1", "mic", 1, b"two",
                start_ms=1000, end_ms=2000,
            )
        )

        self.clock.advance(seconds=120)
        expired = self.put_chunk(
            client, upload_id, "e1", "mic", 2, b"three",
            start_ms=2000, end_ms=3000,
        )
        self.expect(expired, 409, "recovery_window_expired")
        self.assertEqual(self.query_count("chunks"), 2)

    # -- durability, privacy, restart ------------------------------------

    def test_ack_survives_api_restart(self) -> None:
        _, upload_id, token = self.recorded_upload()
        body = b"restart-durable"
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=1000, token=token,
            )
        )
        self.restart_app()

        ack = self.expect(self.ack(self.a, upload_id))
        self.assertEqual(ack.json()["contiguous_ms"], 1000)
        retry = self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=1000, token=token,
            )
        )
        self.assertEqual(retry.json()["duplicate"], True)
        self.assertEqual(self.query_count("chunks"), 1)

    def test_objects_are_private_and_keys_server_generated(self) -> None:
        _, upload_id, token = self.recorded_upload()
        body = b"\x1aE\xdf\xa3private-audio"
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, body,
                start_ms=0, end_ms=1000, token=token,
            )
        )
        key = self.query_value("SELECT object_key FROM chunks")
        self.assertTrue(KEY_SHAPE.match(key), key)
        self.assertEqual(
            key,
            chunk_object_key(self.a_user_id, upload_id, "e1", "mic", 0),
        )
        self.assertTrue(key.startswith(f"chunks/{self.a_user_id}/{upload_id}/"))
        self.assertEqual(self.object_store.get_bytes(key), body)

        # Privacy is a filesystem property now: the object is a plain file
        # under a group-private root and its own mode grants nothing to other
        # users. No HTTP endpoint serves the root, so there is no anonymous
        # URL that could answer 200 (an anonymous HTTP probe answered 403).
        stored = self.object_store.root / key
        self.assertEqual(stored.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.object_store.root.stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.object_store.temp_dir.stat().st_mode & 0o777, 0o700)

    def test_oversized_body_rejected(self) -> None:
        _, upload_id, token = self.recorded_upload()
        small = self.settings.model_copy(update={"request_max_bytes": 4096})
        client = self.app_client(settings=small)
        oversized = b"x" * 8192
        response = self.put_chunk(
            client, upload_id, "e1", "mic", 0, oversized,
            start_ms=0, end_ms=1000, token=token,
        )
        self.expect(response, 413, "request_too_large")
        self.assertEqual(self.query_count("chunks"), 0)
        self.assertEqual(self.object_keys(), [])

    def test_csrf_and_auth_required(self) -> None:
        _, upload_id, token = self.recorded_upload()

        anonymous = self.anon.put(
            f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/0",
            params={"start_ms": 0, "end_ms": 1000, "checksum": sha(b"x"), "codec": CODEC},
            content=b"x",
        )
        self.expect(anonymous, 401)

        # Authenticated but no CSRF header: mutation refused before any write.
        no_csrf = self.a.put(
            f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/0",
            params={"start_ms": 0, "end_ms": 1000, "checksum": sha(b"x"), "codec": CODEC},
            content=b"x",
        )
        self.expect(no_csrf, 403)
        no_csrf_epoch = self.a.post(
            f"/api/cloud/uploads/{upload_id}/epochs",
            json=self.epoch_payload("e2"),
        )
        self.expect(no_csrf_epoch, 403)
        self.assertEqual(self.query_count("chunks"), 0)

    # -- epoch registration ----------------------------------------------

    def test_epoch_registration_conflicts(self) -> None:
        meeting_id = self.create_meeting(self.a)
        token = self.lease(self.a, meeting_id)
        upload_id = self.open_upload(self.a, meeting_id, token)
        self.expect(self.register_epoch(self.a, upload_id, token), 200)
        self.expect(self.register_epoch(self.a, upload_id, token), 409, "epoch_exists")

        bad_id = self.register_epoch(self.a, upload_id, token, epoch_id="e/1")
        self.expect(bad_id, 400, "invalid_epoch_id")

        duplicate_source = self.register_epoch(
            self.a,
            upload_id,
            token,
            epoch_id="e2",
            tracks=[dict(MIC), dict(MIC, id="mic2")],
        )
        self.expect(duplicate_source, 409, "track_conflict")

        no_tracks = self.register_epoch(self.a, upload_id, token, epoch_id="e3", tracks=[])
        self.expect(no_tracks, 422)

        unknown_upload = self.register_epoch(
            self.a, "00000000-0000-0000-0000-000000000000", token
        )
        self.expect(unknown_upload, 404)

        # Registering an epoch without the live lease is refused.
        self.expect(self.register_epoch(self.a, upload_id, None, epoch_id="e4"), 409, "capture_lease_required")

    # -- database constraints --------------------------------------------

    def test_composite_fk_and_unique_constraints(self) -> None:
        _, upload_id, token = self.recorded_upload()
        self.expect(
            self.put_chunk(
                self.a, upload_id, "e1", "mic", 0, b"one",
                start_ms=0, end_ms=1000, token=token,
            )
        )
        insert = text(
            """
            INSERT INTO chunks (
                id, owner_user_id, upload_session_id, epoch_id, track_id,
                sequence, start_ms, end_ms, checksum, codec, object_key,
                byte_size, created_at
            ) VALUES (
                :id, :owner, :upload, 'e1', 'mic',
                0, 0, 1000, :checksum, :codec, :object_key,
                3, now()
            )
            """
        )
        # Owner B cannot link a chunk to owner A's epoch/track (composite FK).
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    insert,
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "owner": self.b_user_id,
                        "upload": upload_id,
                        "checksum": sha(b"one"),
                        "codec": CODEC,
                        "object_key": "chunks/cross-owner.bin",
                    },
                )
        # The same identity twice is rejected by the unique constraint.
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    insert,
                    {
                        "id": "33333333-3333-3333-3333-333333333333",
                        "owner": self.a_user_id,
                        "upload": upload_id,
                        "checksum": sha(b"one"),
                        "codec": CODEC,
                        "object_key": "chunks/duplicate-identity.bin",
                    },
                )
        self.assertEqual(self.query_count("chunks"), 1)


class _CountingRequest:
    def __init__(self, parts: list[bytes]) -> None:
        self._parts = list(parts)
        self.consumed = 0

    async def stream(self):
        for part in self._parts:
            self.consumed += len(part)
            yield part


class BoundedReadCase(CloudCase):
    """The body reader must stop pulling bytes once the limit is exceeded."""

    def test_bounded_reader_stops_before_reading_everything(self) -> None:
        request = _CountingRequest([b"a" * 3000, b"b" * 3000, b"c" * 3000])
        with self.assertRaises(PayloadTooLargeError):
            asyncio.run(read_bounded_body(request, 4096))
        self.assertEqual(request.consumed, 6000, "third part must never be read")

    def test_bounded_reader_returns_body_within_limit(self) -> None:
        request = _CountingRequest([b"a" * 100, b"b" * 100])
        self.assertEqual(asyncio.run(read_bounded_body(request, 4096)), b"a" * 100 + b"b" * 100)
