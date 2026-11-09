"""voiceprint.py 的单元测试（假 extractor，PROFILE_DIR 指向临时目录，不碰真实目录与模型）。"""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import voiceprint

DIM = 512


def unit_vec(index: int) -> np.ndarray:
    """第 index 维为 1 的单位向量。"""
    v = np.zeros(DIM)
    v[index] = 1.0
    return v


def blend(a: np.ndarray, b: np.ndarray, wb: float) -> np.ndarray:
    v = (1 - wb) * a + wb * b
    return v / np.linalg.norm(v)


def speech(seconds: float, level: float = 3000.0, rate: int = 16000) -> np.ndarray:
    """int16 的 440 Hz 正弦，视为有效语音。"""
    t = np.arange(int(seconds * rate)) / rate
    return (level * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


class TmpProfileDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "voiceprints"
        patcher = mock.patch.object(voiceprint, "PROFILE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)


class ValidateNameTest(unittest.TestCase):
    def test_validate_name(self):
        for ok in ("张三", "a_b-1", "Bob", "李" * 20, "田中", "さくら", "김민", "José", "Ольга"):
            self.assertEqual(voiceprint.validate_name(ok), ok)
        for bad in ("", "李" * 21, "说话人A", "说话人", "a b", "a/b", "../x", "a.b", " 张三", "a\nb", "张三\n", "a\\b", "a:b"):
            with self.assertRaises(voiceprint.VoiceprintError, msg=repr(bad)):
                voiceprint.validate_name(bad)

    def test_default_threshold(self):
        self.assertEqual(voiceprint.DEFAULT_THRESHOLD, 0.5)


class ProfileStoreTest(TmpProfileDirTest):
    def test_save_load_roundtrip_perm_600(self):
        vec = np.random.default_rng(0).normal(size=DIM)
        path = voiceprint.save_profile("张三", vec, seconds=31.2, chunks=10, model="m.onnx")
        self.assertEqual(path, self.dir / "张三.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(data), {"version", "name", "model", "created", "seconds", "chunks", "vector"}
        )
        self.assertEqual((data["version"], data["name"], data["model"], data["chunks"]), (1, "张三", "m.onnx", 10))
        self.assertEqual(len(data["vector"]), DIM)
        self.assertAlmostEqual(float(np.linalg.norm(data["vector"])), 1.0, places=6)
        loaded = voiceprint.load_profiles()
        self.assertEqual(list(loaded), ["张三"])
        np.testing.assert_allclose(loaded["张三"], vec / np.linalg.norm(vec), atol=1e-9)

    def test_perm_600_even_with_permissive_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        path = voiceprint.save_profile("a", unit_vec(0), seconds=1, chunks=1, model="m")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_conflict_without_replace(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model="m")
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model="m")
        self.assertIn("已存在", str(cm.exception))
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(0))
        voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model="m", replace=True)
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(1))

    def test_save_atomic(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model="m")
        with mock.patch("os.replace", side_effect=OSError("磁盘错误")):
            with self.assertRaises(OSError):
                voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model="m", replace=True)
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(0))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["张三.json"])  # 无残留临时文件

    def test_save_rejects_bad_name_and_zero_vector(self):
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("../x", unit_vec(0), seconds=1, chunks=1, model="m")
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("a", np.zeros(DIM), seconds=1, chunks=1, model="m")
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()))

    def test_load_empty_when_dir_missing(self):
        self.assertEqual(voiceprint.load_profiles(), {})
        self.assertEqual(voiceprint.list_profiles(), [])

    def test_load_skips_bad_files_with_warning(self):
        voiceprint.save_profile("好", unit_vec(0), seconds=1, chunks=1, model="m")
        (self.dir / "坏.json").write_text("{不是json", encoding="utf-8")
        (self.dir / "空向量.json").write_text(
            json.dumps({"version": 1, "name": "空向量", "vector": []}), encoding="utf-8"
        )
        (self.dir / "改名.json").write_text(
            json.dumps({"version": 1, "name": "别人", "vector": [1.0]}), encoding="utf-8"
        )
        err = io.StringIO()
        loaded = voiceprint.load_profiles(stderr=err)
        self.assertEqual(list(loaded), ["好"])
        for name in ("坏.json", "空向量.json", "改名.json"):
            self.assertIn(name, err.getvalue())

    def test_list_profiles_has_no_vector(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=12.5, chunks=4, model="m")
        rows = voiceprint.list_profiles()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("vector", rows[0])
        self.assertEqual((rows[0]["name"], rows[0]["chunks"], rows[0]["seconds"]), ("张三", 4, 12.5))

    def test_delete_profile(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model="m")
        self.assertTrue(voiceprint.delete_profile("张三"))
        self.assertFalse(voiceprint.delete_profile("张三"))
        self.assertFalse(voiceprint.delete_profile("../x"))
        self.assertEqual(voiceprint.load_profiles(), {})


