"""录音：麦克风 → meeting.wav（边录边落盘）→ m4a 压缩 → 转写。

main 的 deps 字典（均可选，便于测试）：
  stream_factory(callback) -> 上下文管理器   打开输入流；callback(块) 只会把块放入队列
  stop                                       threading.Event，置位即停止录音
  run                                        替换 subprocess.run（压缩 m4a）
  now / free_bytes_fn                        时间与可用空间
  stdout / stderr                            输出流
  transcribe_fn / diarize_fn / started       原样传给 transcribe.main
  vad_factory() -> VAD 检测器                 --live 时替换真实 Silero VAD（缺省会先确保 VAD 模型已下载）
  live_transcribe_fn(audio, **kwargs)         --live 逐句转写用，缺省 mlx_whisper
  vad_model_dir / vad_download                VAD 模型缓存目录与下载函数（测试注入，缺省用 diarize.MODEL_DIR 与 urllib）
"""
import argparse
import queue
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, ContextManager

import numpy as np

import common
import diarize
import live
import transcribe

HEADER_SYNC_S = 1.0          # 每累计这么多秒音频就回写一次 wav 头里的长度
STREAM_OPEN_TIMEOUT_S = 15.0  # 打开麦克风的超时
STREAM_CLOSE_TIMEOUT_S = 5.0  # 关闭麦克风最多等待
SILENCE_WARN_S = 10.0         # 连续静音多久提醒
LEVEL_FULL_SCALE = 0.1        # 电平条满格对应的 RMS（归一化）
EXIT_RECORD_FAILED = 1
EXIT_TRANSCRIBE_FAILED = 2


class RecorderError(Exception):
    """录音相关的可预期错误，消息可直接展示给用户。"""


class WavWriter:
    """边写边可恢复的 16 位单声道 wav 写入器。

    打开时写 44 字节头（长度为 0），每累计 HEADER_SYNC_S 秒音频回写一次长度并刷盘，
    这样进程崩溃后文件也能被 common.read_wav_tolerant 读出。
    """

    def __init__(self, path: Path, sample_rate: int = common.SAMPLE_RATE):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self._data_bytes = 0
        self._since_sync = 0  # 自上次回写以来的样本数
        self._f = open(self.path, "wb")
        self._f.write(self._header(0))
        self._f.flush()

    def _header(self, data_len: int) -> bytes:
        rate = self.sample_rate
        return (
            b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", data_len)
        )

    def _sync(self) -> None:
        self._f.seek(0)
        self._f.write(self._header(self._data_bytes))
        self._f.seek(0, 2)
        self._f.flush()
        self._since_sync = 0

    def write(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples)
        if samples.ndim > 1:
            samples = samples[:, 0]
        data = samples.astype("<i2", copy=False).tobytes()
        self._f.write(data)
        self._data_bytes += len(data)
        self._since_sync += len(data) // 2
        if self._since_sync >= self.sample_rate * HEADER_SYNC_S:
            self._sync()

    def close(self) -> None:
        if self._f.closed:
            return
        try:
            self._sync()
        finally:
            self._f.close()


def level_bar(rms: float, width: int = 20) -> str:
    """把 RMS（0~1）画成定长电平条；超出范围按满格/空格处理。"""
    frac = min(1.0, max(0.0, rms / LEVEL_FULL_SCALE))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


class SilenceWatch:
    """连续静音超过阈值时首次返回 True；声音恢复后再静音会重新计时。"""

    def __init__(self, threshold_s: float = SILENCE_WARN_S):
        self.threshold_s = threshold_s
        self._since = None
        self._fired = False

    def update(self, samples: np.ndarray, now: float) -> bool:
        if not common.is_silent(samples):
            self._since = None
            self._fired = False
            return False
        if self._since is None:
            self._since = now
        if not self._fired and now - self._since >= self.threshold_s:
            self._fired = True
            return True
        return False


def open_stream(factory: Callable[[], ContextManager], timeout: float = STREAM_OPEN_TIMEOUT_S):
    """在守护线程 mr-stream-open 里调用 factory() 并进入上下文，返回该上下文对象。

    系统音频卡住时 PortAudio 可能永远不返回，所以超时后放弃等待；
    若线程之后才打开成功，会自行把这个迟到的流关掉。
    """
    done = threading.Event()
    lock = threading.Lock()
    state = {"cm": None, "error": None, "abandoned": False}

    def work():
        try:
            cm = factory()
            cm.__enter__()
        except BaseException as e:  # noqa: BLE001 线程里必须兜住所有异常
            state["error"] = e
            done.set()
            return
        with lock:
            late = state["abandoned"]
            if not late:
                state["cm"] = cm
        if late:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        done.set()

    threading.Thread(target=work, name="mr-stream-open", daemon=True).start()
    if not done.wait(timeout):
        with lock:
            if state["cm"] is None:
                state["abandoned"] = True
                raise RecorderError("打开麦克风超时（系统音频卡住了），请重启服务后重试")
    if state["error"] is not None:
        raise RecorderError("无法打开麦克风：请检查权限和设备") from state["error"]
    return state["cm"]


