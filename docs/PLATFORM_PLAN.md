# Script2Video Studio 平台方案（当前架构版）

更新时间：2026-10-12
版本：2.0
状态：与代码库实际实现对齐

## 0. 文档说明

本文档是 Script2Video Studio 的顶层方案文档。

它取代了 2026-09-15 产出的初版方案《Clean-Room Script-to-Video Platform Plan》。初版方案描述的是**开工前的设想**，其中若干技术选择在实施过程中被替换（详见第 16 节）。初版方案曾暂存于参考项目的 docs 目录，未随项目演进同步更新，已不再作为实现依据。

本文档的原则：**只描述已经落地的架构**，未落地的能力明确标注为"计划"，避免再出现文档与代码脱节。

相关文档：

- `docs/TASK_BOARD.md`：全局任务板，进度以它为准。
- `docs/FULL_VERSION_ROADMAP.md`：版本路线图，新增功能任务的唯一来源。
- `docs/CURRENT_CONTEXT.md`：当前上下文速查。
- `docs/DECISIONS.md`：架构决策记录（ADR）。
- `docs/MVP_SCOPE.md`：MVP 冻结范围（历史文档）。
- `docs/pipeline-strategies/`：各 pipeline 的阶段策略。

## 1. Clean-Room 边界

本项目是从零自研实现，参考同类 AI 视频工作流平台的**产品形态与工程思想**，不复制其任何产出物。

允许参考的内容：

- "流水线 + 阶段 + 结构化产物"的抽象思想。
- "工具注册表 + provider adapter + fallback"的产品机制。
- "人工审核门禁 + 版本记录 + 任务恢复"的工作流思想。
- 多 pipeline 产品矩阵的组织方式。

禁止复制的内容：

- 第三方项目的源码、YAML 定义、JSON schema、Markdown 技能文档与 prompt 原文。
- 其 UI 代码、渲染组件实现。
- 其文件命名体系与字段体系的成套照搬。

工程约束：

- 所有模块、字段、表名、接口、状态枚举、提示词均自主设计。
- 不在运行时依赖任何 AGPL 项目。
- 商业上线前需由法务复核第三方模型、素材、音乐、数字人与依赖库许可证。

对应 ADR：`ADR-001: Clean-Room Implementation`。

## 2. 产品形态

产品入口是**脚本放映室**：用户拿到的是一个可审核、可追踪的视频生产工作台，而不是一个黑盒生成按钮。

完整目标形态包含八个页面：项目列表、新建任务、生产计划、分镜、素材、审核、成片、日志。

当前已落地三个页面（见第 12 节）。

主流程（对应 `avatar_spokesperson` pipeline 的十个阶段）：

```text
用户输入脚本
  -> 归一化生产简报（intake）
  -> 生成生产计划            [人工审核]
  -> 生成口播脚本            [人工审核]
  -> 拆分镜                  [人工审核]
  -> 生成数字人 / 配音 / 字幕 / BGM / 辅助素材
  -> 审核素材                [人工审核]
  -> 生成剪辑方案
  -> 合成视频
  -> 成片审核                [人工审核]
  -> 导出发布包
```

## 3. 系统架构

```text
Web 工作台（/workspace、/dashboard、/run-board）
  -> FastAPI 服务（apps/api）
  -> 业务与状态层（db_store / repositories）
  -> 编排层（orchestration_kit、tool_kit）
  -> 工具与 provider 层（tool_catalog、provider_kit）
  -> 后台 worker（apps/worker/background_jobs）
  -> MySQL + Redis + 对象存储（本地或 MinIO）
```

模块职责：

- `apps/api`：HTTP 接口、Web 页面托管、业务编排入口。
- `apps/worker`：常驻后台任务处理，领取 job 租约、轮询 provider、归档资产、触发合成。
- `apps/web`：单页工作台（原生 HTML/JS，无前端框架）。
- `packages/pipeline_kit`：pipeline DSL 加载、校验、版本快照与 `extends` 继承。
- `packages/artifact_kit`：artifact schema registry 与写入校验。
- `packages/tool_kit`：工具合同、注册表、preflight、execution plan、selector。
- `packages/provider_kit`：provider adapter（DashScope 数字人/图像、ElevenLabs 音频等）。
- `packages/orchestration_kit`：run 状态机、错误分类、stage executor。
- `packages/composition_kit` / `canvas_renderer_kit` / `compose_kit`：合成图、Canvas 预览、FFmpeg 合成。
- `packages/quality_gate_kit` / `edit_kit` / `render_profile_kit`：质量门禁、剪辑工具、平台渲染规格。
- `packages/media_analysis_kit` / `script_screening_kit` / `proposal_kit` / `storage_kit` / `agent_runner`：素材分析、脚本解析、提案包、存储抽象、规划器。

## 4. 数据模型

当前共 **36 张应用表**（23 个 Alembic migration，head = `20261109_0023`；Alembic 自用的 `alembic_version`
记账表不计入，仓库里说「全库 37 张表」时指的是含它的口径——迁移往返验证打印的正是后者）：

| 表 | 作用 |
|---|---|
| `tenants` | 租户 / 团队 |
| `users` | 用户 |
| `projects` | 视频项目（V8.3 起带可空 `team_id` 归属；`NULL` = 默认团队） |
| `brand_kits` | 品牌资产（色、字体、logo、禁用词） |
| `style_presets` | 样式预设，项目级默认 |
| `provider_credentials` | provider 密钥配置（环境变量仍兼容）。**V8.4 起 `secret_values` 的语义取决于是否配置加密**：加密关闭时仍是「名字 → 明文」；加密开启后是「名字 → 不透明定位符 `sec_<hex12>`」，明文存 `encrypted_secrets`。一个字段两种形态是有界窗口——加密开启前写入的内联密钥仍可读可用（标 `encrypted=false`），由 `POST /api/provider-credentials/{id}/rotate` 迁入列 |
| `encrypted_secrets` | 托管密钥密文（V8.4）：`tenant_id` / `name` / `algorithm`（`AESGCM-256`）/ `key_version` / `ciphertext`（12 字节随机 nonce 前置 + 密文，整体 base64）/ `secret_metadata`。**AAD 绑定 `tenant_id` + `name` + `key_version`**，所以一行的密文挪到别的租户或别的密钥名下会解密失败而不是张冠李戴；主密钥来自 `SECRET_ENCRYPTION_KEY`（`sha256` 派生），**该列为空即加密关闭**，此表不被使用。**不可枚举**（只按 id 取），`SecretRepository` 不提供 list——没有「列出全部密钥」这个需求，提供它只是增加一个泄露面 |
| `pipeline_versions` | pipeline 版本与内容快照，run 创建时冻结 |
| `production_runs` | 一次生产运行 |
| `stage_runs` | 阶段执行记录（含 `invalidated_at` / `invalidated_by_stage_key` / `invalidation_reason`） |
| `artifacts` | 结构化产物（版本化 JSON，含 schema_key / producer / lineage） |
| `assets` | 媒体文件元数据 |
| `tool_invocations` | 工具调用记录（含 external_task_id） |
| `background_jobs` | 后台任务（含租约、重试、轮询状态） |
| `usage_ledger_entries` | 成本 / 用量明细 |
| `event_logs` | 事件日志 |
| `review_requests` | 审核请求：锁定一个阶段执行 + 一份产物版本快照 + 冻结的门禁策略 |
| `review_decisions` | 审核决策，追加写不改写，当前值由最大 `sequence` 派生 |
| `review_comments` | 审核评论，可锚定到具体产物版本，带 severity |
| `review_action_items` | 由"要求修改"派生出的修改待办，复用既有动作合同 |
| `review_attachments` | 审核请求 ↔ 资产挂载（V6.6）：`(review_request_id, asset_id)` 唯一且幂等，**挂载是引用不是复制**（不入 `assets` 副本，只在 detail 里回带快照）；跨项目资产一律 404 而非 403（不泄露存在性）；第 12 种作用域 `SCOPE_REVIEW_ATTACHMENT` |
| `asset_collections` | 资产库集合（项目库 / 品牌库 / 人物 / 声音），带 `collection_kind`、项目归属、创建人 |
| `asset_collection_items` | 集合条目：引用 `assets` + 用途角色 + 来源（provenance）+ 授权（license）；同一集合内同一 asset 唯一，移除为软删 |
| `teams` | 团队：租户内的协作单元，`tenant_id` + `slug` 唯一，默认团队 `is_default=True` |
| `team_memberships` | 用户 ↔ 团队 + 团队角色（`owner` / `admin` / `member` / `viewer`）；`team_id` + `user_id` 唯一，移除为软删 |
| `project_memberships` | 用户 ↔ 项目 + 项目角色（`owner` / `editor` / `reviewer` / `viewer`），在团队基线之上提权/降权 |
| `audit_logs` | 跨实体审计流水（团队、成员、项目成员、权限拒绝等），带 `actor_id` / `action` / `entity_type` / `entity_id` / `project_id` / `payload` |
| `idempotency_keys` | 幂等写请求的占坑与响应回放：`(tenant_id, actor_id, idempotency_key)` 唯一，带请求指纹、状态、已记录响应与 `expires_at` |
| `api_keys` | 编程访问凭证（V7.2）：`key_prefix` 唯一且可索引（非秘密检索句柄），`key_hash = SHA-256(pepper + 明文)` 只存哈希；`scopes` 为 `NULL` 表示继承所有者全部权限，否则是**收窄**；`actor_id` 故意不设外键（外部身份系统） |
| `webhook_subscriptions` | 出站 webhook 订阅（V7.3）：端点 URL、**明文签名 secret**（平台要产生 HMAC，与只存哈希的 API key 相反）、`event_types`（公开事件词汇表）、`status`（active/paused/disabled，`disabled` 是自动停用写的）、连续失败计数。**V8.4 明确不动它**：V7.3 让 secret 可回显是有意设计（订阅者要用它验签），`encrypted_secrets` 服务的是「平台需要还原明文去用」的材料 |
| `webhook_deliveries` | 事务性 outbox 行（V7.3）：与源事件同事务写入的投递快照；`payload` 是复制而非引用，`event_id`/`run_id` 是普通索引列而非外键（投递是快照，绑到可变的行会让记录不再表达入队时所说的话）；`status`（pending/delivering/succeeded/failed/abandoned）、退避用的 `next_attempt_at`、崩溃恢复用的 `locked_by` 租约 |
| `team_quotas` | 团队周期配额（V8.3）：`team_id` **唯一**（一团队一行，不是流水——改配额的痕在 `audit_logs`，否则「当前生效配额」会变成热路径上的 `ORDER BY created_at DESC LIMIT 1`）；`period`（calendar_month / rolling_30d / lifetime）、`limit_cost` / `limit_runs` / `limit_tokens`（`NULL` = 不限）、`enforcement`（hard 阻断 / soft 只警告）、`status`（active / paused）。**窗口起止不落库**——它是 `period` 的纯函数（`quota.py::quota_window`），存下来就必须有人负责跨月翻页 |
| `external_integrations` | 外部集成注册（V7.4）：谁可以从哪些 origin 把工作台 iframe 走。`tenant_id` + `slug` 唯一；`allowed_origins` 是 JSON 数组，直接成为嵌入页下发的 `frame-ancestors`——**存库而非环境变量**，因为这是每个伙伴各自的数据，不是一个部署配置；`status`（active/disabled，`disabled` 即刻撤销）。**嵌入会话本身不落表**：它是无状态签名令牌，没有需要清理的行 |
| `deployment_settings` | 安装自身的姿态（V8.5）：**单行**（主键 `id="default"`，migration 里 `bulk_insert` seed），也是**全库唯一非租户级表**——它描述的是「这台安装」而不是某个租户，加 `tenant_id` 会让「一行描述整个安装」这个不变式碎掉。字段：`deployment_name` / `environment`（标签，允许与 `APP_ENV` 不同——`staging` vs `dev`）/ `maintenance_mode` + `maintenance_message` / `maintenance_updated_at` / `maintenance_updated_by` / `backup_retention_days`（默认 14）/ `last_backup_at` / `last_backup_id` / `notes`。**维护开关放库不放 `.env`**：需要它的时刻正是不能改文件重启的时刻；`maintenance_mode` 是这张表唯一在**热路径读侧**被读的列（闸门每次写请求读一次，单行主键查、无 join）。**seed 放在 migration 而不是 GET 惰性插入**：只读接口不该有副作用（同 V8.4 拒绝 GET 迁移密钥的理由），且两个副本的首请求竞争是真实可能 |

