"""Web 控制台后端：FastAPI 应用（仅限本机访问）。

装配：create_app(controller, root=, config_path=, voiceprint_service=) 内部会调用
meeting_controller.link_exclusive(controller, voiceprint_service)（幂等，只是重新设置互斥回调），
所以调用方不必再自己连一次；应用关闭（lifespan 结束）时会先唤醒所有 SSE 连接，
再 controller.close() 与 voiceprint_service.close()。

安全守卫（全部在 GuardMiddleware 里，纯 ASGI 实现）：
  - 所有请求：Host 必须是 127.0.0.1 或 localhost（可带服务端口），防 DNS rebinding；
  - POST/PATCH/DELETE：必须带 Origin，且为 http://127.0.0.1:端口 或 http://localhost:端口，否则 403；
  - 带请求体时 Content-Type 必须是 application/json（415），体积 ≤ 64KB（413）；
  - 不开启 CORS；/ 和 /static/* 带 Cache-Control: no-cache，/api/* 带 no-store。

错误统一为 {"error": "中文消息"}：Busy→409，找不到→404，ValueError/校验错误→400，
录音没能开始→503，其余未预期异常→500（只回通用消息，详情进服务端日志）。
"""
import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit  # noqa: F401  （保留：Origin 解析以字符串精确比较为主）

import anyio.to_thread
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

import meeting_store
import voiceprint
import web_config
from meeting_controller import Busy, MeetingStartError, link_exclusive
from meeting_store import MeetingNotFound
from voiceprint_service import JobNotFound

log = logging.getLogger("web_server")

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
MAX_BODY_BYTES = 64 * 1024
MUTATING_METHODS = frozenset({"POST", "PATCH", "DELETE"})
HEARTBEAT_S = 15.0          # SSE 空闲多久发一次注释心跳
TICK_S = 0.25               # subscribe 每隔多久回来检查一次断开/关闭
LEVEL_INTERVAL_S = 0.25     # level 事件最短间隔（约 4 次/秒）
RETRY_MS = 2000             # 告诉浏览器断线后多久重连

_CJK = re.compile(r"[\u4e00-\u9fff]")
_DIGITS = re.compile(r"[0-9]+")


