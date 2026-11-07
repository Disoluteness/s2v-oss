# Script2Video Studio

一个从「脚本」到「成片」的 AI 短视频生产平台，以服务形态部署：HTTP API + 后台 Worker + Web 工作台，专为被其他系统（内部 AI 平台、外部合作伙伴、自动化流水线）调用的场景设计。

- 完整的中文口播短视频流水线：脚本 → 生产计划 → 口播稿 → 分镜 → 素材 → 剪辑方案 → 合成出片
- 一切皆产物（artifact）、一切皆审计：每个阶段产物带版本与血缘，关键决策落事件与决策日志
- 成本与风险前置：预算预留、付费供应商人工确认、质量意图降级必须显式确认
- 人机协同：五道人工审核关口，片子出来后仍可回退到任意阶段重做

## 项目定位与 Clean-room 声明

本仓库是从零构建的（clean-room）。它受 OpenMontage 的「分阶段产物 + 人工关口」流水线理念启发，但**不包含** OpenMontage 的任何源代码、pipeline 文件、schema、prompt、skill、UI 代码或渲染器组件。两个项目的唯一共同点是思路：把 AI 视频生产拆成可审核的阶段，而不是一次黑盒调用。

适用对象：

- 要自建数字人口播 / 品牌短视频生产服务的技术团队
- 要把视频生产作为能力通过 API / SDK / iframe 嵌入对外提供的平台方
- 需要多租户、权限、审计、配额、成本核算的内部生产系统

不适用：想要一个开箱即用的桌面剪辑软件 —— 本项目是服务端平台，媒体文件存对象存储，渲染走 FFmpeg Worker。

## 核心特性

**流水线与产物**
- 7 条流水线：正式版 `avatar_spokesperson`（数字人口播，品牌广告 / 知识口播 / 商品种草），兼容键 `spokesperson_ad`，以及 5 条 beta：`animated_explainer`、`source_clip_factory`、`screen_demo`、`hybrid_story`、`localization_dub`
- Pipeline DSL v1：流水线定义随 run 落快照，事后改文件不影响历史 run
- 产物 Schema Registry：`production_plan`、`spoken_script`、`shot_list`、`preflight_report`、`execution_plan`、`composition_graph` 等产物写入前校验，携带 `producer` / `lineage` / `derived_from` 血缘
- Run 状态机：`pause` / `resume` / `cancel` / `rerun_stage` / `rerun_artifact` / `skip_optional_stage`，重跑允许从 `completed` 发起，下游已通过的关口按轮次重开

**审核与风控**
- 五道人工审核关口（plan / script / storyboard / asset_review / final_review），批准范围版本锁定，决策只增不改，重跑自动作废旧关口
- 三道启动闸门：质量意图降级需确认（`intent_downgrade_not_acknowledged`）、成本预留超预算拒绝启动（`cost_reservation_exceeds_budget`）、首次付费供应商调用需人工授权（`paid_provider_confirmation_required`）
- 审核可挂证据附件（引用不复制）、评论与待办事项

**多租户与权限**
- 团队角色（owner/admin/member/viewer）为基线，项目角色只放大不收回，取并集
- 23 个权限键，其中 7 个租户级（团队 / API key / webhook / 集成 / 配额 / 供应商凭据 / 部署管理），不入席位一律 403
- 所有特权变更与拒绝落审计日志

**API 契约**
- 统一失败信封 `{"detail": {"code", "message", ...}}`，错误码稳定可分支，目录可通过 `GET /api/error-codes` 发现
- `Idempotency-Key` 幂等重放（同 key 不同 body 返回 409）、`Page` 分页信封、OpenAPI 文档 124 路径 / 156 操作
- 版本冻结 1.1.0，兼容策略分「稳定面 / 实验面 / 不承诺面」（`docs/API_STABILITY.md`）

