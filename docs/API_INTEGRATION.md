# 对外接入指南：别的服务怎么调这个服务

状态：随 **1.1.0** 编写；端点清单由运行中的应用自身导出（`GET /openapi.json`，124 path / 156 operation），
与 `tests/test_openapi_contract.py` 钉住的契约同源。

这份文档回答一个问题：**另一个服务要调本服务，该怎么接、按什么顺序调。**

## 0. 先回答最直接的那个问题：是调页面的接口吗？

**是，但不只是。** 本服务只有一个 HTTP 面：`/api/*`。工作台页面（`/workspace`、`/run-board` …）
自己就是这套接口的第一个客户端——它没有任何私有通道。所以「页面用的接口」和「对外提供的接口」
是同一批端点，`GET /openapi.json` 里就是全部。

但**页面能用不等于服务能用**，直接照搬页面请求会缺三样东西：

| 页面靠什么 | 服务要靠什么 | 不补上会怎样 |
|---|---|---|
| 浏览器会话（`s2v_sess_` 令牌） | **API key**（`Authorization: Bearer`） | 401 `unauthorized` |
| 用户手点、点错再点一次 | **`Idempotency-Key`** 头 | 超时重试会重复建 run、重复扣费 |
| 人盯着页面等 | **webhook** 收事件（轮询兜底） | 只能盲等，或把 `GET` 打成热循环 |

还有一条**页面用户看不见、服务迟早会撞上**的：这套流水线在每个关键阶段都设了**人工审核关口**
（§4.7）。页面里那是「等你点通过」的按钮；服务里你要么转发给人，要么用自己的 key 自动决策。

另外三类接口**不是**给外部服务用的，别接：

- `POST /api/auth/login`、`/api/auth/logout`、`GET /api/auth/session`——那是**人**在浏览器里登录用的，
  凭据是密码和会话令牌，和 API key 无关。
- HTML 页面（`/workspace` 等 7 条）——`include_in_schema=False`，属实验面，形状随时可变。
- `/api/provider-callbacks/*`——那是**上游供应商**回调本服务的方向，不是你的入口。

## 1. 三个面：什么能依赖，什么明天会变

完整分类见 `docs/API_STABILITY.md`。接之前只需要记住三档：

| 面 | 包含 | 你能依赖吗 |
|---|---|---|
| **稳定面** | `/api` 下除实验面外的全部端点；请求/响应 schema；状态码与错误信封；错误码字符串及 `category`/`retryable`；权限键；webhook 事件名与载荷结构；`X-S2V-Signature` 格式；幂等键语义；分页约定 | **能**。只增不改；删改要走弃用流程 |
| **实验面** | `GET /metrics`；HTML 页面；`GET /api/deployment` 的字段集合 | 可用，形状不承诺 |
| **不承诺面** | 表名/列名、审计 payload 的键、日志格式、`X-Request-Id` 取值格式 | **别依赖** |

**客户端的两条义务**（换上面那句承诺）：① **忽略不认识的响应字段**，不要校验「恰好这么多键」；
② **按 `code` 分支，不要按 `message`** —— `message` 是写给人看的，措辞随时会改得更通顺。

## 2. 认证：先拿到一把 API key

API key 是**唯一**的服务间凭据。签发它的端点是 `POST /api/api-keys`，需要 `apikey:manage`——
而这个权限是**租户级**的（`TENANT_ONLY_PERMISSIONS`），项目角色永远拿不到。

**这意味着引导必须是带外的**：外部服务**无法自己给自己发 key**。流程是平台方（租户 owner）签发一把、
把明文交给对方，之后全部调用都用它。

```bash
# 平台方执行（用 owner 的凭据），签发一把收窄过的 key
curl -X POST "$BASE/api/api-keys" \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "partner-crm-prod",
    "scopes": ["project:read","project:write","run:create","run:read","run:write","run:start","run:review","asset:read","asset:write"],
    "expires_in_days": 365
  }'
```

响应（`ApiKeyCreated`）：

```json
{
  "api_key_id": "apikey_...", "tenant_id": "tenant_...", "actor_id": "user_...",
  "name": "partner-crm-prod", "key_prefix": "s2v_a7b4dabfba68",
  "scopes": ["asset:read","asset:write","project:read","..."],
  "status": "active", "api_key": "s2v_a7b4dabfba68_5d9...（明文，只此一次）"
}
```

三条硬性质：

- **`api_key` 明文只在这一个响应里出现**，之后连列表（`GET /api/api-keys`）都只回 `key_prefix`。丢了只能重签。
- **`scopes` 只能收窄，不能放大**：命名一个 owner 自己没有的权限会被**拒绝**（而不是悄悄裁掉）。
  传了 `scopes` 就是一把受收窄的 key，之后每次调用都被严格限制在这张清单内。
