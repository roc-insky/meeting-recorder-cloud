"""Web 控制台入口：python web.py（只监听 127.0.0.1，端口取 config.toml 的 [web] port，默认 8765）。

导入本模块不会启动任何东西；真实对象只在 build_app / main 里创建。
注：不预热实时转写的 VAD 模型——首次勾选实时转写时才检查/下载，失败只警告、录音照常；
    如需预热，可在这里调用 live.ensure_vad_model(diarize.MODEL_DIR)。
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
    controller = meeting_controller.MeetingController(
        root,
        record_fn=meeting_controller.make_record_fn(),
        transcribe_fn=meeting_controller.make_transcribe_fn(
            deps={"stderr": sys.stderr}, config_path=config_path),
        live_factory=meeting_controller.build_live_factory(deps={"stderr": sys.stderr}, stderr=sys.stderr),
    )
    service = voiceprint_service.VoiceprintService()
    return web_server.create_app(controller, root=root, config_path=config_path, voiceprint_service=service)


def main(argv=None) -> int:
    port = web_config.load_port(CONFIG_PATH)
    app = build_app()
    print(f"会议录音控制台：http://{HOST}:{port}/（仅本机可访问，按 Ctrl-C 退出）")
    uvicorn.run(app, host=HOST, port=port, workers=1, reload=False,
                timeout_graceful_shutdown=SHUTDOWN_GRACE_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
