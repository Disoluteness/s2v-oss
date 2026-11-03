# T74 / V6.3 Review System 实施规划

- 文档版本：v1.1
- 编写日期：2026-10-08
- 状态：**已实施完成（DONE）**
- 上位文档：`docs/PLATFORM_PLAN.md`（第 11 节审核门禁）、`docs/FULL_VERSION_ROADMAP.md`（V6.3）
- 任务板：`docs/TASK_BOARD.md` T74

---

## 0. 实施结果（2026-10-08 收尾）

按本文第 9 节的 C1–C4 四个提交顺序实施完毕，验收证据：

| 分项 | 结果 |
|---|---|
| 数据层 | migration `20261008_0010` 新建 `review_requests` / `review_decisions` / `review_comments` / `review_action_items`，并为 `stage_runs` 增加 `invalidated_at` / `invalidated_by_stage_key` / `invalidation_reason`；upgrade → downgrade → upgrade 往返验证通过 |
| 内核层 | 决策只追加、`sequence` 连续、无 UPDATE/DELETE 路径；四个硬校验（阶段待审、请求未关闭、产物范围未过期、轮次上限）均以结构化 code 拒绝 |
| 失效传播 | 沿 `depends_on_stage_keys` 传递闭包；上游重跑 / 要求修改 / 驳回 / 重开审核都会把下游已通过阶段推回失效并阻断派发，`rerun` 同时作废待审请求 |
| 接口层 | 8 个新端点 + `/api/projects/{project_id}/review-inbox`；`ReviewGateError` 统一映射为 `{code, message, ...}` 的 409/404，便于前端解释；T74 之前的 `POST /runs/{run_id}/stages/{stage_key}/review` 保持外部语义不变（仅内部改走内核） |
| Web 层 | Run Board 审核区块、Dashboard 审核收件箱、Workspace 审核关口清单；`review_focus` 英文 key 在显示层全部中文化；`approve` 与 `approve_with_changes` 在状态层同为 `approved`，三个页面按 `current_decision` 显示「已通过（带修改）」 |
| 质量门 | ruff 通过；全量测试 186 passed, 1 skipped（C4 之前为 125 passed, 1 skipped；T77 修复带来 5 个新测试） |

### 0.1 部署门禁

```bash
docker compose exec -T api python scripts/verify_review_loop.py
```

`scripts/verify_review_loop.py` 只用标准库、只走 HTTP，覆盖 78 项检查：建 run 推到第一道关口 →
关口策略快照 → 审批粒度锁定（`invalid_artifact_scope`）→ 指派 / 逾期 → 评论严重度 →
带修改通过生成待办且未完成评论保持 open → 重跑触发下游失效与请求作废 → board / 收件箱字段 →
错误码映射 → 三个 Web 页面标记。失败逐条打印期望值与实际值并以非 0 退出。

配套的部署变更：新增根级 `.dockerignore`（排除 `.venv` / `.pytest_tmp` / `__pycache__` / `docs` /
`tests` / `storage` / `.env`，`.env` 只由 compose `env_file` 注入）；`Dockerfile` 增加
`COPY scripts ./scripts`，使验证脚本随镜像版本走。

实施过程中发现并同时修掉的两个既有缺陷（属本文第 2 节"现状核查"未覆盖项）：

1. `review_stage` 未校验阶段是否真的处于 `waiting_review`，可在任意阶段写入审批。
2. `approved_with_changes` 与 `approved` 行为完全一致，"带修改通过"实际上与"通过"不可区分。

端到端验证过程中补齐的一处接口缺口：重跑动作的 `result_payload` 只暴露了
`invalidated_stage_keys`，调用方无法判断哪些审核请求被作废（这些 id 当时只写进了 event log）。
现已补 `superseded_review_request_ids`，与 event log 保持一致。

本文第 10 节的"明确不做的事"保持不变；第 11 节列出的相邻缺口已拆成 T75 / T76 记入任务板，未在本任务内扩大范围。

---

## 1. 目标与验收标准

任务板对 T74 的原文是：

> 审批、驳回、带修改通过、评论、附件、责任人；修改请求可生成新的 action。

V6 的整体验收标准是：**非技术用户能在 Web 上完成创建、审核、修复、下载。**

因此 T74 的交付物不能只是"一张记录表"，必须是一个可被人真正用起来的审核闭环。可验收的行为如下：

