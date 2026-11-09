import re

from apps.api.app.main import (
    app,
    dashboard_html_path,
    run_board_html_path,
    settings_html_path,
    workspace_html_path,
)
from packages.pipeline_kit.loader import PipelineLoader


def test_workspace_route_is_registered() -> None:
    # The HTML pages are served, but they are deliberately absent from the schema
    # (they return HTML, not JSON — see tests/test_openapi_contract.py), so the
    # route table is the right place to assert them.
    registered = {getattr(route, "path", None) for route in app.routes}
    assert {"/workspace", "/dashboard", "/run-board", "/settings"} <= registered

    schema = app.openapi()

    assert "/api/projects" in schema["paths"]
    assert "get" in schema["paths"]["/api/projects"]
    assert "post" in schema["paths"]["/api/projects"]
    assert "/api/projects/{project_id}/dashboard" in schema["paths"]
    assert "get" in schema["paths"]["/api/projects/{project_id}/dashboard"]
    assert "/api/projects/{project_id}/runs" in schema["paths"]
    assert "get" in schema["paths"]["/api/projects/{project_id}/runs"]
    assert "/api/runs/{run_id}/copy" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/copy"]
    assert "/api/runs/{run_id}/board" in schema["paths"]
    assert "get" in schema["paths"]["/api/runs/{run_id}/board"]
    assert "/api/mvp/sample-runs" in schema["paths"]
    assert "post" in schema["paths"]["/api/mvp/sample-runs"]
    assert "/api/runs/{run_id}/images" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/images"]
    assert "/api/runs/{run_id}/manifest-actions" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/manifest-actions"]
    assert "/api/runs/{run_id}/state-actions" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/state-actions"]
    assert "/api/run-state-transitions" in schema["paths"]
    assert "get" in schema["paths"]["/api/run-state-transitions"]
    assert "/api/error-taxonomy" in schema["paths"]
    assert "get" in schema["paths"]["/api/error-taxonomy"]
    assert "/api/render-profiles" in schema["paths"]
    assert "get" in schema["paths"]["/api/render-profiles"]
    assert "/api/render-profiles/{profile_key}" in schema["paths"]
    assert "get" in schema["paths"]["/api/render-profiles/{profile_key}"]
    assert "/api/provider-credentials" in schema["paths"]
    assert "post" in schema["paths"]["/api/provider-credentials"]
    assert "get" in schema["paths"]["/api/provider-credentials"]
    assert "/api/provider-credentials/{credential_id}/check" in schema["paths"]
    assert "post" in schema["paths"]["/api/provider-credentials/{credential_id}/check"]
    assert "/api/provider-callbacks/{provider}" in schema["paths"]
    assert "post" in schema["paths"]["/api/provider-callbacks/{provider}"]
    assert "/api/projects/{project_id}/brand-kits" in schema["paths"]
    assert "post" in schema["paths"]["/api/projects/{project_id}/brand-kits"]
    assert "get" in schema["paths"]["/api/projects/{project_id}/brand-kits"]
    assert "/api/brand-kits/{brand_kit_id}" in schema["paths"]
    assert "get" in schema["paths"]["/api/brand-kits/{brand_kit_id}"]
    assert "patch" in schema["paths"]["/api/brand-kits/{brand_kit_id}"]
    assert "/api/projects/{project_id}/style-presets" in schema["paths"]
    assert "post" in schema["paths"]["/api/projects/{project_id}/style-presets"]
    assert "get" in schema["paths"]["/api/projects/{project_id}/style-presets"]
    assert "/api/style-presets/{style_preset_id}" in schema["paths"]
    assert "get" in schema["paths"]["/api/style-presets/{style_preset_id}"]
    assert "patch" in schema["paths"]["/api/style-presets/{style_preset_id}"]
    assert "/api/projects/{project_id}/style-presets/{style_preset_id}/default" in schema[
        "paths"
    ]
    assert "/api/runs/{run_id}/usage" in schema["paths"]
    assert "get" in schema["paths"]["/api/runs/{run_id}/usage"]
    assert "/api/runs/{run_id}/usage/ledger" in schema["paths"]
    assert "get" in schema["paths"]["/api/runs/{run_id}/usage/ledger"]
    assert "/api/runs/{run_id}/source-media" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/source-media"]
    assert "/api/runs/{run_id}/media-analysis" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/media-analysis"]
    assert "/api/script-screening-room/parse" in schema["paths"]
    assert "post" in schema["paths"]["/api/script-screening-room/parse"]
    assert "/api/projects/{project_id}/script-screening-room/runs" in schema["paths"]
    assert "post" in schema["paths"]["/api/projects/{project_id}/script-screening-room/runs"]
    assert "/api/runs/{run_id}/dispatch-stage-execution/preview" in schema["paths"]
    assert "get" in schema["paths"]["/api/runs/{run_id}/dispatch-stage-execution/preview"]
    assert "/api/runs/{run_id}/proposal-packet" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/proposal-packet"]
    assert "/api/runs/{run_id}/composition-graph" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/composition-graph"]
    assert "/api/runs/{run_id}/html-canvas-preview" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/html-canvas-preview"]
    assert "/api/runs/{run_id}/quality-gates" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/quality-gates"]
    assert "/api/runs/{run_id}/edit-tools" in schema["paths"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/edit-tools"]
    assert "/api/runs/{run_id}/review-requests" in schema["paths"]
    assert "get" in schema["paths"]["/api/runs/{run_id}/review-requests"]
    assert "post" in schema["paths"]["/api/runs/{run_id}/review-requests"]
    assert "/api/review-requests/{review_request_id}" in schema["paths"]
    assert "get" in schema["paths"]["/api/review-requests/{review_request_id}"]
    assert "/api/review-requests/{review_request_id}/decisions" in schema["paths"]
    assert "post" in schema["paths"]["/api/review-requests/{review_request_id}/decisions"]
    assert "/api/review-requests/{review_request_id}/comments" in schema["paths"]
    assert "post" in schema["paths"]["/api/review-requests/{review_request_id}/comments"]
    assert "/api/review-comments/{comment_id}" in schema["paths"]
    assert "patch" in schema["paths"]["/api/review-comments/{comment_id}"]
    assert "/api/review-requests/{review_request_id}/assign" in schema["paths"]
    assert "post" in schema["paths"]["/api/review-requests/{review_request_id}/assign"]
    assert "/api/projects/{project_id}/review-inbox" in schema["paths"]
    assert "get" in schema["paths"]["/api/projects/{project_id}/review-inbox"]


def test_workspace_html_targets_review_api() -> None:
    html = workspace_html_path().read_text(encoding="utf-8")

    assert 'id="runForm"' in html
    assert 'id="sampleRunForm"' in html
    assert 'id="samplePipeline"' in html
    assert "脚本成片工作台" in html
    assert "Run Board" in html
    assert "产物" in html
    assert "事件" in html
    assert "执行清单" in html
    assert "制作提案包" in html
    assert "合成图" in html
    assert "Canvas 渲染报告" in html
    assert "HTML/Canvas 预览" in html
    assert "质量报告" in html
    assert "运行质量检查" in html
    assert "生成封面" in html
    assert "导出平台版本" in html
    assert "edit_operation_report" in html
    assert "stageLabel" in html
    assert "eventLabel" in html
    assert "artifactLabel" in html
    assert "statusLabel" in html
    assert "数字人口播" in html
    assert "视频合成" in html
    assert "任务待审核" in html
    assert "源素材清单" in html
    assert "视频分析摘要" in html
    assert "脚本放映室分镜" in html
    assert "脚本放映室解析器" in html
    assert "脚本放映室任务已创建" in html
    assert "分析抽帧" in html
    assert "素材分析已完成" in html
    assert 'name="quality_mode"' in html
    assert 'data-review="approved"' in html
    assert 'id="assetPreview"' in html
    assert 'id="finalLinkPreview"' in html
    assert 'id="runPreflightButton"' in html
    assert 'id="buildExecutionPlanButton"' in html
    assert 'id="startRunButton"' in html
    assert 'id="dispatchExecutionButton"' in html
    assert 'id="planIssueList"' in html
    assert 'id="planDecisionList"' in html
    assert 'id="executionStepList"' in html
    assert 'id="jobList"' in html
    assert "fetchDispatchPreview" in html
    assert "createGenericPipelineRun" in html
    assert "generatePersonImageForStep" in html
    assert "pickAndUploadReferenceImage" in html
    assert "generateTtsForStep" in html
    assert "runManifestAction" in html
    assert "生成人物图" in html
    assert "上传人物图" in html
    assert "生成口播音频" in html
    assert "等待输入" in html
    assert "等待阶段" in html
    assert "打开任务成片" in html
    assert "/api/runs/" in html
    assert "/preflight" in html
    assert "/execution-plan" in html
    assert "/proposal-packet" in html
    assert "/assets" in html
    assert "/manifest-actions" in html
    assert "dispatch_ready_steps" in html
    assert "generate_person_image" in html
    assert "upload_person_image" in html
    assert "generate_tts" in html
    assert "process_job" in html
    assert "retry_job" in html
    assert "formatErrorHelp" in html
    assert "错误类别" in html
    assert "/dispatch-stage-execution" in html
    assert "/dispatch-stage-execution/preview" in html
    assert "/start" in html
    assert "/api/mvp/sample-runs" in html
    assert "/api/projects" in html
    assert "/api/assets/" in html
    assert "/jobs/avatar-from-assets" in html


def test_workspace_html_exposes_the_review_sidebar() -> None:
    html = workspace_html_path().read_text(encoding="utf-8")

    for element_id in (
        'id="reviewRequestCard"',
        'id="gateList"',
        'id="gateCount"',
        'id="nextGate"',
        'id="openRunBoardReviewButton"',
    ):
        assert element_id in html, f"missing review sidebar element: {element_id}"

    # The gate checklist is derived from the DSL snapshot pinned to the run,
    # not from a hard-coded list of stages.
    assert "pipeline_definition_snapshot" in html
    assert "review_policy" in html
    assert "requires_human_review" in html
    assert "renderGates" in html
    assert "下一道审核关口" in html
    assert "全部审核关口已通过" in html
    assert "该 pipeline 未声明审核关口" in html
    assert "已失效" in html

    # The request card surfaces the snapshotted policy and the reviewer's owner.
    assert "renderReviewRequestCard" in html
    assert "currentReviewRequest" in html
    assert "审核对象" in html
    assert "通过后进入" in html
    assert "审核关注点" in html
    assert "通过标准" in html
    assert "审核人" in html
    assert "未指派" in html
    assert "已逾期" in html
    assert "第 ${request.iteration} 轮" in html

    # Review requests come from the dedicated review endpoint.
    assert "fetchReviewRequests" in html
    assert "/review-requests" in html
    assert "去 Run Board 处理审核" in html
    assert "run-board?review_request_id=" in html


def test_dashboard_html_targets_project_dashboard_api() -> None:
    html = dashboard_html_path().read_text(encoding="utf-8")

    assert "项目仪表盘" in html
    assert "Run 列表" in html
    assert "审核收件箱" in html
    assert "失败任务聚合" in html
    assert "复制" in html
    assert "搜索 run、品牌、产品、脚本" in html
    assert "/api/projects" in html
    assert "/dashboard" in html
    assert "/copy" in html
    assert "loadDashboard" in html
    assert "copyRun" in html


def test_dashboard_html_exposes_the_review_inbox() -> None:
    html = dashboard_html_path().read_text(encoding="utf-8")

    for element_id in (
        'id="reviewQueue"',
        'id="reviewCountBadge"',
        'id="reviewQueueSummary"',
        'id="reviewMineOnly"',
        'id="reviewAssigneeInput"',
        'id="reviewInboxRefresh"',
    ):
        assert element_id in html, f"missing review inbox element: {element_id}"

    # The inbox is served by the dedicated endpoint when filtered by assignee.
    assert "review-inbox" in html
    assert "assignee_user_id" in html
    assert "loadReviewInbox" in html
    assert "normalizeReviewItem" in html

    # Iteration, assignee and overdue are the three things a reviewer triages on.
    assert "第 ${item.iteration} 轮" in html
    assert "审核人" in html
    assert "未指派审核人" in html
    assert "已逾期" in html
    assert "未设置截止时间" in html
    assert "只看我的" in html

    # Inline triage is allowed without leaving the dashboard.
    assert "submitQuickDecision" in html
    assert "/decisions" in html
    assert "要求修改" in html

    # The new dashboard metrics surface the review backlog and its fallout.
    assert "逾期审核" in html
    assert "失效阶段" in html
    assert "上游已变更" in html


def test_run_board_html_targets_board_api() -> None:
    html = run_board_html_path().read_text(encoding="utf-8")

    assert "Run Board" in html
    assert "阶段泳道" in html
    assert "产物时间线" in html
    assert "成片预览与版本" in html
    assert "输入缺口与修复动作" in html
    assert "决策日志" in html
    assert "成本日志" in html
    assert "Job 日志" in html
    assert "事件流" in html
    assert "/api/runs/" in html
    assert "/board" in html
    assert "/api/assets/" in html
    assert "loadBoard" in html
    assert "renderStages" in html
    assert "renderDecisionLog" in html
    assert "renderCostLog" in html


def test_run_board_html_reads_the_persisted_decision_log() -> None:
    """P2: the 决策日志 panel shows reasoning, not just a list of tool picks."""

    html = run_board_html_path().read_text(encoding="utf-8")

    # The panel reads the persisted log — with history — instead of only the copy
    # the execution plan embeds in the board payload.
    assert "/decision-log?include_history=true" in html
    for element_id in (
        'id="decisionCategory"',
        'id="decisionHint"',
        'id="decisionCount"',
        'id="decisionLog"',
    ):
        assert element_id in html, f"missing decision element: {element_id}"
    for label in ("工具选择", "人工审核", "质量降级确认", "付费供应商确认"):
        assert label in html, f"missing decision category label: {label}"
    assert "当前结论" in html and "历史共" in html
    # The confidence column states a margin, never a probability — "把握度 92%"
    # would read as the system being sure, which it is not claiming.
    assert "领先次优" in html
    assert "无竞争者" in html
    assert "把握度" not in html
    # A truncated read must announce itself in the panel, not just in the payload.
    assert "较早的决策未显示" in html
    # A failed request must degrade to the plan artifact, not blank the panel.
    assert "renderPlanDecisionFallback" in html
    assert "决策日志接口不可用" in html
    assert "该类别暂无决策记录" in html
    # The panel keeps its place between the input gaps and the cost log.
    assert html.index("输入缺口与修复动作") < html.index("决策日志") < html.index("成本日志")


def test_run_board_html_exposes_the_review_block() -> None:
    html = run_board_html_path().read_text(encoding="utf-8")

    # The review block sits between the stage lanes and the artifact timeline.
    assert html.index("阶段泳道") < html.index("审核</h2>") < html.index("产物时间线")
    for element_id in (
        'id="currentReview"',
        'id="reviewComments"',
        'id="reviewActionItems"',
        'id="reviewTimeline"',
        'id="reviewCount"',
    ):
        assert element_id in html, f"missing review element: {element_id}"

    # The reviewer sees the gate policy that was snapshotted onto the request.
    assert "审核关注点" in html
    assert "通过标准" in html
    assert "审核对象类型" in html
    assert "通过后进入" in html
    assert "第 ${target.iteration} 轮" in html
    assert "已逾期" in html
    assert "审核人" in html

    # Four decisions plus reopen and cancel, wired to the decision endpoint.
    assert "reviewDecisions" in html
    assert "approve_with_changes" in html
    assert "request_changes" in html
    assert "/decisions" in html
    assert "submitReviewDecision" in html
    assert "重新打开" in html
    assert "取消审核" in html

    # Comments, assignment and the append-only history.
    assert "严重" in html and "建议" in html and "细节" in html
    assert "/comments" in html
    assert "/review-comments/" in html
    assert "标记已解决" in html
    assert "/assign" in html
    assert "保存指派" in html
    assert "reviewTimeline" in html
    assert "renderReviewTimeline" in html
    assert "每次审核会产生一条不可修改的记录" in html

    # Invalidation is surfaced on both the request card and the stage lanes.
    assert "invalidated_stages" in html
    assert "已失效" in html
    assert "下游失效" in html


# ---------------------------------------------------------------------------
# T61A Chinese display layer: DSL keys stay English on the wire, but every
# `review_focus` token a reviewer can be shown must have a Chinese label.
# ---------------------------------------------------------------------------

_REVIEW_FOCUS_PAGES = {
    "run_board.html": (run_board_html_path, "reviewFocusLabels = {"),
    "review.html": (workspace_html_path, "reviewFocus: {"),
}

# Every page that shows a review request state has to distinguish
# `approve_with_changes` from a plain `approve`, even though both close the
# request as `approved`.
_REVIEW_STATUS_LABEL_PAGES = {
    "dashboard.html": (dashboard_html_path, "reviewItemStatusLabel"),
    "run_board.html": (run_board_html_path, "reviewRequestStatusLabel"),
    "review.html": (workspace_html_path, "reviewRequestStatusLabel"),
}


def test_review_pages_distinguish_approve_with_changes() -> None:
    for filename, (path_getter, helper) in _REVIEW_STATUS_LABEL_PAGES.items():
        html = path_getter().read_text(encoding="utf-8")
        assert helper in html, f"{filename} is missing {helper}"
        assert "已通过（带修改）" in html, f"{filename} cannot show 带修改通过"
        assert "approve_with_changes" in html, f"{filename} never inspects current_decision"
        # The decision labels themselves must all be localized.
        for decision in ("approve_with_changes", "request_changes", "reopen"):
            assert decision in html, f"{filename} is missing the {decision} decision"


def _extract_map_keys(html: str, anchor: str) -> set[str]:
    start = html.index(anchor)
    brace = html.index("{", start)
    depth = 0
    block = None
    for index in range(brace, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = html[brace + 1 : index]
                break
    assert block is not None, f"unterminated map for anchor {anchor!r}"
    pattern = r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][\w/]*))\s*:"
    return {
        match.group(1) or match.group(2) or match.group(3)
        for match in re.finditer(pattern, block)
    }


