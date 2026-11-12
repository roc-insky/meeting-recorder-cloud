"""meeting_controller 的单元测试（假 record_fn / transcribe_fn / 时钟，不碰麦克风、模型和真实目录）。"""
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

import common
import meeting_controller
import voiceprint
import voiceprint_service
from meeting_controller import (AlreadyTranscribed, AudioUnreadable, MeetingBusy,
                                MeetingController, MeetingStartError)
from tests.test_common import make_wav_bytes

WAIT = 5.0  # 仅作为“不会无限挂起”的上限，正常路径靠 Event 立即返回


def mr_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mr-") and t.is_alive()]


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeRecorder:
    """假 record_fn：建会议目录、调用 on_started，然后阻塞到 stop 置位。"""

    def __init__(self, name="2026-11-09_1030"):
        self.name = name
        self.entered = threading.Event()
        self.calls = 0
        self.fail_before_dir = None
        self.fail_after_dir = None
        self.levels = []
        self.samples = []

    def __call__(self, out_root, *, stop, on_started, on_level, on_samples):
        self.calls += 1
        if self.fail_before_dir:
            raise RuntimeError(self.fail_before_dir)
        d = Path(out_root) / (self.name if self.calls == 1 else f"{self.name}_{self.calls}")
        d.mkdir(parents=True)
        on_started(d)
        for lv in self.levels:
            on_level(lv)
        for s in self.samples:
            on_samples(s)
        self.entered.set()
        stop.wait(WAIT)
        if self.fail_after_dir:
            raise RuntimeError(self.fail_after_dir)
        return d


class FakeTranscriber:
    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.error = None
        self.exc = None
        self.text = None
        self.calls = []

    def __call__(self, meeting_dir, *, speakers):
        self.calls.append((Path(meeting_dir).name, speakers))
        self.entered.set()
        self.gate.wait(WAIT)
        if self.exc is not None:
            raise self.exc
        if self.error:
            raise RuntimeError(self.error)
        if self.text is not None:
            (Path(meeting_dir) / "transcript.md").write_text(self.text, encoding="utf-8")


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "Meetings"
        self.rec = FakeRecorder()
        self.tr = FakeTranscriber()
        self.clock = FakeClock()
        self.ctl = self.make()

    def make(self, **kw):
        ctl = MeetingController(self.root, record_fn=self.rec, transcribe_fn=self.tr,
                                clock=self.clock, **kw)
        self.addCleanup(ctl.close)
        return ctl

    def drain(self, ctl, last_seq=0):
        """取走当前已有的全部事件（遇到心跳即停）。"""
        out = []
        for ev in ctl.subscribe(last_seq, threading.Event(), 0.01):
            if ev is None:
                break
            out.append(ev)
        return out

    def wait_final(self, ctl, want, timeout=WAIT):
        """订阅事件直到出现 final 事件或快照已是目标状态。"""
        stop = threading.Event()
        for ev in ctl.subscribe(None, stop, timeout):
            if ev is None:
                break
            if ev["type"] in ("state", "final") and ctl.snapshot()["final"] == want \
                    and not ctl.snapshot()["state"] == "recording":
                return ev
        self.fail(f"没等到 final={want}，当前 {ctl.snapshot()}")


