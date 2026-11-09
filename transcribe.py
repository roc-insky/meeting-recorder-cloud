"""转写核心：调用 mlx-whisper 转写 wav，过滤、对齐说话人并渲染为 Markdown 文字稿。

deps 字典（均可选，便于测试与后续扩展，新增键不影响旧调用）：
  transcribe_fn(audio, **kwargs) -> {"segments": [...]}   替换 mlx_whisper.transcribe
  diarize_fn(samples_int16, rate, speakers) -> list[Turn]  说话人区分；缺省用 _default_diarize_fn（延迟导入 diarize，必要时下载模型）
  extractor(samples_float32) -> 向量                       声纹提取；缺省用 voiceprint.default_extractor（仅在已登记声纹时才延迟加载）
  stderr                                                   警告输出流，默认 sys.stderr
  started                                                  会议开始时间（datetime），默认由 wav 修改时间推算
  config_path                                              config.toml 路径，默认项目目录下的 config.toml（只读 [voiceprint] 阈值）
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

import common
import voiceprint

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"   # 不存在也能跑，[voiceprint] 缺省用内置阈值
NO_SPEECH_THRESHOLD = 0.6
EMPTY_NOTE = "（未识别到语音）"

# Segment：dict，键 start、end、text，可有 no_speech_prob
# Turn：(起, 止, 说话人)，说话人为 int 或 str
Segment = dict
Turn = tuple


def _real_transcribe(audio, **kwargs):
    """真实转写：延迟导入 mlx_whisper，避免测试和导入时加载模型。"""
    import mlx_whisper

    return mlx_whisper.transcribe(audio, **kwargs)


def transcribe_samples(samples: np.ndarray, *, model: str = DEFAULT_MODEL, transcribe_fn=None) -> list:
    """转写 16 kHz 单声道样本，返回原始片段列表。

    固定中文、不依赖上一段文本，且不传 initial_prompt。
    """
    if samples.dtype.kind in "iu":
        audio = samples.astype(np.float32) / 32768.0
    else:
        audio = samples.astype(np.float32, copy=False)
    fn = transcribe_fn or _real_transcribe
    result = fn(
        audio,
        path_or_hf_repo=model,
        language="zh",
        condition_on_previous_text=False,
    )
    return list(result.get("segments", []))


def filter_segments(segs: list) -> list:
    """去掉空白文本和 no_speech_prob 过高的片段，文本去首尾空白。"""
    out = []
    for seg in segs:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if seg.get("no_speech_prob", 0.0) > NO_SPEECH_THRESHOLD:
            continue
        out.append({**seg, "text": text})
    return out


def speaker_label(spk) -> str:
    """0→说话人A … 25→说话人Z，26 起用数字（26→说话人27）；字符串原样返回。"""
    if isinstance(spk, str):
        return spk
    if spk < 26:
        return "说话人" + chr(ord("A") + spk)
    return f"说话人{spk + 1}"


def assign_speakers(segs: list, turns: list) -> list:
    """每个片段归属与其时间重叠最长的说话人段；没有任何重叠则为 None。"""
    result = []
    for seg in segs:
        best, best_overlap = None, 0.0
        for start, end, spk in turns:
            overlap = min(seg["end"], end) - max(seg["start"], start)
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        result.append((seg, best))
    return result


def render(segs: list, *, started: datetime, duration: float, speakers=None, assigned=None, scores=None) -> str:
    """渲染文字稿。speakers 非 None 时表头加说话人行；assigned 给出时按其附加说话人标签。

    scores 非空时在说话人行之后再加一行「- 声纹匹配：」；为空时输出与旧版逐字相同。
    """
    lines = [
        "# 会议文字稿",
        f"- 开始时间：{started.strftime('%Y-%m-%d %H:%M')}",
        f"- 时长：{common.format_ts(duration)}",
    ]
    if speakers is not None:
        lines.append("- 说话人：" + "、".join(speakers))
    if scores:
        lines.append("- 声纹匹配：" + "、".join(f"{name} {score:.2f}" for name, score in scores.items()))
    lines.append("")
    pairs = assigned if assigned is not None else [(s, None) for s in segs]
    if not pairs:
        lines.append(EMPTY_NOTE)
    for seg, spk in pairs:
        prefix = f"[{common.format_ts(seg['start'])}] "
        if spk is not None:
            prefix += speaker_label(spk) + ": "
        lines.append(prefix + seg["text"])
    return "\n".join(lines) + "\n"


def _resample(samples: np.ndarray, rate: int) -> np.ndarray:
    """非 16 kHz 的音频线性插值到 16 kHz（正常录音已是 16 kHz，仅兜底）。"""
    if rate == common.SAMPLE_RATE:
        return samples
    n = int(round(samples.size * common.SAMPLE_RATE / rate))
    x = np.linspace(0, samples.size - 1, num=n)
    return np.interp(x, np.arange(samples.size), samples.astype(np.float64)).astype(np.int16)


class _ModelDownloadError(Exception):
    """说话人模型下载失败（用于给出更具体的警告）。"""


def _default_diarize_fn(samples, rate, speakers):
    """默认说话人区分：延迟导入 diarize（避免未使用时加载 sherpa-onnx），缺模型先下载。"""
    import diarize

    try:
        diarize.ensure_models()
    except Exception as e:
        raise _ModelDownloadError(e) from e
    return diarize.diarize(samples, num_speakers=speakers)


def _speaker_sort_key(spk):
    return (isinstance(spk, str), spk)


def _apply_voiceprint(turns, samples16k, thresholds, deps, stderr):
    """用已登记声纹给说话人聚类分档改名；任何失败只警告并原样返回 turns。

    没有登记声纹时直接返回，不创建 extractor（不加载嵌入模型）。
    """
    try:
        profiles = voiceprint.load_profiles(stderr=stderr)
        if not profiles:
            return turns, {}
        extractor = deps.get("extractor") or voiceprint.default_extractor()
        return voiceprint.label_turns_with_scores(
            turns, samples16k, profiles=profiles, extractor=extractor, thresholds=thresholds
        )
    except Exception as e:  # 声纹失败不影响说话人区分结果
        print(f"警告：声纹识别失败，已保留说话人A/B 标签（{e}）", file=stderr)
        return turns, {}


def transcribe_wav(
    wav: Path,
    *,
    model: str = DEFAULT_MODEL,
    speakers=None,
    diarize: bool = True,
    voice: bool = True,
    voice_threshold: float | None = None,
    thresholds: voiceprint.Thresholds | None = None,
    deps=None,
) -> str:
    """转写 wav 并返回文字稿文本。

    零长度或静音不调用模型；说话人区分出错只警告并退回无标签文字稿；
    声纹识别出错只警告并保留说话人A/B 标签。
    thresholds 优先；否则 voice_threshold 作单阈值（无灰区）；都没给用内置默认档。
    """
    deps = deps or {}
    stderr = deps.get("stderr", sys.stderr)
    wav = Path(wav)
    started = deps.get("started")

    try:
        rate, samples = common.read_wav_tolerant(wav)
    except common.EmptyAudioError:
        # 仅“确实没有样本”才算没有语音；损坏的 wav（MalformedAudioError）继续向上抛，
        # 由调用方当作失败处理，绝不能用空文字稿覆盖已有的文字稿
        return render([], started=started or _guess_start(wav, 0.0), duration=0.0)

    duration = samples.size / rate
    started = started or _guess_start(wav, duration)
    if common.is_silent(samples):
        return render([], started=started, duration=duration)

    samples16k = _resample(samples, rate)
    segs = filter_segments(
        transcribe_samples(samples16k, model=model, transcribe_fn=deps.get("transcribe_fn"))
    )

    diarize_fn = deps.get("diarize_fn") or _default_diarize_fn
    if diarize and segs:
        turns = None
        try:
            turns = diarize_fn(samples16k, common.SAMPLE_RATE, speakers)
        except _ModelDownloadError as e:
            print(f"警告：说话人模型下载失败，已输出无标签文字稿（{e.__cause__}）", file=stderr)
        except Exception as e:  # 区分失败不影响文字稿
            print(f"警告：说话人区分失败，已输出无标签文字稿（{e}）", file=stderr)
        voice_scores = {}
        if turns and voice:
            # 声纹识别用独立的 try/except（在 _apply_voiceprint 内），失败不影响区分结果
            if thresholds is not None:
                th = thresholds
            elif voice_threshold is not None:
                th = voiceprint.Thresholds(voice_threshold, voice_threshold)  # 旧单阈值语义，无灰区
            else:
                th = voiceprint.Thresholds(voiceprint.DEFAULT_HIGH, voiceprint.DEFAULT_LOW)
            turns, voice_scores = _apply_voiceprint(turns, samples16k, th, deps, stderr)
        try:
            assigned = assign_speakers(segs, turns or [])
            found = sorted({spk for _s, spk in assigned if spk is not None}, key=_speaker_sort_key)
            if found:
                labels = [speaker_label(s) for s in found]
                return render(
                    segs,
                    started=started,
                    duration=duration,
                    speakers=labels,
                    assigned=assigned,
                    scores={k: v for k, v in voice_scores.items() if k in labels},
                )
        except Exception as e:
            print(f"警告：说话人区分失败，已输出无标签文字稿（{e}）", file=stderr)
    return render(segs, started=started, duration=duration)


def _guess_start(wav: Path, duration: float) -> datetime:
    """用 wav 的修改时间（录音结束）减时长推算开始时间。"""
    try:
        end = datetime.fromtimestamp(wav.stat().st_mtime)
    except OSError:
        end = datetime.now()
    return end - timedelta(seconds=duration)


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("须为不小于 1 的整数")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="转写会议录音为文字稿")
    p.add_argument("path", metavar="PATH", help="wav 文件或会议目录")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--speakers", type=_positive_int, metavar="N", help="指定说话人数（整数 ≥1）")
    group.add_argument("--no-diarize", action="store_true", help="不区分说话人")
    p.add_argument("--model", default=DEFAULT_MODEL, help="转写模型")
    p.add_argument("--no-voiceprint", action="store_true", help="不做声纹识别")
    p.add_argument("--voice-threshold", type=float, metavar="X", default=None,
                   help="声纹高阈值（覆盖 config.toml 的 [voiceprint] high；未校准）")
    return p


def main(argv=None, *, deps=None) -> int:
    args = build_parser().parse_args(argv)
    deps = deps or {}
    stderr = deps.get("stderr", sys.stderr)
    # 阈值问题只警告，不中断转写
    thresholds = voiceprint.load_thresholds(deps.get("config_path", CONFIG_PATH), cli_high=args.voice_threshold)
    if thresholds.warning:
        print(f"警告：{thresholds.warning}", file=stderr)
    path = Path(args.path)
    wav = path / "meeting.wav" if path.is_dir() else path
    if not wav.is_file():
        print(f"错误：找不到录音文件：{wav}", file=stderr)
        return 1
    try:
        text = transcribe_wav(
            wav,
            model=args.model,
            speakers=args.speakers,
            diarize=not args.no_diarize,
            voice=not args.no_voiceprint,
            thresholds=thresholds,
            deps=deps,
        )
        common.atomic_write_text(wav.parent / "transcript.md", text)
    except Exception as e:
        print(f"错误：转写失败（{e}）", file=stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