def close_stream(cm, timeout: float = STREAM_CLOSE_TIMEOUT_S) -> bool:
    """在守护线程 mr-stream-close 里关闭流，最多等 timeout 秒；按时关好返回 True。"""
    done = threading.Event()

    def work():
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
        done.set()

    threading.Thread(target=work, name="mr-stream-close", daemon=True).start()
    return done.wait(timeout)


def compress_m4a(wav: Path, m4a: Path, *, run=subprocess.run, stderr=None) -> bool:
    """用 afconvert 把 wav 压成 m4a；失败只警告并返回 False，绝不删 wav。"""
    stderr = stderr if stderr is not None else sys.stderr
    args = ["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)]
    try:
        result = run(args, check=True, capture_output=True)
        if getattr(result, "returncode", 0) != 0:
            raise subprocess.CalledProcessError(result.returncode, args)
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        try:
            Path(m4a).unlink()  # 清掉可能的半成品
        except OSError:
            pass
        print(f"警告：m4a 压缩失败，已保留 wav（{e}）", file=stderr)
        return False


def _existing_ancestor(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def record(
    out_root: Path,
    *,
    stream_factory,
    stop: threading.Event,
    now=datetime.now,
    on_level=None,
    on_samples=None,
    on_started=None,
    free_bytes_fn=common.free_bytes,
) -> Path:
    """录音直到 stop 被置位（或收到 Ctrl-C），返回会议目录。

    流打开成功后才创建会议目录，打不开不留空目录。
    流回调只把块放入队列，写盘与回调都在本函数的主循环里。
    on_started(meeting_dir) 在会议目录和 wav 建好后、开始消费音频前调用一次（实时转写靠它拿到目录）；
    它抛出的异常会被忽略，不影响录音。
    """
    out_root = Path(out_root)
    if free_bytes_fn(_existing_ancestor(out_root)) < common.MIN_FREE_BYTES:
        raise RecorderError("磁盘空间不足（至少需要 1 GB）")

    q: queue.Queue = queue.Queue()
    stream = open_stream(lambda: stream_factory(q.put))
    writer = None
    try:
        try:
            out_root.mkdir(parents=True, exist_ok=True)
            meeting_dir = common.unique_dir(out_root, common.meeting_dir_name(now()))
            meeting_dir.mkdir()
            writer = WavWriter(meeting_dir / "meeting.wav")
        except OSError as e:
            raise RecorderError(f"无法创建录音文件（{e}）") from e
        if on_started is not None:
            try:
                on_started(meeting_dir)
            except Exception:  # noqa: BLE001 附加功能出错不能影响录音
                pass

        def handle(chunk) -> None:
            chunk = np.asarray(chunk)
            if chunk.ndim > 1:
                chunk = chunk[:, 0]
            writer.write(chunk)
            if on_level is not None:
                x = chunk.astype(np.float64) / 32768.0
                on_level(float(np.sqrt(np.mean(x * x))) if x.size else 0.0)
            if on_samples is not None:
                on_samples(chunk)

        try:
            while not stop.is_set():
                try:
                    handle(q.get(timeout=0.1))
                except queue.Empty:
                    pass
        except KeyboardInterrupt:
            pass  # Ctrl-C 就是停止
        # 停止后把队列里剩余的块写完
        while True:
            try:
                handle(q.get_nowait())
            except queue.Empty:
                break
            except KeyboardInterrupt:
                break
        return meeting_dir
    except OSError as e:
        raise RecorderError(f"写入录音失败（{e}），已录部分保留在会议目录") from e
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass
        close_stream(stream)


def _real_stream_factory(callback):
    """真实麦克风输入流。仅在 main 装配时延迟导入 sounddevice。"""
    import sounddevice as sd

    def cb(indata, frames, time_info, status):
        callback(indata[:, 0].copy())

    return sd.InputStream(
        samplerate=common.SAMPLE_RATE, channels=1, dtype="int16", blocksize=1600, callback=cb
    )


def _prepare_segmenter(deps: dict, stderr):
    """为 --live 准备切句器；VAD 模型下载或加载失败就警告并返回 None（录音不受影响）。"""
    try:
        factory = deps.get("vad_factory")
        if factory is None:
            print("正在检查实时转写所需的 VAD 模型…", file=stderr)
            path = live.ensure_vad_model(
                deps.get("vad_model_dir", diarize.MODEL_DIR), download=deps.get("vad_download")
            )
            factory = live._sherpa_vad_factory(path)
        return live.Segmenter(factory)
    except Exception as e:  # noqa: BLE001
        print(f"警告：实时转写不可用，将只录音（{e}）", file=stderr)
        return None


def _finish_live(session, stderr) -> None:
    """录音结束后冲出尾句并等实时转写收尾（最多 10 秒）；出错只警告。"""
    if session is None:
        return
    try:
        session.finish()
    except Exception as e:  # noqa: BLE001
        print(f"警告：实时转写收尾失败（{e}）", file=stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="录制会议并转写")
    p.add_argument("--out", metavar="DIR", default=None, help="输出根目录（默认 ~/Meetings）")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--speakers", type=transcribe._positive_int, metavar="N", help="指定说话人数（整数 ≥1）")
    group.add_argument("--no-diarize", action="store_true", help="不区分说话人")
    p.add_argument("--model", default=transcribe.DEFAULT_MODEL, help="转写模型")
    p.add_argument("--live", action="store_true", help="边录边转写，结果显示在终端并写入 live.txt")
    p.add_argument("--live-model", metavar="REPO", default=transcribe.DEFAULT_MODEL, help="实时转写模型（默认同 --model 的默认值）")
    return p


def main(argv=None, *, deps=None) -> int:
    args = build_parser().parse_args(argv)
    deps = dict(deps or {})
    stdout = deps.get("stdout", sys.stdout)
    stderr = deps.get("stderr", sys.stderr)
    deps["stderr"] = stderr
    out_root = Path(args.out) if args.out else common.MEETINGS_ROOT
    stop = deps.get("stop") or threading.Event()
    watch = SilenceWatch()

    segmenter = _prepare_segmenter(deps, stderr) if args.live else None
    live_state = {"session": None, "warned": False}

    def on_started(meeting_dir: Path) -> None:
        """会议目录建好后启动实时转写；失败只警告，录音照常。"""
        if segmenter is None:
            return
        try:
            sink = live.LiveSink(meeting_dir / "live.txt", out=stdout, stderr=stderr)
            live_fn = deps.get("live_transcribe_fn")
            transcriber = live.LiveTranscriber(
                lambda audio: transcribe.transcribe_samples(audio, model=args.live_model, transcribe_fn=live_fn),
                sink,
                stderr=stderr,
            )
            live_state["session"] = live.LiveSession(segmenter, transcriber)
        except Exception as e:  # noqa: BLE001
            print(f"\n警告：实时转写启动失败，继续录音（{e}）", file=stderr)

    def on_level(rms: float) -> None:
        print(f"\r录音中 {level_bar(rms)}  Ctrl-C 停止", end="", file=stdout, flush=True)

    def on_samples(samples: np.ndarray) -> None:
        if watch.update(samples, time.monotonic()):
            print(f"\n警告：已连续 {int(SILENCE_WARN_S)} 秒几乎没有声音，请检查麦克风", file=stderr)
        session = live_state["session"]
        if session is not None:
            try:
                session.feed(samples)  # 只切句并入队，不等转写
            except Exception as e:  # noqa: BLE001 实时转写出错不能中断录音
                if not live_state["warned"]:
                    live_state["warned"] = True
                    print(f"\n警告：实时转写出错，已跳过（{e}）", file=stderr)

    print("开始录音，按 Ctrl-C 停止。", file=stdout)
    try:
        meeting_dir = record(
            out_root,
            stream_factory=deps.get("stream_factory", _real_stream_factory),
            stop=stop,
            now=deps.get("now", datetime.now),
            on_level=on_level,
            on_samples=on_samples,
            on_started=on_started,
            free_bytes_fn=deps.get("free_bytes_fn", common.free_bytes),
        )
    except RecorderError as e:
        _finish_live(live_state["session"], stderr)
        print(f"\n错误：{e}", file=stderr)
        return EXIT_RECORD_FAILED
    _finish_live(live_state["session"], stderr)

    print(f"\n录音已保存：{meeting_dir}", file=stdout)
    wav = meeting_dir / "meeting.wav"
    print("正在压缩 m4a…", file=stdout)
    compress_m4a(wav, meeting_dir / "meeting.m4a", run=deps.get("run", subprocess.run), stderr=stderr)

    targv = [str(meeting_dir)]
    if args.speakers is not None:
        targv += ["--speakers", str(args.speakers)]
    if args.no_diarize:
        targv.append("--no-diarize")
    targv += ["--model", args.model]
    print("正在转写…", file=stdout)
    try:
        code = transcribe.main(targv, deps=deps)
    except KeyboardInterrupt:
        code = 1
    if code != 0:
        print(f"录音已保存：{wav}，可稍后用 transcribe.py 重新转写", file=stderr)
        return EXIT_TRANSCRIBE_FAILED
    print(f"文字稿：{meeting_dir / 'transcript.md'}", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
