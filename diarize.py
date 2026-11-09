"""说话人区分：管理 sherpa-onnx 模型缓存，并对 16 kHz 样本做说话人分段。

模型放在 ~/.cache/meeting-recorder/models；下载先写 .part 再改名，保证目标文件要么完整要么不存在。
sherpa_onnx 只在真正推理时才导入，测试通过注入 download / runner 完全离线。
"""
import os
import tarfile
import urllib.request
from pathlib import Path

import numpy as np

MODEL_DIR = Path.home() / ".cache/meeting-recorder/models"
SEGMENTATION_FILE = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMBEDDING_FILE = "3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx"

_RELEASE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SEGMENTATION_ARCHIVE_URL = _RELEASE_BASE + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_URL = _RELEASE_BASE + "speaker-recongition-models/" + EMBEDDING_FILE

AUTO_THRESHOLD = 0.5  # 未指定人数时的聚类阈值
SAMPLE_RATE = 16000


def models_present(cache_dir: Path) -> list[str]:
    """返回缺失的模型相对路径（0 字节也算缺失）。只读，不联网、不创建目录。"""
    cache_dir = Path(cache_dir)
    missing = []
    for rel in (SEGMENTATION_FILE, EMBEDDING_FILE):
        try:
            ok = (cache_dir / rel).stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            missing.append(rel)
    return missing


DOWNLOAD_TIMEOUT_S = 15.0  # 连接与每次读取的超时，网络卡住时不会无限等
_DOWNLOAD_CHUNK = 1 << 20


def _default_download(url: str, dest: Path) -> None:
    """真实下载：urllib 分块写到 dest（调用方传入 .part 路径）。

    下载为空，或响应带 Content-Length 而实际字节数不足时抛 OSError（调用方负责清理 .part）。
    """
    written = 0
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as out:
        expected = _expected_length(resp)
        while chunk := resp.read(_DOWNLOAD_CHUNK):
            out.write(chunk)
            written += len(chunk)
    if written == 0:
        raise OSError("下载内容为空")
    if expected is not None and written < expected:
        raise OSError(f"下载不完整：期望 {expected} 字节，实际 {written} 字节")


def _expected_length(resp):
    """读响应的 Content-Length；缺失或不是非负整数时返回 None（不做长度校验）。"""
    try:
        value = int(resp.headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _fetch_embedding(dest: Path, download) -> None:
    part = dest.with_name(dest.name + ".part")
    try:
        download(EMBEDDING_URL, part)
        os.replace(part, dest)
    finally:
        _discard(part)


def _fetch_segmentation(dest: Path, download) -> None:
    """下载分割模型压缩包，只抽取 model.onnx，同样经 .part 改名落盘。"""
    archive = dest.parent.parent / "segmentation.tar.bz2"
    archive_part = archive.with_name(archive.name + ".part")
    model_part = dest.with_name(dest.name + ".part")
    try:
        download(SEGMENTATION_ARCHIVE_URL, archive_part)
        os.replace(archive_part, archive)
        with tarfile.open(archive) as tar:
            member = tar.extractfile(SEGMENTATION_FILE)
            if member is None:
                raise OSError("分割模型压缩包内缺少 model.onnx")
            with open(model_part, "wb") as out:
                while chunk := member.read(1 << 20):
                    out.write(chunk)
        os.replace(model_part, dest)
    finally:
        _discard(archive_part)
        _discard(archive)
        _discard(model_part)


def ensure_models(cache_dir: Path = MODEL_DIR, *, download=None) -> tuple:
    """补齐缺失的模型，返回（分割模型, 嵌入模型）路径。下载失败直接抛异常，由调用方降级。"""
    cache_dir = Path(cache_dir)
    download = download or _default_download
    missing = set(models_present(cache_dir))
    seg, emb = cache_dir / SEGMENTATION_FILE, cache_dir / EMBEDDING_FILE
    if missing:
        seg.parent.mkdir(parents=True, exist_ok=True)
    if SEGMENTATION_FILE in missing:
        _fetch_segmentation(seg, download)
    if EMBEDDING_FILE in missing:
        _fetch_embedding(emb, download)
    return seg, emb


def _sherpa_runner(audio: np.ndarray, num_clusters: int, threshold: float) -> list:
    """真实推理：延迟导入 sherpa_onnx，返回 [(起, 止, 说话人id)]。"""
    import sherpa_onnx

    seg, emb = MODEL_DIR / SEGMENTATION_FILE, MODEL_DIR / EMBEDDING_FILE
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
            num_threads=2,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=2),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("说话人区分配置无效，请检查模型文件")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    result = sd.process(audio).sort_by_start_time()
    return [(float(s.start), float(s.end), int(s.speaker)) for s in result]


def diarize(samples: np.ndarray, *, num_speakers=None, runner=None) -> list:
    """对 16 kHz 单声道样本做说话人分段，返回 [(起, 止, 说话人)]。

    说话人 id 按首次出现顺序重编号为 0,1,2…；num_speakers 为 None 时自动聚类。
    """
    if samples.size == 0:
        return []
    if samples.dtype.kind in "iu":
        audio = samples.astype(np.float32) / 32768.0
    else:
        audio = samples.astype(np.float32, copy=False)
    runner = runner or _sherpa_runner
    num_clusters = -1 if num_speakers is None else int(num_speakers)
    raw = runner(audio, num_clusters, AUTO_THRESHOLD)
    mapping = {}
    turns = []
    for start, end, spk in sorted(raw, key=lambda t: t[0]):
        if spk not in mapping:
            mapping[spk] = len(mapping)
        turns.append((start, end, mapping[spk]))
    return turns
