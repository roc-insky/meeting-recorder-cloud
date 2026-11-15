"""Task 9: owner-scoped decoding and authenticated range playback.

The decoder is a trust boundary: it reads chunks the server ACKed, but a bug or
a hostile row must never turn into a path escape, an unbounded subprocess or a
whole-file read. The playback route is the other boundary: only the owner may
read, a single byte range must be honoured exactly, and the current recording
is never playable while it is still open.

Fixtures are real 48 kHz stereo Opus/WebM produced by ffmpeg 9.0.2 and split at
byte boundaries, matching the P0 capture contract (chunk 0 carries the container
header; only the ordered concatenation decodes). No claim about audio quality or
intelligibility is made here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from typing import Any
from unittest import mock

from sqlalchemy import text

from cloud.audio import MAX_INPUT_BYTES, decode_segment, prepare_audio
from cloud.contracts import (
    AudioInput,
    AuthContext,
    CloudError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PayloadTooLargeError,
)
from cloud.objects import LocalObjectStore, StorageFullError
from tests.cloud.support import CloudCase

CODEC = "audio/webm;codecs=opus"
MIC = {"id": "mic", "source": "mic", "codec": CODEC, "sample_rate": 48000}
SYSTEM = {"id": "system", "source": "system", "codec": CODEC, "sample_rate": 48000}
STARTED_AT = "2026-11-14T07:00:00Z"
WORK_RATE = 16000
EPOCH_MS = 30000
CHUNK_MS = 10000
CHUNKS_PER_TRACK = 3


class _RangeOnlyStore:
    """Delegating store that fails if playback ever reads a whole object."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.ranges: list[tuple[int, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def get_bytes(self, key: str) -> bytes:
        raise AssertionError("playback must not load the whole object into memory")

    def iter_range(self, key: str, start: int, length: int) -> Any:
        self.ranges.append((start, length))
        return self.inner.iter_range(key, start, length)


