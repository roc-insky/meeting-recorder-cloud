"""声纹：登记文件读写、向量提取与按说话人聚类识别。

声纹文件放在 ~/.config/meeting-recorder/voiceprints/<名字>.json（权限 600）。
extractor 是一个可调用对象：接收 16 kHz、[-1,1] 的 float32 一维样本，返回向量；
真实实现 default_extractor 才会导入 sherpa_onnx，测试注入假 extractor，完全离线。
"""
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import common
import diarize

PROFILE_DIR = Path.home() / ".config/meeting-recorder/voiceprints"
DEFAULT_THRESHOLD = 0.5  # 未校准
MODEL_NAME = diarize.EMBEDDING_FILE
PROFILE_VERSION = 1
NAME_MAX_LEN = 20
RESERVED_PREFIX = "说话人"
MAX_CLUSTER_SECONDS = 60.0  # 每个聚类最多取这么长的音频算向量
MIN_CLUSTER_SECONDS = 1.0   # 聚类音频不足这么长就不参与识别
FRAME_S = 0.1               # 去静音时的分帧长度
MIN_TAIL_S = 1.0            # 登记时切块后，太短的尾巴丢弃

_NAME_RE = re.compile(r"[\w-]+")  # \w 含各国文字与数字、下划线，不含 / . 和空白


class VoiceprintError(ValueError):
    """声纹相关的可预期错误，消息可直接展示给用户。"""


def validate_name(name: str) -> str:
    """校验声纹名字：字母/数字/中日韩文字/下划线/连字符，1–20 字，不能以“说话人”开头。返回原名字。"""
    if not isinstance(name, str) or not name:
        raise VoiceprintError("名字不能为空")
    if len(name) > NAME_MAX_LEN:
        raise VoiceprintError(f"名字最多 {NAME_MAX_LEN} 个字")
    if not _NAME_RE.fullmatch(name):
        raise VoiceprintError("名字只能含字母、数字、文字、下划线和连字符")
    if name.startswith(RESERVED_PREFIX):
        raise VoiceprintError(f"名字不能以“{RESERVED_PREFIX}”开头")
    return name


def profile_path(name: str) -> Path:
    """名字对应的声纹文件路径（会校验名字，防止路径穿越）。"""
    return PROFILE_DIR / (validate_name(name) + ".json")


def _unit(vector) -> np.ndarray:
    """转成 float64 并做 L2 归一化；零向量或含非有限值抛 VoiceprintError。"""
    v = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(v))
    if v.size == 0 or not math.isfinite(norm) or norm == 0.0:
        raise VoiceprintError("无法提取有效的声纹向量")
    return v / norm


def existing_profile_path(name: str):
    """已登记的同名声纹文件（按大小写不敏感匹配，返回磁盘上的真实拼写）；没有返回 None。

    macOS 默认文件系统不区分大小写：Bob 与 bob 会指向同一个文件，所以“是否已存在”必须忽略大小写，
    并拿到真实拼写，替换时才能让文件名与里面的名字保持一致。
    """
    want = profile_path(name).name
    try:
        names = os.listdir(PROFILE_DIR)
    except OSError:
        return None
    if want in names:
        return PROFILE_DIR / want
    folded = want.casefold()
    for n in sorted(names):
        if n.casefold() == folded:
            return PROFILE_DIR / n
    return None


def save_profile(name, vector, *, seconds, chunks, model, replace=False) -> Path:
    """保存声纹文件（原子写，权限 600）。同名（忽略大小写）且 replace=False 抛 VoiceprintError。

    替换时若磁盘上的拼写与新名字只差大小写（Bob→bob），先把旧文件挪开再写新文件，
    保证“文件名主干 == 里面的名字”，失败则把旧文件原样挪回。
    """
    path = profile_path(name)
    unit = _unit(vector)
    existing = existing_profile_path(name)
    if existing is not None and not replace:
        raise VoiceprintError(f"声纹“{name}”已存在，加 --replace 可覆盖")
    if not PROFILE_DIR.exists():
        PROFILE_DIR.mkdir(parents=True)
        os.chmod(PROFILE_DIR, 0o700)
    data = {
        "version": PROFILE_VERSION,
        "name": name,
        "model": model,
        "created": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(float(seconds), 1),
        "chunks": int(chunks),
        "vector": unit.tolist(),
    }
    payload = json.dumps(data, ensure_ascii=False)
    if existing is not None and existing.name != path.name:
        aside = existing.with_name(existing.name + ".replacing")
        os.replace(existing, aside)             # 旧拼写先挪开（不删除），新文件才能以新拼写落盘
        try:
            common.atomic_write_text(path, payload)
        except BaseException:
            os.replace(aside, existing)
            raise
        os.chmod(path, 0o600)
        try:
            aside.unlink()
        except OSError:
            pass
        return path
    common.atomic_write_text(path, payload)
    os.chmod(path, 0o600)
    return path


