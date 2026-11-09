# 可信度优先 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让会议录音可恢复、声纹识别带置信度分档、提供只读自检页和可重复的离线评估脚本，并用 spike 确定如何避开 CoreAudio 卡死。

**Architecture:** 全部沿用总计划的"纯函数 + 参数注入"风格。「疑似」标签与阈值集中在 `voiceprint.py`；表头解析收敛到 `meeting_store.parse_header`；「未完成」由文件推导加内存集合，不新增状态文件；doctor 是新增模块 `doctor.py`，只读；评估脚本放 `tools/`，不进运行时。录音隔离方案由 Task 8 的 spike 实测决定，实现另立计划。

**Tech Stack:** Python 3.14、stdlib `unittest`/`tomllib`/`threading`、FastAPI、原生 JS（无构建）、sounddevice、numpy。不新增依赖。

**Spec:** docs/superpowers/specs/2026-11-09-reliability-first-design.md（背景：docs/plan/meeting-recorder-master-plan.md）

## Global Constraints

- 工作目录 `/Users/roc.peng/Documents/Python/meeting-recorder`；测试命令 `.venv/bin/python -m unittest discover -s tests -t . -v`；单模块 `.venv/bin/python -m unittest tests.test_xxx -v`。
- 测试不联网、不加载真实模型、不用真实麦克风；任何测试都不得读写真实的 `~/.config/meeting-recorder/`（用 `mock.patch.object(voiceprint, "PROFILE_DIR", tmp)` 覆盖）。
- 声纹 JSON 权限 600、原子写；名字规则：字母/数字/中日韩文字/`_`/`-`，1–20 字，不得以"说话人"开头（本计划加：不得以"疑似"开头）。
- 无声纹时 `transcript.md` 输出与改动前逐字相同且不警告。
- Web：只绑定 `127.0.0.1`；变更类路由走 Origin 守卫，JSON 体要求 `application/json`（否则 415）；所有用户/转写文本只用 `textContent` 渲染；无 CDN、无前端构建。
- 大模型：API Key 只在服务端，不进任何响应/日志/错误信息；不跟随重定向。
- 说话人区分/声纹/实时转写/自检的任何错误只警告，不改变录音与基础转写的退出码。
- 注释和文档字符串写中文；面向用户的提示写中文，句子要短。
- 每次 git commit 前先运行 `afplay ~/custom-workspace/touch-key.mp3`；`git add` 只写明确路径；GPG 签名失败（`Card error`）不绕过，停下来告诉用户。

## Pre-flight：先核对现状（本计划写作时没有代码）

写计划时本机没有仓库，下列名字取自总计划文字，**均未对照代码**。开始 Task 1 前逐条核对；与实际不符时按实际改名，在 ledger 记一条 `Ruling` 再继续，不要为迎合计划去改已有接口。

| 假设 | 核对命令 |
|---|---|
| `voiceprint.label_turns`、`voiceprint.PROFILE_DIR`、`voiceprint.DEFAULT_THRESHOLD`、名字校验函数存在 | `grep -n "def label_turns\|PROFILE_DIR\|DEFAULT_THRESHOLD\|说话人" voiceprint.py` |
| `transcribe.py` 里有渲染 `transcript.md` 的函数（下称 `render`）和 `- 说话人：` 表头行 | `grep -n "说话人：" transcribe.py meeting_summary.py` |
| `meeting_summary.py` 解析表头并有"列发言人"函数 | `grep -n "^def " meeting_summary.py` |
| `diarize.ensure_models()`、`live.ensure_vad_model()` 的缺失判断与下载是否混在一起 | `grep -n "def ensure_" diarize.py live.py` |
| `WavWriter` 何时回写 wav 头（决定被杀后头里的长度是否为 0） | `grep -n "class WavWriter" -A30 record.py` |
| `web_server.py` 的 Origin 守卫与 415 的复用方式、测试里用的客户端 | `grep -n "Origin\|415" web_server.py tests/test_web_server.py` |
| 录音相关线程的名字（open/close stream） | `grep -n "Thread(" record.py` |

## Review Focus

- 被杀的录音：wav 头里数据长度为 0 或大于文件实际长度，补转写仍要读出真实样本（Task 7 Step 1）。
- 用户在转写进行中再点"补转写"，或正在录音时点：返回 409，不重复转写（Task 7 Step 3）。
- 已有 `transcript.md` 的会议被自动流程碰到：内容逐字不变；"重新转写"才生成 `.bak`（Task 7 Step 3）。
- `config.toml` 没有 `[voiceprint]` 段、值是字符串或布尔、`low>=high`：中文告警并退回默认值，不崩溃（Task 2）。
- 麦克风枚举卡死：自检接口在超时内返回"设备枚举无响应"，不挂起（Task 5 Step 3）。
- 「测试连接」遇到 3xx 重定向或错误体含 Key：不跟随、响应不含 Key（Task 6）。

---

### Task 1: 提交阶段 13、14（#0）

**Files:**
- Modify (commit only): `record.py`、`tests/test_record.py`、`tests/test_meeting_controller.py`、`interview_questions.py`、`tests/test_interview_questions.py`、`README.md`、`docs/superpowers/specs/2026-10-29-interview-helper-design.md`

