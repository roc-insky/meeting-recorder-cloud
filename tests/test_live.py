"""live.py 的单元测试（假 VAD、假转写函数；不联网、不加载真实模型、不使用真实目录）。"""
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

import live


class FakeSegment:
    def __init__(self, samples, start):
        self.samples = samples
        self.start = start


class FakeVad:
    """假 VAD：幅度 >0.01 算有声；有声后遇到静音块就结束一句；接口同 sherpa 的 VoiceActivityDetector。"""

    def __init__(self):
        self._pos = 0
        self._cur = []
        self._cur_start = 0
        self._done = []

    def accept_waveform(self, samples):
        loud = float(np.max(np.abs(samples))) > 0.01 if len(samples) else False
        if loud:
            if not self._cur:
                self._cur_start = self._pos
            self._cur.append(np.asarray(samples, dtype=np.float32))
        elif self._cur:
            self._finish()
        self._pos += len(samples)

    def _finish(self):
        self._done.append(FakeSegment(np.concatenate(self._cur), self._cur_start))
        self._cur = []

    def flush(self):
        if self._cur:
            self._finish()

    def empty(self):
        return not self._done

    @property
    def front(self):
        return self._done[0]

    def pop(self):
        self._done.pop(0)


def loud(seconds=0.5):
    return np.full(int(16000 * seconds), 3000, dtype=np.int16)


def quiet(seconds=0.5):
    return np.zeros(int(16000 * seconds), dtype=np.int16)


class RecordingSink:
    def __init__(self):
        self.lines = []

    def write(self, start_s, text):
        self.lines.append((start_s, text))


def fake_tr(text="好"):
    return lambda samples: [{"start": 0.0, "end": 1.0, "text": text, "no_speech_prob": 0.0}]


class ModelsTest(unittest.TestCase):
    def test_models_present(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(live.models_present(d), [live.VAD_FILE])
            (d / live.VAD_FILE).write_bytes(b"")
            self.assertEqual(live.models_present(d), [live.VAD_FILE])  # 0 字节算缺失
            (d / live.VAD_FILE).write_bytes(b"x" * 10)
            self.assertEqual(live.models_present(d), [live.VAD_FILE])  # 过小视为损坏
            (d / live.VAD_FILE).write_bytes(b"x" * live.VAD_MIN_BYTES)
            self.assertEqual(live.models_present(d), [])

    def test_ensure_vad_model_downloads_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d) / "models"
            calls = []

            def dl(url, dest):
                calls.append((url, dest.name))
                dest.write_bytes(b"o" * live.VAD_MIN_BYTES)

            path = live.ensure_vad_model(d, download=dl)
            self.assertEqual(path, d / live.VAD_FILE)
            self.assertEqual(path.read_bytes(), b"o" * live.VAD_MIN_BYTES)
            self.assertEqual(calls, [(live.VAD_URL, live.VAD_FILE + ".part")])
            self.assertEqual([p.name for p in d.iterdir()], [live.VAD_FILE])
            live.ensure_vad_model(d, download=lambda u, p: self.fail("已存在不应再下载"))

    def test_ensure_vad_model_replaces_tiny_cached_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / live.VAD_FILE).write_bytes(b"0123456789")  # 10 字节：损坏
            path = live.ensure_vad_model(d, download=lambda u, p: p.write_bytes(b"v" * live.VAD_MIN_BYTES))
            self.assertEqual(path.stat().st_size, live.VAD_MIN_BYTES)
            self.assertEqual([p.name for p in d.iterdir()], [live.VAD_FILE])

    def test_ensure_vad_model_failure_leaves_no_part(self):
        with tempfile.TemporaryDirectory() as d:
            def dl(url, dest):
                dest.write_bytes(b"half")
                raise OSError("断网")

            with self.assertRaises(OSError):
                live.ensure_vad_model(Path(d), download=dl)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_ensure_vad_model_rejects_empty_download(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(OSError):
                live.ensure_vad_model(Path(d), download=lambda u, p: p.write_bytes(b""))
            self.assertEqual(list(Path(d).iterdir()), [])


class JoinTextsTest(unittest.TestCase):
    def test_join_texts(self):
        self.assertEqual(live.join_texts(["你好", "世界"]), "你好世界")
        self.assertEqual(live.join_texts(["hello", "world"]), "hello world")
        self.assertEqual(live.join_texts(["你好", "OK"]), "你好OK")
        self.assertEqual(live.join_texts(["OK", "你好"]), "OK你好")
        self.assertEqual(live.join_texts(["好，", "ok"]), "好，ok")
        self.assertEqual(live.join_texts([]), "")

    def test_transcriber_joins_chinese_segments_without_space(self):
        sink = RecordingSink()
        t = live.LiveTranscriber(lambda s: [{"text": "你好"}, {"text": "世界"}], sink)
        t.submit(0.0, np.zeros(1, dtype=np.float32))
        t.stop(5)
        self.assertEqual(sink.lines, [(0.0, "你好世界")])


class SegmenterTest(unittest.TestCase):
    def test_segmenter_emits_sentence_after_silence(self):
        seg = live.Segmenter(FakeVad)
        self.assertEqual(seg.feed(quiet()), [])
        self.assertEqual(seg.feed(loud()), [])
        self.assertEqual(seg.feed(loud()), [])
        out = seg.feed(quiet())
        self.assertEqual(len(out), 1)
        start, audio = out[0]
        self.assertAlmostEqual(start, 0.5)  # 前面静音 0.5 秒
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(len(audio), 16000)
        self.assertEqual(seg.flush(), [])

    def test_segmenter_flush_returns_tail(self):
        seg = live.Segmenter(FakeVad)
        seg.feed(loud())
        out = seg.flush()
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][0], 0.0)


