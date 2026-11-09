"""doctor 自检核心：只调用注入函数，自己不做任何 IO、不联网、不枚举设备。

每项检查只返回固定中文短句，绝不包含绝对路径、设备名/序列号或异常原文，
Web 自检页可以原样展示。
"""
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar

MIN_FREE_BYTES = 2 * 1024 ** 3

_T = TypeVar("_T")


@dataclass(frozen=True)
class Check:
    """一项自检结果。status 只取 ok / warn / fail。"""

    name: str
    status: str
    detail: str


def call_with_timeout(fn: Callable[[], _T], seconds: float) -> tuple[bool, _T | BaseException | None]:
    """在守护线程里跑 fn，最多等 seconds 秒。

    超时返回 (False, None)；fn 抛异常返回 (True, 异常对象)；正常返回 (True, 结果)。
    超时后线程仍是守护线程，不会拖住进程退出。
    """
    box: list = []

    def target() -> None:
        try:
            box.append((True, fn()))
        except BaseException as e:  # noqa: BLE001 自检线程里的任何异常都交给调用方判断
            box.append((False, e))

    thread = threading.Thread(target=target, name="mr-doctor-check", daemon=True)
    thread.start()
    thread.join(max(0.0, float(seconds)))
    if thread.is_alive():
        return False, None
    if not box:
        return True, None
    return True, box[0][1]


def _safe(fn: Callable[[], object]) -> tuple[object, bool]:
    """调用注入函数；抛异常时返回 (None, True)。异常原文一律丢弃。"""
    try:
        return fn(), False
    except Exception:  # noqa: BLE001 单项检查失败不影响其余检查
        return None, True


def _check_mic(list_input_devices: Callable[[], list[str]], timeout_s: float) -> Check:
    finished, value = call_with_timeout(list_input_devices, timeout_s)
    if not finished:
        return Check("麦克风", "fail", "设备枚举无响应")
    if isinstance(value, BaseException):
        return Check("麦克风", "fail", "无法枚举麦克风设备")
    if not value:
        return Check("麦克风", "fail", "未找到麦克风")
    return Check("麦克风", "ok", "麦克风可用")


def _check_disk(free_bytes: Callable[[], int]) -> Check:
    free, failed = _safe(free_bytes)
    try:
        low = int(free) < MIN_FREE_BYTES  # type: ignore[arg-type]
    except (TypeError, ValueError):
        failed = True
        low = False
    if failed:
        return Check("磁盘", "warn", "无法读取磁盘剩余空间")
    if low:
        return Check("磁盘", "warn", "磁盘剩余空间不足 2 GB")
    return Check("磁盘", "ok", "磁盘空间充足")


def _check_models(models_missing: Callable[[], list[str]]) -> Check:
    missing, failed = _safe(models_missing)
    if failed:
        return Check("模型缓存", "warn", "无法检查模型文件")
    if missing:
        return Check("模型缓存", "fail", "模型文件缺失")
    return Check("模型缓存", "ok", "模型文件齐全")


def _check_llm(llm_config: dict | None) -> Check:
    """只核对配置字段，不发任何网络请求。"""
    if not isinstance(llm_config, dict) or not llm_config:
        return Check("大模型配置", "warn", "未配置大模型")

    def blank(value: object) -> bool:
        return not isinstance(value, str) or not value.strip()

    base = llm_config.get("base_url")
    if blank(base) or blank(llm_config.get("api_key")) or blank(llm_config.get("model")):
        return Check("大模型配置", "warn", "大模型配置不完整")
    base = str(base).strip()
    if not (base.startswith("http://") or base.startswith("https://")):
        return Check("大模型配置", "fail", "大模型地址无效")
    return Check("大模型配置", "ok", "大模型配置完整")


def _check_voiceprint(voiceprint_modes: Callable[[], list[int]]) -> Check:
    modes, failed = _safe(voiceprint_modes)
    if failed:
        return Check("声纹文件权限", "warn", "无法检查声纹文件权限")
    if not modes:
        return Check("声纹文件权限", "ok", "未登记声纹")
    try:
        for mode in modes:  # type: ignore[union-attr]
            if (int(mode) & 0o777) != 0o600:
                return Check("声纹文件权限", "warn", "声纹文件权限过宽")
    except (TypeError, ValueError):
        return Check("声纹文件权限", "warn", "无法检查声纹文件权限")
    return Check("声纹文件权限", "ok", "声纹文件权限正常")


def _check_threads(lingering_threads: Callable[[], int]) -> Check:
    count, failed = _safe(lingering_threads)
    try:
        alive = int(count) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        failed = True
        alive = False
    if failed:
        return Check("残留音频线程", "warn", "无法检查音频线程")
    if alive:
        return Check("残留音频线程", "warn", "仍有音频线程未退出")
    return Check("残留音频线程", "ok", "无残留音频线程")


def run_checks(
    *,
    list_input_devices: Callable[[], list[str]],
    free_bytes: Callable[[], int],
    models_missing: Callable[[], list[str]],
    llm_config: dict | None,
    voiceprint_modes: Callable[[], list[int]],
    lingering_threads: Callable[[], int],
    device_timeout_s: float = 3.0,
) -> list[Check]:
    """按固定顺序执行六项自检。只调用注入函数，自身不碰文件系统、网络和音频设备。"""
    return [
        _check_mic(list_input_devices, device_timeout_s),
        _check_disk(free_bytes),
        _check_models(models_missing),
        _check_llm(llm_config),
        _check_voiceprint(voiceprint_modes),
        _check_threads(lingering_threads),
    ]
