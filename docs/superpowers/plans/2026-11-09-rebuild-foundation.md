# meeting-recorder 底座重建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本仓库只有文档和离线评估脚本，没有业务代码。本计划从零实现总计划阶段 1–14 的全部能力（录音、转写、说话人区分、声纹、实时转写、Web 控制台、发言归纳、面试帮助），作为 `2026-11-09-reliability-first.md` 的前置。

**Architecture:** 纯函数与外部依赖分离；麦克风、模型、`afconvert`、磁盘、网络、大模型全部通过参数注入，测试用假对象。分层：`common.py` → `transcribe.py` / `record.py` / `diarize.py` / `voiceprint.py` / `live.py` → Web 层（FastAPI 单进程 + 原生 JS）。增强能力（区分、声纹、实时转写、大模型）失败只警告，不影响录音与基础转写。

**Tech Stack:** Python 3.13（本机 `/opt/homebrew/bin/python3.13`；总计划写的 3.14 本机没有，见 Ruling R1）、stdlib `unittest`/`tomllib`/`threading`、`sounddevice`、`numpy`、`mlx-whisper`、`sherpa-onnx`、`fastapi`/`uvicorn`/`httpx`（测试客户端）、原生 JS；macOS 自带 `afconvert`。

**Spec:** docs/plan/meeting-recorder-master-plan.md（行为与约束的权威来源）；docs/product/meeting-recorder-product-design.md（产品背景）；docs/superpowers/specs/2026-11-09-reliability-first-design.md（与本计划的接口约定见 Task 3、5、7、9）。

## Global Constraints

- 工作目录 `/Users/mima0000/Documents/Python/meeting-record`；解释器 `.venv/bin/python`（Task 1 创建）；测试命令 `.venv/bin/python -m unittest discover -s tests -t . -v`。
- 测试不联网、不加载真实模型、不用真实麦克风、不用真实音频；任何测试都不得读写真实的 `~/.config/meeting-recorder/`、`~/Meetings`、`~/.cache/meeting-recorder/`（用 `tempfile` 和 `mock.patch.object` 覆盖模块常量）。
- 录音 16 kHz 单声道 16 位 wav；转写模型默认 `mlx-community/whisper-large-v3-turbo`，语言 `zh`，固定 `condition_on_previous_text=False`，**不传 `initial_prompt`**。
- 输出根目录 `~/Meetings`，会议目录 `YYYY-MM-DD_HHMM`（重名加 `_2`、`_3`）；文件 `meeting.wav`、`meeting.m4a`、`transcript.md`、`live.txt`、`annotations.json`、`meta.json`（自定义标题）；启动录音前至少 1 GB 可用空间。
- 模型缓存 `~/.cache/meeting-recorder/models/`；声纹 `~/.config/meeting-recorder/voiceprints/`（JSON，权限 600，只存 512 维向量，原子写）；职位 `~/.config/meeting-recorder/jobs.json`。
- 名字规则：字母/数字/中日韩文字/`_`/`-`，1–20 字，不得以“说话人”开头。
- 说话人标签 `说话人A`…`说话人Z`，第 27 个起 `说话人27`；行格式 `[HH:MM:SS] 说话人A: 文字`；无区分时 `[HH:MM:SS] 文字`；表头含 `- 说话人：` 行才算已区分。
- 无声纹时 `transcript.md` 输出与未启用声纹逐字相同且不警告。区分/声纹/实时转写的任何错误只向 stderr 打中文警告，不改变录音和转写的退出码。
- Web：只绑定 `127.0.0.1`（`config.toml` 的 `[web] port`，默认 8765）；单 Uvicorn worker、不 auto-reload；同一时间最多一场录音；变更类路由走 Origin 守卫，JSON 体要求 `application/json`（否则 415）；静态资源与首页返回 `Cache-Control: no-cache`；所有用户/转写文本只用 `textContent` 渲染（面试结果经 DOMPurify 净化是唯一例外）；无 CDN、无前端构建。
- 大模型：`config.toml` 的 `[llm]`（`base_url`、`api_key`、`model`、可选 `timeout_s`=120、`max_input_chars`=60000），OpenAI Chat Completions 兼容；API Key 只在服务端，不进任何响应/日志/错误；不跟随重定向；未配置时服务照常启动，用到才 503。
- 注释和文档字符串写中文；面向用户的提示写中文，句子要短。
- 每次 git commit 前先运行 `afplay ~/custom-workspace/touch-key.mp3`；`git add` 只写明确路径；GPG 签名失败（`Card error`）不绕过，停下来告诉用户。

## Rulings（计划作者已定，执行时不要重新讨论）

