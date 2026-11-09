# Script2Video Studio Full Version Roadmap

更新时间：2026-09-18

## 0. 规划原则

Script2Video Studio 是 clean-room 实现。OpenMontage 只能作为高层产品形态与工程思想参考，不能复制它的源码、配置、schema、prompt、技能文档、UI、渲染组件或目录内容。

本路线图采用“能力对齐、实现自研”的方式推进：

- 可以参考的内容：多 pipeline 思路、工具注册与能力发现、阶段化制作、artifact 契约、人工审核门禁、provider fallback、成本与质量治理、项目工作台形态。
- 必须自研的内容：数据模型、API 协议、pipeline DSL、artifact schema、provider adapter、worker 调度器、Web 工作台、SDK、文案与提示词、渲染模板、测试方案。
- 后续所有开发任务必须从本文档的版本计划中领取，不再临时追加孤立任务。
- 每完成一个版本任务，需要更新 `docs/TASK_BOARD.md`，并执行对应验证。

## 1. 当前状态

当前项目已完成 MVP 阶段、V1 编排内核、V2 Provider / Tool 生态、V3 多 Pipeline 产品矩阵、V4 素材理解与创意规划、V5 高质量合成与渲染、V6 Web Studio 与团队协作版，已完成 V7 全部五项（HTTP API v1 契约、Python / TypeScript SDK、出站 Webhook、可嵌入工作台、CLI），以及 V6.6 审核附件与 **V8 全部五项** V8.1 Worker Scaling、V8.2 Observability、V8.3 配额与计费、V8.4 安全加固、V8.5 Deployment，并完成 **V1.0 发布收口**（任务板 T00-T91 完成，**V0–V8 九个里程碑全部收官，V1.0 完整生产版达成**）。`1.0.0` 起 `docs/API_STABILITY.md` 的兼容性承诺生效。

已具备能力：

- FastAPI 服务、MySQL 持久化、Alembic、Docker Compose、后台 worker。
- project / run / stage / artifact / asset / event / tool invocation / background job 基础模型，以及 V4 的 brand kit / style preset、V6.4 的 asset collection、V6.5 的 team / membership / audit log、V7.1 的 idempotency key、V7.2 的 api key、V7.3 的 webhook subscription / delivery、V7.4 的 external integration、V6.6 的 review attachment、V8.3 的 team quota、V8.4 的 encrypted secret、V8.5 的 deployment settings（34 张应用表；含 Alembic 自用的 `alembic_version` 为全库 35 张表）。
- 多 pipeline：`avatar_spokesperson`、`animated_explainer`、`source_clip_factory`、`screen_demo`、`hybrid_story`、`localization_dub`，素材剪辑三条 `talking_head` / `podcast_repurpose` / `cinematic`（见 `docs/CAPABILITY_GAP_PLAN.md` P1），并保留 `spokesperson_ad` 兼容 key。
- 规则版与 DashScope LLM 规划器入口。
- `production_plan`、`spoken_script`、`shot_list`、`preflight_report`、`execution_plan`、`stage_execution_manifest`、`asset_manifest`、`render_summary`、`publish_package`、`composition_graph`、`quality_report`、`review_*`、`proposal_packet` 等核心 artifact。
- DashScope TTS、DashScope 图像、DashScope 数字人、ElevenLabs BGM、FFmpeg 合成与剪辑、本地预览素材生成。
- 工具注册表、preflight 能力审计、execution plan provider 选择、stage execution manifest、幂等 dispatch、dry-run preview、成本预留与首次付费确认闸门。
- 后台任务入队、租约、轮询、失败重试、数字人归档、自动合成。
- 五个 Web 页面：`/workspace`、`/dashboard`、`/run-board`、`/asset-library`、`/team`；支持创建样片、选择 pipeline、审核、预检、执行方案、manifest 诊断、缺失输入修复、任务推进、资产预览、成片链接、资产库沉淀与复用、团队与项目成员管理、权限矩阵与审计流水。另有租户无关的可嵌入页面壳 `/embed/{integration_id}`（V7.4）。
- 请求身份（受信头 + 默认内部 owner 回退）、团队/项目两级角色、并集生效权限、跨实体审计流水。
- V7.1 API 契约：统一失败信封、稳定错误码目录（现为 26 个，`GET /api/error-codes` 可发现）、可选 `Idempotency-Key` 重放保护、`Page` 分页信封与 `X-Total-Count`，以及把 spec 与权限表钉在一起的契约测试。

- V7.2 SDK：`api_keys` 编程凭证（一次性明文、`scopes` 只减不增、`apikey:manage` 租户级权限）、Bearer 鉴权并入统一身份入口，以及 Python / TypeScript 两个零依赖薄封装（可插拔传输层、自动幂等键与重试、`wait_run` 停在有意义的停点）。
- V7.3 Webhook：事务性 outbox（与源事件同事务入队）、六个发布事件的公开词汇表（`GET /api/webhook-events` 可发现）、HMAC-SHA256 签名（时间戳在签名串内，一次校验同时防篡改与防重放）、指数退避重试（耗尽转 `abandoned` 可手动重投）、连续失败自动停用；两个 SDK 内置接收端校验函数。
- V7.4 Embeddable Workspace：外部平台用 API key 换一条**无状态签名令牌**链接（`s2v_embed_<payload>.<hmac>`，平台签发、伙伴永不持有密钥），iframe 打开钉定 project/run 的工作台；集成注册表带 origin 白名单并据此下发 `frame-ancestors`；上下文钉定（`embed_context`）先于 scope 判定（`embed_scope`），`run:review` 是唯一可选写权限，`run:write` / `run:start` 越界即拒（外宿主页面不得触发付费 provider）；集成 `disabled` 即刻撤销。

当前边界：

