# Changelog

本文件记录对外可见的变更。格式沿用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本。

服务端版本由 `apps/api/app/version.py` 的 `APP_VERSION` 单独持有，并发布在
`GET /openapi.json` 的 `info.version` 与 `GET /api/deployment` 的 `app_version`。
两个 SDK（`s2v` / `s2v-client`）**独立演进**，各自的版本不出现在这里。

从 `1.0.0` 起，稳定面的兼容规则由 `docs/API_STABILITY.md` 约束；本文件只记录
「已经发生的变更」，不预测。

## [1.1.0] - 2026-10-13

V1.1 能力补齐里程碑（`docs/V1.1_CAPABILITY_PLAN.md`）完整落地（T93–T97）。
**这是兼容性承诺（`docs/API_STABILITY.md`，ADR-006）生效后的第一个次版本**：
本节全部为**增量**变更——新增端点、新增可选请求字段、新增错误码、目录扩充；
稳定面的既有形状一字未动。**从 `1.0.0` 升级不需要任何集成方改动**，只需重建 api 镜像
（部署侧需将 migration 升至 `20261012_0022`）。

### Added

- 媒体分析高级项（T93）：
  - `media_analysis_report.v1` 的 `scenes` 由「整段一个窗口」升级为 FFmpeg 场景检测产出的**精确镜头边界**（每条带 `detection_method` / `confidence` / 检测阈值；检测不可用时回退整段条目并写明原因）。
  - 每个源资产报告新增 `scene_cut_count` / `scene_cuts` / `scene_detection_threshold` / `scene_boundary_count` / `audio_energy`（分窗 RMS 响度时间线与静音/响亮分段）。
  - `media_summary` 新增 `scene_cut_count` / `scene_boundary_count` / `audio` 块 / `transcript_engine`，且 `has_transcript` 改为反映真实转写状态。
  - `video_analysis_brief` 新增 `clip_planning_inputs.scene_boundary_available`；`scene_boundary_detection_pending` 限制只在确实没有边界时出现。
  - ASR 转写：新工具 `dashscope_asr`（qwen3-asr-flash，复用既有 `DASHSCOPE_API_KEY`，不引入新 provider）。转写需要 `provider_options.asr.audio_url` 指向引擎可达的音频；不可用时 `transcript.status` 仍为 `unavailable` 并记录具体原因。
  - `POST /api/runs/{run_id}/media-analysis` 请求体新增 5 个可选开关：`detect_scenes` / `scene_threshold` / `analyze_audio` / `audio_window_seconds` / `transcribe_audio`（全部默认开启，不传即获得完整分析）。
- 两条新 pipeline（T94，均为 `beta`）：
  - `localization_dub`：输入视频 → 转写分析 → 本地化方案（目标语言 / 术语表 / 配音选角 / 口型策略）→ 翻译配音稿 → 配音生成 → 重合成 → 导出。
  - `hybrid_story`：用户素材权威 + AI 补充素材——故事大纲明确「用户素材 / AI 补充」切分，素材接入、补充生成后混合合成。
  - 新增 3 个 artifact schema：`localization_plan.v1` / `dubbing_manifest.v1` / `story_outline.v1`；`spoken_script.v1` / `shot_list.v1` / `source_asset_manifest.v1` 补充占位负载许可（占位形如 `{"stage_key", "status": "placeholder"}`，与 `edit_plan.v1` 等既有 manifest 类一致；真实负载校验不变）。
  - `GET /api/pipelines` 目录 5 → **7** 条；Web 工作台下拉与标签同步。
- 列表受控排序（T95）：
  - 六个分页列表端点新增可选 `sort` 查询参数：`GET /api/audit-logs` / `/api/idempotency-keys` / `/api/api-keys` / `/api/webhooks` / `/api/webhooks/{id}/deliveries` / `/api/external-integrations`。
  - 语法：逗号分隔多字段，`-` 前缀降序（如 `sort=status,-created_at`）；每个端点声明允许排序的字段白名单与文档化默认序。
  - 未知字段、主键 `id`、重复字段、空 token 一律 **422 `validation_error`**，不静默回退服务器默认序。
  - 末尾隐式追加主键作决胜键，offset 分页在非唯一排序键上不再漏行 / 重行；`sort=-created_at` 与不传参数（含并列顺序）完全一致。
  - **不传 `sort` 时行为与旧版字节级相同**——这是纯增量、向后兼容的变更。