- **R1 Python 版本：** 用 3.13（`uv venv --python /opt/homebrew/bin/python3.13 .venv`）。可信度优先计划里的“3.14”按 3.13 理解；代码不得使用 3.14 才有的语法。
- **R2 模型与维度：** 区分用 `sherpa-onnx-pyannote-segmentation-3-0`（release `speaker-segmentation-models`，解压得 `model.onnx`）+ 嵌入模型 `3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx`（release `speaker-recongition-models`，116 MB）。总计划要求声纹为 512 维；Task 1 的可行性门槛要实测该模型输出维度，不是 512 就停下报告，不要擅自换模型。
- **R3 与可信度优先计划的分工：** 以下接口由本计划直接建好，可信度优先计划里对应步骤改为“已存在则跳过”：`common.atomic_write_text`、`common.read_wav_tolerant`、`diarize.models_present`、`live.models_present`、`meeting_store.parse_header`、录音流线程统一前缀 `mr-stream-`。

## Review Focus

- 零时长或全程静音的录音：不调用模型，文字稿写“（未识别到语音）”（Task 2）。
- 录音中途被杀：wav 头里长度为 0 或大于实际，仍能读出已写入采样（Task 1、3）。
- 麦克风打不开：中文提示，不留空会议目录（Task 3）。
- 路径含空格或中文时 `afconvert` 调用正常（Task 3）。
- 会议 id 含 `../` 或绝对路径：所有会议路由拒绝，不越出输出根目录（Task 6）。
- 浏览器跨站请求：Origin 不是本机的变更类请求返回 403（Task 6）。
- 转写文本含 `<script>`：前端只经 `textContent` 渲染（Task 8）。

---

### Task 1: 工程骨架、环境与 common.py

**Files:** Create `requirements.txt`、`README.md`、`common.py`、`tests/test_common.py`、`spike/feasibility.py`；Modify `.gitignore`（已含 `.venv/`）

**Interfaces:**
- Produces: `SAMPLE_RATE = 16000`；`MEETINGS_ROOT = Path.home()/"Meetings"`；`MIN_FREE_BYTES = 1024**3`
- `meeting_dir_name(now: datetime) -> str`（`"2026-11-09_1030"`）；`unique_dir(root: Path, name: str) -> Path`（重名加 `_2`、`_3`，不创建）
- `format_ts(seconds: float) -> str`（`"HH:MM:SS"`，向下取整，超过 24 小时继续累加小时）
- `free_bytes(path: Path) -> int`；`is_silent(samples: np.ndarray, *, rms_threshold: float = 0.003) -> bool`（int16 输入，空数组为静音）
- `atomic_write_text(path: Path, text: str) -> None`：同目录写临时文件再 `os.replace`，失败清理临时文件
- `read_wav_tolerant(path: Path) -> tuple[int, np.ndarray]`：返回 `(采样率, int16)`；头里 data 长度为 0 或大于文件实际剩余字节时按实际剩余字节读；样本数 0 抛 `ValueError("录音文件为空或损坏")`