1. 用户打开 Dashboard，能看见"当前有哪些审核等我处理"，并一眼看清是哪个 run、哪个阶段、第几轮。
2. 用户能对某个阶段的产物给出四类结论之一（通过 / 带修改通过 / 要求修改 / 驳回），并附意见。
3. 每条结论都落库且**不可篡改**：能查到谁、在什么时间、针对哪个产物版本、给出了什么判断。
4. 给出"要求修改"时，意见能变成**可追踪的修改项**；执行后回到同一阶段重新审核，形成闭环而不是终点。
5. 上游改动后，下游既有审批**自动失效**，系统不会用一个旧审批去放行新版本。
6. 未通过门禁的阶段，其下游既不能被派发、也不能被静默跳过。

---

## 2. 现状核查（逐条对照代码）

先明确一件事：**门禁机制已经存在，本任务不是"造门禁"，而是补"记录层 + 协作层 + 失效传播"。**

| 能力 | 现状 | 证据位置 |
|---|---|---|
| 门禁声明在 DSL | ✅ 已有 | `packages/pipeline_kit/models.py:24-29`（`StageReviewPolicy`）；4 条 pipeline 的 stage 里均带 `review_policy`，含 `gate_type` / `required_before_next_stage` / `review_focus` / `success_criteria` |
| 阶段暂停等待审核 | ✅ 已有 | `db_store.py:4194-4208`：`_advance_until_gate` 在需要审核时把 stage 置 `waiting_review`、run 置 `waiting_review` |
| 审批接口 | ⚠️ 存在但功能不完整 | `routes.py:992` → `POST /runs/{run_id}/stages/{stage_key}/review` |
| 审批记录 | ❌ 无独立记录 | `db_store.py:888-960`：直接改写 `stage.status`，仅在 event log 留一条 message |
| 审批粒度绑定版本 | ❌ 无 | 审批不记录针对哪个 artifact 版本 |
| 审批前置校验 | ❌ 无 | `db_store.py:906-908`：不检查阶段是否处于 `waiting_review`，`pending` 阶段也能被直接批准 |
| "带修改通过"语义 | ⚠️ 被吞掉 | `db_store.py:907`：`approved_with_changes` 与 `approved` 走完全相同的分支，修改意见从未变成动作 |
| 评论 | ❌ 无 | — |
| 责任人 | ❌ 无 | `users` 表已存在（`db_models.py:34-44`），可外键关联 |
| 修改项（action） | ❌ 无独立实体 | 只有事件流；run board 的 `repair_actions` 是派生的提示，不可追踪 |
| 下游失效传播 | ❌ 无 | 改脚本后下游分镜/素材/剪辑不会失效 |
| 产物血缘 | ⚠️ **字段存在但从未写入** | `artifacts.derived_from` / `lineage` 在 `append_artifact`（`repositories.py:652-695`）里是可选参数，全仓库无任何调用方传入，实际恒为 NULL |
| 阶段依赖 | ✅ 已有且完整 | `depends_on_stage_keys` 在 4 条 pipeline 里都是完整链（如 spokesperson_ad：plan → script → storyboard → asset_generation → asset_review → edit → compose → final_review → export） |
| 待审队列 | ✅ 已有（阶段级） | `DashboardReviewItem`（`models.py:138`）、`metrics.pending_reviews`（`db_store.py:4344`）、`dashboard.html:529` 的"待审核队列"面板 |
| Manifest action 合同 | ✅ 已有 7 个动作 | `models.py:637-644` |
| Run state action 合同 | ✅ 已有 6 个动作（含 `rerun_stage` / `rerun_artifact`） | `models.py:665-671` |

**由此确定两条硬约束：**

- **失效传播必须走 `depends_on_stage_keys`，不能走产物血缘。** 因为 `derived_from` 从未被写入，若本任务依赖血缘，等于顺带要做一套 lineage 采集，会把 T74 撑成两个任务。
- **附件不做。** 附件属于 Asset Library（V6.4）的范畴，而资产库有自己的 provenance、授权与存储模型；现在做必然返工。

---

## 3. 设计原则

这七条是本规划的判断依据，每条都注明来源与落地位置。

