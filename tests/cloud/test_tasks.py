"""Durable processing tasks: claim, fence, publish once, cancel, retry.

Task 8 contract (plan section 2.1 and Task 8):

* a final task is enqueued against a sealed input version and is idempotent
  for the same (meeting, input_version, generation);
* only one worker can claim a task at a time through a row lease; a killed
  worker's task is reclaimed after the lease expires, and the new claim
  invalidates the old token;
* publishing re-checks owner, kind, input, generation and lease inside one
  transaction, so a stale or forged worker writes nothing;
* retries are bounded to transient failures; cancel and delete are terminal;
* fairness rotates across owners so one user's backlog cannot take every slot,
  and no claim or enqueue ever takes a user-wide or global lock.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from fastapi.testclient import TestClient

from cloud.app import create_app
from cloud.contracts import ConflictError, NotFoundError, TranscriptView
from cloud.db import make_session_factory
from cloud.schema import Meeting, ProcessingTask
from cloud.tasks import (
    claim_task,
    enqueue_final,
    fail_task,
    publish_result,
    renew_task,
)
from cloud.worker import TransientProcessingError, WorkItem, open_work, process_once
from tests.cloud.support import A_EMAIL, B_EMAIL, CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"


class TasksCase(CloudCase):
    # -- helpers ----------------------------------------------------------

    def expect(self, response: Any, status: int = 200, code: str | None = None) -> Any:
        self.assertEqual(
            response.status_code,
            status,
            msg=f"unexpected status {response.status_code}: {response.text}",
        )
        if code is not None:
            self.assertEqual(response.json(), {"error": code})
        return response

    def sealed_upload(
        self,
        client: Any,
        *,
        meeting_id: str | None = None,
        chunks: int = 1,
        mode: str = "mic",
    ) -> tuple[str, str, int]:
        """Create (or reuse) a meeting and seal one upload; returns the version."""
        if meeting_id is None:
            meeting_id = self.create_meeting(client, mode=mode)
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
            body = f"task-chunk-{sequence}".encode()
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

    def enqueue_http(
        self, client: Any, meeting_id: str, version: int, *, force: bool = False
    ) -> Any:
        return client.post(
            f"/api/cloud/meetings/{meeting_id}/tasks/final",
            json={"input_version": version, "force": force},
            headers=self.csrf_headers(client),
        )

    def task(
        self, client: Any, meeting_id: str, version: int, *, force: bool = False
    ) -> dict[str, Any]:
        return self.expect(
            self.enqueue_http(client, meeting_id, version, force=force)
        ).json()

    def new_task(self, client: Any, *, chunks: int = 1) -> tuple[dict[str, Any], str]:
        """Open a fresh meeting, seal it and enqueue one final task."""
        meeting_id, _, version = self.sealed_upload(client, chunks=chunks)
        return self.task(client, meeting_id, version), meeting_id

    def claim(self, worker_id: str = "w1", session_factory: Any = None) -> Any:
        factory = session_factory or self.session_factory
        session = factory()
        try:
            return claim_task(
                worker_id,
                self.clock.now(),
                db=session,
                settings=self.settings,
            )
        finally:
            session.close()

    def publish(
        self,
        task_id: str,
        lease_token: str,
        result: TranscriptView,
        *,
        owner_user_id: str | None = None,
    ) -> bool:
        session = self.session_factory()
        try:
            return publish_result(
                task_id,
                lease_token,
                result,
                owner_user_id=owner_user_id,
                now=self.clock.now(),
                db=session,
            )
        finally:
            session.close()

    def renew(self, task_id: str, lease_token: str) -> None:
        session = self.session_factory()
        try:
            renew_task(
                task_id,
                lease_token,
                now=self.clock.now(),
                db=session,
                settings=self.settings,
            )
        finally:
            session.close()

    def mark_failed(self, task_id: str, lease_token: str, *, transient: bool) -> bool:
        session = self.session_factory()
        try:
            return fail_task(
                task_id,
                lease_token,
                transient=transient,
                now=self.clock.now(),
                db=session,
                settings=self.settings,
            )
        finally:
            session.close()

    def run_worker(self, processor: Any, worker_id: str = "w1") -> bool:
        session = self.session_factory()
        try:
            return process_once(
                processor,
                worker_id=worker_id,
                now=self.clock.now(),
                db=session,
                settings=self.settings,
            )
        finally:
            session.close()

    def transcript(self, meeting_id: str, transcript_id: str | None = None) -> TranscriptView:
        return TranscriptView(
            id=transcript_id or str(uuid4()),
            meeting_id=meeting_id,
            segments=[],
            speakers=[],
            warnings=[],
            empty=True,
        )

    def task_row(self, task_id: str) -> tuple[str, str | None, int, int]:
        row = self.query_value(
            "SELECT status || '|' || coalesce(error_code, '') || '|' || attempt "
            "|| '|' || generation FROM processing_tasks WHERE id = :id",
            id=task_id,
        )
        status, error_code, attempt, generation = str(row).split("|")
        return status, error_code or None, int(attempt), int(generation)

    def lease_deadline(self, task_id: str) -> Any:
        return self.query_value(
            "SELECT lease_deadline FROM processing_tasks WHERE id = :id", id=task_id
        )

    def current_transcript(self, meeting_id: str) -> Any:
        return self.query_value(
            "SELECT current_transcript_id FROM meetings WHERE id = :id", id=meeting_id
        )

    # -- required Task 8 cases -------------------------------------------

    def test_expired_worker_cannot_publish(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        task = self.task(self.a, meeting_id, version)
        first = self.claim("w1")
        self.assertIsNotNone(first)
        self.assertEqual(first.task.id, task["id"])

        # The worker stalls; the lease expires and a second worker takes over.
        self.clock.advance(seconds=self.settings.worker_lease_s + 1)
        self.assertFalse(
            self.publish(
                task["id"], first.lease_token, self.transcript(meeting_id, "t-stale")
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 0)

        second = self.claim("w2")
        self.assertIsNotNone(second)
        self.assertEqual(second.task.id, task["id"])
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertEqual(self.task_row(task["id"])[2], 2)
        self.assertTrue(
            self.publish(
                task["id"], second.lease_token, self.transcript(meeting_id, "t-live")
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 1)
        self.assertEqual(self.current_transcript(meeting_id), "t-live")
        # Even finishing first cannot publish after the lease moved on.
        self.assertFalse(
            self.publish(
                task["id"], first.lease_token, self.transcript(meeting_id, "t-stale")
            )
        )

    def test_duplicate_submit_and_consume_once(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        first = self.task(self.a, meeting_id, version)
        again = self.task(self.a, meeting_id, version)
        forced = self.task(self.a, meeting_id, version, force=True)
        self.assertEqual(
            {first["id"], again["id"], forced["id"]}, {first["id"]}
        )
        self.assertEqual(self.query_count("processing_tasks"), 1)
        self.assertEqual(first["status"], "waiting")

        claimed = self.claim("w1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.task.id, first["id"])
        self.assertEqual(
            (
                claimed.task.owner_user_id,
                claimed.task.input_version,
                claimed.task.generation,
            ),
            (self.a_user_id, version, first["generation"]),
        )
        # Only the hash of the lease token is durable.
        stored = self.query_value(
            "SELECT lease_token_hash FROM processing_tasks WHERE id = :id",
            id=first["id"],
        )
        self.assertEqual(stored, hashlib.sha256(claimed.lease_token.encode()).hexdigest())
        self.assertNotEqual(stored, claimed.lease_token)

        result = self.transcript(meeting_id, "t-1")
        self.assertTrue(self.publish(first["id"], claimed.lease_token, result))
        self.assertFalse(self.publish(first["id"], claimed.lease_token, result))
        self.assertEqual(self.query_count("transcript_versions"), 1)
        self.assertEqual(self.current_transcript(meeting_id), "t-1")
        self.assertEqual(self.task_row(first["id"])[:2], ("completed", None))

        # An explicit re-transcribe of the same input supersedes the result.
        retry = self.task(self.a, meeting_id, version, force=True)
        self.assertNotEqual(retry["id"], first["id"])
        self.assertEqual(retry["generation"], first["generation"] + 1)
        self.assertEqual(self.query_count("processing_tasks"), 2)

        second = self.claim("w2")
        self.assertIsNotNone(second)
        self.assertEqual(second.task.id, retry["id"])
        self.assertTrue(
            self.publish(retry["id"], second.lease_token, self.transcript(meeting_id, "t-2"))
        )
        self.assertEqual(self.query_count("transcript_versions"), 2)
        self.assertEqual(self.current_transcript(meeting_id), "t-2")

    def test_users_do_not_share_global_lock(self) -> None:
        meeting_a, _, version_a = self.sealed_upload(self.a)
        task_a = self.task(self.a, meeting_a, version_a)
        claimed_a = self.claim("wa")
        self.assertEqual(claimed_a.task.id, task_a["id"])

        # Hold row locks on A's meeting and A's in-flight task only.
        ready = threading.Event()
        release = threading.Event()

        def hold_a_rows() -> None:
            engine = create_engine(self.database_url)
            try:
                with engine.connect() as connection:
                    transaction = connection.begin()
                    connection.execute(
                        text(
                            "SELECT 1 FROM meetings WHERE owner_user_id = :owner "
                            "AND id = :meeting FOR UPDATE"
                        ),
                        {"owner": self.a_user_id, "meeting": meeting_a},
                    )
                    connection.execute(
                        text("SELECT 1 FROM processing_tasks WHERE id = :task FOR UPDATE"),
                        {"task": task_a["id"]},
                    )
                    ready.set()
                    release.wait(30)
                    transaction.rollback()
            finally:
                engine.dispose()

        holder = threading.Thread(target=hold_a_rows)
        holder.start()
        self.assertTrue(ready.wait(5), "lock holder did not start")

        # B's whole flow runs on a separate app whose statements fail instead
        # of waiting, so a global lock shows up as an error rather than a hang.
        blocked_engine = create_engine(
            self.database_url, connect_args={"options": "-c lock_timeout=3000"}
        )
        blocked_app = create_app(
            self.settings,
            {
                "engine": blocked_engine,
                "clock": self.clock,
                "object_store": self.object_store,
            },
        )
        client_b = self.login(B_EMAIL, TestClient(blocked_app))
        started = time.monotonic()
        try:
            meeting_b, _, version_b = self.sealed_upload(client_b)
            queued_b = self.task(client_b, meeting_b, version_b)
            self.assertEqual(queued_b["owner_user_id"], self.b_user_id)
            claimed_b = self.claim(
                "wb", session_factory=make_session_factory(blocked_engine)
            )
            self.assertIsNotNone(claimed_b)
            self.assertEqual(claimed_b.task.id, queued_b["id"])
        finally:
            elapsed = time.monotonic() - started
            release.set()
            holder.join(5)
            client_b.close()
            blocked_engine.dispose()
        self.assertLess(
            elapsed,
            10.0,
            "B's upload/enqueue/claim blocked on A's task rows",
        )
        # A's in-flight task is still exactly as the first worker left it.
        self.assertEqual(self.task_row(task_a["id"])[0], "running")
        self.assertEqual(claimed_a.task.id, task_a["id"])

    def test_queue_message_owner_mismatch_rejected(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        task = self.task(self.a, meeting_id, version)
        claimed = self.claim("w1")

        honest = WorkItem(
            task_id=task["id"],
            owner_user_id=self.a_user_id,
            lease_token=claimed.lease_token,
            generation=task["generation"],
            input_version=version,
            meeting_id=meeting_id,
        )
        session = self.session_factory()
        try:
            opened = open_work(honest, now=self.clock.now(), db=session)
        finally:
            session.close()
        self.assertEqual(opened.task.id, task["id"])

        # A tampered queue message naming the other user is refused on read.
        forged = WorkItem(
            task_id=task["id"],
            owner_user_id=self.b_user_id,
            lease_token=claimed.lease_token,
            generation=task["generation"],
            input_version=version,
            meeting_id=meeting_id,
        )
        session = self.session_factory()
        try:
            with self.assertRaises(NotFoundError) as refused:
                open_work(forged, now=self.clock.now(), db=session)
        finally:
            session.close()
        self.assertEqual(str(refused.exception), "task_not_found")

        # ... and on publish, without leaking or writing anything.
        self.assertFalse(
            self.publish(
                task["id"],
                claimed.lease_token,
                self.transcript(meeting_id, "t-forged"),
                owner_user_id=self.b_user_id,
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 0)
        self.assertIsNone(self.current_transcript(meeting_id))
        # The legitimate owner still publishes exactly once.
        self.assertTrue(
            self.publish(
                task["id"],
                claimed.lease_token,
                self.transcript(meeting_id, "t-owner"),
                owner_user_id=self.a_user_id,
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 1)

    # -- coverage ---------------------------------------------------------

    def test_concurrent_claim_race(self) -> None:
        task, _ = self.new_task(self.a)
        barrier = threading.Barrier(2)
        claims: list[Any] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            session = self.session_factory()
            try:
                barrier.wait(5)
                claim = claim_task(
                    name, self.clock.now(), db=session, settings=self.settings
                )
                with lock:
                    claims.append(claim)
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(claims), 2)
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1, f"both workers claimed: {claims}")
        self.assertEqual(winners[0].task.id, task["id"])
        self.assertEqual(self.task_row(task["id"])[0], "running")

    def test_fair_rotation_across_owners(self) -> None:
        # Ordered creation: A1, B1, A2, B2, A3.
        sequence: list[tuple[str, dict[str, Any]]] = []
        owners: dict[str, str] = {}
        plan = (("a", self.a), ("b", self.b), ("a", self.a), ("b", self.b), ("a", self.a))
        for owner, client in plan:
            created, _ = self.new_task(client)
            owners[created["id"]] = owner
            sequence.append((owner, created))
            self.clock.advance(milliseconds=100)
        self.assertEqual([owner for owner, _ in sequence], ["a", "b", "a", "b", "a"])

        claimed_order: list[str] = []
        for index in range(5):
            self.clock.advance(seconds=1)
            claimed = self.claim(f"w{index}")
            self.assertIsNotNone(claimed)
            claimed_order.append(owners[claimed.task.id])
        # Strict rotation: no owner takes two slots while the other has work.
        self.assertEqual(claimed_order, ["a", "b", "a", "b", "a"])

    def test_claims_oldest_task_first_for_one_owner(self) -> None:
        first, _ = self.new_task(self.a)
        self.clock.advance(milliseconds=100)
        second, _ = self.new_task(self.a)
        one = self.claim("w1")
        self.assertEqual(one.task.id, first["id"])
        two = self.claim("w2")
        self.assertEqual(two.task.id, second["id"])

    def test_renew_after_expiry(self) -> None:
        task, _ = self.new_task(self.a)
        claimed = self.claim("w1")
        deadline = self.lease_deadline(task["id"])
        self.clock.advance(seconds=self.settings.worker_lease_s + 1)
        with self.assertRaises(ConflictError) as expired:
            self.renew(task["id"], claimed.lease_token)
        self.assertEqual(str(expired.exception), "lease_expired")
        self.assertEqual(self.lease_deadline(task["id"]), deadline)
        with self.assertRaises(ConflictError) as wrong:
            self.renew(task["id"], "not-the-lease-token")
        self.assertEqual(str(wrong.exception), "lease_lost")
        # The expired lease is reclaimable, and the old token never comes back.
        reclaimed = self.claim("w2")
        self.assertEqual(reclaimed.task.id, task["id"])
        self.assertNotEqual(reclaimed.lease_token, claimed.lease_token)

    def test_generation_supersede(self) -> None:
        meeting_id, _, first_version = self.sealed_upload(self.a, chunks=1)
        old = self.task(self.a, meeting_id, first_version)
        self.assertEqual(self.task_row(old["id"])[3], 0)
        stale_worker = self.claim("w1")

        # A newer recording of the same meeting supersedes the running task.
        _, _, second_version = self.sealed_upload(
            self.a, meeting_id=meeting_id, chunks=2
        )
        self.assertNotEqual(first_version, second_version)
        fresh = self.task(self.a, meeting_id, second_version)
        self.assertNotEqual(fresh["id"], old["id"])
        self.assertEqual(fresh["generation"], 1)
        self.assertEqual(self.task_row(old["id"])[:2], ("cancelled", "superseded"))

        self.assertFalse(
            self.publish(
                old["id"], stale_worker.lease_token, self.transcript(meeting_id, "t-old")
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 0)
        self.assertIsNone(self.current_transcript(meeting_id))

        current = self.claim("w2")
        self.assertEqual(current.task.id, fresh["id"])
        self.assertTrue(
            self.publish(
                current.task.id,
                current.lease_token,
                self.transcript(meeting_id, "t-new"),
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 1)
        self.assertEqual(self.current_transcript(meeting_id), "t-new")

    def test_cancel_other_user_is_404_and_never_retries(self) -> None:
        task, _ = self.new_task(self.a)
        foreign = self.b.post(
            f"/api/cloud/tasks/{task['id']}/cancel", headers=self.csrf_headers(self.b)
        )
        self.expect(foreign, 404, "task_not_found")
        self.assertEqual(self.task_row(task["id"])[0], "waiting")

        self.expect(
            self.a.post(
                f"/api/cloud/tasks/{task['id']}/cancel",
                headers=self.csrf_headers(self.a),
            ),
            204,
        )
        self.assertEqual(self.task_row(task["id"])[:2], ("cancelled", "cancelled_by_user"))
        self.assertIsNone(self.claim("w1"))
        # Cancelling again is a no-op, and the cancelled task never retries.
        self.expect(
            self.a.post(
                f"/api/cloud/tasks/{task['id']}/cancel",
                headers=self.csrf_headers(self.a),
            ),
            204,
        )
        self.assertIsNone(self.claim("w2"))

    def test_deleted_meeting_not_enqueued_or_published(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        task = self.task(self.a, meeting_id, version)
        claimed = self.claim("w1")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE meetings SET deleted_at = now() "
                    "WHERE owner_user_id = :owner AND id = :meeting"
                ),
                {"owner": self.a_user_id, "meeting": meeting_id},
            )
        self.assertFalse(
            self.publish(
                task["id"],
                claimed.lease_token,
                self.transcript(meeting_id, "t-deleted"),
            )
        )
        self.assertEqual(self.query_count("transcript_versions"), 0)
        self.assertIsNone(self.current_transcript(meeting_id))
        self.expect(self.enqueue_http(self.a, meeting_id, version), 404, "meeting_not_found")

    def test_unsealed_or_foreign_input_rejected(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.expect(self.enqueue_http(self.a, meeting_id, 1), 409, "input_not_sealed")
        _, _, version = self.sealed_upload(self.a, meeting_id=meeting_id, chunks=2)
        self.assertEqual(version, 2)
        self.expect(self.enqueue_http(self.a, meeting_id, 1), 409, "input_not_sealed")
        self.expect(
            self.enqueue_http(self.b, meeting_id, version), 404, "meeting_not_found"
        )
        queued = self.task(self.a, meeting_id, version)
        self.assertEqual(queued["input_version"], 2)

    def test_bounded_retry_stops_at_three(self) -> None:
        task, _ = self.new_task(self.a)

        def failing(_: WorkItem) -> TranscriptView:
            raise TransientProcessingError("secret engine failure text")

        for attempt in (1, 2):
            self.assertFalse(self.run_worker(failing, f"w{attempt}"))
            self.assertEqual(self.task_row(task["id"])[:2], ("waiting", "transient_failure"))
            self.assertEqual(self.task_row(task["id"])[2], attempt)
        self.assertFalse(self.run_worker(failing, "w3"))
        status, error_code, attempt, _ = self.task_row(task["id"])
        self.assertEqual((status, error_code, attempt), ("failed", "transient_failure", 3))
        self.assertEqual(self.query_count("transcript_versions"), 0)
        self.assertFalse(self.run_worker(failing, "w4"))
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM processing_tasks "
                "WHERE error_code LIKE '%secret%'"
            ),
            0,
        )

    def test_non_transient_failure_is_terminal(self) -> None:
        task, _ = self.new_task(self.a)

        def failing(_: WorkItem) -> TranscriptView:
            raise ValueError("secret engine failure text")

        self.assertFalse(self.run_worker(failing, "w1"))
        self.assertEqual(
            self.task_row(task["id"])[:3], ("failed", "processing_failed", 1)
        )
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM processing_tasks "
                "WHERE error_code LIKE '%secret%'"
            ),
            0,
        )
        self.assertFalse(self.run_worker(failing, "w2"))

    def test_worker_publishes_injected_result(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        task = self.task(self.a, meeting_id, version)
        seen: list[WorkItem] = []

        def processor(item: WorkItem) -> TranscriptView:
            seen.append(item)
            return self.transcript(meeting_id, "t-worker")

        self.assertTrue(self.run_worker(processor, "w1"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].task_id, task["id"])
        self.assertEqual(seen[0].owner_user_id, self.a_user_id)
        self.assertEqual(self.query_count("transcript_versions"), 1)
        self.assertEqual(self.current_transcript(meeting_id), "t-worker")
        self.assertFalse(self.run_worker(processor, "w2"))
