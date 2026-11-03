---
name: s2v-pipeline
description: 用 Script2Video（s2v）服务把一段口播稿做成数字人口播视频，并在每一步停下来让人确认。覆盖：建项目/建 run → 五道人工审核关口逐关决策 → 生成或上传人物图与口播音频 → 提交 wan2.2-s2v 数字人任务 → 合成成片 → 取片；也覆盖「某一步不满意要重做」（重生成素材 / 重跑阶段 / 打回关口）。当用户说「用 s2v 生成数字人视频」「走一遍口播视频流水线」「把这段稿子做成口播视频」「改一下上一条视频的第 N 步」时使用。
agent_created: true
---

# Script2Video 流水线

把一个口播稿推到成片，**一步一停**，每步都能退回去重做。

## 0. 先配一次，再开始

```bash
python3 scripts/s2v.py config --base-url http://<你的服务地址>:18080
```

地址会写进 `~/.s2v/state.json`，**之后所有命令都不必再带 `--base-url`**。同一条命令顺手探活并报出版本与迁移位点：

```
探活        ok  (script2video-studio-api)
版本        1.1.0  ·  环境 local
迁移        20261012_0022  head 20261012_0022  pending=0
```

`探活 ok` + `pending=0` 就是环境可用。若报「库还不在 head 上」，先在服务器升级 schema，别急着建 run。

服务需要鉴权时用环境变量，不要写进配置：`export S2V_API_KEY=s2v_xxx`（`config` **不落盘密钥**）。
`ACTOR_TRUST_MODE=open` 的部署可以匿名调用。

地址解析优先级：`--base-url` > `S2V_BASE_URL` > `~/.s2v/state.json` > `127.0.0.1:18080`。
**环境变量会盖过状态文件** —— 想换地址就改环境变量，只改状态文件会「看起来改了但没生效」。

脚本只用标准库（Python ≥ 3.9，无需装依赖），路径相对本 skill 目录（加载技能时会给出 base directory，先 `cd` 过去或用绝对路径）。
所有子命令都支持 `--json`（写在子命令前后都行）。

服务在 10.x / 127.0.0.1 这类内网地址时，脚本会自动**绕过 HTTP 代理**（否则会得到一个假的 502）。

## 1. 心智模型：两条「步骤」并行，别混

| | 是什么 | 谁在推进 | 停下时 |
|---|---|---|---|
| **流程步骤** | 10 个阶段（`intake` → `plan` → `script` → `storyboard` → `asset_generation` → `asset_review` → `edit` → `compose` → `final_review` → `export`），其中 **5 个设了人工关口** | `start` 后自动跑，遇关口停 | `status=waiting_review` |
| **素材步骤** | 真正花钱的动作：生成人物图、生成口播音频、提交数字人、合成 | 显式调用（`image` / `tts` / `avatar` / `compose`，或 `dispatch` 自动派发） | 作业 `queued/running/waiting_provider` |

**关键事实**：非关口的阶段（`asset_generation` / `edit` / `compose` / `export`）走的是占位产物；**成片是素材步骤做出来的，不是 `start` 一把跑完**。
`start` 跑到 `export` 只代表「流程走完」，画面有没有出来要看 `assets`。

## 2. 标准走法（10 步）

### ① 建 run

```bash
python3 scripts/s2v.py init --script "大家好，这款面霜……" --title "10月投放-面霜" \
  --brand "示例品牌" --product "轻盈面霜" --duration 15 --quality-mode provider_avatar
```

`--quality-mode provider_avatar` 是**承诺**：声明走付费数字人后，环境做不到时 `start` 会拒绝，而不是偷偷降级；不声明等于默许任何降级。
`run_id` 自动存进 `~/.s2v/state.json`，后续命令不用再传。

**不满意**：稿子本身要改 → `init` 一条新的（旧 run 留着做对照）。

### ② 预检 + 执行计划（看清再启动）

```bash
python3 scripts/s2v.py preflight
```

看四行：`execution_policy`、`can_execute`、`intent_downgrades`、`blocking_issues`。
有降级就在这一步讲清楚「哪一环退化成什么」，别等出片才发现。

⚠️ 这两个接口是 **POST 且各追加一个产物版本**，别放进轮询。

### ③ 启动 → 停在第一关

```bash
python3 scripts/s2v.py start                 # 要接受降级才加 --accept-downgrade
```