class StartValidationTests(ControllerCase):
    def test_bad_speakers_rejected(self):
        for bad in (True, False, 0, 21, -1, "3", 3.0, 2.5, [3]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    self.ctl.start(speakers=bad)
                self.assertIn("说话人", str(cm.exception))
        self.assertEqual(self.rec.calls, 0)
        self.assertEqual(self.ctl.snapshot()["state"], "idle")

    def test_good_speakers_accepted_and_forwarded(self):
        mid = self.ctl.start(speakers=3)
        self.assertEqual(mid, "2026-11-09_1030")
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")
        self.assertEqual(self.tr.calls, [("2026-11-09_1030", 3)])

    def test_boundaries_and_none(self):
        for ok in (1, 20, None):
            with self.subTest(ok=ok):
                ctl = self.make()
                ctl.start(speakers=ok)
                ctl.stop()
                self.wait_final(ctl, "completed")

    def test_live_must_be_bool(self):
        with self.assertRaises(ValueError):
            self.ctl.start(live="yes")


class LifecycleTests(ControllerCase):
    def test_duplicate_start_is_busy(self):
        self.ctl.start()
        with self.assertRaises(MeetingBusy):
            self.ctl.start()
        self.assertEqual(self.rec.calls, 1)

    def test_snapshot_elapsed_uses_clock(self):
        self.assertEqual(self.ctl.snapshot()["state"], "idle")
        self.assertEqual(self.ctl.snapshot()["final"], "idle")
        self.assertIsNone(self.ctl.snapshot()["meeting_id"])
        mid = self.ctl.start()
        self.clock.now += 12.5
        snap = self.ctl.snapshot()
        self.assertEqual((snap["state"], snap["meeting_id"], snap["elapsed"]), ("recording", mid, 12.5))
        self.tr.gate.clear()
        self.ctl.stop()
        self.assertTrue(self.tr.entered.wait(WAIT))
        self.clock.now += 50       # 停止后时长冻结
        snap = self.ctl.snapshot()
        self.assertEqual((snap["state"], snap["final"], snap["elapsed"]), ("idle", "running", 12.5))
        self.tr.gate.set()
        self.wait_final(self.ctl, "completed")
        self.assertEqual(self.ctl.snapshot()["meeting_id"], mid)

    def test_stop_returns_promptly_even_if_record_is_slow(self):
        slow = threading.Event()

        def rec(out_root, *, stop, on_started, on_level, on_samples):
            d = Path(out_root) / "m1"
            d.mkdir(parents=True)
            on_started(d)
            stop.wait(WAIT)
            slow.wait(WAIT)          # 停止后收尾很慢
            return d

        ctl = MeetingController(self.root, record_fn=rec, transcribe_fn=self.tr, clock=self.clock)
        self.addCleanup(ctl.close)
        self.addCleanup(slow.set)
        ctl.start()
        ctl.stop()                   # 必须立即返回
        snap = ctl.snapshot()
        self.assertEqual(snap["state"], "recording")
        self.assertTrue(snap["stopping"])
        ctl.stop()                   # 重复 stop 无害
        slow.set()
        self.wait_final(ctl, "completed")

    def test_stop_when_idle_is_noop(self):
        before = self.ctl.last_seq()
        self.ctl.stop()
        self.assertEqual(self.ctl.last_seq(), before)

    def test_transcribe_failure_sets_failed_and_is_usable_again(self):
        self.tr.error = "模型炸了"
        self.ctl.start()
        self.ctl.stop()
        self.wait_final(self.ctl, "failed")
        snap = self.ctl.snapshot()
        self.assertEqual((snap["state"], snap["final"]), ("idle", "failed"))
        self.assertIn("模型炸了", snap["error"])
        self.tr.error = None
        mid2 = self.ctl.start()      # 失败后可以再录
        self.assertEqual(mid2, "2026-11-09_1030_2")
        self.assertEqual(self.ctl.snapshot()["final"], "idle")
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")

    def test_record_failure_before_dir(self):
        self.rec.fail_before_dir = "麦克风坏了"
        with self.assertRaises(MeetingStartError):
            self.ctl.start()
        snap = self.ctl.snapshot()
        self.assertEqual((snap["state"], snap["final"], snap["meeting_id"]), ("idle", "failed", None))
        self.assertIn("麦克风坏了", snap["error"])
        self.assertEqual(self.tr.calls, [])
        self.rec.fail_before_dir = None
        self.ctl.start()              # 仍可再次开始
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")

    def test_record_failure_after_dir_marks_failed_without_transcribe(self):
        self.rec.fail_after_dir = "写盘失败"
        mid = self.ctl.start()
        self.ctl.stop()
        self.wait_final(self.ctl, "failed")
        snap = self.ctl.snapshot()
        self.assertEqual((snap["state"], snap["meeting_id"]), ("idle", mid))
        self.assertEqual(self.tr.calls, [])
        self.assertEqual(self.ctl.is_active(mid), False)

    def test_recorder_error_message_passthrough(self):
        import record
        self.rec.fail_before_dir = None

        def rec(out_root, **kw):
            raise record.RecorderError("磁盘空间不足（至少需要 1 GB）")

        ctl = MeetingController(self.root, record_fn=rec, transcribe_fn=self.tr, clock=self.clock)
        self.addCleanup(ctl.close)
        with self.assertRaises(MeetingStartError) as cm:
            ctl.start()
        self.assertEqual(str(cm.exception), "磁盘空间不足（至少需要 1 GB）")

    def test_busy_while_previous_transcription_runs(self):
        self.tr.gate.clear()
        mid = self.ctl.start()
        self.ctl.stop()
        self.assertTrue(self.tr.entered.wait(WAIT))
        self.assertTrue(self.ctl.is_active(mid))
        with self.assertRaises(MeetingBusy):
            self.ctl.start()
        self.tr.gate.set()
        self.wait_final(self.ctl, "completed")
        self.assertFalse(self.ctl.is_active(mid))
        self.ctl.start()              # 转写结束后可以再录

    def test_is_active_during_recording(self):
        self.assertFalse(self.ctl.is_active("x"))
        self.assertFalse(self.ctl.is_active(None))
        mid = self.ctl.start()
        self.assertTrue(self.ctl.is_active(mid))
        self.assertFalse(self.ctl.is_active("other"))


class EventTests(ControllerCase):
    def collect(self, ctl, last_seq=None, until_type=None, limit=200):
        out, stop = [], threading.Event()
        for ev in ctl.subscribe(last_seq, stop, 0.05):
            if ev is None:
                break
            out.append(ev)
            if until_type and ev["type"] == until_type or len(out) >= limit:
                break
        return out

    def full_run(self):
        self.rec.levels = [0.01, 0.02]
        self.ctl.start()
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")

    def test_seq_strictly_increasing_and_types(self):
        self.full_run()
        evs = self.collect(self.ctl, 0)
        seqs = [e["seq"] for e in evs]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertTrue(all(b > a for a, b in zip(seqs, seqs[1:])))
        types = {e["type"] for e in evs}
        self.assertTrue({"state", "level", "final"} <= types)
        self.assertEqual(evs[-1]["type"], "final")
        self.assertEqual(evs[-1]["data"]["status"], "completed")
        self.assertEqual(evs[-1]["seq"], self.ctl.last_seq())

    def test_resume_from_last_seq_no_dup_no_loss(self):
        self.full_run()
        all_evs = self.collect(self.ctl, 0)
        for cut in range(len(all_evs)):
            with self.subTest(cut=cut):
                last = all_evs[cut]["seq"]
                got = self.collect(self.ctl, last)
                self.assertEqual([e["seq"] for e in got], [e["seq"] for e in all_evs[cut + 1:]])

    def test_resume_receives_new_events_exactly_once(self):
        self.ctl.start()
        first = self.collect(self.ctl, None)
        self.assertEqual(first[0]["type"], "state")
        self.assertTrue(first[0].get("resync"))
        last = first[-1]["seq"]
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")
        got = self.collect(self.ctl, last)
        self.assertTrue(all(e["seq"] > last for e in got))
        seqs = [e["seq"] for e in got]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(got[-1]["type"], "final")

    def test_subscribe_without_last_seq_starts_with_snapshot(self):
        self.full_run()
        evs = self.collect(self.ctl, None)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "state")
        self.assertTrue(evs[0]["resync"])
        self.assertEqual(evs[0]["seq"], self.ctl.last_seq())
        self.assertEqual(evs[0]["data"]["final"], "completed")

    def test_future_last_seq_resyncs(self):
        self.full_run()
        evs = self.collect(self.ctl, self.ctl.last_seq() + 1000)
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0]["resync"])

    def test_live_subscriber_gets_events_in_order(self):
        got, ready = [], threading.Event()
        stop = threading.Event()

        def consume():
            for ev in self.ctl.subscribe(0, stop, 0.05):
                if ev is None:
                    ready.set()
                    continue
                got.append(ev)
                if ev["type"] == "final":
                    return

        t = threading.Thread(target=consume, name="test-consumer")
        t.start()
        self.assertTrue(ready.wait(WAIT))      # 订阅者已在等待
        self.ctl.start()
        self.ctl.stop()
        t.join(WAIT)
        self.assertFalse(t.is_alive())
        seqs = [e["seq"] for e in got]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(got[-1]["type"], "final")

    def test_history_cap_and_resync(self):
        ctl = self.make(history_max=5)
        ctl.start()
        for _ in range(20):                    # 制造大量非 level 事件
            ctl._emit("warning", {"message": "x"})
        self.assertLessEqual(len(ctl._history), 5)
        old = 1                                # 早已被挤出历史
        evs = self.collect(ctl, old)
        self.assertEqual(evs[0]["type"], "state")
        self.assertTrue(evs[0]["resync"])
        self.assertEqual(evs[0]["seq"], ctl.last_seq())
        # 仍在历史内的 last_seq 不会 resync
        last = ctl._history[1]["seq"]
        evs = self.collect(ctl, last)
        self.assertFalse(any(e.get("resync") for e in evs))
        self.assertEqual([e["seq"] for e in evs], [e["seq"] for e in ctl._history[2:]])

    def test_consecutive_levels_coalesce(self):
        self.rec.levels = [0.1] * 500
        ctl = self.make(history_max=50)
        ctl.start()
        self.assertEqual(len([e for e in ctl._history if e["type"] == "level"]), 1)
        self.assertEqual(ctl._history[-1]["data"]["rms"], 0.1)
        self.assertIn("state", [e["type"] for e in ctl._history])   # 电平没有挤掉状态事件

    def test_heartbeat_none_when_idle(self):
        stop = threading.Event()
        it = self.ctl.subscribe(self.ctl.last_seq(), stop, 0.01)
        self.assertIsNone(next(it))
        it.close()

    def test_subscriber_cancel_via_stop_event(self):
        stop = threading.Event()
        done = threading.Event()

        def consume():
            for _ in self.ctl.subscribe(self.ctl.last_seq(), stop, None):   # 无心跳也要能被取消
                pass
            done.set()

        t = threading.Thread(target=consume, name="test-consumer")
        t.start()
        stop.set()
        self.assertTrue(done.wait(WAIT))
        t.join(WAIT)

    def test_close_ends_subscribers(self):
        done = threading.Event()

        def consume():
            for _ in self.ctl.subscribe(self.ctl.last_seq(), threading.Event(), None):
                pass
            done.set()

        t = threading.Thread(target=consume, name="test-consumer")
        t.start()
        self.ctl.close()
        self.assertTrue(done.wait(WAIT))
        t.join(WAIT)

    def test_generator_close_is_clean(self):
        it = self.ctl.subscribe(None, threading.Event(), 0.01)
        next(it)
        it.close()                   # 不抛异常

    def test_error_event_on_failure(self):
        self.tr.error = "炸"
        self.ctl.start()
        self.ctl.stop()
        self.wait_final(self.ctl, "failed")
        types = [e["type"] for e in self.collect(self.ctl, 0)]
        self.assertIn("error", types)
        self.assertEqual(types[-1], "final")


