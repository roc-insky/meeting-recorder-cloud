"""本机开发入口：python cloud_dev.py

在 127.0.0.1 上启动云端 API（cloud.app.create_app），并同源托管浏览器前端 web/cloud/。
点击“开始录音”后由浏览器 getUserMedia/MediaRecorder 采集，分片上传到 /api/cloud/...，
不会打开本机麦克风（与 web.py 的本机录音控制台是两套程序）。

另外默认起一个开发用的会后转写 worker（dev_worker.py：本机 mlx-whisper），
录音封存后前端入队 tasks/final，worker 出稿并让工作台中栏显示文字稿。
CLOUD_DEV_WORKER=0 可关掉；生产 worker 与 deploy/cloud/model-manifest.json 不受影响。

仅用于开发：使用 fake_test 身份提供方、非 Secure Cookie；生产环境仍会被 Settings 拒绝启动。
数据库默认 postgresql+psycopg://mima0000@127.0.0.1:55432/cloud_test，可用 CLOUD_DATABASE_URL 覆盖。
"""
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WEB_DIR = PROJECT_DIR / "web" / "cloud"
DATA_DIR = PROJECT_DIR / ".cloud-dev"

DEFAULTS = {
    "CLOUD_DATABASE_URL": "postgresql+psycopg://mima0000@127.0.0.1:55432/cloud_test",
    "CLOUD_SECRET_KEY": "dev-only-secret-key-not-for-production-0123456789",
    "CLOUD_ENVIRONMENT": "development",
    "CLOUD_AUTH_PROVIDER": "fake_test",
    "CLOUD_COOKIE_SECURE": "false",     # http://127.0.0.1 上 Secure Cookie 不会被浏览器保存
    "CLOUD_HOST": "127.0.0.1",
    "CLOUD_PORT": "8300",
    "CLOUD_OBJECTS_DIR": str(DATA_DIR / "objects"),
    "CLOUD_PRIVACY_AUDIT_PATH": str(DATA_DIR / "privacy-audit.jsonl"),
}


def build_app():
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    DATA_DIR.mkdir(mode=0o750, exist_ok=True)

    from fastapi.staticfiles import StaticFiles

    from cloud.app import create_app
    from cloud.settings import Settings

    app = create_app(Settings.from_env())
    # 实时转写（仅开发）：浏览器把 PCM 发到 /api/dev-live/*，用本机 VAD + mlx-whisper 转写。
    # 必须在静态挂载之前注册；生产 cloud/ 不导入 dev_live。
    sys.path.insert(0, str(PROJECT_DIR))
    import dev_live

    app.include_router(dev_live.router)
    dev_live.warm_up_async()
    # 会后转写（仅开发）：本机 mlx-whisper 当 adapter 轮询任务队列，封存并入队后出稿。
    # CLOUD_DEV_WORKER=0 可关掉。生产 worker（cloud/worker.py）与 model-manifest 未改动。
    if os.environ.get("CLOUD_DEV_WORKER", "1") != "0":
        import dev_worker

        dev_worker.start_background()
    # 必须在所有 /api/cloud 路由之后挂载：路由先匹配，其余路径落到静态前端。
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="cloud-web")
    return app


def main() -> int:
    import uvicorn

    app = build_app()
    settings = app.state.settings
    print(f"云端会议工作台（浏览器录音）：http://{settings.host}:{settings.port}/")
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
