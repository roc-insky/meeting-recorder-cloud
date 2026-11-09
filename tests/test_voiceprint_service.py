"""voiceprint_service 的单元测试（假登记函数，PROFILE_DIR 指向临时目录，不碰真实麦克风、模型和 home）。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import voiceprint
import voiceprint_service
from tests.test_enroll import mic_factory, speech, write_wav
from voiceprint_service import JobNotFound, VoiceprintBusy, VoiceprintService

WAIT = 5.0


def mr_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mr-") and t.is_alive()]


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        p = mock.patch.object(voiceprint, "PROFILE_DIR", self.tmp / "voiceprints")
        p.start()
        self.addCleanup(p.stop)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.record_calls, self.import_calls = [], []
        self.fail = None
        self.honor_stop = True

    def fake_record(self, name, seconds, replace, *, stop, on_progress):
        self.record_calls.append((name, seconds, replace))
        on_progress(1.5)
        self.entered.set()
        if self.honor_stop:
            while not self.gate.is_set() and not stop.is_set():
                stop.wait(0.01)
        else:
            self.gate.wait(WAIT)
        if self.fail:
            raise self.fail
        if stop.is_set():
            raise voiceprint.VoiceprintError("已取消")

    def fake_import(self, name, path, replace, *, stop):
        self.import_calls.append((name, Path(path), replace))
        self.entered.set()
        self.gate.wait(WAIT)
        if self.fail:
            raise self.fail

    def make(self):
        svc = VoiceprintService(record_enroll_fn=self.fake_record, import_fn=self.fake_import)
        self.addCleanup(svc.close)
        self.addCleanup(self.gate.set)
        return svc

    def wait_state(self, svc, job, state):
        self.assertTrue(svc.join(WAIT))
        self.assertEqual(svc.status(job)["state"], state, svc.status(job))


class JobTests(ServiceCase):
    def test_record_job_completes(self):
        svc = self.make()
        job = svc.start_record("张三", 30, False)
        self.assertEqual(len(job), 32)
        self.assertTrue(self.entered.wait(WAIT))
        st = svc.status(job)
        self.assertEqual((st["state"], st["kind"], st["name"], st["progress"]), ("running", "record", "张三", 1.5))
        self.gate.set()
        self.wait_state(svc, job, "completed")
        self.assertEqual(self.record_calls, [("张三", 30, False)])
        self.assertFalse(svc.is_busy())

    def test_import_job_completes(self):
        f = self.tmp / "a.wav"
        write_wav(f, speech(1))
        svc = self.make()
        self.gate.set()
        job = svc.start_import("李四", f, True)
        self.wait_state(svc, job, "completed")
        self.assertEqual(self.import_calls, [("李四", f, True)])

    def test_failure_reports_chinese_message(self):
        svc = self.make()
        self.gate.set()
        self.fail = voiceprint.VoiceprintError("有效语音太短，请再录一次")
        job = svc.start_record("张三", 30, False)
        self.wait_state(svc, job, "failed")
        self.assertEqual(svc.status(job)["error"], "有效语音太短，请再录一次")
        self.fail = RuntimeError("模型坏了")
        job2 = svc.start_record("张三", 30, False)
        self.wait_state(svc, job2, "failed")
        self.assertEqual(svc.status(job2)["error"], "登记失败（模型坏了）")

    def test_single_job_at_a_time(self):
        svc = self.make()
        job = svc.start_record("张三", 30, False)
        self.assertTrue(svc.is_busy())
        with self.assertRaises(VoiceprintBusy):
            svc.start_record("李四", 30, False)
        with self.assertRaises(VoiceprintBusy):
            f = self.tmp / "a.wav"
            write_wav(f, speech(1))
            svc.start_import("李四", f, False)
        self.gate.set()
        self.wait_state(svc, job, "completed")
        self.gate.set()
        svc.start_record("李四", 30, False)    # 结束后可再来

    def test_cancel_cooperative(self):
        svc = self.make()
        job = svc.start_record("张三", 30, False)
        self.assertTrue(self.entered.wait(WAIT))
        self.assertTrue(svc.cancel(job))
        self.assertEqual(svc.status(job)["state"], "cancelled")
        self.assertTrue(svc.join(WAIT))
        self.assertEqual(svc.status(job)["state"], "cancelled")   # 工作线程以“已取消”异常结束也不变成 failed
        self.assertIsNone(svc.status(job)["error"])
        self.assertFalse(svc.is_busy())
        self.assertFalse(svc.cancel(job))                     # 已结束再取消 → False

    def test_cancel_non_cooperative_discards_result(self):
        self.honor_stop = False
        svc = self.make()
        job = svc.start_record("张三", 30, False)
        self.assertTrue(self.entered.wait(WAIT))
        svc.cancel(job)
        self.assertTrue(svc.is_busy())                       # 线程还没退出，仍占用
        with self.assertRaises(VoiceprintBusy):
            svc.start_record("李四", 30, False)
        self.gate.set()                                      # 无视 stop 的函数正常返回
        self.wait_state(svc, job, "cancelled")               # 结果被丢弃，不变成 completed

    def test_unknown_job(self):
        svc = self.make()
        for bad in ("nope", None, 5):
            with self.assertRaises(JobNotFound):
                svc.status(bad)
            with self.assertRaises(JobNotFound):
                svc.cancel(bad)

    def test_jobs_bounded(self):
        svc = self.make()
        self.gate.set()
        ids = []
        for _ in range(voiceprint_service.JOBS_MAX + 6):
            j = svc.start_record("张三", 30, False)
            ids.append(j)
            self.assertTrue(svc.join(WAIT))
        self.assertLessEqual(len(svc._jobs), voiceprint_service.JOBS_MAX)
        with self.assertRaises(JobNotFound):
            svc.status(ids[0])
        self.assertEqual(svc.status(ids[-1])["state"], "completed")
        self.assertEqual(len(set(ids)), len(ids))

    def test_status_returns_copy(self):
        svc = self.make()
        self.gate.set()
        job = svc.start_record("张三", 30, False)
        svc.join(WAIT)
        svc.status(job)["state"] = "hacked"
        self.assertEqual(svc.status(job)["state"], "completed")


class ValidationTests(ServiceCase):
    def test_bad_names(self):
        svc = self.make()
        for bad in ("", "a/b", "../x", "说话人A", "x" * 21, None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(voiceprint.VoiceprintError):
                    svc.start_record(bad, 30, False)
        self.assertEqual(self.record_calls, [])

    def test_bad_seconds(self):
        svc = self.make()
        for bad in (True, 0, 9, 301, -5, "30", 30.0, None):
            with self.subTest(bad=bad):
                with self.assertRaises(voiceprint.VoiceprintError) as cm:
                    svc.start_record("张三", bad, False)
                self.assertIn("秒", str(cm.exception))
        self.gate.set()
        for ok in (10, 300):
            svc.start_record("张三", ok, False)
            svc.join(WAIT)

    def test_replace_must_be_bool_and_existing_needs_replace(self):
        svc = self.make()
        with self.assertRaises(voiceprint.VoiceprintError):
            svc.start_record("张三", 30, "yes")
        voiceprint.save_profile("张三", np.ones(4, dtype=np.float32), seconds=12, chunks=4,
                                model="m", replace=False)
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            svc.start_record("张三", 30, False)
        self.assertIn("已存在", str(cm.exception))
        self.gate.set()
        svc.start_record("张三", 30, True)      # replace=True 通过

    def test_import_missing_or_not_regular_file(self):
        svc = self.make()
        with self.assertRaises(voiceprint.VoiceprintError) as cm:
            svc.start_import("张三", self.tmp / "不存在.wav", False)
        self.assertIn("找不到文件", str(cm.exception))
        with self.assertRaises(voiceprint.VoiceprintError):
            svc.start_import("张三", self.tmp, False)           # 目录
        for bad in ("", None, 5):
            with self.assertRaises(voiceprint.VoiceprintError):
                svc.start_import("张三", bad, False)
        self.assertEqual(self.import_calls, [])
        self.assertFalse(svc.is_busy())


class DefaultImplementationTests(ServiceCase):
    """默认实现：假 extractor / 假麦克风，真实 enroll / voiceprint 保存逻辑。"""

    def extractor(self, samples):
        return np.array([1.0, 0.5, 0.2, 0.1], dtype=np.float32)

    def test_default_import_saves_profile(self):
        f = self.tmp / "a.wav"
        write_wav(f, speech(12))
        svc = VoiceprintService(extractor=self.extractor)
        self.addCleanup(svc.close)
        job = svc.start_import("王五", f, False)
        self.wait_state(svc, job, "completed")
        self.assertTrue(voiceprint.profile_path("王五").exists())

    def test_default_import_too_short_fails(self):
        f = self.tmp / "a.wav"
        write_wav(f, speech(2))
        svc = VoiceprintService(extractor=self.extractor)
        self.addCleanup(svc.close)
        job = svc.start_import("王五", f, False)
        self.wait_state(svc, job, "failed")
        self.assertEqual(svc.status(job)["error"], "有效语音太短，请再录一次")
        self.assertFalse(voiceprint.profile_path("王五").exists())

    def test_default_import_garbage_file_fails(self):
        f = self.tmp / "a.wav"
        f.write_bytes(b"not a wav")
        svc = VoiceprintService(extractor=self.extractor)
        self.addCleanup(svc.close)
        job = svc.start_import("王五", f, False)
        self.wait_state(svc, job, "failed")
        self.assertTrue(svc.status(job)["error"])

    def test_default_record_saves_profile(self):
        svc = VoiceprintService(extractor=self.extractor,
                                stream_factory=mic_factory([speech(10.5)]))
        self.addCleanup(svc.close)
        job = svc.start_record("赵六", 10, False)
        self.wait_state(svc, job, "completed")
        self.assertTrue(voiceprint.profile_path("赵六").exists())

    def test_default_record_cancel_via_stop(self):
        release = threading.Event()

        class Silent:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        svc = VoiceprintService(extractor=self.extractor, stream_factory=lambda cb: Silent())
        self.addCleanup(svc.close)
        self.addCleanup(release.set)
        job = svc.start_record("赵六", 10, False)
        svc.cancel(job)
        self.assertTrue(svc.join(WAIT))          # 麦克风录音循环响应 stop
        self.assertEqual(svc.status(job)["state"], "cancelled")
        self.assertFalse(voiceprint.profile_path("赵六").exists())


class CancelCommitRaceTests(ServiceCase):
    """取消与保存提交点互斥：绝不出现“状态 cancelled 但声纹已落盘”。"""

    def extractor(self, samples):
        return np.array([1.0, 0.5, 0.2, 0.1], dtype=np.float32)

    def test_cancel_while_save_in_flight_ends_completed(self):
        f = self.tmp / "a.wav"
        write_wav(f, speech(12))
        real_save = voiceprint.save_profile
        in_save, release = threading.Event(), threading.Event()

        def blocking_save(*a, **kw):
            in_save.set()
            self.assertTrue(release.wait(WAIT))
            return real_save(*a, **kw)

        svc = VoiceprintService(extractor=self.extractor)
        self.addCleanup(svc.close)
        self.addCleanup(release.set)
        with mock.patch.object(voiceprint, "save_profile", blocking_save):
            job = svc.start_import("王五", f, False)
            self.assertTrue(in_save.wait(WAIT))
            self.assertFalse(svc.cancel(job))     # 已在落盘，取消无效
            release.set()
            self.wait_state(svc, job, "completed")
        self.assertTrue(svc.join(WAIT))
        self.assertTrue(voiceprint.profile_path("王五").exists())

    def test_cancel_before_commit_saves_nothing(self):
        f = self.tmp / "a.wav"
        write_wav(f, speech(12))
        in_extract, release = threading.Event(), threading.Event()

        def slow_extractor(samples):
            in_extract.set()
            self.assertTrue(release.wait(WAIT))
            return self.extractor(samples)

        svc = VoiceprintService(extractor=slow_extractor)
        self.addCleanup(svc.close)
        self.addCleanup(release.set)
        job = svc.start_import("王五", f, False)
        self.assertTrue(in_extract.wait(WAIT))
        self.assertTrue(svc.cancel(job))
        release.set()
        self.assertTrue(svc.join(WAIT))
        self.assertEqual(svc.status(job)["state"], "cancelled")
        self.assertFalse(voiceprint.profile_path("王五").exists())

    def test_cancel_after_stop_check_cannot_replace_existing_profile(self):
        """旧代码：stop 检查通过后、保存前取消 → 状态 cancelled 但旧声纹被替换。"""
        f = self.tmp / "a.wav"
        write_wav(f, speech(12))
        voiceprint.save_profile("王五", np.ones(4, dtype=np.float32), seconds=1, chunks=1, model="old")
        real_save = voiceprint.save_profile
        in_save, release = threading.Event(), threading.Event()

        def blocking_save(*a, **kw):
            in_save.set()
            release.wait(WAIT)
            return real_save(*a, **kw)

        svc = VoiceprintService(extractor=self.extractor)
        self.addCleanup(svc.close)
        self.addCleanup(release.set)
        with mock.patch.object(voiceprint, "save_profile", blocking_save):
            job = svc.start_import("王五", f, True)
            self.assertTrue(in_save.wait(WAIT))
            cancelled = svc.cancel(job)
            release.set()
            self.assertTrue(svc.join(WAIT))
        st = svc.status(job)["state"]
        replaced = voiceprint.list_profiles()[0]["model"] != "old"
        self.assertFalse(st == "cancelled" and replaced)
        self.assertEqual((cancelled, st, replaced), (False, "completed", True))


class ShutdownTests(ServiceCase):
    def test_close_cancels_and_joins(self):
        before = set(mr_threads())
        svc = self.make()
        svc.start_record("张三", 30, False)
        self.assertTrue(self.entered.wait(WAIT))
        self.assertTrue(svc.close(WAIT))
        self.assertEqual(set(mr_threads()) - before, set())

    def test_close_when_idle(self):
        self.assertTrue(self.make().close())


if __name__ == "__main__":
    unittest.main()