- 身份只是受信头（`X-Actor-Id`），没有真实登录与外部 IdP 对接；默认信任模式下任何能直连 API 的客户端都能自称身份。V7.2 的 API key 提供了程序化凭证，但**不是**真实登录——`actor_id` 仍只是外部身份系统里的一个字符串。
- 5 条 provider 密钥类端点按 V6.5 §6 曾保持开放，**V8.4 已接管**（`provider:manage` / 租户级）；第 6 条供应商回调保持开放但加 HMAC 签名校验——它没有平台身份，进不了 `POLICY`。
- 配额与用量归因（V8.3）、监控告警（V8.2）**已完成**：`team_quotas` 周期配额 + `usage_ledger_entries` 归到团队、`/metrics` + 结构化日志 + `GET /api/projects/{id}/observability` 面板。**仍未做的是**：项目级可见性收敛（谁能看见哪些项目需要产品定义，权限矩阵目前只有读/写两档粒度）、真实计费账本（发票 / 支付——平台没有收款方）、对外发布渠道。
- 编程访问凭证已具备（V7.2），但租户隔离未实现：表里有 `tenant_id`，查询仍落在单一默认租户上。
- 嵌入会话（V7.4）是**无状态签名令牌**，因此没有逐令牌吊销列表：撤销只能靠停用整个集成或轮换 `EMBED_TOKEN_SECRET`（后者会连带失效所有在途链接）。令牌也不携带实时席位——被移出团队不会自动让已签发的链接失效，除非集成被停用。这两点是「不落库」换来的，若将来需要细粒度吊销，得引入一张短期会话表。
- 工具系统已有 selector / fallback / 成本估算 / 质量评分；早期「provider 权限与用量计费不完整」这条边界已关闭——V8.4 补齐 provider 密钥权限（`provider:manage`，租户级）与落盘加密，V8.3 补齐用量计费归因。
- 只有一个默认租户，多租户路由（子域名 / 路径前缀）未实现。

## 2. 与 OpenMontage 完整形态的差异

| 能力域 | OpenMontage 思路层能力 | 当前 Script2Video 状态 | 差距 |
|---|---|---|---|
| Pipeline 数量 | 多种视频生产 pipeline | 1 条 spokesperson ad pipeline | 需要扩展成多 pipeline 产品矩阵 |
| Pipeline 定义 | manifest 驱动阶段、工具、审核、产物 | 已有 JSON pipeline 和 loader | 需要自研更完整 DSL、版本、校验、兼容策略 |
| 阶段指导 | 每阶段有导演式指导与审核标准 | 目前主要由代码逻辑和规则规划器驱动 | 需要自研 Stage Strategy 文档与可执行策略引擎 |
| Tool Registry | 按能力发现工具、依赖、fallback、成本 | 已有基础 registry 和 preflight | 需要 selector、fallback scoring、动态可用性、资源画像 |
| Provider 生态 | 多 TTS、图像、视频、音乐、字幕、分析、增强工具 | 已接 DashScope、ElevenLabs、FFmpeg | 需要扩展 provider adapter 与统一 async/webhook/cost 合同 |
| Artifact 契约 | 标准化 artifact schema，作为阶段间契约 | 已有 versioned artifact，但 schema 未系统化 | 需要自研 schema registry、校验、迁移、兼容 |
| Checkpoint / Review | 阶段 checkpoint 与人工审批 | 已有 stage review 和事件 | 需要可配置 gate、复审、变更请求、review report |
| 媒体分析 | 视频理解、转写、场景检测、抽帧、质量检查 | 基本未覆盖 | 需要 source media ingest 与 analysis pipeline |
| 合成运行时 | FFmpeg、Remotion/HTML、HyperFrames 等多运行时思路 | 当前以 FFmpeg 为主 | 需要自研 composition graph 与 HTML/Remotion 类渲染能力 |
| 风格系统 | playbook 管控视觉、动效、排版、音频 | 当前只有简单输入偏好 | 需要自研 style preset / brand kit / render profile |
| 工作台 | 本地 board / production board 思路 | 已有单页 workspace | 需要项目级 dashboard、版本对比、团队协作、操作审计 |
| 服务化 | OpenMontage 偏本地 agent workspace | 我们目标是 API / Web / SDK 服务 | 需要多租户、权限、SDK、Webhook、队列伸缩、观测、部署文档 |

> **上表是 V1 启动时的差距快照，不是当前状态。** 下面逐条回填到 V1.0 收官，用来回答「还有没有遗漏」这个问题；只回填事实，不复述上面已经写过的实现细节。

### 2.1 V1.0 收官后的逐条回填

| 能力域 | V1 启动时 | V1.0 状态 | 遗留（有意保留） |
|---|---|---|---|
| Pipeline 数量 | 1 条 | **5 条在册**（V1.0 时）：`avatar_spokesperson`（production）/ `animated_explainer` / `source_clip_factory` / `screen_demo`（beta）/ `spokesperson_ad`（draft 兼容键）；**V1.1 T94 补齐 `localization_dub` / `hybrid_story`（均 beta）后 7 条在册**；**P1 补齐素材剪辑三条（`talking_head` / `podcast_repurpose` / `cinematic`，均 beta）后 10 条在册**（`docs/CAPABILITY_GAP_PLAN.md`） | V3 点名的 6 条现已全部在册；OpenMontage 13 条中剩余 6 条见 `CAPABILITY_GAP_PLAN.md` P6 / P8 |
| Pipeline 定义 | JSON + loader | **DSL v1.0**（`dsl_version`）+ `pipeline_versions` 表 + run 绑定创建时版本 | `avatar_spokesperson.json` 缺 `dsl_version`，靠模型默认值兜住（见 `RELEASE_CHECKLIST.md` §5） |
| 阶段指导 | 规则规划器 | `docs/pipeline-strategies/` 4 篇 + `stage_executor` | 策略是**代码**而非运行时可配置的策略文档引擎 |
| Tool Registry | 基础 registry + preflight | selector / fallback scoring / cost model / quality profile（`tool_kit`） | — |
| Provider 生态 | DashScope / ElevenLabs / FFmpeg | **仍是这三家**（V1.1 在 DashScope 上补了 `dashscope_asr`，复用既有密钥） | **本表最大遗留**：V2 点名的 Azure / OpenAI-compatible TTS、Kling / HeyGen 类 avatar、视频生成、免版权素材检索均未接（用户决定：保留 adapter 扩展能力，按需再加） |
| Artifact 契约 | versioned，schema 未系统化 | `artifact_kit`（类型注册 + 校验 + `derived_from` 血缘） | artifact diff / replay 只有基础能力 |
| Checkpoint / Review | stage review + events | review request / decision / comment / action item / attachment 全链路 + proposal 与成本闸门 | — |
| 媒体分析 | 基本未覆盖 | `media_analysis_kit`：ffprobe 元数据 + 间隔抽帧 + **精确镜头边界（V1.1/T93）** + **音频能量时间线（V1.1/T93）** + **ASR 转写（V1.1/T93，DashScope qwen3-asr）** + `media_analysis_report` / `video_analysis_brief` | ASR 需要 provider 可达的音频 URL（引擎自拉流），本地存储路径须显式给覆盖地址；引擎只回纯文本，无词级时间戳（不伪造） |
| 合成运行时 | FFmpeg 为主 | `composition_kit`（composition graph）+ `canvas_renderer_kit`（HTML/Canvas）+ `render_profile_kit` | **Remotion-like 运行时**未做（V5 只要求「不依赖纯 FFmpeg 拼接」） |
| 风格系统 | 简单输入偏好 | `brand_kits` + `style_presets` 表 + render profile | — |
| 工作台 | 单页 workspace | **5 个页面**（`/workspace` `/dashboard` `/run-board` `/asset-library` `/team`）+ 审计流水 + 权限矩阵 + `/embed` 嵌入壳 | 版本对比（diff）未做，只有版本列表 |
| 服务化 | 本地 agent | API（120 path / 152 operation）+ 两 SDK + Webhook + 队列伸缩 + 观测 + 部署（V8.5）| **真实登录 / 外部 IdP**、**多租户路由**（子域名 / 路径前缀）未做；租户隔离只有列没有查询收敛 |

