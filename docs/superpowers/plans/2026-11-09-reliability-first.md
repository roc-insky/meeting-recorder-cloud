# 可信度优先 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让会议录音可恢复、声纹识别带置信度分档、提供只读自检页和可重复的离线评估脚本，并用 spike 确定如何避开 CoreAudio 卡死。

**Architecture:** 沿用总计划的"纯函数 + 参数注入"风格。「疑似」标签与阈值集中在 `voiceprint.py`；表头解析收敛到 `meeting_store.parse_header`；「未完成」由文件推导加控制器内存集合，不新增状态文件；doctor 是新增模块 `doctor.py`，只读；评估脚本放 `tools/`，不进运行时。录音隔离方案由 Task 9 的 spike 实测决定，实现另立计划（#2b）。

**Tech Stack:** Python 3.14（项目 `.venv`）、stdlib `unittest`/`tomllib`/`threading`、FastAPI、原生 JS（无构建）、sounddevice、numpy。不新增依赖。

**Spec:** docs/superpowers/specs/2026-11-09-reliability-first-design.md（背景：docs/plan/meeting-recorder-master-plan.md）

## Global Constraints

- 测试命令 `.venv/bin/python -m unittest discover -s tests -t . -v`；单模块 `.venv/bin/python -m unittest tests.test_xxx -v`。
- 测试不联网、不加载真实模型、不用真实麦克风、不用真实音频；任何测试都不得读写真实的 `~/.config/meeting-recorder/`（用 `mock.patch.object(voiceprint, "PROFILE_DIR", tmp)` 覆盖）。
- 声纹 JSON 权限 600、原子写；名字规则：字母/数字/中日韩文字/`_`/`-`，1–20 字，不得以"说话人"开头（本计划加：不得以"疑似"开头）。
- 无声纹时 `transcript.md` 输出与改动前逐字相同且不警告。
- Web：只绑定 `127.0.0.1`；变更类路由走 Origin 守卫，JSON 体要求 `application/json`（否则 415）；所有用户/转写文本只用 `textContent` 渲染；无 CDN、无前端构建。
- 大模型：API Key 只在服务端，不进任何响应/日志/错误信息；不跟随重定向。
- 说话人区分/声纹/实时转写/自检的任何错误只警告，不改变录音与基础转写的退出码。
- 不做：本地 LLM 预设与隐私界面、跨会议搜索/纪要/待办、拆分面试助手、`status.json`、把 transcript.md 表头改成 YAML/JSON、隐私文案修正（TODO，下一轮）。
- 注释和文档字符串写中文；面向用户的提示写中文，句子要短。
- 每次 git commit 前先运行 `afplay ~/custom-workspace/touch-key.mp3`；`git add` 只写明确路径；GPG 签名失败（`Card error`）不绕过，停下来告诉用户。

## Pre-flight：先找到代码，再核对现状

**阻塞项：** 本仓库（`/Users/mima0000/Documents/Python/meeting-record`）目前只有 `docs/`、`tools/eval_voiceprint.py`、`tests/test_eval_voiceprint.py`，**没有** `record.py`、`voiceprint.py`、`web_server.py` 等业务代码；总计划写的工作目录 `/Users/roc.peng/Documents/Python/meeting-recorder` 在本机不存在。Task 1–9、11 都要改业务代码，开始前必须由用户指明业务代码所在目录（或把代码放进本仓库）。找不到就停在这里，只做 Task 12 的文档部分。

下列名字取自总计划文字，**均未对照代码**。开始 Task 1 前在业务仓库逐条核对；与实际不符时按实际改名，在 ledger 记一条 `Ruling` 再继续，不要为迎合计划去改已有接口。

| 假设 | 核对命令 |
|---|---|
| `voiceprint.label_turns`、`PROFILE_DIR`、`DEFAULT_THRESHOLD`、名字校验函数存在 | `grep -n "def label_turns\|PROFILE_DIR\|DEFAULT_THRESHOLD\|说话人" voiceprint.py` |
| `transcribe.py` 有渲染 `transcript.md` 的函数和 `- 说话人：` 表头行 | `grep -n "说话人：" transcribe.py meeting_summary.py` |
| 表头解析是否已集中在某个函数 | `grep -n "^def " meeting_summary.py meeting_store.py` |
| `diarize.ensure_models()`、`live.ensure_vad_model()` 的缺失判断与下载是否混在一起 | `grep -n "def ensure_" diarize.py live.py` |
| `WavWriter` 何时回写 wav 头（被杀后头里长度是否为 0） | `grep -n "class WavWriter" -A30 record.py` |
| Origin 守卫与 415 的复用方式、测试客户端 | `grep -n "Origin\|415" web_server.py tests/test_web_server.py` |
| 录音相关线程名（开/关麦克风流） | `grep -n "Thread(" record.py` |