- [ ] **Step 1: 创建环境并跑可行性门槛。** Run: `uv venv --python /opt/homebrew/bin/python3.13 .venv && uv pip install --python .venv/bin/python numpy sounddevice mlx-whisper sherpa-onnx fastapi uvicorn httpx`；Expected: 全部装好。写 `spike/feasibility.py` 并运行：下载 R2 的两个模型到临时目录，用 `sherpa_onnx.SpeakerEmbeddingExtractor` 对 1 秒静音取向量，打印 `extractor.dim`。**验收：dim == 512**；不是则停下报告（R2）。同时确认 `python -c "import mlx_whisper"` 与 `afconvert -h` 可用。
- [ ] **Step 2: 写失败测试** `tests/test_common.py`：`test_meeting_dir_name`；`test_unique_dir_suffix`（已存在 `x`、`x_2` → `x_3`）；`test_format_ts`（`0→"00:00:00"`、`3661.9→"01:01:01"`、`90000→"25:00:00"`）；`test_is_silent`（全零、空数组 → True；幅值 3000 的正弦 → False）；`test_atomic_write_no_partial`（打补丁让 `os.replace` 抛异常 → 目标仍是旧内容，目录里无临时文件）；`test_read_wav_tolerant_header_zero`（手造 wav，头里 data 长度写 0，后跟 32000 字节 → 16000 个样本，采样率 16000）；`test_read_wav_tolerant_header_too_long`；`test_read_wav_empty`（只有 44 字节头 → `ValueError`）；`test_read_wav_normal`（头正确时与 `wave` 模块读出一致）。
- [ ] **Step 3: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_common -v 2>&1 | tail -15`；Expected: FAIL（模块不存在）。
- [ ] **Step 4: 实现 `common.py`** 上面全部名字；`read_wav_tolerant` 自己解析 RIFF 块（找 `fmt ` 与 `data`），不依赖 `wave` 对坏头的容忍。
- [ ] **Step 5: 运行确认通过；写 `requirements.txt`（版本取 `uv pip freeze` 实际值）和 `README.md` 骨架（安装、测试命令、目录说明，句子要短）。**
- [ ] **Step 6: 提交** —— `git add common.py tests/test_common.py spike/feasibility.py requirements.txt README.md`；`feat: 工程骨架与 common.py`

---

### Task 2: 转写核心 transcribe.py（渲染、过滤、对齐，不含声纹）

**Files:** Create `transcribe.py`、`tests/test_transcribe.py`

**Interfaces:**
- Consumes: Task 1 的 `common`。
- Produces: `DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"`；`Segment = dict`（键 `start`、`end`、`text`，可有 `no_speech_prob`）；`Turn = tuple[float, float, int | str]`（起、止、说话人）
- `transcribe_samples(samples: np.ndarray, *, model: str = DEFAULT_MODEL, transcribe_fn=None) -> list[Segment]`：调用 `mlx_whisper.transcribe(samples_float32, path_or_hf_repo=model, language="zh", condition_on_previous_text=False)`；`transcribe_fn` 注入替换；**不传 `initial_prompt`**
- `filter_segments(segs: list[Segment]) -> list[Segment]`：去掉空白文本和 `no_speech_prob > 0.6` 的片段，文本 `strip()`
- `speaker_label(spk: int | str) -> str`：`0→"说话人A"`、`25→"说话人Z"`、`26→"说话人27"`；str 原样返回
- `assign_speakers(segs: list[Segment], turns: list[Turn]) -> list[tuple[Segment, int | str | None]]`：按时间重叠最大者归属，无重叠为 `None`
- `render(segs, *, started: datetime, duration: float, speakers: list[str] | None = None, assigned=None) -> str`：输出见下方格式；零片段写 `（未识别到语音）`
- 文字稿格式：首行 `# 会议文字稿`；表头 `- 开始时间：YYYY-MM-DD HH:MM`、`- 时长：HH:MM:SS`，仅当 `speakers` 非 None 再加 `- 说话人：A、B`（顿号分隔）；空行；正文每段一行。
- `transcribe_wav(wav: Path, *, model, speakers: int | None, diarize: bool, deps: dict | None = None) -> str`：读 wav（`read_wav_tolerant`），零长度或 `is_silent` → 不调模型，直接 `render` 空结果；区分的错误捕获后 stderr 警告并退回无标签文字稿
- `main(argv: list[str] | None = None, *, deps: dict | None = None) -> int`：参数 `PATH`（wav 或会议目录）、`--speakers N`（整数 ≥1）与 `--no-diarize` 互斥（argparse 互斥组）、`--model`；写 `transcript.md` 用 `atomic_write_text`

