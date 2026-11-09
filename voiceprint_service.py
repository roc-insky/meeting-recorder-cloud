"""声纹后台任务：现场录制或导入文件登记声纹，同一时间只允许一个任务。

注入函数（测试用假的；缺省用 enroll / voiceprint 的真实实现）：
  record_enroll_fn(name, seconds, replace, *, stop, on_progress) -> None
  import_fn(name, path, replace, *, stop) -> None
  失败抛异常（VoiceprintError 的消息原样展示，其他异常包装成“登记失败（…）”）。
取消是协作式的：stop 置位后真实实现会在录音循环里退出，保存前也会再检查一次；
注入的函数若不理会 stop，任务仍标记为 cancelled，其结果被丢弃。

忙碌规则：已有任务的工作线程还没退出 → VoiceprintBusy（取消的任务也要等线程退出，
因为麦克风可能还被占着）；会议正在录音 → VoiceprintBusy（见 meeting_controller.link_exclusive）。
"""
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

import common
import enroll
import record
import transcribe
import voiceprint
from meeting_controller import Busy

SECONDS_MIN = 10        # 登记要求有效语音至少 10 秒（voiceprint.enroll_vector 的 min_seconds）
SECONDS_MAX = 300
JOBS_MAX = 20
JOIN_TIMEOUT_S = 15.0
PENDING, RUNNING, COMPLETED, FAILED, CANCELLED = "pending", "running", "completed", "failed", "cancelled"
_FINISHED = (COMPLETED, FAILED, CANCELLED)


class VoiceprintBusy(Busy):
    """已有声纹任务在进行，或会议正在录音。"""


class JobNotFound(Exception):
    """任务 id 不存在（或已被淘汰）。"""