**接入面**
- REST API + Python SDK（零运行时依赖）+ TypeScript SDK，两者自动附幂等键、只重试可重试错误、`waitRun` 停在审核关口而非空转
- `s2v` CLI（随 Python SDK 安装）：`s2v quick "脚本"` 一条命令从脚本到下载成片
- 出站 Webhook：事件目录公开、事务性 outbox、HMAC 签名 at-least-once 投递、指数退避、自动暂停与手动重放
- 可嵌入工作台：无状态签名令牌（token 只进 URL fragment 不进服务端日志）、按集成的 `frame-ancestors` 白名单、最高只授 `run:review` 写权限
- **让 AI 驱动视频生产**：仓库自带 `skills/s2v-pipeline/` —— 一个给 Claude Code / Codex 等 AI 工具用的 skill（纯标准库 CLI 脚本 + 逐阶段参考文档），装上后直接说「把这段稿子做成 15 秒口播」，AI 就会驱动本平台逐阶段出片，每一步停下来让人确认，不满意还能退回任意阶段重做。详见 `skills/s2v-pipeline/SKILL.md`

**素材与媒体**
- 对象存储抽象（本地 / MinIO），项目级资产库带出处与许可条款，复用语义是「复制 + 记来源」
- DashScope 适配器：TTS（qwen3-tts-flash）、图像（qwen-image-2.0-pro）、数字人（万相 `wan2.2-s2v`，异步提交 / 轮询 / 归档）；EMO（`emo-v1`）已下线，适配器按模型名前缀自动选协议方言
- FFmpeg 合成：画布缩放、SRT 字幕烧录、旁白与 BGM 混音；渲染档案内置抖音 / 小红书 / B 站 / 视频号 / YouTube Shorts 规格
- 质量门：ffprobe 可读性、分辨率、帧率、音频响度、黑帧 / 冻结帧风险、字幕溢出、CJK 字体就绪，0-100 打分
- 剪辑工具：`trim` / `concat` / `subtitle_burn_in` / `auto_reframe` / `watermark` / `cover_generation` / `export_versions`

**Worker 与运维**
- 数据库租约队列，多 Worker 不重复消费；按 `provider` / `analysis` / `media` / `render` 分队列的亲和角色；死信状态可经 API 重放；本地工具步骤自动指数退避重试（付费提交永不自动重试）
- Prometheus `/metrics`（无凭据抓取、不出现在 OpenAPI）、结构化 JSON 日志、每响应 `X-Request-Id`
- 团队配额：成本 / run 数 / token 三维上限，`calendar_month` / `rolling_30d` / `lifetime` 窗口，硬闸与软告警，单 run 覆盖
- 供应商密钥可选静态加密（AES-256-GCM，AAD 绑定租户与密钥名），轮换一键迁移
- 备份 / 恢复脚本（恢复与维护模式互锁）、维护模式（挂写不挂读）、部署姿态自报（版本 / 迁移位点 / 能力开关 / 表数）
- 生产 compose（迁移一次性服务，四角色 Worker）与 Kubernetes manifests

## 架构一览

```text
                ┌────────────────────────────────────────────┐
                │           FastAPI (apps/api)               │
   Web 工作台 ──▶  124 条路由 · 权限中间件 · 幂等 · 事件日志  │◀── SDK / CLI / 外部服务
                └───────┬────────────────────┬───────────────┘
                        │                    │
              ┌─────────▼────────┐  ┌────────▼─────────┐
              │  MySQL 8 (36 表) │  │  Redis / MinIO   │
              │  run / 工件 / 租约│  │  队列 / 媒体对象  │
              └─────────┬────────┘  └────────▲─────────┘
                        │                    │
                ┌───────▼────────────────────┴───────┐
                │        Worker (apps/worker)         │
                │  manifest 步骤调度 · provider 提交/  │
                │  轮询 · FFmpeg 合成 · webhook 投递  │────▶ DashScope / ElevenLabs / Kling
                └────────────────────────────────────┘
```

核心概念：

| 概念 | 说明 |
| --- | --- |
| Pipeline | 流水线定义（`pipelines/*.json`），随 run 落版本快照 |
| Run | 一次生产过程，按阶段推进，状态机管理 |
| Stage | 阶段；带 `gate: true` 的阶段停在 `waiting_review` 等人 |
| Gate | 人工审核关口：快照、批准范围锁定、只增决策 |
| Artifact | 阶段产物（计划 / 口播稿 / 分镜 / 执行计划 / 渲染摘要…），版本化 + 血缘 |
| Asset | 媒体文件元数据；字节在对象存储，访问走签名链接 |
| Job | 后台作业（数字人提交轮询、合成、导出），租约队列消费 |

