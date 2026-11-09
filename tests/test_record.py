"""record.py 的单元测试（假流、假 run，不使用真实麦克风、afconvert、模型或真实目录）。"""
import contextlib
import io
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

import common
import record
import voiceprint


def tone(seconds: float = 1.0, amp: int = 3000) -> np.ndarray:
    t = np.arange(int(16000 * seconds)) / 16000
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


class FakeStream:
    """假输入流：进入时把若干块交给回调，然后（可选）置 stop。"""

    def __init__(self, callback, chunks, stop=None):
        self.callback = callback
        self.chunks = chunks
        self.stop = stop
        self.exited = False

    def __enter__(self):
        for c in self.chunks:
            self.callback(c)
        if self.stop is not None:
            self.stop.set()
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False


def fake_factory(chunks, stop=None):
    return lambda callback: FakeStream(callback, chunks, stop)


class WavWriterTest(unittest.TestCase):
    def test_wavwriter_header_synced(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            w = record.WavWriter(path)
            try:
                w.write(tone(2.5))  # 不 close，模拟崩溃
                rate, samples = common.read_wav_tolerant(path)
                self.assertEqual(rate, 16000)
                self.assertGreaterEqual(samples.size, 2 * 16000)
                raw = path.read_bytes()
                data_len = int.from_bytes(raw[40:44], "little")
                self.assertGreaterEqual(data_len, 2 * 16000 * 2)
            finally:
                w.close()

    def test_wavwriter_header_synced_in_small_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            w = record.WavWriter(path)
            try:
                for _ in range(5):
                    w.write(tone(0.5))
                raw = path.read_bytes()
                self.assertGreaterEqual(int.from_bytes(raw[40:44], "little"), 2 * 16000 * 2)
            finally:
                w.close()

    def test_wavwriter_close_exact(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            samples = tone(1.7)
            w = record.WavWriter(path)
            w.write(samples[:5000])
            w.write(samples[5000:])
            w.close()
            w.close()  # 重复关闭无害
            with wave.open(str(path), "rb") as r:
                self.assertEqual(r.getframerate(), 16000)
                self.assertEqual(r.getnchannels(), 1)
                self.assertEqual(r.getsampwidth(), 2)
                got = np.frombuffer(r.readframes(r.getnframes()), dtype="<i2")
            np.testing.assert_array_equal(got, samples)


class LevelAndSilenceTest(unittest.TestCase):
    def test_level_bar(self):
        self.assertEqual(len(record.level_bar(0.0)), 20)
        self.assertEqual(len(record.level_bar(0.05, width=10)), 10)
        self.assertEqual(record.level_bar(0.0), record.level_bar(0.0))
        self.assertNotEqual(record.level_bar(0.0), record.level_bar(0.05))
        # 超大值被截断为满格；负值按 0 处理
        self.assertEqual(record.level_bar(5.0), record.level_bar(1.0))
        self.assertEqual(record.level_bar(-1.0), record.level_bar(0.0))

    def test_silence_watch(self):
        w = record.SilenceWatch(threshold_s=10.0)
        quiet = np.zeros(1600, dtype=np.int16)
        loud = tone(0.1)
        self.assertFalse(w.update(quiet, 0.0))
        self.assertFalse(w.update(quiet, 9.9))
        self.assertTrue(w.update(quiet, 10.0))
        self.assertFalse(w.update(quiet, 11.0))  # 同一段静音只触发一次
        self.assertFalse(w.update(quiet, 30.0))
        self.assertFalse(w.update(loud, 31.0))  # 恢复
        self.assertFalse(w.update(quiet, 32.0))  # 重新计时
        self.assertFalse(w.update(quiet, 41.9))
        self.assertTrue(w.update(quiet, 42.0))


class OpenStreamTest(unittest.TestCase):
    def test_open_stream_timeout(self):
        names = []

        def factory():
            names.append(threading.current_thread().name)
            threading.Event().wait()

        t0 = time.monotonic()
        with self.assertRaises(record.RecorderError) as cm:
            record.open_stream(factory, timeout=0.2)
        elapsed = time.monotonic() - t0
        self.assertIn("打开麦克风超时", str(cm.exception))
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(names, ["mr-stream-open"])

    def test_open_stream_thread_is_daemon(self):
        seen = []

        class CM:
            def __enter__(self):
                t = threading.current_thread()
                seen.append((t.name, t.daemon))
                return self

            def __exit__(self, *a):
                return False

        cm = record.open_stream(lambda: CM())
        self.assertEqual(seen, [("mr-stream-open", True)])
        self.assertIsNotNone(cm)

    def test_open_stream_error(self):
        def factory():
            raise OSError("no device")

        with self.assertRaises(record.RecorderError) as cm:
            record.open_stream(factory)
        self.assertIn("无法打开麦克风", str(cm.exception))

    def test_close_stream_does_not_block(self):
        names = []

        class Stuck:
            def __exit__(self, *a):
                t = threading.current_thread()
                names.append((t.name, t.daemon))
                threading.Event().wait()

        t0 = time.monotonic()
        ok = record.close_stream(Stuck(), timeout=0.2)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(names, [("mr-stream-close", True)])

    def test_close_stream_normal(self):
        s = FakeStream(lambda c: None, [])
        self.assertTrue(record.close_stream(s))
        self.assertTrue(s.exited)


class RecordTest(unittest.TestCase):
    def test_no_empty_dir_when_open_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def bad(callback):
                raise OSError("denied")

            with self.assertRaises(record.RecorderError):
                record.record(root, stream_factory=bad, stop=threading.Event())
            self.assertEqual(list(root.iterdir()), [])

    def test_low_disk(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def factory(cb):
                calls.append(1)
                raise AssertionError("空间不足时不应打开麦克风")

            with self.assertRaises(record.RecorderError) as cm:
                record.record(
                    Path(d) / "新建" / "root",
                    stream_factory=factory,
                    stop=threading.Event(),
                    free_bytes_fn=lambda p: common.MIN_FREE_BYTES - 1,
                )
            self.assertIn("磁盘空间不足", str(cm.exception))
            self.assertEqual(calls, [])
            self.assertFalse((Path(d) / "新建").exists())

    def test_record_stop_writes_wav(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()
            chunks = [tone(0.5), tone(0.5), tone(0.5)]
            levels, got = [], []
            out = record.record(
                Path(d),
                stream_factory=fake_factory(chunks, stop),
                stop=stop,
                now=lambda: datetime(2026, 11, 9, 10, 30),
                on_level=levels.append,
                on_samples=lambda s: got.append(s.size),
                free_bytes_fn=lambda p: common.MIN_FREE_BYTES,
            )
            self.assertEqual(out, Path(d) / "2026-11-09_1030")
            rate, samples = common.read_wav_tolerant(out / "meeting.wav")
            self.assertEqual(samples.size, 3 * 8000)
            self.assertEqual(len(levels), 3)
            self.assertTrue(all(0 < v < 1 for v in levels))
            self.assertEqual(got, [8000, 8000, 8000])

    def test_record_accepts_2d_chunks_and_dedups_dir(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "2026-11-09_1030").mkdir()
            stop = threading.Event()
            chunk = tone(0.5).reshape(-1, 1)
            out = record.record(
                Path(d),
                stream_factory=fake_factory([chunk], stop),
                stop=stop,
                now=lambda: datetime(2026, 11, 9, 10, 30),
                free_bytes_fn=lambda p: common.MIN_FREE_BYTES,
            )
            self.assertEqual(out.name, "2026-11-09_1030_2")
            _r, samples = common.read_wav_tolerant(out / "meeting.wav")
            self.assertEqual(samples.size, 8000)

    def test_record_keyboard_interrupt_is_stop(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()

            def interrupt(_rms):
                raise KeyboardInterrupt  # 模拟用户在录音循环中按 Ctrl-C

            out = record.record(
                Path(d),
                stream_factory=fake_factory([tone(0.5)]),
                stop=stop,
                on_level=interrupt,
                free_bytes_fn=lambda p: common.MIN_FREE_BYTES,
            )
            _r, samples = common.read_wav_tolerant(out / "meeting.wav")
            self.assertEqual(samples.size, 8000)


class CompressTest(unittest.TestCase):
    def test_compress_m4a_args(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, b"", b"")

        wav = Path("/tmp/我的 会议/meeting.wav")
        m4a = Path("/tmp/我的 会议/meeting.m4a")
        self.assertTrue(record.compress_m4a(wav, m4a, run=fake_run))
        args, kwargs = calls[0]
        self.assertIsInstance(args, list)
        self.assertEqual(args, ["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)])
        self.assertFalse(kwargs.get("shell", False))

    def test_compress_failure_keeps_wav(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "meeting.wav"
            wav.write_bytes(b"x")
            m4a = Path(d) / "meeting.m4a"
            err = io.StringIO()

            def failing(args, **kwargs):
                m4a.write_bytes(b"partial")
                raise subprocess.CalledProcessError(1, args)

            self.assertFalse(record.compress_m4a(wav, m4a, run=failing, stderr=err))
            self.assertTrue(wav.exists())
            self.assertFalse(m4a.exists())
            self.assertIn("警告", err.getvalue())

    def test_compress_missing_binary(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "meeting.wav"
            wav.write_bytes(b"x")

            def missing(args, **kwargs):
                raise FileNotFoundError("afconvert")

            self.assertFalse(
                record.compress_m4a(wav, Path(d) / "m.m4a", run=missing, stderr=io.StringIO())
            )
            self.assertTrue(wav.exists())


class MainTest(unittest.TestCase):
    def setUp(self):
        # 隔离声纹目录：main 会走到 transcribe，不能读到开发机上真实登记的声纹
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(voiceprint, "PROFILE_DIR", Path(self._tmp.name) / "no-voiceprints")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_deps(self, d, chunks, **extra):
        stop = threading.Event()
        runs = []

        def fake_run(args, **kwargs):
            runs.append(args)
            Path(args[-1]).write_bytes(b"m4a")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        deps = {
            "stream_factory": fake_factory(chunks, stop),
            "stop": stop,
            "run": fake_run,
            "stderr": io.StringIO(),
            "stdout": io.StringIO(),
            "now": lambda: datetime(2026, 11, 9, 10, 30),
            "free_bytes_fn": lambda p: common.MIN_FREE_BYTES,
            "transcribe_fn": lambda audio, **kw: {
                "segments": [{"start": 0.0, "end": 1.0, "text": "你好", "no_speech_prob": 0.0}]
            },
        }
        deps.update(extra)
        return deps, runs

    def test_main_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            deps, runs = self.make_deps(d, [tone(1.0)])
            code = record.main(["--out", d, "--no-diarize"], deps=deps)
            self.assertEqual(code, 0)
            mdir = Path(d) / "2026-11-09_1030"
            self.assertTrue((mdir / "meeting.wav").exists())
            self.assertTrue((mdir / "meeting.m4a").exists())
            self.assertIn("你好", (mdir / "transcript.md").read_text(encoding="utf-8"))
            self.assertEqual(runs[0][0], "afconvert")

    def test_main_record_error_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            def bad(cb):
                raise OSError("denied")

            deps, _ = self.make_deps(d, [], stream_factory=bad)
            code = record.main(["--out", d], deps=deps)
            self.assertEqual(code, 1)
            self.assertIn("无法打开麦克风", deps["stderr"].getvalue())
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_main_transcribe_failure_keeps_wav(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(audio, **kw):
                raise RuntimeError("模型坏了")

            deps, _ = self.make_deps(d, [tone(1.0)], transcribe_fn=boom)
            code = record.main(["--out", d, "--no-diarize"], deps=deps)
            self.assertEqual(code, record.EXIT_TRANSCRIBE_FAILED)
            mdir = Path(d) / "2026-11-09_1030"
            self.assertTrue((mdir / "meeting.wav").exists())
            self.assertFalse((mdir / "transcript.md").exists())
            self.assertIn("录音已保存", deps["stderr"].getvalue())

    def test_main_compress_failure_still_transcribes(self):
        with tempfile.TemporaryDirectory() as d:
            def failing(args, **kw):
                raise subprocess.CalledProcessError(1, args)

            deps, _ = self.make_deps(d, [tone(1.0)], run=failing)
            code = record.main(["--out", d, "--no-diarize"], deps=deps)
            self.assertEqual(code, 0)
            mdir = Path(d) / "2026-11-09_1030"
            self.assertTrue((mdir / "meeting.wav").exists())
            self.assertFalse((mdir / "meeting.m4a").exists())
            self.assertTrue((mdir / "transcript.md").exists())

    def test_main_forwards_speakers_and_model(self):
        with tempfile.TemporaryDirectory() as d:
            seen = {}

            def fake_diarize(samples, rate, speakers):
                seen["speakers"] = speakers
                return [(0.0, 1.0, 0)]

            def fake_tr(audio, **kw):
                seen["model"] = kw["path_or_hf_repo"]
                return {"segments": [{"start": 0.0, "end": 1.0, "text": "嗯", "no_speech_prob": 0.0}]}

            deps, _ = self.make_deps(d, [tone(1.0)], diarize_fn=fake_diarize, transcribe_fn=fake_tr)
            code = record.main(["--out", d, "--speakers", "3", "--model", "m/x"], deps=deps)
            self.assertEqual(code, 0)
            self.assertEqual(seen, {"speakers": 3, "model": "m/x"})

    def test_main_live_flag_accepted_but_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            deps, _ = self.make_deps(d, [tone(1.0)])
            code = record.main(["--out", d, "--no-diarize", "--live"], deps=deps)
            self.assertEqual(code, 0)
            self.assertIn("--live", deps["stderr"].getvalue())

    def test_main_exclusive_flags(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            record.main(["--speakers", "2", "--no-diarize"], deps={})


if __name__ == "__main__":
    unittest.main()
