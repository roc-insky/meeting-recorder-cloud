"""实时转写：静音切句（VAD）→ 后台逐句转写 → 终端与 live.txt。

整条路径都不能拖慢录音：submit 非阻塞，工作线程是守护线程，任何错误只警告、不抛出。
sherpa_onnx 只在 _sherpa_vad_factory 里延迟导入；测试用假 VAD 和假转写函数，完全离线。
"""
import collections
import os
import sys
import threading
from pathlib import Path

import numpy as np

import common
import diarize
import transcribe

VAD_FILE = "silero_vad.onnx"
VAD_MIN_BYTES = 100 * 1024  # 官方文件约 644 KB；过小说明损坏，加载会让 sherpa 直接退出进程
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + VAD_FILE

VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_S = 0.6      # 静音多久算一句话说完
VAD_MIN_SPEECH_S = 0.25      # 短于这个的声音当噪声
VAD_MAX_SPEECH_S = 15.0      # 一直不停顿时强制切句，避免延迟过大
VAD_BUFFER_S = 30.0          # 检测器内部缓冲秒数
QUEUE_MAX = 5
STOP_TIMEOUT_S = 10.0


def models_present(cache_dir: Path) -> list:
    """返回缺失的模型文件名（小于 VAD_MIN_BYTES 视为缺失或损坏）。只读，不联网、不创建目录。"""
    try:
        ok = (Path(cache_dir) / VAD_FILE).stat().st_size >= VAD_MIN_BYTES
    except OSError:
        ok = False
    return [] if ok else [VAD_FILE]


def ensure_vad_model(cache_dir: Path = diarize.MODEL_DIR, *, download=None) -> Path:
    """确保 VAD 模型存在，返回路径。先写 .part 再改名；下载失败直接抛异常，由调用方降级。"""
    cache_dir = Path(cache_dir)
    dest = cache_dir / VAD_FILE
    if not models_present(cache_dir):
        return dest
    download = download or diarize._default_download
    cache_dir.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        download(VAD_URL, part)
        if part.stat().st_size < VAD_MIN_BYTES:
            raise OSError("VAD 模型下载不完整")
        os.replace(part, dest)
    finally:
        diarize._discard(part)
    return dest


def _sherpa_vad_factory(model_path: Path):
    """真实 VAD 工厂（sherpa-onnx 2.0.1 的 API）：返回无参函数，调用后得到 VoiceActivityDetector。"""

    def make():
        import sherpa_onnx

        silero = sherpa_onnx.SileroVadModelConfig(
            model=str(model_path),
            threshold=VAD_THRESHOLD,
            min_silence_duration=VAD_MIN_SILENCE_S,
            min_speech_duration=VAD_MIN_SPEECH_S,
            max_speech_duration=VAD_MAX_SPEECH_S,
        )
        config = sherpa_onnx.VadModelConfig(silero_vad=silero, sample_rate=common.SAMPLE_RATE)
        if not config.validate():
            raise RuntimeError("VAD 配置无效，请检查模型文件")
        return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=VAD_BUFFER_S)

    return make


def _is_cjk(ch: str) -> bool:
    return "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"


def join_texts(texts) -> str:
    """拼接同一句的多个片段：任一侧边界字符是中日韩字符或全角标点就不加空格，否则加一个空格。"""
    out = ""
    for t in texts:
        if not t:
            continue
        if out and not (_is_cjk(out[-1]) or _is_cjk(t[0])):
            out += " "
        out += t
    return out


def _warn(stream, message: str) -> None:
    """写警告到 stream（默认 stderr），自身出错也不外抛。"""
    try:
        print(message, file=stream if stream is not None else sys.stderr)
    except Exception:
        pass


class Segmenter:
    """把连续音频按停顿切成句子。vad_factory() 返回带 accept_waveform/empty/front/pop/flush 的检测器。"""

    def __init__(self, vad_factory, sample_rate: int = common.SAMPLE_RATE):
        self._vad = vad_factory()
        self._rate = sample_rate

    def _drain(self) -> list:
        out = []
        while not self._vad.empty():
            seg = self._vad.front
            out.append((seg.start / self._rate, np.asarray(seg.samples, dtype=np.float32)))
            self._vad.pop()
        return out

    def feed(self, samples: np.ndarray) -> list:
        """送入一块音频（int16 或 float），返回已说完的句子 [(起始秒, float32 音频)]。"""
        samples = np.asarray(samples)
        if samples.dtype.kind in "iu":
            samples = samples.astype(np.float32) / 32768.0
        else:
            samples = samples.astype(np.float32, copy=False)
        self._vad.accept_waveform(samples)
        return self._drain()

    def flush(self) -> list:
        """录音结束时冲出还没说完的尾句。"""
        self._vad.flush()
        return self._drain()