class EmbedTest(unittest.TestCase):
    def test_embed_unit_norm(self):
        seen = []

        def extractor(x):
            seen.append(x)
            return np.full(DIM, 3.0, dtype=np.float32)

        v = voiceprint.embed(speech(1.0), extractor=extractor)
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=6)
        self.assertEqual(seen[0].dtype, np.float32)
        self.assertLessEqual(float(np.abs(seen[0]).max()), 1.0)  # int16 已归一化到 [-1,1]

    def test_embed_zero_vector_raises(self):
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.embed(speech(1.0), extractor=lambda x: np.zeros(DIM))

    def test_enroll_too_short(self):
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            voiceprint.enroll_vector(speech(5.0), extractor=lambda x: unit_vec(0))
        self.assertEqual(str(cm.exception), "有效语音太短，请再录一次")

    def test_enroll_silence_does_not_count(self):
        samples = np.concatenate([speech(6.0), np.zeros(16000 * 20, dtype=np.int16)])
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.enroll_vector(samples, extractor=lambda x: unit_vec(0))

    def test_enroll_vector_chunks_and_average(self):
        calls = []

        def extractor(x):
            calls.append(x.size)
            return unit_vec(len(calls) % 2)

        vec, chunks, seconds = voiceprint.enroll_vector(speech(12.0), extractor=extractor)
        self.assertEqual(chunks, 4)  # 12 秒 / 3 秒
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(seconds, 12.0, places=1)
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=6)

    def test_cosine(self):
        self.assertAlmostEqual(voiceprint.cosine(unit_vec(0), unit_vec(0)), 1.0)
        self.assertAlmostEqual(voiceprint.cosine(unit_vec(0), unit_vec(1)), 0.0)
        self.assertEqual(voiceprint.cosine(np.zeros(DIM), unit_vec(0)), 0.0)


