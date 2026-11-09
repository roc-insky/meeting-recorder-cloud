"""录音隔离探针：对比常驻流、abort 关闭、子进程三种方案。

用途：Task 9 的 spike。需要真实麦克风，不进自动测试。
运行：
    .venv/bin/python spike/mic_isolation_probe.py --mode persistent --cycles 20
    .venv/bin/python spike/mic_isolation_probe.py --mode abort --cycles 20
    .venv/bin/python spike/mic_isolation_probe.py --mode subprocess --cycles 20

超时保护：每次开流/关闭最多等 --open-timeout/--close-timeout 秒，
整轮实验最多 --total-timeout 秒；PortAudio 卡死时放弃等待并记一次卡顿，
不会让探针本身永远挂住。没有输入设备或没有麦克风权限时如实报"未测"并退出。
"""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_RATE = 16000  # 与 common.SAMPLE_RATE 一致
CHANNELS = 1
BLOCK_FRAMES = 1600  # 与 record.py 的 blocksize 一致
DTYPE = "int16"


class ProbeError(RuntimeError):
    """探针无法继续（没有输入设备、权限被拒等）。"""


@dataclass
class Attempt:
    """一次可能卡死的调用。"""

    ok: bool
    value: object = None
    elapsed: float = 0.0
    timed_out: bool = False
    error: BaseException | None = None


@dataclass
class Result:
    """一种模式的汇总。"""

    mode: str
    cycles_planned: int = 0
    cycles_done: int = 0
    open_seconds: list[float] = field(default_factory=list)
    abort_seconds: list[float] = field(default_factory=list)
    close_seconds: list[float] = field(default_factory=list)
    stalls: list[str] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stop_reason: str = "done"
    total_seconds: float = 0.0
    extra: dict = field(default_factory=dict)


def positive_int(text: str) -> int:
    """argparse 类型：正整数。"""
    try:
        value = int(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError("必须是正整数") from e
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def positive_float(text: str) -> float:
    """argparse 类型：正数。"""
    try:
        value = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError("必须是正数") from e
    if value <= 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return value


def non_negative_float(text: str) -> float:
    """argparse 类型：非负数。"""
    try:
        value = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError("必须是非负数") from e
    if value < 0:
        raise argparse.ArgumentTypeError("必须是非负数")
    return value


def run_with_timeout(fn, timeout: float) -> Attempt:
    """在守护线程里执行 fn，最多等 timeout 秒。

    PortAudio 卡死时线程可能永远不返回，这里只能放弃等待并记一次卡顿；
    线程是守护线程，不会拖住进程退出，但可能仍占着音频设备。
    """
    box: dict = {}
    done = threading.Event()

    def work() -> None:
        started = time.monotonic()
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 线程里必须兜住所有异常
            box["error"] = e
        finally:
            box["elapsed"] = time.monotonic() - started
            done.set()

    threading.Thread(target=work, name="mr-probe-call", daemon=True).start()
    started = time.monotonic()
    finished = done.wait(timeout)
    elapsed = time.monotonic() - started
    if not finished:
        return Attempt(False, None, elapsed, True, None)
    return Attempt(
        box.get("error") is None,
        box.get("value"),
        float(box.get("elapsed", elapsed)),
        False,
        box.get("error"),
    )


def _device_name(sd, info: dict) -> str:
    """拼出"主机 API / 设备名"，只用于终端显示。"""
    try:
        api = sd.query_hostapis(int(info.get("hostapi", -1)))["name"]
    except Exception:  # noqa: BLE001 设备信息缺字段时不影响测量
        api = "?"
    return f"{api} / {info.get('name', '?')}"


def list_input_devices(sd) -> list[tuple[int, dict]]:
    """返回所有至少有一个输入通道的设备。"""
    attempt = run_with_timeout(lambda: sd.query_devices(), 5.0)
    if attempt.timed_out:
        raise ProbeError("设备枚举超时（系统音频无响应）")
    if not attempt.ok:
        raise ProbeError(f"设备枚举失败（{attempt.error}）")
    devices = attempt.value
    return [
        (index, info)
        for index, info in enumerate(devices)
        if int(info.get("max_input_channels", 0)) > 0
    ]


def resolve_device(sd, wanted: int | None) -> tuple[int, dict]:
    """选输入设备：显式指定 > 系统默认 > 第一个输入设备。"""
    inputs = list_input_devices(sd)
    if not inputs:
        raise ProbeError(
            "没有可用的麦克风输入设备（本机可能没有麦克风，或 macOS 麦克风权限未授予）"
        )
    by_index = {index: info for index, info in inputs}
    if wanted is not None:
        if wanted not in by_index:
            raise ProbeError(f"设备 {wanted} 不是可用的输入设备")
        return wanted, by_index[wanted]
    default = sd.default.device[0]
    if default in by_index:
        return default, by_index[default]
    return inputs[0]


def open_stream(sd, device: int):
    """打开并启动一个 16 kHz 单声道 int16 输入流。"""
    stream = sd.InputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_FRAMES,
    )
    stream.start()
    return stream