class LiveSink:
    """把 [HH:MM:SS] 文字 输出到终端和 live.txt；任何错误只警告一次，不外抛。"""

    def __init__(self, live_path: Path, *, out=None, stderr=None):
        self.live_path = Path(live_path)
        self._out = out
        self._stderr = stderr
        self._file_warned = False
        self._out_warned = False

    def write(self, start_s: float, text: str) -> None:
        line = f"[{common.format_ts(start_s)}] {text}"
        try:
            out = self._out if self._out is not None else sys.stdout
            print("\n" + line, file=out, flush=True)  # 换行，避免盖住录音电平条
        except Exception as e:
            if not self._out_warned:
                self._out_warned = True
                _warn(self._stderr, f"警告：实时转写无法输出到终端（{e}）")
        try:
            with open(self.live_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            if not self._file_warned:
                self._file_warned = True
                _warn(self._stderr, f"警告：实时转写无法写入 live.txt（{e}）")


class LiveTranscriber:
    """后台逐句转写。transcribe_fn(float32 样本) -> 片段列表（同 transcribe.transcribe_samples）。

    submit 永不阻塞：队列满时丢掉最旧的一句，整场只警告一次。
    """

    def __init__(self, transcribe_fn, sink, *, queue_max: int = QUEUE_MAX, stderr=None):
        self._fn = transcribe_fn
        self._sink = sink
        self._max = queue_max
        self._stderr = stderr
        self._items = collections.deque()
        self._cond = threading.Condition()
        self._closing = False
        self._abandoned = False
        self._drop_warned = False
        self._thread = threading.Thread(target=self._run, name="mr-live-worker", daemon=True)
        self._thread.start()

    def submit(self, start_s: float, samples: np.ndarray) -> None:
        warn = False
        with self._cond:
            if self._closing:
                return
            if len(self._items) >= self._max:
                self._items.popleft()
                warn = not self._drop_warned
                self._drop_warned = True
            self._items.append((start_s, samples))
            self._cond.notify()
        if warn:
            _warn(self._stderr, "警告：实时转写跟不上说话速度，已丢弃较早的句子（录音不受影响）")

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closing:
                    self._cond.wait()
                if not self._items:
                    return
                start_s, samples = self._items.popleft()
            try:
                segs = transcribe.filter_segments(self._fn(samples))
                text = join_texts(s["text"] for s in segs)
                if text and not self._abandoned:
                    self._sink.write(start_s, text)
            except Exception as e:  # noqa: BLE001 实时转写出错绝不能影响录音
                _warn(self._stderr, f"警告：实时转写出错，已跳过一句（{e}）")
            if self._abandoned:
                return

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """处理完队列里剩下的句子后停止；最多等 timeout 秒。按时结束返回 True。"""
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._abandoned = True  # 转写还卡着：放弃剩余内容，线程是守护线程不会拖住退出
            with self._cond:
                self._items.clear()
            _warn(self._stderr, f"警告：实时转写在 {timeout:g} 秒内未完成，已放弃剩余内容")
            return False
        return True


class LiveSession:
    """把 Segmenter 和 LiveTranscriber 串起来，供 record 的 on_samples 回调使用。"""

    def __init__(self, segmenter: Segmenter, transcriber: LiveTranscriber):
        self.segmenter = segmenter
        self.transcriber = transcriber

    def feed(self, samples: np.ndarray) -> None:
        for start_s, audio in self.segmenter.feed(samples):
            self.transcriber.submit(start_s, audio)

    def finish(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """冲出尾句并等转写完成（最多 timeout 秒）。"""
        try:
            for start_s, audio in self.segmenter.flush():
                self.transcriber.submit(start_s, audio)
        except Exception as e:  # noqa: BLE001
            _warn(self.transcriber._stderr, f"警告：实时转写冲出尾句失败（{e}）")
        return self.transcriber.stop(timeout)
