# Cloud Meeting Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 HTTPS 登录、本人私有记录、浏览器麦克风与电脑播放声音采集的云端会议记录网站，保留转写、标注、声纹、归纳和面试帮助。

**Architecture:** 保留本地 CLI/Web 的运行边界，新增独立 FastAPI 云端入口；浏览器分轨采集与 IndexedDB 暂存，PostgreSQL 保存归属、清单、版本、任务与事件，S3 兼容私有存储保存音频。采用 PostgreSQL 持久任务表与事务领取，模型 worker 不进入 API 录音关键路径；原生 JS 以云端独立模块复用交互心智，不复用本地全局控制器。

**Tech Stack:** FastAPI、PostgreSQL、SQLAlchemy/Alembic、psycopg、S3 SDK、FFmpeg、原生 ES modules、IndexedDB、Playwright；Linux ASR/ONNX 引擎由 P0 实测锁定。Python 3.12、Node 22 为计划建议的验证基线，不是设计已批准版本要求；新增依赖在执行时生成固定版本锁文件。

**Spec:** [云端产品设计](<../../product/cloud-meeting-recorder-design.md>)，视觉参考 [设计系统草案](<../../../DESIGN.md>)。两份文档与本计划一起交接。

## Global Constraints

- 价值顺序：不丢录音 > 不串用户 > 结果可核对 > AI 增强 > 视觉表现。
- 网站访问、账号登录、每人只能看到自己的记录；采集用户当前电脑的麦克风与电脑播放声音。电脑声音指数字播放流，不是服务器设备或麦克风二次拾音。
- 共享视频帧不得编码、上传、入存储或进入日志；getDisplayMedia 不使用 video:false 假设音频专用授权。
- 人数提示1–20，未填则自动估计；模型工作音频为16kHz单声道，不假定浏览器源采样率16kHz。
- 片段字段：upload_session_id、capture_epoch_id、track_id、sequence、start_ms、end_ms、checksum、codec；持久对象与数据库记录完成后才ACK。
- 所有私人资源、事件、缓存、任务和临时输入绑定 owner_user_id；身份仅来自服务端认证上下文，客户端不得指定归属。
- 401未认证；404资源不可访问；409状态/版本冲突；413大小超限；429限流；安全的5xx服务失败。
- 简历文本≤20000字；图片最多5张、单张≤5MB，png/jpeg/webp/gif；约40分钟：专业20、AI应用12、开放8；答案要点2–4条。
- 首版简历与面试结果不持久保存、不入备份、不记录全文日志、不写持久业务队列；总结未确认前只临时返回。
- 声纹仅匹配本人库；不得照搬当前0.5/0.35宣称已校准，不得仅凭512维相同跨模型匹配。
- 删除立即拒绝访问；旧任务不得复活资源；恢复备份必须重放删除墓碑。
- 单麦是中间里程碑，不替代双音源交付；未实测兼容组合不能写为支持。不得加入共享、收费、辅助客户端、候选人库或旧数据自动迁移。
- 本轮仅写计划，不改产品代码、安装依赖、部署或提交。计划任务的命令是后续执行说明，不是已运行证据。

## Review Focus

以下五类隐含风险在原设计验收中未细化为具体用例；本计划补入对应任务，不以设计沉默视为可忽略。

1. ACK响应丢失、对象已写而数据库事务失败：重传只能得到一个有效片段，不能重复收费/识别或误删已ACK对象（Task 5、7）。
2. 两路不等长、48kHz源和刷新后的时钟回退：连续保存取必需轨覆盖交集，真实缺口不被压缩，标注时间仍一致（Task 6、7、9）。
3. worker崩溃后旧进程晚发布、新重转写先完成：仅有效租约与generation可发布，不覆盖新版本（Task 8、10）。
4. 同名/Unicode等价声纹、同维异模型、删除与提取并发：本人姓名唯一，旧模板原子保留，撤销版本不能继续出实名（Task 13）。
5. 图片解码尺寸炸弹、图片OCR后文本膨胀、长结果和取消请求：全链路限额与清理，不静默截断，不把隐私内容留在日志/队列（Task 14、15）。

---

## 1. 计划状态与执行门槛

日期：2026-11-14。状态：**供审阅的分阶段总计划；P0尚未执行，P1–P4为条件性实施方案。** 设计第18节要求先有探针证据再形成已锁定方案；用户本轮要求先输出plan，因此本文件把证据门槛写成首批任务，不声称技术选型已经验证。

该设计跨多个子系统，建议以后按阶段拆为独立执行包：P0探针、P1私人单麦录音、P2双轨最终稿、P3业务增强、P4上线验收。此处保留一个总文档保证接口一致；每阶段都产生可测试的软件/证据，不把数据库搭架子独立当交付。

| 门槛 | 需用户确认或探针提供 | 未满足时行为 |
|---|---|---|
| G0 范围 | 必须支持的OS/浏览器/会议软件、是否必须macOS桌面音频 | Task 2可做探针；不能承诺兼容或推进正式双轨实现 |
| G1 隐私/身份 | 部署地区、允许的第三方AI数据类别、邀请/开放及登录机制 | 可做本地假认证测试；禁止公网部署/真实资料外发 |
| G2 采集/模型 | 三探针通过；编码、解码、对齐/去重和模型元数据锁定 | Task 4以后的实现须用锁定契约，不猜音轨独立可解码 |
| G3 生命周期 | 保留/备份/注销；同账号1场、2小时、缓存5分钟/100MB、恢复24小时是否采纳 | 配置作候选测试值，不写成正式政策；上线前必须明确 |
| G4 业务/视觉 | 总结持久化、历史实名撤回、设计系统是否采纳 | 默认临时总结、声纹删除只影响新匹配；草案视觉不得覆盖本地UI |

候选配置集中在 `cloud/settings.py`，而不是散落魔数：chunk_target_ms=3000（设计目标2–5秒内）、saved_lag_target_ms=10000、live_latency_target_ms=[5000,15000]、buffer_max_ms=300000、buffer_max_bytes=100*1024*1024、meeting_max_ms=7200000、active_capture_per_user=1、recovery_window_s=86400、cleanup_target_s=86400、ephemeral_cleanup_s=3600、backup_retention_days=30、log_retention_days=30。其中5MB按5*1024*1024字节作为待审接口解释；新增图片max_pixels=20000000、总请求max_bytes=30*1024*1024、AI输出max_chars=40000、请求timeout_s=120、worker租约60秒/心跳20秒是**计划建议安全预算**，必须在Task 1批准记录中单列，不冒充设计原值。缺乏质量/容量数据，不给准确率、最终耗时、在线用户数承诺。

## 2. 基线与文件结构

