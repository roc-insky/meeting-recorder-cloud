# meeting-recorder 总计划（Implementation Plan，合并版）

For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**: 在 macOS 上用命令行或本地 Web 控制台录下麦克风音频，在本机转成带时间戳的中文文字稿；支持会后说话人区分与声纹识别（标真名）、录音时实时转写、会议标注、发言归纳（大模型）和面试问题推荐。音频与声纹始终留在本机。

**Architecture**: 纯函数与外部依赖分离，所有外部依赖（麦克风、模型、`afconvert`、磁盘、网络、大模型）都通过参数注入，测试用假对象。核心层：`common.py`（纯函数）、`record.py`（录音）、`transcribe.py`（Whisper 转写与渲染）、`diarize.py`（说话人区分）、`live.py`（实时转写）、`voiceprint.py` / `enroll.py`（声纹）。Web 层：FastAPI 单进程（`web_server.py`、`meeting_controller.py`、`meeting_store.py`、`voiceprint_service.py`、`meeting_summary.py`、`llm_client.py`、`interview_store.py`、`interview_questions.py`）加原生 JS 前端（`web/static/`），只绑定 `127.0.0.1`。任何增强能力（说话人区分、声纹、实时转写、大模型）失败都不得影响录音与基础转写。

**Tech Stack**: Python 3.14，`sounddevice`，`numpy`，`mlx-whisper`，`sherpa-onnx`，FastAPI/Uvicorn，stdlib（`unittest`、`urllib`、`tomllib`、`threading`），原生 JS（无构建、无 CDN），marked + DOMPurify（随仓库提交的固定版本压缩文件），macOS 自带 `afconvert`。

**Specs**:
- docs/superpowers/specs/2026-10-19-meeting-recorder-design.md
- docs/superpowers/specs/2026-10-20-speaker-diarization-design.md
- docs/superpowers/specs/2026-10-22-live-transcription-design.md
- docs/superpowers/specs/2026-10-23-voiceprint-design.md
- docs/superpowers/specs/2026-10-25-local-web-meeting-console-design.md
- docs/superpowers/specs/2026-10-29-interview-helper-design.md

## 总览（合并说明）

本文件已合并 docs/superpowers/plans/ 下全部其它 plan 的内容：需求、架构、约束、Review Focus、任务拆分与接口、决策、改动文件、验证结果。每个阶段保留可执行的任务清单和接口约定，已被后续改动取代的细节在对应阶段里标注"已被取代"。原分阶段 plan 文件仍保留在同目录，作为逐步骤的详细历史记录；新的改动按 AGENTS.md 追加到本文件末尾的"变更记录"，或新建 YYYY-MM-DD-<主题>.md 后在此处登记。

### 阶段索引与状态