class LabelTurnsTest(unittest.TestCase):
    """音频 = 每 5 秒一段，每段首样本值作“指纹标记”，假 extractor 按标记返回预设向量。"""

    def make_audio(self, marks):
        parts = []
        for m in marks:
            seg = speech(5.0)
            seg[0] = m  # 用首样本标记这一段属于谁
            parts.append(seg)
        return np.concatenate(parts)

    def make_extractor(self, table):
        def extractor(x):
            mark = int(round(float(x[0]) * 32768.0))
            return table[mark]

        return extractor

    def test_label_turns_renames_matching_cluster(self):
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        ext = self.make_extractor({1: unit_vec(0), 2: unit_vec(1)})
        out = voiceprint.label_turns(turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext)
        self.assertEqual(out, [(0.0, 5.0, "张三"), (5.0, 10.0, 1)])

    def test_label_turns_same_name_only_best_cluster(self):
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        # 聚类 0 与声纹相似度约 0.8，聚类 1 约 0.95，只有 1 得到名字
        ext = self.make_extractor({1: blend(unit_vec(0), unit_vec(1), 0.5), 2: blend(unit_vec(0), unit_vec(1), 0.1)})
        out = voiceprint.label_turns(turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext, threshold=0.3)
        self.assertEqual(out[0][2], 0)
        self.assertEqual(out[1][2], "张三")

    def test_label_turns_two_names_two_clusters(self):
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        ext = self.make_extractor({1: unit_vec(1), 2: unit_vec(0)})
        profiles = {"张三": unit_vec(0), "李四": unit_vec(1)}
        out = voiceprint.label_turns(turns, samples, profiles=profiles, extractor=ext)
        self.assertEqual([t[2] for t in out], ["李四", "张三"])

    def test_label_turns_below_threshold_unchanged(self):
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        ext = self.make_extractor({1: unit_vec(2), 2: unit_vec(3)})
        out = voiceprint.label_turns(turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext)
        self.assertEqual(out, turns)

    def test_label_turns_no_profiles_identity(self):
        samples = self.make_audio([1])
        turns = [(0.0, 2.0, 0), (2.0, 5.0, 1)]

        def boom(x):
            raise AssertionError("无声纹时不应提取向量")

        out = voiceprint.label_turns(turns, samples, profiles={}, extractor=boom)
        self.assertEqual(out, turns)

    def test_label_turns_merges_cluster_audio(self):
        # 同一聚类两段不连续音频拼接后一次提取
        samples = self.make_audio([1, 2, 1])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1), (10.0, 15.0, 0)]
        sizes = []

        def ext(x):
            sizes.append(x.size)
            return unit_vec(0)

        voiceprint.label_turns(turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext)
        self.assertEqual(sorted(sizes), [5 * 16000, 10 * 16000])

    def test_label_turns_caps_cluster_audio_at_60s(self):
        samples = np.concatenate([speech(100.0)])
        sizes = []

        def ext(x):
            sizes.append(x.size)
            return unit_vec(0)

        voiceprint.label_turns([(0.0, 100.0, 0)], samples, profiles={"张三": unit_vec(0)}, extractor=ext)
        self.assertEqual(sizes, [60 * 16000])

    def test_label_turns_skips_too_short_cluster(self):
        samples = speech(10.0)
        out = voiceprint.label_turns(
            [(0.0, 0.5, 0)], samples, profiles={"张三": unit_vec(0)}, extractor=lambda x: unit_vec(0)
        )
        self.assertEqual(out, [(0.0, 0.5, 0)])


class DefaultExtractorTest(unittest.TestCase):
    def test_import_does_not_load_sherpa(self):
        import subprocess
        import sys

        code = "import sys, voiceprint, transcribe; sys.exit(1 if 'sherpa_onnx' in sys.modules else 0)"
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)

    def test_default_extractor_wiring_with_fake_sherpa(self):
        """用假 sherpa_onnx 模块验证接线：配置用 diarize 的嵌入模型，流按 采样率+float32 喂入。"""
        import sys
        import types

        log = []

        class Stream:
            def accept_waveform(self, rate, wave):
                log.append(("accept", rate, wave.dtype))

            def input_finished(self):
                log.append("finished")

        class Extractor:
            def __init__(self, config):
                log.append(("model", config.model))

            def create_stream(self):
                return Stream()

            def is_ready(self, stream):
                return True

            def compute(self, stream):
                return [3.0, 4.0]

        class Config:
            def __init__(self, model, num_threads=1):
                self.model = model

            def validate(self):
                return True

        fake = types.SimpleNamespace(SpeakerEmbeddingExtractorConfig=Config, SpeakerEmbeddingExtractor=Extractor)
        with mock.patch.dict(sys.modules, {"sherpa_onnx": fake}), \
                mock.patch.object(voiceprint.diarize, "ensure_models", lambda *a, **k: log.append("ensure")):
            extract = voiceprint.default_extractor()
            vec = voiceprint.embed(speech(1.0), extractor=extract)
        self.assertEqual(log[0], "ensure")
        self.assertEqual(log[1], ("model", str(voiceprint.diarize.MODEL_DIR / voiceprint.diarize.EMBEDDING_FILE)))
        self.assertEqual(log[2], ("accept", 16000, np.dtype("float32")))
        np.testing.assert_allclose(vec, [0.6, 0.8])


if __name__ == "__main__":
    unittest.main()
