"""Private local object storage for audio chunks.

Chunks live in a private local directory under server-generated keys of the
form ``chunks/{owner}/{upload}/{epoch}/{track}/{sequence}.bin`` inside the
configured root. Clients never propose a key, so they cannot read or overwrite
another user's object, and the deterministic key is what makes a retry after a
lost ACK reuse the same bytes.

``put_verified`` checks the real SHA-256 before writing and the stored size
afterwards; a failed write raises ``ObjectStoreError``, which the API maps to a
5xx so the client retries instead of receiving an ACK for missing bytes.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import re
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping
from uuid import uuid4

from cloud.contracts import CloudError, InvalidRequestError

CHUNK_PREFIX = "chunks/"
DERIVED_PREFIX = "derived/"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND_PATTERN = re.compile(r"^(mix|track)$")
_SEQUENCE_WIDTH = 10
#: Upper bound on one streamed read, so a response never buffers a whole file.
STREAM_BLOCK_BYTES = 64 * 1024
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_KEY_MAX_CHARS = 1024
_TEMP_DIRNAME = ".tmp"
_DEFAULT_MIN_FREE_BYTES = 1024 ** 3
_OBJECTS_DIR_ENV = "CLOUD_OBJECTS_DIR"
_MIN_FREE_ENV = "CLOUD_OBJECTS_MIN_FREE_BYTES"


class ObjectStoreError(CloudError):
    """The object could not be stored or read; never acknowledge the chunk."""

    status_code = 500
    code = "object_store_failure"

    def __init__(self, message: str = "object store operation failed") -> None:
        # Keep the wire code fixed: the message is for the log, not the client.
        Exception.__init__(self, message)


class StorageFullError(CloudError):
    """The write would violate the free-space reserve; fail before ACKing."""

    status_code = 507
    code = "storage_full"

    def __init__(self, message: str = "not enough free space") -> None:
        Exception.__init__(self, message)


def chunk_object_key(
    owner_user_id: str,
    upload_session_id: str,
    epoch_id: str,
    track_id: str,
    sequence: int,
) -> str:
    """Derive the only key a chunk may live under (server generated)."""
    if sequence < 0:
        raise InvalidRequestError("invalid_sequence")
    for label, value in (
        ("owner", owner_user_id),
        ("upload", upload_session_id),
        ("epoch", epoch_id),
        ("track", track_id),
    ):
        if not _ID_PATTERN.match(value):
            raise InvalidRequestError(f"invalid_{label}_id")
    return (
        f"{CHUNK_PREFIX}{owner_user_id}/{upload_session_id}/{epoch_id}/"
        f"{track_id}/{sequence:0{_SEQUENCE_WIDTH}d}.bin"
    )


def derived_object_key(
    owner_user_id: str,
    meeting_id: str,
    input_version: int,
    kind: str,
    track_id: str | None = None,
) -> str:
    """Server-generated key for one derived work/playback asset."""
    if not _KIND_PATTERN.match(kind):
        raise InvalidRequestError("invalid_asset_kind")
    if input_version < 0:
        raise InvalidRequestError("invalid_input_version")
    for label, value in (("owner", owner_user_id), ("meeting", meeting_id)):
        if not _ID_PATTERN.match(value):
            raise InvalidRequestError(f"invalid_{label}_id")
    if kind == "mix":
        if track_id is not None:
            raise InvalidRequestError("invalid_track_id")
        suffix = "mix"
    else:
        if track_id is None or not _ID_PATTERN.match(track_id):
            raise InvalidRequestError("invalid_track_id")
        suffix = f"track-{track_id}"
    return (
        f"{DERIVED_PREFIX}{owner_user_id}/{meeting_id}/{input_version}/"
        f"{suffix}/{uuid4().hex}.wav"
    )


class LocalObjectStore:
    """One file per key under ``root``, published atomically.

    Keys are server generated and never client supplied, so a rejected key is a
    bug in the caller and raises ``ObjectStoreError`` instead of a 400. Every
    write is staged in ``root/.tmp`` and published with ``os.replace``, so a
    crash leaves no partial object; the free-space reserve is checked before
    staging, so a full disk is a 507 rather than a truncated object.
    """

    def __init__(
        self, root: Path, *, min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES
    ) -> None:
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        self.root = Path(root).resolve()
        #: Bytes that must stay free after every write; public for health checks.
        self.min_free_bytes = min_free_bytes
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # mkdir's mode is masked by the umask; the deploy expects 0750 on
            # the root and a private staging directory even if .tmp existed.
            os.chmod(self.root, 0o750)
            os.chmod(self.temp_dir, 0o700)
        except OSError as exc:
            # Every construction path reports the same code: a root that cannot
            # be created or secured is unusable, never a raw NotADirectoryError.
            raise ObjectStoreError("unusable_objects_dir") from exc

    @property
    def temp_dir(self) -> Path:
        return self.root / _TEMP_DIRNAME

    # -- write path ------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Map a key to its path under ``root``, refusing every unsafe shape."""
        if not isinstance(key, str) or not 1 <= len(key) <= _KEY_MAX_CHARS:
            raise ObjectStoreError("invalid_object_key")
        if not _KEY_PATTERN.match(key):
            # Also rejects a backslash, a NUL byte and a leading "/".
            raise ObjectStoreError("invalid_object_key")
        if any(segment in ("", ".", "..") for segment in key.split("/")):
            raise ObjectStoreError("invalid_object_key")
        target = (self.root / key).resolve(strict=False)
        if not target.is_relative_to(self.root):
            # Defence in depth: a symlinked parent can escape a valid key.
            raise ObjectStoreError("invalid_object_key")
        return target

    def available_bytes(self) -> int:
        try:
            stats = os.statvfs(self.root)
        except OSError as exc:
            raise ObjectStoreError("storage_unavailable") from exc
        return stats.f_bavail * stats.f_frsize

    def _require_space(self, needed: int) -> None:
        if self.available_bytes() < needed + self.min_free_bytes:
            raise StorageFullError("disk_full")

    def _stage_path(self) -> Path:
        return self.temp_dir / f".put-{uuid4().hex}"

    def _write_bytes(self, stage: Path, body: bytes) -> None:
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    def _fsync_dir(self, directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fsync_parents(self, leaf: Path) -> None:
        """fsync ``leaf`` and every ancestor up to and including ``self.root``."""
        for directory in (leaf, *leaf.parents):
            self._fsync_dir(directory)
            if directory == self.root:
                return

    def _publish(self, stage: Path, target: Path, expected: int) -> None:
        if stage.stat().st_size != expected:
            raise ObjectStoreError("object_size_mismatch")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.replace(stage, target)
        os.chmod(target, 0o640)
        # The rename is durable only once every directory entry from the leaf up
        # to the root has reached disk: the new name in the leaf directory and
        # each ancestor directory created for a first write under a key.
        self._fsync_parents(target.parent)

    def _map_write_error(self, exc: OSError) -> CloudError:
        """Map a failed write: a full disk is a 507, every other failure a 500."""
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return StorageFullError("disk_full")
        return ObjectStoreError("write_failed")

    def put_verified(self, key: str, body: bytes, sha256: str) -> None:
        target = self._resolve(key)
        if not body:
            raise ObjectStoreError("empty_body")
        actual = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual, sha256):
            raise ObjectStoreError("checksum_mismatch")
        self._require_space(len(body))
        stage = self._stage_path()
        try:
            self._write_bytes(stage, body)
            self._publish(stage, target, len(body))
        except ObjectStoreError:
            stage.unlink(missing_ok=True)
            raise
        except OSError as exc:
            stage.unlink(missing_ok=True)
            raise self._map_write_error(exc) from exc

    def put_file_verified(
        self,
        key: str,
        path: Path,
        sha256: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> int:
        """Copy a file already on disk, hashing it while streaming."""
        target = self._resolve(key)
        digest = hashlib.sha256()
        size = 0
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(STREAM_BLOCK_BYTES), b""):
                    digest.update(block)
                    size += len(block)
        except OSError as exc:
            raise ObjectStoreError("unreadable_file") from exc
        if size == 0:
            raise ObjectStoreError("empty_body")
        if not hmac.compare_digest(digest.hexdigest(), sha256):
            raise ObjectStoreError("checksum_mismatch")
        self._require_space(size)
        stage = self._stage_path()
        try:
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
                shutil.copyfileobj(src, out, STREAM_BLOCK_BYTES)
                out.flush()
                os.fsync(out.fileno())
            self._publish(stage, target, size)
        except ObjectStoreError:
            stage.unlink(missing_ok=True)
            raise
        except OSError as exc:
            stage.unlink(missing_ok=True)
            raise self._map_write_error(exc) from exc
        return size


    # -- read path -------------------------------------------------------

    def _stat_object(self, key: str) -> os.stat_result | None:
        """Stat one object; None when the key is absent, error when not a file."""
        target = self._resolve(key)
        try:
            info = target.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ObjectStoreError() from exc
        if not stat.S_ISREG(info.st_mode):
            raise ObjectStoreError("not_a_file")
        return info

    def object_size(self, key: str) -> int | None:
        info = self._stat_object(key)
        return None if info is None else info.st_size

    def exists(self, key: str) -> bool:
        target = self._resolve(key)
        try:
            return stat.S_ISREG(target.stat().st_mode)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ObjectStoreError() from exc

    def iter_range(self, key: str, start: int, length: int) -> Iterator[bytes]:
        """Stream length bytes from start; a short object raises."""
        if start < 0 or length < 0:
            raise InvalidRequestError("invalid_range")
        target = self._resolve(key)
        if length == 0:
            return iter(())
        try:
            info = target.stat()
        except FileNotFoundError as exc:
            raise ObjectStoreError("object_not_found") from exc
        except OSError as exc:
            raise ObjectStoreError() from exc
        if not stat.S_ISREG(info.st_mode):
            raise ObjectStoreError("not_a_file")

        def blocks() -> Iterator[bytes]:
            remaining = length
            try:
                with open(target, "rb") as handle:
                    handle.seek(start)
                    while remaining > 0:
                        block = handle.read(min(STREAM_BLOCK_BYTES, remaining))
                        if not block:
                            raise ObjectStoreError("short_read")
                        remaining -= len(block)
                        yield block
            except OSError as exc:
                raise ObjectStoreError() from exc

        return blocks()

    def get_bytes(self, key: str) -> bytes:
        target = self._resolve(key)
        try:
            with open(target, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise ObjectStoreError() from exc

    def download_to(self, key: str, destination: Path) -> None:
        target = self._resolve(key)
        try:
            info = target.stat()
        except FileNotFoundError as exc:
            raise ObjectStoreError("object_not_found") from exc
        except OSError as exc:
            raise ObjectStoreError() from exc
        if not stat.S_ISREG(info.st_mode):
            raise ObjectStoreError("not_a_file")
        part = destination.with_name(destination.name + ".part")
        try:
            with open(target, "rb") as source, open(part, "wb") as sink:
                shutil.copyfileobj(source, sink, STREAM_BLOCK_BYTES)
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(part, destination)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise ObjectStoreError("download_failed") from exc

    # -- maintenance -----------------------------------------------------

    def _resolve_prefix(self, prefix: str) -> Path:
        stripped = prefix.rstrip("/")
        if not stripped:
            return self.root
        return self._resolve(stripped)

    def list_objects(self, prefix: str = CHUNK_PREFIX) -> list[tuple[str, datetime]]:
        base = self._resolve_prefix(prefix)
        try:
            info = base.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ObjectStoreError() from exc
        if not stat.S_ISDIR(info.st_mode):
            return []
        found: list[tuple[str, datetime]] = []
        for directory, dirnames, filenames in os.walk(base, followlinks=False):
            # The staging area lives under the root but is never an object tree.
            dirnames[:] = [name for name in dirnames if name != _TEMP_DIRNAME]
            for name in filenames:
                path = Path(directory) / name
                try:
                    entry = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(entry.st_mode):
                    continue  # symlinks and specials are not stored objects
                key = path.relative_to(self.root).as_posix()
                modified = datetime.fromtimestamp(entry.st_mtime, timezone.utc)
                found.append((key, modified))
        found.sort(key=lambda item: item[0])
        return found

    def list_keys(self, prefix: str = CHUNK_PREFIX) -> list[str]:
        return [key for key, _ in self.list_objects(prefix)]

    def clear_prefix(self, prefix: str = CHUNK_PREFIX) -> int:
        keys = self.list_keys(prefix)
        for key in keys:
            self.delete(key)
        return len(keys)

    def delete(self, key: str) -> None:
        target = self._resolve(key)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError as exc:
            raise ObjectStoreError("not_a_file") from exc
        except OSError as exc:
            raise ObjectStoreError() from exc
        # Prune the now-empty parents, stopping at the root or the first
        # directory that still holds another object.
        parent = target.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # -- health ----------------------------------------------------------

    def ready(self) -> bool:
        """True only for a writable root that keeps the reserve free.

        Never raises: the health route must report "not ready" rather than a
        5xx when the volume disappeared or cannot be statvfs'd.
        """
        try:
            if not self.root.is_dir():
                return False
            if not os.access(self.root, os.W_OK | os.X_OK):
                return False
            return self.available_bytes() >= self.min_free_bytes
        except (ObjectStoreError, OSError):
            return False

    def sweep_stale_temp(self, max_age_s: int = 3600) -> int:
        """Drop abandoned staging files older than max_age_s seconds.

        Only regular files directly inside .tmp are candidates: it never
        recurses, skips symlinks and directories, and a per-file failure is
        swallowed so one bad entry cannot abort the sweep.
        """
        cutoff = time.time() - max_age_s
        try:
            entries = list(self.temp_dir.iterdir())
        except OSError:
            return 0  # a missing staging directory has nothing to sweep
        removed = 0
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                entry.unlink()
            except OSError:
                continue
            removed += 1
        return removed


def object_store_from_env(env: Mapping[str, str]) -> LocalObjectStore | None:
    """Build the local store from CLOUD_OBJECTS_*; None when unconfigured."""
    raw = (env.get(_OBJECTS_DIR_ENV) or "").strip()
    if not raw:
        return None
    reserve: int | None = None
    raw_reserve = env.get(_MIN_FREE_ENV) or ""
    if raw_reserve:
        # A bad value must fail start-up instead of silently falling back.
        reserve = int(raw_reserve)
        if reserve < 0:
            raise ValueError(_MIN_FREE_ENV + " must be non-negative")
    path = Path(raw)
    if not path.is_absolute():
        raise ObjectStoreError("invalid_objects_dir")
    return LocalObjectStore(
        path,
        min_free_bytes=_DEFAULT_MIN_FREE_BYTES if reserve is None else reserve,
    )