- **吊销**是 `POST /api/api-keys/{api_key_id}/revoke`，幂等（重复吊销不报错），当月即失效。

### 权限键速查（共 23 个，标 ★ 者为租户级，项目角色永远拿不到）

| 常用键 | 作用 |
|---|---|
| `project:read` / `project:write` / `project:delete` | 项目 CRUD |
| `run:create` / `run:read` / `run:write` / `run:start` / `run:review` | 建 run / 读 / 改 / **启动** / **提交审核决策** |
| `asset:read` / `asset:write` | 素材上传、下载、签发签名链接 |
| `library:read` / `library:write` | 项目资产库（可复用素材） |
| `member:read` / `member:manage` | 成员与审计流水 |
| `quota:read` / `quota:manage`★ | 团队配额查看 / 调整 |
| `apikey:manage`★ / `webhook:manage`★ / `integration:manage`★ / `provider:manage`★ / `team:manage`★ / `deployment:manage`★ | 各自领域的管理权 |

`run:start` 与 `run:review` 是分开的：**能启动不等于能替你审**。给合作方的 key 如果不想让它自动放行审核关口，
就别给 `run:review`。

启动前先自检一次身份，能省掉后面所有 403 的猜谜：

```bash
curl -s "$BASE/api/me" -H "Authorization: Bearer $API_KEY"
# → {"actor_id":"...","tenant_id":"...","source":"api_key",
#    "permissions":["asset:read","asset:write","project:read",...]}
```

用收窄 key 调用时，`permissions` 报的是**这把 key 的实际可达集合**（不是 owner 的），所以 403 的原因一眼可见。

## 3. 调用顺序总览

```text
[带外] 平台方签发 API key ─────────────────────────────► 交给外部服务
       平台方建 webhook 订阅（可选但强烈建议）

外部服务侧，每单业务：

①  GET  /api/me                          自检身份与 scopes          （可选）
②  POST /api/projects                    建项目 → project_id        Idempotency-Key
③  POST /api/projects/{id}/runs          建 run → run_id            Idempotency-Key
④  POST /api/runs/{id}/assets            上传素材（可选）            Idempotency-Key
⑤  POST /api/runs/{id}/preflight         能力预检（追加产物）
⑥  POST /api/runs/{id}/execution-plan    执行计划 + 成本预留（追加产物）
⑦  POST /api/runs/{id}/provider-consents 付费 provider 确认（按⑥的结果决定是否要）
⑧  POST /api/runs/{id}/start             ★启动 → 停在第一个审核关口
⑨  循环：GET  /api/runs/{id}/review-requests    取 open 的那条
        POST /api/review-requests/{rid}/decisions  提交决策
        …直到 run.status == completed
⑩  GET  /api/runs/{id}/jobs              作业状态（或用 webhook）
⑪  GET  /api/runs/{id}/assets            取产物列表
    GET  /api/assets/{aid}/download      拉二进制（带 key）
    或 GET /api/assets/{aid}/access-link 签发限时链接给第三方
```

**顺序里有两处不能换**，换了一定出错：

1. **③ 建 run 必须先于 ④~⑪**：素材、预检、启动全都挂在 `run_id` 上。
2. **⑤⑥ 必须在 ⑧ 之前**：`start` 会读执行计划做闸门判断。没看过计划就启动，遇到
   `intent_downgrade_not_acknowledged`（声明了高质量模式但环境只能降级）或
   `paid_provider_confirmation_required`（要调付费 provider 但没确认）时，你手上没有任何信息决定怎么办。

其余步骤是**可选但推荐**：`⑤⑥` 不改 run 状态、可以放心先跑，提前发现「这个环境跑不了这个能力」，
而不是等启动后才在作业里看到失败。

⚠️ **一点反直觉**：`⑤⑥` 虽然是「先看一眼」性质的调用，但它们**是 POST，并且会各自追加一个产物**
（`preflight_report` / 执行计划）+ 一条事件——它们不是 `GET`。所以**别放进轮询循环**：
每次调用都会让该 run 的产物多一个版本。要看结果就直接用上一次的响应，或读
`GET /api/runs/{run_id}/artifacts`。

## 4. 逐步详解

统一约定：`BASE` 是服务地址（如 `http://<你的服务地址>:18080`）；所有请求带
`Authorization: Bearer $API_KEY`；写请求建议带 `Idempotency-Key`。

### 4.0 免凭据的发现类端点（写代码时用，不用带 key）

这些是随构建发布的**静态定义**，不含任何租户数据，所以不鉴权——正好可以拿来在写客户端时探契约：

