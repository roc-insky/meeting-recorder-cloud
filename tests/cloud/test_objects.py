"""Unit tests for LocalObjectStore: no database, no network, no ffmpeg."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cloud.contracts import InvalidRequestError
from cloud.objects import (
    LocalObjectStore,
    ObjectStoreError,
    StorageFullError,
    chunk_object_key,
    object_store_from_env,
)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class LocalStoreCase(unittest.TestCase):
    min_free_bytes = 0

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="objects-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = LocalObjectStore(self.root, min_free_bytes=self.min_free_bytes)


class WriteTests(LocalStoreCase):
    KEY = chunk_object_key("u1", "up1", "e1", "mic", 0)

    def test_put_writes_under_root_and_reads_back(self) -> None:
        body = b"frames"
        self.store.put_verified(self.KEY, body, sha(body))
        path = self.root / self.KEY
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.store.temp_dir.iterdir()), [])

    def test_put_rejects_traversal(self) -> None:
        for key in (
            "../escape",
            "/abs/path",
            "chunks/../../x",
            "chunks//x",
            "chunks/./x",
            "a\\b",
            "",
        ):
            with self.subTest(key=key), self.assertRaises(ObjectStoreError):
                self.store.put_verified(key, b"x", sha(b"x"))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_put_rejects_symlinked_parent_escape(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="objects-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ObjectStoreError) as raised:
            self.store.put_verified("linked/object.bin", b"frames", sha(b"frames"))
        # ObjectStoreError keeps its fixed wire code; the message names the branch.
        self.assertEqual(str(raised.exception), "invalid_object_key")
        self.assertEqual(list(outside.iterdir()), [])

    def test_put_rejects_bad_checksum_and_empty_body(self) -> None:
        with self.assertRaises(ObjectStoreError):
            self.store.put_verified(self.KEY, b"x", "0" * 64)
        with self.assertRaises(ObjectStoreError):
            self.store.put_verified(self.KEY, b"", sha(b""))
        self.assertFalse((self.root / self.KEY).exists())

    def test_failed_publish_leaves_no_object(self) -> None:
        with mock.patch("cloud.objects.os.replace", side_effect=OSError(5, "io")):
            with self.assertRaises(ObjectStoreError):
                self.store.put_verified(self.KEY, b"frames", sha(b"frames"))
        self.assertFalse((self.root / self.KEY).exists())
        self.assertEqual(list(self.store.temp_dir.iterdir()), [])

    def test_enospc_maps_to_507(self) -> None:
        store = LocalObjectStore(self.root, min_free_bytes=0)
        with mock.patch(
            "cloud.objects.os.replace",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            with self.assertRaises(StorageFullError):
                store.put_verified(self.KEY, b"frames", sha(b"frames"))
        self.assertFalse((self.root / self.KEY).exists())
        self.assertEqual(list(store.temp_dir.glob(".put-*")), [])

    def test_put_rejects_when_reserve_would_be_breached(self) -> None:
        store = LocalObjectStore(self.root, min_free_bytes=1 << 62)
        with self.assertRaises(StorageFullError) as raised:
            store.put_verified(self.KEY, b"frames", sha(b"frames"))
        self.assertEqual(raised.exception.status_code, 507)
        self.assertEqual(raised.exception.code, "storage_full")
        self.assertFalse((self.root / self.KEY).exists())

    def test_overwrite_replaces_existing(self) -> None:
        self.store.put_verified(self.KEY, b"old", sha(b"old"))
        self.store.put_verified(self.KEY, b"new!", sha(b"new!"))
        self.assertEqual((self.root / self.KEY).read_bytes(), b"new!")

    def test_put_file_copies_and_verifies(self) -> None:
        source_dir = Path(tempfile.mkdtemp(prefix="objects-src-"))
        self.addCleanup(shutil.rmtree, source_dir, True)
        source = source_dir / "mix.wav"
        source.write_bytes(b"RIFFdata")
        key = "derived/u1/m1/0/mix/deadbeef.wav"
        size = self.store.put_file_verified(key, source, sha(b"RIFFdata"))
        self.assertEqual(size, 8)
        self.assertEqual((self.root / key).read_bytes(), b"RIFFdata")
        self.assertTrue(source.exists())

    def test_put_file_rejects_checksum_mismatch(self) -> None:
        source_dir = Path(tempfile.mkdtemp(prefix="objects-src-"))
        self.addCleanup(shutil.rmtree, source_dir, True)
        source = source_dir / "mix.wav"
        source.write_bytes(b"RIFFdata")
        with self.assertRaises(ObjectStoreError):
            self.store.put_file_verified("derived/u1/m1/0/mix/x.wav", source, "0" * 64)
        self.assertFalse((self.root / "derived/u1/m1/0/mix/x.wav").exists())