| 阶段 | 日期 | 主题 | 原 plan 文件 | 状态 |
|---|---|---|---|---|
| 1 | 2026-10-19 | 基础录音与转写 | 本文件（阶段 1 小节） | 已实现 |
| 2 | 2026-10-20 | 说话人区分 | 2026-10-20-speaker-diarization.md | 已实现；Task 5 真实会议效果评估需用户反馈 |
| 3 | 2026-10-22 | 实时转写（`record.py --live`） | 2026-10-22-live-transcription.md | 已实现 |
| 4 | 2026-10-24 | 声纹登记与识别 | 2026-10-24-voiceprint.md | Task 1–7 已实现；**Task 8 校准未完成**（`voiceprint.DEFAULT_THRESHOLD = 0.5` 仍标"未校准"） |
| 5 | 2026-10-25 | 本地 Web 会议控制台 | 2026-10-25-local-web-meeting-console.md | 已实现 |
| 6 | 2026-10-26 | 历史会议删除与转写排版 | 2026-10-26-history-delete-and-transcript-layout.md | 已实现 |
| 7 | 2026-10-26 | 状态颜色、当前会议人数、历史重命名 | 2026-10-26-status-colors-speaker-count-rename.md | 已实现 |
| 8 | 2026-10-26 | 三栏滚动、类型图标、对话标记 | 2026-10-26-three-column-scroll-kind-icons-transcript-marks.md | 已实现 |
| 9 | 2026-10-26 | 声纹登记独立 Tab 与朗读文案弹窗 | 2026-10-26-voiceprint-tab-and-reading-dialog.md | 已实现 |
| 10 | 2026-10-26 | 发言归纳（大模型） | 2026-10-26-speaker-summary.md | 已实现 |
| 11 | 2026-10-28 | 开始录音重置界面、移除 initial_prompt、移除同意勾选 | 2026-10-28-recording-start-reset-and-drop-initial-prompt.md、2026-10-28-remove-voiceprint-consent-checkbox.md | 已完成 |
| 12 | 2026-10-29 | 面试帮助 Tab（含 Markdown 渲染、缓存修复） | 2026-10-29-interview-helper-tab.md、2026-10-29-interview-markdown-render.md | 已实现 |
| 13 | 2026-10-30 | 打开麦克风加超时 | 2026-10-30-open-microphone-timeout.md | 已实现 |
| 14 | 2026-11-01 | 面试问题 40 分钟 + 答案要点 | 2026-11-01-interview-40min-answer-points.md | 已实现 |
| 15 | 2026-11-09 | 可信度优先 | 2026-11-09-reliability-first.md | 已实现（Task 11 待数据） |

### 当前累计约束（所有阶段合并后仍然有效）

- 工作目录：`/Users/roc.peng/Documents/Python/meeting-recorder`，路径相对此目录。测试命令：`.venv/bin/python -m unittest discover -s tests -t . -v`。测试不联网、不加载真实模型、不用真实麦克风；任何测试都不得读写真实的 `~/.config/meeting-recorder/`（用 `mock.patch.object(voiceprint, "PROFILE_DIR", tmp)` 覆盖）。
- 录音格式 16 kHz 单声道 16 位 wav；转写模型默认 `mlx-community/whisper-large-v3-turbo`，语言 `zh`，固定 `condition_on_previous_text=False`，**不传 `initial_prompt`**（阶段 11 移除）。
- 输出根目录 `~/Meetings`，会议目录 `YYYY-MM-DD_HHMM`（重名加 `_2`、`_3`），文件 `meeting.wav`、`meeting.m4a`、`transcript.md`、`live.txt`（开 `--live` 时）、`annotations.json`、`meta.json`（自定义标题）。
- 模型缓存 `~/.cache/meeting-recorder/models/`；声纹 `~/.config/meeting-recorder/voiceprints/`（JSON，权限 600，只存 512 维向量）；职位 `~/.config/meeting-recorder/jobs.json`。
- 说话人区分默认开（`--speakers N`、`--no-diarize` 互斥）；声纹识别默认开（`--no-voiceprint`、`--voice-threshold X`）；区分/声纹/实时转写的任何错误只警告，不改变录音和转写的退出码。
- Web 控制台：只绑定 `127.0.0.1`，`config.toml` 的 `[web] port`（默认 8765）；单 Uvicorn worker、不 auto-reload；同一时间最多一场录音；变更类路由走 Origin 守卫，JSON 体要求 `application/json`（否则 415）；静态资源与首页返回 `Cache-Control: no-cache`；所有用户/转写文本只用 `textContent` 渲染（面试问题结果经 DOMPurify 白名单净化是唯一例外）；无 CDN、无前端构建。
- 大模型：`config.toml` 的 `[llm]`（`base_url`、`api_key`、`model`、可选 `timeout_s`=120、`max_input_chars`=60000），OpenAI Chat Completions 兼容；API Key 只在服务端，不进任何响应/日志/错误信息；不跟随重定向；未配置时用到才返回 503。仅在点击"提交"（发言归纳）或"推荐"（面试问题）时发送文字（及面试简历图片），音频不上传。
- 注释和文档字符串写中文；面向用户的提示写中文，句子要短。
- 每次 git commit 前先运行 `afplay ~/custom-workspace/touch-key.mp3` 提醒触摸 YubiKey；`git add` 只写明确路径；GPG 签名失败（`Card error`）不绕过，停下来告诉用户。