| # | 原则 | 来源 | 落地位置 |
|---|---|---|---|
| P1 | **门禁是策略数据，不是代码判断** | 参考项目 | 审核要求继续从 `StageReviewPolicy` 读取；代码只做强制校验，不硬编码阶段名 |
| P2 | **一次审批只覆盖一个 `(stage_run, artifact_scope)`** | 参考项目 | `review_requests.artifact_scope` 快照 + 决策时比对 |
| P3 | **结论只追加，不修改**，当前值由最新一条派生 | 参考项目 | `review_decisions` 无 UPDATE / DELETE 路径 |
| P4 | **驳回只重跑受影响阶段，不重跑全流程** | 平台方案第 11 节 | 重跑粒度落在 stage，`attempt += 1` |
| P5 | **上游改动 → 下游标记过期**，且过期即门禁失效 | 平台方案第 11 节 | `stage_runs` 失效标记 + 沿 `depends_on_stage_keys` 传递闭包 |
| P6 | **审核结论分级**（critical / suggestion / nitpick），并要求修改项可被逐条关闭 | 参考项目 | `review_comments.severity` + `review_action_items.status` |
| P7 | **评审轮数有上限**，避免无限返工 | 参考项目 | `review_requests.iteration` + pipeline `metadata` 可配上限 |

---

## 4. 数据模型

**1 个 migration：`20261008_0010_review_system.py`，新增 4 张表 + 1 处 ALTER。**

字段类型与命名遵循现有约定：主键 `String(64)`（形如 `reviewreq_xxx`）、`tenant_id` 用 `String(64), index=True`（与 `brand_kits` / `production_runs` 等业务表一致，不用外键）、时间戳走现有 `TimestampMixin`。

### 4.1 `review_requests` —— 一次待审事项

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(64) PK | 主键 |
| `tenant_id` | String(64) idx | 租户 |
| `project_id` | String(64) idx | 冗余，用于跨 run 聚合"待我处理" |
| `run_id` | FK production_runs.id idx | 所属 run |
| `stage_run_id` | FK stage_runs.id idx | **绑定到具体一次阶段执行**，而非只绑 `stage_key`——阶段可重跑，`stage_key` 不足以定位 |
| `stage_key` | String(120) idx | 冗余，便于按阶段过滤 |
| `pipeline_key` | String(120) idx | 冗余 |
| `iteration` | Integer default 1 | 第几轮审核 |
| `status` | String(32) idx | `open` / `approved` / `changes_requested` / `rejected` / `cancelled` / `superseded`（物化缓存，可由 decisions 重算） |
| `current_decision` | String(32) nullable | 最新决策的类型，便于列表页免 join |
| `gate_type` | String(32) | 取自 DSL，如 `human_review` |
| `gates_stage_key` | String(120) nullable | 被本单门禁挡住的下一阶段（取自 DSL `required_before_next_stage`） |
| `artifact_scope` | JSON | **快照**：`[{"artifact_id":..., "artifact_type":..., "version":...}]`，本单审核的确切产物版本集合 |
| `review_focus` | JSON | **快照**：开单时的 `review_focus` |
| `success_criteria` | JSON | **快照**：开单时的 `success_criteria` |
| `assignee_user_id` | FK users.id nullable idx | 责任人（本任务只存不判，不做权限校验） |
| `due_at` | DateTime nullable | 期望完成时间（可选） |
| `opened_at` | DateTime | 开单时间 |
| `closed_at` | DateTime nullable | 关闭时间 |

**为什么 `review_focus` / `success_criteria` 要做快照？** pipeline 是有版本的（`pipeline_versions` 表已存在），审核标准会随版本变化。若开单时只存引用，日后 pipeline 升级会让"这单当初按什么标准审的"无法复现。快照让每一单自包含——这是审计成立的前提。

### 4.2 `review_decisions` —— 只追加的决策日志

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(64) PK | 主键 |
| `tenant_id` | String(64) idx | 租户 |
| `review_request_id` | FK review_requests.id idx | 所属待审事项 |
| `run_id` | String(64) idx | 冗余，便于按 run 拉时间线 |
| `sequence` | Integer | 同一单内递增，用于稳定排序 |
| `round` | Integer | 决策发生时的审核轮次 |
| `decision` | String(32) idx | `approve` / `approve_with_changes` / `request_changes` / `reject` / `reopen` / `cancel` |
| `actor_user_id` | FK users.id nullable | 操作人 |
| `actor_display_name` | String(160) nullable | **快照**，users 尚未接入真实身份时仍可读 |
| `comment` | Text nullable | 总结性意见 |
| `reason_code` | String(48) nullable | 原因分类：`content_issue` / `quality_issue` / `rights_issue` / `budget_issue` / `style_issue` / `other` |
| `artifact_refs` | JSON | 本决策针对的产物版本 `[{"artifact_id":..., "version":...}]`，必须与 `artifact_scope` 一致 |
| `created_at` | DateTime | 决策时间 |