- [ ] **Step 1: 写失败测试**（假 `transcribe_fn`，不加载模型）：`test_no_initial_prompt`（断言假函数收到的 kwargs 不含 `initial_prompt` 且 `condition_on_previous_text is False`）；`test_filter_drops_empty_and_nospeech`；`test_speaker_label`（0、25、26、含 str）；`test_assign_by_overlap`（两个说话人部分重叠取大者；无重叠 → `None`）；`test_render_plain_lines`（黄金样本逐字比较）；`test_render_with_speakers_header`（含 `- 说话人：说话人A、说话人B` 与 `[00:00:03] 说话人A: 你好`）；`test_render_empty`；`test_zero_length_wav_no_model_call`、`test_silent_wav_no_model_call`（假函数被调用即失败）；`test_diarize_failure_falls_back`（区分抛异常 → 文字稿无标签、stderr 有中文警告、退出码 0）；`test_speakers_and_no_diarize_exclusive`（argparse 退出码 2）。
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_transcribe -v 2>&1 | tail -15`；Expected: FAIL。
- [ ] **Step 3: 实现** 上面全部名字；`deps` 字典可含 `transcribe_fn`、`diarize_fn`、`stderr`。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— `git add transcribe.py tests/test_transcribe.py`；`feat: 转写核心与文字稿渲染`

---

### Task 3: 录音 record.py

**Files:** Create `record.py`、`tests/test_record.py`

**Interfaces:**
- Consumes: Task 1 的 `common`；Task 2 的 `transcribe.main` / `transcribe_wav`。
- Produces: `class WavWriter(path: Path, sample_rate: int = 16000)`：`write(samples: np.ndarray) -> None`、`close() -> None`；打开时写 44 字节头，**每累计 ≥1 秒音频回写一次头里的长度**（`HEADER_SYNC_S = 1.0`），`close` 最终回写；写入的 int16 小端
- `level_bar(rms: float, width: int = 20) -> str`；`class SilenceWatch(threshold_s: float = 10.0)`，`update(samples, now) -> bool` 在连续静音超阈值时首次返回 True（之后每恢复再静音重新计时）
- `STREAM_OPEN_TIMEOUT_S = 15.0`；`class RecorderError(Exception)`；`open_stream(factory: Callable[[], ContextManager], timeout: float = STREAM_OPEN_TIMEOUT_S)`：在**名为 `mr-stream-open` 的守护线程**里调用 `factory()` 并 `__enter__`，超时抛 `RecorderError("打开麦克风超时（系统音频卡住了），请重启服务后重试")`；工厂抛异常 → `RecorderError("无法打开麦克风：请检查权限和设备")`
- 关闭流在**名为 `mr-stream-close` 的守护线程**里做，主线程最多等 `STREAM_CLOSE_TIMEOUT_S = 5.0`，不阻塞
- `compress_m4a(wav: Path, m4a: Path, *, run=subprocess.run) -> bool`：调用 `afconvert -f m4af -d aac <wav> <m4a>`（参数列表形式，不经 shell），失败返回 False 并警告，不删 wav
- `record(out_root: Path, *, stream_factory, stop: threading.Event, now=datetime.now, on_level=None, on_samples=None, free_bytes_fn=common.free_bytes) -> Path`：可用空间 < `MIN_FREE_BYTES` 抛 `RecorderError("磁盘空间不足（至少需要 1 GB）")`；打开流**成功后**才创建会议目录（打不开不留空目录）；回调里只把块放入队列，主循环写盘；返回会议目录
- `main(argv=None, *, deps=None) -> int`：Ctrl-C 即停止 → 压缩 m4a → 调 `transcribe`；选项 `--out`、`--speakers`、`--no-diarize`、`--live`（Task 5 接入）；录音错误退出码 1，转写增强失败不改退出码

- [ ] **Step 1: 写失败测试**（假流、假 `run`，无真实音频）：`test_wavwriter_header_synced`（写 2.5 秒后**不 close**，用 `common.read_wav_tolerant` 读出 ≥2 秒样本，且头里长度字段 ≥ 2 秒字节数）；`test_wavwriter_close_exact`（与 `wave` 模块读出一致）；`test_level_bar`；`test_silence_watch`（10 秒静音触发一次，恢复后再静音再触发）；`test_open_stream_timeout`（工厂 `threading.Event().wait()`、`timeout=0.2` → `RecorderError` 含“打开麦克风超时”，0.2–1 秒内返回，线程名 `mr-stream-open`）；`test_open_stream_error`；`test_no_empty_dir_when_open_fails`（输出根下无新目录）；`test_low_disk`；`test_compress_m4a_args`（路径含空格和中文，断言参数是列表且无 shell）；`test_compress_failure_keeps_wav`；`test_record_stop_writes_wav`（假流产 3 块，置 stop → 会议目录有 `meeting.wav`）。
- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m unittest tests.test_record -v 2>&1 | tail -15`；Expected: FAIL。
- [ ] **Step 3: 实现。** 真实流工厂用 `sounddevice.InputStream(samplerate=16000, channels=1, dtype="int16", callback=...)`，只在 `main` 里装配，测试不碰。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 手动冒烟（真实麦克风，仅此一次）：** `.venv/bin/python record.py --no-diarize` 说几句话 Ctrl-C，应得到 `~/Meetings/<时间>/{meeting.wav,meeting.m4a,transcript.md}`。模型首次下载可能很慢；失败把 stderr 报给用户，不要伪造通过。
- [ ] **Step 6: 提交** —— `git add record.py tests/test_record.py`；`feat: 录音、m4a 压缩与打开麦克风超时`

---

### Task 4: 说话人区分 diarize.py

**Files:** Create `diarize.py`、`tests/test_diarize.py`；Modify `transcribe.py`（接入）

**Interfaces:**
- Consumes: Task 2 的 `Turn`、`assign_speakers`、`transcribe_wav`。
- Produces: `MODEL_DIR = Path.home()/".cache/meeting-recorder/models"`；`SEGMENTATION_FILE = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"`；`EMBEDDING_FILE = "3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx"`
- `models_present(cache_dir: Path) -> list[str]`：返回**缺失**的模型相对路径；0 字节算缺失；零网络零写入
- `ensure_models(cache_dir: Path = MODEL_DIR, *, download=None) -> tuple[Path, Path]`：只对 `models_present` 返回的缺失项调用 `download(url, dest)`；先下到 `.part` 再改名；返回（分割模型, 嵌入模型）路径
- `diarize(samples: np.ndarray, *, num_speakers: int | None = None, runner=None) -> list[Turn]`：`num_speakers=None` 时 `num_clusters=-1, threshold=0.5`；说话人 id 按**首次出现顺序**重编号为 0,1,2…；`runner` 注入替换 sherpa-onnx

