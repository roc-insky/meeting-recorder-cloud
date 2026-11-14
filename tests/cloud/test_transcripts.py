"""Task 10: published transcript versions, stable speakers and re-transcription.

These tests use real PostgreSQL and real MinIO (already running), synthetic
16 kHz mono tone fixtures encoded as Opus/WebM chunks, and a deterministic fake
model adapter. Nothing here measures ASR quality or runs a real recognizer.
"""

from __future__ import annotations

import array
import copy
import hashlib
import io
import json
import math
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from cloud.contracts import (
    AuthContext,
    Embedding,
    ModelMetadata,
    Segment,
    TranscriptView,
)
from cloud.models import (
    EnhancementError,
    ModelNotConfigured,
    RecognitionParams,
    build_adapter,
    load_params,
    require_selected,
)
from cloud.tasks import claim_task, fail_task, publish_result
from cloud.transcripts import transcribe_task
from cloud.worker import WorkItem, process_once
from tests.cloud.support import A_EMAIL, B_EMAIL, CloudCase

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "docs/superpowers/evidence/cloud/model-contract.json"
PARAMS = load_params(json.loads(CONTRACT_PATH.read_text(encoding="utf-8")))

RATE = 16000
HEADER_MS = 0

#: Deterministic voice -> orthogonal unit vector. Fixtures place a pure tone at
#: one of these frequencies in each block; the fake adapter estimates the tone
#: from decoded PCM, so Opus lossiness does not change the identity.
VOICE_VECTORS = {
    200: [1.0, 0.0, 0.0],
    600: [0.0, 1.0, 0.0],
    1000: [0.0, 0.0, 1.0],
}
VOICE_AMPLITUDE = 12000

META = ModelMetadata(
    model_id="fake-speaker",
    model_version="1",
    weights_sha256="0" * 64,
    dimension=3,
    sample_rate=RATE,
    preprocess_version="pcm16",
    normalization_version="l2",
    calibration_version="none",
)


def wav_bytes(duration_ms: int, blocks: list[tuple[int, int, int]]) -> bytes:
    """Build one 16 kHz mono s16 WAV; each block is a pure tone frequency."""
    frames = duration_ms * RATE // 1000
    samples = array.array("h", bytes(2 * frames))
    for start_ms, end_ms, frequency in blocks:
        start = start_ms * RATE // 1000
        end = end_ms * RATE // 1000
        for index in range(start, min(end, frames)):
            samples[index] = int(
                VOICE_AMPLITUDE * math.sin(2 * math.pi * frequency * index / RATE)
            )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(samples.tobytes())
    return buffer.getvalue()


def webm_bytes(wav: bytes) -> bytes:
    """Encode the synthetic WAV as a real 16 kHz mono Opus/WebM chunk."""
    with tempfile.TemporaryDirectory(prefix="task10-fixture-") as directory:
        source = Path(directory) / "in.wav"
        target = Path(directory) / "out.webm"
        source.write_bytes(wav)
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-ar",
                str(RATE),
                "-ac",
                "1",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                str(target),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode("utf-8", "replace"))
        return target.read_bytes()


