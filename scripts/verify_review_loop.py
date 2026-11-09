#!/usr/bin/env python3
"""End-to-end verification of the deployed stack against a running instance.

Covers the V6.3 review system (sections 1-10), the T75/T76 risk gates
(sections 11-12), the V6.4 project asset library (section 13), the V6.5 team /
permission layer (section 14), the V7.1 HTTP API contract (section 15), the
V7.2 programmatic credentials the SDK is built on (section 16), the V7.3
outbound webhooks (section 17), the V7.4 embeddable workspace (section 18),
the V6.6 review attachments (section 19), the V8.1 worker scaling surface
(section 20), the V8.2 observability surface (section 21), the V8.3 team
quotas (section 22) and the V8.4 security hardening (section 23).

Talks to the HTTP API only, so it exercises the full stack — routes, request
validation, the review kernel, invalidation propagation, the risk gates, the
permission policy, API-key authentication, the webhook outbox, embed sessions
and the Web pages — and not just the store layer the unit tests already cover.

Run it against a deployed stack (see docs/CURRENT_CONTEXT.md):

    docker compose exec -T api python - < scripts/verify_review_loop.py

or against a local uvicorn:

    python scripts/verify_review_loop.py --base-url http://127.0.0.1:8080

Exit code 0 means every assertion held. Each check prints PASS/FAIL with the
observed value, so a failure on a remote box is diagnosable from the log alone.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
PIPELINE_KEY = "spokesperson_ad"

#: The frozen contract version (docs/API_STABILITY.md). Section 25 asserts the
#: running instance still agrees with it; the literal is here rather than derived
#: from the app so that comparing "what we built" against "what we meant" is a
#: comparison rather than a tautology.
FROZEN_VERSION = "1.1.0"

RAW_SCRIPT = (
    "开场三秒用一个反常识的问题抓住注意力。"
    "然后介绍产品解决了什么具体问题，给出一个可验证的数据。"
    "最后用一句有记忆点的行动号召收尾。"
)

_failures: list[str] = []
_checks = 0


# --------------------------------------------------------------------------- #
# tiny assertion layer
# --------------------------------------------------------------------------- #


def check(label: str, condition: bool, detail: Any = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  PASS  {label}" + (f"  -> {detail}" if detail != "" else ""))
        return
    print(f"  FAIL  {label}  -> {detail}")
    _failures.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    def __init__(self, status: int, payload: Any, *, method: str, path: str) -> None:
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload
        self.method = method
        self.path = path

    @property
    def _envelope(self) -> dict[str, Any]:
        """FastAPI wraps the review-gate payload as ``{"detail": {...}}``."""
        if isinstance(self.payload, dict):
            detail = self.payload.get("detail")
            if isinstance(detail, dict):
                return detail
            return self.payload
        return {}

    @property
    def code(self) -> str | None:
        return self._envelope.get("code")


def call(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent_headers = dict(headers or {})
    if data:
        sent_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=sent_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        raise ApiError(exc.code, payload, method=method, path=path) from exc


def call_ex(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[Any, dict[str, str]]:
    """Like :func:`call` but also returns the response headers (lower-cased).

    ``Idempotency-Replayed`` and ``X-Total-Count`` are only observable here.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent_headers = dict(headers or {})
    if data:
        sent_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=sent_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            payload: Any = json.loads(raw) if raw else None
            return payload, {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        raise ApiError(exc.code, payload, method=method, path=path) from exc


def call_raw(base_url: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def call_page(base_url: str, path: str) -> tuple[int, dict[str, str], str]:
    """Like :func:`call_raw`, but keeps the response headers.

    The embed gate has to read ``Content-Security-Policy``: for an embeddable page
    the framing allowlist *is* the security boundary, so a check that only looked
    at the HTML body would pass while the boundary was wide open.
    """
    request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
        return exc.code, headers, exc.read().decode("utf-8", errors="replace")


def denial_reason(exc: ApiError) -> str | None:
    """The ``reason`` field a permission denial carries (V6.5 / V7.4)."""
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return None
    reason = detail.get("reason")
    return str(reason) if reason else None


def expect_error(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any],
    *,
    status: int,
    code: str,
) -> None:
    try:
        call(base_url, method, path, body)
    except ApiError as exc:
        check(
            f"{method} {path} -> HTTP {status} {code}",
            exc.status == status and exc.code == code,
            f"got HTTP {exc.status} code={exc.code} payload={exc.payload}",
        )
        return
    check(f"{method} {path} -> HTTP {status} {code}", False, "request unexpectedly succeeded")


# --------------------------------------------------------------------------- #
# verification steps
# --------------------------------------------------------------------------- #


def verify_health(base_url: str) -> None:
    section("1. 服务健康")
    health = call(base_url, "GET", "/api/health")
    check("/api/health 返回", isinstance(health, dict), health)


def create_run(base_url: str) -> tuple[str, str]:
    section("2. 建项目与 run，并推进到第一道审核关口")
    project = call(
        base_url,
        "POST",
        "/api/projects",
        {"title": f"审核体系部署验证 {datetime.now(UTC):%Y-%m-%d %H:%M}"},
    )
    project_id = project["project_id"]
    check("项目已创建", bool(project_id), project_id)

    run = call(
        base_url,
        "POST",
        f"/api/projects/{project_id}/runs",
        {
            "pipeline_key": PIPELINE_KEY,
            "raw_script": RAW_SCRIPT,
            "brand_name": "部署验证品牌",
            "product_name": "部署验证产品",
            "target_platform": "douyin",
            "target_duration_seconds": 15,
        },
    )
    run_id = run["run_id"]
    check("run 已创建", bool(run_id), run_id)
    check("run 初始状态为 draft", run["status"] == "draft", run["status"])

    started = call(base_url, "POST", f"/api/runs/{run_id}/start")
    check("run 已启动", started["status"] != "draft", started["status"])
    check(
        "run 停在第一道审核关口 waiting_review",
        started["status"] == "waiting_review",
        {"status": started["status"], "current_stage_key": started.get("current_stage_key")},
    )
    check("当前阶段是 plan", started["current_stage_key"] == "plan", started["current_stage_key"])
    return project_id, run_id


def verify_request_snapshot(base_url: str, run_id: str) -> str:
    section("3. 审核请求与策略快照")
    requests = call(base_url, "GET", f"/api/runs/{run_id}/review-requests?status=open")
    check("恰好一条 open 审核请求", len(requests) == 1, len(requests))
    request = requests[0]

    check("stage_key 正确", request["stage_key"] == "plan", request["stage_key"])
    check("轮次从 1 开始", request["iteration"] == 1, request["iteration"])
    check("门禁类型来自 DSL 策略", request["gate_type"] == "human_review", request["gate_type"])
    check(
        "审批对象被快照锁定",
        len(request["artifact_scope"]) >= 1,
        [a["artifact_type"] for a in request["artifact_scope"]],
    )
    check(
        "审核关注点已从 pipeline 快照带入",
        isinstance(request["review_focus"], list) and len(request["review_focus"]) >= 1,
        request["review_focus"],
    )
    check(
        "通过标准已从 pipeline 快照带入",
        isinstance(request["success_criteria"], list) and len(request["success_criteria"]) >= 1,
        request["success_criteria"],
    )
    check("通过后进入的下一阶段已知", request["gates_stage_key"] == "script", request["gates_stage_key"])

    first_ref = request["artifact_scope"][0]
    detail = call(base_url, "GET", f"/api/review-requests/{request['review_request_id']}")
    check("详情：决策历史为空（只追加起点）", detail["decisions"] == [], detail["decisions"])
    check("详情：评论为空", detail["comments"] == [], detail["comments"])
    check("详情：待办为空", detail["action_items"] == [], detail["action_items"])
    check("详情：当前决定为空", detail["current_decision"] is None, detail["current_decision"])
    return request["review_request_id"], first_ref  # type: ignore[return-value]


def verify_artifact_scope_lock(
    base_url: str,
    review_request_id: str,
    first_ref: dict[str, Any],
) -> None:
    section("4. 审批粒度锁定 (stage_run, artifact_scope)")
    # A ref outside the snapshotted scope is refused. (The sibling code
    # ``stale_artifact_scope`` covers the case where the artifacts themselves
    # moved on; over HTTP that also supersedes the request first, so that path
    # stays covered by the kernel unit tests.)
    expect_error(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/decisions",
        {
            "decision": "approve",
            "artifact_refs": [
                {
                    "artifact_id": "artifact_not_in_scope",
                    "artifact_type": first_ref["artifact_type"],
                    "version": 999,
                }
            ],
        },
        status=409,
        code="invalid_artifact_scope",
    )
    still_open = call(base_url, "GET", f"/api/review-requests/{review_request_id}")
    check("被拒后请求仍然 open", still_open["status"] == "open", still_open["status"])
    check("被拒的决策没有落库", still_open["decisions"] == [], still_open["decisions"])


def verify_collaboration(base_url: str, review_request_id: str, first_ref: dict[str, Any]) -> str:
    section("5. 指派、逾期与评论")
    past_due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assigned = call(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/assign",
        {"assignee_user_id": "user_deploy_check", "due_at": past_due},
    )
    check("审核人已指派", assigned["assignee_user_id"] == "user_deploy_check", assigned["assignee_user_id"])
    check("过期时间已生效", assigned["due_at"] is not None, assigned["due_at"])
    check("逾期被正确判定", assigned["overdue"] is True, assigned["overdue"])

    comment = call(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/comments",
        {
            "body": "开场钩子不够强，品牌名写法与品牌规范不一致。",
            "severity": "critical",
            "artifact_id": first_ref["artifact_id"],
            "artifact_version": first_ref["version"],
            "author_user_id": "user_deploy_check",
            "author_display_name": "部署核对",
        },
    )
    check("评论已写入", bool(comment["comment_id"]), comment["comment_id"])
    check("严重度分级为 critical", comment["severity"] == "critical", comment["severity"])
    check("评论绑定到具体产物版本", comment["artifact_version"] == first_ref["version"], comment["artifact_version"])

    expect_error(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/comments",
        {"body": "指向不属于本次审批的产物", "artifact_id": "artifact_not_in_scope", "artifact_version": 1},
        status=409,
        code="invalid_artifact_scope",
    )
    return comment["comment_id"]


def verify_decision_and_action_items(
    base_url: str,
    run_id: str,
    review_request_id: str,
    comment_id: str,
) -> None:
    section("6. 带修改通过 -> 生成可追踪待办 -> 推进到下一关口")
    detail = call(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/decisions",
        {
            "decision": "approve_with_changes",
            "comment": "内容方向通过，但开场与品牌名按评论修改。",
            "reason_code": "content_issue",
            "actor_user_id": "user_deploy_check",
            "actor_display_name": "部署核对",
            "action_items": [
                {
                    "kind": "rerun_stage",
                    "stage_key": "plan",
                    "instruction": "重写开场钩子，并统一品牌名写法。",
                    "linked_comment_ids": [comment_id],
                }
            ],
        },
    )
    check("请求状态为 approved（带修改通过不阻断关口）", detail["status"] == "approved", detail["status"])
    check("当前决定为 approve_with_changes", detail["current_decision"] == "approve_with_changes", detail["current_decision"])
    check("决策历史已追加 1 条", len(detail["decisions"]) == 1, len(detail["decisions"]))
    check("sequence 从 1 开始", detail["decisions"][0]["sequence"] == 1, detail["decisions"][0]["sequence"])
    check("round 为 1", detail["decisions"][0]["round"] == 1, detail["decisions"][0]["round"])
    check("决策记录了审核人", detail["decisions"][0]["actor_display_name"] == "部署核对", detail["decisions"][0]["actor_display_name"])

    check("生成了 1 条待办", len(detail["action_items"]) == 1, len(detail["action_items"]))
    item = detail["action_items"][0]
    check("待办复用既有动作契约 rerun_stage", item["result_action"] == "rerun_stage", item["result_action"])
    check("待办状态为 open", item["status"] == "open", item["status"])
    check("待办关联了触发它的评论", item["linked_comment_ids"] == [comment_id], item["linked_comment_ids"])
    # The ask has not been delivered yet, so the comment must stay open. Closing
    # it here would erase the only signal that the fix is still owed.
    check("未完成的评论保持 open（不随通过被吞掉）", detail["comments"][0]["status"] == "open", detail["comments"][0]["status"])

    expect_error(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/decisions",
        {"decision": "approve"},
        status=409,
        code="review_request_closed",
    )

    run = call(base_url, "GET", f"/api/runs/{run_id}")
    check("run 推进到下一阶段 script", run["current_stage_key"] == "script", run["current_stage_key"])
    check("run 停在 script 关口", run["status"] == "waiting_review", run["status"])
    next_requests = call(base_url, "GET", f"/api/runs/{run_id}/review-requests?status=open")
    check("script 关口已自动开出新请求", len(next_requests) == 1, len(next_requests))
    check("新请求轮次为 1（新关口）", next_requests[0]["iteration"] == 1, next_requests[0]["iteration"])


def verify_invalidation(base_url: str, run_id: str) -> None:
    section("7. 审批不是永久通行证：重跑上游 -> 下游失效")
    result = call(
        base_url,
        "POST",
        f"/api/runs/{run_id}/state-actions",
        {"action": "rerun_stage", "stage_key": "plan", "reason": "部署验证：回退上游以检查失效传播"},
    )
    payload = result.get("result_payload") or {}
    superseded = payload.get("superseded_review_request_ids") or []
    invalidated = payload.get("invalidated_stage_keys") or []
    check("重跑作废了下游待审请求", len(superseded) >= 1, superseded)
    check("下游已通过的阶段被标记失效", "script" in invalidated, invalidated)

    board = call(base_url, "GET", f"/api/runs/{run_id}/board")
    check("board 暴露失效阶段", invalidated or "script" in (board.get("invalidated_stages") or []), board.get("invalidated_stages"))
    invalidated_lanes = [lane["stage"]["stage_key"] for lane in board["stage_lanes"] if lane["stage"].get("invalidation")]
    check("阶段泳道带失效标记", "script" in invalidated_lanes, invalidated_lanes)

    requests = call(base_url, "GET", f"/api/runs/{run_id}/review-requests")
    plan_requests = [r for r in requests if r["stage_key"] == "plan"]
    check("plan 关口重新开出请求", len(plan_requests) >= 2, [r["iteration"] for r in plan_requests])
    check(
        "重开后轮次递增",
        max(r["iteration"] for r in plan_requests) >= 2,
        sorted(r["iteration"] for r in plan_requests),
    )
    statuses = sorted(r["status"] for r in requests)
    check("历史请求保留为 superseded（只追加）", "superseded" in statuses, statuses)


def verify_board_and_inbox(base_url: str, project_id: str, run_id: str) -> None:
    section("8. Run Board 与审核收件箱")
    board = call(base_url, "GET", f"/api/runs/{run_id}/board")
    for key in ("review_requests", "review_timeline", "invalidated_stages"):
        check(f"board 含 {key}", key in board, type(board.get(key)).__name__)
    timeline = board["review_timeline"]
    check("决策时间线非空", len(timeline) >= 1, [(d["sequence"], d["decision"]) for d in timeline])
    check(
        "时间线按 sequence 递增",
        [d["sequence"] for d in timeline] == sorted(d["sequence"] for d in timeline),
        [d["sequence"] for d in timeline],
    )
    lanes_with_review = [lane["stage"]["stage_key"] for lane in board["stage_lanes"] if lane.get("review_request")]
    check("泳道带出各阶段最新审核请求", len(lanes_with_review) >= 1, lanes_with_review)
    metrics = board["metrics"]
    check("board 指标含审核计数", "review_request_count" in metrics, {k: v for k, v in metrics.items() if "review" in k})

    inbox = call(base_url, "GET", f"/api/projects/{project_id}/review-inbox")
    check("收件箱有内容", inbox["total"] >= 1, inbox["total"])
    check("收件箱含逾期计数", "overdue_total" in inbox, inbox["overdue_total"])
    item = inbox["items"][0]
    for key in (
        "iteration",
        "status",
        "current_decision",
        "assignee_user_id",
        "overdue",
        "open_critical_comments",
        "open_action_items",
    ):
        check(f"收件箱条目含 {key}", key in item, item.get(key))
    # The display layer needs the decision, because `approve` and
    # `approve_with_changes` share the status `approved`.
    approved_item = next(
        (row for row in inbox["items"] if row["status"] == "approved"), None
    )
    check(
        "已通过条目带出具体决定（区分带修改通过）",
        approved_item is not None and approved_item["current_decision"] == "approve_with_changes",
        approved_item,
    )

    filtered = call(base_url, "GET", f"/api/projects/{project_id}/review-inbox?assignee_user_id=user_deploy_check")
    check("只看我的 过滤生效", filtered["total"] >= 1, filtered["total"])

    section("9. 错误码映射")
    try:
        call(base_url, "GET", "/api/review-requests/reviewreq_does_not_exist")
        check("未知审核请求返回 404", False, "request unexpectedly succeeded")
    except ApiError as exc:
        check("未知审核请求返回 404", exc.status == 404, exc.status)

    expect_error(
        base_url,
        "POST",
        f"/api/runs/{run_id}/review-requests",
        {"stage_key": "storyboard"},
        status=409,
        code="stage_not_awaiting_review",
    )


def verify_pages(base_url: str) -> None:
    section("10. 五个 Web 页面")
    expected = {
        "/workspace": ["审核关口", "下一道审核关口", "reviewRequestCard", "已通过（带修改）", "最近决定"],
        "/dashboard": ["审核收件箱", "只看我的", "逾期审核", "失效阶段", "已通过（带修改）"],
        "/run-board": [
            "审核</h2>",
            "保存指派",
            "每次审核会产生一条不可修改的记录",
            "已失效",
            "已通过（带修改）",
        ],
        "/asset-library": [
            "资产库",
            "新建资产集合",
            "从 Run 沉淀资产",
            "复用到 Run",
            "资产条目",
            "授权",
        ],
        "/team": [
            "团队与权限",
            "当前身份",
            "生效权限",
            "团队成员",
            "项目成员",
            "审计日志",
            "权限矩阵",
        ],
    }
    for path, needles in expected.items():
        status, html = call_raw(base_url, path)
        check(f"GET {path} -> 200", status == 200, status)
        missing = [needle for needle in needles if needle not in html]
        check(f"{path} 含审核界面标记", not missing, missing)


def verify_intent_gate(base_url: str) -> None:
    """T75: declaring a quality mode must never be downgraded silently.

    Whether a downgrade actually happens depends on the deployment's provider
    keys, so both outcomes are asserted rather than assumed: a plan that reports
    a downgrade must refuse to start until it is acknowledged, and a plan that
    does not report one must start without friction.
    """
    section("11. 质量意图闸门（T75）：声明 provider 后不得静默降级")
    project = call(
        base_url,
        "POST",
        "/api/projects",
        {"title": f"意图闸门验证 {datetime.now(UTC):%H:%M:%S}"},
    )
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {
            "pipeline_key": "avatar_spokesperson",
            "raw_script": RAW_SCRIPT,
            "brand_name": "部署验证品牌",
            "product_name": "部署验证产品",
            "quality_mode": "provider_avatar",
        },
    )
    run_id = run["run_id"]
    check(
        "run 携带声明的质量模式创建成功",
        run["input_payload"].get("quality_mode") == "provider_avatar",
        run["input_payload"].get("quality_mode"),
    )

    preflight = call(base_url, "POST", f"/api/runs/{run_id}/preflight")
    stages = preflight["payload"]["stages"]
    capability_reports = [
        report
        for stage in stages
        for report in stage["required_capabilities"] + stage["optional_capabilities"]
    ]
    check(
        "preflight 每个 stage 均带意图降级清单字段",
        all("intent_mismatch_capabilities" in stage for stage in stages),
        len(stages),
    )
    check(
        "preflight 每个能力结论都带意图结论",
        bool(capability_reports)
        and all("intent_satisfied" in report for report in capability_reports),
        len(capability_reports),
    )

    plan_artifact = call(base_url, "POST", f"/api/runs/{run_id}/execution-plan")
    plan = plan_artifact["payload"]
    check("execution plan 暴露意图降级清单", "intent_downgrades" in plan, plan["execution_policy"])
    check("execution plan 的降级开关是布尔值", isinstance(plan.get("requires_intent_acknowledgement"), bool), plan.get("requires_intent_acknowledgement"))

    if plan["requires_intent_acknowledgement"]:
        downgrades = plan["intent_downgrades"]
        check("降级项给出原因", bool(downgrades[0].get("reason")), downgrades[0]["reason"])
        check(
            "降级项区分类型",
            all(item.get("kind") in {"capability_missing", "runtime_downgrade"} for item in downgrades),
            sorted({item.get("kind") for item in downgrades}),
        )
        check("执行策略标记为需确认", plan["execution_policy"] == "proceed_with_intent_downgrades", plan["execution_policy"])
        expect_error(
            base_url,
            "POST",
            f"/api/runs/{run_id}/start",
            {},
            status=400,
            code="intent_downgrade_not_acknowledged",
        )
        started = call(
            base_url,
            "POST",
            f"/api/runs/{run_id}/start",
            {"accept_intent_downgrade": True},
        )
        check("确认接受降级后可以启动", started["status"] != "draft", started["status"])
    else:
        check(
            "本环境 provider 可用，声明已被满足（无降级可验证）",
            True,
            "quality_mode 已满足，跳过阻断断言",
        )
        started = call(base_url, "POST", f"/api/runs/{run_id}/start", {})
        check("无降级时启动不被阻断", started["status"] != "draft", started["status"])

    board = call(base_url, "GET", f"/api/runs/{run_id}/board")
    check(
        "run board 暴露意图降级字段",
        "intent_downgrades" in board and "requires_intent_acknowledgement" in board,
        len(board.get("intent_downgrades") or []),
    )
    return run_id