run 状态枚举：`draft` / `queued` / `running` / `waiting_review` / `paused` / `completed` / `failed` / `cancelled`。
审核请求状态枚举：`open` / `approved` / `changes_requested` / `rejected` / `cancelled` / `superseded`。
成员关系状态：`active` / `removed`（软删，保留审计痕迹）。
幂等键状态：`in_progress` / `completed`（过期行会被下一个同 key 请求无条件接管）。

权限键与角色矩阵
落在应用层常量 `apps/api/app/permissions.py`，**不建表**——它是构建期契约而非运行期数据，建表反而会让
「改一个矩阵」变成一次 migration。

## 5. Pipeline 引擎与 DSL v1

pipeline 定义位于 `pipelines/*.json`，由 `pipeline_kit` 加载并校验。

顶层字段：`dsl_version`、`pipeline_key`、`display_name`、`description`、`version`、`category`、`status`、`default_language`、`entrypoint_stage_key`、`supported_platforms`、`supported_aspect_ratios`、`required_capabilities`、`optional_capabilities`、`artifact_contracts`、`metadata`、`stages`。

stage 字段：`stage_key`、`display_name`、`description`、`order`、`depends_on_stage_keys`、`input_artifacts`、`output_artifacts`、`tool_requirements`、`review_policy`、`dispatch_policy`、`success_criteria`、`required_input_artifacts`、`output_artifact_types`、`required_capabilities`、`optional_capabilities`、`requires_human_review`、`timeout_seconds`。

**`review_policy` 是声明式的门禁数据**，不是代码里的 if 判断：

```json
{
  "required": true,
  "gate_type": "human_review",
  "required_before_next_stage": true,
  "review_focus": ["hook", "product promise", "provider assumptions", "duration fit"],
  "success_criteria": ["The plan is suitable for a vertical spokesperson ad."]
}
```

`dispatch_policy` 控制是否允许自动派发：

```json
{
  "mode": "agent_runner",
  "auto_dispatch_required_tools": false,
  "idempotency_scope": "run_stage"
}
```

当前 5 个 pipeline 定义：

| pipeline_key | 状态 | 说明 |
|---|---|---|
| `avatar_spokesperson` | 正式 | 数字人口播短视频，通过 `extends` 继承稳定 DSL |
| `animated_explainer` | beta | 解释类视频，编排已通，渲染待 V5 渲染能力完善 |
| `source_clip_factory` | beta | 长视频 / 素材剪辑 |
| `screen_demo` | beta | 产品与软件演示 |
| `spokesperson_ad` | 兼容 | 旧 key，仅为历史 run 与测试保留 |

新 pipeline 必须同时产出：pipeline 定义、stage artifact 合同、`docs/pipeline-strategies/` 策略文档、preflight 能力映射、端到端 smoke test。

## 6. Artifact 契约与 Schema Registry

artifact 是阶段之间的结构化合同；媒体文件是 asset，不是 artifact。

当前注册 28 个 schema key：

- 通用阶段产物：`intake_brief.v1`、`production_plan.v1`、`spoken_script.v1`、`shot_list.v1`、`asset_manifest.v1`、`edit_plan.v1`、`render_summary.v1`、`publish_package.v1`、`review_notes.v1`
- 编排与治理：`preflight_report.v1`、`execution_plan.v1`、`stage_execution_manifest.v1`、`proposal_packet.v1`、`quality_report.v1`
- 合成与渲染：`composition_graph.v1`、`canvas_render_report.v1`、`edit_operation_report.v1`
- 素材与理解：`source_asset_manifest.v1`、`source_media_review.v1`、`media_analysis_report.v1`、`video_analysis_brief.v1`、`screening_room_scene_plan.v1`
- 剪辑工厂：`clip_candidate_list.v1`、`clip_export_manifest.v1`
- 屏幕演示：`demo_flow_plan.v1`、`recording_plan.v1`、`screen_capture_manifest.v1`、`interaction_annotation_plan.v1`

规则：

- 关键 artifact 写入时按 registry 校验，校验失败即报错，不写入脏数据。
- 同类型 artifact 重跑后版本递增，`is_current` 指向最新版本。
- 新增字段走向后兼容的新 schema 版本，不原地修改既有 v1 语义。
- artifact 记录 `producer`、`lineage`、`derived_from`，支持追溯与版本对比。

## 7. Tool Registry 与 Provider 适配层

工具目录位于 `tool_catalog/default.json`，当前登记 26 个工具：

- 规划：`rule_based_planner`、`dashscope_planner`
- 数字人与音频：`dashscope_avatar_video`、`dashscope_tts`、`dashscope_asr`、`elevenlabs_tts`、`elevenlabs_music`
- 图像与视频：`dashscope_image`、`kling_video`
- 本地媒体工具：`ffmpeg_compose`、`ffmpeg_media_probe`、`ffmpeg_subtitle_burn`、`ffmpeg_clip_export`、`ffmpeg_edit_tools`、`local_srt_subtitles`、`local_clip_selector`、`local_mvp_avatar_preview`、`local_transcript_placeholder`
- 渲染与质量：`html_canvas_renderer`、`quality_gate_runner`
- 屏幕演示：`playwright_browser_recording`、`local_demo_recording_placeholder`、`local_screen_capture_placeholder`、`local_interaction_highlight_planner`
- 基础设施：`local_object_storage`、`local_asset_upload`

工具合同（Tool Contract v2）声明：`capability`、`provider`、`runtime`、`dependencies`、`input_schema`、`output_schema`、`resource_profile`、`retry_policy`、`cost_model`、`quality_profile`，并统一 `validate` / `estimate` / `submit` / `poll` / `archive` / `cancel` 能力边界。

Provider 适配层职责：

- 归一化可用性检查、成本估算、输入校验、执行、状态轮询、取消、输出归一化。
- 原始响应归档，业务层只消费归一化后的数据。
- **用户已批准的 provider 不能被静默替换**；不可用时返回结构化原因，而不是悄悄降级。
- 同步回调入口 `POST /api/provider-callbacks/{provider}`，callback 与轮询可并存；callback 成功后 worker 不再空转轮询。

Selector 与 fallback：按可用性、成本、延迟、质量、用户偏好和任务适配度评分，execution plan 记录候选项、选择原因和 fallback 原因。

## 8. 编排链路

MVP 之后，系统没有采用"LLM Agent 自主驱动"的路线，而是建立了**确定性编排链路**：

```text
创建 run
  -> preflight 能力预检          (preflight_report)
  -> execution plan 决策         (execution_plan)
  -> proposal packet 提案包      (proposal_packet)
  -> stage execution manifest    (stage_execution_manifest)
  -> 幂等派发就绪的 tool step
  -> worker 领取 job 租约并执行
  -> 归档 asset、推进 stage
  -> 人工审核门禁（见第 10 节）
  -> 合成、质量检查、导出
```