class ApiError(Exception):
    """可预期的请求错误，直接映射为 {"error": message} 和对应状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


# ---------- 安全守卫（纯 ASGI 中间件） ----------
class GuardMiddleware:
    """Host / Origin / Content-Type / 体积守卫，并统一设置缓存头。"""

    def __init__(self, app, *, port: int):
        self.app = app
        self._hosts = {"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or hosts[0].lower() not in self._hosts:
            await _error(403, "不允许的访问地址")(scope, receive, send)
            return
        if scope["method"] in MUTATING_METHODS:
            origins = headers.getlist("origin")
            if len(origins) != 1 or origins[0].lower() not in self._origins:
                await _error(403, "来源不被允许")(scope, receive, send)
                return
            rejection, receive = await self._check_body(headers, receive)
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        await self.app(scope, receive, self._with_cache_headers(scope["path"], send))

    @staticmethod
    async def _check_body(headers, receive):
        """有请求体时检查 Content-Type 与体积；返回 (拒绝响应或 None, 可能被替换的 receive)。"""
        length = headers.get("content-length")
        chunked = "transfer-encoding" in headers
        if length is not None:
            if not _DIGITS.fullmatch(length.strip()):
                return _error(400, "请求头无效"), receive
            length = int(length)
        if not chunked and not length:
            return None, receive                      # 没有请求体（如 stop）不要求 Content-Type
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return _error(415, "请求内容必须是 application/json"), receive
        if length is not None and length > MAX_BODY_BYTES:
            return _error(413, "请求内容太大"), receive
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return _error(400, "连接已断开"), receive
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                return _error(413, "请求内容太大"), receive
            if not message.get("more_body", False):
                break
        data = bytes(body)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()                   # 之后透传（断开通知）
        return None, replay

    @staticmethod
    def _with_cache_headers(path: str, send):
        if path == "/" or path.startswith("/static/"):
            value = "no-cache"
        elif path.startswith("/api/") and path != "/api/events":
            value = "no-store"
        else:
            return send

        async def wrapped(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = value
            await send(message)
        return wrapped


# ---------- SSE ----------
class LevelThrottle:
    """level 事件限速：每 interval 秒最多放行一条，期间只保留最新一条；其他事件直接放行。"""

    def __init__(self, interval: float = LEVEL_INTERVAL_S):
        self._interval = interval
        self._last_sent = None
        self._pending = None

    def _due(self, now: float) -> bool:
        return self._last_sent is None or now - self._last_sent >= self._interval

    def offer(self, ev: dict, now: float) -> list:
        """收到一条事件，返回现在应当发出的事件列表。"""
        if ev.get("type") != "level":
            self._pending = None                     # 过期电平不能排在更新的事件之后发出（id 须单调）
            return [ev]
        if self._due(now):
            self._pending = None
            self._last_sent = now
            return [ev]
        self._pending = ev
        return []

    def flush(self, now: float) -> list:
        """到点了就放出被压住的最新电平。"""
        if self._pending is not None and self._due(now):
            ev, self._pending = self._pending, None
            self._last_sent = now
            return [ev]
        return []


def format_event(ev: dict) -> str:
    """把事件格式化成 SSE 文本；id 为事件 seq。"""
    data = ev["data"]
    if ev.get("resync"):
        data = dict(data, resync=True)
    return f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_END = object()


def _next_or_end(gen):
    try:
        return next(gen)
    except StopIteration:
        return _END


async def event_stream(controller, *, last_seq=None, stop=None, is_disconnected=None,
                       heartbeat_s: float = HEARTBEAT_S, tick_s: float = TICK_S,
                       level_interval: float = LEVEL_INTERVAL_S, clock=time.monotonic):
    """SSE 字节流：先快照再增量；level 限速；空闲发心跳；客户端断开、stop 置位或控制器关闭时结束。

    controller.subscribe 是阻塞生成器，放到线程里逐条取，不堵事件循环。
    """
    stop = stop if stop is not None else threading.Event()
    gen = controller.subscribe(last_seq=last_seq, stop=stop, timeout=tick_s)
    throttle = LevelThrottle(level_interval)
    loop = asyncio.get_running_loop()
    last_out = clock()
    retry_sent = False
    try:
        while not stop.is_set():
            if is_disconnected is not None and await is_disconnected():
                break
            item = await loop.run_in_executor(None, _next_or_end, gen)
            if item is _END:
                break
            now = clock()
            out = throttle.offer(item, now) if item is not None else []
            out += throttle.flush(now)
            for ev in out:
                text = format_event(ev)
                if not retry_sent:
                    retry_sent = True
                    text = f"retry: {RETRY_MS}\n" + text
                yield text.encode("utf-8")
                last_out = now
            if not out and now - last_out >= heartbeat_s:
                yield b": heartbeat\n\n"
                last_out = now
    finally:
        stop.set()
        with contextlib.suppress(ValueError):        # 生成器正在线程里运行时不能 close，它会自己因 stop 退出
            gen.close()


def _parse_last_seq(value):
    """Last-Event-ID / last_seq：非负整数文本才算数，其余当作没有（回到快照）。"""
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        return int(value.strip())
    return None


# ---------- 请求体解析 ----------
def _reject_constant(name):
    raise ValueError(name)


async def _read_json(request: Request, *, required: bool) -> dict:
    raw = await request.body()
    if not raw.strip():
        if required:
            raise ApiError(400, "请求内容不能为空")
        return {}
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "请求内容不是有效的 JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "请求内容必须是 JSON 对象")
    return data


async def optional_body(request: Request) -> dict:
    return await _read_json(request, required=False)


async def required_body(request: Request) -> dict:
    return await _read_json(request, required=True)


def _check_keys(body: dict, allowed) -> None:
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise ApiError(400, f"不支持的字段：{'、'.join(map(str, extra))}")


def _read_text(meeting_dir: Path, name: str):
    """读会议目录里的文本文件；不存在、符号链接或读失败返回 None（避免经链接读到目录外）。"""
    path = meeting_dir / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---------- 应用 ----------
def create_app(controller, *, root, config_path, voiceprint_service, port: int | None = None) -> FastAPI:
    """组装 FastAPI 应用；port 缺省取 config.toml 的 [web] port（默认 8765）。"""
    root = Path(root)
    port = port if port is not None else web_config.load_port(config_path)
    link_exclusive(controller, voiceprint_service)
    sse_stops: set = set()
    sse_lock = threading.Lock()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        with sse_lock:                               # 先让所有 SSE 连接收尾，再停控制器
            for evt in list(sse_stops):
                evt.set()
        await anyio.to_thread.run_sync(controller.close)
        await anyio.to_thread.run_sync(voiceprint_service.close)

    app = FastAPI(title="会议录音", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.router.redirect_slashes = False              # 否则 /api/meetings/. 之类会被 307 重定向到列表
    app.state.controller = controller
    app.state.voiceprint_service = voiceprint_service
    app.add_middleware(GuardMiddleware, port=port)

    # ---- 错误处理 ----
    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return _error(exc.status, exc.message)

    @app.exception_handler(Busy)
    async def _busy(_req, exc):
        return _error(409, str(exc) or "资源正忙，请稍后再试")

    @app.exception_handler(MeetingNotFound)
    async def _no_meeting(_req, _exc):
        return _error(404, "会议不存在")

    @app.exception_handler(JobNotFound)
    async def _no_job(_req, _exc):
        return _error(404, "任务不存在")

    @app.exception_handler(MeetingStartError)
    async def _start_error(_req, exc):
        return _error(503, str(exc) or "录音没能开始")

    @app.exception_handler(ValueError)
    async def _value_error(_req, exc):
        msg = str(exc)
        return _error(400, msg if _CJK.search(msg) else "请求参数无效")   # 非中文消息多半是内部细节，不外露

    @app.exception_handler(RequestValidationError)
    async def _validation(_req, _exc):
        return _error(400, "请求参数无效")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req, exc):
        messages = {404: "未找到", 405: "不支持的请求方法"}
        return _error(exc.status_code, messages.get(exc.status_code, "请求失败"),
                      headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(req, exc):
        log.error("未处理的异常：%s %s", req.method, req.url.path, exc_info=exc)
        return _error(500, "服务器内部错误")

    def meeting_dir(meeting_id: str) -> Path:
        return meeting_store.resolve(root, meeting_id)    # 先 resolve，再碰任何文件

    def idle_dir(meeting_id: str) -> Path:
        d = meeting_dir(meeting_id)
        if controller.is_active(d.name):
            raise ApiError(409, "该会议正在录音或转写，请稍后再试")
        return d

    # ---- 页面与静态资源 ----
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    # ---- 状态与事件 ----
    @app.get("/api/state")
    def state():
        return dict(controller.snapshot(), voiceprint_busy=bool(voiceprint_service.is_busy()))

    @app.get("/api/events")
    async def events(request: Request, last_seq: str | None = Query(default=None)):
        header = request.headers.get("last-event-id")
        stop = threading.Event()
        with sse_lock:
            sse_stops.add(stop)

        async def body():
            try:
                async for chunk in event_stream(
                        controller, last_seq=_parse_last_seq(header if header is not None else last_seq),
                        stop=stop, is_disconnected=request.is_disconnected):
                    yield chunk
            finally:
                with sse_lock:
                    sse_stops.discard(stop)
        return StreamingResponse(body(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 录音 ----
    @app.post("/api/recording/start")
    def recording_start(body: dict = Depends(optional_body)):
        _check_keys(body, ("speakers", "live"))
        return {"meeting_id": controller.start(**body)}   # 严格类型由控制器校验，不做转换

    @app.post("/api/recording/stop")
    def recording_stop():
        controller.stop()
        return controller.snapshot()

    # ---- 会议 ----
    @app.get("/api/meetings")
    def meetings_list():
        return {"meetings": meeting_store.list_meetings(root)}

    @app.get("/api/meetings/{meeting_id}")
    def meeting_detail(meeting_id: str):
        d = meeting_dir(meeting_id)
        transcript = _read_text(d, "transcript.md")
        return {
            "id": d.name,
            "title": meeting_store.get_title(d),
            "transcript": transcript,
            "live": _read_text(d, "live.txt"),
            "speakers": meeting_store.parse_header(transcript or "")["speakers"],
            "annotations": meeting_store.list_annotations(d),
            "active": bool(controller.is_active(d.name)),
        }

    @app.patch("/api/meetings/{meeting_id}")
    def meeting_rename(meeting_id: str, body: dict = Depends(required_body)):
        _check_keys(body, ("title",))
        if "title" not in body:
            raise ApiError(400, "缺少标题")
        d = idle_dir(meeting_id)
        meeting_store.set_title(d, body["title"])
        return {"id": d.name, "title": meeting_store.get_title(d)}

    @app.delete("/api/meetings/{meeting_id}")
    def meeting_delete(meeting_id: str):
        d = idle_dir(meeting_id)
        meeting_store.delete_meeting(root, d.name)
        return {"ok": True}

    @app.get("/api/meetings/{meeting_id}/annotations")
    def annotations_list(meeting_id: str):
        return {"annotations": meeting_store.list_annotations(meeting_dir(meeting_id))}

    @app.post("/api/meetings/{meeting_id}/annotations", status_code=201)
    def annotations_add(meeting_id: str, body: dict = Depends(required_body)):
        _check_keys(body, ("kind", "t", "text"))
        missing = [k for k in ("kind", "t", "text") if k not in body]
        if missing:
            raise ApiError(400, f"缺少字段：{'、'.join(missing)}")
        d = meeting_dir(meeting_id)                  # 录音中也允许加标注
        return meeting_store.add_annotation(d, body["kind"], body["t"], body["text"])

    # ---- 声纹 ----
    @app.get("/api/voiceprints")
    def voiceprints_list():
        return {"voiceprints": voiceprint.list_profiles()}

    @app.delete("/api/voiceprints/{name}")
    def voiceprints_delete(name: str):
        if not voiceprint.delete_profile(name):
            raise ApiError(404, "声纹不存在")
        return {"ok": True}

    @app.post("/api/voiceprints/jobs", status_code=202)
    def voiceprint_job_start(body: dict = Depends(required_body)):
        _check_keys(body, ("kind", "name", "seconds", "path", "replace"))
        kind = body.get("kind")
        replace = body.get("replace", False)
        if kind == "record":
            job_id = voiceprint_service.start_record(body.get("name"), body.get("seconds"), replace)
        elif kind == "import":
            job_id = voiceprint_service.start_import(body.get("name"), body.get("path"), replace)
        else:
            raise ApiError(400, "kind 必须是 record 或 import")
        return {"job_id": job_id}

    @app.get("/api/voiceprints/jobs/{job_id}")
    def voiceprint_job_status(job_id: str):
        return voiceprint_service.status(job_id)

    @app.post("/api/voiceprints/jobs/{job_id}/cancel")
    def voiceprint_job_cancel(job_id: str):
        return {"cancelled": voiceprint_service.cancel(job_id)}

    return app