### 当前文件结构（全项目）

| 文件 | 责任 |
|---|---|
| common.py | 路径、时间格式、磁盘、静音判断、读 wav。 |
| transcribe.py | 转格式、Whisper、片段过滤、说话人对齐与标签、声纹接入、渲染 `transcript.md`、命令行。 |
| record.py | `WavWriter`、音量条、静音警告、录音主循环（`open_stream` 带超时）、压缩 m4a、`--live` 接入、命令行。 |
| diarize.py | 模型下载与缓存、sherpa-onnx 说话人区分。 |
| live.py | VAD 模型下载、`Segmenter`、`LiveTranscriber`、`LiveSink`。 |
| voiceprint.py / enroll.py | 声纹存取、登记算法、两档阈值与 `label_turns_with_scores` / 登记命令行。 |
| doctor.py | 启动自检六项（只调用注入函数，不做 IO、不联网）。 |
| tools/eval_voiceprint.py | 离线声纹评估：用标注样例算准确率与各阈值误判率。 |
| web.py / web_config.py / web_server.py | Web 启动与配置（`load_port`、`load_llm`）、FastAPI 路由与 SSE。 |
| meeting_store.py / meeting_controller.py | 会议目录查找、标注/标题持久化与删除 / 单场录音生命周期、事件流。 |
| voiceprint_service.py | Web 声纹登记任务（现场录制、导入、取消）。 |
| meeting_summary.py / llm_client.py | 从 transcript.md 解析发言人 / OpenAI 兼容客户端（`chat_summary`、`describe_images`）。 |
| interview_store.py / interview_questions.py | 职位 JSON 存储 / 面试问题提示词。 |
| web/static/{index.html,style.css,app.js,interview.js,marked.min.js,purify.min.js} | 浏览器界面。 |
| spike/ | 临时实验脚本，仅供参考（`live_probe.py`、`tap_probe.py` 等）。 |
| tests/ | 与各模块对应的单元测试。 |

---

## 阶段 1 · 基础录音与转写（2026-10-19）

详细步骤见原 plan 文件。本阶段建立了项目骨架、录音循环、Whisper 转写与命令行。

### 主要约束

- 录音格式：16 kHz，单声道，16 位 wav
- 转写模型默认：`mlx-community/whisper-large-v3-turbo`，语言 `zh`
- 输出根目录：`~/Meetings`，会议目录 `YYYY-MM-DD_HHMM`
- 文件：`meeting.wav`、`meeting.m4a`、`transcript.md`
- 磁盘要求：启动前至少 1 GB 可用空间

### Review Focus

1. 零时长录音不调用模型，文字稿写"（未识别到语音）"
2. 全程静音录音不调用模型
3. 崩溃后 wav 头部长度不准仍能读出已写入采样
4. 路径含空格或中文时 `afconvert` 调用正常
5. 麦克风打不开给出中文提示且不留空目录

### 文件结构

| 文件 | 责任 |
|---|---|
| common.py | 路径、时间格式、磁盘、静音判断、读 wav。纯函数，无外部副作用。 |
| transcribe.py | 转格式、调用 Whisper、过滤片段、生成 `transcript.md`、命令行入口。 |
| record.py | 写 wav、音量条、静音警告、录音主循环、压缩 m4a、命令行入口。 |
| tests/ | 对应单元测试。 |

### 任务清单

- [x] Task 1: 项目骨架与 common.py
- [x] Task 2: transcribe.py
- [x] Task 3: WavWriter、音量条、m4a 压缩
- [x] Task 4: 录音主循环与命令行
- [x] Task 5: README 与手动验收

**状态**：已实现

---

## 阶段 2 · 说话人区分（2026-10-20）

