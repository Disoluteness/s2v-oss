# 端点与响应形状速查

只在需要写脚本、或 `scripts/s2v.py` 覆盖不到时才读这份。所有路径以 `/api` 开头，`BASE` 由 `S2V_BASE_URL` 给出。

## 1. 鉴权

- **服务间**：`Authorization: Bearer s2v_...`（API key）。`scopes` 只能收窄不能放大，`run:start` 与 `run:review` 是分开的两个权限。
- **本机内部部署**（`ACTOR_TRUST_MODE=open`）：不带凭据会回落到默认身份（默认团队 owner），所以 `S2V_API_KEY` 可以留空。
- 自检：`GET /api/me` → `{actor_id, tenant_id, source, permissions[]}`。403 时先看这里的 `permissions`。
- 免鉴权的发现类端点：`/api/pipelines`、`/api/tools`、`/api/error-codes`、`/api/webhook-events`、`/api/run-state-transitions`、`/api/permissions`、`/api/health`、`/openapi.json`、`/docs`。

## 2. 本 skill 用到的端点

| 动作 | 端点 | scope | 备注 |
|---|---|---|---|
| 建项目 | `POST /api/projects` | `project:write` | `{title, description?, default_language?, default_aspect_ratio?}` |
| 建 run | `POST /api/projects/{project_id}/runs` | `run:create` | 见 §3 字段 |
| 读 run | `GET /api/runs/{run_id}` | `run:read` | 含 `stages[]` |
| 预检 | `POST /api/runs/{run_id}/preflight` | `run:write` | **无 body，会追加产物** |
| 执行计划 | `POST /api/runs/{run_id}/execution-plan` | `run:write` | **无 body，会追加产物** |
| 付费确认 | `POST /api/runs/{run_id}/provider-consents` | `run:start` | `{provider, accept, note?}` |
| 启动 | `POST /api/runs/{run_id}/start` | `run:start` | `{accept_intent_downgrade?}` |
| 关口列表 | `GET /api/runs/{run_id}/review-requests` | `run:read` | 找 `status=="open"` |
| 关口详情 | `GET /api/review-requests/{rid}` | `run:read` | `review_focus` / `success_criteria` / `artifact_scope` |
| 提交决策 | `POST /api/review-requests/{rid}/decisions` | `run:review` | 见 §4 |
| 一键过阶段 | `POST /api/runs/{run_id}/stages/{stage_key}/review` | `run:review` | 只需 `{decision, comment?}` |
| 产物（文件） | `GET /api/runs/{run_id}/assets` | `asset:read` | 见 §5 类型 |
| 产物（阶段） | `GET /api/runs/{run_id}/artifacts` | `run:read` | 含 `payload`，就是计划/剧本原文 |
| 作业列表 | `GET /api/runs/{run_id}/jobs` | `run:read` | |
| 单作业 | `GET /api/jobs/{job_id}` | `run:read` | |
| 催一次 | `POST /api/jobs/{job_id}/process` | `run:write` | 等价 manifest-actions `process_job` |
| 重试作业 | `POST /api/jobs/{job_id}/retry` | `run:write` | 死信才可 `requeue` |
| 生成图片 | `POST /api/runs/{run_id}/images` | `asset:write` | `{prompt!, negative_prompt?, size?, filename?}` |
| 生成语音 | `POST /api/runs/{run_id}/tts` | `asset:write` | `{text!, voice="Cherry", filename?}` |
| 上传素材 | `POST /api/runs/{run_id}/assets` | `asset:write` | `{asset_type!, filename!, content_base64!, mime_type?}` |
| 数字人（作业） | `POST /api/runs/{run_id}/jobs/avatar-from-assets` | `asset:write` | 见 §6 |
| 数字人（同步） | `POST /api/runs/{run_id}/avatar-tasks/from-assets` | `asset:write` | 立刻提交上游，不等 worker |
| 合成 | `POST /api/runs/{run_id}/compose` | `asset:write` | `{avatar_video_asset_id!, narration_audio_asset_id?, subtitle_asset_id?, music_asset_id?, render_profile_key?}` |
| 派发步骤 | `POST /api/runs/{run_id}/manifest-actions` | `run:write` | `{action:"dispatch_ready_steps"}`（会自动补建执行清单） |
| 重跑阶段 | `POST /api/runs/{run_id}/state-actions` | `run:write` | `{action:"rerun_stage", stage_key}` |
| 重跑产物 | 同上 | `run:write` | `{action:"rerun_artifact", artifact_type}` |
| 下载 | `GET /api/assets/{asset_id}/download` | `asset:read` | 注意 wav 的已知 500，见 troubleshooting |
| 签名链接 | `GET /api/assets/{asset_id}/access-link?expires_in_seconds=` | `asset:read` | 返回 `{asset_id, url, expires_at}`，TTL 会被夹到上限 |
| 复制 run | `POST /api/runs/{run_id}/copy` | `run:write` | `{target_project_id?, copy_reason?, overrides?}` |

`manifest-actions` 的 `action` 枚举：`dispatch_ready_steps` / `generate_person_image` / `upload_person_image` / `generate_tts` / `analyze_source_media` / `process_job` / `retry_job`。
`state-actions` 的 `action` 枚举：`pause` / `resume` / `cancel` / `rerun_stage` / `rerun_artifact` / `skip_optional_stage`。