## 快速开始

本机开发（Python ≥ 3.11）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
make api
```

打开：

```text
http://localhost:8080/api/health
http://localhost:8080/docs
http://localhost:8080/workspace
```

Docker Compose（MySQL 8 + Redis + MinIO + API + Worker）：

```bash
cp .env.example .env
docker compose up --build
docker compose exec api alembic upgrade head
```

Compose 网络内 API 连 `mysql:3306`；宿主机暴露 MySQL `3307`、API `18080`（避开常见端口冲突），Redis 与 MinIO 不出网络。Worker 启动即自动处理队列任务；调试用：

```bash
docker compose exec worker python -m apps.worker.background_jobs --once
```

## 第一个 Run（最小调用链）

> 要从别的服务接入？先读 `docs/API_INTEGRATION.md`：它给出调用顺序（先调哪个后调哪个、为什么）、如何拿 API key、每条 run 都要走的审核循环，以及轮询之外的 webhook 方案。本节只是最短路径，那份文档才是通往生产的路径。

```bash
### 1. 建项目
curl -X POST http://localhost:8080/api/projects \
  -H "Content-Type: application/json" \
  -d '{"title":"秋天奶茶广告"}'

### 2. 在项目下建 run
curl -X POST http://localhost:8080/api/projects/<project_id>/runs \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline_key":"avatar_spokesperson",
    "raw_script":"秋天第一杯奶茶，还没人给你点？",
    "brand_name":"示例品牌",
    "product_name":"冲泡奶茶",
    "target_platform":"douyin",
    "target_duration_seconds":15
  }'

### 3. 预检 → 生成可审计执行计划 → 启动
curl -X POST http://localhost:8080/api/runs/<run_id>/preflight
curl -X POST http://localhost:8080/api/runs/<run_id>/execution-plan
curl -X POST http://localhost:8080/api/runs/<run_id>/start

### 4. 审核循环：run 停在 waiting_review 时，找到打开的关口并批准
curl "http://localhost:8080/api/runs/<run_id>/review-requests?status=open"

curl -X POST http://localhost:8080/api/review-requests/<request_id>/decisions \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"OK"}'

### 或一键过当前阶段（等价，基于同一审核内核）
curl -X POST http://localhost:8080/api/runs/<run_id>/stages/<stage_key>/review \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"OK"}'
```

两条重要认知：

- **没配供应商 key 时，run 停在 `waiting_review` 是正常态**，不是失败 —— 人本来就是流水线的资源。逐关提交决策后 run 才继续推进。
- `start` 有三道闸门：质量意图降级需带 `{"accept_intent_downgrade": true}` 确认；计划总成本超出 `cost_budget` 拒绝启动；首次付费供应商调用需 `POST /api/runs/<run_id>/provider-consents` 人工授权。

不想马上接真实供应商？MVP 示例端点用本地确定性 FFmpeg 媒体走完整脚本到 MP4 链路（默认 `local_preview` 模式）：

```bash
curl -X POST http://localhost:8080/api/mvp/sample-runs \
  -H "Content-Type: application/json" \
  -d '{
    "title":"秋天奶茶示例",
    "raw_script":"秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
    "brand_name":"示例品牌",
    "product_name":"冲泡奶茶",
    "target_duration_seconds":15
  }'
```

可运行的端到端示例（仅 curl + python3）在 `examples/curl/run_pipeline.sh`；SDK 版在 `sdk/python/examples/` 与 `sdk/typescript/examples/`；`examples/README.md` 是三者的索引图。

## SDK 与 CLI

```bash
pip install ./sdk/python   # 提供 `import s2v` 与 `s2v` 命令
```

```python
from s2v import Script2VideoClient

