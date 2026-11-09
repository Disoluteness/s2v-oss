"""The single place that says which API endpoints need which permission.

Enforcement runs as one app-level dependency (see ``main.py``) rather than as a
parameter on every route. That keeps the policy readable in one table, avoids
touching sixty-odd route signatures, and means the rule for an endpoint can be
audited without opening the endpoint.

Endpoints absent from ``POLICY`` and from ``OPEN_ENDPOINTS`` are a bug, not a
default: a test asserts the two tables together cover every registered route, so
a new endpoint cannot quietly escape enforcement.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from apps.api.app.actor import api_key_prefix, api_key_scopes, embed_session, resolve_actor
from apps.api.app.db_store import DatabaseStore
from apps.api.app.deployment import DEFAULT_MAINTENANCE_MESSAGE, is_maintenance_exempt
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.permissions import Permission
from apps.api.app.routes import get_store

# Scope keys decide how the target project is found from the path parameters.
SCOPE_TENANT = "tenant"
SCOPE_PROJECT = "project"
SCOPE_RUN = "run"
SCOPE_REVIEW = "review"
SCOPE_COMMENT = "review_comment"
SCOPE_REVIEW_ATTACHMENT = "review_attachment"
SCOPE_COLLECTION = "collection"
SCOPE_ASSET = "asset"
SCOPE_JOB = "job"
SCOPE_TOOL_INVOCATION = "tool_invocation"
SCOPE_BRAND_KIT = "brand_kit"
SCOPE_STYLE_PRESET = "style_preset"

POLICY: dict[tuple[str, str], tuple[Permission, str]] = {
    # --- projects -----------------------------------------------------------
    ("GET", "/api/projects"): (Permission.PROJECT_READ, SCOPE_TENANT),
    ("POST", "/api/projects"): (Permission.PROJECT_WRITE, SCOPE_TENANT),
    ("GET", "/api/projects/{project_id}"): (Permission.PROJECT_READ, SCOPE_PROJECT),
    ("GET", "/api/projects/{project_id}/dashboard"): (Permission.PROJECT_READ, SCOPE_PROJECT),
    ("GET", "/api/projects/{project_id}/observability"): (
        Permission.PROJECT_READ,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/projects/{project_id}/runs"): (Permission.PROJECT_READ, SCOPE_PROJECT),
    ("POST", "/api/projects/{project_id}/runs"): (Permission.RUN_CREATE, SCOPE_PROJECT),
    ("GET", "/api/projects/{project_id}/review-inbox"): (
        Permission.PROJECT_READ,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/projects/{project_id}/brand-kits"): (Permission.PROJECT_READ, SCOPE_PROJECT),
    ("POST", "/api/projects/{project_id}/brand-kits"): (Permission.PROJECT_WRITE, SCOPE_PROJECT),
    ("GET", "/api/brand-kits/{brand_kit_id}"): (Permission.PROJECT_READ, SCOPE_BRAND_KIT),
    ("PATCH", "/api/brand-kits/{brand_kit_id}"): (Permission.PROJECT_WRITE, SCOPE_BRAND_KIT),
    ("GET", "/api/projects/{project_id}/style-presets"): (
        Permission.PROJECT_READ,
        SCOPE_PROJECT,
    ),
    ("POST", "/api/projects/{project_id}/style-presets"): (
        Permission.PROJECT_WRITE,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/style-presets/{style_preset_id}"): (
        Permission.PROJECT_READ,
        SCOPE_STYLE_PRESET,
    ),
    ("PATCH", "/api/style-presets/{style_preset_id}"): (
        Permission.PROJECT_WRITE,
        SCOPE_STYLE_PRESET,
    ),
    ("POST", "/api/projects/{project_id}/style-presets/{style_preset_id}/default"): (
        Permission.PROJECT_WRITE,
        SCOPE_PROJECT,
    ),
    ("POST", "/api/projects/{project_id}/script-screening-room/runs"): (
        Permission.RUN_CREATE,
        SCOPE_PROJECT,
    ),
    ("POST", "/api/mvp/sample-runs"): (Permission.RUN_CREATE, SCOPE_TENANT),
    ("POST", "/api/script-screening-room/parse"): (Permission.PROJECT_READ, SCOPE_TENANT),
    # --- runs: read ---------------------------------------------------------
    ("GET", "/api/runs/{run_id}"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/board"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/artifacts"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/events"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/jobs"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/usage"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/usage/ledger"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/tool-invocations"): (Permission.RUN_READ, SCOPE_RUN),
    # P2: the reasoning behind the choices is part of the run's record, so it
    # rides on the same read permission as the run itself. It is tenant-scoped
    # like every other ``/runs/{run_id}`` read: the guard runs before the
    # handler, so a cross-tenant id is a 404 rather than an empty list.
    ("GET", "/api/runs/{run_id}/decision-log"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/review-requests"): (Permission.RUN_READ, SCOPE_RUN),
    ("GET", "/api/runs/{run_id}/dispatch-stage-execution/preview"): (
        Permission.RUN_READ,
        SCOPE_RUN,
    ),
    ("GET", "/api/runs/{run_id}/assets"): (Permission.ASSET_READ, SCOPE_RUN),
    # --- runs: lifecycle ----------------------------------------------------
    ("POST", "/api/runs/{run_id}/start"): (Permission.RUN_START, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/provider-consents"): (Permission.RUN_START, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/copy"): (Permission.RUN_CREATE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/state-actions"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/preflight"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/execution-plan"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/stage-execution-manifest"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/dispatch-stage-execution"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/manifest-actions"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/proposal-packet"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/composition-graph"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/html-canvas-preview"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/quality-gates"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/edit-tools"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/compose"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/media-analysis"): (Permission.RUN_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/events"): (Permission.RUN_WRITE, SCOPE_RUN),
    # --- runs: assets ------------------------------------------------------
    ("POST", "/api/runs/{run_id}/assets"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/source-media"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/images"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/tts"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/music"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/subtitles"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/avatar-tasks"): (Permission.ASSET_WRITE, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/avatar-tasks/from-assets"): (
        Permission.ASSET_WRITE,
        SCOPE_RUN,
    ),
    ("POST", "/api/runs/{run_id}/jobs/avatar-from-assets"): (
        Permission.ASSET_WRITE,
        SCOPE_RUN,
    ),
    # --- runs: review ------------------------------------------------------
    ("POST", "/api/runs/{run_id}/review-requests"): (Permission.RUN_REVIEW, SCOPE_RUN),
    ("POST", "/api/runs/{run_id}/stages/{stage_key}/review"): (
        Permission.RUN_REVIEW,
        SCOPE_RUN,
    ),
    ("GET", "/api/review-requests/{review_request_id}"): (Permission.RUN_READ, SCOPE_REVIEW),
    ("POST", "/api/review-requests/{review_request_id}/decisions"): (
        Permission.RUN_REVIEW,
        SCOPE_REVIEW,
    ),
    ("POST", "/api/review-requests/{review_request_id}/comments"): (
        Permission.RUN_REVIEW,
        SCOPE_REVIEW,
    ),
    ("POST", "/api/review-requests/{review_request_id}/assign"): (
        Permission.RUN_REVIEW,
        SCOPE_REVIEW,
    ),
    ("PATCH", "/api/review-comments/{comment_id}"): (Permission.RUN_REVIEW, SCOPE_COMMENT),
    ("POST", "/api/review-requests/{review_request_id}/attachments"): (
        Permission.RUN_REVIEW,
        SCOPE_REVIEW,
    ),
    ("GET", "/api/review-requests/{review_request_id}/attachments"): (
        Permission.RUN_READ,
        SCOPE_REVIEW,
    ),
    ("DELETE", "/api/review-attachments/{attachment_id}"): (
        Permission.RUN_REVIEW,
        SCOPE_REVIEW_ATTACHMENT,
    ),
    # --- assets ------------------------------------------------------------
    ("GET", "/api/assets/{asset_id}"): (Permission.ASSET_READ, SCOPE_ASSET),
    ("GET", "/api/assets/{asset_id}/access-link"): (Permission.ASSET_READ, SCOPE_ASSET),
    ("GET", "/api/assets/{asset_id}/content"): (Permission.ASSET_READ, SCOPE_ASSET),
    ("GET", "/api/assets/{asset_id}/download"): (Permission.ASSET_READ, SCOPE_ASSET),
    # --- jobs --------------------------------------------------------------
    ("GET", "/api/jobs/{job_id}"): (Permission.RUN_READ, SCOPE_JOB),
    ("POST", "/api/jobs/{job_id}/process"): (Permission.RUN_WRITE, SCOPE_JOB),
    ("POST", "/api/jobs/{job_id}/retry"): (Permission.RUN_WRITE, SCOPE_JOB),
    ("POST", "/api/jobs/{job_id}/requeue"): (Permission.RUN_WRITE, SCOPE_JOB),
    # --- tool invocations --------------------------------------------------
    ("GET", "/api/tool-invocations/{invocation_id}"): (
        Permission.RUN_READ,
        SCOPE_TOOL_INVOCATION,
    ),
    ("POST", "/api/tool-invocations/{invocation_id}/poll"): (
        Permission.RUN_WRITE,
        SCOPE_TOOL_INVOCATION,
    ),
    ("POST", "/api/tool-invocations/{invocation_id}/archive-avatar-video"): (
        Permission.ASSET_WRITE,
        SCOPE_TOOL_INVOCATION,
    ),
    # --- asset library -----------------------------------------------------
    ("GET", "/api/projects/{project_id}/asset-collections"): (
        Permission.LIBRARY_READ,
        SCOPE_PROJECT,
    ),
    ("POST", "/api/projects/{project_id}/asset-collections"): (
        Permission.LIBRARY_WRITE,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/projects/{project_id}/asset-library"): (
        Permission.LIBRARY_READ,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/asset-collections/{collection_id}"): (
        Permission.LIBRARY_READ,
        SCOPE_COLLECTION,
    ),
    ("PATCH", "/api/asset-collections/{collection_id}"): (
        Permission.LIBRARY_WRITE,
        SCOPE_COLLECTION,
    ),
    ("GET", "/api/asset-collections/{collection_id}/items"): (
        Permission.LIBRARY_READ,
        SCOPE_COLLECTION,
    ),
    ("POST", "/api/asset-collections/{collection_id}/items"): (
        Permission.LIBRARY_WRITE,
        SCOPE_COLLECTION,
    ),
    ("POST", "/api/asset-collections/{collection_id}/import-from-run"): (
        Permission.LIBRARY_WRITE,
        SCOPE_COLLECTION,
    ),
    ("PATCH", "/api/asset-library/items/{item_id}"): (
        Permission.LIBRARY_WRITE,
        SCOPE_COLLECTION,
    ),
    ("DELETE", "/api/asset-library/items/{item_id}"): (
        Permission.LIBRARY_WRITE,
        SCOPE_COLLECTION,
    ),
    ("POST", "/api/runs/{run_id}/asset-library/reuse"): (
        Permission.LIBRARY_WRITE,
        SCOPE_RUN,
    ),
    # --- teams & members ---------------------------------------------------
    ("GET", "/api/teams"): (Permission.MEMBER_READ, SCOPE_TENANT),
    ("POST", "/api/teams"): (Permission.TEAM_MANAGE, SCOPE_TENANT),
    ("GET", "/api/teams/{team_id}"): (Permission.MEMBER_READ, SCOPE_TENANT),
    ("PATCH", "/api/teams/{team_id}"): (Permission.TEAM_MANAGE, SCOPE_TENANT),
    ("GET", "/api/teams/{team_id}/members"): (Permission.MEMBER_READ, SCOPE_TENANT),
    ("POST", "/api/teams/{team_id}/members"): (Permission.MEMBER_MANAGE, SCOPE_TENANT),
    ("PATCH", "/api/team-memberships/{membership_id}"): (
        Permission.MEMBER_MANAGE,
        SCOPE_TENANT,
    ),
    ("DELETE", "/api/team-memberships/{membership_id}"): (
        Permission.MEMBER_MANAGE,
        SCOPE_TENANT,
    ),
    ("GET", "/api/projects/{project_id}/members"): (Permission.MEMBER_READ, SCOPE_PROJECT),
    ("POST", "/api/projects/{project_id}/members"): (Permission.MEMBER_MANAGE, SCOPE_PROJECT),
    ("PATCH", "/api/project-memberships/{membership_id}"): (
        Permission.MEMBER_MANAGE,
        SCOPE_PROJECT,
    ),
    ("DELETE", "/api/project-memberships/{membership_id}"): (
        Permission.MEMBER_MANAGE,
        SCOPE_PROJECT,
    ),
    ("GET", "/api/audit-logs"): (Permission.MEMBER_READ, SCOPE_TENANT),
    ("GET", "/api/idempotency-keys"): (Permission.MEMBER_READ, SCOPE_TENANT),
    # Seating a login identity (V1.1) is roster administration: whoever may
    # manage members may give one a password, so it rides ``member:manage`` at
    # tenant scope — a project role that can invite a collaborator can also
    # give them a way to sign in, and nobody without the roster key can mint
    # credentials for someone else's account.
    ("POST", "/api/users/{user_id}/password"): (Permission.MEMBER_MANAGE, SCOPE_TENANT),
    # --- programmatic credentials (V7.2) ------------------------------------
    # ``api_key:manage`` is tenant-only (``TENANT_ONLY_PERMISSIONS``): a project
    # role can never confer it, because a key acts as its owner across the tenant.
    ("GET", "/api/api-keys"): (Permission.APIKEY_MANAGE, SCOPE_TENANT),
    ("POST", "/api/api-keys"): (Permission.APIKEY_MANAGE, SCOPE_TENANT),
    ("POST", "/api/api-keys/{api_key_id}/revoke"): (
        Permission.APIKEY_MANAGE,
        SCOPE_TENANT,
    ),
    # --- outbound webhooks (V7.3) ---------------------------------------------
    # ``webhook:manage`` is tenant-only for the same reason as api keys, one step
    # removed: a subscription's endpoint receives events from every run in the
    # tenant, so granting it inside one project would misdescribe its reach.
    ("GET", "/api/webhooks"): (Permission.WEBHOOK_MANAGE, SCOPE_TENANT),
    ("POST", "/api/webhooks"): (Permission.WEBHOOK_MANAGE, SCOPE_TENANT),
    ("GET", "/api/webhooks/{subscription_id}"): (Permission.WEBHOOK_MANAGE, SCOPE_TENANT),
    ("PATCH", "/api/webhooks/{subscription_id}"): (
        Permission.WEBHOOK_MANAGE,
        SCOPE_TENANT,
    ),
    ("DELETE", "/api/webhooks/{subscription_id}"): (
        Permission.WEBHOOK_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/webhooks/{subscription_id}/rotate-secret"): (
        Permission.WEBHOOK_MANAGE,
        SCOPE_TENANT,
    ),
    ("GET", "/api/webhooks/{subscription_id}/deliveries"): (
        Permission.WEBHOOK_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/webhook-deliveries/{delivery_id}/requeue"): (
        Permission.WEBHOOK_MANAGE,
        SCOPE_TENANT,
    ),
    # --- external integrations (V7.4) ------------------------------------------
    # ``integration:manage`` is tenant-only for the same reason as the two groups
    # above, taken one step further out: registering a host decides where tenant
    # data may be *displayed*, and a link it mints lets that host's end users read
    # a run without any seat here. A project role must not be able to widen that,
    # so every endpoint that creates, reads or revokes a registration is tenant
    # scoped — including the mint call, whose whole output is a capability.
    ("GET", "/api/external-integrations"): (Permission.INTEGRATION_MANAGE, SCOPE_TENANT),
    ("POST", "/api/external-integrations"): (Permission.INTEGRATION_MANAGE, SCOPE_TENANT),
    ("GET", "/api/external-integrations/{integration_id}"): (
        Permission.INTEGRATION_MANAGE,
        SCOPE_TENANT,
    ),
    ("PATCH", "/api/external-integrations/{integration_id}"): (
        Permission.INTEGRATION_MANAGE,
        SCOPE_TENANT,
    ),
    ("DELETE", "/api/external-integrations/{integration_id}"): (
        Permission.INTEGRATION_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/external-integrations/{integration_id}/embed-sessions"): (
        Permission.INTEGRATION_MANAGE,
        SCOPE_TENANT,
    ),
    # --- team quotas (V8.3) ---------------------------------------------------
    # Reading a team's quota is a read-only view of tenant data, so ``quota:read``
    # rides the ordinary read path (it ends in ``:read``, and
    # ``READ_ONLY_PERMISSIONS`` is derived from that suffix) — the same treatment
    # ``member:read`` gets. Writing one is not: ``quota:manage`` is tenant-only
    # (``TENANT_ONLY_PERMISSIONS``), because it sets the tenant's own *spending
    # ceiling* and a project role must not be able to raise it. That is also why
    # the one run-scoped row below is safe: ``SCOPE_RUN`` resolves a project, but
    # a project role can never confer this key, so the exemption cannot be
    # self-granted by whoever starts the run.
    ("GET", "/api/team-quotas"): (Permission.QUOTA_READ, SCOPE_TENANT),
    ("GET", "/api/teams/{team_id}/quota"): (Permission.QUOTA_READ, SCOPE_TENANT),
    ("PUT", "/api/teams/{team_id}/quota"): (Permission.QUOTA_MANAGE, SCOPE_TENANT),
    ("DELETE", "/api/teams/{team_id}/quota"): (Permission.QUOTA_MANAGE, SCOPE_TENANT),
    ("POST", "/api/runs/{run_id}/quota-override"): (Permission.QUOTA_MANAGE, SCOPE_RUN),
    # Attribution *is* the quota — see ``assign_project_team`` — so moving a
    # project between teams needs the same key as setting a ceiling, and gets
    # the ordinary project scope for existence checking.
    ("PATCH", "/api/projects/{project_id}/team"): (Permission.QUOTA_MANAGE, SCOPE_PROJECT),
    # --- provider credentials (V8.4) ------------------------------------------
    # V6.5 left these open on purpose (see §5.2.1 / §6 of that plan) and said
    # V8.4 would move them here. What V6.5 could not decide is what the key would
    # be called; what it did decide is that it should exist.
    #
    # One key, not a read/manage pair. The read responses are not harmless: a
    # credential carries ``secret_preview`` (the first three and last four
    # characters of the key) and the listing enumerates which paid providers the
    # tenant holds, so "who may look" and "who may change" are the same question.
    # And the key is tenant-only: writing a provider key chooses the account the
    # whole tenant's paid calls are billed to, which no single project may decide.
    ("GET", "/api/provider-credentials"): (Permission.PROVIDER_MANAGE, SCOPE_TENANT),
    ("GET", "/api/provider-credentials/{credential_id}"): (
        Permission.PROVIDER_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/provider-credentials"): (Permission.PROVIDER_MANAGE, SCOPE_TENANT),
    ("PATCH", "/api/provider-credentials/{credential_id}"): (
        Permission.PROVIDER_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/provider-credentials/{credential_id}/check"): (
        Permission.PROVIDER_MANAGE,
        SCOPE_TENANT,
    ),
    # The rotate call both writes (seals values) and touches every reference, so
    # it rides the same key; a read-only view of a credential is not a thing.
    ("POST", "/api/provider-credentials/{credential_id}/rotate"): (
        Permission.PROVIDER_MANAGE,
        SCOPE_TENANT,
    ),
    # --- deployment posture (V8.5) --------------------------------------------
    # The only endpoints in this table whose subject is the *install* rather than
    # a tenant's data — the table the policy keys into is the single
    # non-tenant-scoped one. The permission is still a tenant-level one on
    # purpose: "who may reconfigure this deployment" is answered by the role
    # matrix, and a project role must never be able to hand it out, which is why
    # ``deployment:manage`` lives in ``TENANT_ONLY_PERMISSIONS``.
    #
    # Reading is split out and granted broadly, unlike ``provider:manage`` above.
    # Posture is a version string, a revision id, a boolean and a backup
    # timestamp: nothing here is worth hiding, and an operator who cannot check
    # "is this box up to date" without the right to *change* the answer will
    # simply be handed the stronger key for no reason.
    ("GET", "/api/deployment"): (Permission.DEPLOYMENT_READ, SCOPE_TENANT),
    ("PATCH", "/api/deployment"): (Permission.DEPLOYMENT_MANAGE, SCOPE_TENANT),
    # Both of these are writes to the install's own row. They are also the two
    # paths ``enforce_maintenance_mode`` exempts, or the switch would be one-way:
    # the endpoint that turns maintenance off is a write to a deployment path.
    ("POST", "/api/deployment/maintenance"): (
        Permission.DEPLOYMENT_MANAGE,
        SCOPE_TENANT,
    ),
    ("POST", "/api/deployment/backups"): (Permission.DEPLOYMENT_MANAGE, SCOPE_TENANT),
}

# Endpoints that are deliberately *not* gated, with the reason. Naming them here
# rather than leaving them implicit is what makes silent drift impossible: a test
# asserts that every registered route is either in ``POLICY`` or in this table,
# so adding an endpoint forces a decision about who may call it.
OPEN_ENDPOINTS: dict[tuple[str, str], str] = {
    # Reference data: definitions shipped with the build, carrying no tenant rows.
    ("GET", "/api/error-codes"): "静态错误码目录，供 SDK 发现错误契约，不含租户数据",
    ("GET", "/api/error-taxonomy"): "静态错误分类字典",
    ("GET", "/api/health"): "存活探针",
    ("GET", "/api/permissions"): "静态权限矩阵，供页面渲染，不含租户数据",
    ("GET", "/api/pipelines"): "流水线定义",
    ("GET", "/api/render-profiles"): "渲染档位定义",
    ("GET", "/api/render-profiles/{profile_key}"): "渲染档位定义",
    ("GET", "/api/run-state-transitions"): "状态机元数据",
    ("GET", "/api/tools"): "工具目录",
    ("GET", "/api/tools/capabilities"): "工具目录",
    ("GET", "/api/tools/providers"): "工具目录",
    ("GET", "/api/tools/{tool_key}"): "工具目录",
    ("GET", "/api/webhook-events"): "静态 webhook 事件目录，订阅前可发现事件契约，不含租户数据",
    # Identity bootstrap: the question "who am I" has to be answerable before any
    # permission can be evaluated, so this one endpoint cannot require one.
    ("GET", "/api/me"): "身份自举端点，判断权限之前必须先能回答身份",
    # Login (V1.1) is bootstrap for the same reason one step earlier: the login
    # itself is how an identity gets established, so there is no permission to
    # evaluate yet. Open does not mean anonymous — the password is checked, and
    # every failure is the same 401 so the endpoint is not a user oracle.
    ("POST", "/api/auth/login"): "登录自举端点，密码本身即凭据；会话令牌是登录的产物而非前提",
    # Logout and introspection are about *the caller's own session*: the token
    # is the credential, and a missing or dead one is refused 401 in the session
    # identity dependency before the handler runs. Gating them would be circular
    # — the only permission a session can present is its own existence.
    ("POST", "/api/auth/logout"): "凭据即自身会话；无/坏会话令牌在身份依赖处即 401",
    ("GET", "/api/auth/session"): "凭据即自身会话；无/坏会话令牌在身份依赖处即 401",
    # The embed page's own bootstrap (V7.4), open for the same reason and asked by
    # a caller with no seat at all. Open does not mean anonymous: the token is the
    # credential, and ``enforce_embed_identity`` — an application-level dependency,
    # so it runs here too — refuses a missing, expired or tampered one with 401
    # before the handler is entered.
    ("GET", "/api/embed/{integration_id}/session"): (
        "嵌入会话自举端点，令牌本身即身份；无令牌/坏令牌在身份依赖处即 401"
    ),
    # Provider credentials moved into ``POLICY`` in V8.4, which is where V6.5
    # said they would end up. The callback below is the one that could not: its
    # caller is the upstream provider, so there is no platform identity to
    # evaluate a permission against — inventing one would create a key nobody
    # could hold. It stays open, and V8.4 gave it what *can* be checked: an HMAC
    # signature over the body (``PROVIDER_CALLBACK_SECRET``), refused when a
    # secret is configured and the signature is missing, stale or wrong.
    ("POST", "/api/provider-callbacks/{provider}"): (
        "外部供应商回调，无可解析的平台身份；真实性由 HMAC 签名校验（V8.4，配置密钥后强制）"
    ),
}


def _resolve_project_id(
    store: DatabaseStore,
    scope: str,
    path_params: dict[str, str],
) -> str | None:
    if scope == SCOPE_PROJECT:
        return path_params.get("project_id")
    if scope == SCOPE_RUN:
        run_id = path_params.get("run_id")
        return store.project_id_for_run(run_id) if run_id else None
    if scope == SCOPE_REVIEW:
        review_request_id = path_params.get("review_request_id")
        return (
            store.project_id_for_review_request(review_request_id)
            if review_request_id
            else None
        )
    if scope == SCOPE_COMMENT:
        comment_id = path_params.get("comment_id")
        return store.project_id_for_review_comment(comment_id) if comment_id else None
    if scope == SCOPE_REVIEW_ATTACHMENT:
        attachment_id = path_params.get("attachment_id")
        return (
            store.project_id_for_review_attachment(attachment_id) if attachment_id else None
        )
    if scope == SCOPE_TOOL_INVOCATION:
        invocation_id = path_params.get("invocation_id")
        return (
            store.project_id_for_tool_invocation(invocation_id) if invocation_id else None
        )
    if scope == SCOPE_BRAND_KIT:
        brand_kit_id = path_params.get("brand_kit_id")
        return store.project_id_for_brand_kit(brand_kit_id) if brand_kit_id else None
    if scope == SCOPE_STYLE_PRESET:
        style_preset_id = path_params.get("style_preset_id")
        return (
            store.project_id_for_style_preset(style_preset_id)
            if style_preset_id
            else None
        )
    if scope == SCOPE_COLLECTION:
        collection_id = path_params.get("collection_id")
        # ``/asset-library/items/{item_id}`` uses the collection scope but carries
        # only an item id; the membership check still needs a project, so resolve it.
        if collection_id:
            return store.project_id_for_collection(collection_id)
        item_id = path_params.get("item_id")
        return store.project_id_for_library_item(item_id) if item_id else None
    if scope == SCOPE_ASSET:
        asset_id = path_params.get("asset_id")
        return store.project_id_for_asset(asset_id) if asset_id else None
    if scope == SCOPE_JOB:
        job_id = path_params.get("job_id")
        return store.project_id_for_job(job_id) if job_id else None
    return None


def _resolve_run_id(
    store: DatabaseStore,
    scope: str,
    path_params: dict[str, str],
) -> str | None:
    """The run an endpoint is aimed at, for embed context pinning (V7.4).

    Only asked when the caller is an embed session. Most resources have no run of
    their own, and the caller has to read ``None`` as "cannot tell" rather than "no
    run": the guard then falls back to the project comparison it can still make.
    Without this, a run-pinned token would reach a sibling run of the same project
    through any endpoint whose path carries no ``run_id`` — a review request id,
    for instance, says nothing about a run until the row is read.
    """
    if scope == SCOPE_RUN:
        return path_params.get("run_id")
    if scope == SCOPE_REVIEW:
        review_request_id = path_params.get("review_request_id")
        return (
            store.run_id_for_review_request(review_request_id)
            if review_request_id
            else None
        )
    if scope == SCOPE_COMMENT:
        comment_id = path_params.get("comment_id")
        return store.run_id_for_review_comment(comment_id) if comment_id else None
    if scope == SCOPE_REVIEW_ATTACHMENT:
        attachment_id = path_params.get("attachment_id")
        return store.run_id_for_review_attachment(attachment_id) if attachment_id else None
    if scope == SCOPE_JOB:
        job_id = path_params.get("job_id")
        return store.run_id_for_job(job_id) if job_id else None
    if scope == SCOPE_TOOL_INVOCATION:
        invocation_id = path_params.get("invocation_id")
        return (
            store.run_id_for_tool_invocation(invocation_id) if invocation_id else None
        )
    if scope == SCOPE_ASSET:
        asset_id = path_params.get("asset_id")
        return store.run_id_for_asset(asset_id) if asset_id else None
    return None


def _request_template(request: Request) -> str | None:
    """The ``POLICY`` key for this request: the fully-prefixed path template.

    ``scope["route"]`` is the matched ``APIRoute``, and its ``path`` is *relative
    to the router's mount prefix* — the API router is mounted at ``/api``, so it
    reports ``/projects`` for a request to ``/api/projects``. Policy keys are
    written in full request form, so the mount prefix has to be put back.

    The prefix is recovered by aligning the template's segments with the tail of
    the requested path: every placeholder in this app matches exactly one
    segment, so the template and the concrete tail have the same length and
    whatever is left over at the front is the prefix. Deriving it this way keeps
    the guard free of any assumption about *where* the router happens to be
    mounted, and free of FastAPI routing internals.
    """

    template = getattr(request.scope.get("route"), "path", None)
    if not template or template == "/":
        return template
    path_segments = request.url.path.strip("/").split("/")
    template_segments = template.strip("/").split("/")
    prefix_length = len(path_segments) - len(template_segments)
    if prefix_length <= 0:
        return template
    prefix = "/" + "/".join(path_segments[:prefix_length])
    return prefix + template


async def enforce_api_permissions(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """App-level dependency: look the request up in ``POLICY`` and enforce it."""
    template = _request_template(request)
    if template is None:
        return
    entry = POLICY.get((request.method.upper(), template))
    if entry is None:
        return
    permission, scope = entry
    actor = resolve_actor(request)
    project_id = _resolve_project_id(store, scope, request.path_params)
    # An embed session (V7.4) is an external platform's end user with no seat here,
    # so the guard is told to hold it to the token's own context and scopes instead
    # of a role chain that would answer "no permissions" to everything. Resolving
    # the target run is a read, so it is only paid for when there is a token.
    embed = embed_session(request)
    # ``key_scopes`` is ``None`` for a header / default identity and for an
    # unrestricted key, and a concrete set for a narrowed one. Either way the
    # decision is made in one place — ``require_permission`` — so a denial is
    # audited and rendered identically no matter why it happened.
    store.require_permission(
        actor,
        permission,
        project_id=project_id,
        key_scopes=api_key_scopes(request),
        key_prefix=api_key_prefix(request),
        embed_session=embed,
        tenant_scoped=scope == SCOPE_TENANT,
        run_id=_resolve_run_id(store, scope, request.path_params) if embed else None,
    )


async def enforce_maintenance_mode(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """Refuse mutating requests while the deployment is in maintenance (V8.5).

    This lives beside the permission guard rather than in ``deployment.py``
    because it is the same kind of thing — an application-level dependency on the
    API router — and keeping the two together is what makes the mounting order in
    ``main.py`` auditable from one file. The rules it applies are pure functions
    from ``deployment.py``; only the lookup and the refusal are here.

    Three properties are worth stating, because each is a decision:

    * **Reads are never refused.** The point of the switch is to make the install
      safe to repair *without* taking it dark: the UI keeps rendering, an
      operator can see what state things are in, and probes keep answering. A
      maintenance mode that also stops reads is just an outage with extra steps.
    * **Two writes stay open** — the deployment endpoints themselves and the
      provider callbacks. The first makes the switch reversible without a
      database edit; the second refuses to throw away a paid provider result that
      the platform cannot regenerate. See ``MAINTENANCE_EXEMPT_PREFIXES``.
    * **The lookup is skipped entirely for reads**, so a read-only deployment
      pays nothing for a feature it is not using.
    """

    if is_maintenance_exempt(request.method, request.url.path):
        return
    state = store.maintenance_state()
    if not state.enabled:
        return
    raise ApiError(
        ErrorCode.MAINTENANCE_MODE,
        state.message or DEFAULT_MAINTENANCE_MESSAGE,
        # ``read_only`` is the actionable half: it tells a client its request was
        # fine and that retrying later is the whole remedy, which is also why the
        # code is marked retryable in the error catalog.
        details={"read_only": True},
    )
