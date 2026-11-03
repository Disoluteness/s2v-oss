# 流水线阶段逐个说明

以 `avatar_spokesperson`（production，继承 `spokesperson_ad`）为准：**10 个阶段，5 道人工关口**。
换 pipeline 前先 `s2v.py api GET /api/pipelines` 看 `status` —— 关口数量由 pipeline 定义，别把「5 关」写死进代码。

## 总表

| # | stage_key | 中文 | 产物 | 人工关口 | 这一步实际发生什么 |
|---|---|---|---|---|---|
| 1 | `intake` | 需求摘要 | `intake_brief` | — | 把你的稿子 + 品牌/产品/时长/比例固化成一个不可变输入快照 |
| 2 | `plan` | 制作计划 | `production_plan` | ✅ | 定 hook、核心卖点、CTA、时长分配、要调哪些工具 |
| 3 | `script` | 口播稿 | `spoken_script` | ✅ | 把计划写成可念的口播稿（`segments` + `full_text`） |
| 4 | `storyboard` | 分镜 | `shot_list` | ✅ | 每个口播段落配一个画面处理 + 素材需求 |
| 5 | `asset_generation` | 素材生成 | `asset_manifest` | — | **真正出素材**：人物图、口播音频、数字人片段（靠 manifest 工具步骤派发） |
| 6 | `asset_review` | 素材复核 | `review_notes` | ✅ | 人看图/听音，判断能不能进剪辑 |
| 7 | `edit` | 剪辑计划 | `edit_plan` | — | 片段顺序、字幕、BGM 安排 |
| 8 | `compose` | 合成 | `render_summary` | — | ffmpeg 合成成片（`rendered_video`） |
| 9 | `final_review` | 成片终审 | `review_notes` | ✅ | 看成片：画面、字幕、混音、品牌口径 |
| 10 | `export` | 导出 | `publish_package` | — | 发布包（平台、封面、标题） |

## 每关在定什么（`gate` 命令会原样打印这些）

| 关口 | review_focus | success_criteria |
|---|---|---|
| `plan` | hook / 产品承诺 / provider 假设 / 时长匹配 | 适合竖屏口播广告；写清了必需与可选的媒体能力 |
| `script` | 口语化 / 字幕可读性 / 时长 / 品牌与产品准确 | 目标时长内念得完；字幕短到手机上看得清 |
| `storyboard` | 镜头顺序 / 产品露出 / 数字人适配度 / 视觉节奏 | 每段口播都有画面处理；镜头符合平台与时长 |
| `asset_review` | 数字人质量 / 音频清晰度 / 字幕可读性 / 品牌产品契合 | 审核人认可现有素材，或记录要改什么 |
| `final_review` | 成片质量 / 字幕贴合 / 混音 / 品牌产品准确 | 终版可以导出 |

## 每关「不满意」怎么改（按层次，别越级）

| 关口 | 症状 | 该做的 | 别做的 |
|---|---|---|---|
| `plan` | hook 不抓人、卖点错、时长不对 | `decide --decision request_changes --comment "..."`，或 `rerun --stage plan` | 直接改 script —— 上游没变，下游会再对不上 |
| `script` | 文案生硬、太长、违反品牌口径 | `request_changes`；要重写就 `rerun --stage script` | 只重跑 `tts`（文字没变，声音变了也没用） |
| `storyboard` | 镜头单调、产品露出不够 | `request_changes`；结构性问题 `rerun --stage storyboard` | 重跑 `plan`（会把 script 也一起作废） |
| `asset_review` | 人物图不像 / 声音不对 / 口型差 | **层 1**：`image` / `tts` / `avatar` 重做对应素材，再回来 `approve` | `rerun --stage asset_review`（它只产出 review_notes，不重做素材） |
| `final_review` | 字幕错位 / 比例不对 / BGM 太响 | **层 1**：`compose --profile ... ` 重渲染 | `rerun --stage compose`（会连带作废 final_review 的审批） |

**层 1 vs 层 2 的分界**：素材/渲染参数的问题 → 层 1（重做那个动作本身）；文字与结构的问题 → 层 2（重跑阶段）。
判据写在 `SKILL.md` §4。

## `asset_generation` 内部（真正花钱的一段）

这一阶段自身产出 `asset_manifest` 占位产物，真正干活的是**工具步骤**，按能力分：

| 能力 | 动作 | 命令 | 产物类型 |
|---|---|---|---|
| `image_generation`（可选） | 生成人物图 | `image --prompt ...` | `person_image` |
| `tts`（可选） | 生成口播音频 | `tts` | `tts_audio` |
| `avatar_video`（必需） | 提交数字人 | `avatar` | `avatar_video_clip` |
| `video_composition`（必需） | 合成 | `compose`（`avatar` 也能自动触发） | `rendered_video` |
| `subtitle_generation`（必需） | 字幕 | `api POST /api/runs/{id}/subtitles` | `subtitle_file` |

`dispatch` 会按执行清单把「已就绪且声明自动派发」的步骤一次性入队；但**人物图与音频通常要先手工备好**，
否则数字人作业会以「缺输入」失败。顺序：**人物图 → 音频 → 数字人 → 合成**。

## 状态机（`rerun` 能做什么）

`run.status`：`draft` → `queued` → `running` ⇄ `waiting_review` → `completed` / `failed` / `cancelled`，另有 `paused`。

`rerun_stage` / `rerun_artifact` 允许从 `draft` / `running` / `waiting_review` / `paused` / `failed` / **`completed`** 发起 —— 已完成的片子也能回退重做。
`skip_optional_stage` 只在 `running` / `waiting_review` / `paused` 可用，且仅对 pipeline 标记为可跳过的阶段（`final_review`）有效。