**Interfaces:**
- Produces: 干净的工作区，后续任务的 BASE。

- [ ] **Step 1: 跑全量测试，确认基线是绿的**

Run: `.venv/bin/python -m unittest discover -s tests -t . -v 2>&1 | tail -5`
Expected: `OK`。不绿则停下，把失败列给用户，不要带着红灯提交。

- [ ] **Step 2: 看工作区只包含这两个阶段的改动**

Run: `git status --short`
Expected: 只出现上面 Files 列出的路径（加 `docs/` 新文件）。出现别的路径就停下确认。

- [ ] **Step 3: 提交阶段 13**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add record.py tests/test_record.py tests/test_meeting_controller.py
git add -p README.md   # 只选与"打开麦克风超时"相关的 hunk
git commit -m "fix: 打开麦克风加 15 秒超时，避免系统音频卡住时界面无响应"
```
Expected: 提交成功；`git show --stat HEAD` 不含面试相关文件。

- [ ] **Step 4: 提交阶段 14**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add interview_questions.py tests/test_interview_questions.py README.md docs/superpowers/specs/2026-10-29-interview-helper-design.md
git commit -m "feat: 面试问题改为约 40 分钟并附答案要点"
```
Expected: `git status --short` 无输出（本计划与 spec 文件另行提交，见 Task 9）。

---

### Task 2: 声纹标签与阈值（#5 基础）

**Files:**
- Modify: `voiceprint.py`
- Test: `tests/test_voiceprint.py`

**Interfaces:**
- Produces（供 Task 3、4、5 使用）:
  - `SUSPECT_PREFIX = "疑似"`
  - `suspect_label(name: str) -> str`：`"张三"→"疑似张三"`
  - `parse_label(label: str) -> tuple[str, bool]`：返回 `(名字, 是否疑似)`，只剥一层前缀
  - `class Thresholds(NamedTuple): high: float; low: float; warning: str | None = None`
  - `DEFAULT_HIGH = 0.5`、`DEFAULT_LOW = 0.35`（均标"未校准"）；`DEFAULT_THRESHOLD = DEFAULT_HIGH` 保留为别名
  - `load_thresholds(config_path: Path | None, cli_high: float | None = None) -> Thresholds`
  - `classify_score(score: float, th: Thresholds) -> str`：`"named"` / `"suspect"` / `"anonymous"`

- [ ] **Step 1: 写失败测试**

在 `tests/test_voiceprint.py` 新增，断言如下（配置文件用 `tempfile` 写 toml）：
- `test_parse_label`：`"疑似张三"→("张三",True)`；`"张三"→("张三",False)`；`"说话人A"→("说话人A",False)`；`"疑似疑似张三"→("疑似张三",True)`
- `test_suspect_label_roundtrip`：`parse_label(suspect_label("张三")) == ("张三", True)`
- `test_register_rejects_suspect_prefix`：名字 `"疑似张三"` 被名字校验拒绝（沿用现有"说话人"前缀被拒的断言方式）
- `test_thresholds_default`：配置文件不存在 / 没有 `[voiceprint]` 段 → `Thresholds(0.5, 0.35, None)`
- `test_thresholds_valid`：`high=0.6, low=0.4` → `(0.6, 0.4, None)`
- `test_thresholds_invalid`（四条子用例）：`low>=high`、`low=-0.1`、`high="abc"`、`high=true` → 都得 `(0.5, 0.35, warning)`，`warning` 非空且含中文
- `test_cli_high_overrides_high_only`：配置 `low=0.4`，`cli_high=0.7` → `(0.7, 0.4, None)`；`cli_high=0.3` → 退回默认并带 warning
- `test_classify_boundaries`：`Thresholds(0.6,0.4)` 下 `0.6→named`、`0.5999→suspect`、`0.4→suspect`、`0.3999→anonymous`

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_voiceprint -v 2>&1 | tail -15`
Expected: FAIL，报 `AttributeError` 或 `ImportError`（新名字未定义）。

- [ ] **Step 3: 实现上面 Interfaces 里的全部名字**

在 `voiceprint.py` 实现；`load_thresholds` 用 `tomllib` 读 `[voiceprint]` 的 `high`/`low`，类型必须是 int/float 且不是 bool；先应用 `cli_high` 覆盖 high，再校验 `0 < low < high <= 1`；失败返回默认值加中文 `warning`。名字校验加"以 `SUSPECT_PREFIX` 开头则拒绝"。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest tests.test_voiceprint -v 2>&1 | tail -5 && .venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: 两次都 `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add voiceprint.py tests/test_voiceprint.py
git commit -m "feat: 声纹增加疑似标签与高低两个阈值"
```

---

### Task 3: 按说话人聚类分档（#5）

**Files:**
- Modify: `voiceprint.py`（`label_turns` 附近）、`transcribe.py`（调用处）
- Test: `tests/test_voiceprint.py`、`tests/test_transcribe.py`

**Interfaces:**
- Consumes: Task 2 的 `Thresholds`、`classify_score`、`suspect_label`。
- Produces:
  - `label_turns_with_scores(turns, ..., thresholds: Thresholds | None = None) -> tuple[list, dict[str, float]]`：参数沿用现有 `label_turns`；返回标注后的 turns 与 `{显示名: 该聚类最高分}`；显示名是 `"张三"` 或 `"疑似张三"`
  - `label_turns(...)` 保留，返回值不变（只取前者），供旧调用方使用
  - `transcribe.py` 把 `Thresholds` 来源接到 `voiceprint.load_thresholds(config_path, cli_high=args.voice_threshold)`，告警走现有的"只警告"通道

- [ ] **Step 1: 写失败测试**

用假的向量与假的匹配函数（沿用现有 `label_turns` 测试的注入方式）：
- `test_cluster_high_score_gets_real_name`：聚类分 `0.7`（high=0.6）→ 该聚类所有 turn 的 `speaker == "张三"`
- `test_cluster_gray_zone_gets_suspect`：分 `0.5` → `"疑似张三"`，且同一聚类的每一句都一样（不是逐句判断）
- `test_cluster_low_stays_anonymous`：分 `0.3` → 保持原匿名序号
- `test_two_clusters_same_person`：两个聚类都匹配张三时，只有分更高的拿名字，另一个保持匿名（沿用现有"同一个名字只给一个聚类"的行为；核对现状后若无此行为，在 ledger 记 Ruling 并跳过本条）
- `test_no_profiles_output_unchanged`：没有声纹时，返回的 turns 与改动前逐项相同，scores 为 `{}`
- 在 `tests/test_transcribe.py`：`--voice-threshold 0.3` 低于配置 low 时，stderr 有中文告警且转写继续

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_voiceprint tests.test_transcribe -v 2>&1 | tail -15`
Expected: FAIL（`label_turns_with_scores` 未定义）。