返回 `status=waiting_review, stage=plan` **是正常的**。四道闸门会在这拒绝，都有稳定错误码：
`intent_downgrade_not_acknowledged` / `paid_provider_confirmation_required` / `cost_reservation_exceeds_budget` / `team_quota_exceeded`。

付费确认（只在执行计划显示要调付费 provider 时才需要）：

```bash
python3 scripts/s2v.py api POST /api/runs/$RUN_ID/provider-consents --body '{"provider":"dashscope","accept":true}'
```

### ④ 逐关审核（重复到 `completed`）

**这里就是「跟 AI 对话」发生的地方**，按 §3 的对话协议做：

```bash
python3 scripts/s2v.py gates                      # 取 open 的那条
python3 scripts/s2v.py gate reviewreq_xxx         # 看要审什么 + 被审产物内容
python3 scripts/s2v.py decide reviewreq_xxx --decision approve --comment "钩子可以"
python3 scripts/s2v.py decide reviewreq_xxx --decision request_changes --comment "开头太硬，改成提问式"
```

`decision` 六选一：`approve` / `approve_with_changes` / `request_changes` / `reject` / `reopen` / `cancel`。
`approve_with_changes` = 放行但留下待办（不阻断）；`request_changes` / `reject` = 打回，关口重开。

**不满意**：直接 `request_changes` —— 最轻的改法，不作废上游，只让本关重开（轮次 +1）。

### ⑤ 素材：人物图

```bash
python3 scripts/s2v.py image --prompt "25 岁女性，正面半身，纯色背景，看镜头，口型自然" \
  --negative "文字, 水印, 侧脸, 遮嘴, 畸形手指"
python3 scripts/s2v.py upload --asset-type person_image --file ./me.png   # 有现成图就用这个
python3 scripts/s2v.py fetch asset_xxx --out ./person.png                # 拉下来给人看
```

硬约束（来自上游 `wan2.2-s2v`，不是本服务的策略）：宽高各 **400–7000 px**、须公网可访问、**正面、嘴部无遮挡**。

**不满意**：再跑一次 `image`（旧图保留，可对比）→ 再 `fetch` 给用户挑。**不需要重跑任何阶段**。

### ⑥ 素材：口播音频

```bash
python3 scripts/s2v.py tts                   # 默认读 script 阶段的 spoken_script 全文
python3 scripts/s2v.py tts --voice Cherry --text "只想读这一句"
```

脚本会打印时长；**≥20 秒会警告** —— `wan2.2-s2v` 硬限 `<20s` 且 `<15MB`，超了必被拒。写稿按 **18 秒以内**留余量。

**不满意**：换 `--voice` 重跑 `tts`；要改文字 → 回 §④ 打回 `script` 关口，或 `rerun --stage script`。

### ⑦ 提交数字人

```bash
python3 scripts/s2v.py avatar                  # 自动取「最新人物图 + 最新音频」
python3 scripts/s2v.py avatar --no-auto-compose # 想自己控合成参数时
```

**同一个账号同时只能有 1 个数字人任务**（主账号与子账号共用）；端到端 **5–15 分钟属正常**（服务每轮 poll 约 5 分钟，上游本身 5–10 分钟）。

```bash
python3 scripts/s2v.py jobs
python3 scripts/s2v.py job job_xxx --process    # 手动催一次，别循环猛催
```

**不满意**：口型/画面不对 → 换人物图后再 `avatar`；声音不对 → 换 `tts` 后再 `avatar`。都在素材层，不动流程。

### ⑧ 合成成片

```bash
python3 scripts/s2v.py compose                                # 自动取最新数字人片段 + 音频
python3 scripts/s2v.py compose --profile portrait_1080x1920    # 指定渲染档位
python3 scripts/s2v.py fetch asset_xxx --out ./out.mp4
```

`avatar` 默认 `auto_compose=true` 已合成过；这里再跑是**换参数重渲染**。`compose` 要求先有 `avatar_video_clip` 产物。

**不满意**：字幕/尺寸/BGM 不对 → 调参再 `compose`（最便宜的重做）。

### ⑨ 取片

```bash
python3 scripts/s2v.py fetch <rendered_video_asset_id> --out ./out.mp4   # 自己拿文件
python3 scripts/s2v.py link  <asset_id> --ttl 3600                      # 给第三方限时链接
```