详细步骤：`2026-10-20-speaker-diarization.md`。Spec：`specs/2026-10-20-speaker-diarization-design.md`。

**Goal**: 转写时自动区分说话人，文字稿每段前加匿名标签 `说话人A:`。

### Architecture

新增 `diarize.py`（模型下载 + sherpa-onnx 调用）；`transcribe.py` 新增纯函数 `assign_speakers` 按时间重叠把 Whisper 片段归给说话人。依赖全部注入；区分失败退回无标签文字稿。只新增依赖 `sherpa-onnx`。

### 约束

- 默认开启，人数自动（`num_clusters=-1`，`threshold=0.5`）
- `--speakers N`（整数 ≥1）、`--no-diarize` 互斥
- 标签按首次出现顺序 `说话人A`…`说话人Z`，第 27 个起 `说话人27`
- 行格式 `[HH:MM:SS] 说话人A: 文字`
- 区分失败不改变退出码，向 stderr 打印中文警告

### 任务清单

- [x] Task 1: diarize.py
- [x] Task 2: 对齐与渲染（纯函数）
- [x] Task 3: 串联
- [x] Task 4: record.py 选项与 README
- [ ] Task 5: 效果验收（手动）——真实会议评估需用户反馈

**状态**：Task 1–4 已实现；Task 5 真实会议评估待用户反馈

---

## 阶段 3 · 实时转写 `record.py --live`（2026-10-22）

详细步骤：`2026-10-22-live-transcription.md`。Spec：`specs/2026-10-22-live-transcription-design.md`。

**Goal**: 录音时每说完一句就在终端打印 `[HH:MM:SS] 文字`，并写入会议目录 `live.txt`。

### Architecture

新增 `live.py`：`Segmenter`（sherpa-onnx silero VAD 切句）→ `LiveTranscriber`（工作线程逐句调 `transcribe_samples`）→ `LiveSink`（终端与 `live.txt`，状态行不被覆盖）。`record()` 的 `drain()` 只多调一次非阻塞 `live.feed()`。实时转写任何错误只警告，不影响录音。

### 约束

- 默认关闭，`--live` 打开
- `--live-model REPO` 默认 `transcribe.DEFAULT_MODEL`
- VAD 模型 `silero_vad.onnx`（约 644 KB）
- 句子队列上限 5，满时丢最旧并整场只警告一次
- `stop(timeout=10.0)` 先冲出尾句，最多等 10 s
- 输出行 `[HH:MM:SS] 文字`（无说话人标签）

### 任务清单

- [x] Task 1: 可行性门槛
- [x] Task 2: ensure_vad_model、Segmenter
- [x] Task 3: LiveTranscriber
- [x] Task 4: LiveSink
- [x] Task 5: 接入 record.py
- [x] Task 6: 文档与真实试跑

**状态**：已实现

---

## 阶段 4 · 声纹登记与识别（2026-10-24）

详细步骤：`2026-10-24-voiceprint.md`。Spec：`specs/2026-10-23-voiceprint-design.md`。

**Goal**: 用户登记声纹后，离线转写时自动把属于他的片段标成登记的名字，其余保持 `说话人A/B…`。

### Architecture

新增 `voiceprint.py`（保存读取、登记算法、匹配）与 `enroll.py`（命令行）。`transcribe.py` 在说话人区分后调用 `voiceprint.label_turns`，把匹配片段的 `speaker` 从匿名序号 int 换成名字 str。不新增依赖；声纹模型复用 `diarize.ensure_models()` 的第二个路径（512 维）。

### 约束

- 声纹 JSON：`version`=1、`name`、`model`、`created`、`seconds`、`chunks`、`vector`（512 个 L2 归一化浮点）
- 权限 600；只存向量不存音频；原子写
- 名字规则：字母/数字/中日韩文字/`_`/`-`，1–20 字，不得以"说话人"开头
- 无声纹时输出与改动前逐字相同且不警告
- `DEFAULT_THRESHOLD = 0.5`（**未校准**）

### 任务清单