class AudioCase(CloudCase):
    # -- fixtures ---------------------------------------------------------

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixtures = Path(tempfile.mkdtemp(prefix="cloud-audio-fixtures-"))
        cls.webm_low = cls._encode(cls.fixtures / "low.webm", frequency=440)
        cls.webm_high = cls._encode(cls.fixtures / "high.webm", frequency=880)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixtures, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def _encode(cls, path: Path, *, frequency: int, seconds: int = EPOCH_MS // 1000) -> bytes:
        # 48 kHz stereo on purpose: the work asset must be 16 kHz, the source
        # must stay 48 kHz and byte-identical.
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
                "-ac",
                "2",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                str(path),
            ],
            check=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        body = path.read_bytes()
        if not body:
            raise AssertionError("ffmpeg produced an empty fixture")
        return body

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="cloud-audio-work-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- API helpers ------------------------------------------------------

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

    def open_upload(self, client: Any, mode: str = "mic") -> tuple[str, str, dict[str, str]]:
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
        return meeting_id, response.json()["id"], {"X-Capture-Lease": token}

    def register_epoch(
        self,
        client: Any,
        upload_id: str,
        headers: dict[str, str],
        epoch_id: str,
        *,
        offset_ms: int,
        tracks: list[dict[str, Any]],
    ) -> None:
        self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/epochs",
                json={
                    "id": epoch_id,
                    "offset_ms": offset_ms,
                    "uncertainty_ms": 0,
                    "started_at": STARTED_AT,
                    "tracks": tracks,
                },
                headers={**self.csrf_headers(client), **headers},
            )
        )

    def put_track(
        self,
        client: Any,
        upload_id: str,
        headers: dict[str, str],
        epoch_id: str,
        track_id: str,
        body: bytes,
        *,
        start_ms: int,
    ) -> None:
        step = len(body) // CHUNKS_PER_TRACK
        for index in range(CHUNKS_PER_TRACK):
            piece = (
                body[index * step :]
                if index == CHUNKS_PER_TRACK - 1
                else body[index * step : (index + 1) * step]
            )
            seq_start = start_ms + index * CHUNK_MS
            self.expect(
                client.put(
                    f"/api/cloud/uploads/{upload_id}/chunks/{epoch_id}/{track_id}/{index}",
                    params={
                        "start_ms": seq_start,
                        "end_ms": seq_start + CHUNK_MS,
                        "checksum": hashlib.sha256(piece).hexdigest(),
                        "codec": CODEC,
                    },
                    content=piece,
                    headers={**self.csrf_headers(client), **headers},
                ),
                code=None,
            )

    def seal(
        self,
        client: Any,
        upload_id: str,
        epochs: list[tuple[str, list[str], int]],
        *,
        accept_partial: bool = False,
    ) -> int:
        manifest = [
            {
                "capture_epoch_id": epoch_id,
                "track_id": track_id,
                "last_sequence": CHUNKS_PER_TRACK - 1,
                "end_ms": end_ms,
            }
            for epoch_id, track_ids, end_ms in epochs
            for track_id in track_ids
        ]
        response = self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/seal",
                json={"tracks": manifest, "accept_partial": accept_partial},
                headers=self.csrf_headers(client),
            )
        )
        return int(response.json()["input_version"])

    def seal_manifest(
        self,
        client: Any,
        upload_id: str,
        tracks: list[dict[str, Any]],
        *,
        accept_partial: bool = False,
    ) -> int:
        response = self.expect(
            client.post(
                f"/api/cloud/uploads/{upload_id}/seal",
                json={"tracks": tracks, "accept_partial": accept_partial},
                headers=self.csrf_headers(client),
            )
        )
        return int(response.json()["input_version"])

    def record_two_epochs(self, client: Any) -> tuple[str, int]:
        """One mic track recorded 0-30 s, interrupted, resumed at 60-90 s."""
        meeting_id, upload_id, headers = self.open_upload(client)
        self.register_epoch(
            client, upload_id, headers, "e1", offset_ms=0, tracks=[dict(MIC)]
        )
        self.put_track(
            client, upload_id, headers, "e1", "mic", self.webm_low, start_ms=0
        )
        self.register_epoch(
            client, upload_id, headers, "e2", offset_ms=60000, tracks=[dict(MIC)]
        )
        self.put_track(
            client, upload_id, headers, "e2", "mic", self.webm_high, start_ms=60000
        )
        version = self.seal(
            client,
            upload_id,
            [("e1", ["mic"], 30000), ("e2", ["mic"], 90000)],
        )
        return meeting_id, version

    def record_one_epoch(self, client: Any, *, mode: str = "mic") -> tuple[str, int]:
        meeting_id, upload_id, headers = self.open_upload(client, mode=mode)
        self.register_epoch(
            client, upload_id, headers, "e1", offset_ms=0, tracks=[dict(MIC)]
        )
        self.put_track(
            client, upload_id, headers, "e1", "mic", self.webm_low, start_ms=0
        )
        version = self.seal(client, upload_id, [("e1", ["mic"], 30000)])
        return meeting_id, version

    def prepare(
        self, client_user_id: str, meeting_id: str, version: int
    ) -> tuple[AudioInput, Path]:
        workspace = Path(tempfile.mkdtemp(prefix="audio-call-", dir=self.tmp))
        self.addCleanup(shutil.rmtree, workspace, True)
        session = self.session_factory()
        self.addCleanup(session.close)
        ctx = AuthContext(
            user_id=client_user_id,
            session_id="test-session",
            db=session,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )
        return prepare_audio(ctx, meeting_id, version, workspace), workspace

    # -- small assertions -------------------------------------------------

    def read_wav(self, path: Path) -> tuple[int, int, int, int, bytes]:
        with wave.open(str(path), "rb") as reader:
            return (
                reader.getframerate(),
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getnframes(),
                reader.readframes(reader.getnframes()),
            )

    def chunk_keys(self) -> list[str]:
        rows = self.query_value(
            "SELECT array_agg(object_key ORDER BY epoch_id, track_id, sequence) "
            "FROM chunks"
        )
        return list(rows or [])

    # -- required Task 9 cases -------------------------------------------

    def test_preserves_48k_source_and_gap_timeline(self) -> None:
        meeting_id, version = self.record_two_epochs(self.a)
        sources = {key: self.object_store.get_bytes(key) for key in self.chunk_keys()}
        self.assertEqual(len(sources), 6)

        audio_input, _ = self.prepare(self.a_user_id, meeting_id, version)

        self.assertEqual(audio_input.rate, WORK_RATE)
        self.assertEqual(set(audio_input.tracks), {"mic"})
        self.assertTrue(audio_input.mix_path.is_file())
        self.assertTrue(audio_input.tracks["mic"].is_file())

        # The gap is invisible in the bytes but explicit in the contract.
        spans = [
            (gap.track_id, gap.start_ms, gap.end_ms, gap.reason)
            for gap in audio_input.gaps
        ]
        self.assertIn(("mic", 30000, 60000, "capture_interruption"), spans)

        # 90 s timeline: 30 s audio, 30 s explicit silence, 30 s audio.
        for path in (audio_input.mix_path, audio_input.tracks["mic"]):
            rate, channels, width, frames, _ = self.read_wav(path)
            self.assertEqual((rate, channels, width), (WORK_RATE, 1, 2))
            self.assertEqual(frames, 90 * WORK_RATE)

        _, _, _, _, mix = self.read_wav(audio_input.mix_path)
        frame = WORK_RATE * 2
        self.assertTrue(any(mix[1 * frame : 2 * frame]), "first epoch is silent")
        self.assertFalse(any(mix[35 * frame : 55 * frame]), "gap is not silent")
        self.assertTrue(any(mix[65 * frame : 67 * frame]), "second epoch is silent")

        # Sources were never mutated or relabelled: same bytes, still 48 kHz.
        for key, before in sources.items():
            self.assertEqual(self.object_store.get_bytes(key), before, key)
        combined = Path(tempfile.mkdtemp(dir=self.tmp)) / "epoch1.webm"
        combined.write_bytes(b"".join(sources[key] for key in self.chunk_keys()[:3]))
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,channels",
                "-of",
                "json",
                str(combined),
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual((stream["sample_rate"], stream["channels"]), ("48000", 2))

        # Durable derived assets: one mix plus one track for this input version.
        self.assertEqual(self.query_count("derived_assets"), 2)
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM derived_assets WHERE duration_ms = 90000"
            ),
            2,
        )

        # A second prepare reuses the frozen version instead of re-encoding.
        again, _ = self.prepare(self.a_user_id, meeting_id, version)
        self.assertEqual(self.query_count("derived_assets"), 2)
        self.assertEqual(again.mix_path.read_bytes(), audio_input.mix_path.read_bytes())
        self.assertEqual(
            [(gap.start_ms, gap.end_ms) for gap in again.gaps],
            [(gap.start_ms, gap.end_ms) for gap in audio_input.gaps],
        )

    def test_mixes_both_tracks_on_one_timeline(self) -> None:
        meeting_id, upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(
            self.a,
            upload_id,
            headers,
            "e1",
            offset_ms=0,
            tracks=[dict(MIC), dict(SYSTEM)],
        )
        self.put_track(
            self.a, upload_id, headers, "e1", "mic", self.webm_low, start_ms=0
        )
        self.put_track(
            self.a, upload_id, headers, "e1", "system", self.webm_high, start_ms=0
        )
        version = self.seal(self.a, upload_id, [("e1", ["mic", "system"], 30000)])

        prepared, workspace = self.prepare(self.a_user_id, meeting_id, version)
        self.assertEqual(set(prepared.tracks), {"mic", "system"})
        self.assertEqual(prepared.gaps, [])
        self.assertEqual(self.query_count("derived_assets"), 3)

        rate, channels, width, frames, mix = self.read_wav(prepared.mix_path)
        self.assertEqual((rate, channels, width), (WORK_RATE, 1, 2))
        self.assertEqual(frames, 30 * WORK_RATE)
        mic = self.read_wav(prepared.tracks["mic"])[4]
        system = self.read_wav(prepared.tracks["system"])[4]
        self.assertEqual(len(mic), frames * 2)
        self.assertEqual(len(system), frames * 2)
        # The two tracks carry different tones, and the mix is their sum.
        self.assertNotEqual(mic, system)
        self.assertNotEqual(mix, mic)
        self.assertNotEqual(mix, system)
        self.assertEqual(sorted(p.name for p in workspace.iterdir() if p.is_file()),
                         sorted([prepared.mix_path.name] + [
                             path.name for path in prepared.tracks.values()
                         ]))

    def test_pads_timeline_to_explicit_tail_gap(self) -> None:
        meeting_id, upload_id, headers = self.open_upload(self.a, mode="dual")
        self.register_epoch(
            self.a,
            upload_id,
            headers,
            "e1",
            offset_ms=0,
            tracks=[dict(MIC), dict(SYSTEM)],
        )
        self.put_track(
            self.a, upload_id, headers, "e1", "mic", self.webm_low, start_ms=0
        )
        # The client claims 30 s of mic missing chunks and a track that never
        # produced any chunk; both become durable seal gaps out to 60 s.
        version = self.seal_manifest(
            self.a,
            upload_id,
            [
                {
                    "capture_epoch_id": "e1",
                    "track_id": "mic",
                    "last_sequence": 3,
                    "end_ms": 60000,
                },
                {
                    "capture_epoch_id": "e1",
                    "track_id": "system",
                    "last_sequence": -1,
                    "end_ms": 0,
                },
            ],
            accept_partial=True,
        )

        prepared, _ = self.prepare(self.a_user_id, meeting_id, version)
        rate, channels, width, frames, _ = self.read_wav(prepared.mix_path)
        self.assertEqual((rate, channels, width), (WORK_RATE, 1, 2))
        self.assertEqual(frames, 60 * WORK_RATE)
        self.assertEqual(set(prepared.tracks), {"mic"})
        self.assertEqual(self.read_wav(prepared.tracks["mic"])[3], 60 * WORK_RATE)

        tail = [gap for gap in prepared.gaps if gap.track_id == "system"]
        self.assertEqual(
            [(gap.start_ms, gap.end_ms, gap.reason) for gap in tail],
            [(0, 60000, "missing_track")],
        )

    def test_private_range(self) -> None:
        meeting_id, version = self.record_one_epoch(self.a)

        # A sealed recording with no derived mix is not playable yet.
        self.expect(
            self.a.get(f"/api/cloud/meetings/{meeting_id}/audio"), 404, "audio_not_ready"
        )

        audio_input, _ = self.prepare(self.a_user_id, meeting_id, version)
        body = audio_input.mix_path.read_bytes()
        total = len(body)
        _, _, _, frames, _ = self.read_wav(audio_input.mix_path)
        self.assertEqual(frames, 30 * WORK_RATE)
        self.assertGreater(total, 44)
        url = f"/api/cloud/meetings/{meeting_id}/audio"

        full = self.expect(self.a.get(url))
        self.assertEqual(full.content, body)
        self.assertEqual(full.headers["content-type"], "audio/wav")
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["cache-control"], "private, no-store")
        self.assertEqual(full.headers["content-length"], str(total))

        partial = self.expect(self.a.get(url, headers={"Range": "bytes=100-199"}), 206)
        self.assertEqual(partial.content, body[100:200])
        self.assertEqual(partial.headers["content-range"], f"bytes 100-199/{total}")
        self.assertEqual(partial.headers["content-length"], "100")
        self.assertEqual(partial.headers["cache-control"], "private, no-store")

        tail = self.expect(self.a.get(url, headers={"Range": f"bytes={total - 50}-"}), 206)
        self.assertEqual(tail.content, body[-50:])

        suffix = self.expect(self.a.get(url, headers={"Range": "bytes=-50"}), 206)
        self.assertEqual(suffix.content, body[-50:])
        self.assertEqual(suffix.headers["content-range"], f"bytes {total - 50}-{total - 1}/{total}")

        for header in (
            "bytes=abc",
            "bytes=10-5",
            "bytes=0-1,5-6",
            f"bytes={total}-",
            "bytes=",
        ):
            refused = self.a.get(url, headers={"Range": header})
            self.assertEqual(refused.status_code, 416, header)
            self.assertEqual(refused.headers["content-range"], f"bytes */{total}")
            self.assertEqual(refused.headers["accept-ranges"], "bytes")

        # Only the owner can read; a foreign id is indistinguishable from none.
        self.expect(self.anon.get(url), 401, "authentication_required")
        self.expect(self.b.get(url), 404, "meeting_not_found")

        # An open recording is never playable (no self re-recording).
        open_meeting, _, _ = self.open_upload(self.a)
        self.expect(
            self.a.get(f"/api/cloud/meetings/{open_meeting}/audio"),
            409,
            "recording_in_progress",
        )

        # Range serving must stream, never load the whole object per request.
        ranged = _RangeOnlyStore(self.object_store)
        client = self.app_client(object_store=ranged)
        self.expect(client.get(url))
        self.expect(client.get(url, headers={"Range": "bytes=10-19"}), 206)
        self.assertEqual(ranged.ranges, [(0, total), (10, 10)])

    def test_decoder_rejects_path_and_resource_bombs(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="decode-", dir=self.tmp))
        source_dir = workspace / "in"
        out_dir = workspace / "out"
        source_dir.mkdir()
        out_dir.mkdir()
        source = source_dir / "epoch.webm"
        source.write_bytes(self.webm_low)
        destination = out_dir / "epoch.pcm"

        with mock.patch(
            "cloud.audio.subprocess.Popen", wraps=subprocess.Popen
        ) as popen:
            decode_segment(
                source,
                destination,
                workspace=workspace,
                max_seconds=1.0,
                timeout_s=60,
                max_bytes=10 * 1024 * 1024,
            )
        argv = popen.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(all(isinstance(part, str) for part in argv))
        self.assertIn(str(source), argv)
        self.assertIn(str(workspace.resolve()), popen.call_args.kwargs["cwd"])
        # A duration bomb is capped, not decoded in full: 1 s at 16 kHz mono s16.
        self.assertLessEqual(destination.stat().st_size, WORK_RATE * 2 * 1.1)

        outside = Path(tempfile.mkdtemp(prefix="outside-", dir=self.tmp)) / "secret"
        outside.write_bytes(self.webm_low)
        link = source_dir / "link.webm"
        link.symlink_to(outside)
        unsafe = [
            outside,
            Path("/etc/passwd"),
            source_dir / ".." / ".." / "etc" / "passwd",
            source_dir,
            link,
            workspace / ".." / "escape.pcm",
            "/tmp/escape.pcm",
        ]
        for index, candidate in enumerate(unsafe):
            with self.subTest(candidate=str(candidate)):
                with self.assertRaises(InvalidRequestError) as caught:
                    decode_segment(
                        candidate,
                        out_dir / f"bad{index}.pcm",
                        workspace=workspace,
                        max_seconds=1.0,
                        timeout_s=60,
                        max_bytes=10 * 1024 * 1024,
                    )
                self.assertEqual(caught.exception.code, "unsafe_audio_path")

        with self.assertRaises(InvalidRequestError) as caught:
            decode_segment(
                source,
                outside,
                workspace=workspace,
                max_seconds=1.0,
                timeout_s=60,
                max_bytes=10 * 1024 * 1024,
            )
        self.assertEqual(caught.exception.code, "unsafe_audio_path")

    def test_sequence_hole_is_rejected(self) -> None:
        meeting_id, version = self.record_one_epoch(self.a)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM chunks WHERE sequence = 1"))

        workspace = Path(tempfile.mkdtemp(prefix="hole-", dir=self.tmp))
        session = self.session_factory()
        self.addCleanup(session.close)
        ctx = AuthContext(
            user_id=self.a_user_id,
            session_id="test-session",
            db=session,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )
        with self.assertRaises(ConflictError) as caught:
            prepare_audio(ctx, meeting_id, version, workspace)
        self.assertEqual(caught.exception.code, "audio_sequence_gap")
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(self.query_count("derived_assets"), 0)

    def test_foreign_owner_cannot_prepare(self) -> None:
        meeting_id, version = self.record_one_epoch(self.a)
        with self.assertRaises(NotFoundError) as caught:
            self.prepare(self.b_user_id, meeting_id, version)
        self.assertEqual(caught.exception.code, "meeting_not_found")
        self.assertEqual(self.query_count("derived_assets"), 0)

    def test_cleanup_after_decode_failure(self) -> None:
        meeting_id, upload_id, headers = self.open_upload(self.a)
        self.register_epoch(
            self.a, upload_id, headers, "e1", offset_ms=0, tracks=[dict(MIC)]
        )
        garbage = b"\x00\x01\x02not-a-container" * 64
        for index in range(CHUNKS_PER_TRACK):
            self.expect(
                self.a.put(
                    f"/api/cloud/uploads/{upload_id}/chunks/e1/mic/{index}",
                    params={
                        "start_ms": index * 1000,
                        "end_ms": (index + 1) * 1000,
                        "checksum": hashlib.sha256(garbage).hexdigest(),
                        "codec": CODEC,
                    },
                    content=garbage,
                    headers={**self.csrf_headers(self.a), **headers},
                )
            )
        version = self.seal(self.a, upload_id, [("e1", ["mic"], 3000)])

        workspace = Path(tempfile.mkdtemp(prefix="broken-", dir=self.tmp))
        session = self.session_factory()
        self.addCleanup(session.close)
        ctx = AuthContext(
            user_id=self.a_user_id,
            session_id="test-session",
            db=session,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )
        with self.assertRaises(CloudError) as caught:
            prepare_audio(ctx, meeting_id, version, workspace)
        self.assertEqual(caught.exception.code, "audio_decode_failed")
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(self.query_count("derived_assets"), 0)

    def test_oversized_input_is_rejected_before_spawn(self) -> None:
        meeting_id, version = self.record_one_epoch(self.a)
        workspace = Path(tempfile.mkdtemp(prefix="bomb-", dir=self.tmp))
        session = self.session_factory()
        self.addCleanup(session.close)
        ctx = AuthContext(
            user_id=self.a_user_id,
            session_id="test-session",
            db=session,
            clock=self.clock,
            settings=self.settings,
            objects=self.object_store,
        )
        with mock.patch("cloud.audio.MAX_INPUT_BYTES", 10), mock.patch(
            "cloud.audio.subprocess.Popen", side_effect=AssertionError("spawned")
        ):
            with self.assertRaises(PayloadTooLargeError) as caught:
                prepare_audio(ctx, meeting_id, version, workspace)
        self.assertEqual(caught.exception.code, "audio_input_too_large")
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(self.query_count("derived_assets"), 0)
        self.assertGreater(MAX_INPUT_BYTES, 10)

    def test_preflight_rejects_below_estimate(self) -> None:
        import cloud.audio as audio_mod

        plan = audio_mod._Plan(
            spans=[(0, 1000)],
            gaps=[],
            duration_ms=1000,
            track_ids=["mic"],
            input_bytes=1024,
        )
        store = LocalObjectStore(
            Path(tempfile.mkdtemp(prefix="audio-space-")), min_free_bytes=1 << 62
        )
        self.addCleanup(shutil.rmtree, store.root, True)
        with self.assertRaises(StorageFullError) as raised:
            audio_mod._require_build_space(store, plan)
        self.assertEqual(raised.exception.code, "storage_full")
        self.assertEqual(raised.exception.status_code, 507)
        self.assertIn("audio_build_needs_space", str(raised.exception))

    def test_preflight_counts_the_store_copy_and_the_reserve(self) -> None:
        import cloud.audio as audio_mod

        plan = audio_mod._Plan(
            spans=[(0, 1000)],
            gaps=[],
            duration_ms=1000,
            track_ids=["mic"],
            input_bytes=1024,
        )
        estimate = audio_mod._estimate_build_bytes(plan)

        class FakeStore:
            min_free_bytes = 10

            def __init__(self, free: int) -> None:
                self.free = free

            def available_bytes(self) -> int:
                return self.free

        with self.assertRaises(StorageFullError):
            audio_mod._require_build_space(FakeStore(estimate + 9), plan)
        audio_mod._require_build_space(FakeStore(estimate + 10), plan)

        class NoProbe:
            pass

        class NotCallable:
            available_bytes = 123

        audio_mod._require_build_space(NoProbe(), plan)
        audio_mod._require_build_space(NotCallable(), plan)
        self.assertGreater(audio_mod._estimate_build_bytes(plan), plan.input_bytes)

    def test_unknown_or_open_input_is_not_preparable(self) -> None:
        meeting_id, version = self.record_one_epoch(self.a)
        with self.assertRaises(ConflictError) as caught:
            self.prepare(self.a_user_id, meeting_id, version + 1)
        self.assertEqual(caught.exception.code, "input_not_sealed")


if __name__ == "__main__":
    unittest.main()