| 端点 | 给你什么 |
|---|---|
| `GET /api/pipelines` | 流水线目录（7 条：`avatar_spokesperson`、`spokesperson_ad`、`animated_explainer`、`hybrid_story`、`localization_dub`、`screen_demo`、`source_clip_factory`），含 `status`（`production`/`beta`/`draft`） |
| `GET /api/tools`、`/api/tools/{tool_key}`、`/api/tools/capabilities`、`/api/tools/providers` | 工具目录（26 个）与能力→provider 的映射 |
| `GET /api/render-profiles`、`/api/render-profiles/{profile_key}` | 渲染档位 |
| `GET /api/error-codes` | **28 个错误码**及 `status_code`/`category`/`retryable`/`description` |
| `GET /api/webhook-events` | **6 个事件**的定义与内部来源 |
| `GET /api/run-state-transitions` | run 状态机元数据 |
| `GET /api/permissions` | 权限矩阵（23 键 × 角色） |
| `GET /api/health` | 存活探针 |

交互式文档：`/docs`（Swagger UI）与 `/openapi.json`（机器可读，SDK 生成器的输入）。

### 4.1 建项目 `POST /api/projects`

scope：`project:write`（解析口径：租户级）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `title` | ✅ | — | 项目名 |
| `description` | | `null` | |
| `default_language` | | `zh-CN` | |
| `default_aspect_ratio` | | `9:16` | |

返回项目对象，取 `project_id`。**建项目是幂等键最该用的地方**：一次网络超时重试就可能多出一个项目。

### 4.2 建 run `POST /api/projects/{project_id}/runs`

scope：`run:create`（项目级）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `raw_script` | ✅ | — | 原始口播稿 |
| `pipeline_key` | | `avatar_spokesperson` | 默认就是数字人口播流水线 |
| `brand_name` / `product_name` | | `null` | 品牌 / 产品 |
| `target_platform` | | `douyin` | |
| `target_duration_seconds` | | `15` | |
| `aspect_ratio` | | `9:16` | |
| `style_key` | | `null` | 风格预设 |
| `cost_budget` | | `null` | 成本上限；声明后预算闸门才开始工作 |
| `quality_mode` | | `null` | `local_preview` / `provider_audio` / `provider_avatar`。**声明 `provider_avatar` 就是承诺走付费数字人**，环境做不到时会触发意图降级闸门 |

`quality_mode` 是这套系统里最容易被忽略的一个字段：它把「我要什么档次的成品」变成一条可校验的声明。
不声明 = 不承诺 = 任何降级都默许；声明了 `provider_avatar` 而环境只能降级，`start` 会**拒绝**
（见 §4.6），除非你明确接受降级。

新 run 的初始状态是 `draft`，此时没有作业、没有扣费。

### 4.3 上传素材 `POST /api/runs/{run_id}/assets`（可选）

scope：`asset:write`

| 字段 | 必填 | 说明 |
|---|---|---|
| `asset_type` | ✅ | 如 `person_image`、`tts_audio`、`source_video` |
| `filename` | ✅ | 原始文件名（决定后缀推断与魔数校验） |
| `content_base64` | ✅ | **base64 编码的文件内容**（不是 URL） |
| `mime_type` | | 不传则按内容嗅探；声明与内容不符会被 415 拒 |
| `duration_ms` / `width` / `height` | | 元数据 |
| `asset_metadata` | | 自由对象，数字人链路会从这里读 `source_audio_url` 等 |

同类需求还有更专用的端点（都落在 `asset:write`）：

| 端点 | 用途 |
|---|---|
| `POST /api/runs/{run_id}/tts` | 直接生成口播音频（`text` 必填，`voice` 默认 `Cherry`，`model` 默认 `qwen3-tts-flash`） |
| `POST /api/runs/{run_id}/images` | 生成图片（`prompt` 必填） |
| `POST /api/runs/{run_id}/source-media` | 批量摄入已有素材（文件 + 外链 + 授权信息） |
| `POST /api/runs/{run_id}/music`、`/subtitles`、`/media-analysis` | 配乐 / 字幕 / 媒体分析 |

**上传有门槛，不是装饰**（V8.4）：超出 `ASSET_UPLOAD_MAX_BYTES` → 413；
声明 mp4 实际是可执行文件（魔数不符）或黑名单类型 → 415 `unsupported_media_type`。
这层**不是病毒扫描**，只挡类型错配。

### 4.4 能力预检 `POST /api/runs/{run_id}/preflight`

scope：`run:write`。**无请求体**。不推进 run 状态，可重复调用——但**每次调用都会追加一个
`preflight_report` 产物和一条事件**，所以别把它当查询端点轮询。

返回每个阶段的能力结论，以及每个能力的**意图结论**：环境是否真能兑现你在 `quality_mode` 里声明的东西。
在 `start` 之前跑一次，就能把「跑不了」变成一条读得懂的响应，而不是十几分钟后作业里的失败。

