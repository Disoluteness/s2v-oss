# 发布回归清单

适用版本：**1.1.0** 起（兼容性承诺自 `1.0.0` 起生效，`1.1.0` 为首个增量次版本）

这份文件回答「**发版前要确认什么**」。它不回答「怎么部署」——那是
[`docs/DEPLOYMENT.md`](DEPLOYMENT.md) 的职责。两者的分界线是：DEPLOYMENT 讲步骤与故障处置，
本清单讲**判据**——什么算通过、什么算不通过、不通过时退回去做什么。

## 1. 回归基线

一次「全绿」的定义。任何一项不绿都当作发布阻塞，除非在本文件里写明是允许的偏差。

| 项 | 命令 | 1.1.0 基线 |
|---|---|---|
| 单元 / 契约测试 | `find /tmp/pt_release -mindepth 1 -delete; .venv/bin/python -m pytest -q --basetemp=/tmp/pt_release` | **832 passed, 1 skipped**（`test_embeddable_workspace` 的 3 个分帧测试要求 `starlette<1.0`：1.x 改了 `BaseHTTPMiddleware` 的 `receive` 语义，会报 `Unexpected message received: http.request`） |
| 静态检查 | `.venv/bin/python -m ruff check apps tests scripts` | All checks passed |
| 部署门禁（默认配置） | `docker compose exec -T api python scripts/verify_review_loop.py` | **408 项，失败 0** |
| 部署门禁（可选配置全开） | 同上，另设 `SECRET_ENCRYPTION_KEY` 与 `PROVIDER_CALLBACK_SECRET` | **412 项，失败 0** |
| 页面内联脚本 | `node --check`（7 个页面） | 全部通过 |
| 迁移往返 | `alembic upgrade head` → `downgrade -1` → `upgrade head` | 37 → 36 → 37 张表（P2 起，head `20261109_0023`） |
| 预检 | `python scripts/deployment_preflight.py` | 失败 0 |

**门禁可重入**：同一个库连跑两次必须都全绿。每次都直接在部署机上重跑即可，不需要先清库。
若第一次跑的失败项在第二次消失，说明有断言依赖了「这是第一次跑」（历史缺陷来源），
按 bug 处理。

**允许的偏差**（不是回归）：

- 部署机配了真实 provider key 时，第 11 节有 3 条「意图降级」检查按设计跳过 —— 总项数少 3。
- 可选配置分支：默认配置 408 项，`SECRET_ENCRYPTION_KEY` + `PROVIDER_CALLBACK_SECRET`
  全开时 412 项（第 23 节由 14 项变 18 项）。两种配置**各自**连跑两次都要绿，不能只测一种。

## 2. V1.0 达标判据

路线图 §3 把完整版定义成「可嵌入内部平台的 Script-to-Video 服务」，§9 给出六条判据。
逐条指认——指认不到的那条就是没做到：

| 路线图 §9 判据 | 现状 | 证据 |
|---|---|---|
| 用户可从内部平台用 SDK 创建 run | ✅ | `sdk/python/examples/script_to_video.py`；门禁第 16 节；`POST /api/projects/{id}/runs` |
| 系统自动完成 preflight → execution plan → proposal → 生成 → 合成 → QA → 发布包 | ✅ | 五条 pipeline 的 stage 序列（`pipelines/*.json`）；门禁第 1–10 节走完到第一道人工关口 |
| 任何失败都能在 Web Studio 看到原因与修复动作 | ✅ | `GET /api/runs/{id}/board` 的 `input_issues` / `repair_actions`；门禁第 14 节；`/run-board` 页面 |
| 管理员能看到 provider key 状态、用量、成本、失败率 | ✅ | `POST /api/provider-credentials/{id}/check`；`GET /api/projects/{id}/observability`（V8.2）；`/team` 页 |
| 至少 4 类 pipeline 稳定可用 | ✅ | V1.0 时 5 条在册，4 条非 draft（`avatar_spokesperson` production；`animated_explainer` / `screen_demo` / `source_clip_factory` beta）；V1.1 T94 补齐 `localization_dub` / `hybrid_story`（均 beta）后 **7 条在册，6 条非 draft**；P1 补齐 `talking_head` / `podcast_repurpose` / `cinematic` 后 **10 条在册，9 条非 draft**（`docs/CAPABILITY_GAP_PLAN.md`）。**注**：`spokesperson_ad`（MVP 那条）仍是 `draft`，见 §5 |
| 部署、升级、备份、回滚都有明确文档 | ✅ | `docs/DEPLOYMENT.md` 12 节（含第 12 节「明确不覆盖」） |

路线图 §4 对 V1.0 的另外四条要求：