## 3. 建 run 的字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `raw_script` | ✅ | — | 原始口播稿 |
| `pipeline_key` | | `avatar_spokesperson` | 也可 `spokesperson_ad` / `animated_explainer` / `hybrid_story`(beta) / `localization_dub` / `screen_demo` / `source_clip_factory` |
| `brand_name` / `product_name` | | `null` | |
| `target_platform` | | `douyin` | |
| `target_duration_seconds` | | `15` | |
| `aspect_ratio` | | `9:16` | |
| `style_key` | | `null` | |
| `cost_budget` | | `null` | 声明后预算闸门才开始工作 |
| `quality_mode` | | `null` | `local_preview` / `provider_audio` / `provider_avatar` |

## 4. 审核决策的字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `decision` | ✅ | `approve` / `approve_with_changes` / `request_changes` / `reject` / `reopen` / `cancel` |
| `comment` | | 人话说明 |
| `reason_code` | | 机器可读理由 |
| `artifact_refs` | | 必须指向**被审阶段的当前产物**，否则 409 `invalid_artifact_scope` |
| `action_items[]` | | `approve_with_changes` 时的待办 |
| `override_round_limit` | | 默认 `false` |

## 5. 响应形状里两个反直觉的点

**① `preflight` / `execution-plan` 返回的是「产物对象」，业务字段在 `payload` 里**

```json
{"artifact_id":"artifact_...", "artifact_type":"execution_plan", "version":4, "is_current":true,
 "payload": {"execution_policy":"proceed_with_degradations","can_execute":true,
             "intent_downgrades":[...], "cost_reservation":{...}, "stage_plans":[...]}}
```

直接读顶层 `execution_policy` 会拿到 `None` —— `scripts/s2v.py` 已经帮你 unwrap 了。

**② 提交决策返回的是「审核请求详情」，不是决策对象**

`{review_request_id, status:"approved", current_decision:"approve", gates_stage_key:"storyboard", ...}`。
`gates_stage_key` 就是下一关，可用来预告「下一关审什么」。

## 6. 数字人提交的字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `person_image_asset_id` 或 `person_image_url` | — | 二选一 |
| `audio_asset_id` 或 `audio_url` | — | 二选一 |
| `model` | `wan2.2-s2v` | 万相-数字人 |
| `face_bbox` / `ext_bbox` | `null` | **只有 EMO 方言（`emo*`）才需要**；`wan2.2-s2v` 自己定位，传了会被拒 |
| `auto_archive` | `true` | 成果自动归档为 `avatar_video_clip` |
| `auto_compose` | `true` | 出片后自动合成 |
| `access_link_ttl_seconds` | `3600` | 给上游取素材的签名链接有效期 |

不含 `resolution` 字段 —— 分辨率由部署的 `DASHSCOPE_AVATAR_RESOLUTION`（默认 `480P`）决定。

## 7. 资产类型

`person_image` / `tts_audio` / `avatar_video_clip` / `rendered_video` / `subtitle_file` / `source_video` / `reference_image`。
`avatar_video` 是「能力名」，落到资产上是 **`avatar_video_clip`**；口播音频是 **`tts_audio`**（历史上有代码找 `narration_audio`，那是死路径）。

## 8. 错误信封

```json
{"detail": {"code": "review_request_closed", "message": "...", "review_status": "approved"}}
```

按 `code` 分支，别按 `message`。常用：

| 状态 | code | 怎么办 |
|---|---|---|
| 400 | `bad_request` | 请求本身有问题 |
| 401 | `api_key_invalid` / `unauthorized` | key 错或吊销 |
| 403 | `permission_denied` | scope 不够，响应里写明是哪个 |
| 404 | `not_found` | **跨租户也是 404**，不能用来判断「不存在」 |
| 409 | `review_request_closed` | 该关已决策过，要 `reopen` |
| 409 | `invalid_artifact_scope` | `artifact_refs` 指到了阶段外产物；被拒的决策**不落库**，改对再提 |
| 409 | `stage_not_awaiting_review` | 该阶段不在待审状态 |
| 409 | `intent_downgrade_not_acknowledged` | 传 `accept_intent_downgrade` 或改 `quality_mode` |
| 409 | `paid_provider_confirmation_required` | 先 `provider-consents` |
| 409 | `cost_reservation_exceeds_budget` / `team_quota_exceeded` | 提预算 / 提配额 / 一次性豁免 `/quota-override` |
| 413 / 415 | `payload_too_large` / `unsupported_media_type` | 素材超限或类型错配（魔数嗅探） |
| 503 | `maintenance_mode` | 维护模式只关写，退避后重试 |

全量 28 个码及 `retryable`：`GET /api/error-codes`。

## 9. 幂等与分页

- 写请求可带 `Idempotency-Key`：同 key 同 body 返回原响应（`Idempotency-Replayed: true`），同 key 不同 body → 409 `idempotency_conflict`。**用「业务单号 + 操作」派生，别用随机 UUID**。
- 列表端点：新端点回 `Page{items,total,limit,offset}`，老端点回裸数组但带 `X-Total-Count`。按 `openapi.json` 的 schema 写，别按记忆。