### 2.2 结论：还剩什么

把上表与 §1「当前边界」、`docs/RELEASE_CHECKLIST.md` §5 合起来看，**规划内（V0–V8 九个里程碑 + V1.0）没有未完成的项**。V1.1（`docs/V1.1_CAPABILITY_PLAN.md`）**已全部收官（T93–T98，版本抬 1.1.0）**：媒体分析高级项已由 T93 关闭，`localization_dub` / `hybrid_story` 已由 T94 关闭（在册 pipeline 5 → 7），列表排序已由 T95 关闭（六个分页端点的受控 `sort` 参数），真实登录已由 T96 关闭（migration `20261012_0022`），租户隔离收敛已由 T97 关闭，收口已由 T98 关闭（`APP_VERSION` = 1.1.0，首个增量次版本）。仍开放的：

1. **Provider 生态广度**：目前 2 家真实 provider（DashScope / ElevenLabs）。**用户已决定不做集中扩张**——保留 adapter 合同，按需再加；Azure / OpenAI-compatible TTS、Kling / HeyGen 类 avatar、视频生成、免版权素材检索都属于这一类。
2. **产品决策项**：`spokesperson_ad` 的 `draft` 状态、项目级可见性收敛的粒度定义。
3. **明确列为「不做」的运维与商业化项**：HA 数据库 / 跨区灾备 / KMS / worker 自动故障转移（`docs/DEPLOYMENT.md` §12）、真实计费账本 / 发票 / 支付、对外发布渠道。

## 3. 最终完整版本定义

完整版本不是“复制 OpenMontage”，而是一个可嵌入内部 AI 平台或 Web 平台的 Script-to-Video 服务。

完整版本需要达到：

1. 用户输入脚本、主题、素材、参考视频或已有媒体后，系统能选择合适 pipeline，并输出可审核、可重试、可追踪的视频成片。
2. 平台支持多种视频生产类型，而不是单一口播广告。
3. 每个阶段都有结构化 artifact、审核标准、执行记录、成本记录、工具调用记录。
4. 所有生成能力通过 provider adapter 接入，支持能力预检、fallback、异步任务、重试、取消、归档和成本归因。
5. Web 工作台和 SDK 使用同一套 API，不依赖本机手动安装复杂工具。
6. 部署形态支持单机 Docker、团队内部服务器、以及后续 Kubernetes / 云部署。
7. 质量、成本、权限、配额、安全和可观测性达到团队生产可用水平。

## 4. 版本路线图

### V0：MVP 闭环版（已完成）

目标：证明从脚本到成片的服务化闭环可行。

状态：DONE，对应 T00-T46。

已完成内容：

- 单 pipeline 口播广告。
- MySQL、worker、object storage、artifact、asset、event。
- provider-backed TTS、图像、数字人、BGM、FFmpeg 合成。
- Web 工作台和基础部署路径。
- preflight、execution plan、stage execution manifest、dispatch dry-run。

验收状态：

- 本地测试套件通过。
- Docker 部署路径已在 Linux 验证过健康检查与样片生成。

### V1：生产级编排内核版

目标：把 MVP 中散落的运行逻辑收束成稳定、可扩展、可审计的生产编排内核。

建议版本号：`0.2.x`

主要任务：

1. Pipeline DSL v1
   - 自研 pipeline schema。
   - 支持 stage、inputs、outputs、required artifacts、optional artifacts、review gates、tool requirements、dispatch policies。
   - pipeline 版本化，run 绑定创建时的 pipeline version。
   - 增加 pipeline migration / compatibility 策略。

2. Artifact Schema Registry
   - 自研 artifact schema registry。
   - artifact 写入前后做 schema 校验。
   - 支持 artifact version、schema_version、producer、lineage、derived_from。
   - 提供 artifact diff 和 artifact replay 基础能力。

3. Manifest Action API
   - 将前端修复动作抽象为后端 action。
   - 例如 `generate_person_image`、`upload_person_image`、`generate_tts`、`dispatch_ready_steps`、`retry_failed_job`。
   - API、Web、SDK、worker 共用同一套 action 合同。

4. Run State Machine v1
   - 明确 run / stage / job 状态转移表。
   - 增加取消、暂停、恢复、重跑阶段、重跑指定 artifact、跳过可选阶段。
   - 状态转移全部写 event log。

5. Cost / Usage Ledger
   - 每个 tool invocation 和 job 记录估算成本、实际成本、token/秒数/素材大小。
   - run 级成本汇总。
   - 支持成本预算门禁。

6. Error Taxonomy
   - 统一 provider/auth/quota/input/network/runtime/quality/cancelled 等错误类别。
   - worker 和前端显示可读修复建议。

