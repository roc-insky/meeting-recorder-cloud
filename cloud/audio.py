"""Decode sealed private tracks into 16 kHz work assets (Task 9).

Design boundaries:

* the input is the frozen, owner-scoped chunk set of one sealed upload,
  rebuilt in epoch then sequence order; a hole in the sequence chain is a
  conflict, never silently closed by concatenating across it;
* sources are read, never modified: the 48 kHz originals stay byte-identical
  and every derived track/mix is a new 16 kHz mono WAV;
* the mix keeps the meeting timeline. Silence fills real gaps (an epoch
  interruption, a missing chunk run) and those gaps stay explicit in
  AudioInput, so downstream ASR can skip them instead of hallucinating
  speech into silence;
* FFmpeg runs as an argv list (shell=False), on paths validated inside the
  per-call workspace, with a duration cap, a wall-clock timeout and an output
  size cap; stderr is read only as a bounded tail.

The derived WAVs are uploaded to private object storage first and only then
recorded in derived_assets, so a restart reuses the same bytes for the same
input_version instead of re-encoding. The scratch directory is always removed;
published files appear in the caller workspace only after a successful decode.
"""

from __future__ import annotations

import array
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from cloud.contracts import (
    AudioInput,
    AuthContext,
    CloudError,
    ConflictError,
    Gap,
    InvalidRequestError,
    NotFoundError,
    PayloadTooLargeError,
)
from cloud.objects import ObjectStoreError, derived_object_key
from cloud.schema import (
    Chunk,
    DerivedAsset,
    Meeting,
    UploadEpoch,
    UploadEpochTrack,
    UploadSession,
)

WORK_RATE = 16000
SAMPLE_WIDTH = 2
#: Refuse a frozen input larger than this before any decoder is spawned.
MAX_INPUT_BYTES = 512 * 1024 * 1024
#: Wall-clock budget for one decoder run (a 2 h input decodes far faster).
DECODE_TIMEOUT_S = 120
#: Never buffer more than this much FFmpeg stderr for the log.
MAX_STDERR_BYTES = 8192
#: A decoder that yields materially less audio than the timeline is broken.
MIN_DECODED_RATIO = 0.9
_BLOCK = 64 * 1024
_LOG = logging.getLogger(__name__)


@dataclass
class _Span:
    """One contiguous chunk run of a track inside one epoch."""

    track_id: str
    epoch_id: str
    start_ms: int
    end_ms: int
    chunks: list[Chunk]


@dataclass
class _Piece:
    """A run of decoded frames that belongs at an absolute timeline offset."""

    start_ms: int
    pcm_path: Path
    frame_start: int
    frames: int


@dataclass
class _Plan:
    """Validated, not yet decoded structure of one frozen input."""

    spans: list[_Span]
    gaps: list[Gap]
    duration_ms: int
    track_ids: list[str]
    input_bytes: int


def _safe_path(path: Path | str, workspace: Path) -> Path:
    """Resolve a path that must stay inside the call workspace.

    Absolute paths are required so a relative argument can never be
    reinterpreted against an unexpected cwd; symlinks are refused outright so
    a link planted in the workspace cannot reach outside it.
    """
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise InvalidRequestError("unsafe_audio_path")
    root = workspace.resolve()
    try:
        resolved = candidate.resolve(strict=False)
        if resolved != root:
            resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        raise InvalidRequestError("unsafe_audio_path") from exc
    return resolved


