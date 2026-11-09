# meeting-recorder

本地会议录音、转写与说话人区分工具。全部在本机运行。

## 安装

需要 Python 3.13 和 [uv](https://github.com/astral-sh/uv)。

```bash
uv venv --python /opt/homebrew/bin/python3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

测试不联网，不加载真实模型，不使用真实麦克风。

## 可行性检查

```bash
.venv/bin/python spike/feasibility.py
```

会下载声纹与分割模型到临时目录，确认声纹向量为 512 维。

## 实时转写

```bash
.venv/bin/python record.py --live                      # 边录边转写，每句输出 [HH:MM:SS] 文字
.venv/bin/python record.py --live --live-model REPO    # 指定实时转写模型（默认同转写默认模型）
```

- 每句话同时显示在终端并追加到会议目录的 `live.txt`；停止录音后仍会照常生成完整的 `transcript.md`。
- 首次使用会下载约 644 KB 的 VAD 模型（silero_vad.onnx）到 `~/.cache/meeting-recorder/models`；下载失败只警告，录音照常进行，只是没有实时转写。
- 实时转写出错或跟不上时只警告（跟不上时丢弃较早的句子），绝不影响录音；停止时最多等 10 秒收尾。

## Web 控制台后端

```bash
.venv/bin/python web.py        # 浏览器打开 http://127.0.0.1:8765/
```

- 端口取项目根目录 `config.toml` 的 `[web] port`（缺省 8765，文件不存在也能启动）；**只监听 127.0.0.1**，并校验 Host / Origin，不开启 CORS。
- 前端页面由 Task 9 实现，目前 `/` 只是占位页。
- 路由摘要：
  - `GET /api/state`（状态快照 + `voiceprint_busy`）、`GET /api/events`（SSE，先发快照再发增量，支持 `Last-Event-ID` / `?last_seq=`）
  - `POST /api/recording/start`（`{speakers?, live?}`）、`POST /api/recording/stop`
  - `GET /api/meetings`、`GET|PATCH|DELETE /api/meetings/{id}`、`GET|POST /api/meetings/{id}/annotations`
  - `GET /api/voiceprints`、`DELETE /api/voiceprints/{name}`、`POST /api/voiceprints/jobs`、`GET /api/voiceprints/jobs/{job_id}`、`POST /api/voiceprints/jobs/{job_id}/cancel`
- 变更类请求（POST/PUT/PATCH/DELETE）必须带 `Origin: http://127.0.0.1:端口`，JSON 体必须是 `application/json` 且不超过 64KB；录音/转写中的会议不能删除或改名（409）。
- 按 Ctrl-C 退出时，打开着的 SSE 连接最多拖住 3 秒，随后停止录音并等待后台线程收尾。

## 大模型功能

在项目根目录 `config.toml` 里填写（未配置时相关接口返回 503，其余功能不受影响）：

```toml
[llm]
base_url = "https://api.example.com/v1"   # OpenAI 兼容接口，实际请求 {base_url}/chat/completions
api_key = "sk-..."
model = "模型名"
timeout_s = 120            # 可选，默认 120
max_input_chars = 60000    # 可选，默认 60000，发言超过则拒绝（413）
```

- **发言归纳**：`GET /api/meetings/{id}/speakers` 列出说话人；`POST /api/meetings/{id}/summary`（可选 `{speakers: [...]}`）按 摘要/要点/待办/决议 生成纪要，返回 `{summary}`。
- **面试问题**：职位库 `GET|POST /api/jobs`、`PUT|DELETE /api/jobs/{id}`（保存在 `config.toml` 同目录的 `jobs.json`）；`POST /api/jobs/{id}/questions`（`{resume_text?, images?}`，简历文字 ≤20000 字，图片 ≤5 张、单张 ≤5MB，仅 png/jpeg/webp/gif 的 data URI）生成约 40 分钟的面试题（专业能力 20 / AI 应用 12 / 开放性 8 分钟，每题附答案要点）。**简历与生成结果不落盘。**
- 安全：不跟随重定向；错误只返回固定中文消息（502），不含服务器原文和 Key。

## 目录说明

- `common.py`：公共常量与工具函数。
- `record.py`：录音（边录边落盘）、m4a 压缩、转写；`--live` 边录边转写（见下）。退出码：0 成功，1 录音失败，2 录音已保存但转写失败。
- `live.py`：实时转写（Silero VAD 切句 + 后台逐句转写 + live.txt）。
- `transcribe.py`：转写核心与文字稿渲染。
- `tools/`：离线评估工具。
- `web.py` / `web_server.py`：Web 控制台入口与 FastAPI 应用；`web_config.py`、`meeting_store.py`、`meeting_controller.py`、`voiceprint_service.py` 为其后端模块。
- `llm_client.py` / `meeting_summary.py` / `interview_store.py` / `interview_questions.py`：大模型调用、发言归纳、面试职位库与面试题提示词。
- `web/static/`：前端静态文件（Task 9）。
- `tests/`：单元测试。
- `spike/`：可行性验证脚本。
- `docs/`：设计与计划文档。

## 验收记录

- Task 3 真实麦克风冒烟：未测
- Task 6 实时转写真实试跑：未测
- Task 7 Web 后端：真实浏览器/麦克风试跑：未测
- Task 8 大模型功能：真实大模型调用未测
