"""Health, readiness and offline warmup behavior (Task 17).

These tests use the real disposable PostgreSQL database and the per-class
local object-store root through CloudCase. They never start a deployment: the
Docker image, compose file and nginx configuration are static, unexecuted
scaffolds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from cloud.health import ModelWarmupError, model_ready, readiness, warm_models
from cloud.objects import LocalObjectStore
from tests.cloud.support import TEST_SECRET, CloudCase

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_IN_MANIFEST = REPO_ROOT / "deploy" / "cloud" / "model-manifest.json"

READINESS_KEYS = {"database", "storage", "model", "queue"}


def _route_paths(app: Any) -> set[str]:
    """Flatten the lazy included routers into their concrete paths."""
    paths: set[str] = set()

    def walk(routes: Any) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                walk(original.routes)
                continue
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)

    walk(app.routes)
    return paths


class HealthCase(CloudCase):
    """Shared fixtures: temp model dirs and explicit subprocess environments."""

    def temp_dir(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="cloud-health-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory

    def write_manifest(
        self,
        directory: Path,
        *,
        payload: bytes = b"test-only-model-bytes",
        schema_version: int = 1,
        models: Any = None,
        **entry_overrides: Any,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        weights = directory / "weights.bin"
        weights.write_bytes(payload)
        entry: dict[str, Any] = {
            "id": "test-only-engine",
            "version": "0-test",
            "runtime": "test-only",
            "file": "weights.bin",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "license": "test-only-license",
            "bytes": len(payload),
        }
        entry.update(entry_overrides)
        body = {
            "schema_version": schema_version,
            "status": "test_only",
            "models": [entry] if models is None else models,
        }
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(body), encoding="utf-8")
        return manifest

    def subprocess_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "CLOUD_DATABASE_URL": self.database_url,
            "CLOUD_SECRET_KEY": TEST_SECRET,
            "CLOUD_ENVIRONMENT": "test",
            "CLOUD_OBJECTS_DIR": str(self.object_store.root),
            "CLOUD_OBJECTS_MIN_FREE_BYTES": "0",
        }
        return env

    def run_python(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 120
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=REPO_ROOT,
            env=env or self.subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    # -- tests -------------------------------------------------------------

    def test_unwarmed_worker_not_ready(self) -> None:
        self.assertTrue(CHECKED_IN_MANIFEST.is_file())
        checked_in = json.loads(CHECKED_IN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(checked_in["models"], [])
        self.assertFalse(model_ready(CHECKED_IN_MANIFEST))
        with self.assertRaises(ModelWarmupError) as raised:
            warm_models(CHECKED_IN_MANIFEST)
        self.assertEqual(str(raised.exception), "model_manifest_empty")

        settings = self.settings.model_copy(
            update={"model_manifest_path": str(CHECKED_IN_MANIFEST)}
        )
        client = self.app_client(settings=settings)
        response = client.get("/api/cloud/health")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), READINESS_KEYS)
        self.assertIs(body["database"], True)
        self.assertIs(body["storage"], True)
        self.assertIs(body["queue"], True)
        self.assertIs(body["model"], False)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

        # Authorized capture and upload do not need an engine.
        meeting_id = self.create_meeting(self.a)
        lease = self.a.post(
            "/api/cloud/capture-lease",
            json={"meeting_id": meeting_id},
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(lease.status_code, 201, lease.text)
        upload = self.a.post(
            "/api/cloud/uploads",
            json={"meeting_id": meeting_id},
            headers={
                **self.csrf_headers(self.a),
                "X-Capture-Lease": lease.json()["lease_token"],
            },
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        self.assertEqual(self.query_count("processing_tasks"), 0)

    def test_import_never_opens_server_devices(self) -> None:
        stubs = self.temp_dir()
        (stubs / "sounddevice.py").write_text(
            "raise RuntimeError('forbidden audio device import')", encoding="utf-8"
        )
        (stubs / "mlx_whisper.py").write_text(
            "raise RuntimeError('forbidden model import')", encoding="utf-8"
        )
        env = self.subprocess_env()
        env["PYTHONPATH"] = os.pathsep.join([str(stubs), str(REPO_ROOT)])

        control = self.run_python("-c", "import sounddevice", env=env)
        self.assertNotEqual(control.returncode, 0)

        result = self.run_python(
            "-c", "import cloud.main, cloud.worker, cloud.health", env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Uvicorn running", result.stdout + result.stderr)
        self.assertNotIn("Application startup complete", result.stdout + result.stderr)

        twice = self.run_python("-c", "import cloud.main; import cloud.main", env=env)
        self.assertEqual(twice.returncode, 0, twice.stderr)
        self.assertNotIn("Uvicorn running", twice.stdout + twice.stderr)

    def test_health_has_no_secret_paths(self) -> None:
        secret = "sentinel-secret-value-0123456789abcdef"
        manifest_path = "/private/sentinel/manifest.json"
        cache_path = "/private/sentinel/cache"
        settings = self.settings.model_copy(
            update={
                "secret_key": secret,
                "voiceprint_key": "A" * 43,
                "model_manifest_path": manifest_path,
                "model_cache_dir": cache_path,
            }
        )
        client = self.app_client(settings=settings)
        response = client.get("/api/cloud/health")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), READINESS_KEYS)
        self.assertIs(body["database"], True)
        self.assertIs(body["storage"], True)
        self.assertIs(body["queue"], True)
        self.assertIs(body["model"], False)

        rendered = response.text + json.dumps(dict(response.headers))
        for sentinel in (
            secret,
            TEST_SECRET,
            manifest_path,
            cache_path,
            # The random temp-root segment, never the full path: on macOS
            # /var resolves to /private/var and the sentinel below would trip.
            self.object_store.root.name,
            "postgresql+psycopg",
            "/Users/",
            "/private/",
            "SELECT",
        ):
            self.assertNotIn(sentinel, rendered)
        for value in body.values():
            self.assertIsInstance(value, bool)

    def test_warm_models_verifies_local_cache_without_download(self) -> None:
        directory = self.temp_dir()
        payload = b"local-model-bytes-that-were-never-downloaded"
        manifest = self.write_manifest(directory, payload=payload)

        with mock.patch(
            "socket.socket.connect",
            side_effect=AssertionError("warmup must not open a network socket"),
        ), mock.patch("urllib.request.urlopen", side_effect=AssertionError("no download")):
            warm_models(manifest)
            self.assertTrue(model_ready(manifest))

        # A successful verification is cached: no re-hash on every probe.
        with mock.patch(
            "cloud.health._sha256_file",
            side_effect=AssertionError("weights were re-hashed"),
        ):
            self.assertTrue(model_ready(manifest))

        # Same length, different bytes: digest mismatch closes the gate.
        (directory / "weights.bin").write_bytes(b"x" * len(payload))
        self.assertFalse(model_ready(manifest))
        with self.assertRaises(ModelWarmupError) as raised:
            warm_models(manifest)
        self.assertEqual(str(raised.exception), "model_digest_mismatch")

        # Restored bytes verify again; a deleted file fails immediately.
        (directory / "weights.bin").write_bytes(payload)
        self.assertTrue(model_ready(manifest))
        (directory / "weights.bin").unlink()
        self.assertFalse(model_ready(manifest))
        with self.assertRaises(ModelWarmupError) as raised:
            warm_models(manifest)
        self.assertEqual(str(raised.exception), "model_file_missing")

    def test_warm_models_rejects_unsafe_or_invalid_manifests(self) -> None:
        root = self.temp_dir()

        def manifest_in(name: str, **overrides: Any) -> Path:
            return self.write_manifest(root / name, **overrides)

        empty = root / "empty"
        empty.mkdir()
        (empty / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "models": []}), encoding="utf-8"
        )
        cases = {
            "model_manifest_empty": empty / "manifest.json",
            "model_manifest_schema_version": manifest_in("schema", schema_version=2),
            "model_path_invalid": manifest_in("absolute", file="/etc/passwd"),
            "model_path_invalid_escape": manifest_in("escape", file="../weights.bin"),
            "model_manifest_invalid_entry": manifest_in("license", license=""),
            "model_sha256_malformed": manifest_in("digest", sha256="NOT-A-SHA256"),
            "model_size_mismatch": manifest_in("size", bytes=1),
            "model_file_missing": manifest_in("missing", file="absent.bin"),
        }
        for name, path in cases.items():
            code = name.rsplit("_escape", 1)[0] if name.endswith("_escape") else name
            with self.subTest(case=name):
                with self.assertRaises(ModelWarmupError) as raised:
                    warm_models(path)
                self.assertEqual(str(raised.exception), code)
                self.assertFalse(model_ready(path))

        invalid_json = root / "invalid-json"
        invalid_json.mkdir()
        (invalid_json / "manifest.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ModelWarmupError) as raised:
            warm_models(invalid_json / "manifest.json")
        self.assertEqual(str(raised.exception), "model_manifest_invalid_json")

        self.assertFalse(model_ready(root / "does-not-exist.json"))
        self.assertFalse(model_ready(None))

    def test_worker_check_fails_closed_without_warmed_model(self) -> None:
        env = self.subprocess_env()
        env["CLOUD_MODEL_MANIFEST"] = str(CHECKED_IN_MANIFEST)
        checked = self.run_python("-m", "cloud.worker", "--check", env=env)
        self.assertEqual(checked.returncode, 1, checked.stderr)
        payload = json.loads(checked.stdout)
        self.assertEqual(set(payload), READINESS_KEYS)
        self.assertEqual(
            payload,
            {"database": True, "storage": True, "model": False, "queue": True},
        )
        self.assertNotIn(self.object_store.root.name, checked.stdout + checked.stderr)

        # A warmed local manifest lets --check succeed; still no processing.
        directory = self.temp_dir()
        manifest = self.write_manifest(directory)
        env["CLOUD_MODEL_MANIFEST"] = str(manifest)
        env["CLOUD_MODEL_CACHE"] = str(directory)
        warmed = self.run_python("-m", "cloud.worker", "--check", env=env)
        self.assertEqual(warmed.returncode, 0, warmed.stderr)
        self.assertEqual(
            json.loads(warmed.stdout),
            {"database": True, "storage": True, "model": True, "queue": True},
        )

        # No processor is supplied: fail closed with the fixed code, no tasks.
        bare = self.run_python("-m", "cloud.worker", env=env, timeout=30)
        self.assertEqual(bare.returncode, 2)
        self.assertIn("worker_processor_not_configured", bare.stderr)
        self.assertNotIn("Traceback", bare.stderr)
        self.assertEqual(self.query_count("processing_tasks"), 0)

    def test_create_app_keeps_existing_routers(self) -> None:
        paths = _route_paths(self.app)
        for path in (
            "/api/cloud/health",
            "/api/cloud/auth/login",
            "/api/cloud/auth/session",
            "/api/cloud/meetings",
            "/api/cloud/meetings/{meeting_id}",
            "/api/cloud/capture-lease",
            "/api/cloud/uploads",
            "/api/cloud/meetings/{meeting_id}/events/stream",
            "/api/cloud/tasks/{task_id}/cancel",
            "/api/cloud/voiceprints",
        ):
            self.assertIn(path, paths)

    def test_readiness_body_is_exactly_four_booleans(self) -> None:
        values = readiness(self.settings, self.session_factory, self.object_store)
        self.assertEqual(set(values), READINESS_KEYS)
        self.assertTrue(all(isinstance(value, bool) for value in values.values()))

    def test_storage_unready_when_root_disappears(self) -> None:
        shutil.rmtree(self.object_store.root)
        self.addCleanup(
            lambda: LocalObjectStore(self.object_store.root, min_free_bytes=0)
        )
        response = self.a.get("/api/cloud/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["storage"])


if __name__ == "__main__":
    unittest.main()