| 要求 | 现状 | 证据 |
|---|---|---|
| Web Studio、SDK、Webhook 可用 | ✅ | 7 个页面；`s2v` / `s2v-client`；6 个 webhook 事件（`GET /api/webhook-events`） |
| 至少 3 类核心 provider 能力具备主备 | ✅ | 6 类具备 ≥2 provider：`avatar_video`、`browser_recording`、`llm_generation`、`subtitle_generation`、`tts`、`video_composition`（`tool_catalog/default.json`） |
| 成本、权限、审计、质量检查、部署文档完整 | ✅ | V8.3 配额与用量账本 / V6.5 权限矩阵 / `audit_logs` / 质量门禁 stage / `docs/DEPLOYMENT.md` |
| 有正式回归测试集和示例项目 | ✅ | 本文件 §1；`examples/` + 两个 SDK 的 `examples/` |

## 3. 发布前清单

按顺序执行。前一步不通过就不要进下一步——尤其不要在门禁红的情况下动 schema。

- [ ] **1. 预检**：`python scripts/deployment_preflight.py`（退出码 0）。它直接连库读配置，
      能在 API 起来之前发现「密钥没配 / schema 落后于代码 / 库连不上」。
- [ ] **2. 备份**：`./scripts/backup.sh`（**先备份再迁移**，顺序不可换）。
      记下 `backup_id`，稍后 `POST /api/deployment/backups` 把它登记进姿态。
- [ ] **3. 迁移**：重建镜像后由 `migrate` 一次性服务跑到 head（`docker compose up -d --build`）。
      确认 `GET /api/deployment` 的 `migration.up_to_date = true` 且 `pending = []`。
- [ ] **4. worker 存活**：`docker compose ps` —— **门禁全绿 ≠ worker 在列**。四个角色都要在。
- [ ] **5. 门禁收口**：`docker compose exec -T api python scripts/verify_review_loop.py`。
      两种配置各连跑两次。
- [ ] **6. 示例冒烟**：`./examples/curl/run_pipeline.sh`（可选，但要跑通一次——
      它验证的是「文档里写的调用形状，在**这台**上真的成立」）。
- [ ] **7. 版本三处一致**：`GET /api/deployment` 的 `app_version`、`GET /openapi.json` 的
      `info.version`、`CHANGELOG.md` 最新条目标题，三者相同。
- [ ] **8. 姿态记账**：`POST /api/deployment/backups` 登记第 2 步的 `backup_id`，
      让「上次备份是什么时候」变成一次读。

## 4. 发布后验证

- [ ] 冒烟一条真实 run（Web 工作台建、SDK 建，或 curl 示例建），确认走到 `waiting_review`
      且 board 上能看到阶段与用量。
- [ ] `GET /metrics` 有数据；`X-Request-Id` 出现在响应头里（V8.2）。
- [ ] **若在维护窗口内发布**：确认维护模式已关（`GET /api/deployment` 的
      `settings.maintenance_mode = false`）。这是唯一会把安装留在停写状态的地方。
- [ ] webhook 订阅方收到至少一次投递（若本次发布触及事件源）。

## 5. 已知偏差与待决事项

如实记录，不当作通过：

1. **`spokesperson_ad` 的状态是 `draft`**。它是 MVP 的第一条 pipeline，也是门禁自检用的那条，
   但状态字段从未随之提升。V1.0 的「≥4 条稳定 pipeline」由另外四条满足，所以这**不是**发布阻塞；
   它是一个需要产品决策的悬项：要么提升它，要么说明为什么 MVP 管线仍是 draft。
   §1 的测试与门禁只断言「非 draft 的至少 4 条」，不会替你把这个决定做掉。
2. **`pipelines/avatar_spokesperson.json` 没有 `dsl_version` 字段**，靠模型默认值 `"1.0"` 兜住。
   其余四条显式声明。行为上一致，但显式声明更不容易在将来被误读。

## 6. 回滚触发条件

什么时候退回去，以及退到哪。判据写在前面，免得在出事的当下现编。

| 触发条件 | 动作 |
|---|---|
| 迁移失败，或迁移后新版本起不来 | **schema 回滚**：`alembic downgrade -1`（只对可逆的那一步有效；先确认 `docs/DEPLOYMENT.md` §7） |
| 新版本起来了但门禁出现**新**失败项 | **代码回滚**：回到上一个镜像 tag。**不要**同时回滚 schema——先让服务可用 |
| 数据损坏 / 误删 / 恢复演练 | **恢复**：`./scripts/restore.sh --archive <归档> --yes`（默认要求先开维护模式；数据库侧是**破坏性**的，storage 侧是叠加式的） |
| 只是某个 provider 或某个 pipeline 出问题 | **不回滚**：停下那条 pipeline（或换个 provider），用维护模式挡住写，其余照常服务 |

回滚不是失败——**在没有判据的情况下坚持不回滚**才是。上表每一条都要求「先让它可用，
再找原因」，因为一个停写但正确的系统，和一个在跑但会写坏数据的系统，代价完全不同。
