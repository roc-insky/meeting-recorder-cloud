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

## 命令行用法

```bash
.venv/bin/python record.py                  # 录音，Ctrl-C 停止后压缩 m4a 并转写，写出 transcript.md
.venv/bin/python record.py --live           # 同时边录边转写（见下一节）
.venv/bin/python enroll.py --help           # 命令行登记声纹
```

## Web 使用

```bash
.venv/bin/python web.py        # 浏览器打开 http://127.0.0.1:8765/
```

- 在 Chrome / Safari / Edge / Firefox 的较新版本打开 `http://127.0.0.1:8765/`（须用 `127.0.0.1` 或 `localhost`，其他主机名会被拒绝）；首次录音时 macOS 会请求麦克风权限。
- 三个页面（标签页顺序固定）：
  - **当前会议**：左栏历史会议（点标题查看，可编辑标题、删除，删除前会确认）；中栏计时与转写（可填“当前会议人数”1–20，可勾选实时转写；点击转写行可把时间填进标注框，带标注的行末有标记，点标记或右栏标注可互相定位）；右栏添加标注（📝笔记 / ⭐重点 / 💬注释）和“发言归纳”（选说话人，需配置 `[llm]`）。三栏在宽屏（≥1041px）下各自独立滚动。开始新录音会清掉上一场选中的行和转写残留。
  - **面试帮助**：维护职位（前端 / 后端 / 客户端 / 测试），粘贴简历文字或截图（也可选择图片文件，最多 5 张、每张 ≤5MB），生成约 40 分钟的面试题；结果用 marked 渲染并经 DOMPurify 白名单净化后显示，简历与结果都不保存。
  - **声纹登记**：只需填写姓名；如要覆盖同名声纹须勾选“明确替换同名声纹”。点“现场录制”后弹出朗读文案，登记结束自动关闭；点“取消录制”、关闭弹窗或按 Esc 会取消本次录制。
- 前端是原生 JS，无框架、无构建、无 CDN；所有文本经 `textContent` 写入，`innerHTML` 只出现在 `web/static/interview.js` 且只接受净化后的内容。

### 随仓库的第三方库

| 文件 | 版本 | 来源 |
| --- | --- | --- |
| `web/static/marked.min.js` | marked 18.1.2（UMD，`lib/marked.umd.js`） | `npm pack marked@18.1.2` |
| `web/static/purify.min.js` | DOMPurify 3.4.17（`dist/purify.min.js`） | `npm pack dompurify@3.4.17` |

两个版本在 npm registry 均存在，已按指定版本取用（无需 Ruling 回退）。仅删除了文件末尾的 `sourceMappingURL` 注释行（仓库里没有 .map 文件），其余原样。升级时重新 `npm pack` 并同步更新上表与 `tests/test_web_ui.py` 里的版本号。

### config.toml 示例

放在项目根目录（可不存在，全部取默认值）：

```toml
[web]
port = 8765                # 默认 8765；只监听 127.0.0.1

[llm]                      # 发言归纳与面试题需要；不配置则这两个功能返回 503，其余不受影响
base_url = "https://api.example.com/v1"
api_key = "sk-..."
model = "模型名"
timeout_s = 120            # 可选
max_input_chars = 60000    # 可选
```

## Web 控制台后端

- 端口取项目根目录 `config.toml` 的 `[web] port`（缺省 8765，文件不存在也能启动）；**只监听 127.0.0.1**，并校验 Host / Origin，不开启 CORS。
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
- `web/static/`：前端静态文件（`index.html`、`style.css`、`app.js`、`interview.js`，以及随仓库的 `marked.min.js`、`purify.min.js`）。
- `tests/`：单元测试。
- `spike/`：可行性验证脚本。
- `docs/`：设计与计划文档。

## 验收记录

- Task 3 真实麦克风冒烟：未测
- Task 6 实时转写真实试跑：未测
- Task 7 Web 后端：真实浏览器/麦克风试跑：未测
- Task 8 大模型功能：真实大模型调用未测
- Task 9 前端（以下均需真实浏览器/麦克风，执行者无法运行）：
  - 真实麦克风录音：未测
  - 浏览器端到端（开始 → 说话 → 停止 → 转写 → 历史出现）：未测
  - 标注添加与点击定位：未测
  - 转写文本含 `<script>alert(1)</script>` 的会议不弹窗：未测
  - 声纹现场录制弹窗（朗读文案、结束自动关闭、关闭/Esc 取消本次录制）：未测
  - 面试截图粘贴/上传与面试题生成：未测
  - 实时转写页面显示、SSE 断线重连：未测
  - 发言归纳页面（真实大模型）：未测
- Task 9 离线冒烟：通过（离线冒烟）——在子进程里用 uvicorn 启动 `create_app`（假录音/假转写控制器、临时目录），用 urllib 取 `/`、`/static/app.js`、`interview.js`、`style.css`、`marked.min.js`、`purify.min.js` 均 200 且类型正确，`/` 含三个 Tab；并走通 start → stop → final=completed → 会议列表 → 添加标注（含 `<script>`，原样存储）→ 关闭。另用 Node 伪 DOM 加载 `app.js`/`interview.js`：初始化不抛异常，并发起 `/api/state`、`/api/meetings`、`/api/events`；含 `<script>` 的转写行文本原样进入 textContent。这**不等于**真实浏览器验收。
