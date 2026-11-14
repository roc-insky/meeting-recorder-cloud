# 云端会议记录：部署与运维（Task 17 草案）

> **状态：仅编写，未构建、未启动、未演练。** 本仓库的 Docker 不可用（裁决 R3），
> Linux 镜像构建、容器启动、compose、nginx 配置、FFmpeg 安装、模型加载与真实部署
> 全部为 **not_run**。G0–G4 仍未回答，AC01–AC20 仍未运行（not_run）。
> 本文不构成容量、SLA、RPO/RTO 或合规承诺，**不可直接用于生产上线**。

本文件描述 Task 17 交付的部署脚手架与就绪语义。脚手架（`deploy/cloud/` 下的
Dockerfile、compose、nginx 配置、模型清单）只经过静态审阅，没有执行过；
任何“可以上线”的结论都必须等到 G0–G4 有书面答案并完成真实演练。

## 1. 上线前必须回答的门槛

| 门槛 | 当前状态 | 影响 |
| --- | --- | --- |
| G0 范围 | 未回答 | 不能承诺目标 OS/浏览器/会议软件兼容，也不能决定是否必须 macOS 桌面音频 |
| G1 隐私/身份 | 未实现 | 部署地区、第三方 AI 数据类别、邀请/开放与登录机制未定；`production` 启动会拒绝 `fake_test` |
| G2 采集/模型 | not_measured | 三探针、编码/解码/对齐去重与模型元数据未锁定；模型清单为空，`model` 就绪恒为 false |
| G3 生命周期 | 未证实 | 保留/备份/注销与 24h/30d 等数值只是候选值 |
| G4 业务/视觉 | 未采纳 | 总结持久化、历史实名撤回与设计系统未定；不得锁定视觉或宣称 AC20 |

在 G1 落地之前，`Settings.validate_for_startup()` 会在 `CLOUD_ENVIRONMENT=production`
时抛出固定错误；这是有意的 fail-closed 行为，不是部署故障。

## 2. 组件与文件

- `cloud/main.py`：API 入口，`python -m cloud.main`，只提供 HTTP API，不导入 worker。
- `cloud/worker.py`：独立 worker 入口，`python -m cloud.worker`；`--check` 只做本地就绪检查。
- `cloud/health.py`：四个就绪布尔值与离线模型校验；健康路由不鉴权、不泄露路径/桶名/密钥/异常文本/计数。
- `deploy/cloud/Dockerfile.api`、`Dockerfile.worker`：Linux 镜像（仅编写，未构建）。
- `deploy/cloud/compose.yml`：PostgreSQL + 私有 MinIO + API + worker（profile）+ nginx TLS（仅编写，未启动）。
- `deploy/cloud/proxy.conf`：同源 TLS 反向代理（仅编写，未执行 `nginx -t`）。
- `deploy/cloud/model-manifest.json`：空清单，`status: blocked_no_approved_engine`。

仓库说明：`docs/` 被 `.gitignore` 忽略，评审后如需提交本文档，必须由负责人执行
`git add -f docs/cloud-operations.md`（本任务不提交）。

## 3. 必需环境变量

凭据、密钥与 TLS 路径全部来自环境变量；compose 使用 `${VAR:?}`，缺失即拒绝启动。
不存在默认密码。

| 变量 | 说明 |
| --- | --- |
| `CLOUD_ENVIRONMENT` | 部署为 `production`；开发/测试才允许其他值 |
| `CLOUD_DATABASE_URL` | 必须是 `postgresql+psycopg://`，不允许 query/fragment |
| `CLOUD_SECRET_KEY` | 生产至少 32 字符；轮换会使现有会话失效 |
| `CLOUD_PUBLIC_BASE_URL` | 生产必须是 `https://` 源（无路径） |
| `CLOUD_COOKIE_SECURE` | 生产必须为 true |
| `CLOUD_VOICEPRINT_KEY` | base64/base64url 的 32 字节 AES-GCM 密钥 |
| `CLOUD_FORWARDED_ALLOW_IPS` | 只信任的代理地址；必须设为 nginx 在 internal 网络中的固定地址 `172.31.240.10`，配置校验拒绝 `*` |
| `CLOUD_PRIVACY_AUDIT_PATH` | 追加写墓碑审计文件，例如 `/var/lib/cloud/privacy-audit.jsonl` |
| `CLOUD_S3_ENDPOINT` / `CLOUD_S3_ACCESS_KEY` / `CLOUD_S3_SECRET_KEY` / `CLOUD_S3_BUCKET` | 私有桶与专用服务账号；compose 不向应用传 MinIO root 凭据 |
| `CLOUD_MODEL_MANIFEST` / `CLOUD_MODEL_CACHE` | 本地清单与权重目录；compose 挂载 `/models:ro` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | 数据库凭据 |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | 仅初始化与运维使用，不进入应用容器 |
| `FFMPEG_VERSION` | 构建参数，无默认值；为空则构建失败 |
| `TLS_CERT_PATH` / `TLS_KEY_PATH` / `HTTPS_PORT` / `CLOUD_MODEL_CACHE_DIR` | TLS 与挂载路径、发布端口 |

