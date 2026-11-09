"""面试职位存储：一个 JSON 文件（职位列表），原子写。

只存职位本身（id / type / title / requirements / created_at / updated_at）；
候选人简历与生成的面试题不落盘，只在请求里经过内存。
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

import common

JOB_TYPES = ("前端", "后端", "客户端", "测试")
TITLE_MAX = 100
REQUIREMENTS_MAX = 10000
MAX_JOBS = 200
_LINE_BREAKS = ("\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")


class JobNotFound(Exception):
    """职位不存在。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate(type_, title, requirements) -> None:
    """校验职位字段，不合法抛 ValueError（中文消息）。"""
    if type_ not in JOB_TYPES or not isinstance(type_, str):
        raise ValueError("职位类型必须是 前端、后端、客户端 或 测试")
    if not isinstance(title, str):
        raise ValueError("职位名称必须是文字")
    if not title.strip():
        raise ValueError("职位名称不能为空")
    if len(title) > TITLE_MAX:
        raise ValueError(f"职位名称不能超过 {TITLE_MAX} 个字")
    if any(c in title for c in _LINE_BREAKS) or "\x00" in title:
        raise ValueError("职位名称不能包含换行")
    if not isinstance(requirements, str):
        raise ValueError("职位要求必须是文字")
    if len(requirements) > REQUIREMENTS_MAX:
        raise ValueError(f"职位要求不能超过 {REQUIREMENTS_MAX} 个字")


def _valid_entry(j) -> bool:
    try:
        return (isinstance(j, dict) and isinstance(j.get("id"), str) and bool(j["id"])
                and isinstance(j.get("created_at"), str) and isinstance(j.get("updated_at"), str)
                and not validate(j.get("type"), j.get("title"), j.get("requirements")))
    except ValueError:
        return False


class JobStore:
    """职位库；同进程内用锁串行化「读—改—写」。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        """读文件；缺失返回 []；损坏时留档为 .corrupt 后当作空，非法条目跳过。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            self._quarantine()
            return []
        if not isinstance(data, list):
            self._quarantine()
            return []
        return [j for j in data if _valid_entry(j)]

    def _quarantine(self) -> None:
        try:
            self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass

    def _save(self, jobs: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        common.atomic_write_text(self.path, json.dumps(jobs, ensure_ascii=False, indent=2) + "\n")

    def list(self) -> list[dict]:
        with self._lock:
            return self._load()

    def get(self, job_id: str) -> dict:
        for j in self.list():
            if j["id"] == job_id:
                return j
        raise JobNotFound("职位不存在")

    def create(self, type_, title, requirements="") -> dict:
        validate(type_, title, requirements)
        with self._lock:
            jobs = self._load()
            if len(jobs) >= MAX_JOBS:
                raise ValueError(f"职位最多 {MAX_JOBS} 个，请先删除不用的")
            now = _now()
            job = {"id": uuid.uuid4().hex, "type": type_, "title": title, "requirements": requirements,
                   "created_at": now, "updated_at": now}
            jobs.append(job)
            self._save(jobs)
            return job

    def update(self, job_id: str, type_, title, requirements="") -> dict:
        validate(type_, title, requirements)
        with self._lock:
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id:
                    j.update(type=type_, title=title, requirements=requirements, updated_at=_now())
                    self._save(jobs)
                    return dict(j)
        raise JobNotFound("职位不存在")

    def delete(self, job_id: str) -> None:
        with self._lock:
            jobs = self._load()
            kept = [j for j in jobs if j["id"] != job_id]
            if len(kept) == len(jobs):
                raise JobNotFound("职位不存在")
            self._save(kept)