- [ ] **Step 3: 实现**

以聚类为单位取最高匹配分，用 `classify_score` 决定：`named` 写真名；`suspect` 写 `suspect_label(名字)`；`anonymous` 不动。`label_turns` 改成调用 `label_turns_with_scores`。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add voiceprint.py transcribe.py tests/test_voiceprint.py tests/test_transcribe.py
git commit -m "feat: 声纹按说话人聚类分档，灰区标疑似"
```

---

### Task 4: 表头新增分数行与统一解析（#5）

**Files:**
- Modify: `transcribe.py`（render）、`meeting_store.py`、`meeting_summary.py`
- Test: `tests/test_transcribe.py`、`tests/test_meeting_store.py`、`tests/test_meeting_summary.py`

**Interfaces:**
- Consumes: Task 3 的 scores 字典；Task 2 的 `parse_label`。
- Produces:
  - 表头新增一行 `- 声纹匹配：张三 0.62、疑似李四 0.48`（逗号为中文顿号 `、`，分数两位小数）；`- 说话人：` 行格式不变
  - `meeting_store.parse_header(text: str) -> dict`：键 `"speakers": list[str]`（原样，含「疑似」）、`"voice_scores": dict[str, float]`
  - `meeting_summary` 的"列发言人"与"取某人发言"改用 `parse_header` 与 `parse_label`：疑似X 与 X 合并为同一人；结果里带 `"含疑似片段": bool`（字段名按现有响应风格核对后定）

- [ ] **Step 1: 写失败测试**

- `test_render_no_scores_byte_identical`：scores 为空时 render 输出与改动前的黄金样本逐字相同
- `test_render_scores_line`：scores `{"张三":0.62,"疑似李四":0.48}` → 含整行 `- 声纹匹配：张三 0.62、疑似李四 0.48`，且 `- 说话人：` 行与无分数时相同
- `test_parse_header_old_format`：没有「声纹匹配」行 → `voice_scores == {}`，`speakers` 正确
- `test_parse_header_new_and_gray`：新格式、含「疑似」各一条，分数解析为 float
- `test_summary_merges_suspect`：文字稿里有 `张三` 与 `疑似张三` 两种发言 → 发言人列表只有「张三」一项，取其发言时两部分都在，结果标出含疑似片段
- `test_summary_anonymous_untouched`：`说话人A` 的行为与改动前一致

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_transcribe tests.test_meeting_store tests.test_meeting_summary -v 2>&1 | tail -15`
Expected: FAIL（`parse_header` 未定义等）。

- [ ] **Step 3: 实现**

