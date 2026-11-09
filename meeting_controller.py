"""会议控制器：后台录音线程、会后转写线程、事件日志与订阅（供 Web 层的 SSE 使用）。

约定的注入函数（均可在测试里替换成假的）：
  record_fn(out_root, *, stop, on_started, on_level, on_samples) -> Path
      阻塞录音直到 stop 置位，返回会议目录；建好目录后须调用一次 on_started(会议目录)。
      真实实现见 make_record_fn（包装 record.record）。
  transcribe_fn(meeting_dir, *, speakers) -> None
      会后转写并写出 transcript.md，失败抛异常。真实实现见 make_transcribe_fn。
  clock() -> float
      单调时钟（秒），只用来算已录时长，便于测试注入假时钟。
  live_factory(meeting_dir, on_text) -> 会话（可选）
      start(live=True) 时调用，返回带 feed(samples) / finish() 的对象；
      on_text(start_s, text) 会产生 live_text 事件。真实实现见 build_live_factory。

忙碌规则（一次只做一件事，违反抛 Busy 的子类，Web 层映射为 409）：
  - 正在录音或停止收尾中 → 不能再开始录音；
  - 上一场会议还在转写 → 不能开始新录音（最简单、最安全，避免 final 状态串场）；
  - 声纹任务进行中 → 不能开始录音；录音中 → 不能开始声纹任务。
    后者靠 link_exclusive(controller, service) 把两边连起来，并共用一把锁让“检查+占用”原子。

事件：{"seq": 单调递增整数, "type": ..., "data": {...}}，类型有
  state（data 为快照）、level、final、live_text、warning、error。
  历史最多保留 history_max 条（默认 2000）；连续的 level 事件会合并成最新一条。
  订阅者从 last_seq 续传；若 last_seq 已被挤出历史或超前（服务重启过），先收到一条
  带 resync=True 的 state 快照事件，其 seq 为当前最新 seq，之后继续收增量。
"""
import io
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import common
import live
import record
import transcribe

SPEAKERS_MIN = 1
SPEAKERS_MAX = 20
HISTORY_MAX = 2000
START_TIMEOUT_S = 30.0       # start() 最多等多久拿到会议目录（打开麦克风自身有 15 秒超时）
JOIN_TIMEOUT_S = 15.0        # close() 默认等线程收尾的时间
LIVE_INIT_TIMEOUT_S = 60.0   # 实时转写初始化（下载/加载 VAD）最多等多久，超时就放弃实时转写，只保证录音完整
LIVE_INIT_JOIN_S = 2.0       # close() 等初始化线程收尾的上限（它是守护线程，卡住也不会拖住退出）
_WAIT_SLICE_S = 0.25         # 订阅者每次最长睡多久就检查一次取消


class Busy(Exception):
    """资源被占用，Web 层映射为 409；消息可直接展示。"""


class MeetingBusy(Busy):
    """已有录音/转写在进行，或有声纹任务占用麦克风。"""


class MeetingStartError(Exception):
    """录音没能开始（打不开麦克风、磁盘不足等），消息可直接展示。"""


def link_exclusive(controller, service) -> None:
    """让会议控制器与声纹服务互斥：互相检查对方是否忙碌，并共用一把锁使“检查+占用”原子。"""
    lock = threading.RLock()
    controller.set_exclusion(service.is_busy, lock)
    service.set_exclusion(controller.is_busy, lock)


def _validate_speakers(speakers) -> Optional[int]:
    if speakers is None:
        return None
    if type(speakers) is not int or not SPEAKERS_MIN <= speakers <= SPEAKERS_MAX:
        raise ValueError(f"说话人数必须是 {SPEAKERS_MIN}–{SPEAKERS_MAX} 的整数")
    return speakers