class VoiceprintService:
    def __init__(self, *, record_enroll_fn=None, import_fn=None, extractor=None, stream_factory=None):
        self._extractor = extractor
        self._stream_factory = stream_factory
        self._record_enroll_fn = record_enroll_fn or self._default_record_enroll
        self._import_fn = import_fn or self._default_import
        self._lock = threading.RLock()
        self._coord = threading.RLock()
        self._peer_busy: Callable[[], bool] = lambda: False
        self._jobs: "OrderedDict[str, dict]" = OrderedDict()
        self._threads: dict = {}
        self._stops: dict = {}
        self._active: Optional[str] = None

    # ---------- 互斥 ----------
    def set_exclusion(self, peer_busy: Callable[[], bool], lock) -> None:
        self._peer_busy = peer_busy
        self._coord = lock

    def is_busy(self) -> bool:
        """是否有任务占用（工作线程未退出）；会议控制器用它做互斥检查。"""
        return self._active is not None

    # ---------- 校验 ----------
    @staticmethod
    def _check_common(name, replace) -> None:
        voiceprint.validate_name(name)
        if type(replace) is not bool:
            raise voiceprint.VoiceprintError("replace 必须是布尔值")
        if not replace and voiceprint.profile_path(name).exists():
            raise voiceprint.VoiceprintError(f"声纹“{name}”已存在，勾选覆盖后可重新登记")

    # ---------- 提交任务 ----------
    def start_record(self, name, seconds, replace=False) -> str:
        self._check_common(name, replace)
        if type(seconds) is not int or not SECONDS_MIN <= seconds <= SECONDS_MAX:
            raise voiceprint.VoiceprintError(f"录音秒数必须是 {SECONDS_MIN}–{SECONDS_MAX} 的整数")
        return self._submit("record", name, replace,
                            lambda job, stop: self._record_enroll_fn(
                                name, seconds, replace, stop=stop,
                                on_progress=lambda s: self._set(job, progress=float(s))))

    def start_import(self, name, path, replace=False) -> str:
        self._check_common(name, replace)
        if not isinstance(path, (str, Path)) or not str(path):
            raise voiceprint.VoiceprintError("文件路径不能为空")
        p = Path(path)
        if not p.is_file():
            raise voiceprint.VoiceprintError(f"找不到文件：{p}")
        return self._submit("import", name, replace,
                            lambda job, stop: self._import_fn(name, p, replace, stop=stop))

    def _submit(self, kind, name, replace, work) -> str:
        with self._coord:
            with self._lock:
                if self._active is not None:
                    raise VoiceprintBusy("已有声纹任务在进行，请稍候")
                if self._peer_busy():
                    raise VoiceprintBusy("正在录音，不能登记声纹")
                job_id = uuid.uuid4().hex
                job = {"id": job_id, "kind": kind, "name": name, "replace": replace,
                       "state": PENDING, "error": None, "progress": 0.0}
                stop = threading.Event()
                self._jobs[job_id] = job
                self._stops[job_id] = stop
                self._active = job_id
                self._trim_locked()
                t = threading.Thread(target=self._run, args=(job, stop, work),
                                     name="mr-voiceprint", daemon=True)
                self._threads[job_id] = t
                t.start()
        return job_id

    def _trim_locked(self) -> None:
        for jid in list(self._jobs):
            if len(self._jobs) <= JOBS_MAX:
                break
            if jid != self._active and self._jobs[jid]["state"] in _FINISHED:
                del self._jobs[jid]
                self._stops.pop(jid, None)
                self._threads.pop(jid, None)

    def _set(self, job, **kv) -> None:
        with self._lock:
            if job["state"] not in _FINISHED:
                job.update(kv)

    def _run(self, job, stop, work) -> None:
        self._set(job, state=RUNNING)
        state, error = COMPLETED, None
        try:
            work(job, stop)
        except Exception as e:  # noqa: BLE001 后台线程不能崩
            state = FAILED
            error = str(e) if isinstance(e, voiceprint.VoiceprintError) else f"登记失败（{e}）"
        with self._lock:
            if job["state"] != CANCELLED:        # 已取消的任务丢弃结果
                if stop.is_set():
                    state, error = CANCELLED, None
                job.update(state=state, error=error)
            self._active = None if self._active == job["id"] else self._active

    # ---------- 查询与取消 ----------
    def status(self, job_id) -> dict:
        with self._lock:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
            if job is None:
                raise JobNotFound("任务不存在")
            return dict(job)

    def cancel(self, job_id) -> bool:
        """取消任务；已结束的任务返回 False，未知 id 抛 JobNotFound。"""
        with self._lock:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
            if job is None:
                raise JobNotFound("任务不存在")
            if job["state"] in _FINISHED:
                return False
            job.update(state=CANCELLED, error=None)
            self._stops[job_id].set()
            return True

    def join(self, timeout: float = JOIN_TIMEOUT_S) -> bool:
        """等当前任务的工作线程退出；按时退出返回 True。"""
        with self._lock:
            t = self._threads.get(self._active) if self._active else None
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    def close(self, timeout: float = JOIN_TIMEOUT_S) -> bool:
        """取消进行中的任务并等线程结束。"""
        with self._lock:
            active = self._active
        if active is not None:
            self.cancel(active)
        return self.join(timeout)

    # ---------- 默认的真实实现 ----------
    def _enroll_samples(self, name, samples, replace, stop) -> None:
        extractor = self._extractor or voiceprint.default_extractor()
        vector, chunks, speech_s = voiceprint.enroll_vector(samples, extractor=extractor)
        if stop.is_set():
            raise voiceprint.VoiceprintError("已取消")
        voiceprint.save_profile(name, vector, seconds=speech_s, chunks=chunks,
                                model=voiceprint.MODEL_NAME, replace=replace)

    def _default_record_enroll(self, name, seconds, replace, *, stop, on_progress) -> None:
        samples = enroll.record_from_mic(
            seconds, stream_factory=self._stream_factory or record._real_stream_factory,
            stop=stop, on_progress=on_progress)
        self._enroll_samples(name, samples, replace, stop)

    def _default_import(self, name, path, replace, *, stop) -> None:
        try:
            rate, samples = common.read_wav_tolerant(Path(path))
        except OSError as e:
            raise voiceprint.VoiceprintError(f"无法读取文件：{path}（{e}）") from e
        except ValueError as e:
            raise voiceprint.VoiceprintError(str(e)) from e
        self._enroll_samples(name, transcribe._resample(samples, rate), replace, stop)