class TranscriberTest(unittest.TestCase):
    def test_transcriber_orders_output(self):
        sink = RecordingSink()
        t = live.LiveTranscriber(lambda s: [{"text": f"n{len(s)}"}], sink)
        for i, n in enumerate((10, 20, 30)):
            t.submit(float(i), np.zeros(n, dtype=np.float32))
        self.assertTrue(t.stop(5))
        self.assertEqual(sink.lines, [(0.0, "n10"), (1.0, "n20"), (2.0, "n30")])

    def test_worker_is_daemon_thread(self):
        t = live.LiveTranscriber(fake_tr(), RecordingSink())
        self.assertTrue(t._thread.daemon)
        self.assertEqual(t._thread.name, "mr-live-worker")
        t.stop(1)

    def test_filters_empty_text(self):
        sink = RecordingSink()
        t = live.LiveTranscriber(lambda s: [{"text": "  "}, {"text": "你好", "no_speech_prob": 0.9}], sink)
        t.submit(0.0, np.zeros(5, dtype=np.float32))
        t.stop(5)
        self.assertEqual(sink.lines, [])

    def test_queue_full_drops_oldest_warns_once(self):
        gate = threading.Event()
        started = threading.Event()
        seen = []

        def blocking(samples):
            started.set()
            gate.wait(5)
            seen.append(len(samples))
            return [{"text": "x"}]

        err = io.StringIO()
        sink = RecordingSink()
        t = live.LiveTranscriber(blocking, sink, queue_max=2, stderr=err)
        t.submit(0.0, np.zeros(1, dtype=np.float32))  # 被工作线程取走并卡住
        self.assertTrue(started.wait(2))
        for n in range(2, 8):  # 队列容量 2：依次挤掉最旧的
            t.submit(float(n), np.zeros(n, dtype=np.float32))
        gate.set()
        self.assertTrue(t.stop(5))
        self.assertEqual(seen, [1, 6, 7])  # 只剩最新的两句
        self.assertEqual(err.getvalue().count("警告"), 1)

    def test_submit_never_blocks(self):
        gate = threading.Event()
        t = live.LiveTranscriber(lambda s: gate.wait(5) or [], RecordingSink(), stderr=io.StringIO())
        t0 = time.monotonic()
        for i in range(50):
            t.submit(float(i), np.zeros(1, dtype=np.float32))
        self.assertLess(time.monotonic() - t0, 0.5)
        gate.set()
        t.stop(5)

    def test_stop_flushes_tail(self):
        sink = RecordingSink()
        t = live.LiveTranscriber(fake_tr("尾句"), sink)
        session = live.LiveSession(live.Segmenter(FakeVad), t)
        session.feed(loud())  # 还没说完，VAD 里只有半句
        self.assertEqual(sink.lines, [])
        self.assertTrue(session.finish(5))
        self.assertEqual(sink.lines, [(0.0, "尾句")])

    def test_stop_timeout_returns(self):
        gate = threading.Event()
        started = threading.Event()
        err = io.StringIO()

        def blocking(samples):
            started.set()
            gate.wait(10)
            return [{"text": "x"}]

        sink = RecordingSink()
        t = live.LiveTranscriber(blocking, sink, stderr=err)
        t.submit(0.0, np.zeros(1, dtype=np.float32))
        t.submit(1.0, np.zeros(1, dtype=np.float32))
        self.assertTrue(started.wait(2))
        t0 = time.monotonic()
        self.assertFalse(t.stop(0.2))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("放弃", err.getvalue())
        gate.set()
        time.sleep(0.2)
        self.assertEqual(sink.lines, [])  # 放弃后迟到的结果不输出

    def test_transcribe_error_only_warns(self):
        err = io.StringIO()
        sink = RecordingSink()
        calls = []

        def flaky(samples):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("模型坏了")
            return [{"text": "后一句"}]

        t = live.LiveTranscriber(flaky, sink, stderr=err)
        t.submit(0.0, np.zeros(1, dtype=np.float32))
        t.submit(1.0, np.zeros(1, dtype=np.float32))
        t.stop(5)
        self.assertIn("模型坏了", err.getvalue())
        self.assertEqual(sink.lines, [(1.0, "后一句")])


class SinkTest(unittest.TestCase):
    def test_sink_writes_file_and_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            sink = live.LiveSink(Path(d) / "live.txt", out=out)
            sink.write(3725.9, "你好")
            sink.write(5.0, "再见")
            self.assertIn("[01:02:05] 你好", out.getvalue())
            self.assertEqual(
                (Path(d) / "live.txt").read_text(encoding="utf-8"), "[01:02:05] 你好\n[00:00:05] 再见\n"
            )

    def test_sink_errors_only_warn_once(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            sink = live.LiveSink(Path(d) / "不存在的目录" / "live.txt", out=io.StringIO(), stderr=err)
            sink.write(1.0, "a")
            sink.write(2.0, "b")
        self.assertEqual(err.getvalue().count("警告"), 1)


if __name__ == "__main__":
    unittest.main()