## Review Focus

- 被杀的录音：wav 头里数据长度为 0 或大于文件实际长度，补转写仍要读出真实样本（Task 7）。
- 转写进行中再点"补转写"，或正在录音时点：返回 409，不重复转写（Task 7）。
- 已有 `transcript.md` 的会议被自动流程碰到：内容逐字不变；"重新转写"才生成 `.bak`（Task 7）。
- `config.toml` 没有 `[voiceprint]` 段、值是字符串或布尔、`low>=high`：中文告警并退回默认值，不崩溃（Task 2）。
- 麦克风枚举卡死：自检接口在超时内返回"设备枚举无响应"，不挂起（Task 5、8）。
- 「测试连接」遇到 3xx 重定向或错误体含 Key：不跟随、响应不含 Key（Task 6）。

---

### Task 1: 提交阶段 13、14（#0）

**Files:** commit only —— `record.py`、`tests/test_record.py`、`tests/test_meeting_controller.py`、`interview_questions.py`、`tests/test_interview_questions.py`、`README.md`、`docs/superpowers/specs/2026-10-29-interview-helper-design.md`

**Interfaces:** Produces: 干净的工作区，后续任务的 BASE。

- [ ] **Step 1: 跑全量测试确认基线是绿的**
  Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -5`；Expected: `OK`。不绿则停下，把失败列给用户，不带红灯提交。
- [ ] **Step 2: 确认工作区只含这两个阶段的改动**
  Run: `git status --short`；Expected: 只出现上面 Files 的路径（及 `docs/` 新文件）。出现别的路径就停下确认。
- [ ] **Step 3: 提交阶段 13**（`record.py`、两个测试文件，`git add -p README.md` 只选"打开麦克风超时"的 hunk）
  `git commit -m "fix: 打开麦克风加 15 秒超时，避免系统音频卡住时界面无响应"`；Expected: `git show --stat HEAD` 不含面试相关文件。
- [ ] **Step 4: 提交阶段 14**（`interview_questions.py`、对应测试、`README.md` 余下部分、面试 spec）
  `git commit -m "feat: 面试问题改为约 40 分钟并附答案要点"`；Expected: `git status --short` 只剩与本计划相关的 `docs/` 文件。

---

### Task 2: 声纹标签与阈值（#5 基础）

**Files:** Modify `voiceprint.py`；Test `tests/test_voiceprint.py`

**Interfaces:** Produces（供 Task 3、4、5 使用）:
- `SUSPECT_PREFIX = "疑似"`；`suspect_label(name: str) -> str`；`parse_label(label: str) -> tuple[str, bool]`（只剥一层前缀）
- `class Thresholds(NamedTuple): high: float; low: float; warning: str | None = None`
- `DEFAULT_HIGH = 0.5`、`DEFAULT_LOW = 0.35`（均标"未校准"）；`DEFAULT_THRESHOLD = DEFAULT_HIGH` 保留为别名
- `load_thresholds(config_path: Path | None, cli_high: float | None = None) -> Thresholds`
- `classify_score(score: float, th: Thresholds) -> str`：`"named"`（>= high）/ `"suspect"`（>= low）/ `"anonymous"`

- [ ] **Step 1: 写失败测试**（配置文件用 `tempfile` 写 toml）
  - `test_parse_label`：`"疑似张三"→("张三",True)`；`"张三"→("张三",False)`；`"说话人A"→("说话人A",False)`；`"疑似疑似张三"→("疑似张三",True)`
  - `test_register_rejects_suspect_prefix`：`"疑似张三"` 被名字校验拒绝
  - `test_thresholds_default`：文件不存在 / 无 `[voiceprint]` 段 → `Thresholds(0.5, 0.35, None)`
  - `test_thresholds_valid`：`high=0.6, low=0.4` → `(0.6, 0.4, None)`
  - `test_thresholds_invalid`：`low>=high`、`low=-0.1`、`high="abc"`、`high=true` 四种都得 `(0.5, 0.35, warning)`，warning 非空且含中文
  - `test_cli_high_overrides_high_only`：配置 `low=0.4`，`cli_high=0.7` → `(0.7, 0.4, None)`；`cli_high=0.3` → 默认值加 warning
  - `test_classify_boundaries`：`Thresholds(0.6,0.4)` 下 `0.6→named`、`0.5999→suspect`、`0.4→suspect`、`0.3999→anonymous`
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_voiceprint -v 2>&1 | tail -15`；Expected: FAIL（新名字未定义）
- [ ] **Step 3: 实现上面 Interfaces 的全部名字**。`load_thresholds` 用 `tomllib` 读 `[voiceprint]` 的 `high`/`low`，类型必须是 int/float 且不是 bool；先应用 `cli_high` 再校验 `0 < low < high <= 1`，失败返回默认值加中文 warning。名字校验加"以 `SUSPECT_PREFIX` 开头则拒绝"。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`
- [ ] **Step 5: 提交** —— `git add voiceprint.py tests/test_voiceprint.py`；`feat: 声纹增加疑似标签与高低两个阈值`

---

### Task 3: 按说话人聚类分档（#5）

**Files:** Modify `voiceprint.py`、`transcribe.py`；Test `tests/test_voiceprint.py`、`tests/test_transcribe.py`

**Interfaces:**
- Consumes: Task 2 的 `Thresholds`、`classify_score`、`suspect_label`
- Produces: `label_turns_with_scores(turns, ..., thresholds: Thresholds | None = None) -> tuple[list, dict[str, float]]`（其余参数沿用现有 `label_turns`；返回标注后的 turns 与 `{显示名: 该聚类最高分}`，显示名为 `"张三"` 或 `"疑似张三"`）；`label_turns(...)` 保留且返回值不变；`transcribe.py` 用 `voiceprint.load_thresholds(config_path, cli_high=args.voice_threshold)`，告警走现有"只警告"通道

- [ ] **Step 1: 写失败测试**（假向量、假匹配函数，沿用现有 `label_turns` 测试的注入方式）
  - `test_cluster_high_score_gets_real_name`：聚类分 0.7（high=0.6）→ 该聚类每句 `speaker == "张三"`
  - `test_cluster_gray_zone_gets_suspect`：分 0.5 → 同一聚类每一句都是 `"疑似张三"`（按聚类，不逐句）
  - `test_cluster_low_stays_anonymous`：分 0.3 → 保持匿名序号
  - `test_two_clusters_same_person`：两个聚类都匹配张三时只有分更高的拿名字（核对现状后若无此行为，记 Ruling 并跳过）
  - `test_no_profiles_output_unchanged`：无声纹时 turns 与改动前逐项相同，scores 为 `{}`
  - `tests/test_transcribe.py`：`--voice-threshold 0.3` 低于配置 low 时 stderr 有中文告警，转写继续
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_voiceprint tests.test_transcribe -v 2>&1 | tail -15`；Expected: FAIL（`label_turns_with_scores` 未定义）
- [ ] **Step 3: 实现**：以聚类为单位取最高匹配分，`classify_score` 决定写真名 / `suspect_label(名字)` / 不动；`label_turns` 改为调用 `label_turns_with_scores`。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`
- [ ] **Step 5: 提交** —— `git add voiceprint.py transcribe.py tests/test_voiceprint.py tests/test_transcribe.py`；`feat: 声纹按说话人聚类分档，灰区标疑似`

---

### Task 4: 表头新增分数行与统一解析（#5）

**Files:** Modify `transcribe.py`（render）、`meeting_store.py`、`meeting_summary.py`；Test `tests/test_transcribe.py`、`tests/test_meeting_store.py`、`tests/test_meeting_summary.py`

**Interfaces:**
- Consumes: Task 3 的 scores 字典；Task 2 的 `parse_label`
- Produces:
  - 表头新增一行 `- 声纹匹配：张三 0.62、疑似李四 0.48`（顿号分隔，分数两位小数）；`- 说话人：` 行格式不变
  - `meeting_store.parse_header(text: str) -> dict`，键 `"speakers": list[str]`（原样含「疑似」）、`"voice_scores": dict[str, float]`；Pre-flight 若已有统一解析函数则扩展它并记 Ruling
  - `meeting_summary` 的"列发言人"与"取某人发言"改用 `parse_header`/`parse_label`：疑似X 与 X 合并为同一人，结果带"含疑似片段"布尔字段（字段名按现有响应风格定）

- [ ] **Step 1: 写失败测试**
  - `test_render_no_scores_byte_identical`：scores 为空时输出与改动前黄金样本逐字相同
  - `test_render_scores_line`：scores `{"张三":0.62,"疑似李四":0.48}` → 含整行 `- 声纹匹配：张三 0.62、疑似李四 0.48`，`- 说话人：` 行与无分数时相同
  - `test_parse_header_old_format`：无「声纹匹配」行 → `voice_scores == {}`，`speakers` 正确
  - `test_parse_header_new_and_gray`：新格式、含「疑似」各一条，分数为 float
  - `test_summary_merges_suspect`：文字稿里有 `张三` 与 `疑似张三` → 发言人列表只有「张三」，取其发言时两部分都在且标出含疑似片段
  - `test_summary_anonymous_untouched`：`说话人A` 行为与改动前一致
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_transcribe tests.test_meeting_store tests.test_meeting_summary -v 2>&1 | tail -15`；Expected: FAIL
- [ ] **Step 3: 实现**，并把其余读表头的地方（`meeting_summary.py`、会议列表）全部改用 `parse_header`。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`
- [ ] **Step 5: 提交** —— `git add transcribe.py meeting_store.py meeting_summary.py tests/test_transcribe.py tests/test_meeting_store.py tests/test_meeting_summary.py`；`feat: 文字稿表头记录声纹分数，发言归纳合并疑似与真名`

---

### Task 5: 模型存在性检查与 doctor 核心（#1）

**Files:** Modify `diarize.py`、`live.py`；Create `doctor.py`；Test `tests/test_diarize.py`、`tests/test_live.py`、`tests/test_doctor.py`

**Interfaces:** Produces:
- `diarize.models_present(cache_dir: Path) -> list[str]`、`live.models_present(cache_dir: Path) -> list[str]`：返回**缺失**的模型文件相对名；0 字节算缺失；零网络零写入；`ensure_*` 改用它判断缺失
- `@dataclass(frozen=True) class Check: name: str; status: str; detail: str`，`status ∈ {"ok","warn","fail"}`
- `doctor.call_with_timeout(fn: Callable[[], T], seconds: float) -> tuple[bool, T | BaseException | None]`：守护线程里跑；超时 `(False, None)`；抛异常 `(True, 异常对象)`
- `doctor.run_checks(*, list_input_devices: Callable[[], list[str]], free_bytes: Callable[[], int], models_missing: Callable[[], list[str]], llm_config: dict | None, voiceprint_modes: Callable[[], list[int]], lingering_threads: Callable[[], int], device_timeout_s: float = 3.0) -> list[Check]`
- 固定检查名：`麦克风`、`磁盘`、`模型缓存`、`大模型配置`、`声纹文件权限`、`残留音频线程`；`detail` 一律固定中文短句，**不含绝对路径、设备序列号、异常原文**；`MIN_FREE_BYTES = 2 * 1024**3`（低于则 `warn`）

- [ ] **Step 1: 写失败测试**
  - `tests/test_diarize.py`、`tests/test_live.py`：`models_present` 对（目录为空 / 齐全 / 其中一个 0 字节）各一条，断言返回列表；调用前后目录内容不变；注入的假下载函数被调用即失败
  - `tests/test_doctor.py`：`test_all_ok`（六项都 ok）；`test_mic_hang_times_out`（枚举阻塞、`device_timeout_s=0.2` → 1 秒内返回，`麦克风` 为 fail，detail == `"设备枚举无响应"`）；`test_no_input_device`（`[]` → fail）；`test_low_disk`（`MIN_FREE_BYTES-1` → warn）；`test_models_missing`（fail，detail 不含文件路径）；`test_llm_config`（缺 `api_key` → warn；齐全且 `base_url` 以 http 开头 → ok；`base_url` 非法 → fail；全程无网络调用，注入 opener 被调用即失败）；`test_voiceprint_perm`（含 `0o644` → warn，全 `0o600` → ok）；`test_lingering_threads`（>0 → warn）；`test_no_sensitive_text`（所有 detail 拼接后不含 `str(Path.home())`、`"sk-"`、传入的假设备序列号）
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_diarize tests.test_live tests.test_doctor -v 2>&1 | tail -15`；Expected: FAIL
- [ ] **Step 3: 实现**：先写 `models_present` 并让 `ensure_*` 复用；再写 `doctor.py`，`run_checks` 只调用注入函数，自己不碰文件系统、网络、sounddevice。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`
- [ ] **Step 5: 提交** —— `git add diarize.py live.py doctor.py tests/test_diarize.py tests/test_live.py tests/test_doctor.py`；`feat: 新增 doctor 自检核心与模型存在性检查`

---

### Task 6: 「测试连接」（#1）

**Files:** Modify `llm_client.py`；Test `tests/test_llm_client.py`

**Interfaces:**
- Consumes: 现有 `llm_client` 的 opener 注入方式与配置结构（Pre-flight 核对）
- Produces: `llm_client.test_connection(cfg: dict | None, *, opener=None) -> tuple[bool, str]`。原因只能取：`"连接成功"`、`"未配置大模型"`、`"无法连接，请检查地址和网络"`、`"认证失败，请检查 API Key"`、`"服务返回错误"`、`"请求超时"`、`"地址发生了重定向，已拒绝"`。发最小请求（一条 `max_tokens=1` 的消息）；不跟随重定向；绝不回传服务器原文。

- [ ] **Step 1: 写失败测试**（假 opener，不联网）
  - `test_ok`：200 → `(True, "连接成功")`
  - `test_not_configured`：`cfg=None` → `(False, "未配置大模型")`，opener 未被调用
  - `test_auth_failed`：401 `HTTPError` → `"认证失败，请检查 API Key"`
  - `test_server_error_body_has_key`：500，错误体 `"bad key sk-secret-123"`，`api_key="sk-secret-123"` → 原因 `"服务返回错误"`；断言 `"sk-secret-123" not in repr(返回值)`
  - `test_redirect_refused`：302 `HTTPError` → `"地址发生了重定向，已拒绝"`，且没有第二次请求
  - `test_timeout`：`TimeoutError` → `"请求超时"`；`URLError` → `"无法连接，请检查地址和网络"`
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_llm_client -v 2>&1 | tail -10`；Expected: FAIL
- [ ] **Step 3: 实现**：复用现有"不跟随重定向"opener 构造；异常只映射到固定原因，不拼接任何异常文本。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`
- [ ] **Step 5: 提交** —— `git add llm_client.py tests/test_llm_client.py`；`feat: 大模型测试连接，只返回固定中文原因`

---

### Task 7: 恢复中断的录音（#3）

**Files:** Modify `common.py`、`meeting_store.py`、`transcribe.py`（落盘）、`meeting_controller.py`、`web_server.py`、`web/static/app.js`、`web/static/index.html`；Test `tests/test_common.py`、`tests/test_meeting_store.py`、`tests/test_meeting_controller.py`、`tests/test_web_server.py`

**Interfaces:** Produces:
- `common.atomic_write_text(path: Path, text: str) -> None`：同目录写临时文件再 `os.replace`
- `common.read_wav_tolerant(path: Path) -> tuple[int, numpy.ndarray]`：返回 `(采样率, int16 样本)`；头里数据长度为 0 或超过文件实际长度时改用文件实际剩余字节；样本数 0 抛 `ValueError("录音文件为空或损坏")`
- `meeting_store.list_incomplete(root: Path, transcribing: Collection[str]) -> list[str]`：返回会议目录名，条件：有 `meeting.wav`、无 `transcript.md`、不在 `transcribing`；新到旧；只做 `stat`
- `GET /api/meetings` 每项新增布尔 `incomplete`
- `MeetingController.retranscribe(meeting_id: str, *, force: bool = False) -> None`；异常 `MeetingBusy`（录音中或该会议正在转写 → 409）、`AlreadyTranscribed`（已有 transcript 且 `force=False` → 409）；坏 wav → 422，消息 `"录音文件损坏，无法转写"`
- `POST /api/meetings/{id}/retranscribe`，JSON 体 `{"force": bool}`，走 Origin 守卫与 415
- 控制器内存集合 `_transcribing: set[str]`（加锁），会后正常转写也进出这个集合；`force=True` 时旧稿先改名 `transcript.md.bak`（已有 `.bak` 则覆盖）

- [ ] **Step 1: 写失败测试**
  - `tests/test_common.py`：`test_atomic_write_no_partial`（`os.replace` 抛异常 → 目标仍是旧内容，临时文件被清理）；`test_read_wav_tolerant_header_zero`（头长度 0，后跟 32000 字节 → 16000 个样本、采样率 16000）；`test_read_wav_tolerant_header_too_long`（读出实际样本数）；`test_read_wav_empty`（只有头 → `ValueError`）
  - `tests/test_meeting_store.py`：`test_list_incomplete` 四种目录（有 wav 无 transcript 命中；有 transcript 不命中；在 `transcribing` 里不命中；无 wav 不命中）；用 `mock.patch("builtins.open")` 包住扫描，断言没有 open 过文件
  - `tests/test_meeting_controller.py`（假转写函数、假录音）：`test_retranscribe_fills_gap`；`test_retranscribe_busy_while_transcribing`（转写函数只被调用一次）；`test_retranscribe_busy_while_recording`；`test_existing_transcript_untouched`（`force=False` → `AlreadyTranscribed`，文件逐字不变）；`test_force_makes_bak`；`test_set_cleared_on_failure`（转写抛异常后集合仍被清空）；`test_bad_wav_message`
  - `tests/test_web_server.py`：路由 409 / 422 / 415 / Origin 守卫各一条；列表 `incomplete` 字段
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_common tests.test_meeting_store tests.test_meeting_controller tests.test_web_server -v 2>&1 | tail -20`；Expected: FAIL
- [ ] **Step 3: 实现后端**：`transcribe.py` 写 `transcript.md` 改用 `atomic_write_text`，读 wav 改用 `read_wav_tolerant`（Pre-flight 若现有读取器已容忍坏头，则复用并补缺口，记 Ruling）；控制器在转写开始/结束（含异常）处维护 `_transcribing`。
- [ ] **Step 4: 实现前端并冒烟**：历史列表给 `incomplete` 会议加「未完成」徽标和「补转写」按钮（`textContent`）；转写中按钮置灰；已有 transcript 的会议提供「重新转写」，点击前 `confirm` 说明旧稿备份为 `.bak`。冒烟：造一个只有 `meeting.wav` 的会议目录，启动服务，页面出现「未完成」，点「补转写」后徽标消失、目录出现 `transcript.md`。
- [ ] **Step 5: 运行全量** —— Expected: `OK`
- [ ] **Step 6: 提交** —— `git add common.py meeting_store.py transcribe.py meeting_controller.py web_server.py web/static/app.js web/static/index.html tests/test_common.py tests/test_meeting_store.py tests/test_meeting_controller.py tests/test_web_server.py`；`feat: 恢复中断的录音，补转写与重新转写`