无 `updated_at`，无 `is_current`。当前值一律由"`sequence` 最大的一条"派生。

### 4.3 `review_comments` —— 可锚定到版本的评论

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(64) PK | 主键 |
| `tenant_id` | String(64) idx | 租户 |
| `review_request_id` | FK review_requests.id idx | 所属待审事项 |
| `run_id` | String(64) idx | 冗余 |
| `stage_key` | String(120) idx | 冗余 |
| `artifact_id` | String(64) nullable idx | 锚定产物 |
| `artifact_version` | Integer nullable | **锚定版本**——评论针对的是第几版 |
| `author_user_id` | FK users.id nullable | 作者 |
| `author_display_name` | String(160) nullable | 快照 |
| `severity` | String(24) idx | `critical` / `suggestion` / `nitpick` |
| `body` | Text | 评论内容 |
| `status` | String(24) idx | `open` / `resolved` / `dismissed` |
| `resolved_by_decision_id` | String(64) nullable | 由哪次决策关闭 |

**为什么评论不能合并进决策？** 因为"带修改通过"这个语义要求：结论是"通过"，但同时还挂着 2 条未处理的 nitpick。把评论塞进决策表就无法表达"结论已定、意见未清"的状态。

### 4.4 `review_action_items` —— 修改要求变成可追踪的动作

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(64) PK | 主键 |
| `tenant_id` | String(64) idx | 租户 |
| `review_request_id` | FK review_requests.id idx | 来源待审事项 |
| `decision_id` | FK review_decisions.id idx | 由哪次决策产生 |
| `run_id` | String(64) idx | 冗余 |
| `kind` | String(48) idx | 动作类型，见下 |
| `stage_key` | String(120) nullable | 目标阶段 |
| `artifact_type` | String(120) nullable | 目标产物类型 |
| `stage_run_id` | String(64) nullable | 目标阶段执行实例 |
| `instruction` | Text | 审核人写的具体修改要求 |
| `linked_comment_ids` | JSON | 关联的评论 |
| `result_action` | String(48) nullable | 实际派发给已有合同时用的动作名 |
| `status` | String(24) idx | `open` / `in_progress` / `done` / `dismissed` |
| `produced_artifact_id` | String(64) nullable | 完成后产出的新产物 |
| `closed_at` | DateTime nullable | 关闭时间 |

`kind` 取值**直接引用已有的两套动作合同**，不发明平行词汇：

| `kind` | 对应已有合同 | 说明 |
|---|---|---|
| `rerun_stage` | `RunStateActionName.RERUN_STAGE` | 重跑整个阶段（最常见：重写脚本） |
| `rerun_artifact` | `RunStateActionName.RERUN_ARTIFACT` | 只重生成某个产物 |
| `regenerate_media` | `ManifestActionName.GENERATE_PERSON_IMAGE` / `GENERATE_TTS` | 重生成图片/配音类素材 |
| `reanalyze_source` | `ManifestActionName.ANALYZE_SOURCE_MEDIA` | 重新分析源素材 |
| `manual` | 无 | 人工在外部处理后重新上传，走 `POST /runs/{run_id}/assets` |

`result_action` 记录实际调用的是哪个动作，让"审核提出的要求"与"系统执行的动作"可对齐审计。

### 4.5 `stage_runs` ALTER —— 下游失效标记

新增 3 列（均可空，不改变现有枚举）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `invalidated_at` | DateTime nullable | 失效时间 |
| `invalidated_by_stage_key` | String(120) nullable | 由哪个上游阶段的改动导致 |
| `invalidation_reason` | Text nullable | 失效原因说明 |

**为什么放在 `stage_runs` 而不是 `artifacts`？** 失效的传播单位是阶段（沿 `depends_on_stage_keys` 传递），不是单个产物；一个阶段可能有多个产物，标记在阶段上避免在 N 个产物上重复写同一件事。产物层的失效可由 `artifacts.stage_run_id` 继承得到。

**为什么不加 `StageStatus.STALE`？** 失效与阶段执行状态是正交的：一个 `approved` 的阶段可以同时是"已失效"。塞进同一个枚举会让状态机分叉，也会让现有 `stage.status == waiting_review` 之类的判断全线受影响。