需要 migration：

- pipelines / pipeline_versions 表。
- artifact schema 字段扩展。
- run action / cost ledger / error taxonomy 字段。

验收标准：

- 现有 spokesperson pipeline 用 Pipeline DSL v1 表达。
- 任一 run 都能解释“当前为何停在这里、下一步可做什么、缺什么输入”。
- Web 和 SDK 不再直接拼散落的修复接口，而是调用 manifest action。
- 全量测试通过，并包含状态机转移测试、artifact schema 测试、action 幂等测试。

### V2：Provider 与 Tool 生态版

目标：把单 provider MVP 扩展成可配置、可 fallback、可比较质量和成本的工具生态。

建议版本号：`0.3.x`

主要任务：

1. Tool Contract v2
   - 完整字段：capability、provider、runtime、dependencies、input_schema、output_schema、resource_profile、retry_policy、cost_model、quality_profile。
   - 所有工具统一 `validate`、`estimate`、`submit`、`poll`、`archive`、`cancel` 能力边界。

2. Selector 与 Fallback
   - 为 TTS、image、avatar、video、music、subtitle、compose 建立 selector。
   - selector 按可用性、成本、延迟、质量、用户偏好、任务适配度评分。
   - execution plan 记录所有候选项、选择原因和 fallback 原因。

3. Provider 扩展
   - TTS：DashScope、ElevenLabs、Azure、OpenAI-compatible、本地 TTS。
   - Image：DashScope、通用 OpenAI-compatible、可选本地图像工具。
   - Avatar / Lip sync：DashScope、Kling、HeyGen 类 adapter，但按我们自己的合同实现。
   - Video generation：Kling、通义万相/可灵类视频、Runway/其他 provider adapter 预留。
   - Music / SFX：ElevenLabs、素材库、免版权素材检索、本地 fallback。
   - Subtitle / ASR：本地 SRT、ASR 转写、字幕对齐。

4. Provider Credential Manager
   - 环境变量仍支持，但增加数据库加密存储或团队级配置。
   - 支持 workspace / project / user 级 provider key。
   - 支持 key 可用性测试、配额显示、禁用 provider。

5. Webhook / Callback
   - 支持 provider 异步回调。
   - worker 轮询与 webhook 可并存。
   - provider callback 统一转成 job event。

需要 migration：

- provider_credentials。
- tool_capability_snapshot。
- provider_usage_ledger。
- callback_events。

验收标准：

- 任一能力至少支持一个主 provider 和一个 fallback 或明确的 degraded path。
- provider 不可用时 preflight 和 execution plan 能准确解释原因。
- async provider 支持 submit/poll/cancel/archive。
- provider key 不再只能依赖 `.env`。

### V3：多 Pipeline 产品矩阵版

目标：从单一口播广告扩展到内部平台常见的视频生产场景。

建议版本号：`0.4.x`

优先 pipeline：

1. `avatar_spokesperson`
   - 从当前 `spokesperson_ad` 演进而来。
   - 支持品牌广告、知识口播、产品介绍、带货短视频。

2. `animated_explainer`
   - 纯文字/图形/图像/动画解释视频。
   - 适合“为什么天空是蓝色的”这类解释类视频。

3. `source_clip_factory`
   - 输入长视频或多个素材，输出短视频切片。
   - 需要转写、场景检测、片段评分、重构字幕。

4. `screen_demo`
   - 软件功能演示、网页录屏、产品 walkthrough。
   - 需要浏览器录制、步骤脚本、鼠标/高亮/字幕。

5. `localization_dub`
   - 输入视频，输出翻译字幕、配音、可选口型同步。

6. `hybrid_story`
   - 用户提供素材 + AI 补充素材 + 合成。

每条 pipeline 需要：

- 自研 pipeline definition。
- stage artifact schema。
- stage strategy 文档。
- preflight 能力映射。
- execution plan 策略。
- Web 创建表单和结果页。
- 至少一个端到端测试样例。

需要 migration：

- pipeline category / template metadata。
- source media metadata。
- transcript / scene / clip candidate tables。

验收标准：

- 至少 4 条 pipeline 可在 Web 工作台创建和运行。
- 每条 pipeline 都能生成 preflight、execution plan、stage manifest。
- 每条 pipeline 都有端到端 smoke test。

### V4：素材理解与创意规划版

目标：让平台可以处理“参考视频、已有素材、品牌资料、脚本放映室”这类真实生产输入。

建议版本号：`0.5.x`

主要任务：

1. Source Media Ingest
   - 上传视频、音频、图片、文档、链接。
   - 自动探测媒体信息、时长、分辨率、音轨、字幕。
   - 生成 source asset manifest。
   - 当前实现状态：T62 已完成 API / artifact 合同；先复用 assets、artifacts、events，不新增表结构。

2. Video Understanding
   - 抽帧、场景检测、镜头边界、音频能量、语音转写。
   - 生成 `source_media_review`、`video_analysis_brief`。
   - 参考视频只抽象节奏、结构、风格，不复制内容。
   - 当前实现状态：T63 已完成本地 ffprobe 元数据探测、ffmpeg 间隔抽帧、`media_analysis_report` 与 `video_analysis_brief`；精确镜头边界、音频能量和 ASR 转写后续增强。

3. Script Screening Room Integration
   - 提供脚本解析 API，将分镜脚本转成结构化 scene plan。
   - 支持内部平台把“脚本放映室”中的脚本一键创建 run。
   - 支持脚本版本、批注、审批、锁定。

4. Brand Kit / Style Preset
   - 品牌色、字体、logo、安全区、字幕样式、语气、禁用词。
   - project 默认 style preset。
   - pipeline 在 planning 阶段引用 style preset。

5. Proposal Packet
   - 在昂贵生成前输出 2-3 个制作方案。
   - 包含工具选择、预算、预计耗时、风险、样式预览。

需要 migration：

- source_media。
- media_analysis。
- script_versions。
- brand_kits。
- style_presets。
- proposal_packets。

验收标准：

- 能从参考视频生成结构化分析，不复制原内容。
- 能从脚本放映室传入的中文分镜脚本生成 scene plan。
- 昂贵 provider 调用前必须有 proposal / cost gate。

### V5：高质量合成与渲染版