- 用户名密码登录与可撤销会话（T96）：
  - 新端点：`POST /api/auth/login`、`POST /api/auth/logout`（吊销当前会话，幂等）、
    `GET /api/auth/session`（自省），以及 `POST /api/users/{user_id}/password`
    （设置 / 更换登录密码，`member:manage` 租户级；设新密码会使该用户既有会话立即失效）。
  - 会话令牌形如 `Bearer s2v_sess_<handle>.<secret>`；明文只在登录响应出现一次，
    存储只留查询前缀 + 混入 `API_KEY_PEPPER` 的 SHA-256 摘要（轮换 pepper 会同时作废
    API key 与登录会话，仍是单一紧急开关）。TTL 默认 72 小时（`SESSION_TTL_HOURS` 可调）。
  - 密码散列用标准库 `hashlib.scrypt`（逐哈希随机盐，成本参数内嵌在存储串中，升级无需迁移）；
    未设密码的用户不能登录，既有身份路径不受影响。
  - 身份优先级：**API key → 登录会话 → 受信头 → 默认 actor**。不出示任何凭据的请求行为
    与 `1.0.0` 完全一致。错误码目录 26 → **28**（新增 `auth_invalid_credentials` / `session_invalid`）。
  - 维护模式豁免 `/api/auth`：维护窗口停的是租户写，不是「证明你是谁」。
  - migration `20261012_0022`（`users` 加 `password_hash` / `password_updated_at` 两列 +
    新表 `user_sessions`），全库 35 → **36** 张表。
- 租户隔离收敛（T97）：
  - 新增 app 级租户守卫：路径寻址的资源（项目 / run / 资产 / 任务 / 工具调用 / 评审 / 品牌 /
    样式 / 集合 / 凭据 / webhook / 团队 / 用户等 18 类 id）必须属于调用方租户，否则返回与
    「资源不存在」**完全相同**的 404（同状态、同信封、同文案）——不泄露存在性。
  - 登录会话与嵌入令牌身份按各自租户比较；嵌入令牌的 project/run 钉定语义不变。
  - 项目子资源创建（品牌套件 / 样式预设 / 资产集合 / 集合条目 / run）继承父项目租户；
    项目创建跟随调用方租户。单租户部署行为与 `1.0.0` 一致。
  - `GET /api/projects` 与 `GET /api/provider-credentials`（含单个读取）此前不按租户过滤，
    现已收敛；这两个列表在 `1.0.0` 是多租户下的已知信息泄露面。

### Changed

- `scenes[].detection_method` 在场景检测产出时取值为 `ffmpeg_scene_detection`（此前恒为 `ffprobe_full_asset_interval_sampling`）。按 `detection_method` 做分支的调用方需要兼容两种取值；`video_analysis_brief` 的 `candidate_windows[].basis` 同步跟随。
- 工具目录 25 → 26（`dashscope_asr`，其 fallback 为既有 `local_transcript_placeholder`）。

## [1.0.0] - 2026-10-12

V1.0 完整生产版。不新增能力，把已能运行的系统变成**可以对外承诺**的系统。

### Added

- `docs/API_STABILITY.md`：稳定面 / 实验面划分、兼容规则表、弃用流程（ADR-006）。
- `docs/RELEASE_CHECKLIST.md`：发布前清单、回归基线、回滚触发条件。
- `examples/`：三条端到端示例（`curl` / Python SDK / TypeScript SDK）。
- `tests/test_release.py`：把版本与契约冻结写成断言。
- 部署门禁新增第 25 节「发布契约冻结」（只走 HTTP）。

### Changed

- `info.version` 与 `app_version` 由 `0.9.0` 抬至 **`1.0.0`**。这是一次**承诺生效**而非行为变更：
  稳定面的形状不变，变的是「可否无公告改动」的答案。

### Compatibility

- 无端点增删、无字段增删、无错误码增删、无 migration、无新依赖、无新环境变量。
- 既有集成方**无需任何改动**即可升级到 `1.0.0`。

## [0.9.0] - 2026-10-12

V8 规模化运行与企业治理版（T86–T90）。

### Added

- **V8.5 Deployment**（T90）：`GET /api/deployment` 部署姿态自省；**维护模式**（开关存
  `deployment_settings` 而非 `.env`，只停写不停读，`/api/deployment*` 与 `/api/provider-callbacks*`
  两条写豁免）；生产 compose `docker-compose.prod.yml`；K8s 部署模板 `infra/deploy/k8s/`；
  `scripts/backup.sh` / `scripts/restore.sh`；`scripts/deployment_preflight.py`；`docs/DEPLOYMENT.md`。
  新表 `deployment_settings`（`20261012_0021`）。权限键 `deployment:read` / `deployment:manage`；
  错误码 `maintenance_mode`（503，`retryable = true`）。
