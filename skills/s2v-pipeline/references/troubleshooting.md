# 坑与对症解法

## 调用侧

### 「作业一直 waiting_provider，是不是卡住了」

不是。数字人上游本身就 5–10 分钟出片；服务对 `waiting_provider` 的轮询节奏约 5 分钟一轮（租约 300 秒续一次），
所以**端到端 5–15 分钟是正常的**。判断依据是 `job.error_retryable`，不是等了多久。

想主动推一次：`s2v.py job <job_id> --process`。别写成 `while true` 猛催 —— 每次都是真实的上游查询。

### 「两个数字人任务互相卡住」

`wan2.2-s2v` 的「同时处理中任务数量」上限是 **1**（主账号与子账号共用）。
并行提交会排队或直接失败。批量出片必须串行。

### 「音频明明只有 18 秒还是被拒」

三个独立限制：**时长 <20s、大小 <15MB、格式 wav/mp3**。
24kHz/16bit/mono 的语音大约 1 秒 ≈ 48KB，15MB 对应约 5 分钟，所以**时长先撞线**。
写稿按 18 秒以内留余量；超了就切段分别生成，再拼。

### 「数字人报 Model not exist.」

模型名被上游下线，或部署机 `.env` 里写着旧值。调用侧先确认这次请求用的 `model`：
`s2v.py api GET /api/runs/{run_id}/jobs` 看 `request_payload.model`。
若是 `emo-v1` / `emo-detect-v1`：这两个已被百炼下线，改部署的 `DASHSCOPE_AVATAR_MODEL=wan2.2-s2v`。
**注意**：改完 `.env` 后 `docker compose up -d` 可能既不重建容器也不改变行为（shell 里的同名 env 会劫持插值），
必须用 `docker compose exec -T api printenv DASHSCOPE_AVATAR_MODEL` 或应用自检确认。

### 「传了 face_bbox 反而被拒」

`wan2.2-s2v` 自己定位人物，**不接受 bbox**；只有 EMO 方言（`emo*`）才需要 `face_bbox` / `ext_bbox`。
`s2v.py avatar` 默认不传。

### 「图片上传了但上游说取不到」

人物图必须能被上游抓取：宽高各 400–7000 px，且 `PUBLIC_BASE_URL` / 签名链接可达。
签名链接**默认 1 小时有效**，所以提交任务后别轮换 `ASSET_LINK_SECRET` —— 在途链接会立刻失效，上游就取不到素材。

### 「`dispatch` 派发了 0 个作业」

三种原因，按顺序查：

1. **阶段没到**：只有 `running` / `waiting_review` / `approved` / `completed` 的阶段才会派发。当前停在 `plan` 关口时派发必然为 0。
2. **步骤未声明自动派发**：`auto_dispatch=false` 的步骤（例如 `llm_generation`）永不派发。
3. **执行计划 `can_execute=false`**：先 `preflight` 看 `blocking_issues`。

### 「`avatar` 说没有人物图 / 没有音频」

这是有意的前置校验：数字人需要 `person_image` + `tts_audio` 两类素材。
先 `image --prompt ...`（或 `upload --asset-type person_image`）与 `tts`（没有文本时先过 `script` 关口），再提交。

### 「`compose` 说没有数字人视频片段」

`compose` 要 `avatar_video_asset_id`，即先有 `avatar_video_clip` 产物。`avatar` 默认 `auto_compose=true` 会自动合成一次；
如果当时关了自动合成，等作业 `completed` 后再手动 `compose`。

### 「`review_request_id` 用过一次就报 409」

一次请求只能决策一次；已 `approved` 再决策 → `review_request_closed`（要 `reopen`）。
另外**重跑上游会让下游审批失效**并按递增轮次重新开出新请求 —— **别缓存 id**，每轮都从 `gates` 现取。

### 「打回的决策没生效」

`artifact_refs` 指到了被审阶段以外的产物 → 409 `invalid_artifact_scope`，**被拒的决策不落库**，关口仍然 `open`。
不带 `artifact_refs`（默认锚定当前产物）最省事。

### 「`preflight` 调了几次，产物多了一堆版本」

`preflight` 与 `execution-plan` 都是 POST 且各追加一个产物版本。它们是「先看一眼」的动作，**不是查询端点**，别轮询。

### 「403 和 404 分不清是有权还是不存在」

跨租户访问刻意返回 404（避免 403 泄露「这行存在」），所以**不能用状态码区分「无权」与「不存在」**。
判断权限看 `403 permission_denied` 的 `message`（会写明缺哪个 scope）。

### 「作业失败信息说是输入缺失，但素材明明在」

provider 侧的 4xx 有时被归成 `input.invalid_or_missing`，掩盖真实原因。
**先读 `job.error_message` 原文**，再看 `job.result_payload` 里的上游响应。

### 「预算闸门没拦住数字人花费」

已知缺口：`estimate_cost` 对数字人返回 0（按时长计费、提交时算不出时长），
所以声明 `cost_budget` 的 run 在数字人链路上**预留恒为 0，闸门不生效**。
花钱前自己跟用户确认，别把「没被拦」当成「不花钱」。

### 「下载 wav 返回 500」

`GET /api/assets/{id}/download` 对音频（wav）有已知 500，mp4 正常。
绕法：`s2v.py link <asset_id>` 签发限时链接，再用链接走 `/content` 取。

## 部署侧（只需转达，不要在调用侧改）

- **改 `.env` 纯配置项后行为不变**：Compose 的 `environment: ${VAR:-默认}` 插值里 **shell 环境优先于 `.env`**，
  且解析结果不变时 config-hash 不变 → `up -d` 连容器都不重建。验证只能进容器 `printenv`。
- **门禁与预检都不覆盖「provider 模型名配错」**：它们读状态与行为，模型名两者都不是。
- **`ASSET_LINK_SECRET` 轮换 = 在途签名链接全部失效**；`API_KEY_PEPPER` 轮换 = 所有 API key 与会话作废。

## 排查顺序（照这个走，别跳）

1. `s2v.py status` —— 现在在哪一阶段、有没有 open 关口、有没有在跑的作业。
2. `s2v.py jobs` —— 看 `error_code` / `error_retryable` / `error_remediation`。
3. `s2v.py gate <rid>` —— 被审产物到底是什么、通过标准是什么。
4. `s2v.py artifacts --type <type> --show` —— 计划/剧本原文。
5. `s2v.py api GET /api/deployment` —— 版本与迁移是否对齐（部署问题才看这步）。
