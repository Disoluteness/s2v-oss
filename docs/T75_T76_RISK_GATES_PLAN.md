# T75 / T76 风控闸门实施规划

- 文档版本：v1.0
- 编写日期：2026-10-08
- 状态：**已完成（T75 + T76 均已实现并验证）**
- 上位文档：`docs/PLATFORM_PLAN.md`（第 12 节执行方案 / 第 14 节成本治理）、`docs/FULL_VERSION_ROADMAP.md`（V2.2 Selector 与 Fallback、V8.3 Quota / Billing）
- 任务板：`docs/TASK_BOARD.md` T75、T76
- 来源：T74 实施过程中暴露的相邻缺口

---

## 1. 目标与验收标准

任务板原文：

> **T75**：preflight / execution plan / stage manifest 在选择 provider 失败或能力缺失时，必须显式产出 blocker 或 warning，禁止静默降级到低成本路径；Web 需可见降级原因。
>
> **T76**：run 启动前按 execution plan 预留预算；首次触达真实付费 provider 前要求人工确认；超预算时阻断派发并给出结构化原因。

两个任务共用一句话：**系统不能在用户不知情的情况下改变"要花多少钱"和"得到什么质量"这两件事。**

可验收的行为：

1. run 创建时可以声明质量意图（`quality_mode`），意图随后被 preflight / execution plan / 派发链路一路携带。
2. 当实际选中的工具偏离声明的意图时，preflight 与 execution plan 必须显式记录偏离项（期望 vs 实际 + 原因），而不是把该能力记为"可用"。
3. 存在未确认的意图偏离时，`POST /runs/{run_id}/start` 必须阻断并返回结构化原因；用户确认后放行，并留下审计记录。
4. run 启动时按 execution plan 预留预算；预留额超出 `cost_budget` 时阻断启动并给出结构化原因。
5. 首次调用真实付费 provider（`estimated_cost > 0` 的 API 工具）前必须有人确认；未确认就派发要被拒绝，并说明是哪个 provider、预计多少钱。
6. 三项阻断原因都在 Web 上可见（工作台 + Run Board），不需要读日志。

## 2. 现状核查（缺口的确切位置）

### 2.1 质量意图根本没有入口（T75 的根因之一）

`RunCreate`（`apps/api/app/models.py:328`）只有 pipeline、脚本、品牌、平台、时长、画幅、样式和 `cost_budget`，**没有 `quality_mode`**。

`quality_mode` 只存在于 `MvpSampleRequest`（`models.py:1148`），属于 MVP 样片那条独立链路。

而 selector 是按 `run_input.get("quality_mode")` 打分的（`packages/tool_kit/selector.py:261-269`）：

```python
if quality_mode == "provider_avatar" and capability == "avatar_video":
    score += 120.0 if runtime == RuntimeType.API else -60.0
if quality_mode == "provider_audio" and capability in {"tts", "music_generation"}:
    score += 80.0 if runtime == RuntimeType.API else -30.0
```

普通 run 的 `input_payload` 由 `RunCreate.model_dump()` 组成（`db_store.py:666-670` 的 `copy_run` 是同一份结构），因此这些分支对普通 run **永远不生效**。

### 2.2 能力状态只看工具自身，不看是否满足意图（T75 的根因之二）

`packages/tool_kit/preflight.py:114-119`：

```python
if recommended and recommended.status == ToolStatus.AVAILABLE:
    status = "available"
elif recommended and recommended.status == ToolStatus.DEGRADED:
    status = "degraded"
else:
    status = "unavailable"
```

`status` 完全由被选中工具的 `ToolStatus` 决定。后果：

- 用户要求 provider 音质，但 provider key 未配置 → 该工具 `unavailable` → selector 选中本地 TTS（`available`）→ capability 记为 **`available`**。
- execution plan 于是给出 `decision = "use_tool"`、`execution_policy = "proceed"`。
- 整条链路（preflight / execution plan / run board）都显示正常，用户拿到的是本地模拟音，**没有任何地方提示降级**。

这正是 T75 要禁止的"静默降级到低成本路径"。

### 2.3 预算只在派发时检查，且默认不设防（T76 的根因）

`_assert_budget_allows`（`db_store.py:6556-6578`）：

```python
budget_limit = _cost_budget_limit(run.input_payload)
if budget_limit is None:
    return                      # 未配置预算 = 完全放行
current = self._usage_summary_for_run(run)
projected = max(current.estimated_cost, current.actual_cost) + max(0.0, estimated_cost or 0.0)
if projected > budget_limit:
    raise ValueError(...)        # 裸异常，无结构化 code
```

三个问题：

1. **没有预留概念**：`start_run` 传的是 `estimated_cost=0.0`（`db_store.py:940-944`），启动时不校验整体花费；等到某个步骤实际派发时才逐步发现超支，此时前面已经花了钱。
2. **默认不设防**：`cost_budget` 为空时直接放行，任何付费 provider 都可能被调起。
3. **阻断原因不可编程**：`ValueError` 经 `routes.py:1000-1001` 变成 `400 {"detail": "<字符串>"}`，前端只能展示原始英文串，无法按 code 分流，也没有"预计花费 / 剩余预算"结构。