class MeetingController:
    def __init__(self, root, *, record_fn, transcribe_fn, clock=time.monotonic,
                 live_factory=None, history_max: int = HISTORY_MAX,
                 start_timeout: float = START_TIMEOUT_S,
                 live_init_timeout: float = LIVE_INIT_TIMEOUT_S):
        self._root = Path(root)
        self._record_fn = record_fn
        self._transcribe_fn = transcribe_fn
        self._clock = clock
        self._live_factory = live_factory
        self._history_max = max(1, int(history_max))
        self._start_timeout = start_timeout
        self._live_init_timeout = live_init_timeout
        self._cond = threading.Condition(threading.RLock())
        self._coord = threading.RLock()       # 与声纹服务共用时被 set_exclusion 替换
        self._peer_busy: Callable[[], bool] = lambda: False
        # 以下状态都受 self._cond 保护
        self._seq = 0
        self._history: list = []
        self._evicted_seq = 0                 # 被挤出历史的最大 seq
        self._recording = False
        self._stopping = False
        self._final = "idle"
        self._meeting_id: Optional[str] = None
        self._t0 = 0.0
        self._elapsed_end: Optional[float] = None
        self._level = 0.0
        self._last_error: Optional[str] = None
        self._live_on = False
        self._live_session = None
        self._live_warned = False
        self._live_gen = 0                    # 每场录音/收尾加一；初始化线程用它判断自己是否已过期
        self._live_init_thread: Optional[threading.Thread] = None
        self._stop_evt: Optional[threading.Event] = None
        self._record_thread: Optional[threading.Thread] = None
        self._transcribe_thread: Optional[threading.Thread] = None
        self._closed = False

    # ---------- 互斥 ----------
    def set_exclusion(self, peer_busy: Callable[[], bool], lock) -> None:
        """由 link_exclusive 调用：设置对方忙碌检查与共用锁。"""
        self._peer_busy = peer_busy
        self._coord = lock

    def is_busy(self) -> bool:
        """是否正占用麦克风（录音中或停止收尾中）；声纹服务用它做互斥检查。"""
        return self._recording

    def is_active(self, meeting_id) -> bool:
        """该会议是否正在录音或转写；Web 层据此拒绝删除/改名（409）。"""
        with self._cond:
            if meeting_id is None or meeting_id != self._meeting_id:
                return False
            return self._recording or self._final == "running"

    # ---------- 快照与事件 ----------
    def _elapsed_locked(self) -> float:
        if self._meeting_id is None and not self._recording:
            return 0.0
        end = self._clock() if self._elapsed_end is None else self._elapsed_end
        return max(0.0, round(end - self._t0, 3))

    def _snapshot_locked(self) -> dict:
        return {
            "state": "recording" if self._recording else "idle",
            "stopping": self._stopping,
            "final": self._final,
            "meeting_id": self._meeting_id,
            "elapsed": self._elapsed_locked(),
            "level": self._level,
            "live": self._live_on,
            "error": self._last_error,
        }

    def snapshot(self) -> dict:
        with self._cond:
            return self._snapshot_locked()

    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def _emit_locked(self, type_: str, data: dict) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "type": type_, "data": data}
        if type_ == "level" and self._history and self._history[-1]["type"] == "level":
            self._history[-1] = ev            # 连续电平只留最新一条，防止挤掉重要事件
        else:
            self._history.append(ev)
            while len(self._history) > self._history_max:
                self._evicted_seq = self._history.pop(0)["seq"]
        self._cond.notify_all()
        return ev

    def _emit(self, type_: str, data: dict) -> dict:
        with self._cond:
            return self._emit_locked(type_, data)

    def _emit_state_locked(self) -> None:
        self._emit_locked("state", self._snapshot_locked())

    def subscribe(self, last_seq=None, stop: Optional[threading.Event] = None,
                  timeout: Optional[float] = 15.0):
        """订阅事件的生成器：先按 last_seq 补发历史，再等待增量。

        - last_seq 为 None / 已被挤出历史 / 超前：先发一条 resync 快照事件，再发增量；
        - 空闲满 timeout 秒 yield None 作为心跳（timeout 为 None 则不发心跳）；
        - stop 置位、控制器关闭（且无积压事件）或调用方 close() 生成器时结束。
        """
        if type(last_seq) is not int or last_seq < 0:
            last_seq = None
        cursor = last_seq
        idle_since = time.monotonic()
        while True:
            batch: list = []
            with self._cond:
                if stop is not None and stop.is_set():
                    return
                if cursor is None or cursor > self._seq or cursor < self._evicted_seq:
                    cursor = self._seq
                    batch.append({"seq": self._seq, "type": "state",
                                  "data": self._snapshot_locked(), "resync": True})
                for ev in reversed(self._history):
                    if ev["seq"] <= cursor:
                        break
                    batch.append(ev)
                    # 反向收集，下面统一翻转（resync 快照永远排最前）
                if not batch:
                    if self._closed:
                        return
                    waited = time.monotonic() - idle_since
                    if timeout is not None and waited >= timeout:
                        batch = None
                    else:
                        slice_s = _WAIT_SLICE_S if timeout is None else min(_WAIT_SLICE_S, timeout - waited)
                        self._cond.wait(max(0.001, slice_s))
                        continue
            if batch is None:
                idle_since = time.monotonic()
                yield None
                continue
            if batch and batch[0].get("resync"):
                ordered = [batch[0]] + list(reversed(batch[1:]))
            else:
                ordered = list(reversed(batch))
            for ev in ordered:
                if stop is not None and stop.is_set():
                    return
                cursor = max(cursor, ev["seq"])
                idle_since = time.monotonic()
                yield dict(ev)

    # ---------- 录音 ----------
    def start(self, *, speakers=None, live: bool = False) -> str:
        """开始录音，返回会议 id（阻塞到会议目录建好）。"""
        speakers = _validate_speakers(speakers)
        if type(live) is not bool:
            raise ValueError("live 必须是布尔值")
        with self._coord:
            with self._cond:
                if self._closed:
                    raise MeetingBusy("服务正在关闭")
                if self._recording:
                    raise MeetingBusy("正在录音，请先停止")
                if self._final == "running":
                    raise MeetingBusy("上一场会议还在转写，请稍候")
                if self._peer_busy():
                    raise MeetingBusy("正在登记声纹，请稍后再录音")
                self._recording = True
                self._stopping = False
                self._final = "idle"
                self._meeting_id = None
                self._t0 = self._clock()
                self._elapsed_end = None
                self._level = 0.0
                self._last_error = None
                self._live_on = bool(live)
                self._live_session = None
                self._live_warned = False
                self._live_gen += 1
                self._live_init_thread = None
                stop_evt = threading.Event()
                self._stop_evt = stop_evt
                started_evt = threading.Event()
                t = threading.Thread(
                    target=self._record_main, args=(speakers, bool(live), stop_evt, started_evt),
                    name="mr-record", daemon=True)
                self._record_thread = t
                self._emit_state_locked()
                t.start()
        if not started_evt.wait(self._start_timeout):
            stop_evt.set()
            raise MeetingStartError("打开麦克风超时，请重试")
        with self._cond:
            if self._meeting_id is None:
                raise MeetingStartError(self._last_error or "录音没能开始")
            return self._meeting_id

    def stop(self) -> None:
        """请求停止录音，立即返回；收尾与转写在后台完成。未在录音时什么都不做。"""
        with self._cond:
            if not self._recording or self._stopping:
                return
            self._stopping = True
            evt = self._stop_evt
            self._emit_state_locked()
        if evt is not None:
            evt.set()

    # record_fn 的回调（在录音线程里执行）
    def _make_on_started(self, live: bool, started_evt: threading.Event):
        def on_started(meeting_dir) -> None:
            meeting_dir = Path(meeting_dir)
            with self._cond:
                self._meeting_id = meeting_dir.name
                self._emit_state_locked()
            started_evt.set()          # 先放行 start()
            if live:
                # 实时转写初始化可能很慢（下载/加载 VAD 模型），绝不能占用录音线程：
                # 录音线程必须马上去消费音频队列，所以放到独立线程里，好了再挂接
                self._spawn_live_init(meeting_dir)
        return on_started

    def _warn_once_locked(self, message: str) -> None:
        if not self._live_warned:
            self._live_warned = True
            self._emit_locked("warning", {"message": message})

    def _spawn_live_init(self, meeting_dir: Path) -> None:
        with self._cond:
            gen = self._live_gen
            token = {"expired": False, "done": False}
            t = threading.Thread(target=self._live_init_main, args=(meeting_dir, gen, token),
                                 name="mr-live-init", daemon=True)
            self._live_init_thread = t
            # 看门狗：初始化卡住（例如下载不动）时到点就放弃实时转写，录音与停止都不受影响
            timer = threading.Timer(self._live_init_timeout, self._live_init_expire, args=(gen, token))
            timer.daemon = True
            token["timer"] = timer
        t.start()
        timer.start()

    def _live_init_expire(self, gen: int, token: dict) -> None:
        with self._cond:
            if token["done"] or token["expired"]:
                return
            token["expired"] = True
            if gen == self._live_gen and self._recording:
                self._live_on = False
                self._warn_once_locked("实时转写初始化超时，已放弃，继续录音")

    def _live_init_main(self, meeting_dir: Path, gen: int, token: dict) -> None:
        """独立线程里创建实时转写会话；录音已结束或初始化超时则静默丢弃（音频不受影响）。"""
        session = None
        error = None
        if self._live_factory is None:
            error = "实时转写不可用，将只录音"
        else:
            try:
                session = self._live_factory(meeting_dir, self._on_live_text)
            except Exception as e:  # noqa: BLE001 实时转写失败不影响录音
                error = f"实时转写启动失败，继续录音（{e}）"
        with self._cond:
            token["done"] = True
            token["timer"].cancel()
            current = (gen == self._live_gen and self._recording and not self._stopping
                       and not self._closed and not token["expired"])
            if error is not None:
                # 失败提示只要这场录音还没结束就给（即使用户已按停止）
                if gen == self._live_gen and self._recording and not token["expired"]:
                    self._live_on = False
                    self._warn_once_locked(error)
                return
            if current:
                self._live_session = session
                return
        # 过期的会话（录音已结束/已超时）：静默收掉它的后台线程，不挂接、不喂音频
        try:
            session.finish()
        except Exception:  # noqa: BLE001
            pass

    def _on_live_text(self, start_s: float, text: str) -> None:
        self._emit("live_text", {"t": start_s, "text": text})

    def _on_level(self, rms: float) -> None:
        with self._cond:
            self._level = float(rms)
            self._emit_locked("level", {"rms": float(rms)})

    def _on_samples(self, samples) -> None:
        session = self._live_session
        if session is None:
            return
        try:
            session.feed(samples)
        except Exception as e:  # noqa: BLE001
            with self._cond:
                self._warn_once_locked(f"实时转写出错，已跳过（{e}）")

    def _record_main(self, speakers, live, stop_evt, started_evt) -> None:
        meeting_dir = None
        error = None
        try:
            try:
                meeting_dir = self._record_fn(
                    self._root, stop=stop_evt,
                    on_started=self._make_on_started(live, started_evt),
                    on_level=self._on_level, on_samples=self._on_samples)
            except Exception as e:  # noqa: BLE001 线程里不能让异常逃出去
                error = str(e) if isinstance(e, record.RecorderError) else f"录音失败（{e}）"
            with self._cond:
                self._live_gen += 1         # 让还没完成的初始化线程知道录音已结束，不得再挂接
                session = self._live_session
            if session is not None:
                try:
                    session.finish()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._finish_recording(meeting_dir, error, speakers, started_evt)

    def _finish_recording(self, meeting_dir, error, speakers, started_evt) -> None:
        with self._cond:
            if meeting_dir is not None and self._meeting_id is None:
                self._meeting_id = Path(meeting_dir).name
            self._elapsed_end = self._clock()
            self._recording = False
            self._stopping = False
            self._live_session = None
            self._level = 0.0
            if error is not None:
                self._final = "failed"
                self._last_error = error
                self._emit_state_locked()
                self._emit_locked("error", {"message": error})
                self._emit_locked("final", {"status": "failed", "meeting_id": self._meeting_id})
            elif self._meeting_id is None:
                self._final = "failed"
                self._last_error = "录音没有产生会议目录"
                self._emit_state_locked()
                self._emit_locked("error", {"message": self._last_error})
                self._emit_locked("final", {"status": "failed", "meeting_id": None})
            else:
                self._final = "running"
                mdir = self._root / self._meeting_id
                t = threading.Thread(target=self._transcribe_main, args=(mdir, speakers),
                                     name="mr-transcribe", daemon=True)
                self._transcribe_thread = t
                self._emit_state_locked()
                t.start()
        started_evt.set()

    def _transcribe_main(self, meeting_dir: Path, speakers) -> None:
        error = None
        try:
            self._transcribe_fn(meeting_dir, speakers=speakers)
        except Exception as e:  # noqa: BLE001
            error = f"转写失败（{e}）"
        with self._cond:
            mid = self._meeting_id
            if error is None:
                self._final = "completed"
                self._emit_state_locked()
                self._emit_locked("final", {"status": "completed", "meeting_id": mid})
            else:
                self._final = "failed"
                self._last_error = error
                self._emit_state_locked()
                self._emit_locked("error", {"message": error})
                self._emit_locked("final", {"status": "failed", "meeting_id": mid})

    # ---------- 收尾 ----------
    def close(self, timeout: float = JOIN_TIMEOUT_S) -> bool:
        """停止录音、唤醒订阅者并等待后台线程结束；全部按时结束返回 True。可重复调用。"""
        with self._cond:
            self._closed = True
            evt = self._stop_evt if self._recording else None
            threads = [self._record_thread, self._transcribe_thread]
            self._cond.notify_all()
        if evt is not None:
            evt.set()
        deadline = time.monotonic() + timeout
        ok = True
        for t in threads:
            if t is not None and t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))
                ok = ok and not t.is_alive()
        # 录音线程收尾时才会创建转写线程，所以录音线程结束后再取一次
        with self._cond:
            t2 = self._transcribe_thread
            t3 = self._live_init_thread
        if t3 is not None and t3 is not threading.current_thread():
            # 实时初始化线程是守护线程且已被作废；只做有上限的等待，卡住也不影响退出
            t3.join(min(LIVE_INIT_JOIN_S, max(0.0, deadline - time.monotonic())))
        if t2 is not None and t2 not in threads:
            t2.join(max(0.0, deadline - time.monotonic()))
            ok = ok and not t2.is_alive()
        return ok

    shutdown = close