- [x] Task 1: 存取
- [x] Task 2: 向量与登记
- [x] Task 3: 匹配
- [x] Task 4: speaker_label 与 assign_speakers 支持名字型说话人
- [x] Task 5: 接入转写
- [x] Task 6: enroll.py
- [x] Task 7: 文档与真实冒烟
- [ ] **Task 8: 校准（需要用户录音）**——未完成

**状态**：Task 1–7 已实现；**Task 8 校准未完成**，`DEFAULT_THRESHOLD` 仍为 0.5（未校准）

---

## 阶段 5 · 本地 Web 会议控制台（2026-10-25）

详细步骤：`2026-10-25-local-web-meeting-console.md`。Spec：`specs/2026-10-25-local-web-meeting-console-design.md`。

**Goal**: 只在本机监听的三栏 Web 控制台：录音、实时/最终转写、声纹、会议标注。

### Architecture

FastAPI/Uvicorn 单进程持有一个录音控制器并托管静态页面；带序号的 SSE 事件加状态快照反映服务器真实状态；会议目录内 JSON 存标注，不改变 CLI 输出契约。

### 约束

- 仅绑定 `127.0.0.1`，`config.toml` 的 `[web] port`（默认 8765）
- 音频、声纹、标注都留在本机
- 最多一场录音
- 无认证/远程访问
- 单 worker、无 auto-reload

### 任务清单

- [x] Task 1: 框架与配置
- [x] Task 2: 会议存储
- [x] Task 3: 控制器
- [x] Task 4: HTTP 与 SSE
- [x] Task 5: 声纹 API
- [x] Task 6: 三栏界面
- [x] Task 7: 集成回归与使用指南

**状态**：已实现

**后续变更**：声纹登记改为独立 Tab（阶段 9）；"同意"勾选已移除（阶段 11）

---

## 阶段 6 · 历史会议删除与转写排版（2026-10-26）

详细步骤：`2026-10-26-history-delete-and-transcript-layout.md`。

**Goal**: 左栏历史会议行可删除；所有转写行左对齐，换行缩进等于时间标记宽度。

### 设计

- `meeting_store.delete_meeting(root, id)` → `MeetingController.delete_meeting(id, remove)` → `DELETE /api/meetings/{id}`
- 前端：历史行 `[标题按钮, 删除按钮]`，原生 `confirm()`
- 共享 `renderTranscriptLine(raw, onClick)` 输出两列 grid（时间 | 文本）

### 任务清单

- [x] Task 1: 存储删除
- [x] Task 2: 控制器守卫与 DELETE 端点
- [x] Task 3: 前端删除按钮
- [x] Task 4: 行排版
- [x] Task 5: README 与全量回归

**状态**：已实现

---

## 阶段 7 · 状态颜色、当前会议人数、历史重命名（2026-10-26）

详细步骤：`2026-10-26-status-colors-speaker-count-rename.md`。

**Goal**: (1) 会后转写状态按状态着不同背景色；(2) "开始录音"上方新增"当前会议人数"输入；(3) 历史行增加"编辑"来改标题。

### 设计

- 状态：`#final-status.className = 'status-pill final-' + final`，背景 idle/running/completed/failed
- 人数：`MeetingController.start(..., speakers=None)`；`StartRequest.speakers` 为 1–20 的严格整数
- 重命名：`meeting_store.get_title` / `set_title`；`PATCH /api/meetings/{id}`；目录名与 ID 不变

### 任务清单

- [x] Task 1-5: 全部任务

**状态**：已实现

---

## 阶段 8 · 三栏滚动、类型图标选择与对话标记（2026-10-26）

详细步骤：`2026-10-26-three-column-scroll-kind-icons-transcript-marks.md`。纯前端，后端 API 与存储不变。

**Goal**: (1) 三栏各自滚动；(2) 标注类型改为带图标的复选框样式单选；(3) 标注在中栏对话行打标记，点击右栏记录定位并滚动。

### 设计

