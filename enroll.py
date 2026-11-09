"""声纹登记命令行：现场录音或读取 wav 文件，生成声纹并保存。

用法：
  python enroll.py --name 张三              录 30 秒
  python enroll.py --name 张三 --seconds 20
  python enroll.py --name 张三 --file a.wav
  python enroll.py --name 张三 --replace   覆盖已有声纹

main 的 deps 字典（均可选，便于测试）：
  extractor          假的向量提取函数，缺省用 voiceprint.default_extractor（延迟加载模型）
  stream_factory     同 record.py，缺省用真实麦克风
  stop               threading.Event，置位即取消录音
  stdout / stderr    输出流
"""
import argparse
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

import common
import record
import transcribe
import voiceprint

DEFAULT_SECONDS = 30
NO_DATA_GRACE_S = 10.0  # 超过目标时长这么久仍凑不够样本，视为麦克风没有数据


def record_from_mic(seconds, *, stream_factory, stop=None, on_progress=None) -> np.ndarray:
    """从麦克风录 seconds 秒，返回 16 kHz int16 样本。

    stop 事件置位或按 Ctrl-C 则提前结束并抛 VoiceprintError("已取消")。
    """
    target = int(seconds * common.SAMPLE_RATE)
    q: queue.Queue = queue.Queue()
    try:
        stream = record.open_stream(lambda: stream_factory(q.put))
    except record.RecorderError as e:
        raise voiceprint.VoiceprintError(str(e)) from e
    chunks, got = [], 0
    deadline = time.monotonic() + seconds + NO_DATA_GRACE_S
    try:
        while got < target:
            if stop is not None and stop.is_set():
                raise voiceprint.VoiceprintError("已取消")
            if time.monotonic() > deadline:
                raise voiceprint.VoiceprintError("麦克风没有数据，请检查权限和设备")
            try:
                chunk = np.asarray(q.get(timeout=0.1))
            except queue.Empty:
                continue
            if chunk.ndim > 1:
                chunk = chunk[:, 0]
            chunks.append(chunk.astype(np.int16, copy=False))
            got += chunk.size
            if on_progress is not None:
                on_progress(min(got, target) / common.SAMPLE_RATE)
    except KeyboardInterrupt:
        raise voiceprint.VoiceprintError("已取消") from None
    finally:
        record.close_stream(stream)
    return np.concatenate(chunks)[:target]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="登记声纹")
    p.add_argument("--name", required=True, help="名字（字母/数字/汉字/_/-，1–20 字）")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--seconds", type=transcribe._positive_int, metavar="N", default=None,
                     help=f"现场录音秒数（默认 {DEFAULT_SECONDS}）")
    src.add_argument("--file", metavar="PATH", help="用已有 wav 文件登记")
    p.add_argument("--replace", action="store_true", help="同名声纹已存在时覆盖")
    return p


def main(argv=None, *, deps=None) -> int:
    args = build_parser().parse_args(argv)
    deps = deps or {}
    stdout = deps.get("stdout", sys.stdout)
    stderr = deps.get("stderr", sys.stderr)
    try:
        voiceprint.validate_name(args.name)
        if voiceprint.existing_profile_path(args.name) is not None and not args.replace:
            raise voiceprint.VoiceprintError(f"声纹“{args.name}”已存在，加 --replace 可覆盖")

        if args.file:
            wav = Path(args.file)
            try:
                rate, samples = common.read_wav_tolerant(wav)
            except OSError as e:
                raise voiceprint.VoiceprintError(f"无法读取文件：{wav}（{e}）") from e
            except ValueError as e:
                raise voiceprint.VoiceprintError(str(e)) from e
            samples = transcribe._resample(samples, rate)
        else:
            seconds = args.seconds or DEFAULT_SECONDS
            print(f"请自然说话 {seconds} 秒（Ctrl-C 取消）…", file=stdout)

            def progress(done: float) -> None:
                print(f"\r已录 {int(done)}/{seconds} 秒", end="", file=stdout, flush=True)

            samples = record_from_mic(
                seconds,
                stream_factory=deps.get("stream_factory", record._real_stream_factory),
                stop=deps.get("stop"),
                on_progress=progress,
            )
            print(file=stdout)

        extractor = deps.get("extractor") or voiceprint.default_extractor()
        vector, chunks, speech_s = voiceprint.enroll_vector(samples, extractor=extractor)
        path = voiceprint.save_profile(
            args.name, vector, seconds=speech_s, chunks=chunks,
            model=voiceprint.MODEL_NAME, replace=args.replace,
        )
    except voiceprint.VoiceprintError as e:
        print(f"错误：{e}", file=stderr)
        return 1
    except Exception as e:
        print(f"错误：登记失败（{e}）", file=stderr)
        return 1
    print(f"已保存声纹“{args.name}”：{path}", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
