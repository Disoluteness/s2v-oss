# 版本说明

本文件记录仓库的发布版本，对应 git tag 与 GitHub / Gitee Release。逐条变更明细见 [`CHANGELOG.md`](../CHANGELOG.md)。

> 服务端会另报一个 API 版本（`GET /api/deployment` 的 `app_version`，当前 `1.1.0`），
> 它描述的是**对外接口的形状**，只在端点、字段、错误码变化时前进。
> `1.0.3` 在选择器的输出里新增七维明细字段，全部为纯增量——既有字段（含旧的
> 六键 `score_breakdown`）一字未动，按 `docs/API_STABILITY.md` 的分类属于
> 「无需通知」的一栏，因此 API 版本仍是 `1.1.0`。

---

## 1.0.3 — 未发布

**选择器可解释化：把「为什么是它」写进输出。**

现有选择器能把候选筛对（资格筛选 + 质量分排序），但总分说不清「为什么是它」。
本版把打分重组成七个可解释的维度，每条候选都带维度明细与最强维度，
决策日志（1.0.1 引入）直接引用这份明细——审计时不用再翻执行计划产物。

### 七个维度

| 维度 | 判什么 | 来源 |
| --- | --- | --- |
| `task_fit` 任务契合 | 用户显式指定的工具 / 供应商偏好 + 质量模式偏置 + `best_for` 命中 | 既有信号重组 |
| `quality` 输出质量 | 质量档位（production 140 → preview 35） | 既有信号重组 |
| `control` 控制能力 | `deterministic`（同输入同输出，可复现可干预） | 从 quality 拆出 |
| `reliability` 可靠性 | 工具可用性（available 600 / degraded 350 / 不可用 -1000） | 从 availability 改名 |
| `cost` 成本 | 预估成本越低分越高 | 既有信号重组 |
| `latency` 延迟 | 预期延迟越低分越高 | 既有信号重组 |
| `continuity` 连续性 | 与 run 已声明的供应商保持一致（视觉一致性） | **新增** |

### 兼容承诺（这是本版最重要的一句话）

重组**只分组、不改值**：默认权重（全 1.0）下，七维加权和与旧六键总分**逐位相等**，
排序与选择结果不变。旧的 `score_breakdown` 六键原样保留——按旧文档写的客户端
看到的数字与升级前完全一致。新增的只有 `dimension_scores`（七键）、
`strongest_dimension`、`dimension_weights`，出现在工具候选、执行计划选项与
决策日志候选里。`selector_version` 升到 `1.1`。

### 权重可配置

run 输入带 `dimension_weights`（dict）即可按维度调整或关闭（权重 0 = 该维度
不参与总分，但信号仍在 `dimension_scores` 里照算，供审计对照）。未知键被忽略，
拼错维度名不会静默改变选择。

### 连续性维度

run 输入可声明 `continuity_provider`（单一供应商贯穿多个能力，+90）或
`continuity_providers`（按能力映射，+45）。不声明则恒为 0——既有选择不受影响。

### 未变更项

不新增表、不新增迁移、不新增端点、不新增错误码。`RunCreate` **新增三个可选字段**
（`dimension_weights` / `continuity_provider` / `continuity_providers`，纯增量，
不传即与旧行为完全一致）；既有 JSON 字段与数值一字未动。

---

## 1.0.2 — 2026-11-16

**渲染前承诺校验：把「带动画的 PPT」挡在渲染前。**

一条 run 的计划可以描述素材永远交付不了的运动——「动感、运镜丰富」的片子如果
几乎全由静态图拼成，渲染出来就是幻灯片。等人在成片里发现问题，唯一的修复方式
是重新计划。本版在合成之前把「计划声称的」与「素材能支撑的」对一遍，
代价只是让人类确认一次。

### 六个信号

全部从 run 已有的产物推导（shot_list / execution_plan / edit_plan / media_analysis），
不引入新模型、不新增二进制依赖：