def _record_stall(result: Result, label: str, elapsed: float, threshold: float) -> None:
    """超过阈值就记一次卡顿。"""
    if elapsed > threshold:
        result.stalls.append(f"{label} {elapsed:.2f}s")


def run_persistent(sd, device: int, args) -> Result:
    """只开一次流，循环"开始写盘/停止写盘"；空转时回调直接返回。"""
    result = Result(mode="persistent", cycles_planned=args.cycles)
    started = time.monotonic()
    active = threading.Event()
    q: queue.Queue = queue.Queue(maxsize=64)
    state = {"first": threading.Event(), "last": None, "gap_max": 0.0}
    counters = {"chunks": 0, "dropped": 0}

    def callback(indata, frames, time_info, status) -> None:
        # 空转：不拷贝、不入队，只花一次 Event 判断。
        if not active.is_set():
            return
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            state["gap_max"] = max(state["gap_max"], now - last)
        state["last"] = now
        state["first"].set()
        try:
            q.put_nowait(indata.tobytes())
            counters["chunks"] += 1
        except queue.Full:
            counters["dropped"] += 1

    attempt = run_with_timeout(
        lambda: _open_with_callback(sd, device, callback), args.open_timeout
    )
    if attempt.timed_out:
        result.stop_reason = "open_timeout"
        result.timeouts.append(f"开流 {attempt.elapsed:.2f}s")
        result.total_seconds = time.monotonic() - started
        return result
    if not attempt.ok:
        raise ProbeError(f"打开麦克风失败（{attempt.error}）")
    stream = attempt.value
    result.open_seconds.append(attempt.elapsed)

    tmp = Path(tempfile.mkdtemp(prefix="mr-mic-probe-"))
    wav_path = tmp / "persistent.wav"

    def writer() -> None:
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            while True:
                data = q.get()
                if data is None:
                    break
                wf.writeframes(data)

    writer_thread = threading.Thread(target=writer, name="mr-probe-writer", daemon=True)
    writer_thread.start()
    try:
        for index in range(1, args.cycles + 1):
            if time.monotonic() - started >= args.total_timeout:
                result.stop_reason = "total_timeout"
                break
            state["first"] = threading.Event()
            state["last"] = None
            active.set()
            cycle_started = time.monotonic()
            got = state["first"].wait(args.stall_threshold)
            first_wait = time.monotonic() - cycle_started
            if not got:
                active.clear()
                state["last"] = None
                result.cycles_done = index
                result.timeouts.append(
                    f"第{index}轮首块 {args.stall_threshold:.1f}s 内未到达"
                )
                result.stop_reason = "no_callback"
                break
            _record_stall(result, f"第{index}轮首块", first_wait, args.stall_threshold)
            remaining = args.cycle_seconds - (time.monotonic() - cycle_started)
            if remaining > 0:
                time.sleep(remaining)
            active.clear()
            state["last"] = None
            result.cycles_done = index
            print(
                f"第 {index}/{args.cycles} 轮：首块 {first_wait:.3f}s，"
                f"已收 {counters['chunks']} 块，丢弃 {counters['dropped']} 块"
            )
        # 停止时把队列里剩余的块写完
        q.put(None)
        writer_thread.join(timeout=2.0)
        if args.idle_seconds > 0 and result.stop_reason == "done":
            # 空转时间也受总时长约束，避免 --idle-seconds 把总时长保护架空
            budget = max(0.0, args.total_timeout - (time.monotonic() - started))
            idle_seconds = min(args.idle_seconds, budget)
            if idle_seconds <= 0:
                print("总时长已用完，跳过空转 CPU 采样。")
                result.timeouts.append("空转 CPU 采样被总时长截断")
                result.stop_reason = "total_timeout"
            else:
                print(
                    f"常驻流空转 {idle_seconds:.1f} 秒"
                    f"（请求 {args.idle_seconds:.1f} 秒），采样 CPU…"
                )
                cpu_started = time.process_time()
                idle_started = time.monotonic()
                time.sleep(idle_seconds)
                cpu = time.process_time() - cpu_started
                wall = time.monotonic() - idle_started
                result.extra["idle_cpu_percent"] = (
                    round(cpu / wall * 100, 2) if wall > 0 else None
                )
        close = run_with_timeout(lambda: (stream.stop(), stream.close()), args.close_timeout)
        if close.timed_out:
            result.timeouts.append(f"关闭 {close.elapsed:.2f}s")
            result.stop_reason = "close_timeout"
        else:
            result.close_seconds.append(close.elapsed)
    finally:
        active.clear()
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        q.put(None)
        writer_thread.join(timeout=2.0)
        result.extra["wav_bytes"] = wav_path.stat().st_size if wav_path.exists() else 0
        shutil.rmtree(tmp, ignore_errors=True)
    result.total_seconds = time.monotonic() - started
    result.extra["callback_gap_max"] = round(state["gap_max"], 3)
    return result


