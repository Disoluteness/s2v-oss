#!/usr/bin/env bash
#
# 端到端示例：脚本进，run 出。
#
# 这条路径只用 curl 说话——不装 SDK，也没有包装层，所以它也是排查集成问题时的
# 参照物：SDK 报错时，先用它确认「服务端本身答的是不是这个形状」。
#
#     ./examples/curl/run_pipeline.sh
#     SCRIPT2VIDEO_BASE_URL=https://studio.internal ./examples/curl/run_pipeline.sh
#     SCRIPT2VIDEO_ACTOR_ID=alice ./examples/curl/run_pipeline.sh
#
# 前置：curl + python3（只用它从响应里取一个字段，不引入 jq 依赖）。
#
# 退出码（与 CLI、两条 SDK 示例一致，方便直接进 CI）：
#   0  成功——run 停在了一个等待人的状态，或已经跑完
#   1  API 拒绝了某一步，或 run 以 failed/cancelled 收场
#   2  用法错误
#   3  连不上服务
#
# 没有配 provider key 的部署上，run 会停在第一道审核关口 waiting_review ——
# 这是**正确行为**而不是失败：平台没有把「无人值守地花钱」设成默认。

set -euo pipefail

BASE_URL="${SCRIPT2VIDEO_BASE_URL:-http://127.0.0.1:8080}"
if [ "$#" -gt 1 ]; then
  die_usage "最多接受一个参数（base_url），收到 $# 个"
fi
if [ "$#" -eq 1 ]; then
  BASE_URL="$1"
fi
ACTOR_ID="${SCRIPT2VIDEO_ACTOR_ID:-}"
PIPELINE_KEY="${SCRIPT2VIDEO_PIPELINE_KEY:-spokesperson_ad}"
POLL_ATTEMPTS="${SCRIPT2VIDEO_POLL_ATTEMPTS:-60}"
POLL_INTERVAL="${SCRIPT2VIDEO_POLL_INTERVAL:-2}"

RAW_SCRIPT="开场用一个反常识的问题抓住注意力，然后给出一个可验证的数据，最后用一句行动号召收尾。"

# --- 小工具 ---------------------------------------------------------------

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

die_usage() { say "用法错误：$*" >&2; exit 2; }

json_get() { # json_get <json> <dotted.path>；路径为空则原样回显
  python3 -c '
import json, sys

value = json.loads(sys.argv[1])
path = sys.argv[2]
if path:
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
if value is None:
    print("")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
' "$1" "$2"
}

# 发一次请求，把「响应体」与「HTTP 状态码」分开交回来（状态码在最后一行）。
# 数组的第一个元素就位，所以 "${args[@]}" 在 set -u 下永远不会是空展开。
LAST_STATUS=""
LAST_BODY=""
http() { # http <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H 'Accept: application/json')
  # if/then 而不是 `&&`：在 set -e 下，一个失败的短路测试会终止整个脚本。
  if [ -n "$ACTOR_ID" ]; then
    args+=(-H "X-Actor-Id: ${ACTOR_ID}")
  fi
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' --data "$body")
  fi

  local response
  if ! response="$(curl "${args[@]}" -w $'\n%{http_code}' "${BASE_URL}${path}")"; then
    say "连不上 ${BASE_URL} —— 服务起来了吗？" >&2
    exit 3
  fi
  LAST_STATUS="${response##*$'\n'}"
  LAST_BODY="${response%$'\n'*}"
}

# 取一句话解释失败：优先用统一信封里的 detail.code，SDK 就是按它分支的。
fail() {
  local code
  code="$(json_get "$LAST_BODY" 'detail.code' 2>/dev/null || true)"
  say "失败（HTTP ${LAST_STATUS}${code:+, ${code}}）：${LAST_BODY}" >&2
  exit 1
}

# --- 1. 探活 ---------------------------------------------------------------

step "1/7 探活"
http GET /api/health
[ "$LAST_STATUS" = "200" ] || fail
say "服务在线：$(json_get "$LAST_BODY" 'service')"

# --- 2. 我是谁 -------------------------------------------------------------

step "2/7 确认身份"
http GET /api/me
[ "$LAST_STATUS" = "200" ] || fail
say "身份：$(json_get "$LAST_BODY" 'actor_id')（来源 $(json_get "$LAST_BODY" 'source')）"
say "生效权限：$(json_get "$LAST_BODY" 'permissions.0') … 共 $(python3 -c '
import json, sys
print(len(json.loads(sys.argv[1]).get("permissions") or []))
' "$LAST_BODY") 项"

# --- 3. 建项目 -------------------------------------------------------------