核对到： [本地入口](<../../../web.py#L24-L48>) 构建全局 MeetingController 并仅监听回环；[本地路由](<../../../web_server.py#L475-L507>) 的录音接口打开服务器设备，不能公网复用；[ASR入口](<../../../transcribe.py#L57-L65>) 延迟导入MLX；[前端](<../../../web/static/app.js#L1-L4>) 为两个经典脚本共用全局作用域；[现有测试](<../../../tests/test_web_server.py#L1-L21>) 使用 unittest、TestClient、假依赖。

**隔离迁移决策（待审建议）：** 新增云端入口而非重写本地入口。本地录音、文件存储、全局声纹路径、doctor和全局线程锁不接云端请求。只复用纯文本/纯样本算法；不通过按请求改全局路径实现租户隔离。

| 文件/目录（拟新增） | 职责 | 归属任务 |
|---|---|---|
| `spike/cloud_capture/`、`spike/cloud_models.py`、`docs/superpowers/evidence/cloud/` | 三探针与带版本的选择/实测记录 | 1–3 |
| `cloud/contracts.py`、`cloud/settings.py` | DTO、状态枚举、配置验证 | 4 |
| `cloud/auth.py`、`cloud/app.py`、`cloud/main.py`、`cloud/db.py`、`cloud/schema.py`、`cloud/meetings.py` | 认证、私有API装配、数据库/会议元信息 | 4 |
| `cloud/migrations/`、`alembic.ini`、`requirements-cloud.txt`、`requirements-cloud.lock` | 云端独立依赖和数据库迁移 | 4及各实体任务 |
| `cloud/objects.py`、`cloud/uploads.py`、`cloud/leases.py`、`cloud/sealing.py` | 私有对象、可靠ACK、录音租约、完整/部分封口 | 5、7 |
| `web/cloud/session.mjs`、`capture.mjs`、`buffer.mjs`、`upload.mjs`、`recording.mjs` | 账号生命周期、浏览器采集、暂存、补传 | 4、6、7 |
| `cloud/tasks.py`、`cloud/worker.py`、`cloud/events.py` | 持久任务、公平领取/重试、持久事件恢复 | 8、11 |
| `cloud/audio.py`、`cloud/playback.py`、`cloud/models.py`、`cloud/transcripts.py`、`cloud/live.py` | 授权音频构建、Range、模型适配、版本稿、实时稿 | 9–11 |
| `cloud/annotations.py`、`web/cloud/workbench.mjs`、`index.html`、`style.css` | 时间标注、私有工作台 | 12 |
| `cloud/voiceprints.py`、`web/cloud/voiceprints.mjs` | 私有声纹与浏览器登记 | 13 |
| `cloud/positions.py`、`cloud/ai.py`、`cloud/ephemeral.py`、`web/cloud/interview.mjs`、`render.mjs` | 职位、临时AI、净化渲染 | 14 |
| `cloud/privacy.py`、`cloud/limits.py`、`cloud/health.py`、`cloud/observability.py` | 墓碑/清理、限额、非敏感自检与指标 | 15–17 |
| `tests/cloud/`、`tests/browser/cloud/`、`tests/e2e/cloud/`、`package.json`、`package-lock.json` | unittest集成、Node单测、真实浏览器验收 | 各任务 |
| `deploy/cloud/`、`docs/cloud-operations.md` | Linux构建、TLS代理、备份/恢复与运行说明 | 17 |

现有业务文件默认不改。允许在后续任务引用 [繁简转换](<../../../transcribe.py#L71-L88>)、[声纹样本算法](<../../../voiceprint.py#L303-L339>)、[归纳提示](<../../../meeting_summary.py#L108-L114>)、[面试提示](<../../../interview_questions.py#L35-L42>)，通过显式参数注入；不调用它们的全局磁盘存储或默认下载器。云端镜像若无法安全import含本地依赖的模块，只把所需纯函数抽到 `cloud/prompts.py` / `cloud/text.py` 并为原提示输出加快照回归，不为复用而安装本地设备依赖。静态净化库可使用 [现有 marked](<../../../web/static/marked.min.js>) 与 [DOMPurify](<../../../web/static/purify.min.js>)，上线前固定版本/校验和并审计。

### 2.1 共享契约（Task 4定义，后续不得自行改名）

ID均为UUID字符串；UTC绝对时间为ISO8601；会议定位毫秒为非负整数。Python DTO采用Pydantic，内部数组为list，JS以同名JSON字段使用。所有私有请求先得到 `AuthContext(user_id: str, session_id: str)`；DTO禁止extra字段。

- `MeetingView(id: str, title: str, mode: Literal["mic","dual"], generation: int, current_transcript_id: str | None, deleted_at: str | None)`。
- `ChunkMeta(upload_session_id: str, capture_epoch_id: str, track_id: str, sequence: int, start_ms: int, end_ms: int, checksum: str, codec: str)`；checksum为SHA-256 hex，时间为会议相对时间，sequence在(epoch,track)内从0递增。
- `CaptureEpoch(id: str, offset_ms: int, uncertainty_ms: int, started_at: str)`；`TrackSpec(id: str, source: Literal["mic","system"], codec: str, sample_rate: int)`。offset是新epoch的会议偏移，不是performance.now原值。
- `Gap(track_id: str, start_ms: int, end_ms: int, reason: str)`；`AckView(accepted: bool, duplicate: bool, contiguous_ms: int, missing: list[Gap])`。contiguous_ms指必需活动轨共同连续覆盖位置。
- `TrackManifest(capture_epoch_id: str, track_id: str, last_sequence: int, end_ms: int)`；`SealRequest(tracks: list[TrackManifest], accept_partial: bool)`；`SealView(input_version: int, status: Literal["complete","partial"], gaps: list[Gap])`。零片段轨last_sequence=-1，只能部分封口；epoch/轨道拓扑必须服务端已登记，不能删掉system轨伪装完整。
- `TaskView(id: str, meeting_id: str | None, owner_user_id: str, input_version: int, generation: int, status: str)`；owner仅worker内部/本人的响应，业务输入不接受owner。`ClaimedTask(task: TaskView, lease_token: str, lease_deadline: str)`为worker内部返回；声纹任务meeting_id为空并关联本人采样/模板，final/live必须关联会议。
- `Segment(id: str, start_ms: int, end_ms: int, text: str, speaker_id: str | None)`；`Speaker(id: str, label: str, suspect: bool)`；`TranscriptView(id: str, meeting_id: str, segments: list[Segment], speakers: list[Speaker], warnings: list[str], empty: bool)`。speaker_id在会议级稳定，不用音源或姓名当ID。
- `AnnotationView(id: str, meeting_id: str, offset_ms: int, kind: Literal["note","highlight","comment"], text: str)`；`EventView(cursor: int, meeting_id: str, generation: int, type: str, payload: dict)`。
- `ModelMetadata(model_id: str, model_version: str, weights_sha256: str, dimension: int, sample_rate: int, preprocess_version: str, normalization_version: str, calibration_version: str)`；`Embedding(vector: list[float], metadata: ModelMetadata)`；`VoiceprintView(id: str, name: str, version: int, metadata: ModelMetadata, revoked: bool)`。向量不返前端。
- `PositionView(id: str, type: Literal["前端","后端","客户端","测试"], title: str, requirements: str)`；`AIResult(markdown: str, transcript_version_id: str | None)`；`InterviewInput(resume_text: str, images: list[bytes])`只在请求内存中存在。

### 2.2 测试与提交约定

每个代码任务完整走 RED→GREEN→提交。Python沿用 unittest；示例方法置于对应 `unittest.TestCase` 中，使用self.assertEqual等断言。Task 4创建 `tests/cloud/support.py: CloudCase`：setUp建立独立真实PostgreSQL schema，A/B已认证TestClient `self.a/self.b`、匿名client `self.anon`、可控制时钟 `self.clock`；提供 `create_meeting(client, mode="mic") -> str`、`restart_app() -> None`、`query_count(table: str) -> int`。服务依赖通过app工厂注入，FakeObjectStore/FakeModels/FakeLLM在各自任务添加到support；假依赖只测故障逻辑，不替代真实S3/音频/浏览器。

每任务Step 1写测试时一并建立其测试基础设施/依赖和fixture（先不写被测行为），Step 2环境自检通过后才判RED。Task 2创建 `tests/cloud/__init__.py`，Task 4沿用而非重复覆盖；`ProbeResult(dualReady: boolean, encodedSources: string[], codec: string)`在探针模块定义。Node单测用 `node --test`，ESM无需前端框架；Task 2在 `tests/browser/cloud/support.mjs` 定义 FakeMediaDevices、FakeRecorder、FakeClock，Task 6添加浏览器IndexedDB测试。E2E使用独立Playwright浏览器context验证A/B，不使用真实个人会议资料。测试环境仅本地Compose的PostgreSQL/S3模拟与本地假身份；真实模型/实机另记录证据。

下列TestCase类名、测试名及断言是必须实现的合同；辅助fixture由拥有任务定义，不是目前已有API。RED允许首次ImportError（模块未建），后续必须看到断言因预期行为不满足而失败，不能把数据库没启动或依赖缺失当RED。每任务提交前复查 `git diff --check`，只stage该任务文件，不运行git add .。文档目录被 [忽略规则](<../../../.gitignore#L10>) 忽略；未来若要把批准证据随代码提交，必须显式 `git add -f` 指定文件，不取消整条docs忽略规则。本轮不提交。

### 2.3 关键测试代码基准

以下为Task 4和6的RED测试最小写法；各任务其余测试名及精确断言见Step 1。测试辅助函数由2.2指定的fixture提供。

```python
class AuthCase(CloudCase):
    def test_own_meetings_only(self):
        mid = self.create_meeting(self.a)
        self.assertEqual(self.a.get(f"/api/cloud/meetings/{mid}").status_code, 200)
        self.assertEqual(self.b.get(f"/api/cloud/meetings/{mid}").status_code, 404)
        self.assertEqual(self.anon.get(f"/api/cloud/meetings/{mid}").status_code, 401)
        page = self.b.get("/api/cloud/meetings").json()
        self.assertNotIn(mid, [item["id"] for item in page["items"]])
```

```javascript
// recording.test.mjs：fixture返回当前任务实际构造的buffer/pump/meta。
import assert from "node:assert/strict";
import test from "node:test";
import { recordingFixture } from "./support.mjs";
test("buffers_before_send_and_clears_only_after_ack", async () => {
  const { buffer, pump, meta, transport } = await recordingFixture();
  await buffer.put("A", meta, new Uint8Array([1, 2, 3]));
  transport.rejectNext();
  await assert.rejects(() => pump.flush("A", meta.upload_session_id));
  assert.equal((await buffer.pending("A", meta.upload_session_id)).length, 1);
  transport.acceptNext();
  await pump.flush("A", meta.upload_session_id);
  assert.equal((await buffer.pending("A", meta.upload_session_id)).length, 0);
});
```

## 3. 分阶段任务

### Task 1（P0）：范围、隐私与配置决策记录

**Files:** Create `docs/superpowers/evidence/cloud/decisions.md`、`docs/superpowers/evidence/cloud/acceptance.csv`。
**Interfaces:** Consumes设计第17节；Produces G0–G4逐项的approved/rejected/pending状态、批准人/日期，以及每条AC的证据位置。

- [ ] **Step 1:** 向用户确认G0/G1及候选安全预算，记录真实目标组合；未知写pending，不代用户回答。
- [ ] **Step 2:** 建20行AC01–AC20验收表，列阶段、自动测试、实机步骤、结果、版本、证据；初始全部not_run。
- [ ] **Step 3:** 核对地区、身份、第三方输入、生命周期和视觉选择均有状态；G0/G1未批准则探针仅用合成数据、本地身份。
- [ ] **Step 4:** 用户审阅；只在后续实际批准后提交证据：`git add -f docs/superpowers/evidence/cloud/decisions.md docs/superpowers/evidence/cloud/acceptance.csv && git commit -m "docs: record cloud migration decision gates"`。本任务为决策证据，不虚构TDD软件周期。

### Task 2（P0）：双音源与可靠上传三分之二探针

**Files:** Create `spike/cloud_capture/index.html`、`spike/cloud_capture/capture.mjs`、`spike/cloud_capture/probe_server.py`、`tests/browser/cloud/support.mjs`、`tests/browser/cloud/capture-probe.test.mjs`、`tests/cloud/test_probe_upload.py`、`docs/superpowers/evidence/cloud/capture.md`、`docs/superpowers/evidence/cloud/upload.md`、`docs/superpowers/evidence/cloud/capture-contract.json`。
**Interfaces:** Produces捕获契约JSON：selected_codec、chunk_target_ms、continuity_mode（header+ordered_chunks或pcm）、video_track_lifecycle、epoch_anchor_rule及实测支持组合；不是生产接口。

- [ ] **Step 1:** 写 `rejects_missing_system_audio`、`never_serializes_video`：假共享返回只有video，assert dualReady=false、上传编码器输入tracks全为audio；写 `ProbeUploadCase.test_reordered_stream_reconstructs`：3片段按2/0/1输入，assert恢复音频时长与原流一致，单blob不能解码时明确标dependent。
- [ ] **Step 2:** Run `node --test tests/browser/cloud/capture-probe.test.mjs && python -m unittest tests.cloud.test_probe_upload -v`；Expected初次缺探针函数失败，随后行为断言失败。
- [ ] **Step 3:** 实现 `probeCapture(mode, mediaDevices) -> Promise<ProbeResult>`、`reconstruct_probe(chunks: list[bytes], codec: str) -> bytes`；每次用户点击共享，只把audio交编码器。测试MediaRecorder格式、头片依赖、连续解码；不能达实时目标则同场比较AudioWorklet PCM，不预定最终方案。
- [ ] **Step 4:** 重跑自动测试PASS；在G0所有目标组合分别外放/耳机读两段不同句子，持续录音及2小时候选长测，断网30秒、刷新、共享停止；记OS/浏览器/会议软件版本、轨存在/内容/漂移/解码延迟。必需组合失败先回用户，不推进全功能。
- [ ] **Step 5:** 固定捕获契约与原始非敏感证据，`git add spike/cloud_capture tests/browser/cloud tests/cloud/test_probe_upload.py && git add -f docs/superpowers/evidence/cloud/capture.md docs/superpowers/evidence/cloud/upload.md docs/superpowers/evidence/cloud/capture-contract.json && git commit -m "test: prove browser audio and chunk continuity"`。

### Task 3（P0）：Linux模型与跨轨身份探针

**Files:** Create `spike/cloud_models.py`、`tests/cloud/test_model_probe.py`、`docs/superpowers/evidence/cloud/models.md`、`docs/superpowers/evidence/cloud/model-contract.json`。
**Interfaces:** ProducesASR/diarization/embedding引擎、精确包版本/权重摘要/许可、calibration文件，以及 `recognition_strategy`（mixed或separate_merge）、去重与会议级speaker合并规则。Task 10只实现最终选中的策略。

- [ ] **Step 1:** 写 `ModelProbeCase.test_contract_rejects_unmeasured_strategy`：无证据不能输出approved；`test_cross_track_identity_fixture`：同人跨轨期望1个身份、重复拾音期望1句、两位远端期望2个身份；`test_same_dimension_not_compatible`：同维不同权重摘要assert兼容=false。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_model_probe -v`；Expected策略验证未实现FAIL。
- [ ] **Step 3:** 实现 `evaluate_models(dataset: Path, output_dir: Path) -> dict`、`validate_model_contract(contract: dict) -> None`；比较混合统一识别与分轨后合并，记录具体去重时间窗/相似度和聚类匹配参数及取值理由。若两者不能达到用户批准的语料门槛，不锁定方案。
- [ ] **Step 4:** 在目标Linux机器跑真实语料：中文/中英/静音/重叠/跨轨同人/多人远端；报告字错率、说话人错误、误识/漏识、延迟/内存/容量，比较本地基线而非要求逐字相同。断网冷启动必须工作，记录发行版/架构/Python/CUDA/驱动/FFmpeg/VAD/ONNX/缓存。
- [ ] **Step 5:** 单测PASS后提交探针与批准契约；不提交真实授权音频：`git add spike/cloud_models.py tests/cloud/test_model_probe.py && git add -f docs/superpowers/evidence/cloud/models.md docs/superpowers/evidence/cloud/model-contract.json && git commit -m "test: qualify Linux models and speaker merge strategy"`。

### Task 4（P1）：可登录的私人会议最小API与真实数据库隔离

**Files:** Create `cloud/__init__.py`、`cloud/contracts.py`、`cloud/settings.py`、`cloud/db.py`、`cloud/schema.py`、`cloud/auth.py`、`cloud/meetings.py`、`cloud/app.py`、`cloud/main.py`、`cloud/migrations/`、`alembic.ini`、`requirements-cloud.txt`、`requirements-cloud.lock`、`tests/cloud/__init__.py`、`tests/cloud/support.py`、`tests/cloud/test_auth.py`、`web/cloud/session.mjs`、`deploy/cloud/compose.test.yml`。
**Interfaces:** `create_app(settings: Settings, dependencies: dict | None = None) -> FastAPI`；`require_user(request: Request) -> AuthContext`；`create_meeting(ctx: AuthContext, title: str, mode: str, speakers: int | None) -> MeetingView`；`get_meeting(ctx: AuthContext, meeting_id: str) -> MeetingView`；`list_meetings(ctx: AuthContext, cursor: str | None, limit: int) -> MeetingPage`；`rename_meeting(ctx: AuthContext, meeting_id: str, title: str) -> MeetingView`；`MeetingPage(items: list[MeetingView], next_cursor: str | None)`定义于contracts。

- [ ] **Step 1:** 写 `AuthCase.test_own_meetings_only`：A创建mid，assert A GET=200、B GET=404、anon GET=401、B列表不含mid；`test_owner_injection_rejected`：POST包含owner_user_id=B，assert422；`test_forged_session_and_csrf_rejected`：伪cookie=401，缺CSRF变更=403，资源ID猜测不泄露。
- [ ] **Step 2:** Run `docker compose -f deploy/cloud/compose.test.yml up -d --wait && python -m unittest tests.cloud.test_auth -v`；环境正确时鉴权/隔离断言FAIL。Compose包含PostgreSQL与私有S3测试服务，无公网业务数据。
- [ ] **Step 3:** 定义2.1全部DTO和候选配置校验；首迁移只创建User/Session/Meeting及复合(owner,id)键。按G1选中的成熟认证建立服务端会话与验证callback状态/过期/重放；生产不得启用fake auth。Cookie HttpOnly/Secure/SameSite=Lax，同源CSRF；登录错误不透露账号存在。所有下述API统一以 `/api/cloud` 为前缀；session、GET/POST meetings、GET/PATCH meetings/{id} 与本地API隔离，分页limit范围1–100/默认20，按(created_at,id)稳定倒序且cursor只作用于本人查询；cloud app不导入录音控制器。
- [ ] **Step 4:** 重跑PASS并加入 `test_login_callback_replay_rejected`、`test_invalid_speakers`（0/21/bool拒绝，None/1/20接受）、`test_private_pagination_and_rename`（A分页每条只属A、同时间按id稳定，B改名404、limit=0/101拒绝）；生产HTTPS身份沙盒实测登录/失败/退出。无认证不请求麦克风。
- [ ] **Step 5:** `git add cloud requirements-cloud.txt requirements-cloud.lock alembic.ini tests/cloud deploy/cloud/compose.test.yml web/cloud/session.mjs && git commit -m "feat: add private authenticated cloud meetings"`。

### Task 5（P1）：私有对象与持久片段ACK

**Files:** Create `cloud/objects.py`、`cloud/uploads.py`、`cloud/leases.py`、`tests/cloud/test_uploads.py`；Modify `cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`tests/cloud/support.py`。
**Interfaces:** `put_chunk(ctx: AuthContext, meta: ChunkMeta, body: bytes, capture_lease_token: str | None) -> AckView`；`get_ack(ctx: AuthContext, upload_session_id: str) -> AckView`；`acquire_capture(ctx: AuthContext, meeting_id: str) -> str`（lease_token）；`heartbeat_capture(ctx: AuthContext, token: str) -> None`；`ObjectStore.put_verified(key: str, body: bytes, sha256: str) -> None`。

- [ ] **Step 1:** 写 `UploadsCase.test_ack_loss_retry_is_idempotent`：首上传成功但客户端丢响应，重传assert duplicate=true、Chunk行数=1；`test_hash_conflict`：同幂等键异hash=409；`test_object_failure_never_acks`：对象失败assert5xx、Chunk行数=0；`test_owner_and_one_capture`：B上传A=404、A第二租约=409、B独立租约成功。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_uploads -v`；ExpectedACK/租约约束FAIL。
- [ ] **Step 3:** 创建UploadSession/Epoch/Track/Chunk/CaptureLease，幂等唯一(owner,upload,epoch,track,sequence)，复合FK防跨owner关联；对象键仅服务端生成。先限制读取字节/验证真实hash及允许codec，再持久对象，后提交数据库，再ACK。数据库失败后的对象可重试关联，孤儿扫描须查引用；新采集片必须携带当前有效录音租约token，旧epoch已登记片段仅在同账号恢复窗口内允许补传，不能凭恢复资格创建新epoch。录音租约到期不删除已ACK片段。POST `/uploads`、`/uploads/{id}/epochs`、PUT `/uploads/{id}/chunks/{epoch}/{track}/{seq}`、GET `/uploads/{id}/ack`、POST `/capture-lease` 与心跳/释放全部鉴权。
- [ ] **Step 4:** 重跑PASS；补 `test_db_commit_failure_then_retry`、`test_unregistered_track_rejected`、`test_negative_or_overlapping_times_rejected`；真实S3校验私有ACL和写失败回滚，重启API后ACK仍在。
- [ ] **Step 5:** `git add cloud tests/cloud && git commit -m "feat: persist private audio chunks before acknowledgment"`。

### Task 6（P1）：浏览器单麦采集、暂存、账号生命周期

**Files:** Create `web/cloud/capture.mjs`、`web/cloud/buffer.mjs`、`web/cloud/upload.mjs`、`web/cloud/recording.mjs`、`tests/browser/cloud/recording.test.mjs`、`tests/browser/cloud/buffer.spec.mjs`、`package.json`、`package-lock.json`、`playwright.config.mjs`；Modify `web/cloud/session.mjs`、`tests/browser/cloud/support.mjs`。
**Interfaces:** `startCapture({mode, epoch, tracks, mediaDevices, onChunk, onState}) -> Promise<CaptureHandle>`，handle有 `stop() -> Promise<void>`；`ChunkBuffer.put(userId, meta, bytes) -> Promise<void>`、`pending(userId, uploadId) -> Promise<Array<{meta,bytes}>>`、`ack(userId, meta) -> Promise<void>`；`UploadPump.flush(userId, uploadId) -> Promise<void>`；`RecordingSession.stop() -> Promise<void>`。

- [ ] **Step 1:** 写 `buffers_before_send_and_clears_only_after_ack`：上传失败后pending=1，ACK后pending=0；`never_restores_into_other_user`：B读A暂存=[]；`quota_stops_without_silent_drop`：达到300000ms或100*1024*1024先到者产生persistent warning和stop；`epoch_clock_reset_keeps_gap`：旧epoch结束30000、新offset60000，assert新片start>=60000，不拼接成30000。
- [ ] **Step 2:** Run `node --test tests/browser/cloud/recording.test.mjs && npx playwright test tests/browser/cloud/buffer.spec.mjs`；Expected状态/暂存FAIL。首次增加Playwright依赖锁、`playwright.config.mjs`（testMatch=**/*.spec.mjs、baseURL本地测试app）和测试命令作为本任务一部分；临时fixture页在Task12工作台之前即可测试IndexedDB，不依赖尚未创建的正式UI。
- [ ] **Step 3:** 按Task 2契约实现mic模式，输入单调时钟共享到片段回调；IndexedDB键含owner/upload/epoch/track/seq，先持久暂存再发送；ACK后删除，重试退避到上限不无限吃内存。账号过期仅同账号重登补传；显式退出先停止尾片/确认放弃，关闭媒体、SSE、内存/页面缓存；不安装公共Service Worker缓存。状态分别维护capture/upload/processing。
- [ ] **Step 4:** 重跑PASS；实浏览器Test `permission_denied_does_not_start`、`logout_releases_every_track`、`natural_expiry_preserves_same_owner_buffer`；48kHz源不误当16kHz，储存异常不能当ACK成功，刷新不宣称仍在采集。
- [ ] **Step 5:** `git add web/cloud tests/browser/cloud package.json package-lock.json && git commit -m "feat: record and recover private browser microphone chunks"`。

### Task 7（P1→P2）：完整清单、缺口、自动部分封口与双轨

**Files:** Create `cloud/sealing.py`、`tests/cloud/test_sealing.py`、`tests/browser/cloud/dual.test.mjs`；Modify `cloud/uploads.py`、`cloud/leases.py`、`cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`web/cloud/capture.mjs`、`web/cloud/recording.mjs`、`web/cloud/upload.mjs`。
**Interfaces:** `seal_upload(ctx: AuthContext, upload_session_id: str, manifest: SealRequest) -> SealView`；`seal_expired(now: datetime) -> int`；`register_epoch(ctx: AuthContext, upload_session_id: str, anchor: CaptureEpoch, tracks: list[TrackSpec]) -> CaptureEpoch`。

- [ ] **Step 1:** 写 `SealingCase.test_missing_tail_never_complete`：清单0..2仅有0/1，accept_partial=false=409、true=partial且gap可见；`test_dual_ack_is_intersection`：mic至10000/system至6000，assert contiguous_ms=6000；`test_expiry_keeps_acked_objects`：过86400秒扫描，assert partial、ACK对象仍存在、后续追加409；Node `missing_system_requires_explicit_downgrade` 与 `video_never_enters_encoder`。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_sealing -v && node --test tests/browser/cloud/dual.test.mjs`；Expected封口/双轨FAIL。
- [ ] **Step 3:** 停止先await两轨尾片落IndexedDB及flush，再提交服务端登记的全部epoch/轨道清单，不能省略失败轨；验证长度/顺序/hash，空轨只部分保存。新增dual采集，用户独立点击触发共享、有轨/无能量区别展示；降级必须确认并记录mode/区间变化。跨epoch服务端时间锚点加不确定性，共享结束记录缺口，继续mic不冒充dual完整。事务封口后冻结input_version，自动超时扫描只封口不清有效对象。
- [ ] **Step 4:** 重跑PASS；测试双标签冲突、两轨不等长、断网恢复乱序、恢复后旧清单冲突、同epoch重复片时间冲突；实机AC05–AC10通过G0目标组合后才宣布双音源阶段完成。
- [ ] **Step 5:** `git add cloud web/cloud tests/cloud tests/browser/cloud && git commit -m "feat: seal recoverable dual-track recordings with visible gaps"`。

### Task 8（P2）：可恢复、公平、代次防护的持久任务

**Files:** Create `cloud/tasks.py`、`cloud/worker.py`、`tests/cloud/test_tasks.py`；Modify `cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`tests/cloud/support.py`。
**Interfaces:** `enqueue_final(ctx: AuthContext, meeting_id: str, input_version: int, force: bool) -> TaskView`；`claim_task(worker_id: str, now: datetime) -> ClaimedTask | None`；`renew_task(task_id: str, lease_token: str) -> None`；`publish_result(task_id: str, lease_token: str, result: TranscriptView) -> bool`；`cancel_task(ctx: AuthContext, task_id: str) -> None`。

- [ ] **Step 1:** 写 `TasksCase.test_expired_worker_cannot_publish`：租约过期后W2领取，W1发布false；`test_duplicate_submit_and_consume_once`：同输入/代次重复提交task id相同；`test_users_do_not_share_global_lock`：A任务在处理，B仍可上传/排队；`test_queue_message_owner_mismatch_rejected`：篡改owner不能读取或发布。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_tasks -v`；Expected持久领取/发布FAIL。
- [ ] **Step 3:** ProcessingTask表保存kind=final/live/voiceprint、受约束的会议或声纹关联、输入版本、generation、attempt、lease_token/deadline与安全错误码，FOR UPDATE SKIP LOCKED事务领取；按各用户最老等待任务轮转，不把所有槽给单用户。重试仅瞬态故障、有界次数（候选3），显式取消/删除不重试。提交重转写递增generation；前置和发布事务重新检查owner、输入、删除/用户撤销、generation、有效租约。
- [ ] **Step 4:** 重跑PASS；杀worker/重启API后领取同task，重复消费只1次发布；Task取消B=404；失效租约不意味着上传完整。
- [ ] **Step 5:** `git add cloud tests/cloud && git commit -m "feat: resume cloud processing through fenced durable tasks"`。

### Task 9（P2）：授权解码、分轨资产与Range回放

**Files:** Create `cloud/audio.py`、`cloud/playback.py`、`tests/cloud/test_audio.py`；Modify `cloud/objects.py`、`cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`。
**Interfaces:** `prepare_audio(ctx: AuthContext, meeting_id: str, input_version: int, workspace: Path) -> AudioInput`；`AudioInput(rate: int, tracks: dict[str, Path], mix_path: Path, gaps: list[Gap])`定义于contracts；`stream_audio(ctx: AuthContext, meeting_id: str, range_header: str | None) -> Response`。

- [ ] **Step 1:** 写 `AudioCase.test_preserves_48k_source_and_gap_timeline`：原轨对象不变、工作rate=16000、30000–60000缺口回放持续时间包含间隔；`test_private_range`：A bytes=0-99返回206/100字节，B=404、非法范围=416；`test_decoder_rejects_path_and_resource_bombs`：外部路径/超时解码拒绝且临时目录清除。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_audio -v`；Expected资产/RangeFAIL。
- [ ] **Step 3:** 仅从已授权冻结清单按epoch/序列连续解码，依据P0头片规则；FFmpeg固定参数argv无shell、时间/输出上限，工作区每任务独立。保存原轨，补静音只为回放保持时间轴，同时保留显式gap；ASR按有效区间处理防静音幻觉。认证代理流式Range返回，Cache-Control: private,no-store；多Range首版拒绝416，不用公网签名URL。
- [ ] **Step 4:** 重跑PASS及真实FFmpeg/Ogg/WebM或P0选定PCM链路；验证当前录音不播放、历史录音不自动播放并警告系统可能回录。
- [ ] **Step 5:** `git add cloud tests/cloud && git commit -m "feat: decode private tracks and serve authenticated playback"`。

### Task 10（P2）：最终稿、稳定说话人和重转写版本

**Files:** Create `cloud/models.py`、`cloud/transcripts.py`、`tests/cloud/test_transcripts.py`；Modify `cloud/worker.py`、`cloud/contracts.py`、`cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`tests/cloud/support.py`。
**Interfaces:** `ModelAdapter.transcribe(audio: AudioInput, speakers: int | None) -> TranscriptView`；`ModelAdapter.embed(samples: np.ndarray) -> Embedding`；`get_transcript(ctx: AuthContext, meeting_id: str, version_id: str | None) -> TranscriptView`；`list_versions(ctx: AuthContext, meeting_id: str) -> list[TranscriptView]`。

- [ ] **Step 1:** 写 `TranscriptsCase.test_old_task_finishes_last_without_pointer_regression`：T2先成/T1后成，current=T2；`test_enhancement_failure_keeps_basic_text`：ASR成功而diarize失败，assert有文字/warning匿名；`test_silence_is_empty`：empty=true、无纪要语料；`test_cross_track_no_duplicate_sentence_or_person`：沿用Task3三类期望断言，speaker不是source标签。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_transcripts -v`；Expected版本/降级FAIL。
- [ ] **Step 3:** 按model-contract实现选中策略与锁定参数，transcribe_samples仅显式注入引擎、繁简沿用zh-cn；不调用全局load_profiles。写入TranscriptVersion/Segment/Speaker及稳定会议speaker映射，成功事务才切当前指针；失败保旧稿，增强失败为completed_with_warnings。GET版本/详情、POST最终/补/重转写走Task8任务。
- [ ] **Step 4:** 重跑PASS；真实Linux离线模型回归Task3语料，报告实际结果；不同用户模型上下文/缓存隔离，断言A句子从不出现在B结果；重转写段落变化不移动原相对标注。
- [ ] **Step 5:** `git add cloud tests/cloud && git commit -m "feat: publish versioned cloud transcripts without regression"`。

### Task 11（P3）：持久事件快照与可降级实时稿

**Files:** Create `cloud/events.py`、`cloud/live.py`、`tests/cloud/test_events.py`；Modify `cloud/schema.py`、`cloud/worker.py`、`cloud/uploads.py`、`cloud/app.py`、`cloud/migrations/`、`web/cloud/session.mjs`、`web/cloud/recording.mjs`。
**Interfaces:** `read_events(ctx: AuthContext, meeting_id: str, after: int | None) -> list[EventView]`；`get_snapshot(ctx: AuthContext, meeting_id: str) -> dict`；`submit_live(meeting_id: str, owner_user_id: str, input_cursor: int) -> None`（仅内部调用）。

- [ ] **Step 1:** 写 `EventsCase.test_restart_replays_private_snapshot`：重启app后cursor可续，不可用cursor返回本人snapshot；`test_expired_session_closes_stream`：过期后无私人事件；`test_live_backlog_does_not_block_ack`：live阻塞时PUT片段仍ACK；`test_b_subscription_to_a_is_404`。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_events -v`；Expected持久恢复/实时降级FAIL。
- [ ] **Step 3:** 状态与结果提交同事务写ResourceEvent；SSE按meeting认证、不用内存pubsub作真相，Last-Event-ID为持久cursor，过期/撤销定期校验并关闭。实时任务单独低优先级容量与限流，从已ACK连续音频解码；拥堵暂停实时增强，最终任务不丢音频；临时稿不冒称最终声纹实名。
- [ ] **Step 4:** 重跑PASS；断线/切实例/丢事件/过期cursor/退出账号后确认不串资源；候选5–15秒仅负载测试报告，不当SLA。
- [ ] **Step 5:** `git add cloud web/cloud tests/cloud && git commit -m "feat: recover private events and degrade live transcription safely"`。

### Task 12（P3）：工作台、相对时间标注与无障碍

**Files:** Create `cloud/annotations.py`、`web/cloud/workbench.mjs`、`web/cloud/index.html`、`web/cloud/style.css`、`tests/cloud/test_annotations.py`、`tests/e2e/cloud/workbench.spec.mjs`；Modify `cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`。
**Interfaces:** `add_annotation(ctx: AuthContext, meeting_id: str, kind: str, offset_ms: int, text: str) -> AnnotationView`；`list_annotations(ctx: AuthContext, meeting_id: str) -> list[AnnotationView]`；`renderWorkbench(root, {meeting, transcript, annotations, recording}) -> void`。

- [ ] **Step 1:** 写 `AnnotationsCase.test_retranscribe_keeps_offset`：offset_ms=12345，切版本后仍12345；非法kind/负时间拒绝、B读写404；E2E `history_does_not_replace_active_recording`、`keyboard_and_narrow_screen`：录音条持续可见、手动滚动不追尾、320px无横向溢出、焦点可见/关闭弹窗回触发点。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_annotations -v && npx playwright test tests/e2e/cloud/workbench.spec.mjs`；Expected工作台定位/焦点FAIL。
- [ ] **Step 3:** 独立云端页保留三栏导航/文稿/笔记重点注释；time以offset_ms定位audio.currentTime，显示所用稿版本。录音条独立生命周期、显示连续ACK/缓存/两个音源与持久错误；未上传不能成功toast。G4采纳时用4px间距、16px正文、44px关键目标，1280/768断点、深浅色与reduced-motion；未采纳时只落实可达性/真实状态，视觉锁定前不能宣称AC20全部完成。不扩展标注编辑删除/稿手工编辑。
- [ ] **Step 4:** 重跑PASS，真实浏览器键盘、375/768/1280px、深浅色和减少动效验证；普通文字4.5:1、大字与控件3:1，计时/音量不每帧aria-live；保存截图作为视觉批准证据。
- [ ] **Step 5:** `git add cloud web/cloud tests/cloud tests/e2e/cloud && git commit -m "feat: add accessible cloud transcript and annotation workbench"`。

### Task 13（P3）：本人声纹库、浏览器登记与撤销竞态

**Files:** Create `cloud/voiceprints.py`、`web/cloud/voiceprints.mjs`、`tests/cloud/test_voiceprints.py`、`tests/e2e/cloud/voiceprints.spec.mjs`；Modify `cloud/models.py`、`cloud/worker.py`、`cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`tests/cloud/support.py`。
**Interfaces:** `enroll_profile(ctx: AuthContext, name: str, consent: str, audio: bytes, replace_version: int | None) -> TaskView`（登记任务meeting_id为空，TaskView遵循2.1契约）；`revoke_profile(ctx: AuthContext, profile_id: str) -> None`；`load_compatible_profiles(ctx: AuthContext, metadata: ModelMetadata) -> list[CompatibleProfile]`；`CompatibleProfile(profile_id: str, name: str, version: int, embedding: Embedding)`仅内部使用并定义于contracts；`match_profiles(ctx: AuthContext, transcript: TranscriptView, audio: AudioInput) -> TranscriptView`。

- [ ] **Step 1:** 写 `VoiceprintsCase.test_same_name_private_and_atomic_replace`：A/B同名分别成功，A替换失败旧version仍在，Unicode NFC等价姓名不重复；`test_same_dimension_different_model_excluded`：权重不同不能匹配；`test_revoke_before_publish_removes_new_real_name`：发布前撤销，assert新稿匿名/警告；E2E Esc后麦克风全部stop且无模板保存。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_voiceprints -v && npx playwright test tests/e2e/cloud/voiceprints.spec.mjs`；Expected版本/授权/撤销FAIL。
- [ ] **Step 3:** 姓名NFC+trim后(owner,name)唯一，显式授权与替换确认expected_version，上传音频而非服务器path；任务提取用注入extractor和样本算法，不用global PROFILE_DIR。加密特征，记录完整ModelMetadata、授权/采集时间与revocation；兼容全元数据后匹配P0校准high/low，疑似保留说明。输出事务核对所有使用模板版本/撤销；必要时重算匿名标签，失败不毁基本稿。
- [ ] **Step 4:** 重跑PASS，取消提取与关弹窗竞态、A/B并行登记/同名、撤销后缓存失效、原样本成功/失败/取消清理；历史标签默认保留须界面明示，若G4要求历史匿名化，先另写重处理计划，不暗中上线。
- [ ] **Step 5:** `git add cloud web/cloud tests/cloud tests/e2e/cloud && git commit -m "feat: enroll and revoke isolated versioned voiceprints"`。

### Task 14（P3）：职位、临时归纳/面试、安全AI渲染

**Files:** Create `cloud/positions.py`、`cloud/ai.py`、`cloud/ephemeral.py`、`web/cloud/interview.mjs`、`web/cloud/render.mjs`、`tests/cloud/test_ai.py`、`tests/e2e/cloud/ai.spec.mjs`；Modify `cloud/schema.py`、`cloud/app.py`、`cloud/migrations/`、`tests/cloud/support.py`。
**Interfaces:** `create_position(ctx: AuthContext, type: str, title: str, requirements: str) -> PositionView`、`update_position(ctx: AuthContext, position_id: str, type: str, title: str, requirements: str) -> PositionView`、`delete_position(ctx: AuthContext, position_id: str) -> None`；`summarize(ctx: AuthContext, meeting_id: str, transcript_version_id: str, speaker_ids: list[str] | None) -> AIResult`；`generate_interview(ctx: AuthContext, position_id: str, resume: InterviewInput) -> AIResult`；`renderSafeMarkdown(target: Element, markdown: str) -> void`。

- [ ] **Step 1:** 写 `AICase.test_exact_limits_and_no_persistence`：20000字接受/20001拒绝，5图接受/6拒绝，5*1024*1024+1拒绝，伪PNG/20000001pixels拒绝；请求成功/失败/取消后数据库、对象、持久队列、日志均无简历/结果哨兵；`test_ocr_and_output_budget`：OCR合并>20000拒绝，输出>40000安全失败不截断；`test_summary_selected_stable_identity`：按speaker_id只送所选稿版本发言；`test_untrusted_resume_not_system_instruction`：资料含“忽略规则/输出密钥”，assert固定system提示不变、资料只在user消息且模型不接工具/密钥上下文。真实模型恶意资料探针也须人工检查，不能由单元测试保证模型永不服从注入。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_ai -v && npx playwright test tests/e2e/cloud/ai.spec.mjs`；Expected限额/净化/清理FAIL。
- [ ] **Step 3:** 职位持久CRUD(owner)；AI调用使用平台固定allowlist配置/Key，不返回供应商原错，不随用户任意URL出网。临时隔离执行器限时120秒，断开取消/清理内存/临时文件，不写任务表正文；汇总显式版ID与稳定speaker集合，复用提示仅把资料放user，超长拒绝。图片解码验证真实类型/尺寸/总30MiB，OCR后重验长度；空简历按职位题，20/12/8分钟及2–4条答案要点沿用既有提示。总结默认不持久保存；如G4批准持久化，另增SummaryVersion任务，不悄加。
- [ ] **Step 4:** 重跑PASS；E2E script/onerror/javascript:/data:text/html等输入不执行、不出现危险链接；使用DOMPurify与允许http/https链接策略，输出长度验证后渲染；界面提示临时结果、疑似身份、未知负责人待定/无决议。真实模型生成结构由人工核对，不把mock提示测试当真实性保证。
- [ ] **Step 5:** `git add cloud web/cloud tests/cloud tests/e2e/cloud && git commit -m "feat: generate private ephemeral summaries and interview questions"`。

### Task 15（P4前置）：删除、注销、备份墓碑及临时清理

**Files:** Create `cloud/privacy.py`、`tests/cloud/test_privacy.py`；Modify `cloud/schema.py`、`cloud/tasks.py`、`cloud/uploads.py`、`cloud/playback.py`、`cloud/events.py`、`cloud/ephemeral.py`、`cloud/voiceprints.py`、`cloud/app.py`、`cloud/migrations/`。
**Interfaces:** `delete_meeting(ctx: AuthContext, meeting_id: str) -> None`；`revoke_account(ctx: AuthContext) -> None`；`run_cleanup(now: datetime) -> dict[str, int]`；`replay_tombstones(backup_id: str) -> int`。

- [ ] **Step 1:** 写 `PrivacyCase.test_delete_fences_inflight_publish`：删除事务后下载/事件/上传404，旧任务publish=false；`test_restore_does_not_resurrect`：恢复旧DB/对象快照再重放墓碑，assert记录不可见；`test_ack_reference_not_orphan`：租约到期有效chunk未删；`test_cancelled_ephemeral_cleanup`：请求清理失败残留按批准≤3600秒兜底。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_privacy -v`；Expected墓碑/清理FAIL。
- [ ] **Step 3:** 每个私人读写/Range/SSE/worker均强制检查墓碑，从第4任务起deleted_at即不可读；本任务完成立即generation递增、取消任务、吊销session、对象/派生版本/职位/声纹级联清理。删除墓碑保存在独立可重放审计存储，不随旧备份回退；恢复时先屏蔽服务，重放后再开流量。孤儿清理仅无有效归属/引用；法规保留及对象版本历史由批准政策明确。
- [ ] **Step 4:** 重跑PASS及真实备份恢复演练；目标24小时物理清理/30天备份只在G3批准后验收，权限即时拒绝始终必需。账号注销入口需批准流程与等待期；导出未批准不加功能，保留文档说明。
- [ ] **Step 5:** `git add cloud tests/cloud && git commit -m "feat: prevent deleted cloud records from returning"`。

### Task 16（P4）：全资源隔离、限流与攻击回归

**Files:** Create `cloud/limits.py`、`cloud/observability.py`、`tests/cloud/test_security.py`、`tests/e2e/cloud/isolation.spec.mjs`；Modify `cloud/app.py`、`cloud/auth.py`、`cloud/objects.py`、`cloud/ai.py`、`cloud/worker.py`、`cloud/settings.py`。
**Interfaces:** `check_limit(ctx: AuthContext, action: str, cost: int) -> None`；`safe_error(exc: Exception) -> tuple[int, str]`；`emit_metric(name: str, value: float, labels: dict[str, str]) -> None`。

- [ ] **Step 1:** 写 `SecurityCase.test_all_private_resource_operations`，矩阵覆盖meeting/upload/epoch/chunk/lease/task/versions/annotations/profile/position/summary/audio/events的列举、读改删取消；A资源给B全404且无变化。`test_size_limit_before_reading_full_body` assert超限413无完整读取；`test_no_sensitive_error_or_log` assertKey/验证码/正文/向量哨兵不在响应/日志；`test_session_revocation_and_ssrf` assert注销旧cookie401、用户URL不接受。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_security -v && npx playwright test tests/e2e/cloud/isolation.spec.mjs`；Expected遗漏接口/限流/日志断言FAIL。
- [ ] **Step 3:** 限制body流读取、时长、每owner活动租约、队列/AI调用、并发解码；批准预算驱动429、Retry-After与前端持久说明，失败不能成功toast。限流状态在PostgreSQL跨实例共享，DB不可用安全拒绝而非无限放行。鉴权/业务错误日志仅固定code/request_id/阶段，不记敏感正文、查询token或服务器路径。指标包括ACK延迟/缓存/缺片/排队/失败/降级/越权拒绝。
- [ ] **Step 4:** 全矩阵PASS；两context同时录音/转写/同名声纹、切账号和自然过期证明UI/IndexedDB/事件/worker隔离；测试CSRF/XSS/路径穿越/文件伪装/解码炸弹/过期租约/身份撤销。管理员无默认业务浏览接口。
- [ ] **Step 5:** `git add cloud tests/cloud tests/e2e/cloud && git commit -m "test: enforce cloud isolation and bounded private processing"`。

### Task 17（P4）：生产可部署交付、恢复与实际验收

**Files:** Create `cloud/health.py`、`deploy/cloud/Dockerfile.api`、`deploy/cloud/Dockerfile.worker`、`deploy/cloud/compose.yml`、`deploy/cloud/proxy.conf`、`deploy/cloud/model-manifest.json`、`docs/cloud-operations.md`、`tests/cloud/test_health.py`；Modify `cloud/main.py`、`cloud/app.py`、`cloud/worker.py`、`requirements-cloud.lock`、`docs/superpowers/evidence/cloud/acceptance.csv`。
**Interfaces:** `readiness() -> dict[str, bool]`（只给DB/存储/模型/队列就绪，无路径/Key）；`warm_models(manifest: Path) -> None`（缺缓存fail-fast、无隐式下载）；`python -m cloud.main`与`python -m cloud.worker`独立进程入口。

- [ ] **Step 1:** 写 `HealthCase.test_unwarmed_worker_not_ready`：缺模型readiness=false，API依然可接授权上传但不误报处理可用；`test_import_never_opens_server_devices`：mock sounddevice/MLX默认加载/下载被调用即FAIL；`test_health_has_no_secret_paths`。
- [ ] **Step 2:** Run `python -m unittest tests.cloud.test_health -v`；Expected就绪/启动边界FAIL。
- [ ] **Step 3:** API与worker分别构建Linux镜像，云端依赖不安装mlx-whisper/sounddevice，不照搬 [本地依赖](<../../../requirements.txt>)。固定Python/FFmpeg/ONNX/选定引擎/模型sha/license，模型由部署步骤预置；TLS同源代理、可信代理头、非root、加密DB/私有bucket/密钥轮换；SSE禁缓冲、Range可用。运行说明列迁移、预热、恢复、备份墓碑、清理、限额和回滚。
- [ ] **Step 4:** Run `python -m unittest discover -s tests -p "test_*.py" -v`、`node --test tests/browser/cloud/*.test.mjs`、`npx playwright test tests/e2e/cloud tests/browser/cloud/buffer.spec.mjs`、`git diff --check`；云端单独Linux跑 `python -m unittest discover -s tests/cloud -p "test_*.py" -v`。Expected所有适用测试PASS、无裸密钥/敏感fixture。Linux不要求执行依赖本机音频的本地测试；本地兼容套件在原支持平台跑。完成目标环境AC05–10、真实模型AC11–14、隐私恢复AC16–19、视觉AC20，把每条证据和版本填表；not_run不能改pass。
- [ ] **Step 5:** 经用户授权在既定地区部署，不另起服务冒充已有GUI；测试双用户同时、断网/尾片/崩溃、kill/restart、备份恢复、清理与限额。测3000ms片段、≤10000ms保存落后和5–15秒实时目标是否达到，报告硬件/网络/负载及真实容量；达不到调候选参数并复验，不编SLA。用户验收G0–G4/AC01–20通过后再提交/发布：`git add cloud deploy/cloud tests requirements-cloud.lock && git add -f docs/cloud-operations.md docs/superpowers/evidence/cloud/acceptance.csv && git commit -m "feat: qualify cloud deployment and recovery"`。

## 4. 需求覆盖与自审记录

| 设计章节/验收 | 负责任务 | 特殊门槛/证据 |
|---|---|---|
| 1–3 定位/基线/流程 | 1、4、6–7、12 | 不复用本地设备录音，单麦非终点 |
| 4–5 工作台/音源/视频边界 | 2、6–7、9、12–13 | 实机，不以mock替代音源 |
| 6 全链路归属/账号切换 | 4–8、11、13–16 | 所有操作统一矩阵 |
| 7 状态/上传/缺口/恢复 | 5–8、11、17 | ACK丢失、部分封口、保已ACK对象 |
| 8 文字/说话人/标注 | 3、10、12、14 | 会议级身份、版本、空音频 |
| 9 声纹元数据/授权 | 3、13、15 | 校准证据/撤销竞态 |
| 10 总结/面试 | 14–15 | 临时默认；持久化另审任务 |
| 11–14 架构/生命周期/API/安全 | 4–17 | PostgreSQL真相、Linux预热、墓碑 |
| 15 视觉 | 12、17 | G4与截图/键盘/对比度 |
| 16–18 探针/决策/交接 | 1–3、17 | 未批准/未测试仍明示，不提前编码 |
| AC01–04 | 4–5、8、16 | 认证、全资源越权、双用户、双标签 |
| AC05–10 | 2、5–7、9、17 | 实机双轨/断网/尾片/缓存/恢复 |
| AC11–14 | 3、8–10、12–13 | 增强失败、乱序版本、定位、声纹 |
| AC15–17 | 6、11、14–16 | 安全输入、不保存、会话过期/换账号 |
| AC18–20 | 8、12、15–17 | 墓碑/恢复/幂等/视觉可达性 |

**自审结论（计划检查，不是实现验收）：** 设计核心需求均有任务映射；五项Review Focus已进入拥有任务的断言。代码任务均有RED/GREEN与独立交付；决策任务不伪装软件测试。接口唯一来源为2.1与每任务Interfaces；依赖顺序4→5→6→7→8→9→10，11/12/13依赖10，14依赖10/12，15/16/17集成收口。Task13登记任务meeting_id为空；final/live非空，领取返回ClaimedTask携带租约，不允许各任务自创返回结构。

**不在本计划自动实现的条件分支：** 总结长期保存、历史实名批量匿名化、辅助客户端、开放注册/密码自研、数据导出/旧数据迁移、团队共享。前两项若获批准需补具体任务/迁移/验收；其余保持非目标或另行评审。认证商、编码及模型具体版本/参数由G1/G2证据锁定，不留给实施者随意猜测。

**共享任务约束：** final/live必须关联本人会议，voiceprint必须关联本人profile/temporary_sample；owner校验、租约和generation发布栅栏一致。不为InterviewInput建立持久任务。

## 5. 执行交接

请先审阅本计划及设计。未确认启动和执行方式前不执行任何实施任务；Task1沟通与探针执行也需用户确认启动，不把本次文档请求当执行授权。

- **Subagent-driven（推荐）：** 每个任务新实施者+新审查者，再做全分支审查；适合17个任务、跨浏览器/事务/隐私边界，避免一处接口错误导致丢音或越权。
- **Native：** 当前agent顺序实现，最后独立全分支审查；成本更低，但任务间没有持续独立审查。

建议先选择执行P0任务1–3，提交证据后锁定G0–G2，再重新核对后续任务。无需等待所有商业增强决定才能做本地合成探针；但任何部署/第三方传输和最终兼容性承诺都必须先过相应门槛。