---

### Task 8: 自检接口与页面（#1）

**Files:** Modify `web_server.py`、`record.py`、`web/static/index.html`、`web/static/app.js`；Test `tests/test_web_server.py`、`tests/test_record.py`

**Interfaces:**
- Consumes: Task 5 的 `doctor.run_checks`、两个 `models_present`；Task 6 的 `llm_client.test_connection`
- Produces: `record.lingering_stream_threads() -> int`（仍存活的开/关麦克风流线程数；线程名统一前缀 `mr-stream-`，已有名字则沿用）；`GET /api/doctor` → `{"checks": [{"name","status","detail"}]}`（只读，不走 Origin 守卫，不含绝对路径）；`POST /api/doctor/llm-test` → `{"ok": bool, "reason": str}`（Origin 守卫 + 415）；页面「自检」区列出各项，`textContent`，带「测试连接」按钮

- [ ] **Step 1: 写失败测试**
  - `tests/test_record.py`：`test_lingering_threads_count`（存活的 `mr-stream-x` 线程 → 1；结束后 0）
  - `tests/test_web_server.py`：`test_doctor_get_no_paths`（响应不含 `str(Path.home())`）；`test_doctor_get_is_readonly`（没调用任何下载/网络函数）；`test_llm_test_requires_origin`（Origin 不对拒绝；非 JSON → 415）；`test_llm_test_never_leaks_key`；`test_doctor_get_returns_while_mic_hangs`（枚举阻塞，总耗时 < 5 秒返回）
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_record tests.test_web_server -v 2>&1 | tail -15`；Expected: FAIL
- [ ] **Step 3: 实现**：路由里把真实依赖（`sounddevice.query_devices` 只取名称、`shutil.disk_usage`、两个 `models_present`、声纹目录 `os.stat` 权限、`record.lingering_stream_threads`）装配为默认值，测试注入假的。
- [ ] **Step 4: 运行全量并冒烟** —— Expected: `OK`；页面「自检」区显示六项；清空 `[llm]` `api_key` 后对应项为「未配置」而非故障。
- [ ] **Step 5: 提交** —— `git add web_server.py record.py web/static/index.html web/static/app.js tests/test_web_server.py tests/test_record.py`；`feat: 自检接口与页面，测试连接走 POST`

---

### Task 9: 录音隔离 spike（#2a）

**Files:** Create `spike/mic_isolation_probe.py`、`docs/superpowers/plans/2026-11-09-mic-isolation-spike-result.md`

**Interfaces:** Produces: 一份结论文档，含下表（每格填实测值）和**唯一的结论**：哪一种方案进 #2b。#2b 的实现计划另写。需要真实麦克风，不进自动测试；是实验不走 TDD，验收是结论文档数据完整。

- [ ] **Step 1: 写探针** `spike/mic_isolation_probe.py`，命令行 `--mode persistent|abort|subprocess`、`--cycles N`（默认 20）。`persistent`：只开一次 `sd.InputStream`，循环 N 次"开始写盘/停止写盘"，不写盘时回调直接返回，不拷贝不入队；`abort`：每轮新开流，结束用 `stream.abort()` 再 `close()`，记录各自耗时；`subprocess`：每轮子进程开流录 2 秒写 wav，父进程只收文件，记录子进程退出耗时。每种输出：轮数、开流耗时最大/平均、是否出现超过 5 秒的卡顿。
- [ ] **Step 2: 运行三种模式** —— `.venv/bin/python spike/mic_isolation_probe.py --mode persistent --cycles 20`（另两种同理）；Expected: 各打印汇总，贴进结论文档。
- [ ] **Step 3: 复现卡死（尽力而为）**：每种模式下一边录音一边切换输入设备（含蓝牙耳机插拔），记录开流是否超过 15 秒：复现 / 未复现 / 不适用。未复现不等于已修复，照实写。
- [ ] **Step 4: 常驻流专项验收（仅当 persistent 要入选）**：空转 1 小时 CPU（`ps -o %cpu -p <pid>` 采样，取平均与最大）；菜单栏麦克风指示灯是否一直亮；录音中切蓝牙耳机后流能否自动恢复。
- [ ] **Step 5: 写结论文档**：表格列 方案 / 20 轮最大开流耗时 / 是否卡顿 / 切设备后表现 / 空转 CPU（仅常驻流）/ 改动面。结论一句话：选哪个、`--no-isolate` 回退开关怎么接。没测的写"未测"并说明原因。
- [ ] **Step 6: 提交** —— `git add spike/mic_isolation_probe.py docs/superpowers/plans/2026-11-09-mic-isolation-spike-result.md`；`docs: 录音隔离 spike 结论`

---

### Task 10: 离线评估算法（#4）—— 已完成

已在提交 `17174ca` 实现：`tools/eval_voiceprint.py` 的 `far_frr`、`Recommendation`、`recommend_thresholds`、`diarization_error_rate`，测试 `tests/test_eval_voiceprint.py`（14 个）。不再有实现步骤。

**Ruling（写进 ledger）：** CEO 计划写"low = FRR ≤ 10% 的最高阈值"，分数分离良好时它会高于 high，灰区消失并违反 `0 < low < high`。实现改为"low = FAR ≤ 10% 的最小候选分数；若 high - low < min_gap 则取 high - min_gap（不足以为正时取 high/2）"。

- [ ] **Step 1: 确认仍为绿** —— Run: `python3 -m unittest tests.test_eval_voiceprint -v 2>&1 | tail -3`；Expected: `OK`（目前 14 个测试通过）。
- [ ] **Step 2: 确认 `.gitignore` 含 `eval/`**（已有）；无需提交。

---

### Task 11: 用真实数据评估并校准（#4 / #5，**数据到位后才执行**）

**前置条件：** `~/.config/meeting-recorder/eval/` 下 ≥2 场真实会议，每场 ≥3 位说话人、≥1 位已登记声纹，并有手工标注 `ref.json`（`{"segments": [[起, 止, "说话人"]], "enrolled": {"说话人": "登记名"}}`）；可靠阈值另需 ≥5 位冒名者。**不满足就停在这里，留给用户，不用别的数据凑。** 真实数据不进仓库、不进自动测试。

**Files:** Modify `tools/eval_voiceprint.py`（加 `collect_scores` 与 `main`）、`voiceprint.py`（只改 `DEFAULT_HIGH`/`DEFAULT_LOW`）、`docs/plan/meeting-recorder-master-plan.md`

**Interfaces:** Consumes: Task 10 三个函数；`diarize`、`voiceprint` 公开函数。Produces: `collect_scores(eval_dir: Path) -> tuple[list[float], list[float]]`（本人分数、冒名分数）；`python -m tools.eval_voiceprint --dir <eval 目录>` 打印 FAR/FRR 表、推荐阈值、DER。

- [ ] **Step 1: 实现 `collect_scores` 与命令行**，复用现有区分与声纹匹配的公开函数；需要真实模型，无单测，只手动冒烟。
- [ ] **Step 2: 运行** —— `.venv/bin/python -m tools.eval_voiceprint --dir ~/.config/meeting-recorder/eval`；Expected: 打印推荐 `high`/`low`；样本不足时显示"置信有限，阈值仅作初值"。
- [ ] **Step 3: 把推荐值写进 `DEFAULT_HIGH`/`DEFAULT_LOW`，去掉"未校准"注释**（`limited` 则保留"仅作初值"）；跑全量，Expected: `OK`。
- [ ] **Step 4: 更新总计划**：阶段 4 Task 8 勾选（`limited` 则注明"初值"），阶段 2 Task 5 记录 DER 数值。
- [ ] **Step 5: 提交** —— `git add tools/eval_voiceprint.py voiceprint.py docs/plan/meeting-recorder-master-plan.md`；`feat: 用真实数据校准声纹阈值`

---

### Task 12: 登记阶段 15、变更记录并提交本计划

**Files:** Modify `docs/plan/meeting-recorder-master-plan.md`、`README.md`；提交本计划与 spec、`docs/product/`

- [ ] **Step 1: 总计划增补**
  - 阶段索引表加一行：`15 | 2026-11-09 | 可信度优先 | docs/superpowers/plans/2026-11-09-reliability-first.md | 已实现（Task 11 待数据）`（按实际完成情况填）
  - 新增"阶段 15"小节（目标 / 设计 / 约束 / 任务 / 状态，格式同其他阶段）
  - 「当前文件结构」表加 `doctor.py`、`tools/eval_voiceprint.py`
  - 变更记录追加一条：新增能力、`transcript.md` 新增 `- 声纹匹配：` 行、`config.toml` 新增 `[voiceprint] high/low`、`GET /api/doctor`、`POST /api/doctor/llm-test`、`POST /api/meetings/{id}/retranscribe`
  - 「已知未完成」更新：阶段 13/14 已提交；#2b 待 spike 结论另立计划；隐私文案（"音频与声纹始终留在本机"在启用 `[llm]` 后不严谨）与评估数据收集记为后续
- [ ] **Step 2: README 同步**：`config.toml` 示例加 `[voiceprint]`；自检页、补转写、「疑似」含义各一段，句子要短。
- [ ] **Step 3: 全量测试** —— Expected: `OK`
- [ ] **Step 4: 提交** —— `git add docs/plan/meeting-recorder-master-plan.md README.md docs/superpowers/plans/2026-11-09-reliability-first.md docs/superpowers/specs/2026-11-09-reliability-first-design.md docs/product/meeting-recorder-product-design.md`；`docs: 登记阶段 15 可信度优先`