# ---------- 真实实现的装配（供 web.py 使用，单元测试只用假的） ----------
def make_record_fn(*, stream_factory=None, now=datetime.now, free_bytes_fn=common.free_bytes):
    """包装 record.record，得到 MeetingController 需要的 record_fn。"""
    def record_fn(out_root, *, stop, on_started, on_level, on_samples):
        return record.record(
            out_root, stream_factory=stream_factory or record._real_stream_factory,
            stop=stop, now=now, on_level=on_level, on_samples=on_samples,
            on_started=on_started, free_bytes_fn=free_bytes_fn)
    return record_fn


def make_transcribe_fn(*, model: str = transcribe.DEFAULT_MODEL, deps=None, run=None):
    """会后处理：压缩 m4a（失败只警告）→ transcribe_wav → 原子写 transcript.md。"""
    deps = dict(deps or {})

    def transcribe_fn(meeting_dir, *, speakers):
        meeting_dir = Path(meeting_dir)
        wav = meeting_dir / "meeting.wav"
        kwargs = {"run": run} if run is not None else {}
        record.compress_m4a(wav, meeting_dir / "meeting.m4a", stderr=deps.get("stderr"), **kwargs)
        text = transcribe.transcribe_wav(wav, model=model, speakers=speakers, deps=deps)
        common.atomic_write_text(meeting_dir / "transcript.md", text)
    return transcribe_fn