- 滚动：`@media(min-width:1041px)` 下 flex 布局，`.workspace>.panel{overflow-y:auto}`
- 类型：`KINDS={note:📝笔记, highlight:⭐重点, comment:💬注释}`
- 标记：`renderTranscriptLine(..., anchor)` 写 dataset，`applyMarks()` 幂等添加图标，`locateAnnotation(item)` 滚动定位

### 任务清单

- [x] Task 1-4: 全部任务

**状态**：已实现

---

## 阶段 9 · 声纹登记独立 Tab 与朗读文案弹窗（2026-10-26）

详细步骤：`2026-10-26-voiceprint-tab-and-reading-dialog.md`。

**Goal**: 声纹登记从左栏拆成独立 Tab；现场录制时弹出全局朗读文案弹窗，登记结束自动关闭，点关闭/Esc 则取消本次录制。

### 设计

- Tab：`#tab-meeting`、`#tab-voiceprint`、`#view-meeting`、`#view-voiceprint`
- 取消：`enroll.record_from_mic(..., stop=None)`；`VoiceprintService.cancel(job_id)`；`POST /api/voiceprints/jobs/{job_id}/cancel`
- 弹窗：`#voiceprint-dialog`，`openEnrollDialog(seconds)` 仅在现场录制成功后打开

### 任务清单

- [x] Task 1-3: 全部任务

**状态**：已实现

---

## 阶段 10 · 发言归纳（大模型）（2026-10-26）

详细步骤：`2026-10-26-speaker-summary.md`。

**Goal**: 右栏新增"发言归纳"：选当前会议的某位发言人，把其全部发言交给大模型归纳。

### 设计与决策

- OpenAI Chat Completions：`POST {base_url}/chat/completions`，Bearer，非流式
- 发言人只来自会后最终稿 `transcript.md`，以表头 `- 说话人：` 为解析门槛
- 模块：`meeting_summary.py`、`llm_client.py`、`web_config.load_llm(path)`
- 接口：`GET /api/meetings/{id}/speakers` 与 `POST /api/meetings/{id}/summary`
- 前端：右栏新增 `#summary-speaker`、`#summary-result`、`#summary-prompt`、`#summary-submit`、`#summary-advanced`

### 约束

- API Key 只在服务端 `config.toml` 的 `[llm]`，永不进响应/日志/错误
- 发言超过 `max_input_chars`（默认 60000）直接 413
- 未配置 [llm] 时服务照常启动，用到才 503

### 任务清单

- [x] Task 1-6: 全部任务

**状态**：已实现

---

## 阶段 11 · 开始录音重置界面、移除 initial_prompt、移除同意勾选（2026-10-28）

详细记录：`2026-10-28-recording-start-reset-and-drop-initial-prompt.md`、`2026-10-28-remove-voiceprint-consent-checkbox.md`。

### 需求与决策

1. 开始新录音时清空上一场选中行与转写区残留
2. 标注时间输入框 `step="any"`，允许多位小数
3. 转写不再向 mlx_whisper 传 `initial_prompt`，删除 `DEFAULT_PROMPT`
4. 声纹登记表单去掉"我已取得被登记者同意"勾选，只保留"明确替换同名声纹"

### 改动文件

- `web/static/app.js`、`web/static/index.html`
- `transcribe.py`、`tests/test_transcribe.py`、`tests/test_web_ui.py`
- 文档同步

**状态**：已完成

---

## 阶段 12 · 面试帮助 Tab（2026-10-29）

详细步骤：`2026-10-29-interview-helper-tab.md`、`2026-10-29-interview-markdown-render.md`。Spec：`specs/2026-10-29-interview-helper-design.md`。

**Goal**: Web 控制台新增"面试帮助"Tab：维护职位描述，粘贴候选人简历（文字/截图），由大模型生成针对性面试问题。

### Architecture

后端新增 JSON 职位存储与提示词模块，`llm_client` 增加图片识别；前端新增独立 `interview.js`。

### 约束