client = Script2VideoClient(base_url="http://localhost:8080", api_key="s2v_...")
video = client.script_to_video("秋天第一杯奶茶，还没人给你点？", project_title="上新")
print(video.download_url)
```

```bash
s2v quick "把这段脚本做成 15 秒口播" --download out.mp4
s2v runs create <project_id> "脚本内容" --budget 5 --platform douyin
s2v runs start <run_id> --accept-paid-providers
s2v runs wait <run_id>      # 停在 waiting_review，退出码 0 —— 这是停靠不是失败
s2v review submit <run_id> approve --note "OK"
s2v video download <run_id> -o final.mp4
```

退出码是契约的一部分：`0` 成功、`1` 失败、`2` 用法错误、`3` 等待超时、`4` run 以 `failed` 收场。

## 数字人链路要点

适配器 `dashscope_avatar_video`，异步生命周期：提交 → 轮询 → 归档为资产 → FFmpeg 合成。

- 默认模型 `wan2.2-s2v`（万相-数字人），自己定位说话人，无需人脸框；`provider_options.resolution` 选 480P / 720P
- **硬约束：音频必须 < 20 秒且 < 15MB** —— 长口播脚本先切段再提交
- 已下线的 `emo-v1` 模型走旧协议（需要人脸检测一跳）；适配器按模型名前缀 `emo*` 自动分流，把 `DASHSCOPE_AVATAR_MODEL` 改回 emo 开头即回旧协议
- 本地存储的人物图 / 音频要提交给供应商时，必须配 `PUBLIC_BASE_URL`（平台生成临时签名资产链接，把绝对 URL 交给供应商拉取）

从已有资产提交（平台代签临时链接）：

```bash
curl -X POST http://localhost:8080/api/runs/<run_id>/avatar-tasks/from-assets \
  -H "Content-Type: application/json" \
  -d '{"person_image_asset_id":"<asset_id>","audio_asset_id":"<asset_id>"}'
```

非阻塞执行用后台作业（Worker 自动提交 / 轮询 / 归档 / 合成）：

```bash
curl -X POST http://localhost:8080/api/runs/<run_id>/jobs/avatar-from-assets \
  -H "Content-Type: application/json" \
  -d '{"person_image_asset_id":"<asset_id>","audio_asset_id":"<asset_id>"}'
```

## 配置速查

完整清单与注释见 `.env.example`，最常用的几项：

| 变量 | 作用 |
| --- | --- |
| `DATABASE_URL` / `REDIS_URL` | MySQL 与 Redis 连接（compose 默认指向内置服务） |
| `OBJECT_STORAGE_BACKEND` | `local`（默认）或 MinIO/S3 |
| `PUBLIC_BASE_URL` | 供应商回拉本地资产用的对外地址；嵌入链接也基于它 |
| `DASHSCOPE_API_KEY` | TTS / 图像 / 数字人供应商 key（缺省时相关工具显示不可用，不报错） |
| `DASHSCOPE_AVATAR_MODEL` / `DASHSCOPE_AVATAR_RESOLUTION` | 数字人模型（默认 `wan2.2-s2v`）与清晰度（默认 480P） |
| `SCRIPT2VIDEO_PLANNER_PROVIDER` | `rule_based`（默认，本地确定性，不花钱）或 `dashscope`（qwen-plus） |
| `ACTOR_TRUST_MODE` | `open`（默认，信任网关注入的身份头）或 `secret`（对外暴露前必切） |
| `API_KEY_PEPPER` | API key 哈希盐，**改了全部已发 key 作废**，设一次保持稳定 |
| `SECRET_ENCRYPTION_KEY` | 可选：供应商密钥静态加密（AES-256-GCM） |
| `EMBED_TOKEN_SECRET` | 可选：嵌入令牌签名密钥，接入外部平台前必设 |

注意：compose 的 `environment:` 插值中，**shell 环境变量优先于 `.env` 文件** —— 改了 `.env` 之后如果容器没生效，先 `unset` 残留变量再 `docker compose up -d --force-recreate`。详见 `docs/DEPLOYMENT.md` §2。

## Planner 与供应商

Planner 默认本地确定性 JSON 生成（不花钱、结果可复现）。切到 DashScope：

```bash
SCRIPT2VIDEO_PLANNER_PROVIDER=dashscope
DASHSCOPE_API_KEY=your-key
DASHSCOPE_LLM_MODEL=qwen-plus
```

供应商 key 三种来源：`.env` 环境变量、数据库托管的 provider credentials（响应只回掩码预览，需 `provider:manage` 权限）、供应商回调（`POST /api/provider-callbacks/<provider>`，可选 HMAC 签名）。预检报告会按工具依赖声明每个阶段是 `ready` / `degraded` / `blocked`，缺什么环境变量、二进制、回退方案一目了然。

## 部署与运维

```bash
### 生产栈：基础设施端口不发布，迁移作为一次性 migrate 服务，四角色 Worker
docker compose -f docker-compose.prod.yml up -d --build