class FakeAdapter:
    """Stateful, deterministic stand-in for a real ASR/diarization adapter."""

    def __init__(
        self,
        meeting_id: str,
        drafts: list[dict[str, Any]],
        *,
        fail_embed: bool = False,
        metadata: ModelMetadata = META,
        vectors: dict[int, list[float]] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.meeting_id = meeting_id
        self.drafts = list(drafts)
        self.fail_embed = fail_embed
        self.metadata = metadata
        self.vectors = dict(VOICE_VECTORS if vectors is None else vectors)
        self.warnings = list(warnings or [])
        self.transcribe_calls: list[dict[str, Any]] = []
        self.embed_calls: list[int] = []

    def transcribe(self, audio: Any, speakers: int | None) -> TranscriptView:
        self.transcribe_calls.append(
            {
                "rate": audio.rate,
                "tracks": sorted(audio.tracks),
                "speakers": speakers,
            }
        )
        segments = [
            Segment(
                id=f"draft-{index}",
                start_ms=draft["start_ms"],
                end_ms=draft["end_ms"],
                text=draft["text"],
                track_id=draft.get("track"),
            )
            for index, draft in enumerate(self.drafts)
        ]
        return TranscriptView(
            id=str(uuid4()),
            meeting_id=self.meeting_id,
            segments=segments,
            speakers=[],
            warnings=list(self.warnings),
            empty=not segments,
        )

    def embed(self, samples: Any) -> Embedding:
        self.embed_calls.append(len(samples))
        if self.fail_embed:
            raise EnhancementError("diarization unavailable")
        crossings = 0
        previous = samples[0]
        for sample in samples[1:]:
            if (previous < 0) != (sample < 0):
                crossings += 1
            previous = sample
        duration_s = len(samples) / self.metadata.sample_rate
        frequency = crossings / (2 * duration_s)
        voice = min(self.vectors, key=lambda candidate: abs(candidate - frequency))
        if abs(voice - frequency) > 120:
            raise EnhancementError(f"unknown voice frequency {frequency:.1f}")
        return Embedding(vector=list(self.vectors[voice]), metadata=self.metadata)


class TranscriptsCase(CloudCase):
    """Publish-path tests: upload+seal, real prepare_audio, fake adapter."""

    # -- fixtures ---------------------------------------------------------

    def sealed_media(
        self,
        *,
        meeting_id: str | None = None,
        tracks: dict[str, list[tuple[int, int, int]]] | None = None,
        duration_ms: int = 6000,
        mode: str = "mic",
        client: Any = None,
    ) -> tuple[str, int]:
        client = client or self.a
        if meeting_id is None:
            meeting_id = self.create_meeting(client, mode=mode)
        if tracks is None:
            tracks = {"mic": [(0, duration_ms, 600)]}
        headers = self.csrf_headers(client)
        lease = client.post(
            "/api/cloud/capture-lease",
            json={"meeting_id": meeting_id},
            headers=headers,
        )
        self.assertEqual(lease.status_code, 201, lease.text)
        upload_headers = {
            **headers,
            "X-Capture-Lease": lease.json()["lease_token"],
        }
        upload = client.post(
            "/api/cloud/uploads",
            json={"meeting_id": meeting_id},
            headers=upload_headers,
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["id"]
        specs = [
            {
                "id": track_id,
                "source": "system" if track_id == "system" else "mic",
                "codec": "audio/webm;codecs=opus",
                "sample_rate": RATE,
            }
            for track_id in tracks
        ]
        epoch = client.post(
            f"/api/cloud/uploads/{upload_id}/epochs",
            json={
                "id": "e1",
                "offset_ms": 0,
                "uncertainty_ms": 0,
                "started_at": "2026-11-14T07:00:00Z",
                "tracks": specs,
            },
            headers=upload_headers,
        )
        self.assertEqual(epoch.status_code, 200, epoch.text)
        for track_id, blocks in tracks.items():
            body = webm_bytes(wav_bytes(duration_ms, blocks))
            checksum = hashlib.sha256(body).hexdigest()
            chunk = client.put(
                f"/api/cloud/uploads/{upload_id}/chunks/e1/{track_id}/0",
                params={
                    "start_ms": 0,
                    "end_ms": duration_ms,
                    "checksum": checksum,
                    "codec": "audio/webm;codecs=opus",
                },
                content=body,
                headers=upload_headers,
            )
            self.assertEqual(chunk.status_code, 200, chunk.text)
        seal = client.post(
            f"/api/cloud/uploads/{upload_id}/seal",
            json={
                "tracks": [
                    {
                        "capture_epoch_id": "e1",
                        "track_id": track_id,
                        "last_sequence": 0,
                        "end_ms": duration_ms,
                    }
                    for track_id in tracks
                ],
                "accept_partial": False,
            },
            headers=upload_headers,
        )
        self.assertEqual(seal.status_code, 200, seal.text)
        self.assertEqual(seal.json()["status"], "complete")
        return meeting_id, int(seal.json()["input_version"])

    def context(self, db: Any, *, user_id: str | None = None) -> AuthContext:
        return AuthContext(
            user_id=user_id or self.a_user_id,
            session_id="test-session",
            db=db,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )

    def queue(
        self,
        meeting_id: str,
        input_version: int,
        *,
        force: bool = False,
        client: Any = None,
    ) -> dict[str, Any]:
        client = client or self.a
        response = client.post(
            f"/api/cloud/meetings/{meeting_id}/tasks/final",
            json={"input_version": input_version, "force": force},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def start_task(
        self,
        adapter: FakeAdapter,
        meeting_id: str,
        input_version: int,
        *,
        force: bool = False,
        params: RecognitionParams = PARAMS,
        worker_id: str = "w1",
        user_id: str | None = None,
        client: Any = None,
    ) -> tuple[Any, Any, TranscriptView, dict[str, Any]]:
        queued = self.queue(
            meeting_id, input_version, force=force, client=client
        )
        db = self.session_factory()
        self.addCleanup(db.close)
        claimed = claim_task(
            worker_id, self.clock.now(), db=db, settings=self.settings
        )
        self.assertIsNotNone(claimed, "expected a claimable final task")
        assert claimed is not None
        item = WorkItem(
            task_id=claimed.task.id,
            owner_user_id=claimed.task.owner_user_id,
            lease_token=claimed.lease_token,
            generation=claimed.task.generation,
            input_version=claimed.task.input_version,
            meeting_id=claimed.task.meeting_id,
        )
        view = transcribe_task(
            self.context(db, user_id=user_id),
            item,
            adapter=adapter,
            params=params,
            workspace=Path(tempfile.mkdtemp(prefix="task10-audio-")),
        )
        return db, claimed, view, queued

    def publish(
        self, db: Any, claimed: Any, view: TranscriptView
    ) -> bool:
        return publish_result(
            claimed.task.id,
            claimed.lease_token,
            view,
            owner_user_id=claimed.task.owner_user_id,
            now=self.clock.now(),
            db=db,
        )

    def run_worker(
        self,
        adapter: FakeAdapter,
        meeting_id: str,
        input_version: int,
        *,
        params: RecognitionParams = PARAMS,
        user_id: str | None = None,
        worker_id: str = "w1",
        client: Any = None,
    ) -> bool:
        self.queue(meeting_id, input_version, client=client)
        db = self.session_factory()
        self.addCleanup(db.close)
        ctx = self.context(db, user_id=user_id)
        workspace = Path(tempfile.mkdtemp(prefix="task10-audio-"))

        def processor(item: WorkItem, run_ctx: AuthContext) -> TranscriptView:
            return transcribe_task(
                run_ctx,
                item,
                adapter=adapter,
                params=params,
                workspace=workspace,
            )

        return process_once(
            processor,
            worker_id=worker_id,
            now=self.clock.now(),
            db=db,
            settings=self.settings,
            context=ctx,
        )

    # -- read helpers -----------------------------------------------------

    def list_http(
        self, meeting_id: str, *, client: Any = None
    ) -> list[dict[str, Any]]:
        client = client or self.a
        response = client.get(
            f"/api/cloud/meetings/{meeting_id}/transcripts"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def detail_http(
        self, meeting_id: str, version_id: str, *, client: Any = None
    ) -> dict[str, Any]:
        client = client or self.a
        response = client.get(
            f"/api/cloud/meetings/{meeting_id}/transcripts/{version_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def current_view(
        self, meeting_id: str, *, client: Any = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        client = client or self.a
        views = self.list_http(meeting_id, client=client)
        pointer = self.query_value(
            "SELECT current_transcript_id FROM meetings"
            " WHERE owner_user_id = :owner AND id = :id",
            owner=self.session_payload(client)["user_id"],
            id=meeting_id,
        )
        self.assertIsNotNone(pointer, "current transcript pointer was not set")
        current = next(view for view in views if view["id"] == pointer)
        return current, views

    def meeting_pointer(self, meeting_id: str, *, owner: str | None = None) -> Any:
        return self.query_value(
            "SELECT current_transcript_id FROM meetings"
            " WHERE owner_user_id = :owner AND id = :id",
            owner=owner or self.a_user_id,
            id=meeting_id,
        )

    def version_row(self, version_id: str) -> tuple[str, bool, Any]:
        status, empty, warnings = self.query_value(
            "SELECT status || '|' || empty || '|' || warnings::text"
            " FROM transcript_versions WHERE id = :id",
            id=version_id,
        ).split("|", 2)
        return status, empty == "true", json.loads(warnings)

    def task_row(self, task_id: str) -> tuple[str, str | None, int]:
        row = self.query_value(
            "SELECT status || '|' || coalesce(error_code, '') || '|' || generation"
            " FROM processing_tasks WHERE id = :id",
            id=task_id,
        )
        status, error_code, generation = str(row).split("|")
        return status, error_code or None, int(generation)

    # -- mandatory tests --------------------------------------------------

    def test_old_task_finishes_last_without_pointer_regression(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=3000)
        old_adapter = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "旧稿内容",
                }
            ],
        )
        old_db, old_claim, old_view, _ = self.start_task(
            old_adapter, meeting_id, input_version
        )
        new_adapter = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "新稿内容",
                }
            ],
        )
        self.clock.advance(seconds=1)
        new_db, new_claim, new_view, queued = self.start_task(
            new_adapter, meeting_id, input_version, force=True
        )
        self.assertNotEqual(old_claim.task.id, new_claim.task.id)
        self.assertEqual(queued["generation"], 1)
        self.assertEqual(new_claim.task.generation, 1)
        self.assertEqual(
            self.task_row(old_claim.task.id), ("cancelled", "superseded", 0)
        )

        self.assertTrue(self.publish(new_db, new_claim, new_view))
        self.assertEqual(self.meeting_pointer(meeting_id), new_view.id)
        # The superseded worker wakes up last and must write nothing.
        self.assertFalse(self.publish(old_db, old_claim, old_view))
        self.assertEqual(self.meeting_pointer(meeting_id), new_view.id)
        self.assertEqual(self.query_count("transcript_versions"), 1)
        self.assertEqual(self.query_count("transcript_segments"), 1)
        current, _ = self.current_view(meeting_id)
        self.assertEqual(current["id"], new_view.id)
        self.assertEqual(current["segments"][0]["text"], "新稿内容")
        self.assertEqual(
            self.task_row(new_claim.task.id)[0], "completed"
        )

    def test_enhancement_failure_keeps_basic_text(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=6000)
        adapter = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "甲说第一句话",
                },
                {
                    "track": "mic",
                    "start_ms": 3000,
                    "end_ms": 5000,
                    "text": "乙说第二句话",
                },
            ],
            fail_embed=True,
        )
        self.assertTrue(self.run_worker(adapter, meeting_id, input_version))

        current, views = self.current_view(meeting_id)
        self.assertEqual(len(views), 1)
        self.assertEqual(current["status"], "completed_with_warnings")
        self.assertIn("speaker_enhancement_failed", current["warnings"])
        self.assertFalse(current["empty"])
        self.assertEqual(
            [segment["text"] for segment in current["segments"]],
            ["甲说第一句话", "乙说第二句话"],
        )
        self.assertTrue(
            all(segment["speaker_id"] is None for segment in current["segments"])
        )
        self.assertEqual(current["speakers"], [])
        self.assertEqual(self.query_count("transcript_segments"), 2)
        self.assertEqual(self.query_count("transcript_speakers"), 0)
        self.assertEqual(self.query_count("meeting_speakers"), 0)
        status, empty, warnings = self.version_row(current["id"])
        self.assertEqual(status, "completed_with_warnings")
        self.assertFalse(empty)
        self.assertIn("speaker_enhancement_failed", warnings)
        task_id = self.query_value(
            "SELECT id FROM processing_tasks WHERE meeting_id = :id",
            id=meeting_id,
        )
        self.assertEqual(self.task_row(task_id)[0], "completed")
        self.assertEqual(self.meeting_pointer(meeting_id), current["id"])

    def test_silence_is_empty(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=3000)
        adapter = FakeAdapter(meeting_id, [])
        self.assertTrue(self.run_worker(adapter, meeting_id, input_version))

        current, views = self.current_view(meeting_id)
        self.assertEqual(len(views), 1)
        self.assertTrue(current["empty"])
        self.assertEqual(current["segments"], [])
        self.assertEqual(current["speakers"], [])
        self.assertEqual(current["warnings"], [])
        self.assertEqual(current["status"], "complete")
        self.assertEqual(self.query_count("transcript_segments"), 0)
        self.assertEqual(self.query_count("transcript_speakers"), 0)
        self.assertEqual(self.query_count("meeting_speakers"), 0)
        status, empty, warnings = self.version_row(current["id"])
        self.assertEqual(status, "complete")
        self.assertTrue(empty)
        self.assertEqual(warnings, [])
        self.assertEqual(self.meeting_pointer(meeting_id), current["id"])

    def test_cross_track_no_duplicate_sentence_or_person(self) -> None:
        dedup = replace(PARAMS, strategy="separate_merge")
        cases = {
            "same_person_across_tracks": {
                "tracks": {
                    "mic": [(0, 2000, 200)],
                    "system": [(3000, 5000, 200)],
                },
                "drafts": [
                    {
                        "track": "mic",
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "我们开始吧",
                    },
                    {
                        "track": "system",
                        "start_ms": 3000,
                        "end_ms": 5000,
                        "text": "收到，我先说",
                    },
                ],
                "identities": 1,
                "utterances": 2,
                "duplicates": 0,
            },
            "duplicate_pickup": {
                "tracks": {
                    "mic": [(0, 2000, 200)],
                    "system": [(100, 2100, 200)],
                },
                "drafts": [
                    {
                        "track": "mic",
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "这句被两个麦克风同时收到",
                    },
                    {
                        "track": "system",
                        "start_ms": 100,
                        "end_ms": 2100,
                        "text": "这句被两个麦克风同时收到",
                    },
                ],
                "identities": 1,
                "utterances": 1,
                "duplicates": 1,
            },
            "two_remote_speakers": {
                "tracks": {
                    "system": [(0, 2000, 200), (3000, 5000, 600)],
                },
                "drafts": [
                    {
                        "track": "system",
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "我是远端第一位",
                    },
                    {
                        "track": "system",
                        "start_ms": 3000,
                        "end_ms": 5000,
                        "text": "我是远端第二位",
                    },
                ],
                "identities": 2,
                "utterances": 2,
                "duplicates": 0,
            },
        }
        for name, case in cases.items():
            with self.subTest(name=name):
                meeting_id, input_version = self.sealed_media(
                    tracks=case["tracks"],
                    duration_ms=6000,
                    mode="dual",
                )
                adapter = FakeAdapter(meeting_id, case["drafts"])
                self.assertTrue(
                    self.run_worker(
                        adapter, meeting_id, input_version, params=dedup
                    )
                )
                current, _ = self.current_view(meeting_id)
                self.assertEqual(
                    len(current["segments"]),
                    case["utterances"],
                    f"{name}: segments {current['segments']}",
                )
                self.assertEqual(
                    2 - len(current["segments"]), case["duplicates"]
                )
                self.assertEqual(
                    len(current["speakers"]), case["identities"]
                )
                assigned = {
                    segment["speaker_id"] for segment in current["segments"]
                }
                self.assertEqual(len(assigned), case["identities"])
                for speaker in current["speakers"]:
                    self.assertNotIn(
                        speaker["label"], {"mic", "system", "mix", "mixdown"}
                    )
                    self.assertNotIn(
                        speaker["id"], {"mic", "system", "mix", "mixdown"}
                    )
                    self.assertTrue(speaker["label"].startswith("说话人 "))
                if name == "duplicate_pickup":
                    self.assertEqual(current["segments"][0]["start_ms"], 0)
                    self.assertEqual(
                        current["segments"][0]["track_id"], "mic"
                    )

    def test_users_cannot_read_each_others_transcripts(self) -> None:
        a_meeting, a_version = self.sealed_media(duration_ms=3000)
        a_adapter = FakeAdapter(
            a_meeting,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "甲方私密句子",
                }
            ],
        )
        self.assertTrue(
            self.run_worker(a_adapter, a_meeting, a_version, worker_id="w-a")
        )
        a_current, _ = self.current_view(a_meeting)

        b_meeting, b_version = self.sealed_media(
            duration_ms=3000, client=self.b
        )
        b_adapter = FakeAdapter(
            b_meeting,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "乙方独立句子",
                }
            ],
        )
        self.assertTrue(
            self.run_worker(
                b_adapter,
                b_meeting,
                b_version,
                worker_id="w-b",
                client=self.b,
                user_id=self.b_user_id,
            )
        )

        self.assertEqual(
            self.b.get(
                f"/api/cloud/meetings/{a_meeting}/transcripts"
            ).status_code,
            404,
        )
        cross = self.b.get(
            f"/api/cloud/meetings/{a_meeting}/transcripts/{a_current['id']}"
        )
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["error"], "meeting_not_found")

        b_views = self.list_http(b_meeting, client=self.b)
        self.assertEqual(len(b_views), 1)
        self.assertEqual(b_views[0]["segments"][0]["text"], "乙方独立句子")
        self.assertNotIn(
            "甲方私密句子",
            json.dumps(b_views, ensure_ascii=False),
        )
        a_views = self.list_http(a_meeting)
        self.assertEqual(a_views[0]["segments"][0]["text"], "甲方私密句子")
        self.assertNotIn(
            "乙方独立句子",
            json.dumps(a_views, ensure_ascii=False),
        )

        # A fresh app/adapter context never leaks the other user's sentence.
        self.restart_app()
        after_restart = self.detail_http(a_meeting, a_current["id"])
        self.assertEqual(after_restart["segments"][0]["text"], "甲方私密句子")
        self.assertNotIn(
            "乙方独立句子", json.dumps(after_restart, ensure_ascii=False)
        )

    def test_retranscribe_versioning_and_stable_speakers(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=3000)
        first = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "第一版句子",
                }
            ],
        )
        self.assertTrue(
            self.run_worker(first, meeting_id, input_version, worker_id="w1")
        )
        first_view, _ = self.current_view(meeting_id)
        first_speaker = first_view["segments"][0]["speaker_id"]
        self.assertIsNotNone(first_speaker)

        self.clock.advance(seconds=1)
        response = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/re-transcribe",
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(response.status_code, 200, response.text)
        queued = response.json()
        self.assertEqual(queued["input_version"], input_version)
        self.assertEqual(queued["generation"], 1)
        self.assertEqual(queued["status"], "waiting")
        self.assertEqual(
            self.query_count("transcript_versions"), 1,
            "old versions must stay until the new publish succeeds",
        )

        second = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "第二版句子",
                }
            ],
        )
        db = self.session_factory()
        self.addCleanup(db.close)
        claimed = claim_task(
            "w2", self.clock.now(), db=db, settings=self.settings
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        item = WorkItem(
            task_id=claimed.task.id,
            owner_user_id=claimed.task.owner_user_id,
            lease_token=claimed.lease_token,
            generation=claimed.task.generation,
            input_version=claimed.task.input_version,
            meeting_id=claimed.task.meeting_id,
        )
        view = transcribe_task(
            self.context(db),
            item,
            adapter=second,
            params=PARAMS,
            workspace=Path(tempfile.mkdtemp(prefix="task10-audio-")),
        )
        self.assertTrue(self.publish(db, claimed, view))

        views = self.list_http(meeting_id)
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["id"], view.id)
        self.assertEqual(views[1]["id"], first_view["id"])
        self.assertEqual(views[1]["segments"][0]["text"], "第一版句子")
        self.assertEqual(
            self.detail_http(meeting_id, first_view["id"])["segments"][0][
                "text"
            ],
            "第一版句子",
        )
        self.assertEqual(self.meeting_pointer(meeting_id), view.id)
        second_speaker = self.detail_http(meeting_id, view.id)["segments"][0][
            "speaker_id"
        ]
        self.assertEqual(second_speaker, first_speaker)
        self.assertEqual(self.query_count("meeting_speakers"), 1)
        self.assertEqual(self.query_count("transcript_speakers"), 2)
        old_speaker = self.query_value(
            "SELECT speaker_id FROM transcript_segments"
            " WHERE version_id = :id AND ordinal = 0",
            id=first_view["id"],
        )
        self.assertEqual(old_speaker, first_speaker)

    def test_supplement_refuses_existing_and_retries_failure(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=3000)
        headers = self.csrf_headers(self.a)
        first = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/supplement",
            headers=headers,
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_task = first.json()
        self.assertEqual(first_task["status"], "waiting")
        self.assertEqual(first_task["input_version"], input_version)

        db = self.session_factory()
        self.addCleanup(db.close)
        claimed = claim_task(
            "w1", self.clock.now(), db=db, settings=self.settings
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.task.id, first_task["id"])
        self.assertTrue(
            fail_task(
                claimed.task.id,
                claimed.lease_token,
                transient=False,
                error_code="processing_failed",
                now=self.clock.now(),
                db=db,
                settings=self.settings,
            )
        )
        self.assertEqual(
            self.task_row(first_task["id"]),
            ("failed", "processing_failed", 0),
        )

        self.clock.advance(seconds=1)
        retry = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/supplement",
            headers=headers,
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        retried = retry.json()
        self.assertNotEqual(retried["id"], first_task["id"])
        self.assertEqual(retried["status"], "waiting")
        self.assertEqual(retried["generation"], 1)

        adapter = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "补转写成功",
                }
            ],
        )
        self.assertTrue(
            self.run_worker(adapter, meeting_id, input_version, worker_id="w2")
        )
        current, _ = self.current_view(meeting_id)
        self.assertEqual(current["segments"][0]["text"], "补转写成功")

        refused = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/supplement",
            headers=headers,
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(
            refused.json()["error"], "transcript_already_exists"
        )

    def test_re_transcribe_requires_existing(self) -> None:
        meeting_id, input_version = self.sealed_media(duration_ms=3000)
        headers = self.csrf_headers(self.a)
        missing = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/re-transcribe",
            headers=headers,
        )
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["error"], "transcript_not_found")

        adapter = FakeAdapter(
            meeting_id,
            [
                {
                    "track": "mic",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "可重转写稿件",
                }
            ],
        )
        self.assertTrue(
            self.run_worker(adapter, meeting_id, input_version, worker_id="w1")
        )
        self.clock.advance(seconds=1)
        response = self.a.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/re-transcribe",
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "waiting")
        self.assertEqual(response.json()["input_version"], input_version)

        # Another user cannot start work on this meeting.
        foreign = self.b.post(
            f"/api/cloud/meetings/{meeting_id}/transcripts/re-transcribe",
            headers=self.csrf_headers(self.b),
        )
        self.assertEqual(foreign.status_code, 404)


