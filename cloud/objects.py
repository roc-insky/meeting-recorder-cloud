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
from typing import Any, Mapping

from minio import Minio
from minio.error import S3Error

from cloud.contracts import CloudError, InvalidRequestError

CHUNK_PREFIX = "chunks/"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEQUENCE_WIDTH = 10
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "NoSuchObject"})


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

    # -- reads and maintenance -------------------------------------------

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