在 `meeting_store.py` 实现 `parse_header`；Pre-flight 若已有统一表头解析函数，则扩展它而不是新增，Ruling 记入 ledger。把其余读表头的地方（`meeting_summary.py`、会议列表）全部改用它。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add transcribe.py meeting_store.py meeting_summary.py tests/test_transcribe.py tests/test_meeting_store.py tests/test_meeting_summary.py
git commit -m "feat: 文字稿表头记录声纹分数，发言归纳合并疑似与真名"
```

---

### Task 5: 模型存在性检查与 doctor 核心（#1）

**Files:**
- Modify: `diarize.py`、`live.py`
- Create: `doctor.py`
- Test: `tests/test_diarize.py`、`tests/test_live.py`、`tests/test_doctor.py`

**Interfaces:**
- Produces:
  - `diarize.models_present(cache_dir: Path) -> list[str]`、`live.models_present(cache_dir: Path) -> list[str]`：返回**缺失**的模型文件名（相对名，不含目录）；0 字节文件算缺失；零网络零写入；`ensure_*` 内部改用它判断缺失
  - `doctor.Check`：`@dataclass(frozen=True) class Check: name: str; status: str; detail: str`，`status ∈ {"ok","warn","fail"}`
  - `doctor.call_with_timeout(fn: Callable[[], T], seconds: float) -> tuple[bool, T | BaseException | None]`：在守护线程里跑 `fn`；超时返回 `(False, None)`；抛异常返回 `(True, 异常对象)`
  - `doctor.run_checks(*, list_input_devices: Callable[[], list[str]], free_bytes: Callable[[], int], models_missing: Callable[[], list[str]], llm_config: dict | None, voiceprint_modes: Callable[[], list[int]], lingering_threads: Callable[[], int], device_timeout_s: float = 3.0) -> list[Check]`
  - 固定检查名：`麦克风`、`磁盘`、`模型缓存`、`大模型配置`、`声纹文件权限`、`残留音频线程`；`detail` 一律是固定中文短句，**不含绝对路径、设备序列号、异常原文**
  - 常量 `MIN_FREE_BYTES = 2 * 1024**3`（低于则 `warn`）

- [ ] **Step 1: 写失败测试**

`tests/test_diarize.py`、`tests/test_live.py`：`models_present` 对（目录为空 / 文件齐全 / 其中一个 0 字节）各一条，用 `tempfile`，断言返回列表；并断言调用前后目录内容不变、没有调用下载函数（注入假下载函数，被调用即失败）。

`tests/test_doctor.py`：
- `test_all_ok`：全部注入正常值 → 六项都是 `ok`
- `test_mic_hang_times_out`：`list_input_devices` 阻塞（`threading.Event().wait()`）、`device_timeout_s=0.2` → 在 1 秒内返回，`麦克风` 项为 `fail`，detail 为 `"设备枚举无响应"`
- `test_no_input_device`：返回 `[]` → `麦克风` 为 `fail`
- `test_low_disk`：`free_bytes` 返回 `MIN_FREE_BYTES-1` → `warn`
- `test_models_missing`：`["a.onnx"]` → `fail`，detail 里**不含**文件路径
- `test_llm_config`：缺 `api_key` → `warn`（未配置不是故障）；齐全且 `base_url` 以 `http` 开头 → `ok`；`base_url` 非法 → `fail`；断言全程没有网络调用（注入的 opener 若被调用即失败）
- `test_voiceprint_perm`：模式含 `0o644` → `warn`；全 `0o600` → `ok`
- `test_lingering_threads`：`>0` → `warn`
- `test_no_sensitive_text`：把所有 detail 拼接，断言不含 `str(Path.home())`、不含 `"sk-"`、不含传入的假设备序列号

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_diarize tests.test_live tests.test_doctor -v 2>&1 | tail -15`
Expected: FAIL（`models_present`、`doctor` 未定义）。

- [ ] **Step 3: 实现**

先在 `diarize.py`/`live.py` 实现 `models_present` 并让 `ensure_*` 复用；再写 `doctor.py`。`run_checks` 只调用注入的函数，自己不碰文件系统、网络、sounddevice。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add diarize.py live.py doctor.py tests/test_diarize.py tests/test_live.py tests/test_doctor.py
git commit -m "feat: 新增 doctor 自检核心与模型存在性检查"
```

---

### Task 6: 「测试连接」（#1）

**Files:**
- Modify: `llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: 现有 `llm_client` 的 opener 注入方式与 `web_config.load_llm` 的配置结构（Pre-flight 核对）。
- Produces: `llm_client.test_connection(cfg: dict | None, *, opener=None) -> tuple[bool, str]`，返回 `(是否通, 固定中文原因)`。原因只能取这个集合：`"连接成功"`、`"未配置大模型"`、`"无法连接，请检查地址和网络"`、`"认证失败，请检查 API Key"`、`"服务返回错误"`、`"请求超时"`、`"地址发生了重定向，已拒绝"`。发送的是最小请求（一条 `max_tokens=1` 的消息）；**不跟随重定向**，**绝不回传服务器原文**。

- [ ] **Step 1: 写失败测试**（用注入的假 opener，不联网）

- `test_ok`：假 opener 返回 200 → `(True, "连接成功")`
- `test_not_configured`：`cfg=None` → `(False, "未配置大模型")`，opener 未被调用
- `test_auth_failed`：抛 401 的 `HTTPError` → `"认证失败，请检查 API Key"`
- `test_server_error_body_has_key`：假 opener 抛 500，错误体是 `"bad key sk-secret-123"`，cfg 的 `api_key="sk-secret-123"` → 返回原因是 `"服务返回错误"`，**断言 `"sk-secret-123" not in 返回的原因` 且 not in `repr(返回值)`**
- `test_redirect_refused`：假 opener 抛 302 的 `HTTPError` → `"地址发生了重定向，已拒绝"`，且没有发出第二次请求
- `test_timeout`：抛 `TimeoutError` → `"请求超时"`；抛 `URLError` → `"无法连接，请检查地址和网络"`

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_llm_client -v 2>&1 | tail -10`
Expected: FAIL（`test_connection` 未定义）。

- [ ] **Step 3: 实现**

复用 `llm_client` 里现有的"不跟随重定向"opener 构造；捕获异常后只映射到上面的固定原因，不拼接任何异常文本。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add llm_client.py tests/test_llm_client.py
git commit -m "feat: 大模型测试连接，只返回固定中文原因"
```

