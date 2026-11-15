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

    def test_put_creates_every_parent_at_0750(self) -> None:
        self.store.put_verified(self.KEY, b"frames", sha(b"frames"))
        directory = (self.root / self.KEY).parent
        while directory != self.root:
            self.assertEqual(directory.stat().st_mode & 0o777, 0o750, directory)
            directory = directory.parent

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


class ReadTests(LocalStoreCase):
    KEY = "chunks/u1/up1/e1/mic/0000000000.bin"
    BODY = bytes(range(256)) * 900  # 230_400 bytes, > 3 stream blocks

    def setUp(self) -> None:
        super().setUp()
        self.store.put_verified(self.KEY, self.BODY, sha(self.BODY))

    def test_object_size_and_exists(self) -> None:
        self.assertEqual(self.store.object_size(self.KEY), len(self.BODY))
        self.assertIsNone(self.store.object_size("chunks/missing.bin"))
        self.assertTrue(self.store.exists(self.KEY))
        self.assertFalse(self.store.exists("chunks/missing.bin"))
        self.assertFalse(self.store.exists("chunks"))  # directory is not an object

    def test_iter_range_returns_exact_window_in_bounded_blocks(self) -> None:
        blocks = list(self.store.iter_range(self.KEY, 100, 200_000))
        self.assertTrue(all(len(block) <= 65536 for block in blocks))
        self.assertEqual(b"".join(blocks), self.BODY[100:200_100])
        self.assertEqual(list(self.store.iter_range(self.KEY, 0, 0)), [])

    def test_iter_range_short_read_raises(self) -> None:
        with self.assertRaises(ObjectStoreError):
            list(self.store.iter_range(self.KEY, 0, len(self.BODY) + 1))

    def test_iter_range_rejects_negative(self) -> None:
        with self.assertRaises(InvalidRequestError):
            self.store.iter_range(self.KEY, -1, 10)

    def test_reads_missing_key_raise(self) -> None:
        with self.assertRaises(ObjectStoreError):
            self.store.get_bytes("chunks/missing.bin")
        # Eager: the call itself raises, before any block is consumed, so
        # playback maps a missing object before it starts streaming.
        with self.assertRaises(ObjectStoreError):
            self.store.iter_range("chunks/missing.bin", 0, 1)

    def test_get_bytes_returns_whole_object(self) -> None:
        self.assertEqual(self.store.get_bytes(self.KEY), self.BODY)

    def test_download_replaces_destination(self) -> None:
        destination = Path(tempfile.mkdtemp(prefix="objects-out-")) / "out.bin"
        self.addCleanup(shutil.rmtree, destination.parent, True)
        destination.write_bytes(b"stale")
        self.store.download_to(self.KEY, destination)
        self.assertEqual(destination.read_bytes(), self.BODY)
        self.assertFalse(destination.with_name("out.bin.part").exists())

    def test_download_missing_leaves_no_part(self) -> None:
        destination = Path(tempfile.mkdtemp(prefix="objects-out-")) / "out.bin"
        self.addCleanup(shutil.rmtree, destination.parent, True)
        with self.assertRaises(ObjectStoreError):
            self.store.download_to("chunks/missing.bin", destination)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name("out.bin.part").exists())

    def test_download_failure_removes_part_and_keeps_destination(self) -> None:
        destination = Path(tempfile.mkdtemp(prefix="objects-out-")) / "out.bin"
        self.addCleanup(shutil.rmtree, destination.parent, True)
        destination.write_bytes(b"stale")
        with mock.patch("cloud.objects.shutil.copyfileobj", side_effect=OSError(errno.EIO, "boom")):
            with self.assertRaises(ObjectStoreError):
                self.store.download_to(self.KEY, destination)
        self.assertEqual(destination.read_bytes(), b"stale")
        self.assertFalse(destination.with_name("out.bin.part").exists())


class MaintenanceTests(LocalStoreCase):
    def test_delete_is_idempotent_and_prunes_empty_dirs(self) -> None:
        key = "derived/u1/m1/0/mix/abc.wav"
        self.store.put_verified(key, b"x", sha(b"x"))
        self.store.delete(key)
        self.assertFalse((self.root / key).exists())
        self.assertFalse((self.root / "derived/u1/m1/0/mix").exists())
        self.assertTrue(self.root.is_dir())
        self.store.delete(key)  # second delete is a no-op

    def test_delete_keeps_non_empty_parent(self) -> None:
        self.store.put_verified("derived/u1/m1/0/mix/a.wav", b"a", sha(b"a"))
        self.store.put_verified("derived/u1/m1/0/mix/b.wav", b"b", sha(b"b"))
        self.store.delete("derived/u1/m1/0/mix/a.wav")
        self.assertTrue((self.root / "derived/u1/m1/0/mix/b.wav").exists())

    def test_list_is_sorted_with_utc_mtime(self) -> None:
        self.store.put_verified("chunks/a/1.bin", b"a", sha(b"a"))
        self.store.put_verified("chunks/b/2.bin", b"b", sha(b"b"))
        listed = self.store.list_objects("chunks/")
        self.assertEqual([key for key, _ in listed], ["chunks/a/1.bin", "chunks/b/2.bin"])
        for _, modified in listed:
            self.assertIsNotNone(modified.tzinfo)
            self.assertEqual(modified.utcoffset().total_seconds(), 0)

    def test_list_missing_prefix_is_empty(self) -> None:
        self.assertEqual(self.store.list_objects("chunks/"), [])

    def test_list_skips_temp_dir_and_symlinks(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="objects-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.bin").write_bytes(b"secret")
        self.store.put_verified("chunks/real.bin", b"real", sha(b"real"))
        (self.store.temp_dir / ".put-leftover").write_bytes(b"x")
        (self.root / "chunks/link.bin").symlink_to(outside / "secret.bin")
        (self.root / "chunks/.tmp").mkdir()
        (self.root / "chunks/.tmp/ghost.bin").write_bytes(b"ghost")
        self.assertEqual(self.store.list_keys("chunks/"), ["chunks/real.bin"])
        # The same escaping symlink is rejected on the read path too.
        with self.assertRaises(ObjectStoreError):
            self.store.get_bytes("chunks/link.bin")

    def test_clear_prefix_deletes_only_that_subtree(self) -> None:
        self.store.put_verified("chunks/a.bin", b"a", sha(b"a"))
        self.store.put_verified("chunks/b.bin", b"b", sha(b"b"))
        self.store.put_verified("derived/c.bin", b"c", sha(b"c"))
        self.assertEqual(self.store.clear_prefix("chunks/"), 2)
        self.assertEqual(self.store.list_keys("chunks/"), [])
        self.assertEqual(self.store.list_keys("derived/"), ["derived/c.bin"])