这样做的收益：任一 run 都能解释"当前为何停在这里、下一步可做什么、缺什么输入"。这是 V1 验收标准的核心，也是 Web 工作台"缺失输入修复"能力的基础。

## 9. 合成与渲染

- **composition graph**：渲染中立的中间层，描述 timeline、tracks、layers、captions、transitions、effects、audio mix、render targets。`compose_video` 自动生成并引用它。
- **HTML / Canvas 预览**：基于 composition graph 生成自包含动画预览，供审核前的低成本预览使用。
- **render profiles**：`douyin_vertical`、`xiaohongshu_portrait`、`bilibili_landscape`、`wechat_channels_vertical`、`youtube_shorts_vertical`、`default_vertical`、`default_landscape`。composition graph、Canvas 预览与 FFmpeg 合成共用同一份分辨率、帧率、码率、安全区、字幕位置规格。
- **quality gates**：ffprobe 可读性、分辨率、帧率、音频存在与响度、黑帧、静帧、字幕宽度、中文字体声明、provider provenance 评分。
- **edit tools**：trim、concat、字幕烧录、auto reframe、水印、封面生成、多平台版本导出。

未采用 Remotion 类组件方案。当前渲染路径为 FFmpeg + composition graph + HTML/Canvas，理由见第 16 节。

## 10. 审核门禁机制

### 10.1 已落地的部分

- pipeline DSL 的每个 stage 通过 `review_policy` 声明门禁：是否需要审核、门禁类型、是否必须在进入下一阶段前通过、审核关注点、通过标准。
- run 进入 `waiting_review` 状态，`stage_runs` 记录阶段执行。
- 审核请求（`review_requests`）在阶段进入 `waiting_review` 时自动创建，冻结当轮门禁策略与产物版本快照。
- 审核决策（`review_decisions`）、评论（`review_comments`）、修改待办（`review_action_items`）三类记录落库。
- 审核动作写入 event log。
- `review_notes` 作为 artifact 类型存在，schema 已注册。
- `asset_review` 与 `final_review` 是本项目的两个独立审核阶段。

### 10.2 审核接口面

| 接口 | 作用 |
|---|---|
| `GET /api/runs/{run_id}/review-requests` | 按 run 列出审核请求，可按 `status` / `stage_key` 过滤 |
| `POST /api/runs/{run_id}/review-requests` | 显式开一轮审核（幂等：已有 open 请求时返回同一条） |
| `GET /api/review-requests/{review_request_id}` | 审核详情：决策、评论、修改待办、深链 |
| `POST /api/review-requests/{review_request_id}/decisions` | 提交决策（含 action items） |
| `POST /api/review-requests/{review_request_id}/comments` | 新增评论（可锚定产物版本） |
| `PATCH /api/review-comments/{comment_id}` | 修改评论状态（open / resolved / dismissed） |
| `POST /api/review-requests/{review_request_id}/assign` | 指派审核人与截止时间 |
| `GET /api/projects/{project_id}/review-inbox` | 项目审核收件箱，按逾期优先排序 |
| `POST /api/runs/{run_id}/stages/{stage_key}/review` | 一键审核（兼容旧接口，内部走审核内核） |

拒绝一律返回 `{code, message, ...}` 结构，`code` 取值包括 `stage_not_awaiting_review`、`review_request_closed`、`review_request_superseded`、`stale_artifact_scope`、`invalid_artifact_scope`、`review_round_limit_reached` 等。

### 10.3 设计原则（审核系统必须遵守）

1. 门禁是策略数据，绑定在阶段定义上；代码只做强制校验，不重新判断是否该审。
2. 未通过门禁不得推进到关键阶段。
3. 驳回只重跑受影响的阶段，不重跑全流程。
4. 审核记录必须包含：审核人、时间、意见、对应 artifact 版本。
5. 审核决策日志采用**只追加**语义：决策变更通过新增记录表达，历史记录不可修改；当前状态由最新一条决策派生。
6. 审核结论需要分级（如 critical / suggestion / nitpick），避免返工无限循环。
7. 修改上游产物后，下游受影响产物标记为过期。
8. **一次审批不是永久通行证**：审批只覆盖 `(stage_run, artifact_scope)`；上游变更会把下游已通过的阶段推回失效，并阻断派发。

### 10.4 尚未落地的部分

- **附件**：审核请求 ↔ 资产挂载已建立（V6.6 / `review_attachments`，见 `docs/V6.6_REVIEW_ATTACHMENTS_PLAN.md`）：审核人把证据素材直接挂在关口上，Detail 一次带回决策、评论、待办与附件快照。
- **失效传播的驱动依据**：当前沿 `depends_on_stage_keys` 传递闭包推导；`derived_from` / `lineage` 字段存在但写入不完整，尚不能作为传播依据。
- **`success_criteria` 的显示层本地化**：它是 DSL 自由文本句子（非 key），目前按原样展示。

## 11. 成本与用量

- 每个 tool invocation 与 job 记录估算成本、实际成本、用量指标。
- run 级成本汇总：`GET /api/runs/{run_id}/usage`；明细：`GET /api/runs/{run_id}/usage/ledger`。
- 支持预算门禁，超预算时可阻断。
- 成本策略三档：`observe`（只记录）、`warn`（告警但放行）、`block`（直接阻断）。
- **预算预留（T76 已实现）**：execution plan 汇总所选工具的估算成本为 `cost_reservation`（逐 stage 累加，含 `budget_status`），`start` 时按整轮预留校验；超预算抛结构化 `cost_reservation_exceeds_budget`。未声明预算不再静默放行，而是标记 `budget_status=not_configured` 并留事件。
- **首次付费确认（T76 已实现）**：`estimated_cost > 0` 且 `runtime=api` 的工具在某个 run 首次被调用时必须先确认；未确认抛结构化 `paid_provider_confirmation_required`，确认粒度 run × provider，通过 `POST /api/runs/{run_id}/provider-consents` 写入。

待补强：预留与实际成本的**对账（reconcile）**——目前预留只在 `start` 校验一次，尚未随执行推进动态释放/追补；以及项目级 / 团队级的预算与付费确认（V8.3 Quota / Billing）。

## 12. Web Studio 页面

| 路由 | 用途 |
|---|---|
| `/` | 重定向到 `/workspace` |
| `/workspace` | 单 run 工作台：创建样片、选择质量模式、预检、生成执行方案、启动 run、查看 manifest、修复缺失输入、推进 job、预览资产、审核阶段；右栏「审核关口」展示门禁清单与下一道关口 |
| `/dashboard` | 项目仪表盘：项目列表、run 列表、状态筛选、搜索、复制 run、失败任务、审核收件箱（轮次 / 责任人 / 逾期 / 只看我的 / 行内快速决策） |
| `/run-board` | 单 run 看板：阶段泳道、审核区块（策略快照、四类决策、评论、修改待办、只追加决策历史、失效标记）、artifact 时间线、decision / cost / job / event log、输入缺口与修复动作、成片预览、版本分组与下载 |
| `/asset-library` | 项目资产库：集合列表与新建、条目网格（来源与授权标签）、从 run 沉淀资产、把库内条目复用到目标 run |
| `/team` | 团队与权限：当前身份与生效权限、团队列表与新建、团队成员（角色 + 每成员权限标签）、项目成员、审计流水、权限矩阵、访问方式说明 |

前端为原生 HTML/JS 单页，无构建步骤。页面显示层已统一为中文，后端与 API 保留稳定英文 key。
审核深链：`/run-board?review_request_id=<id>`、`/workspace?review_request_id=<id>`。

### 12.1 资产库接口面（V6.4）

| 方法 路径 | 用途 |
|---|---|
| `GET /api/projects/{project_id}/asset-collections` | 列出项目集合（可按 `kind` 过滤） |
| `POST /api/projects/{project_id}/asset-collections` | 新建集合（`collection_kind` = `project_asset` / `brand_asset` / `person` / `voice`） |
| `GET /api/projects/{project_id}/asset-library` | 项目资产库聚合（集合 + 条目 + `by_kind` 统计） |
| `GET /api/asset-collections/{collection_id}` | 集合详情（`with_items` 控制是否带条目） |
| `PATCH /api/asset-collections/{collection_id}` | 改名 / 改描述 |
| `GET /api/asset-collections/{collection_id}/items` | 列出条目（`include_removed` / `role` 过滤） |
| `POST /api/asset-collections/{collection_id}/items` | 幂等加入条目（asset + role + provenance + license） |
| `POST /api/asset-collections/{collection_id}/import-from-run` | 从 run 批量沉淀资产（按 checksum 去重） |
| `PATCH /api/asset-library/items/{item_id}` | 更新条目角色 / 来源 / 授权 |
| `DELETE /api/asset-library/items/{item_id}` | 软移除条目（`status=removed`） |
| `POST /api/runs/{run_id}/asset-library/reuse` | 把库内条目复用进目标 run（复制为新资产并记 `reused_from`） |

设计要点：复用**不改动、不移动**库内原资产，而是在目标 run 内新建一条 `assets` 行并写入
`asset_metadata.reused_from`（collection / item / source asset / source run），使「这条成片用的是哪个库版本」
可追溯；来源（provenance）与授权（license）随复用一并带过去，避免二创后丢失授权信息。

### 12.2 团队与权限接口面（V6.5）