def _stderr_tail(handle: Any) -> str:
    try:
        handle.flush()
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - MAX_STDERR_BYTES))
        return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _kill(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def decode_segment(
    source: Path | str,
    destination: Path | str,
    *,
    workspace: Path,
    max_seconds: float,
    timeout_s: float,
    max_bytes: int,
) -> Path:
    """Decode one container stream to 16 kHz mono signed-16-bit PCM.

    The destination is raw PCM (no container), which is what the timeline
    splicer needs. Every refusal happens before a process is spawned; while
    the process runs its output file is checked so a resource bomb cannot
    fill the disk, and the wall clock is capped independently of -t.
    """
    if max_seconds <= 0 or timeout_s <= 0 or max_bytes <= 0:
        raise InvalidRequestError("unsafe_audio_path")
    root = workspace.resolve()
    source_arg = Path(source)
    destination_arg = Path(destination)
    safe_source = _safe_path(source_arg, root)
    if not safe_source.is_file():
        raise InvalidRequestError("unsafe_audio_path")
    try:
        source_size = safe_source.stat().st_size
    except OSError as exc:
        raise InvalidRequestError("unsafe_audio_path") from exc
    if source_size <= 0:
        raise CloudError("audio_decode_failed")
    if source_size > max_bytes:
        raise PayloadTooLargeError("audio_input_too_large")
    safe_destination = _safe_path(destination_arg, root)
    if not safe_destination.parent.is_dir():
        raise InvalidRequestError("unsafe_audio_path")

    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_arg),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-t",
        f"{max_seconds:.3f}",
        "-ar",
        str(WORK_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        str(destination_arg),
    ]
    with tempfile.TemporaryFile(dir=root, prefix="ffmpeg-stderr-") as stderr:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            cwd=str(root),
        )
        deadline = time.monotonic() + timeout_s
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    _kill(process)
                    raise CloudError("audio_decode_failed")
                if (
                    safe_destination.exists()
                    and safe_destination.stat().st_size > max_bytes
                ):
                    _kill(process)
                    raise PayloadTooLargeError("audio_output_too_large")
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                _kill(process)
        if process.returncode != 0:
            _LOG.debug(
                "decode failed for %s: %s", safe_source, _stderr_tail(stderr)
            )
            raise CloudError("audio_decode_failed")
        if not safe_destination.is_file():
            raise CloudError("audio_decode_failed")
        written = safe_destination.stat().st_size
        if written <= 0:
            raise CloudError("audio_decode_failed")
        if written > max_bytes:
            raise PayloadTooLargeError("audio_output_too_large")
    return safe_destination


def _chunks(
    db: Any, ctx: AuthContext, upload_id: str, epoch_id: str, track_id: str
) -> list[Chunk]:
    return list(
        db.scalars(
            select(Chunk)
            .where(
                Chunk.owner_user_id == ctx.user_id,
                Chunk.upload_session_id == upload_id,
                Chunk.epoch_id == epoch_id,
                Chunk.track_id == track_id,
            )
            .order_by(Chunk.sequence)
        )
    )


def _pair_rows(db: Any, ctx: AuthContext, upload_id: str) -> list[Any]:
    return list(
        db.execute(
            select(UploadEpochTrack, UploadEpoch)
            .join(
                UploadEpoch,
                (UploadEpoch.owner_user_id == UploadEpochTrack.owner_user_id)
                & (
                    UploadEpoch.upload_session_id
                    == UploadEpochTrack.upload_session_id
                )
                & (UploadEpoch.id == UploadEpochTrack.epoch_id),
            )
            .where(
                UploadEpochTrack.owner_user_id == ctx.user_id,
                UploadEpochTrack.upload_session_id == upload_id,
            )
            .order_by(
                UploadEpoch.offset_ms,
                UploadEpoch.created_at,
                UploadEpoch.id,
                UploadEpochTrack.track_id,
            )
        ).all()
    )