- [ ] **Step 1: 写失败测试：** `test_models_present`（空目录 → 两项都缺；齐全 → `[]`；其中一个 0 字节 → 该项缺；调用前后目录内容不变）；`test_ensure_downloads_only_missing`（假 `download` 只被调用缺失项次数）；`test_ensure_atomic_part`（下载中途抛异常 → 目标文件不存在、无残留 `.part` 之外的半成品）；`test_diarize_renumbers_by_first_appearance`（假 runner 先返回 id 3 再 id 1 → 输出 0、1）；`test_diarize_num_speakers_passed`；`test_diarize_empty`。在 `tests/test_transcribe.py` 加 `test_main_uses_diarize`（注入 `diarize_fn` → 文字稿含 `说话人A:` 与 `- 说话人：` 行）。
- [ ] **Step 2: 运行确认失败；Step 3: 实现** `diarize.py` 并在 `transcribe_wav` 里默认接入（`--no-diarize` 或 `diarize=False` 时跳过；缺模型先 `ensure_models`，下载失败 → 警告并退回无标签）。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— `git add diarize.py tests/test_diarize.py transcribe.py tests/test_transcribe.py`；`feat: 说话人区分`

---

### Task 5: 声纹 voiceprint.py / enroll.py，并串联转写

**Files:** Create `voiceprint.py`、`enroll.py`、`tests/test_voiceprint.py`、`tests/test_enroll.py`；Modify `transcribe.py`

**Interfaces:**
- Consumes: Task 4 的 `diarize.models_present` / `EMBEDDING_FILE`；Task 2 的 `Turn`。
- Produces: `PROFILE_DIR = Path.home()/".config/meeting-recorder/voiceprints"`；`DEFAULT_THRESHOLD = 0.5  # 未校准`；`class VoiceprintError(ValueError)`
- `validate_name(name: str) -> str`（规则见 Global Constraints，不合法抛 `VoiceprintError` 带中文消息）
- `save_profile(name, vector: np.ndarray, *, seconds: float, chunks: int, model: str, replace: bool = False) -> Path`：JSON 键 `version=1, name, model, created, seconds, chunks, vector`（512 个 L2 归一化浮点），权限 600，原子写；同名且 `replace=False` 抛 `VoiceprintError`
- `load_profiles() -> dict[str, np.ndarray]`（坏文件跳过并警告）；`delete_profile(name) -> bool`；`list_profiles() -> list[dict]`（不含向量）
- `embed(samples: np.ndarray, *, extractor) -> np.ndarray`：L2 归一化的向量；`enroll_vector(samples, *, extractor, chunk_s: float = 3.0, min_seconds: float = 10.0) -> tuple[np.ndarray, int, float]`：去静音后切块取向量再平均并归一化，有效语音不足 `min_seconds` 抛 `VoiceprintError("有效语音太短，请再录一次")`
- `cosine(a, b) -> float`；`label_turns(turns: list[Turn], samples: np.ndarray, *, profiles: dict[str, np.ndarray], extractor, threshold: float = DEFAULT_THRESHOLD) -> list[Turn]`：按**说话人聚类**拼接该聚类音频（上限 60 秒）取向量，与每个声纹比余弦，≥ 阈值则整聚类改名；**同一个名字只给得分最高的聚类**；无声纹时原样返回
- `enroll.py` 命令行：`--name`、`--seconds N`（默认 30，现场录制）或 `--file PATH`、`--replace`；`record_from_mic(seconds, *, stream_factory, stop=None) -> np.ndarray`（`stop` 事件置位则提前结束并抛 `VoiceprintError("已取消")`）
- `transcribe.py`：新增 `--no-voiceprint`、`--voice-threshold X`；区分之后调用 `label_turns`，标签 str 直接作说话人名

- [ ] **Step 1: 写失败测试**（假 extractor：按输入返回预设向量；`mock.patch.object(voiceprint, "PROFILE_DIR", tmp)`）：`test_validate_name`（合法：`张三`、`a_b-1`；非法：空、21 字、`说话人A`、含空格、含 `/`）；`test_save_load_roundtrip_perm_600`；`test_save_conflict_without_replace`；`test_save_atomic`；`test_embed_unit_norm`；`test_enroll_too_short`；`test_label_turns_renames_matching_cluster`；`test_label_turns_same_name_only_best_cluster`；`test_label_turns_below_threshold_unchanged`；`test_label_turns_no_profiles_identity`（返回与输入逐项相等）；`test_transcript_unchanged_without_profiles`（在 `tests/test_transcribe.py`：无声纹时输出与 Task 4 黄金样本逐字相同且 stderr 为空）；`test_record_from_mic_cancel`。
- [ ] **Step 2: 运行确认失败；Step 3: 实现。**
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— `git add voiceprint.py enroll.py transcribe.py tests/test_voiceprint.py tests/test_enroll.py tests/test_transcribe.py`；`feat: 声纹登记与识别`

---

### Task 6: 实时转写 live.py 并接入 record.py

**Files:** Create `live.py`、`tests/test_live.py`；Modify `record.py`、`tests/test_record.py`