### 升级前预检（读数据库与配置本身，exit 0/1/2，支持 --json）
python scripts/deployment_preflight.py

### 部署后冒烟：纯 HTTP 走一遍审核内核与全部版本化能力面（默认 390 项检查）
docker compose exec -T api python scripts/verify_review_loop.py

### 部署姿态自报：版本、迁移位点、能力开关、表数、维护开关
curl http://localhost:18080/api/deployment

### 维护模式：挂写不挂读
curl -X POST localhost:18080/api/deployment/maintenance \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

备份 `scripts/backup.sh`（dump + manifest + sha256，**有意不**备份 `.env`；retention 默认只报告，加 `--prune` 才删）；恢复 `scripts/restore.sh`（维护模式未开拒绝执行）。完整升级顺序、回滚路径、密钥轮换影响面见 `docs/DEPLOYMENT.md`（12 节 runbook，含有意不覆盖的部分：无 HA 数据库、无跨区灾备、无 KMS）。

测试：

```bash
pytest                 # 812 项
make lint              # ruff
```

## 目录结构

```text
apps/api/         FastAPI 应用（路由、中间件、领域逻辑）
apps/worker/      后台作业 Worker（队列消费、provider 轮询、FFmpeg）
apps/web/         内置 Web 工作台（无前端构建步骤的 FastAPI 页面）
packages/         provider 适配器、权限、工具契约等领域包
pipelines/        流水线 DSL 定义（7 条）
tool_catalog/     运行时工具目录（26 个工具）
sdk/python/       Python SDK（零依赖）+ s2v CLI
sdk/typescript/   TypeScript SDK
examples/         curl / Python / TypeScript 端到端示例
skills/           s2v-pipeline：给 Claude Code / Codex 等 AI 工具用的流水线 skill
scripts/          backup.sh / restore.sh / deployment_preflight.py / verify_review_loop.py
infra/            Dockerfile、生产 compose、K8s manifests
docs/             架构决策、API 稳定性契约、部署 runbook、版本计划
tests/            812 项 pytest
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| `docs/API_INTEGRATION.md` | 外部服务接入指南：调用顺序、API key、审核循环、webhook 方案 |
| `docs/API_STABILITY.md` | 版本兼容策略：稳定面 / 实验面 / 不承诺面 |
| `docs/DEPLOYMENT.md` | 12 节部署 runbook：升级、回滚、备份恢复、密钥轮换 |
| `docs/DECISIONS.md` | 架构决策记录（ADR） |
| `CHANGELOG.md` | 版本历史 |
| `docs/FULL_VERSION_ROADMAP.md` | 各版本验收说明 |
| `README.en.md` | 英文版 README（更长的端点级说明） |

## 已知边界

- 没配供应商 key 时，数字人 / TTS / 图像工具在预检中显示 `unavailable`，run 停在审核关口 —— 这是设计行为，不是故障
- 数字人成本估算目前返回 0：`cost_budget` 预算闸门对数字人调用暂无保护，配额维度不受影响
- `wan2.2-s2v` 要求音频 < 20 秒且 < 15MB；平台暂不自动切段，长脚本需调用方预切
- `/api/assets/{id}/download` 对 wav 文件可能返回 500（用 `/api/assets/{id}/content` 签名链接代替）
- 供应商 4xx 错误目前被归类为 `input.invalid_or_missing`，分类粒度待细化
- HTML/Canvas 预览是预览渲染器；最终 MP4 编码始终走 FFmpeg

## 版本与许可

当前版本 **1.1.0**（冻结承诺生效，见 `docs/API_STABILITY.md`）。数据库 36 张表，迁移 head `20261012_0022`。

以 [Apache License 2.0](LICENSE) 发布。

Clean-room 致谢：感谢 OpenMontage 项目公开分享的流水线设计思想 —— 分阶段产物、人工关口、可审计的执行计划 —— 本项目的实现从零开始，未复制其任何代码与资产。
