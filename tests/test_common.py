"""common.py 的单元测试（不触碰网络、真实模型与真实目录）。"""
import os
import struct
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

import common


def make_wav_bytes(data: bytes, *, rate: int = 16000, data_len_field=None) -> bytes:
    """手造 16 位单声道 wav；data_len_field 可伪造头里的 data 长度。"""
    if data_len_field is None:
        data_len_field = len(data)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<I", 16) + fmt
        + b"data" + struct.pack("<I", data_len_field) + data
    )


class CommonTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_constants(self):
        self.assertEqual(common.SAMPLE_RATE, 16000)
        self.assertEqual(common.MEETINGS_ROOT, Path.home() / "Meetings")
        self.assertEqual(common.MIN_FREE_BYTES, 1024 ** 3)

    def test_meeting_dir_name(self):
        self.assertEqual(
            common.meeting_dir_name(datetime(2026, 11, 9, 10, 30, 45)),
            "2026-11-09_1030",
        )

    def test_unique_dir_suffix(self):
        (self.tmp / "x").mkdir()
        (self.tmp / "x_2").mkdir()
        self.assertEqual(common.unique_dir(self.tmp, "x"), self.tmp / "x_3")
        self.assertEqual(common.unique_dir(self.tmp, "y"), self.tmp / "y")
        self.assertFalse((self.tmp / "y").exists())  # 不创建

    def test_format_ts(self):
        self.assertEqual(common.format_ts(0), "00:00:00")
        self.assertEqual(common.format_ts(3661.9), "01:01:01")
        self.assertEqual(common.format_ts(90000), "25:00:00")

    def test_free_bytes(self):
        self.assertGreater(common.free_bytes(self.tmp), 0)

    def test_is_silent(self):
        self.assertTrue(common.is_silent(np.zeros(16000, dtype=np.int16)))
        self.assertTrue(common.is_silent(np.array([], dtype=np.int16)))
        t = np.arange(16000) / 16000
        sine = (3000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        self.assertFalse(common.is_silent(sine))

    def test_atomic_write_ok(self):
        p = self.tmp / "a.txt"
        common.atomic_write_text(p, "新内容")
        self.assertEqual(p.read_text(encoding="utf-8"), "新内容")
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["a.txt"])

    def test_atomic_write_no_partial(self):
        p = self.tmp / "a.txt"
        p.write_text("旧内容", encoding="utf-8")
        with mock.patch("common.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                common.atomic_write_text(p, "新内容")
        self.assertEqual(p.read_text(encoding="utf-8"), "旧内容")
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["a.txt"])

    def test_read_wav_tolerant_header_zero(self):
        p = self.tmp / "z.wav"
        p.write_bytes(make_wav_bytes(b"\x01\x00" * 16000, data_len_field=0))
        rate, samples = common.read_wav_tolerant(p)
        self.assertEqual(rate, 16000)
        self.assertEqual(samples.dtype, np.int16)
        self.assertEqual(len(samples), 16000)

    def test_read_wav_tolerant_header_too_long(self):
        p = self.tmp / "l.wav"
        p.write_bytes(make_wav_bytes(b"\x02\x00" * 8000, data_len_field=10_000_000))
        rate, samples = common.read_wav_tolerant(p)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(samples), 8000)
        self.assertTrue((samples == 2).all())

    def test_read_wav_empty(self):
        p = self.tmp / "e.wav"
        p.write_bytes(make_wav_bytes(b"", data_len_field=0))
        self.assertEqual(p.stat().st_size, 44)
        with self.assertRaises(common.EmptyAudioError) as cm:
            common.read_wav_tolerant(p)
        self.assertEqual(str(cm.exception), "录音文件为空（没有音频样本）")
        self.assertNotIsInstance(cm.exception, common.MalformedAudioError)

    def test_read_wav_zero_frames_with_real_data_chunk_is_empty(self):
        p = self.tmp / "e2.wav"
        p.write_bytes(make_wav_bytes(b""))   # 头里 data 长度也是 0，文件刚好 44 字节
        with self.assertRaises(common.EmptyAudioError):
            common.read_wav_tolerant(p)

    def _assert_malformed(self, raw: bytes):
        p = self.tmp / "bad.wav"
        p.write_bytes(raw)
        with self.assertRaises(common.MalformedAudioError) as cm:
            common.read_wav_tolerant(p)
        self.assertNotIsInstance(cm.exception, common.EmptyAudioError)
        self.assertIn("损坏", str(cm.exception))
        # 两种异常都仍是 ValueError 子类，旧的 except ValueError 调用方不受影响
        self.assertIsInstance(cm.exception, ValueError)

    def test_read_wav_garbage_is_malformed(self):
        self._assert_malformed(b"this is not a wav file at all, just garbage bytes")

    def test_read_wav_zero_byte_file_is_malformed(self):
        self._assert_malformed(b"")

    def test_read_wav_truncated_header_is_malformed(self):
        good = make_wav_bytes(b"\x01\x00" * 100)
        self._assert_malformed(good[:20])      # 只剩 RIFF 头与半个 fmt 块
        self._assert_malformed(good[:12])      # 只有 RIFF/WAVE 标志

    def test_read_wav_missing_data_chunk_is_malformed(self):
        good = make_wav_bytes(b"\x01\x00" * 100)
        self._assert_malformed(good[:36])      # fmt 完整但没有 data 块

    def test_read_wav_missing_fmt_chunk_is_malformed(self):
        raw = b"RIFF" + struct.pack("<I", 4 + 8 + 4) + b"WAVE" + b"data" + struct.pack("<I", 4) + b"\x01\x00\x02\x00"
        self._assert_malformed(raw)

    def test_read_wav_wrong_chunk_size_is_malformed(self):
        # 在 fmt 与 data 之间塞一个声称很大的未知块，data 被它吞掉
        fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
        raw = (b"RIFF" + struct.pack("<I", 100) + b"WAVE"
               + b"fmt " + struct.pack("<I", 16) + fmt
               + b"junk" + struct.pack("<I", 0x7FFFFFFF)
               + b"data" + struct.pack("<I", 4) + b"\x01\x00\x02\x00")
        self._assert_malformed(raw)

    def test_read_wav_bad_format_fields_are_malformed(self):
        for channels, rate in ((0, 16000), (1, 0)):
            fmt = struct.pack("<HHIIHH", 1, channels, rate, 32000, 2, 16)
            raw = (b"RIFF" + struct.pack("<I", 40) + b"WAVE"
                   + b"fmt " + struct.pack("<I", 16) + fmt
                   + b"data" + struct.pack("<I", 4) + b"\x01\x00\x02\x00")
            self._assert_malformed(raw)

    def test_read_wav_normal(self):
        p = self.tmp / "n.wav"
        data = np.arange(-500, 500, dtype=np.int16)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(data.tobytes())
        rate, samples = common.read_wav_tolerant(p)
        self.assertEqual(rate, 16000)
        np.testing.assert_array_equal(samples, data)


if __name__ == "__main__":
    unittest.main()
