"""会议存储：列出、定位、标题、标注、删除与文字稿表头解析（只操作 root 下的会议目录）。"""
import json
import math
import shutil
import threading
from collections.abc import Collection
from pathlib import Path

import common

TITLE_MAX = 100
ANNOTATION_TEXT_MAX = 2000
ANNOTATION_KINDS = ("note", "highlight", "comment")
_META = "meta.json"
_ANNOTATIONS = "annotations.json"
_TRANSCRIPT = "transcript.md"
_SPEAKERS_PREFIX = "- 说话人："
_VOICE_PREFIX = "- 声纹匹配："
_LINE_BREAKS = ("\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")

# 标注是「读—改—写」，同进程内用一把锁串行化，避免并发丢更新
_annotation_lock = threading.Lock()


class MeetingNotFound(Exception):
    """会议不存在、id 非法或会逃出 root。"""


def _is_meeting_dir(p: Path) -> bool:
    """真实目录（非符号链接）且不以 . 开头才算会议。"""
    return not p.name.startswith(".") and p.is_dir() and not p.is_symlink()


def list_meetings(root: Path) -> list[dict]:
    """列出会议，新到旧（按目录名倒序）；每项含 id、title、has_transcript。"""
    root = Path(root)
    try:
        entries = [p for p in root.iterdir() if _is_meeting_dir(p)]
    except OSError:
        return []
    entries.sort(key=lambda p: p.name, reverse=True)
    return [
        {"id": p.name, "title": get_title(p), "has_transcript": (p / _TRANSCRIPT).is_file()}
        for p in entries
    ]


def list_incomplete(root: Path, transcribing: Collection[str]) -> list[str]:
    """有录音、没文字稿、且不在转写中的会议 id，新到旧；只做 stat，不打开文件。"""
    root = Path(root)
    try:
        entries = [p for p in root.iterdir() if _is_meeting_dir(p)]
    except OSError:
        return []
    busy = set(transcribing or ())
    out = []
    for p in entries:
        if p.name in busy:
            continue
        try:
            if not (p / "meeting.wav").is_file() or (p / _TRANSCRIPT).is_file():
                continue
        except OSError:
            continue
        out.append(p.name)
    out.sort(reverse=True)
    return out


def resolve(root: Path, meeting_id) -> Path:
    """把会议 id 解析为 root 下的会议目录；任何非法或越界情形抛 MeetingNotFound。"""
    if (not isinstance(meeting_id, str) or not meeting_id or "\x00" in meeting_id
            or "/" in meeting_id or "\\" in meeting_id
            or meeting_id in (".", "..") or meeting_id.startswith(".")):
        raise MeetingNotFound("会议不存在")
    try:
        root_real = Path(root).resolve()
        candidate = root_real / meeting_id
        if candidate.is_symlink():  # 符号链接一律不当作会议
            raise MeetingNotFound("会议不存在")
        real = candidate.resolve()
        real.relative_to(root_real)
        if real.parent != root_real or not real.is_dir():
            raise MeetingNotFound("会议不存在")
    except (OSError, ValueError, RuntimeError):
        raise MeetingNotFound("会议不存在") from None
    return real


def _valid_title(title) -> bool:
    return (isinstance(title, str) and bool(title.strip()) and len(title) <= TITLE_MAX
            and not any(c in title for c in _LINE_BREAKS) and "\x00" not in title)