- **V8.4 Security**（T89）：provider 密钥落盘加密（`encrypted_secrets` + AES-256-GCM 的
  `SecretBox`，`SECRET_ENCRYPTION_KEY` 为空即关闭）；5 条 `provider-credentials*` 端点由开放改为
  `provider:manage` 受控；供应商回调加 HMAC 签名校验；上传编码长度 / 大小 / 类型 / 魔数四道检查；
  资产签名链接 TTL 夹取与生产默认密钥拒签。新表 `encrypted_secrets`（`20261012_0020`）。
  新依赖 `cryptography>=43`。
- **V8.3 Quota / Billing**（T88）：团队周期配额（`calendar_month` / `rolling_30d` / `lifetime`，
  窗口在读取时算）+ 硬/软闸门 + 单 run 一次性豁免；用量归因 `usage_ledger_entries → runs →
  projects.team_id`。新表 `team_quotas` + `projects.team_id`（`20261012_0019`）。
- **V8.2 Observability**（T87）：`/metrics`（免鉴权、不进 OpenAPI）；`LOG_FORMAT=json` 与
  `X-Request-Id`；`GET /api/projects/{id}/observability` 四视图面板。
- **V8.1 Worker Scaling**（T86）：`background_jobs` 增加队列 / 优先级 / 租约 / 退避列
  （`20261010_0018`）；worker 支持 `--role`；可重试失败自动指数退避、耗尽进死信并可 requeue；
  RUNNING 超时收割。

### Changed

- OpenAPI `info.version` 由 `0.8.0` 抬至 `0.9.0`。
- 5 条 `provider-credentials*` 端点由免鉴权改为需要 `provider:manage`（**破坏性**：未持该权限的
  调用方会开始收到 403）。

## [0.8.0] - 2026-10-11

V7 SDK 与平台嵌入版（T80–T84）。

### Added

- **HTTP API v1 契约**（T80）：统一失败信封 `{"detail": {code, message, ...}}`、稳定错误码目录
  `GET /api/error-codes`、幂等键（`Idempotency-Key` + 重放标记）、`Page[T]` 分页、OpenAPI 每个
  operation 声明错误响应模型。
- **Python / TypeScript SDK**（T81）：`s2v` / `s2v-client`，可插拔 Transport，自动幂等键。
- **Webhook**（T82）：`GET /api/webhook-events` 事件目录；事务性 outbox；`X-S2V-Signature`
  签名（HMAC-SHA256，时间戳在签名串内）；指数退避、`abandoned` 可重投、连续失败自动停用。
- **可嵌入工作台**（T83）：无状态签名令牌、上下文钉定、双层 scope 裁剪。
- **CLI**（T84）：`s2v` 命令行，退出码 `0/1/2/3/4` 为脚本契约。

### Changed

- OpenAPI `info.version` 由 `0.6.0` 抬至 `0.8.0`（V6 同期发布的 0.7.0 见下）。

## [0.7.0] - 2026-10-10

V6 Web Studio 与团队协作版（T74–T79、T85）。

### Added

- 审核体系（T74）：审核请求 / 决策 / 评论 / 由「要求修改」派生的待办。
- 风险闸门（T75 / T76）：provider 降级不得静默；预算预留与首次付费确认。
- 资产库（T78）：集合、来源与授权、从 run 沉淀、复用到 run。
- 团队与权限（T79）：团队 / 成员 / 项目成员 / 审计流水 / 权限矩阵。
- 审核附件（T85，V6.6）：审核请求 ↔ 资产挂载。

## [0.6.0] - 2026-10-09

V5 高质量合成与渲染版。

### Added

- 合成与渲染管线：渲染配置、画布渲染、质量门禁、发布包。

## [0.5.0]

V4 素材理解与创意规划版。

### Added

- 素材理解（媒体分析）与创意规划（proposal）。

## [0.4.0]

V3 多 Pipeline 产品矩阵版。

### Added

- 四条 pipeline：`avatar_spokesperson`、`animated_explainer`、`source_clip_factory`、`screen_demo`。

## [0.3.0]

V2 Provider 与 Tool 生态版。

### Added

- provider adapter、工具契约、能力预检、fallback 评分、凭证管理。

## [0.2.0]

V1 生产级编排内核版。

### Added

- Pipeline DSL、artifact schema、manifest action、run 状态机、成本与用量账本、错误分类。

## [0.1.0]

V0 MVP 闭环版。

### Added

- 第一个可用闭环：脚本 → 规划 → 生成 → 合成，一个口播广告 pipeline 与 Web 工作台雏形。