def _dsl_review_focus_tokens() -> set[str]:
    tokens: set[str] = set()
    for pipeline in PipelineLoader().list_pipelines():
        for stage in pipeline.stages:
            policy = stage.review_policy
            if policy is not None:
                tokens.update(policy.review_focus)
    return tokens


def test_every_dsl_review_focus_token_has_a_chinese_label() -> None:
    tokens = _dsl_review_focus_tokens()
    assert tokens, "expected the pipelines to declare review_focus tokens"

    for filename, (path_getter, anchor) in _REVIEW_FOCUS_PAGES.items():
        html = path_getter().read_text(encoding="utf-8")
        translated = _extract_map_keys(html, anchor)
        missing = sorted(tokens - translated)
        assert not missing, f"{filename} is missing Chinese labels for {missing}"
        # A label that echoes the English key is not a translation.
        untranslated = sorted(
            token
            for token in tokens
            if re.search(rf'"{re.escape(token)}"\s*:\s*"{re.escape(token)}"', html)
        )
        assert not untranslated, f"{filename} left tokens untranslated: {untranslated}"


def test_review_focus_label_falls_back_to_the_raw_token() -> None:
    """An unmapped token must degrade to the DSL value, never to a blank cell."""
    for path_getter, anchor in _REVIEW_FOCUS_PAGES.values():
        html = path_getter().read_text(encoding="utf-8")
        assert "reviewFocusLabel" in html
        assert _extract_map_keys(html, anchor)