def _read_json(path: Path):
    """读 JSON；缺失、损坏或不可读返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get_title(meeting_dir: Path) -> str:
    """会议标题；meta.json 缺失、损坏或标题非法时用目录名。"""
    meeting_dir = Path(meeting_dir)
    meta = _read_json(meeting_dir / _META)
    if isinstance(meta, dict) and _valid_title(meta.get("title")):
        return meta["title"]
    return meeting_dir.name


def set_title(meeting_dir: Path, title) -> None:
    """设置标题（原子写 meta.json）；非字符串、空白、超过 100 字或含换行抛 ValueError，不做静默修改。"""
    if not isinstance(title, str):
        raise ValueError("标题必须是文字")
    if not title.strip():
        raise ValueError("标题不能为空")
    if len(title) > TITLE_MAX:
        raise ValueError(f"标题不能超过 {TITLE_MAX} 个字")
    if any(c in title for c in _LINE_BREAKS) or "\x00" in title:
        raise ValueError("标题不能包含换行")
    meeting_dir = Path(meeting_dir)
    meta = _read_json(meeting_dir / _META)
    if not isinstance(meta, dict):
        meta = {}
    meta["title"] = title
    common.atomic_write_text(meeting_dir / _META, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def _valid_time(t) -> bool:
    """t 必须是可表示为有限浮点数的非负数字。

    math.isfinite() 遇到超大整数（如 401 位）会抛 OverflowError，这里统一转成“无效”。
    """
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return False
    try:
        return math.isfinite(t) and t >= 0
    except OverflowError:
        return False


def _valid_annotation(a) -> bool:
    if not isinstance(a, dict):
        return False
    t = a.get("t")
    text = a.get("text")
    return (a.get("kind") in ANNOTATION_KINDS
            and _valid_time(t)
            and isinstance(text, str) and bool(text.strip()) and len(text) <= ANNOTATION_TEXT_MAX)


def _load_annotations(meeting_dir: Path) -> list[dict]:
    """读标注，跳过非法条目；整体损坏返回空列表。"""
    data = _read_json(meeting_dir / _ANNOTATIONS)
    if not isinstance(data, list):
        return []
    return [a for a in data if _valid_annotation(a)]


def _items_for_write(path: Path) -> list[dict]:
    """写之前读出现有标注。文件损坏、形状不对或含被拒绝的条目时，先把完整原件备份到唯一路径再返回可保留部分；
    备份失败抛 DataProtectionError（调用方不得继续写）。读取只在写路径上留档，纯读取不改动任何文件。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise common.DataProtectionError(f"无法读取标注文件，已取消写入（{e}）") from e
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, list):
        common.backup_original(path)
        return []
    items = [a for a in data if _valid_annotation(a)]
    if len(items) != len(data):
        common.backup_original(path)     # 有条目将被丢弃：原件完整留档后才重写
    return items


def list_annotations(meeting_dir: Path) -> list[dict]:
    """标注列表，按 t 升序，同 t 保持插入顺序（稳定排序）。"""
    items = _load_annotations(Path(meeting_dir))
    return sorted(items, key=lambda a: a["t"])


def add_annotation(meeting_dir: Path, kind, t, text) -> dict:
    """追加一条标注并原子落盘，返回该条；参数非法抛 ValueError，目录不存在抛 MeetingNotFound。"""
    if kind not in ANNOTATION_KINDS or not isinstance(kind, str):
        raise ValueError("标注类型必须是 note、highlight 或 comment")
    if not _valid_time(t):
        raise ValueError("时间必须是非负数字（秒）")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("标注内容不能为空")
    if len(text) > ANNOTATION_TEXT_MAX:
        raise ValueError(f"标注内容不能超过 {ANNOTATION_TEXT_MAX} 个字")
    meeting_dir = Path(meeting_dir)
    if not meeting_dir.is_dir():
        raise MeetingNotFound("会议不存在")
    record = {"kind": kind, "t": t, "text": text}
    path = meeting_dir / _ANNOTATIONS
    with _annotation_lock:
        items = _items_for_write(path)
        items.append(record)
        common.atomic_write_text(path, json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    return record


def delete_meeting(root: Path, meeting_id) -> None:
    """删除会议目录（先 resolve，符号链接与越界一律拒绝）。"""
    shutil.rmtree(resolve(root, meeting_id))


def parse_header(text: str) -> dict:
    """解析文字稿表头（首个空行之前）。

    返回 {"speakers": [...]（含「疑似」前缀原文）, "voice_scores": {标签: 分数}}；
    没有对应行时分别为空列表与空字典。声纹匹配行里无法解析的项直接跳过。
    """
    speakers: list[str] = []
    scores: dict[str, float] = {}
    for line in text.splitlines():
        if not line.strip():  # 表头以首个空行结束
            break
        if line.startswith(_SPEAKERS_PREFIX):
            value = line[len(_SPEAKERS_PREFIX):].replace("，", "、").replace(",", "、")
            for name in value.split("、"):
                name = name.strip()
                if name and name not in speakers:
                    speakers.append(name)
        elif line.startswith(_VOICE_PREFIX):
            value = line[len(_VOICE_PREFIX):].replace("，", "、").replace(",", "、")
            for item in value.split("、"):
                parts = item.strip().rsplit(None, 1)
                if len(parts) != 2 or not parts[0]:
                    continue
                try:
                    scores[parts[0]] = float(parts[1])
                except ValueError:
                    continue
    return {"speakers": speakers, "voice_scores": scores}