目标：提升最终视频质量，让平台从“能生成”变成“能稳定产出可交付视频”。

建议版本号：`0.6.x`

主要任务：

1. Composition Graph
   - 自研中间层描述：timeline、tracks、clips、layers、captions、transitions、effects、audio mix。
   - FFmpeg、HTML/Canvas、Remotion-like renderer 都从同一个 composition graph 渲染。

2. HTML / Canvas Renderer
   - 服务端渲染动态图文、字幕、图表、品牌版式。
   - 适合解释视频、数据视频、产品演示。
   - 不复制 OpenMontage/HyperFrames/Remotion 组件，只实现自己的 scene graph 和模板。

3. Render Profiles
   - 抖音、小红书、B站、视频号、YouTube Shorts 等平台规格。
   - 分辨率、帧率、码率、安全区、字幕位置、封面规格。

4. Quality Gates
   - ffprobe 校验。
   - 音频响度、黑帧、静帧、字幕越界、中文字体缺失、分辨率错误。
   - provider 输出质量评分和人工复审入口。

5. Edit Tools
   - trim、concat、subtitle burn-in、auto reframe、watermark、cover generation。
   - 成片多版本导出。

需要 migration：

- composition_graph artifacts。
- render_profiles。
- render_jobs。
- quality_reports。

验收标准：

- 同一个 run 可导出多个平台版本。
- 字幕、字体、音频、黑帧、成片编码有自动 QA。
- animated explainer 不依赖纯 FFmpeg 拼接，具备动态图文合成能力。

### V6：Web Studio 与团队协作版

目标：把当前单页工作台升级成内部团队可长期使用的 Studio。

建议版本号：`0.7.x`

当前实现状态（2026-10-09）：1. Project Dashboard（T72）DONE；2. Run Board（T73）DONE；3. Review System（T74，另含 T75/T76/T77 三道相邻闸门）DONE；4. Asset Library（T78）DONE；5. Team / Permission（T79）DONE。**V6 五项任务全部完成，V6 收官。** 已落地页面：`/dashboard`、`/run-board`、`/workspace`、`/asset-library`、`/team`。验收标准「一个团队可同时管理多个项目和多个 run」与「所有关键操作有审计记录」由 V6.5 的 teams / memberships / audit_logs 承接。

主要任务：

1. Project Dashboard
   - 项目列表、run 列表、状态筛选、搜索、复制 run。
   - 失败任务聚合、待审核队列。

2. Run Board
   - 阶段泳道、artifact 时间线、decision log、cost log、job log。
   - 输入缺口和修复动作集中展示。
   - 成片预览、版本对比、下载。

3. Review System
   - 审批、驳回、带修改通过、评论、附件、责任人。
   - 修改请求可生成新的 action。

4. Asset Library
   - 项目资产库、品牌资产库、可复用人物图/声音/音乐/片段。
   - asset provenance 和授权信息。

5. Team / Permission
   - user、team、role、project permission。
   - 内部平台 SSO 或 token 接入。

需要 migration：

- users / teams / memberships。（`users` 已存在；`teams` / `team_memberships` / `project_memberships` 已完成，`20261009_0012`）
- comments / review_requests。（已完成，`20261008_0010`）
- asset_collections。（已完成，`20261009_0011`）
- permissions / audit_logs。（权限键与角色矩阵落在应用层常量 `apps/api/app/permissions.py`，不建表；`audit_logs` 提前在 `20261009_0012` 建表，V8 只需扩展不重建）

验收标准：

- 非技术用户能在 Web 上完成创建、审核、修复、下载。
- 一个团队可同时管理多个项目和多个 run。
- 所有关键操作有审计记录。

### V7：SDK 与平台嵌入版

目标：让你的 AI 工具平台或 Web 平台可以“像调用 SDK 一样”使用 Script2Video。

建议版本号：`0.8.x`

当前实现状态（2026-10-10）：1. HTTP API v1（T80）DONE；2. **SDK（T81）DONE**；3. **Webhook（T82）DONE**；4. **Embeddable Workspace（T83）DONE**；5. **CLI（T84）DONE——V7 收官**。**契约层**：统一失败信封与 22 个稳定错误码（`GET /api/error-codes` 可发现）、可选 `Idempotency-Key`（重放/冲突/过期接管）、`Page` 分页信封与 `X-Total-Count`，以及「spec 与权限表精确对齐」的契约测试。URL 前缀仍为 `/api`，版本以 `info.version` 表达（见 `docs/V7.1_API_CONTRACT_PLAN.md`）。**SDK 层**：`api_keys` 表已建（`20261010_0014`）并与 V6.5 的「请求身份」**合并成同一个判定入口**，Python（`sdk/python`，`import s2v`）与 TypeScript（`sdk/typescript`，npm `s2v-client`）两个零依赖薄封装已落地，覆盖路线图点名的六个业务封装（见 `docs/V7.2_SDK_PLAN.md`）。**嵌入层**：`external_integrations` 表已建（`20261010_0016`），嵌入会话是**无状态签名令牌**而非席位，`integration:manage` 是第 18 个权限键且为租户级，页面壳 `/embed/{integration_id}` 按集成的 origin 白名单下发 `frame-ancestors`（见 `docs/V7.4_EMBED_WORKSPACE_PLAN.md`）。

主要任务：

1. HTTP API v1
   - 稳定 OpenAPI。**已完成**：每个 operation 声明 ErrorEnvelope 响应模型，operation 集合被契约测试钉住。
   - 统一错误码。**已完成**：22 个码 + 类别 + 可重试标记，`GET /api/error-codes` 发布。
   - 幂等键。**已完成**：表 `idempotency_keys`，表头 `Idempotency-Key`，不带则行为不变。
   - 分页、过滤、排序。**分页已完成**（`Page` + `X-Total-Count`）；过滤按端点既有参数；**排序已由 V1.1 T95 完成**——六个分页列表端点的受控 `sort` 参数（字段白名单、未知字段 422、隐式主键决胜、默认序与旧版字节级一致），其余端点留待有真实需求时按同一 `SortSpec` 模式补。