def verify_cost_gate(base_url: str) -> str:
    """T76: reserve the plan's whole cost up front, and ask before the first charge.

    Whether a plan reserves anything depends on the deployment's provider keys, so
    both outcomes are handled rather than assumed: an unaffordable reservation must
    be refused at start, while a plan that reserves nothing must still say the run
    is unguarded instead of staying silent about it.
    """
    section("12. 预算预留与首次付费确认（T76）")
    project = call(
        base_url,
        "POST",
        "/api/projects",
        {"title": f"预算闸门验证 {datetime.now(UTC):%H:%M:%S}"},
    )

    # A run that never declared a budget must say so, not quietly allow spending.
    unguarded = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {
            "pipeline_key": "avatar_spokesperson",
            "raw_script": RAW_SCRIPT,
            "brand_name": "部署验证品牌",
            "product_name": "部署验证产品",
        },
    )
    call(base_url, "POST", f"/api/runs/{unguarded['run_id']}/preflight")
    plan = call(base_url, "POST", f"/api/runs/{unguarded['run_id']}/execution-plan")["payload"]
    reservation = plan.get("cost_reservation") or {}
    check("execution plan 暴露成本预留", bool(reservation), plan.get("execution_policy"))
    check(
        "预留额与币种字段完整",
        isinstance(reservation.get("reserved_cost"), (int, float))
        and bool(reservation.get("currency")),
        f"{reservation.get('reserved_cost')} {reservation.get('currency')}",
    )
    check(
        "未声明预算时状态标记为未配置",
        reservation.get("budget_status") == "not_configured",
        reservation.get("budget_status"),
    )
    check(
        "预留按选中工具逐项列出",
        isinstance(reservation.get("line_items"), list) and bool(reservation["line_items"]),
        len(reservation.get("line_items") or []),
    )

    board = call(base_url, "GET", f"/api/runs/{unguarded['run_id']}/board")
    check(
        "run board 暴露成本预留",
        (board.get("cost_reservation") or {}).get("reserved_cost")
        == reservation.get("reserved_cost"),
        (board.get("cost_reservation") or {}).get("reserved_cost"),
    )
    check(
        "run board 暴露待确认付费 provider 字段",
        "paid_providers_pending" in board,
        board.get("paid_providers_pending"),
    )

    consent = call(
        base_url,
        "POST",
        f"/api/runs/{unguarded['run_id']}/provider-consents",
        {"provider": "dashscope", "accept": True, "note": "deployment gate"},
    )
    check(
        "付费 provider 确认可写入",
        (consent.get("paid_provider_consents") or {}).get("dashscope") is True,
        consent.get("paid_provider_consents"),
    )
    board = call(base_url, "GET", f"/api/runs/{unguarded['run_id']}/board")
    check(
        "run board 读回付费确认",
        (board.get("paid_provider_consents") or {}).get("dashscope") is True,
        board.get("paid_provider_consents"),
    )
    revoked = call(
        base_url,
        "POST",
        f"/api/runs/{unguarded['run_id']}/provider-consents",
        {"provider": "dashscope", "accept": False},
    )
    check(
        "付费 provider 确认可撤销",
        "dashscope" not in (revoked.get("paid_provider_consents") or {}),
        revoked.get("paid_provider_consents"),
    )

    # A run with an explicit zero budget: the reservation must be enforced at start.
    strict = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {
            "pipeline_key": "avatar_spokesperson",
            "raw_script": RAW_SCRIPT,
            "brand_name": "部署验证品牌",
            "product_name": "部署验证产品",
            "cost_budget": 0,
        },
    )
    call(base_url, "POST", f"/api/runs/{strict['run_id']}/preflight")
    strict_plan = call(
        base_url, "POST", f"/api/runs/{strict['run_id']}/execution-plan"
    )["payload"]
    strict_reservation = strict_plan.get("cost_reservation") or {}
    check(
        "声明预算后预算状态已解析",
        strict_reservation.get("budget_status") in {"within_budget", "exceeds_budget"},
        strict_reservation.get("budget_status"),
    )
    if float(strict_reservation.get("reserved_cost") or 0.0) > 0.0:
        expect_error(
            base_url,
            "POST",
            f"/api/runs/{strict['run_id']}/start",
            {},
            status=400,
            code="cost_reservation_exceeds_budget",
        )
    else:
        check(
            "本环境无付费 provider，预留为 0 时不误阻断",
            True,
            "reserved_cost=0，跳过超预算断言",
        )
        started = call(base_url, "POST", f"/api/runs/{strict['run_id']}/start", {})
        check("预留为 0 时启动不受预算影响", started["status"] != "draft", started["status"])

    return unguarded["run_id"]


def verify_asset_library(base_url: str, project_id: str, run_id: str) -> str:
    """V6.4: a project asset library that survives across runs.

    The library owns no bytes — entries point at run assets — so the checks focus on
    what that indirection can get wrong: duplicate entries, lost provenance, and a
    reuse that forgets which rights the material came with.
    """
    section("13. 项目资产库（V6.4）：可复用素材 + 来源与授权")
    collection = call(
        base_url,
        "POST",
        f"/api/projects/{project_id}/asset-collections",
        {"name": "部署验证人物形象库", "kind": "person", "description": "验证脚本创建"},
    )
    collection_id = collection["collection_id"]
    check("资产集合已创建", collection_id.startswith("acol_"), collection_id)
    check("新建集合条目数为 0", collection["item_count"] == 0, collection["item_count"])

    uploaded = call(
        base_url,
        "POST",
        f"/api/runs/{run_id}/assets",
        {
            "asset_type": "person_image",
            "filename": "gate-person.png",
            "content_base64": base64.b64encode(b"gate person image").decode("ascii"),
            "mime_type": "image/png",
        },
    )
    asset_id = uploaded["asset_id"]
    check("为验证上传了一个可入库资产", bool(asset_id), asset_id)

    item_payload = {
        "asset_id": asset_id,
        "role": "person_image",
        "note": "部署门禁创建",
        "provenance": {
            "source": "generated",
            "provider": "dashscope",
            "source_run_id": run_id,
            "source_asset_id": asset_id,
        },
        "license": {
            "license_type": "licensed",
            "holder": "部署验证品牌",
            "usage_scope": "中国大陆 / 线上投放",
            "restrictions": ["不得用于医疗宣称"],
        },
    }
    first = call(
        base_url,
        "POST",
        f"/api/asset-collections/{collection_id}/items",
        item_payload,
    )
    check("资产条目已加入集合", first["item_id"].startswith("aitem_"), first["item_id"])
    check("条目带回用途角色", first["role"] == "person_image", first["role"])
    check(
        "条目保留来源信息",
        (first.get("provenance") or {}).get("source_run_id") == run_id,
        first.get("provenance"),
    )
    check(
        "条目保留授权信息",
        (first.get("license") or {}).get("license_type") == "licensed",
        first.get("license"),
    )

    # Adding the same asset again must not create a second entry.
    duplicate = call(
        base_url,
        "POST",
        f"/api/asset-collections/{collection_id}/items",
        item_payload,
    )
    check("重复加入同一资产不产生新条目", duplicate["item_id"] == first["item_id"], duplicate["item_id"])
    detail = call(base_url, "GET", f"/api/asset-collections/{collection_id}")
    check("集合条目数仍为 1", detail["item_count"] == 1, detail["item_count"])

    # Promotion from a run is the way a project's material actually accumulates.
    bulk = call(
        base_url,
        "POST",
        f"/api/projects/{project_id}/asset-collections",
        {"name": "部署验证项目资产库", "kind": "project_asset"},
    )
    imported = call(
        base_url,
        "POST",
        f"/api/asset-collections/{bulk['collection_id']}/import-from-run",
        {
            "run_id": run_id,
            "role": "reference",
            "license": {"license_type": "owned", "holder": "部署验证品牌"},
        },
    )
    check(
        "从 run 沉淀资产成功",
        imported["imported_count"] >= 1,
        {"imported": imported["imported_count"], "skipped": imported["skipped_asset_ids"]},
    )
    reimported = call(
        base_url,
        "POST",
        f"/api/asset-collections/{bulk['collection_id']}/import-from-run",
        {
            "run_id": run_id,
            "role": "reference",
            "license": {"license_type": "owned", "holder": "部署验证品牌"},
        },
    )
    check(
        "重复沉淀只跳过不重复",
        reimported["imported_count"] == 0 and len(reimported["skipped_asset_ids"]) >= 1,
        {"imported": reimported["imported_count"], "skipped": len(reimported["skipped_asset_ids"])},
    )

    library = call(base_url, "GET", f"/api/projects/{project_id}/asset-library")
    check(
        "项目资产库聚合接口返回集合与统计",
        library["totals"]["collection_count"] >= 2 and library["totals"]["item_count"] >= 2,
        library["totals"],
    )

    # Reuse must produce a run-owned asset that still knows where it came from.
    reused = call(
        base_url,
        "POST",
        f"/api/runs/{run_id}/asset-library/reuse",
        {"item_id": first["item_id"], "note": "部署门禁复用"},
    )
    check(
        "复用生成新资产（不覆盖原资产）",
        reused["asset"]["asset_id"] != asset_id,
        reused["asset"]["asset_id"],
    )
    check(
        "复用资产指向同一份素材",
        reused["asset"]["storage_url"] == uploaded["storage_url"],
        reused["asset"]["storage_url"],
    )
    check(
        "复用资产带回来源与授权",
        reused["asset"]["asset_metadata"]["reused_from"]["asset_id"] == asset_id
        and reused["asset"]["asset_metadata"]["library_license"]["license_type"] == "licensed",
        reused["asset"]["asset_metadata"].get("reused_from"),
    )
    check(
        "复用结果回报复用自哪个条目",
        reused["reused_from_asset_id"] == asset_id
        and reused["source_collection_id"] == collection_id,
        reused["reused_from_asset_id"],
    )
    run_assets = call(base_url, "GET", f"/api/runs/{run_id}/assets")
    check(
        "复用资产已挂到目标 run",
        any(item["asset_id"] == reused["asset"]["asset_id"] for item in run_assets),
        len(run_assets),
    )

    removed = call(base_url, "DELETE", f"/api/asset-library/items/{first['item_id']}")
    check("条目移除为软删除", removed["status"] == "removed", removed["status"])
    items = call(base_url, "GET", f"/api/asset-collections/{collection_id}/items")
    check("已移除条目不再出现在清单", items == [], len(items))

    try:
        call(base_url, "GET", "/api/asset-collections/acol_does_not_exist")
        check("未知资产集合返回 404", False, "request unexpectedly succeeded")
    except ApiError as exc:
        check("未知资产集合返回 404", exc.status == 404, exc.status)

    try:
        call(
            base_url,
            "POST",
            f"/api/asset-collections/{collection_id}/items",
            {"asset_id": "asset_does_not_exist"},
        )
        check("加入未知资产返回 404", False, "request unexpectedly succeeded")
    except ApiError as exc:
        check("加入未知资产返回 404", exc.status == 404, exc.status)

    try:
        call(
            base_url,
            "POST",
            "/api/runs/run_does_not_exist/asset-library/reuse",
            {"item_id": first["item_id"]},
        )
        check("复用到未知 run 返回 404", False, "request unexpectedly succeeded")
    except ApiError as exc:
        check("复用到未知 run 返回 404", exc.status == 404, exc.status)

    return collection_id