---

### Task 7: 恢复中断的录音（#3）

**Files:**
- Modify: `common.py`、`meeting_store.py`、`transcribe.py`（落盘）、`meeting_controller.py`、`web_server.py`、`web/static/app.js`、`web/static/index.html`
- Test: `tests/test_common.py`、`tests/test_meeting_store.py`、`tests/test_meeting_controller.py`、`tests/test_web_server.py`

**Interfaces:**
- Consumes: Task 4 的 `parse_header`（不直接用，仅保证表头写入走新的落盘函数）。
- Produces:
  - `common.atomic_write_text(path: Path, text: str) -> None`：同目录写临时文件再 `os.replace`
  - `common.read_wav_tolerant(path: Path) -> tuple[int, numpy.ndarray]`：返回 `(采样率, int16 样本)`；头里的数据长度为 0 或超过文件实际长度时，改用文件实际剩余字节；样本数为 0 抛 `ValueError("录音文件为空或损坏")`
  - `meeting_store.list_incomplete(root: Path, transcribing: Collection[str]) -> list[str]`：返回会议 id（目录名），条件：有 `meeting.wav`、没有 `transcript.md`、不在 `transcribing` 里；新到旧排序；只做 `stat`，不读文件内容
  - `GET /api/meetings` 每项新增布尔字段 `incomplete`
  - `MeetingController.retranscribe(meeting_id: str, *, force: bool = False) -> None`；异常 `MeetingBusy`（正在录音或该会议正在转写）→ 路由返回 409；`AlreadyTranscribed`（已有 transcript 且 `force=False`）→ 409；坏 wav → 422，消息 `"录音文件损坏，无法转写"`
  - `POST /api/meetings/{id}/retranscribe`，JSON 体 `{"force": bool}`，走 Origin 守卫与 415
  - 控制器内存集合 `_transcribing: set[str]`（加锁）；会后正常转写也进出这个集合

- [ ] **Step 1: 写失败测试**

- `tests/test_common.py`
  - `test_atomic_write_no_partial`：注入使 `os.replace` 抛异常 → 目标文件内容仍是旧内容，临时文件被清理
  - `test_read_wav_tolerant_header_zero`：手造 wav，头里 data 长度写 0，后面跟 `32000` 字节 → 读出 `16000` 个样本，采样率 `16000`
  - `test_read_wav_tolerant_header_too_long`：头里长度大于实际 → 读出实际样本数
  - `test_read_wav_empty`：只有头 → 抛 `ValueError`
- `tests/test_meeting_store.py`：`test_list_incomplete` 覆盖四种目录：有 wav 无 transcript（命中）、有 transcript（不命中）、在 `transcribing` 里（不命中）、没有 wav（不命中）；并断言实现没有 `open()` 过任何文件（用 `mock.patch("builtins.open")` 包住扫描调用）
- `tests/test_meeting_controller.py`（假转写函数、假录音）
  - `test_retranscribe_fills_gap`：只有 wav → 生成 transcript，调用后集合为空
  - `test_retranscribe_busy_while_transcribing`：转写进行中再调用 → `MeetingBusy`，转写函数只被调用一次
  - `test_retranscribe_busy_while_recording`：录音中 → `MeetingBusy`
  - `test_existing_transcript_untouched`：已有 transcript，`force=False` → `AlreadyTranscribed`，文件内容逐字不变
  - `test_force_makes_bak`：`force=True` → 旧内容在 `transcript.md.bak`，新文件写入；已有 `.bak` 时被覆盖
  - `test_set_cleared_on_failure`：转写函数抛异常 → 集合仍被清空
  - `test_bad_wav_message`：空 wav → 抛出带 `"录音文件损坏，无法转写"` 的错误
- `tests/test_web_server.py`：路由 409 / 422 / 415 / Origin 守卫各一条；列表接口 `incomplete` 字段

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_common tests.test_meeting_store tests.test_meeting_controller tests.test_web_server -v 2>&1 | tail -20`
Expected: FAIL（新名字未定义）。

- [ ] **Step 3: 实现后端**

`transcribe.py` 写 `transcript.md` 改用 `atomic_write_text`；读 wav 处改用 `read_wav_tolerant`（Pre-flight 若现有读取器已容忍坏头，则复用并只补缺口，记 Ruling）。控制器在转写开始/结束（含异常）处维护 `_transcribing`。

- [ ] **Step 4: 实现前端并手动冒烟**

`app.js` 在历史列表里给 `incomplete` 的会议加「未完成」徽标和「补转写」按钮（文本一律 `textContent`）；转写中按钮置灰；已有 transcript 的会议提供「重新转写」，点击前 `confirm` 一次，说明旧稿会备份为 `.bak`。

冒烟：用 `spike/` 里造一个只有 `meeting.wav` 的会议目录，启动服务 `.venv/bin/python web.py`（用实际入口名），页面应出现「未完成」，点「补转写」后变为已完成。Expected: 页面徽标消失，目录里出现 `transcript.md`。

- [ ] **Step 5: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 6: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add common.py meeting_store.py transcribe.py meeting_controller.py web_server.py web/static/app.js web/static/index.html tests/test_common.py tests/test_meeting_store.py tests/test_meeting_controller.py tests/test_web_server.py
git commit -m "feat: 恢复中断的录音，补转写与重新转写"
```