### 4.5 执行计划 `POST /api/runs/{run_id}/execution-plan`

scope：`run:write`。**无请求体**，同样**每次调用追加一个产物**。
返回选中了哪些工具、**成本预留**（金额 + 币种 + 逐项构成）、以及 `proceed_with_degradations` 这类结论。
这是你判断「要不要调用 §4.6」的依据。

预算/配额闸门在这里开始生效：声明了 `cost_budget` 的项目若预留超出预算，`start` 会以
`cost_reservation_exceeds_budget` / `team_quota_exceeded` 拒绝。

### 4.6 付费 provider 确认 `POST /api/runs/{run_id}/provider-consents`

scope：`run:start`。**仅在 ⑤⑥ 显示有付费 provider 待确认时才需要。**

| 字段 | 必填 | 默认 |
|---|---|---|
| `provider` | ✅ | — |
| `accept` | | `true` |
| `note` | | `null` |

这是「首次付费确认」：系统不替你决定要不要花钱。撤销就再发一次 `accept: false`。
没确认就 `start`，会拿到 `paid_provider_confirmation_required`。

### 4.7 启动 `POST /api/runs/{run_id}/start` ★

scope：`run:start`。

请求体可选：`{"accept_intent_downgrade": true}` —— 只在「我知道环境会降级，我接受」时才传。
不传而实际会降级时，得到 409 `intent_downgrade_not_acknowledged`。

`start` 可能被四道闸门拒绝，**每一种都是 HTTP 4xx + 稳定错误码**，都能精确分支：

| `code` | 含义 | 怎么办 |
|---|---|---|
| `intent_downgrade_not_acknowledged` | 声明了高质量模式，环境只能降级 | 接受降级（传 `accept_intent_downgrade`）或改 `quality_mode` |
| `paid_provider_confirmation_required` | 要调付费 provider，未确认 | 走 §4.6 |
| `cost_reservation_exceeds_budget` | 预留超出声明预算 | 提高 `cost_budget` 或降级 |
| `team_quota_exceeded` | 团队本期配额不足 | 提配额 / 申请一次性豁免 `POST /api/runs/{run_id}/quota-override` / 等下个窗口 |
| `review_gate` | 被审核关口挡住 | 见 §4.8 |

**启动成功 ≠ 跑完。** 典型结果是 **`status: "waiting_review"`**，`current_stage_key` 指向第一个
设了审核关口的阶段。这时人在页面里看到的是「待审批」，服务看到的是**一个必须处理的阻塞**。

> **这套流水线在 5 个阶段设了人工审核关口。** 以默认的 `avatar_spokesperson` 为例：
> `plan`（制作计划）→ `script`（口播稿）→ `storyboard`（分镜）→ `asset_review`（素材复核）→ `final_review`（成片终审）。
> 其余阶段（`intake`、`asset_generation`、`edit`、`compose`、`export`）自动流转。
> **一个跑到底的 run，会停在 `waiting_review` 五次。** 这是设计，不是缺陷——它是「成品出去之前有人看过」
> 这件事的实现方式。

### 4.8 审核关口循环 ★ 外部集成最容易漏掉的一段

**取关口：**

```bash
GET /api/runs/$RUN_ID/review-requests        # scope: run:read
```

返回该 run 的审核请求列表。找 `status == "open"` 的那条（`review_request_id`）。
列表为空而你还在 `waiting_review`，说明关口正由其他调用方持有或刚被推进——按 §4.9 等事件，别打热循环。

单条详情（`GET /api/review-requests/{review_request_id}`，scope `run:read`）会告诉你**这次要审什么**：
`artifact_scope`（被快照锁定的产物与版本）、`review_focus`（关注点）、`success_criteria`（通过标准）、
`gate_type`、`assignee_user_id`、`due_at`、`overdue`。**用这些字段决定放行还是打回**——
它们是 pipeline 快照带进来的，和你当初声明的 `quality_mode` 是同一套语义。

**提交决策（主路径）：**