def _dedupe(gaps: Iterable[Gap]) -> list[Gap]:
    seen: set[tuple[str, int, int, str]] = set()
    unique: list[Gap] = []
    for gap in gaps:
        if gap.end_ms <= gap.start_ms:
            continue
        key = (gap.track_id, gap.start_ms, gap.end_ms, gap.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(gap)
    return unique


def _plan(db: Any, ctx: AuthContext, upload: UploadSession) -> _Plan:
    """Validate ordering and build the timeline skeleton, without decoding."""
    spans: list[_Span] = []
    gaps: list[Gap] = []
    track_ids: list[str] = []
    duration_ms = 0
    input_bytes = 0
    for link, _epoch in _pair_rows(db, ctx, upload.id):
        chunks = _chunks(db, ctx, upload.id, link.epoch_id, link.track_id)
        if not chunks:
            continue
        sequences = [chunk.sequence for chunk in chunks]
        if sequences != list(range(len(sequences))):
            raise ConflictError("audio_sequence_gap")
        for previous, current in zip(chunks, chunks[1:]):
            if current.start_ms < previous.end_ms:
                raise ConflictError("audio_timeline_overlap")
            if current.start_ms > previous.end_ms:
                gaps.append(
                    Gap(
                        track_id=link.track_id,
                        start_ms=previous.end_ms,
                        end_ms=current.start_ms,
                        reason="missing_chunks",
                    )
                )
        span = _Span(
            track_id=link.track_id,
            epoch_id=link.epoch_id,
            start_ms=chunks[0].start_ms,
            end_ms=chunks[-1].end_ms,
            chunks=chunks,
        )
        spans.append(span)
        if link.track_id not in track_ids:
            track_ids.append(link.track_id)
        duration_ms = max(duration_ms, span.end_ms)
        input_bytes += sum(chunk.byte_size for chunk in chunks)
    if not spans:
        raise CloudError("audio_decode_failed")
    by_track: dict[str, list[_Span]] = {}
    for span in spans:
        by_track.setdefault(span.track_id, []).append(span)
    for track_spans in by_track.values():
        track_spans.sort(key=lambda item: (item.start_ms, item.end_ms))
        for previous, current in zip(track_spans, track_spans[1:]):
            if current.start_ms < previous.end_ms:
                raise ConflictError("audio_timeline_overlap")
            if current.start_ms > previous.end_ms:
                gaps.append(
                    Gap(
                        track_id=current.track_id,
                        start_ms=previous.end_ms,
                        end_ms=current.start_ms,
                        reason="capture_interruption",
                    )
                )
    for raw in upload.seal_gaps or []:
        try:
            gaps.append(Gap(**raw))
        except (TypeError, ValueError):
            continue
    return _Plan(
        spans=spans,
        gaps=_dedupe(gaps),
        duration_ms=duration_ms,
        track_ids=track_ids,
        input_bytes=input_bytes,
    )


def _write_silence(destination: Any, frames: int) -> None:
    remaining = frames * SAMPLE_WIDTH
    block = b"\x00" * _BLOCK
    while remaining > 0:
        count = min(remaining, len(block))
        destination.write(block[:count])
        remaining -= count


def _copy_frames(source: Any, destination: Any, frames: int) -> None:
    remaining = frames * SAMPLE_WIDTH
    while remaining > 0:
        block = source.read(min(remaining, _BLOCK))
        if not block:
            _write_silence(destination, remaining // SAMPLE_WIDTH)
            return
        destination.write(block)
        remaining -= len(block)


def _build_track(pieces: list[_Piece], destination: Path, total_frames: int) -> None:
    """Place decoded chunk runs on the absolute meeting timeline."""
    position = 0
    with open(destination, "wb") as output:
        for piece in sorted(pieces, key=lambda item: item.start_ms):
            start = round(piece.start_ms * WORK_RATE / 1000)
            if start < position:
                raise ConflictError("audio_timeline_overlap")
            _write_silence(output, start - position)
            with open(piece.pcm_path, "rb") as source:
                source.seek(piece.frame_start * SAMPLE_WIDTH)
                _copy_frames(source, output, piece.frames)
            position = start + piece.frames
        if position > total_frames:
            raise CloudError("audio_decode_failed")
        _write_silence(output, total_frames - position)


def _write_wav(destination: Path, pcm_path: Path, frames: int) -> None:
    with wave.open(str(destination), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(WORK_RATE)
        written = 0
        with open(pcm_path, "rb") as source:
            while written < frames:
                want = min(_BLOCK, (frames - written) * SAMPLE_WIDTH)
                block = source.read(want)
                if len(block) < want:
                    block += b"\x00" * (want - len(block))
                writer.writeframes(block)
                written += want // SAMPLE_WIDTH


def _mix_tracks(
    track_paths: list[Path], destination: Path, frames: int
) -> None:
    if len(track_paths) == 1:
        shutil.copyfile(track_paths[0], destination)
        return
    total = frames * SAMPLE_WIDTH
    readers = [open(path, "rb") for path in track_paths]
    try:
        with open(destination, "wb") as output:
            done = 0
            while done < total:
                size = min(_BLOCK, total - done)
                mixed = [0] * (size // SAMPLE_WIDTH)
                for reader in readers:
                    block = reader.read(size)
                    if len(block) < size:
                        block += b"\x00" * (size - len(block))
                    samples = array.array("h")
                    samples.frombytes(block)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    for index, value in enumerate(samples):
                        mixed[index] += value
                output.write(
                    array.array(
                        "h",
                        [max(-32768, min(32767, value)) for value in mixed],
                    ).tobytes()
                )
                done += size
    finally:
        for reader in readers:
            reader.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _meeting(db: Any, ctx: AuthContext, meeting_id: str) -> Meeting:
    meeting = db.get(Meeting, (ctx.user_id, meeting_id))
    if meeting is None or meeting.deleted_at is not None:
        raise NotFoundError("meeting_not_found")
    return meeting


def _upload(
    db: Any, ctx: AuthContext, meeting_id: str, input_version: int
) -> UploadSession:
    upload = db.scalar(
        select(UploadSession)
        .where(
            UploadSession.owner_user_id == ctx.user_id,
            UploadSession.meeting_id == meeting_id,
            UploadSession.status == "sealed",
            UploadSession.input_version == input_version,
        )
        .order_by(UploadSession.created_at.desc(), UploadSession.id.desc())
        .limit(1)
    )
    if upload is None:
        raise ConflictError("input_not_sealed")
    return upload


def _existing_assets(
    db: Any, ctx: AuthContext, meeting_id: str, input_version: int
) -> list[DerivedAsset]:
    return list(
        db.scalars(
            select(DerivedAsset).where(
                DerivedAsset.owner_user_id == ctx.user_id,
                DerivedAsset.meeting_id == meeting_id,
                DerivedAsset.input_version == input_version,
            )
        )
    )


def _reuse(
    ctx: AuthContext,
    workspace: Path,
    plan: _Plan,
    input_version: int,
    existing: list[DerivedAsset],
) -> AudioInput | None:
    objects = ctx.require_objects()
    needed = {("mix", None)} | {("track", track) for track in plan.track_ids}
    available = {(row.kind, row.track_id): row for row in existing}
    if not needed.issubset(available):
        return None
    final_paths: dict[tuple[str, str | None], Path] = {}
    try:
        for kind, track in sorted(needed, key=lambda item: (item[0], item[1] or "")):
            row = available[(kind, track)]
            name = (
                f"mix-{input_version}-{uuid4().hex[:12]}.wav"
                if kind == "mix"
                else f"track-{track}-{input_version}-{uuid4().hex[:12]}.wav"
            )
            destination = workspace / name
            objects.download_to(row.object_key, destination)
            if _sha256(destination) != row.sha256:
                destination.unlink(missing_ok=True)
                for path in final_paths.values():
                    path.unlink(missing_ok=True)
                return None
            final_paths[(kind, track)] = destination
    except (ObjectStoreError, OSError):
        for path in final_paths.values():
            path.unlink(missing_ok=True)
        return None
    return AudioInput(
        rate=WORK_RATE,
        tracks={track: final_paths[("track", track)] for track in plan.track_ids},
        mix_path=final_paths[("mix", None)],
        gaps=list(plan.gaps),
    )


def prepare_audio(
    ctx: AuthContext,
    meeting_id: str,
    input_version: int,
    workspace: Path,
) -> AudioInput:
    """Decode (or reuse) the frozen input at input_version. Owner-only."""
    db = ctx.require_db()
    objects = ctx.require_objects()
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    _meeting(db, ctx, meeting_id)
    upload = _upload(db, ctx, meeting_id, input_version)
    plan = _plan(db, ctx, upload)
    if plan.input_bytes > MAX_INPUT_BYTES:
        raise PayloadTooLargeError("audio_input_too_large")

    existing = _existing_assets(db, ctx, meeting_id, input_version)
    if existing:
        reused = _reuse(ctx, root, plan, input_version, existing)
        if reused is not None:
            return reused
        for row in existing:
            db.delete(row)
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise CloudError("commit_failed") from exc

    scratch = Path(tempfile.mkdtemp(prefix=".audio-", dir=root))
    published: list[Path] = []
    try:
        total_frames = round(plan.duration_ms * WORK_RATE / 1000)
        if total_frames <= 0:
            raise CloudError("audio_decode_failed")
        track_pcm: dict[str, Path] = {}
        for track_id in plan.track_ids:
            pieces: list[_Piece] = []
            track_spans = [span for span in plan.spans if span.track_id == track_id]
            track_spans.sort(key=lambda span: (span.start_ms, span.epoch_id))
            for span in track_spans:
                source = scratch / f"source-{span.epoch_id}-{track_id}.bin"
                with open(source, "wb") as handle:
                    for chunk in span.chunks:
                        handle.write(objects.get_bytes(chunk.object_key))
                expected_ms = span.end_ms - span.start_ms
                expected_frames = round(expected_ms * WORK_RATE / 1000)
                pcm = scratch / f"decoded-{span.epoch_id}-{track_id}.pcm"
                decode_segment(
                    source,
                    pcm,
                    workspace=root,
                    max_seconds=expected_ms / 1000 + 1.0,
                    timeout_s=float(
                        min(ctx.settings.request_timeout_s, DECODE_TIMEOUT_S)
                    ),
                    max_bytes=max(expected_frames * SAMPLE_WIDTH + 2 * WORK_RATE, 1 << 20),
                )
                available = pcm.stat().st_size // SAMPLE_WIDTH
                if available < expected_frames * MIN_DECODED_RATIO:
                    raise CloudError("audio_decode_failed")
                cursor = 0
                for chunk in span.chunks:
                    frames = round((chunk.end_ms - chunk.start_ms) * WORK_RATE / 1000)
                    frames = max(0, min(frames, available - cursor))
                    if frames == 0:
                        break
                    pieces.append(
                        _Piece(
                            start_ms=chunk.start_ms,
                            pcm_path=pcm,
                            frame_start=cursor,
                            frames=frames,
                        )
                    )
                    cursor += frames
                source.unlink(missing_ok=True)
            track_path = scratch / f"track-{track_id}.pcm"
            _build_track(pieces, track_path, total_frames)
            track_pcm[track_id] = track_path

        mix_pcm = scratch / "mix.pcm"
        _mix_tracks([track_pcm[track] for track in plan.track_ids], mix_pcm, total_frames)

        wavs: list[tuple[str, str | None, Path]] = []
        mix_wav = scratch / "mix.wav"
        _write_wav(mix_wav, mix_pcm, total_frames)
        wavs.append(("mix", None, mix_wav))
        for track_id in plan.track_ids:
            track_wav = scratch / f"track-{track_id}.wav"
            _write_wav(track_wav, track_pcm[track_id], total_frames)
            wavs.append(("track", track_id, track_wav))

        rows: list[DerivedAsset] = []
        for kind, track, wav in wavs:
            key = derived_object_key(
                ctx.user_id, meeting_id, input_version, kind, track
            )
            digest = _sha256(wav)
            size = objects.put_file_verified(
                key, wav, digest, content_type="audio/wav"
            )
            rows.append(
                DerivedAsset(
                    owner_user_id=ctx.user_id,
                    id=str(uuid4()),
                    meeting_id=meeting_id,
                    input_version=input_version,
                    kind=kind,
                    track_id=track,
                    object_key=key,
                    content_type="audio/wav",
                    bytes=size,
                    duration_ms=plan.duration_ms,
                    sha256=digest,
                )
            )
        db.add_all(rows)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            concurrent = _existing_assets(db, ctx, meeting_id, input_version)
            if concurrent:
                reused = _reuse(ctx, root, plan, input_version, concurrent)
                if reused is not None:
                    return reused
            raise CloudError("commit_failed") from exc
        except SQLAlchemyError as exc:
            db.rollback()
            raise CloudError("commit_failed") from exc

        paths: dict[tuple[str, str | None], Path] = {}
        for row, (kind, track, wav) in zip(rows, wavs):
            name = (
                f"mix-{input_version}-{uuid4().hex[:12]}.wav"
                if kind == "mix"
                else f"track-{track}-{input_version}-{uuid4().hex[:12]}.wav"
            )
            final_path = root / name
            os.replace(wav, final_path)
            published.append(final_path)
            paths[(kind, track)] = final_path
        return AudioInput(
            rate=WORK_RATE,
            tracks={track: paths[("track", track)] for track in plan.track_ids},
            mix_path=paths[("mix", None)],
            gaps=list(plan.gaps),
        )
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
