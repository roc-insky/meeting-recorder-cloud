"""Durable owner-scoped events, SSE resume, and the degraded live seam.

Task 11 contracts (plan section 2.1 and the Task 11 brief):

* every state change writes its ResourceEvent in the same transaction, so a
  rolled-back change leaves no event and a committed one is never lost;
* the durable cursor is the ResourceEvent identity id, never a timestamp;
* a missing/foreign/future cursor replays the CALLER's snapshot, never another
  owner's event;
* the SSE stream revalidates the server session and the meeting on every poll,
  honors Last-Event-ID over ?after, and never holds the request session;
* submit_live is O(1), never raises, never waits on the model, and a full
  backlog degrades to a paused snapshot instead of blocking the upload ACK.

The live seam deliberately has no live.updated event, transcript text, speaker
label or ASR claim: this task only delivers scheduling/backpressure plumbing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from cloud import events, live
from cloud.contracts import AuthContext, Segment, TranscriptView
from cloud.events import read_events, sse_frame, stream_events
from cloud.schema import EVENT_NAMES, Meeting, ResourceEvent
from cloud.tasks import claim_task, enqueue_final, publish_result
from tests.cloud.support import CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"

FORBIDDEN_KEYS = {
    "audio",
    "email",
    "object_key",
    "owner_user_id",
    "payload",
    "speaker",
    "speaker_id",
    "text",
    "token",
}


def _assert_no_private_keys(test: CloudCase, value: object, path: str = "data") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            test.assertNotIn(key, FORBIDDEN_KEYS, f"private key {path}.{key}")
            _assert_no_private_keys(test, child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_private_keys(test, child, f"{path}[{index}]")


class EventsCase(CloudCase):
    def setUp(self) -> None:
        super().setUp()
        # The scheduler is a process-wide object; a backlog must not leak
        # between tests.
        live.reset_live_scheduler()

    def tearDown(self) -> None:
        live.reset_live_scheduler()
        super().tearDown()

    # -- helpers ----------------------------------------------------------

    def expect(self, response: object, status: int = 200) -> object:
        self.assertEqual(
            response.status_code,
            status,
            msg=f"unexpected status {response.status_code}: {response.text}",
        )
        return response

    def events_http(
        self, meeting_id: str, after: object = None, *, client: object = None
    ) -> dict[str, object]:
        client = client or self.a
        params = {} if after is None else {"after": after}
        response = self.expect(
            client.get(f"/api/cloud/meetings/{meeting_id}/events", params=params)
        )
        return response.json()

    def event_rows(self, meeting_id: str, after: int = 0) -> list[dict[str, object]]:
        return list(self.events_http(meeting_id, after)["items"])

    def context(self, db: object) -> AuthContext:
        return AuthContext(
            user_id=self.a_user_id,
            session_id="test-session",
            db=db,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )

    def open_upload(
        self, client: object, meeting_id: str | None = None
    ) -> tuple[str, str, dict[str, str]]:
        """Create/reuse a meeting and an open upload epoch; no chunks yet."""
        meeting_id = meeting_id or self.create_meeting(client)
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
        return meeting_id, upload_id, headers

    def put_chunk(
        self,
        client: object,
        upload_id: str,
        headers: dict[str, str],
        sequence: int = 0,
        body: bytes | None = None,
    ) -> object:
        body = body if body is not None else f"event-chunk-{sequence}".encode()
        return client.put(
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

    def sealed_upload(
        self, client: object, *, chunks: int = 1, meeting_id: str | None = None
    ) -> tuple[str, str, int]:
        meeting_id, upload_id, headers = self.open_upload(client, meeting_id)
        for sequence in range(chunks):
            self.expect(self.put_chunk(client, upload_id, headers, sequence))
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

    def queue(self, meeting_id: str, version: int, *, force: bool = False) -> dict[str, object]:
        response = self.expect(
            self.a.post(
                f"/api/cloud/meetings/{meeting_id}/tasks/final",
                json={"input_version": version, "force": force},
                headers=self.csrf_headers(self.a),
            )
        )
        return response.json()

    def claim(self, worker_id: str = "w1") -> object:
        db = self.session_factory()
        try:
            return claim_task(
                worker_id, self.clock.now(), db=db, settings=self.settings
            )
        finally:
            db.close()

    def task_events(self, meeting_id: str) -> list[dict[str, object]]:
        return [
            row
            for row in self.event_rows(meeting_id)
            if row["event"] == "task.updated"
        ]

    def session_id(self, user_id: str) -> str:
        return self.query_value(
            "SELECT id FROM sessions WHERE user_id = :uid ORDER BY expires_at DESC LIMIT 1",
            uid=user_id,
        )

    # -- required tests ---------------------------------------------------

    def test_restart_replays_private_snapshot(self) -> None:
        meeting_id = self.create_meeting(self.a, title="Restart replay")
        _, upload_id, _ = self.open_upload(self.a, meeting_id)

        first = self.events_http(meeting_id)
        self.assertEqual(len(first["items"]), 1)
        snapshot = first["items"][0]
        self.assertEqual(snapshot["event"], "snapshot")
        self.assertEqual(snapshot["cursor"], snapshot["data"]["cursor"])
        self.assertEqual(snapshot["data"]["meeting"]["id"], meeting_id)
        self.assertGreaterEqual(snapshot["data"]["cursor"], 1)
        self.assertIn(upload_id, [row["id"] for row in snapshot["data"]["uploads"]])
        _assert_no_private_keys(self, snapshot["data"])
        self.assertNotIn("event-chunk", json.dumps(snapshot))

        # A process restart rebuilds all in-memory state; the durable feed and
        # the private snapshot survive because they live in PostgreSQL.
        self.restart_app()
        replay = self.events_http(meeting_id, 0)
        names = [row["event"] for row in replay["items"]]
        self.assertEqual(names[:1], ["meeting.created"])
        self.assertIn("upload.created", names)
        after_restart = self.events_http(meeting_id)
        self.assertEqual(after_restart["items"][0]["event"], "snapshot")
        self.assertEqual(
            after_restart["items"][0]["data"]["meeting"]["id"], meeting_id
        )

    def test_expired_session_closes_stream(self) -> None:
        meeting_id = self.create_meeting(self.a)
        session_id = self.session_id(self.a_user_id)

        async def first_frame() -> str:
            generator = stream_events(
                session_factory=self.session_factory,
                clock=self.clock,
                settings=self.settings,
                user_id=self.a_user_id,
                session_id=session_id,
                meeting_id=meeting_id,
                after=None,
                poll_seconds=0,
                heartbeat_seconds=0,
            )
            try:
                return await generator.__anext__()
            finally:
                await generator.aclose()

        valid = asyncio.run(first_frame())
        self.assertIn("event: snapshot", valid)
        self.assertIn(meeting_id, valid)

        self.clock.advance(seconds=self.settings.session_ttl_s + 1)

        async def collect_until_close() -> list[str]:
            return [
                frame
                async for frame in stream_events(
                    session_factory=self.session_factory,
                    clock=self.clock,
                    settings=self.settings,
                    user_id=self.a_user_id,
                    session_id=session_id,
                    meeting_id=meeting_id,
                    after=None,
                    poll_seconds=0,
                    heartbeat_seconds=0,
                )
            ]

        # Expiry is checked before any event/snapshot is read: the stream stops
        # without emitting one private byte.
        self.assertEqual(asyncio.run(collect_until_close()), [])

    def test_live_backlog_does_not_block_ack(self) -> None:
        meeting_id, upload_id, headers = self.open_upload(self.a)

        # Fill the bounded scheduler with distinct meetings so the real ACK's
        # submit_live is a new key while the backlog is saturated.
        for index in range(1, live.MAX_PENDING + 2):
            # Cursor 0 is a non-advancing repeat of the initial state, so start
            # at 1 to queue exactly MAX_PENDING distinct meetings.
            live.submit_live(f"backlog-{index}", self.a_user_id, index * 1000)
        scheduler = live.get_live_scheduler()
        self.assertEqual(scheduler.pending(), live.MAX_PENDING)
        self.assertEqual(
            scheduler.snapshot(
                self.a_user_id, f"backlog-{live.MAX_PENDING + 1}"
            )["state"],
            "paused",
        )

        started = time.monotonic()
        ack = self.expect(self.put_chunk(self.a, upload_id, headers, 0))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "submit_live must not block the upload ACK")
        self.assertFalse(ack.json()["duplicate"])

        # The ACK is already durable and the event exists before the scheduler
        # is consulted; a duplicate ACK writes no second progress event.
        progress = [
            row
            for row in self.event_rows(meeting_id)
            if row["event"] == "upload.progress"
        ]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["data"]["sequence"], 0)
        self.assertEqual(progress[0]["data"]["track_id"], "mic")

        duplicate = self.expect(self.put_chunk(self.a, upload_id, headers, 0))
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(
            len(
                [
                    row
                    for row in self.event_rows(meeting_id)
                    if row["event"] == "upload.progress"
                ]
            ),
            1,
        )

    def test_b_subscription_to_a_is_404(self) -> None:
        meeting_id = self.create_meeting(self.a)
        b_page = self.b.get(f"/api/cloud/meetings/{meeting_id}/events")
        self.assertEqual(b_page.status_code, 404)
        self.assertEqual(b_page.json(), {"error": "meeting_not_found"})
        b_stream = self.b.get(f"/api/cloud/meetings/{meeting_id}/events/stream")
        self.assertEqual(b_stream.status_code, 404)
        self.assertEqual(b_stream.json(), {"error": "meeting_not_found"})

    # -- cursor and snapshot rules ---------------------------------------

    def test_cursor_rules_return_snapshot_empty_or_replay(self) -> None:
        meeting_id = self.create_meeting(self.a)
        latest = self.events_http(meeting_id)["items"][0]["cursor"]
        self.assertEqual(latest, 1)

        self.assertEqual(
            [row["event"] for row in self.events_http(meeting_id, 0)["items"]],
            ["meeting.created"],
        )
        self.assertEqual(self.events_http(meeting_id, latest)["items"], [])
        empty_next = self.events_http(meeting_id, latest)["next_cursor"]
        self.assertEqual(empty_next, latest)

        for invalid in (latest + 1, "bogus", "-2", ""):
            page = self.events_http(meeting_id, invalid)
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["items"][0]["event"], "snapshot")

        self.expect(
            self.a.patch(
                f"/api/cloud/meetings/{meeting_id}",
                json={"title": "Renamed"},
                headers=self.csrf_headers(self.a),
            )
        )
        replayed = self.events_http(meeting_id, latest)["items"]
        self.assertEqual([row["event"] for row in replayed], ["meeting.updated"])

    def test_foreign_and_other_meeting_cursor_returns_caller_snapshot(self) -> None:
        a_first = self.create_meeting(self.a, title="A first")
        b_meeting = self.create_meeting(self.b, title="B secret")
        self.expect(
            self.a.patch(
                f"/api/cloud/meetings/{a_first}",
                json={"title": "A renamed"},
                headers=self.csrf_headers(self.a),
            )
        )
        # Event ids 1, 2, 3 exist; 2 belongs to B and 3 to A. Replaying A from
        # B's id must not disclose B's event or state.
        page = self.events_http(a_first, 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["event"], "snapshot")
        self.assertEqual(page["items"][0]["data"]["meeting"]["id"], a_first)
        self.assertNotIn(b_meeting, json.dumps(page))

        a_second = self.create_meeting(self.a, title="A second")
        other = self.events_http(a_second, 1)
        self.assertEqual(other["items"][0]["event"], "snapshot")
        self.assertEqual(other["items"][0]["data"]["meeting"]["id"], a_second)

        db = self.session_factory()
        try:
            ctx = self.context(db)
            self.assertEqual(read_events(ctx, a_first, True)[0].event, "snapshot")
            self.assertEqual(read_events(ctx, a_first, -3)[0].event, "snapshot")
        finally:
            db.close()

    def test_replay_cap_falls_back_to_snapshot(self) -> None:
        meeting_id = self.create_meeting(self.a)
        db = self.session_factory()
        try:
            rows = [
                ResourceEvent(
                    owner_user_id=self.a_user_id,
                    meeting_id=meeting_id,
                    event="meeting.updated",
                    data={"meeting_id": meeting_id, "generation": 1},
                    created_at=self.clock.now(),
                )
                for _ in range(events.MAX_REPLAY + 1)
            ]
            db.add_all(rows)
            db.commit()
        finally:
            db.close()
        page = self.events_http(meeting_id, 0)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["event"], "snapshot")

    # -- same-transaction event writes -----------------------------------

    def test_record_rolls_back_with_its_state_change(self) -> None:
        meeting_id = self.create_meeting(self.a)
        before = self.query_count("resource_events")
        db = self.session_factory()
        try:
            events.record(
                db,
                owner_user_id=self.a_user_id,
                meeting_id=meeting_id,
                event="meeting.updated",
                data={"meeting_id": meeting_id, "generation": 99},
                now=self.clock.now(),
            )
            db.flush()
            self.assertEqual(
                db.scalar(
                    select(func.count(ResourceEvent.id)).where(
                        ResourceEvent.meeting_id == meeting_id
                    )
                ),
                2,
            )
            db.rollback()
        finally:
            db.close()
        self.assertEqual(self.query_count("resource_events"), before)

        db = self.session_factory()
        try:
            events.record(
                db,
                owner_user_id=self.a_user_id,
                meeting_id=meeting_id,
                event="meeting.updated",
                data={"meeting_id": meeting_id, "generation": 100},
                now=self.clock.now(),
            )
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.query_count("resource_events"), before + 1)

    def test_seal_records_upload_sealed_event(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        sealed = [
            row for row in self.event_rows(meeting_id) if row["event"] == "upload.sealed"
        ]
        self.assertEqual(len(sealed), 1)
        self.assertEqual(sealed[0]["data"]["status"], "complete")
        self.assertEqual(sealed[0]["data"]["input_version"], version)
        self.assertEqual(sealed[0]["data"]["gap_count"], 0)
        self.assertFalse(
            {row["event"] for row in self.event_rows(meeting_id)} - set(EVENT_NAMES)
        )

    def test_supersede_and_cancel_record_same_commit(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        waiting = self.queue(meeting_id, version)
        claimed = self.claim("w1")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(waiting["id"], claimed.task.id)

        before_rows = len(self.event_rows(meeting_id))
        superseding = self.queue(meeting_id, version, force=True)
        after = self.event_rows(meeting_id)
        new_rows = after[before_rows:]
        self.assertEqual(
            [(row["event"], row["data"].get("status")) for row in new_rows],
            [("task.updated", "cancelled"), ("task.updated", "waiting")],
        )
        self.assertEqual(new_rows[0]["data"]["task_id"], claimed.task.id)
        self.assertEqual(new_rows[0]["data"]["error_code"], "superseded")
        self.assertEqual(new_rows[1]["data"]["task_id"], superseding["id"])

        # A duplicate submit of the same input reuses the row and writes no
        # event: the caller's commit is the only side effect.
        count = len(self.event_rows(meeting_id))
        duplicate = self.queue(meeting_id, version)
        self.assertEqual(duplicate["id"], superseding["id"])
        self.assertEqual(len(self.event_rows(meeting_id)), count)

        self.expect(
            self.a.post(
                f"/api/cloud/tasks/{superseding['id']}/cancel",
                headers=self.csrf_headers(self.a),
            ),
            204,
        )
        cancelled = [
            row
            for row in self.task_events(meeting_id)
            if row["data"].get("task_id") == superseding["id"]
            and row["data"].get("status") == "cancelled"
        ]
        self.assertEqual(len(cancelled), 1)

    def test_claim_and_expired_lease_record_resulting_states(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        self.queue(meeting_id, version)

        claimed = self.claim("w1")
        self.assertIsNotNone(claimed)
        self.assertEqual(
            [
                row["data"]["status"]
                for row in self.task_events(meeting_id)
                if row["data"].get("status") == "running"
            ],
            ["running"],
        )

        task_id = claimed.task.id
        self.clock.advance(seconds=self.settings.worker_lease_s + 1)
        reclaimed = self.claim("w2")
        self.assertIsNotNone(reclaimed)
        statuses = [
            row["data"]["status"]
            for row in self.task_events(meeting_id)
            if row["data"].get("task_id") == task_id
        ]
        # One claim call records the expired requeue, then the new running lease.
        self.assertEqual(statuses[-2:], ["waiting", "running"])

        self.clock.advance(seconds=self.settings.worker_lease_s + 1)
        self.assertIsNotNone(self.claim("w3"))
        self.clock.advance(seconds=self.settings.worker_lease_s + 1)
        self.assertIsNone(self.claim("w4"))
        failed = [
            row
            for row in self.task_events(meeting_id)
            if row["data"].get("task_id") == task_id
            and row["data"].get("status") == "failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["data"]["error_code"], "lease_expired")

    def test_publish_records_transcript_and_task_in_one_commit(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        self.queue(meeting_id, version)
        claimed = self.claim("w1")
        self.assertIsNotNone(claimed)
        result = TranscriptView(
            id=str(uuid4()),
            meeting_id=meeting_id,
            segments=[
                Segment(
                    id="seg-1",
                    start_ms=0,
                    end_ms=1000,
                    text="private transcript text",
                )
            ],
            speakers=[],
            warnings=[],
            empty=False,
            status="complete",
        )
        db = self.session_factory()
        try:
            self.assertTrue(
                publish_result(
                    claimed.task.id,
                    claimed.lease_token,
                    result,
                    owner_user_id=self.a_user_id,
                    now=self.clock.now(),
                    db=db,
                )
            )
        finally:
            db.close()

        rows = self.event_rows(meeting_id)
        names = [row["event"] for row in rows]
        self.assertIn("transcript.published", names)
        published = rows[names.index("transcript.published")]
        self.assertEqual(published["data"]["version_id"], result.id)
        self.assertEqual(published["data"]["status"], "complete")
        self.assertNotIn("private transcript text", json.dumps(rows))
        completed = [
            row
            for row in self.task_events(meeting_id)
            if row["data"].get("status") == "completed"
        ]
        self.assertEqual(completed[-1]["data"]["task_id"], claimed.task.id)

        snapshot = self.events_http(meeting_id)["items"][0]["data"]
        self.assertEqual(snapshot["transcript"]["id"], result.id)
        self.assertEqual(snapshot["meeting"]["current_transcript_id"], result.id)

    def test_rejected_publish_records_nothing(self) -> None:
        meeting_id, _, version = self.sealed_upload(self.a)
        self.queue(meeting_id, version)
        claimed = self.claim("w1")
        self.assertIsNotNone(claimed)
        count = self.query_count("resource_events")
        result = TranscriptView(
            id=str(uuid4()),
            meeting_id=meeting_id,
            segments=[],
            speakers=[],
            warnings=[],
            empty=True,
            status="complete",
        )
        db = self.session_factory()
        try:
            self.assertFalse(
                publish_result(
                    claimed.task.id,
                    "forged-lease-token",
                    result,
                    owner_user_id=self.a_user_id,
                    now=self.clock.now(),
                    db=db,
                )
            )
        finally:
            db.close()
        self.assertEqual(self.query_count("resource_events"), count)

    # -- stream framing, ordering and close conditions -------------------

    def test_stream_replays_durable_frames_then_closes_on_delete(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.expect(
            self.a.patch(
                f"/api/cloud/meetings/{meeting_id}",
                json={"title": "Stream renamed"},
                headers=self.csrf_headers(self.a),
            )
        )
        session_id = self.session_id(self.a_user_id)

        async def drive() -> list[str]:
            generator = stream_events(
                session_factory=self.session_factory,
                clock=self.clock,
                settings=self.settings,
                user_id=self.a_user_id,
                session_id=session_id,
                meeting_id=meeting_id,
                after=0,
                poll_seconds=0,
                heartbeat_seconds=0,
            )
            frames = [await generator.__anext__(), await generator.__anext__()]
            db = self.session_factory()
            try:
                meeting = db.get(Meeting, (self.a_user_id, meeting_id))
                meeting.deleted_at = self.clock.now()
                db.commit()
            finally:
                db.close()
            try:
                await generator.__anext__()
            except StopAsyncIteration:
                return frames
            finally:
                await generator.aclose()
            raise AssertionError("stream did not close after meeting deletion")

        frames = asyncio.run(drive())
        self.assertTrue(frames[0].startswith("id: 1\nevent: meeting.created\ndata: "))
        self.assertTrue(frames[0].endswith("\n\n"))
        self.assertIn("event: meeting.updated", frames[1])
        self.assertIn(meeting_id, frames[1])
        self.assertIn('"generation":0', frames[1])
        json.loads(frames[1].split("data: ", 1)[1].strip())

    def test_stream_uses_last_event_id_over_query(self) -> None:
        meeting_id = self.create_meeting(self.a)
        self.expect(
            self.a.patch(
                f"/api/cloud/meetings/{meeting_id}",
                json={"title": "Header wins"},
                headers=self.csrf_headers(self.a),
            )
        )
        captured: dict[str, object] = {}
        original = events.stream_events

        def fake_stream(**kwargs: object):
            # Capture at call time: the route never iterates the generator in
            # this unit test, so an async generator body would not run.
            captured.update(kwargs)

            async def empty():
                if False:  # pragma: no cover - empty async iterator
                    yield ""

            return empty()

        db = self.session_factory()
        request = SimpleNamespace(
            headers={"last-event-id": "1"},
            app=SimpleNamespace(state=self.app.state),
        )
        events.stream_events = fake_stream
        try:
            response = asyncio.run(
                events.stream_events_route(
                    meeting_id,
                    request,
                    after="0",
                    ctx=self.context(db),
                )
            )
        finally:
            events.stream_events = original
        self.assertEqual(captured["after"], 1)
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertFalse(db.in_transaction(), "request session must be closed")

        # A malformed Last-Event-ID is a snapshot, never a replay.
        captured.clear()
        db = self.session_factory()
        request = SimpleNamespace(
            headers={"last-event-id": "not-a-cursor"},
            app=SimpleNamespace(state=self.app.state),
        )
        events.stream_events = fake_stream
        try:
            asyncio.run(
                events.stream_events_route(
                    meeting_id,
                    request,
                    after="0",
                    ctx=self.context(db),
                )
            )
        finally:
            events.stream_events = original
        self.assertIsNone(captured["after"])

    def test_sse_frame_is_compact_and_durable(self) -> None:
        from cloud.contracts import EventView
        from cloud.contracts import iso_utc

        frame = sse_frame(
            EventView(
                cursor=42,
                event="task.updated",
                data={"task_id": "t1", "status": "waiting"},
                created_at=iso_utc(self.clock.now()),
            )
        )
        self.assertEqual(
            frame,
            'id: 42\nevent: task.updated\n'
            'data: {"task_id":"t1","status":"waiting"}\n\n',
        )

    def test_live_snapshot_has_no_invented_transcript(self) -> None:
        meeting_id = self.create_meeting(self.a)
        live.submit_live(meeting_id, self.a_user_id, 3000)
        snapshot = self.events_http(meeting_id)["items"][0]["data"]
        self.assertEqual(snapshot["live"], {"state": "queued", "cursor": 3000})
        self.assertNotIn("text", json.dumps(snapshot["live"]))
        self.assertNotIn("speaker", json.dumps(snapshot["live"]))


if __name__ == "__main__":
    unittest.main()