```bash
POST /api/review-requests/$REVIEW_REQUEST_ID/decisions     # scope: run:review
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `decision` | ✅ | `approve` / `approve_with_changes` / `request_changes` / `reject` / `reopen` / `cancel` |
| `comment` | | 说明 |
| `reason_code` | | 机器可读的理由 |
| `artifact_refs` | | **必须指向被审阶段的当前产物**，指向阶段外的产物会被 409 `invalid_artifact_scope` 拒 |
| `action_items[]` | | `{kind, instruction, stage_key?, artifact_type?, linked_comment_ids[]}`——「带修改通过」时的待办 |
| `override_round_limit` | `false` | 绕过轮次上限 |

**便捷版（一键过阶段）：** `POST /api/runs/{run_id}/stages/{stage_key}/review`，body 只需 `{decision, comment?}`，
内部走同一套审核内核。适合「我就是要放行这一关」的自动化场景；需要精细语义（待办、产物锚定）时用上面的主路径。

三条语义值得先知道：

- **`approve_with_changes` 不阻断关口**：请求状态变 `approved`、run 继续推进，同时生成待办
  （`action_items`）挂在产物上，不会被通过吞掉。
- **一次请求只能决策一次**：已 `approved` 的请求再决策 → 409 `review_request_closed`（要 `reopen`）。
- **重跑上游会让下游审批失效**：`plan` 重跑后，已通过的 `script` 被标记失效并在 board 上暴露，
  关口按**递增的轮次**重新开出请求。别缓存 `review_request_id`——它每一轮都是新的。

**循环的终止条件**是 `GET /api/runs/{run_id}` 的 `status` 变成 `completed`（或 `failed` / `cancelled`）。
把 §4.8 写成「拿 open 关口 → 决策 → 再拿」的循环，比写死「审五遍」稳——流水线换成 beta 的
`hybrid_story` 时关口数量就变了。

### 4.9 数字人直通：只做一条数字人视频

不需要整条流水线、只要「一张人物图 + 一段口播音频 → 一条数字人视频」时，走这条捷径：

```bash
POST /api/runs/$RUN_ID/jobs/avatar-from-assets      # scope: asset:write
```

（等价端点：`POST /api/runs/{run_id}/avatar-tasks/from-assets`；另有 `/avatar-tasks` 接收外链版本。）

| 字段 | 默认 | 说明 |
|---|---|---|
| `person_image_asset_id` 或 `person_image_url` | — | 二选一 |
| `audio_asset_id` 或 `audio_url` | — | 二选一 |
| `model` | `wan2.2-s2v` | 万相-数字人 |
| `face_bbox` / `ext_bbox` | `null` | **只有 EMO 方言（`emo*`）才需要**，`wan2.2-s2v` 自己定位人物，别传 |
| `auto_archive` | `true` | 成果自动归档成本平台资产 |
| `auto_compose` | `true` | 自动合成（`compose_width/height/fps/crf/preset`、`bgm_volume`） |
| `access_link_ttl_seconds` | `3600` | 给 provider 取素材用的签名链接有效期 |

**这条链路的素材约束来自上游模型，不是本服务的限制**（`wan2.2-s2v` 官方口径）：音频 **wav/mp3、<15 MB、<20 秒**；
图片宽高各在 **[400, 7000] 像素**、须公网可访问；**同一个账号同时只能有 1 个数字人任务在处理**。
音频超过 20 秒会被上游拒——需要长口播时按 <20 秒切段，各生成一段再拼接。

### 4.10 取结果

**作业状态：**

| 端点 | scope | 用途 |
|---|---|---|
| `GET /api/runs/{run_id}/jobs` | `run:read` | 该 run 的全部作业 |
| `GET /api/jobs/{job_id}` | `run:read` | 单个作业（webhook 里的 `job_id` 拿它反查） |
| `GET /api/tool-invocations/{invocation_id}` | `run:read` | 单次工具调用的明细 |
| `POST /api/tool-invocations/{invocation_id}/poll` | `run:write` | **主动催一次** provider 状态（兜底用，别当主路径） |
| `POST /api/jobs/{job_id}/retry` | `run:write` | 重试失败的作业 |
| `POST /api/jobs/{job_id}/requeue` | `run:write` | 只有**死信**作业可重排（否则 409 `conflict`） |

`BackgroundJob` 的关键字段：`job_id`、`job_type`、`status`
（`queued` / `running` / `waiting_provider` / `completed` / `failed` / `dead` / `cancelled`）、
`progress`、`attempt` / `max_attempts`、`queue`、`priority`、`error_code` / `error_message` /
`error_category` / `error_retryable` / `error_remediation`、`result_payload`。

**`waiting_provider` 不是卡住**：这是「已提交给上游、等它出片」。数字人任务的上游耗时是分钟级，
本服务的轮询节奏约 5 分钟一轮，所以**端到端 5~15 分钟属正常**。看 `error_retryable` 而不是猜。

**产物：**

| 端点 | scope | 给谁用 |
|---|---|---|
| `GET /api/runs/{run_id}/assets` | `asset:read` | 列出产物（`Asset`：`asset_id`、`asset_type`、`storage_url`、`mime_type`、`duration_ms`、`width`、`height`、`file_size`、`checksum`、`asset_metadata`） |
| `GET /api/assets/{asset_id}/download` | `asset:read` | **自己拉二进制**，带 API key |
| `GET /api/assets/{asset_id}/access-link?expires_in_seconds=` | `asset:read` | **签发限时签名链接**给第三方 / CDN / 前端播放器 |
| `GET /api/assets/{asset_id}/content?expires=&signature=` | 见下 | 签名链接的落地端点 |

`access-link` 返回 `{asset_id, url, expires_at}`，`url` 就是带 `expires`/`signature` 的 `/content` 地址。
TTL 会被**夹到上限**（不接受十年链接），且 `app_env != local` 时若 `ASSET_LINK_SECRET` 还是内置默认值，
签发会以 503 `insecure_secret_configuration` 被**拒绝**（不是警告）。

**`/content` 的授权模型值得单独说清**，因为它是这批端点里唯一一个「凭签名而非凭席位」的：

- 它**仍在权限表内**（`asset:read`），但真正证权的是 URL 上的签名——
  签名是**bearer 能力**，签发给可能根本不持有任何身份的下游（上游供应商的抓取器、第三方播放器）。
- 它能被无凭据的下游取用，是因为无身份的请求会**回落到部署的默认身份**，而该身份在本部署里是团队 owner。
  这与租户守卫对它的显式豁免（`_SIGNATURE_AUTHORITATIVE_TEMPLATES`）是两件不同的事。
- **实践含义**：① 别把带签名的 URL 记进日志或工单，它就是一把钥匙；
  ② 别依赖「这个地址匿名可访问」做业务判断——那是部署身份的副产品，不是接口承诺。

## 5. 异步结果：webhook 优先，轮询兜底

`start` 之后你不该盯着 `GET` 打转。**订阅事件，让服务推给你。**

### 5.1 建订阅 `POST /api/webhooks`

scope：`webhook:manage`（**租户级**，见 §2）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | |
| `url` | ✅ | **只接受 http/https**；不得内嵌凭据（`https://u:p@host` 会被 400 拒） |
| `event_types` | | 留空 = 全部 |
| `description` / `status` | | `status` 默认 `active` |