**Interfaces:**
- Consumes: Task 2 的 `transcribe_samples`；Task 3 的 `record`（`on_samples` 回调）。
- Produces: `VAD_FILE = "silero_vad.onnx"`；`models_present(cache_dir: Path) -> list[str]`；`ensure_vad_model(cache_dir=diarize.MODEL_DIR, *, download=None) -> Path`
- `class Segmenter(vad_factory)`：`feed(samples: np.ndarray) -> list[tuple[float, np.ndarray]]`（返回已说完的句子：起始秒, 音频）；`flush() -> list[...]`
- `class LiveTranscriber(transcribe_fn, sink, *, queue_max: int = 5)`：工作线程逐句转写；队列满时丢最旧并整场只警告一次；`submit(start_s, samples)` 非阻塞；`stop(timeout: float = 10.0)` 先冲出尾句再停
- `class LiveSink(live_path: Path, *, out=sys.stdout)`：输出 `[HH:MM:SS] 文字` 到终端和 `live.txt`；所有错误只警告
- `record.py`：新增 `--live`、`--live-model REPO`（默认 `transcribe.DEFAULT_MODEL`）；`on_samples` 里只调非阻塞的 `feed/submit`

- [ ] **Step 1: 写失败测试**（假 VAD、假转写函数）：`test_models_present`；`test_segmenter_emits_sentence_after_silence`；`test_transcriber_orders_output`；`test_queue_full_drops_oldest_warns_once`；`test_stop_flushes_tail`（断言尾句被转写）；`test_stop_timeout_returns`（转写函数阻塞 → `stop(0.2)` 在 1 秒内返回）；`test_sink_writes_file_and_terminal`；`test_live_error_does_not_stop_recording`（假转写抛异常 → `record` 仍写完 wav）。
- [ ] **Step 2: 运行确认失败；Step 3: 实现；Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— `git add live.py record.py tests/test_live.py tests/test_record.py`；`feat: 实时转写`

---

### Task 7: Web 后端：配置、会议存储、控制器、HTTP/SSE

**Files:** Create `web.py`、`web_config.py`、`meeting_store.py`、`meeting_controller.py`、`web_server.py`、`voiceprint_service.py`、`tests/test_web_config.py`、`tests/test_meeting_store.py`、`tests/test_meeting_controller.py`、`tests/test_web_server.py`、`tests/test_voiceprint_service.py`

**Interfaces:**
- Consumes: Task 3 的 `record`、Task 2/4/5 的 `transcribe_wav`、Task 5 的 voiceprint 公开函数。
- `web_config.load_port(path: Path | None) -> int`（默认 8765，非法值退回默认）；`load_llm(path: Path | None) -> dict | None`（缺任一必填项返回 None；`timeout_s=120`、`max_input_chars=60000` 默认）
- `meeting_store`：`list_meetings(root) -> list[dict]`（新到旧，每项 `id`、`title`、`has_transcript`）；`resolve(root, meeting_id) -> Path`（id 含 `/`、`..`、绝对路径、不存在 → `MeetingNotFound`，保证结果在 root 内）；`get_title/set_title`（`meta.json`，≤100 字无换行）；`add_annotation(dir, kind, t, text)` / `list_annotations(dir)`（`kind ∈ note|highlight|comment`，`t` 为秒，允许多位小数）；`delete_meeting(root, id)`；**`parse_header(text: str) -> dict`**（键 `"speakers": list[str]`，仅解析 `- 说话人：` 行，无该行为空列表；可信度优先计划会扩展 `voice_scores`）
- `MeetingController`：`__init__(root, *, record_fn, transcribe_fn, clock)`；`start(*, speakers: int | None = None, live: bool = False) -> str`（已在录音 → `MeetingBusy`；`speakers` 必须是 1–20 的严格整数，bool 不算）；`stop() -> None`；`snapshot() -> dict`（`recording|idle`、`final: idle|running|completed|failed`、当前会议 id、已录秒数）；事件队列带单调递增 `seq`，订阅者可从 `last_seq` 续传；会后转写在后台线程；转写异常 → `final=failed` 且不崩溃
- `web_server.create_app(controller, *, root, config_path, voiceprint_service) -> FastAPI`；路由：`GET /`、`/static/*`、`GET /api/state`、`GET /api/events`（SSE，先发快照再发增量，支持 `Last-Event-ID`）、`POST /api/recording/start|stop`、`GET /api/meetings`、`GET|PATCH|DELETE /api/meetings/{id}`、`GET|POST /api/meetings/{id}/annotations`、`GET /api/voiceprints`、`DELETE /api/voiceprints/{name}`、`POST /api/voiceprints/jobs`、`POST /api/voiceprints/jobs/{job_id}/cancel`、`GET /api/voiceprints/jobs/{job_id}`
- Origin 守卫：POST/PATCH/DELETE 必须带 `Origin` 且其主机是 `127.0.0.1` 或 `localhost` 并与服务端口一致，否则 403；JSON 体 `Content-Type` 不是 `application/json` → 415；删除/重命名正在录音或转写的会议 → 409
- `VoiceprintService`：现场录制/导入的后台任务，`start_record(name, seconds, replace)`、`start_import(name, path, replace)`、`cancel(job_id)`、`status(job_id)`；同一时间一个任务；任务期间禁止开始会议录音（409）
- `web.py`：读 `config.toml`（项目根目录，不存在也能启动），`uvicorn.run(app, host="127.0.0.1", port=..., workers=1, reload=False)`