2. SDK
   - Python SDK。**已完成**（`sdk/python`，导入名 `s2v`，零运行时依赖）。
   - Node/TypeScript SDK。**已完成**（`sdk/typescript`，npm `s2v-client`，ESM + NodeNext + strict）。
   - 常见业务封装：createRun、uploadAsset、startRun、waitRun、getFinalVideo、retryAction。**已完成**，并补齐 project / board / artifact / job / event / review / asset / consent / run-state / api-key 全套与两个一站式入口（`run_script` / `script_to_video`）。
   - 凭证。**已完成**：`api_keys` + `apikey:manage` + `scopes` 收窄，Bearer 优先于 `X-Actor-Id`，无请求头则零行为变化。

3. Webhook。**已完成**（`docs/V7.3_WEBHOOK_PLAN.md`）：六个发布事件（含 `run.completed` / `publish.completed`）经 `GET /api/webhook-events` 可发现；事务性 outbox 与 at-least-once 投递；`X-S2V-Signature` 签名（HMAC-SHA256、时间戳在签名串内）即重放保护；指数退避、`abandoned` 可手动重投、连续失败自动停用；两个 SDK 内置 `verify_webhook_signature`。

4. Embeddable Workspace。**已完成**（T83，`docs/V7.4_EMBED_WORKSPACE_PLAN.md`）：`external_integrations` 注册表 + 无状态签名令牌（`s2v_embed_<payload>.<hmac>`，平台签发、伙伴永不持有密钥）；`POST /api/external-integrations/{id}/embed-sessions` 换现成链接，开放端点 `GET /api/embed/{id}/session` 自举；页面 `/embed/{integration_id}[/run/{run_id}]` 把集成 id 放路径（算 `frame-ancestors`）、令牌放 fragment（不发给服务端、不进日志与 Referer）；上下文钉定先于 scope 判定，`run:review` 是唯一可选写权限，`run:write` / `run:start` 越界即拒；第 18 个权限键 `integration:manage`（租户级）与第 22 个错误码 `embed_session_invalid`；集成 `disabled` 即刻撤销（页面仍渲染但 CSP 退化为 `'none'`）。

5. CLI。**已完成**（T84，`docs/V7.5_CLI_PLAN.md`）：`s2v` 命令是 SDK 的零依赖外壳（`sdk/python/s2v/cli.py`，stdlib argparse；`pip install s2v-client` 即得），用于本地调试和服务器运维——支持创建 run（`runs create` / 一站式 `quick`）、上传素材（`assets upload`）、查看 job（`jobs list` / `retry`）、下载成片（`video download`），外加 `whoami` / `health` / `projects` / `runs wait` / `review submit` / `events` 等。配置优先级 flag > env > 配置文件（`s2v config set`，0600 落盘）；输出支持 `--json`；退出码 0/1/2/3/4 可脚本化（3 = 等待超时、4 = run 失败终态；`waiting_review` 是停点不是失败）。无新表、无 migration、无新端点——纯客户端版本。

需要 migration：

- ~~api_keys~~（已完成，`20261010_0014`）。
- ~~idempotency_keys~~（已完成，`20261010_0013`）。
- ~~webhook_subscriptions~~ / ~~webhook_deliveries~~（已完成，`20261010_0015`）。
- ~~external_integrations~~（已完成，`20261010_0016`）/ ~~review_attachments~~（已完成，`20261010_0017`）。V7.5 CLI 不需要 migration。

验收标准：

- 内部 AI 平台可以不接触底层 provider，直接通过 SDK 创建并等待一个视频 run。
- Webhook 能通知外部平台成片完成或人工审核需要介入。**已达成**：`stage.waiting_review` / `render.completed` / `publish.completed` 均已发布。
- SDK 示例覆盖脚本到成片、上传素材、参考视频、失败重试。

### V8：规模化运行与企业治理版

目标：支撑团队规模化使用和更严格的企业内控。

建议版本号：`0.9.x`

当前实现状态（2026-10-12）：1. **Worker Scaling（T86）DONE**——`background_jobs` 加 `queue`（角色亲和：provider / analysis / media / render）、`priority`（0–99 越小越优先，便宜的分析先于昂贵的合成）、真租约 `lease_expires_at`、`started_at`、退避 `next_run_at`（`20261010_0018`）；worker 支持 `--role`（默认 `all` 兼容单实例）；可重试的本地工具步骤失败自动指数退避重试（付费的数字人提交绝不无人值守重试），重试耗尽进死信 `dead` 并可 `POST /api/jobs/{id}/requeue` 全新重放；RUNNING 超时由收割器按 `WORKER_JOB_TIMEOUT_SECONDS`（默认 1800）处置（`waiting_provider` 豁免）。2. **Observability（T87）DONE**——`/metrics`（免鉴权、app 级直挂、不进 OpenAPI）+ `LOG_FORMAT=json` 结构化日志与 `X-Request-Id` + `GET /api/projects/{id}/observability` 四视图面板（成本 / 延迟分位 / 作业 / 错误事件）；worker 侧 outcome 计数走 `WORKER_METRICS_PORT`（默认关）；tracing 明确收缩不做。3. **Quota / Billing（T88）DONE**——团队周期配额 `team_quotas`（`calendar_month` / `rolling_30d` / `lifetime`，窗口在读取时算）+ 用量归因 `usage_ledger_entries → runs → projects.team_id`（`NULL` = 默认团队）+ 硬/软闸门接在 run 启动与每次付费调用前 + 单 run 一次性豁免（`quota:manage`）；真实计费账本 / 发票 / 支付明确不做（平台没有收款方）。4. **Security（T89）DONE**——provider 密钥落盘加密（`encrypted_secrets` + AES-256-GCM 的 `SecretBox`，AAD 绑定 `tenant_id`/`name`/`key_version`；**`SECRET_ENCRYPTION_KEY` 为空即关闭**）+ 5 条 `provider-credentials*` 端点从开放改受控（`provider:manage`，租户级第 6 个）+ 供应商回调改 HMAC 签名校验（复用 V7.3 的 `X-S2V-Signature` 格式）+ 上传三道关卡（编码长度先于解码 / 解码大小 / 类型白黑名单 + 魔数嗅探）+ 资产链接 TTL 夹取与生产默认密钥拒签 + 凭证操作审计（只记字段名不记值）；新表 `encrypted_secrets`（`20261012_0020`），新依赖 `cryptography>=43`，7 个可选环境变量。5. **Deployment（T90）DONE**——四件制品 + 两个新能力。**生产 compose** `docker-compose.prod.yml`（基础设施不发布端口；migration 收敛为一次性 `migrate` 服务，api 与四个 worker 全部 `service_completed_successfully`；worker 按角色四分；资源限制 + 日志轮转）；**K8s 模板** `infra/deploy/k8s/` 7 个清单（**明确不做 Helm chart**——只有一种部署形态，chart 只会引入 release-name 前缀 / values 文件 / 渲染步骤；Job 注释写明六步升级顺序是判断而非 YAML 能表达的）；**备份恢复** `scripts/backup.sh` / `scripts/restore.sh`（restore 有 maintenance 互锁，DB 侧破坏性、storage 侧叠加式；backup **不备份 `.env`**、retention 默认只报告）；**migration runbook** `docs/DEPLOYMENT.md`（12 节）+ 预检脚本 `scripts/deployment_preflight.py`（退出码 0/1/2）。两个新能力：**部署姿态 `GET /api/deployment`**（版本 / 迁移 head 与 pending / 能力布尔 / 维护开关 / 表数，全部算出来不缓存；`up_to_date` 在 head 未知时**不是**空真）与**维护模式**（开关放**数据库**不放 `.env`——需要它的时刻正是不能改文件重启的时刻；**只停变更不停读取**，`/api/deployment*` 与 `/api/provider-callbacks*` 两条写豁免，路径段匹配避免 `/api/deployment-evil` 继承豁免）。新表 `deployment_settings`（`20261012_0021`，单行 seed，全库 **35 张表**，唯一非租户级表）；权限键 21→**23**（`deployment:read` 宽授 / `deployment:manage` 租户级第 7 个）；错误码 25→**26**（`maintenance_mode`，503 但 **`retryable = true`**）；OpenAPI 120 path / 152 operation；  `app_version` **0.9.0**；无新依赖。（V1.0 随后把 `app_version` 冻结为 **1.0.0**，见下。）