响应含 `secret`（64 位十六进制）。**与 API key 不同，它每次读取都会回显**——
因为它证的是「平台 → 你」的方向，重显示不增加风险；要换用
`POST /api/webhooks/{subscription_id}/rotate-secret`。

### 5.2 六个事件

`GET /api/webhook-events`（免鉴权）拿到权威清单：

| 事件 | 何时推送 |
|---|---|
| `run.started` | run 启动 |
| `stage.waiting_review` | **某阶段进入待审** → 触发你的审核流程（§4.8） |
| `job.failed` | 作业失败 |
| `render.completed` | 渲染完成 |
| `publish.completed` | 发布完成 |
| `run.completed` | run 结束 |

`run.completed` 与 `render.completed` 是**刻意分开**的：run 可能不产出视频（停在文档产物），
渲染也可能在一个还在等人审的 run 里完成。别用其中一个替代另一个。

### 5.3 投递的格式与验签

请求体（`build_webhook_payload`）：

```json
{
  "delivery_id": "whdel_...", "event_id": "evt_...", "event_type": "stage.waiting_review",
  "run_id": "run_...", "project_id": "project_...", "sequence": 7,
  "occurred_at": "2026-10-13T10:56:23+00:00", "severity": "info",
  "message": "...", "stage_key": "plan", "data": { "…": "原始事件载荷" }
}
```

请求头：

| 头 | 用途 |
|---|---|
| `X-S2V-Signature` | `t=<unix>,v1=<hex>`，**HMAC-SHA256 over `"{t}.{body}"`**，密钥是订阅 `secret` |
| `X-S2V-Delivery-Id` | **稳定的投递 ID——用它去重** |
| `X-S2V-Event-Id` / `X-S2V-Event-Type` | 事件身份与类型 |
| `X-S2V-Attempt` | 第几次投递尝试 |

验签要点：

- **`t` 在签名串里**，所以「拒绝过旧的 `t`」就免费获得重放保护。容差默认 **300 秒**。
- **至少一次投递**：投递与事件在同一事务入队，失败按指数退避重试（基础 30 秒，上限 3600 秒）。
  所以**接收端必须幂等**，用 `X-S2V-Delivery-Id` 去重。
- 连续失败超过 `WEBHOOK_DISABLE_AFTER_FAILURES`（默认 20 次）订阅会被**自动停用**；
  修好端点后 `PATCH /api/webhooks/{id}` 把 `status` 改回 `active`（同时清零失败计数）。
- 想手工补投：`GET /api/webhooks/{id}/deliveries` 找到投递，
  `POST /api/webhook-deliveries/{delivery_id}/requeue` 重排。

验签参考实现（Python，无第三方依赖）：

```python
import hashlib, hmac, time

def verify(secret: str, header: str, raw_body: bytes, tolerance=300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, digest = int(parts["t"]), parts["v1"]
    except Exception:
        return False
    if abs(time.time() - ts) > tolerance:          # 重放保护：时间戳在签名串里
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)   # 常量时间比较
```

