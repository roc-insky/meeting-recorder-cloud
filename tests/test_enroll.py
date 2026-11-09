"""enroll.py 的单元测试（假 extractor / 假麦克风，PROFILE_DIR 指向临时目录）。"""
import contextlib
import io
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import enroll
import voiceprint


def speech(seconds: float, rate: int = 16000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (3000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def write_wav(path: Path, samples: np.ndarray, rate: int = 16000) -> None:
    data = samples.astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


class FakeMic:
    """假输入流：打开时立即把预设块交给回调。"""

    def __init__(self, chunks, callback):
        self.chunks, self.callback = chunks, callback

    def __enter__(self):
        for c in self.chunks:
            self.callback(c)
        return self

    def __exit__(self, *exc):
        return False


def mic_factory(chunks):
    return lambda callback: FakeMic(chunks, callback)


class RecordFromMicTest(unittest.TestCase):
    def test_record_from_mic_collects_seconds(self):
        chunks = [speech(1.0) for _ in range(3)]
        out = enroll.record_from_mic(2, stream_factory=mic_factory(chunks))
        self.assertEqual(out.size, 2 * 16000)
        self.assertEqual(out.dtype, np.int16)

    def test_record_from_mic_cancel(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            enroll.record_from_mic(30, stream_factory=mic_factory([speech(1.0)]), stop=stop)
        self.assertEqual(str(cm.exception), "已取消")

    def test_record_from_mic_cancel_midway(self):
        stop = threading.Event()
        seen = []

        def progress(done):
            seen.append(done)
            stop.set()  # 收到第一块后取消

        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            enroll.record_from_mic(
                30, stream_factory=mic_factory([speech(1.0), speech(1.0)]), stop=stop, on_progress=progress
            )
        self.assertEqual(str(cm.exception), "已取消")
        self.assertEqual(len(seen), 1)

    def test_record_from_mic_open_failure(self):
        def factory(callback):
            raise OSError("没权限")

        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            enroll.record_from_mic(1, stream_factory=factory)
        self.assertIn("麦克风", str(cm.exception))


class EnrollMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profiles = self.root / "voiceprints"
        patcher = mock.patch.object(voiceprint, "PROFILE_DIR", self.profiles)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out, self.err = io.StringIO(), io.StringIO()

    def run_main(self, argv, **deps):
        deps.setdefault("extractor", lambda x: np.arange(1, 513, dtype=np.float32))
        deps.update(stdout=self.out, stderr=self.err)
        return enroll.main(argv, deps=deps)

    def test_enroll_from_file(self):
        wav = self.root / "a.wav"
        write_wav(wav, speech(12.0))
        code = self.run_main(["--name", "张三", "--file", str(wav)])
        self.assertEqual(code, 0, self.err.getvalue())
        data = json.loads((self.profiles / "张三.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["vector"]), 512)
        self.assertEqual(data["chunks"], 4)
        self.assertIn("已保存", self.out.getvalue())

    def test_enroll_from_mic_default_30s(self):
        chunks = [speech(1.0) for _ in range(30)]
        code = self.run_main(["--name", "李四"], stream_factory=mic_factory(chunks))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertTrue((self.profiles / "李四.json").exists())

    def test_enroll_too_short_exit_1(self):
        wav = self.root / "a.wav"
        write_wav(wav, speech(3.0))
        code = self.run_main(["--name", "张三", "--file", str(wav)])
        self.assertEqual(code, 1)
        self.assertIn("有效语音太短", self.err.getvalue())
        self.assertFalse(self.profiles.exists())

    def test_enroll_conflict_needs_replace_and_skips_extractor(self):
        voiceprint.save_profile("张三", np.ones(512), seconds=1, chunks=1, model=voiceprint.MODEL_NAME)
        wav = self.root / "a.wav"
        write_wav(wav, speech(12.0))

        def boom(x):
            raise AssertionError("已存在时不应提取向量")

        code = self.run_main(["--name", "张三", "--file", str(wav)], extractor=boom)
        self.assertEqual(code, 1)
        self.assertIn("--replace", self.err.getvalue())
        code = self.run_main(["--name", "张三", "--file", str(wav), "--replace"])
        self.assertEqual(code, 0, self.err.getvalue())

    def test_enroll_bad_name(self):
        code = self.run_main(["--name", "说话人A", "--file", "x.wav"])
        self.assertEqual(code, 1)
        self.assertIn("说话人", self.err.getvalue())

    def test_enroll_missing_file(self):
        code = self.run_main(["--name", "张三", "--file", str(self.root / "nope.wav")])
        self.assertEqual(code, 1)
        self.assertIn("无法读取", self.err.getvalue())

    def test_seconds_and_file_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            enroll.main(["--name", "a", "--seconds", "5", "--file", "x.wav"])
        self.assertEqual(cm.exception.code, 2)

    def test_name_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            enroll.main([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