---

## 5. 状态与流转

### 5.1 `review_request` 状态机

```
                    ┌─────────────── cancel ───────────────┐
                    ▼                                      │
  (开单) ──→ open ──┬── approve ──────────────→ approved ──┼──→ 下游放行
                    │                                      │
                    ├── approve_with_changes → approved ───┤   （同时产生 action items，可后续关闭）
                    │                                      │
                    ├── request_changes ──→ changes_requested ──→ 阶段重跑 → 新开一单（iteration+1）
                    │                                      │
                    ├── reject ───────────→ rejected ──────┘   （run 暂停，等人工决定）
                    │
                    └── （上游改动 / 阶段重跑）── superseded
```

- `reopen` 不改变状态机：它对**已关闭**的单追加一条决策，把单重新拉回 `open`，并产生新的一轮 `iteration`。这条路径专门用于"审批通过之后发现漏了东西"。
- `superseded` 由系统置位，不由人触发：当该单的 `artifact_scope` 被新版本取代、或阶段被重跑时自动进入。

### 5.2 与既有 stage / run 状态的耦合

| 决策 | `stage_runs.status` | `production_runs.status` | 附加动作 |
|---|---|---|---|
| `approve` | `approved` | `running`，`current_stage_key` 推进到下一阶段 | 调 `_advance_until_gate` 继续推进 |
| `approve_with_changes` | `approved` | 同上 | 产生 `review_action_items`，状态 `open`；**不阻断推进** |
| `request_changes` | `pending`（重置），`attempt += 1` | `paused` | 产生 action items；下游标记失效；等 `rerun_stage` |
| `reject` | `rejected` | `paused` | 不自动重跑，等人决定是重跑还是取消 run |
| `reopen` | `pending` | `paused` | 已被放行的下游被标记失效 |
| `cancel` | `cancelled` | `cancelled` | — |

### 5.3 决策 → 修改项自动生成规则

提交 `request_changes` / `reject` / `approve_with_changes` 时，系统按以下规则尝试把意见转成 action item：

1. 请求体里显式给了 `action_items[]` → 直接按给定的 `kind` / 目标 / 指令创建。
2. 请求体没给，但该单存在 `severity = critical` 的评论 → 每条 critical 评论自动生成一个 action item，`kind` 按阶段推断：
   - 阶段属于脚本类（`plan` / `script` / `storyboard` / `demo_plan` / `recording_plan` / `clip_selection`）→ `rerun_stage`
   - 阶段是 `asset_generation` / `capture` → `regenerate_media`
   - 阶段是 `analysis` / `source_ingest` → `reanalyze_source`
   - 其余 → `manual`
3. 都没有 → 不产生 action item，只留决策与评论（`reject` 的常见情形）。

**推断规则写在代码常量表里，不写进 pipeline DSL。** 因为这是"阶段能力 → 动作合同"的映射，属于平台能力，不是单条流水线的业务策略；写进 DSL 会让 4 条 pipeline 都要重复声明。

---

## 6. 关键语义细化

### 6.1 审批粒度 = `(stage_run, artifact_scope)`

开单时把该阶段的 current artifacts 快照为 `artifact_scope`。提交决策时校验：

- 请求里的 `artifact_refs`（若有）必须是 `artifact_scope` 的子集；
- 当前该阶段的 current artifacts 必须与 `artifact_scope` 完全一致。

不一致 → 拒绝，返回 `409` + `code = "stale_artifact_scope"`，并附上新的 `artifact_scope` 建议值，提示"产物已更新，请在新的一单上审核"。

这条规则直接消除"用一个旧审批放行一个新版本"的可能性。

### 6.2 拒绝无效审批（4 条硬校验）

现有实现里，`pending` 阶段也能被直接批准。T74 补上：

| 校验 | 失败返回 |
|---|---|
| 目标阶段必须存在，且本单 `run_id` / `stage_run_id` 匹配 | `404` |
| 阶段必须处于 `waiting_review` | `409` `stage_not_awaiting_review` |
| 本单状态必须是 `open`（已关闭的单需先 `reopen`） | `409` `review_request_closed` |
| `artifact_scope` 必须与当前 current artifacts 一致 | `409` `stale_artifact_scope` |

另外：**同一 `(stage_run)` 同时只允许存在 1 个 `open` 状态的 review request**（数据库唯一索引保证）。

