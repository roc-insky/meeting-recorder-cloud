"""transcribe.py 的单元测试（假转写函数，不加载模型、不碰网络与真实目录）。"""
import io
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

import common
import transcribe


def write_wav(path: Path, samples: np.ndarray, rate: int = 16000) -> None:
    """手造 16 位单声道 wav。"""
    data = samples.astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def tone(seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(16000 * seconds)) / 16000
    return (3000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def must_not_call(*args, **kwargs):
    raise AssertionError("不应调用转写模型")


class TranscribeSamplesTest(unittest.TestCase):
    def test_no_initial_prompt(self):
        calls = []

        def fake(audio, **kwargs):
            calls.append((audio, kwargs))
            return {"segments": [{"start": 0.0, "end": 1.0, "text": " 你好", "no_speech_prob": 0.1, "tokens": [1]}]}

        segs = transcribe.transcribe_samples(np.zeros(16000, dtype=np.float32), transcribe_fn=fake)
        _audio, kwargs = calls[0]
        self.assertNotIn("initial_prompt", kwargs)
        self.assertIs(kwargs["condition_on_previous_text"], False)
        self.assertEqual(kwargs["language"], "zh")
        self.assertEqual(kwargs["path_or_hf_repo"], transcribe.DEFAULT_MODEL)
        self.assertEqual(segs[0]["text"], " 你好")


class FilterTest(unittest.TestCase):
    def test_filter_drops_empty_and_nospeech(self):
        segs = [
            {"start": 0, "end": 1, "text": "  ", "no_speech_prob": 0.0},
            {"start": 1, "end": 2, "text": "噪声", "no_speech_prob": 0.7},
            {"start": 2, "end": 3, "text": " 你好 ", "no_speech_prob": 0.6},
            {"start": 3, "end": 4, "text": "再见"},
        ]
        out = transcribe.filter_segments(segs)
        self.assertEqual([s["text"] for s in out], ["你好", "再见"])


class SpeakerTest(unittest.TestCase):
    def test_speaker_label(self):
        self.assertEqual(transcribe.speaker_label(0), "说话人A")
        self.assertEqual(transcribe.speaker_label(25), "说话人Z")
        self.assertEqual(transcribe.speaker_label(26), "说话人27")
        self.assertEqual(transcribe.speaker_label("张三"), "张三")

    def test_assign_by_overlap(self):
        turns = [(0.0, 4.0, 0), (4.0, 10.0, 1)]
        segs = [
            {"start": 3.0, "end": 8.0, "text": "a"},   # 与 0 重叠 1s，与 1 重叠 4s
            {"start": 0.0, "end": 2.0, "text": "b"},
            {"start": 20.0, "end": 21.0, "text": "c"},  # 无重叠
        ]
        got = transcribe.assign_speakers(segs, turns)
        self.assertEqual([spk for _s, spk in got], [1, 0, None])
        self.assertIs(got[0][0], segs[0])


class RenderTest(unittest.TestCase):
    STARTED = datetime(2026, 11, 9, 10, 30, 45)

    def test_render_plain_lines(self):
        segs = [
            {"start": 3.2, "end": 5.0, "text": "你好"},
            {"start": 3725.0, "end": 3730.0, "text": "再见"},
        ]
        out = transcribe.render(segs, started=self.STARTED, duration=3730.5)
        expected = (
            "# 会议文字稿\n"
            "- 开始时间：2026-11-09 10:30\n"
            "- 时长：01:02:10\n"
            "\n"
            "[00:00:03] 你好\n"
            "[01:02:05] 再见\n"
        )
        self.assertEqual(out, expected)

    def test_render_with_speakers_header(self):
        segs = [{"start": 3.0, "end": 4.0, "text": "你好"}, {"start": 5.0, "end": 6.0, "text": "嗯"}]
        assigned = [(segs[0], 0), (segs[1], None)]
        out = transcribe.render(
            segs, started=self.STARTED, duration=10, speakers=["说话人A", "说话人B"], assigned=assigned
        )
        self.assertIn("- 说话人：说话人A、说话人B\n", out)
        self.assertIn("[00:00:03] 说话人A: 你好\n", out)
        self.assertIn("[00:00:05] 嗯\n", out)

    def test_render_empty(self):
        out = transcribe.render([], started=self.STARTED, duration=0)
        self.assertEqual(
            out,
            "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:00:00\n\n（未识别到语音）\n",
        )


class TranscribeWavTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_zero_length_wav_no_model_call(self):
        wav = self.dir / "meeting.wav"
        write_wav(wav, np.zeros(0, dtype=np.int16))
        out = transcribe.transcribe_wav(
            wav, model="m", speakers=None, diarize=False, deps={"transcribe_fn": must_not_call}
        )
        self.assertIn("（未识别到语音）", out)

    def test_silent_wav_no_model_call(self):
        wav = self.dir / "meeting.wav"
        write_wav(wav, np.zeros(16000, dtype=np.int16))
        out = transcribe.transcribe_wav(
            wav, model="m", speakers=None, diarize=False, deps={"transcribe_fn": must_not_call}
        )
        self.assertIn("（未识别到语音）", out)
        self.assertIn("- 时长：00:00:01", out)

    def test_normal_wav_uses_fake_model(self):
        wav = self.dir / "meeting.wav"
        write_wav(wav, tone(2.0))
        fake = lambda audio, **kw: {"segments": [{"start": 0.5, "end": 1.5, "text": "你好", "no_speech_prob": 0.0}]}
        out = transcribe.transcribe_wav(wav, model="m", speakers=None, diarize=False, deps={"transcribe_fn": fake})
        self.assertIn("[00:00:00] 你好", out)

    def test_diarize_hook_labels_speakers(self):
        wav = self.dir / "meeting.wav"
        write_wav(wav, tone(2.0))
        fake = lambda audio, **kw: {"segments": [{"start": 0.5, "end": 1.5, "text": "你好", "no_speech_prob": 0.0}]}
        diar = lambda samples, rate, speakers: [(0.0, 2.0, 1)]
        out = transcribe.transcribe_wav(
            wav, model="m", speakers=None, diarize=True, deps={"transcribe_fn": fake, "diarize_fn": diar}
        )
        self.assertIn("- 说话人：说话人B", out)
        self.assertIn("[00:00:00] 说话人B: 你好", out)

    def test_diarize_failure_falls_back(self):
        wav = self.dir / "meeting.wav"
        write_wav(wav, tone(2.0))
        fake = lambda audio, **kw: {"segments": [{"start": 0.5, "end": 1.5, "text": "你好", "no_speech_prob": 0.0}]}

        def boom(samples, rate, speakers):
            raise RuntimeError("坏了")

        err = io.StringIO()
        code = transcribe.main(
            [str(wav)], deps={"transcribe_fn": fake, "diarize_fn": boom, "stderr": err}
        )
        self.assertEqual(code, 0)
        self.assertIn("区分", err.getvalue())
        text = (self.dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("[00:00:00] 你好", text)
        self.assertNotIn("说话人", text)

    def test_main_accepts_meeting_dir(self):
        write_wav(self.dir / "meeting.wav", tone(1.0))
        fake = lambda audio, **kw: {"segments": []}
        code = transcribe.main([str(self.dir), "--no-diarize"], deps={"transcribe_fn": fake})
        self.assertEqual(code, 0)
        self.assertIn("（未识别到语音）", (self.dir / "transcript.md").read_text(encoding="utf-8"))

    def test_main_missing_path(self):
        err = io.StringIO()
        code = transcribe.main([str(self.dir / "nope")], deps={"stderr": err})
        self.assertEqual(code, 1)
        self.assertIn("找不到", err.getvalue())

    def test_speakers_and_no_diarize_exclusive(self):
        with self.assertRaises(SystemExit) as cm:
            transcribe.main(["x.wav", "--speakers", "2", "--no-diarize"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
