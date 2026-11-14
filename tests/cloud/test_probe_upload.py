"""Task 2 upload-continuity probe tests.

Synthetic PCM/container bytes only. No real recording, no ffmpeg, no network
beyond a loopback probe server bound to an ephemeral port.
"""

from __future__ import annotations

import http.client
import json
import math
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from spike.cloud_capture.probe_server import (
    HeaderDependencyError,
    SequenceError,
    pcm_duration_ms,
    reconstruct_probe,
    create_probe_server,
)

SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2
CHUNK_MS = 500


def _tone_pcm(freq_hz: int, duration_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    out = bytearray()
    for index in range(frames):
        value = int(0.3 * 32767 * math.sin(2 * math.pi * freq_hz * index / sample_rate))
        out += struct.pack("<h", value)
    return bytes(out)


class ProbeUploadCase(unittest.TestCase):
    def setUp(self) -> None:
        self.pcm_chunks = [_tone_pcm(440 + 110 * index, CHUNK_MS) for index in range(3)]
        self.pcm_stream = b"".join(self.pcm_chunks)

    def test_reordered_stream_reconstructs(self) -> None:
        received = [
            (2, self.pcm_chunks[2]),
            (0, self.pcm_chunks[0]),
            (1, self.pcm_chunks[1]),
        ]

        reconstructed = reconstruct_probe(received, "pcm_s16le")

        self.assertEqual(reconstructed, self.pcm_stream)
        self.assertAlmostEqual(
            pcm_duration_ms(reconstructed),
            pcm_duration_ms(self.pcm_stream),
            delta=1,
        )
        self.assertAlmostEqual(pcm_duration_ms(reconstructed), 1500, delta=1)

    def test_ordered_pcm_chunks_are_independent(self) -> None:
        reconstructed = reconstruct_probe(self.pcm_chunks, "pcm_s16le")
        self.assertEqual(reconstructed, self.pcm_stream)
        self.assertAlmostEqual(pcm_duration_ms(reconstructed), 1500, delta=1)

    def test_container_chunks_are_header_dependent(self) -> None:
        header_chunk = b"\x1a\x45\xdf\xa3" + b"webm-header"
        cluster_chunk = b"\x1f\x43\xb6\x75" + b"cluster-payload"

        self.assertEqual(
            reconstruct_probe([(1, cluster_chunk), (0, header_chunk)], "audio/webm;codecs=opus"),
            header_chunk + cluster_chunk,
        )

        with self.assertRaises(HeaderDependencyError) as raised:
            reconstruct_probe([cluster_chunk], "audio/webm;codecs=opus")
        self.assertTrue(raised.exception.dependent)
        self.assertIn("dependent", str(raised.exception))

    def test_single_container_chunk_without_header_is_marked_dependent(self) -> None:
        cluster_only = b"\x1f\x43\xb6\x75only-cluster"
        with self.assertRaises(HeaderDependencyError) as raised:
            reconstruct_probe([(0, cluster_only)], "audio/webm")
        self.assertTrue(raised.exception.dependent)

    def test_missing_sequence_is_rejected(self) -> None:
        with self.assertRaises(SequenceError) as raised:
            reconstruct_probe(
                [(0, self.pcm_chunks[0]), (2, self.pcm_chunks[2])],
                "pcm_s16le",
            )
        self.assertIn("1", str(raised.exception))

    def test_duplicate_sequence_is_rejected(self) -> None:
        with self.assertRaises(SequenceError):
            reconstruct_probe(
                [(0, self.pcm_chunks[0]), (0, self.pcm_chunks[1])],
                "pcm_s16le",
            )

    def test_empty_stream_requires_sequence_zero(self) -> None:
        with self.assertRaises(SequenceError):
            reconstruct_probe([(1, self.pcm_chunks[1])], "pcm_s16le")


class ProbeServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = create_probe_server(
            root=Path(__file__).resolve().parents[2] / "spike" / "cloud_capture",
            out_dir=Path(self.tmp.name) / "chunks",
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _request(self, method: str, path: str, body: bytes | None = None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()

    def _upload(self, sequence: int, data: bytes, codec: str = "pcm_s16le") -> dict:
        status, body, _ = self._request(
            "POST",
            f"/probe/upload?track=mic&sequence={sequence}&codec={codec}",
            data,
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_probe_server_reassembles_out_of_order(self) -> None:
        chunks = [_tone_pcm(440 + 110 * index, CHUNK_MS) for index in range(3)]
        for sequence in (2, 0, 1):
            stored = self._upload(sequence, chunks[sequence])
            self.assertEqual(stored["sequence"], sequence)
            self.assertEqual(stored["bytes"], len(chunks[sequence]))

        status, body, _ = self._request(
            "GET", "/probe/status?track=mic"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sequences"], [0, 1, 2])

        status, body, _ = self._request(
            "GET", "/probe/reconstruct?track=mic&codec=pcm_s16le"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"".join(chunks))

    def test_probe_server_rejects_missing_sequence(self) -> None:
        self._upload(2, b"tail")
        status, body, _ = self._request(
            "GET", "/probe/reconstruct?track=mic&codec=pcm_s16le"
        )
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "sequence_gap")

    def test_probe_server_marks_header_dependent_container(self) -> None:
        self._upload(0, b"cluster-without-header", codec="audio/webm")
        status, body, _ = self._request(
            "GET", "/probe/reconstruct?track=mic&codec=audio/webm"
        )
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "header_dependency")
        self.assertTrue(payload["dependent"])

    def test_probe_server_serves_spike_page(self) -> None:
        status, body, content_type = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"cloud capture probe", body.lower())


if __name__ == "__main__":
    unittest.main()