### 2.4 首次付费确认完全缺失（T76 的核心）

全仓库没有"付费 provider 确认"相关状态；`ToolCostModel.estimated_cost`（`packages/tool_kit/contracts.py:61-65`）只用于打分与 usage ledger，没有任何闸门读它来决定"是否允许首次真实付费"。

## 3. T75 语义设计：意图一致性（intent consistency）

### 3.1 意图的来源

在 `RunCreate` 增加与 MVP 同名的字段，让普通 run 也能声明意图：

```python
quality_mode: str | None = Field(default=None, pattern="^(local_preview|provider_audio|provider_avatar)$")
```

语义（沿用既有能力，不发明新枚举）：

| `quality_mode` | 期望 runtime | 覆盖能力 |
|---|---|---|
| `local_preview` | `local` | 全部能力 |
| `provider_audio` | `api` | `tts`、`music_generation` |
| `provider_avatar` | `api` | `avatar_video` |
| `None`（未声明） | — | 不产生任何意图断言，保持向后兼容 |

### 3.2 判定规则

新增纯函数 `capability_intent(capability, run_input)` 与 `ToolSelectionResult.intent_satisfied`：

- 未声明意图，或该能力不在意图覆盖范围 → `intent_satisfied = True`（不打扰）。
- 声明了意图且**选中工具存在**，但 `runtime` 与期望不符 → `intent_satisfied = False`，`intent_mismatch_reason` 写明"期望 api，实际 local，原因是所期望的 provider 工具不可用（缺依赖 X）"。
- 能力完全不可用（没有选中工具）→ 仍走既有的 `unavailable` / `blocked` 路径，不重复记意图偏离。

只对 `local ↔ api` 的**跨层**偏离判定。同层换 provider（例如两个 API provider 之间）不算违背意图，只在 `fallback_reason` 里说明，避免把正常容错也变成阻断。

### 3.3 三级暴露

| 层 | 字段 |
|---|---|
| preflight capability report | `intent`、`intent_satisfied`、`intent_mismatch_reason` |
| preflight stage report | `intent_mismatch_capabilities`、`intent_warnings` |
| execution plan decision | `intent`、`intent_satisfied`、`intent_mismatch_reason`，并在 `decision` 上新增取值 `use_tool_below_intent` |
| execution plan 顶层 | `intent_downgrades`（结构化清单）、`requires_intent_acknowledgement`（bool） |

`execution_policy` 新增 `proceed_with_intent_downgrades`，优先级高于 `proceed_with_degradations`：

```
blocked > proceed_with_intent_downgrades > proceed_with_degradations > proceed
```

**`can_execute` 不受影响**（仍为 True）。意图偏离是"需要你确认"，不是"不能做"。

### 3.4 启动门禁

`start_run` 在预算校验之前插入意图检查：

- `requires_intent_acknowledgement == True` 且本次启动未带确认 → 抛 `RunGateError`，`code = "intent_downgrade_not_acknowledged"`，`details` 带完整偏离清单，HTTP 400。
- 确认途径：`POST /api/runs/{run_id}/start` 接受可选请求体 `{"accept_intent_downgrade": true}`。
- 确认后写入 `run.intent_downgrade_acknowledged`（input_payload）+ `run.intent_downgrade_acknowledged` 事件，Run Board 可见"本次运行已确认接受降级"。

## 4. T76 语义设计：预算预留与付费确认

### 4.1 预算预留

新增 `build_cost_reservation(execution_plan)`：累加每个 stage 选中工具的 `cost_model.estimated_cost`，得到 `reserved_cost`，连同 `currency` 与逐项明细写入 execution plan 的 `cost_reservation` 字段。

`start_run` 时：

- `cost_budget` 未配置 → 不阻断，但 event 与 execution plan 都标记 `budget_status = "not_configured"`（**可见**，替代当前的静默放行）。
- `reserved_cost > cost_budget` → 抛 `RunGateError(code="cost_reservation_exceeds_budget")`，`details` 带 `reserved_cost` / `budget_limit` / 超支明细。

### 4.2 首次付费确认

"付费 provider" = `cost_model.estimated_cost > 0` 且 `runtime_type == api` 的工具。

- 派发前（`_assert_budget_allows` 的同一位置）检查该 run 是否已确认过这个 provider。
- 未确认 → 抛 `RunGateError(code="paid_provider_confirmation_required")`，`details` 带 `provider` / `tool_key` / `estimated_cost` / `capability`。
- 确认途径：`POST /api/runs/{run_id}/provider-consents`（体 `{"provider": "...", "accept": true}`），写入 `run` 的 input_payload（`paid_provider_consents`）+ event。
- 粒度：**run 级**。跨 run 记忆需要项目级/团队级设置，属 V8.3，本任务不做。