def _open_with_callback(sd, device: int, callback):
    """打开并启动带回调的输入流。"""
    stream = sd.InputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_FRAMES,
        callback=callback,
    )
    stream.start()
    return stream


def run_abort(sd, device: int, args) -> Result:
    """每轮新开流，结束用 abort() 再 close()，分别计时。"""
    result = Result(mode="abort", cycles_planned=args.cycles)
    started = time.monotonic()
    for index in range(1, args.cycles + 1):
        if time.monotonic() - started >= args.total_timeout:
            result.stop_reason = "total_timeout"
            break
        attempt = run_with_timeout(lambda: open_stream(sd, device), args.open_timeout)
        if attempt.timed_out:
            result.timeouts.append(f"第{index}轮开流 {attempt.elapsed:.2f}s")
            result.stop_reason = "open_timeout"
            break
        if not attempt.ok:
            raise ProbeError(f"第 {index} 轮打开麦克风失败（{attempt.error}）")
        stream = attempt.value
        result.open_seconds.append(attempt.elapsed)
        _record_stall(result, f"第{index}轮开流", attempt.elapsed, args.stall_threshold)
        try:
            time.sleep(args.cycle_seconds)
            aborted = run_with_timeout(stream.abort, args.close_timeout)
            if aborted.timed_out:
                result.timeouts.append(f"第{index}轮 abort {aborted.elapsed:.2f}s")
                result.stop_reason = "abort_timeout"
                break
            result.abort_seconds.append(aborted.elapsed)
            _record_stall(result, f"第{index}轮 abort", aborted.elapsed, args.stall_threshold)
            closed = run_with_timeout(stream.close, args.close_timeout)
            if closed.timed_out:
                result.timeouts.append(f"第{index}轮 close {closed.elapsed:.2f}s")
                result.stop_reason = "close_timeout"
                break
            result.close_seconds.append(closed.elapsed)
            _record_stall(result, f"第{index}轮 close", closed.elapsed, args.stall_threshold)
        finally:
            result.cycles_done = index
        print(
            f"第 {index}/{args.cycles} 轮：开流 {attempt.elapsed:.3f}s，"
            f"abort {result.abort_seconds[-1]:.3f}s，close {result.close_seconds[-1]:.3f}s"
        )
    result.total_seconds = time.monotonic() - started
    return result