class ContractParamsCase(unittest.TestCase):
    """Model contract locking; no database or object store needed."""

    def contract(self) -> dict[str, Any]:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_params_locked_from_contract(self) -> None:
        contract = self.contract()
        params = load_params(contract)
        proposed = contract["proposed_parameters"]
        self.assertEqual(
            params.dedup_window_ms, proposed["dedup_window_ms"]["value"]
        )
        self.assertEqual(
            params.dedup_min_text_similarity,
            proposed["dedup_min_text_similarity"]["value"],
        )
        self.assertEqual(
            params.cluster_similarity_threshold,
            proposed["cluster_similarity_threshold"]["value"],
        )
        self.assertEqual(
            params.max_identities, proposed["max_identities"]["value"]
        )
        self.assertEqual(params.language, "zh-cn")
        self.assertIsNone(params.strategy)
        self.assertEqual(
            params.contract_status, "proposed_pending_measurement"
        )
        with self.assertRaises(ModelNotConfigured):
            require_selected(params)
        with self.assertRaises(ModelNotConfigured):
            build_adapter(contract, {})

    def test_contract_values_are_validated(self) -> None:
        contract = self.contract()
        broken = copy.deepcopy(contract)
        broken["proposed_parameters"]["dedup_min_text_similarity"][
            "value"
        ] = 1.5
        with self.assertRaises(ValueError):
            load_params(broken)

    def test_build_adapter_requires_approved_evidence(self) -> None:
        contract = self.contract()
        approved = copy.deepcopy(contract)
        approved["recognition"] = {
            "strategy": "approved",
            "approved_strategy": "separate_merge",
            "evidence": {
                "metrics": {"der": 0.1},
                "corpus": {"id": "synthetic-corpus"},
                "engines": {
                    "asr": {
                        "name": "fake-asr",
                        "version": "1.0",
                        "weights_sha256": "0" * 64,
                    }
                },
                "calibration": {"version": "cal-1"},
            },
        }
        params = load_params(approved)
        self.assertEqual(params.strategy, "separate_merge")
        self.assertEqual(params.contract_status, "approved")

        class Asr:
            def transcribe(self, audio: Any, speakers: int | None) -> Any:
                return ("asr", audio, speakers)

        class Speaker:
            def embed(self, samples: Any) -> Any:
                return ("embed", samples)

        adapter = build_adapter(approved, {"asr": Asr(), "speaker_embedding": Speaker()})
        self.assertEqual(adapter.transcribe("audio", 2), ("asr", "audio", 2))
        self.assertEqual(adapter.embed([0.1]), ("embed", [0.1]))

        incomplete = copy.deepcopy(approved)
        incomplete["recognition"]["evidence"]["engines"] = {}
        with self.assertRaises(ModelNotConfigured):
            build_adapter(incomplete, {})


if __name__ == "__main__":
    unittest.main()
