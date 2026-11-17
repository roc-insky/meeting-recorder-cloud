"""仅开发用：浏览器录音的“实时转写”端点（由 cloud_dev.py 挂载，生产 cloud/ 不导入本模块）。

浏览器把 16 kHz 单声道 Int16 PCM 原始字节 POST 过来 → 本地 VAD 切句（live.Segmenter）
→ 后台线程用本机 mlx-whisper 转写（live.LiveTranscriber）→ 文本随下一次 POST 的响应返回。
文本只保存在内存里，不写数据库、不进入云端文字稿；云端没有 worker/模型，不会产生会后文字稿。
"""
from __future__ import annotations

import io
import threading
import time
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request

import common
import live
import transcribe
from cloud.auth import AuthContext, require_csrf

router = APIRouter(prefix="/api/dev-live", tags=["dev-live"])

MAX_BODY_BYTES = 1 << 20          # 单次最多 1 MiB PCM（约 32 秒）
MAX_SESSIONS_PER_USER = 2
FINISH_TIMEOUT_S = 120.0          # 首次加载 ASR 模型约 77 秒，收尾时给足时间

_warm = {"state": "idle", "error": None}
_warm_lock = threading.Lock()
_sessions: dict[str, "_Live"] = {}
_sessions_lock = threading.Lock()
_vad_factory = None


def _get_vad_factory():
    global _vad_factory
    if _vad_factory is None:
        _vad_factory = live._sherpa_vad_factory(live.ensure_vad_model())
    return _vad_factory


def warm_up_async() -> None:
    """服务启动时在后台加载 VAD 与 ASR 模型，避免第一次说话等待太久。"""
    with _warm_lock:
        if _warm["state"] != "idle":
            return
        _warm["state"] = "warming"

    def run():
        try:
            _get_vad_factory()
            transcribe.transcribe_samples(np.zeros(common.SAMPLE_RATE, dtype=np.float32))
            _warm.update(state="ready", error=None)
        except Exception as exc:  # noqa: BLE001
            _warm.update(state="failed", error=str(exc))

    threading.Thread(target=run, name="dev-live-warmup", daemon=True).start()


class _Collector:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[dict] = []

    def write(self, start_s: float, text: str) -> None:
        with self._lock:
            self._items.append({"start_s": round(float(start_s), 2), "text": text})

    def drain(self) -> list[dict]:
        with self._lock:
            items, self._items = self._items, []
        return items


class _Live:
    def __init__(self, owner: str):
        self.owner = owner
        self.collector = _Collector()
        self.lock = threading.Lock()
        self.finished = False
        self.created = time.time()
        stderr = io.StringIO()
        transcriber = live.LiveTranscriber(
            lambda audio: transcribe.transcribe_samples(audio), self.collector, stderr=stderr
        )
        self.session = live.LiveSession(live.Segmenter(_get_vad_factory()), transcriber)


def _find(ctx: AuthContext, live_id: str) -> "_Live":
    with _sessions_lock:
        item = _sessions.get(live_id)
    if item is None or item.owner != ctx.user_id:
        raise HTTPException(status_code=404, detail="live_session_not_found")
    return item


@router.get("/status")
def status() -> dict:
    return {"state": _warm["state"], "error": _warm["error"]}


@router.post("", status_code=201)
def create(ctx: AuthContext = Depends(require_csrf)) -> dict:
    warm_up_async()
    with _sessions_lock:
        mine = [k for k, v in _sessions.items() if v.owner == ctx.user_id and not v.finished]
        if len(mine) >= MAX_SESSIONS_PER_USER:
            raise HTTPException(status_code=409, detail="live_session_limit")
    try:
        item = _Live(ctx.user_id)
    except Exception as exc:  # noqa: BLE001 VAD 模型缺失等：只影响实时转写
        raise HTTPException(status_code=503, detail=f"live_unavailable: {exc}") from exc
    live_id = uuid4().hex
    with _sessions_lock:
        _sessions[live_id] = item
    return {"id": live_id, "sample_rate": common.SAMPLE_RATE, "model_state": _warm["state"]}


@router.post("/{live_id}/pcm")
async def feed_pcm(live_id: str, request: Request, ctx: AuthContext = Depends(require_csrf)) -> dict:
    item = _find(ctx, live_id)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES or len(body) % 2:
        raise HTTPException(status_code=422, detail="bad_pcm")
    if item.finished:
        raise HTTPException(status_code=409, detail="live_session_finished")
    samples = np.frombuffer(body, dtype="<i2")
    if samples.size:
        with item.lock:
            item.session.feed(samples)
    return {"texts": item.collector.drain(), "model_state": _warm["state"]}


@router.post("/{live_id}/finish")
def finish(live_id: str, ctx: AuthContext = Depends(require_csrf)) -> dict:
    item = _find(ctx, live_id)
    if not item.finished:
        item.finished = True
        with item.lock:
            item.session.finish(timeout=FINISH_TIMEOUT_S)
    texts = item.collector.drain()
    with _sessions_lock:
        _sessions.pop(live_id, None)
    return {"texts": texts}
