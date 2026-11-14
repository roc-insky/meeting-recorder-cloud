"""Local upload-continuity probe for the cloud meeting recorder spike.

The probe accepts synthetic audio chunks over loopback, stores them by
sequence number, and proves two things the design depends on:

* chunks may arrive out of order and can still be reassembled;
* a container chunk is not always independently decodable — WebM/Ogg chunks
  after the first one depend on a header that arrived earlier.

No production API is defined here. No real recording is ever uploaded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_SEQUENCE = 10_000_000
_TRACK_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class SequenceError(ValueError):
    """A chunk sequence is missing a number, duplicated, or not zero-based."""


class HeaderDependencyError(ValueError):
    """A container chunk cannot be decoded without an earlier header chunk."""

    def __init__(self, message: str, *, dependent: bool = True) -> None:
        super().__init__(message)
        self.dependent = dependent


def _container_magic(codec: str) -> Optional[bytes]:
    lowered = (codec or "").lower()
    if "webm" in lowered or "matroska" in lowered:
        return b"\x1a\x45\xdf\xa3"
    if "ogg" in lowered:
        return b"OggS"
    return None


def _looks_like_mp4(codec: str) -> bool:
    lowered = (codec or "").lower()
    return "mp4" in lowered or "m4a" in lowered or "iso5" in lowered


def _has_container_header(codec: str, data: bytes) -> bool:
    magic = _container_magic(codec)
    if magic is not None:
        return data.startswith(magic)
    if _looks_like_mp4(codec):
        return len(data) >= 8 and data[4:8] == b"ftyp"
    return True


def _header_is_required(codec: str) -> bool:
    return _container_magic(codec) is not None or _looks_like_mp4(codec)


def _missing_sequences(sequences: Sequence[int]) -> List[int]:
    if not sequences:
        return []
    highest = max(sequences)
    present = set(sequences)
    return [value for value in range(highest + 1) if value not in present]


def _normalise_chunks(
    chunks: Iterable[object],
) -> Tuple[List[int], List[bytes]]:
    """Accept [(sequence, bytes), ...] or plain [bytes, ...] (implicit order)."""
    entries = list(chunks)
    if not entries:
        return [], []

    if all(
        isinstance(entry, (tuple, list)) and len(entry) == 2 for entry in entries
    ):
        pairs: List[Tuple[int, bytes]] = []
        for entry in entries:
            sequence, data = entry  # type: ignore[misc]
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise SequenceError(f"sequence must be an integer, got {sequence!r}")
            if sequence < 0:
                raise SequenceError(f"sequence must be >= 0, got {sequence}")
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError("chunk payload must be bytes")
            pairs.append((sequence, bytes(data)))
        sequences = [pair[0] for pair in pairs]
        if len(set(sequences)) != len(sequences):
            duplicates = sorted(
                {value for value in sequences if sequences.count(value) > 1}
            )
            raise SequenceError(f"duplicate sequence numbers: {duplicates}")
        pairs.sort(key=lambda pair: pair[0])
        sequences = [pair[0] for pair in pairs]
        datas = [pair[1] for pair in pairs]
    elif all(
        isinstance(entry, (bytes, bytearray, memoryview)) for entry in entries
    ):
        sequences = list(range(len(entries)))
        datas = [bytes(entry) for entry in entries]  # type: ignore[arg-type]
    else:
        raise TypeError(
            "chunks must be bytes or (sequence, bytes) pairs, not a mix"
        )

    missing = _missing_sequences(sequences)
    if missing:
        raise SequenceError(
            f"sequence gap: missing {missing} (received {sequences})"
        )
    return sequences, datas


def reconstruct_probe(chunks: Iterable[object], codec: str = "") -> bytes:
    """Reassemble chunks in sequence order.

    Raises SequenceError for gaps/duplicates and HeaderDependencyError when the
    first chunk does not begin with the container header it needs.
    """
    _sequences, datas = _normalise_chunks(chunks)
    if not datas:
        return b""
    if _header_is_required(codec) and not _has_container_header(codec, datas[0]):
        raise HeaderDependencyError(
            f"first chunk is not a {codec or 'container'} header; this blob is "
            "dependent on an earlier header chunk and cannot be decoded alone",
            dependent=True,
        )
    return b"".join(datas)


def pcm_duration_ms(
    data: bytes,
    sample_rate: int = 48_000,
    sample_width: int = 2,
    channels: int = 1,
) -> float:
    """Duration of raw interleaved PCM, in milliseconds."""
    frame_bytes = sample_width * channels
    if frame_bytes <= 0 or sample_rate <= 0:
        raise ValueError("sample rate, sample width, and channels must be positive")
    return (len(data) / frame_bytes) / sample_rate * 1000.0


def _safe_track(track: str) -> str:
    if not track or not _TRACK_RE.fullmatch(track):
        raise ValueError("track must match [A-Za-z0-9_-]{1,64}")
    return track


class ChunkStore:
    """Chunk files on disk, keyed by track and sequence."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _track_dir(self, track: str) -> Path:
        directory = self.root / _safe_track(track)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _chunk_path(self, track: str, sequence: int) -> Path:
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("sequence must be an integer")
        if sequence < 0 or sequence > MAX_SEQUENCE:
            raise ValueError(f"sequence must be between 0 and {MAX_SEQUENCE}")
        return self._track_dir(track) / f"{sequence:08d}.chunk"

    def put(
        self, track: str, sequence: int, data: bytes, codec: str = ""
    ) -> dict:
        path = self._chunk_path(track, sequence)
        with tempfile.NamedTemporaryFile(
            dir=str(path.parent), prefix=".tmp-", delete=False
        ) as handle:
            handle.write(data)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        return {
            "track": track,
            "sequence": sequence,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "codec": codec or "unknown",
        }

    def sequences(self, track: str) -> List[int]:
        directory = self._track_dir(track)
        return sorted(
            int(path.stem) for path in directory.glob("*.chunk") if path.is_file()
        )

    def read(self, track: str, sequence: int) -> bytes:
        return self._chunk_path(track, sequence).read_bytes()

    def reconstruct(self, track: str, codec: str) -> bytes:
        sequences = self.sequences(track)
        missing = _missing_sequences(sequences)
        if missing:
            raise SequenceError(f"sequence gap on {track}: missing {missing}")
        return reconstruct_probe(
            [(sequence, self.read(track, sequence)) for sequence in sequences],
            codec,
        )

    def reset(self, track: Optional[str] = None) -> None:
        if track is None:
            for path in sorted(self.root.glob("*")):
                if path.is_dir():
                    for child in path.glob("*.chunk"):
                        child.unlink()
            return
        directory = self.root / _safe_track(track)
        if directory.is_dir():
            for child in directory.glob("*.chunk"):
                child.unlink()


