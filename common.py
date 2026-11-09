"""公共常量与工具函数：目录命名、时间格式、静音判断、原子写、容错读 wav。"""
import os
import shutil
import struct
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
MEETINGS_ROOT = Path.home() / "Meetings"
MIN_FREE_BYTES = 1024 ** 3  # 录音前至少要有 1 GiB 可用空间


def meeting_dir_name(now: datetime) -> str:
    """会议目录名，如 2026-11-09_1030。"""
    return now.strftime("%Y-%m-%d_%H%M")


def unique_dir(root: Path, name: str) -> Path:
    """返回 root 下不重名的目录路径；重名依次加 _2、_3。不创建目录。"""
    candidate = root / name
    n = 2
    while candidate.exists():
        candidate = root / f"{name}_{n}"
        n += 1
    return candidate


def format_ts(seconds: float) -> str:
    """秒数转 HH:MM:SS，向下取整，超过 24 小时继续累加小时。"""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def free_bytes(path: Path) -> int:
    """path 所在磁盘的可用字节数。"""
    return shutil.disk_usage(path).free


def is_silent(samples: np.ndarray, *, rms_threshold: float = 0.003) -> bool:
    """int16 样本的 RMS（归一化到 [-1,1]）低于阈值视为静音；空数组为静音。"""
    if samples.size == 0:
        return True
    x = samples.astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(x * x))) < rms_threshold


def atomic_write_text(path: Path, text: str) -> None:
    """同目录写临时文件再 os.replace；失败时清理临时文件，目标保持旧内容。"""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class AudioError(ValueError):
    """音频文件读取失败的基类（仍是 ValueError，旧的 except ValueError 调用方不受影响）。"""


class EmptyAudioError(AudioError):
    """文件结构合法，但没有任何音频样本（例如录音刚开始就停止）。"""


class MalformedAudioError(AudioError):
    """文件不是合法 wav（RIFF 头、块结构或格式字段损坏）；不能当作“没有语音”处理。"""


def read_wav_tolerant(path: Path) -> tuple[int, np.ndarray]:
    """容错读取 16 位 wav，返回 (采样率, int16 样本)。

    自己解析 RIFF 块。录音中断时头里的 data 长度可能为 0 或偏大，
    此时按文件实际剩余字节读取。多声道只取第一声道。
    结构损坏（非 RIFF、缺 fmt/data 块、块长度异常、格式字段非法）抛 MalformedAudioError；
    结构合法但样本数为 0 抛 EmptyAudioError。两者都是 ValueError 子类。
    """
    raw = Path(path).read_bytes()
    bad = MalformedAudioError("录音文件已损坏（不是有效的 wav 文件）")
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise bad
    rate = None
    channels = 1
    pos = 12
    data = None
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= len(raw):
            _fmt, channels, rate, _br, _ba, bits = struct.unpack("<HHIIHH", raw[body:body + 16])
            if bits != 16:
                raise MalformedAudioError("仅支持 16 位 wav，该录音文件格式不受支持")
            if channels < 1 or rate < 1:
                raise bad
        elif chunk_id == b"data":
            avail = len(raw) - body
            if size == 0 or size > avail:
                size = avail
            data = raw[body:body + size]
            break
        pos = body + size + (size & 1)
    if rate is None or data is None:
        raise bad
    data = data[: len(data) // 2 * 2]
    samples = np.frombuffer(data, dtype="<i2").astype(np.int16)
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels)[:, 0].copy()
    if samples.size == 0:
        raise EmptyAudioError("录音文件为空（没有音频样本）")
    return rate, samples