- [ ] **Step 1: 写失败测试**（假 `record_fn`、假转写、FastAPI `TestClient` 且每个变更请求带 `Origin: http://127.0.0.1:8765`）：`tests/test_web_config.py`（缺文件、缺段、非法端口、llm 缺 key → None、默认值）；`tests/test_meeting_store.py`（排序；`resolve` 拒绝 `../x`、`/etc`、符号链接逃逸；标题 101 字、含换行被拒；标注 `t=1.2345` 保留；删除；`parse_header` 有/无说话人行）；`tests/test_meeting_controller.py`（重复 start → `MeetingBusy`；`speakers=True/0/21/"3"` 被拒、`speakers=3` 通过；转写异常 → `final=failed`；SSE 事件 `seq` 严格递增、从 `last_seq` 续传不重不漏）；`tests/test_web_server.py`（Origin 缺失/不符 → 403；text/plain → 415；`/` 与静态资源有 `no-cache`；录音中删除 → 409；绑定地址断言为 `127.0.0.1`）；`tests/test_voiceprint_service.py`（取消、单任务、任务期间禁止开始录音）。
- [ ] **Step 2: 运行确认失败；Step 3: 实现。** 先 `meeting_store` + `web_config`，再控制器，再路由。
- [ ] **Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— 明确列出以上源文件与测试文件；`feat: Web 控制台后端`

---

### Task 8: 大模型：发言归纳与面试帮助（后端）

**Files:** Create `llm_client.py`、`meeting_summary.py`、`interview_store.py`、`interview_questions.py`、对应 `tests/test_*.py`；Modify `web_server.py`

**Interfaces:**
- Consumes: Task 7 的 `web_config.load_llm`、`meeting_store.parse_header`。
- `llm_client`：`chat_summary(cfg: dict, system: str, user: str, *, opener=None) -> str`（`POST {base_url}/chat/completions`，Bearer，非流式）；`describe_images(cfg, images: list[str], *, opener=None) -> str`；**不跟随重定向**（自定义 `HTTPRedirectHandler` 抛错）；错误只映射为固定中文消息，不含服务器原文和 Key；异常类 `LLMError(status: int, message: str)`
- `meeting_summary`：`list_speakers(transcript_text: str) -> list[str]`（经 `parse_header`，无表头说话人行返回 `[]`）；`speech_of(transcript_text: str, speaker: str) -> str`
- `interview_store`：`JobStore(path: Path)`，职位 `type ∈ 前端|后端|客户端|测试`、标题 ≤100 字无换行、要求 ≤10000 字；`list/create/update/delete`，原子写；简历与生成结果**不落盘**
- `interview_questions.build_prompt(job: dict, resume_text: str) -> tuple[str, str]`；`SYSTEM_PROMPT` 按“专业能力约 20 分钟 / AI 应用约 12 分钟 / 开放性问题约 8 分钟”共约 40 分钟组织，每题在“预计用时”“考察点”后附粗体 `**答案要点：**` 加 2–4 条一句话无序列表（阶段 14 的最终形态，直接实现，不要走两步）
- 路由：`GET /api/meetings/{id}/speakers`；`POST /api/meetings/{id}/summary`（发言超过 `max_input_chars` → 413；未配置 llm → 503）；`GET|POST /api/jobs`、`PUT|DELETE /api/jobs/{job_id}`；`POST /api/jobs/{job_id}/questions`（简历文字 ≤20000 字；图片 ≤5 张，仅 `data:image/(png|jpeg|webp|gif);base64,`，单张解码后 ≤5 MB）

- [ ] **Step 1: 写失败测试**（假 opener，不联网）：`test_chat_ok`；`test_redirect_not_followed`（302 → `LLMError`，没有第二次请求）；`test_error_body_with_key_not_leaked`（500 响应体含 `sk-secret-123` → 消息与 `repr` 都不含）；`test_timeout_message`；`test_list_speakers_requires_header`；`test_speech_of_anonymous`；`test_job_validation`（类型、101 字标题、含换行、10001 字要求）；`test_job_store_atomic_and_reload`；`test_prompt_has_40min_and_answer_points`（断言含“专业能力”“AI 应用”“开放性问题”“答案要点”“40”）；路由测试：未配置 → 503、超长 → 413、图片 6 张/错误前缀/单张超 5 MB → 422、简历 20001 字 → 422、`Origin` 与 415 规则同 Task 7。
- [ ] **Step 2: 运行确认失败；Step 3: 实现；Step 4: 运行确认通过，再跑全量** —— Expected: `OK`。
- [ ] **Step 5: 提交** —— 明确列出源文件与测试文件；`feat: 发言归纳与面试问题`

