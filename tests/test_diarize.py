"""diarize 模块测试：全部使用假下载与假 runner，不联网、不加载真实模型。"""
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

import numpy as np

import diarize


def make_seg_tar(path: Path) -> None:
    """造一个只含 model.onnx 的假分割模型压缩包。"""
    with tempfile.TemporaryDirectory() as d:
        inner = Path(d) / "sherpa-onnx-pyannote-segmentation-3-0"
        inner.mkdir()
        (inner / "model.onnx").write_bytes(b"seg")
        with tarfile.open(path, "w:bz2") as t:
            t.add(inner, arcname=inner.name)


def fake_download(url: str, dest: Path) -> None:
    dest = Path(dest)
    if "segmentation" in url:
        make_seg_tar(dest)
    else:
        dest.write_bytes(b"emb")


class ModelsPresentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def snapshot(self):
        return sorted((str(p.relative_to(self.dir)), p.stat().st_size if p.is_file() else -1) for p in self.dir.rglob("*"))

    def fill(self):
        seg = self.dir / diarize.SEGMENTATION_FILE
        seg.parent.mkdir(parents=True)
        seg.write_bytes(b"x")
        (self.dir / diarize.EMBEDDING_FILE).write_bytes(b"y")

    def test_empty_dir_both_missing(self):
        before = self.snapshot()
        self.assertEqual(
            diarize.models_present(self.dir), [diarize.SEGMENTATION_FILE, diarize.EMBEDDING_FILE]
        )
        self.assertEqual(self.snapshot(), before)

    def test_all_present(self):
        self.fill()
        before = self.snapshot()
        self.assertEqual(diarize.models_present(self.dir), [])
        self.assertEqual(self.snapshot(), before)

    def test_zero_byte_counts_missing(self):
        self.fill()
        (self.dir / diarize.EMBEDDING_FILE).write_bytes(b"")
        self.assertEqual(diarize.models_present(self.dir), [diarize.EMBEDDING_FILE])

    def test_nonexistent_dir_not_created(self):
        missing = self.dir / "no" / "such"
        self.assertEqual(len(diarize.models_present(missing)), 2)
        self.assertFalse(missing.exists())


class EnsureModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "models"

    def test_downloads_all_when_empty(self):
        calls = []

        def dl(url, dest):
            calls.append(url)
            fake_download(url, dest)

        seg, emb = diarize.ensure_models(self.dir, download=dl)
        self.assertEqual(len(calls), 2)
        self.assertEqual(seg, self.dir / diarize.SEGMENTATION_FILE)
        self.assertEqual(emb, self.dir / diarize.EMBEDDING_FILE)
        self.assertEqual(seg.read_bytes(), b"seg")
        self.assertEqual(emb.read_bytes(), b"emb")
        self.assertEqual(diarize.models_present(self.dir), [])
        # 下载完成后不残留 .part 与压缩包
        self.assertEqual([p.name for p in self.dir.rglob("*") if ".part" in p.name or p.suffix == ".bz2"], [])

    def test_ensure_downloads_only_missing(self):
        diarize.ensure_models(self.dir, download=fake_download)
        (self.dir / diarize.EMBEDDING_FILE).unlink()
        calls = []

        def dl(url, dest):
            calls.append(url)
            fake_download(url, dest)

        diarize.ensure_models(self.dir, download=dl)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith(diarize.EMBEDDING_FILE))
        calls.clear()
        diarize.ensure_models(self.dir, download=dl)
        self.assertEqual(calls, [])

    def test_urls(self):
        urls = []
        diarize.ensure_models(self.dir, download=lambda u, d: (urls.append(u), fake_download(u, d)))
        base = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        self.assertIn(base + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", urls)
        self.assertIn(base + "speaker-recongition-models/" + diarize.EMBEDDING_FILE, urls)

    def test_ensure_atomic_part(self):
        def dl(url, dest):
            Path(dest).write_bytes(b"partial")
            raise OSError("网络断了")

        with self.assertRaises(OSError):
            diarize.ensure_models(self.dir, download=dl)
        self.assertFalse((self.dir / diarize.EMBEDDING_FILE).exists())
        self.assertFalse((self.dir / diarize.SEGMENTATION_FILE).exists())
        self.assertEqual(diarize.models_present(self.dir), [diarize.SEGMENTATION_FILE, diarize.EMBEDDING_FILE])
        self.assertEqual([p.name for p in self.dir.rglob("*.part")], [])


class DiarizeTest(unittest.TestCase):
    def samples(self, seconds=1.0):
        return (np.ones(int(16000 * seconds)) * 1000).astype(np.int16)

    def test_diarize_renumbers_by_first_appearance(self):
        runner = lambda audio, num_clusters, threshold: [(0.0, 1.0, 3), (1.0, 2.0, 1), (2.0, 3.0, 3)]
        turns = diarize.diarize(self.samples(3), runner=runner)
        self.assertEqual(turns, [(0.0, 1.0, 0), (1.0, 2.0, 1), (2.0, 3.0, 0)])

    def test_diarize_auto_clusters_default(self):
        seen = {}

        def runner(audio, num_clusters, threshold):
            seen.update(nc=num_clusters, th=threshold, dtype=audio.dtype)
            return []

        diarize.diarize(self.samples(), runner=runner)
        self.assertEqual(seen["nc"], -1)
        self.assertEqual(seen["th"], 0.5)
        self.assertEqual(seen["dtype"], np.float32)

    def test_diarize_num_speakers_passed(self):
        seen = {}

        def runner(audio, num_clusters, threshold):
            seen.update(nc=num_clusters)
            return []

        diarize.diarize(self.samples(), num_speakers=3, runner=runner)
        self.assertEqual(seen["nc"], 3)

    def test_diarize_empty(self):
        def boom(*a, **k):
            raise AssertionError("空音频不应调用 runner")

        self.assertEqual(diarize.diarize(np.zeros(0, dtype=np.int16), runner=boom), [])

    def test_diarize_sorts_by_start(self):
        runner = lambda audio, num_clusters, threshold: [(2.0, 3.0, 5), (0.0, 1.0, 9)]
        self.assertEqual(diarize.diarize(self.samples(3), runner=runner), [(0.0, 1.0, 0), (2.0, 3.0, 1)])


if __name__ == "__main__":
    unittest.main()