### ⑩ 收尾

`status` 里 `run.status == completed`。把「每关做了什么决定、产物 id、本地成片路径」汇总给用户。

## 3. 对话协议（这一步决定体验）

用户要的是「跟 AI 说话就能做出来」，所以**默认一步一停**：

1. **每到一个关口**：先 `gate` 拿到被审产物 → 用中文说清三件事：**这关在定什么、这版写了什么（摘关键句，别贴整段 JSON）、通过标准是什么** → 再问「这版行不行，要不要改哪里」。
2. **不要替用户默认放行**。只有用户明确说「全自动 / 一路通过」时才连续 `approve`，且事后必须汇总每一关的决定。
3. **素材出来必须给人看**：图片 `fetch` 到本地后用读图能力描述并判断；视频报告时长、分辨率、大小，并给出文件路径。
4. **用户说「不满意」时先分层**（见 §4），再动手；不要一上来就 `rerun --stage`（三层里最贵的一层）。
5. **每次动作后重述现状**：现在第几阶段、还剩几关、已经花了什么。用户不需要记 id。

## 4. 「不满意」的三层回退（先选层，再执行）

| 层 | 适用症状 | 命令 | 代价 |
|---|---|---|---|
| **1 素材级** | 图不像 / 声音不对 / 口型差 / 字幕尺寸不对 | `image` / `tts` / `avatar` / `compose` | 最轻：不动流程、不作废审批，只多一个资产版本 |
| **2 阶段级** | 计划方向错 / 口播稿要重写 / 分镜要重排 | `rerun --stage <key>` 或 `rerun --artifact <type>` | 中：**该阶段及下游全部作废**，已通过的关口重新开出（轮次 +1），旧 `review_request_id` 立刻失效 |
| **3 重开一条** | 换 pipeline / 换平台 / 想保留旧版对照 | `init` 新 run，或 `api POST /api/runs/{id}/copy` | 最贵但最干净 |

判据一句话：**「画面/声音不对」→ 层 1；「文字/方向不对」→ 层 2；「换做法」→ 层 3。**

层 2 的阶段键（`avatar_spokesperson`）：`intake` `plan` `script` `storyboard` `asset_generation` `asset_review` `edit` `compose` `final_review` `export`；
产物类型：`intake_brief` `production_plan` `spoken_script` `shot_list` `asset_manifest` `review_notes` `edit_plan` `render_summary` `publish_package`。

`rerun_stage` **在 `completed` 之后仍然可用** —— 片子已经出了还想改，不必重开 run。

## 5. 红线与已知坑

- **音频 <20 秒、<15MB**；图片 400–7000 px；**数字人并发 1**。三条都来自上游。
- **`waiting_provider` 不是卡住**：5–15 分钟正常，看 `error_retryable` 判断要不要重试。
- **`preflight` / `execution-plan` 有副作用**（各追加一个产物版本），别轮询。
- **一次审核请求只能决策一次**：已 `approved` 再决策会 409 `review_request_closed`，要 `reopen`。
- **404 不等于不存在**：跨租户也是 404，别用状态码探测资源。
- **失败分类可能失真**：provider 4xx 有时被归成 `input.invalid_or_missing`，先看 `job.error_message` 原文。
- **`estimate_cost` 对数字人返回 0**（按时长计费、提交时算不出），所以**预算/配额闸门拦不住数字人花费** —— 花钱前自己跟用户确认。
- 更多见 `references/troubleshooting.md`。

## 6. 失败排查

```bash
python3 scripts/s2v.py jobs                      # error_code / error_retryable / remediation
python3 scripts/s2v.py job job_xxx --process     # 催一次
python3 scripts/s2v.py retry job_xxx             # 重试（仅对可重试作业有意义）
python3 scripts/s2v.py rerun --stage asset_generation   # 换输入后重跑该阶段
```

## 7. 参考文件

- `references/pipeline-stages.md` —— 10 个阶段逐个：做什么 / 产物 / 是否关口 / 不满意怎么改
- `references/api-endpoints.md` —— 用到的端点、字段、响应形状、错误码分支
- `references/troubleshooting.md` —— 坑与对症解法
- `scripts/s2v.py` —— 所有动作的实现；`--help` 是完整命令表，`api <METHOD> <path>` 是逃生通道
