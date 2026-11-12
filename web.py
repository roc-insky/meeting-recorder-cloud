"""Web 控制台入口：python web.py（只监听 127.0.0.1，端口取 config.toml 的 [web] port，默认 8765）。

导入本模块不会启动任何东西；真实对象只在 build_app / main 里创建。
注：main() 启动时会在后台预热实时转写（检查/下载 VAD 模型 + 加载 ASR 模型）；
    mlx-whisper 首次加载权重的冷启动约 1 分钟，不预热的话录音开始后的第一句字幕会迟到甚至丢失。
"""
import sys
from pathlib import Path

import uvicorn

import common
import meeting_controller
import voiceprint_service
import web_config
import web_server

HOST = "127.0.0.1"
SHUTDOWN_GRACE_S = 3      # Ctrl-C 后最多等多久；SSE 是长连接，超时会被取消，随后才会关闭控制器
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.toml"      # 不存在也能启动，全部用默认值


def build_app(*, config_path=CONFIG_PATH, root=None):
    """按真实实现装配控制器、声纹服务与 FastAPI 应用（不打开麦克风、不加载模型）。"""
    root = Path(root) if root is not None else common.MEETINGS_ROOT
    live_deps = {"stderr": sys.stderr}
    controller = meeting_controller.MeetingController(
        root,
        record_fn=meeting_controller.make_record_fn(),
        transcribe_fn=meeting_controller.make_transcribe_fn(
            deps={"stderr": sys.stderr}, config_path=config_path),
        live_factory=meeting_controller.build_live_factory(deps=live_deps, stderr=sys.stderr),
        live_warmup=meeting_controller.build_live_warmup(deps=live_deps, stderr=sys.stderr),
    )
    service = voiceprint_service.VoiceprintService()
    return web_server.create_app(controller, root=root, config_path=config_path, voiceprint_service=service)


def main(argv=None) -> int:
    port = web_config.load_port(CONFIG_PATH)
    app = build_app()
    print(f"会议录音控制台：http://{HOST}:{port}/（仅本机可访问，按 Ctrl-C 退出）")
    controller = getattr(getattr(app, "state", None), "controller", None)
    if controller is not None and controller.warm_live():
        print("正在后台预热实时转写：首次需加载模型，约 1 分钟；期间可以正常录音，字幕稍后出现。")
    uvicorn.run(app, host=HOST, port=port, workers=1, reload=False,
                timeout_graceful_shutdown=SHUTDOWN_GRACE_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