| # | 信号 | 判什么 | 最高严重度 |
| --- | --- | --- | --- |
| 1 | `static_shot_share` | 静态图镜头占比 vs 声称的运动期望 | critical |
| 2 | `avg_shot_duration` | 平均镜头时长过长 + 声称运动为主 | critical |
| 3 | `missing_intent_share` | 镜头没有任何运镜 / 转场说明的比例 | warning |
| 4 | `text_screen_share` | 字幕 + 标题承载的画面时间占比 | warning |
| 5 | `duplicate_composition_share` | 相邻镜头复用同一画面的比例 | warning |
| 6 | `shot_density` | 镜头数 / 目标时长比不足以支撑宣称的节奏 | critical |

阈值随信号写在响应里（`observed` / `threshold`），审核者不需要重新推导结论。

### 闸门行为

- critical 信号存在时，`POST /api/runs/{run_id}/compose` 返回
  **409 `pre_render_check_not_acknowledged`**，`detail.signals` 携带全部证据；
- 同一请求带 `accept_quality_risk: true` 重发即放行——确认是人给的，不是重试；
- warning 永不拦截，只出现在检查产物里；
- 每次尝试都落一份 `pre_render_check` 产物：`blocked`（被拦）、
  `confirmed_with_risk`（接受风险后渲染）、`pass`（无 critical）。被拦的尝试也留证据；
- 确认接受风险写进决策日志（类别 `pre_render_risk`，与既有四类并列；
  人工裁决的置信度保持为空，与 P2 的约定一致）；
- 后台合成 job 走同一个闸门：被拦时 job 失败、错误码为闸门码、**不自动重试**——
  重试解决不了需要人的决定。

### 不会误报的约定

产物给不出结论时**不出信号、不拦截**：没有镜头清单、素材类型不可知、计划没有
声明运动期望，对应信号直接缺席。声称的运动期望必须由计划显式给出
（execution_plan 的 `motion_expectation`），不从被判断的镜头里反推——
用被审者自己的话审自己，检查就形同虚设。错误码目录 28 → **29**。

### 未变更项

- 不新增表、不新增迁移、不新增工具目录条目、不改变既有 JSON 字段与错误码。
- 闸门挂在 `compose_video` 的 ffmpeg 可用性检查之后、预算闸门之前，既有闸门顺序不变。

### 升级

无数据库变更。重建 api 与 worker（两侧都含闸门代码）：

```bash
docker compose build api worker
docker compose up -d api worker
```

升级后确认：

- `GET /api/error-codes` 含 `pre_render_check_not_acknowledged`（409 / gate / 不可重试）；
- `GET /openapi.json` 的 `ComposeVideoRequest` 含 `accept_quality_risk` 字段；
- 既有 run 的合成行为不变：没有矛盾信号的 run 渲染照旧，无新产物、无新决策记录。

### 已知限制

| 限制 | 说明 |
| --- | --- |
| 六个信号依赖产物字段齐全 | 产物缺字段时信号静默缺席（不误报），意味着覆盖随上游产物质量浮动 |
| 「声称的运动期望」目前只能由计划显式声明 | 上游未声明时，信号 1 / 2 / 6 退化为提示而非拦截 |
| 阈值是全局常量，不按产线区分 | 纪录片式的长镜头产线需要未来按 pipeline 覆盖阈值 |
| 部署门禁无法演练 critical 分支 | 公开 API 不能注入矛盾产物，拦截行为由仓库测试证明；部署门禁验证词汇与确认字段已发布 |
| 重复构图用文本 / 资产引用判等 | 画面级相似（同一素材不同裁切）检测不到 |

---

## 1.0.1 — 2026-11-09

**供应商密钥可以在网页上维护；新增三条素材剪辑流水线，以及可审计的决策日志。**

这一版把三件原本只能从代码或 `curl` 里读出来的事搬到了台面上：
凭据在哪里维护、素材按什么标准选、以及选择背后的理由。

### 新增：供应商密钥页