---

### Task 9: 前端 web/static 与整体验收

**Files:** Create `web/static/index.html`、`style.css`、`app.js`、`interview.js`、`marked.min.js`、`purify.min.js`、`tests/test_web_ui.py`；Modify `README.md`

**Interfaces:**
- Consumes: Task 7、8 的全部路由。
- 页面：三个 Tab，顺序“当前会议 / 面试帮助 / 声纹登记”（`#tab-meeting`、`#tab-interview`、`#tab-voiceprint` 与 `#view-*`）；会议页三栏各自独立滚动（`@media(min-width:1041px)` 下 flex）：左=历史（标题按钮、编辑、删除，删除前 `confirm`）、中=计时与转写（`renderTranscriptLine(raw, onClick, anchor)` 两列 grid：时间 | 文本；标注在对应行打标记，点击定位）、右=标注（类型 `KINDS = {note:📝笔记, highlight:⭐重点, comment:💬注释}` 的单选，时间输入 `step="any"`）与发言归纳（`#summary-speaker`、`#summary-result`、`#summary-prompt`、`#summary-submit`、`#summary-advanced`）；`#final-status` 的 class 为 `status-pill final-<状态>`；“当前会议人数”输入（1–20 整数）；开始新录音时清空上一场选中行与转写残留
- 声纹页：登记表单只有姓名、“明确替换同名声纹”勾选（**不要**“已取得同意”勾选）；现场录制成功后弹出 `#voiceprint-dialog` 朗读文案，登记结束自动关闭，关闭/Esc 取消本次录制（调用 cancel 路由）
- 面试页：职位列表与编辑、简历文字 + 粘贴截图；结果经 `marked.parse` 后 `DOMPurify.sanitize`（白名单）再渲染——**这是唯一允许 `innerHTML` 的位置**
- `marked.min.js` / `purify.min.js` 为随仓库提交的固定版本压缩文件：用 `npm pack marked@18.1.2 dompurify@3.4.17` 取 dist 文件；若这两个版本在 registry 不存在，取各自最新稳定版并在 `README.md` 记录版本号（Ruling）。无 CDN、无构建

- [ ] **Step 1: 写失败测试** `tests/test_web_ui.py`（读静态文件做字符串/正则断言，不起浏览器）：`test_no_cdn`（html/js 不含 `http://` 或 `https://` 外链，仅允许注释外的 `127.0.0.1`）；`test_no_innerhtml_except_interview`（`app.js` 不含 `innerHTML` 与 `insertAdjacentHTML`；`interview.js` 中的 `innerHTML` 紧邻 `DOMPurify.sanitize`）；`test_tab_order`；`test_no_consent_checkbox`（不含“已取得被登记者同意”）；`test_step_any`；`test_kinds`；`test_render_line_uses_textContent`。
- [ ] **Step 2: 运行确认失败；Step 3: 实现前端。**
- [ ] **Step 4: 全量测试** —— Expected: `OK`。
- [ ] **Step 5: 手动验收（真实麦克风与模型，仅一次）：** `.venv/bin/python web.py`，浏览器开 `http://127.0.0.1:8765`：开始录音→说话→停止→转写完成→历史出现；加一条标注并点击定位；转写文本里含 `<script>alert(1)</script>` 的会议不弹窗。把每一项结果（通过/失败/未测）写进 `README.md` 的“验收记录”，**未测不写通过**。
- [ ] **Step 6: 更新 `README.md`**（安装、CLI、Web、`config.toml` 示例含 `[web]` 与 `[llm]`、验收记录），然后提交 —— 明确列出以上文件；`feat: Web 前端与验收`

---

## Self-Review 记录

- **Spec 覆盖：** 总计划阶段 1（Task 1–3）、2（Task 4）、3（Task 6）、4（Task 5；Task 8 阈值校准不在本计划，归可信度优先 Task 11）、5–9（Task 7、9）、10（Task 8）、11（Task 9 直接取终态：无同意勾选、无 `initial_prompt`、开始录音重置界面）、12（Task 8、9）、13（Task 3 的 `open_stream` 超时）、14（Task 8 的 `SYSTEM_PROMPT` 直接取终态）。
- **与可信度优先计划的接口：** `common.atomic_write_text`、`common.read_wav_tolerant`、两个 `models_present`、`parse_header` 基础版、`mr-stream-` 线程名、`MeetingController` 的转写线程均已在本计划建好；可信度优先 Task 1 作废，Task 4/5/7/8 对应步骤“已存在则跳过”。
- **已知风险（未在本机验证）：** `mlx_whisper` 首次下载模型体积大；嵌入模型输出维度是否为 512（Task 1 门槛实测）；`sounddevice` 需要麦克风权限；Task 3/9 的手动步骤需要用户在场。