class LiveTests(ControllerCase):
    class FakeSession:
        def __init__(self, on_text):
            self.on_text = on_text
            self.fed = 0
            self.finished = False

        def feed(self, samples):
            self.fed += 1
            if self.fed == 1:
                self.on_text(0.0, "实时一句")

        def finish(self):
            self.finished = True

    def test_live_text_events_and_finish(self):
        sessions = []

        def factory(meeting_dir, on_text):
            s = self.FakeSession(on_text)
            sessions.append(s)
            return s

        self.rec.samples = [np.zeros(160, dtype=np.int16)] * 2
        ctl = self.make(live_factory=factory)
        ctl.start(live=True)
        self.assertTrue(self.rec.entered.wait(WAIT))
        ctl.stop()
        self.wait_final(ctl, "completed")
        evs = [e for e in self.drain(ctl) if e["type"] == "live_text"]
        self.assertEqual([e["data"] for e in evs][:1], [{"t": 0.0, "text": "实时一句"}])
        self.assertEqual((sessions[0].fed, sessions[0].finished), (2, True))

    def test_live_factory_failure_degrades(self):
        def factory(meeting_dir, on_text):
            raise RuntimeError("VAD 不可用")

        ctl = self.make(live_factory=factory)
        ctl.start(live=True)
        # 初始化在独立线程里异步进行：等降级提示出现后再停止，避免与停止竞争
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and "warning" not in [e["type"] for e in self.drain(ctl)]:
            time.sleep(0.01)
        ctl.stop()
        self.wait_final(ctl, "completed")
        types = [e["type"] for e in self.drain(ctl)]
        self.assertIn("warning", types)
        self.assertFalse(ctl.snapshot()["live"])

    def test_live_without_factory_degrades(self):
        self.ctl.start(live=True)
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")