主要任务：

1. Worker Scaling。**已完成**（T86，`docs/V8.1_WORKER_SCALING_PLAN.md`）：见上。不建 `worker_queues` 表——队列是 job 的属性列；planner 角色无对应 job 类型，留给后续任务化。
   - 多 worker 类型：planner、media、provider-poll、render、analysis。
   - 队列优先级。
   - job timeout、dead letter queue、重试策略。

2. Observability（已完成，T87）
   - Prometheus metrics（`/metrics`：HTTP 计数/直方图、队列与 run 抓取仪表；worker 侧 outcome 计数，`WORKER_METRICS_PORT` 可选开启）。
   - structured logs（`LOG_FORMAT=json` 的 `JsonFormatter` + 全链路 `X-Request-Id`）。
   - tracing（明确收缩：不做 OTel，request-id 已覆盖 api↔worker 日志关联，见 `docs/V8.2_OBSERVABILITY_PLAN.md`）。
   - job latency / provider error rate / cost dashboard（`GET /api/projects/{id}/observability`：成本 / 延迟分位 / 作业状态 / 错误事件四视图）。

3. Quota / Billing（已完成，T88）
   - team quota（`team_quotas`：周期 / `limit_cost` / `limit_runs` / `limit_tokens` / hard|soft）。
   - provider usage attribution（`usage_ledger_entries → production_runs.project_id → projects.team_id`；`NULL` 归默认团队）。
   - 成本上限和审批（启动与每次付费前拦截；`POST /api/runs/{id}/quota-override` 一次性豁免，需 `quota:manage`；项目归属用 `PATCH /api/projects/{id}/team`，同权限）。
   - 明确不做：真实计费账本 / 发票 / 支付、配额历史表、阶梯定价、新页面（见 `docs/V8.3_QUOTA_BILLING_PLAN.md`）。

4. Security（已完成，T89）
   - provider key 加密（`encrypted_secrets` + `SecretBox`：AES-256-GCM，AAD 绑定租户/名字/密钥版本；**opt-in**，`SECRET_ENCRYPTION_KEY` 为空即关闭；`rotate` 端点同时覆盖升级迁移与主密钥轮换）。
   - provider 密钥端点接管（5 条进 `POLICY`，`provider:manage` 租户级；第 6 条回调改 HMAC 签名校验）。
   - asset signed URL 策略（TTL 上限夹取 + 生产环境仍用开发默认密钥则拒绝签发）。
   - 文件类型扫描和大小限制（编码长度先于解码 / 解码大小 256 MiB / 类型白黑名单 + 魔数嗅探；**不是 AV 引擎**——拦的是「声明与内容不符」）。
   - 操作审计（凭证创建 / 更新 / 检查 / 轮换落 `audit_logs`，只记字段名与数量，**绝不记值**）。

5. Deployment（已完成，T90，`docs/DEPLOYMENT.md`）
   - production Docker Compose（`docker-compose.prod.yml`：基础设施不发布端口 + `migrate` 一次性服务 + worker 四角色 + 资源限制与日志轮转）。
   - Kubernetes Helm chart 或部署模板（**选部署模板**：`infra/deploy/k8s/` 7 个清单，明确不做 Helm）。
   - backup / restore（`scripts/backup.sh` / `scripts/restore.sh`；后者默认要 maintenance 互锁）。
   - migration runbook（`docs/DEPLOYMENT.md`：六步升级顺序 + 失败处置 + 回滚 + 症状排查表 + 明确不覆盖范围）。
   - 附带：维护模式（只停写不停读）与部署姿态自省（`GET /api/deployment`）。

需要 migration：

- ~~quotas~~（V8.3 已完成，落地为 `team_quotas` + `projects.team_id`，`20261012_0019`，全库 33 张表）。
- audit_logs（已存在，`20261009_0012`，只需扩展）。
- ~~encrypted_secrets~~（V8.4 已完成，`20261012_0020`，全库 34 张表）。
- ~~worker_queues~~（V8.1 用 `background_jobs` 的列替代，未建表）；job 扩展列已完成（`20261010_0018`）。
- ~~deployment_settings~~（V8.5 已完成，`20261012_0021`）。
- ~~user_sessions + users 密码列~~（V1.1 T96 已完成，`20261012_0022`，head，全库 **36 张表**）。

