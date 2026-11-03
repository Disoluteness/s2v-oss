# 示例

> 这几条路径是「**怎么把请求发出去**」；如果你要问的是「**该按什么顺序调、先调哪个后调哪个**」，
> 那是 [`docs/API_INTEGRATION.md`](../docs/API_INTEGRATION.md) —— 对外接入指南（鉴权、调用顺序、
> 审核关口循环、webhook 收事件、幂等与错误分支）。

三条路径，同一个契约。按你手上有什么来选：

| 路径 | 文件 | 装什么 | 什么时候用它 |
|---|---|---|---|
| **curl** | [`curl/run_pipeline.sh`](curl/run_pipeline.sh) | `curl` + `python3` | 想看清**线上真正传的字节长什么样**；SDK 报错时用它确认是服务端形状的问题还是 SDK 的问题 |
| **Python SDK** | [`sdk/python/examples/`](../sdk/python/examples/) | `pip install -e sdk/python` | 内部平台、批处理、定时任务 |
| **TypeScript SDK** | [`sdk/typescript/examples/`](../sdk/typescript/examples/) | `npm i s2v-client` | Node 服务、前端集成、Web 工作台之外的界面 |

三条路径都遵守同一个退出码约定（`0` 成功 / `1` API 拒绝或 run 失败 / `2` 用法错误 /
`3` 连不上），所以它们可以直接进 CI。这条约定与 CLI（`s2v`）一致——一个脚本包住另一个脚本时，
不需要翻译错误。

SDK 的示例**不在这里复制一份**：两份同样的代码意味着两份会各自腐坏。本目录只放
「SDK 之外的那条路」（curl）与这张地图。

## 先起一个服务

```bash
# 本机（无 docker）
make api                      # uvicorn，监听 0.0.0.0:8080

# 或者整套
docker compose up --build
```

## 跑 curl 示例

```bash
./examples/curl/run_pipeline.sh
./examples/curl/run_pipeline.sh https://studio.internal
SCRIPT2VIDEO_ACTOR_ID=alice ./examples/curl/run_pipeline.sh
```

可覆盖的环境变量：`SCRIPT2VIDEO_BASE_URL`、`SCRIPT2VIDEO_ACTOR_ID`、
`SCRIPT2VIDEO_PIPELINE_KEY`（默认 `spokesperson_ad`，另外四条见 `/api/pipelines`）、
`SCRIPT2VIDEO_POLL_ATTEMPTS`、`SCRIPT2VIDEO_POLL_INTERVAL`。

## 跑 SDK 示例

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e sdk/python
SCRIPT2VIDEO_BASE_URL=http://127.0.0.1:8080 python sdk/python/examples/script_to_video.py
```

## 预期会发生什么（以及不会发生什么）

run 会走到第一道人工审核关口就**停下**：

```
… waiting_review @ plan
```

这是设计，不是故障。平台不会在没人确认的情况下替你花 provider 的钱（见
`docs/TASK_BOARD.md` 的 T75 / T76）。要继续，去 Web 工作台
（`${BASE_URL}/workspace?run_id=…`）批一下，或者读
`sdk/python/examples/upload_and_retry.py` 看怎么用代码推进。

**没有配 provider key 的部署上，run 会一直停在 `waiting_review`** —— 示例不会伪造一条
走完全程的记录，它只演示**调用形状**。要真的出片，先按
`POST /api/provider-credentials` 配好至少一个 provider 的凭证。

## 想换一条 pipeline

```bash
# 看有哪些
curl -s http://127.0.0.1:8080/api/pipelines | python3 -m json.tool | head -40
```

五条 pipeline（`avatar_spokesperson` / `animated_explainer` / `source_clip_factory` /
`screen_demo` / 兼容键 `spokesperson_ad`）各自的适用场景写在 `docs/PLATFORM_PLAN.md`；
`pipeline_key` 直接传给建 run 的请求即可（示例里是 `SCRIPT2VIDEO_PIPELINE_KEY`）。
注意 `spokesperson_ad` 是历史兼容键，当前仍是 `draft`（见 `docs/RELEASE_CHECKLIST.md` §5）。