step "3/7 建项目"
http POST /api/projects "$(python3 -c '
import json, sys
print(json.dumps({"title": sys.argv[1]}, ensure_ascii=False))
' "示例项目 $(date '+%Y-%m-%d %H:%M:%S')")"
[ "$LAST_STATUS" = "201" ] || [ "$LAST_STATUS" = "200" ] || fail
PROJECT_ID="$(json_get "$LAST_BODY" 'project_id')"
say "project_id = ${PROJECT_ID}"

# --- 4. 建 run -------------------------------------------------------------
#
# 先建项目再建 run，而不是一步到位：项目是幂等键、成员、配额与资产库的归属边界，
# 也是「这条 run 属于谁」的唯一答案。

step "4/7 建 run（pipeline_key = ${PIPELINE_KEY}）"
http POST "/api/projects/${PROJECT_ID}/runs" "$(python3 -c '
import json, sys
print(json.dumps({
    "pipeline_key": sys.argv[1],
    "raw_script": sys.argv[2],
    "brand_name": "示例品牌",
    "product_name": "示例产品",
    "target_platform": "douyin",
    "target_duration_seconds": 15,
}, ensure_ascii=False))
' "$PIPELINE_KEY" "$RAW_SCRIPT")"
[ "$LAST_STATUS" = "201" ] || [ "$LAST_STATUS" = "200" ] || fail
RUN_ID="$(json_get "$LAST_BODY" 'run_id')"
say "run_id = ${RUN_ID}（初始状态 $(json_get "$LAST_BODY" 'status')）"

# --- 5. 启动 ---------------------------------------------------------------

step "5/7 启动 run"
http POST "/api/runs/${RUN_ID}/start" '{"accept_intent_downgrade": false}'
[ "$LAST_STATUS" = "200" ] || fail
say "已启动 -> $(json_get "$LAST_BODY" 'status') @ $(json_get "$LAST_BODY" 'current_stage_key')"

# --- 6. 轮询 ---------------------------------------------------------------
#
# 停在 waiting_review / paused 就**停止轮询**：等人不是轮询能解决的事。
# 这是 SDK 的 wait_run(stop_on_review=True) 在说同一句话。

step "6/7 等待 run 安定（最多 $((POLL_ATTEMPTS * POLL_INTERVAL)) 秒）"
STATUS=""
for _ in $(seq 1 "$POLL_ATTEMPTS"); do
  http GET "/api/runs/${RUN_ID}"
  [ "$LAST_STATUS" = "200" ] || fail
  STATUS="$(json_get "$LAST_BODY" 'status')"
  say "  … ${STATUS} @ $(json_get "$LAST_BODY" 'current_stage_key')"
  case "$STATUS" in
    completed | failed | cancelled | waiting_review | paused) break ;;
  esac
  sleep "$POLL_INTERVAL"
done

case "$STATUS" in
  waiting_review | paused)
    say "停在人工关口 ${STATUS} —— 在 Web 工作台看完/批完才会继续。"
    say "（没有配 provider key 的部署会停在这里，这是设计而不是故障。）"
    ;;
  completed) say "run 已跑完。" ;;
  *) say "run 以 ${STATUS} 收场——证据在下面第 7 步的 board 里。" ;;
esac

# --- 7. 取证据 -------------------------------------------------------------
#
# artifacts 是阶段之间的契约，board 是人看的汇总。两者都读，因为它们回答不同的问题：
# 「生成了什么」与「为什么停在这里」。

step "7/7 读 artifacts 与 board"
http GET "/api/runs/${RUN_ID}/artifacts"
[ "$LAST_STATUS" = "200" ] || fail
say "artifacts：$(python3 -c '
import json, sys
print(len(json.loads(sys.argv[1]) or []))
' "$LAST_BODY") 个"
python3 -c "
import json, sys
for item in (json.loads(sys.argv[1]) or [])[:10]:
    print('  - {} @ {} v{}  schema={}{}'.format(
        item.get('artifact_type', '?'),
        item.get('stage_key', '?'),
        item.get('version', '?'),
        item.get('schema_key', '?'),
        '' if item.get('is_current') else '  (superseded)',
    ))
" "$LAST_BODY"

http GET "/api/runs/${RUN_ID}/board"
[ "$LAST_STATUS" = "200" ] || fail
say "board 汇总：$(python3 -c "
import json, sys
board = json.loads(sys.argv[1])
print('阶段 {} 个 / 待审 {} 条 / 可用修复动作 {} 个 / 成片 {} 个'.format(
    len(board.get('stage_lanes') or []),
    len(board.get('review_requests') or []),
    len(board.get('repair_actions') or []),
    len(board.get('final_outputs') or []),
))
" "$LAST_BODY")"

say ""
say "打开工作台看这条 run：${BASE_URL}/workspace?run_id=${RUN_ID}"
if [ "$STATUS" = "failed" ] || [ "$STATUS" = "cancelled" ]; then
  exit 1
fi
exit 0