### 6.3 下游失效传播算法

触发时机：阶段重跑（`rerun_stage`）或产生新版本产物时。

```
输入：源阶段 S，run R
1. 取 R 的 pipeline 定义，构造 stage_key -> depends_on_stage_keys 的邻接表
2. 从 S 出发做传递闭包，得到下游阶段集合 D（不含 S 自身）
3. 对 D 中每个阶段 t（按 stage_order 升序）：
   a. 若 t 已有 current artifact：把 t 的 stage_run 置 invalidated_at=now,
      invalidated_by_stage_key=S, invalidation_reason="Upstream stage <S> produced a new version."
   b. 若 t.status 曾是 approved：回退为 pending（该批准作废）
   c. 若 t 存在 open 的 review request：置 superseded 并关闭
   d. 追加 event：stage.invalidated
4. 不触达 S 的上游
```

规则里的关键点是 (b)：**批准不是一次性授予的永久通行证**。上游一变，下游的旧批准作废，必须重跑重审。否则"驳回只重跑受影响阶段"就变成了"重跑但不重审"，闭环仍然漏。

反向约束：下游被标记失效后，`POST /runs/{run_id}/dispatch-stage-execution` 与 `manifest-actions` 的 `dispatch_ready_steps` 必须跳过这些阶段，并在响应里给出结构化 blocker，说明"等待 <S> 重新审核通过"。（对应平台方案第 11 节与 P5：禁止静默降级/跳过。）

### 6.4 修改项 → 重跑 → 回到审核

一个完整的修复回环：

1. 审核人提交 `request_changes`，给出一条 critical 评论「第 3 段口播太长，压到 2 句」。
2. 系统生成 action item：`kind = rerun_stage`, `stage_key = script`, `instruction = 上述评论`。
3. 用户在 run board 看到这条 action item，点"执行"→ 调 `POST /runs/{run_id}/state-actions`，`action = rerun_stage`。系统把 `instruction` 注入该阶段的 `input_payload`（作为修改要求），重跑。
4. 新版本产物产生 → 下游标记失效 → 该阶段重新进入 `waiting_review` → 自动开新单，`iteration = 2`。
5. 新单批准后，action item 置 `done`，关联的评论置 `resolved`。

第 3 步的 `instruction` 注入是本回环的关键：**修改要求必须真的到达生成环节**，否则评论只是留言板。

### 6.5 评审轮数上限

- `review_requests.iteration` 逐轮累加。
- 上限读取顺序：pipeline `metadata.review.max_rounds` → 全局默认 5。
- 达上限后，新开单返回 `409` `review_round_limit_reached`，响应里带上 `allow_override: true` 提示；请求体带 `override_round_limit: true` 时放行并记录 `reason_code = "round_limit_override"`。

做成"可强制放行"而不是硬阻断，因为业务上确实存在合理多轮；硬阻断会把用户卡死在流程里。

### 6.6 与现有接口的兼容

`POST /runs/{run_id}/stages/{stage_key}/review` **保留且不改变对外语义**（它仍是"一键批准"的快捷入口），但内部改为走新内核：

1. 若无 `open` 单 → 自动开单（在 `artifact_scope` 快照阶段）；
2. 提交决策 → 走 6.1 / 6.2 的全部校验。

`ApprovalRequest.decision` 的 4 个取值保持不变；但映射到新模型时：`approved → approve`、`approved_with_changes → approve_with_changes`、`rejected → reject`、`cancelled → cancel`。

这样 `tests/test_db_store.py:600-613` 那条既有断言（批准 `plan` 后 `current_stage_key == "script"`、run 状态 `waiting_review`）仍然成立，同时获得了审计记录。**现有测试不需要改**——这是判断改造是否安全的一个硬指标。

---

## 7. API 合同

### 新增（8 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/runs/{run_id}/review-requests` | 列出该 run 的审核单，支持 `status` / `stage_key` 过滤 |
| POST | `/api/runs/{run_id}/review-requests` | 手动开单（正常由系统自动开；用于补开、重开） |
| GET | `/api/review-requests/{review_request_id}` | 详情：含 decisions、comments、action items、artifact_scope |
| POST | `/api/review-requests/{review_request_id}/decisions` | 提交决策 |
| POST | `/api/review-requests/{review_request_id}/comments` | 新增评论 |
| PATCH | `/api/review-comments/{comment_id}` | 更新评论状态（`open` / `resolved` / `dismissed`） |
| POST | `/api/review-requests/{review_request_id}/assign` | 设责任人 / 期望完成时间 |
| GET | `/api/projects/{project_id}/review-inbox` | 跨 run 的"待我处理"聚合（按责任人、轮次、超期排序） |