`CLOUD_HOST=0.0.0.0` 只在容器内使用；宿主机只发布 nginx 的 TLS 端口，
数据库、MinIO 与 API 端口都不发布，也不占用现有本地 GUI 端口 3080。

## 4. 数据库迁移

```bash
export CLOUD_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DB'
.venv-cloud/bin/alembic upgrade head
.venv-cloud/bin/alembic current
.venv-cloud/bin/alembic check        # 仅在迁移与模型一致时通过
.venv-cloud/bin/alembic downgrade -1 # 回滚一步；是否可逆取决于具体迁移
```

迁移必须在 API 启动前完成。真实 PostgreSQL 上的演练属于部署阶段，**not_run**。

## 5. 启动命令

```bash
# API（生产；当前会因 G1 未落地而拒绝启动）
python -m cloud.main

# worker 本地就绪检查：输出与健康路由相同的四个布尔值
python -m cloud.worker --check

# worker 正常启动：没有注入 processor 时以固定代码退出 2
python -m cloud.worker
```

`--check` 退出码：0 表示四项全为 true；1 表示至少一项为 false（空清单时 `model` 必为 false）。
未提供 processor 的正常启动以 `worker_processor_not_configured`（stderr，退出码 2）拒绝，
不会伪造转写结果。

## 6. 就绪语义

`GET /api/cloud/health` 不需要登录，响应体**恰好**四个布尔值：

| 键 | true 的条件 |
| --- | --- |
| `database` | 新建的短生命周期会话在短超时内执行 `SELECT 1` 成功 |
| `storage` | 已配置的私有桶可达（`bucket_exists`） |
| `model` | 本地清单与权重存在、大小与 SHA-256 匹配、许可证字段存在 |
| `queue` | `processing_tasks` 表可查询 |

- `database`/`storage`/`queue` 任一为 false → HTTP 503。
- 仅 `model` 为 false → 仍返回 200：已授权用户的上传与采集不依赖引擎。
- 响应始终 `Cache-Control: no-store`；不返回路径、桶名、密钥、异常文本或行数。
- 健康探针不下载模型、不导入 MLX/音频设备库、不依赖全局引擎。

## 7. 模型清单与离线预热

清单格式（`schema_version: 1`）：

```json
{
  "schema_version": 1,
  "status": "blocked_no_approved_engine",
  "models": [
    {
      "id": "operator-defined",
      "version": "operator-defined",
      "runtime": "operator-defined",
      "file": "relative/path/weights.bin",
      "sha256": "64 lowercase hex characters",
      "license": "operator-verified license id",
      "bytes": 123456789
    }
  ]
}
```

- `file` 必须是相对清单目录（或 `CLOUD_MODEL_CACHE`）的相对路径；绝对路径与 `..` 逃逸一律拒绝。
- `sha256` 必须是 64 位小写十六进制；`bytes` 必须与实际大小完全一致；`license` 不能为空。
- 校验只读本地字节，**不会下载、安装或导入任何引擎**。
- 仓库内的清单 `models` 为空，因此 `model` 恒为 false，worker 的 `--check` 也会失败；
  这是对 Task 3（not_measured）的诚实表达。
- 离线预热：把权重与清单挂载到 `/models`（只读），运行 `python -m cloud.worker --check`，
  四项全为 true 才允许启动 worker。校验结果按清单/文件元数据缓存，成功探针不会重复哈希大文件。