**注意用原始字节验签**，别先反序列化再 `json.dumps` —— 重新序列化会改变字节。

## 6. 幂等、错误与分页

### 6.1 幂等键

任何**写**请求都可以带 `Idempotency-Key: <客户端生成的唯一值>`：

```bash
curl -X POST "$BASE/api/projects" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Idempotency-Key: crm-order-88213-create-project" \
  -H "Content-Type: application/json" \
  -d '{"title":"10 月投放 - 数字人口播"}'
```

- 重放同 key 同 body → 返回**原响应** + 头 `Idempotency-Replayed: true`（活儿不重做）。
- 同 key 不同 body → 409 `idempotency_conflict`。
- 原请求还在处理 → 409 `idempotency_in_progress`（稍后重试）。
- key 过长 → 400 `bad_request`。
- **不带 key 时行为完全不变**（各自创建）——幂等是 opt-in。

**做法**：key 用「业务单据号 + 操作」派生（如 `order-88213:create-run`），而不是随机 UUID。
随机 UUID 在重试时是新 key，等于没幂等。

### 6.2 错误信封

所有失败都是同一个形状，**按 `code` 分支**：

```json
{ "detail": { "code": "team_quota_exceeded", "message": "…（给人看的）", "team_id": "team_…", "period": "calendar_month", "limits": {"cost": 25.0} } }
```

| 状态 | 典型 code | 你该做什么 |
|---|---|---|
| 400 | `bad_request` / `idempotency_conflict` | 请求本身有问题，改请求 |
| 401 | `api_key_invalid` / `unauthorized` | key 错/被吊销，换 key |
| 403 | `permission_denied` | key 的 scope 不够（响应里会写明是哪个 scope） |
| 404 | `not_found` | **注意：跨租户访问也是 404**，与「不存在」同文案同信封（T97 的刻意设计）。**不要用它探测资源是否存在** |
| 409 | `conflict` / `review_request_closed` / `idempotency_*` / 各闸门码 | 状态冲突，按 code 分别处理 |
| 413 | `payload_too_large` | 素材超上限（`ASSET_UPLOAD_MAX_BYTES`，默认 256 MiB 解码后） |
| 415 | `unsupported_media_type` | 类型错配或魔数不符 |
| 422 | `validation_error` | 字段级错误在 `detail.errors[{type,loc,msg,input}]` |
| 500 / 502 | `internal_error` / `bad_gateway` | 服务端问题 |
| 503 | `maintenance_mode` / `service_unavailable` / `insecure_secret_configuration` | **维护模式只关写、不关读**；`maintenance_mode` 是 `retryable`，退避后重试即可 |

拿到 `code` 后去 `GET /api/error-codes` 查 `retryable` 决定重试策略——**别自己猜**，
28 个码的 `category`（`request` / `identity` / `idempotency` / `gate` / `server`）与 `retryable` 都是契约的一部分。

### 6.3 分页

新版列表端点返回 `Page` 信封：

```json
{ "items": [...], "total": 137, "limit": 20, "offset": 0 }
```

部分既有端点是**裸数组**（为兼容），但它们会回填 `X-Total-Count` 头。两者都属稳定面，
按 `openapi.json` 的 schema 写客户端即可，别按「我记得是数组」写。

## 7. 端到端 curl 走查（数字人口播）

假设已拿到 `API_KEY`，`BASE=http://localhost:18080`。

