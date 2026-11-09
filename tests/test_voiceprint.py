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


class SuspectNameTest(TmpProfileDirTest):
    def test_register_rejects_suspect_prefix(self):
        """“疑似”是灰区标签前缀，不能用于登记名字。"""
        for name in ("疑似张三", "疑似"):
            with self.assertRaises(voiceprint.VoiceprintError, msg=name):
                voiceprint.validate_name(name)
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("疑似张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()))


class SuspectLabelTest(unittest.TestCase):
    def test_parse_label(self):
        cases = {
            "疑似张三": ("张三", True),
            "张三": ("张三", False),
            "说话人A": ("说话人A", False),
            "疑似疑似张三": ("疑似张三", True),  # 只剥一层前缀
        }
        for label, want in cases.items():
            self.assertEqual(voiceprint.parse_label(label), want, msg=label)

    def test_suspect_label(self):
        self.assertEqual(voiceprint.SUSPECT_PREFIX, "疑似")
        self.assertEqual(voiceprint.suspect_label("张三"), "疑似张三")
        self.assertEqual(voiceprint.parse_label(voiceprint.suspect_label("张三")), ("张三", True))


class ThresholdsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_config(self, text):
        path = self.dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_default_thresholds(self):
        self.assertEqual(voiceprint.DEFAULT_HIGH, 0.5)
        self.assertEqual(voiceprint.DEFAULT_LOW, 0.35)
        self.assertEqual(voiceprint.DEFAULT_THRESHOLD, voiceprint.DEFAULT_HIGH)
        self.assertEqual(voiceprint.Thresholds(0.5, 0.35), voiceprint.Thresholds(0.5, 0.35, None))

    def test_thresholds_default(self):
        """文件不存在、无 [voiceprint] 段：静默使用默认值。"""
        self.assertEqual(voiceprint.load_thresholds(None), (0.5, 0.35, None))
        self.assertEqual(voiceprint.load_thresholds(self.dir / "不存在.toml"), (0.5, 0.35, None))
        path = self.write_config("[web]\nport = 8765\n")
        self.assertEqual(voiceprint.load_thresholds(path), (0.5, 0.35, None))

    def test_thresholds_valid(self):
        path = self.write_config("[voiceprint]\nhigh = 0.6\nlow = 0.4\n")
        self.assertEqual(voiceprint.load_thresholds(path), (0.6, 0.4, None))

    def test_thresholds_invalid(self):
        """low>=high、low 越界、字符串、布尔：一律退回默认值并给中文 warning。"""
        cases = [
            "[voiceprint]\nhigh = 0.4\nlow = 0.4\n",
            "[voiceprint]\nhigh = 0.6\nlow = -0.1\n",
            '[voiceprint]\nhigh = "abc"\nlow = 0.4\n',
            "[voiceprint]\nhigh = true\nlow = 0.4\n",
        ]
        for text in cases:
            th = voiceprint.load_thresholds(self.write_config(text))
            self.assertEqual((th.high, th.low), (0.5, 0.35), msg=text)
            self.assertTrue(th.warning, msg=text)
            self.assertRegex(th.warning, r"[\u4e00-\u9fff]", msg=text)

    def test_cli_high_overrides_high_only(self):
        path = self.write_config("[voiceprint]\nlow = 0.4\n")
        self.assertEqual(voiceprint.load_thresholds(path, cli_high=0.7), (0.7, 0.4, None))
        th = voiceprint.load_thresholds(path, cli_high=0.3)
        self.assertEqual((th.high, th.low), (0.5, 0.35))
        self.assertTrue(th.warning)
        self.assertEqual(voiceprint.load_thresholds(None, cli_high=0.7), (0.7, 0.35, None))
        # 先应用 cli_high 再校验：配置里的 high 坏了，但 cli_high 合法，仍可用
        bad = self.write_config('[voiceprint]\nhigh = "abc"\nlow = 0.4\n')
        self.assertEqual(voiceprint.load_thresholds(bad, cli_high=0.7), (0.7, 0.4, None))

    def test_classify_boundaries(self):
        th = voiceprint.Thresholds(0.6, 0.4)
        self.assertEqual(voiceprint.classify_score(1.0, th), "named")
        self.assertEqual(voiceprint.classify_score(0.6, th), "named")
        self.assertEqual(voiceprint.classify_score(0.5999, th), "suspect")
        self.assertEqual(voiceprint.classify_score(0.4, th), "suspect")
        self.assertEqual(voiceprint.classify_score(0.3999, th), "anonymous")
        self.assertEqual(voiceprint.classify_score(0.0, th), "anonymous")


class ProfileStoreTest(TmpProfileDirTest):
    def test_save_load_roundtrip_perm_600(self):
        vec = np.random.default_rng(0).normal(size=DIM)
        path = voiceprint.save_profile("张三", vec, seconds=31.2, chunks=10, model=voiceprint.MODEL_NAME)
        self.assertEqual(path, self.dir / "张三.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(data), {"version", "name", "model", "created", "seconds", "chunks", "vector"}
        )
        self.assertEqual((data["version"], data["name"], data["model"], data["chunks"]), (1, "张三", voiceprint.MODEL_NAME, 10))
        self.assertEqual(len(data["vector"]), DIM)
        self.assertAlmostEqual(float(np.linalg.norm(data["vector"])), 1.0, places=6)
        loaded = voiceprint.load_profiles()
        self.assertEqual(list(loaded), ["张三"])
        np.testing.assert_allclose(loaded["张三"], vec / np.linalg.norm(vec), atol=1e-9)

    def test_perm_600_even_with_permissive_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        path = voiceprint.save_profile("a", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_conflict_without_replace(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertIn("已存在", str(cm.exception))
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(0))
        voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME, replace=True)
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(1))

    def test_save_atomic(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        with mock.patch("os.replace", side_effect=OSError("磁盘错误")):
            with self.assertRaises(OSError):
                voiceprint.save_profile("张三", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME, replace=True)
        np.testing.assert_allclose(voiceprint.load_profiles()["张三"], unit_vec(0))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["张三.json"])  # 无残留临时文件

    def test_save_rejects_bad_name_and_zero_vector(self):
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("../x", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("a", np.zeros(DIM), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()))

    def test_case_only_replace_keeps_one_loadable_profile(self):
        """Bob → bob（replace）：不论文件系统是否区分大小写，只剩 bob.json，里面的名字与文件名一致。"""
        voiceprint.save_profile("Bob", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        voiceprint.save_profile("bob", unit_vec(1), seconds=2, chunks=2, model=voiceprint.MODEL_NAME, replace=True)
        self.assertEqual(sorted(os.listdir(self.dir)), ["bob.json"])
        err = io.StringIO()
        loaded = voiceprint.load_profiles(stderr=err)
        self.assertEqual(list(loaded), ["bob"])
        self.assertEqual(err.getvalue(), "")
        np.testing.assert_allclose(loaded["bob"], unit_vec(1))
        data = json.loads((self.dir / "bob.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], Path(os.listdir(self.dir)[0]).stem)

    def test_case_only_conflict_without_replace(self):
        voiceprint.save_profile("Bob", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        with self.assertRaises(voiceprint.VoiceprintError):
            voiceprint.save_profile("bob", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertEqual(sorted(os.listdir(self.dir)), ["Bob.json"])
        self.assertEqual(list(voiceprint.load_profiles()), ["Bob"])
        self.assertEqual(voiceprint.existing_profile_path("bOB").name, "Bob.json")

    def test_case_only_replace_on_simulated_case_insensitive_fs(self):
        """模拟不区分大小写的 os.replace：目标目录里已有同名不同拼写的文件时，写进那个旧拼写的文件。"""
        voiceprint.save_profile("Bob", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        real_replace = os.replace

        def ci_replace(src, dst):
            d = Path(dst)
            for n in os.listdir(d.parent):
                if n.casefold() == d.name.casefold() and n != d.name:
                    dst = d.parent / n
                    break
            return real_replace(src, dst)

        with mock.patch("voiceprint.os.replace", ci_replace), mock.patch("common.os.replace", ci_replace):
            voiceprint.save_profile("bob", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME, replace=True)
        self.assertEqual(sorted(os.listdir(self.dir)), ["bob.json"])
        self.assertEqual(list(voiceprint.load_profiles()), ["bob"])

    def test_case_only_replace_failure_restores_old_file(self):
        voiceprint.save_profile("Bob", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        with mock.patch("common.atomic_write_text", side_effect=OSError("磁盘满")):
            with self.assertRaises(OSError):
                voiceprint.save_profile("bob", unit_vec(1), seconds=1, chunks=1, model=voiceprint.MODEL_NAME, replace=True)
        self.assertEqual(sorted(os.listdir(self.dir)), ["Bob.json"])
        np.testing.assert_allclose(voiceprint.load_profiles()["Bob"], unit_vec(0))

    def test_load_empty_when_dir_missing(self):
        self.assertEqual(voiceprint.load_profiles(), {})
        self.assertEqual(voiceprint.list_profiles(), [])

    def test_load_skips_bad_files_with_warning(self):
        voiceprint.save_profile("好", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
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
        voiceprint.save_profile("张三", unit_vec(0), seconds=12.5, chunks=4, model=voiceprint.MODEL_NAME)
        rows = voiceprint.list_profiles()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("vector", rows[0])
        self.assertEqual((rows[0]["name"], rows[0]["chunks"], rows[0]["seconds"]), ("张三", 4, 12.5))

    def test_delete_profile(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.assertTrue(voiceprint.delete_profile("张三"))
        self.assertFalse(voiceprint.delete_profile("张三"))
        self.assertFalse(voiceprint.delete_profile("../x"))
        self.assertEqual(voiceprint.load_profiles(), {})


class CompatibilityTest(TmpProfileDirTest):
    """模型/维度不同的声纹不可比：匹配载入时告警跳过，列表仍展示并标记 compatible。"""

    def write_profile(self, name, *, model, dim, with_model=True):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1, "name": name, "created": "2026-01-01T00:00:00",
            "seconds": 1.0, "chunks": 1, "vector": [1.0] + [0.0] * (dim - 1),
        }
        if with_model:
            data["model"] = model
        (self.dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_load_skips_other_model_with_warning(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.write_profile("旧模型", model="old.onnx", dim=512)
        err = io.StringIO()
        self.assertEqual(list(voiceprint.load_profiles(stderr=err)), ["张三"])
        self.assertIn("不兼容", err.getvalue())
        self.assertIn("旧模型", err.getvalue())
        self.assertIn("old.onnx", err.getvalue())

    def test_load_skips_wrong_dim_with_warning(self):
        self.write_profile("小明", model=voiceprint.MODEL_NAME, dim=4)
        err = io.StringIO()
        self.assertEqual(voiceprint.load_profiles(stderr=err), {})
        self.assertIn("维度", err.getvalue())
        self.assertIn("小明", err.getvalue())

    def test_load_skips_legacy_profile_without_model(self):
        self.write_profile("旧版", model=None, dim=512, with_model=False)
        err = io.StringIO()
        self.assertEqual(voiceprint.load_profiles(stderr=err), {})
        self.assertIn("旧版", err.getvalue())
        self.assertIn("模型", err.getvalue())

    def test_list_marks_compatibility_and_keeps_incompatible(self):
        voiceprint.save_profile("张三", unit_vec(0), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        self.write_profile("旧模型", model="old.onnx", dim=512)
        rows = {r["name"]: r for r in voiceprint.list_profiles(stderr=io.StringIO())}
        self.assertEqual(set(rows), {"张三", "旧模型"})
        self.assertTrue(rows["张三"]["compatible"])
        self.assertFalse(rows["旧模型"]["compatible"])
        self.assertNotIn("vector", rows["旧模型"])


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


class LabelTurnsWithScoresTest(unittest.TestCase):
    """按聚类分档：假音频的段首样本作标记，假 extractor 按标记返回预设向量。"""

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

    @staticmethod
    def at_score(score: float) -> np.ndarray:
        """与 unit_vec(0) 余弦恰为 score 的单位向量（放在前两维）。"""
        v = np.zeros(DIM)
        v[0] = score
        v[1] = np.sqrt(1.0 - score * score)
        return v

    def test_cluster_high_score_gets_real_name(self):
        samples = self.make_audio([1])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 0)]
        ext = self.make_extractor({1: unit_vec(0)})
        th = voiceprint.Thresholds(0.6, 0.4)
        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext, thresholds=th
        )
        self.assertEqual([t[2] for t in out], ["张三", "张三"])
        self.assertEqual(scores, {"张三": 1.0})

    def test_cluster_gray_zone_gets_suspect(self):
        """整段聚类都拿灰区分（0.5），不是逐句判断。"""
        samples = self.make_audio([1])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 0)]
        ext = self.make_extractor({1: self.at_score(0.5)})
        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext,
            thresholds=voiceprint.Thresholds(0.6, 0.4),
        )
        self.assertEqual([t[2] for t in out], ["疑似张三", "疑似张三"])
        self.assertAlmostEqual(scores["疑似张三"], 0.5, places=6)

    def test_cluster_low_stays_anonymous(self):
        samples = self.make_audio([1])
        turns = [(0.0, 5.0, 0)]
        ext = self.make_extractor({1: self.at_score(0.3)})
        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext,
            thresholds=voiceprint.Thresholds(0.6, 0.4),
        )
        self.assertEqual(out, turns)
        self.assertEqual(scores, {})

    def test_two_clusters_same_person_only_best_gets_name(self):
        """两个聚类都匹配张三：只有分高的拿名字，另一个保持匿名（不写疑似）。"""
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        ext = self.make_extractor({1: self.at_score(0.9), 2: self.at_score(0.7)})
        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext,
            thresholds=voiceprint.Thresholds(0.6, 0.4),
        )
        self.assertEqual([t[2] for t in out], ["张三", 1])
        self.assertEqual(scores, {"张三": 0.9})

    def test_loser_of_same_name_does_not_borrow_second_choice(self):
        """分低的聚类即使对别的声纹分也够，也不退而取次高名字（每个聚类只看最高分）。"""
        samples = self.make_audio([1, 2])
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        # 聚类 2 与张三、李四的分都过 low，但最高分是张三（已被聚类 1 拿走）
        mixed = np.zeros(DIM)
        mixed[0], mixed[1] = 0.7, 0.6
        mixed = mixed / np.linalg.norm(mixed)
        ext = self.make_extractor({1: self.at_score(0.9), 2: mixed})
        profiles = {"张三": unit_vec(0), "李四": unit_vec(1)}
        out, _scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles=profiles, extractor=ext,
            thresholds=voiceprint.Thresholds(0.6, 0.4),
        )
        self.assertEqual([t[2] for t in out], ["张三", 1])

    def test_no_profiles_output_unchanged(self):
        samples = self.make_audio([1])
        turns = [(0.0, 2.0, 0), (2.0, 5.0, 1)]

        def boom(x):
            raise AssertionError("无声纹时不应提取向量")

        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={}, extractor=boom, thresholds=voiceprint.Thresholds(0.6, 0.4)
        )
        self.assertEqual(out, turns)
        self.assertEqual(scores, {})

    def test_thresholds_none_falls_back_to_single_threshold(self):
        """不传 thresholds 时沿用旧的单阈值语义：达阈值即真名，没有灰区。"""
        samples = self.make_audio([1])
        turns = [(0.0, 5.0, 0)]
        ext = self.make_extractor({1: self.at_score(0.5)})
        out, scores = voiceprint.label_turns_with_scores(
            turns, samples, profiles={"张三": unit_vec(0)}, extractor=ext, threshold=0.4
        )
        self.assertEqual([t[2] for t in out], ["张三"])
        self.assertAlmostEqual(scores["张三"], 0.5, places=6)


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