## 8. 对象存储、加密与密钥轮换

- 桶必须私有；compose 的 `minio-init` 关闭匿名访问，并为应用创建仅限该桶的专用策略用户（不是全局 `readwrite`）。该初始化命令与策略未在 MinIO 上执行过，属于 **not_run**。
- 传输：容器内到 MinIO 使用私有网络；是否启用对象存储 TLS 由部署决定，当前脚手架未配置。
- 静态加密：脚手架**未配置也未验证** MinIO SSE；必须在 G3 前启用并验证，否则不得声称“已加密”。
- 轮换：`CLOUD_SECRET_KEY` 轮换会使会话失效；`CLOUD_VOICEPRINT_KEY` 轮换会使既有声纹无法解密；
  S3 凭据轮换需要同步更新 MinIO 用户与 compose 环境变量。以上流程均未演练。

## 9. TLS 与可信代理

- 只有 nginx 发布 TLS 端口；`proxy.conf` 设置 HSTS、`nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`，不添加任何 CORS 头。
- SSE（`/api/cloud/meetings/<id>/events/stream`）关闭缓冲与缓存，读超时 3600s。
- Range 请求与 206 响应原样透传；私有响应不缓存。
- API 通过 `CLOUD_FORWARDED_ALLOW_IPS` 只信任 nginx 一个地址；`*` 被配置校验拒绝。
- 证书与域名由运维提供；续期、OCSP、密码套件审计均未验证（**not_run**）。

## 10. 备份与恢复（真实演练未完成）

建议顺序（**仅单元级模拟，真实恢复 not_run**）：

1. 进入维护：停止 API，或设置 `CLOUD_PRIVACY_MAINTENANCE=true` 让 `/api/cloud` 返回 503。
2. 备份 PostgreSQL（`pg_dump`）与对象桶（版本化/镜像复制），记录时间点。
3. 恢复：先恢复数据库，再恢复对象；保持墓碑审计文件独立于数据库与对象存储。
4. 重放墓碑：把审计中的删除/撤销记录重新应用到恢复出的数据，并追加一条恢复回执。
5. 校验计数、抽样对象与审计链，确认后再解除维护。

墓碑审计必须append-only、独立存放；恢复期间不得写入新业务数据。RPO/RTO 未测量，不作承诺。

## 11. 清理与保留

`cleanup_target_s=86400`、`backup_retention_days=30`、`log_retention_days=30`、
`account_revocation_wait_s=604800` 都是**候选值**，G3 未证实。生产清理任务的调度、
执行时长、失败重试与监控均未演练；不得把它们当作已生效的保留承诺。

## 12. 限额与背压

- 请求体上限 30 MiB（`request_max_bytes`），nginx `client_max_body_size 30m`；
  **恰好 30 MiB 的文件在 multipart 编码后一定超过上限并返回 413**——这是有意的整请求上限。
- 采集缓冲、会议时长、恢复窗口、worker 租约与解码槽位均有配置上限；
  解码槽位（`decode_slot_s=180`）保证同一用户串行解码，过期可回收。
- 限流为固定窗口的候选预算（按动作区分），不是容量证明；并发与延迟仍未测量。

## 13. 回滚

1. 停止新版本 API 与 worker，保留旧镜像标签。
2. 若新迁移可逆且已演练，执行 `alembic downgrade` 到上一版本；**未演练的迁移不得盲目回滚**。
3. 回退镜像并重放健康检查；旧版本可能读不懂新版本写入的数据，需先备份。
4. 对象与审计文件按备份点恢复；回滚不是数据删除操作。

## 14. 未验证清单（必须如实上报）

- Linux 镜像构建、容器启动、HEALTHCHECK：**not_run**（Docker 不可用）。
- compose 配置解析与 `up`、MinIO 初始化命令、nginx `nginx -t`：**not_run**。
- FFmpeg 指定版本安装、真实音视频解码：**not_run**。
- 真实模型权重、许可证与 SHA-256、模型加载：**not_run**（Task 3 not_measured）。
- PostgreSQL 迁移/备份/恢复演练、对象存储加密与密钥轮换：**not_run**。
- 负载、并发、延迟、容量、SLA、RPO/RTO：**未测量**。
- G0–G4 未回答；AC01–AC20 未运行。