更换 `DASHSCOPE_API_KEY` 这类密钥，不必再改 `.env` 并重启进程。
凭据可以存放在数据库中（`/api/provider-credentials` 提供新增、更新、查询、检查与加密托管），
且**库内凭据优先于环境变量**，保存即生效。本次把这个能力接入界面。

`/settings` 页面（供应商密钥），能力如下：

| 能力 | 说明 |
| --- | --- |
| 列表 | 供应商、备注、状态、密钥掩码、存储方式（明文 / 加密托管）、上次检查结果 |
| 新增 | 选供应商自动带出变量名（`dashscope` → `DASHSCOPE_API_KEY`，`elevenlabs` → `ELEVENLABS_API_KEY`，`kling` → 两个变量），也可自定义 |
| 改密钥 | 复用同一表单，只填要换的变量，留空表示保持不变 |
| 启停 | 一键停用 / 启用，停用后该供应商的工具降级为 `unavailable` |
| 可用性检查 | 显示结论、变量名、缺失依赖、受影响工具状态 |
| 加密托管 | 配合 `SECRET_ENCRYPTION_KEY` 把明文凭据迁入加密存储 |

仪表盘工具栏同时新增「供应商密钥」入口。

#### 供应商密钥：通过 API 维护

同一套能力也提供 HTTP 接口，便于脚本化、批量初始化或在 CI 中执行。所有请求需要 `provider:manage`
权限，身份放在 `Authorization: Bearer <api_key>`：

| 操作 | 端点 |
| --- | --- |
| 列表（可按 `provider` / `status` 过滤） | `GET /api/provider-credentials` |
| 新增 | `POST /api/provider-credentials` |
| 查看单条 | `GET /api/provider-credentials/{credential_id}` |
| 更新：换密钥、改备注、启停 | `PATCH /api/provider-credentials/{credential_id}` |
| 可用性检查 | `POST /api/provider-credentials/{credential_id}/check` |
| 迁入加密存储 | `POST /api/provider-credentials/{credential_id}/rotate` |

凭据只支持停用，**没有删除端点**——历史记录保留，便于审计。

```bash
BASE=http://localhost:18080       # compose 默认 18080；本机 make api 直接跑则是 8080
AUTH="Authorization: Bearer $S2V_API_KEY"

# 新增：provider 必填，secret_values 的键名必须与工具目录一致
curl -s -X POST "$BASE/api/provider-credentials" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"provider":"dashscope","label":"primary","secret_values":{"DASHSCOPE_API_KEY":"sk-..."}}'

# 换密钥：只传要改的变量，其余保持不变
curl -s -X PATCH "$BASE/api/provider-credentials/<credential_id>" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"secret_values":{"DASHSCOPE_API_KEY":"sk-..."}}'

# 停用 / 启用
curl -s -X PATCH "$BASE/api/provider-credentials/<credential_id>" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"status":"disabled"}'

# 检查配置完整性：该供应商有哪些工具就绪、缺什么依赖
curl -s -X POST "$BASE/api/provider-credentials/<credential_id>/check" -H "$AUTH"
```

几点读响应时要知道：

- `secret_preview` 只返回掩码（如 `{"DASHSCOPE_API_KEY": "sk-...4f60"}`），完整密钥不会回显。
- `secret_source` 说明密钥材料存在哪里：`inline`（库内明文）、`vault`（加密存储）、`mixed`（迁移未完成）、
  `locked`（当前密钥解不开）、`none`。
- `/check` 校验的是**配置完整性**，不是密钥能否被供应商接受——它按解析后的密钥重建一次工具目录，
  在 `tool_statuses` 里报告工具状态与缺失依赖，**不发起真实的网络请求**。
- `/rotate` 需要先配置 `SECRET_ENCRYPTION_KEY`；未配置时返回
  `503 insecure_secret_configuration`。这是提示而非故障，明文凭据照常工作。

访问 `/settings` 需要 `provider:manage` 权限（团队 owner 或 admin）。

### 新增：三条素材剪辑流水线