请求体定义要点：

- `SubmitReviewDecision`：`decision`（枚举）、`comment`、`reason_code`、`artifact_refs[]`、`action_items[]`、`override_round_limit`
- `CreateReviewComment`：`severity`、`body`、`artifact_id`、`artifact_version`
- `ReviewActionItemInput`：`kind`、`stage_key`、`artifact_type`、`instruction`、`linked_comment_ids[]`

### 改造（3 个）

| 路径 | 改造 |
|---|---|
| `POST /api/runs/{run_id}/stages/{stage_key}/review` | 内部委托到新内核，对外语义不变 |
| `GET /api/runs/{run_id}/board` | `stage_lanes[]` 增加 `review`（当前审核单摘要）、`invalidated` 标记；顶层增加 `review_timeline` |
| `GET /api/projects/{project_id}/dashboard` | `review_queue[]`（`DashboardReviewItem`）扩展 `iteration` / `assignee` / `overdue`；`metrics` 增加 `overdue_reviews` |
| `POST /api/runs/{run_id}/manifest-actions` | `dispatch_ready_steps` 跳过已失效阶段并返回结构化 blocker |

### 事件类型（写入现有 `event_logs`）

`review.requested` / `review.decision.{approve|approve_with_changes|request_changes|reject|reopen|cancel}` / `review.comment_added` / `review.action_item.created` / `review.action_item.closed` / `stage.invalidated` / `stage.revalidation_required`

`source_type = "review"`，`source_id = review_request_id`——复用现有事件流，run board 的"事件流"面板无需改造即可显示。

---

## 8. Web 接入（3 处）

| 页面 | 改造 |
|---|---|
| `apps/web/run_board.html`（28KB，单 run 看板） | 在「阶段泳道」与「产物时间线」之间插入**「审核」**区块：当前审核单卡片（轮次、责任人、聚焦点、成功标准快照）＋ 四按钮操作区 ＋ 评论表单（带严重度选择）＋ 审核历史时间线（append-only 决策列表）＋ 修改项清单。阶段泳道对已失效阶段打标记 |
| `apps/web/dashboard.html` | 「待审核队列」升级为「审核收件箱」：显示轮次、责任人、是否超期；增加"只看我的"筛选；行内提供快速通过/要求修改 |
| `apps/web/review.html`（97KB，实际是 `/workspace` 的载体，命名与用途错位） | 加入审核侧栏：新建 run 后立刻能看到本次运行的门禁清单与"下一道门禁是什么" |

界面文案沿用 T61A 的中文显示层约定：**后端与 API 全部保留英文 key，中文只在 Web 显示层**。

---

## 9. 实施顺序

按可独立验证的 4 个提交推进，每个提交跑完整测试并回写任务板。

| 提交 | 内容 | 验证 |
|---|---|---|
| C1 | migration `0010`：4 张新表 + `stage_runs` 3 列；对应 ORM 模型与 Pydantic 模型 | `alembic upgrade head` 通过；`test_db_models.py` 新增表结构断言 |
| C2 | 审核内核：开单、快照 `artifact_scope`、提交决策、4 条校验、append-only 派生状态；改造旧 review 接口委托到内核 | 新增 `tests/test_review_kernel.py`；**`test_db_store.py` 既有断言必须全绿不改** |
| C3 | 协作层：评论、责任人、`review_action_items` 生成与关闭、轮数上限；失效传播 + 派发阻断 | 新增 `tests/test_review_collaboration.py`、`tests/test_invalidation.py` |
| C4 | API 与 Web：8 个新端点、board/dashboard 扩展、3 个页面接入 | 新增 `tests/test_review_api.py`、`test_web_review.py`；ruff；全量测试 |

预计测试增量：**约 35–45 条**，全量从 `125 passed, 1 skipped` 升到约 `160–170 passed, 1 skipped`。

---

## 10. 测试清单（要点）

**内核（C2）**