| 方法 路径 | 用途 |
|---|---|
| `GET /api/me` | 当前请求身份档案：`actor_id` / `source` / `team_id` / `team_role` / 生效权限清单 |
| `GET /api/permissions` | 静态权限矩阵：23 个权限键 + 4 个团队角色 + 4 个项目角色的授予关系 |
| `GET /api/audit-logs` | 审计流水查询（可按 `actor_id` / `action` / `entity_type` / `project_id` / `entity_id` 过滤，`limit` 上限 500） |
| `GET /api/teams` | 租户内团队列表（`include_archived` 控制是否含归档） |
| `POST /api/teams` | 新建团队（显式 `slug` 冲突返回 409；未给 `slug` 时自动派生并去重） |
| `GET /api/teams/{team_id}` | 团队详情（未知团队 404） |
| `PATCH /api/teams/{team_id}` | 改名 / 描述 / 状态 |
| `GET /api/teams/{team_id}/members` | 团队成员列表（`include_removed` 控制是否含已移除） |
| `POST /api/teams/{team_id}/members` | 加入团队（幂等：重复加入就地更新同一行；非法角色 400） |
| `PATCH /api/team-memberships/{membership_id}` | 改团队角色 / 状态 / 备注 |
| `DELETE /api/team-memberships/{membership_id}` | 软移除团队成员 |
| `GET /api/projects/{project_id}/members` | 项目成员列表 |
| `POST /api/projects/{project_id}/members` | 加入项目（角色为 `owner` / `editor` / `reviewer` / `viewer`） |
| `PATCH /api/project-memberships/{membership_id}` | 改项目角色 / 状态 / 备注 |
| `DELETE /api/project-memberships/{membership_id}` | 软移除项目成员 |
| `GET /api/team-quotas` | `quota:read`（租户）：全租户配额总览，未配置的团队也在列（不列会让「没配额」和「不是团队」长得一样） |
| `GET /api/teams/{team_id}/quota` | `quota:read`（租户）：单团队配额 + 当前窗口用量（含 `cost_remaining` / `runs_remaining` / `tokens_remaining`） |
| `PUT /api/teams/{team_id}/quota` | `quota:manage`（租户）：upsert——一个团队只有一行，「改」和「建」是同一件事，所以是 `PUT` 不是 `POST` |
| `DELETE /api/teams/{team_id}/quota` | `quota:manage`（租户）：回到不限额；返回**删除后**的视图；本来就没有配额 → 404（「没得删」≠「删成功」） |
| `POST /api/runs/{run_id}/quota-override` | `quota:manage`（run）：批准/撤销单个 run 的一次性豁免；写进 `run.input_payload.quota_override`，可撤销 |
| `PATCH /api/projects/{project_id}/team` | `quota:manage`（项目）：移动项目归属；`{"team_id": null}` = 回到默认团队。**归属就是配额**，所以它和「定上限」同权限 |

设计要点：**生效权限 = 团队角色权限 ∪ 项目角色权限**（并集取宽）。团队角色是租户级基线，项目角色只在
单个项目上做加宽，**永远不会撤销**团队基线；反过来 `team:manage` 只由团队角色授予，任一项目角色都拿不到，
因此「把自己加进某个项目」不是提升租户管理权限的后门。**租户级权限共 6 个**——`team:manage` /
`apikey:manage`（V7.2）/ `webhook:manage`（V7.3）/ `integration:manage`（V7.4）/ `quota:manage`（V8.3）/
`provider:manage`（V8.4）
——它们永不由项目角色授予，因为它们的授予范围都是整个租户（能把工作台嵌进任何一个 origin 的人，管的
不止一个项目；能改花钱上限的人，改的也不止一个项目；**能写 provider 密钥的人，等于能决定整个租户的付费调用走哪个账号**）。
V8.4 的 `provider:manage` 还有一处刻意的不对称：**没有 `provider:read` 孪生键**——读接口带
`secret_preview`（前 3 后 4 位），「谁能看」与「谁能改」在这里是同一件事，拆成两个键只会制造
一个「看得见密钥指纹但改不动」的角色。反过来 `quota:read` 以 `:read` 结尾自动进
`READ_ONLY_PERMISSIONS`，看额度是只读数据、与 `member:read` 同类。
作用域解析会把路径参数里的具体 id（run / asset / job / 集合条目 / 评论 / 工具调用 / 品牌套件 /
样式预设）回溯到所属 project，再叠加项目角色判定。

执行层是**一张策略表 + 一个应用级依赖**：`apps/api/app/permission_policy.py` 的 `POLICY`（132 条）由
`main.py` 以 `include_router(..., dependencies=[Depends(enforce_api_permissions)])` 挂载，因此新增端点
无法通过忘记声明依赖来绕过；未受控端点必须显式登记进同文件的 `OPEN_ENDPOINTS`（16 条，逐条写明理由），
并有测试断言两表**精确划分**全部文档化端点（当前 125 path / 157 operation；见 §12.3），杜绝新端点静默逃逸。`/metrics`（V8.2 Prometheus 抓取端点）刻意不在其中——它是 app 级路由、`include_in_schema=False`，消费者是抓取器而非 API 调用方。
V7.2 的三个凭证端点（`GET` / `POST /api/api-keys`、`POST /api/api-keys/{api_key_id}/revoke`）落在
`POLICY` 里，要求租户级的 `apikey:manage`；V7.4 的六个集成端点（含签发嵌入会话）落在 `POLICY` 里，
要求租户级的 `integration:manage`，只有开放的自举端点 `GET /api/embed/{integration_id}/session`
登记进 `OPEN_ENDPOINTS`——它靠令牌本身鉴权，与 `/api/me` 同类。
**V8.4 把 V6.5 里显式开放的 5 条 `provider-credentials*` 端点从 `OPEN_ENDPOINTS` 移进了 `POLICY`**
（`provider:manage` + `SCOPE_TENANT`）；第 6 条 `POST /api/provider-callbacks/{provider}` **留在开放**，
因为它的调用方是上游供应商而不是平台身份，没有任何 actor 可判权限——硬塞进 `POLICY` 等于发明一个
「谁也拿不到」的权限；它的真实性由 HMAC 签名校验承担（见 §12.7）。
拒绝走 `PermissionDenied`，
payload 与 `RunGateError` 同构（`{"detail": {code, message, ...}}`），且拒绝本身先落 `permission.denied`
审计再抛出，不会随请求中止而回滚。

**租户范围守卫（V1.1 T97）**在权限守卫**之前**运行（挂载顺序：key 身份 → 登录会话 → embed →
`enforce_tenant_scope` → 权限 → 维护 → 幂等）：路径里的资源 id 必须属于调用方租户，否则以处理器
自己的 404 文案拒绝——排在权限之前是因为一次 403 会确认「行存在」，而 404 让「跨租户」与「不存在」
在 wire 上不可区分。resolver 家族 `tenant_id_for_*` 覆盖 18 类 id 形路径参数；嵌入会话按集成租户
比较（project/run 钉定仍在权限守卫里做更细粒度的承诺检查）；`integration_id` / `api_key_id`
在处理器层已按租户收敛、`/assets/{id}/content` 的权威是 V8.4 签名链接，均不在守卫表内。
写路径同样收敛：项目子资源创建继承父项目租户，项目创建跟随调用方租户，`GET /api/projects` 与
provider-credentials 列表按租户过滤。

身份来自受信头 `X-Actor-Id` / `X-Actor-Name`；`ACTOR_TRUST_MODE=open`（默认）直接信任，`secret` 模式要求
`X-Actor-Secret` 匹配 `ACTOR_HEADER_SECRET`（该模式下未配置密钥则**不信任任何头**）。无身份时回退
`DEFAULT_ACTOR_ID`（默认 `system`），该身份首次使用时按 `owner` 自动登记到默认团队，使部署门禁脚本、
MVP 样片链路与全部既有调用方零改动继续工作；而**带头的未登记身份不会被自动入座**，因此拿不到任何权限
（含读）——这是「默认身份必须向后兼容」与「新身份不得默认可读」之间的取舍点。

### 12.3 HTTP API v1 契约接口面（V7.1）

| 方法 路径 | 用途 |
|---|---|
| `GET /api/error-codes` | 错误码目录：每个码的 `status_code` / `category` / `retryable` / `description`，SDK 据此分支 |
| `GET /api/idempotency-keys` | 已记录的幂等写请求（`Page` 信封，可按 `state` / `actor_id` 过滤），供 SDK 排查自己重试过什么 |

V7.1 不新增业务端点，改的是**所有端点的对外行为**：

1. **一种失败形状。** 全部失败统一为 `{"detail": {code, message, ...details}}`，`code` 取自
   `apps/api/app/errors.py` 的稳定错误码（现为 **25 个**，V7.2 补入 `api_key_invalid`、V7.4 补入
   `embed_session_invalid`、V8.3 补入 `team_quota_exceeded`、V8.4 补入
   `provider_callback_invalid` 与 `insecure_secret_configuration`）。此前只有三道风控闸门
   是结构化的，404 / 405 / 422 / 500 还是 FastAPI 默认输出（`{"detail": "字符串"}` 或
   `{"detail": [{...}]}`），客户端无法用一个字段分支。
   状态码与错误码是**多对一**：一个 400 既可能是 `bad_request`，也可能是路由有意抛出的
   `cost_budget_exceeded`，因此不变式是「状态的回退码必须与该状态自洽」。
2. **可选幂等键。** 任何写请求可带 `Idempotency-Key`。首次执行并记录响应；同 key 同 body 重放该响应并
   附 `Idempotency-Replayed: true`；同 key 不同 body → `409 idempotency_conflict`（客户端 bug，不静默
   别名到首个 payload）；**不带该头则行为与之前完全一致**。过期行会被下一个同 key 请求**无条件接管**，
   避免 key 被历史用途永久毒化。只有 2xx + JSON 会被记录，其余释放占坑以便改请求后重试。