验收标准：

- 多 worker 并发处理不会重复执行同一 job。
- 关键指标可观测，失败可追踪。
- 支持生产环境升级、备份、恢复。

### V1.0：完整生产版

目标：达到可对内部团队正式发布的完整版本。

建议版本号：`1.0.0`

**状态：已完成（T91，`docs/V1.0_RELEASE_PLAN.md`）**——不新增能力，把「已经能跑」变成「可以对外承诺」：
`APP_VERSION` 冻结为 `1.0.0`（`info.version` 与之一致）；`CHANGELOG.md` 按版本聚合且每条写明兼容性影响；
`docs/API_STABILITY.md`（+ `docs/DECISIONS.md` 的 ADR-006）把接口分成稳定面 / 实验面 / 不承诺面并给出兼容规则；
`docs/RELEASE_CHECKLIST.md` 讲判据（含已知偏差如实记录与回滚触发条件）；`examples/` 提供 curl / Python SDK /
TypeScript SDK 三条可运行路径；`tests/test_release.py`（16 项）+ 门禁第 25 节（13 项）把承诺变成断言。
无 migration / 无新表 / 无新依赖 / 无新环境变量。

必须满足：

- 至少 4 条稳定 pipeline。**已满足**：`avatar_spokesperson` / `animated_explainer` / `source_clip_factory` /
  `screen_demo` 四条稳定，外加历史兼容键 `spokesperson_ad`（仍为 `draft`，见 `docs/RELEASE_CHECKLIST.md` §5）。
- 至少 3 类核心 provider 能力具备主备选择。**已满足**（工具目录里多能力具备 ≥2 provider，见门禁第 25 节自适应检查）。
- Web Studio、SDK、Webhook 可用。**已满足**。
- 成本、权限、审计、质量检查、部署文档完整。**已满足**（V6.3 / V6.5 / V8.2 / V8.3 / V8.4 / V8.5 逐项落地）。
- 有正式回归测试集和示例项目。**已满足**：707 项 pytest + 门禁 390/394 项 + `examples/`。

V1.0 不要求：

- 覆盖所有可能的视频 provider。
- 建立公开 marketplace。
- 做复杂多租户商业计费。
- 做完整 NLE 级时间线编辑器。

## 5. 推荐执行顺序

后续从 V1 开始，不再临时追加孤立任务。

推荐顺序：

1. V1.1：Pipeline DSL v1 与 pipeline version。
2. V1.2：Artifact Schema Registry。
3. V1.3：Manifest Action API。
4. V1.4：Run State Machine v1。
5. V1.5：Cost / Usage Ledger。
6. V1.6：Error Taxonomy。
7. V2.1：Tool Contract v2。
8. V2.2：Selector 与 fallback scoring。
9. V2.3：Provider credential manager。
10. V2.4：Provider 扩展与 webhook。
11. V3.1：`avatar_spokesperson` pipeline 正式化。
12. V3.2：`animated_explainer` pipeline。
13. V3.3：`source_clip_factory` pipeline。
14. V3.4：`screen_demo` 或 `localization_dub` pipeline。
15. V4-V8 按业务优先级推进。
16. V1.0：发布收口（版本冻结 `1.0.0` + `CHANGELOG` + 兼容性承诺 + 示例 + 发布回归清单）。

## 6. 近期三步计划

> 本节是 V1 启动时的历史计划，三步均已完成，保留以说明当时的取舍理由。当前进度请看
> `docs/TASK_BOARD.md` 与本节上方的版本状态。

下一阶段先做 V1，不急着扩 provider 或 pipeline。

原因：

- MVP 当前已经能跑，但“动作、状态、artifact、pipeline schema”还没完全产品化。
- 如果先扩工具和 pipeline，会把编排层的不稳定放大。
- 先把内核打稳，后面接 provider、Web、SDK 都会更干净。

近期任务：

| 顺序 | 任务 | 版本归属 | 是否需要 migration | 验收标准 |
|---|---|---|---|---|
| 1 | Pipeline DSL v1 与 pipeline version | V1.1 | 是 | 现有 `spokesperson_ad` 迁移到新 DSL，旧 run 不受影响 |
| 2 | Artifact Schema Registry | V1.2 | 可能 | 关键 artifact 写入时校验 schema，测试覆盖成功和失败 |
| 3 | Manifest Action API | V1.3 | 可能 | Web 修复动作、SDK、worker 均可调用统一 action |

## 7. 任务板使用规则

从本路线图落地后，`TASK_BOARD.md` 的新增任务必须遵循：

- ID 继续使用 T 编号，但任务名称必须带版本前缀，例如 `V1.1 Pipeline DSL v1`。
- 每个任务必须引用本路线图中的版本归属。
- 不允许新增没有归属版本的临时功能任务，除非是 bugfix 或生产阻塞。
- 每个任务完成后必须写明验证命令或验证方式。

## 8. 风险与取舍

主要风险：

- Provider API 变化快，必须通过 adapter 层隔离。
- 视频生成和数字人任务耗时长，worker 与 webhook 需要可靠。
- 多 pipeline 会快速增加 schema 和 UI 复杂度。
- 成本不可控会影响团队内部使用体验。
- 如果过早做大而全 Web UI，会拖慢核心编排能力。

建议取舍：

- 先做编排内核，再扩 pipeline。
- 先做稳定 API，再做完整 Studio UI。
- 先支持内部团队生产流程，再考虑外部 SaaS 化。
- provider 数量按真实业务需求增加，不追求一次性覆盖所有供应商。

## 9. 完整版里程碑判断

可以认为进入“完整版本”的条件：

- 用户从内部平台提交脚本或素材后，可以通过 SDK 创建 run。
- 系统自动完成 preflight、execution plan、proposal、生成、合成、QA、发布包。
- 任何失败都能在 Web Studio 中看到原因和修复动作。
- 管理员能看到 provider key 状态、用量、成本和失败率。
- 同一个系统可稳定支持口播广告、解释视频、素材切片、屏幕演示或本地化配音中的至少 4 类。
- 部署、升级、备份、回滚都有明确文档。