def _read_sidecar(path: Path) -> dict | None:
    """读子进程写的 JSON；缺失或坏掉返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_subprocess(sd, device: int, args) -> Result:
    """每轮子进程开流录 --seconds 秒写 wav，父进程只收文件。"""
    result = Result(mode="subprocess", cycles_planned=args.cycles)
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="mr-mic-probe-sub-"))
    # 子进程要在 open + 录音 + close 之外再留 5 秒余量
    child_timeout = args.open_timeout + args.seconds + args.close_timeout + 5.0
    try:
        for index in range(1, args.cycles + 1):
            elapsed_total = time.monotonic() - started
            if elapsed_total >= args.total_timeout:
                result.stop_reason = "total_timeout"
                break
            out = tmp / f"cycle-{index:02d}.wav"
            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child-out",
                str(out),
                "--seconds",
                f"{args.seconds}",
                "--device",
                str(device),
                "--open-timeout",
                f"{args.open_timeout}",
                "--close-timeout",
                f"{args.close_timeout}",
            ]
            limit = min(child_timeout, max(1.0, args.total_timeout - elapsed_total))
            t0 = time.monotonic()
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
                exit_seconds = time.monotonic() - t0
            except subprocess.TimeoutExpired:
                exit_seconds = time.monotonic() - t0
                result.timeouts.append(
                    f"第{index}轮子进程退出 {exit_seconds:.2f}s（已杀）"
                )
                result.stop_reason = "child_timeout"
                break
            sidecar = _read_sidecar(Path(str(out) + ".json"))
            if cp.returncode != 0 or sidecar is None:
                detail = (cp.stderr or cp.stdout or "").strip().splitlines()
                result.stop_reason = "child_failed"
                result.errors.append(
                    f"第{index}轮子进程退出码 {cp.returncode}"
                    + (f"：{detail[-1]}" if detail else "")
                )
                break
            result.cycles_done = index
            result.open_seconds.append(float(sidecar.get("open_seconds", 0.0)))
            _record_stall(
                result,
                f"第{index}轮子进程开流",
                float(sidecar.get("open_seconds", 0.0)),
                args.stall_threshold,
            )
            overhead = exit_seconds - args.seconds
            _record_stall(result, f"第{index}轮子进程退出", overhead, args.stall_threshold)
            print(
                f"第 {index}/{args.cycles} 轮：子进程退出 {exit_seconds:.3f}s，"
                f"开流 {sidecar.get('open_seconds', 0):.3f}s，"
                f"wav {sidecar.get('frames', 0) // SAMPLE_RATE:.2f}s"
            )
    finally:
        if args.keep_files:
            print(f"保留的 wav 在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    result.total_seconds = time.monotonic() - started
    return result


def run_child(args) -> int:
    """子进程：开流录 --seconds 秒写 wav，再写一份 JSON 旁车文件。"""
    out = Path(args.child_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path = Path(str(out) + ".json")
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        print(f"错误：无法导入 sounddevice（{e}）", file=sys.stderr)
        return 2
    try:
        device, _info = resolve_device(sd, args.device)
    except ProbeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status) -> None:
        q.put(indata.tobytes())

    attempt = run_with_timeout(
        lambda: _open_with_callback(sd, device, callback),
        args.open_timeout,
    )
    if not attempt.ok or attempt.timed_out:
        print(
            f"错误：打开麦克风{'超时' if attempt.timed_out else '失败'}"
            f"（{attempt.error}）",
            file=sys.stderr,
        )
        return 1
    stream = attempt.value
    open_seconds = attempt.elapsed
    frames = 0
    close_seconds = 0.0
    error = None
    try:
        target = int(round(args.seconds * SAMPLE_RATE))
        record_started = time.monotonic()
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            while frames < target:
                if time.monotonic() - record_started > args.seconds + args.close_timeout:
                    break
                try:
                    data = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                wf.writeframes(data)
                frames += len(data) // 2
        closed = run_with_timeout(lambda: (stream.stop(), stream.close()), args.close_timeout)
        close_seconds = closed.elapsed
        if closed.timed_out:
            error = "关闭超时"
    finally:
        if frames == 0 and error is None:
            error = "没有收到音频"
        sidecar_path.write_text(
            json.dumps(
                {
                    "device": device,
                    "open_seconds": open_seconds,
                    "close_seconds": close_seconds,
                    "frames": frames,
                    "duration_seconds": frames / SAMPLE_RATE,
                    "error": error,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if error is not None:
        print(f"错误：{error}", file=sys.stderr)
        return 4
    return 0


def _max_avg(values: list[float]) -> tuple[float | None, float | None]:
    """返回（最大, 平均）；没有样本时都是 None。"""
    if not values:
        return None, None
    return max(values), sum(values) / len(values)


def print_summary(result: Result, args) -> None:
    """按计划要求的四项打印汇总。"""
    open_max, open_avg = _max_avg(result.open_seconds)
    print("")
    print("== 汇总 ==")
    print(f"模式: {result.mode}")
    print(f"轮数: {result.cycles_done}/{args.cycles}")
    if open_max is None:
        print("开流耗时: 未测（没有成功的开流样本）")
    else:
        print(
            f"开流耗时: 最大 {open_max:.3f}s / 平均 {open_avg:.3f}s"
            f"（样本 {len(result.open_seconds)}）"
        )
    print(f"超过 {args.stall_threshold:.1f}s 的卡顿: {len(result.stalls)} 次")
    for item in result.stalls:
        print(f"  - {item}")
    if result.timeouts:
        print(f"超时放弃: {len(result.timeouts)} 次")
        for item in result.timeouts:
            print(f"  - {item}")
    if result.errors:
        print(f"其他失败: {len(result.errors)} 次")
        for item in result.errors:
            print(f"  - {item}")
    if result.abort_seconds:
        abort_max, abort_avg = _max_avg(result.abort_seconds)
        print(f"abort 耗时: 最大 {abort_max:.3f}s / 平均 {abort_avg:.3f}s")
    if result.close_seconds:
        close_max, close_avg = _max_avg(result.close_seconds)
        print(f"close 耗时: 最大 {close_max:.3f}s / 平均 {close_avg:.3f}s")
    print(f"总耗时: {result.total_seconds:.1f}s")
    print(f"停止原因: {result.stop_reason}")
    if result.extra:
        for key, value in result.extra.items():
            print(f"{key}: {value}")
    if args.json:
        payload = {
            "mode": result.mode,
            "cycles_planned": args.cycles,
            "cycles_done": result.cycles_done,
            "open_seconds": result.open_seconds,
            "open_seconds_max": open_max,
            "open_seconds_avg": open_avg,
            "abort_seconds": result.abort_seconds,
            "close_seconds": result.close_seconds,
            "stalls": result.stalls,
            "timeouts": result.timeouts,
            "errors": result.errors,
            "stop_reason": result.stop_reason,
            "total_seconds": round(result.total_seconds, 3),
            **result.extra,
        }
        try:
            Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"JSON 已写入: {args.json}")
        except OSError as e:
            print(f"警告：JSON 写入失败（{e}）")


def build_parser() -> argparse.ArgumentParser:
    """命令行参数。"""
    parser = argparse.ArgumentParser(
        description="录音隔离探针：比较常驻流 / abort / 子进程三种方案（Task 9 spike）",
        epilog=(
            "需要真实麦克风。没有输入设备或没有权限时不会编造数据，"
            "会报“未测”并以非零码退出。"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["persistent", "abort", "subprocess"],
        default="persistent",
        help="persistent=只开一次流；abort=每轮 abort 关闭；subprocess=每轮子进程（默认 persistent）",
    )
    parser.add_argument("--cycles", type=positive_int, default=20, help="轮数（默认 20）")
    parser.add_argument(
        "--cycle-seconds",
        type=non_negative_float,
        default=0.5,
        help="每轮模拟录音的秒数（默认 0.5；子进程模式用 --seconds）",
    )
    parser.add_argument(
        "--seconds",
        type=positive_float,
        default=2.0,
        help="子进程模式每轮实际录音秒数（默认 2.0）",
    )
    parser.add_argument("--device", type=int, default=None, help="输入设备编号（默认系统默认设备）")
    parser.add_argument(
        "--open-timeout", type=positive_float, default=15.0, help="单次开流超时秒数（默认 15）"
    )
    parser.add_argument(
        "--close-timeout", type=positive_float, default=5.0, help="单次关闭超时秒数（默认 5）"
    )
    parser.add_argument(
        "--stall-threshold",
        type=positive_float,
        default=5.0,
        help="超过多少秒算一次卡顿（默认 5）",
    )
    parser.add_argument(
        "--total-timeout",
        type=positive_float,
        default=300.0,
        help="整轮实验总时长上限秒数（默认 300）",
    )
    parser.add_argument(
        "--idle-seconds",
        type=non_negative_float,
        default=0.0,
        help="persistent 收尾空转秒数，用于 1 小时空转 CPU 验收（默认 0）",
    )
    parser.add_argument("--list-devices", action="store_true", help="列出输入设备后退出")
    parser.add_argument("--json", metavar="FILE", default=None, help="把汇总写成 JSON 文件")
    parser.add_argument(
        "--keep-files", action="store_true", help="子进程模式保留临时 wav（排错用）"
    )
    parser.add_argument(
        "--child-out", metavar="FILE", default=None, help=argparse.SUPPRESS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口。返回码：0 完成；1 有超时/失败；2 无设备；3 音频错误。"""
    args = build_parser().parse_args(argv)
    if args.child_out:
        return run_child(args)
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        print(f"错误：无法导入 sounddevice（{e}）")
        return 2
    try:
        if args.list_devices:
            inputs = list_input_devices(sd)
            if not inputs:
                print("没有可用的麦克风输入设备。")
                return 2
            for index, info in inputs:
                print(
                    f"{index}: {_device_name(sd, info)}"
                    f"（输入 {info.get('max_input_channels')} 通道，"
                    f"默认采样率 {info.get('default_samplerate')}）"
                )
            return 0
        device, info = resolve_device(sd, args.device)
    except ProbeError as e:
        print(f"错误：{e}")
        print("结论：未测（没有可用的麦克风输入设备，不能生成实测数据）")
        if args.json:
            payload = {
                "mode": args.mode,
                "cycles_planned": args.cycles,
                "cycles_done": 0,
                "open_seconds": [],
                "open_seconds_max": None,
                "open_seconds_avg": None,
                "stalls": [],
                "timeouts": [],
                "errors": [],
                "stop_reason": "no_input_device",
                "total_seconds": 0.0,
                "error": str(e),
            }
            try:
                Path(args.json).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"JSON 已写入: {args.json}")
            except OSError as exc:
                print(f"警告：JSON 写入失败（{exc}）")
        return 2

    print("== 录音隔离探针 ==")
    print(f"模式: {args.mode}")
    print(f"设备: {device} {_device_name(sd, info)}")
    print(
        f"参数: {SAMPLE_RATE} Hz / {CHANNELS} 声道 / 块 {BLOCK_FRAMES} 帧，"
        f"轮数 {args.cycles}，单轮 {args.cycle_seconds}s"
    )
    print(
        f"超时: 开流 {args.open_timeout}s，关闭 {args.close_timeout}s，"
        f"总时长 {args.total_timeout}s，卡顿阈值 {args.stall_threshold}s"
    )
    print("")
    try:
        if args.mode == "persistent":
            result = run_persistent(sd, device, args)
        elif args.mode == "abort":
            result = run_abort(sd, device, args)
        else:
            result = run_subprocess(sd, device, args)
    except ProbeError as e:
        print(f"错误：{e}")
        print("结论：未测（音频设备不可用，不能生成实测数据）")
        return 3
    except KeyboardInterrupt:
        print("已中断。")
        return 130
    print_summary(result, args)
    failed = result.stalls or result.timeouts or result.errors
    return 0 if not failed and result.cycles_done == args.cycles else 1


if __name__ == "__main__":
    sys.exit(main())