def verify_team_permissions(base_url: str, project_id: str, run_id: str) -> str:
    """V6.5: request identity, team/project roles and the audit trail.

    The identity is carried in a trusted header (``X-Actor-Id``), and the
    fallback identity is an internal owner — which is what keeps the deployment
    gate itself, and every pre-V6.5 caller, working without a credential.
    """

    section("14. 团队与权限（V6.5）")

    # --- identity ---------------------------------------------------------
    profile = call(base_url, "GET", "/api/me")
    check("GET /api/me -> 默认身份", profile.get("source") == "default", profile.get("source"))
    check("默认身份是团队 owner", profile.get("team_role") == "owner", profile.get("team_role"))
    check(
        "默认身份拥有全部权限",
        len(profile.get("permissions") or []) == 23,
        len(profile.get("permissions") or []),
    )

    ghost = call(base_url, "GET", "/api/me", headers={"X-Actor-Id": "verify_ghost"})
    check("未登记的请求身份不会被自动入座", ghost.get("team_role") is None, ghost.get("team_role"))
    check("未登记身份权限为空", ghost.get("permissions") == [], ghost.get("permissions"))

    # --- permission matrix ------------------------------------------------
    matrix = call(base_url, "GET", "/api/permissions")
    check("权限键共 23 个", len(matrix.get("permissions") or []) == 23)
    check("团队角色 4 种", len(matrix.get("team_roles") or []) == 4)
    check("项目角色 4 种", len(matrix.get("project_roles") or []) == 4)
    project_grants = {
        grant for role in matrix.get("project_roles") or [] for grant in role["permissions"]
    }
    tenant_only = {
        "team:manage",
        "apikey:manage",
        "webhook:manage",
        "integration:manage",
        "quota:manage",
        "provider:manage",
        "deployment:manage",
    }
    check(
        "项目角色永远不会给出租户级权限"
        "（team:manage / apikey:manage / webhook:manage / integration:manage"
        " / quota:manage / provider:manage / deployment:manage）",
        not (tenant_only & project_grants),
        sorted(tenant_only & project_grants) or "项目角色未触及团队级权限",
    )

    # --- enforcement ------------------------------------------------------
    ghost_headers = {"X-Actor-Id": "verify_ghost"}
    try:
        call(base_url, "POST", "/api/projects", {"title": "ghost project"}, headers=ghost_headers)
        check("未登记身份建项目被拒（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未登记身份建项目被拒（403）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )

    denials = call(base_url, "GET", "/api/audit-logs?action=permission.denied&limit=50")
    check(
        "权限拒绝已写入审计",
        any(entry.get("actor_id") == "verify_ghost" for entry in denials),
        [entry.get("actor_id") for entry in denials][:5],
    )

    # --- teams -----------------------------------------------------------
    teams = call(base_url, "GET", "/api/teams")
    check("默认团队已自动创建", any(team.get("is_default") for team in teams), len(teams))

    existing = next((t for t in teams if t.get("slug") == "verify-team"), None)
    try:
        team = call(base_url, "POST", "/api/teams", {"name": "验证组", "slug": "verify-team"})
        check("POST /api/teams -> 201", team.get("slug") == "verify-team", team.get("slug"))
    except ApiError as exc:
        # The gate must be re-runnable against a deployed database that an
        # earlier run already seeded: a 409 on the fixed slug means the team
        # exists, so adopt it instead of demanding a fresh database.
        if not (exc.status == 409 and existing is not None):
            raise
        team = existing
        check(
            "POST /api/teams -> 201",
            team.get("slug") == "verify-team",
            "复用既有 verify-team（HTTP 409）",
        )
    team_id = team["team_id"]

    try:
        call(base_url, "POST", "/api/teams", {"name": "重复组", "slug": "verify-team"})
        check("重复 slug -> 409", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("重复 slug -> 409", exc.status == 409, f"HTTP {exc.status}")

    member = call(
        base_url,
        "POST",
        f"/api/teams/{team_id}/members",
        {"user_id": "verify_viewer", "role": "viewer", "name": "只读同学"},
    )
    membership_id = member["membership_id"]
    check("添加团队成员 -> role=viewer", member.get("role") == "viewer", member.get("role"))
    check(
        "成员权限是只读集合",
        sorted(member.get("permissions") or []) == sorted(
            {
                "project:read",
                "run:read",
                "asset:read",
                "library:read",
                "member:read",
                "quota:read",
                # V8.5: reading deployment posture is ordinary self-service, so
                # the key joins the read-only set every role inherits.
                "deployment:read",
            }
        ),
        member.get("permissions"),
    )

    again = call(
        base_url,
        "POST",
        f"/api/teams/{team_id}/members",
        {"user_id": "verify_viewer", "role": "member"},
    )
    check(
        "重复添加在同一行就地更新",
        again["membership_id"] == membership_id and again.get("role") == "member",
        f"{again['membership_id']} role={again.get('role')}",
    )
    members = call(base_url, "GET", f"/api/teams/{team_id}/members")
    check(
        "团队成员不会重复出现",
        [m["user_id"] for m in members].count("verify_viewer") == 1,
        [m["user_id"] for m in members],
    )

    changed = call(
        base_url,
        "PATCH",
        f"/api/team-memberships/{membership_id}",
        {"role": "viewer"},
    )
    check("PATCH 团队成员 -> viewer", changed.get("role") == "viewer", changed.get("role"))

    # A seated viewer reads but never writes.
    viewer_headers = {"X-Actor-Id": "verify_viewer"}
    listing = call(base_url, "GET", "/api/projects", headers=viewer_headers)
    check("只读成员可以列项目", isinstance(listing, list), type(listing).__name__)
    try:
        call(base_url, "POST", "/api/projects", {"title": "viewer project"}, headers=viewer_headers)
        check("只读成员建项目被拒（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "只读成员建项目被拒（403）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )
    try:
        call(base_url, "POST", "/api/teams", {"name": "viewer team"}, headers=viewer_headers)
        check("项目之外的团队级操作同样被拒", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("团队级操作不受只读成员影响（403）", exc.status == 403, f"HTTP {exc.status}")

    # --- project memberships ---------------------------------------------
    project_member = call(
        base_url,
        "POST",
        f"/api/projects/{project_id}/members",
        {"user_id": "verify_reviewer", "role": "reviewer"},
    )
    check("添加项目成员 -> role=reviewer", project_member.get("role") == "reviewer")
    project_members = call(base_url, "GET", f"/api/projects/{project_id}/members")
    check(
        "项目成员列表包含新成员",
        "verify_reviewer" in [m["user_id"] for m in project_members],
        [m["user_id"] for m in project_members],
    )
    try:
        call(
            base_url,
            "POST",
            f"/api/runs/{run_id}/start",
            None,
            headers={"X-Actor-Id": "verify_reviewer"},
        )
        check("未入座成员无法启动 run（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未入座成员无法启动 run（403）",
            exc.status == 403,
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- soft removal revokes --------------------------------------------
    removed = call(base_url, "DELETE", f"/api/team-memberships/{membership_id}")
    check("移除团队成员 -> status=removed", removed.get("status") == "removed", removed.get("status"))
    try:
        call(base_url, "GET", "/api/projects", headers=viewer_headers)
        check("移除后权限立即失效（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("移除后权限立即失效（403）", exc.status == 403, f"HTTP {exc.status}")

    # --- unknown entities are 404, not 403 --------------------------------
    try:
        call(base_url, "GET", "/api/teams/team_missing")
        check("未知团队 -> 404", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("未知团队 -> 404", exc.status == 404, f"HTTP {exc.status}")
    try:
        call(
            base_url,
            "POST",
            "/api/teams/team_missing/members",
            {"user_id": "user_x", "role": "member"},
        )
        check("未知团队加成员 -> 404", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("未知团队加成员 -> 404", exc.status == 404, f"HTTP {exc.status}")

    # --- audit trail ------------------------------------------------------
    entries = call(base_url, "GET", "/api/audit-logs?limit=200")
    actions = {entry.get("action") for entry in entries}
    check(
        "审计含团队与成员变更",
        {"team.created", "team.member_added", "team.member_role_changed", "team.member_removed"}
        <= actions,
        sorted(actions),
    )
    check(
        "审计含权限拒绝",
        "permission.denied" in actions,
        sorted(actions),
    )
    project_scoped = call(base_url, "GET", f"/api/audit-logs?project_id={project_id}&limit=50")
    check(
        "审计可按项目过滤",
        all(entry.get("project_id") == project_id for entry in project_scoped)
        and bool(project_scoped),
        len(project_scoped),
    )

    return team_id


def verify_api_contract(base_url: str) -> None:
    section("15. HTTP API v1 契约（V7.1）：错误信封 / 幂等键 / 分页")

    # --- the published error catalog --------------------------------------
    catalog = call(base_url, "GET", "/api/error-codes")
    check(
        "GET /api/error-codes 发布错误码目录",
        isinstance(catalog, list) and bool(catalog),
        len(catalog) if isinstance(catalog, list) else type(catalog).__name__,
    )
    codes = {entry.get("code") for entry in catalog}
    check(
        "目录含客户端要分支的关键码",
        {"not_found", "validation_error", "idempotency_conflict", "permission_denied"} <= codes,
        sorted(codes),
    )
    check(
        "每个码都带 status_code / category / retryable",
        all(
            isinstance(entry.get("status_code"), int)
            and 400 <= entry["status_code"] < 600
            and entry.get("category")
            and isinstance(entry.get("retryable"), bool)
            for entry in catalog
        ),
    )

    # --- 4xx now share one envelope ---------------------------------------
    try:
        call(base_url, "GET", "/api/projects/project_does_not_exist")
        check("未知项目 -> 404 not_found", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未知项目 -> 404 not_found",
            exc.status == 404 and exc.code == "not_found",
            f"HTTP {exc.status} code={exc.code}",
        )
        check(
            "404 也走统一信封（detail.code + detail.message）",
            isinstance(exc.payload.get("detail"), dict)
            and isinstance(exc.payload["detail"].get("message"), str)
            and bool(exc.payload["detail"]["message"]),
            exc.payload,
        )

    try:
        call(base_url, "POST", "/api/projects", {})
        check("请求体缺字段 -> 422 validation_error", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "请求体缺字段 -> 422 validation_error",
            exc.status == 422 and exc.code == "validation_error",
            f"HTTP {exc.status} code={exc.code}",
        )
        check(
            "422 带字段级 errors",
            bool((exc.payload.get("detail") or {}).get("errors")),
            (exc.payload.get("detail") or {}).get("errors"),
        )

    try:
        call(base_url, "DELETE", "/api/health")
        check("方法不支持 -> 405 method_not_allowed", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "方法不支持 -> 405 method_not_allowed",
            exc.status == 405 and exc.code == "method_not_allowed",
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- Idempotency-Key ---------------------------------------------------
    key = f"verify-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    first, _ = call_ex(
        base_url,
        "POST",
        "/api/projects",
        {"title": "幂等验证"},
        headers={"Idempotency-Key": key},
    )
    check("带幂等键的写入成功", bool(first.get("project_id")), first.get("project_id"))

    replay, replay_headers = call_ex(
        base_url,
        "POST",
        "/api/projects",
        {"title": "幂等验证"},
        headers={"Idempotency-Key": key},
    )
    check(
        "重放同 key 同 body 返回原结果",
        replay.get("project_id") == first.get("project_id"),
        f"{replay.get('project_id')} vs {first.get('project_id')}",
    )
    check(
        "重放响应带 Idempotency-Replayed: true",
        (replay_headers.get("idempotency-replayed") or "").lower() == "true",
        replay_headers.get("idempotency-replayed"),
    )

    try:
        call(
            base_url,
            "POST",
            "/api/projects",
            {"title": "换了 body"},
            headers={"Idempotency-Key": key},
        )
        check("同 key 不同 body -> 409 idempotency_conflict", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "同 key 不同 body -> 409 idempotency_conflict",
            exc.status == 409 and exc.code == "idempotency_conflict",
            f"HTTP {exc.status} code={exc.code}",
        )

    try:
        call(
            base_url,
            "POST",
            "/api/projects",
            {"title": "超长键"},
            headers={"Idempotency-Key": "x" * 300},
        )
        check("超长幂等键 -> 400 bad_request", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "超长幂等键 -> 400 bad_request",
            exc.status == 400 and exc.code == "bad_request",
            f"HTTP {exc.status} code={exc.code}",
        )

    plain_a = call(base_url, "POST", "/api/projects", {"title": "无幂等键"})
    plain_b = call(base_url, "POST", "/api/projects", {"title": "无幂等键"})
    check(
        "不带幂等键时行为完全不变（各自创建）",
        plain_a["project_id"] != plain_b["project_id"],
        f"{plain_a['project_id']} / {plain_b['project_id']}",
    )

    # --- pagination --------------------------------------------------------
    page = call(base_url, "GET", "/api/idempotency-keys?limit=5")
    check(
        "GET /api/idempotency-keys -> Page 信封",
        isinstance(page, dict) and {"items", "total", "limit", "offset"} <= set(page),
        list(page) if isinstance(page, dict) else type(page).__name__,
    )
    check(
        "幂等键列表含刚写入的 key",
        any(entry.get("idempotency_key") == key for entry in page.get("items") or []),
        [entry.get("idempotency_key") for entry in (page.get("items") or [])][:5],
    )
    check("Page.total >= 1", int(page.get("total") or 0) >= 1, page.get("total"))
    check("Page.limit 回显请求值", page.get("limit") == 5, page.get("limit"))

    raw_list, raw_headers = call_ex(base_url, "GET", "/api/audit-logs?limit=3")
    check(
        "既有裸数组端点保持原形状",
        isinstance(raw_list, list) and len(raw_list) <= 3,
        type(raw_list).__name__,
    )
    check(
        "裸数组端点回填 X-Total-Count",
        (raw_headers.get("x-total-count") or "").isdigit(),
        raw_headers.get("x-total-count"),
    )

    # --- the spec itself ---------------------------------------------------
    status, spec_raw = call_raw(base_url, "/openapi.json")
    check("OpenAPI 文档可获取", status == 200, status)
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError:
        spec = {}
    check(
        "OpenAPI 声明了版本号",
        bool((spec.get("info") or {}).get("version")),
        (spec.get("info") or {}).get("version"),
    )
    check(
        "OpenAPI 收录错误码目录端点",
        "/api/error-codes" in (spec.get("paths") or {}),
        sorted(spec.get("paths") or [])[:3],
    )


def verify_api_keys(base_url: str) -> str:
    """V7.2: programmatic credentials, and the contract the SDK depends on."""

    section("16. API key 与 SDK 契约（V7.2）：签发 / 鉴权 / 收窄 / 吊销")

    # --- the default identity can issue, and only sees the plaintext once ----
    created = call(base_url, "POST", "/api/api-keys", {"name": "gate 验证"})
    token = created.get("api_key")
    prefix = created.get("key_prefix")
    check(
        "POST /api/api-keys 返回一次性明文",
        isinstance(token, str) and token.startswith("s2v_") and token.count("_") == 2,
        f"prefix={prefix}",
    )
    check(
        "明文前缀与 key_prefix 一致",
        isinstance(token, str) and token.startswith(f"{prefix}_"),
        f"{token[:20] if isinstance(token, str) else token} vs {prefix}",
    )

    listed = call(base_url, "GET", "/api/api-keys")
    check(
        "GET /api/api-keys 返回 Page 信封",
        isinstance(listed, dict) and {"items", "total", "limit", "offset"} <= set(listed),
        list(listed) if isinstance(listed, dict) else type(listed).__name__,
    )
    entries = listed.get("items") or []
    check(
        "列表不回显明文（只留 prefix）",
        bool(entries) and all("api_key" not in entry for entry in entries),
        [sorted(entry)[:3] for entry in entries[:1]],
    )

    # --- a key is an identity ------------------------------------------------
    me = call(base_url, "GET", "/api/me", headers={"Authorization": f"Bearer {token}"})
    check(
        "Bearer key 解析为 api_key 身份",
        me.get("source") == "api_key" and bool(me.get("actor_id")),
        f"source={me.get('source')} actor={me.get('actor_id')}",
    )
    check(
        "身份可达 apikey:manage（拥有者）",
        "apikey:manage" in (me.get("permissions") or []),
        sorted(me.get("permissions") or [])[-3:],
    )

    # A key produces real work, not just an identity.
    project = call(
        base_url,
        "POST",
        "/api/projects",
        {"title": "SDK 验证项目"},
        headers={"Authorization": f"Bearer {token}"},
    )
    check("持有 key 可以真正写入", bool(project.get("project_id")), project.get("project_id"))

    # --- unusable keys read the same ----------------------------------------
    for label, candidate in (
        ("未知", f"s2v_{'0' * 12}_{'a' * 48}"),
        ("畸形", "not-a-key"),
        ("错密钥", f"{prefix}_{'b' * 48}"),
    ):
        try:
            call(base_url, "GET", "/api/me", headers={"Authorization": f"Bearer {candidate}"})
            check(f"{label} key -> 401 api_key_invalid", False, "奇偶校验：应抛错")
        except ApiError as exc:
            check(
                f"{label} key -> 401 api_key_invalid",
                exc.status == 401 and exc.code == "api_key_invalid",
                f"HTTP {exc.status} code={exc.code}",
            )

    # A non-bearer scheme is not an authentication attempt at all; the request
    # falls through to the trusted-header / default identity rather than failing.
    basic = call(base_url, "GET", "/api/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    check(
        "非 Bearer 的 Authorization 不影响身份解析",
        basic.get("source") in {"default", "header"},
        basic.get("source"),
    )

    # --- scopes narrow, never widen -----------------------------------------
    narrow = call(
        base_url,
        "POST",
        "/api/api-keys",
        {"name": "gate 只读", "scopes": ["project:read", "run:read"]},
    )
    read_token = narrow.get("api_key")
    check(
        "签发受收窄的 key（scopes 被回显）",
        narrow.get("scopes") == ["project:read", "run:read"],
        narrow.get("scopes"),
    )
    narrow_me = call(
        base_url, "GET", "/api/me", headers={"Authorization": f"Bearer {read_token}"}
    )
    check(
        "收窄 key 的 /api/me 只报它的 scopes",
        set(narrow_me.get("permissions") or []) == {"project:read", "run:read"},
        sorted(narrow_me.get("permissions") or []),
    )
    check(
        "收窄 key 在 scope 内可读",
        isinstance(
            call(base_url, "GET", "/api/projects", headers={"Authorization": f"Bearer {read_token}"}),
            list,
        ),
    )
    try:
        call(
            base_url,
            "POST",
            "/api/projects",
            {"title": "越权"},
            headers={"Authorization": f"Bearer {read_token}"},
        )
        check("收窄 key 越界 -> 403 permission_denied", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "收窄 key 越界 -> 403 permission_denied",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )
        check(
            "越界理由写明是 key 的 scope，而非角色",
            "not scoped" in str((exc.payload.get("detail") or {}).get("message") or ""),
            (exc.payload.get("detail") or {}).get("message"),
        )

    try:
        call(base_url, "POST", "/api/api-keys", {"name": "坏 scope", "scopes": ["nope:nope"]})
        check("未知 scope -> 400 bad_request", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未知 scope -> 400 bad_request",
            exc.status == 400 and exc.code == "bad_request",
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- revoking stops the key immediately ---------------------------------
    key_id = created.get("api_key_id")
    revoked = call(base_url, "POST", f"/api/api-keys/{key_id}/revoke")
    check("吊销返回 revoked 状态", revoked.get("status") == "revoked", revoked.get("status"))
    try:
        call(base_url, "GET", "/api/me", headers={"Authorization": f"Bearer {token}"})
        check("吊销后当月即失效", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "吊销后当月即失效",
            exc.status == 401 and exc.code == "api_key_invalid",
            f"HTTP {exc.status} code={exc.code}",
        )
    again = call(base_url, "POST", f"/api/api-keys/{key_id}/revoke")
    check("重复吊销是幂等的（不是错误）", again.get("status") == "revoked", again.get("status"))
    try:
        call(base_url, "POST", "/api/api-keys/key_does_not_exist/revoke")
        check("吊销未知 key -> 404", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("吊销未知 key -> 404", exc.status == 404, f"HTTP {exc.status}")

    # --- the endpoints the SDK is built on are in the published spec ---------
    spec_status, spec_raw = call_raw(base_url, "/openapi.json")
    check("SDK 视角下 OpenAPI 可获取", spec_status == 200, spec_status)
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError:
        spec = {}
    paths = spec.get("paths") or {}
    check(
        "OpenAPI 收录凭证生命周期端点",
        {"/api/api-keys", "/api/api-keys/{api_key_id}/revoke"} <= set(paths),
        sorted(p for p in paths if "api-key" in p),
    )
    sdk_ops = {
        "/api/projects/{project_id}/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/board",
        "/api/runs/{run_id}/start",
        "/api/runs/{run_id}/assets",
        "/api/assets/{asset_id}/download",
        "/api/runs/{run_id}/manifest-actions",
        "/api/jobs/{job_id}/retry",
    }
    missing = sorted(sdk_ops - set(paths))
    check(
        "SDK 六个业务封装依赖的端点全部在 spec 中",
        not missing,
        missing or "全部命中",
    )
    check(
        "spec 声明了 401（api_key_invalid 所在状态）",
        all(
            "401" in item.get(method, {}).get("responses", {})
            for path, item in paths.items()
            if path.startswith("/api/")
            for method in item
            if method in {"get", "post", "patch", "put", "delete"}
        ),
        "401 未出现在全部 /api 操作上",
    )

    return key_id


def verify_webhooks(base_url: str) -> str:
    """V7.3: outbound webhooks — catalog, subscriptions, outbox, replay.

    The gate drives the API surface only: the outbox is proven by the delivery
    rows a real run start leaves behind, and the worker's delivery loop itself
    (transport, backoff, auto-pause) is pinned by the unit tests with a scripted
    transport, since the gate cannot assume a worker is watching this instance.
    """

    section("17. Webhook（V7.3）：事件目录 / 订阅 / outbox / 重投")

    # --- the published vocabulary is open and static -------------------------
    catalog = call(base_url, "GET", "/api/webhook-events")
    check("GET /api/webhook-events 开放且返回目录", isinstance(catalog, list) and bool(catalog),
          type(catalog).__name__ if not isinstance(catalog, list) else len(catalog))
    names = [entry.get("event_type") for entry in catalog] if isinstance(catalog, list) else []
    check(
        "目录收录全部六个发布事件",
        names
        == [
            "run.started",
            "run.completed",
            "stage.waiting_review",
            "job.failed",
            "render.completed",
            "publish.completed",
        ],
        names,
    )
    check(
        "每个事件都带描述与内部来源",
        bool(catalog)
        and all(entry.get("description") and entry.get("sources") for entry in catalog),
        [sorted(entry) for entry in catalog[:1]] if isinstance(catalog, list) else catalog,
    )

    # --- validation: refuse a subscription that cannot work ------------------
    for label, url, events in (
        ("未知事件类型被拒绝（400 bad_request）", "https://ops.internal/h", ["nope.nope"]),
        ("非 http(s) URL 被拒绝", "ftp://ops.internal/h", ["run.started"]),
        ("URL 内嵌凭据被拒绝", "https://user:pw@ops.internal/h", ["run.started"]),
    ):
        try:
            call(base_url, "POST", "/api/webhooks",
                 {"name": "bad", "url": url, "event_types": events})
            check(label, False, "奇偶校验：应被拒绝")
        except ApiError as exc:
            check(label, exc.status == 400 and exc.code == "bad_request",
                  f"HTTP {exc.status} code={exc.code}")

    # --- subscription lifecycle ----------------------------------------------
    created = call(
        base_url,
        "POST",
        "/api/webhooks",
        {
            "name": "gate-hook",
            "url": "https://ops.internal/hooks/gate",
            "event_types": ["run.started", "run.started", "run.completed"],
        },
    )
    subscription_id = created.get("subscription_id")
    secret = created.get("secret")
    check(
        "创建订阅返回 id 与 64 位签名密钥",
        isinstance(subscription_id, str)
        and subscription_id.startswith("whsub_")
        and isinstance(secret, str)
        and len(secret) == 64,
        f"id={subscription_id} secret_len={len(secret) if isinstance(secret, str) else secret}",
    )
    check(
        "事件类型去重且保持顺序",
        created.get("event_types") == ["run.started", "run.completed"],
        created.get("event_types"),
    )

    listed = call(base_url, "GET", "/api/webhooks")
    check(
        "GET /api/webhooks 返回 Page 信封",
        isinstance(listed, dict) and {"items", "total", "limit", "offset"} <= set(listed),
        list(listed) if isinstance(listed, dict) else type(listed).__name__,
    )

    fetched = call(base_url, "GET", f"/api/webhooks/{subscription_id}")
    check("GET 单个订阅 200 且密钥可回读", fetched.get("secret") == secret, "secret mismatch")
    try:
        call(base_url, "GET", "/api/webhooks/whsub_absent")
        check("未知订阅 404 not_found", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("未知订阅 404 not_found", exc.status == 404, f"HTTP {exc.status}")

    patched = call(
        base_url, "PATCH", f"/api/webhooks/{subscription_id}", {"name": "gate-hook-renamed"}
    )
    check("PATCH 部分更新生效", patched.get("name") == "gate-hook-renamed", patched.get("name"))

    rotated = call(base_url, "POST", f"/api/webhooks/{subscription_id}/rotate-secret", {})
    check("轮换签名密钥后新旧不同", rotated.get("secret") not in (None, secret), "rotated")

    # --- the outbox: a real run leaves delivery rows -------------------------
    project = call(base_url, "POST", "/api/projects", {"title": "gate webhook"})
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "门禁 webhook 验证脚本", "pipeline_key": "avatar_spokesperson"},
    )
    # a second subscription that must NOT match run.started
    other = call(
        base_url,
        "POST",
        "/api/webhooks",
        {
            "name": "gate-hook-completed-only",
            "url": "https://ops.internal/hooks/gate-completed",
            "event_types": ["run.completed"],
        },
    )
    call(base_url, "POST", f"/api/runs/{run['run_id']}/start", {})
    deliveries = call(
        base_url, "GET", f"/api/webhooks/{subscription_id}/deliveries"
    )
    rows = deliveries.get("items") or []
    started_rows = [row for row in rows if row.get("event_type") == "run.started"]
    check(
        "run 启动后 outbox 出现 run.started 投递",
        bool(started_rows),
        [(row.get("event_type"), row.get("status")) for row in rows],
    )
    row = started_rows[0] if started_rows else {}
    payload = row.get("payload") or {}
    check(
        "投递行是事件的自描述快照",
        {
            "delivery_id",
            "event_id",
            "event_type",
            "run_id",
            "project_id",
            "occurred_at",
            "data",
        }
        <= set(payload)
        and payload.get("run_id") == run.get("run_id"),
        sorted(payload),
    )
    check(
        "投递行处于待投递状态（outbox 由 worker 排空）",
        row.get("status") in ("pending", "succeeded", "delivering"),
        row.get("status"),
    )
    other_deliveries = call(
        base_url, "GET", f"/api/webhooks/{other['subscription_id']}/deliveries"
    )
    check(
        "事件过滤按订阅生效（只订 run.completed 的订阅没有投递）",
        other_deliveries.get("total") == 0,
        other_deliveries.get("total"),
    )

    # --- manual replay -------------------------------------------------------
    requeued = call(
        base_url, "POST", f"/api/webhook-deliveries/{row.get('delivery_id')}/requeue", {}
    )
    check(
        "手动重投重置状态与次数",
        requeued.get("status") == "pending" and requeued.get("attempt") == 0,
        (requeued.get("status"), requeued.get("attempt")),
    )
    try:
        call(base_url, "POST", "/api/webhook-deliveries/whdel_absent/requeue", {})
        check("重投未知投递 404", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("重投未知投递 404", exc.status == 404, f"HTTP {exc.status}")

    # --- permission and audit -------------------------------------------------
    try:
        call(base_url, "GET", "/api/webhooks", None,
             headers={"X-Actor-Id": "gate-unseated-identity"})
        check("未入座身份管理订阅被 403", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未入座身份管理订阅被 403（租户级权限不入座即拒绝）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )
    logs = call(base_url, "GET", "/api/audit-logs?limit=200")
    actions = {entry.get("action") for entry in logs} if isinstance(logs, list) else set()
    check(
        "订阅创建进入审计流水",
        "webhook.subscription_created" in actions,
        sorted(a for a in actions if a and a.startswith("webhook.")) or "无 webhook.* 审计",
    )

    # --- contract -------------------------------------------------------------
    _status, spec_raw = call_raw(base_url, "/openapi.json")
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError:
        spec = {}
    paths = spec.get("paths") or {}
    webhook_paths = {p for p in paths if "webhook" in p}
    check(
        "OpenAPI 收录全部 webhook 端点",
        {
            "/api/webhook-events",
            "/api/webhooks",
            "/api/webhooks/{subscription_id}",
            "/api/webhooks/{subscription_id}/rotate-secret",
            "/api/webhooks/{subscription_id}/deliveries",
            "/api/webhook-deliveries/{delivery_id}/requeue",
        } <= webhook_paths,
        sorted(webhook_paths),
    )

    # --- cleanup: delete leaves nothing behind --------------------------------
    deleted = call(base_url, "DELETE", f"/api/webhooks/{subscription_id}")
    check("删除订阅 200 且回显被删对象", deleted.get("subscription_id") == subscription_id,
          deleted.get("subscription_id"))
    try:
        call(base_url, "GET", f"/api/webhooks/{subscription_id}")
        check("删除后订阅不可再读", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("删除后订阅不可再读", exc.status == 404, f"HTTP {exc.status}")
    call(base_url, "DELETE", f"/api/webhooks/{other['subscription_id']}")

    return subscription_id


# --------------------------------------------------------------------------- #


def verify_embeddable_workspace(base_url: str) -> tuple[str, str]:
    """V7.4: integrations, signed embed sessions, context pinning, framing.

    Three properties get exercised over the wire because they are the ones that
    cannot be seen from a unit test alone:

    * the minted link works end to end — a sibling run and a tenant-wide endpoint
      are both out of bounds while the pinned run is readable,
    * ``disabled`` is a real revocation lever for tokens that were minted *before*
      it was flipped (they are stateless, so nothing else can revoke them), and
    * the document response carries the framing allowlist, which is the only
      thing standing between a host page and the tenant's data.
    """

    section("18. 可嵌入工作台（V7.4）：集成注册 / 签名会话 / 上下文钉定 / 分帧")

    # --- a host platform registers -------------------------------------------
    # The name carries a timestamp so a re-run derives a fresh slug instead of
    # colliding with the previous one (see §14 for the fixed-slug variant).
    stamp = f"{datetime.now(UTC):%H%M%S}"
    name = f"Gate Embed {stamp}"
    origin = "https://gate.embed.test"
    integration = call(
        base_url,
        "POST",
        "/api/external-integrations",
        {"name": name, "allowed_origins": [f"{origin}/"]},
    )
    integration_id = integration.get("integration_id")
    check(
        "注册外部集成返回 extint_ id 且 slug 由名称派生",
        isinstance(integration_id, str)
        and integration_id.startswith("extint_")
        and integration.get("slug") == f"gate-embed-{stamp}",
        f"id={integration_id} slug={integration.get('slug')}",
    )
    check(
        "allowed_origins 归一化为完整 origin（去尾斜杠）",
        integration.get("allowed_origins") == [origin],
        integration.get("allowed_origins"),
    )

    capabilities = integration.get("capabilities") or {}
    check(
        "集成回显嵌入契约（页面 / scope 上限 / TTL 上限）",
        capabilities.get("pages") == ["run-board", "workspace"]
        and capabilities.get("max_ttl_seconds") == 86400
        and capabilities.get("embed_path") == "/embed",
        {key: capabilities.get(key) for key in ("pages", "max_ttl_seconds", "embed_path")},
    )
    allowed_scopes = set(capabilities.get("allowed_scopes") or [])
    check(
        "scope 上限含 run:review（唯一可选写权限）",
        "run:review" in allowed_scopes
        and {"project:read", "run:read", "asset:read", "library:read"} <= allowed_scopes,
        sorted(allowed_scopes),
    )
    check(
        "scope 上限不含 run:write / run:start（外宿主页面不得触发付费动作）",
        not {"run:write", "run:start"} & allowed_scopes,
        sorted(allowed_scopes),
    )

    # --- validation happens at write time, not at frame time -----------------
    expect_error(
        base_url,
        "POST",
        "/api/external-integrations",
        {"name": "Wild", "allowed_origins": ["*"]},
        status=400,
        code="bad_request",
    )
    expect_error(
        base_url,
        "POST",
        "/api/external-integrations",
        {"name": "Conflict", "slug": f"gate-embed-{stamp}"},
        status=409,
        code="conflict",
    )

    listed = call(base_url, "GET", "/api/external-integrations")
    check(
        "GET /api/external-integrations 返回 Page 信封",
        isinstance(listed, dict)
        and {"items", "total", "limit", "offset"} <= set(listed)
        and any(item.get("integration_id") == integration_id for item in listed.get("items", [])),
        listed.get("total") if isinstance(listed, dict) else type(listed).__name__,
    )
    fetched = call(base_url, "GET", f"/api/external-integrations/{integration_id}")
    check("GET 单个集成 200", fetched.get("integration_id") == integration_id, "id mismatch")
    try:
        call(base_url, "GET", "/api/external-integrations/extint_absent")
        check("未知集成 404 not_found", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("未知集成 404 not_found", exc.status == 404 and exc.code == "not_found",
              f"HTTP {exc.status} code={exc.code}")

    # --- a project to embed, and a run pinned inside it ----------------------
    project = call(base_url, "POST", "/api/projects", {"title": f"可嵌入验证 {stamp}"})
    embed_project_id = project["project_id"]
    paused = call(
        base_url,
        "POST",
        f"/api/projects/{embed_project_id}/runs",
        {"raw_script": "可嵌入工作台验证脚本", "pipeline_key": PIPELINE_KEY},
    )
    embedded_run = call(base_url, "POST", f"/api/runs/{paused['run_id']}/start", {})
    check(
        "被嵌入的 run 停在第一道审核关口",
        embedded_run.get("status") == "waiting_review",
        embedded_run.get("status"),
    )
    sibling = call(
        base_url,
        "POST",
        f"/api/projects/{embed_project_id}/runs",
        {"raw_script": "兄弟 run，不得被嵌入会话读到", "pipeline_key": PIPELINE_KEY},
    )

    # --- mint: every wrong link is refused where the caller can see why -------
    expect_error(
        base_url, "POST", f"/api/external-integrations/{integration_id}/embed-sessions",
        {"project_id": "project_absent", "actor_id": "gate_ext_user"},
        status=404, code="not_found",
    )
    expect_error(
        base_url, "POST", f"/api/external-integrations/{integration_id}/embed-sessions",
        {"project_id": embed_project_id, "run_id": "run_absent", "actor_id": "gate_ext_user"},
        status=404, code="not_found",
    )
    other_project = call(base_url, "POST", "/api/projects", {"title": f"另一个项目 {stamp}"})
    other_run = call(
        base_url,
        "POST",
        f"/api/projects/{other_project['project_id']}/runs",
        {"raw_script": "别的项目的 run", "pipeline_key": PIPELINE_KEY},
    )
    expect_error(
        base_url, "POST", f"/api/external-integrations/{integration_id}/embed-sessions",
        {
            "project_id": embed_project_id,
            "run_id": other_run["run_id"],
            "actor_id": "gate_ext_user",
        },
        status=404, code="not_found",
    )
    expect_error(
        base_url, "POST", f"/api/external-integrations/{integration_id}/embed-sessions",
        {
            "project_id": embed_project_id,
            "run_id": paused["run_id"],
            "actor_id": "gate_ext_user",
            "scopes": ["run:read", "run:write"],
        },
        status=400, code="bad_request",
    )

    issued = call(
        base_url,
        "POST",
        f"/api/external-integrations/{integration_id}/embed-sessions",
        {
            "project_id": embed_project_id,
            "run_id": paused["run_id"],
            "actor_id": "gate_ext_user",
            "actor_name": "门禁外部用户",
            "scopes": ["project:read", "run:read", "run:review"],
        },
    )
    token = issued.get("token") or ""
    embed_link = issued.get("embed_url") or ""
    check(
        "签发返回 s2v_embed_ 前缀的签名令牌",
        token.startswith("s2v_embed_") and token.count(".") == 1,
        token[:28] + "..." if token else token,
    )
    check(
        "嵌入链接把令牌放在 fragment、集成 id 放在路径",
        embed_link.endswith(f"#token={token}")
        and f"/embed/{integration_id}/run/{paused['run_id']}?page=" not in embed_link
        and f"/embed/{integration_id}/run/{paused['run_id']}" in embed_link,
        embed_link.replace(token, "<token>") if token else embed_link,
    )
    check(
        "上下文钉定到 project + run",
        issued.get("context", {}).get("project_id") == embed_project_id
        and issued.get("context", {}).get("run_id") == paused["run_id"],
        issued.get("context"),
    )
    check(
        "签发时 scope 被裁到上限内",
        set(issued.get("scopes") or []) == {"project:read", "run:read", "run:review"},
        issued.get("scopes"),
    )

    holder = {"X-Embed-Token": token}

    # --- the token describing itself -----------------------------------------
    described = call(base_url, "GET", f"/api/embed/{integration_id}/session", headers=holder)
    check(
        "嵌入会话自举端点由令牌本身建立身份",
        described.get("source") == "embed_session"
        and described.get("actor_id") == "gate_ext_user"
        and described.get("integration_id") == integration_id,
        {key: described.get(key) for key in ("source", "actor_id", "integration_id")},
    )
    check(
        "自举响应不给席位、角色或权限清单（嵌入会话没有席位）",
        not {"permissions", "team_role", "seat"} & set(described),
        sorted(described),
    )
    check(
        "返回剩余有效期（短时效令牌）",
        isinstance(described.get("remaining_seconds"), int)
        and 0 < described["remaining_seconds"] <= 900,
        described.get("remaining_seconds"),
    )

    try:
        call(base_url, "GET", f"/api/embed/{integration_id}/session")
        check("无令牌访问自举端点 401 embed_session_invalid", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "无令牌访问自举端点 401 embed_session_invalid",
            exc.status == 401 and exc.code == "embed_session_invalid",
            f"HTTP {exc.status} code={exc.code}",
        )
    for label, bad in (
        ("篡改令牌 401", "s2v_embed_bm90anNvbg.deadbeef"),
        ("前缀不符的词串 401", "not-a-token"),
    ):
        try:
            call(base_url, "GET", f"/api/embed/{integration_id}/session",
                 headers={"X-Embed-Token": bad})
            check(f"{label} embed_session_invalid", False, "奇偶校验：应抛错")
        except ApiError as exc:
            check(f"{label} embed_session_invalid",
                  exc.status == 401 and exc.code == "embed_session_invalid",
                  f"HTTP {exc.status} code={exc.code}")
    try:
        call(base_url, "GET", "/api/embed/extint_other/session", headers=holder)
        check("路径集成 id 与令牌不符 401", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("路径集成 id 与令牌不符 401（不能借 A 的许可渲染 B 的钉定 run）",
              exc.status == 401 and exc.code == "embed_session_invalid",
              f"HTTP {exc.status} code={exc.code}")

    # --- context pinning comes before scope ----------------------------------
    own_run = call(base_url, "GET", f"/api/runs/{paused['run_id']}", headers=holder)
    check("钉定的 run 可读", own_run.get("run_id") == paused["run_id"], own_run.get("run_id"))
    requests = call(
        base_url, "GET", f"/api/runs/{paused['run_id']}/review-requests", headers=holder
    )
    check(
        "钉定 run 的审核队列可读",
        isinstance(requests, list) and bool(requests),
        len(requests) if isinstance(requests, list) else type(requests).__name__,
    )

    try:
        call(base_url, "GET", f"/api/runs/{sibling['run_id']}", headers=holder)
        check("同项目的兄弟 run 被拒（钉定比项目更窄）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "同项目的兄弟 run 被拒为 embed_context（钉定先于 scope 判定）",
            exc.status == 403 and denial_reason(exc) == "embed_context",
            f"HTTP {exc.status} reason={denial_reason(exc)}",
        )
    for path in ("/api/teams", "/api/external-integrations", "/api/api-keys"):
        try:
            call(base_url, "GET", path, headers=holder)
            check(f"租户级端点 {path} 对嵌入会话不可达", False, "奇偶校验：应抛错")
        except ApiError as exc:
            check(
                f"租户级端点 {path} 对嵌入会话不可达（embed_context）",
                exc.status == 403 and denial_reason(exc) == "embed_context",
                f"HTTP {exc.status} reason={denial_reason(exc)}",
            )
    try:
        call(base_url, "POST", f"/api/runs/{paused['run_id']}/start", {}, headers=holder)
        check("上下文正确但 scope 不足 -> embed_scope", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "上下文正确但 scope 不足 -> embed_scope（两种拒绝不混为一谈）",
            exc.status == 403 and denial_reason(exc) == "embed_scope",
            f"HTTP {exc.status} reason={denial_reason(exc)}",
        )

    # --- the one write scope an embed may carry ------------------------------
    review_request_id = requests[0]["review_request_id"] if isinstance(requests, list) else None
    decided = call(
        base_url,
        "POST",
        f"/api/review-requests/{review_request_id}/decisions",
        {
            "decision": "approve",
            "comment": "在外宿主页面内通过",
            "actor_user_id": "gate_ext_user",
            "actor_display_name": "门禁外部用户",
        },
        headers=holder,
    )
    check(
        "带 run:review 的嵌入会话可在宿主页面内完成审核",
        decided.get("status") == "approved" and decided.get("current_decision") == "approve",
        (decided.get("status"), decided.get("current_decision")),
    )
    decisions = decided.get("decisions") or []
    check(
        "嵌入会话的决策以外部终端用户身份落库",
        bool(decisions) and decisions[-1].get("actor_user_id") == "gate_ext_user",
        [item.get("actor_user_id") for item in decisions],
    )

    logs = call(base_url, "GET", "/api/audit-logs?limit=200")
    # Match by *this* integration, not by position: a reused database keeps
    # audit rows from earlier gate runs (whose integrations are long deleted),
    # so "the last embed denial" is not necessarily this run's.
    embed_denials = [
        entry
        for entry in (logs if isinstance(logs, list) else [])
        if entry.get("action") == "permission.denied"
        and (entry.get("payload") or {}).get("integration_id") == integration_id
        and str((entry.get("payload") or {}).get("reason", "")).startswith("embed_")
    ]
    check(
        "嵌入会话的拒绝落审计并带 reason",
        bool(embed_denials),
        (embed_denials[0]["payload"].get("reason") if embed_denials else None),
    )

    # --- the document request: framing is the boundary -----------------------
    status, headers, html = call_page(base_url, f"/embed/{integration_id}")
    check("GET /embed/{integration_id} -> 200（首个文档请求不带任何凭据）", status == 200, status)
    check(
        "文档响应带 frame-ancestors 白名单",
        headers.get("content-security-policy") == f"frame-ancestors {origin}",
        headers.get("content-security-policy"),
    )
    check(
        "嵌入壳页面禁止缓存（逐集成生成，不得共享）",
        headers.get("cache-control") == "no-store",
        headers.get("cache-control"),
    )
    check(
        "文档响应不含令牌材料（令牌留在 fragment，不进服务端）",
        "s2v_embed_" not in html,
        "html 中出现 s2v_embed_ 前缀",
    )
    check(
        "嵌入壳含工作台标记与 postMessage 契约",
        all(
            marker in html
            for marker in (
                "s2v.embed.ready",
                "s2v.embed.resize",
                "s2v.embed.navigate",
                "URLSearchParams",
                "replaceState",
            )
        ),
        "缺少嵌入壳标记",
    )
    check(
        "嵌入壳不向 \"*\" 广播（只回 postMessage 给分帧方自己的 origin）",
        'postMessage' in html
        and 'postMessage(entry, "*")' not in html
        and "postMessage(payload, \"*\")" not in html,
        "postMessage 目标需要收窄",
    )

    _status, unknown_headers, _html = call_page(base_url, "/embed/extint_absent")
    check(
        "未知集成仍渲染页面但 frame-ancestors 退化为 'none'",
        unknown_headers.get("content-security-policy") == "frame-ancestors 'none'",
        unknown_headers.get("content-security-policy"),
    )

    # --- disabled is the revocation lever for links already in the wild ------
    call(base_url, "PATCH", f"/api/external-integrations/{integration_id}",
         {"status": "disabled"})
    _status, off_headers, _html = call_page(base_url, f"/embed/{integration_id}")
    check(
        "停用后文档仍渲染（宿主看到我们自己的错误态而非空白 iframe），但无人可分帧",
        _status == 200
        and off_headers.get("content-security-policy") == "frame-ancestors 'none'",
        (_status, off_headers.get("content-security-policy")),
    )
    expect_error(
        base_url, "POST", f"/api/external-integrations/{integration_id}/embed-sessions",
        {"project_id": embed_project_id, "actor_id": "gate_ext_user"},
        status=409, code="conflict",
    )
    try:
        call(base_url, "GET", f"/api/embed/{integration_id}/session", headers=holder)
        check("停用后旧链接立即失效（无逐令牌撤销点）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "停用后旧链接立即失效（stateless 令牌靠重读集成行收口）",
            exc.status == 401 and exc.code == "embed_session_invalid",
            f"HTTP {exc.status} code={exc.code}",
        )
    call(base_url, "PATCH", f"/api/external-integrations/{integration_id}", {"status": "active"})
    revived = call(base_url, "GET", f"/api/embed/{integration_id}/session", headers=holder)
    check(
        "重新启用后原链接恢复（令牌本身没过期）",
        revived.get("integration_id") == integration_id,
        revived.get("integration_id"),
    )

    # --- permission: a tenant-wide capability --------------------------------
    try:
        call(base_url, "GET", "/api/external-integrations", None,
             headers={"X-Actor-Id": "gate-unseated-identity"})
        check("未入座身份管理集成被 403", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未入座身份管理集成被 403（integration:manage 租户级，不入座即拒绝）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- contract -------------------------------------------------------------
    _status, spec_raw = call_raw(base_url, "/openapi.json")
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError:
        spec = {}
    paths = spec.get("paths") or {}
    integration_paths = {path for path in paths if "external-integrations" in path}
    check(
        "OpenAPI 收录全部外部集成端点",
        {
            "/api/external-integrations",
            "/api/external-integrations/{integration_id}",
            "/api/external-integrations/{integration_id}/embed-sessions",
        }
        <= integration_paths,
        sorted(integration_paths),
    )
    check(
        "开放的自举端点进 spec，而 HTML 嵌入页不进",
        "/api/embed/{integration_id}/session" in paths
        and "/embed/{integration_id}" not in paths,
        sorted(path for path in paths if path.startswith("/embed")),
    )
    error_codes = call(base_url, "GET", "/api/error-codes")
    check(
        "错误码目录收录 embed_session_invalid",
        any(entry.get("code") == "embed_session_invalid" for entry in error_codes),
        len(error_codes) if isinstance(error_codes, list) else type(error_codes).__name__,
    )

    # --- cleanup --------------------------------------------------------------
    removed = call(base_url, "DELETE", f"/api/external-integrations/{integration_id}")
    check("删除集成 200 且回显被删对象",
          removed.get("integration_id") == integration_id, removed.get("integration_id"))
    try:
        call(base_url, "GET", f"/api/embed/{integration_id}/session", headers=holder)
        check("删除后链接不可再验证", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check("删除后链接不可再验证", exc.status == 401, f"HTTP {exc.status}")
    expect_error(
        base_url, "DELETE", f"/api/external-integrations/{integration_id}", None,
        status=404, code="not_found",
    )

    return integration_id, embed_link.replace(token, "<token>")


def verify_review_attachments(base_url: str) -> str:
    """Section 19 — review attachments (V6.6): evidence mounts on a gate."""
    section("19. 审核附件（V6.6）：审核请求 ↔ 资产挂载")

    project = call(base_url, "POST", "/api/projects", {"title": "门禁·审核附件"})
    check("项目已创建", "project_id" in project, project.get("project_id"))
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "审核附件门禁脚本", "pipeline_key": "avatar_spokesperson"},
    )
    check("run 已创建", "run_id" in run, run.get("run_id"))
    started = call(base_url, "POST", f"/api/runs/{run['run_id']}/start", {})
    check(
        "run 停在第一道审核关口",
        started.get("status") == "waiting_review",
        started.get("status"),
    )
    requests = call(
        base_url, "GET", f"/api/runs/{run['run_id']}/review-requests?status=open"
    )
    request_id = requests[0]["review_request_id"]
    check("恰好一条 open 审核请求", len(requests) == 1, len(requests))

    asset = call(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/assets",
        {
            "asset_type": "person_image",
            "filename": "gate-attachment.png",
            "content_base64": base64.b64encode(b"gate attachment bytes").decode("ascii"),
            "mime_type": "image/png",
        },
    )
    check("证据资产已上传", "asset_id" in asset, asset.get("asset_id"))

    attachment = call(
        base_url,
        "POST",
        f"/api/review-requests/{request_id}/attachments",
        {"asset_id": asset["asset_id"], "note": "客户原始素材"},
    )
    check(
        "挂载返回附件行并内嵌资产快照",
        attachment.get("attachment_id", "").startswith("rvatt_")
        and (attachment.get("asset") or {}).get("asset_id") == asset["asset_id"],
        attachment.get("attachment_id"),
    )
    again = call(
        base_url,
        "POST",
        f"/api/review-requests/{request_id}/attachments",
        {"asset_id": asset["asset_id"], "note": "换备注再挂"},
    )
    check(
        "重复挂载幂等（同一行，首条备注保留）",
        again.get("attachment_id") == attachment["attachment_id"]
        and again.get("note") == "客户原始素材",
        again.get("attachment_id"),
    )
    listed = call(base_url, "GET", f"/api/review-requests/{request_id}/attachments")
    check("附件清单只有一条", len(listed) == 1, len(listed))
    detail = call(base_url, "GET", f"/api/review-requests/{request_id}")
    check(
        "Detail 内嵌附件",
        [item.get("attachment_id") for item in detail.get("attachments") or []]
        == [attachment["attachment_id"]],
        len(detail.get("attachments") or []),
    )

    other_project = call(
        base_url, "POST", "/api/projects", {"title": "门禁·附件外部项目"}
    )
    other_run = call(
        base_url,
        "POST",
        f"/api/projects/{other_project['project_id']}/runs",
        {"raw_script": "外部项目脚本", "pipeline_key": "avatar_spokesperson"},
    )
    foreign = call(
        base_url,
        "POST",
        f"/api/runs/{other_run['run_id']}/assets",
        {
            "asset_type": "person_image",
            "filename": "foreign.png",
            "content_base64": base64.b64encode(b"foreign bytes").decode("ascii"),
            "mime_type": "image/png",
        },
    )
    expect_error(
        base_url, "POST", f"/api/review-requests/{request_id}/attachments",
        {"asset_id": foreign["asset_id"]},
        status=404, code="not_found",
    )
    expect_error(
        base_url, "POST", f"/api/review-requests/{request_id}/attachments",
        {"asset_id": "asset_missing"},
        status=404, code="not_found",
    )
    expect_error(
        base_url, "POST", "/api/review-requests/reviewreq_missing/attachments",
        {"asset_id": asset["asset_id"]},
        status=404, code="not_found",
    )

    def _run_events() -> list[dict[str, Any]]:
        events = call(base_url, "GET", f"/api/runs/{run['run_id']}/events")
        if isinstance(events, dict):
            events = events.get("items") or events.get("events") or []
        return list(events)

    check(
        "挂载落 review.attachment_added 事件",
        any(e.get("event_type") == "review.attachment_added" for e in _run_events()),
        "ok",
    )

    removed = call(
        base_url, "DELETE", f"/api/review-attachments/{attachment['attachment_id']}"
    )
    check(
        "移除返回附件行",
        removed.get("attachment_id") == attachment["attachment_id"],
        removed.get("attachment_id"),
    )
    listed = call(base_url, "GET", f"/api/review-requests/{request_id}/attachments")
    check("移除后清单为空", listed == [], len(listed))
    expect_error(
        base_url, "DELETE", f"/api/review-attachments/{attachment['attachment_id']}", None,
        status=404, code="not_found",
    )
    check(
        "移除落 review.attachment_removed 事件",
        any(e.get("event_type") == "review.attachment_removed" for e in _run_events()),
        "ok",
    )

    return request_id


def verify_worker_scaling(base_url: str) -> str:
    """Section 20 — worker scaling (V8.1): queue affinity, retry, DLQ replay."""
    section("20. Worker Scaling（V8.1）：队列 / 优先级 / 重试 / 死信重放")

    project = call(base_url, "POST", "/api/projects", {"title": "门禁·Worker Scaling"})
    check("项目已创建", "project_id" in project, project.get("project_id"))
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "worker scaling 门禁脚本", "pipeline_key": "avatar_spokesperson"},
    )
    check("run 已创建", "run_id" in run, run.get("run_id"))

    job = call(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/jobs/avatar-from-assets",
        {"auto_archive": False, "auto_compose": False},
    )
    check("avatar 任务已入队", str(job.get("job_id", "")).startswith("job_"), job.get("job_id"))
    check(
        "付费生成任务落在 provider 队列",
        job.get("queue") == "provider",
        job.get("queue"),
    )
    check("默认优先级 50（0-99，越小越优先）", job.get("priority") == 50, job.get("priority"))
    check(
        "新任务无租约 / 开始时间 / 退避时间",
        job.get("lease_expires_at") is None
        and job.get("started_at") is None
        and job.get("next_run_at") is None,
        "ok",
    )

    listed = call(base_url, "GET", f"/api/runs/{run['run_id']}/jobs")
    check(
        "run 任务清单回显队列字段",
        any(
            item.get("job_id") == job.get("job_id") and item.get("queue") == "provider"
            for item in listed
        ),
        len(listed),
    )

    retried = call(base_url, "POST", f"/api/jobs/{job['job_id']}/retry", {})
    check(
        "手动重试消耗一次 attempt",
        retried.get("status") == "queued" and retried.get("attempt") == 2,
        retried.get("attempt"),
    )

    expect_error(
        base_url, "POST", f"/api/jobs/{job['job_id']}/requeue", None,
        status=409, code="conflict",
    )
    expect_error(
        base_url, "POST", "/api/jobs/job_absent/requeue", None,
        status=404, code="not_found",
    )

    return job["job_id"]


def verify_observability(base_url: str) -> None:
    """Section 21 — observability (V8.2): /metrics, request ids, the panel."""
    section("21. Observability（V8.2）：/metrics / 请求标识 / 成本延迟错误面板")

    status, metrics_text = call_raw(base_url, "/metrics")
    check("/metrics 免鉴权可抓取", status == 200, status)
    check(
        "Prometheus 文本暴露 HTTP 请求计数",
        "s2v_http_requests_total" in metrics_text,
        "ok" if "s2v_http_requests_total" in metrics_text else "missing",
    )

    spec = call(base_url, "GET", "/openapi.json")
    check(
        "/metrics 不进入 OpenAPI（抓取器不是 API 调用方）",
        "/metrics" not in spec.get("paths", {}),
        "ok",
    )

    _, health_headers = call_ex(base_url, "GET", "/api/health")
    check(
        "响应携带生成的 X-Request-Id",
        str(health_headers.get("x-request-id", "")).startswith("req_"),
        health_headers.get("x-request-id"),
    )
    _, echoed = call_ex(
        base_url, "GET", "/api/health", headers={"X-Request-Id": "req_gate_echo_7"}
    )
    check(
        "入站 X-Request-Id 原样回显",
        echoed.get("x-request-id") == "req_gate_echo_7",
        echoed.get("x-request-id"),
    )

    project = call(base_url, "POST", "/api/projects", {"title": "门禁·可观测性"})
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "observability 门禁脚本"},
    )
    call(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/jobs/avatar-from-assets",
        {"auto_archive": False, "auto_compose": False},
    )

    panel = call(
        base_url, "GET", f"/api/projects/{project['project_id']}/observability"
    )
    check(
        "面板回显项目并给出聚合时刻",
        panel.get("project_id") == project["project_id"]
        and panel.get("generated_at") is not None,
        panel.get("generated_at"),
    )
    check(
        "成本面板三视图齐全（provider / tool / source）",
        {"by_provider", "by_tool_key", "by_source_type"}.issubset(panel.get("cost", {})),
        sorted(panel.get("cost", {}).keys()),
    )
    check(
        "延迟面板给出样本数与分位数",
        {"sample_count", "avg_ms", "p50_ms", "p95_ms"}.issubset(panel.get("latency", {})),
        panel.get("latency", {}).get("sample_count"),
    )
    check(
        "作业面板看到刚入队的 provider 任务",
        panel.get("jobs", {}).get("by_queue", {}).get("provider", 0) >= 1
        and panel.get("jobs", {}).get("dead_letter") == 0,
        panel.get("jobs", {}).get("by_queue"),
    )
    check(
        "事件面板给出错误分类与最近错误",
        isinstance(panel.get("events", {}).get("errors_by_type"), dict)
        and isinstance(panel.get("events", {}).get("recent_errors"), list),
        "ok",
    )

    _, metrics_text = call_raw(base_url, "/metrics")
    check(
        "抓取内容包含 runs 与 background_jobs 仪表",
        's2v_runs{status="' in metrics_text
        and 's2v_background_jobs{dimension="queue"' in metrics_text,
        "ok",
    )

    expect_error(
        base_url,
        "GET",
        "/api/projects/proj_gate_missing/observability",
        None,
        status=404,
        code="not_found",
    )
    try:
        call(
            base_url,
            "GET",
            f"/api/projects/{project['project_id']}/observability",
            None,
            headers={"X-Actor-Id": "gate-unseated-identity"},
        )
        check("未入座身份读面板被 403", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未入座身份读面板被 403（面板复用 project:read，不入座即拒绝）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )


def verify_quota_billing(base_url: str) -> None:
    """Section 22 — team quotas (V8.3): the window, the gate, the override, the API.

    Reuses the team section 14 already created, and removes the quota it sets on
    the way out, so a second run against the same database starts from the same
    place as the first.
    """

    section("22. 配额与计费（V8.3）：周期窗口 / 硬软闸门 / 一次性豁免 / 归属")

    teams = call(base_url, "GET", "/api/teams")
    team = next((item for item in teams if item.get("slug") == "verify-team"), None)
    check("复用第 14 节创建的 verify-team", team is not None, (team or {}).get("team_id"))
    if team is None:
        return
    team_id = team["team_id"]

    # --- the read surface -------------------------------------------------
    overview = call(base_url, "GET", "/api/team-quotas")
    row = next((item for item in overview if item["team_id"] == team_id), None)
    check(
        "配额总览覆盖到该团队并给出周期",
        row is not None and row["period"] == "calendar_month",
        {"period": row["period"]} if row else "missing",
    )

    created = call(
        base_url,
        "PUT",
        f"/api/teams/{team_id}/quota",
        {"limit_cost": 25.0, "limit_runs": 0, "enforcement": "hard", "note": "门禁验证"},
    )
    check(
        "PUT 配额 -> 生效并给出周期窗口",
        created["configured"] is True
        and created["limit_runs"] == 0
        and created["period"] == "calendar_month"
        and created["usage"]["window_end"] is not None,
        {"period": created["period"], "window_end": created["usage"]["window_end"]},
    )
    check(
        "无用量时剩余额度等于上限",
        created["usage"]["cost_remaining"] == 25.0,
        created["usage"]["cost_remaining"],
    )

    again = call(
        base_url,
        "PUT",
        f"/api/teams/{team_id}/quota",
        {"limit_cost": 25.0, "limit_runs": 0, "enforcement": "hard"},
    )
    check(
        "PUT 幂等：同一团队仍是一行",
        again["quota_id"] == created["quota_id"],
        again["quota_id"],
    )

    # Section 14 deliberately removes its own viewer on the way out, so this
    # section seats its own: a fixed user id, re-added on every run (adding an
    # existing member updates the row in place).
    membership = call(
        base_url,
        "POST",
        f"/api/teams/{team_id}/members",
        {"user_id": "verify_quota_viewer", "role": "viewer", "name": "配额只读同学"},
    )
    check(
        "配额只读同学入座（role=viewer）",
        membership.get("role") == "viewer",
        membership.get("role"),
    )

    viewer_headers = {"X-Actor-Id": "verify_quota_viewer"}
    readers = call(base_url, "GET", "/api/team-quotas", headers=viewer_headers)
    check("只读成员可看配额（quota:read）", isinstance(readers, list), type(readers).__name__)
    try:
        call(
            base_url,
            "PUT",
            f"/api/teams/{team_id}/quota",
            {"limit_cost": 1.0},
            headers=viewer_headers,
        )
        check("只读成员改配额被拒（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "只读成员改配额被拒（403，quota:manage 是租户级权限）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- attribution + the hard gate --------------------------------------
    project = call(base_url, "POST", "/api/projects", {"title": "门禁·配额"})
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "配额门禁脚本"},
    )
    moved = call(
        base_url,
        "PATCH",
        f"/api/projects/{project['project_id']}/team",
        {"team_id": team_id},
    )
    check("PATCH 项目归属 -> 归入团队", moved["team_id"] == team_id, moved.get("team_id"))

    expect_error(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/start",
        None,
        status=400,
        code="team_quota_exceeded",
    )
    events = call(base_url, "GET", f"/api/runs/{run['run_id']}/events?limit=200")
    exceeded = [item for item in events if item["event_type"] == "run.team_quota_exceeded"]
    check(
        "拒绝先落事件再抛错，且指出越界的维度",
        bool(exceeded) and exceeded[-1]["payload"]["exceeded"] == ["runs"],
        exceeded[-1]["payload"].get("exceeded") if exceeded else "missing",
    )

    # --- the override -----------------------------------------------------
    override = call(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/quota-override",
        {"accept": True, "note": "门禁一次性放行"},
    )
    check(
        "一次性豁免记录批准人",
        override["accepted"] is True and override["accepted_by"] is not None,
        override.get("accepted_by"),
    )

    started = call(base_url, "POST", f"/api/runs/{run['run_id']}/start", {})
    check(
        "豁免后同一 run 可以启动",
        started.get("status") not in (None, "draft"),
        started.get("status"),
    )
    events = call(base_url, "GET", f"/api/runs/{run['run_id']}/events?limit=200")
    check(
        "豁免改写事件：出现 overridden",
        any(item["event_type"] == "run.team_quota_overridden" for item in events),
        sorted({item["event_type"] for item in events if "quota" in item["event_type"]}),
    )
    recorded = [
        item for item in events if item["event_type"] == "run.cost_reservation_recorded"
    ]
    check(
        "run 启动事件带上团队维度（未受管的 run 不隐形）",
        bool(recorded) and recorded[-1]["payload"]["team_quota"]["team_id"] == team_id,
        recorded[-1]["payload"].get("team_quota") if recorded else "missing",
    )

    # --- soft enforcement -------------------------------------------------
    call(
        base_url,
        "PUT",
        f"/api/teams/{team_id}/quota",
        {"limit_runs": 0, "enforcement": "soft"},
    )
    soft_project = call(base_url, "POST", "/api/projects", {"title": "门禁·配额·软"})
    soft_run = call(
        base_url,
        "POST",
        f"/api/projects/{soft_project['project_id']}/runs",
        {"raw_script": "软配额门禁脚本"},
    )
    call(
        base_url,
        "PATCH",
        f"/api/projects/{soft_project['project_id']}/team",
        {"team_id": team_id},
    )
    soft_started = call(base_url, "POST", f"/api/runs/{soft_run['run_id']}/start", {})
    check(
        "soft 配额只警告不拦截，run 照常启动",
        soft_started.get("status") not in (None, "draft"),
        soft_started.get("status"),
    )
    soft_events = call(base_url, "GET", f"/api/runs/{soft_run['run_id']}/events?limit=200")
    check(
        "soft 越界落 warning 事件",
        any(item["event_type"] == "run.team_quota_warning" for item in soft_events),
        sorted({item["event_type"] for item in soft_events if "quota" in item["event_type"]}),
    )

    # --- back to unguarded ------------------------------------------------
    removed = call(base_url, "DELETE", f"/api/teams/{team_id}/quota")
    check(
        "DELETE 配额 -> 回落未配置（返回删除后的视图）",
        removed["configured"] is False and removed["quota_id"] is None,
        removed.get("quota_id"),
    )
    overview = call(base_url, "GET", "/api/team-quotas")
    row = next((item for item in overview if item["team_id"] == team_id), None)
    check(
        "删除后团队仍在总览里，且显式标注未配置",
        row is not None and row["configured"] is False,
        {"configured": row["configured"]} if row else "missing",
    )
    expect_error(
        base_url,
        "DELETE",
        f"/api/teams/{team_id}/quota",
        None,
        status=404,
        code="not_found",
    )
    expect_error(
        base_url,
        "GET",
        "/api/teams/team_gate_missing/quota",
        None,
        status=404,
        code="not_found",
    )
    expect_error(
        base_url,
        "PATCH",
        "/api/projects/proj_gate_missing/team",
        {"team_id": None},
        status=404,
        code="not_found",
    )


# --------------------------------------------------------------------------- #
# section 23 — security hardening (V8.4)
# --------------------------------------------------------------------------- #

#: A provider name no registered tool uses, so a credential created here cannot
#: change any other section's availability picture — and the probe credential is
#: never deleted (there is no delete endpoint), so re-runs would otherwise pile
#: real-looking keys in front of the tool registry.
_GATE_PROBE_PROVIDER = "gate_security_probe"


def _sign_callback(secret: str, body: bytes, *, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify_security_hardening(base_url: str) -> None:
    """Section 23 — security hardening (V8.4).

    Two of the checks below depend on how the instance under test is configured,
    and rather than assume, the section *probes and then asserts the consistent
    behaviour* — the same stance section 11 takes for intent downgrade:

    * provider secret encryption is opt-in, so the credential created here is
      read back to see whether it landed in the vault or inline, and the vault
      assertions apply only in the case that actually happened;
    * callback signature verification is opt-in too, so an unsigned callback is
      sent first and the signed branch is exercised only if it was refused.

    Neither branch is skipped silently: both print which one was taken.
    """

    section("23. 安全加固（V8.4）：密钥落盘加密 / 端点接管 / 回调签名 / 上传门槛")

    # --- the static catalogue is env-independent --------------------------
    catalog = call(base_url, "GET", "/api/error-codes")
    codes = {entry["code"] for entry in catalog}
    check(
        "错误码目录含 V8.4 新增的两个码",
        {"provider_callback_invalid", "insecure_secret_configuration"} <= codes,
        sorted(codes & {"provider_callback_invalid", "insecure_secret_configuration"}),
    )

    # --- the takeover: provider credentials need provider:manage ----------
    try:
        call(
            base_url,
            "GET",
            "/api/provider-credentials",
            headers={"X-Actor-Id": "verify_security_ghost"},
        )
        check("未登记身份读 provider 凭证被拒（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未登记身份读 provider 凭证被拒（403，provider:manage 是租户级权限）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )

    listed = call(base_url, "GET", "/api/provider-credentials")
    check("租户 owner 可列出 provider 凭证", isinstance(listed, list), type(listed).__name__)

    secret_value = "gate-secret-value-0123456789"
    credential = call(
        base_url,
        "POST",
        "/api/provider-credentials",
        {
            "provider": _GATE_PROBE_PROVIDER,
            "label": "门禁探针",
            "secret_values": {"GATE_PROBE_API_KEY": secret_value},
        },
    )
    credential_id = credential["credential_id"]
    source = credential["secret_source"]
    check(
        "创建凭证 -> 报告密钥存放位置",
        source in {"vault", "inline"},
        source,
    )
    check(
        "凭证响应不含明文密钥（只给掩码预览）",
        secret_value not in json.dumps(credential),
        credential.get("secret_preview"),
    )

    if source == "vault":
        check(
            "加密开启：secret_source=vault 且 encrypted=true",
            credential["encrypted"] is True,
            {"source": source, "encrypted": credential["encrypted"]},
        )
        rotation = call(
            base_url, "POST", f"/api/provider-credentials/{credential_id}/rotate"
        )
        check(
            "rotate -> 重密封已有引用（幂等操作仍报计数）",
            rotation["resealed"] >= 1 and rotation["locked"] == 0,
            {"resealed": rotation["resealed"], "sealed": rotation["sealed"]},
        )
        check(
            "重密封后仍可解出（source=vault）",
            rotation["credential"]["secret_source"] == "vault",
            rotation["credential"]["secret_source"],
        )
        check(
            "vault 行标注算法与密钥版本",
            bool(rotation["secrets"])
            and rotation["secrets"][0]["algorithm"] == "AESGCM-256"
            and rotation["secrets"][0]["key_version"] >= 1,
            rotation["secrets"][0] if rotation["secrets"] else "missing",
        )
    else:
        check(
            "加密未开启（默认）：credential 以 inline 存放，行为与 V8.3 前一致",
            credential["encrypted"] is False,
            {"source": source, "encrypted": credential["encrypted"]},
        )
        expect_error(
            base_url,
            "POST",
            f"/api/provider-credentials/{credential_id}/rotate",
            None,
            status=503,
            code="insecure_secret_configuration",
        )

    # --- the audit trail never holds the value ----------------------------
    logs = call(
        base_url,
        "GET",
        f"/api/audit-logs?action=provider_credential.created&entity_id={credential_id}",
    )
    check(
        "凭证创建落审计，且只记字段名不记值",
        bool(logs)
        and logs[0]["payload"]["secret_names"] == ["GATE_PROBE_API_KEY"]
        and secret_value not in json.dumps(logs),
        logs[0]["payload"].get("secret_names") if logs else "missing",
    )

    # --- the callback: probe, then follow the branch ----------------------
    callback_payload = {"external_task_id": "gate_probe_task", "status": "succeeded"}
    try:
        call(
            base_url,
            "POST",
            f"/api/provider-callbacks/{_GATE_PROBE_PROVIDER}",
            callback_payload,
        )
        check(
            "回调未配置签名密钥时按现状接受（opt-in，跳过签名分支）",
            True,
            "未配置 PROVIDER_CALLBACK_SECRET",
        )
    except ApiError as exc:
        check(
            "回调签名密钥已配置：未签名回调被拒（401 provider_callback_invalid）",
            exc.status == 401 and exc.code == "provider_callback_invalid",
            f"HTTP {exc.status} code={exc.code}",
        )
        body = json.dumps(callback_payload).encode("utf-8")
        # The positive branch is only reachable when this script runs where the
        # shared secret is visible — which is exactly the documented invocation
        # (``docker compose exec -T api python - < ...``), since the script then
        # inherits the container's environment. Run from outside the cluster and
        # the opposite assertion still holds: a well-formed header that cannot be
        # right is refused rather than waved through on shape alone.
        local_secret = os.environ.get("PROVIDER_CALLBACK_SECRET")
        if local_secret:
            signed = _sign_callback(
                local_secret, body, timestamp=int(time.time())
            )
            call(
                base_url,
                "POST",
                f"/api/provider-callbacks/{_GATE_PROBE_PROVIDER}",
                callback_payload,
                headers={"X-S2V-Signature": signed},
            )
            check("正确签名回调放行（200）", True, "已用容器内密钥签名")
        else:
            check(
                "回调签名密钥不在脚本环境内，跳过正向分支",
                True,
                "从集群外运行时属预期；密钥在 api 容器内时可验证正向分支",
            )
        forged = f"t={int(time.time())},v1={'0' * 64}"
        try:
            call(
                base_url,
                "POST",
                f"/api/provider-callbacks/{_GATE_PROBE_PROVIDER}",
                callback_payload,
                headers={"X-S2V-Signature": forged},
            )
            check("伪造签名仍被拒（401）", False, "奇偶校验：应抛错")
        except ApiError as exc:
            check(
                "伪造签名仍被拒（401，不因头部形状而放行）",
                exc.status == 401 and exc.code == "provider_callback_invalid",
                f"HTTP {exc.status} code={exc.code}",
            )

    # --- the doors: upload admission --------------------------------------
    project = call(base_url, "POST", "/api/projects", {"title": "门禁·安全"})
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {"raw_script": "安全门禁脚本"},
    )

    good = call(
        base_url,
        "POST",
        f"/api/runs/{run['run_id']}/assets",
        {
            "asset_type": "video",
            "filename": "gate-clip.mp4",
            "content_base64": base64.b64encode(b"gate video bytes").decode("ascii"),
            "mime_type": "video/mp4",
        },
    )
    check("普通上传放行", good.get("asset_id") is not None, good.get("asset_id"))

    try:
        call(
            base_url,
            "POST",
            f"/api/runs/{run['run_id']}/assets",
            {
                "asset_type": "video",
                "filename": "innocent.mp4",
                "content_base64": base64.b64encode(
                    b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32
                ).decode("ascii"),
                "mime_type": "video/mp4",
            },
        )
        check("声明 mp4 实为可执行文件被拒（415）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "声明 mp4 实为可执行文件被拒（415，魔数嗅探）",
            exc.status == 415 and exc.code == "unsupported_media_type",
            f"HTTP {exc.status} code={exc.code}",
        )

    try:
        call(
            base_url,
            "POST",
            f"/api/runs/{run['run_id']}/assets",
            {
                "asset_type": "video",
                "filename": "gate-run.sh",
                "content_base64": base64.b64encode(b"echo hi").decode("ascii"),
                "mime_type": "application/x-sh",
            },
        )
        check("脚本类型被拒（415）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "脚本类型被拒（415，类型黑名单）",
            exc.status == 415 and exc.code == "unsupported_media_type",
            f"HTTP {exc.status} code={exc.code}",
        )

    # --- the doors: the signed link's ceiling -----------------------------
    short = call(
        base_url,
        "GET",
        f"/api/assets/{good['asset_id']}/access-link?expires_in_seconds=60",
    )
    check(
        "签名链接可按调用方要求签发（60s）",
        _remaining_seconds(short["expires_at"]) <= 61,
        short["expires_at"],
    )
    capped = call(
        base_url,
        "GET",
        f"/api/assets/{good['asset_id']}/access-link?expires_in_seconds=1000000000",
    )
    check(
        "签名链接 TTL 被夹到上限（不接受十年链接）",
        _remaining_seconds(capped["expires_at"]) < 1000000000,
        capped["expires_at"],
    )


def verify_deployment(base_url: str) -> None:
    """Section 24 — deployment posture and maintenance mode (V8.5).

    The section has one job the others do not: it deliberately *leaves* the
    instance in a state it did not find it in — maintenance mode on — and then
    has to put it back. That is the only honest way to test a write guard from
    the outside, and the ``finally`` below is not tidiness: a gate that exited
    while the box was refusing writes would be worse than no gate at all.

    Re-runnable against a deployed database: the member it seats is removed
    again, the settings it edits are overwritten on the next run, and the
    maintenance flag starts and ends off.
    """

    section("24. 部署姿态（V8.5）：姿态自省 / 迁移自检 / 维护模式")

    # --- posture is a read -------------------------------------------------
    posture = call(base_url, "GET", "/api/deployment")
    check(
        "GET /api/deployment -> 完整姿态（settings / migration / capabilities）",
        {"settings", "app_version", "environment", "migration", "capabilities", "table_count"}
        <= set(posture),
        sorted(posture),
    )
    check(
        f"姿态报告本进程版本（{FROZEN_VERSION}）",
        posture.get("app_version") == FROZEN_VERSION,
        posture.get("app_version"),
    )
    migration = posture.get("migration") or {}
    check(
        "迁移自检：head=20261109_0023 且当前库已在 head（pending 为空）",
        migration.get("head") == "20261109_0023"
        and migration.get("up_to_date") is True
        and migration.get("pending") == [],
        {"head": migration.get("head"), "current": migration.get("current")},
    )
    capabilities = posture.get("capabilities") or {}
    check(
        "能力开关只报布尔（五项，绝不落配置值）",
        set(capabilities)
        == {
            "secret_encryption",
            "provider_callback_signature",
            "asset_link_secret_secure",
            "structured_logs",
            "webhook_dispatch",
        }
        and all(isinstance(value, bool) for value in capabilities.values()),
        capabilities,
    )
    # The count includes alembic_version when the migrations created it, so a
    # migrated box reports one more than an ORM-assembled one. Asserting the floor
    # keeps the check meaningful without pinning an incidental bookkeeping table —
    # and the label deliberately names no total, because a label that says "35"
    # is wrong the next time a migration adds a table (V1.1: 36 including the
    # bookkeeping table, 35 application tables; the assertion below is unaffected).
    check(
        "表数不少于 34（含 alembic_version 记账表）",
        isinstance(posture.get("table_count"), int) and posture["table_count"] >= 34,
        posture.get("table_count"),
    )

    # --- the catalogue names the new refusal ------------------------------
    catalog = call(base_url, "GET", "/api/error-codes")
    entry = next(
        (item for item in catalog if item.get("code") == "maintenance_mode"),
        None,
    )
    check(
        "错误码目录含 maintenance_mode（503 / server / 可重试）",
        entry is not None
        and entry.get("status_code") == 503
        and entry.get("category") == "server"
        and entry.get("retryable") is True,
        entry,
    )

    # --- who may see posture, and who may move it -------------------------
    try:
        call(base_url, "GET", "/api/deployment", headers={"X-Actor-Id": "verify_deploy_ghost"})
        check("未登记身份读部署姿态被拒（403）", False, "奇偶校验：应抛错")
    except ApiError as exc:
        check(
            "未登记身份读部署姿态被拒（403，未入座就没有任何权限）",
            exc.status == 403 and exc.code == "permission_denied",
            f"HTTP {exc.status} code={exc.code}",
        )

    teams = call(base_url, "GET", "/api/teams")
    team = next((item for item in teams if item.get("slug") == "verify-team"), None)
    if team is None:
        team = call(base_url, "POST", "/api/teams", {"name": "验证组", "slug": "verify-team"})
    member = call(
        base_url,
        "POST",
        f"/api/teams/{team['team_id']}/members",
        {"user_id": "verify_deployment_member", "role": "member", "name": "值班同学"},
    )
    membership_id = member["membership_id"]
    member_headers = {"X-Actor-Id": "verify_deployment_member"}
    try:
        readable = call(base_url, "GET", "/api/deployment", headers=member_headers)
        check(
            "团队成员可读部署姿态（deployment:read 刻意宽授：能看才谈得上自助）",
            readable.get("app_version") == FROZEN_VERSION,
            readable.get("app_version"),
        )
        try:
            call(
                base_url,
                "PATCH",
                "/api/deployment",
                {"deployment_name": "member-was-here"},
                headers=member_headers,
            )
            check("团队成员不可改部署设置（403）", False, "奇偶校验：应抛错")
        except ApiError as exc:
            check(
                "团队成员不可改部署设置（403，deployment:manage 是租户级）",
                exc.status == 403 and exc.code == "permission_denied",
                f"HTTP {exc.status} code={exc.code}",
            )
    finally:
        call(base_url, "DELETE", f"/api/team-memberships/{membership_id}")

    # --- the write guard: stop changes, keep answering --------------------
    note = "门禁：维护窗口自检"
    # Counted *before* the flip so the section stays re-runnable against a
    # deployed database: the assertion at the end is "exactly one entry was
    # added", not "exactly one exists" — the latter an earlier run would already
    # have falsified.
    before_enabled = len(
        call(base_url, "GET", "/api/audit-logs?action=deployment.maintenance_enabled")
    )
    before_disabled = len(
        call(base_url, "GET", "/api/audit-logs?action=deployment.maintenance_disabled")
    )
    call(base_url, "POST", "/api/deployment/maintenance", {"enabled": True, "message": note})
    try:
        try:
            call(base_url, "POST", "/api/projects", {"title": "维护中不该建成的项目"})
            check("维护中普通写被拒（503 maintenance_mode）", False, "奇偶校验：应抛错")
        except ApiError as exc:
            envelope = exc.payload if isinstance(exc.payload, dict) else {}
            detail = envelope.get("detail") if isinstance(envelope.get("detail"), dict) else {}
            check(
                "维护中普通写被拒（503 maintenance_mode，且回显只读提示）",
                exc.status == 503
                and exc.code == "maintenance_mode"
                and detail.get("read_only") is True,
                f"HTTP {exc.status} code={exc.code}",
            )

        during = call(base_url, "GET", "/api/deployment")
        check(
            "维护是关写不是关灯：读姿态仍 200 且 maintenance_mode=true",
            during["settings"]["maintenance_mode"] is True
            and during["settings"]["maintenance_message"] == note,
            during["settings"].get("maintenance_message"),
        )
        health = call(base_url, "GET", "/api/health")
        check(
            "维护中健康探针仍答（探针答不出会换来一轮无谓重启）",
            isinstance(health, dict),
            health,
        )

        escape = call(
            base_url,
            "PATCH",
            "/api/deployment",
            {"deployment_name": "gate-maintenance-probe"},
        )
        check(
            "维护中 /api/deployment 仍可写（关掉它的开关本身就是这条路径的写）",
            escape.get("deployment_name") == "gate-maintenance-probe",
            escape.get("deployment_name"),
        )
    finally:
        # Unconditional. Whatever happened above, the instance goes back to
        # accepting writes before this section returns.
        call(base_url, "POST", "/api/deployment/maintenance", {"enabled": False})

    resumed = call(base_url, "POST", "/api/projects", {"title": "门禁·部署姿态"})
    check(
        "关闭维护后写立即恢复（建项目 201）",
        bool(resumed.get("project_id")),
        resumed.get("project_id"),
    )

    # --- the backup book (the fact, not the archive) ----------------------
    recorded = call(
        base_url,
        "POST",
        "/api/deployment/backups",
        {"backup_id": "gate-verify-backup", "note": "门禁记录"},
    )
    check(
        "记录备份 -> 姿态可读出上次备份时间（把「什么时候备份的」变成一次读）",
        recorded.get("last_backup_at") is not None
        and recorded.get("last_backup_id") == "gate-verify-backup",
        {"last_backup_at": recorded.get("last_backup_at")},
    )

    # --- the switch leaves a trail ----------------------------------------
    # Both transitions were exercised above, so both entries must exist. Only
    # transitions are audited — the section flips the flag twice and writes no
    # further entries, which is the property that keeps the log readable during
    # an incident instead of burying it in no-ops.
    enabled_log = call(base_url, "GET", "/api/audit-logs?action=deployment.maintenance_enabled")
    disabled_log = call(base_url, "GET", "/api/audit-logs?action=deployment.maintenance_disabled")
    check(
        "维护开关只按跃迁落审计（本次开启与关闭各新增一条，无空转条目）",
        len(enabled_log) == before_enabled + 1 and len(disabled_log) == before_disabled + 1,
        {
            "enabled": (before_enabled, len(enabled_log)),
            "disabled": (before_disabled, len(disabled_log)),
        },
    )


def verify_release(base_url: str) -> None:
    """V1.0: the contract freeze, checked against the running instance.

    ``tests/test_release.py`` pins the same things in the repository. This section
    exists because the two are not the same statement: the tests say "the code we
    committed agrees with the policy", this says "the image you deployed agrees
    with the code". A build that silently carries an older ``version.py`` passes
    the first and fails this one.

    Only the counts and the version are pinned. Their *values* are allowed to
    change — adding an error code or a pipeline is a normal, additive release —
    but the change has to be deliberate enough to edit this list and
    ``docs/API_STABILITY.md`` together.
    """
    section("25. 发布契约冻结（V1.0）：版本 / 目录 / 迁移")

    # --- the version, in the two places a client can read it --------------
    posture = call(base_url, "GET", "/api/deployment")
    spec = call(base_url, "GET", "/openapi.json")
    info = spec.get("info") or {}
    check(
        "版本两处一致：/openapi.json 的 info.version == /api/deployment 的 app_version",
        info.get("version") == posture.get("app_version"),
        {"openapi": info.get("version"), "posture": posture.get("app_version")},
    )
    check(
        f"契约冻结在 {FROZEN_VERSION}（SDK 生成器据此判断稳定性承诺已生效）",
        posture.get("app_version") == FROZEN_VERSION,
        posture.get("app_version"),
    )

    # --- the contract did not shrink on the way to 1.0 --------------------
    # A floor, not an equality: growing the surface is additive and allowed
    # (it is in the "no notice needed" column of the policy). Shrinking it
    # silently is what this catches.
    paths = spec.get("paths") or {}
    operations = sum(
        1
        for methods in paths.values()
        for method in methods
        if method.lower() in {"get", "post", "patch", "put", "delete"}
    )
    check(
        "OpenAPI 文档化端点不少于 125 path / 157 operation",
        len(paths) >= 125 and operations >= 157,
        {"paths": len(paths), "operations": operations},
    )

    # --- the freeze is only meaningful at head ----------------------------
    migration = posture.get("migration") or {}
    check(
        "契约只在 head 上有效：migration.up_to_date 为真且 pending 为空",
        migration.get("up_to_date") is True and migration.get("pending") == [],
        {"head": migration.get("head"), "current": migration.get("current")},
    )

    # --- the published catalogues ----------------------------------------
    catalog = call(base_url, "GET", "/api/error-codes")
    check("错误码目录恰好 28 条", len(catalog) == 28, len(catalog))
    codes = [entry.get("code") for entry in catalog]
    check("错误码不重复（重复会让客户端的 if 分支有一支永远走不到）", len(set(codes)) == len(codes))
    incomplete = [
        entry.get("code")
        for entry in catalog
        if not entry.get("description")
        or not entry.get("category")
        or not isinstance(entry.get("status_code"), int)
        or not isinstance(entry.get("retryable"), bool)
    ]
    check("每条错误码都带 status_code / category / retryable / description", not incomplete, incomplete)

    events = call(base_url, "GET", "/api/webhook-events")
    event_types = [entry.get("event_type") for entry in events]
    check(
        "webhook 事件目录恰好 6 条且事件名唯一",
        len(events) == 6 and len(set(event_types)) == len(event_types),
        len(events),
    )
    unmapped = [entry.get("event_type") for entry in events if not entry.get("sources")]
    check(
        "每个对外事件都映射到至少一个内部事件源（映射表是内外词汇的唯一桥）",
        not unmapped,
        unmapped,
    )

    pipelines = call(base_url, "GET", "/api/pipelines")
    keys = [entry.get("pipeline_key") for entry in pipelines]
    check(
        "pipeline 目录恰好 10 条且 key 唯一",
        len(pipelines) == 10 and len(set(keys)) == len(keys),
        keys,
    )
    stable = [entry for entry in pipelines if entry.get("status") != "draft"]
    check(
        "非 draft 的 pipeline 至少 4 条（V1.0 的发布标尺）",
        len(stable) >= 4,
        [entry.get("pipeline_key") for entry in stable],
    )
    unfinished = [
        entry.get("pipeline_key")
        for entry in pipelines
        if not entry.get("stages") or not entry.get("entrypoint_stage_key")
    ]
    check("每条 pipeline 都声明了 stages 与 entrypoint（在册不等于可跑）", not unfinished, unfinished)

    # --- the catalogues are readable without a credential -----------------
    # "Discoverable without reading our source" is the point of publishing them,
    # so an identity that has no seat at all must still get 200. If this ever
    # starts returning 403 the contract has quietly moved behind auth.
    ghost = {"X-Actor-Id": "verify_release_ghost"}
    unreadable = []
    for path in ("/api/error-codes", "/api/webhook-events", "/api/pipelines"):
        try:
            call(base_url, "GET", path, headers=ghost)
        except ApiError as exc:
            unreadable.append(f"{path} -> {exc.status}")
    check(
        "三个目录端点对未入座的外部身份仍可读（免凭据是它们的意义）",
        not unreadable,
        unreadable,
    )


def verify_decision_log(base_url: str) -> None:
    """P2: the run records *why*, and the record is read-only.

    ``event_logs`` says what happened and ``audit_logs`` says who changed what.
    Neither can answer "why this tool and not the other one", which is the
    question actually asked when a run produces something unexpected. The three
    things worth checking against a live deployment are therefore: the reasoning
    lands with the choice, the alternatives travel with it, and nothing in the
    surface lets you edit the record afterwards.
    """
    section("26. 决策审计日志（P2）：为什么选它 / 备选 / 只读")
    project = call(
        base_url,
        "POST",
        "/api/projects",
        {"title": f"决策日志验证 {datetime.now(UTC):%H:%M:%S}"},
    )
    run = call(
        base_url,
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        {
            "pipeline_key": "avatar_spokesperson",
            "raw_script": RAW_SCRIPT,
            "brand_name": "部署验证品牌",
            "product_name": "部署验证产品",
        },
    )
    run_id = run["run_id"]

    # --- empty is a valid answer for a run that has planned nothing yet -----
    empty = call(base_url, "GET", f"/api/runs/{run_id}/decision-log")
    check(
        "未做任何决策的 run 返回空日志而不是 404",
        empty.get("count") == 0 and empty.get("entries") == [],
        empty.get("count"),
    )

    # --- planning writes one row per capability ----------------------------
    call(base_url, "POST", f"/api/runs/{run_id}/execution-plan")
    log = call(base_url, "GET", f"/api/runs/{run_id}/decision-log")
    entries = log.get("entries") or []
    check(
        "执行计划把每个能力的取舍都写进日志",
        log.get("count", 0) > 0 and "tool_selection" in (log.get("categories") or []),
        {"count": log.get("count"), "categories": log.get("categories")},
    )
    check(
        "日志条目的 subject 不重复（当前视图一题一行）",
        len({entry.get("subject") for entry in entries}) == len(entries),
        len(entries),
    )
    picked = [entry for entry in entries if (entry.get("chosen") or {}).get("tool_key")]
    check("选中的决策带工具与理由", bool(picked) and all(entry.get("rationale") for entry in picked), len(picked))
    check(
        "选中的决策带备选方案（只有赢家不算可审计）",
        bool(picked) and all(entry.get("candidates") for entry in picked),
        [len(entry.get("candidates") or []) for entry in picked][:5],
    )
    with_candidates = [entry for entry in picked if entry.get("confidence") is not None]
    check(
        "置信度是拉开差距的度量，落在 0..1 且写明依据",
        bool(with_candidates)
        and all(0.0 <= entry["confidence"] <= 1.0 for entry in with_candidates)
        and all(
            entry.get("confidence_basis") in {"score_margin", "uncontested"}
            for entry in with_candidates
        ),
        len(with_candidates),
    )
    skipped = [entry for entry in entries if entry.get("decision") in {"blocked", "skip_optional"}]
    check(
        "被跳过/阻塞的能力也有记录（否则「为什么没有背景音乐」无法回答）",
        not skipped or all(entry.get("rationale") for entry in skipped),
        [entry.get("subject") for entry in skipped],
    )
    check(
        "机器决策标记 actor_type=system 且置信度留空于不可度量处",
        all(entry.get("actor_type") == "system" for entry in entries)
        and all(entry.get("confidence") is None for entry in skipped),
        sorted({entry.get("actor_type") for entry in entries}),
    )
    check(
        "决策指向产生它的产物（可从日志回溯到计划）",
        all(entry.get("source_artifact_id") for entry in picked),
        [entry.get("source_artifact_id") for entry in picked][:3],
    )

    # --- a human verdict becomes a decision too ----------------------------
    call(base_url, "POST", f"/api/runs/{run_id}/start", {})
    requests = call(base_url, "GET", f"/api/runs/{run_id}/review-requests?status=open")
    if requests:
        request_id = requests[0]["review_request_id"]
        call(
            base_url,
            "POST",
            f"/api/review-requests/{request_id}/decisions",
            {
                "decision": "approve",
                "comment": "决策日志验证：通过。",
                "actor_user_id": "user_deploy_check",
                "actor_display_name": "部署核对",
            },
        )
        human = call(
            base_url,
            "GET",
            f"/api/runs/{run_id}/decision-log?category=review_decision",
        )
        human_entries = human.get("entries") or []
        check(
            "人工裁决也落进同一本账",
            len(human_entries) == 1 and human_entries[0].get("decision") == "approve",
            len(human_entries),
        )
        check(
            "裁决记录审核人身份与理由",
            bool(human_entries)
            and human_entries[0].get("actor_id") == "user_deploy_check"
            and human_entries[0].get("rationale") == "决策日志验证：通过。",
            human_entries[0].get("actor_id") if human_entries else None,
        )
        check(
            "人类裁决不编造置信度与备选（没给他们比过的东西）",
            bool(human_entries)
            and human_entries[0].get("confidence") is None
            and human_entries[0].get("candidates") == [],
            human_entries[0].get("confidence") if human_entries else None,
        )
        # The category list is computed over the whole log, so a filter narrows
        # the entries without narrowing the chips the UI draws from it.
        unfiltered = call(base_url, "GET", f"/api/runs/{run_id}/decision-log")
        check(
            "分类过滤只收窄条目，不改变分类清单（否则筛选片会被自己筛掉）",
            human.get("categories") == unfiltered.get("categories")
            and all(entry.get("category") == "review_decision" for entry in human_entries),
            human.get("categories"),
        )
    else:
        check("本环境未开出审核请求（跳过人工裁决断言）", True, "no open review request")

    history = call(base_url, "GET", f"/api/runs/{run_id}/decision-log?include_history=true")
    check(
        "历史视图包含全部分类且条数不少于当前视图",
        history.get("history_included") is True
        and history.get("count", 0) >= log.get("count", 0),
        {"current": log.get("count"), "history": history.get("count")},
    )
    check(
        "历史视图按 sequence 升序（事件顺序即审计顺序）",
        [entry.get("sequence") for entry in history.get("entries") or []]
        == sorted(entry.get("sequence") for entry in history.get("entries") or []),
        len(history.get("entries") or []),
    )
    cut = call(base_url, "GET", f"/api/runs/{run_id}/decision-log?limit=1")
    check(
        "被 limit 截断时如实标记（沉默的截断会被读成完整）",
        cut.get("count") == 1
        and cut.get("truncated") is True
        and cut.get("total", 0) > 1,
        {"count": cut.get("count"), "total": cut.get("total"), "truncated": cut.get("truncated")},
    )

    expect_error(
        base_url,
        "GET",
        "/api/runs/run_does_not_exist/decision-log",
        None,
        status=404,
        code="not_found",
    )

    # --- the record is append-only because nothing can reach it ------------
    spec = call(base_url, "GET", "/openapi.json")
    methods = sorted(
        method.lower() for method in (spec.get("paths") or {}).get("/api/runs/{run_id}/decision-log", {})
    )
    check(
        "决策日志只有读取方法（能改的记录不是证据）",
        methods == ["get"],
        methods,
    )


def _remaining_seconds(expires_at: str) -> float:
    moment = datetime.fromisoformat(expires_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - datetime.now(UTC)).total_seconds()


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    base_url = args.base_url

    print(f"审核体系端到端验证 -> {base_url}")
    try:
        verify_health(base_url)
        project_id, run_id = create_run(base_url)
        review_request_id, first_ref = verify_request_snapshot(base_url, run_id)  # type: ignore[misc]
        verify_artifact_scope_lock(base_url, review_request_id, first_ref)
        comment_id = verify_collaboration(base_url, review_request_id, first_ref)
        verify_decision_and_action_items(base_url, run_id, review_request_id, comment_id)
        verify_invalidation(base_url, run_id)
        verify_board_and_inbox(base_url, project_id, run_id)
        verify_pages(base_url)
        gate_run_id = verify_intent_gate(base_url)
        cost_run_id = verify_cost_gate(base_url)
        asset_collection_id = verify_asset_library(base_url, project_id, run_id)
        verify_team_permissions(base_url, project_id, run_id)
        verify_api_contract(base_url)
        verify_api_keys(base_url)
        verify_webhooks(base_url)
        embed_integration_id, embed_link = verify_embeddable_workspace(base_url)
        verify_review_attachments(base_url)
        verify_worker_scaling(base_url)
        verify_observability(base_url)
        verify_quota_billing(base_url)
        verify_security_hardening(base_url)
        verify_deployment(base_url)
        verify_release(base_url)
        verify_decision_log(base_url)

        print("\n=== 可直接在浏览器打开的地址 ===")
        print(f"  {base_url}/dashboard                                  （审核收件箱）")
        print(f"  {base_url}/run-board?run_id={run_id}    （审核区块 / 失效标记）")
        print(f"  {base_url}/workspace?run_id={run_id}    （审核关口清单）")
        print(f"  {base_url}/run-board?run_id={gate_run_id}    （质量意图降级标记）")
        print(f"  {base_url}/workspace?run_id={cost_run_id}    （成本预留 / 付费确认）")
        print(
            f"  {base_url}/asset-library?project_id={project_id}"
            f"&collection_id={asset_collection_id}    （资产库 / 来源与授权）"
        )
        print(f"  {base_url}/team    （团队 / 权限 / 审计流水）")
        print(
            f"  {embed_link}    （可嵌入工作台 · 集成 {embed_integration_id}；"
            "令牌已在收尾时删除，示例链接仅供参考）"
        )
    except ApiError as exc:
        print(f"\n调用 API 失败：{exc.method} {exc.path} -> HTTP {exc.status}")
        print(f"  响应体：{exc.payload}")
        if exc.status >= 500:
            print("  服务端 5xx 通常来自数据库约束或未捕获异常，排查命令：")
            print("    docker compose logs --tail=120 api")
        return 1
    except urllib.error.URLError as exc:
        print(f"\n无法连接 {base_url}：{exc}")
        return 1

    print(f"\n共 {_checks} 项检查，失败 {len(_failures)} 项")
    if _failures:
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("审核体系端到端验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