- 职位类型仅 `前端/后端/客户端/测试`
- 标题 ≤100 字无换行，职位要求 ≤10000 字，简历文字 ≤20000 字
- 图片 ≤5 张，仅 `data:image/(png|jpeg|webp|gif);base64,`，单张解码后 ≤5 MB
- 职位文件 `~/.config/meeting-recorder/jobs.json`；简历与生成结果不落盘

### 任务清单

- [x] Task 1: interview_store.py
- [x] Task 2: llm_client.py 图片识别
- [x] Task 3: /api/jobs
- [x] Task 4: interview_questions.py 与 POST /api/jobs/{job_id}/questions
- [x] Task 5: 前端
- [x] Task 6: README 与整体验证

**追加：Markdown 渲染**：成功结果由 marked 18.1.2 解析、DOMPurify 3.4.17 白名单净化后渲染；压缩文件固定版本直接提交，无 CDN/npm。

**追加修复：Tab 点击无反应**：静态资源与首页统一返回 `Cache-Control: no-cache`；Tab 顺序改为 当前会议 / 面试帮助 / 声纹登记。

**状态**：已实现

---

## 阶段 13 · 打开麦克风加超时（2026-10-30）

详细记录：`2026-10-30-open-microphone-timeout.md`。**工作区未提交。**

### 现象

点"开始录音"后中栏计时不走，点"停止录音"无反应，也不进入转写。

### 根因

此前某次录音的 close-stream 线程卡在 `AudioOutputUnitStop`，一直持有 CoreAudio 的 `HALB_Mutex`；之后新录音线程在 `sd.InputStream(...)` 里等同一把锁，`record()` 主循环从未进入。

### 决策

`record.py` 新增 `open_stream()`，在 open-stream 守护线程里创建并进入麦克风流，最多等 `STREAM_OPEN_TIMEOUT_S = 15.0` 秒，超时抛 `RecorderError("打开麦克风超时（系统音频卡住了），请重启服务后重试")`。

### 改动文件

- `record.py`
- `tests/test_record.py`
- `tests/test_meeting_controller.py`
- `README.md`

**状态**：已实现（工作区未提交）

---

## 阶段 14 · 面试问题 40 分钟 + 答案要点（2026-11-01）

详细记录：`2026-11-01-interview-40min-answer-points.md`。**工作区未提交。**

### 需求

面试问题推荐按"专业能力 / AI 应用 / 开放性问题"三维度组织，总时长由约 30 分钟改为约 40 分钟，并为每题附答案的核心点（言简意赅）。

### 决策

- 时间分配：专业能力约 20 分钟、AI 应用约 12 分钟、开放性问题约 8 分钟
- 每题在"预计用时""考察点"后加粗体标签 **答案要点：** 加 2–4 条无序列表，每条一句话

### 改动文件

- `interview_questions.py`（`SYSTEM_PROMPT`）
- `tests/test_interview_questions.py`
- `README.md`
- `specs/2026-10-29-interview-helper-design.md`

**状态**：已实现（工作区未提交）

---

## 阶段 15 · 可信度优先（2026-11-09）

详细记录：`docs/superpowers/plans/2026-11-09-reliability-first.md`；设计：`docs/superpowers/specs/2026-11-09-reliability-first-design.md`。

### 目标

从“能录、能转”提高到“结果可信、出错可控”：声纹分档且可核对、异常路径不丢数据、启动即可自检、中断的会议能补转写。

### 设计

