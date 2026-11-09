"""Web 配置：从 config.toml 读取端口与大模型设置；任何异常都退回默认，绝不抛出。"""
import math
import tomllib
from pathlib import Path

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_INPUT_CHARS = 60000
_LLM_REQUIRED = ("base_url", "api_key", "model")


def _load_section(path, name: str) -> dict | None:
    """读取 TOML 的某个表；文件缺失、不可读、非 UTF-8、语法错误或该项不是表都返回 None。"""
    if path is None:
        return None
    try:
        with open(Path(path), "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):  # tomllib.TOMLDecodeError 与 UnicodeDecodeError 都是 ValueError
        return None
    section = data.get(name)
    return section if isinstance(section, dict) else None


def load_port(path: Path | None) -> int:
    """[web] port；缺失或非 1–65535 的整数（含 bool）一律返回 8765。"""
    section = _load_section(path, "web")
    port = section.get("port") if section else None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return DEFAULT_PORT
    return port


def load_llm(path: Path | None) -> dict | None:
    """[llm] 配置；缺任一必填项（base_url、api_key、model 须为非空字符串）返回 None。

    timeout_s 默认 120（正数），max_input_chars 默认 60000（正整数）；非法可选项退回默认。
    """
    section = _load_section(path, "llm")
    if not section:
        return None
    cfg: dict = {}
    for key in _LLM_REQUIRED:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        cfg[key] = value
    timeout = section.get("timeout_s")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        timeout = DEFAULT_TIMEOUT_S
    cfg["timeout_s"] = timeout
    limit = section.get("max_input_chars")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = DEFAULT_MAX_INPUT_CHARS
    cfg["max_input_chars"] = limit
    return cfg
