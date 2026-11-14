"""Sealing: a recording becomes complete or visibly partial, never a guess.

Task 7 contract (design §7.2, plan Task 7):

* sealing is transactional: it verifies the manifest against what the server
  actually ACKed, freezes input_version and only then flips the upload to
  sealed (a failed seal leaves the upload open and appendable);
* a missing tail or a missing track never yields complete: with
  accept_partial=False the request is refused, with accept_partial=True the
  result is partial with explicit gaps;
* the saved position is the intersection of every registered track (never the
  newest epoch alone, never the maximum sequence);
* expiry sealing keeps every ACKed object and only closes the upload;
* a new epoch after a refresh keeps its real interruption and gains anchor
  uncertainty instead of pretending the clock never moved.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tests.cloud.support import A_EMAIL, CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
SYSTEM = {"id": "system", "source": "system", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"


class SealingCase(CloudCase):
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

    def lease(self, client: Any, meeting_id: str) -> str:
        response = self.expect(
            client.post(
                "/api/cloud/capture-lease",
                json={"meeting_id": meeting_id},
                headers=self.csrf_headers(client),
            ),
            201,
        )
        return response.json()["lease_token"]

    def open_upload(
        self, client: Any, mode: str = "mic"
    ) -> tuple[str, dict[str, str]]:
        meeting_id = self.create_meeting(client, mode=mode)
        token = self.lease(client, meeting_id)
        response = self.expect(
            client.post(
                "/api/cloud/uploads",
                json={"meeting_id": meeting_id},
                headers={**self.csrf_headers(client), "X-Capture-Lease": token},
            ),
            201,
        )
        return response.json()["id"], {"X-Capture-Lease": token}

    def register_epoch(
        self,
        client: Any,
        upload_id: str,
        headers: dict[str, str],
        epoch_id: str = "e1",
        *,
        offset_ms: int = 0,
        uncertainty_ms: int = 0,
        tracks: list[dict[str, Any]] | None = None,
    ) -> Any:
        return self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/epochs",
                json={
                    "id": epoch_id,
                    "offset_ms": offset_ms,
                    "uncertainty_ms": uncertainty_ms,
                    "started_at": STARTED_AT,
                    "tracks": tracks or [dict(MIC)],
                },
                headers={**self.csrf_headers(client), **headers},
            ),
            200,
        )

    def put_chunk(
        self,
        client: Any,
        upload_id: str,
        headers: dict[str, str],
        epoch_id: str,
        track_id: str,
        sequence: int,
        start_ms: int,
        end_ms: int,
        body: bytes | None = None,
    ) -> Any:
        payload = body if body is not None else f"{epoch_id}-{track_id}-{sequence}".encode()
        query = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "checksum": hashlib.sha256(payload).hexdigest(),
            "codec": CODEC,
        }
        return client.put(
            f"/api/cloud/uploads/{upload_id}/chunks/{epoch_id}/{track_id}/{sequence}",
            params=query,
            content=payload,
            headers={**self.csrf_headers(client), **headers},
        )

    def ack(self, client: Any, upload_id: str) -> Any:
        return client.get(f"/api/cloud/uploads/{upload_id}/ack")

    def seal(
        self,
        client: Any,
        upload_id: str,
        tracks: list[dict[str, Any]],
        *,
        accept_partial: bool = False,
    ) -> Any:
        return client.post(
            f"/api/cloud/uploads/{upload_id}/seal",
            json={"tracks": tracks, "accept_partial": accept_partial},
            headers=self.csrf_headers(client),
        )

    def manifest_entry(
        self, epoch_id: str, track_id: str, last_sequence: int, end_ms: int
    ) -> dict[str, Any]:
        return {
            "capture_epoch_id": epoch_id,
            "track_id": track_id,
            "last_sequence": last_sequence,
            "end_ms": end_ms,
        }

    def upload_status(self, upload_id: str) -> tuple[str, str | None, int]:
        row = self.query_value(
            "SELECT status || '|' || coalesce(sealed_status, '') || '|' || input_version "
            "FROM upload_sessions WHERE id = :id",
            id=upload_id,
        )
        status, sealed_status, version = str(row).split("|")
        return status, sealed_status or None, int(version)

    def object_keys(self) -> list[str]:
        keys = self.query_value(
            "SELECT array_agg(object_key ORDER BY object_key) FROM chunks"
        )
        return sorted(keys or [])

    # -- the required Task 7 cases ---------------------------------------

    def test_missing_tail_never_complete(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 1, 3000, 6000))
        manifest = [self.manifest_entry("e1", "mic", 2, 9000)]

        refused = self.seal(self.a, upload_id, manifest, accept_partial=False)
        self.expect(refused, 409, "incomplete_recording")
        self.assertEqual(self.upload_status(upload_id), ("open", None, 0))

        partial = self.expect(self.seal(self.a, upload_id, manifest, accept_partial=True))
        self.assertEqual(partial.json()["status"], "partial")
        self.assertEqual(
            partial.json()["gaps"],
            [
                {
                    "track_id": "mic",
                    "start_ms": 6000,
                    "end_ms": 9000,
                    "reason": "missing_chunks",
                }
            ],
        )
        status, sealed_status, _ = self.upload_status(upload_id)
        self.assertEqual((status, sealed_status), ("sealed", "partial"))

        # A sealed recording never accepts more audio; a new recording is new.
        self.expect(
            self.put_chunk(self.a, upload_id, headers, "e1", "mic", 2, 6000, 9000),
            409,
            "upload_closed",
        )

    def test_dual_ack_is_intersection(self) -> None:
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(self.a, upload_id, headers, tracks=[dict(MIC), dict(SYSTEM)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 10000))
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "system", 0, 0, 6000))

        ack = self.expect(self.ack(self.a, upload_id))
        self.assertEqual(ack.json()["contiguous_ms"], 6000)

    def test_ack_intersection_spans_epochs(self) -> None:
        # The system track only exists in epoch 1; a page reload starts epoch 2
        # with mic only. The saved position must not jump to the mic's 10000.
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(
            self.a, upload_id, headers, "e1", tracks=[dict(MIC), dict(SYSTEM)]
        )
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 6000))
        self.expect(
            self.put_chunk(self.a, upload_id, headers, "e1", "system", 0, 0, 6000)
        )
        self.register_epoch(self.a, upload_id, headers, "e2", offset_ms=6000, tracks=[dict(MIC)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e2", "mic", 0, 6000, 10000))

        ack = self.expect(self.ack(self.a, upload_id))
        self.assertEqual(ack.json()["contiguous_ms"], 6000)

    def test_expiry_keeps_acked_objects(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        keys = self.object_keys()
        self.assertEqual(len(keys), 1)
        self.assertTrue(self.object_store.exists(keys[0]))

        self.clock.advance(seconds=86401)
        from cloud.sealing import seal_expired

        session = self.session_factory()
        try:
            sealed = seal_expired(self.clock.now(), db=session, settings=self.settings)
        finally:
            session.close()
        self.assertEqual(sealed, 1)

        status, sealed_status, _ = self.upload_status(upload_id)
        self.assertEqual((status, sealed_status), ("sealed", "partial"))
        for key in keys:
            self.assertTrue(self.object_store.exists(key), key)
        # A day later the session itself has expired, so re-authenticate: the
        # sealed upload must still refuse the append, not accept more audio.
        fresh = self.login(A_EMAIL)
        self.expect(
            self.put_chunk(fresh, upload_id, headers, "e1", "mic", 1, 3000, 6000),
            409,
            "upload_closed",
        )

    def test_incomplete_manifest_is_rejected(self) -> None:
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(self.a, upload_id, headers, tracks=[dict(MIC), dict(SYSTEM)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        # The client forgot the system track it registered: never seal silently.
        self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 0, 3000)]),
            409,
            "incomplete_manifest",
        )
        self.assertEqual(self.upload_status(upload_id)[0], "open")

    def test_stale_manifest_after_recovery_is_rejected(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        # The manifest was built before the recovered chunk landed.
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 1, 3000, 6000))
        self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 0, 3000)]),
            409,
            "manifest_stale",
        )

    def test_incomplete_seal_is_transactional(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 1, 6000)]),
            409,
            "incomplete_recording",
        )
        # Nothing was frozen: the missing tail can still be recovered.
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 1, 3000, 6000))
        complete = self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 1, 6000)])
        )
        self.assertEqual(complete.json()["status"], "complete")
        self.assertEqual(complete.json()["gaps"], [])

    def test_seal_freezes_input_version(self) -> None:
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(self.a, upload_id, headers, tracks=[dict(MIC), dict(SYSTEM)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "system", 0, 0, 3000))
        manifest = [
            self.manifest_entry("e1", "mic", 0, 3000),
            self.manifest_entry("e1", "system", 0, 3000),
        ]
        sealed = self.expect(self.seal(self.a, upload_id, manifest))
        self.assertEqual(
            sealed.json(), {"input_version": 2, "status": "complete", "gaps": []}
        )
        self.assertEqual(self.upload_status(upload_id), ("sealed", "complete", 2))

    def test_second_tab_seal_conflicts(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        other = self.login(A_EMAIL)
        self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 0, 3000)])
        )
        self.expect(
            self.seal(other, upload_id, [self.manifest_entry("e1", "mic", 0, 3000)]),
            409,
            "upload_closed",
        )

    def test_unequal_track_lengths_are_visible_gaps(self) -> None:
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(self.a, upload_id, headers, tracks=[dict(MIC), dict(SYSTEM)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 10000))
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "system", 0, 0, 6000))
        manifest = [
            self.manifest_entry("e1", "mic", 0, 10000),
            self.manifest_entry("e1", "system", 0, 6000),
        ]
        self.expect(self.seal(self.a, upload_id, manifest), 409, "incomplete_recording")
        partial = self.expect(self.seal(self.a, upload_id, manifest, accept_partial=True))
        self.assertEqual(partial.json()["status"], "partial")
        self.assertIn(
            {
                "track_id": "system",
                "start_ms": 6000,
                "end_ms": 10000,
                "reason": "missing_tail",
            },
            partial.json()["gaps"],
        )

    def test_cross_epoch_anchor_gains_uncertainty(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers, "e1")
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 6000))

        overlapping = self.register_epoch(
            self.a, upload_id, headers, "e2", offset_ms=3000, uncertainty_ms=0
        )
        # The new anchor contradicts 3s of audio the server already has.
        self.assertEqual(overlapping.json()["uncertainty_ms"], 3000)

        continued = self.register_epoch(
            self.a, upload_id, headers, "e3", offset_ms=6000, uncertainty_ms=0
        )
        # A refresh anchor is never claimed to be exact.
        self.assertGreaterEqual(continued.json()["uncertainty_ms"], 1000)

    def test_out_of_order_recovery_seals_complete(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 1, 3000, 6000))
        self.assertEqual(self.ack(self.a, upload_id).json()["contiguous_ms"], 0)
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000))
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 2, 6000, 9000))
        complete = self.expect(
            self.seal(self.a, upload_id, [self.manifest_entry("e1", "mic", 2, 9000)])
        )
        self.assertEqual(
            complete.json(), {"input_version": 3, "status": "complete", "gaps": []}
        )

    def test_duplicate_chunk_with_conflicting_range_is_rejected(self) -> None:
        upload_id, headers = self.open_upload(self.a)
        self.register_epoch(self.a, upload_id, headers)
        self.expect(
            self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 3000, body=b"same")
        )
        # Same identity and checksum, different time range: a conflicting retry.
        self.expect(
            self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 4000, body=b"same"),
            409,
            "chunk_conflict",
        )

    def test_dual_downgrade_records_system_gap(self) -> None:
        upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(
            self.a, upload_id, headers, "e1", tracks=[dict(MIC), dict(SYSTEM)]
        )
        self.expect(self.put_chunk(self.a, upload_id, headers, "e1", "mic", 0, 0, 6000))
        self.expect(
            self.put_chunk(self.a, upload_id, headers, "e1", "system", 0, 0, 6000)
        )
        # Sharing stopped; the user confirmed a mic-only continuation.
        self.register_epoch(self.a, upload_id, headers, "e2", offset_ms=6000, tracks=[dict(MIC)])
        self.expect(self.put_chunk(self.a, upload_id, headers, "e2", "mic", 0, 6000, 9000))

        manifest = [
            self.manifest_entry("e1", "mic", 0, 6000),
            self.manifest_entry("e1", "system", 0, 6000),
            self.manifest_entry("e2", "mic", 0, 9000),
        ]
        partial = self.expect(self.seal(self.a, upload_id, manifest, accept_partial=True))
        self.assertEqual(partial.json()["status"], "partial")
        self.assertIn(
            {
                "track_id": "system",
                "start_ms": 6000,
                "end_ms": 9000,
                "reason": "missing_tail",
            },
            partial.json()["gaps"],
        )
