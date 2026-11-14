"""Private object storage for audio chunks (Task 5).

Chunks live in a private bucket under server-generated keys of the form
``chunks/{owner}/{upload}/{epoch}/{track}/{sequence}.bin``. Clients never
propose a key, so they cannot read or overwrite another user's object, and the
deterministic key is what makes a retry after a lost ACK reuse the same bytes.

``put_verified`` checks the real SHA-256 before writing and the stored size
afterwards; a failed write raises ``ObjectStoreError``, which the API maps to a
5xx so the client retries instead of receiving an ACK for missing bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from cloud.contracts import CloudError, InvalidRequestError

CHUNK_PREFIX = "chunks/"
DERIVED_PREFIX = "derived/"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND_PATTERN = re.compile(r"^(mix|track)$")
_SEQUENCE_WIDTH = 10
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "NoSuchObject"})
#: Upper bound on one streamed read, so a response never buffers a whole file.
STREAM_BLOCK_BYTES = 64 * 1024


class ObjectStoreError(CloudError):
    """The object could not be stored or read; never acknowledge the chunk."""

    status_code = 500
    code = "object_store_failure"

    def __init__(self, message: str = "object store operation failed") -> None:
        # Keep the wire code fixed: the message is for the log, not the client.
        Exception.__init__(self, message)


@dataclass(frozen=True)
class S3Config:
    """Endpoint and credentials for one bucket (private, no public policy)."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    secure: bool = False
    region: str | None = None

    @property
    def endpoint_url(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"{scheme}://{self.endpoint}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> S3Config:
        source = os.environ if env is None else env
        raw = source.get("CLOUD_S3_ENDPOINT", "127.0.0.1:59000").strip()
        secure = False
        if "://" in raw:
            scheme, raw = raw.split("://", 1)
            secure = scheme.lower() == "https"
        secure = secure or source.get("CLOUD_S3_SECURE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        endpoint = raw.split("/", 1)[0].rstrip("/")
        if not endpoint:
            raise ValueError("CLOUD_S3_ENDPOINT must be host[:port]")
        return cls(
            endpoint=endpoint,
            access_key=source.get("CLOUD_S3_ACCESS_KEY", "cloudtest"),
            secret_key=source.get("CLOUD_S3_SECRET_KEY", "cloudtest-secret"),
            bucket=source.get("CLOUD_S3_BUCKET", "cloud-test"),
            secure=secure,
            region=source.get("CLOUD_S3_REGION") or None,
        )


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


class S3ObjectStore:
    """Thin minio-py client; every call maps transport errors to a 5xx."""

    def __init__(self, config: S3Config) -> None:
        self.config = config
        self._client = Minio(
            config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            region=config.region,
        )

    @property
    def client(self) -> Minio:
        return self._client

    # -- required interface ----------------------------------------------

    def put_verified(self, key: str, body: bytes, sha256: str) -> None:
        if not body:
            raise ObjectStoreError("empty_body")
        actual = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual, sha256):
            raise ObjectStoreError("checksum_mismatch")
        try:
            self._client.put_object(
                self.config.bucket,
                key,
                io.BytesIO(body),
                length=len(body),
                content_type="application/octet-stream",
            )
            stat = self._client.stat_object(self.config.bucket, key)
        except Exception as exc:  # transport, auth, missing bucket, ...
            raise ObjectStoreError() from exc
        if stat.size != len(body):
            raise ObjectStoreError("object_size_mismatch")

    def put_file_verified(
        self,
        key: str,
        path: Path,
        sha256: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> int:
        """Upload a file already on disk, hashing it as it streams."""
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
        try:
            with open(path, "rb") as handle:
                self._client.put_object(
                    self.config.bucket,
                    key,
                    handle,
                    length=size,
                    content_type=content_type,
                )
            stat = self._client.stat_object(self.config.bucket, key)
        except Exception as exc:  # transport, auth, missing bucket, ...
            raise ObjectStoreError() from exc
        if stat.size != size:
            raise ObjectStoreError("object_size_mismatch")
        return size

    # -- reads and maintenance -------------------------------------------

    def download_to(self, key: str, destination: Path) -> None:
        """Stream one object to a file without holding it in memory."""
        try:
            response = self._client.get_object(self.config.bucket, key)
            try:
                with open(destination, "wb") as handle:
                    for block in iter(
                        lambda: response.read(STREAM_BLOCK_BYTES), b""
                    ):
                        handle.write(block)
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise ObjectStoreError() from exc

    def object_size(self, key: str) -> int | None:
        """Stored byte count, or None when the object does not exist."""
        try:
            return self._client.stat_object(self.config.bucket, key).size
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return None
            raise ObjectStoreError() from exc
        except Exception as exc:
            raise ObjectStoreError() from exc

    def iter_range(self, key: str, start: int, length: int) -> Iterator[bytes]:
        """Yield exactly the requested byte window, in bounded blocks."""
        if start < 0 or length < 0:
            raise InvalidRequestError("invalid_range")
        if length == 0:
            return
        try:
            response = self._client.get_object(
                self.config.bucket, key, offset=start, length=length
            )
        except S3Error as exc:
            raise ObjectStoreError() from exc
        try:
            remaining = length
            while remaining > 0:
                block = response.read(min(STREAM_BLOCK_BYTES, remaining))
                if not block:
                    raise ObjectStoreError("short_read")
                remaining -= len(block)
                yield block
        finally:
            response.close()
            response.release_conn()

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(self.config.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            raise ObjectStoreError() from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self.config.bucket, key)
            return True
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return False
            raise ObjectStoreError() from exc
        except Exception as exc:
            raise ObjectStoreError() from exc

    def delete(self, key: str) -> None:
        try:
            self._client.remove_object(self.config.bucket, key)
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return
            raise ObjectStoreError() from exc
        except Exception as exc:
            raise ObjectStoreError() from exc

    def list_objects(self, prefix: str = CHUNK_PREFIX) -> list[tuple[str, datetime]]:
        try:
            return [
                (item.object_name, item.last_modified)
                for item in self._client.list_objects(
                    self.config.bucket, prefix=prefix, recursive=True
                )
            ]
        except Exception as exc:
            raise ObjectStoreError() from exc

    def list_keys(self, prefix: str = CHUNK_PREFIX) -> list[str]:
        return [key for key, _ in self.list_objects(prefix)]

    def clear_prefix(self, prefix: str = CHUNK_PREFIX) -> int:
        """Delete every object under a prefix (test cleanup only)."""
        removed = 0
        for key in self.list_keys(prefix):
            self.delete(key)
            removed += 1
        return removed


def object_store_from_env(env: Mapping[str, str] | None = None) -> S3ObjectStore | None:
    """Build the production store from CLOUD_S3_* when it is configured."""
    source = os.environ if env is None else env
    if not source.get("CLOUD_S3_BUCKET"):
        return None
    return S3ObjectStore(S3Config.from_env(source))