```bash
BASE=http://localhost:18080
AUTH="Authorization: Bearer $API_KEY"

# ① 自检
curl -s $BASE/api/me -H "$AUTH"

# ② 建项目
PROJECT_ID=$(curl -s -X POST $BASE/api/projects -H "$AUTH" \
  -H "Idempotency-Key: demo-project-1" -H "Content-Type: application/json" \
  -d '{"title":"10月投放 - 数字人口播"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["project_id"])')

# ③ 建 run（声明走付费数字人）
RUN_ID=$(curl -s -X POST $BASE/api/projects/$PROJECT_ID/runs -H "$AUTH" \
  -H "Idempotency-Key: demo-run-1" -H "Content-Type: application/json" \
  -d '{"raw_script":"大家好，这款面霜质地轻盈……","pipeline_key":"avatar_spokesperson",
       "quality_mode":"provider_avatar","target_duration_seconds":15,
       "brand_name":"示例品牌","product_name":"轻盈面霜"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')

# ④（可选）上传人物图：content_base64 是文件内容的 base64
IMG_B64=$(base64 -i person.png)
curl -s -X POST $BASE/api/runs/$RUN_ID/assets -H "$AUTH" \
  -H "Idempotency-Key: demo-asset-1" -H "Content-Type: application/json" \
  -d "{\"asset_type\":\"person_image\",\"filename\":\"person.png\",\"content_base64\":\"$IMG_B64\"}"

# ⑤⑥ 预检 + 计划（都是只读，先看清楚再启动）
curl -s -X POST $BASE/api/runs/$RUN_ID/preflight      -H "$AUTH"
curl -s -X POST $BASE/api/runs/$RUN_ID/execution-plan -H "$AUTH"

# ⑦ 若上一步显示有付费 provider 未确认
curl -s -X POST $BASE/api/runs/$RUN_ID/provider-consents -H "$AUTH" \
  -H "Content-Type: application/json" -d '{"provider":"dashscope","accept":true}'

# ⑧ 启动（接受可能的意图降级）
curl -s -X POST $BASE/api/runs/$RUN_ID/start -H "$AUTH" \
  -H "Content-Type: application/json" -d '{"accept_intent_downgrade":true}'
#    → 预期 status: waiting_review, current_stage_key: plan

# ⑨ 审核循环：把这段包成 while，直到 run.status == completed
curl -s "$BASE/api/runs/$RUN_ID/review-requests" -H "$AUTH"     # 取 status=open 的那条
curl -s -X POST $BASE/api/review-requests/$REVIEW_REQUEST_ID/decisions -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"自动放行：符合投放规范"}'

# ⑩⑪ 结果
curl -s "$BASE/api/runs/$RUN_ID"        -H "$AUTH"    # status / current_stage_key
curl -s "$BASE/api/runs/$RUN_ID/jobs"   -H "$AUTH"    # 作业与 error_code
curl -s "$BASE/api/runs/$RUN_ID/assets" -H "$AUTH"    # 产物列表
# 自己拉二进制
curl -s "$BASE/api/assets/$ASSET_ID/download" -H "$AUTH" -o out.mp4
# 或签发限时链接交给别人
curl -s "$BASE/api/assets/$ASSET_ID/access-link?expires_in_seconds=3600" -H "$AUTH"
```

## 8. 上线前建议做的四件事

1. **给 key 收窄 scope**，别用 owner 的全权 key。合作方只需出片就不给 `run:review`，
   审核关口自然交回人工。
2. **订阅 `stage.waiting_review` + `job.failed` + `run.completed`** 三个事件起步。
   少了 `job.failed` 你只会看到「跑了一半没动静」。
3. **所有写请求带派生式幂等键**（业务单据号 + 操作）。
4. **用 `GET /api/deployment` 做版本对齐**：它报 `app_version` 与 `migration.up_to_date`。
   你的客户端对版本有要求时，这是唯一一处「只读就问得到」的答案。

## 9. 已知边界（别踩）

- **跨租户访问返回 404，不是 403**。这是刻意的（避免 403 泄露「这行存在」），
  所以**不能用 403/404 区分「无权」和「不存在」**。
- **外部服务不能自己给自己发 API key / 建 webhook**：`apikey:manage` 与 `webhook:manage` 都是租户级。
  引导必须带外完成一次。
- **一条 run 的审核关口数量由 pipeline 决定**，别把「五个关口」写死在代码里。
  `avatar_spokesperson` 继承自 `spokesperson_ad`；换 pipeline 前先 `GET /api/pipelines` 看 `status`。
- **数字人链路的上游约束**（音频 <20 秒 / <15 MB、图片 400~7000px、同账号并发 1）来自 `wan2.2-s2v` 本身，
  不随本服务版本变化，但会随上游调整。以供应商文档为准。
- **被拒的决策不落库**：`invalid_artifact_scope` 这类 409 之后关口仍然 `open`，可以改对再提交。
- **`maintenance_mode` 只关写**：读、健康探针、provider 回调照常。收到 503 时退避重试，不要熔断整个集成。
- **签名素材链接依赖部署的默认身份**：`ASSET_LINK_SECRET` 是签名链接的一部分，**轮换它会让所有在途链接立即失效**
  （数字人任务提交后若密钥被换，上游就取不到已交付的素材 URL）。同一把密钥还决定 `/content` 能否被无凭据下游取到——
  若把默认身份改成未入座的身份，供应商抓取会 403，数字人链路会断在「取素材」这一步。这是部署侧要留意的耦合。

## 10. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/API_STABILITY.md` | 三个面与兼容规则、弃用流程、版本号含义 |
| `docs/DEPLOYMENT.md` | 部署、迁移、维护模式、备份恢复、故障排查 |
| `docs/CURRENT_CONTEXT.md` | 当前能力清单与里程碑状态 |
| `GET /openapi.json` | 权威的端点与 schema（本文档的端点清单即由它导出） |
| `GET /api/error-codes` | 28 个错误码及 `retryable` |
| `GET /api/webhook-events` | 6 个事件定义 |