---

### Task 8: 自检接口与页面（#1）

**Files:**
- Modify: `web_server.py`、`record.py`、`web/static/index.html`、`web/static/app.js`
- Test: `tests/test_web_server.py`、`tests/test_record.py`

**Interfaces:**
- Consumes: Task 5 的 `doctor.run_checks`；Task 6 的 `llm_client.test_connection`；`diarize.models_present`、`live.models_present`。
- Produces:
  - `record.lingering_stream_threads() -> int`：当前仍存活的打开/关闭麦克风流线程数（线程名统一加前缀 `mr-stream-`，Pre-flight 核对后若已有名字则沿用）
  - `GET /api/doctor` → `{"checks": [{"name","status","detail"}]}`，不走 Origin 守卫（只读），响应不含绝对路径
  - `POST /api/doctor/llm-test` → `{"ok": bool, "reason": str}`，走 Origin 守卫与 415
  - 页面「自检」区：列出各项，颜色用现有状态色类，文本 `textContent`；「测试连接」按钮

- [ ] **Step 1: 写失败测试**

- `tests/test_record.py`：`test_lingering_threads_count`：起一个名为 `mr-stream-x` 的存活线程，计数 `1`；线程结束后 `0`
- `tests/test_web_server.py`
  - `test_doctor_get_no_paths`：注入假依赖，响应 JSON 序列化后不含 `str(Path.home())`
  - `test_doctor_get_is_readonly`：断言没有调用任何下载或网络函数
  - `test_llm_test_requires_origin`：Origin 不对 → 拒绝；Content-Type 非 JSON → 415
  - `test_llm_test_never_leaks_key`：假服务器错误体含 Key → 响应不含 Key
  - `test_doctor_get_returns_while_mic_hangs`：枚举阻塞，接口在超时后返回（总耗时 < 5 秒）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_record tests.test_web_server -v 2>&1 | tail -15`
Expected: FAIL。

- [ ] **Step 3: 实现后端与前端**

路由里把真实依赖（`sounddevice.query_devices`、`shutil.disk_usage`、两个 `models_present`、`os.stat` 读声纹目录权限、`record.lingering_stream_threads`）作为默认值装配，测试注入假的。设备枚举**只取名称列表**，不返回序列号。

- [ ] **Step 4: 运行确认通过，再跑全量，并手动冒烟**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。冒烟：启动服务，页面「自检」区显示六项；故意把 `[llm]` `api_key` 清空，对应项为「未配置」而非故障。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add web_server.py record.py web/static/index.html web/static/app.js tests/test_web_server.py tests/test_record.py
git commit -m "feat: 自检接口与页面，测试连接走 POST"
```

---

### Task 9: 录音隔离 spike（#2a）

**Files:**
- Create: `spike/mic_isolation_probe.py`
- Create: `docs/superpowers/plans/2026-11-09-mic-isolation-spike-result.md`
- Create（提交本计划与 spec）: `docs/superpowers/plans/2026-11-09-reliability-first.md`、`docs/superpowers/specs/2026-11-09-reliability-first-design.md`

**Interfaces:**
- Produces: 一份结论文档，含下表（每格填实测值）和**唯一的结论**：选哪一种方案进 #2b。#2b 的实现计划另写，不在本计划内。

这个任务需要真实麦克风，不进自动测试（Global Constraints）。它是实验，不走 TDD；验收是结论文档里的数据完整。

- [ ] **Step 1: 写探针脚本**

`spike/mic_isolation_probe.py`，命令行 `--mode persistent|abort|subprocess`，`--cycles N`（默认 20）：
- `persistent`：进程内只开一次 `sd.InputStream`，循环 N 次「开始写盘 / 停止写盘」，回调在不写盘时直接返回，不拷贝不入队
- `abort`：每轮新开流，结束时用 `stream.abort()` 然后 `close()`，记录每次 `abort`/`close` 耗时
- `subprocess`：每轮在子进程里开流录 2 秒写 wav，父进程只收文件，记录子进程退出耗时
- 每种模式输出：轮数、每轮开流耗时（最大/平均）、是否出现超过 5 秒的卡顿

- [ ] **Step 2: 运行三种模式并记录**

Run: `.venv/bin/python spike/mic_isolation_probe.py --mode persistent --cycles 20`（另外两种同理）
Expected: 每种模式各打印一份汇总；把输出贴进结论文档。

- [ ] **Step 3: 复现卡死场景（尽力而为）**