3. **分页约定。** 新增端点返回 `Page[T]`（`items` / `total` / `limit` / `offset`）；既有裸数组端点
   （`/api/audit-logs`）**保持 body 不变**，只接受 `limit` / `offset` 并回填 `X-Total-Count`，
   因此 5 个已上线页面与门禁脚本零改动。
4. **spec 即契约。** `/openapi.json` 里每个 operation 的 400/403/404/409/422/500 都声明为
   `ErrorEnvelope`（否则会谎报 422 为 `HTTPValidationError`），7 个 HTML 页面（含 V7.4 的两个
   嵌入壳）`include_in_schema=False`；
   `POLICY` ∪ `OPEN_ENDPOINTS` 与文档化 operation **精确相等**，由 `tests/test_openapi_contract.py` 守护。
   URL 前缀仍是 `/api`——重命名为 `/api/v1` 会打挂全部既有调用方，版本改用 `info.version` 表达。

### 12.4 编程凭证与 SDK 接口面（V7.2）

| 方法 路径 | 权限 | 用途 |
|---|---|---|
| `POST /api/api-keys` | `apikey:manage`（租户） | 签发凭证；**明文只在响应里出现一次**，`scopes` 只能收窄所有者权限 |
| `GET /api/api-keys` | `apikey:manage`（租户） | 列举（`Page` 信封），永不含明文，只剩 `key_prefix` |
| `POST /api/api-keys/{api_key_id}/revoke` | `apikey:manage`（租户） | 吊销，**幂等**且只写一条审计；未知 id → 404 |

V7.2 的关键点是把凭证**并入既有的身份入口**而不是另开一条鉴权路径：应用级依赖
`enforce_api_key_identity` 把 `Authorization: Bearer s2v_...` 解析成 `ActorContext`，权限守卫
（`enforce_api_permissions`）与幂等守卫（`enforce_idempotency`）随后照常工作，于是 scope 收窄只有
一个落点（`DatabaseStore.require_permission` 的 `key_scopes`）。三个取舍值得记住：

1. **Bearer 优先于 `X-Actor-Id`**——显式凭证比网关隐式身份更可信。
2. **没有 `Authorization` 头的请求完全不受影响**——与 `Idempotency-Key` 同样的「可选能力」策略，
   是既有页面 / MVP 链路 / 部署门禁零改动的前提。
3. **非 Bearer 的 `Authorization` 不算鉴权尝试**（Basic 等照常回退），否则引入这个头会意外打挂
   既有调用方。

客户端侧是 `sdk/python`（导入名 `s2v`，发行版 `s2v-client`，**零运行时依赖**）与 `sdk/typescript`
（npm `s2v-client`，ESM + `NodeNext` + `strict`）。两者都以**可插拔传输层**为核心（`Transport` 协议，
不对 4xx/5xx 抛异常，默认 urllib / `fetch`），从而让调用方接管网络、让测试直接打进程内 ASGI 应用
（`tests/harness.py` 的 `AsgiTransport`）。TS 版响应体是 `Uint8Array` 而非 `string`，因为同一个客户端
要下载 MP4。

### 12.5 出站 Webhook 接口面（V7.3）

平台把六类状态变化推到租户端点（详见 `docs/V7.3_WEBHOOK_PLAN.md`）：

- **事件目录**：`GET /api/webhook-events` 开放、静态、无租户数据，订阅前可发现事件契约。
- **订阅管理**：`GET|POST /api/webhooks`、`GET|PATCH|DELETE /api/webhooks/{id}`、
  `POST /api/webhooks/{id}/rotate-secret`、`GET /api/webhooks/{id}/deliveries`、
  `POST /api/webhook-deliveries/{id}/requeue`，全部 `webhook:manage`（第 17 个权限，
  租户级）。跨租户 id 一律读作 404，不泄露存在性。
- **投递语义**：事务性 outbox（与源事件同事务入队）+ worker 带租约认领 + 指数退避
  （30s 起、1h 封顶、6 次耗尽转 `abandoned`）+ 连续失败自动停用（阈值 20，人工重激活清零）。
- **签名**：`X-S2V-Signature: t=<unix>,v1=<hex>` = HMAC-SHA256 over `"{t}." + body`，
  时间戳在签名串内，接收方一侧校验同时获得防篡改与防重放；两个 SDK 都内置校验函数。
- **worker**：`run_once` 在任务处理后排空 outbox；`webhook_dispatch_enabled=false`
  可整体关闭。T82 同时改了 api 与 worker，部署要重建两个镜像。

### 12.6 可嵌入工作台接口面（V7.4）

外部平台把工作台 iframe 进自己的页面，给**没有本平台账号**的终端用户看一个钉定的 run
（详见 `docs/V7.4_EMBED_WORKSPACE_PLAN.md`）。集成是注册，会话是令牌：

| 方法 路径 | 权限 | 用途 |
|---|---|---|
| `POST /api/external-integrations` | `integration:manage`（租户） | 注册集成；`allowed_origins` 只接受 http/https origin，`*` → 400 `bad_request`；同租户同名 slug → 409 `conflict` |
| `GET /api/external-integrations` | `integration:manage`（租户） | 列举（`Page` 信封，按 `created_at, id` 排序） |
| `GET /api/external-integrations/{id}` | `integration:manage`（租户） | 读取单个集成 |
| `PATCH /api/external-integrations/{id}` | `integration:manage`（租户） | 改名 / 改 origin / `status=active\|disabled` |
| `DELETE /api/external-integrations/{id}` | `integration:manage`（租户） | 删除；已签发的链接随之 401 |
| `POST /api/external-integrations/{id}/embed-sessions` | `integration:manage`（租户） | 签发嵌入链接：返回 `meta.embed_url` + `context` / `scopes` / `expires_at` |
| `GET /api/embed/{integration_id}/session` | **开放**（令牌即凭证） | 页面自举：读 `X-Embed-Token`，回显集成、`source == "embed_session"` 与 `remaining_seconds` |

关键取舍：

1. **令牌无状态，不落库。** `s2v_embed_<base64url(payload_json)>.<hex hmac_sha256>`
   （前缀比 API key 的 `s2v_` 长，解析无歧义）。代价是没有逐令牌吊销列表——撤销只能停用集成，
   或轮换 `EMBED_TOKEN_SECRET`（连带失效全部在途链接）。
2. **平台签发而非伙伴自签。** 伙伴用 API key 换现成链接，永不持有签名密钥，否则每换一个伙伴
   就要多轮换一次秘密。签名不是加密（payload 可读），失败一律 401 `embed_session_invalid` 且不区分原因。
3. **集成 id 在路径、令牌在 fragment。** 首个文档请求不带任何凭据，服务端必须靠路径 id 算
   `frame-ancestors`；令牌放 `#token=` 则不发给服务端、不进日志与 Referer，页面取到即
   `history.replaceState` 抹除。页面固定 `Cache-Control: no-store`。
4. **令牌是第二授权来源，不是席位。** 嵌入会话在租户里没有 `team_memberships` 行，因此
   `require_permission` 的 `embed_session` 分支**替换**（而非收窄）席位链；上下文钉定
   （403 `embed_context`：兄弟 run / 其他项目 / 租户级端点）先于 scope 判定
   （403 `embed_scope`）。`run:review` 是唯一可选写权限，`run:write` / `run:start` 越界即 400。
5. **专用请求头 `X-Embed-Token`**，不复用 `Authorization: Bearer`——后者归 V7.2 API key 层，
   任何它不认识的 Bearer 会得 401，混用只会让两类凭证互相污染。身份优先级
   API key > `X-Embed-Token` > `X-Actor-Id`（受信头只在 `resolve_actor` 里作最后回退，
   所以**不夺权**，否则每个钉定都只是「建议」）。

### 12.7 provider 密钥与安全加固接口面（V8.4）

收 V6.5 显式开放的那 6 条 provider 端点的口，并把「密钥落盘 / 上传入口 / 资产链接 / 敏感操作」四处
补上最小完备的防线（详见 `docs/V8.4_SECURITY_PLAN.md`）：

| 方法 路径 | 权限 | 用途 |
|---|---|---|
| `GET /api/provider-credentials` | `provider:manage`（租户） | 列出凭证；带 `secret_preview`（前 3 后 4 位）与 `secret_source`，**永不回传完整密钥或定位符**；**V6.5 起开放，V8.4 起受控** |
| `GET /api/provider-credentials/{id}` | `provider:manage`（租户） | 单个凭证（未知 404） |
| `POST /api/provider-credentials` | `provider:manage`（租户） | 新建；加密开启时 `secret_values` 落 `sec_<hex>` 定位符 |
| `PATCH /api/provider-credentials/{id}` | `provider:manage`（租户） | 改配置 / 轮换密钥材料 |
| `POST /api/provider-credentials/{id}/check` | `provider:manage`（租户） | 健康检查；**用解密后的真实值**去试探，并写 `last_check_*`（所以它不是读） |
| `POST /api/provider-credentials/{id}/rotate` | `provider:manage`（租户） | **V8.4 新增**：把内联遗留密钥迁入密文表 + 用当前密钥版本重加密已有引用。**幂等**；返回 `sealed` / `resealed` / `locked` 计数。一个端点同时覆盖「升级迁移」与「主密钥轮换」两条 runbook |
| `POST /api/provider-callbacks/{provider}` | **开放** + 签名校验 | 上游供应商回调。进不了 `POLICY`（没有 actor），配置 `PROVIDER_CALLBACK_SECRET` 后强制 `X-S2V-Signature` |

关键取舍：