### 4.3 结构化阻断原因

新增 `RunGateError(code, message, status_code=400, details)`，与审核体系的 `ReviewGateError` 同构；`routes.py` 统一映射为 `{"detail": {"code", "message", ...details}}`。现有裸 `ValueError` 路径保持兼容（仍是 400 字符串）。

## 5. 数据与接口变更

| 变更 | 类型 | 说明 |
|---|---|---|
| `RunCreate.quality_mode` | 新增可选字段 | 意图入口，默认 `None` 保持兼容 |
| `POST /runs/{run_id}/start` | 请求体可选 | `{"accept_intent_downgrade": bool}` |
| `POST /runs/{run_id}/provider-consents` | 新端点 | 付费 provider 确认 |
| `preflight_report` / `execution_plan` payload | 新增字段 | 见第 3.3 / 4.1 节，均为增量字段 |
| `RunGateError` | 新增 | 结构化闸门错误 |

**不需要 migration**：确认状态存在 `run.input_payload` 与 event log，与既有 `style_preset_snapshot` / `cost_budget` 的做法一致。

## 6. 实施顺序

**第一批（T75）**

1. `selector.py`：`capability_intent` + `ToolSelectionResult` 意图字段。
2. `preflight.py`：capability / stage report 暴露意图结论与 `intent_warnings`。
3. `execution_plan.py`：decision 取值、`intent_downgrades`、policy 优先级。
4. `models.py`：`RunCreate.quality_mode`、start 请求体、`provider-consents` 请求体。
5. `db_store.py`：`RunGateError` + `start_run` 意图门禁 + 确认落库与事件。
6. `routes.py`：错误映射 + start 请求体 + 新端点。
7. Web：workspace 与 run-board 展示偏离项与确认入口。
8. 测试：selector / preflight / execution plan / start 门禁 / Web 标记。

**第二批（T76）**

9. `execution_plan.py`：`cost_reservation`。
10. `db_store.py`：预留校验、付费 provider 确认闸门、`_assert_budget_allows` 结构化错误。
11. `routes.py` + Web：确认入口与展示。
12. 测试：预留超支、未确认付费 provider 被拒、确认后放行。

**收尾**

13. 部署门禁脚本扩展：`scripts/verify_review_loop.py` 增加一节覆盖 T75 / T76 的阻断与确认路径。
14. 文档：`PLATFORM_PLAN.md`、`CURRENT_CONTEXT.md`、`TASK_BOARD.md`、验证基线。

## 7. 明确不做的事

- 不做项目级 / 团队级预算与付费确认（V8.3 Quota / Billing）。
- 不做 provider key 加密（V8.4 Security）。
- 不引入"意图"新枚举，只复用既有 `quality_mode` 三个取值。
- 不改变 `blocked` 的语义：能力完全不可用仍是硬阻断，与"意图偏离但可用"区分开。
- 不在本任务内实现 `preferred_providers` 的 Web 输入界面（selector 已支持该字段，留给后续）。

---

## 8. 实施结果与偏差（2026-10-08 完成）

14 个步骤全部落地。与原设计的差异只有三处，均为实现中发现的必要修正：

1. **新增 `capability_missing` 偏离类型**（第 3.2 节原设计只区分"选中工具 runtime 不符"）。
   实测发现 `provider_audio` 声明的能力在无 key 环境下**完全没有可用工具**，selector 返回
   `selected_tool = None`，原判定会把这种情况当成"没有偏离"而静默跳过。现按
   `capability_missing` 显式记录，`intent_downgrades` 不再依赖"必须有选中工具"。
2. **`cost_reservation` 逐 stage 累加**（第 4.1 节原文只说"累加每个 stage 选中工具的估算成本"）。
   实现按 `decision_log` 逐条累加而非按工具去重：同一个能力出现在两个阶段（例如
   `image_generation` 同时在 `storyboard` 与 `asset_generation`）就是两次真实 provider 调用，
   去重会低估花费。这是**有意的保守估计**，已在 `tests/test_cost_gate.py` 中用显式断言固定。
3. **全局 `RunGateError` 异常处理器**（第 4.3 节只提到"`routes.py` 统一映射"）。
   因为闸门会在适配器深处抛出（`generate_tts_audio` / `generate_music` / `generate_image` /
   `submit_avatar_task`），逐个路由加 handler 既啰嗦又容易漏；改为在 `main.py` 注册全局处理器，
   保证任何路由都返回同一种 `{"detail": {code, message, ...details}}`。worker 侧另加
   `except RunGateError`，把拒绝记为 job 失败原因而不是让 worker 崩溃。

验证：全量测试 `220 passed, 1 skipped`；ruff 通过；部署门禁 101 项全通过
（78 审核 + 11 T75 + 12 T76）。无 schema 变更，未新增 migration。