此前的产线偏「生成」：写脚本、配图、配音、合成。输入已有素材的只有
`source_clip_factory` 一条通用线，它回答的是「哪一段有意思」。本次补三条目标明确的线，
共用同一套 ingest → analysis → clip_selection → edit → export 内核，差别在**选片判据**：

| 流水线 | 输入 | 选片判据 | 硬要求 |
| --- | --- | --- | --- |
| `talking_head` | 真人出镜素材（访谈、课程、vlog） | 观点是否完整、开头是否有钩子、口误与停顿的密度 | 转写与字幕（按口播格式传播，默认静音观看） |
| `podcast_repurpose` | 长音频（播客、访谈录音） | 片段能否独立成立、金句密度、是否从句首开始 | 转写（没有真实转写就无法选片，占位转写直接判失败） |
| `cinematic` | 素材 + 音乐（品牌片、预告） | 情绪契合度、画面强度、与节拍的贴合 | 无（调色为可选，缺失时按平色记录） |

三条线均为 `beta`，`GET /api/pipelines` 的目录从 7 条变为 10 条。

### 新增：决策审计日志

新端点 `GET /api/runs/{run_id}/decision-log`，四个写入点：

| 记录什么 | 触发时机 | 谁 |
| --- | --- | --- |
| 工具选择 | 执行计划生成 / 重算 | 系统 |
| 审核裁决 | 关口上提交通过、打回、重开 | 审核人 |
| 质量降级接受 | 启动时接受低于声明画质的运行 | 操作人 |
| 首次付费确认 | 确认或撤销某供应商的首次付费调用 | 操作人 |

此前一个 run 出问题时，能从系统里读到的只有两件事：发生了什么（`event_logs`）、
谁改了什么（`audit_logs`）。真正被追问的第三件事——「为什么这里选了 A 而不是 B」——
过去只存在于执行计划的 JSON 里，计划被重算一次就没了。现在每个选择连同它的
备选与理由一起落库。

每条记录带四样东西：**选了什么**（`chosen`）、**放弃了什么**（`candidates`，
含各自的分数与排序位）、**差距有多大**（`confidence`）、**为什么**（`rationale`）。
被跳过或被阻塞的能力同样有记录——「这条片子为什么没有背景音乐」的答案在跳过项里，
不在成功项里。

关于 `confidence`：它是**差距度量，不是概率**。`0.0` 表示与次优并列，`1.0` 表示没有
对手，`confidence_basis` 会写明用的是哪一种度量（`score_margin` / `uncontested`）。
人工裁决的这条字段保持为空——审核人给出的是结论，不是偏好排序，
造一个数字会让人误读成系统对判断的置信度。

读取语义：

- 默认视图按 `(category, subject)` 去重，回答「这个 run 现在相信什么」；
- `include_history=true` 返回全部追加记录，回答「它中途改过什么主意」；
- `category=` 只收窄条目，返回的 `categories` 始终按整本账计算（否则筛选片会被自己筛掉）；
- `count` 是本次返回条数、`total` 是同一查询在库里的总数，`truncated` 表示被 `limit` 截断
  ——沉默的截断会被读成完整；
- 只有 `GET`。一份能被编辑的记录不是证据。

### 未变更项

- 不新增工具目录条目：三条线只用既有能力（`media_analysis` / `speech_to_text` /
  `clip_selection` / `subtitle_generation` / `clip_export`）。
- 已有 7 条流水线的定义一字未动。
- 不改变任何既有 JSON 字段与错误码。

### 数据层变更

新增一张表 `run_decisions`（migration `20261109_0023`，全库 36 → 37 张表）。
upgrade → downgrade → upgrade 往返已验证。

### 升级

```bash
docker compose build api worker
docker compose run --rm api alembic upgrade head
docker compose up -d api worker
```

迁移必须用 `run --rm`（`exec` 跑的是**旧镜像里**的迁移目录，会提示「已是最新」却什么也没做）。
api 与 worker 一并重建：两侧共用同一份存储层代码，只升一侧会让进程间的代码版本对不上。

### 升级后确认