class LiveWarmupTests(ControllerCase):
    """服务启动时的实时转写预热：状态可见，失败只降级、不影响录音。"""

    class Session:
        def __init__(self):
            self.fed = 0
            self.finished = False

        def feed(self, samples):
            self.fed += 1

        def finish(self):
            self.finished = True

    def wait_state(self, ctl, want):
        deadline = time.monotonic() + WAIT
        while ctl.snapshot()["live_state"] != want and time.monotonic() < deadline:
            time.sleep(0.01)
        return ctl.snapshot()["live_state"]

    def test_no_factory_means_unavailable_and_no_warmup(self):
        self.assertEqual(self.ctl.snapshot()["live_state"], "unavailable")
        self.assertFalse(self.ctl.warm_live())

    def test_warmup_runs_once_and_reports_ready(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def warmup():
            calls.append(1)
            started.set()
            release.wait(WAIT)

        ctl = self.make(live_factory=lambda d, on_text: self.Session(), live_warmup=warmup)
        self.assertEqual(ctl.snapshot()["live_state"], "idle")
        self.assertTrue(ctl.warm_live())
        self.assertTrue(started.wait(WAIT))
        self.assertEqual(ctl.snapshot()["live_state"], "warming")
        self.assertFalse(ctl.warm_live())          # 预热中不重复启动
        release.set()
        self.assertEqual(self.wait_state(ctl, "ready"), "ready")
        self.assertIsNone(ctl.snapshot()["live_error"])
        self.assertFalse(ctl.warm_live())          # 已就绪也不再启动
        self.assertEqual(calls, [1])

    def test_warmup_failure_warns_but_recording_still_works(self):
        def warmup():
            raise RuntimeError("没有网络")

        ctl = self.make(live_factory=lambda d, on_text: self.Session(), live_warmup=warmup)
        ctl.warm_live()
        self.assertEqual(self.wait_state(ctl, "failed"), "failed")
        self.assertIn("没有网络", ctl.snapshot()["live_error"])
        kinds = [e["type"] for e in self.drain(ctl)]
        self.assertIn("warning", kinds)
        self.assertNotIn("error", kinds)           # 预热失败不是录音错误
        ctl.start(live=True)
        self.assertTrue(self.rec.entered.wait(WAIT))
        ctl.stop()
        self.wait_final(ctl, "completed")
        self.assertEqual(ctl.snapshot()["final"], "completed")

    def test_live_flag_is_reset_after_recording(self):
        """停止后快照里的 live 必须为 false，否则页面会以为下一场仍开着实时转写。"""
        ctl = self.make(live_factory=lambda d, on_text: self.Session())
        ctl.start(live=True)
        self.assertTrue(ctl.snapshot()["live"])
        ctl.stop()
        self.wait_final(ctl, "completed")
        self.assertFalse(ctl.snapshot()["live"])


class SlowLiveInitTests(unittest.TestCase):
    """实时转写初始化很慢（下载/加载 VAD）时，不得拖住录音写盘、停止与退出（真实 record.record + 假流）。"""

    class Session:
        def __init__(self):
            self.fed = 0
            self.finished = False

        def feed(self, samples):
            self.fed += 1

        def finish(self):
            self.finished = True

    def setUp(self):
        import common
        from datetime import datetime
        from tests.test_record import fake_factory, tone
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "Meetings"
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)         # 无论测试成败都放行阻塞的工厂线程
        self.entered = threading.Event()
        self.session = self.Session()

        def factory(meeting_dir, on_text):
            self.entered.set()
            self.gate.wait(WAIT * 4)
            return self.session

        self.factory = factory
        self.rec_fn = meeting_controller.make_record_fn(
            stream_factory=fake_factory([tone(1.0)]),
            now=lambda: datetime(2026, 11, 9, 10, 30),
            free_bytes_fn=lambda p: common.MIN_FREE_BYTES)

    def make(self, **kw):
        ctl = MeetingController(self.root, record_fn=self.rec_fn, transcribe_fn=lambda d, *, speakers: None,
                                clock=FakeClock(), live_factory=self.factory, **kw)
        self.addCleanup(ctl.close)
        return ctl

    def wav_samples(self, mid):
        import common
        try:
            return common.read_wav_tolerant(self.root / mid / "meeting.wav")[1].size
        except ValueError:
            return 0

    def wait_samples(self, mid, want):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.wav_samples(mid) >= want:
                return True
            time.sleep(0.01)
        return False

    def wait_final(self, ctl, want):
        stop = threading.Event()
        for ev in ctl.subscribe(None, stop, WAIT):
            if ev is None or (ctl.snapshot()["final"] == want and ctl.snapshot()["state"] != "recording"):
                break
        return ctl.snapshot()["final"]

    def test_blocked_init_does_not_stall_audio_or_stop(self):
        ctl = self.make()
        mid = ctl.start(live=True)
        self.assertTrue(self.entered.wait(WAIT))
        # 初始化仍卡着（gate 未放行），音频也必须照常写盘
        self.assertTrue(self.wait_samples(mid, 16000), "实时初始化卡住时音频没有写盘")
        self.assertFalse(self.gate.is_set())
        t0 = time.monotonic()
        ctl.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(self.wait_final(ctl, "completed"), "completed")
        self.assertEqual(self.wav_samples(mid), 16000)          # 音频完整、wav 有效
        self.assertEqual(ctl.snapshot()["state"], "idle")

    def test_late_attached_live_is_dropped_after_recording_ended(self):
        ctl = self.make()
        mid = ctl.start(live=True)
        self.assertTrue(self.entered.wait(WAIT))
        ctl.stop()
        self.assertEqual(self.wait_final(ctl, "completed"), "completed")
        self.gate.set()                        # 录音结束后才初始化完成
        deadline = time.monotonic() + WAIT
        while not self.session.finished and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.session.finished)  # 被静默收掉
        self.assertEqual(self.session.fed, 0)   # 没有喂过任何音频
        self.assertEqual(self.wav_samples(mid), 16000)

    def test_init_timeout_drops_live_but_keeps_recording(self):
        ctl = self.make(live_init_timeout=0.05)
        mid = ctl.start(live=True)
        self.assertTrue(self.entered.wait(WAIT))
        deadline = time.monotonic() + WAIT
        while ctl.snapshot()["live"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(ctl.snapshot()["live"])
        self.assertTrue(any(e["type"] == "warning" and "超时" in e["data"]["message"]
                            for e in ctl.subscribe(0, threading.Event(), 0.01) if e))
        self.assertEqual(ctl.snapshot()["state"], "recording")   # 录音继续
        self.gate.set()                       # 超时之后才完成的会话不得再挂接
        time.sleep(0.1)
        ctl.stop()
        self.assertEqual(self.wait_final(ctl, "completed"), "completed")
        self.assertEqual(self.session.fed, 0)
        self.assertEqual(self.wav_samples(mid), 16000)

    def test_close_returns_promptly_with_blocked_init(self):
        ctl = self.make()
        ctl.start(live=True)
        self.assertTrue(self.entered.wait(WAIT))
        t0 = time.monotonic()
        ctl.close(timeout=3.0)
        self.assertLess(time.monotonic() - t0, 3.5)
        self.assertEqual(ctl.snapshot()["state"], "idle")


class VoiceprintCoordinationTests(ControllerCase):
    def make_service(self):
        gate = threading.Event()
        entered = threading.Event()

        def fake_record(name, seconds, replace, *, stop, on_progress):
            entered.set()
            gate.wait(WAIT)

        svc = voiceprint_service.VoiceprintService(record_enroll_fn=fake_record,
                                                   import_fn=lambda *a, **k: None)
        self.addCleanup(svc.close)
        self.addCleanup(gate.set)
        meeting_controller.link_exclusive(self.ctl, svc)
        return svc, gate, entered

    def test_no_recording_during_voiceprint_job(self):
        svc, gate, entered = self.make_service()
        job = svc.start_record("张三", 30, False)
        self.assertTrue(entered.wait(WAIT))
        with self.assertRaises(MeetingBusy):
            self.ctl.start()
        self.assertEqual(self.rec.calls, 0)
        gate.set()
        self.assertTrue(svc.join(WAIT))
        self.assertEqual(svc.status(job)["state"], "completed")
        self.ctl.start()               # 任务结束后可以录音

    def test_no_voiceprint_job_during_recording(self):
        svc, gate, entered = self.make_service()
        self.ctl.start()
        with self.assertRaises(voiceprint_service.VoiceprintBusy) as cm:
            svc.start_record("张三", 30, False)
        self.assertIsInstance(cm.exception, meeting_controller.Busy)
        with self.assertRaises(meeting_controller.Busy):
            svc.start_import("张三", __file__, False)
        self.ctl.stop()
        self.wait_final(self.ctl, "completed")
        svc.start_record("张三", 30, False)   # 录音结束后可以登记

    def test_transcribing_does_not_block_voiceprint(self):
        svc, gate, entered = self.make_service()
        self.tr.gate.clear()
        self.ctl.start()
        self.ctl.stop()
        self.assertTrue(self.tr.entered.wait(WAIT))
        svc.start_record("张三", 30, False)   # 转写不占麦克风
        self.tr.gate.set()


class ShutdownTests(ControllerCase):
    def test_close_while_recording_leaves_no_threads(self):
        before = set(mr_threads())
        self.ctl.start()
        self.assertTrue(self.ctl.close(WAIT))
        self.assertEqual(set(mr_threads()) - before, set())
        self.assertEqual(self.ctl.snapshot()["state"], "idle")

    def test_close_while_transcribing_leaves_no_threads(self):
        before = set(mr_threads())
        self.ctl.start()
        self.ctl.stop()
        self.assertTrue(self.ctl.close(WAIT))
        self.assertEqual(set(mr_threads()) - before, set())

    def test_close_idempotent_and_blocks_start(self):
        self.assertTrue(self.ctl.close())
        self.assertTrue(self.ctl.close())
        with self.assertRaises(MeetingBusy):
            self.ctl.start()
        self.assertIs(MeetingController.shutdown, MeetingController.close)


class RealWiringTests(unittest.TestCase):
    """make_record_fn / make_transcribe_fn 与真实 record.record 的接线（假流、假转写）。"""

    def test_record_and_transcribe_fn_end_to_end(self):
        import common
        from datetime import datetime
        from tests.test_record import fake_factory, tone

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "Meetings"
            rec_fn = meeting_controller.make_record_fn(
                stream_factory=fake_factory([tone(1.0)]),
                now=lambda: datetime(2026, 11, 9, 10, 30),
                free_bytes_fn=lambda p: common.MIN_FREE_BYTES)
            import subprocess

            def fake_run(args, **kw):
                Path(args[-1]).write_bytes(b"m4a")
                return subprocess.CompletedProcess(args, 0, b"", b"")

            tr_fn = meeting_controller.make_transcribe_fn(run=fake_run, deps={
                "transcribe_fn": lambda audio, **kw: {"segments": [
                    {"start": 0.0, "end": 1.0, "text": "你好", "no_speech_prob": 0.0}]},
                "diarize_fn": lambda s, r, n: [(0.0, 1.0, 0)],
                "extractor": None})
            ctl = MeetingController(root, record_fn=rec_fn, transcribe_fn=tr_fn, clock=FakeClock())
            self.addCleanup(ctl.close)
            mid = ctl.start(speakers=1)
            ctl.stop()
            stop = threading.Event()
            for ev in ctl.subscribe(0, stop, WAIT):
                if ev is None or ev["type"] == "final":
                    break
            self.assertEqual(ctl.snapshot()["final"], "completed", ctl.snapshot())
            mdir = root / mid
            self.assertIn("你好", (mdir / "transcript.md").read_text(encoding="utf-8"))
            self.assertTrue((mdir / "meeting.m4a").exists())

    def test_malformed_wav_final_failed_and_transcript_kept(self):
        """损坏的 wav 不是“没有语音”：final=failed，且已有的 transcript.md 原样保留。"""
        import subprocess
        from datetime import datetime

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "Meetings"
            mdir = root / "2026-11-09_1030"

            def rec_fn(out_root, *, stop, on_started, on_level, on_samples):
                mdir.mkdir(parents=True)
                (mdir / "meeting.wav").write_bytes(b"garbage garbage garbage garbage")
                (mdir / "transcript.md").write_text("旧文字稿", encoding="utf-8")
                on_started(mdir)
                stop.wait(WAIT)
                return mdir

            def fake_run(args, **kw):
                return subprocess.CompletedProcess(args, 1, b"", b"")

            tr_fn = meeting_controller.make_transcribe_fn(run=fake_run, deps={"stderr": io.StringIO()})
            ctl = MeetingController(root, record_fn=rec_fn, transcribe_fn=tr_fn, clock=FakeClock())
            self.addCleanup(ctl.close)
            ctl.start()
            ctl.stop()
            stop = threading.Event()
            for ev in ctl.subscribe(0, stop, WAIT):
                if ev is None or ev["type"] == "final":
                    break
            snap = ctl.snapshot()
            self.assertEqual(snap["final"], "failed", snap)
            self.assertIn("损坏", snap["error"] or "")
            self.assertEqual((mdir / "transcript.md").read_text(encoding="utf-8"), "旧文字稿")


    def _thresholds_from_config(self, body, *, stderr=None):
        """用临时 config.toml 调 make_transcribe_fn，返回真正传给 transcribe_wav 的 thresholds。"""
        import subprocess
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(body, encoding="utf-8")
            mdir = Path(d) / "2026-11-09_1030"
            mdir.mkdir()
            (mdir / "meeting.wav").write_bytes(make_wav_bytes(b"\x00\x00" * 1600))
            tr_fn = meeting_controller.make_transcribe_fn(
                run=lambda args, **kw: subprocess.CompletedProcess(args, 1, b"", b""),
                deps={"stderr": stderr or io.StringIO()}, config_path=cfg)
            with mock.patch.object(meeting_controller.transcribe, "transcribe_wav",
                                   return_value="# 稿\n") as fake:
                tr_fn(mdir, speakers=None)
            self.assertEqual((mdir / "transcript.md").read_text(encoding="utf-8"), "# 稿\n")
            return fake.call_args.kwargs["thresholds"]

    def test_transcribe_fn_reads_voiceprint_thresholds(self):
        """config.toml 的 [voiceprint] 必须真的传到 transcribe_wav（Web 录音也按配置分档）。"""
        self.assertEqual(
            self._thresholds_from_config("[voiceprint]\nhigh = 0.7\nlow = 0.4\n"),
            voiceprint.Thresholds(0.7, 0.4, None))

    def test_transcribe_fn_warns_on_bad_thresholds(self):
        err = io.StringIO()
        th = self._thresholds_from_config("[voiceprint]\nhigh = 0.2\nlow = 0.4\n", stderr=err)
        self.assertEqual((th.high, th.low), (voiceprint.DEFAULT_HIGH, voiceprint.DEFAULT_LOW))
        self.assertIn("声纹阈值无效", err.getvalue())


class RetranscribeTests(ControllerCase):
    MID = "2026-11-09_1030"

    def mk(self, mid=MID, *, transcript=None, samples=b"\x00\x00" * 1600, wav_bytes=None):
        d = self.root / mid
        d.mkdir(parents=True, exist_ok=True)
        if wav_bytes is not None:
            (d / "meeting.wav").write_bytes(wav_bytes)
        elif samples is not None:
            (d / "meeting.wav").write_bytes(make_wav_bytes(samples))
        if transcript is not None:
            (d / "transcript.md").write_text(transcript, encoding="utf-8")
        return d

    def test_retranscribe_fills_gap(self):
        d = self.mk()
        self.tr.text = "# 会议\n\n[00:00:01] 你好\n"
        self.ctl.retranscribe(self.MID)
        self.assertEqual((d / "transcript.md").read_text(encoding="utf-8"), self.tr.text)
        self.assertEqual(self.tr.calls, [(self.MID, None)])
        self.assertEqual(self.ctl.transcribing_ids(), set())

    def test_retranscribe_busy_while_transcribing(self):
        self.mk()
        self.tr.gate.clear()
        self.addCleanup(self.tr.gate.set)
        errors = []

        def work():
            try:
                self.ctl.retranscribe(self.MID)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=work, name="test-retranscribe")
        t.start()
        self.assertTrue(self.tr.entered.wait(WAIT))
        try:
            with self.assertRaises(MeetingBusy):
                self.ctl.retranscribe(self.MID)
            self.assertEqual(len(self.tr.calls), 1)
            self.assertIn(self.MID, self.ctl.transcribing_ids())
        finally:
            self.tr.gate.set()
            t.join(WAIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

    def test_retranscribe_busy_while_recording(self):
        self.ctl.start()
        try:
            with self.assertRaises(MeetingBusy):
                self.ctl.retranscribe(self.MID)
            self.assertEqual(self.tr.calls, [])   # 补转写没有启动
        finally:
            self.ctl.stop()
        self.wait_final(self.ctl, "completed")
        self.assertEqual(self.tr.calls, [(self.MID, None)])  # 只有停止后的正常转写

    def test_busy_while_other_meeting_transcribing(self):
        """别的会议正在后台转写时不再并发补转写，避免重复加载模型。"""
        self.mk()
        with self.ctl._cond:
            self.ctl._final = "running"
        try:
            with self.assertRaises(MeetingBusy):
                self.ctl.retranscribe(self.MID)
            self.assertEqual(self.tr.calls, [])
        finally:
            with self.ctl._cond:
                self.ctl._final = "idle"

    def test_existing_transcript_untouched(self):
        d = self.mk(transcript="旧稿\n")
        with self.assertRaises(AlreadyTranscribed):
            self.ctl.retranscribe(self.MID)
        self.assertEqual((d / "transcript.md").read_text(encoding="utf-8"), "旧稿\n")
        self.assertFalse((d / "transcript.md.bak").exists())
        self.assertEqual(self.tr.calls, [])

    def test_force_makes_bak(self):
        d = self.mk(transcript="旧稿\n")
        (d / "transcript.md.bak").write_text("更旧的\n", encoding="utf-8")
        self.tr.text = "新稿\n"
        self.ctl.retranscribe(self.MID, force=True)
        self.assertEqual((d / "transcript.md").read_text(encoding="utf-8"), "新稿\n")
        self.assertEqual((d / "transcript.md.bak").read_text(encoding="utf-8"), "旧稿\n")
        self.assertEqual(self.tr.calls, [(self.MID, None)])

    def test_force_failure_restores_transcript(self):
        """重新转写失败时旧稿必须回到 transcript.md，不能只剩 .bak。"""
        d = self.mk(transcript="旧稿\n")
        self.tr.exc = RuntimeError("模型炸了")
        with self.assertRaises(RuntimeError):
            self.ctl.retranscribe(self.MID, force=True)
        self.assertEqual((d / "transcript.md").read_text(encoding="utf-8"), "旧稿\n")
        self.assertFalse((d / "transcript.md.bak").exists())
        self.assertNotIn(self.MID, self.ctl.transcribing_ids())

    def test_set_cleared_on_failure(self):
        self.mk()
        self.tr.exc = common.MalformedAudioError("坏")
        with self.assertRaises(AudioUnreadable):
            self.ctl.retranscribe(self.MID)
        self.assertNotIn(self.MID, self.ctl.transcribing_ids())
        self.tr.exc = RuntimeError("模型炸了")
        with self.assertRaises(RuntimeError):
            self.ctl.retranscribe(self.MID)
        self.assertNotIn(self.MID, self.ctl.transcribing_ids())
        self.tr.exc = None
        self.tr.text = "补好了\n"
        self.ctl.retranscribe(self.MID)
        self.assertEqual((self.root / self.MID / "transcript.md").read_text(encoding="utf-8"), "补好了\n")

    def test_bad_wav_message(self):
        self.mk(wav_bytes=b"not a wav at all")
        with self.assertRaises(AudioUnreadable) as cm:
            self.ctl.retranscribe(self.MID)
        self.assertEqual(str(cm.exception), "录音文件损坏，无法转写")
        self.assertEqual(self.tr.calls, [])
        self.assertNotIn(self.MID, self.ctl.transcribing_ids())

    def test_normal_transcription_tracks_set(self):
        self.tr.gate.clear()
        self.tr.text = "正常稿\n"
        self.addCleanup(self.tr.gate.set)
        self.ctl.start()
        self.ctl.stop()
        self.assertTrue(self.tr.entered.wait(WAIT))
        self.assertIn(self.MID, self.ctl.transcribing_ids())
        self.tr.gate.set()
        self.wait_final(self.ctl, "completed")
        self.assertNotIn(self.MID, self.ctl.transcribing_ids())


if __name__ == "__main__":
    unittest.main()