在每种模式下，一边录音一边在系统里切换输入设备（含蓝牙耳机插拔），观察是否再出现开流超过 15 秒。记录"复现 / 未复现 / 不适用"。**未复现不等于已修复**，在结论里照实写。

- [ ] **Step 4: 常驻流专项验收（仅当 persistent 模式要入选时做）**

- 空转 1 小时的 CPU 占用（`ps -o %cpu -p <pid>` 采样，取平均与最大）
- 菜单栏麦克风指示灯是否一直亮（目视，记录）
- 录音中切换蓝牙耳机后，流能否自动恢复，还是需要重开

- [ ] **Step 5: 写结论文档**

表格列：方案 / 20 轮最大开流耗时 / 是否出现卡顿 / 切设备后表现 / 空转 CPU（仅常驻流）/ 改动面（估计要动哪几个文件）。结论一句话：选哪个，以及 `--no-isolate` 回退开关怎么接。任何一项没测，写"未测"并说明原因。

- [ ] **Step 6: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add spike/mic_isolation_probe.py docs/superpowers/plans/2026-11-09-mic-isolation-spike-result.md docs/superpowers/plans/2026-11-09-reliability-first.md docs/superpowers/specs/2026-11-09-reliability-first-design.md
git commit -m "docs: 可信度优先计划与录音隔离 spike 结论"
```

---

### Task 10: 离线评估算法（#4）

**Files:**
- Create: `tools/__init__.py`、`tools/eval_voiceprint.py`
- Test: `tests/test_eval_voiceprint.py`
- Modify: `.gitignore`（加一行 `eval/`）

**Interfaces:**
- Produces:
  - `far_frr(genuine: Sequence[float], impostor: Sequence[float], threshold: float) -> tuple[float, float]`：FAR = 冒名分数 `>= threshold` 的比例；FRR = 本人分数 `< threshold` 的比例；任一列表为空抛 `ValueError("样本为空")`
  - `@dataclass(frozen=True) class Recommendation: high: float | None; low: float | None; limited: bool; note: str`
  - `recommend_thresholds(genuine, impostor, *, min_genuine: int = 10, min_impostor: int = 20, min_gap: float = 0.05) -> Recommendation`
  - `Segment = tuple[float, float, str]`（起、止、说话人）；`diarization_error_rate(reference: list[Segment], hypothesis: list[Segment], *, step: float = 0.01) -> float`：按帧比较；说话人映射在假设说话人数 ≤ 6 时穷举排列取最优，超过抛 `ValueError`；分母是参考语音帧数；假设片段互不重叠

**阈值规则（与 CEO 计划原文有一处偏差，见下方 Ruling）：**
- `high` = 候选分数中 FAR ≤ 1% 的最小值；
- `low` = 候选分数中 FAR ≤ 10% 的最小值；
- 若 `low >= high`（两类分数分离很好）：`low = high - min_gap`，`note` 写明"灰区取最小宽度"；
- 找不到满足 FAR ≤ 1% 的候选 → `high=None, low=None`，`note="两类分数无法区分"`；
- 样本数低于 `min_genuine`/`min_impostor` → `limited=True`，`note` 含 `"置信有限，阈值仅作初值"`。

**Ruling（写进 ledger）：** CEO 计划写的是"low = FRR ≤ 10% 的最高阈值"。当分数分离良好时，这个值会高于 high，灰区消失且违反 `0 < low < high` 的校验。改为"FAR ≤ 10% 的最小阈值"后 low 永远 ≤ high，且语义是"灰区里的疑似结论冒名率不超过 10%"。代价：若用户更关心漏认本人，需要另议。

- [ ] **Step 1: 写失败测试**（全部用手造分数集，不读文件、不用音频）

- `test_far_frr_basic`：本人 `[0.8,0.7,0.6,0.4]`、冒名 `[0.5,0.3,0.2,0.1]`，阈值 `0.5` → `(0.25, 0.25)`
- `test_far_frr_empty`：空列表抛 `ValueError`
- `test_recommend_separable`：本人 `0.7~0.9` 各 12 个、冒名 `0.1~0.3` 各 25 个 → `high ≈ 最小满足 FAR≤1% 的值`，`low == high - 0.05`，`limited is False`
- `test_recommend_overlap`：构造重叠分布（手算期望值写进断言），断言 `high` 与 `low` 的具体数值且 `0 < low < high`
- `test_recommend_far_exactly_one_percent`：冒名 100 个，恰好 1 个 `>= t` → 该 t 被接受
- `test_recommend_indistinguishable`：本人全低于冒名 → `high is None`
- `test_recommend_small_sample_limited`：本人 3 个、冒名 5 个 → `limited is True` 且 note 含 `"置信有限，阈值仅作初值"`
- `test_der_perfect`：→ `0.0`
- `test_der_label_swap`：假设里 A/B 与参考互换 → `0.0`
- `test_der_half_wrong`：参考 0–10s 全是 A，假设 0–5s 是 A、5–10s 是静音 → `0.5`（漏检）
- `test_der_too_many_speakers`：7 个假设说话人 → `ValueError`

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_eval_voiceprint -v 2>&1 | tail -15`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现上面三个函数**