1. **加密是 opt-in。** `SECRET_ENCRYPTION_KEY` 为空 = 关闭，行为与升级前完全一致。理由同 V8.2 的
   `LOG_FORMAT=json`：打开加密把「谁能还原明文」从进程内问题变成**运维**问题（环境变量会不会随备份
   一起丢、随镜像一起进仓库），设为默认等于给每个现有部署加一个「丢了就没法调用 provider」的单点。
2. **AAD 绑定身份。** `AES-256-GCM` 的附加数据是 `tenant_id` + `name` + `key_version`，所以一行的
   密文挪到别的租户、别的密钥名下会**解密失败**，而不是「解出来了但张冠李戴」。
3. **一个字段两种形态是有界窗口。** 加密开启后遇到非托管定位符的值 = V8.4 之前的遗留内联密钥，
   仍可读可用（标 `encrypted=false`），由 `rotate` 迁入。不做迁移期 backfill（migration 不该依赖
   运行时秘密），也不做 GET 惰性迁移（只读接口不该有副作用）。`ProviderSecretSource` 五态里
   `locked`（当前密钥解不开）优先级最高——此时 preview 为空且 `environment_overrides` **省略该值**，
   绝不把 `sec_...` 当凭据传给 provider，也不拿错误密钥静默降级。
4. **回调签名复用 webhook 格式。** `X-S2V-Signature: t=<unix>,v1=<hex>`（HMAC-SHA256 over
   `"{t}.{body}"`，时间戳在签名串里 → 同时防篡改与防重放），缺失/不符/超容差一律 401
   `provider_callback_invalid`（不区分原因，不做 oracle）。未配置时维持接受——回调的真实性来自
   **上游怎么配**，平台不能替它做主。
5. **上传加固不是杀毒。** 「文件类型扫描」的可行含义是**魔数嗅探 + 类型白/黑名单**（拒绝「声明是 mp4、
   开头是 ELF/MZ/脚本 shebang」这类伪装），不是 ClamAV 集成——那是独立进程、独立特征库更新通道与
   独立运维面。三道关卡在**解码之前**先设编码长度闸，避免「先吃下整个字符串再发现超限」。
6. **资产签名是叠加不是替代。** `/assets/{id}/content` 同时要 `asset:read` 与有效签名，所以泄露一条
   链接不等于泄露资产。V8.4 只加 TTL 夹取与「生产环境仍用开发默认密钥则拒绝签发」（503
   `insecure_secret_configuration`）——**只拦签发不拦验证**，已发出的链接在迁移完成前仍可用；
   让「生产用开发密钥」静默成功比 503 更糟，因为前者不会有人发现。
7. **审计只记形状不记值。** 凭证创建 / 更新 / 检查 / 轮换落 `audit_logs`，`payload` 只记字段名与数量
   （`secret_names`），**绝不记值**。

### 12.8 部署姿态接口面（V8.5）

安装自身的姿态与运行时可写开关。**这四个端点的主体是「这台安装」而不是某个租户的数据**，
是全库唯一一处（详见 `docs/DEPLOYMENT.md`）：

| 方法 路径 | 权限 | 用途 |
|---|---|---|
| `GET /api/deployment` | `deployment:read` | 姿态：`app_version` / `environment` / `migration`（head、current、pending、`up_to_date`）/ `capabilities`（五个布尔）/ `settings` / `table_count`。**全部算出来不缓存**——一个「我们是新的」缓存比没有缓存更糟 |
| `PATCH /api/deployment` | `deployment:manage`（租户） | 改标签 / 环境标签 / 备份保留天数 / 备注。**维护模式中仍可达**——关掉它的开关本身就是这条路径的写，拒绝它等于把开关做成一扇单向门 |
| `POST /api/deployment/maintenance` | `deployment:manage`（租户） | 维护开关。返回**结果行**而非请求回显，所以调用方能看到生效消息（空消息会补默认值）与最后改动者 |
| `POST /api/deployment/backups` | `deployment:manage`（租户） | 记录「备份已做」这个**事实**（归档由 `scripts/backup.sh` 在进程外产生，API 没法 dump 一个自己正在服务的库）。把「上次备份是什么时候」变成一次读，而那是决定「一次坏 migration 是否可恢复」的判据 |

关键取舍：

1. **维护开关放数据库不放 `.env`。** 需要它的时刻正是**不能改文件重启**的时刻（migration 跑了一半、
   restore 正在进行、provider 灰度炸了）。单行表 `deployment_settings` 是全库**唯一非租户级表**。
2. **闸门位置是载重设计。** `… → enforce_api_permissions → enforce_maintenance_mode →
   enforce_idempotency`：放权限**之后**（无权者该听到「你缺 project:write」而不是「我们在维护」，
   否则维护窗口会把每一个权限 bug 都伪装成临时故障），放幂等**之前**（什么都没改的拒绝不该占住
   幂等键，否则窗口结束后的重试会**重放那个 503**）。
3. **读永不拒绝。** 维护不是关灯：页面、探针、`/api/deployment` 继续答，只停**变更**。这是它可以
   安全地开一整个 migration 时长的原因。
4. **两条写豁免。** `/api/deployment*`（否则开关单向）与 `/api/provider-callbacks*`（回调携带
   **已付费**的 provider 结果，拒绝不是推迟工作而是**丢弃**工作，且平台自己生成不出来；该端点
   V8.4 已有 HMAC 鉴权，豁免不新开任何门）。匹配按**路径段**而不是裸字符前缀——
   `startswith("/api/deployment")` 会让 `/api/deployment-evil` 继承豁免。
5. **读键刻意宽授，与 `provider:manage` 正好相反。** 判据是**读响应里有没有值**：posture 只有版本串、
   revision id、布尔与时间戳，能查「这台上没上到最新」是普通运维自助，所以 `deployment:read` 进
   `READ_ONLY_PERMISSIONS`；而 `provider:manage` 的读响应带 `secret_preview`，看与改是同一件事。
6. **`maintenance_mode` 是 `retryable = true` 的 503。** 维护窗口会结束——把它标成永久失败只会让
   客户端需要人工重启。

## 13. 部署与运行

本机无 Docker，正式部署在 Linux 服务器（有 Docker 环境）：

```bash
cd /data/script2video-studio
docker compose up -d --build api worker
docker compose exec api alembic upgrade head
curl http://localhost:18080/api/health
```

Compose 服务：`mysql`、`redis`、`minio`、`api`、`worker`。对外端口 `18080`（容器内 `8080`），MySQL 主机映射 `3307:3306`。

**V8.5 起生产部署有专用栈**：`docker-compose.prod.yml` 把 migration 收敛为一次性 `migrate` 服务
（api 与四个 worker 全部 `depends_on: service_completed_successfully`，没有副本会跑到未迁移的 schema 上）、
基础设施一律不发布端口、worker 按角色四分（provider / analysis / media / render，各带 `WORKER_ROLE`
与 `WORKER_METRICS_PORT`）、资源 `limits` 与 json-file 日志轮转。K8s 侧另有 `infra/deploy/k8s/`
7 个清单（**不做 Helm**——只有一种部署形态，chart 只会引入 release-name 前缀 / values 文件 / 渲染步骤
三层间接；Job 的注释写明**六步升级顺序是判断而不是 YAML 能表达的**）。完整步骤、回滚、密钥轮换
爆炸半径与**明确不覆盖的范围**（无 HA DB / 无跨区 DR / 无 KMS / 无 worker 自动故障转移）见
`docs/DEPLOYMENT.md`（12 节）。

运维脚本（V8.5）：

```bash
python scripts/deployment_preflight.py          # 直接连库读配置，退出码 0/1/2；--json 可机读
python scripts/deployment_preflight.py --database-url "$DATABASE_URL"
scripts/backup.sh --dir /backups                # --single-transaction dump + MANIFEST + .sha256
scripts/backup.sh --dir /backups --prune --dry-run   # retention 默认只报告；--prune 才真删
scripts/restore.sh --archive /backups/x.tar.gz --yes # 默认还要 maintenance 互锁
```

`backup.sh` **不备份 `.env`**——它持有解密 provider 密钥的 `SECRET_ENCRYPTION_KEY`，把钥匙和保险箱
放同一份归档不是备份策略；retention **默认只报告不删除**，因为一次 retention 笔误不该毁掉数据。
`restore.sh` 数据库侧是**破坏性**的（dump 含 drop/create），storage 侧是**叠加式**的（新文件保留）；
它默认先从 `GET /api/deployment` 确认维护模式已开（`--skip-maintenance-check` 只给停机态或临时环境），
并在结束后提示人工收口四步。

维护模式（V8.5）：`POST /api/deployment/maintenance {"enabled": true}` 只停**变更**不停**读取**——
页面、`/api/health`、`/api/deployment` 一律继续答；普通写请求得 503 `maintenance_mode`
（`retryable = true`）。整库恢复或 schema 变更前打开它，是让「migration 期间有人提交」这件事从
「数据不一致」变成「一个可重试的 503」的全部代价。

部署后用一条命令收口验证：

```bash
docker compose exec -T api python scripts/verify_review_loop.py
```