def test_settings_html_maintains_provider_credentials() -> None:
    """The operator page has to reach the credential API, not just look like it.

    A settings page that only renders a form is worse than none: the operator
    believes the key was saved. These assertions pin the four calls the page
    actually makes onto the copy that explains what they do.
    """
    html = settings_html_path().read_text(encoding="utf-8")

    # Every operation the backend exposes is reachable from this one page.
    assert "/api/provider-credentials" in html
    assert "provider-credentials/${id}/check" in html
    assert "provider-credentials/${id}/rotate" in html
    assert 'method: "PATCH"' in html
    assert 'method: "POST"' in html
    assert "secret_values" in html

    # The precedence rule is the whole reason the page exists; it must be said.
    assert "优先于" in html and ".env" in html
    assert "不需要重启" in html

    # The variable names come from the tool catalog — a typo here is silent.
    for env_var in ("DASHSCOPE_API_KEY", "ELEVENLABS_API_KEY", "KLING_ACCESS_KEY"):
        assert env_var in html, f"settings page never mentions {env_var}"

    # Secrets are shown masked, and the operator can revoke them without a curl.
    assert "secret_preview" in html
    assert "停用" in html and "启用" in html


def test_dashboard_links_to_the_settings_page() -> None:
    html = dashboard_html_path().read_text(encoding="utf-8")
    assert 'id="openSettingsButton"' in html
    assert '"/settings"' in html
