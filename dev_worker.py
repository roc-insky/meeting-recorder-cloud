"""本机开发用的会后转写 worker：python dev_worker.py

生产部署没有批准引擎（deploy/cloud/model-manifest.json 仍是
blocked_no_approved_engine），所以 cloud/worker.py 的 main() 会在 warm_models()
处直接退出，云端也就永远没有会后文字稿。这个文件只在本机开发时补上这一环：

    python dev_worker.py            # 前台跑，Ctrl-C 结束
    （cloud_dev.py 默认会用后台线程起同一个循环，见 start_background）

它做三件事：
  1. 实现 cloud.models.ModelAdapter —— transcribe() 调本机 mlx-whisper
     （transcribe.transcribe_samples），embed() 一律抛 EnhancementError，
     因此不做声纹说话人归属，稿子会带 speaker_enhancement_failed 警告。
  2. 把 adapter 交给生产管线 cloud.transcripts.transcribe_task()：解码、去重、
     归属、发布、租约/代际校验全部复用，不另写一套。
  3. 用 cloud.worker.process_once() 轮询任务队列。

仅用于开发：生产开关（model-manifest、cloud.worker.main）一行未改。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from cloud.contracts import AuthContext, Segment, TranscriptView  # noqa: E402
from cloud.db import create_database_engine, make_session_factory  # noqa: E402
from cloud.models import EnhancementError, RecognitionParams  # noqa: E402
from cloud.objects import object_store_from_env  # noqa: E402
from cloud.settings import Settings  # noqa: E402
from cloud.transcripts import transcribe_task  # noqa: E402
from cloud.worker import WorkItem, process_once  # noqa: E402

WORKER_ID = "dev-worker"
POLL_SECONDS = float(os.environ.get("CLOUD_DEV_WORKER_POLL", "1.0"))

#: 开发参数：没有模型契约，显式标成未测量，避免被误当成已批准配置。
DEV_PARAMS = RecognitionParams(strategy=None, contract_status="not_measured")


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """读取 16-bit 单声道 wav 为 float32（prepare_audio 的输出固定是这个格式）。"""
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise ValueError("dev worker expects mono audio")
        if handle.getsampwidth() != 2:
            raise ValueError("dev worker expects 16-bit audio")
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return samples, rate


class DevWhisperAdapter:
    """mlx-whisper 的 ModelAdapter：只做语音识别，不做声纹。"""

    def __init__(self, meeting_id: str | None = None) -> None:
        self.meeting_id = meeting_id or "dev"

    def transcribe(self, audio, speakers: int | None) -> TranscriptView:  # noqa: ANN001
        import transcribe as local  # 延迟导入：没装 mlx 的环境也能 import dev_worker

        samples, rate = _read_wav(Path(audio.mix_path))
        if rate != audio.rate:
            raise ValueError(f"sample rate mismatch: {rate} != {audio.rate}")

        segments: list[Segment] = []
        for item in local.transcribe_samples(samples, model=local.DEFAULT_MODEL):
            text = str(item.get("text", "")).strip()
            start_ms = int(round(float(item.get("start", 0.0)) * 1000))
            end_ms = int(round(float(item.get("end", 0.0)) * 1000))
            if not text or end_ms <= start_ms:
                continue
            segments.append(
                Segment(
                    id=str(uuid.uuid4()),
                    start_ms=max(start_ms, 0),
                    end_ms=end_ms,
                    text=text,
                    track_id=None,
                )
            )
        segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
        return TranscriptView(
            id=str(uuid.uuid4()),
            meeting_id=self.meeting_id,
            segments=segments,
            speakers=[],
            warnings=[],
            empty=not segments,
        )

    def embed(self, samples):  # noqa: ANN001, ARG002
        # 没有声纹模型：让 transcribe_task 走"归属失败但保留文字稿"的分支。
        raise EnhancementError("dev_embedding_unsupported")


def make_processor(settings: Settings, session_factory, objects) -> "Callable[[WorkItem], TranscriptView]":  # noqa: ANN001, F821
    """返回 process_once 需要的 processor(item)。

    cloud.worker 的处理器契约是单参数（见 cloud/worker.py:252 run_forever），
    不传 AuthContext；这里自己开一个只读会话，把 items 的 owner 身份冻进上下文，
    否则 transcribe_task 拿不到 db/objects（objects 缺失会直接 503）。
    """

    def processor(item: WorkItem) -> TranscriptView:
        workspace = Path(tempfile.mkdtemp(prefix=f"dev-worker-{item.task_id[:8]}-"))
        db = session_factory()
        try:
            ctx = AuthContext(
                user_id=item.owner_user_id,
                session_id="",
                db=db,
                settings=settings,
                objects=objects,
            )
            try:
                return transcribe_task(
                    ctx,
                    item,
                    adapter=DevWhisperAdapter(item.meeting_id),
                    params=DEV_PARAMS,
                    workspace=workspace,
                )
            except Exception:  # noqa: BLE001 - dev 输出完整栈，生产 worker 只记 error_code
                traceback.print_exc()
                raise
        finally:
            db.close()
            shutil.rmtree(workspace, ignore_errors=True)

    return processor


def warm_up() -> None:
    """后台加载一次模型：首个任务的首次推理要 70 秒以上，容易撞上租约。"""
    try:
        import transcribe as local

        local.transcribe_samples(np.zeros(16000, dtype=np.float32), model=local.DEFAULT_MODEL)
        print("dev_worker_model_ready", flush=True)
    except Exception as exc:  # noqa: BLE001 - 预热失败不致命，首个任务会自己加载
        print(f"dev_worker_warmup_failed:{type(exc).__name__}", flush=True)


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    engine = create_database_engine(settings)
    factory = make_session_factory(engine)
    objects = object_store_from_env(os.environ)
    if objects is None:
        print("dev_worker_no_object_store: set CLOUD_OBJECTS_DIR", flush=True)
        return
    processor = make_processor(settings, factory, objects)
    print("dev_worker_started", flush=True)
    try:
        while True:
            db = factory()
            try:
                worked = process_once(
                    processor,
                    worker_id=WORKER_ID,
                    now=datetime.now(timezone.utc),
                    db=db,
                    settings=settings,
                )
            finally:
                db.close()
            if not worked:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        print("dev_worker_stopped", flush=True)
    finally:
        engine.dispose()


def start_background() -> threading.Thread:
    """给 cloud_dev.py 用：在守护线程里跑同一个循环，失败也不拖垮 API。"""
    def target() -> None:
        try:
            run_forever()
        except Exception as exc:  # noqa: BLE001 - dev 线程不许把进程带崩
            print(f"dev_worker_crashed:{type(exc).__name__}:{exc}", flush=True)

    threading.Thread(target=warm_up, name="dev-worker-warmup", daemon=True).start()
    thread = threading.Thread(target=target, name="dev-worker", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-warmup" not in argv:
        threading.Thread(target=warm_up, name="dev-worker-warmup", daemon=True).start()
    try:
        run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