`scripts/verify_review_loop.py` 只用标准库、只走 HTTP，因此同时覆盖路由层与内核层。
它建 run 推到第一道关口，然后逐项核对：关口策略快照、审批粒度锁定、指派与逾期、评论严重度、
带修改通过生成待办、重跑触发下游失效与请求作废、board / 收件箱字段、错误码映射、质量意图降级、
成本预留与首次付费确认、资产库沉淀与复用、团队与权限（身份解析、角色矩阵、越权 403、审计流水）、
API v1 契约（错误信封、错误码目录、幂等重放/冲突、分页）、API key（一次性明文、Bearer 身份、
scope 收窄与越界理由、吊销幂等）、webhook（事件目录、订阅校验、outbox 快照、手动重投、自动停用阈值与人工重激活清零）、可嵌入工作台（集成注册与契约回显、`*` origin 400 与同名 slug 409、mint 的四种失败、令牌自举、钉定 run 可读而兄弟 run 与租户级端点 403 `embed_context`、越权写 403 `embed_scope`、页面 CSP 与 `no-store`、停用后旧链接失效并可恢复）、审核附件（挂载幂等、快照回显、跨项目 404、移除与事件）、worker scaling（provider 队列归属、优先级与租约/退避字段回显、手动重试消耗 attempt、非死信重放 409、未知 job 404）、observability（/metrics 免鉴权可抓且不进 OpenAPI、请求标识生成与回显、面板四视图、未入座 403）、配额与计费（未配置静默、hard 越界 400 与 soft 放行、一次性豁免、归属端点 `quota:manage`、`ProjectCreate` 不收 `team_id`）、安全加固（`provider-credentials*` 已受控、`provider:manage` 租户级且无 read 孪生、`rotate` 迁移与重加密、回调签名两分支、上传 413/415、资产链接 TTL 夹取与生产默认密钥 503）、部署姿态（姿态形状与版本、迁移自检、能力只报布尔、未入座读 403 而 member 读 200 写 403、维护中拒写 503 且读与探针仍答、维护中逃生舱可写、关闭后写立即恢复、备份记账与审计只按跃迁落条）、发布契约冻结（`app_version` 与 `info.version` 一致、迁移到 head、pipeline 目录 ≥4 且字段齐全、错误码目录 28 条、webhook 事件目录 6 条、provider 能力主备自适应、权限矩阵 23 键 / 租户级 7 个）、七个 Web 页面标记。共 **408 项检查**（默认配置；同时配置 `SECRET_ENCRYPTION_KEY` 与 `PROVIDER_CALLBACK_SECRET` 时为 **412 项**）。失败会打印期望值与实际值
并以非 0 退出，可作为每个版本的部署门禁。

注意：门禁**可对同一个库重复运行**——第 14 节的固定 slug 团队（`verify-team`）在已存在时按
409 复用既有团队继续验证，第 18 节的集成用时间戳 slug 天然无冲突，第 22 / 23 节先用探针探测配置
再按实际行为断言（两分支都会打印），第 24 节的维护模式在 `finally` 里**无条件**收回（断言中途失败
也不会把安装留在停写状态），审计断言按本轮资源 id 精确匹配、按「本次比上次多几条」而非「总共几条」。
部署机上每个版本收口都直接重跑即可。

构建上下文：仓库根 `.dockerignore` 排除了 `.venv`、`.pytest_tmp`、`__pycache__`、`docs`、`tests`、`storage`、`.env`。
`.env` 仅由 compose 的 `env_file` 注入，不进镜像。`Dockerfile` 复制 `scripts/`，验证脚本随镜像版本走。

关键环境变量：`DATABASE_URL`、`REDIS_URL`、`OBJECT_STORAGE_*`、`LOCAL_STORAGE_ROOT`、`ASSET_LINK_SECRET`、`PUBLIC_BASE_URL`、`DASHSCOPE_*`、`KLING_*`、`ELEVENLABS_*`、`WORKER_*`、`SCRIPT2VIDEO_PLANNER_PROVIDER`，V6.5 的身份配置 `ACTOR_TRUST_MODE` / `ACTOR_HEADER_SECRET` / `DEFAULT_ACTOR_ID` / `DEFAULT_ACTOR_NAME` / `DEFAULT_TEAM_SLUG` / `DEFAULT_TEAM_NAME`，V7.2 的 `API_KEY_PEPPER` / `API_KEY_DEFAULT_EXPIRES_DAYS` / `WORKER_PURGE_INTERVAL_SECONDS`，V7.3 的 `WEBHOOK_DISPATCH_ENABLED` / `WEBHOOK_REQUEST_TIMEOUT_SECONDS` / `WEBHOOK_LEASE_SECONDS` / `WEBHOOK_BACKOFF_BASE_SECONDS` / `WEBHOOK_BACKOFF_CAP_SECONDS` / `WEBHOOK_DISABLE_AFTER_FAILURES` / `WEBHOOK_BATCH_SIZE`，V7.4 的 `EMBED_TOKEN_SECRET` / `EMBED_TOKEN_DEFAULT_TTL_SECONDS` / `EMBED_TOKEN_MAX_TTL_SECONDS`，V8.2 的 `LOG_FORMAT` / `LOG_LEVEL` / `WORKER_METRICS_PORT`，以及 V8.4 的 `SECRET_ENCRYPTION_KEY` / `PROVIDER_CALLBACK_SECRET` / `PROVIDER_CALLBACK_TOLERANCE_SECONDS` / `ASSET_UPLOAD_MAX_BYTES` / `ASSET_ALLOWED_MIME_TYPES` / `ASSET_CONTENT_TYPE_DENYLIST` / `ASSET_LINK_MAX_TTL_SECONDS`，和 V8.5 的 `DEPLOYMENT_NAME` / `DEPLOYMENT_BACKUP_RETENTION_DAYS`（**维护开关刻意不在配置里**——需要它的时刻正是不能改文件重启的时刻）。完整清单以 `.env.example` 为准。

**`API_KEY_PEPPER` 是破坏性的**：哈希是 `SHA-256(pepper + 明文)`，改动它会让所有已签发的 key 立即
失效。要么部署时就配上并保持不变，要么接受轮换时的全量重签。

**`EMBED_TOKEN_SECRET` 同样是破坏性的**：它是嵌入令牌的 HMAC 密钥，改动它会让**所有在途嵌入链接**
立即失效——这正是它作为应急拉杆的价值。接入任何外部平台前必须配置；留空时使用开发默认值，仅供
本机联调。

**`SECRET_ENCRYPTION_KEY` 是「配了就得负责到底」的那一类**：它是 provider 密钥落盘加密的主密钥，
留空即关闭加密（默认，行为与升级前一致）。配置后**改动它会让所有旧密文解不开**（凭证报
`encrypted=false` 且 provider 调用失败，**不会**拿错误密钥静默降级），恢复方式是逐凭证调
`POST /api/provider-credentials/{id}/rotate` 用新密钥重加密。推荐 `openssl rand -base64 32`；
接受任意高熵字符串（内部做 `sha256` 派生），不做 base64 长度校验——校验只会制造一种「配了但因为
格式不对而静默退化成明文」的失败模式。

**`ASSET_LINK_SECRET` 在生产环境是强制项（V8.4 起）**：`APP_ENV` 不在
`local` / `dev` / `development` / `test` / `testing` 时，仍用默认 `dev-only-change-me` 会让
`/assets/{id}/access-link` 一律 503 `insecure_secret_configuration`——默认密钥是公开的，用它签出的
链接任何人都能伪造。

`PUBLIC_BASE_URL` 用于 provider 回调与外部任务访问本地资产；V7.4 起还决定签发嵌入链接的 origin
（网关后面 `Host` 头是内部一跳，按它拼出的链接只有我们自己打得开）。Linux 部署需填写服务器可访问
地址，留空会导致 worker 校验失败。

## 14. 版本进度

| 版本 | 内容 | 状态 |
|---|---|---|
| V0 | MVP 闭环（T00–T46） | 已完成 |
| V1 | 生产级编排内核（T48–T53） | 已完成 |
| V2 | Provider 与 Tool 生态（T54–T57） | 已完成 |
| V3 | 多 Pipeline 产品矩阵（T58–T61A） | 已完成 |
| V4 | 素材理解与创意规划（T62–T66） | 已完成 |
| V5 | 高质量合成与渲染（T67–T71） | 已完成 |
| V6 | Web Studio 与团队协作（T72–T79） | 已完成：Dashboard（T72）、Run Board（T73）、Review System（T74，含 T75/T76/T77 三道相邻闸门）、Asset Library（T78）、Team / Permission（T79） |
| V7 | SDK 与平台嵌入 | **已完成**：HTTP API v1 契约（V7.1 / T80）、SDK（V7.2 / T81）、Webhook（V7.3 / T82）、可嵌入工作台（V7.4 / T83）与 CLI（V7.5 / T84）全部完成 |
| V8 | 规模化运行与企业治理 | **已完成**：Worker Scaling（V8.1 / T86）、Observability（V8.2 / T87）、Quota/Billing（V8.3 / T88）、Security（V8.4 / T89）、Deployment（V8.5 / T90）五项全部完成 |

V6 / V7 / V8 均已收官（V7.5 CLI 是纯客户端版本；V6.6 补上了最后一块拼图——审核附件表 `review_attachments`，`20261010_0017`）；V6.5 已提前把
`audit_logs` 建出来（V8 的 migration 清单里原本列了它），V8 只需在其上扩展而不用重建。V8.1 没有新建表——队列是
`background_jobs` 上的列（`queue` / `priority` / `lease_expires_at` / `started_at` / `next_run_at`，`20261010_0018`），
不是独立实体。V8.3 新增 `team_quotas` + `projects.team_id`（`20261012_0019`）；V8.4 新增 `encrypted_secrets`
（`20261012_0020`）——V8 里唯一一张「存秘密」的表，且**为空即功能关闭**；V8.5 新增 `deployment_settings`
（`20261012_0021`，单行 seed）——全库唯一非租户级表，存的是「这台安装」的姿态与维护开关。
V1.1 的 T96 新增登录会话：`users` 加 `password_hash` / `password_updated_at` 两列（可空——没有
密码的用户不能登录，既有身份路径不受影响），新表 `user_sessions`（`20261012_0022`）——
存储拆分与 API key 相同（前缀点查 + 加 pepper 摘要），密码散列用标准库 scrypt。