def _make_handler(root: Path, store: ChunkStore):
    root = root.resolve()

    class ProbeHandler(BaseHTTPRequestHandler):
        server_version = "CloudCaptureProbe/0.1"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            if os.environ.get("PROBE_QUIET") == "1":
                return
            super().log_message(fmt, *args)

        # -- helpers -----------------------------------------------------
        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra_headers: Optional[dict] = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, encoded, "application/json; charset=utf-8")

        def _query(self) -> dict:
            return parse_qs(urlparse(self.path).query)

        def _one(self, query: dict, key: str, default: str = "") -> str:
            values = query.get(key) or [default]
            return values[0]

        def _body(self) -> Optional[bytes]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._json(400, {"error": "invalid_content_length"})
                return None
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "body_too_large", "limit": MAX_BODY_BYTES})
                return None
            return self.rfile.read(length) if length else b""

        def _static(self, name: str) -> None:
            path = (root / name).resolve()
            if root not in path.parents or not path.is_file():
                self._json(404, {"error": "not_found"})
                return
            content_type = _CONTENT_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            )
            self._send(200, path.read_bytes(), content_type)

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            query = self._query()
            try:
                if route in ("/", "/index.html"):
                    self._static("index.html")
                    return
                if route == "/capture.mjs":
                    self._static("capture.mjs")
                    return
                if route == "/probe/status":
                    track = _safe_track(self._one(query, "track"))
                    sequences = store.sequences(track)
                    self._json(
                        200,
                        {
                            "track": track,
                            "sequences": sequences,
                            "missing": _missing_sequences(sequences),
                        },
                    )
                    return
                if route == "/probe/reconstruct":
                    track = _safe_track(self._one(query, "track"))
                    codec = self._one(query, "codec")
                    try:
                        data = store.reconstruct(track, codec)
                    except HeaderDependencyError as error:
                        self._json(
                            409,
                            {
                                "error": "header_dependency",
                                "dependent": bool(error.dependent),
                                "message": str(error),
                            },
                        )
                        return
                    except SequenceError as error:
                        self._json(
                            409,
                            {
                                "error": "sequence_gap",
                                "missing": _missing_sequences(store.sequences(track)),
                                "message": str(error),
                            },
                        )
                        return
                    self._send(
                        200,
                        data,
                        _CONTENT_TYPES.get(".bin", "application/octet-stream"),
                    )
                    return
            except ValueError as error:
                self._json(400, {"error": "invalid_request", "message": str(error)})
                return
            self._json(404, {"error": "not_found", "route": route})

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            query = self._query()
            try:
                if route == "/probe/reset":
                    track = self._one(query, "track")
                    store.reset(track or None)
                    self._json(200, {"reset": True, "track": track or "*"})
                    return
                if route == "/probe/upload":
                    track = _safe_track(self._one(query, "track"))
                    sequence_text = self._one(query, "sequence")
                    try:
                        sequence = int(sequence_text)
                    except ValueError:
                        self._json(
                            400,
                            {
                                "error": "invalid_sequence",
                                "sequence": sequence_text,
                            },
                        )
                        return
                    body = self._body()
                    if body is None:
                        return
                    stored = store.put(
                        track, sequence, body, self._one(query, "codec")
                    )
                    self._json(200, stored)
                    return
            except ValueError as error:
                self._json(400, {"error": "invalid_request", "message": str(error)})
                return
            self._json(404, {"error": "not_found", "route": route})

    return ProbeHandler


def create_probe_server(
    root: Path,
    out_dir: Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Create (but do not start) the loopback probe server."""
    store = ChunkStore(Path(out_dir))
    handler = _make_handler(Path(root), store)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.probe_store = store  # type: ignore[attr-defined]
    server.probe_root = Path(root).resolve()  # type: ignore[attr-defined]
    return server


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="cloud capture probe server")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory holding index.html and capture.mjs",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cloud-capture-probe-chunks",
        help="where uploaded chunks are stored",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    server = create_probe_server(args.root, args.out_dir, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"cloud capture probe: http://{host}:{port}", flush=True)
    print(f"chunks: {args.out_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