1. 开单时 `artifact_scope` 正确快照当前 current artifacts
2. `pending` 阶段的审批被拒（`409 stage_not_awaiting_review`）
3. 产物版本变化后旧单决策被拒（`409 stale_artifact_scope`）
4. 同一 `stage_run` 不能有 2 个 `open` 单（唯一索引）
5. `approved_with_changes` 不再等同于 `approved`：产生 action item 且 run 继续推进
6. `reject` 后 run 为 `paused` 且阶段为 `rejected`
7. `reopen` 对已关闭单追加决策并把状态拉回 `open`，`iteration` 递增
8. `review_decisions` 无 UPDATE/DELETE 路径（可用 SQLAlchemy 事件或直接断言无对应方法）
9. 旧接口 `review_stage` 行为不变（沿用既有断言）

**协作（C3）**

10. critical 评论自动生成 `rerun_stage` action item，`kind` 按阶段推断正确
11. action item 完成后关联评论变 `resolved`
12. `rerun_stage` 的 `instruction` 被注入阶段 `input_payload`
13. 轮数达上限被拒；带 `override_round_limit` 放行
14. 责任人字段可写入并可查询；未设置时不报错

**失效传播（C3）**

15. 重跑 `script` 后，`storyboard` / `asset_generation` / `asset_review` / `edit` / `compose` / `final_review` / `export` 全部标记失效；`plan` / `intake` 不受影响
16. 被失效阶段的既有 `approved` 回退为 `pending`
17. 被失效阶段的 `open` 审核单转为 `superseded`
18. `dispatch_ready_steps` 跳过失效阶段并返回结构化 blocker
19. 新版本产物产生后该阶段可重新进入 `waiting_review` 并开新单

**API / Web（C4）**

20. 8 个新端点的正常路径与 404/409 分支
21. `GET /runs/{run_id}/board` 含 `review` 与 `invalidated` 字段
22. `review-inbox` 按责任人与轮次排序正确
23. Web 页面含审核区块与中文文案（沿用 `test_web_workspace.py` 的断言风格）

---

## 11. 本任务不做（明确边界）

| 不做 | 原因 | 归入 |
|---|---|---|
| 附件上传 / 附件预览 | 依赖 Asset Library 的 provenance、授权与存储模型 | V6.4 |
| 权限校验（"谁能审哪个阶段"） | 责任人字段先存后判；`users` 表尚无真实身份接入 | V6.5 |
| 通知（邮件 / IM / webhook） | 属于 V7 的 SDK / Webhook 范畴 | V7 |
| 产物级血缘采集（回填 `derived_from`） | 本任务用阶段依赖替代；血缘是独立的数据工程 | 待评估 |
| 审核 SLA 与自动升级（超时自动转交） | 需要先有通知能力 | V7 之后 |

---

## 12. 相邻缺口（本次顺手发现，建议另立任务）

这两条是我在前几轮扫描中发现的、**与审核强相关但不应塞进 T74** 的缺口。它们都已在 `docs/PLATFORM_PLAN.md` 里记为待办，这里给出建议的落位。

1. **禁止静默降级未实现。** 平台方案第 11 节明确要求"用户已批准的 provider 不能被静默替换"，但 `packages/tool_kit/selector.py` 的 fallback 目前没有"已批准则禁止替换"的约束，也没有结构化 blocker 返回。T74 只在"阶段派发"这一层加了阻断（6.3 的反向约束），provider 层的替换约束仍未做。
   → 建议新增 **T75 / V6.6 Provider 替换约束**（依赖 T74 的 blocker 响应规范）。

2. **预算缺 `reserve` 与首次付费确认。** `usage_ledger_entries` 目前只有记账与预算门禁，缺 `estimate → reserve → reconcile` 中的 reserve 环节，也没有"首次使用某付费 provider 需单独确认"。
   → 建议新增 **T76 / V6.7 预算预留与首次付费确认**。

如果这两条优先级更低，也可以先落 TASK_BOARD 的 `TODO` 占位，不排期。

---

## 13. 待确认事项

开工前需确认 3 点：

1. **表结构调整**：4 张新表 + `stage_runs` 加 3 列，1 个 migration。是否接受？（4 张表的拆分理由见 4.3 / 4.4 的"为什么不合并"）
2. **任务拆分**：C1–C4 作为一个 T74 交付，还是把 C1–C2（内核）与 C3–C4（协作 + Web）拆成 T74 / T75 两条任务板条目？（若拆，第 12 节的两条缺口顺延为 T76 / T77）
3. **轮数上限默认值**：全局默认 5 轮，可被 pipeline `metadata.review.max_rounds` 覆盖；这是否合适？