class ReadinessTests(LocalStoreCase):
    def test_ready_true_for_writable_root(self) -> None:
        self.assertTrue(self.store.ready())

    def test_ready_false_without_root(self) -> None:
        shutil.rmtree(self.root)
        self.assertFalse(self.store.ready())

    def test_ready_false_below_reserve(self) -> None:
        store = LocalObjectStore(self.root, min_free_bytes=1 << 62)
        self.assertFalse(store.ready())

    def test_init_rejects_unusable_root(self) -> None:
        with self.assertRaises(ObjectStoreError):
            LocalObjectStore(Path("/dev/null/nope"), min_free_bytes=0)
        with self.assertRaises(ValueError):
            LocalObjectStore(self.root, min_free_bytes=-1)

    def test_sweep_returns_zero_without_temp_dir(self) -> None:
        shutil.rmtree(self.store.temp_dir)
        self.assertEqual(self.store.sweep_stale_temp(max_age_s=0), 0)

    def test_sweep_skips_symlinks(self) -> None:
        old = 1_000_000.0
        link = self.store.temp_dir / ".put-link"
        link.symlink_to(self.root / "chunks")
        os.utime(link, (old, old), follow_symlinks=False)
        self.assertEqual(self.store.sweep_stale_temp(max_age_s=60), 0)
        self.assertTrue(link.is_symlink())

    def test_sweep_removes_only_stale_regular_files(self) -> None:
        fresh = self.store.temp_dir / ".put-fresh"
        stale = self.store.temp_dir / ".put-stale"
        nested = self.store.temp_dir / "nested"
        fresh.write_bytes(b"f")
        stale.write_bytes(b"s")
        nested.mkdir()
        (nested / ".put-inside").write_bytes(b"n")
        old = 1_000_000.0
        os.utime(stale, (old, old))
        os.utime(nested, (old, old))
        self.assertEqual(self.store.sweep_stale_temp(max_age_s=60), 1)
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(nested.exists())


class FactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="objects-factory-")
        self.addCleanup(self._tmp.cleanup)

    def test_returns_none_without_directory(self) -> None:
        self.assertIsNone(object_store_from_env({}))

    def test_rejects_relative_directory(self) -> None:
        with self.assertRaises(ObjectStoreError):
            object_store_from_env({"CLOUD_OBJECTS_DIR": "relative/dir"})

    def test_reads_reserve_and_creates_temp_dir(self) -> None:
        env = {
            "CLOUD_OBJECTS_DIR": self._tmp.name,
            "CLOUD_OBJECTS_MIN_FREE_BYTES": "4096",
        }
        store = object_store_from_env(env)
        self.assertIsNotNone(store)
        assert store is not None
        self.assertEqual(store.min_free_bytes, 4096)
        self.assertTrue(store.temp_dir.is_dir())

    def test_zero_reserve_is_honoured(self) -> None:
        store = object_store_from_env(
            {"CLOUD_OBJECTS_DIR": self._tmp.name, "CLOUD_OBJECTS_MIN_FREE_BYTES": "0"}
        )
        self.assertIsNotNone(store)
        assert store is not None
        self.assertEqual(store.min_free_bytes, 0)

    def test_blank_directory_is_unconfigured(self) -> None:
        self.assertIsNone(object_store_from_env({"CLOUD_OBJECTS_DIR": "   "}))

    def test_unusable_directory_uses_documented_code(self) -> None:
        with self.assertRaises(ObjectStoreError) as caught:
            object_store_from_env({"CLOUD_OBJECTS_DIR": "/dev/null/nope"})
        self.assertEqual(str(caught.exception), "unusable_objects_dir")

    def test_rejects_bad_reserve(self) -> None:
        for value in ("abc", "-1", "   "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                object_store_from_env(
                    {"CLOUD_OBJECTS_DIR": self._tmp.name, "CLOUD_OBJECTS_MIN_FREE_BYTES": value}
                )