- `/settings` 能看到当前生效的凭据（密钥以掩码显示）。停用其中一条后，该供应商的工具变为
  `unavailable`，启用后恢复——整个过程不需要重启服务。
- `GET /api/pipelines` 返回 10 条，其中 `talking_head` / `podcast_repurpose` / `cinematic`
  的 `status` 为 `beta`，且每条都带 `stages` 与 `entrypoint_stage_key`；
- `GET /api/deployment` 的 `migration.head` 为 `20261109_0023`、`pending` 为空、
  `table_count` 为 37；
- 随便挑一个跑过的 run 请求 `GET /api/runs/{run_id}/decision-log`，`count` 应大于 0，
  且选中的条目带 `candidates` 与 `rationale`。

### 已知限制

| 限制 | 说明 |
| --- | --- |
| 凭据只能停用，没有删除端点 | 历史记录保留以便审计，停用后密钥仍在库内 |
| `/check` 不发起真实网络请求 | 它校验的是配置完整性，密钥是否被供应商接受要等真实调用才知道 |
| 三条线都没有人脸检测与说话人分离 | 构图与说话人标签是人工决策，不是自动跟踪结果 |
| `cinematic` 暂无自动调色 | 调色工具尚未提供，计划里记录平色，不会假装已调色 |
| 无 21:9 渲染配置 | 宽幅输出用 16:9 加信箱边，并在导出元数据里记录 |
| 静音裁剪基于音频能量时间线，不是 VAD 模型 | 长环境停顿可能残留，需人工微调 |
| 升级前产生的 run 没有决策日志 | 日志只在写入点追加，不做历史回填；老 run 的 `count` 为 0 |
| 决策日志不记录「谁重跑了阶段」 | 那是 `audit_logs` 的职责，两本账刻意分开，不互相复制 |
| 超长 run 需要按 `category` 分批读 | 单次上限 200 条且无分页游标；被截断时响应中的 `truncated` 为真、`total` 给出真实条数 |

---

## 1.0.0 — 2026-11-03

**首个开源版本。**

### 包含内容

- **流水线**：7 条（其中两条标记 `beta`），工具目录 26 个工具。
- **审核关口**：五道人工关口（plan / script / storyboard / asset_review / final_review），
  批准即锁定产物版本，决策只增不改，重跑作废下游旧关口。
- **对外接口**：124 个路径 / 156 个操作；统一失败信封 `{"detail": {code, message, ...}}`；
  API key 鉴权、幂等键、Webhook（HMAC 签名）、嵌入工作台（无状态签名令牌）。
- **多租户与权限**：23 个权限键（其中 7 个租户级）、团队配额、租户范围守卫。
- **数据层**：36 张表，迁移 head `20261012_0022`。
- **测试**：812 项。
- **接入面**：REST API、Python / TypeScript SDK、`s2v` CLI、Webhook、可嵌入工作台，
  以及 `skills/s2v-pipeline/`（面向 AI 编程工具的流水线技能）。
- **运维**：部署 runbook、预检与门禁脚本、备份 / 恢复脚本。
- **许可**：Apache License 2.0，clean-room 实现。

### 已知限制

| 限制 | 说明 |
| --- | --- |
| 未配置供应商密钥时，相关工具显示 `unavailable`，run 停在审核关口 | 设计行为，不是故障 |
| 数字人成本估算返回 0 | 预算闸门对该阶段暂无保护，配额维度不受影响 |
| `wan2.2-s2v` 要求音频 < 20 秒且 < 15MB，平台不自动切段 | 长脚本需调用方预切 |
| `/api/assets/{id}/download` 对 wav 可能返回 500 | 改用 `/api/assets/{id}/content` 签名链接 |
| 供应商 4xx 统一归类为 `input.invalid_or_missing` | 无法从错误码区分「参数错」与「密钥错」 |
| HTML / Canvas 预览是预览渲染器 | 最终 MP4 编码始终走 FFmpeg |

以上与 README「已知边界」一节保持一致。
