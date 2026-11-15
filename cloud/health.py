"""Local readiness probes and offline model warmup (Task 17).

The four readiness booleans are the only health surface this deployment
exposes. Nothing here ever returns a filesystem path, the object-store root,
engine name, key, environment value, exception message or row count: probes
catch every failure and collapse it to ``False``.

Two rules keep the deployment honest:

* the model probe never downloads, installs or imports an ASR/MLX/audio
  stack; it only hashes bytes that an operator already placed on disk, so an
  unwarmed host reports ``model: false`` instead of failing startup;
* the probe cache remembers only successful local verification, keyed by the
  manifest and file metadata, so a large weights file is not re-hashed on
  every health request while a deleted or replaced file still fails closed.

This is Linux deployment code. It has no import of mlx_whisper, sounddevice
or any MLX package, at import time or otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from cloud.schema import ProcessingTask
from cloud.settings import Settings

#: The exact body of GET /api/cloud/health. Order is the order clients see.
READINESS_KEYS = ("database", "storage", "model", "queue")

MODEL_SCHEMA_VERSION = 1

_DB_PROBE_TIMEOUT_MS = 2000
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_MODEL_FIELDS = ("id", "version", "runtime", "file", "sha256", "license", "bytes")
_HASH_BLOCK_BYTES = 1024 * 1024
_MAX_CACHED_MANIFESTS = 16


class ModelWarmupError(RuntimeError):
    """Local model verification failed.

    The message is a fixed code, never a path, digest or exception text, so a
    startup failure can be logged without leaking operator topology.
    """


def _fail(code: str) -> None:
    raise ModelWarmupError(code)


# (absolute path, size, mtime_ns) for every verified model file
VerifiedFile = tuple[str, int, int]
# (absolute manifest path, mtime_ns, size, absolute cache dir or "")
VerifiedManifest = tuple[str, int, int, str]

_verified: dict[VerifiedManifest, tuple[VerifiedFile, ...]] = {}
_verified_lock = threading.Lock()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_text(entry: dict, name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str) or not value.strip():
        _fail("model_manifest_invalid_entry")
    return value


def _resolve_model_file(base: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        _fail("model_path_invalid")
    if ".." in candidate.parts:
        _fail("model_path_invalid")
    base_resolved = base.resolve()
    resolved = (base_resolved / candidate).resolve()
    if base_resolved not in resolved.parents:
        _fail("model_path_invalid")
    return resolved


def _verify_entry(base: Path, entry: Any) -> VerifiedFile:
    if not isinstance(entry, dict):
        _fail("model_manifest_invalid_entry")
    for name in _REQUIRED_MODEL_FIELDS:
        if name not in entry:
            _fail("model_manifest_invalid_entry")
    for name in ("id", "version", "runtime", "license"):
        _entry_text(entry, name)
    raw_file = _entry_text(entry, "file")
    digest_text = _entry_text(entry, "sha256")
    if not _SHA256_HEX.match(digest_text):
        _fail("model_sha256_malformed")
    size = entry.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _fail("model_manifest_invalid_entry")
    path = _resolve_model_file(base, raw_file)
    try:
        stat = path.stat()
    except OSError:
        _fail("model_file_missing")
    if not path.is_file():
        _fail("model_file_missing")
    if stat.st_size != size:
        _fail("model_size_mismatch")
    try:
        actual = _sha256_file(path)
    except OSError:
        _fail("model_file_unreadable")
    if actual != digest_text:
        _fail("model_digest_mismatch")
    return (str(path), stat.st_size, stat.st_mtime_ns)


def _read_manifest(manifest: Path) -> tuple[dict, os.stat_result]:
    try:
        stat = manifest.stat()
    except OSError:
        _fail("model_manifest_missing")
    if not manifest.is_file():
        _fail("model_manifest_missing")
    try:
        raw = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _fail("model_manifest_unreadable")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _fail("model_manifest_invalid_json")
    if not isinstance(data, dict):
        _fail("model_manifest_invalid_json")
    if data.get("schema_version") != MODEL_SCHEMA_VERSION:
        _fail("model_manifest_schema_version")
    models = data.get("models")
    if not isinstance(models, list):
        _fail("model_manifest_invalid_entry")
    if not models:
        _fail("model_manifest_empty")
    return data, stat


def _verify_manifest(
    manifest: Path, cache_dir: str | Path | None
) -> tuple[VerifiedManifest, tuple[VerifiedFile, ...]]:
    path = Path(manifest)
    data, stat = _read_manifest(path)
    base = Path(cache_dir) if cache_dir else path.parent
    files = tuple(_verify_entry(base, entry) for entry in data["models"])
    key: VerifiedManifest = (
        str(path.absolute()),
        stat.st_mtime_ns,
        stat.st_size,
        str(Path(cache_dir).absolute()) if cache_dir else "",
    )
    return key, files


def _remember(key: VerifiedManifest, files: tuple[VerifiedFile, ...]) -> None:
    with _verified_lock:
        if len(_verified) >= _MAX_CACHED_MANIFESTS:
            _verified.clear()
        _verified[key] = files


def warm_models(manifest: Path, *, cache_dir: str | Path | None = None) -> None:
    """Verify an operator-provided manifest and its local model files.

    Every failure mode raises :class:`ModelWarmupError` with a fixed code.
    Nothing is downloaded or installed, and no engine is imported: this
    function only proves that the bytes an operator mounted match the
    manifest they wrote. ``cache_dir`` defaults to the manifest directory.
    """
    key, files = _verify_manifest(Path(manifest), cache_dir)
    _remember(key, files)


def _still_verified(files: tuple[VerifiedFile, ...]) -> bool:
    for name, size, mtime_ns in files:
        try:
            stat = os.stat(name)
        except OSError:
            return False
        if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
            return False
    return True


def model_ready(
    manifest: Path | str | None, *, cache_dir: str | Path | None = None
) -> bool:
    """True only when the configured local model bytes verify right now.

    Never raises. A successful verification is cached by the manifest and
    file metadata; a later probe re-stats the files but does not re-hash them
    unless the manifest or a file changed. A deleted file fails immediately.
    """
    if manifest is None:
        return False
    try:
        path = Path(manifest)
        stat = path.stat()
    except (OSError, TypeError, ValueError):
        return False
    key: VerifiedManifest = (
        str(path.absolute()),
        stat.st_mtime_ns,
        stat.st_size,
        str(Path(cache_dir).absolute()) if cache_dir else "",
    )
    with _verified_lock:
        cached = _verified.get(key)
    if cached is not None:
        if _still_verified(cached):
            return True
        with _verified_lock:
            _verified.pop(key, None)
    try:
        warm_models(path, cache_dir=cache_dir)
    except ModelWarmupError:
        return False
    except Exception:
        return False
    return True


def _probe(session_factory: Callable[[], Session] | None, statement: Any) -> bool:
    """Run one statement on a fresh, short-lived session; collapse failure.

    ``statement_timeout`` bounds query time so a wedged database cannot hold
    the health endpoint open. The session is rolled back and closed even when
    the probe succeeds, so no connection or transaction leaks.
    """
    if session_factory is None:
        return False
    session: Session | None = None
    try:
        session = session_factory()
        session.execute(text(f"SET LOCAL statement_timeout = {_DB_PROBE_TIMEOUT_MS}"))
        session.execute(statement)
        return True
    except Exception:
        return False
    finally:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
            try:
                session.close()
            except Exception:
                pass


def _storage_ready(object_store: object) -> bool:
    """True when the object store root is present, writable and above reserve.

    The probe is the store's own ``ready()``: it re-stats the root and its free
    space instead of trusting the value captured at construction, so a removed
    volume or a vanished directory fails closed. A store without that method
    is not a usable object store and reports False.
    """
    probe = getattr(object_store, "ready", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return False


def _model_from_settings(settings: Settings) -> bool:
    manifest = getattr(settings, "model_manifest_path", None)
    if not manifest:
        return False
    return model_ready(manifest, cache_dir=getattr(settings, "model_cache_dir", None))


def readiness(
    settings: Settings,
    session_factory: Callable[[], Session] | None,
    object_store: Any = None,
) -> dict[str, bool]:
    """Return the four readiness booleans; never raises and never leaks detail.

    ``database`` and ``queue`` each use a fresh short-lived session with a
    short statement timeout. ``storage`` checks that the object store root is
    present, writable and above its free-space reserve. ``model`` is true only
    when the local manifest and its files verify; it is expected to be false
    until G0/G2 approve real weights.
    """
    return {
        "database": _probe(session_factory, text("SELECT 1")),
        "storage": _storage_ready(object_store),
        "model": _model_from_settings(settings),
        "queue": _probe(session_factory, select(ProcessingTask.id).limit(1)),
    }


router = APIRouter(prefix="/api/cloud", tags=["health"])


@router.get("/health")
def health_route(request: Request) -> JSONResponse:
    """Readiness for the proxy and operators; no auth, no detail, no caching.

    Model readiness is deliberately separate: authorized uploads still work
    without an engine, so the endpoint answers 200 while ``model`` is false.
    Any failed core dependency answers 503.
    """
    values = readiness(
        request.app.state.settings,
        getattr(request.app.state, "session_factory", None),
        getattr(request.app.state, "object_store", None),
    )
    core_ready = values["database"] and values["storage"] and values["queue"]
    return JSONResponse(
        content=values,
        status_code=200 if core_ready else 503,
        headers={"Cache-Control": "no-store"},
    )