def _read_profile(path: Path) -> dict:
    """读取并校验一个声纹文件，格式不对抛 ValueError。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != PROFILE_VERSION:
        raise ValueError("版本不支持")
    name = validate_name(data.get("name"))
    if name != Path(path).stem:
        raise ValueError("名字与文件名不符")
    vec = data.get("vector")
    if not isinstance(vec, list) or not vec or not all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vec
    ):
        raise ValueError("向量无效")
    data["vector"] = _unit(vec)
    return data


def _iter_profiles(stderr=None):
    """按文件名顺序产出读成功的声纹字典；坏文件警告后跳过。"""
    if not PROFILE_DIR.is_dir():
        return
    for path in sorted(PROFILE_DIR.glob("*.json")):
        try:
            yield _read_profile(path)
        except (OSError, ValueError) as e:  # JSON 解析错误也是 ValueError
            print(f"警告：声纹文件损坏，已跳过：{path.name}（{e}）", file=stderr or sys.stderr)


def load_profiles(*, stderr=None) -> dict:
    """读取全部声纹，返回 {名字: 单位向量}；坏文件跳过并警告。目录不存在返回空字典。"""
    return {p["name"]: p["vector"] for p in _iter_profiles(stderr)}


def list_profiles(*, stderr=None) -> list:
    """声纹摘要列表（不含向量）。"""
    keys = ("name", "model", "created", "seconds", "chunks")
    return [{k: p.get(k) for k in keys} for p in _iter_profiles(stderr)]


def delete_profile(name) -> bool:
    """删除声纹，真的删了返回 True；名字不合法或不存在返回 False。"""
    try:
        path = profile_path(name)
    except VoiceprintError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _to_float32(samples: np.ndarray) -> np.ndarray:
    """整型样本按 int16 归一化到 [-1,1]；浮点样本原样转 float32。"""
    samples = np.asarray(samples)
    if samples.dtype.kind in "iu":
        return samples.astype(np.float32) / 32768.0
    return samples.astype(np.float32, copy=False)


def embed(samples: np.ndarray, *, extractor) -> np.ndarray:
    """提取一段样本的声纹向量，返回 L2 归一化结果。"""
    return _unit(extractor(_to_float32(samples)))


def enroll_vector(samples, *, extractor, chunk_s: float = 3.0, min_seconds: float = 10.0):
    """去静音后切块取向量再平均并归一化，返回 (向量, 块数, 有效语音秒数)。

    有效语音不足 min_seconds 抛 VoiceprintError。samples 为 16 kHz int16。
    """
    rate = common.SAMPLE_RATE
    frame = int(rate * FRAME_S)
    kept = [
        samples[i:i + frame]
        for i in range(0, samples.size, frame)
        if not common.is_silent(samples[i:i + frame])
    ]
    speech = np.concatenate(kept) if kept else samples[:0]
    seconds = speech.size / rate
    if seconds < min_seconds:
        raise VoiceprintError("有效语音太短，请再录一次")
    size = int(rate * chunk_s)
    pieces = [speech[i:i + size] for i in range(0, speech.size, size)]
    if len(pieces) > 1 and pieces[-1].size < rate * MIN_TAIL_S:
        pieces.pop()
    vectors = [embed(p, extractor=extractor) for p in pieces]
    return _unit(np.mean(vectors, axis=0)), len(vectors), seconds


def cosine(a, b) -> float:
    """余弦相似度；任一为零向量或维度不同返回 0.0。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def _cluster_audio(turns, samples: np.ndarray) -> dict:
    """按说话人拼接各自音频（按时间顺序，每人最多 MAX_CLUSTER_SECONDS 秒）。"""
    rate = common.SAMPLE_RATE
    limit = int(MAX_CLUSTER_SECONDS * rate)
    parts, total = {}, {}
    for start, end, spk in sorted(turns, key=lambda t: t[0]):
        room = limit - total.get(spk, 0)
        if room <= 0:
            continue
        a = max(0, int(start * rate))
        b = min(samples.size, int(end * rate))
        piece = samples[a:b][:room]
        if piece.size:
            parts.setdefault(spk, []).append(piece)
            total[spk] = total.get(spk, 0) + piece.size
    return {spk: np.concatenate(p) for spk, p in parts.items() if total[spk] >= MIN_CLUSTER_SECONDS * rate}


def label_turns(turns, samples, *, profiles, extractor, threshold: float = DEFAULT_THRESHOLD):
    """把说话人聚类与已登记声纹比对，达阈值的整个聚类改名为声纹名字。

    每个聚类最多一个名字，同一个名字只给得分最高的聚类；无声纹时原样返回。
    """
    if not profiles or not turns:
        return list(turns)
    candidates = []
    for spk, audio in _cluster_audio(turns, samples).items():
        try:
            vec = embed(audio, extractor=extractor)
        except VoiceprintError:
            continue  # 这个聚类提不出声纹，保持原标签
        for name, prof in profiles.items():
            score = cosine(vec, prof)
            if score >= threshold:
                candidates.append((score, spk, name))
    # 得分从高到低贪心分配：聚类和名字都只用一次
    order = {}
    for _s, _e, spk in turns:
        order.setdefault(spk, len(order))
    candidates.sort(key=lambda c: (-c[0], order[c[1]], c[2]))
    rename, used = {}, set()
    for _score, spk, name in candidates:
        if spk in rename or name in used:
            continue
        rename[spk] = name
        used.add(name)
    return [(s, e, rename.get(spk, spk)) for s, e, spk in turns]


def default_extractor():
    """真实 extractor：补齐模型后加载 sherpa-onnx 的说话人嵌入模型（延迟导入，仅在需要时调用）。"""
    import sherpa_onnx

    diarize.ensure_models()
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(diarize.MODEL_DIR / diarize.EMBEDDING_FILE), num_threads=2
    )
    if not config.validate():
        raise VoiceprintError("声纹模型不可用，请检查模型文件")
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def extract(samples: np.ndarray) -> np.ndarray:
        stream = ex.create_stream()
        stream.accept_waveform(common.SAMPLE_RATE, samples)
        stream.input_finished()
        if not ex.is_ready(stream):
            raise VoiceprintError("音频太短，无法提取声纹")
        return np.asarray(ex.compute(stream), dtype=np.float32)

    return extract