- 声纹两档阈值：`config.toml` 的 `[voiceprint] high / low`（默认 0.5 / 0.35）；`>= high` 写真名，`>= low` 写“疑似名”，其余保留“说话人 N”。阈值非法（非数字、`0 < low < high <= 1` 不成立）整体回退默认值并打印中文警告。
- 文字稿表头新增 `- 声纹匹配：<标签> <分数>` 行；发言归纳把“疑似X”并到 X，接口另给 `suspect_speakers` 标出哪些发言是疑似。
- 自检 `doctor.run_checks` 六项：麦克风、磁盘、模型缓存、大模型配置、声纹文件权限、残留音频线程。全部只读、只给固定中文短句；`GET /api/doctor`、`POST /api/doctor/llm-test`（后者只回报固定原因，不显示地址和 Key）。
- 中断恢复：`meeting_store.list_incomplete` 找出有录音无文字稿的会议，历史列表标“未完成”；`POST /api/meetings/{id}/retranscribe` 补转写，`force` 重新转写前把旧稿改名为 `transcript.md.bak`。
- 最终审查的 10 个 MUST-FIX 全部落地：损坏 wav 不当空录音、实时转写初始化移到独立线程、保存/删除响应不串、标注/职位损坏先唯一备份、仅大小写不同的替换保持一致、声纹取消与保存提交点互斥、Ctrl-C 清理迟到流并返回 130、超大标注时间戳不溢出、声纹模型/维度不兼容时告警并跳过。

### 约束

- 新功能全部离线可测；真实麦克风、模型下载、真实大模型、真实声纹登记只记“未测”。
- 自检绝不展示路径、设备名、Key 或异常原文；不自动下载模型、不改文件。
- 声纹文件权限过宽只告警，不自动改权限。

### 任务与状态

| Task | 内容 | 状态 |
|---|---|---|
| 1 | 容错读取 wav | 作废（基线已实现，不重复做） |
| 2 | 声纹“疑似”标签与两档阈值 | 已实现（11ce704） |
| 3 | 按聚类最高分分配标签，同名冲突整个聚类跳过 | 已实现（c391115） |
| 4 | 文字稿表头分数行、疑似合并与 `suspect_speakers` | 已实现（99c6c64） |
| 5 | `doctor.py` 六项自检 | 已实现（db480fa） |
| 6 | 大模型连接自检与固定中文原因 | 已实现（19bf9f9） |
| 7 | 中断录音的补转写 / 重新转写 | 已实现（6504624） |
| 8 | 自检接口与页面 | 已实现（ed45fe0） |
| 9 | 麦克风隔离 spike | 已实现（c6362fd、3aece19）；本机无麦克风输入，三种模式均未测，#2b 不做 |
| 10 | `tools/eval_voiceprint.py` 离线评估 | 已实现 |
| 11 | 用真实标注数据校准阈值 | 阻塞：需真实数据 |
| 12 | 登记本阶段、更新 README | 已实现 |

**状态**：Task 1 作废、Task 11 待真实数据，其余全部实现；全量 587 条测试通过。

---

## 变更记录（后续按 AGENTS.md 在此追加）

- **2026-11-09**：阶段 15 可信度优先落地。新增 `doctor.py` 自检与 `GET /api/doctor`、`POST /api/doctor/llm-test`；`transcript.md` 表头新增 `- 声纹匹配：` 行，发言归纳接口新增 `suspect_speakers`；`config.toml` 新增 `[voiceprint] high / low`；新增 `POST /api/meetings/{id}/retranscribe` 与历史列表“未完成”标记；修复最终审查的 10 个 MUST-FIX。阶段 13/14 已提交。已知未完成：阶段 4 Task 8 阈值校准（由阶段 15 Task 11 承担，待真实数据）；“全部在本机运行”的隐私文案在启用 `[llm]` 后不严谨，待改；#2b 麦克风隔离待另立计划（本机无输入设备，spike 未测）。
- **2026-11-08**：把 docs/superpowers/plans/ 下全部分阶段 plan 合并进本文件，新增总览、阶段索引、当前累计约束与文件结构；阶段 1 的 Task 标题加前缀以免与后续阶段混淆；阶段 1 之后各阶段按"目标 / 设计 / 约束 / Review Focus / 任务 / 状态"重写为摘要，逐步骤细节仍在原文件。已知未完成项见阶段索引：阶段 2 Task 5 真实会议评估、阶段 4 Task 8 声纹阈值校准、阶段 13/14 待提交。