纯函数，不 import `voiceprint`/`sherpa_onnx`/`mlx_whisper`，不读任何目录。

- [ ] **Step 4: 运行确认通过，再跑全量**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add tools/__init__.py tools/eval_voiceprint.py tests/test_eval_voiceprint.py .gitignore
git commit -m "feat: 声纹离线评估算法（FAR/FRR、推荐阈值、DER）"
```

---

### Task 11: 用真实数据评估并校准（#4 / #5，**数据到位后才执行**）

**前置条件：** `~/.config/meeting-recorder/eval/` 下有 ≥2 场真实会议，每场 ≥3 位说话人、≥1 位已登记声纹，并有手工标注 `ref.json`（`{"segments": [[起, 止, "说话人"]], "enrolled": {"说话人": "登记名"}}`）；可靠阈值另需 ≥5 位冒名者。**条件不满足就停在这里，把本任务留给用户，不要用别的数据凑。**

**Files:**
- Modify: `tools/eval_voiceprint.py`（加命令行 `collect_scores` 与 `main`）、`voiceprint.py`（只改 `DEFAULT_HIGH`/`DEFAULT_LOW`）、`docs/plan/meeting-recorder-master-plan.md`

**Interfaces:**
- Consumes: Task 10 的三个函数；`diarize`、`voiceprint` 的公开函数。
- Produces: `collect_scores(eval_dir: Path) -> tuple[list[float], list[float]]`（本人分数、冒名分数）；`python -m tools.eval_voiceprint --dir <eval 目录>` 打印 FAR/FRR 表、推荐阈值、DER。

- [ ] **Step 1: 实现 `collect_scores` 与命令行**，复用现有区分与声纹匹配的公开函数；此步需要真实模型，无单测，只做手动冒烟。
- [ ] **Step 2: 运行**：`.venv/bin/python -m tools.eval_voiceprint --dir ~/.config/meeting-recorder/eval`。Expected: 打印推荐的 `high`/`low`；样本不足时显示"置信有限，阈值仅作初值"。
- [ ] **Step 3: 把推荐值写进 `voiceprint.DEFAULT_HIGH`/`DEFAULT_LOW`，去掉"未校准"注释**；若 `limited`，保留"仅作初值"注释。跑全量测试，Expected: `OK`。
- [ ] **Step 4: 更新总计划**：阶段 4 Task 8 勾选（若 `limited` 则注明"初值"），阶段 2 Task 5 记录 DER 数值。
- [ ] **Step 5: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add tools/eval_voiceprint.py voiceprint.py docs/plan/meeting-recorder-master-plan.md
git commit -m "feat: 用真实数据校准声纹阈值"
```

---

### Task 12: 登记阶段 15 与变更记录

**Files:**
- Modify: `docs/plan/meeting-recorder-master-plan.md`、`README.md`

- [ ] **Step 1: 总计划增补**

- 阶段索引表加一行：`15 | 2026-11-09 | 可信度优先 | docs/superpowers/plans/2026-11-09-reliability-first.md | 已实现（Task 11 待数据）`
- 新增"阶段 15"小节（目标 / 设计 / 约束 / 任务 / 状态，格式同其他阶段）
- 「当前文件结构」表加 `doctor.py`、`tools/eval_voiceprint.py`
- 变更记录追加一条 `2026-11-xx`：列出新增能力、`transcript.md` 新增 `- 声纹匹配：` 行、`config.toml` 新增 `[voiceprint] high/low`、`GET /api/doctor`、`POST /api/doctor/llm-test`、`POST /api/meetings/{id}/retranscribe`
- 「已知未完成」更新：阶段 13/14 已提交；#2b 录音隔离待 spike 结论后另立计划；隐私文案（"音频与声纹始终留在本机"在启用 `[llm]` 后不严谨）与评估数据收集记为后续事项

- [ ] **Step 2: README 同步**：`config.toml` 示例加 `[voiceprint]`；自检页、补转写、「疑似」含义各一段，句子要短。

- [ ] **Step 3: 全量测试**

Run: `.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3`
Expected: `OK`。

- [ ] **Step 4: 提交**

```bash
afplay ~/custom-workspace/touch-key.mp3
git add docs/plan/meeting-recorder-master-plan.md README.md
git commit -m "docs: 登记阶段 15 与变更记录"
```

---

## Self-Review 记录

- **Spec 覆盖：** #0→Task 1；#1→Task 5、6、8；#2a→Task 9；#3→Task 7；#4→Task 10、11；#5→Task 2、3、4；决策 1A→Task 7、1B→Task 4、1C→Task 5、1D→Task 6、1E→Task 8、1F→Task 2、2A→Task 2、2B→Task 4、2C→Task 7、3A→Task 10、4A→Task 9 Step 4 与 Task 5。#2b 实现不在本计划（spike 结论决定），已在 Task 9 和阶段 15 登记。
- **类型一致：** `Thresholds`、`parse_label`、`parse_header`、`Check`、`models_present` 在定义任务与使用任务里签名相同。
- **与评审的偏差：** 仅 Task 10 的 low 阈值规则一处，已写成 Ruling。
- **未核实项：** 全部集中在 Pre-flight 表，写作时无代码可对照。