class _NullOut:
    """吞掉 LiveSink 往终端的输出（Web 版只写 live.txt 和事件）。"""

    def write(self, _s) -> int:
        return 0

    def flush(self) -> None:
        pass


class _EventSink(live.LiveSink):
    """写 live.txt 后再回调 on_text，用来产生 live_text 事件。"""

    def __init__(self, live_path, on_text, *, stderr=None):
        super().__init__(live_path, out=_NullOut(), stderr=stderr)
        self._on_text = on_text

    def write(self, start_s: float, text: str) -> None:
        super().write(start_s, text)
        try:
            self._on_text(start_s, text)
        except Exception:  # noqa: BLE001
            pass


def build_live_factory(*, deps=None, model: str = transcribe.DEFAULT_MODEL, stderr=None):
    """装配 live_factory：与 record.main 的实时转写同一套部件（切句器 + LiveTranscriber + LiveSession）。

    deps 的键同 record.main（vad_factory / vad_model_dir / vad_download / live_transcribe_fn）。
    首次调用可能要下载 VAD 模型，建议服务启动时先预热；失败抛异常，由控制器降级为只录音。
    """
    deps = dict(deps or {})
    stderr = stderr if stderr is not None else io.StringIO()

    def factory(meeting_dir, on_text):
        segmenter = record._prepare_segmenter(deps, stderr)
        if segmenter is None:
            raise RuntimeError("实时转写所需的 VAD 模型不可用")
        sink = _EventSink(Path(meeting_dir) / "live.txt", on_text, stderr=stderr)
        live_fn = deps.get("live_transcribe_fn")
        transcriber = live.LiveTranscriber(
            lambda audio: transcribe.transcribe_samples(audio, model=model, transcribe_fn=live_fn),
            sink, stderr=stderr)
        return live.LiveSession(segmenter, transcriber)
    return factory