## 15. 验证基线

当前基线（2026-10-12 实测）：

```bash
.venv/bin/python -m pytest -q --basetemp=.pytest_tmp
# 707 passed, 1 skipped
```

说明：pytest 必须带 `--basetemp=.pytest_tmp`。沙箱会拦截 pytest 默认临时目录的 mkdir；且残留的 `.pytest_tmp` 会造成大批 collection error，失败时应先清空（`find .pytest_tmp -mindepth 1 -delete`，`rm -rf` 会被安全钩子拦截）。

测试库与 MySQL 的一致性：SQLite 默认不校验外键，会在本地吞掉 MySQL 会拒绝的写入
（曾导致部署后 `POST /review-requests/{id}/assign` 返回 500）。`tests/conftest.py` 通过
`apps/api/app/db.py` 的 `enforce_sqlite_foreign_keys` 让测试引擎统一打开
`PRAGMA foreign_keys=ON`，`tests/test_review_reviewers.py` 守护这个开关。

其他检查：

- `ruff` 通过。
- 23 个 migration 可 upgrade 到 head（head = `20261109_0023`，全库 37 张表）；`20261009_0011` / `20261009_0012` / `20261010_0013` / `20261010_0014` / `20261010_0015` / `20261010_0016` / `20261010_0017` / `20261010_0018` / `20261012_0019` / `20261012_0020` / `20261012_0021` / `20261012_0022` / `20261109_0023` 都做了 upgrade/downgrade 往返验证。
- OpenAPI 共 124 个 path / 156 个 operation；7 个 HTML 页面（含 V7.4 的两个嵌入壳）不计入 schema。
- Python SDK 零运行时依赖（`sdk/python`，`import s2v`）；TypeScript SDK 编译产物 5 个 `dist/*.js` 全部通过 `node --check`（`sdk/typescript`，npm `s2v-client`）。
- 7 个页面的内联脚本通过 `node --check`。
- 部署后门禁：`docker compose exec -T api python scripts/verify_review_loop.py`（**390 项检查** = 第 1–10 节 82（审核体系 / 错误码 / 五个页面标记）+ 11 质量意图 + 12 预算与付费确认 + 22 资产库 + 32 团队与权限 + 23 API 契约 + 25 API key 与 SDK 契约 + 24 webhook + 54 可嵌入工作台 + 17 审核附件 + 10 worker scaling（队列归属 / 优先级 / 手动重试 / 死信重放 409 与 404）+ 13 observability（/metrics、请求标识、面板、权限）+ 22 配额与计费（周期窗口 / 硬软闸门 / 一次性豁免 / 归属与 404）+ 14/18 安全加固（端点接管 / 权限矩阵 / rotate / 回调签名 / 上传 / 链接）+ 16 部署姿态（迁移自检 / 维护模式拒写放读 / 逃生舱 / 备份记账 / 审计只按跃迁）+ 13 发布契约冻结（版本三处一致 / 迁移到 head / pipeline 目录 / 错误码目录 / webhook 事件 / provider 主备 / 权限矩阵）+ 18 决策审计日志（取舍与备选 / 置信度依据 / 跳过项也有账 / 人工裁决身份 / 分类过滤 / 截断标记 / 只读；同时配置 `SECRET_ENCRYPTION_KEY` 与 `PROVIDER_CALLBACK_SECRET` 时为 **412 项**），只走 HTTP）。
- 部署制品（V8.5）：`docker-compose.prod.yml`、`infra/deploy/k8s/`（7 个清单，明确不做 Helm）、`scripts/backup.sh` / `restore.sh` / `deployment_preflight.py`，步骤见 `docs/DEPLOYMENT.md`。
- 发布收口制品（V1.0）：`CHANGELOG.md`、`docs/API_STABILITY.md`（含 `docs/DECISIONS.md` 的 ADR-006）、`docs/RELEASE_CHECKLIST.md`、`examples/`（curl / Python SDK / TypeScript SDK 三条路径）、`tests/test_release.py`（16 项）。`APP_VERSION = "1.1.0"`（V1.1 T98 抬号；`1.0.0` 起兼容性承诺生效，`1.1.0` 为首个增量次版本，稳定面形状未动）。
- 每个版本任务的验收方式记录在 `docs/TASK_BOARD.md`。

## 16. 与初版方案的偏差记录

以下偏差是有意的工程决策，不是遗漏：

| 维度 | 初版方案 | 当前实现 | 原因 |
|---|---|---|---|
| 数据库 | PostgreSQL | MySQL | 部署环境既有习惯，T27 整体切换 |
| 编排方式 | Agent Runner 自主决策 | preflight → execution plan → manifest → job 的确定性链路 | 可解释、可测试、可审计；避免一次性付费调用失控 |
| 渲染 | FFmpeg + Remotion | FFmpeg + composition graph + HTML/Canvas | 避免引入 Node/React 构建链；composition graph 对多平台导出更可控 |
| pipeline 命名 | spokesperson_ad / motion_explainer / product_showcase / social_clip / localized_video / hybrid_edit | avatar_spokesperson / animated_explainer / source_clip_factory / screen_demo | 按真实业务优先级收敛，命名与业务语义对齐 |
| 阶段产物 | 10 类 artifact | 28 个 schema key | 编排、治理、质量、素材理解都需要各自的合同 |
| 审核机制 | approval_records 表 + 四值 decision | 4 张审核表 + 六值 decision，且带下游失效传播 | 门禁已在 DSL 声明，记录层在 V6.3 补齐；六值决策覆盖 reopen / cancel，失效传播避免旧审批放行新版本 |
| 审计表时机 | `audit_logs` 排在 V8 的 migration 清单 | V6.5 提前建出，V8 只在其上扩展 | V6 验收标准要求「所有关键操作有审计记录」，而 `event_logs.run_id` 非空、无法承载跨实体操作 |
| API 版本表达 | 路径前缀 `/api/v1` | 前缀保持 `/api`，版本走 `info.version` | 改前缀会同时打挂全部既有调用方、5 个页面与门禁脚本；契约稳定性靠 spec 与契约测试守护，不靠路径 |
| 错误码与状态码关系 | 一码一状态 | 允许**多码共享一个状态**（400 既可能是 `bad_request`，也可能是 `cost_budget_exceeded`） | 路由需要上报比状态码更精确的原因；强制双射会逼出无意义的合成码，因此不变式改为「状态的回退码必须与该状态自洽」 |

## 17. 后续方向

1. **相邻缺口（已完成）**：T75 provider 降级不得静默、T76 预算预留与首次付费确认、T77 审核人外键。三者均来自 V6.3 实施过程中暴露的真实风险，现已落地（见 `docs/T75_T76_RISK_GATES_PLAN.md` 与任务板）。
2. **V6（已完成）**：Team / Permission（V6.5 / T79）与审核附件（V6.6 / T85）均已落地，见 `docs/V6.5_TEAM_PERMISSION_PLAN.md` 与 `docs/V6.6_REVIEW_ATTACHMENTS_PLAN.md`。
3. **V7（已完成）**：HTTP API v1 契约（V7.1 / T80）与 SDK（V7.2 / T81）已完成，见 `docs/V7.1_API_CONTRACT_PLAN.md` 与 `docs/V7.2_SDK_PLAN.md`——前者是统一错误信封、可发现的错误码目录、可选幂等键、分页约定与「spec 与权限表精确对齐」的契约测试；后者解决了此前并列的两件事：V6.5 的「请求身份」回答「谁在调」，V7 的 `api_keys` / scopes 回答「这个程序化调用被授权做什么」，现已**合并成同一个判定入口**（`enforce_api_key_identity` → `enforce_api_permissions` → `enforce_idempotency`；凭证解析成 `ActorContext` 后与 `X-Actor-Id` 走同一条路径，scope 收窄单点判定），并落地 Python / TypeScript 两个零依赖薄封装。V7.3 Webhook、V7.4 可嵌入工作台与 V7.5 CLI（`s2v` 命令，见 `docs/V7.5_CLI_PLAN.md`）随后相继完成，**V7 全部收官**。
4. **V8（已完成，5/5）**：多类型 worker 与队列优先级（V8.1 / T86）、可观测性（V8.2 / T87）、配额与计费（V8.3 / T88）、安全加固（V8.4 / T89）、生产部署模板（V8.5 / T90）均已落地。V8.4 已接管 V6.5 里显式标记为开放的 6 条 provider 密钥端点：5 条进 `POLICY`（`provider:manage`），回调改为 HMAC 签名校验（见本文 §12.7 与 `docs/V8.4_SECURITY_PLAN.md`）。V8.5 把「能在本机跑起来」变成「能在生产上被运维」：生产 compose、K8s 模板（明确不做 Helm）、备份恢复、migration runbook，以及维护模式与部署姿态自省（见 `docs/DEPLOYMENT.md`）。
5. **V1.0（已完成，T91）**：发布收口——版本冻结 `1.0.0`、`CHANGELOG.md`、兼容性承诺（`docs/API_STABILITY.md` + ADR-006）、发布回归清单（`docs/RELEASE_CHECKLIST.md`）、端到端示例（`examples/`）与发布契约测试（`tests/test_release.py` + 门禁第 25 节）。**完整版判据逐条达成**：至少 4 条稳定 pipeline（V1.0 时在册 5 条；V1.1 T94 补齐 `localization_dub` / `hybrid_story` 后在册 7 条，`spokesperson_ad` 仍为 `draft`，见 `RELEASE_CHECKLIST` §5）；至少 3 类核心 provider 具备主备；Studio / SDK / Webhook 可用；成本、权限、审计、质量检查、部署文档完整。**V0–V8 九个里程碑全部收官。**
