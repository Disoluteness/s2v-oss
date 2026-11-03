from __future__ import annotations

import base64
import binascii
import mimetypes
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from apps.api.app.actor import ActorContext, SessionIdentity
from apps.api.app.config import settings
from apps.api.app.db_models import (
    ApiKeyRecord,
    ArtifactRecord,
    AssetCollectionItemRecord,
    AssetCollectionRecord,
    AssetRecord,
    BackgroundJobRecord,
    BrandKitRecord,
    EventLogRecord,
    ProductionRunRecord,
    ProjectRecord,
    ProviderCredentialRecord,
    ReviewAttachmentRecord,
    ReviewCommentRecord,
    ReviewRequestRecord,
    StageRunRecord,
    StylePresetRecord,
    TeamMembershipRecord,
    TeamRecord,
    ToolInvocationRecord,
    UsageLedgerRecord,
    UserRecord,
    UserSessionRecord,
    WebhookDeliveryRecord,
    WebhookSubscriptionRecord,
)
from apps.api.app.deployment import (
    DEFAULT_MAINTENANCE_MESSAGE,
    DEPLOYMENT_SETTINGS_ID,
    alembic_head,
    alembic_pending,
    deployment_capabilities,
)
from apps.api.app.embed import (
    EmbedSession,
    issue_embed_session,
)
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.models import (
    ActorProfile,
    AlembicStatus,
    ApiKey,
    ApiKeyCreate,
    ApprovalRequest,
    Artifact,
    Asset,
    AssetAccessLink,
    AssetCollection,
    AssetCollectionCreate,
    AssetCollectionImportRequest,
    AssetCollectionImportResult,
    AssetCollectionItem,
    AssetCollectionItemAdd,
    AssetCollectionItemUpdate,
    AssetCollectionUpdate,
    AssetReuseRequest,
    AssetReuseResult,
    AssetUpload,
    AuditLogEntry,
    AvatarVideoArchiveRequest,
    AvatarVideoFromAssetsRequest,
    AvatarVideoSubmitRequest,
    BackgroundJob,
    BackgroundJobCreate,
    BackgroundJobStatus,
    BrandKit,
    BrandKitCreate,
    BrandKitUpdate,
    ComposeVideoRequest,
    CompositionGraphBuildRequest,
    CreateReviewAttachment,
    CreateReviewComment,
    DashboardJobSummary,
    DashboardReviewItem,
    DashboardRunSummary,
    DeploymentBackupRequest,
    DeploymentOverview,
    DeploymentSettings,
    DeploymentSettingsUpdate,
    EditOperation,
    EditToolRequest,
    EmbedContext,
    EmbedSessionCreate,
    EmbedSessionDescription,
    EventCreate,
    EventLog,
    EventSeverity,
    EventSourceType,
    ExternalIntegration,
    ExternalIntegrationCreate,
    ExternalIntegrationStatus,
    ExternalIntegrationUpdate,
    HtmlCanvasPreviewRequest,
    IdempotencyKeyEntry,
    ImageGenerateRequest,
    MaintenanceState,
    ManifestActionName,
    ManifestActionRequest,
    ManifestActionResult,
    MediaAnalysisRequest,
    MediaAnalysisResult,
    MusicGenerateRequest,
    ObservabilityCost,
    ObservabilityEvents,
    ObservabilityJobs,
    ObservabilityLatency,
    ObservabilityRecentError,
    ProductionRun,
    Project,
    ProjectAssetLibrary,
    ProjectCreate,
    ProjectDashboard,
    ProjectDashboardMetrics,
    ProjectDashboardProjectSummary,
    ProjectMemberAdd,
    ProjectMembership,
    ProjectMemberUpdate,
    ProjectObservability,
    ProviderCallbackRequest,
    ProviderCallbackResult,
    ProviderCredential,
    ProviderCredentialCheckResult,
    ProviderCredentialCreate,
    ProviderCredentialRotation,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    QualityGateRequest,
    QuotaOverride,
    ReviewActionItem,
    ReviewActionStatus,
    ReviewArtifactRef,
    ReviewAttachment,
    ReviewComment,
    ReviewCommentSeverity,
    ReviewCommentStatus,
    ReviewDecision,
    ReviewDecisionType,
    ReviewInbox,
    ReviewInboxItem,
    ReviewReasonCode,
    ReviewRequest,
    ReviewRequestCreate,
    ReviewRequestDetail,
    ReviewRequestStatus,
    RunBoard,
    RunBoardArtifactSummary,
    RunBoardAssetSummary,
    RunBoardStageLane,
    RunBoardVersionGroup,
    RunCopyRequest,
    RunCopyResult,
    RunCreate,
    RunStateActionName,
    RunStateActionRequest,
    RunStateActionResult,
    RunStatus,
    ScriptScreeningParseRequest,
    ScriptScreeningParseResult,
    ScriptScreeningRunCreateRequest,
    ScriptScreeningRunCreateResult,
    SourceMediaIngestRequest,
    SourceMediaIngestResult,
    StageStatus,
    StylePreset,
    StylePresetCreate,
    StylePresetUpdate,
    SubmitReviewDecision,
    SubtitleGenerateRequest,
    Team,
    TeamCreate,
    TeamMemberAdd,
    TeamMembership,
    TeamMemberUpdate,
    TeamQuota,
    TeamQuotaPeriod,
    TeamQuotaUpdate,
    TeamUpdate,
    ToolInvocation,
    ToolInvocationStatus,
    TtsGenerateRequest,
    UsageLedgerEntry,
    UsageSummary,
    WebhookDelivery,
    WebhookSubscription,
    WebhookSubscriptionCreate,
    WebhookSubscriptionUpdate,
)
from apps.api.app.permissions import (
    Permission,
    PermissionDenied,
    TeamRole,
    effective_permissions,
)
from apps.api.app.quota import evaluate_quota, quota_window
from apps.api.app.repositories import (
    DEFAULT_TENANT_ID,
    ApiKeyIssued,
    ApiKeyOutcome,
    ApiKeyRepository,
    ArtifactRepository,
    AssetCollectionRepository,
    AssetRepository,
    AssetReuseOutcome,
    AuditLogRepository,
    BackgroundJobRepository,
    BrandStyleRepository,
    CollectionAddOutcome,
    CollectionImportOutcome,
    DeploymentSettingsRepository,
    EventRepository,
    ExternalIntegrationOutcome,
    ExternalIntegrationRepository,
    ExternalIntegrationResult,
    IdempotencyClaim,
    IdempotencyRepository,
    ProductionRunRepository,
    ProjectMembershipOutcome,
    ProjectRepository,
    ProjectTeamOutcome,
    ProviderCredentialRepository,
    ReviewAttachmentOutcome,
    ReviewRepository,
    SecretRepository,
    TeamMembershipOutcome,
    TeamQuotaRepository,
    TeamRepository,
    ToolInvocationRepository,
    UsageLedgerRepository,
    UserRepository,
    WebhookDeliveryAttempt,
    WebhookRepository,
    WebhookSubscriptionOutcome,
    WebhookSubscriptionResult,
    _utcnow,
    as_utc,
    background_job_from_record,
    review_attachment_from_record,
    review_request_from_record,
    store_utc,
    team_quota_from_record,
)
from apps.api.app.review_gate import (
    ReviewGateError,
    action_item_result_action,
    infer_action_kind,
)
from apps.api.app.run_gate import RunGateError
from apps.api.app.secrets import (
    SecretBox,
    SecretEncryptionError,
    asset_link_secret_is_secure,
)
from apps.api.app.upload_guard import (
    check_content_type,
    check_decoded_size,
    check_encoded_length,
    parse_mime_set,
)
from apps.api.app.version import APP_VERSION
from packages.agent_runner.models import AgentInput
from packages.agent_runner.runner import RuleBasedAgentRunner
from packages.canvas_renderer_kit import HtmlCanvasRenderer, HtmlCanvasRenderRequest
from packages.compose_kit import FfmpegComposeRequest, FfmpegComposeWorker
from packages.composition_kit import build_composition_graph
from packages.edit_kit import FfmpegEditRequest, FfmpegEditResult, FfmpegEditWorker
from packages.media_analysis_kit import FfmpegMediaAnalysisResult, FfmpegMediaAnalyzer
from packages.orchestration_kit import build_stage_execution_manifest
from packages.orchestration_kit.error_taxonomy import ErrorClassification, classify_error
from packages.orchestration_kit.state_machine import (
    UNFINISHED_JOB_STATUSES,
    assert_run_action_allowed,
    run_state_transition_table,
)
from packages.pipeline_kit.loader import PipelineLoader
from packages.pipeline_kit.models import PipelineDefinition, StageDefinition
from packages.proposal_kit import build_proposal_packet
from packages.provider_kit import (
    DashScopeAsrAdapter,
    DashScopeAvatarVideoAdapter,
    DashScopeImageAdapter,
    DashScopeTtsAdapter,
    ElevenLabsMusicAdapter,
)
from packages.quality_gate_kit import QualityGateEvaluationRequest, QualityGateRunner
from packages.render_profile_kit import resolve_render_profile
from packages.script_screening_kit import ScriptScreeningRoomParser
from packages.storage_kit import LocalObjectStorage
from packages.tool_kit.contracts import ToolStatus
from packages.tool_kit.execution_plan import build_execution_plan, declared_budget_limit
from packages.tool_kit.preflight import build_preflight_report
from packages.tool_kit.registry import ToolRegistry


class EmbedMintOutcome(StrEnum):
    """Why a mint request did or did not produce a link (V7.4).

    Every failure keeps its own name here even though the route collapses most of
    them into one status code: the *route* is allowed to be vague to the caller,
    while the audit trail and the tests are not.
    """

    ISSUED = "issued"
    INTEGRATION_NOT_FOUND = "integration_not_found"
    INTEGRATION_DISABLED = "integration_disabled"
    PROJECT_NOT_FOUND = "project_not_found"
    RUN_NOT_FOUND = "run_not_found"
    INVALID_REQUEST = "invalid_request"


@dataclass
class EmbedMintResult:
    outcome: EmbedMintOutcome
    token: str | None = None
    session: EmbedSession | None = None
    integration: ExternalIntegration | None = None
    detail: str | None = None


def _embed_refusal(
    embed: EmbedSession,
    permission: Permission,
    *,
    project_id: str | None,
    tenant_scoped: bool,
    run_id: str | None,
) -> str | None:
    """``None`` when the token covers this call, else the reason it does not.

    Returning a reason instead of raising keeps ``require_permission`` the single
    place a denial is audited and rendered, so an embed refusal reaches the wire in
    exactly the same shape as every other one.

    The two failure kinds are kept apart because they ask the caller to do
    different things: ``embed_context`` means "mint a link for *this* project or
    run", ``embed_scope`` means "mint one that carries a write scope".
    """
    if tenant_scoped:
        return "embed_context"
    if project_id is not None and project_id != embed.project_id:
        return "embed_context"
    if embed.run_id is not None and run_id is not None and run_id != embed.run_id:
        return "embed_context"
    if permission.value not in embed.scopes:
        return "embed_scope"
    return None


class DatabaseStore:
    """Database-backed run store with the same state-machine behavior as the MVP store."""

    def __init__(
        self,
        session: Session,
        *,
        pipeline_loader: PipelineLoader | None = None,
        agent_runner: RuleBasedAgentRunner | None = None,
        storage: LocalObjectStorage | None = None,
        tool_registry: ToolRegistry | None = None,
        avatar_video_adapter: DashScopeAvatarVideoAdapter | None = None,
        image_adapter: DashScopeImageAdapter | None = None,
        tts_adapter: DashScopeTtsAdapter | None = None,
        asr_adapter: DashScopeAsrAdapter | None = None,
        music_adapter: ElevenLabsMusicAdapter | None = None,
        compose_worker: FfmpegComposeWorker | None = None,
        canvas_renderer: HtmlCanvasRenderer | None = None,
        quality_runner: QualityGateRunner | None = None,
        edit_worker: FfmpegEditWorker | None = None,
        media_analyzer: FfmpegMediaAnalyzer | None = None,
        script_screening_parser: ScriptScreeningRoomParser | None = None,
    ) -> None:
        self.session = session
        self.pipeline_loader = pipeline_loader or PipelineLoader()
        self.agent_runner = agent_runner or RuleBasedAgentRunner()
        self.projects = ProjectRepository(session)
        self.brand_styles = BrandStyleRepository(session)
        # V8.4: one box per store, built from configuration. An unset key makes it
        # a disabled box rather than an error, which is what keeps encryption
        # opt-in — every deployment that does not set the key behaves exactly as
        # it did before this version.
        self.secret_box = SecretBox(key=settings.secret_encryption_key)
        self.secrets = SecretRepository(session)
        self.provider_credentials = ProviderCredentialRepository(
            session, secret_box=self.secret_box
        )
        credential_env = self._credential_environment()
        self._owns_tool_registry = tool_registry is None
        self.tool_registry = tool_registry or self._build_tool_registry()
        self.runs = ProductionRunRepository(session)
        self.artifacts = ArtifactRepository(session)
        self.events = EventRepository(session)
        self.assets = AssetRepository(session)
        self.asset_library = AssetCollectionRepository(session)
        self.tool_invocations = ToolInvocationRepository(session)
        self.background_jobs = BackgroundJobRepository(session)
        self.usage_ledger = UsageLedgerRepository(session)
        self.reviews = ReviewRepository(session)
        self.teams = TeamRepository(session)
        self.team_quotas = TeamQuotaRepository(session)
        # V8.5: the install's own settings row. Not tenant-scoped, and the only
        # repository here that is not — the permission that guards its endpoints
        # is a tenant permission, but the row describes the deployment itself.
        self.deployment_settings_repo = DeploymentSettingsRepository(session)
        self.audit = AuditLogRepository(session)
        self.idempotency = IdempotencyRepository(session)
        self.api_keys = ApiKeyRepository(session)
        self.webhooks = WebhookRepository(session)
        self.integrations = ExternalIntegrationRepository(session)
        self.storage = storage or LocalObjectStorage(
            root=settings.local_storage_root,
            bucket=settings.object_storage_bucket,
            link_secret=settings.asset_link_secret,
        )
        dashscope_api_key = credential_env.get("DASHSCOPE_API_KEY")
        elevenlabs_api_key = credential_env.get("ELEVENLABS_API_KEY")
        self.avatar_video_adapter = avatar_video_adapter or DashScopeAvatarVideoAdapter(
            api_key=dashscope_api_key,
        )
        self.image_adapter = image_adapter or DashScopeImageAdapter(
            api_key=dashscope_api_key,
        )
        self.tts_adapter = tts_adapter or DashScopeTtsAdapter(
            api_key=dashscope_api_key,
        )
        # V1.1: ASR rides the same DashScope credential as TTS — no new
        # provider, no new secret. An adapter without a key still exists and
        # reports itself unavailable, which is what keeps the transcript path
        # degraded-but-honest instead of absent.
        self.asr_adapter = asr_adapter or DashScopeAsrAdapter(
            api_key=dashscope_api_key,
        )
        self.music_adapter = music_adapter or ElevenLabsMusicAdapter(
            api_key=elevenlabs_api_key,
        )
        self.compose_worker = compose_worker or FfmpegComposeWorker()
        self.canvas_renderer = canvas_renderer or HtmlCanvasRenderer()
        self.quality_runner = quality_runner or QualityGateRunner()
        self.edit_worker = edit_worker or FfmpegEditWorker()
        self.media_analyzer = media_analyzer or FfmpegMediaAnalyzer()
        self.script_screening_parser = script_screening_parser or ScriptScreeningRoomParser()

    def _credential_environment(self) -> dict[str, str]:
        return self.provider_credentials.environment_overrides()

    def _disabled_providers(self) -> set[str]:
        return self.provider_credentials.disabled_providers()

    def _build_tool_registry(self) -> ToolRegistry:
        return ToolRegistry(
            env_overrides=self._credential_environment(),
            disabled_providers=self._disabled_providers(),
        )

    def _refresh_tool_registry(self) -> None:
        if self._owns_tool_registry:
            self.tool_registry = self._build_tool_registry()

    def list_tools(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        status: ToolStatus | None = None,
    ) -> list[dict]:
        return [
            tool.model_dump()
            for tool in self.tool_registry.list_tools(
                capability=capability,
                provider=provider,
                status=status,
            )
        ]

    def get_tool(self, tool_key: str) -> dict | None:
        tool = self.tool_registry.get_tool(tool_key)
        return tool.model_dump() if tool is not None else None

    def list_tool_capabilities(self) -> list[dict]:
        return [summary.model_dump() for summary in self.tool_registry.list_capabilities()]

    def list_tool_providers(self) -> list[dict]:
        return [summary.model_dump() for summary in self.tool_registry.list_providers()]

    def create_provider_credential(
        self,
        payload: ProviderCredentialCreate,
        *,
        actor_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ProviderCredential:
        credential = self.provider_credentials.create_credential(payload, tenant_id=tenant_id)
        self._audit_provider_credential(
            action="provider_credential.created",
            credential=credential,
            actor_id=actor_id,
            summary=f"provider credential for {credential.provider}",
        )
        self.session.commit()
        self._refresh_tool_registry()
        return credential

    def list_provider_credentials(
        self,
        *,
        provider: str | None = None,
        status: ProviderCredentialStatus | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[ProviderCredential]:
        return self.provider_credentials.list_credentials(
            provider=provider, status=status, tenant_id=tenant_id
        )

    def get_provider_credential(
        self,
        credential_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ProviderCredential | None:
        return self.provider_credentials.get_credential(credential_id, tenant_id=tenant_id)

    def update_provider_credential(
        self,
        credential_id: str,
        payload: ProviderCredentialUpdate,
        *,
        actor_id: str | None = None,
    ) -> ProviderCredential | None:
        credential = self.provider_credentials.update_credential(credential_id, payload)
        if credential is not None:
            self._audit_provider_credential(
                action="provider_credential.updated",
                credential=credential,
                actor_id=actor_id,
                summary=f"provider credential for {credential.provider}",
                extra={
                    # Which fields moved, never their values. A secret written to
                    # an audit table is a secret written to every log that reads
                    # one — and the audit trail is the *last* place to keep a copy.
                    "secret_values_replaced": payload.secret_values is not None,
                },
            )
            self.session.commit()
            self._refresh_tool_registry()
        return credential

    def rotate_provider_credential(
        self,
        credential_id: str,
        *,
        actor_id: str | None = None,
    ) -> ProviderCredentialRotation | None:
        """Re-seal a credential's secrets under the configured key (V8.4).

        Serves both runbooks: `migrate the inline secrets we already had` and
        `re-encrypt everything after rotating the master key`.
        """
        try:
            rotation = self.provider_credentials.rotate_credential(credential_id)
        except SecretEncryptionError as exc:
            # A deployment problem, not a caller problem: the remedy is to set
            # SECRET_ENCRYPTION_KEY, which no request body can do.
            raise ApiError(
                ErrorCode.INSECURE_SECRET_CONFIGURATION,
                str(exc),
            ) from exc
        if rotation is None:
            return None
        self._audit_provider_credential(
            action="provider_credential.secrets_encrypted",
            credential=rotation.credential,
            actor_id=actor_id,
            summary=f"provider credential for {rotation.credential.provider}",
            extra={
                "sealed": rotation.sealed,
                "resealed": rotation.resealed,
                "locked": rotation.locked,
            },
        )
        self.session.commit()
        self._refresh_tool_registry()
        return rotation

    def check_provider_credential(
        self,
        credential_id: str,
        *,
        actor_id: str | None = None,
    ) -> ProviderCredentialCheckResult | None:
        record = self.provider_credentials.get_credential_record(credential_id)
        if record is None:
            return None
        disabled = record.status == ProviderCredentialStatus.DISABLED.value
        # Resolved plaintext, not the stored locators: with encryption on the row
        # holds ``sec_...`` references, and handing those to the registry would
        # report every encrypted credential as "configured with a nonsense key".
        _source, resolved = self.provider_credentials.secrets_status(credential_id)
        registry = ToolRegistry(
            env_overrides=resolved,
            disabled_providers={record.provider} if disabled else set(),
        )
        tools = registry.list_tools(provider=record.provider)
        tool_statuses = [
            {
                "tool_key": tool.tool_key,
                "capability": tool.capability,
                "status": tool.status.value,
                "missing_dependencies": tool.missing_dependencies,
                "status_reason": tool.status_reason,
            }
            for tool in tools
        ]
        missing = sorted(
            {
                dependency
                for tool in tools
                for dependency in tool.missing_dependencies
            }
        )
        ok = (
            not disabled
            and bool(tools)
            and any(tool.status.value in {"available", "degraded"} for tool in tools)
        )
        if disabled:
            message = f"Provider is disabled: {record.provider}"
        elif ok:
            message = f"Provider credential can satisfy at least one tool: {record.provider}"
        elif tools:
            message = f"Provider credential is incomplete for provider: {record.provider}"
        else:
            message = f"No registered tools use provider: {record.provider}"
        result = self.provider_credentials.check_credential(
            credential_id,
            tool_statuses=tool_statuses,
            missing_dependencies=missing,
            ok=ok,
            message=message,
        )
        if result is not None:
            self._audit_provider_credential(
                action="provider_credential.checked",
                credential=result.credential,
                actor_id=actor_id,
                summary=f"{record.provider}: {'available' if ok else 'unavailable'}",
                extra={"missing_dependencies": missing, "ok": ok},
            )
        self.session.commit()
        return result

    def _audit_provider_credential(
        self,
        *,
        action: str,
        credential: ProviderCredential,
        actor_id: str | None,
        summary: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a credential change without recording the credential.

        ``secret_names`` is the useful half of an audit line — it answers "which
        key was replaced" — and the value is the one half that must never appear.
        """
        payload: dict[str, Any] = {
            "provider": credential.provider,
            "secret_names": credential.secret_names,
            "secret_source": credential.secret_source.value,
        }
        if extra:
            payload.update(extra)
        self.audit.record(
            action=action,
            entity_type="provider_credential",
            entity_id=credential.credential_id,
            actor_id=actor_id,
            project_id=credential.project_id,
            summary=summary,
            payload=payload,
            tenant_id=credential.tenant_id,
        )

    def create_project(
        self,
        payload: ProjectCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Project:
        # T97: the caller's tenant, passed by the route from the resolved actor.
        # The default is the default tenant, so every pre-existing caller (the
        # MVP sample path, the screening room) writes exactly where it wrote
        # before.
        project = self.projects.create_project(payload, tenant_id=tenant_id)
        self.session.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get_project(project_id)

    # --- teams, memberships and permissions (V6.5) ---------------------------

    def ensure_default_seat(self, actor: ActorContext) -> None:
        """Give the fallback identity its owner seat in the default team.

        Only the configured default actor is auto-seated. A header-supplied
        identity that has no membership stays unseated and therefore gets no
        permissions, instead of silently inheriting read access.
        """
        if not actor.is_default:
            return
        team = self.teams.ensure_default_team(actor.actor_id)
        membership = self.teams.get_membership(team.id, actor.actor_id)
        if membership is not None and membership.status == "active":
            return
        self.teams.add_member(
            team.id,
            TeamMemberAdd(
                user_id=actor.actor_id,
                role=TeamRole.OWNER.value,
                name=actor.display_name,
            ),
            invited_by=actor.actor_id,
        )
        self.session.commit()

    def actor_seat(self, actor: ActorContext) -> tuple[str | None, TeamRole | None]:
        self.ensure_default_seat(actor)
        seat = self.teams.team_role_for_user(actor.actor_id, tenant_id=actor.tenant_id)
        if seat is None:
            return None, None
        return seat

    def actor_profile(
        self,
        actor: ActorContext,
        *,
        key_scopes: frozenset[str] | None = None,
    ) -> ActorProfile:
        team_id, team_role = self.actor_seat(actor)
        permissions = effective_permissions(team_role)
        if key_scopes is not None:
            # Report what the credential can actually do, not what its owner
            # could: a caller debugging a 403 needs to see the narrowed set.
            permissions = frozenset(
                permission for permission in permissions if permission.value in key_scopes
            )
        return ActorProfile(
            actor_id=actor.actor_id,
            display_name=actor.display_name,
            tenant_id=actor.tenant_id,
            source=actor.source,
            team_id=team_id,
            team_role=team_role.value if team_role else None,
            permissions=sorted(permission.value for permission in permissions),
        )

    def permissions_for_project(
        self,
        actor: ActorContext,
        project_id: str | None,
    ) -> frozenset[Permission]:
        _, team_role = self.actor_seat(actor)
        project_role = (
            self.teams.project_role_for_user(project_id, actor.actor_id)
            if project_id
            else None
        )
        return effective_permissions(team_role, project_role)

    def require_permission(
        self,
        actor: ActorContext,
        permission: Permission,
        *,
        project_id: str | None = None,
        key_scopes: frozenset[str] | None = None,
        key_prefix: str | None = None,
        embed_session: EmbedSession | None = None,
        tenant_scoped: bool = False,
        run_id: str | None = None,
    ) -> None:
        """Refuse unless the actor's seat allows ``permission``.

        ``key_scopes`` narrows the check when the caller authenticated with an API
        key: the key sits *below* its owner, so a permission the owner holds but
        the key does not scope is still refused, and the refusal says so.

        ``embed_session`` (V7.4) *replaces* the seat chain instead of narrowing it.
        An embed session is an external platform's end user, who has no
        ``team_memberships`` row here, so a seat lookup would answer "no
        permissions" for everything and the token would be the only thing left
        that could authorize. Two rules apply, in this order and deliberately:

        1. **Context pinning** — a tenant-wide endpoint, a different project, or a
           different run than the token names is refused outright
           (``embed_context``). This runs *before* the scope decision because a
           token aimed at the wrong resource is out of bounds however wide its
           scopes are; reporting ``embed_scope`` there would imply a wider scope
           would have helped.
        2. **Scope** — the permission has to be in the token's scopes
           (``embed_scope``). The decoder has already re-capped those against
           today's allowlist, so a scope we stopped granting takes effect on links
           that are already in circulation.
        """
        if embed_session is not None:
            reason = _embed_refusal(
                embed_session,
                permission,
                project_id=project_id,
                tenant_scoped=tenant_scoped,
                run_id=run_id,
            )
            if reason is None:
                return
            self._audit(
                actor,
                action="permission.denied",
                entity_type="permission",
                entity_id=permission.value,
                project_id=project_id,
                summary=f"拒绝 {permission.value}",
                payload={
                    "permission": permission.value,
                    "project_id": project_id,
                    "reason": reason,
                    "integration_id": embed_session.integration_id,
                    "embed_project_id": embed_session.project_id,
                    "embed_run_id": embed_session.run_id,
                    "embed_scopes": sorted(embed_session.scopes),
                },
            )
            self.session.commit()
            raise PermissionDenied(
                permission,
                message=(
                    "embed session is pinned to another project or run"
                    if reason == "embed_context"
                    else f"embed session is not scoped for '{permission.value}'"
                ),
                actor_id=actor.actor_id,
                actor_source=actor.source,
                details={
                    "project_id": project_id,
                    "reason": reason,
                    "integration_id": embed_session.integration_id,
                },
            )
        _, team_role = self.actor_seat(actor)
        project_role = (
            self.teams.project_role_for_user(project_id, actor.actor_id)
            if project_id
            else None
        )
        seat_allows = permission in effective_permissions(team_role, project_role)
        out_of_scope = (
            seat_allows and key_scopes is not None and permission.value not in key_scopes
        )
        if seat_allows and not out_of_scope:
            return
        reason = "api_key_scope" if out_of_scope else "seat"
        # A refusal is itself an auditable event; commit it before raising so it
        # survives the request being aborted.
        self._audit(
            actor,
            action="permission.denied",
            entity_type="permission",
            entity_id=permission.value,
            project_id=project_id,
            summary=f"拒绝 {permission.value}",
            payload={
                "permission": permission.value,
                "project_id": project_id,
                "reason": reason,
                "team_role": team_role.value if team_role else None,
                "project_role": project_role.value if project_role else None,
                "api_key_prefix": key_prefix,
            },
        )
        self.session.commit()
        raise PermissionDenied(
            permission,
            message=(
                f"api key is not scoped for '{permission.value}'"
                if out_of_scope
                else None
            ),
            actor_id=actor.actor_id,
            actor_source=actor.source,
            team_role=team_role.value if team_role else None,
            project_role=project_role.value if project_role else None,
            details={"project_id": project_id, "api_key_prefix": key_prefix},
        )

    def project_id_for_run(self, run_id: str) -> str | None:
        run = self.runs.get_run(run_id)
        return run.project_id if run else None

    def project_id_for_asset(self, asset_id: str) -> str | None:
        record = self.session.get(AssetRecord, asset_id)
        if record is None:
            return None
        return self.project_id_for_run(record.run_id)

    def project_id_for_job(self, job_id: str) -> str | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        if record is None:
            return None
        return self.project_id_for_run(record.run_id)

    def project_id_for_collection(self, collection_id: str) -> str | None:
        record = self.session.get(AssetCollectionRecord, collection_id)
        return record.project_id if record is not None else None

    def project_id_for_library_item(self, item_id: str) -> str | None:
        record = self.session.get(AssetCollectionItemRecord, item_id)
        return self.project_id_for_collection(record.collection_id) if record else None

    def project_id_for_review_request(self, review_request_id: str) -> str | None:
        record = self.session.get(ReviewRequestRecord, review_request_id)
        return record.project_id if record is not None else None

    def project_id_for_review_comment(self, comment_id: str) -> str | None:
        record = self.session.get(ReviewCommentRecord, comment_id)
        return self.project_id_for_run(record.run_id) if record is not None else None

    def project_id_for_review_attachment(self, attachment_id: str) -> str | None:
        record = self.session.get(ReviewAttachmentRecord, attachment_id)
        return self.project_id_for_run(record.run_id) if record is not None else None

    def project_id_for_tool_invocation(self, invocation_id: str) -> str | None:
        record = self.session.get(ToolInvocationRecord, invocation_id)
        return self.project_id_for_run(record.run_id) if record is not None else None

    def project_id_for_brand_kit(self, brand_kit_id: str) -> str | None:
        record = self.session.get(BrandKitRecord, brand_kit_id)
        return record.project_id if record is not None else None

    def project_id_for_style_preset(self, style_preset_id: str) -> str | None:
        record = self.session.get(StylePresetRecord, style_preset_id)
        return record.project_id if record is not None else None

    # --- run resolution for embed context pinning (V7.4) -----------------------
    # An embed token may name a run, and that name is a promise to the platform:
    # the link shows one run, not its siblings. Enforcing it needs the *run* an
    # endpoint is aimed at, which is not always in the path — a review request id
    # says nothing about a run until the row is read. These are the read side of
    # that, mirroring the ``project_id_for_*`` family above. A resource with no run
    # of its own (a collection, a brand kit) answers ``None``, which makes the
    # guard fall back to the project comparison it can actually make.

    def run_id_for_review_request(self, review_request_id: str) -> str | None:
        record = self.session.get(ReviewRequestRecord, review_request_id)
        return record.run_id if record is not None else None

    def run_id_for_review_comment(self, comment_id: str) -> str | None:
        record = self.session.get(ReviewCommentRecord, comment_id)
        return record.run_id if record is not None else None

    def run_id_for_review_attachment(self, attachment_id: str) -> str | None:
        record = self.session.get(ReviewAttachmentRecord, attachment_id)
        return record.run_id if record is not None else None

    def run_id_for_job(self, job_id: str) -> str | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        return record.run_id if record is not None else None

    def run_id_for_tool_invocation(self, invocation_id: str) -> str | None:
        record = self.session.get(ToolInvocationRecord, invocation_id)
        return record.run_id if record is not None else None

    def run_id_for_asset(self, asset_id: str) -> str | None:
        record = self.session.get(AssetRecord, asset_id)
        return record.run_id if record is not None else None

    # --- tenant resolution for the scope guard (V1.1 T97) -----------------------
    # The mirror of the two families above, answered in tenants instead of
    # projects or runs: given a path's resource id, which tenant owns the row?
    # ``enforce_tenant_scope`` asks exactly one of these per id-shaped path param
    # and refuses with a 404 shaped like the handler's own "not found" when the
    # row belongs to somebody else. A missing row answers ``None`` — the guard
    # steps aside and the handler produces the same 404 it always has, so a
    # foreign id and an absent one are indistinguishable on the wire.

    def tenant_id_for_project(self, project_id: str) -> str | None:
        record = self.session.get(ProjectRecord, project_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_run(self, run_id: str) -> str | None:
        record = self.session.get(ProductionRunRecord, run_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_brand_kit(self, brand_kit_id: str) -> str | None:
        record = self.session.get(BrandKitRecord, brand_kit_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_style_preset(self, style_preset_id: str) -> str | None:
        record = self.session.get(StylePresetRecord, style_preset_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_asset_collection(self, collection_id: str) -> str | None:
        record = self.session.get(AssetCollectionRecord, collection_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_library_item(self, item_id: str) -> str | None:
        record = self.session.get(AssetCollectionItemRecord, item_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_provider_credential(self, credential_id: str) -> str | None:
        record = self.session.get(ProviderCredentialRecord, credential_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_asset(self, asset_id: str) -> str | None:
        run_id = self.run_id_for_asset(asset_id)
        return self.tenant_id_for_run(run_id) if run_id is not None else None

    def tenant_id_for_job(self, job_id: str) -> str | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_tool_invocation(self, invocation_id: str) -> str | None:
        run_id = self.run_id_for_tool_invocation(invocation_id)
        return self.tenant_id_for_run(run_id) if run_id is not None else None

    def tenant_id_for_review_request(self, review_request_id: str) -> str | None:
        record = self.session.get(ReviewRequestRecord, review_request_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_review_comment(self, comment_id: str) -> str | None:
        record = self.session.get(ReviewCommentRecord, comment_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_review_attachment(self, attachment_id: str) -> str | None:
        record = self.session.get(ReviewAttachmentRecord, attachment_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_team(self, team_id: str) -> str | None:
        record = self.session.get(TeamRecord, team_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_team_membership(self, membership_id: str) -> str | None:
        record = self.session.get(TeamMembershipRecord, membership_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_webhook_subscription(self, subscription_id: str) -> str | None:
        record = self.session.get(WebhookSubscriptionRecord, subscription_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_webhook_delivery(self, delivery_id: str) -> str | None:
        record = self.session.get(WebhookDeliveryRecord, delivery_id)
        return record.tenant_id if record is not None else None

    def tenant_id_for_user(self, user_id: str) -> str | None:
        record = self.session.get(UserRecord, user_id)
        return record.tenant_id if record is not None else None

    def list_teams(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        include_archived: bool = False,
    ) -> list[Team]:
        return self.teams.list_teams(tenant_id=tenant_id, include_archived=include_archived)

    def get_team(self, team_id: str) -> Team | None:
        return self.teams.get_team(team_id)

    def create_team(self, payload: TeamCreate, actor: ActorContext) -> Team:
        team = self.teams.create_team(
            payload,
            tenant_id=actor.tenant_id,
            created_by=actor.actor_id,
        )
        self._audit(
            actor,
            action="team.created",
            entity_type="team",
            entity_id=team.team_id,
            summary=f"创建团队 {team.name}",
            payload={"slug": team.slug},
        )
        self.session.commit()
        return team

    def update_team(self, team_id: str, payload: TeamUpdate, actor: ActorContext) -> Team | None:
        team = self.teams.update_team(team_id, payload)
        if team is None:
            return None
        self._audit(
            actor,
            action="team.updated",
            entity_type="team",
            entity_id=team_id,
            summary=f"更新团队 {team.name}",
            payload=payload.model_dump(exclude_none=True),
        )
        self.session.commit()
        return team

    # --- team quotas (V8.3) --------------------------------------------------

    def assign_project_team(
        self,
        project_id: str,
        team_id: str | None,
        actor: ActorContext,
    ) -> tuple[ProjectTeamOutcome, Project | None]:
        """Point a project's spend at a team, or take it off every team.

        This is the one write that can *move* a ceiling, so it is the one that
        can defeat one: a project attributed to a team with room spends that
        team's allowance instead. Hence it is gated on ``quota:manage`` rather
        than ``project:write``, and audited — the interesting question after an
        overrun is always "who moved this, and when".

        ``team_id=None`` is a legitimate target (the default team), not a
        missing argument: ``ProjectTeamAssign`` requires the field to be sent.
        """
        if team_id is not None and self.teams.get_team(team_id) is None:
            return ProjectTeamOutcome.TEAM_NOT_FOUND, None
        project = self.projects.assign_team(project_id, team_id)
        if project is None:
            return ProjectTeamOutcome.PROJECT_NOT_FOUND, None
        outcome = (
            ProjectTeamOutcome.CLEARED if team_id is None else ProjectTeamOutcome.ASSIGNED
        )
        self._audit(
            actor,
            action=(
                "project.team_cleared" if team_id is None else "project.team_assigned"
            ),
            entity_type="project",
            entity_id=project_id,
            project_id=project_id,
            summary=(
                f"项目 {project.title} 取消团队归属（回落默认团队）"
                if team_id is None
                else f"项目 {project.title} 归入团队 {team_id}"
            ),
            payload={"project_id": project_id, "team_id": team_id},
        )
        self.session.commit()
        return outcome, project

    # --- V8.5 deployment posture ---------------------------------------------
    #
    # Two audiences, one row. An operator reads the posture (which revision the
    # database is on, which optional capabilities are on) and flips the
    # maintenance switch; the request pipeline reads the switch alone, on every
    # mutating request. That is why the switch gets its own narrow reader
    # (``maintenance_state``) instead of the pipeline calling ``deployment_overview``:
    # a hot path should pay for the one column it needs, not for a status report.

    def _deployment_settings_view(self, record: Any) -> DeploymentSettings:
        """The row as a DTO, falling back to configuration when it is absent.

        A missing row is not an error here. The migration seeds it, but a
        database assembled from the ORM metadata (the test harness, a scratch
        environment) has none — and a status endpoint that 500s precisely when
        someone is trying to find out what is wrong would be a poor tool.
        """
        if record is None:
            return DeploymentSettings(
                settings_id=DEPLOYMENT_SETTINGS_ID,
                deployment_name=settings.deployment_name,
                environment=settings.app_env,
                backup_retention_days=settings.deployment_backup_retention_days,
                configured=False,
            )
        return DeploymentSettings(
            settings_id=record.id,
            # ``or`` rather than ``is not None``: an empty string in either column
            # means "never filled in", and the environment default is a better
            # answer than a blank label everyone has to guess at.
            deployment_name=record.deployment_name or settings.deployment_name,
            environment=record.environment or settings.app_env,
            maintenance_mode=bool(record.maintenance_mode),
            maintenance_message=record.maintenance_message,
            maintenance_updated_at=record.maintenance_updated_at,
            maintenance_updated_by=record.maintenance_updated_by,
            backup_retention_days=record.backup_retention_days,
            last_backup_at=record.last_backup_at,
            last_backup_id=record.last_backup_id,
            notes=record.notes,
            configured=True,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def deployment_settings(self) -> DeploymentSettings:
        return self._deployment_settings_view(self.deployment_settings_repo.get())

    def maintenance_state(self) -> MaintenanceState:
        """Whether mutating requests are currently refused, and what to say.

        Reads nothing else on purpose: this runs per write request, so it is a
        primary-key lookup of one row plus no joins. Caching it per process would
        be faster and wrong — the replica that flipped the switch is not
        necessarily the one answering the next request.
        """
        record = self.deployment_settings_repo.get()
        if record is None or not record.maintenance_mode:
            return MaintenanceState(enabled=False)
        return MaintenanceState(enabled=True, message=record.maintenance_message)

    def alembic_current_revision(self) -> str | None:
        """``alembic_version.version_num``, or ``None`` when it cannot be read.

        ``None`` is a real answer, not a failure: a database built from the ORM
        metadata has no ``alembic_version`` table at all, and "cannot tell" is
        exactly what should be reported then. The table is checked for first so
        the common case does not have to raise and recover.
        """
        try:
            bind = self.session.get_bind()
            if not inspect(bind).has_table("alembic_version"):
                return None
            value = self.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
            return str(value) if value is not None else None
        except SQLAlchemyError:
            # A dead connection fails the whole request anyway; roll back so the
            # session is not left poisoned for the error handler that follows.
            self.session.rollback()
            return None

    def table_count(self) -> int | None:
        """How many tables the connected schema has, or ``None`` if unreadable."""
        try:
            return len(inspect(self.session.get_bind()).get_table_names())
        except SQLAlchemyError:
            self.session.rollback()
            return None

    def deployment_overview(self) -> DeploymentOverview:
        """Everything an operator wants about this install, in one read.

        ``up_to_date`` is deliberately ``False`` when the head is unknown rather
        than vacuously true: "we cannot tell which revision the code expects" is
        not a statement that the schema is current.
        """
        current = self.alembic_current_revision()
        head = alembic_head()
        return DeploymentOverview(
            settings=self.deployment_settings(),
            app_version=APP_VERSION,
            environment=settings.app_env,
            migration=AlembicStatus(
                head=head,
                current=current,
                pending=alembic_pending(current),
                up_to_date=head is not None and head == current,
            ),
            capabilities=deployment_capabilities(settings),
            table_count=self.table_count(),
        )

    def update_deployment_settings(
        self,
        payload: DeploymentSettingsUpdate,
        actor: ActorContext,
    ) -> DeploymentSettings:
        """Apply the metadata that changed. Absent fields are left untouched.

        An audit entry is written only when something actually moved. Recording
        requests rather than changes is how an audit log becomes unreadable
        exactly when it is needed: the interesting question is "who changed this",
        and a no-op PATCH has no answer to give.
        """
        record = self.deployment_settings_repo.get_or_create()
        changes: dict[str, Any] = {}
        for field in ("deployment_name", "environment", "backup_retention_days", "notes"):
            value = getattr(payload, field)
            if value is not None and getattr(record, field) != value:
                setattr(record, field, value)
                changes[field] = value
        if changes:
            self._audit(
                actor,
                action="deployment.settings_updated",
                entity_type="deployment_settings",
                entity_id=record.id,
                summary="更新部署设置",
                payload=changes,
            )
        self.session.commit()
        return self.deployment_settings()

    def set_maintenance_mode(
        self,
        *,
        enabled: bool,
        message: str | None,
        actor: ActorContext,
    ) -> DeploymentSettings:
        """Flip the write switch. Reads keep working either way.

        The audit entry marks the *transition*, not the request: flipping the
        switch to the value it already has is a no-op, and logging it would put
        noise in the trail that exists to answer "when did writes stop, and who
        stopped them".
        """
        record = self.deployment_settings_repo.get_or_create()
        was_enabled = bool(record.maintenance_mode)
        cleaned = message.strip() if isinstance(message, str) and message.strip() else None
        record.maintenance_mode = enabled
        # Enabling with no message gets the built-in one; disabling clears it, so
        # a stale "back in five minutes" cannot outlive the window it described.
        record.maintenance_message = (cleaned or DEFAULT_MAINTENANCE_MESSAGE) if enabled else None
        record.maintenance_updated_at = _naive_utc_now()
        record.maintenance_updated_by = actor.actor_id
        if was_enabled != enabled:
            self._audit(
                actor,
                action=(
                    "deployment.maintenance_enabled"
                    if enabled
                    else "deployment.maintenance_disabled"
                ),
                entity_type="deployment_settings",
                entity_id=record.id,
                summary="开启维护模式（写请求暂停）" if enabled else "关闭维护模式（写请求恢复）",
                payload={"enabled": enabled, "message": record.maintenance_message},
            )
        self.session.commit()
        return self.deployment_settings()

    def record_backup(
        self,
        payload: DeploymentBackupRequest,
        actor: ActorContext,
    ) -> DeploymentSettings:
        """Bookkeeping for a backup taken outside the process.

        The API cannot dump its own database, so ``scripts/backup.sh`` does the
        work on the host and reports back here. Having the fact in the database is
        what makes "when did we last back this up" answerable without shelling
        into the box — which is the question that decides whether a bad migration
        is survivable.
        """
        record = self.deployment_settings_repo.get_or_create()
        record.last_backup_id = payload.backup_id
        record.last_backup_at = store_utc(payload.taken_at) or _naive_utc_now()
        self._audit(
            actor,
            action="deployment.backup_recorded",
            entity_type="deployment_settings",
            entity_id=record.id,
            summary=f"记录备份 {payload.backup_id}",
            payload={
                "backup_id": payload.backup_id,
                "taken_at": record.last_backup_at.isoformat(),
                "note": payload.note,
            },
        )
        self.session.commit()
        return self.deployment_settings()

    def team_quota(
        self,
        team_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> TeamQuota | None:
        """One team's quota row, measured against its current window."""
        team = self.teams.get_team(team_id)
        if team is None:
            return None
        return self._team_quota_view(
            team.team_id,
            team.name,
            team.is_default,
            tenant_id=tenant_id,
        )

    def list_team_quotas(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        include_archived: bool = False,
    ) -> list[TeamQuota]:
        """Every team in the tenant, with its quota (or the visible lack of one)."""
        teams = self.teams.list_teams(
            tenant_id=tenant_id,
            include_archived=include_archived,
        )
        return [
            self._team_quota_view(
                team.team_id,
                team.name,
                team.is_default,
                tenant_id=tenant_id,
            )
            for team in teams
        ]

    def set_team_quota(
        self,
        team_id: str,
        payload: TeamQuotaUpdate,
        actor: ActorContext,
    ) -> TeamQuota | None:
        """Create or replace a team's ceiling. Audited, because it moves money."""
        team = self.teams.get_team(team_id)
        if team is None:
            return None
        record = self.team_quotas.upsert(
            team_id,
            tenant_id=actor.tenant_id,
            payload=payload,
            created_by=actor.actor_id,
        )
        self._audit(
            actor,
            action="team.quota_updated",
            entity_type="team_quota",
            entity_id=record.id,
            summary=f"更新团队配额 {team.name}",
            payload={
                "team_id": team_id,
                "period": record.period,
                "limit_cost": payload.limit_cost,
                "limit_runs": payload.limit_runs,
                "limit_tokens": payload.limit_tokens,
                "enforcement": record.enforcement,
                "status": record.status,
            },
        )
        self.session.commit()
        return self.team_quota(team_id, tenant_id=actor.tenant_id)

    def delete_team_quota(self, team_id: str, actor: ActorContext) -> bool | None:
        """Remove a quota entirely (back to unlimited). ``None`` = unknown team."""
        team = self.teams.get_team(team_id)
        if team is None:
            return None
        if not self.team_quotas.delete(team_id):
            return False
        self._audit(
            actor,
            action="team.quota_deleted",
            entity_type="team_quota",
            entity_id=team_id,
            summary=f"删除团队配额 {team.name}（回落为不限额）",
            payload={"team_id": team_id},
        )
        self.session.commit()
        return True

    def quota_override(self, run_id: str) -> QuotaOverride | None:
        """The exemption currently recorded on a run, if any."""
        record = self._get_run_record(run_id)
        if record is None:
            return None
        override = _quota_override(record.input_payload or {})
        return QuotaOverride(
            run_id=run_id,
            accepted=bool(override.get("accepted")),
            accepted_by=override.get("accepted_by"),
            accepted_at=override.get("accepted_at"),
            note=override.get("note"),
        )

    def confirm_quota_override(
        self,
        run_id: str,
        *,
        accept: bool = True,
        note: str | None = None,
        actor: ActorContext,
    ) -> ProductionRun | None:
        """Grant (or retract) a one-run exemption from the team's hard ceiling.

        Stored on the run's ``input_payload`` exactly like T76's paid-provider
        consent, and for the same reason: the exemption is a property of *this
        run*, recorded where the gate already looks, and revocable by the same
        call that granted it.
        """
        record = self._get_run_record(run_id)
        if record is None:
            return None
        payload = dict(record.input_payload or {})
        if accept:
            payload["quota_override"] = {
                "accepted": True,
                "accepted_by": actor.actor_id,
                "accepted_at": _utc_now(),
                "note": note,
            }
        else:
            payload.pop("quota_override", None)
        # input_payload is a plain JSON column, so it must be reassigned for
        # SQLAlchemy to notice the mutation.
        record.input_payload = payload
        self.events.append_event(
            run_id=record.id,
            event_type=(
                "run.quota_override_granted" if accept else "run.quota_override_revoked"
            ),
            source_type=EventSourceType.RUN,
            source_id=record.id,
            stage_key="quota",
            severity=EventSeverity.WARNING,
            message=(
                f"Quota override granted by {actor.actor_id}."
                if accept
                else "Quota override revoked."
            ),
            payload={
                "accepted": accept,
                "accepted_by": actor.actor_id if accept else None,
                "note": note,
            },
        )
        self.session.commit()
        return self.runs.get_run(run_id)

    def _team_quota_view(
        self,
        team_id: str,
        team_name: str | None,
        is_default: bool,
        *,
        tenant_id: str,
    ) -> TeamQuota:
        record = self.team_quotas.get(team_id)
        period = (
            TeamQuotaPeriod(record.period)
            if record is not None
            else TeamQuotaPeriod.CALENDAR_MONTH
        )
        window = quota_window(period, now=_naive_utc_now())
        usage = self.team_quotas.usage_for_team(
            tenant_id,
            team_id,
            window=window,
            default_team_id=self.teams.default_team_id(tenant_id),
        )
        return team_quota_from_record(
            record,
            tenant_id=tenant_id,
            team_id=team_id,
            usage=usage,
            team_name=team_name,
            is_default_team=is_default,
        )

    def _run_team_id(self, record: ProductionRunRecord) -> str | None:
        """Which team's ceiling this run's spend counts against.

        A project with no explicit team is the default team's, so a deployment
        that never assigns one still has exactly one answer rather than none.
        """
        attributed = self.team_quotas.project_team_id(record.project_id)
        if attributed:
            return attributed
        return self.teams.default_team_id(record.tenant_id)

    def _assert_team_quota_allows(
        self,
        run_id: str,
        *,
        estimated_cost: float | None,
        operation: str,
        additional_runs: int = 0,
        additional_tokens: int = 0,
    ) -> dict[str, Any] | None:
        """Refuse a call that would push the run's team past a *hard* ceiling.

        Returns the numbers behind the verdict when a quota was in force, and
        ``None`` when there was none to consult. The caller keeps the result so
        the once-per-run cost-reservation event can carry the team dimension
        too: without it, "this run had no ceiling" and "the ceiling was never
        looked at" would read the same in the log.

        The three outcomes are kept apart deliberately:

        * no quota / paused quota — return silently. The absence of a ceiling is
          not an event, or an unguarded team would write a row on every paid call.
        * breach under ``soft`` enforcement — one warning, call proceeds. A team
          calibrating a number it cannot yet defend still wants to know when it
          sailed past it.
        * breach under ``hard`` — one warning, then refuse, *unless* an explicit
          override is recorded on the run. The override is not a bypass of the
          record: it changes which event is written.
        """
        record = self._get_run_record(run_id)
        if record is None:
            return None
        team_id = self._run_team_id(record)
        if team_id is None:
            return None
        quota = self.team_quotas.get(team_id)
        if quota is None or quota.status != "active":
            return None
        period = TeamQuotaPeriod(quota.period)
        window = quota_window(period, now=_naive_utc_now())
        usage = self.team_quotas.usage_for_team(
            record.tenant_id,
            team_id,
            window=window,
            default_team_id=self.teams.default_team_id(record.tenant_id),
        )
        verdict = evaluate_quota(
            period=period,
            usage=usage,
            limit_cost=float(quota.limit_cost) if quota.limit_cost is not None else None,
            limit_runs=quota.limit_runs,
            limit_tokens=quota.limit_tokens,
            enforcement=quota.enforcement,
            additional_cost=estimated_cost or 0.0,
            additional_runs=additional_runs,
            additional_tokens=additional_tokens,
        )
        details: dict[str, Any] = {
            "team_id": team_id,
            "period": period.value,
            "operation": operation,
            **verdict.details(),
        }
        if not verdict.exceeded:
            return details

        override = _quota_override(record.input_payload or {})
        overridden = bool(override.get("accepted"))
        details["overridden"] = overridden
        if overridden:
            event_type = "run.team_quota_overridden"
            message = f"Team quota exceeded for {operation}; allowed by an override."
        elif verdict.blocking:
            event_type = "run.team_quota_exceeded"
            message = f"Team quota exceeded for {operation}."
        else:
            event_type = "run.team_quota_warning"
            message = f"Team quota exceeded for {operation} (soft enforcement)."
        self.events.append_event(
            run_id=record.id,
            event_type=event_type,
            source_type=EventSourceType.RUN,
            source_id=record.id,
            stage_key=operation,
            severity=EventSeverity.WARNING,
            message=message,
            payload=details,
        )
        self.session.commit()
        if verdict.blocking and not overridden:
            raise RunGateError(
                "team_quota_exceeded",
                (
                    f"this call would take the team past its {period.value} quota; "
                    "raise the quota, have a quota manager exempt this run, or wait "
                    "for the next window"
                ),
                details=details,
            )
        return details

    def list_team_members(
        self,
        team_id: str,
        *,
        include_removed: bool = False,
    ) -> list[TeamMembership] | None:
        if self.teams.get_team(team_id) is None:
            return None
        return self.teams.list_members(team_id, include_removed=include_removed)

    def add_team_member(
        self,
        team_id: str,
        payload: TeamMemberAdd,
        actor: ActorContext,
    ) -> tuple[TeamMembershipOutcome, TeamMembership | None]:
        outcome, membership = self.teams.add_member(
            team_id,
            payload,
            invited_by=actor.actor_id,
        )
        if membership is None:
            return outcome, None
        self._audit(
            actor,
            action="team.member_added" if outcome is TeamMembershipOutcome.CREATED else "team.member_updated",
            entity_type="team_membership",
            entity_id=membership.membership_id,
            summary=f"{membership.user_id} -> {membership.role}",
            payload={"team_id": team_id, "user_id": membership.user_id, "role": membership.role},
        )
        self.session.commit()
        return outcome, membership

    def update_team_member(
        self,
        membership_id: str,
        payload: TeamMemberUpdate,
        actor: ActorContext,
    ) -> TeamMembership | None:
        membership = self.teams.update_member(membership_id, payload)
        if membership is None:
            return None
        self._audit(
            actor,
            action="team.member_role_changed",
            entity_type="team_membership",
            entity_id=membership_id,
            summary=f"{membership.user_id} -> {membership.role}",
            payload=payload.model_dump(exclude_none=True),
        )
        self.session.commit()
        return membership

    def remove_team_member(self, membership_id: str, actor: ActorContext) -> TeamMembership | None:
        membership = self.teams.remove_member(membership_id)
        if membership is None:
            return None
        self._audit(
            actor,
            action="team.member_removed",
            entity_type="team_membership",
            entity_id=membership_id,
            summary=f"移除团队成员 {membership.user_id}",
            payload={"team_id": membership.team_id, "user_id": membership.user_id},
        )
        self.session.commit()
        return membership

    def list_project_members(
        self,
        project_id: str,
        *,
        include_removed: bool = False,
    ) -> list[ProjectMembership] | None:
        if self.projects.get_project(project_id) is None:
            return None
        return self.teams.list_project_members(project_id, include_removed=include_removed)

    def add_project_member(
        self,
        project_id: str,
        payload: ProjectMemberAdd,
        actor: ActorContext,
    ) -> tuple[ProjectMembershipOutcome, ProjectMembership | None]:
        outcome, membership = self.teams.add_project_member(
            project_id,
            payload,
            invited_by=actor.actor_id,
        )
        if membership is None:
            return outcome, None
        self._audit(
            actor,
            action=(
                "project.member_added"
                if outcome is ProjectMembershipOutcome.CREATED
                else "project.member_updated"
            ),
            entity_type="project_membership",
            entity_id=membership.membership_id,
            project_id=project_id,
            summary=f"{membership.user_id} -> {membership.role}",
            payload={"user_id": membership.user_id, "role": membership.role},
        )
        self.session.commit()
        return outcome, membership

    def update_project_member(
        self,
        membership_id: str,
        payload: ProjectMemberUpdate,
        actor: ActorContext,
    ) -> ProjectMembership | None:
        membership = self.teams.update_project_member(membership_id, payload)
        if membership is None:
            return None
        self._audit(
            actor,
            action="project.member_role_changed",
            entity_type="project_membership",
            entity_id=membership_id,
            project_id=membership.project_id,
            summary=f"{membership.user_id} -> {membership.role}",
            payload=payload.model_dump(exclude_none=True),
        )
        self.session.commit()
        return membership

    def remove_project_member(
        self,
        membership_id: str,
        actor: ActorContext,
    ) -> ProjectMembership | None:
        membership = self.teams.remove_project_member(membership_id)
        if membership is None:
            return None
        self._audit(
            actor,
            action="project.member_removed",
            entity_type="project_membership",
            entity_id=membership_id,
            project_id=membership.project_id,
            summary=f"移除项目成员 {membership.user_id}",
            payload={"user_id": membership.user_id},
        )
        self.session.commit()
        return membership

    def list_audit_logs(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> list[AuditLogEntry]:
        return self.audit.list_logs(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            project_id=project_id,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def count_audit_logs(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        return self.audit.count_logs(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            project_id=project_id,
            entity_id=entity_id,
        )

    # --- idempotent writes (V7.1) --------------------------------------------

    def begin_idempotent_request(
        self,
        actor: ActorContext,
        *,
        key: str,
        method: str,
        path: str,
        fingerprint: str,
    ) -> IdempotencyClaim:
        """Claim (or match) an ``Idempotency-Key`` and make the claim durable.

        The commit is deliberate: the claim has to be visible to a concurrent
        duplicate *before* this request starts doing work, otherwise the second
        caller would start the same work in parallel.
        """
        claim = self.idempotency.claim(
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            idempotency_key=key,
            method=method,
            path=path,
            fingerprint=fingerprint,
            ttl_seconds=settings.idempotency_ttl_seconds,
        )
        self.session.commit()
        return claim

    def list_idempotency_keys(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        state: str | None = None,
        actor_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[IdempotencyKeyEntry], int]:
        return self.idempotency.list_entries(
            tenant_id=tenant_id,
            state=state,
            actor_id=actor_id,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def purge_expired_idempotency_keys(self) -> int:
        count = self.idempotency.purge_expired()
        self.session.commit()
        return count

    # --- programmatic credentials (V7.2) --------------------------------------

    def create_api_key(self, payload: ApiKeyCreate, actor: ActorContext) -> ApiKeyIssued:
        """Issue a credential owned by ``actor``.

        The key inherits the owner's *tenant-level* permissions and may narrow
        them via ``scopes``; the repository refuses a scope the owner does not
        hold. Returns the issued record plus the one-time plaintext — the only
        moment the plaintext exists outside the caller's hands.
        """
        owner_permissions = frozenset(
            permission.value for permission in self.permissions_for_project(actor, None)
        )
        issued = self.api_keys.create(
            payload,
            owner_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            created_by=actor.actor_id,
            owner_permissions=owner_permissions,
        )
        if issued.outcome is not ApiKeyOutcome.CREATED or issued.api_key is None:
            return issued
        # ``api_keys.actor_id`` has no foreign key (identities come from the
        # gateway), but the owner still has to exist in ``users`` for MySQL-side
        # referential integrity of anything that later points at them.
        UserRepository(self.session).ensure_user(
            actor.actor_id,
            tenant_id=actor.tenant_id,
            display_name=actor.display_name,
        )
        self._audit(
            actor,
            action="apikey.created",
            entity_type="api_key",
            entity_id=issued.api_key.api_key_id,
            summary=f"签发 API key {issued.api_key.name}",
            payload={
                "key_prefix": issued.api_key.key_prefix,
                "scopes": issued.api_key.scopes,
            },
        )
        self.session.commit()
        return issued

    def list_api_keys(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[ApiKey], int]:
        return self.api_keys.list_keys(
            tenant_id=tenant_id,
            actor_id=actor_id,
            status=status,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def get_api_key(self, api_key_id: str) -> ApiKey | None:
        return self.api_keys.get(api_key_id)

    def revoke_api_key(self, api_key_id: str, actor: ActorContext) -> ApiKey | None:
        """Revoke a credential. Idempotent: the second call is a no-op, not an error.

        Only a revocation that actually changed the row is audited — repeating it
        must not inflate the trail into a count of retries.
        """
        outcome = self.api_keys.revoke(api_key_id, tenant_id=actor.tenant_id)
        key = outcome.api_key
        if key is None:
            return None
        if not outcome.changed:
            return key
        self._audit(
            actor,
            action="apikey.revoked",
            entity_type="api_key",
            entity_id=key.api_key_id,
            summary=f"吊销 API key {key.name}",
            payload={"key_prefix": key.key_prefix},
        )
        self.session.commit()
        return key

    def authenticate_api_key(self, plaintext: str) -> ApiKeyRecord | None:
        """Resolve a presented credential and stamp its last-use time.

        Returning ``None`` covers every reason a key is unusable — unknown,
        revoked, expired, malformed — so the caller has one branch and the wire
        response does not tell an attacker which of those it was.
        """
        record = self.api_keys.verify(plaintext)
        if record is None:
            return None
        self.api_keys.touch(record.id)
        self.session.commit()
        return record

    # --- username-password login (V1.1) -----------------------------------------

    def set_user_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        actor: ActorContext,
        email: str | None = None,
    ) -> UserRecord | None:
        """Set (or replace) a user's login password and return the row.

        Provisioning is deliberately part of this call: an operator can seat a
        brand-new login user in one request, and the reviewer identities the
        review system created on demand stay untouched. Setting a password
        invalidates every session minted before the change — the verify path
        compares ``password_updated_at`` against the session's creation — so a
        compromised session does not survive a password reset.
        """
        existing = self.session.get(UserRecord, user_id)
        if existing is not None and existing.tenant_id != actor.tenant_id:
            # A user from another tenant reads exactly like a user who does not
            # exist — 404, not 403, so the refusal leaks no existence.
            return None
        # ``ensure_user`` returns the id once the row is guaranteed to back it;
        # the record is read fresh so the caller always sees the seated row.
        seated_id = UserRepository(self.session).ensure_user(
            user_id,
            tenant_id=actor.tenant_id,
            email=email,
        )
        user = self.session.get(UserRecord, seated_id) if seated_id else None
        if user is None:
            return None
        user.password_hash = password_hash
        user.password_updated_at = _utcnow()
        self._audit(
            actor,
            action="auth.password_set",
            entity_type="user",
            entity_id=user.id,
            summary=f"设置用户 {user.email} 的登录密码",
        )
        self.session.commit()
        return user

    def authenticate_password(self, email: str, password: str) -> UserRecord | None:
        """Return the user behind working login credentials, or ``None``.

        ``None`` covers unknown email, no password set, wrong password and an
        inactive account alike — one verdict, no oracle. When several rows
        share the address (only possible once tenants do), the login is refused
        rather than guessed: ambiguity is a configuration problem, not
        something a login form should resolve silently.
        """
        records = list(
            self.session.scalars(
                select(UserRecord).where(
                    UserRecord.email == email.strip(),
                    UserRecord.password_hash.is_not(None),
                )
            )
        )
        if len(records) != 1:
            return None
        user = records[0]
        if user.status != "active":
            return None
        from apps.api.app.passwords import verify_password

        if not verify_password(password, user.password_hash):
            return None
        return user

    def create_login_session(self, user: UserRecord) -> tuple[UserSessionRecord, str]:
        """Mint a session for a just-verified user; return ``(row, plaintext)``.

        The plaintext token is returned here and nowhere else, exactly like an
        API key at issue time: only the peppered digest is stored.
        """
        from apps.api.app.passwords import (
            default_session_expiry,
            generate_session_token,
            hash_session_token,
        )

        prefix, plaintext = generate_session_token()
        record = UserSessionRecord(
            id=f"sess_{uuid4().hex[:12]}",
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_prefix=prefix,
            token_hash=hash_session_token(plaintext),
            expires_at=default_session_expiry(),
            # Written explicitly, not left to the server default: the rotation
            # check compares this against ``password_updated_at``, and the
            # server-side CURRENT_TIMESTAMP is second-precise while the
            # password timestamp is not — a session minted in the same second
            # as its password would read as "minted before the change" and
            # refuse its very first request.
            created_at=_utcnow(),
        )
        self.session.add(record)
        self._audit(
            ActorContext(
                actor_id=user.id,
                tenant_id=user.tenant_id,
                display_name=user.name,
                source="session",
            ),
            action="auth.login",
            entity_type="user_session",
            entity_id=record.id,
            summary=f"用户 {user.email} 登录",
        )
        self.session.commit()
        return record, plaintext

    def authenticate_session_token(self, plaintext: str) -> SessionIdentity | None:
        """Resolve a presented session token into user + session, or ``None``.

        One ``None`` for unknown, revoked, expired, malformed and
        password-rotated alike — the same one-verdict rule the API key and
        embed verifiers follow. A live session also stamps ``last_used_at``.
        """
        from hmac import compare_digest

        from apps.api.app.passwords import hash_session_token, session_token_prefix_of

        prefix = session_token_prefix_of(plaintext)
        if prefix is None:
            return None
        record = self.session.scalar(
            select(UserSessionRecord).where(UserSessionRecord.token_prefix == prefix)
        )
        if record is None:
            return None
        if not compare_digest(record.token_hash, hash_session_token(plaintext)):
            return None
        if record.revoked_at is not None:
            return None
        expires = as_utc(record.expires_at)
        if expires is not None and expires <= _utcnow():
            return None
        user = self.session.get(UserRecord, record.user_id)
        if user is None or user.status != "active":
            return None
        # A password change revokes every session minted before it (see
        # ``set_user_password``); comparing on the normalized timestamps keeps
        # the check honest on both MySQL and SQLite.
        rotated = as_utc(user.password_updated_at)
        minted = as_utc(record.created_at)
        if rotated is not None and minted is not None and rotated > minted:
            return None
        record.last_used_at = _utcnow()
        self.session.commit()
        return SessionIdentity(
            session_id=record.id,
            user_id=user.id,
            tenant_id=user.tenant_id,
            user=user,
            record=record,
        )

    def revoke_session(
        self,
        session_id: str,
        *,
        actor: ActorContext,
    ) -> UserSessionRecord | None:
        """Revoke a login session. Idempotent: the second call is a no-op."""
        record = self.session.get(UserSessionRecord, session_id)
        if record is None or record.tenant_id != actor.tenant_id:
            return None
        if record.revoked_at is not None:
            return record
        record.revoked_at = _utcnow()
        self._audit(
            actor,
            action="auth.logout",
            entity_type="user_session",
            entity_id=record.id,
            summary=f"用户会话 {record.id} 退出登录",
        )
        self.session.commit()
        return record

    # --- outbound webhooks (V7.3) ----------------------------------------------

    def create_webhook_subscription(
        self,
        payload: WebhookSubscriptionCreate,
        actor: ActorContext,
    ) -> WebhookSubscriptionResult:
        result = self.webhooks.create_subscription(
            payload,
            tenant_id=actor.tenant_id,
            created_by=actor.actor_id,
        )
        if result.outcome is not WebhookSubscriptionOutcome.CREATED:
            return result
        assert result.subscription is not None
        self._audit(
            actor,
            action="webhook.subscription_created",
            entity_type="webhook_subscription",
            entity_id=result.subscription.subscription_id,
            summary=f"创建 webhook 订阅 {result.subscription.name}",
            payload={"url_host": urlparse(result.subscription.url).netloc,
                     "event_types": result.subscription.event_types},
        )
        self.session.commit()
        return result

    def list_webhook_subscriptions(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[WebhookSubscription], int]:
        return self.webhooks.list_subscriptions(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def get_webhook_subscription(
        self,
        subscription_id: str,
        *,
        tenant_id: str | None = None,
    ) -> WebhookSubscription | None:
        """Read one subscription. ``tenant_id=None`` is worker-side use; the API
        always passes the caller's tenant so a cross-tenant id reads as absent."""
        subscription = self.webhooks.get_subscription(subscription_id)
        if subscription is None:
            return None
        if tenant_id is not None and subscription.tenant_id != tenant_id:
            return None
        return subscription

    def update_webhook_subscription(
        self,
        subscription_id: str,
        payload: WebhookSubscriptionUpdate,
        actor: ActorContext,
    ) -> WebhookSubscriptionResult:
        result = self.webhooks.update_subscription(
            subscription_id,
            payload,
            tenant_id=actor.tenant_id,
        )
        if result.outcome is not WebhookSubscriptionOutcome.UPDATED:
            return result
        assert result.subscription is not None
        self._audit(
            actor,
            action="webhook.subscription_updated",
            entity_type="webhook_subscription",
            entity_id=subscription_id,
            summary=f"更新 webhook 订阅 {result.subscription.name}",
            payload={"changed": sorted(payload.model_dump(exclude_unset=True))},
        )
        self.session.commit()
        return result

    def delete_webhook_subscription(
        self,
        subscription_id: str,
        actor: ActorContext,
    ) -> WebhookSubscription | None:
        subscription = self.webhooks.delete_subscription(
            subscription_id,
            tenant_id=actor.tenant_id,
        )
        if subscription is None:
            return None
        self._audit(
            actor,
            action="webhook.subscription_deleted",
            entity_type="webhook_subscription",
            entity_id=subscription_id,
            summary=f"删除 webhook 订阅 {subscription.name}",
            payload={"url_host": urlparse(subscription.url).netloc},
        )
        self.session.commit()
        return subscription

    def rotate_webhook_secret(
        self,
        subscription_id: str,
        actor: ActorContext,
    ) -> WebhookSubscription | None:
        subscription = self.webhooks.rotate_secret(
            subscription_id,
            tenant_id=actor.tenant_id,
        )
        if subscription is None:
            return None
        self._audit(
            actor,
            action="webhook.secret_rotated",
            entity_type="webhook_subscription",
            entity_id=subscription_id,
            summary=f"轮换 webhook 签名密钥 {subscription.name}",
        )
        self.session.commit()
        return subscription

    def list_webhook_deliveries(
        self,
        *,
        tenant_id: str,
        subscription_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[WebhookDelivery], int]:
        return self.webhooks.list_deliveries(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            status=status,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def requeue_webhook_delivery(
        self,
        delivery_id: str,
        actor: ActorContext,
    ) -> WebhookDelivery | None:
        delivery = self.webhooks.requeue_delivery(
            delivery_id,
            tenant_id=actor.tenant_id,
        )
        if delivery is None:
            return None
        self._audit(
            actor,
            action="webhook.delivery_requeued",
            entity_type="webhook_delivery",
            entity_id=delivery_id,
            summary=f"手动重投 webhook 投递 {delivery_id}",
            payload={"event_type": delivery.event_type,
                     "subscription_id": delivery.subscription_id},
        )
        self.session.commit()
        return delivery

    def claim_webhook_deliveries(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> list[WebhookDelivery]:
        deliveries = self.webhooks.claim_pending_deliveries(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        self.session.commit()
        return deliveries

    def settle_webhook_delivery(
        self,
        delivery_id: str,
        *,
        attempt: int,
        signature: str,
        response_status: int | None,
        response_body: str | None,
        error_message: str | None,
        retry_delay_seconds: float | None,
        disable_after_consecutive_failures: int = 0,
    ) -> WebhookDeliveryAttempt:
        """Record one attempt's outcome. ``error_message is None`` means success.

        The dispatcher computes the retry schedule (or exhaustion) and hands it
        over as ``retry_delay_seconds``; ``None`` plus an error means abandoned.
        A failure also counts against the subscription, which can auto-disable
        it once ``disable_after_consecutive_failures`` is crossed.
        """
        now = datetime.now(UTC)
        if error_message is None:
            delivery = self.webhooks.mark_delivery_succeeded(
                delivery_id,
                attempt=attempt,
                signature=signature,
                response_status=response_status,
                response_body=response_body,
                now=now,
            )
            if delivery is not None:
                self.webhooks.note_subscription_success(delivery.subscription_id, now=now)
            self.session.commit()
            return WebhookDeliveryAttempt(delivery=delivery)
        next_attempt_at = (
            now + timedelta(seconds=max(1.0, retry_delay_seconds))
            if retry_delay_seconds is not None
            else None
        )
        delivery = self.webhooks.mark_delivery_failed(
            delivery_id,
            attempt=attempt,
            signature=signature,
            response_status=response_status,
            response_body=response_body,
            error_message=error_message,
            next_attempt_at=next_attempt_at,
            now=now,
        )
        disabled = False
        if delivery is not None and next_attempt_at is not None:
            # Exhausted attempts have already stopped mattering to the endpoint;
            # only a still-retrying failure says "this endpoint keeps answering
            # badly right now", which is what auto-pause should key on.
            disabled = self.webhooks.note_subscription_failure(
                delivery.subscription_id,
                disable_after=disable_after_consecutive_failures,
                now=now,
            )
        self.session.commit()
        return WebhookDeliveryAttempt(
            delivery=delivery,
            subscription_disabled=disabled,
        )

    # --- external integrations and embed sessions (V7.4) -----------------------

    def create_external_integration(
        self,
        payload: ExternalIntegrationCreate,
        actor: ActorContext,
    ) -> ExternalIntegrationResult:
        result = self.integrations.create_integration(
            payload,
            tenant_id=actor.tenant_id,
            created_by=actor.actor_id,
        )
        if result.outcome is not ExternalIntegrationOutcome.CREATED:
            return result
        assert result.integration is not None
        self._audit(
            actor,
            action="integration.created",
            entity_type="external_integration",
            entity_id=result.integration.integration_id,
            summary=f"注册外部集成 {result.integration.name}",
            payload={
                "slug": result.integration.slug,
                "allowed_origins": result.integration.allowed_origins,
                "status": str(result.integration.status),
            },
        )
        self.session.commit()
        return result

    def list_external_integrations(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[ExternalIntegration], int]:
        return self.integrations.list_integrations(
            tenant_id=tenant_id,
            status=status,
            limit=limit,
            offset=offset,
            sort=sort,
        )

    def get_external_integration(
        self,
        integration_id: str,
        actor: ActorContext,
    ) -> ExternalIntegration | None:
        """Tenant-scoped read: a cross-tenant id is indistinguishable from absent."""
        return self.integrations.get_integration(
            integration_id,
            tenant_id=actor.tenant_id,
        )

    def find_external_integration(self, integration_id: str) -> ExternalIntegration | None:
        """Tenant-agnostic read, used only by the embed document route (see repo)."""
        return self.integrations.find_integration(integration_id)

    def update_external_integration(
        self,
        integration_id: str,
        payload: ExternalIntegrationUpdate,
        actor: ActorContext,
    ) -> ExternalIntegrationResult:
        result = self.integrations.update_integration(
            integration_id,
            payload,
            tenant_id=actor.tenant_id,
        )
        if result.outcome is not ExternalIntegrationOutcome.UPDATED:
            return result
        assert result.integration is not None
        self._audit(
            actor,
            action="integration.updated",
            entity_type="external_integration",
            entity_id=integration_id,
            summary=f"更新外部集成 {result.integration.name}",
            payload={
                "changed": sorted(payload.model_dump(exclude_unset=True)),
                "status": str(result.integration.status),
                "allowed_origins": result.integration.allowed_origins,
            },
        )
        self.session.commit()
        return result

    def delete_external_integration(
        self,
        integration_id: str,
        actor: ActorContext,
    ) -> ExternalIntegration | None:
        integration = self.integrations.delete_integration(
            integration_id,
            tenant_id=actor.tenant_id,
        )
        if integration is None:
            return None
        self._audit(
            actor,
            action="integration.deleted",
            entity_type="external_integration",
            entity_id=integration_id,
            summary=f"删除外部集成 {integration.name}",
            payload={"slug": integration.slug},
        )
        self.session.commit()
        return integration

    def mint_embed_session(
        self,
        integration_id: str,
        payload: EmbedSessionCreate,
        actor: ActorContext,
    ) -> EmbedMintResult:
        """Turn one integration plus one project into a link that is ready to open.

        Everything that could make the link wrong is settled here rather than at
        open time, because at open time the holder can only be told "this link
        does not work" — with no hint which of the ways it is wrong. So a caller
        that mixed up a project id learns it from a 404 now, in its own logs,
        instead of from a support ticket later.
        """
        integration = self.integrations.get_integration(
            integration_id,
            tenant_id=actor.tenant_id,
        )
        if integration is None:
            return EmbedMintResult(EmbedMintOutcome.INTEGRATION_NOT_FOUND)
        if integration.status is not ExternalIntegrationStatus.ACTIVE:
            return EmbedMintResult(EmbedMintOutcome.INTEGRATION_DISABLED)
        if self.projects.get_project(payload.project_id) is None:
            return EmbedMintResult(EmbedMintOutcome.PROJECT_NOT_FOUND)
        if payload.run_id is not None:
            run = self.get_run(payload.run_id)
            if run is None or run.project_id != payload.project_id:
                # A run that belongs to another project answers exactly like a run
                # that does not exist: the reply must not confirm it exists.
                return EmbedMintResult(EmbedMintOutcome.RUN_NOT_FOUND)
        try:
            token, session = issue_embed_session(
                integration_id=integration_id,
                tenant_id=actor.tenant_id,
                actor_id=payload.actor_id,
                project_id=payload.project_id,
                run_id=payload.run_id,
                page=payload.page,
                display_name=payload.actor_name,
                scopes=payload.scopes,
                ttl_seconds=payload.ttl_seconds,
            )
        except ValueError as error:
            return EmbedMintResult(EmbedMintOutcome.INVALID_REQUEST, detail=str(error))
        self._audit(
            actor,
            action="embed.session_issued",
            entity_type="external_integration",
            entity_id=integration_id,
            project_id=payload.project_id,
            summary=f"为 {integration.name} 签发嵌入会话",
            payload={
                "run_id": payload.run_id,
                "page": payload.page,
                "scopes": sorted(session.scopes),
                "expires_at": session.expires_at.isoformat(),
                "subject": session.actor_id,
            },
        )
        self.session.commit()
        return EmbedMintResult(
            EmbedMintOutcome.ISSUED,
            token=token,
            session=session,
            integration=integration,
        )

    def describe_embed_session(
        self,
        session: EmbedSession,
    ) -> EmbedSessionDescription | None:
        """The context an embed page renders from, or ``None`` if it lost its footing.

        Re-reading the integration on *every* call is the whole point: the token
        is stateless, so disabling or deleting the row is the only revocation
        there is. Without this read a link would keep working until it expired,
        which is exactly the gap ``disabled`` exists to close.
        """
        integration = self.integrations.get_integration(
            session.integration_id,
            tenant_id=session.tenant_id,
        )
        if integration is None or integration.status is not ExternalIntegrationStatus.ACTIVE:
            return None
        return EmbedSessionDescription(
            actor_id=session.actor_id,
            display_name=session.display_name or session.actor_id,
            integration_id=session.integration_id,
            integration_name=integration.name,
            context=EmbedContext(
                project_id=session.project_id,
                run_id=session.run_id,
                page=session.page,
            ),
            scopes=sorted(session.scopes),
            expires_at=session.expires_at,
            remaining_seconds=session.remaining_seconds,
        )

    def _audit(
        self,
        actor: ActorContext,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None = None,
        project_id: str | None = None,
        summary: str | None = None,
        payload: dict | None = None,
    ) -> AuditLogEntry:
        return self.audit.record(
            action=action,
            entity_type=entity_type,
            actor_id=actor.actor_id,
            entity_id=entity_id,
            project_id=project_id,
            summary=summary,
            payload=payload,
            tenant_id=actor.tenant_id,
        )

    def list_project_summaries(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[ProjectDashboardProjectSummary]:
        projects = self.projects.list_projects(tenant_id=tenant_id)
        if not projects:
            return []
        project_ids = [project.project_id for project in projects]
        run_records = list(
            self.session.scalars(
                select(ProductionRunRecord)
                .where(ProductionRunRecord.project_id.in_(project_ids))
                .order_by(ProductionRunRecord.updated_at.desc(), ProductionRunRecord.id)
            )
        )
        counts_by_project: dict[str, Counter] = defaultdict(Counter)
        latest_by_project: dict[str, ProductionRunRecord] = {}
        for record in run_records:
            counts_by_project[record.project_id][record.status] += 1
            latest_by_project.setdefault(record.project_id, record)
        summaries = []
        for project in projects:
            latest = latest_by_project.get(project.project_id)
            summaries.append(
                ProjectDashboardProjectSummary(
                    project=project,
                    run_count=sum(counts_by_project[project.project_id].values()),
                    status_counts=dict(counts_by_project[project.project_id]),
                    latest_run_id=latest.id if latest else None,
                    latest_run_status=RunStatus(latest.status) if latest else None,
                    latest_run_updated_at=latest.updated_at if latest else None,
                )
            )
        return summaries

    def project_dashboard(
        self,
        project_id: str,
        *,
        status: RunStatus | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> ProjectDashboard | None:
        project = self.projects.get_project(project_id)
        if project is None:
            return None
        run_records = self._project_run_records(project_id)
        visible_records = self._filter_dashboard_runs(run_records, status=status, search=search)
        limited_records = visible_records[: max(1, min(limit, 200))]
        run_ids = [record.id for record in run_records]
        visible_run_ids = [record.id for record in limited_records]
        jobs_by_run = self._dashboard_jobs_by_run(run_ids)
        artifact_counts = self._dashboard_artifact_counts(run_ids)
        asset_counts = self._dashboard_asset_counts(run_ids)
        runs = [
            self._dashboard_run_summary(
                record,
                jobs=jobs_by_run.get(record.id, []),
                artifact_count=artifact_counts.get(record.id, 0),
                asset_count=asset_counts.get(record.id, 0),
            )
            for record in limited_records
        ]
        failed_jobs = [
            _dashboard_job_summary(job)
            for job in sorted(
                (
                    job
                    for jobs in jobs_by_run.values()
                    for job in jobs
                    if job.status == BackgroundJobStatus.FAILED.value
                ),
                key=lambda job: (_dashboard_sort_datetime(job.updated_at), job.id),
                reverse=True,
            )[:20]
        ]
        open_requests_by_stage_run_id: dict[str, ReviewRequest] = {}
        for record in run_records:
            for request in self.reviews.list_open_requests_for_run(record.id):
                open_requests_by_stage_run_id.setdefault(request.stage_run_id, request)
        review_queue = [
            _dashboard_review_item(
                record,
                stage,
                review_request=open_requests_by_stage_run_id.get(stage.id),
                invalidated=stage.invalidated_at is not None,
            )
            for record in run_records
            for stage in sorted(record.stages, key=lambda item: (item.stage_order, item.id))
            if stage.status == StageStatus.WAITING_REVIEW.value
        ][:50]
        metrics = self._dashboard_metrics(
            run_records,
            jobs_by_run=jobs_by_run,
            review_queue=review_queue,
            invalidated_stage_count=sum(
                1
                for record in run_records
                for stage in record.stages
                if stage.invalidated_at is not None
            ),
        )
        return ProjectDashboard(
            project=project,
            metrics=metrics,
            status_counts=dict(Counter(record.status for record in run_records)),
            pipeline_counts=dict(Counter(record.pipeline_key for record in run_records)),
            runs=runs,
            failed_jobs=failed_jobs,
            review_queue=review_queue,
            filters={
                "status": status.value if status else None,
                "search": search or "",
                "limit": max(1, min(limit, 200)),
                "returned_runs": len(visible_run_ids),
                "matched_runs": len(visible_records),
            },
        )

    def list_project_dashboard_runs(
        self,
        project_id: str,
        *,
        status: RunStatus | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> list[DashboardRunSummary] | None:
        dashboard = self.project_dashboard(
            project_id,
            status=status,
            search=search,
            limit=limit,
        )
        return dashboard.runs if dashboard else None

    def run_board(self, run_id: str) -> RunBoard | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        project = self.projects.get_project(run.project_id)
        if project is None:
            return None
        artifact_records = self._run_board_artifact_records(run_id)
        asset_records = self._run_board_asset_records(run_id)
        artifacts = [_run_board_artifact_summary(record) for record in artifact_records]
        assets = [_run_board_asset_summary(record) for record in asset_records]
        jobs = self.list_background_jobs(run_id) or []
        invocations = self.list_tool_invocations(run_id) or []
        usage_summary = self.get_usage_summary(run_id)
        if usage_summary is None:
            return None
        events = self.list_events(run_id, limit=200) or []
        dispatch_preview = self.preview_stage_execution_dispatch(run_id) or {}
        issues_by_stage, actions_by_stage = _run_board_input_maps(dispatch_preview)
        artifacts_by_stage = _group_by_stage_key(artifacts)
        assets_by_stage_run_id = _group_by_stage_run_id(assets)
        jobs_by_stage_run_id = _group_by_stage_run_id(jobs)
        invocations_by_stage_run_id = _group_by_stage_run_id(invocations)
        review_requests = self.reviews.list_requests(run_id)
        review_timeline = self.reviews.list_run_decisions(run_id)
        # Requests are ordered by iteration, so the last write per stage wins.
        # The board shows the newest request even when it is already closed, so a
        # reviewer can see why a gate was rejected.
        latest_request_by_stage_key: dict[str, ReviewRequest] = {}
        for request in review_requests:
            latest_request_by_stage_key[request.stage_key] = request
        open_request_count = sum(
            1 for request in review_requests if request.status == ReviewRequestStatus.OPEN
        )
        open_comment_counts = Counter(
            comment.stage_key
            for comment in self.reviews.list_run_comments(run_id)
            if comment.status == ReviewCommentStatus.OPEN
        )
        open_action_item_counts = Counter(
            item.stage_key
            for item in self.reviews.list_run_action_items(
                run_id,
                status=ReviewActionStatus.OPEN,
            )
            if item.stage_key
        )
        stage_lanes = [
            RunBoardStageLane(
                stage=stage,
                current=stage.stage_key == run.current_stage_key,
                artifacts=artifacts_by_stage.get(stage.stage_key, []),
                assets=assets_by_stage_run_id.get(stage.stage_run_id, []),
                jobs=jobs_by_stage_run_id.get(stage.stage_run_id, []),
                tool_invocations=invocations_by_stage_run_id.get(stage.stage_run_id, []),
                input_issues=issues_by_stage.get(stage.stage_key, []),
                repair_actions=actions_by_stage.get(stage.stage_key, []),
                review_request=latest_request_by_stage_key.get(stage.stage_key),
                open_comment_count=open_comment_counts.get(stage.stage_key, 0),
                open_action_item_count=open_action_item_counts.get(stage.stage_key, 0),
            )
            for stage in sorted(run.stages, key=lambda item: item.order)
        ]
        decision_log = _run_board_decision_log(artifact_records)
        final_outputs = _run_board_final_outputs(assets)
        version_groups = _run_board_version_groups(final_outputs)
        input_issues = [
            issue
            for stage_issues in issues_by_stage.values()
            for issue in stage_issues
        ]
        repair_actions = _dedupe_run_board_actions(
            action
            for stage_actions in actions_by_stage.values()
            for action in stage_actions
        )
        stage_counts = Counter(stage.status.value for stage in run.stages)
        job_counts = Counter(job.status.value for job in jobs)
        execution_plan_payload = self.artifacts.current_payloads(run_id).get("execution_plan") or {}
        cost_reservation = execution_plan_payload.get("cost_reservation") or {}
        paid_provider_consents = {
            provider: True
            for provider in (run.input_payload or {}).get("paid_provider_consents", {})
            if (run.input_payload or {}).get("paid_provider_consents", {}).get(provider)
        }
        paid_providers_pending = [
            provider
            for provider in cost_reservation.get("paid_providers", [])
            if provider not in paid_provider_consents
        ]
        return RunBoard(
            run=run,
            project=project,
            metrics={
                "artifact_count": len(artifacts),
                "asset_count": len(assets),
                "decision_count": len(decision_log),
                "final_output_count": len(final_outputs),
                "stage_counts": dict(stage_counts),
                "job_counts": dict(job_counts),
                "input_issue_count": len(input_issues),
                "review_request_count": len(review_requests),
                "open_review_request_count": open_request_count,
                "invalidated_stage_count": sum(
                    1 for stage in run.stages if stage.invalidation is not None
                ),
                "actual_cost": usage_summary.actual_cost,
                "estimated_cost": usage_summary.estimated_cost,
                "asset_bytes": usage_summary.asset_bytes,
            },
            stage_lanes=stage_lanes,
            artifact_timeline=artifacts,
            decision_log=decision_log,
            usage_summary=usage_summary,
            job_log=jobs,
            cost_log=usage_summary.entries,
            event_log=events,
            input_issues=input_issues,
            repair_actions=repair_actions,
            final_outputs=final_outputs,
            version_groups=version_groups,
            review_requests=review_requests,
            review_timeline=review_timeline,
            invalidated_stages=[
                stage.stage_key for stage in run.stages if stage.invalidation is not None
            ],
            intent_downgrades=execution_plan_payload.get("intent_downgrades", []),
            requires_intent_acknowledgement=bool(
                execution_plan_payload.get("requires_intent_acknowledgement")
            ),
            cost_reservation=cost_reservation,
            paid_provider_consents=paid_provider_consents,
            paid_providers_pending=paid_providers_pending,
            workspace_url=f"/workspace?run_id={run_id}",
            dashboard_url=f"/dashboard?project_id={run.project_id}",
        )

    def create_run(
        self,
        *,
        project_id: str,
        payload: RunCreate,
        pipeline: PipelineDefinition,
    ) -> ProductionRun:
        style_snapshot = self.brand_styles.default_style_preset_snapshot_for_project(project_id)
        run = self.runs.create_run(
            project_id=project_id,
            payload=payload,
            pipeline=pipeline,
            style_preset_snapshot=style_snapshot,
            # T97: the run inherits the tenant of the project it belongs to, so
            # the scope guard reads back the same tenant the writer put in —
            # including for the internal sample/screening paths, which create
            # runs under projects they just made or resolved.
            tenant_id=self.tenant_id_for_project(project_id) or DEFAULT_TENANT_ID,
        )
        self.events.append_event(
            run_id=run.run_id,
            event_type="run.created",
            source_type=EventSourceType.RUN,
            source_id=run.run_id,
            message="Run created.",
            payload={
                "pipeline_key": run.pipeline_key,
                "pipeline_version_id": run.pipeline_version_id,
                "pipeline_version": run.pipeline_version,
                "pipeline_dsl_version": run.pipeline_dsl_version,
                "status": run.status.value,
                "style_preset_id": (
                    (style_snapshot.get("style_preset") or {}).get("style_preset_id")
                    if style_snapshot
                    else None
                ),
            },
        )
        self.session.commit()
        return run

    def copy_run(self, run_id: str, payload: RunCopyRequest) -> RunCopyResult | None:
        source_record = self._get_run_record(run_id)
        if source_record is None:
            return None
        target_project_id = payload.target_project_id or source_record.project_id
        if self.projects.get_project(target_project_id) is None:
            raise ValueError("target project not found")
        input_payload = dict(source_record.input_payload or {})
        input_payload["pipeline_key"] = source_record.pipeline_key
        input_payload.update(payload.overrides)
        input_payload.pop("style_preset_snapshot", None)
        run_payload = RunCreate.model_validate(input_payload)
        pipeline = self.pipeline_loader.get_pipeline(run_payload.pipeline_key)
        copied = self.create_run(
            project_id=target_project_id,
            payload=run_payload,
            pipeline=pipeline,
        )
        self.events.append_event(
            run_id=copied.run_id,
            event_type="run.copied",
            source_type=EventSourceType.RUN,
            source_id=copied.run_id,
            message="Run copied from existing run.",
            payload={
                "source_run_id": run_id,
                "copy_reason": payload.copy_reason,
            },
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.copy_created",
            source_type=EventSourceType.RUN,
            source_id=run_id,
            message="A draft copy was created from this run.",
            payload={
                "copied_run_id": copied.run_id,
                "target_project_id": target_project_id,
                "copy_reason": payload.copy_reason,
            },
        )
        self.session.commit()
        refreshed = self.get_run(copied.run_id)
        if refreshed is None:
            raise RuntimeError("copied run disappeared")
        return RunCopyResult(
            source_run_id=run_id,
            run=refreshed,
            workspace_url=f"/workspace?run_id={refreshed.run_id}",
        )

    def create_brand_kit(self, project_id: str, payload: BrandKitCreate) -> BrandKit | None:
        # T97: a child row inherits the tenant of the parent project it hangs
        # under — never a global default — so the scope guard reads the same
        # tenant back that the writer put in.
        brand_kit = self.brand_styles.create_brand_kit(
            project_id,
            payload,
            tenant_id=self.tenant_id_for_project(project_id) or DEFAULT_TENANT_ID,
        )
        if brand_kit is not None:
            self.session.commit()
        return brand_kit

    def list_brand_kits(self, project_id: str) -> list[BrandKit] | None:
        return self.brand_styles.list_brand_kits(project_id)

    def get_brand_kit(self, brand_kit_id: str) -> BrandKit | None:
        return self.brand_styles.get_brand_kit(brand_kit_id)

    def update_brand_kit(self, brand_kit_id: str, payload: BrandKitUpdate) -> BrandKit | None:
        brand_kit = self.brand_styles.update_brand_kit(brand_kit_id, payload)
        if brand_kit is not None:
            self.session.commit()
        return brand_kit

    def create_style_preset(
        self,
        project_id: str,
        payload: StylePresetCreate,
    ) -> StylePreset | None:
        style_preset = self.brand_styles.create_style_preset(
            project_id,
            payload,
            tenant_id=self.tenant_id_for_project(project_id) or DEFAULT_TENANT_ID,
        )
        if style_preset is not None:
            self.session.commit()
        return style_preset

    def list_style_presets(self, project_id: str) -> list[StylePreset] | None:
        return self.brand_styles.list_style_presets(project_id)

    def get_style_preset(self, style_preset_id: str) -> StylePreset | None:
        return self.brand_styles.get_style_preset(style_preset_id)

    def update_style_preset(
        self,
        style_preset_id: str,
        payload: StylePresetUpdate,
    ) -> StylePreset | None:
        style_preset = self.brand_styles.update_style_preset(style_preset_id, payload)
        if style_preset is not None:
            self.session.commit()
        return style_preset

    def set_project_default_style_preset(
        self,
        project_id: str,
        style_preset_id: str,
    ) -> StylePreset | None:
        style_preset = self.brand_styles.set_project_default_style_preset(project_id, style_preset_id)
        if style_preset is not None:
            self.session.commit()
        return style_preset

    def parse_script_screening_room(
        self,
        payload: ScriptScreeningParseRequest,
    ) -> ScriptScreeningParseResult:
        parse_payload = self.script_screening_parser.parse(
            raw_script=payload.raw_script,
            title=payload.title,
            brand_name=payload.brand_name,
            product_name=payload.product_name,
            target_platform=payload.target_platform,
            target_duration_seconds=payload.target_duration_seconds,
            aspect_ratio=payload.aspect_ratio,
            default_language=payload.default_language,
            style_key=payload.style_key,
            parser_options=payload.parser_options,
        )
        return ScriptScreeningParseResult.model_validate(parse_payload)

    def create_script_screening_room_run(
        self,
        *,
        project_id: str,
        payload: ScriptScreeningRunCreateRequest,
        pipeline: PipelineDefinition,
    ) -> ScriptScreeningRunCreateResult | None:
        project = self.projects.get_project(project_id)
        if project is None:
            return None

        parse_result = self.parse_script_screening_room(payload)
        run_input = parse_result.run_input
        run_payload = RunCreate(
            pipeline_key=payload.pipeline_key,
            raw_script=str(run_input.get("raw_script") or payload.raw_script),
            brand_name=payload.brand_name or run_input.get("brand_name"),
            product_name=payload.product_name or run_input.get("product_name"),
            target_platform=str(run_input.get("target_platform") or "douyin"),
            target_duration_seconds=int(run_input.get("target_duration_seconds") or 15),
            aspect_ratio=payload.aspect_ratio,
            style_key=payload.style_key,
            cost_budget=payload.cost_budget,
        )
        run = self.create_run(project_id=project_id, payload=run_payload, pipeline=pipeline)
        record = self._get_run_record(run.run_id)
        if record is None:
            return None

        script_stage = self._stage_record(record, "script")
        storyboard_stage = self._stage_record(record, "storyboard")
        scene_stage_key = storyboard_stage.stage_key if storyboard_stage else "storyboard"
        scene_stage_run_id = storyboard_stage.id if storyboard_stage else None
        script_stage_key = script_stage.stage_key if script_stage else "script"
        script_stage_run_id = script_stage.id if script_stage else None

        scene_plan_artifact = self.artifacts.append_artifact(
            run_id=run.run_id,
            stage_run_id=scene_stage_run_id,
            stage_key=scene_stage_key,
            artifact_type="screening_room_scene_plan",
            payload=parse_result.scene_plan,
            producer="script_screening_room_parser",
            lineage={
                "operation": "script_screening_room_parse",
                "source": "raw_storyboard_script",
            },
        )
        spoken_script_artifact = self.artifacts.append_artifact(
            run_id=run.run_id,
            stage_run_id=script_stage_run_id,
            stage_key=script_stage_key,
            artifact_type="spoken_script",
            payload=parse_result.spoken_script,
            producer="script_screening_room_parser",
            lineage={"operation": "script_screening_room_spoken_script"},
            derived_from=[
                {
                    "artifact_id": scene_plan_artifact.artifact_id,
                    "artifact_type": scene_plan_artifact.artifact_type,
                    "version": scene_plan_artifact.version,
                }
            ],
        )
        shot_list_artifact = self.artifacts.append_artifact(
            run_id=run.run_id,
            stage_run_id=scene_stage_run_id,
            stage_key=scene_stage_key,
            artifact_type="shot_list",
            payload=parse_result.shot_list,
            producer="script_screening_room_parser",
            lineage={"operation": "script_screening_room_shot_list"},
            derived_from=[
                {
                    "artifact_id": scene_plan_artifact.artifact_id,
                    "artifact_type": scene_plan_artifact.artifact_type,
                    "version": scene_plan_artifact.version,
                },
                {
                    "artifact_id": spoken_script_artifact.artifact_id,
                    "artifact_type": spoken_script_artifact.artifact_type,
                    "version": spoken_script_artifact.version,
                },
            ],
        )
        self.events.append_event(
            run_id=run.run_id,
            event_type="screening_room.run_created",
            source_type=EventSourceType.SYSTEM,
            source_id=scene_plan_artifact.artifact_id,
            stage_key=scene_stage_key,
            stage_run_id=scene_stage_run_id,
            message="Script screening room run created.",
            payload={
                "scene_plan_artifact_id": scene_plan_artifact.artifact_id,
                "spoken_script_artifact_id": spoken_script_artifact.artifact_id,
                "shot_list_artifact_id": shot_list_artifact.artifact_id,
                "shot_count": len(parse_result.scene_plan.get("shots", [])),
            },
        )
        self.session.commit()
        return ScriptScreeningRunCreateResult(
            project=project,
            run=self.runs.get_run(run.run_id) or run,
            scene_plan_artifact=scene_plan_artifact,
            spoken_script_artifact=spoken_script_artifact,
            shot_list_artifact=shot_list_artifact,
            parse_result=parse_result,
        )

    def get_run(self, run_id: str) -> ProductionRun | None:
        return self.runs.get_run(run_id)

    def start_run(
        self,
        run_id: str,
        *,
        accept_intent_downgrade: bool = False,
    ) -> ProductionRun | None:
        if self.runs.get_run(run_id) is None:
            return None
        execution_plan_artifact = self.plan_execution(run_id)
        if execution_plan_artifact is None:
            return None
        proposal_packet_artifact = self.build_proposal_packet(run_id)
        stage_execution_artifact = self.plan_stage_execution(
            run_id,
            execution_plan_artifact=execution_plan_artifact,
        )
        if not execution_plan_artifact.payload["can_execute"]:
            record = self._get_run_record(run_id)
            if record is None:
                return None
            self.events.append_event(
                run_id=record.id,
                event_type="run.start_blocked",
                source_type=EventSourceType.RUN,
                source_id=record.id,
                stage_key="preflight",
                severity=EventSeverity.WARNING,
                message="Run start blocked by execution plan.",
                payload={
                    "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                    "proposal_packet_artifact_id": (
                        proposal_packet_artifact.artifact_id
                        if proposal_packet_artifact is not None
                        else None
                    ),
                    "stage_execution_manifest_artifact_id": (
                        stage_execution_artifact.artifact_id
                        if stage_execution_artifact is not None
                        else None
                    ),
                    "execution_policy": execution_plan_artifact.payload["execution_policy"],
                    "blocking_issues": execution_plan_artifact.payload["blocking_issues"],
                },
            )
            self.session.commit()
            raise ValueError("execution plan is blocked; resolve blocking issues before starting run")

        self._assert_intent_downgrade_acknowledged(
            run_id,
            execution_plan_artifact=execution_plan_artifact,
            accepted=accept_intent_downgrade,
        )

        self._assert_cost_reservation_allows(
            run_id,
            execution_plan_artifact=execution_plan_artifact,
        )

        record = self._get_run_record(run_id)
        if record is None:
            return None
        self._assert_budget_allows(
            run_id,
            estimated_cost=0.0,
            operation="start_run",
        )
        record.status = RunStatus.RUNNING.value
        self._append_run_event(
            record,
            "run.started",
            message="Run started.",
            payload={
                "status": record.status,
                "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                "proposal_packet_artifact_id": (
                    proposal_packet_artifact.artifact_id
                    if proposal_packet_artifact is not None
                    else None
                ),
                "stage_execution_manifest_artifact_id": (
                    stage_execution_artifact.artifact_id
                    if stage_execution_artifact is not None
                    else None
                ),
                "execution_policy": execution_plan_artifact.payload["execution_policy"],
            },
        )
        self._advance_until_gate(record)
        self.session.commit()
        return self.runs.get_run(run_id)

    def review_stage(
        self,
        run_id: str,
        stage_key: str,
        payload: ApprovalRequest,
    ) -> ProductionRun | None:
        """One-click stage review entry point.

        Kept behaviour-compatible with the pre-T74 endpoint, but every approval
        now goes through the review kernel so the decision, the reviewed artifact
        versions and the actor are recorded instead of only mutating stage state.
        """
        record = self._get_run_record(run_id)
        if record is None:
            return None
        ordered_stages = self._ordered_stage_records(record)
        stage = next(
            (item for item in ordered_stages if item.stage_key == stage_key),
            None,
        )
        if stage is None:
            return None
        decision = _approval_request_decision(payload.decision)
        if decision is None:
            return None

        request = self.reviews.find_open_request(stage_run_id=stage.id)
        if request is None:
            request = self._open_review_request_for_stage(record, stage_key=stage_key)
        self.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=decision, comment=payload.comment),
        )
        return self.runs.get_run(run_id)

    # ------------------------------------------------------------------
    # Review system (T74 / V6.3)
    #
    # The gate itself is policy data declared in the pipeline DSL
    # (`StageReviewPolicy`). This layer adds the record of who decided what
    # against which artifact version, plus the invalidation rules that stop a
    # stale approval from releasing a new version.
    # ------------------------------------------------------------------

    DEFAULT_REVIEW_ROUND_LIMIT = 5

    def list_review_requests(
        self,
        run_id: str,
        *,
        status: ReviewRequestStatus | None = None,
        stage_key: str | None = None,
    ) -> list[ReviewRequest] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.reviews.list_requests(run_id, status=status, stage_key=stage_key)

    def get_review_request(self, review_request_id: str) -> ReviewRequestDetail | None:
        return self.reviews.get_request_detail(
            review_request_id,
            workspace_url=f"/workspace?review_request_id={review_request_id}",
            run_board_url=f"/run-board?review_request_id={review_request_id}",
            attachments=self._review_attachment_models(review_request_id),
        )

    def _review_attachment_models(self, review_request_id: str) -> list[ReviewAttachment]:
        """Records joined with their asset snapshots, in stable mount order."""
        models: list[ReviewAttachment] = []
        for record in self.reviews.list_attachments(review_request_id):
            models.append(
                review_attachment_from_record(
                    record,
                    asset=self.assets.get_asset(record.asset_id),
                )
            )
        return models

    def open_review_request(
        self,
        run_id: str,
        payload: ReviewRequestCreate,
    ) -> ReviewRequestDetail | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None
        request = self._open_review_request_for_stage(
            record,
            stage_key=payload.stage_key,
            assignee_user_id=payload.assignee_user_id,
            due_at=payload.due_at,
            artifact_refs=payload.artifact_refs,
            override_round_limit=payload.override_round_limit,
            note=payload.note,
            explicit=True,
        )
        self.session.commit()
        return self.get_review_request(request.review_request_id)

    def submit_review_decision(
        self,
        review_request_id: str,
        payload: SubmitReviewDecision,
    ) -> ReviewRequestDetail:
        request_record = self.reviews.get_request_record(review_request_id)
        if request_record is None:
            raise ReviewGateError(
                "review_request_not_found",
                f"review request not found: {review_request_id}",
                status_code=404,
            )
        record = self._get_run_record(request_record.run_id)
        if record is None:
            raise ReviewGateError(
                "run_not_found",
                f"run not found for review request: {review_request_id}",
                status_code=404,
            )
        ordered_stages = self._ordered_stage_records(record)
        stage = next(
            (item for item in ordered_stages if item.id == request_record.stage_run_id),
            None,
        )
        if stage is None:
            raise ReviewGateError(
                "stage_not_found",
                "the stage execution behind this review request no longer exists",
                status_code=404,
            )

        if payload.decision == ReviewDecisionType.REOPEN:
            self._reopen_review_request(
                record,
                request_record=request_record,
                stage=stage,
                payload=payload,
            )
            self.session.commit()
            return self.get_review_request(review_request_id)  # type: ignore[return-value]

        if request_record.status != ReviewRequestStatus.OPEN.value:
            superseded = request_record.status == ReviewRequestStatus.SUPERSEDED.value
            raise ReviewGateError(
                "review_request_superseded" if superseded else "review_request_closed",
                (
                    "this review request was superseded by an upstream change"
                    if superseded
                    else f"review request is {request_record.status}; reopen it before deciding again"
                ),
                details={"review_status": request_record.status},
            )
        if stage.status != StageStatus.WAITING_REVIEW.value:
            raise ReviewGateError(
                "stage_not_awaiting_review",
                f"stage {stage.stage_key} is {stage.status}, not waiting_review",
                details={"stage_key": stage.stage_key, "stage_status": stage.status},
            )

        current_scope = self._artifact_scope_for_stage(record.id, stage.stage_key)
        if not self._scope_matches_snapshot(request_record.artifact_scope, current_scope):
            raise ReviewGateError(
                "stale_artifact_scope",
                "the artifacts under review changed; open a new review request",
                details={"current_artifact_scope": current_scope},
            )
        requested_refs = self._normalize_artifact_refs(payload.artifact_refs)
        if requested_refs and not self._scope_refs_subset(requested_refs, current_scope):
            raise ReviewGateError(
                "invalid_artifact_scope",
                "artifact_refs must reference current artifacts of the reviewed stage",
                details={"current_artifact_scope": current_scope},
            )

        decision_row = self.reviews.append_decision(
            review_request_id=review_request_id,
            run_id=record.id,
            round_number=request_record.iteration,
            decision=payload.decision,
            actor_user_id=payload.actor_user_id,
            actor_display_name=payload.actor_display_name,
            comment=payload.comment,
            reason_code=payload.reason_code.value if payload.reason_code else None,
            artifact_refs=requested_refs or list(request_record.artifact_scope or []),
        )
        action_items = self._create_review_action_items(
            record,
            review_request=review_request_from_record(request_record),
            decision_id=decision_row.decision_id,
            payload=payload,
        )

        if payload.decision in {
            ReviewDecisionType.APPROVE,
            ReviewDecisionType.APPROVE_WITH_CHANGES,
        }:
            self._apply_review_approval(
                record,
                request_record=request_record,
                stage=stage,
                decision_row=decision_row,
                payload=payload,
                action_items=action_items,
            )
        elif payload.decision == ReviewDecisionType.REQUEST_CHANGES:
            self._apply_review_request_changes(
                record,
                request_record=request_record,
                stage=stage,
                decision_row=decision_row,
                payload=payload,
                action_items=action_items,
            )
        elif payload.decision == ReviewDecisionType.REJECT:
            self._apply_review_rejection(
                record,
                request_record=request_record,
                stage=stage,
                decision_row=decision_row,
                payload=payload,
                action_items=action_items,
            )
        elif payload.decision == ReviewDecisionType.CANCEL:
            self._apply_review_cancellation(
                record,
                request_record=request_record,
                stage=stage,
                decision_row=decision_row,
                payload=payload,
            )

        if payload.decision != ReviewDecisionType.CANCEL:
            self.reviews.set_request_status(
                review_request_id,
                status=self._request_status_for_decision(payload.decision),
                current_decision=payload.decision,
                closed=True,
            )
        self.session.commit()
        return self.get_review_request(review_request_id)  # type: ignore[return-value]

    def add_review_comment(
        self,
        review_request_id: str,
        payload: CreateReviewComment,
    ) -> ReviewComment | None:
        request_record = self.reviews.get_request_record(review_request_id)
        if request_record is None:
            return None
        if payload.severity == ReviewCommentSeverity.CRITICAL and request_record.status != (
            ReviewRequestStatus.OPEN.value
        ):
            raise ReviewGateError(
                "review_request_closed",
                "cannot anchor a critical finding to a closed review request",
                details={"review_status": request_record.status},
            )
        self._assert_comment_artifact_ref(
            review_request_id,
            artifact_id=payload.artifact_id,
            artifact_version=payload.artifact_version,
        )
        comment = self.reviews.create_comment(
            review_request_id=review_request_id,
            run_id=request_record.run_id,
            stage_key=request_record.stage_key,
            body=payload.body,
            severity=payload.severity,
            artifact_id=payload.artifact_id,
            artifact_version=payload.artifact_version,
            author_user_id=payload.author_user_id,
            author_display_name=payload.author_display_name,
        )
        self.events.append_event(
            run_id=request_record.run_id,
            event_type="review.comment_added",
            source_type=EventSourceType.REVIEW,
            source_id=review_request_id,
            stage_key=request_record.stage_key,
            stage_run_id=request_record.stage_run_id,
            message="Review comment added.",
            payload={
                "comment_id": comment.comment_id,
                "severity": comment.severity.value,
                "artifact_id": comment.artifact_id,
                "artifact_version": comment.artifact_version,
            },
        )
        self.session.commit()
        return comment

    def add_review_attachment(
        self,
        review_request_id: str,
        payload: CreateReviewAttachment,
    ) -> tuple[ReviewAttachmentOutcome, ReviewAttachment | None]:
        request_record = self.reviews.get_request_record(review_request_id)
        if request_record is None:
            return ReviewAttachmentOutcome.REQUEST_NOT_FOUND, None
        asset = self.assets.get_asset(payload.asset_id)
        if asset is None:
            return ReviewAttachmentOutcome.ASSET_NOT_FOUND, None
        # The asset must live in the same project as the review request. A
        # foreign asset is reported as plain not-found rather than forbidden:
        # a 403 would confirm the asset exists somewhere else.
        asset_run = self.runs.get_run(asset.run_id)
        if asset_run is None or asset_run.project_id != request_record.project_id:
            return ReviewAttachmentOutcome.ASSET_NOT_FOUND, None
        record, created = self.reviews.mount_attachment(
            review_request_id=review_request_id,
            run_id=request_record.run_id,
            asset_id=payload.asset_id,
            note=payload.note,
            added_by_user_id=payload.added_by_user_id,
            added_by_display_name=payload.added_by_display_name,
        )
        if created:
            self.events.append_event(
                run_id=request_record.run_id,
                event_type="review.attachment_added",
                source_type=EventSourceType.REVIEW,
                source_id=review_request_id,
                stage_key=request_record.stage_key,
                stage_run_id=request_record.stage_run_id,
                message="Review attachment mounted.",
                payload={
                    "attachment_id": record.id,
                    "asset_id": payload.asset_id,
                    "note": payload.note,
                },
            )
        self.session.commit()
        return (
            ReviewAttachmentOutcome.CREATED if created else ReviewAttachmentOutcome.EXISTING,
            self._attachment_model(record),
        )

    def list_review_attachments(self, review_request_id: str) -> list[ReviewAttachment] | None:
        """Attachments of one request; ``None`` when the request does not exist."""
        if self.reviews.get_request_record(review_request_id) is None:
            return None
        return self._review_attachment_models(review_request_id)

    def remove_review_attachment(self, attachment_id: str) -> ReviewAttachment | None:
        record = self.reviews.get_attachment_record(attachment_id)
        if record is None:
            return None
        model = self._attachment_model(record)
        self.reviews.remove_attachment(attachment_id)
        self.events.append_event(
            run_id=record.run_id,
            event_type="review.attachment_removed",
            source_type=EventSourceType.REVIEW,
            source_id=record.review_request_id,
            message="Review attachment removed.",
            payload={"attachment_id": attachment_id, "asset_id": record.asset_id},
        )
        self.session.commit()
        return model

    def _attachment_model(self, record: Any) -> ReviewAttachment:
        return review_attachment_from_record(
            record,
            asset=self.assets.get_asset(record.asset_id),
        )

    def update_review_comment(
        self,
        comment_id: str,
        *,
        status: ReviewCommentStatus,
    ) -> ReviewComment | None:
        record = self.reviews.get_comment_record(comment_id)
        if record is None:
            return None
        comment = self.reviews.set_comment_status(comment_id, status=status)
        self.events.append_event(
            run_id=record.run_id,
            event_type="review.comment_updated",
            source_type=EventSourceType.REVIEW,
            source_id=record.review_request_id,
            stage_key=record.stage_key,
            message="Review comment status updated.",
            payload={"comment_id": comment_id, "status": status.value},
        )
        self.session.commit()
        return comment

    def assign_review_request(
        self,
        review_request_id: str,
        *,
        assignee_user_id: str | None,
        due_at: datetime | None,
    ) -> ReviewRequest | None:
        record = self.reviews.get_request_record(review_request_id)
        if record is None:
            return None
        result = self.reviews.assign_request(
            review_request_id,
            assignee_user_id=assignee_user_id,
            due_at=due_at,
        )
        self.events.append_event(
            run_id=record.run_id,
            event_type="review.assigned",
            source_type=EventSourceType.REVIEW,
            source_id=review_request_id,
            stage_key=record.stage_key,
            stage_run_id=record.stage_run_id,
            message="Review request assigned.",
            payload={"assignee_user_id": assignee_user_id, "due_at": _isoformat(due_at)},
        )
        self.session.commit()
        return result

    def project_review_inbox(
        self,
        project_id: str,
        *,
        assignee_user_id: str | None = None,
        stage_key: str | None = None,
        status: ReviewRequestStatus | None = None,
    ) -> ReviewInbox | None:
        project = self.projects.get_project(project_id)
        if project is None:
            return None
        requests = self.reviews.list_project_requests(
            project_id,
            status=status,
            assignee_user_id=assignee_user_id,
            stage_key=stage_key,
        )
        if status is None:
            requests = [
                item
                for item in requests
                if item.status != ReviewRequestStatus.SUPERSEDED
            ]
        ordered = sorted(
            requests,
            key=lambda item: (
                not item.overdue,
                item.due_at is None,
                _sort_datetime(item.opened_at),
                item.review_request_id,
            ),
        )
        items = [
            ReviewInboxItem(
                review_request_id=item.review_request_id,
                project_id=item.project_id,
                run_id=item.run_id,
                stage_run_id=item.stage_run_id,
                stage_key=item.stage_key,
                pipeline_key=item.pipeline_key,
                iteration=item.iteration,
                status=item.status,
                current_decision=item.current_decision,
                assignee_user_id=item.assignee_user_id,
                due_at=item.due_at,
                overdue=item.overdue,
                opened_at=item.opened_at,
                open_critical_comments=self.reviews.count_open_comments_by_severity(
                    item.review_request_id,
                    severity=ReviewCommentSeverity.CRITICAL,
                ),
                open_action_items=self.reviews.count_open_action_items(item.review_request_id),
                workspace_url=f"/workspace?review_request_id={item.review_request_id}",
            )
            for item in ordered
        ]
        status_counts = Counter(item.status.value for item in requests)
        return ReviewInbox(
            project_id=project_id,
            assignee_user_id=assignee_user_id,
            total=len(items),
            overdue_total=sum(1 for item in items if item.overdue),
            status_counts=dict(status_counts),
            items=items,
        )

    # --- review kernel internals -------------------------------------

    def _review_policy_for_stage(
        self,
        pipeline: PipelineDefinition,
        stage_definition: StageDefinition,
    ) -> tuple[str, bool, list[str], list[str]]:
        policy = stage_definition.review_policy
        gate_type = policy.gate_type if policy is not None else "human_review"
        required = bool(
            stage_definition.requires_human_review or (policy is not None and policy.required)
        )
        focus = list(policy.review_focus) if policy is not None else []
        criteria = list(policy.success_criteria) if policy is not None else []
        if not criteria:
            criteria = list(stage_definition.success_criteria)
        return gate_type, required, focus, criteria

    def _next_stage_key(
        self,
        pipeline: PipelineDefinition,
        stage_key: str,
    ) -> str | None:
        ordered = pipeline.ordered_stages()
        for index, stage in enumerate(ordered):
            if stage.stage_key == stage_key and index + 1 < len(ordered):
                return ordered[index + 1].stage_key
        return None

    def _artifact_scope_for_stage(self, run_id: str, stage_key: str) -> list[dict]:
        return [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "version": artifact.version,
            }
            for artifact in self.artifacts.list_artifacts(run_id, current_only=True)
            if artifact.stage_key == stage_key
        ]

    @staticmethod
    def _scope_matches_snapshot(snapshot: list[dict] | None, current: list[dict]) -> bool:
        def keyed(items: list[dict] | None) -> dict[str, int]:
            return {
                str(item.get("artifact_id")): int(item.get("version") or 0)
                for item in (items or [])
            }

        return keyed(snapshot) == keyed(current)

    @staticmethod
    def _normalize_artifact_refs(
        refs: list[ReviewArtifactRef] | None,
    ) -> list[dict]:
        return [
            {
                "artifact_id": ref.artifact_id,
                "artifact_type": ref.artifact_type,
                "version": ref.version,
            }
            for ref in (refs or [])
        ]

    @staticmethod
    def _scope_refs_subset(refs: list[dict], current: list[dict]) -> bool:
        available = {
            (str(item.get("artifact_id")), int(item.get("version") or 0)) for item in current
        }
        return all(
            (str(item.get("artifact_id")), int(item.get("version") or 0)) in available
            for item in refs
        )

    def _review_round_limit(
        self,
        pipeline: PipelineDefinition,
        stage_definition: StageDefinition,
    ) -> int:
        policy = pipeline.metadata.get("review") if isinstance(pipeline.metadata, dict) else None
        configured = policy.get("max_rounds") if isinstance(policy, dict) else None
        if isinstance(configured, int) and configured > 0:
            return configured
        stage_configured = None
        if stage_definition.review_policy is not None:
            stage_configured = getattr(stage_definition.review_policy, "max_rounds", None)
        if isinstance(stage_configured, int) and stage_configured > 0:
            return stage_configured
        return self.DEFAULT_REVIEW_ROUND_LIMIT

    def _open_review_request_for_stage(
        self,
        record: ProductionRunRecord,
        *,
        stage_key: str,
        assignee_user_id: str | None = None,
        due_at: datetime | None = None,
        artifact_refs: list[ReviewArtifactRef] | None = None,
        override_round_limit: bool = False,
        note: str | None = None,
        explicit: bool = False,
    ) -> ReviewRequest:
        pipeline = self._pipeline_for_run_record(record)
        stage_definitions = {stage.stage_key: stage for stage in pipeline.stages}
        stage_definition = stage_definitions.get(stage_key)
        if stage_definition is None:
            raise ReviewGateError(
                "stage_not_found",
                f"stage not found in pipeline: {stage_key}",
                status_code=404,
                details={"stage_key": stage_key},
            )
        ordered_stages = self._ordered_stage_records(record)
        stage = next((item for item in ordered_stages if item.stage_key == stage_key), None)
        if stage is None:
            raise ReviewGateError(
                "stage_run_not_found",
                f"stage execution not found: {stage_key}",
                status_code=404,
                details={"stage_key": stage_key},
            )

        existing = self.reviews.find_open_request(stage_run_id=stage.id)
        if existing is not None:
            return existing

        if explicit and stage.status != StageStatus.WAITING_REVIEW.value:
            raise ReviewGateError(
                "stage_not_awaiting_review",
                f"stage {stage_key} is {stage.status}; only a stage awaiting review can be opened",
                details={"stage_key": stage_key, "stage_status": stage.status},
            )

        iteration = self.reviews.next_iteration(run_id=record.id, stage_key=stage_key)
        round_limit = self._review_round_limit(pipeline, stage_definition)
        if iteration > round_limit and not override_round_limit:
            raise ReviewGateError(
                "review_round_limit_reached",
                f"stage {stage_key} already went through {iteration - 1} review rounds",
                details={
                    "stage_key": stage_key,
                    "round_limit": round_limit,
                    "next_iteration": iteration,
                    "allow_override": True,
                },
            )

        current_scope = self._artifact_scope_for_stage(record.id, stage_key)
        requested_refs = self._normalize_artifact_refs(artifact_refs)
        if requested_refs:
            if not self._scope_refs_subset(requested_refs, current_scope):
                raise ReviewGateError(
                    "invalid_artifact_scope",
                    "artifact_refs must reference current artifacts of the reviewed stage",
                    details={"current_artifact_scope": current_scope},
                )
            scope = requested_refs
        else:
            scope = current_scope

        gate_type, _required, focus, criteria = self._review_policy_for_stage(
            pipeline,
            stage_definition,
        )
        request = self.reviews.create_request(
            run_id=record.id,
            project_id=record.project_id,
            stage_run_id=stage.id,
            stage_key=stage_key,
            pipeline_key=record.pipeline_key,
            iteration=iteration,
            gate_type=gate_type,
            gates_stage_key=self._next_stage_key(pipeline, stage_key),
            artifact_scope=scope,
            review_focus=focus,
            success_criteria=criteria,
            assignee_user_id=assignee_user_id,
            due_at=due_at,
        )
        self.events.append_event(
            run_id=record.id,
            event_type="review.requested",
            source_type=EventSourceType.REVIEW,
            source_id=request.review_request_id,
            stage_key=stage_key,
            stage_run_id=stage.id,
            message="Review requested for stage output.",
            payload={
                "review_request_id": request.review_request_id,
                "iteration": iteration,
                "gate_type": gate_type,
                "gates_stage_key": request.gates_stage_key,
                "artifact_scope": scope,
                "review_focus": focus,
                "success_criteria": criteria,
                "explicit": explicit,
                "note": note,
                "round_limit": round_limit,
            },
        )
        return request

    def _assert_comment_artifact_ref(
        self,
        review_request_id: str,
        *,
        artifact_id: str | None,
        artifact_version: int | None,
    ) -> None:
        request_record = self.reviews.get_request_record(review_request_id)
        if request_record is None or artifact_id is None:
            return
        scope = {
            str(item.get("artifact_id")): int(item.get("version") or 0)
            for item in (request_record.artifact_scope or [])
        }
        if artifact_id not in scope:
            raise ReviewGateError(
                "invalid_artifact_scope",
                "the comment must anchor to an artifact under review",
                details={"artifact_id": artifact_id},
            )
        if artifact_version is not None and scope[artifact_id] != artifact_version:
            raise ReviewGateError(
                "stale_artifact_scope",
                "the comment targets a version that is no longer under review",
                details={
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                    "current_version": scope[artifact_id],
                },
            )

    @staticmethod
    def _request_status_for_decision(decision: ReviewDecisionType) -> ReviewRequestStatus:
        if decision in {ReviewDecisionType.APPROVE, ReviewDecisionType.APPROVE_WITH_CHANGES}:
            return ReviewRequestStatus.APPROVED
        if decision == ReviewDecisionType.REQUEST_CHANGES:
            return ReviewRequestStatus.CHANGES_REQUESTED
        if decision == ReviewDecisionType.REJECT:
            return ReviewRequestStatus.REJECTED
        if decision == ReviewDecisionType.CANCEL:
            return ReviewRequestStatus.CANCELLED
        return ReviewRequestStatus.OPEN

    def _create_review_action_items(
        self,
        record: ProductionRunRecord,
        *,
        review_request: ReviewRequest,
        decision_id: str,
        payload: SubmitReviewDecision,
    ) -> list[ReviewActionItem]:
        items: list[ReviewActionItem] = []
        for item in payload.action_items:
            items.append(
                self.reviews.create_action_item(
                    review_request_id=review_request.review_request_id,
                    decision_id=decision_id,
                    run_id=record.id,
                    kind=item.kind,
                    instruction=item.instruction,
                    stage_key=item.stage_key or review_request.stage_key,
                    artifact_type=item.artifact_type,
                    stage_run_id=review_request.stage_run_id,
                    linked_comment_ids=item.linked_comment_ids,
                    result_action=action_item_result_action(item.kind),
                )
            )
        if payload.action_items:
            self._record_review_action_items_event(record, review_request, items)
            return items

        if payload.decision not in {
            ReviewDecisionType.REQUEST_CHANGES,
            ReviewDecisionType.REJECT,
            ReviewDecisionType.APPROVE_WITH_CHANGES,
        }:
            return items

        for comment in self.reviews.list_comments(
            review_request.review_request_id,
            status=ReviewCommentStatus.OPEN,
        ):
            if comment.severity != ReviewCommentSeverity.CRITICAL:
                continue
            kind = infer_action_kind(review_request.stage_key)
            items.append(
                self.reviews.create_action_item(
                    review_request_id=review_request.review_request_id,
                    decision_id=decision_id,
                    run_id=record.id,
                    kind=kind,
                    instruction=comment.body,
                    stage_key=review_request.stage_key,
                    stage_run_id=review_request.stage_run_id,
                    linked_comment_ids=[comment.comment_id],
                    result_action=action_item_result_action(kind),
                )
            )
        if items:
            self._record_review_action_items_event(record, review_request, items)
        return items

    def _record_review_action_items_event(
        self,
        record: ProductionRunRecord,
        review_request: ReviewRequest,
        items: list[ReviewActionItem],
    ) -> None:
        if not items:
            return
        self.events.append_event(
            run_id=record.id,
            event_type="review.action_item.created",
            source_type=EventSourceType.REVIEW,
            source_id=review_request.review_request_id,
            stage_key=review_request.stage_key,
            stage_run_id=review_request.stage_run_id,
            message="Review request produced change actions.",
            payload={
                "review_request_id": review_request.review_request_id,
                "action_item_ids": [item.action_item_id for item in items],
                "kinds": [item.kind.value for item in items],
            },
        )

    def _record_review_decision_event(
        self,
        record: ProductionRunRecord,
        review_request: ReviewRequest,
        decision_row: ReviewDecision,
        *,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        self.events.append_event(
            run_id=record.id,
            event_type=f"review.decision.{decision_row.decision.value}",
            source_type=EventSourceType.REVIEW,
            source_id=review_request.review_request_id,
            stage_key=review_request.stage_key,
            stage_run_id=review_request.stage_run_id,
            severity=severity,
            message=f"Review decision: {decision_row.decision.value}.",
            payload={
                "review_request_id": review_request.review_request_id,
                "decision_id": decision_row.decision_id,
                "decision": decision_row.decision.value,
                "iteration": decision_row.round,
                "actor_user_id": decision_row.actor_user_id,
                "actor_display_name": decision_row.actor_display_name,
                "comment": decision_row.comment,
                "reason_code": decision_row.reason_code,
                "artifact_refs": _artifact_refs_payload(decision_row.artifact_refs),
            },
        )

    def _resolve_comments_for_decision(
        self,
        review_request: ReviewRequest,
        decision_row: ReviewDecision,
    ) -> None:
        open_comments = self.reviews.list_comments(
            review_request.review_request_id,
            status=ReviewCommentStatus.OPEN,
        )
        if not open_comments:
            return
        self.reviews.resolve_comments(
            [comment.comment_id for comment in open_comments],
            resolved_by_decision_id=decision_row.decision_id,
        )

    def _apply_review_approval(
        self,
        record: ProductionRunRecord,
        *,
        request_record,
        stage: StageRunRecord,
        decision_row: ReviewDecision,
        payload: SubmitReviewDecision,
        action_items: list[ReviewActionItem],
    ) -> None:
        review_request = review_request_from_record(request_record)
        stage.status = StageStatus.APPROVED.value
        self._append_stage_event(
            record,
            stage,
            f"stage.review.{payload.decision.value}",
            message="Stage review approved.",
            payload={
                "decision": payload.decision.value,
                "comment": payload.comment,
                "review_request_id": review_request.review_request_id,
                "artifact_refs": _artifact_refs_payload(decision_row.artifact_refs),
                "open_action_items": [item.action_item_id for item in action_items],
            },
        )
        self._record_review_decision_event(record, review_request, decision_row)
        if payload.decision == ReviewDecisionType.APPROVE:
            self._resolve_comments_for_decision(review_request, decision_row)

        ordered_stages = self._ordered_stage_records(record)
        index = self._stage_index(ordered_stages, stage.stage_key)
        if index + 1 < len(ordered_stages):
            next_stage = ordered_stages[index + 1]
            record.current_stage_key = next_stage.stage_key
            record.status = RunStatus.RUNNING.value
            self._advance_until_gate(record)
            return
        record.status = RunStatus.COMPLETED.value
        record.current_stage_key = None
        self._append_run_event(
            record,
            "run.completed",
            message="Run completed after final approval.",
            payload={"status": record.status},
        )

    def _apply_review_request_changes(
        self,
        record: ProductionRunRecord,
        *,
        request_record,
        stage: StageRunRecord,
        decision_row: ReviewDecision,
        payload: SubmitReviewDecision,
        action_items: list[ReviewActionItem],
    ) -> None:
        review_request = review_request_from_record(request_record)
        stage.status = StageStatus.PENDING.value
        stage.attempt = (stage.attempt or 1) + 1
        record.status = RunStatus.PAUSED.value
        record.current_stage_key = stage.stage_key
        invalidated = self.invalidate_downstream_stages(
            record,
            source_stage_key=stage.stage_key,
            reason=(
                f"Stage {stage.stage_key} was sent back for changes; "
                "downstream outputs must be regenerated."
            ),
            cause="review_request_changes",
        )
        self._append_stage_event(
            record,
            stage,
            "stage.review.request_changes",
            message="Stage review requested changes.",
            payload={
                "decision": payload.decision.value,
                "comment": payload.comment,
                "review_request_id": review_request.review_request_id,
                "artifact_refs": _artifact_refs_payload(decision_row.artifact_refs),
                "action_item_ids": [item.action_item_id for item in action_items],
                "invalidated_stage_keys": invalidated,
            },
        )
        self._record_review_decision_event(
            record,
            review_request,
            decision_row,
            severity=EventSeverity.WARNING,
        )
        self._append_run_event(
            record,
            "run.paused",
            message="Run paused for requested changes.",
            payload={"status": record.status, "stage_key": stage.stage_key},
        )

    def _apply_review_rejection(
        self,
        record: ProductionRunRecord,
        *,
        request_record,
        stage: StageRunRecord,
        decision_row: ReviewDecision,
        payload: SubmitReviewDecision,
        action_items: list[ReviewActionItem],
    ) -> None:
        review_request = review_request_from_record(request_record)
        stage.status = StageStatus.REJECTED.value
        record.status = RunStatus.PAUSED.value
        record.current_stage_key = stage.stage_key
        invalidated = self.invalidate_downstream_stages(
            record,
            source_stage_key=stage.stage_key,
            reason=(
                f"Stage {stage.stage_key} was rejected; downstream outputs derived "
                "from it are no longer valid."
            ),
            cause="review_rejected",
        )
        self._append_stage_event(
            record,
            stage,
            "stage.review.rejected",
            message="Stage review rejected.",
            payload={
                "decision": payload.decision.value,
                "comment": payload.comment,
                "review_request_id": review_request.review_request_id,
                "artifact_refs": _artifact_refs_payload(decision_row.artifact_refs),
                "action_item_ids": [item.action_item_id for item in action_items],
                "invalidated_stage_keys": invalidated,
            },
        )
        self._record_review_decision_event(
            record,
            review_request,
            decision_row,
            severity=EventSeverity.WARNING,
        )
        self._append_run_event(
            record,
            "run.paused",
            message="Run paused after stage rejection.",
            payload={"status": record.status, "stage_key": stage.stage_key},
        )

    def _apply_review_cancellation(
        self,
        record: ProductionRunRecord,
        *,
        request_record,
        stage: StageRunRecord,
        decision_row: ReviewDecision,
        payload: SubmitReviewDecision,
    ) -> None:
        review_request = review_request_from_record(request_record)
        cancelled_jobs = self._cancel_unfinished_jobs(
            record.id,
            reason=payload.comment or "Run cancelled during stage review.",
        )
        stage.status = StageStatus.CANCELLED.value
        record.status = RunStatus.CANCELLED.value
        self._append_stage_event(
            record,
            stage,
            "stage.review.cancelled",
            message="Stage review cancelled.",
            payload={
                "decision": payload.decision.value,
                "comment": payload.comment,
                "review_request_id": review_request.review_request_id,
                "cancelled_job_ids": cancelled_jobs,
            },
        )
        self._record_review_decision_event(
            record,
            review_request,
            decision_row,
            severity=EventSeverity.WARNING,
        )
        self._append_run_event(
            record,
            "run.cancelled",
            message="Run cancelled during stage review.",
            payload={"status": record.status, "stage_key": stage.stage_key},
        )

    def _reopen_review_request(
        self,
        record: ProductionRunRecord,
        *,
        request_record,
        stage: StageRunRecord,
        payload: SubmitReviewDecision,
    ) -> None:
        review_request = review_request_from_record(request_record)
        if request_record.status == ReviewRequestStatus.SUPERSEDED.value:
            raise ReviewGateError(
                "review_request_superseded",
                "a superseded review request cannot be reopened; open a new one",
            )
        if request_record.status == ReviewRequestStatus.OPEN.value:
            raise ReviewGateError(
                "review_request_already_open",
                "this review request is already open",
            )
        pipeline = self._pipeline_for_run_record(record)
        stage_definitions = {item.stage_key: item for item in pipeline.stages}
        stage_definition = stage_definitions.get(stage.stage_key)
        if stage_definition is None:
            raise ReviewGateError(
                "stage_not_found",
                f"stage not found in pipeline: {stage.stage_key}",
                status_code=404,
            )
        iteration = self.reviews.next_iteration(
            run_id=record.id,
            stage_key=stage.stage_key,
        )
        round_limit = self._review_round_limit(pipeline, stage_definition)
        over_limit = iteration > round_limit
        if over_limit and not payload.override_round_limit:
            raise ReviewGateError(
                "review_round_limit_reached",
                f"stage {stage.stage_key} already went through {iteration - 1} review rounds",
                details={
                    "stage_key": stage.stage_key,
                    "round_limit": round_limit,
                    "next_iteration": iteration,
                    "allow_override": True,
                },
            )
        reason_code = payload.reason_code
        if reason_code is None and over_limit:
            reason_code = ReviewReasonCode.ROUND_LIMIT_OVERRIDE

        decision_row = self.reviews.append_decision(
            review_request_id=review_request.review_request_id,
            run_id=record.id,
            round_number=iteration,
            decision=ReviewDecisionType.REOPEN,
            actor_user_id=payload.actor_user_id,
            actor_display_name=payload.actor_display_name,
            comment=payload.comment,
            reason_code=reason_code.value if reason_code else None,
            artifact_refs=self._artifact_scope_for_stage(record.id, stage.stage_key),
        )
        invalidated = self.invalidate_downstream_stages(
            record,
            source_stage_key=stage.stage_key,
            reason=(
                f"Review gate for stage {stage.stage_key} was reopened; "
                "downstream approvals no longer hold."
            ),
            cause="review_reopened",
        )
        self.reviews.reopen_request(
            review_request.review_request_id,
            iteration=iteration,
            artifact_scope=self._artifact_scope_for_stage(record.id, stage.stage_key),
        )
        stage.status = StageStatus.WAITING_REVIEW.value
        record.status = RunStatus.WAITING_REVIEW.value
        record.current_stage_key = stage.stage_key
        self._append_stage_event(
            record,
            stage,
            "stage.review.reopened",
            message="Stage review reopened.",
            payload={
                "review_request_id": review_request.review_request_id,
                "iteration": iteration,
                "comment": payload.comment,
                "invalidated_stage_keys": invalidated,
            },
        )
        self._record_review_decision_event(
            record,
            review_request,
            decision_row,
            severity=EventSeverity.WARNING,
        )
        self._append_run_event(
            record,
            "run.waiting_review",
            message="Run returned to an earlier review gate.",
            payload={"status": record.status, "stage_key": stage.stage_key},
        )

    def invalidate_downstream_stages(
        self,
        record: ProductionRunRecord,
        *,
        source_stage_key: str,
        reason: str,
        cause: str,
    ) -> list[str]:
        """Mark every downstream stage whose output was derived from a changed upstream.

        Approval is not a permanent pass: a stage that was approved before the
        upstream change is pushed back to ``pending`` so it has to run and be
        reviewed again.
        """
        ordered_stages = self._ordered_stage_records(record)
        try:
            index = self._stage_index(ordered_stages, source_stage_key)
        except ValueError:
            return []
        stages_with_artifacts = self._stages_with_artifacts(record.id)
        invalidated: list[str] = []
        for stage in ordered_stages[index + 1 :]:
            if stage.stage_key not in stages_with_artifacts:
                continue
            previous_status = stage.status
            stage.invalidated_at = datetime.now(UTC)
            stage.invalidated_by_stage_key = source_stage_key
            stage.invalidation_reason = reason
            # Approval is not a permanent pass: any stage that already finished
            # processing has to be re-derived before it can be trusted again.
            if previous_status not in {
                StageStatus.PENDING.value,
                StageStatus.QUEUED.value,
                StageStatus.RUNNING.value,
            }:
                stage.status = StageStatus.PENDING.value
            invalidated.append(stage.stage_key)
            self._append_stage_event(
                record,
                stage,
                "stage.invalidated",
                message="Stage output invalidated by an upstream change.",
                severity=EventSeverity.WARNING,
                payload={
                    "cause": cause,
                    "source_stage_key": source_stage_key,
                    "previous_status": previous_status,
                    "status": stage.status,
                    "reason": reason,
                },
            )
        closed_requests = self.reviews.supersede_requests_for_stage_run(
            stage_run_ids=[
                stage.id
                for stage in ordered_stages[index + 1 :]
                if stage.stage_key in invalidated
            ]
        )
        if closed_requests:
            self.events.append_event(
                run_id=record.id,
                event_type="review.requests_superseded",
                source_type=EventSourceType.REVIEW,
                source_id=record.id,
                stage_key=source_stage_key,
                message="Open review requests were superseded by an upstream change.",
                payload={
                    "cause": cause,
                    "review_request_ids": closed_requests,
                    "invalidated_stage_keys": invalidated,
                },
            )
        return invalidated

    def _stages_with_artifacts(self, run_id: str) -> set[str]:
        return {
            artifact.stage_key
            for artifact in self.artifacts.list_artifacts(run_id, current_only=False)
        }

    def _open_review_request_guarded(
        self,
        record: ProductionRunRecord,
        stage: StageRunRecord,
    ) -> ReviewRequest | None:
        """Open the gate request for a stage that just reached ``waiting_review``.

        A gate that cannot be opened (for example the review round limit was
        reached) must not silently release the stage: the run stays in
        ``waiting_review`` and a structured blocker is recorded instead.
        """
        try:
            return self._open_review_request_for_stage(record, stage_key=stage.stage_key)
        except ReviewGateError as exc:
            self.events.append_event(
                run_id=record.id,
                event_type="review.request_blocked",
                source_type=EventSourceType.REVIEW,
                source_id=record.id,
                stage_key=stage.stage_key,
                stage_run_id=stage.id,
                severity=EventSeverity.WARNING,
                message="Review gate could not be opened automatically.",
                payload=exc.to_payload(),
            )
            return None

    def _clear_stage_invalidation(
        self,
        record: ProductionRunRecord,
        stage: StageRunRecord,
    ) -> None:
        if stage.invalidated_at is None:
            return
        previous_source = stage.invalidated_by_stage_key
        stage.invalidated_at = None
        stage.invalidated_by_stage_key = None
        stage.invalidation_reason = None
        self._append_stage_event(
            record,
            stage,
            "stage.invalidation_cleared",
            message="Stage re-derived from updated upstream inputs.",
            payload={"previous_source_stage_key": previous_source},
        )

    def pending_review_gate(self, record: ProductionRunRecord) -> ReviewRequest | None:
        """The open review request, if any, that currently blocks the run."""
        for request in self.reviews.list_open_requests_for_run(record.id):
            stage = next(
                (
                    item
                    for item in self._ordered_stage_records(record)
                    if item.id == request.stage_run_id
                ),
                None,
            )
            if stage is not None and stage.status == StageStatus.WAITING_REVIEW.value:
                return request
        return None

    def _invalidated_stage_keys(self, run_id: str) -> list[str]:
        statement = (
            select(StageRunRecord)
            .where(StageRunRecord.run_id == run_id)
            .where(StageRunRecord.invalidated_at.is_not(None))
        )
        return [record.stage_key for record in self.session.scalars(statement)]

    def _invalidation_blockers(self, run_id: str) -> list[dict]:
        statement = (
            select(StageRunRecord)
            .where(StageRunRecord.run_id == run_id)
            .where(StageRunRecord.invalidated_at.is_not(None))
            .order_by(StageRunRecord.stage_order)
        )
        return [
            {
                "code": "stage_invalidated",
                "stage_key": record.stage_key,
                "source_stage_key": record.invalidated_by_stage_key,
                "message": record.invalidation_reason
                or "Stage output was invalidated by an upstream change.",
            }
            for record in self.session.scalars(statement)
        ]

    def run_state_action(
        self,
        run_id: str,
        payload: RunStateActionRequest,
    ) -> RunStateActionResult | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None

        before_status = record.status
        before_stage_key = record.current_stage_key
        assert_run_action_allowed(payload.action.value, before_status)

        if payload.action == RunStateActionName.PAUSE:
            record.status = RunStatus.PAUSED.value
            self._append_run_event(
                record,
                "run.paused",
                message="Run paused by state action.",
                payload=self._state_action_event_payload(payload, before_status, before_stage_key),
            )
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message="Run paused.",
            )

        if payload.action == RunStateActionName.RESUME:
            self._resume_paused_run(record, payload, before_status, before_stage_key)
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message="Run resumed.",
            )

        if payload.action == RunStateActionName.CANCEL:
            cancelled_jobs = self._cancel_unfinished_jobs(
                record.id,
                reason=payload.reason or "Run cancelled by state action.",
            )
            self._cancel_run_record(record, payload, before_status, before_stage_key)
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message="Run cancelled.",
                result_payload={"cancelled_job_ids": cancelled_jobs},
            )

        if payload.action == RunStateActionName.RERUN_STAGE:
            stage_key = self._state_action_stage_key(payload)
            affected, invalidated, superseded = self._reset_run_to_stage(
                record,
                stage_key=stage_key,
                payload=payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
            )
            self._advance_until_gate(record)
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message=f"Stage {stage_key} queued for rerun.",
                result_payload={
                    "affected_stage_keys": affected,
                    "invalidated_stage_keys": invalidated,
                    "superseded_review_request_ids": superseded,
                },
            )

        if payload.action == RunStateActionName.RERUN_ARTIFACT:
            artifact_type = self._state_action_artifact_type(payload)
            artifact = self._current_artifact_by_type(record.id, artifact_type)
            stage_key = artifact.stage_key if artifact else self._stage_key_for_output_artifact(record, artifact_type)
            affected, invalidated, superseded = self._reset_run_to_stage(
                record,
                stage_key=stage_key,
                payload=payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                artifact_type=artifact_type,
                artifact_id=artifact.artifact_id if artifact else None,
            )
            self._advance_until_gate(record)
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message=f"Artifact {artifact_type} queued for rerun.",
                result_payload={
                    "artifact_type": artifact_type,
                    "artifact_id": artifact.artifact_id if artifact else None,
                    "stage_key": stage_key,
                    "affected_stage_keys": affected,
                    "invalidated_stage_keys": invalidated,
                    "superseded_review_request_ids": superseded,
                },
            )

        if payload.action == RunStateActionName.SKIP_OPTIONAL_STAGE:
            stage_key = self._state_action_stage_key(payload, default=record.current_stage_key)
            created_artifacts = self._skip_optional_stage(
                record,
                stage_key=stage_key,
                payload=payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
            )
            self.session.commit()
            return self._state_action_result(
                record,
                payload,
                before_status=before_status,
                before_stage_key=before_stage_key,
                message=f"Stage {stage_key} skipped.",
                result_payload={"created_artifact_ids": created_artifacts},
            )

        raise ValueError(f"unsupported run state action: {payload.action}")

    def list_artifacts(self, run_id: str, *, current_only: bool = False) -> list[Artifact] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.artifacts.list_artifacts(run_id, current_only=current_only)

    def run_preflight(self, run_id: str) -> Artifact | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        report = self._build_preflight_report(run)
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_key="preflight",
            artifact_type="preflight_report",
            payload=report,
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.preflight_completed",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_key="preflight",
            message="Pipeline preflight completed.",
            payload={
                "artifact_id": artifact.artifact_id,
                "overall_status": report["overall_status"],
                "blocking_issue_count": len(report["blocking_issues"]),
            },
        )
        self.session.commit()
        return artifact

    def plan_execution(self, run_id: str) -> Artifact | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        preflight_report = self.artifacts.current_payloads(run_id).get("preflight_report")
        if preflight_report is None:
            preflight_artifact = self.run_preflight(run_id)
            if preflight_artifact is None:
                return None
            preflight_report = preflight_artifact.payload
        execution_plan = build_execution_plan(preflight_report=preflight_report)
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_key="preflight",
            artifact_type="execution_plan",
            payload=execution_plan,
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.execution_plan_created",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_key="preflight",
            message="Pipeline execution plan created.",
            payload={
                "artifact_id": artifact.artifact_id,
                "execution_policy": execution_plan["execution_policy"],
                "can_execute": execution_plan["can_execute"],
                "decision_count": len(execution_plan["decision_log"]),
            },
        )
        self.session.commit()
        return artifact

    def build_proposal_packet(self, run_id: str) -> Artifact | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None

        preflight_artifact = self._current_artifact_by_type(run_id, "preflight_report")
        if preflight_artifact is None:
            preflight_artifact = self.run_preflight(run_id)
        execution_plan_artifact = self._current_artifact_by_type(run_id, "execution_plan")
        if execution_plan_artifact is None:
            execution_plan_artifact = self.plan_execution(run_id)
        if preflight_artifact is None or execution_plan_artifact is None:
            return None

        current_payloads = self.artifacts.current_payloads(run_id)
        packet = build_proposal_packet(
            preflight_report=preflight_artifact.payload,
            execution_plan=execution_plan_artifact.payload,
            current_artifacts=current_payloads,
            source_artifacts={
                "preflight_report": preflight_artifact.artifact_id,
                "execution_plan": execution_plan_artifact.artifact_id,
            },
        )
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_key="preflight",
            artifact_type="proposal_packet",
            payload=packet,
            producer="proposal_packet_builder",
            lineage={
                "preflight_artifact_id": preflight_artifact.artifact_id,
                "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                "source": "clean_room_proposal_kit",
            },
            derived_from=[
                {
                    "artifact_id": preflight_artifact.artifact_id,
                    "artifact_type": preflight_artifact.artifact_type,
                    "version": preflight_artifact.version,
                },
                {
                    "artifact_id": execution_plan_artifact.artifact_id,
                    "artifact_type": execution_plan_artifact.artifact_type,
                    "version": execution_plan_artifact.version,
                },
            ],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.proposal_packet_created",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_key="proposal",
            message="Proposal packet created.",
            payload={
                "artifact_id": artifact.artifact_id,
                "recommended_option_id": packet["recommended_option_id"],
                "option_count": len(packet["options"]),
                "execution_policy": execution_plan_artifact.payload["execution_policy"],
            },
        )
        self.session.commit()
        return artifact

    def plan_stage_execution(
        self,
        run_id: str,
        *,
        execution_plan_artifact: Artifact | None = None,
    ) -> Artifact | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        if execution_plan_artifact is None:
            execution_plan_artifact = self._current_artifact_by_type(run_id, "execution_plan")
        if execution_plan_artifact is None:
            execution_plan_artifact = self.plan_execution(run_id)
        if execution_plan_artifact is None:
            return None

        record = self._get_run_record(run_id)
        if record is None:
            return None
        pipeline = self._pipeline_for_run_record(record)
        manifest = build_stage_execution_manifest(
            run_id=run_id,
            pipeline=pipeline,
            execution_plan=execution_plan_artifact.payload,
            execution_plan_artifact_id=execution_plan_artifact.artifact_id,
        )
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_key="preflight",
            artifact_type="stage_execution_manifest",
            payload=manifest,
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.stage_execution_manifest_created",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_key="preflight",
            message="Stage execution manifest created.",
            payload={
                "artifact_id": artifact.artifact_id,
                "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                "total_stage_tasks": manifest["summary"]["total_stage_tasks"],
                "auto_dispatch_tool_steps": manifest["summary"]["auto_dispatch_tool_steps"],
            },
        )
        self.session.commit()
        return artifact

    def _decode_upload(self, payload: AssetUpload, *, mime_type: str | None) -> bytes:
        """Admission control for an upload, before anything is stored (V8.4).

        Order is the whole point: the encoded length is checked *before* decoding,
        so an oversized body is refused without ever being materialised as bytes,
        and the type/magic checks run before the object store sees anything.
        """
        verdict = check_encoded_length(
            payload.content_base64, max_bytes=settings.asset_upload_max_bytes
        )
        if not verdict.ok:
            raise ApiError(verdict.code, verdict.message, details=verdict.detail)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except binascii.Error as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        verdict = check_decoded_size(content, max_bytes=settings.asset_upload_max_bytes)
        if not verdict.ok:
            raise ApiError(verdict.code, verdict.message, details=verdict.detail)
        verdict = check_content_type(
            declared_mime=mime_type,
            filename=self._safe_filename(payload.filename),
            content=content,
            allowed_types=parse_mime_set(settings.asset_allowed_mime_types),
            denied_types=parse_mime_set(settings.asset_content_type_denylist),
        )
        if not verdict.ok:
            raise ApiError(verdict.code, verdict.message, details=verdict.detail)
        return content

    def upload_asset(self, run_id: str, payload: AssetUpload) -> Asset | None:
        if self.runs.get_run(run_id) is None:
            return None
        filename = self._safe_filename(payload.filename)
        mime_type = payload.mime_type or mimetypes.guess_type(filename)[0]
        content = self._decode_upload(payload, mime_type=mime_type)

        asset_id = f"asset_{uuid4().hex[:12]}"
        object_key = f"runs/{run_id}/{asset_id}/{filename}"
        stored = self.storage.put_bytes(object_key=object_key, content=content, mime_type=mime_type)
        asset_metadata = {
            **payload.asset_metadata,
            "filename": filename,
            "object_key": stored.object_key,
        }
        asset = self.assets.create_asset(
            asset_id=asset_id,
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type=payload.asset_type,
            provider=payload.provider,
            tool_key=payload.tool_key,
            storage_url=stored.storage_url,
            mime_type=mime_type,
            duration_ms=payload.duration_ms,
            width=payload.width,
            height=payload.height,
            file_size=stored.file_size,
            checksum=stored.checksum,
            asset_metadata=asset_metadata,
        )
        self._record_asset_usage(asset, operation="asset_upload")
        self.events.append_event(
            run_id=run_id,
            event_type="asset.uploaded",
            source_type=EventSourceType.SYSTEM,
            source_id=asset.asset_id,
            stage_run_id=asset.stage_run_id,
            message="Asset uploaded.",
            payload={
                "asset_id": asset.asset_id,
                "asset_type": asset.asset_type,
                "file_size": asset.file_size,
                "checksum": asset.checksum,
            },
        )
        self.session.commit()
        return asset

    def ingest_source_media(
        self,
        run_id: str,
        payload: SourceMediaIngestRequest,
    ) -> SourceMediaIngestResult | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None
        if not payload.files and not payload.links:
            raise ValueError("source media ingest requires at least one file or link")

        stage_key = payload.stage_key or "source_ingest"
        stage_record = self._stage_record(record, stage_key)
        stage_run_id = payload.stage_run_id or (stage_record.id if stage_record else None)
        if payload.stage_run_id and not any(
            stage.id == payload.stage_run_id for stage in record.stages
        ):
            raise ValueError("stage_run_id does not belong to this run")

        assets: list[Asset] = []
        source_assets: list[dict[str, Any]] = []
        source_links: list[dict[str, Any]] = []

        for index, source_file in enumerate(payload.files, start=1):
            filename = self._safe_filename(source_file.filename)
            mime_type = source_file.mime_type or mimetypes.guess_type(filename)[0]
            media_type = self._source_media_type(
                media_type=source_file.media_type,
                mime_type=mime_type,
                source_name=filename,
            )
            rights = self._merged_source_rights(payload.default_rights, source_file.rights)
            asset = self.upload_asset(
                run_id,
                AssetUpload(
                    asset_type=self._source_asset_type(media_type),
                    filename=filename,
                    content_base64=source_file.content_base64,
                    stage_run_id=stage_run_id,
                    provider="user_input",
                    tool_key="source_media_ingest",
                    mime_type=mime_type,
                    duration_ms=source_file.duration_ms,
                    width=source_file.width,
                    height=source_file.height,
                    asset_metadata={
                        "source_kind": "file",
                        "media_type": media_type,
                        "label": source_file.label,
                        "role": source_file.role,
                        "rights": rights,
                        "notes": list(source_file.notes),
                        "source_url": source_file.source_url,
                        "source_metadata": source_file.source_metadata,
                        "ingest_stage_key": stage_key,
                    },
                ),
            )
            if asset is None:
                return None
            assets.append(asset)
            source_assets.append(
                {
                    "source_id": f"source_asset_{index:03d}",
                    "source_kind": "file",
                    "asset_id": asset.asset_id,
                    "asset_type": asset.asset_type,
                    "media_type": media_type,
                    "label": source_file.label or filename,
                    "role": source_file.role,
                    "filename": filename,
                    "mime_type": asset.mime_type,
                    "duration_ms": asset.duration_ms,
                    "width": asset.width,
                    "height": asset.height,
                    "file_size": asset.file_size,
                    "checksum": asset.checksum,
                    "storage_url": asset.storage_url,
                    "rights": rights,
                    "notes": list(source_file.notes),
                    "source_url": source_file.source_url,
                    "metadata": dict(source_file.source_metadata),
                }
            )

        for index, source_link in enumerate(payload.links, start=1):
            mime_type = source_link.mime_type or mimetypes.guess_type(
                urllib.parse.urlparse(source_link.url).path
            )[0]
            media_type = self._source_media_type(
                media_type=source_link.media_type,
                mime_type=mime_type,
                source_name=source_link.url,
            )
            rights = self._merged_source_rights(payload.default_rights, source_link.rights)
            source_links.append(
                {
                    "source_id": f"source_link_{index:03d}",
                    "source_kind": "link",
                    "url": source_link.url,
                    "media_type": media_type,
                    "label": source_link.label or source_link.url,
                    "role": source_link.role,
                    "mime_type": mime_type,
                    "duration_ms": source_link.duration_ms,
                    "width": source_link.width,
                    "height": source_link.height,
                    "rights": rights,
                    "notes": list(source_link.notes),
                    "metadata": dict(source_link.source_metadata),
                }
            )

        ingest_notes = [
            {"type": "user_note", "message": note}
            for note in payload.notes
            if str(note).strip()
        ]
        manifest_payload = {
            "version": "1.0",
            "run_id": run_id,
            "source_assets": source_assets,
            "source_links": source_links,
            "source_context": dict(payload.source_context),
            "ingest_notes": ingest_notes,
            "analysis_entry": {
                "status": "pending",
                "recommended_next_artifacts": ["media_analysis_report"],
                "source_asset_count": len(source_assets),
                "source_link_count": len(source_links),
            },
            "ingest_status": "ready",
        }
        manifest_artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            artifact_type="source_asset_manifest",
            payload=manifest_payload,
            producer="source_media_ingest",
            lineage={
                "operation": "source_media_ingest",
                "file_count": len(source_assets),
                "link_count": len(source_links),
            },
        )
        review_payload = {
            "version": "1.0",
            "source_assets": source_assets,
            "source_links": source_links,
            "ingest_notes": ingest_notes,
        }
        review_artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            artifact_type="source_media_review",
            payload=review_payload,
            producer="source_media_ingest",
            lineage={"operation": "source_media_ingest"},
            derived_from=[
                {
                    "artifact_id": manifest_artifact.artifact_id,
                    "artifact_type": manifest_artifact.artifact_type,
                    "version": manifest_artifact.version,
                }
            ],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="source_media.ingested",
            source_type=EventSourceType.SYSTEM,
            source_id=manifest_artifact.artifact_id,
            stage_key=stage_key,
            stage_run_id=stage_run_id,
            message="Source media ingested.",
            payload={
                "source_asset_manifest_id": manifest_artifact.artifact_id,
                "source_media_review_id": review_artifact.artifact_id,
                "file_count": len(source_assets),
                "link_count": len(source_links),
                "asset_ids": [asset.asset_id for asset in assets],
            },
        )
        self.session.commit()
        return SourceMediaIngestResult(
            artifact=manifest_artifact,
            source_media_review=review_artifact,
            assets=assets,
            links=source_links,
            manifest=manifest_payload,
        )

    def analyze_source_media(
        self,
        run_id: str,
        payload: MediaAnalysisRequest,
    ) -> MediaAnalysisResult | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None
        if not self.media_analyzer.check_availability():
            raise ValueError("ffmpeg and ffprobe are required for media analysis")

        stage_key = payload.stage_key or "analysis"
        stage_record = self._stage_record(record, stage_key)
        stage_run_id = payload.stage_run_id or (stage_record.id if stage_record else None)
        if payload.stage_run_id and not any(
            stage.id == payload.stage_run_id for stage in record.stages
        ):
            raise ValueError("stage_run_id does not belong to this run")

        source_entries = self._source_media_entries_for_analysis(
            run_id,
            source_asset_ids=payload.source_asset_ids,
            source_artifact_type=payload.source_artifact_type,
        )
        if not source_entries:
            raise ValueError("media analysis requires at least one source asset")

        frame_assets: list[Asset] = []
        source_reports: list[dict[str, Any]] = []
        scenes: list[dict[str, Any]] = []
        frame_samples: list[dict[str, Any]] = []
        analysis_notes: list[dict[str, Any]] = []
        request_payload = payload.model_dump()

        with tempfile.TemporaryDirectory(prefix="script2video-media-analysis-") as tmpdir:
            temp_root = Path(tmpdir)
            for source_index, source_entry in enumerate(source_entries, start=1):
                asset = self._asset_for_run(run_id, source_entry.get("asset_id"))
                if asset is None:
                    analysis_notes.append(
                        {
                            "level": "warning",
                            "code": "source_asset_missing",
                            "asset_id": source_entry.get("asset_id"),
                        }
                    )
                    continue
                media_type = source_entry.get("media_type") or asset.asset_metadata.get("media_type")
                if media_type not in {"video", "audio"} and asset.asset_type not in {
                    "source_video",
                    "source_audio",
                }:
                    analysis_notes.append(
                        {
                            "level": "info",
                            "code": "source_asset_not_probe_target",
                            "asset_id": asset.asset_id,
                            "asset_type": asset.asset_type,
                        }
                    )
                    continue

                source_path = self._write_asset_to_temp(
                    asset,
                    temp_root,
                    asset.asset_metadata.get("filename") or "source-media.bin",
                )
                analysis = self.media_analyzer.analyze(
                    source_path,
                    output_dir=temp_root / f"{asset.asset_id}_frames",
                    max_frames=payload.max_frames_per_video,
                    detect_scenes=payload.detect_scenes,
                    scene_threshold=payload.scene_threshold,
                    analyze_audio=payload.analyze_audio,
                    audio_window_seconds=payload.audio_window_seconds,
                )
                uploaded_frames = self._archive_analysis_frames(
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    source_asset=asset,
                    source_index=source_index,
                    analysis=analysis,
                    include_frame_assets=payload.include_frame_assets,
                )
                frame_assets.extend(uploaded_frames)
                source_report = self._media_source_report(
                    source_entry=source_entry,
                    asset=asset,
                    analysis=analysis,
                    frame_assets=uploaded_frames,
                )
                source_reports.append(source_report)
                frame_samples.extend(source_report["frame_samples"])
                if analysis.has_video:
                    scenes.extend(
                        self._analysis_scene_summary(
                            source_index=source_index,
                            asset=asset,
                            analysis=analysis,
                            frame_assets=uploaded_frames,
                        )
                    )
                for warning in analysis.warnings:
                    analysis_notes.append(
                        {
                            "level": "warning",
                            "code": "ffmpeg_warning",
                            "asset_id": asset.asset_id,
                            "message": warning,
                        }
                    )

        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=stage_run_id,
            tool_key="ffmpeg_media_probe",
            provider="ffmpeg",
            request_payload=request_payload,
            response_payload={
                "analyzed_source_count": len(source_reports),
                "frame_asset_count": len(frame_assets),
                "scene_count": len(scenes),
                "scene_cut_count": sum(
                    len(report.get("scene_cuts") or []) for report in source_reports
                ),
                "audio_analyzed_source_count": sum(
                    1 for report in source_reports if report.get("audio_energy")
                ),
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
        )
        self._record_tool_invocation_usage(invocation, operation="media_analysis")

        transcript, asr_invocation = self._transcribe_sources(
            run_id=run_id,
            stage_run_id=stage_run_id,
            payload=payload,
            source_reports=source_reports,
        )
        if asr_invocation is not None:
            self._record_tool_invocation_usage(asr_invocation, operation="speech_to_text")
        media_summary = self._media_analysis_summary(
            requested_source_count=len(source_entries),
            source_reports=source_reports,
            frame_assets=frame_assets,
            transcript=transcript,
        )
        report_payload = {
            "version": "1.0",
            "run_id": run_id,
            "tool_invocation_id": invocation.invocation_id,
            "source_artifact_type": payload.source_artifact_type,
            "media_summary": media_summary,
            "source_assets": source_reports,
            "frame_samples": frame_samples,
            "scenes": scenes,
            "transcript": transcript,
            "analysis_notes": [
                *analysis_notes,
                {
                    "level": "info" if transcript.get("status") == "available" else "warning",
                    "code": "transcript_unavailable"
                    if transcript.get("status") != "available"
                    else "transcript_available",
                    "message": transcript.get("reason")
                    or transcript.get("engine")
                    or "",
                },
            ],
        }
        report_artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            artifact_type="media_analysis_report",
            payload=report_payload,
            producer="ffmpeg_media_probe",
            lineage={
                "operation": "media_analysis",
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        brief_payload = self._video_analysis_brief_payload(
            run_id=run_id,
            run_input=record.input_payload or {},
            report_artifact=report_artifact,
            report_payload=report_payload,
        )
        brief_artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            artifact_type="video_analysis_brief",
            payload=brief_payload,
            producer="ffmpeg_media_probe",
            lineage={
                "operation": "media_analysis_brief",
                "tool_invocation_id": invocation.invocation_id,
            },
            derived_from=[
                {
                    "artifact_id": report_artifact.artifact_id,
                    "artifact_type": report_artifact.artifact_type,
                    "version": report_artifact.version,
                }
            ],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="media_analysis.completed",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_key=stage_key,
            stage_run_id=stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Source media analysis completed.",
            payload={
                "media_analysis_report_artifact_id": report_artifact.artifact_id,
                "video_analysis_brief_artifact_id": brief_artifact.artifact_id,
                "analyzed_source_count": len(source_reports),
                "frame_asset_count": len(frame_assets),
            },
        )
        self.session.commit()
        return MediaAnalysisResult(
            artifact=report_artifact,
            video_analysis_brief=brief_artifact,
            frame_assets=frame_assets,
            report=report_payload,
            brief=brief_payload,
        )

    def get_asset(self, asset_id: str) -> Asset | None:
        return self.assets.get_asset(asset_id)

    def list_assets(self, run_id: str) -> list[Asset] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.assets.list_assets(run_id)

    # ------------------------------------------------------------------ #
    # asset library (V6.4)
    # ------------------------------------------------------------------ #

    def create_asset_collection(
        self,
        project_id: str,
        payload: AssetCollectionCreate,
    ) -> AssetCollection | None:
        collection = self.asset_library.create_collection(
            project_id,
            payload,
            tenant_id=self.tenant_id_for_project(project_id) or DEFAULT_TENANT_ID,
        )
        if collection is not None:
            self.session.commit()
        return collection

    def list_asset_collections(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        include_archived: bool = False,
    ) -> list[AssetCollection] | None:
        return self.asset_library.list_collections(
            project_id,
            kind=kind,
            include_archived=include_archived,
        )

    def get_asset_collection(
        self,
        collection_id: str,
        *,
        with_items: bool = True,
    ) -> AssetCollection | None:
        return self.asset_library.get_collection(collection_id, with_items=with_items)

    def update_asset_collection(
        self,
        collection_id: str,
        payload: AssetCollectionUpdate,
    ) -> AssetCollection | None:
        collection = self.asset_library.update_collection(collection_id, payload)
        if collection is not None:
            self.session.commit()
        return collection

    def project_asset_library(self, project_id: str) -> ProjectAssetLibrary | None:
        return self.asset_library.project_library(project_id)

    def list_asset_collection_items(
        self,
        collection_id: str,
        *,
        include_removed: bool = False,
        role: str | None = None,
    ) -> list[AssetCollectionItem] | None:
        if self.asset_library.get_collection(collection_id) is None:
            return None
        return self.asset_library.list_items(
            collection_id,
            include_removed=include_removed,
            role=role,
        )

    def add_asset_collection_item(
        self,
        collection_id: str,
        payload: AssetCollectionItemAdd,
    ) -> tuple[CollectionAddOutcome, AssetCollectionItem | None]:
        outcome, item = self.asset_library.add_item(
            collection_id,
            payload,
            tenant_id=self.tenant_id_for_asset_collection(collection_id)
            or DEFAULT_TENANT_ID,
        )
        if outcome is CollectionAddOutcome.CREATED:
            self.session.commit()
        return outcome, item

    def update_asset_collection_item(
        self,
        item_id: str,
        payload: AssetCollectionItemUpdate,
    ) -> AssetCollectionItem | None:
        item = self.asset_library.update_item(item_id, payload)
        if item is not None:
            self.session.commit()
        return item

    def remove_asset_collection_item(self, item_id: str) -> AssetCollectionItem | None:
        item = self.asset_library.remove_item(item_id)
        if item is not None:
            self.session.commit()
        return item

    def import_run_assets_into_collection(
        self,
        collection_id: str,
        payload: AssetCollectionImportRequest,
    ) -> tuple[CollectionImportOutcome, AssetCollectionImportResult | None]:
        outcome, result = self.asset_library.import_from_run(collection_id, payload)
        if outcome is CollectionImportOutcome.IMPORTED:
            self.session.commit()
            if result is not None and result.imported_count:
                self.events.append_event(
                    run_id=result.run_id,
                    event_type="run.assets_promoted_to_library",
                    source_type=EventSourceType.SYSTEM,
                    source_id=collection_id,
                    message=(
                        f"{result.imported_count} 个资产已沉淀到资产库集合 {collection_id}"
                    ),
                    payload={
                        "collection_id": collection_id,
                        "imported_count": result.imported_count,
                        "added_item_ids": result.added_item_ids,
                        "skipped_asset_ids": result.skipped_asset_ids,
                    },
                )
                self.session.commit()
        return outcome, result

    def reuse_asset_into_run(
        self,
        run_id: str,
        payload: AssetReuseRequest,
    ) -> tuple[AssetReuseOutcome, AssetReuseResult | None]:
        outcome, asset, item = self.asset_library.reuse_into_run(
            run_id,
            item_id=payload.item_id,
            asset_type=payload.asset_type,
            stage_run_id=payload.stage_run_id,
            note=payload.note,
        )
        if outcome is not AssetReuseOutcome.REUSED or asset is None or item is None:
            return outcome, None
        self.session.commit()
        self.events.append_event(
            run_id=run_id,
            event_type="run.asset_reused_from_library",
            source_type=EventSourceType.SYSTEM,
            source_id=asset.asset_id,
            message=f"复用资产库条目 {payload.item_id} 生成新资产 {asset.asset_id}",
            payload={
                "item_id": payload.item_id,
                "collection_id": item.collection_id,
                "source_asset_id": item.asset_id,
                "asset_id": asset.asset_id,
                "license": item.license.model_dump(mode="json"),
            },
        )
        self.session.commit()
        return outcome, AssetReuseResult(
            run_id=run_id,
            asset=asset,
            source_item_id=payload.item_id,
            source_collection_id=item.collection_id,
            reused_from_asset_id=item.asset_id,
            provenance=item.provenance,
            license=item.license,
        )

    def build_composition_graph(
        self,
        run_id: str,
        payload: CompositionGraphBuildRequest | None = None,
    ) -> Artifact | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        request = payload or CompositionGraphBuildRequest()
        selected_asset_ids = {
            "primary_video_asset_id": request.primary_video_asset_id,
            "avatar_video_asset_id": request.avatar_video_asset_id,
            "narration_audio_asset_id": request.narration_audio_asset_id,
            "subtitle_asset_id": request.subtitle_asset_id,
            "music_asset_id": request.music_asset_id,
        }
        for field_name, asset_id in selected_asset_ids.items():
            if asset_id is not None and self._asset_for_run(run_id, asset_id) is None:
                raise ValueError(f"{field_name} does not belong to this run")

        current_artifacts = self.artifacts.list_artifacts(run_id, current_only=True)
        artifact_payloads = {
            artifact.artifact_type: artifact.payload for artifact in current_artifacts
        }
        source_artifact_ids = {
            artifact.artifact_type: artifact.artifact_id for artifact in current_artifacts
        }
        assets = [asset.model_dump() for asset in self.assets.list_assets(run_id)]
        metadata_render_settings = request.graph_metadata.get("render_settings", {})
        render_profile_overrides = request.graph_metadata.get("render_profile_overrides", {})
        graph = build_composition_graph(
            run=run.model_dump(),
            current_artifacts=artifact_payloads,
            assets=assets,
            render_settings={
                "runtime": request.runtime,
                "render_profile_key": request.render_profile_key,
                "width": request.width,
                "height": request.height,
                "fps": request.fps,
                "crf": request.crf,
                "preset": request.preset,
                "video_bitrate": request.video_bitrate,
                "audio_bitrate": request.audio_bitrate,
                "bgm_volume": request.bgm_volume,
                "render_profile_overrides": render_profile_overrides,
                **metadata_render_settings,
            },
            selected_asset_ids=selected_asset_ids,
            source_artifacts=source_artifact_ids,
        )
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=request.stage_run_id,
            stage_key="compose",
            artifact_type="composition_graph",
            payload=graph,
            producer="composition_graph_builder",
            lineage={
                "source": "clean_room_composition_kit",
                "artifact_ids": source_artifact_ids,
                "selected_asset_ids": {
                    key: value for key, value in selected_asset_ids.items() if value is not None
                },
            },
            derived_from=[
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "version": artifact.version,
                }
                for artifact in current_artifacts
                if artifact.artifact_type
                in {
                    "production_plan",
                    "spoken_script",
                    "shot_list",
                    "asset_manifest",
                    "edit_plan",
                    "proposal_packet",
                    "screening_room_scene_plan",
                }
            ],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.composition_graph_created",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_key="compose",
            message="Composition graph created.",
            payload={
                "artifact_id": artifact.artifact_id,
                "ready_for_render": graph["validation"]["ready_for_render"],
                "track_count": len(graph["tracks"]),
                "duration_seconds": graph["duration_seconds"],
                "render_profile_key": graph.get("render_profile", {}).get("profile_key"),
            },
        )
        self.session.commit()
        return artifact

    def render_html_canvas_preview(
        self,
        run_id: str,
        payload: HtmlCanvasPreviewRequest | None = None,
    ) -> tuple[ToolInvocation, Asset, Artifact] | None:
        if self.runs.get_run(run_id) is None:
            return None
        request = payload or HtmlCanvasPreviewRequest()
        graph_artifact = self._composition_graph_for_preview(run_id, request)
        if graph_artifact is None:
            return None

        result = self.canvas_renderer.render(
            HtmlCanvasRenderRequest(
                composition_graph=graph_artifact.payload,
                title=request.title,
                preview_time_seconds=request.preview_time_seconds,
                include_debug_overlay=request.include_debug_overlay,
            )
        )
        request_payload = request.model_dump()
        request_payload["composition_graph_artifact_id"] = graph_artifact.artifact_id
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=request.stage_run_id,
            tool_key=self.canvas_renderer.renderer_key,
            provider=self.canvas_renderer.provider_key,
            request_payload=request_payload,
            response_payload={
                **result.report,
                "validation": result.validation,
                "first_frame": result.first_frame,
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=request.stage_run_id,
            asset_type="html_canvas_preview",
            provider=self.canvas_renderer.provider_key,
            tool_key=self.canvas_renderer.renderer_key,
            filename=request.filename,
            content=result.html.encode("utf-8"),
            mime_type="text/html; charset=utf-8",
            duration_ms=round(float(graph_artifact.payload.get("duration_seconds") or 0) * 1000),
            asset_metadata={
                **request.asset_metadata,
                "composition_graph_artifact_id": graph_artifact.artifact_id,
                "renderer": self.canvas_renderer.renderer_key,
                "html_sha256": result.report["html_sha256"],
            },
        )
        report = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=request.stage_run_id,
            stage_key="compose",
            artifact_type="canvas_render_report",
            payload={
                "schema_version": "1.0",
                "generated_at": result.report["generated_at"],
                "renderer": self.canvas_renderer.renderer_key,
                "composition_graph_artifact_id": graph_artifact.artifact_id,
                "html_asset_id": asset.asset_id,
                "output": {
                    "asset_id": asset.asset_id,
                    "filename": request.filename,
                    "storage_url": asset.storage_url,
                    "mime_type": asset.mime_type,
                    "file_size": asset.file_size,
                    "checksum": asset.checksum,
                    "html_sha256": result.report["html_sha256"],
                    "html_bytes": result.report["html_bytes"],
                },
                "inputs": {
                    "composition_graph_artifact_id": graph_artifact.artifact_id,
                    "source_graph_ready_for_render": (
                        graph_artifact.payload.get("validation") or {}
                    ).get("ready_for_render"),
                },
                "settings": {
                    "title": request.title,
                    "preview_time_seconds": request.preview_time_seconds,
                    "include_debug_overlay": request.include_debug_overlay,
                    "render_profile": result.report.get("render_profile") or {},
                },
                "validation": result.validation,
                "first_frame": result.first_frame,
            },
            producer="html_canvas_renderer",
            lineage={
                "source": "clean_room_canvas_renderer_kit",
                "composition_graph_artifact_id": graph_artifact.artifact_id,
                "tool_invocation_id": invocation.invocation_id,
            },
            derived_from=[
                {
                    "artifact_id": graph_artifact.artifact_id,
                    "artifact_type": graph_artifact.artifact_type,
                    "version": graph_artifact.version,
                }
            ],
        )
        self._record_generated_asset_event(invocation, asset, "HTML Canvas preview rendered.")
        self.events.append_event(
            run_id=run_id,
            event_type="run.canvas_render_report_created",
            source_type=EventSourceType.SYSTEM,
            source_id=report.artifact_id,
            stage_key="compose",
            message="Canvas render report created.",
            payload={
                "artifact_id": report.artifact_id,
                "html_asset_id": asset.asset_id,
                "composition_graph_artifact_id": graph_artifact.artifact_id,
                "ready_for_preview": result.validation["ready_for_preview"],
            },
        )
        self.session.commit()
        return invocation, asset, report

    def run_quality_gates(
        self,
        run_id: str,
        payload: QualityGateRequest | None = None,
    ) -> tuple[ToolInvocation, Artifact] | None:
        if self.runs.get_run(run_id) is None:
            return None
        request = payload or QualityGateRequest()
        render_summary = self._quality_render_summary(run_id, request)
        render_asset = self._quality_render_asset(run_id, request, render_summary)
        if render_asset is None:
            raise ValueError("rendered video asset is required before quality gates")
        composition_graph = self._quality_composition_graph(run_id, request, render_summary)
        if not self.quality_runner.check_availability():
            raise ValueError("ffprobe is required for quality gates")

        with tempfile.TemporaryDirectory(prefix="script2video-quality-") as tmpdir:
            media_path = self._write_asset_to_temp(render_asset, Path(tmpdir), "rendered.mp4")
            report_payload = self.quality_runner.evaluate(
                QualityGateEvaluationRequest(
                    media_path=media_path,
                    render_asset=render_asset.model_dump(),
                    render_summary=render_summary.payload if render_summary is not None else {},
                    composition_graph=composition_graph.payload
                    if composition_graph is not None
                    else {},
                    checks=request.checks
                    if request.checks is not None
                    else None,
                    min_score=request.min_score,
                    expected_audio=request.expected_audio,
                )
            )

        stage_run_id = request.stage_run_id or self._stage_run_id(run_id, "final_review")
        request_payload = request.model_dump()
        request_payload.update(
            {
                "render_asset_id": render_asset.asset_id,
                "render_summary_artifact_id": render_summary.artifact_id
                if render_summary is not None
                else None,
                "composition_graph_artifact_id": composition_graph.artifact_id
                if composition_graph is not None
                else None,
            }
        )
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=stage_run_id,
            tool_key=self.quality_runner.runner_key,
            provider=self.quality_runner.provider_key,
            request_payload=request_payload,
            response_payload={
                "status": report_payload["status"],
                "quality_score": report_payload["quality_score"],
                "summary": report_payload["summary"],
                "human_review": report_payload["human_review"],
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
        )
        report_payload["inputs"] = {
            "render_asset_id": render_asset.asset_id,
            "render_summary_artifact_id": render_summary.artifact_id
            if render_summary is not None
            else None,
            "composition_graph_artifact_id": composition_graph.artifact_id
            if composition_graph is not None
            else None,
            "tool_invocation_id": invocation.invocation_id,
        }
        artifact = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key="final_review",
            artifact_type="quality_report",
            payload=report_payload,
            producer=self.quality_runner.runner_key,
            lineage={
                "source": "clean_room_quality_gate_kit",
                "render_asset_id": render_asset.asset_id,
                "render_summary_artifact_id": render_summary.artifact_id
                if render_summary is not None
                else None,
                "composition_graph_artifact_id": composition_graph.artifact_id
                if composition_graph is not None
                else None,
                "tool_invocation_id": invocation.invocation_id,
            },
            derived_from=[
                item
                for item in [
                    _artifact_reference(render_summary),
                    _artifact_reference(composition_graph),
                ]
                if item is not None
            ],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.quality_report_created",
            source_type=EventSourceType.SYSTEM,
            source_id=artifact.artifact_id,
            stage_run_id=stage_run_id,
            stage_key="final_review",
            severity=EventSeverity.WARNING
            if report_payload["status"] != "passed"
            else EventSeverity.INFO,
            message="Quality report created.",
            payload={
                "artifact_id": artifact.artifact_id,
                "render_asset_id": render_asset.asset_id,
                "status": report_payload["status"],
                "quality_score": report_payload["quality_score"],
                "human_review_required": report_payload["human_review"]["required"],
            },
        )
        self.session.commit()
        return invocation, artifact

    def run_edit_tool(
        self,
        run_id: str,
        payload: EditToolRequest,
    ) -> tuple[ToolInvocation, list[Asset], Artifact] | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        if not self.edit_worker.check_availability():
            raise ValueError("ffmpeg and ffprobe are required for edit tools")

        source_assets = self._edit_source_assets(run_id, payload)
        subtitle_asset = self._edit_subtitle_asset(run_id, payload)
        stage_run_id = payload.stage_run_id or self._stage_run_id(run_id, "edit")
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=stage_run_id,
            tool_key="ffmpeg_edit_tools",
            provider="ffmpeg",
            request_payload=payload.model_dump(),
            status=ToolInvocationStatus.RUNNING,
            cost=0.0,
        )
        outputs: list[dict[str, Any]] = []
        output_assets: list[Asset] = []
        commands: list[list[str]] = []
        warnings: list[str] = []

        try:
            with tempfile.TemporaryDirectory(prefix="script2video-edit-") as tmpdir:
                temp_root = Path(tmpdir)
                source_paths = [
                    self._write_asset_to_temp(asset, temp_root, f"source_{index}.mp4")
                    for index, asset in enumerate(source_assets, start=1)
                ]
                subtitle_path = (
                    self._write_asset_to_temp(subtitle_asset, temp_root, "subtitles.srt")
                    if subtitle_asset is not None
                    else None
                )
                edit_results = self._run_edit_requests(
                    run=run,
                    payload=payload,
                    source_paths=source_paths,
                    subtitle_path=subtitle_path,
                    temp_root=temp_root,
                )
                for result in edit_results:
                    commands.extend(result.commands)
                    warnings.extend(result.warnings)
                    asset = self._create_generated_asset(
                        run_id=run_id,
                        stage_run_id=stage_run_id,
                        asset_type=result.asset_type,
                        provider="ffmpeg",
                        tool_key="ffmpeg_edit_tools",
                        filename=result.output_path.name,
                        content=result.output_path.read_bytes(),
                        mime_type=result.mime_type,
                        duration_ms=round(result.duration_seconds * 1000)
                        if result.duration_seconds is not None
                        else None,
                        width=result.width,
                        height=result.height,
                        asset_metadata={
                            **payload.asset_metadata,
                            "operation": result.operation,
                            "source_asset_ids": [asset.asset_id for asset in source_assets],
                            "subtitle_asset_id": subtitle_asset.asset_id
                            if subtitle_asset is not None
                            else None,
                            "tool_invocation_id": invocation.invocation_id,
                            **result.metadata,
                        },
                    )
                    output_assets.append(asset)
                    outputs.append(
                        {
                            "asset_id": asset.asset_id,
                            "asset_type": asset.asset_type,
                            "filename": result.output_path.name,
                            "mime_type": asset.mime_type,
                            "duration_ms": asset.duration_ms,
                            "width": asset.width,
                            "height": asset.height,
                            "file_size": asset.file_size,
                            "checksum": asset.checksum,
                            "metadata": result.metadata,
                        }
                    )
        except Exception as exc:
            classification = classify_error(
                error_code=type(exc).__name__,
                error_message=str(exc),
                provider="ffmpeg",
                tool_key="ffmpeg_edit_tools",
                status=ToolInvocationStatus.FAILED.value,
            )
            self.tool_invocations.update_invocation(
                invocation.invocation_id,
                status=ToolInvocationStatus.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                **_error_classification_fields(classification),
            )
            self.session.commit()
            raise

        response_payload = {
            "operation": payload.operation.value,
            "output_asset_ids": [asset.asset_id for asset in output_assets],
            "outputs": outputs,
            "warnings": sorted(set(warnings)),
        }
        invocation = self.tool_invocations.update_invocation(
            invocation.invocation_id,
            status=ToolInvocationStatus.SUCCEEDED,
            response_payload=response_payload,
            duration_ms=sum(asset.duration_ms or 0 for asset in output_assets) or None,
        ) or invocation
        report = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key="edit",
            artifact_type="edit_operation_report",
            payload={
                "schema_version": "1.0",
                "generated_at": _utc_now(),
                "operation": payload.operation.value,
                "status": "completed",
                "source_asset_ids": [asset.asset_id for asset in source_assets],
                "output_asset_ids": [asset.asset_id for asset in output_assets],
                "outputs": outputs,
                "settings": payload.model_dump(),
                "commands": commands,
                "warnings": sorted(set(warnings)),
            },
            producer="ffmpeg_edit_tools",
            lineage={
                "source": "clean_room_edit_kit",
                "tool_invocation_id": invocation.invocation_id,
                "source_asset_ids": [asset.asset_id for asset in source_assets],
                "subtitle_asset_id": subtitle_asset.asset_id if subtitle_asset else None,
            },
            derived_from=[],
        )
        self.events.append_event(
            run_id=run_id,
            event_type="run.edit_operation_completed",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_run_id=stage_run_id,
            stage_key="edit",
            message="Edit operation completed.",
            payload={
                "artifact_id": report.artifact_id,
                "operation": payload.operation.value,
                "output_asset_ids": [asset.asset_id for asset in output_assets],
            },
        )
        self.session.commit()
        return invocation, output_assets, report

    def generate_tts_audio(
        self,
        run_id: str,
        payload: TtsGenerateRequest,
    ) -> tuple[ToolInvocation, Asset] | None:
        if self.runs.get_run(run_id) is None:
            return None
        if not self.tts_adapter.check_availability():
            raise ValueError("DASHSCOPE_API_KEY is required for DashScope TTS")

        request_payload = payload.model_dump()
        estimated_cost = self._estimate_adapter_cost(
            self.tts_adapter,
            capability="tts",
            payload=request_payload,
        )
        self._assert_budget_allows(
            run_id,
            estimated_cost=estimated_cost,
            operation="generate_tts_audio",
            provider=self.tts_adapter.provider_key,
            tool_key=self.tts_adapter.tool_key,
            capability="tts",
        )
        result = self.tts_adapter.synthesize(request_payload)
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key=self.tts_adapter.tool_key,
            provider=self.tts_adapter.provider_key,
            request_payload=request_payload,
            response_payload={
                "audio_url": result.get("audio_url"),
                "model": result.get("model"),
                "voice": result.get("voice"),
                "language_type": result.get("language_type"),
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=estimated_cost,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type="tts_audio",
            provider=self.tts_adapter.provider_key,
            tool_key=self.tts_adapter.tool_key,
            filename=payload.filename,
            content=result["content"],
            mime_type=result.get("mime_type") or "audio/wav",
            duration_ms=None,
            asset_metadata={
                **payload.asset_metadata,
                "segment_id": payload.segment_id,
                "voice": result.get("voice"),
                "model": result.get("model"),
                "source_audio_url": result.get("audio_url"),
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        self._record_generated_asset_event(invocation, asset, "TTS audio generated.")
        self.session.commit()
        return invocation, asset

    def generate_subtitles(
        self,
        run_id: str,
        payload: SubtitleGenerateRequest,
    ) -> tuple[ToolInvocation, Asset] | None:
        if self.runs.get_run(run_id) is None:
            return None

        segments = payload.segments
        if segments is None:
            source = self.artifacts.current_payloads(run_id).get(payload.source_artifact_type)
            if not source:
                raise ValueError(f"{payload.source_artifact_type} artifact is required")
            segments = source.get("segments")
        if not segments:
            raise ValueError("subtitle segments are required")

        srt_text = _build_srt(segments, max_chars_per_caption=payload.max_chars_per_caption)
        request_payload = payload.model_dump()
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key="local_srt_subtitles",
            provider="internal",
            request_payload=request_payload,
            response_payload={"subtitle_format": "srt", "segment_count": len(segments)},
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type="subtitle_file",
            provider="internal",
            tool_key="local_srt_subtitles",
            filename=payload.filename,
            content=srt_text.encode("utf-8"),
            mime_type="application/x-subrip",
            duration_ms=None,
            asset_metadata={
                **payload.asset_metadata,
                "source_artifact_type": payload.source_artifact_type,
                "subtitle_format": "srt",
                "segment_count": len(segments),
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        self._record_generated_asset_event(invocation, asset, "Subtitles generated.")
        self.session.commit()
        return invocation, asset

    def generate_music(
        self,
        run_id: str,
        payload: MusicGenerateRequest,
    ) -> tuple[ToolInvocation, Asset] | None:
        if self.runs.get_run(run_id) is None:
            return None
        if not self.music_adapter.check_availability():
            raise ValueError("ELEVENLABS_API_KEY is required for ElevenLabs music")

        request_payload = payload.model_dump()
        estimated_cost = self._estimate_adapter_cost(
            self.music_adapter,
            capability="music_generation",
            payload=request_payload,
        )
        self._assert_budget_allows(
            run_id,
            estimated_cost=estimated_cost,
            operation="generate_music",
            provider=self.music_adapter.provider_key,
            tool_key=self.music_adapter.tool_key,
            capability="music_generation",
        )
        result = self.music_adapter.compose(request_payload)
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key=self.music_adapter.tool_key,
            provider=self.music_adapter.provider_key,
            request_payload=request_payload,
            response_payload={
                "output_format": result.get("output_format"),
                "music_length_ms": payload.music_length_ms,
                "force_instrumental": payload.force_instrumental,
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=estimated_cost,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type="background_music",
            provider=self.music_adapter.provider_key,
            tool_key=self.music_adapter.tool_key,
            filename=payload.filename,
            content=result["content"],
            mime_type=result.get("mime_type") or "audio/mpeg",
            duration_ms=payload.music_length_ms,
            asset_metadata={
                **payload.asset_metadata,
                "prompt": payload.prompt,
                "output_format": result.get("output_format"),
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        self._record_generated_asset_event(invocation, asset, "Background music generated.")
        self.session.commit()
        return invocation, asset

    def generate_image(
        self,
        run_id: str,
        payload: ImageGenerateRequest,
    ) -> tuple[ToolInvocation, Asset] | None:
        if self.runs.get_run(run_id) is None:
            return None
        if not self.image_adapter.check_availability():
            raise ValueError("DASHSCOPE_API_KEY is required for DashScope image generation")

        request_payload = payload.model_dump()
        estimated_cost = self._estimate_adapter_cost(
            self.image_adapter,
            capability="image_generation",
            payload=request_payload,
        )
        self._assert_budget_allows(
            run_id,
            estimated_cost=estimated_cost,
            operation="generate_image",
            provider=self.image_adapter.provider_key,
            tool_key=self.image_adapter.tool_key,
            capability="image_generation",
        )
        result = self.image_adapter.generate(request_payload)
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key=self.image_adapter.tool_key,
            provider=self.image_adapter.provider_key,
            request_payload=request_payload,
            response_payload={
                "image_url": result.get("image_url"),
                "model": result.get("model"),
                "size": result.get("size"),
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=estimated_cost,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type="reference_image",
            provider=self.image_adapter.provider_key,
            tool_key=self.image_adapter.tool_key,
            filename=payload.filename,
            content=result["content"],
            mime_type=result.get("mime_type") or "image/png",
            duration_ms=None,
            asset_metadata={
                **payload.asset_metadata,
                "prompt": payload.prompt,
                "model": result.get("model"),
                "size": result.get("size"),
                "source_image_url": result.get("image_url"),
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        self._record_generated_asset_event(invocation, asset, "Image generated.")
        self.session.commit()
        return invocation, asset

    def compose_video(
        self,
        run_id: str,
        payload: ComposeVideoRequest,
    ) -> tuple[ToolInvocation, Asset, Artifact] | None:
        if self.runs.get_run(run_id) is None:
            return None
        if not self.compose_worker.check_availability():
            raise ValueError("ffmpeg and ffprobe are required for video composition")
        self._assert_budget_allows(
            run_id,
            estimated_cost=0.0,
            operation="compose_video",
        )

        avatar_asset = self._asset_for_run(run_id, payload.avatar_video_asset_id)
        narration_asset = self._asset_for_run(run_id, payload.narration_audio_asset_id)
        subtitle_asset = self._asset_for_run(run_id, payload.subtitle_asset_id)
        music_asset = self._asset_for_run(run_id, payload.music_asset_id)
        if avatar_asset is None:
            raise ValueError("avatar_video_asset_id does not belong to this run")
        if payload.narration_audio_asset_id is not None and narration_asset is None:
            raise ValueError("narration_audio_asset_id does not belong to this run")
        if payload.subtitle_asset_id is not None and subtitle_asset is None:
            raise ValueError("subtitle_asset_id does not belong to this run")
        if payload.music_asset_id is not None and music_asset is None:
            raise ValueError("music_asset_id does not belong to this run")

        composition_graph = self.build_composition_graph(
            run_id,
            CompositionGraphBuildRequest(
                stage_run_id=payload.stage_run_id,
                avatar_video_asset_id=payload.avatar_video_asset_id,
                narration_audio_asset_id=payload.narration_audio_asset_id,
                subtitle_asset_id=payload.subtitle_asset_id,
                music_asset_id=payload.music_asset_id,
                runtime="ffmpeg",
                render_profile_key=payload.render_profile_key,
                width=payload.width,
                height=payload.height,
                fps=payload.fps,
                crf=payload.crf,
                preset=payload.preset,
                video_bitrate=payload.video_bitrate,
                audio_bitrate=payload.audio_bitrate,
                bgm_volume=payload.bgm_volume,
                graph_metadata={"source": "compose_video"},
            ),
        )
        compose_settings = _render_target_settings(
            composition_graph.payload if composition_graph is not None else {},
            "ffmpeg",
        )

        with tempfile.TemporaryDirectory(prefix="script2video-compose-") as tmpdir:
            temp_root = Path(tmpdir)
            avatar_path = self._write_asset_to_temp(avatar_asset, temp_root, "avatar.mp4")
            narration_path = (
                self._write_asset_to_temp(narration_asset, temp_root, "narration.audio")
                if narration_asset
                else None
            )
            subtitle_path = (
                self._write_asset_to_temp(subtitle_asset, temp_root, "subtitles.srt")
                if subtitle_asset
                else None
            )
            music_path = (
                self._write_asset_to_temp(music_asset, temp_root, "bgm.audio")
                if music_asset
                else None
            )
            output_path = temp_root / "final.mp4"
            result = self.compose_worker.compose(
                FfmpegComposeRequest(
                    avatar_video_path=avatar_path,
                    narration_path=narration_path,
                    subtitle_path=subtitle_path,
                    music_path=music_path,
                    output_path=output_path,
                    width=int(compose_settings["width"]),
                    height=int(compose_settings["height"]),
                    fps=int(compose_settings["fps"]),
                    crf=int(compose_settings["crf"]),
                    preset=compose_settings["preset"],
                    video_bitrate=compose_settings.get("video_bitrate"),
                    audio_bitrate=compose_settings.get("audio_bitrate") or "128k",
                    bgm_volume=float(compose_settings["bgm_volume"]),
                    subtitle_style=compose_settings.get("caption") or {},
                )
            )
            output_bytes = output_path.read_bytes()

        request_payload = payload.model_dump()
        request_payload["composition_graph_artifact_id"] = (
            composition_graph.artifact_id if composition_graph is not None else None
        )
        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key="ffmpeg_compose",
            provider="ffmpeg",
            request_payload=request_payload,
            response_payload={
                "duration_seconds": result.duration_seconds,
                "width": result.width,
                "height": result.height,
                "has_audio": result.has_audio,
                "subtitles_embedded": result.subtitles_embedded,
                "subtitle_track_added": result.subtitle_track_added,
                "warnings": result.warnings,
                "command": result.command,
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
            duration_ms=round(result.duration_seconds * 1000)
            if result.duration_seconds is not None
            else None,
        )
        asset = self._create_generated_asset(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            asset_type="rendered_video",
            provider="ffmpeg",
            tool_key="ffmpeg_compose",
            filename=payload.filename,
            content=output_bytes,
            mime_type="video/mp4",
            duration_ms=round(result.duration_seconds * 1000)
            if result.duration_seconds is not None
            else None,
            asset_metadata={
                **payload.asset_metadata,
                "avatar_video_asset_id": avatar_asset.asset_id,
                "narration_audio_asset_id": narration_asset.asset_id if narration_asset else None,
                "subtitle_asset_id": subtitle_asset.asset_id if subtitle_asset else None,
                "music_asset_id": music_asset.asset_id if music_asset else None,
                "composition_graph_artifact_id": (
                    composition_graph.artifact_id if composition_graph is not None else None
                ),
                "tool_invocation_id": invocation.invocation_id,
            },
        )
        render_summary = self.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            stage_key="compose",
            artifact_type="render_summary",
            payload={
                "version": "1.0",
                "renderer": "ffmpeg",
                "render_asset_id": asset.asset_id,
                "output": {
                    "filename": payload.filename,
                    "storage_url": asset.storage_url,
                    "mime_type": asset.mime_type,
                    "duration_ms": asset.duration_ms,
                    "width": result.width,
                    "height": result.height,
                    "has_audio": result.has_audio,
                    "subtitles_embedded": result.subtitles_embedded,
                    "subtitle_track_added": result.subtitle_track_added,
                    "warnings": result.warnings,
                    "file_size": asset.file_size,
                    "checksum": asset.checksum,
                },
                "inputs": {
                    "avatar_video_asset_id": avatar_asset.asset_id,
                    "narration_audio_asset_id": narration_asset.asset_id if narration_asset else None,
                    "subtitle_asset_id": subtitle_asset.asset_id if subtitle_asset else None,
                    "music_asset_id": music_asset.asset_id if music_asset else None,
                    "composition_graph_artifact_id": (
                        composition_graph.artifact_id if composition_graph is not None else None
                    ),
                },
                "settings": {
                    "width": compose_settings["width"],
                    "height": compose_settings["height"],
                    "fps": compose_settings["fps"],
                    "crf": compose_settings["crf"],
                    "preset": compose_settings["preset"],
                    "video_bitrate": compose_settings.get("video_bitrate"),
                    "audio_bitrate": compose_settings.get("audio_bitrate"),
                    "bgm_volume": compose_settings["bgm_volume"],
                    "render_profile_key": compose_settings.get("render_profile_key"),
                    "safe_area": compose_settings.get("safe_area") or {},
                    "caption": compose_settings.get("caption") or {},
                    "cover": compose_settings.get("cover") or {},
                },
            },
        )
        self._record_generated_asset_event(invocation, asset, "Video composed with FFmpeg.")
        self.session.commit()
        return invocation, asset, render_summary

    def submit_avatar_task(
        self,
        run_id: str,
        payload: AvatarVideoSubmitRequest,
    ) -> ToolInvocation | None:
        if self.runs.get_run(run_id) is None:
            return None
        if not self.avatar_video_adapter.check_availability():
            raise ValueError("DASHSCOPE_API_KEY is required for DashScope avatar video")

        request_payload = payload.model_dump()
        estimated_cost = self._estimate_adapter_cost(
            self.avatar_video_adapter,
            capability="avatar_video",
            payload=request_payload,
        )
        self._assert_budget_allows(
            run_id,
            estimated_cost=estimated_cost,
            operation="submit_avatar_task",
            provider=self.avatar_video_adapter.provider_key,
            tool_key=self.avatar_video_adapter.tool_key,
            capability="avatar_video",
        )
        detected_bbox = None
        # Only the EMO dialect consumes a bbox, and only it exposes face-detect.
        # s2v models read the raw image, so the hop is skipped rather than paid
        # for and thrown away.
        needs_detection = self.avatar_video_adapter.requires_bbox(request_payload.get("model"))
        bbox_missing = not request_payload.get("face_bbox") or not request_payload.get("ext_bbox")
        if needs_detection and bbox_missing:
            detected_bbox = self.avatar_video_adapter.detect_bbox(
                image_url=request_payload["image_url"],
                provider_options=request_payload.get("provider_options") or {},
                model=request_payload.get("model"),
            )
            request_payload["face_bbox"] = detected_bbox["face_bbox"]
            request_payload["ext_bbox"] = detected_bbox["ext_bbox"]
        raw_response = self.avatar_video_adapter.submit(request_payload)
        normalized = self.avatar_video_adapter.normalize_output("avatar_video", raw_response)
        external_task_id = normalized.get("external_task_id")
        if not external_task_id:
            raise ValueError("DashScope avatar response did not include external task id")

        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            tool_key=self.avatar_video_adapter.tool_key,
            provider=self.avatar_video_adapter.provider_key,
            request_payload=request_payload,
            response_payload={
                "submit_response": normalized,
                "provider_status": normalized.get("provider_status"),
                **({"detected_bbox": detected_bbox} if detected_bbox else {}),
            },
            status=ToolInvocationStatus.SUBMITTED,
            external_task_id=external_task_id,
            cost=estimated_cost,
        )
        self._record_tool_invocation_usage(
            invocation,
            operation="submit_avatar_task",
        )
        self.events.append_event(
            run_id=run_id,
            event_type="tool.submitted",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_run_id=payload.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Avatar video task submitted.",
            payload={
                "tool_key": invocation.tool_key,
                "provider": invocation.provider,
                "external_task_id": invocation.external_task_id,
            },
        )
        self.session.commit()
        return invocation

    def submit_avatar_task_from_assets(
        self,
        run_id: str,
        payload: AvatarVideoFromAssetsRequest,
    ) -> ToolInvocation | None:
        if self.runs.get_run(run_id) is None:
            return None
        image_url = self._resolve_provider_media_url(
            run_id=run_id,
            direct_url=payload.person_image_url,
            asset_id=payload.person_image_asset_id,
            expires_in_seconds=payload.access_link_ttl_seconds,
            public_base_url=payload.public_base_url,
            label="person_image",
        )
        audio_url = self._resolve_provider_media_url(
            run_id=run_id,
            direct_url=payload.audio_url,
            asset_id=payload.audio_asset_id,
            expires_in_seconds=payload.access_link_ttl_seconds,
            public_base_url=payload.public_base_url,
            label="audio",
        )
        invocation = self.submit_avatar_task(
            run_id,
            AvatarVideoSubmitRequest(
                stage_run_id=payload.stage_run_id,
                shot_id=payload.shot_id,
                image_url=image_url,
                audio_url=audio_url,
                face_bbox=payload.face_bbox,
                ext_bbox=payload.ext_bbox,
                model=payload.model,
                provider_options=payload.provider_options,
            ),
        )
        if invocation is not None:
            response_payload = {
                **invocation.response_payload,
                "resolved_inputs": {
                    "person_image_asset_id": payload.person_image_asset_id,
                    "audio_asset_id": payload.audio_asset_id,
                    "used_direct_person_image_url": payload.person_image_url is not None,
                    "used_direct_audio_url": payload.audio_url is not None,
                },
            }
            updated = self.tool_invocations.update_invocation(
                invocation.invocation_id,
                response_payload=response_payload,
            )
            self.session.commit()
            if updated is None:
                raise ValueError("tool invocation disappeared after avatar submit")
            return updated
        return None

    def poll_avatar_task(self, invocation_id: str) -> ToolInvocation | None:
        invocation = self.tool_invocations.get_invocation(invocation_id)
        if invocation is None:
            return None
        if not invocation.external_task_id:
            raise ValueError("tool invocation does not have an external task id")

        normalized = self.avatar_video_adapter.get_status(invocation.external_task_id)
        status = _tool_invocation_status(normalized.get("status"))
        response_payload = {
            **invocation.response_payload,
            "latest_status": normalized,
            "provider_status": normalized.get("provider_status"),
        }
        if normalized.get("video_url"):
            response_payload["video_url"] = normalized["video_url"]

        error_message = normalized.get("error_message")
        error_code = _provider_error_code(normalized)
        taxonomy_fields = {}
        if status in {ToolInvocationStatus.FAILED, ToolInvocationStatus.CANCELLED}:
            error_code = error_code or (
                "provider_cancelled"
                if status == ToolInvocationStatus.CANCELLED
                else "provider_failed"
            )
            taxonomy_fields = _error_classification_fields(
                classify_error(
                    error_code=error_code,
                    error_message=error_message,
                    provider=invocation.provider,
                    tool_key=invocation.tool_key,
                    status=status.value,
                )
            )

        updated = self.tool_invocations.update_invocation(
            invocation_id,
            status=status,
            response_payload=response_payload,
            error_code=error_code,
            error_message=error_message,
            **taxonomy_fields,
        )
        self.events.append_event(
            run_id=invocation.run_id,
            event_type="tool.polled",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_run_id=invocation.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Avatar video task polled.",
            payload={
                "tool_key": invocation.tool_key,
                "provider": invocation.provider,
                "status": status.value,
                "external_task_id": invocation.external_task_id,
                **taxonomy_fields,
            },
        )
        self.session.commit()
        return updated

    def archive_avatar_task(
        self,
        invocation_id: str,
        payload: AvatarVideoArchiveRequest,
    ) -> tuple[ToolInvocation, Asset] | None:
        invocation = self.tool_invocations.get_invocation(invocation_id)
        if invocation is None:
            return None

        video_url = payload.video_url or invocation.response_payload.get("video_url")
        content = self._avatar_video_content(payload, video_url)
        asset_id = f"asset_{uuid4().hex[:12]}"
        filename = self._safe_filename(payload.filename)
        object_key = f"runs/{invocation.run_id}/{asset_id}/{filename}"
        stored = self.storage.put_bytes(
            object_key=object_key,
            content=content,
            mime_type=payload.mime_type,
        )
        asset_metadata = {
            **payload.asset_metadata,
            "filename": filename,
            "object_key": stored.object_key,
            "tool_invocation_id": invocation.invocation_id,
            "external_task_id": invocation.external_task_id,
            "source_video_url": video_url,
        }
        asset = self.assets.create_asset(
            asset_id=asset_id,
            run_id=invocation.run_id,
            stage_run_id=invocation.stage_run_id,
            asset_type="avatar_video_clip",
            provider=invocation.provider,
            tool_key=invocation.tool_key,
            storage_url=stored.storage_url,
            mime_type=payload.mime_type,
            file_size=stored.file_size,
            checksum=stored.checksum,
            asset_metadata=asset_metadata,
        )
        self._record_asset_usage(
            asset,
            operation="archive_avatar_video",
            source_id=invocation.invocation_id,
            metadata={"external_task_id": invocation.external_task_id},
        )
        response_payload = {
            **invocation.response_payload,
            "archived_asset": {
                "asset_id": asset.asset_id,
                "storage_url": asset.storage_url,
                "file_size": asset.file_size,
                "checksum": asset.checksum,
            },
        }
        updated = self.tool_invocations.update_invocation(
            invocation.invocation_id,
            status=ToolInvocationStatus.ARCHIVED,
            response_payload=response_payload,
        )
        if updated is None:
            raise ValueError("tool invocation disappeared during archive")

        self.events.append_event(
            run_id=invocation.run_id,
            event_type="tool.archived",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_run_id=invocation.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Avatar video task archived as an asset.",
            payload={
                "asset_id": asset.asset_id,
                "storage_url": asset.storage_url,
                "external_task_id": invocation.external_task_id,
            },
        )
        self.session.commit()
        return updated, asset

    def get_tool_invocation(self, invocation_id: str) -> ToolInvocation | None:
        return self.tool_invocations.get_invocation(invocation_id)

    def list_tool_invocations(self, run_id: str) -> list[ToolInvocation] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.tool_invocations.list_invocations(run_id)

    def handle_provider_callback(
        self,
        provider: str,
        payload: ProviderCallbackRequest,
    ) -> ProviderCallbackResult:
        invocation = self.tool_invocations.get_invocation_by_external_task_id(
            provider=provider,
            external_task_id=payload.external_task_id,
        )
        if invocation is None:
            return ProviderCallbackResult(
                matched=False,
                provider=provider,
                external_task_id=payload.external_task_id,
                message="No matching tool invocation for provider callback.",
            )

        normalized = _normalize_provider_callback(provider=provider, payload=payload)
        status = _tool_invocation_status(normalized.get("status"))
        response_payload = {
            **invocation.response_payload,
            "latest_status": normalized,
            "provider_status": normalized.get("provider_status"),
            "provider_callback": normalized,
        }
        if normalized.get("video_url"):
            response_payload["video_url"] = normalized["video_url"]

        error_code = normalized.get("error_code") or _provider_error_code(normalized)
        error_message = normalized.get("error_message")
        taxonomy_fields = {}
        if status in {ToolInvocationStatus.FAILED, ToolInvocationStatus.CANCELLED}:
            error_code = error_code or (
                "provider_cancelled"
                if status == ToolInvocationStatus.CANCELLED
                else "provider_failed"
            )
            taxonomy_fields = _error_classification_fields(
                classify_error(
                    error_code=error_code,
                    error_message=error_message,
                    provider=invocation.provider,
                    tool_key=invocation.tool_key,
                    status=status.value,
                )
            )

        updated_invocation = self.tool_invocations.update_invocation(
            invocation.invocation_id,
            status=status,
            response_payload=response_payload,
            error_code=error_code,
            error_message=error_message,
            **taxonomy_fields,
        )
        if updated_invocation is None:
            raise ValueError("tool invocation disappeared during provider callback")

        event = self.events.append_event(
            run_id=updated_invocation.run_id,
            event_type="provider.callback_received",
            source_type=EventSourceType.TOOL,
            source_id=updated_invocation.invocation_id,
            stage_run_id=updated_invocation.stage_run_id,
            tool_invocation_id=updated_invocation.invocation_id,
            message="Provider callback received.",
            payload={
                "provider": provider,
                "tool_key": updated_invocation.tool_key,
                "status": status.value,
                "external_task_id": payload.external_task_id,
                **taxonomy_fields,
            },
        )

        job = self._background_job_for_invocation(updated_invocation)
        if job is not None:
            job = self._apply_provider_callback_to_background_job(
                job=job,
                invocation=updated_invocation,
                normalized=normalized,
                status=status,
                taxonomy_fields=taxonomy_fields,
            )

        self.session.commit()
        return ProviderCallbackResult(
            matched=True,
            provider=provider,
            external_task_id=payload.external_task_id,
            invocation=updated_invocation,
            job=job,
            event=event,
            message="Provider callback recorded.",
        )

    def _background_job_for_invocation(
        self,
        invocation: ToolInvocation,
    ) -> BackgroundJob | None:
        for job in self.background_jobs.list_jobs(invocation.run_id):
            if job.result_payload.get("tool_invocation_id") == invocation.invocation_id:
                return job
            if (
                invocation.external_task_id
                and job.result_payload.get("external_task_id") == invocation.external_task_id
            ):
                return job
        return None

    def _apply_provider_callback_to_background_job(
        self,
        *,
        job: BackgroundJob,
        invocation: ToolInvocation,
        normalized: dict[str, Any],
        status: ToolInvocationStatus,
        taxonomy_fields: dict[str, Any],
    ) -> BackgroundJob:
        result_payload = {
            **job.result_payload,
            "tool_invocation_id": invocation.invocation_id,
            "external_task_id": invocation.external_task_id,
            "provider_status": normalized.get("provider_status"),
            "latest_status": normalized,
            "provider_callback_received": True,
        }
        if normalized.get("video_url"):
            result_payload["video_url"] = normalized["video_url"]

        if status in {ToolInvocationStatus.FAILED, ToolInvocationStatus.CANCELLED}:
            return self._fail_background_job(
                job,
                error_code=invocation.error_code or "provider_failed",
                error_message=invocation.error_message or "provider task failed",
                result_payload=result_payload,
            )

        updated = self.background_jobs.update_job(
            job.job_id,
            status=BackgroundJobStatus.WAITING_PROVIDER,
            progress=max(job.progress, 80 if status == ToolInvocationStatus.SUCCEEDED else 60),
            result_payload=result_payload,
            lease_expires_at=_job_now() + timedelta(seconds=_JOB_LEASE_SECONDS),
        )
        if updated is None:
            raise ValueError("background job disappeared during provider callback")
        self.events.append_event(
            run_id=updated.run_id,
            event_type="job.provider_callback_received",
            source_type=EventSourceType.SYSTEM,
            source_id=updated.job_id,
            stage_run_id=updated.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Background job received provider callback.",
            payload={
                "job_id": updated.job_id,
                "job_type": updated.job_type,
                "status": status.value,
                "external_task_id": invocation.external_task_id,
                **taxonomy_fields,
            },
        )
        return updated

    def list_usage_ledger(self, run_id: str) -> list[UsageLedgerEntry] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.usage_ledger.list_entries(run_id)

    def get_usage_summary(self, run_id: str) -> UsageSummary | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        return self._usage_summary_for_run(run)

    def enqueue_avatar_from_assets_job(
        self,
        run_id: str,
        payload: AvatarVideoFromAssetsRequest,
    ) -> BackgroundJob | None:
        if self.runs.get_run(run_id) is None:
            return None
        job = self.background_jobs.create_job(
            run_id=run_id,
            payload=BackgroundJobCreate(
                stage_run_id=payload.stage_run_id,
                job_type="avatar_from_assets",
                request_payload=payload.model_dump(),
            ),
            queue=_queue_for_job("avatar_from_assets", payload.model_dump()),
        )
        self._record_background_job_usage(job, operation="job_queued")
        self.events.append_event(
            run_id=run_id,
            event_type="job.queued",
            source_type=EventSourceType.SYSTEM,
            source_id=job.job_id,
            stage_run_id=payload.stage_run_id,
            message="Background avatar job queued.",
            payload={"job_id": job.job_id, "job_type": job.job_type},
        )
        self.session.commit()
        return job

    def get_background_job(self, job_id: str) -> BackgroundJob | None:
        return self.background_jobs.get_job(job_id)

    def list_background_jobs(self, run_id: str) -> list[BackgroundJob] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.background_jobs.list_jobs(run_id)

    def list_processable_background_jobs(self, *, limit: int = 10) -> list[BackgroundJob]:
        return self.background_jobs.list_processable_jobs(limit=limit)

    def claim_processable_background_jobs(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
        queues: list[str] | None = None,
    ) -> list[BackgroundJob]:
        jobs = self.background_jobs.claim_processable_jobs(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            queues=queues,
        )
        self.session.commit()
        return jobs

    def reap_stalled_background_jobs(
        self,
        *,
        timeout_seconds: int = 1800,
    ) -> list[BackgroundJob]:
        """Fail jobs stuck in ``running`` past the timeout (V8.1 reaper).

        A synchronous job that never returns would otherwise hold its lease
        forever: the lease expiry lets another worker re-claim it, but the run
        would still hang in ``running`` with no explanation. The reaper gives
        the stall a name (``job_timeout``), which the error taxonomy classifies
        as transient — so retryable job types go straight back into the queue
        with backoff and everything else lands in a terminal state. Jobs in
        ``waiting_provider`` are exempt on purpose: provider polls are slow by
        nature and the lease, not the timeout, guards those.
        """
        cutoff = _job_now() - timedelta(seconds=max(1, timeout_seconds))
        records = list(
            self.session.scalars(
                select(BackgroundJobRecord).where(
                    BackgroundJobRecord.status == BackgroundJobStatus.RUNNING.value,
                    BackgroundJobRecord.started_at.is_not(None),
                    BackgroundJobRecord.started_at < cutoff,
                )
            )
        )
        reaped: list[BackgroundJob] = []
        for record in records:
            job = background_job_from_record(record)
            reaped.append(
                self._fail_background_job(
                    job,
                    error_code="job_timeout",
                    error_message=(
                        f"background job timed out after running for more than "
                        f"{max(1, timeout_seconds)} seconds"
                    ),
                )
            )
        return reaped

    def requeue_background_job(self, job_id: str) -> BackgroundJob | None:
        """Replay a dead-lettered job with a fresh attempt count (V8.1)."""
        job = self.background_jobs.get_job(job_id)
        if job is None:
            return None
        if job.status != BackgroundJobStatus.DEAD:
            return job
        requeued = self.background_jobs.requeue_dead_job(job_id)
        if requeued is not None and requeued.status == BackgroundJobStatus.QUEUED:
            self._record_background_job_usage(requeued, operation="job_requeued")
            self.events.append_event(
                run_id=requeued.run_id,
                event_type="job.requeued",
                source_type=EventSourceType.SYSTEM,
                source_id=requeued.job_id,
                stage_run_id=requeued.stage_run_id,
                message="Dead-lettered background job replayed with a fresh attempt count.",
                payload={"job_id": requeued.job_id, "attempt": requeued.attempt},
            )
            self.session.commit()
        return requeued

    def dispatch_stage_execution_tasks(self, run_id: str) -> list[BackgroundJob] | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        manifest_artifact = self._current_artifact_by_type(run_id, "stage_execution_manifest")
        if manifest_artifact is None:
            manifest_artifact = self.plan_stage_execution(run_id)
        if manifest_artifact is None:
            return None
        if manifest_artifact.payload.get("source", {}).get("can_execute") is not True:
            return []

        existing_step_keys = {
            job.request_payload.get("manifest_step_key")
            for job in self.background_jobs.list_jobs(run_id)
            if job.request_payload.get("manifest_step_key")
        }
        stage_statuses = {stage.stage_key: stage.status.value for stage in run.stages}
        invalidated_stage_keys = {
            stage.stage_key for stage in run.stages if stage.invalidation is not None
        }
        created_jobs: list[BackgroundJob] = []
        blocked_stage_keys: list[str] = []
        for task in manifest_artifact.payload.get("stage_tasks", []):
            task_stage_key = task.get("stage_key")
            if task_stage_key in invalidated_stage_keys:
                blocked_stage_keys.append(task_stage_key)
                continue
            if not _is_stage_ready_for_manifest_dispatch(task, stage_statuses):
                continue
            for step in task.get("tool_steps", []):
                if not _is_dispatchable_manifest_step(step):
                    continue
                manifest_step_key = _manifest_step_key(
                    manifest_artifact=manifest_artifact,
                    task=task,
                    step=step,
                )
                if manifest_step_key in existing_step_keys:
                    continue
                payload = self._background_job_payload_for_manifest_step(
                    run=run,
                    task=task,
                    step=step,
                    manifest_step_key=manifest_step_key,
                )
                if payload is None:
                    continue
                job = self.background_jobs.create_job(
                    run_id=run_id,
                    payload=payload,
                    queue=_queue_for_job(payload.job_type, payload.request_payload),
                )
                self._record_background_job_usage(job, operation="job_queued")
                created_jobs.append(job)
                existing_step_keys.add(manifest_step_key)
                self.events.append_event(
                    run_id=run_id,
                    event_type="job.queued",
                    source_type=EventSourceType.SYSTEM,
                    source_id=job.job_id,
                    stage_key=task.get("stage_key"),
                    stage_run_id=job.stage_run_id,
                    message="Stage execution tool step queued.",
                    payload={
                        "job_id": job.job_id,
                        "job_type": job.job_type,
                        "manifest_step_key": manifest_step_key,
                        "stage_key": task.get("stage_key"),
                        "capability": step.get("capability"),
                        "tool_key": step.get("tool_key"),
                    },
                )

        if created_jobs:
            self.events.append_event(
                run_id=run_id,
                event_type="run.stage_execution_dispatched",
                source_type=EventSourceType.SYSTEM,
                source_id=manifest_artifact.artifact_id,
                stage_key="preflight",
                message="Stage execution manifest dispatched background jobs.",
                payload={
                    "manifest_artifact_id": manifest_artifact.artifact_id,
                    "job_count": len(created_jobs),
                    "job_ids": [job.job_id for job in created_jobs],
                    "blocked_stage_keys": sorted(set(blocked_stage_keys)),
                },
            )
        self.session.commit()
        return created_jobs

    def preview_stage_execution_dispatch(self, run_id: str) -> dict | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        manifest_artifact = self._current_artifact_by_type(run_id, "stage_execution_manifest")
        if manifest_artifact is None:
            return {
                "schema_version": "1.0",
                "run_id": run_id,
                "manifest_artifact_id": None,
                "can_execute": False,
                "summary": _dispatch_preview_summary([]),
                "steps": [],
                "reason": "需要先生成 stage_execution_manifest",
            }

        stage_statuses = {stage.stage_key: stage.status.value for stage in run.stages}
        jobs_by_step_key = {
            job.request_payload.get("manifest_step_key"): job
            for job in self.background_jobs.list_jobs(run_id)
            if job.request_payload.get("manifest_step_key")
        }
        steps = []
        for task in manifest_artifact.payload.get("stage_tasks", []):
            for step in task.get("tool_steps", []):
                if not step.get("auto_dispatch"):
                    continue
                manifest_step_key = _manifest_step_key(
                    manifest_artifact=manifest_artifact,
                    task=task,
                    step=step,
                )
                steps.append(
                    self._manifest_step_dispatch_preview(
                        run=run,
                        manifest_artifact=manifest_artifact,
                        task=task,
                        step=step,
                        manifest_step_key=manifest_step_key,
                        stage_statuses=stage_statuses,
                        job=jobs_by_step_key.get(manifest_step_key),
                    )
                )

        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "manifest_artifact_id": manifest_artifact.artifact_id,
            "can_execute": manifest_artifact.payload.get("source", {}).get("can_execute") is True,
            "summary": _dispatch_preview_summary(steps),
            "steps": steps,
            "blockers": self._invalidation_blockers(run_id),
            "reason": None,
        }

    def run_manifest_action(
        self,
        run_id: str,
        payload: ManifestActionRequest,
    ) -> ManifestActionResult | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None

        action_payload = dict(payload.payload or {})
        if payload.action == ManifestActionName.DISPATCH_READY_STEPS:
            jobs = self.dispatch_stage_execution_tasks(run_id) or []
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Manifest ready steps dispatched.",
                result_payload={
                    "jobs": [job.model_dump() for job in jobs],
                    "dispatch_preview": self.preview_stage_execution_dispatch(run_id),
                    "blockers": self._invalidation_blockers(run_id),
                },
                refresh={"run": True, "jobs": True, "dispatch_preview": True},
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=payload.stage_run_id,
                manifest_step_key=payload.manifest_step_key,
            )
            return result

        if payload.action == ManifestActionName.GENERATE_PERSON_IMAGE:
            stage_run_id = self._manifest_action_stage_run_id(
                run_id,
                request=payload,
                action_payload=action_payload,
                default_stage_key="asset_generation",
            )
            manifest_step_key = payload.manifest_step_key or self._manifest_action_payload_str(
                action_payload,
                "manifest_step_key",
            )
            metadata = self._manifest_action_payload_dict(action_payload, "asset_metadata")
            metadata.setdefault("mode", "manifest_action")
            metadata.setdefault("role", "person_image")
            metadata.setdefault("source", "manifest_action")
            metadata.setdefault("manifest_step_key", manifest_step_key)
            metadata.setdefault("description", "Manifest action generated talking-avatar person image.")
            image_request = ImageGenerateRequest(
                stage_run_id=stage_run_id,
                prompt=self._manifest_action_payload_str(action_payload, "prompt")
                or self._default_person_image_prompt(run),
                model=self._manifest_action_payload_str(action_payload, "model"),
                size=self._manifest_action_payload_str(action_payload, "size"),
                n=int(action_payload.get("n") or 1),
                prompt_extend=bool(action_payload.get("prompt_extend", True)),
                watermark=bool(action_payload.get("watermark", False)),
                negative_prompt=self._manifest_action_payload_str(action_payload, "negative_prompt")
                or (
                    "text, watermark, logo, extra fingers, distorted face, hidden mouth, "
                    "side profile, low quality, blurry"
                ),
                seed=action_payload.get("seed"),
                filename=self._manifest_action_payload_str(action_payload, "filename")
                or "manifest-person-image.png",
                provider_options=self._manifest_action_payload_dict(action_payload, "provider_options"),
                asset_metadata=metadata,
            )
            image_result = self.generate_image(run_id, image_request)
            if image_result is None:
                return None
            invocation, asset = image_result
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Person image generated.",
                result_payload={
                    "invocation": invocation.model_dump(),
                    "asset": asset.model_dump(),
                },
                refresh={
                    "run": True,
                    "assets": True,
                    "tool_invocations": True,
                    "dispatch_preview": True,
                },
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=stage_run_id,
                manifest_step_key=manifest_step_key,
            )
            return result

        if payload.action == ManifestActionName.UPLOAD_PERSON_IMAGE:
            stage_run_id = self._manifest_action_stage_run_id(
                run_id,
                request=payload,
                action_payload=action_payload,
                default_stage_key="asset_generation",
            )
            manifest_step_key = payload.manifest_step_key or self._manifest_action_payload_str(
                action_payload,
                "manifest_step_key",
            )
            metadata = self._manifest_action_payload_dict(action_payload, "asset_metadata")
            metadata.setdefault("mode", "manifest_action")
            metadata.setdefault("role", self._manifest_action_payload_str(action_payload, "role") or "person_image")
            metadata.setdefault("source", "manifest_action_upload")
            metadata.setdefault("manifest_step_key", manifest_step_key)
            metadata.setdefault("description", "Manifest action uploaded talking-avatar person image.")
            content_base64 = self._manifest_action_payload_str(action_payload, "content_base64")
            if not content_base64:
                raise ValueError("content_base64 is required for upload_person_image")
            upload_request = AssetUpload(
                asset_type=self._manifest_action_payload_str(action_payload, "asset_type")
                or "reference_image",
                filename=self._manifest_action_payload_str(action_payload, "filename")
                or "manifest-person-image.png",
                content_base64=content_base64,
                stage_run_id=stage_run_id,
                provider=self._manifest_action_payload_str(action_payload, "provider") or "user_input",
                tool_key=self._manifest_action_payload_str(action_payload, "tool_key")
                or "manifest_action_upload",
                mime_type=self._manifest_action_payload_str(action_payload, "mime_type")
                or "application/octet-stream",
                width=action_payload.get("width"),
                height=action_payload.get("height"),
                asset_metadata=metadata,
            )
            asset = self.upload_asset(run_id, upload_request)
            if asset is None:
                return None
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Person image uploaded.",
                result_payload={"asset": asset.model_dump()},
                refresh={"run": True, "assets": True, "dispatch_preview": True},
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=stage_run_id,
                manifest_step_key=manifest_step_key,
            )
            return result

        if payload.action == ManifestActionName.GENERATE_TTS:
            stage_run_id = self._manifest_action_stage_run_id(
                run_id,
                request=payload,
                action_payload=action_payload,
                default_stage_key="asset_generation",
            )
            manifest_step_key = payload.manifest_step_key or self._manifest_action_payload_str(
                action_payload,
                "manifest_step_key",
            )
            text = self._manifest_action_payload_str(action_payload, "text") or self._manifest_action_spoken_text(
                run_id,
                max_chars=600,
            )
            if not text:
                raise ValueError("spoken_script artifact is required before generating TTS")
            if len(text) > 600:
                text = text[:597].rstrip() + "..."
            metadata = self._manifest_action_payload_dict(action_payload, "asset_metadata")
            metadata.setdefault("mode", "manifest_action")
            metadata.setdefault("source_artifact_type", "spoken_script")
            metadata.setdefault("manifest_step_key", manifest_step_key)
            tts_request = TtsGenerateRequest(
                stage_run_id=stage_run_id,
                segment_id=self._manifest_action_payload_str(action_payload, "segment_id") or "full_script",
                text=text,
                voice=self._manifest_action_payload_str(action_payload, "voice") or "Cherry",
                model=self._manifest_action_payload_str(action_payload, "model") or "qwen3-tts-flash",
                language_type=self._manifest_action_payload_str(action_payload, "language_type") or "Auto",
                filename=self._manifest_action_payload_str(action_payload, "filename")
                or "manifest-narration.wav",
                provider_options=self._manifest_action_payload_dict(action_payload, "provider_options"),
                asset_metadata=metadata,
            )
            tts_result = self.generate_tts_audio(run_id, tts_request)
            if tts_result is None:
                return None
            invocation, asset = tts_result
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="TTS audio generated.",
                result_payload={
                    "invocation": invocation.model_dump(),
                    "asset": asset.model_dump(),
                },
                refresh={
                    "run": True,
                    "assets": True,
                    "tool_invocations": True,
                    "dispatch_preview": True,
                },
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=stage_run_id,
                manifest_step_key=manifest_step_key,
            )
            return result

        if payload.action == ManifestActionName.ANALYZE_SOURCE_MEDIA:
            stage_run_id = self._manifest_action_stage_run_id(
                run_id,
                request=payload,
                action_payload=action_payload,
                default_stage_key="analysis",
            )
            source_asset_ids = action_payload.get("source_asset_ids") or []
            if not isinstance(source_asset_ids, list):
                raise ValueError("source_asset_ids must be an array")
            analysis_result = self.analyze_source_media(
                run_id,
                MediaAnalysisRequest(
                    stage_run_id=stage_run_id,
                    source_asset_ids=[str(asset_id) for asset_id in source_asset_ids],
                    source_artifact_type=self._manifest_action_payload_str(
                        action_payload,
                        "source_artifact_type",
                    )
                    or "source_asset_manifest",
                    max_frames_per_video=int(action_payload.get("max_frames_per_video") or 5),
                    include_frame_assets=bool(action_payload.get("include_frame_assets", True)),
                    provider_options=self._manifest_action_payload_dict(
                        action_payload,
                        "provider_options",
                    ),
                ),
            )
            if analysis_result is None:
                return None
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Source media analysis completed.",
                result_payload={
                    "artifact": analysis_result.artifact.model_dump(),
                    "video_analysis_brief": analysis_result.video_analysis_brief.model_dump(),
                    "frame_assets": [
                        asset.model_dump() for asset in analysis_result.frame_assets
                    ],
                },
                refresh={
                    "run": True,
                    "artifacts": True,
                    "assets": True,
                    "tool_invocations": True,
                    "events": True,
                    "dispatch_preview": True,
                },
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=stage_run_id,
                manifest_step_key=payload.manifest_step_key,
            )
            return result

        if payload.action == ManifestActionName.PROCESS_JOB:
            job = self._manifest_action_job(run_id, payload.job_id)
            processed = self.process_background_job(job.job_id)
            if processed is None:
                return None
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Background job processed.",
                result_payload={"job": processed.model_dump()},
                refresh={"run": True, "jobs": True, "assets": True, "dispatch_preview": True},
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=processed.stage_run_id,
                manifest_step_key=processed.request_payload.get("manifest_step_key"),
            )
            return result

        if payload.action == ManifestActionName.RETRY_JOB:
            job = self._manifest_action_job(run_id, payload.job_id)
            retried = self.retry_background_job(job.job_id)
            if retried is None:
                return None
            result = ManifestActionResult(
                action=payload.action,
                run_id=run_id,
                status="completed",
                message="Background job queued for retry.",
                result_payload={"job": retried.model_dump()},
                refresh={"jobs": True, "dispatch_preview": True},
            )
            self._record_manifest_action_event(
                result,
                stage_run_id=retried.stage_run_id,
                manifest_step_key=retried.request_payload.get("manifest_step_key"),
            )
            return result

        raise ValueError(f"unsupported manifest action: {payload.action}")

    def dispatch_all_stage_execution_tasks(self) -> list[BackgroundJob]:
        run_ids = set(
            self.session.scalars(
                select(ArtifactRecord.run_id)
                .where(ArtifactRecord.artifact_type == "stage_execution_manifest")
                .where(ArtifactRecord.is_current.is_(True))
            )
        )
        dispatched: list[BackgroundJob] = []
        for run_id in sorted(run_ids):
            jobs = self.dispatch_stage_execution_tasks(run_id) or []
            dispatched.extend(jobs)
        return dispatched

    def retry_background_job(self, job_id: str) -> BackgroundJob | None:
        job = self.background_jobs.get_job(job_id)
        if job is None:
            return None
        retried = self.background_jobs.retry_job(job_id)
        if retried is not None and retried.status == BackgroundJobStatus.QUEUED:
            self._record_background_job_usage(retried, operation="job_retried")
            self.events.append_event(
                run_id=retried.run_id,
                event_type="job.retried",
                source_type=EventSourceType.SYSTEM,
                source_id=retried.job_id,
                stage_run_id=retried.stage_run_id,
                message="Background job queued for retry.",
                payload={"job_id": retried.job_id, "attempt": retried.attempt},
            )
            self.session.commit()
        return retried

    def process_background_job(
        self,
        job_id: str,
        *,
        worker_id: str = "api_worker",
    ) -> BackgroundJob | None:
        job = self.background_jobs.get_job(job_id)
        if job is None:
            return None
        if job.job_type not in {"avatar_from_assets", "stage_tool_step"}:
            return self._fail_background_job(
                job,
                error_code="unsupported_job_type",
                error_message=f"unsupported background job type: {job.job_type}",
            )
        if job.status in {BackgroundJobStatus.COMPLETED, BackgroundJobStatus.CANCELLED}:
            return job
        try:
            if job.job_type == "stage_tool_step":
                return self._process_stage_tool_step_background_job(
                    job,
                    worker_id=worker_id,
                )
            if job.status in {
                BackgroundJobStatus.QUEUED,
                BackgroundJobStatus.RUNNING,
                BackgroundJobStatus.FAILED,
            }:
                return self._submit_avatar_background_job(job, worker_id=worker_id)
            if job.status == BackgroundJobStatus.WAITING_PROVIDER:
                return self._poll_avatar_background_job(job)
        except RunGateError as exc:
            # A risk gate is a decision, not a crash: fail this job with the
            # gate's own code so the board can explain it, and let the worker
            # keep running. Once the operator confirms, a retry will pass.
            # Gate failures are decisions, not transient faults — never an
            # automatic retry, whatever the taxonomy says.
            job = self.background_jobs.get_job(job_id) or job
            return self._fail_background_job(
                job,
                error_code=exc.code,
                error_message=exc.message,
                result_payload={"gate_error": exc.to_payload()},
                allow_auto_retry=False,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            job = self.background_jobs.get_job(job_id) or job
            return self._fail_background_job(
                job,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        return job

    def download_asset(self, asset_id: str) -> tuple[Asset, bytes] | None:
        asset = self.assets.get_asset(asset_id)
        if asset is None:
            return None
        return asset, self.storage.read_bytes(asset.storage_url)

    def create_asset_access_link(
        self,
        asset_id: str,
        *,
        expires_in_seconds: int | None = None,
    ) -> AssetAccessLink | None:
        asset = self.assets.get_asset(asset_id)
        if asset is None:
            return None
        # V8.4: the caller may ask for a TTL, not dictate one. Without a ceiling a
        # single request mints a link that outlives the deployment that signed it.
        ttl = expires_in_seconds or settings.asset_link_default_ttl_seconds
        if ttl <= 0:
            ttl = settings.asset_link_default_ttl_seconds
        ttl = min(ttl, settings.asset_link_max_ttl_seconds)
        if not asset_link_secret_is_secure(settings.asset_link_secret, app_env=settings.app_env):
            # Refusing beats warning: a warning printed by a call that succeeded is
            # read by nobody, and a link signed with the shipped default can be
            # forged by anyone who has read the source.
            raise ApiError(
                ErrorCode.INSECURE_SECRET_CONFIGURATION,
                (
                    "asset link signing is still using the built-in development "
                    "secret; set ASSET_LINK_SECRET before serving "
                    f"{settings.app_env}"
                ),
                details={"app_env": settings.app_env},
            )
        link = self.storage.temporary_url(
            asset_id=asset.asset_id,
            storage_url=asset.storage_url,
            expires_in_seconds=ttl,
        )
        return AssetAccessLink(asset_id=asset.asset_id, url=link.url, expires_at=link.expires_at)

    def resolve_asset_access_link(
        self,
        asset_id: str,
        *,
        expires: int,
        signature: str,
    ) -> tuple[Asset, bytes] | None:
        asset = self.assets.get_asset(asset_id)
        if asset is None:
            return None
        if not self.storage.verify(
            asset_id=asset.asset_id,
            storage_url=asset.storage_url,
            expires=expires,
            signature=signature,
        ):
            return None
        return asset, self.storage.read_bytes(asset.storage_url)

    def record_event(self, run_id: str, payload: EventCreate) -> EventLog | None:
        if self.runs.get_run(run_id) is None:
            return None
        event = self.events.append_event(
            run_id=run_id,
            event_type=payload.event_type,
            source_type=payload.source_type,
            source_id=payload.source_id,
            stage_key=payload.stage_key,
            stage_run_id=payload.stage_run_id,
            tool_invocation_id=payload.tool_invocation_id,
            severity=payload.severity,
            message=payload.message,
            payload=payload.payload,
        )
        self.session.commit()
        return event

    def list_events(
        self,
        run_id: str,
        *,
        source_type: EventSourceType | None = None,
        stage_key: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[EventLog] | None:
        if self.runs.get_run(run_id) is None:
            return None
        return self.events.list_events(
            run_id,
            source_type=source_type,
            stage_key=stage_key,
            event_type=event_type,
            limit=limit,
        )

    def _background_job_payload_for_manifest_step(
        self,
        *,
        run: ProductionRun,
        task: dict,
        step: dict,
        manifest_step_key: str,
    ) -> BackgroundJobCreate | None:
        capability = step.get("capability")
        tool_key = step.get("tool_key")
        if capability == "avatar_video" and tool_key == "dashscope_avatar_video":
            return self._avatar_job_payload_for_manifest_step(
                run=run,
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        if capability == "subtitle_generation" and tool_key == "local_srt_subtitles":
            if self._latest_asset_by_type(run.run_id, "subtitle_file") is not None:
                return None
            if "spoken_script" not in self.artifacts.current_payloads(run.run_id):
                return None
            return self._stage_tool_step_job_payload(
                run=run,
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        if capability == "media_analysis" and tool_key == "ffmpeg_media_probe":
            current_report = self._current_artifact_by_type(run.run_id, "media_analysis_report")
            if current_report is not None and current_report.payload.get("tool_invocation_id"):
                return None
            if not self._source_media_entries_for_analysis(
                run.run_id,
                source_asset_ids=[],
                source_artifact_type="source_asset_manifest",
            ):
                return None
            return self._stage_tool_step_job_payload(
                run=run,
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        if capability == "video_composition" and tool_key == "ffmpeg_compose":
            current_render_summary = self._current_artifact_by_type(
                run.run_id,
                "render_summary",
            )
            if (
                current_render_summary is not None
                and current_render_summary.payload.get("render_asset_id")
            ):
                return None
            if self._latest_asset_by_type(run.run_id, "avatar_video_clip") is None:
                return None
            return self._stage_tool_step_job_payload(
                run=run,
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        if capability == "storage" and tool_key == "local_object_storage":
            current_publish_package = self._current_artifact_by_type(
                run.run_id,
                "publish_package",
            )
            if (
                current_publish_package is not None
                and (
                    current_publish_package.payload.get("render_asset_id")
                    or current_publish_package.payload.get("final_video_url")
                )
            ):
                return None
            if self._latest_asset_by_type(run.run_id, "rendered_video") is None:
                return None
            return self._stage_tool_step_job_payload(
                run=run,
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        return None

    def _avatar_job_payload_for_manifest_step(
        self,
        *,
        run: ProductionRun,
        task: dict,
        step: dict,
        manifest_step_key: str,
    ) -> BackgroundJobCreate | None:
        if self._latest_asset_by_type(run.run_id, "avatar_video_clip") is not None:
            return None
        person_asset = self._latest_person_image_asset(run.run_id)
        audio_asset = self._latest_asset_by_type(run.run_id, "tts_audio")
        if person_asset is None or audio_asset is None:
            return None
        stage_run_id = self._stage_run_id(run.run_id, task["stage_key"])
        payload = AvatarVideoFromAssetsRequest(
            stage_run_id=stage_run_id,
            person_image_asset_id=person_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url=_blank_to_none(settings.public_base_url),
            auto_archive=True,
            auto_compose=False,
            provider_options={
                "manifest_step_key": manifest_step_key,
                "tool_step_id": step.get("step_id"),
            },
        ).model_dump()
        payload.update(
            _manifest_job_payload(
                task=task,
                step=step,
                manifest_step_key=manifest_step_key,
            )
        )
        payload["resolved_dispatch_inputs"] = {
            "person_image_asset_id": person_asset.asset_id,
            "audio_asset_id": audio_asset.asset_id,
        }
        return BackgroundJobCreate(
            stage_run_id=stage_run_id,
            job_type="avatar_from_assets",
            request_payload=payload,
            priority=_priority_for_job("avatar_from_assets", payload),
        )

    def _stage_tool_step_job_payload(
        self,
        *,
        run: ProductionRun,
        task: dict,
        step: dict,
        manifest_step_key: str,
    ) -> BackgroundJobCreate:
        stage_run_id = self._stage_run_id(run.run_id, task["stage_key"])
        step_payload = _manifest_job_payload(
            task=task,
            step=step,
            manifest_step_key=manifest_step_key,
        )
        return BackgroundJobCreate(
            stage_run_id=stage_run_id,
            job_type="stage_tool_step",
            request_payload=step_payload,
            priority=_priority_for_job("stage_tool_step", step_payload),
        )

    def _manifest_step_dispatch_preview(
        self,
        *,
        run: ProductionRun,
        manifest_artifact: Artifact,
        task: dict,
        step: dict,
        manifest_step_key: str,
        stage_statuses: dict[str, str],
        job: BackgroundJob | None,
    ) -> dict:
        base = {
            "manifest_artifact_id": manifest_artifact.artifact_id,
            "manifest_step_key": manifest_step_key,
            "stage_task_id": task.get("task_id"),
            "stage_key": task.get("stage_key"),
            "stage_status": stage_statuses.get(task.get("stage_key")),
            "tool_step_id": step.get("step_id"),
            "capability": step.get("capability"),
            "tool_key": step.get("tool_key"),
            "provider": step.get("provider"),
            "decision_id": step.get("decision_id"),
            "auto_dispatch": step.get("auto_dispatch") is True,
            "dispatchable": False,
            "missing_inputs": [],
            "job": _background_job_preview(job),
        }
        if job is not None:
            return {
                **base,
                "status": job.status.value,
                "reason": job.error_message or "该步骤已有后台任务",
            }
        if manifest_artifact.payload.get("source", {}).get("can_execute") is not True:
            return {
                **base,
                "status": "blocked",
                "reason": "执行方案已阻塞",
            }
        if not _is_dispatchable_manifest_step(step):
            return {
                **base,
                "status": "not_dispatchable",
                "reason": "该 manifest step 不是 planned auto-dispatch 步骤",
            }
        if not _is_stage_ready_for_manifest_dispatch(task, stage_statuses):
            return {
                **base,
                "status": "waiting_stage",
                "reason": f"等待阶段 {task.get('stage_key')} 进入可执行状态",
            }

        capability = step.get("capability")
        if capability == "avatar_video":
            return self._avatar_dispatch_preview(run=run, base=base)
        if capability == "subtitle_generation":
            return self._subtitle_dispatch_preview(run=run, base=base)
        if capability == "media_analysis":
            return self._media_analysis_dispatch_preview(run=run, base=base)
        if capability == "video_composition":
            return self._compose_dispatch_preview(run=run, base=base)
        if capability == "storage":
            return self._storage_dispatch_preview(run=run, base=base)
        return {
            **base,
            "status": "unsupported",
            "reason": f"暂不支持自动派发能力：{capability}",
        }

    def _avatar_dispatch_preview(self, *, run: ProductionRun, base: dict) -> dict:
        avatar_asset = self._latest_asset_by_type(run.run_id, "avatar_video_clip")
        if avatar_asset is not None:
            return {
                **base,
                "status": "existing_output",
                "reason": "已有数字人视频资产",
                "output_asset_ids": [avatar_asset.asset_id],
            }
        missing_inputs = []
        resolved_inputs = {}
        person_asset = self._latest_person_image_asset(run.run_id)
        audio_asset = self._latest_asset_by_type(run.run_id, "tts_audio")
        if person_asset is None:
            missing_inputs.append("person_image")
        else:
            resolved_inputs["person_image_asset_id"] = person_asset.asset_id
        if audio_asset is None:
            missing_inputs.append("tts_audio")
        else:
            resolved_inputs["audio_asset_id"] = audio_asset.asset_id
        if missing_inputs:
            return {
                **base,
                "status": "waiting_input",
                "reason": "等待人物图和口播音频",
                "missing_inputs": missing_inputs,
                "resolved_inputs": resolved_inputs,
            }
        return {
            **base,
            "status": "ready",
            "reason": "人物图和口播音频已就绪",
            "dispatchable": True,
            "resolved_inputs": resolved_inputs,
        }

    def _subtitle_dispatch_preview(self, *, run: ProductionRun, base: dict) -> dict:
        subtitle_asset = self._latest_asset_by_type(run.run_id, "subtitle_file")
        if subtitle_asset is not None:
            return {
                **base,
                "status": "existing_output",
                "reason": "已有字幕资产",
                "output_asset_ids": [subtitle_asset.asset_id],
            }
        if "spoken_script" not in self.artifacts.current_payloads(run.run_id):
            return {
                **base,
                "status": "waiting_input",
                "reason": "等待 spoken_script artifact",
                "missing_inputs": ["spoken_script"],
            }
        return {
            **base,
            "status": "ready",
            "reason": "口播脚本已就绪",
            "dispatchable": True,
        }

    def _media_analysis_dispatch_preview(self, *, run: ProductionRun, base: dict) -> dict:
        current_report = self._current_artifact_by_type(run.run_id, "media_analysis_report")
        if current_report is not None and current_report.payload.get("tool_invocation_id"):
            return {
                **base,
                "status": "existing_output",
                "reason": "已有真实素材分析报告",
                "output_artifact_ids": [current_report.artifact_id],
            }
        source_entries = self._source_media_entries_for_analysis(
            run.run_id,
            source_asset_ids=[],
            source_artifact_type="source_asset_manifest",
        )
        source_asset_ids = [
            entry.get("asset_id")
            for entry in source_entries
            if entry.get("asset_id")
        ]
        if not source_asset_ids:
            return {
                **base,
                "status": "waiting_input",
                "reason": "等待源素材",
                "missing_inputs": ["source_media"],
            }
        return {
            **base,
            "status": "ready",
            "reason": "源素材已就绪",
            "dispatchable": True,
            "resolved_inputs": {"source_asset_ids": source_asset_ids},
        }

    def _compose_dispatch_preview(self, *, run: ProductionRun, base: dict) -> dict:
        current_render_summary = self._current_artifact_by_type(run.run_id, "render_summary")
        if current_render_summary is not None and current_render_summary.payload.get("render_asset_id"):
            return {
                **base,
                "status": "existing_output",
                "reason": "已有真实 render_summary",
                "output_artifact_ids": [current_render_summary.artifact_id],
            }
        avatar_asset = self._latest_asset_by_type(run.run_id, "avatar_video_clip")
        if avatar_asset is None:
            return {
                **base,
                "status": "waiting_input",
                "reason": "等待数字人视频",
                "missing_inputs": ["avatar_video_clip"],
            }
        return {
            **base,
            "status": "ready",
            "reason": "数字人视频已就绪",
            "dispatchable": True,
            "resolved_inputs": {"avatar_video_asset_id": avatar_asset.asset_id},
        }

    def _storage_dispatch_preview(self, *, run: ProductionRun, base: dict) -> dict:
        current_publish_package = self._current_artifact_by_type(run.run_id, "publish_package")
        if (
            current_publish_package is not None
            and (
                current_publish_package.payload.get("render_asset_id")
                or current_publish_package.payload.get("final_video_url")
            )
        ):
            return {
                **base,
                "status": "existing_output",
                "reason": "已有发布包",
                "output_artifact_ids": [current_publish_package.artifact_id],
            }
        render_asset = self._latest_asset_by_type(run.run_id, "rendered_video")
        if render_asset is None:
            return {
                **base,
                "status": "waiting_input",
                "reason": "等待渲染成片",
                "missing_inputs": ["rendered_video"],
            }
        return {
            **base,
            "status": "ready",
            "reason": "渲染成片已就绪",
            "dispatchable": True,
            "resolved_inputs": {"render_asset_id": render_asset.asset_id},
        }

    def _advance_until_gate(self, record: ProductionRunRecord) -> None:
        pipeline = self._pipeline_for_run_record(record)
        stage_definitions = {stage.stage_key: stage for stage in pipeline.stages}

        while record.current_stage_key is not None:
            ordered_stages = self._ordered_stage_records(record)
            stage_index = next(
                index
                for index, candidate in enumerate(ordered_stages)
                if candidate.stage_key == record.current_stage_key
            )
            stage_run = ordered_stages[stage_index]
            stage_definition = stage_definitions[stage_run.stage_key]
            stage_run.status = StageStatus.RUNNING.value
            self._clear_stage_invalidation(record, stage_run)
            self._append_stage_event(
                record,
                stage_run,
                "stage.started",
                message="Stage started.",
                payload={"status": stage_run.status},
            )

            agent_output = self.agent_runner.run_stage(
                AgentInput(
                    run_id=record.id,
                    pipeline_key=record.pipeline_key,
                    stage_key=stage_run.stage_key,
                    stage_definition=stage_definition.model_dump(),
                    current_artifacts=self.artifacts.current_payloads(record.id),
                    available_tools=[
                        tool.model_dump() for tool in self.tool_registry.list_tools()
                    ],
                    user_preferences=record.input_payload or {},
                    cost_policy={},
                )
            )
            if agent_output.artifact_type and agent_output.artifact_payload is not None:
                artifact = self.artifacts.append_artifact(
                    run_id=record.id,
                    stage_run_id=stage_run.id,
                    stage_key=stage_run.stage_key,
                    artifact_type=agent_output.artifact_type,
                    payload=agent_output.artifact_payload,
                )
                self._append_stage_event(
                    record,
                    stage_run,
                    "artifact.created",
                    message="Stage artifact created.",
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "artifact_type": artifact.artifact_type,
                        "version": artifact.version,
                    },
                )

            if self._requires_review(stage_run.requires_human_review, stage_definition):
                stage_run.status = StageStatus.WAITING_REVIEW.value
                record.status = RunStatus.WAITING_REVIEW.value
                self._append_stage_event(
                    record,
                    stage_run,
                    "stage.waiting_review",
                    message="Stage is waiting for review.",
                    payload={"status": stage_run.status},
                )
                self._append_run_event(
                    record,
                    "run.waiting_review",
                    message="Run is waiting for review.",
                    payload={"status": record.status, "stage_key": stage_run.stage_key},
                )
                self._open_review_request_guarded(record, stage_run)
                return

            stage_run.status = StageStatus.COMPLETED.value
            self._append_stage_event(
                record,
                stage_run,
                "stage.completed",
                message="Stage completed.",
                payload={"status": stage_run.status},
            )
            if stage_index + 1 >= len(ordered_stages):
                record.current_stage_key = None
                record.status = RunStatus.COMPLETED.value
                self._append_run_event(
                    record,
                    "run.completed",
                    message="Run completed.",
                    payload={"status": record.status},
                )
                return
            record.current_stage_key = ordered_stages[stage_index + 1].stage_key
            record.status = RunStatus.RUNNING.value

    def _get_run_record(self, run_id: str) -> ProductionRunRecord | None:
        statement = (
            select(ProductionRunRecord)
            .where(ProductionRunRecord.id == run_id)
            .options(selectinload(ProductionRunRecord.stages))
        )
        return self.session.scalar(statement)

    def _project_run_records(self, project_id: str) -> list[ProductionRunRecord]:
        statement = (
            select(ProductionRunRecord)
            .where(ProductionRunRecord.project_id == project_id)
            .options(selectinload(ProductionRunRecord.stages))
            .order_by(ProductionRunRecord.updated_at.desc(), ProductionRunRecord.id.desc())
        )
        return list(self.session.scalars(statement))

    def _filter_dashboard_runs(
        self,
        records: list[ProductionRunRecord],
        *,
        status: RunStatus | None,
        search: str | None,
    ) -> list[ProductionRunRecord]:
        filtered = [record for record in records if status is None or record.status == status.value]
        query = (search or "").strip().lower()
        if not query:
            return filtered
        return [record for record in filtered if _dashboard_run_matches(record, query)]

    def _dashboard_jobs_by_run(
        self,
        run_ids: list[str],
    ) -> dict[str, list[BackgroundJobRecord]]:
        if not run_ids:
            return {}
        statement = (
            select(BackgroundJobRecord)
            .where(BackgroundJobRecord.run_id.in_(run_ids))
            .order_by(BackgroundJobRecord.updated_at.desc(), BackgroundJobRecord.id)
        )
        jobs_by_run: dict[str, list[BackgroundJobRecord]] = defaultdict(list)
        for job in self.session.scalars(statement):
            jobs_by_run[job.run_id].append(job)
        return jobs_by_run

    def _dashboard_artifact_counts(self, run_ids: list[str]) -> dict[str, int]:
        if not run_ids:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for record in self.session.scalars(
            select(ArtifactRecord).where(ArtifactRecord.run_id.in_(run_ids))
        ):
            counts[record.run_id] += 1
        return counts

    def _dashboard_asset_counts(self, run_ids: list[str]) -> dict[str, int]:
        if not run_ids:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for record in self.session.scalars(select(AssetRecord).where(AssetRecord.run_id.in_(run_ids))):
            counts[record.run_id] += 1
        return counts

    def _run_board_artifact_records(self, run_id: str) -> list[ArtifactRecord]:
        statement = (
            select(ArtifactRecord)
            .outerjoin(StageRunRecord, ArtifactRecord.stage_run_id == StageRunRecord.id)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(
                StageRunRecord.stage_order,
                ArtifactRecord.created_at,
                ArtifactRecord.version,
                ArtifactRecord.id,
            )
        )
        return list(self.session.scalars(statement))

    def _run_board_asset_records(self, run_id: str) -> list[AssetRecord]:
        statement = (
            select(AssetRecord)
            .where(AssetRecord.run_id == run_id)
            .order_by(AssetRecord.created_at, AssetRecord.id)
        )
        return list(self.session.scalars(statement))

    def observability_gauges(self) -> dict[str, dict[str, int]]:
        """Scrape-time counters for ``/metrics`` (V8.2).

        Plain ``Counter`` over full-row scans, like every other aggregate in
        this store: the tables are indexed and a deployment that outgrows
        this is the one that also outgrew Python-side dashboards.
        """
        jobs = list(self.session.scalars(select(BackgroundJobRecord)))
        runs = list(self.session.scalars(select(ProductionRunRecord)))
        return {
            "jobs_by_status": dict(Counter(job.status for job in jobs)),
            "jobs_by_queue": dict(Counter(job.queue for job in jobs)),
            "runs_by_status": dict(Counter(run.status for run in runs)),
        }

    def project_observability(self, project_id: str) -> ProjectObservability | None:
        """Cost / latency / error picture of one project in one response (V8.2)."""
        project = self.projects.get_project(project_id)
        if project is None:
            return None
        run_records = self._project_run_records(project_id)
        run_ids = [record.id for record in run_records]
        if run_ids:
            entries = list(
                self.session.scalars(
                    select(UsageLedgerRecord).where(UsageLedgerRecord.run_id.in_(run_ids))
                )
            )
            jobs = list(
                self.session.scalars(
                    select(BackgroundJobRecord).where(
                        BackgroundJobRecord.run_id.in_(run_ids)
                    )
                )
            )
            error_events = list(
                self.session.scalars(
                    select(EventLogRecord)
                    .where(EventLogRecord.run_id.in_(run_ids))
                    .where(EventLogRecord.severity == EventSeverity.ERROR.value)
                    .order_by(EventLogRecord.created_at.desc(), EventLogRecord.id.desc())
                )
            )
        else:
            entries, jobs, error_events = [], [], []
        return ProjectObservability(
            project_id=project_id,
            generated_at=_job_now(),
            cost=_observability_cost(entries),
            latency=_observability_latency(entries),
            jobs=ObservabilityJobs(
                by_status=dict(Counter(job.status for job in jobs)),
                by_queue=dict(Counter(job.queue for job in jobs)),
                dead_letter=sum(1 for job in jobs if job.status == "dead"),
                top_error_codes=dict(
                    Counter(
                        job.error_code
                        for job in jobs
                        if job.error_code
                    ).most_common(5)
                ),
            ),
            events=ObservabilityEvents(
                errors_by_type=dict(Counter(event.event_type for event in error_events)),
                recent_errors=[
                    ObservabilityRecentError(
                        event_type=event.event_type,
                        message=event.message,
                        run_id=event.run_id,
                        created_at=event.created_at,
                    )
                    for event in error_events[:10]
                ],
            ),
        )

    def _dashboard_metrics(
        self,
        run_records: list[ProductionRunRecord],
        *,
        jobs_by_run: dict[str, list[BackgroundJobRecord]],
        review_queue: list[DashboardReviewItem],
        invalidated_stage_count: int = 0,
    ) -> ProjectDashboardMetrics:
        run_status_counts = Counter(record.status for record in run_records)
        job_status_counts = Counter(
            job.status for jobs in jobs_by_run.values() for job in jobs
        )
        active_statuses = {
            RunStatus.DRAFT.value,
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
            RunStatus.WAITING_REVIEW.value,
            RunStatus.PAUSED.value,
        }
        return ProjectDashboardMetrics(
            total_runs=len(run_records),
            active_runs=sum(run_status_counts[status] for status in active_statuses),
            completed_runs=run_status_counts[RunStatus.COMPLETED.value],
            failed_runs=run_status_counts[RunStatus.FAILED.value],
            cancelled_runs=run_status_counts[RunStatus.CANCELLED.value],
            waiting_review_runs=run_status_counts[RunStatus.WAITING_REVIEW.value],
            pending_reviews=len(review_queue),
            overdue_reviews=sum(1 for item in review_queue if item.overdue),
            invalidated_stages=invalidated_stage_count,
            queued_jobs=job_status_counts[BackgroundJobStatus.QUEUED.value],
            running_jobs=job_status_counts[BackgroundJobStatus.RUNNING.value],
            waiting_provider_jobs=job_status_counts[BackgroundJobStatus.WAITING_PROVIDER.value],
            failed_jobs=job_status_counts[BackgroundJobStatus.FAILED.value],
        )

    def _dashboard_run_summary(
        self,
        record: ProductionRunRecord,
        *,
        jobs: list[BackgroundJobRecord],
        artifact_count: int,
        asset_count: int,
    ) -> DashboardRunSummary:
        input_payload = record.input_payload or {}
        stage_counts = Counter(stage.status for stage in record.stages)
        job_counts = Counter(job.status for job in jobs)
        return DashboardRunSummary(
            run_id=record.id,
            project_id=record.project_id,
            pipeline_key=record.pipeline_key,
            status=RunStatus(record.status),
            current_stage_key=record.current_stage_key,
            target_platform=record.target_platform,
            target_duration_seconds=record.target_duration_seconds,
            brand_name=_dashboard_payload_text(input_payload, "brand_name"),
            product_name=_dashboard_payload_text(input_payload, "product_name"),
            script_preview=_dashboard_script_preview(input_payload.get("raw_script")),
            stage_counts=dict(stage_counts),
            job_counts=dict(job_counts),
            artifact_count=artifact_count,
            asset_count=asset_count,
            failed_job_count=job_counts[BackgroundJobStatus.FAILED.value],
            waiting_review_stage_count=stage_counts[StageStatus.WAITING_REVIEW.value],
            created_at=record.created_at,
            updated_at=record.updated_at,
            workspace_url=f"/workspace?run_id={record.id}",
        )

    def _ordered_stage_records(self, record: ProductionRunRecord) -> list[StageRunRecord]:
        return sorted(record.stages, key=lambda stage: stage.stage_order)

    def _requires_review(self, stored_value: bool, stage_definition: StageDefinition) -> bool:
        return bool(stored_value or stage_definition.requires_human_review)

    def _current_artifact_by_type(self, run_id: str, artifact_type: str) -> Artifact | None:
        return next(
            (
                artifact
                for artifact in self.artifacts.list_artifacts(run_id, current_only=True)
                if artifact.artifact_type == artifact_type
            ),
            None,
        )

    def _artifact_for_run(
        self,
        run_id: str,
        artifact_id: str | None,
        *,
        artifact_type: str | None = None,
    ) -> Artifact | None:
        if artifact_id is None:
            return None
        artifact = next(
            (
                candidate
                for candidate in self.artifacts.list_artifacts(run_id, current_only=False)
                if candidate.artifact_id == artifact_id
            ),
            None,
        )
        if artifact is None:
            return None
        if artifact_type is not None and artifact.artifact_type != artifact_type:
            return None
        return artifact

    def _quality_render_summary(
        self,
        run_id: str,
        request: QualityGateRequest,
    ) -> Artifact | None:
        if request.render_summary_artifact_id:
            artifact = self._artifact_for_run(
                run_id,
                request.render_summary_artifact_id,
                artifact_type="render_summary",
            )
            if artifact is None:
                raise ValueError("render_summary_artifact_id does not belong to this run")
            return artifact
        return self._current_artifact_by_type(run_id, "render_summary")

    def _quality_render_asset(
        self,
        run_id: str,
        request: QualityGateRequest,
        render_summary: Artifact | None,
    ) -> Asset | None:
        if request.render_asset_id:
            asset = self._asset_for_run(run_id, request.render_asset_id)
            if asset is None or asset.asset_type != "rendered_video":
                raise ValueError("render_asset_id does not belong to this run")
            return asset
        if render_summary is not None:
            asset_id = render_summary.payload.get("render_asset_id")
            asset = self._asset_for_run(run_id, asset_id)
            if asset is not None:
                return asset
        return self._latest_asset_by_type(run_id, "rendered_video")

    def _quality_composition_graph(
        self,
        run_id: str,
        request: QualityGateRequest,
        render_summary: Artifact | None,
    ) -> Artifact | None:
        if request.composition_graph_artifact_id:
            artifact = self._artifact_for_run(
                run_id,
                request.composition_graph_artifact_id,
                artifact_type="composition_graph",
            )
            if artifact is None:
                raise ValueError("composition_graph_artifact_id does not belong to this run")
            return artifact
        if render_summary is not None:
            artifact_id = (render_summary.payload.get("inputs") or {}).get(
                "composition_graph_artifact_id"
            )
            artifact = self._artifact_for_run(
                run_id,
                artifact_id,
                artifact_type="composition_graph",
            )
            if artifact is not None:
                return artifact
        return self._current_artifact_by_type(run_id, "composition_graph")

    def _composition_graph_for_preview(
        self,
        run_id: str,
        request: HtmlCanvasPreviewRequest,
    ) -> Artifact | None:
        if request.composition_graph_artifact_id:
            artifact = next(
                (
                    candidate
                    for candidate in self.artifacts.list_artifacts(run_id, current_only=False)
                    if candidate.artifact_id == request.composition_graph_artifact_id
                ),
                None,
            )
            if artifact is None or artifact.artifact_type != "composition_graph":
                raise ValueError("composition_graph_artifact_id does not belong to this run")
            return artifact

        current_graph = self._current_artifact_by_type(run_id, "composition_graph")
        if current_graph is not None:
            return current_graph

        graph_request = request.graph_request or CompositionGraphBuildRequest(
            stage_run_id=request.stage_run_id
        )
        if request.stage_run_id is not None and graph_request.stage_run_id is None:
            graph_request = graph_request.model_copy(update={"stage_run_id": request.stage_run_id})
        return self.build_composition_graph(run_id, graph_request)

    def _build_preflight_report(self, run: ProductionRun) -> dict:
        pipeline = self._pipeline_for_run(run)
        return build_preflight_report(
            pipeline=pipeline,
            tools=self.tool_registry.list_tools(),
            run_input=run.input_payload,
        )

    def _pipeline_for_run(self, run: ProductionRun) -> PipelineDefinition:
        if run.pipeline_definition_snapshot:
            return PipelineDefinition.model_validate(run.pipeline_definition_snapshot)
        return self.pipeline_loader.get_pipeline(run.pipeline_key)

    def _pipeline_for_run_record(self, record: ProductionRunRecord) -> PipelineDefinition:
        if record.pipeline_definition_snapshot:
            return PipelineDefinition.model_validate(record.pipeline_definition_snapshot)
        return self.pipeline_loader.get_pipeline(record.pipeline_key)

    def _asset_for_run(self, run_id: str, asset_id: str | None) -> Asset | None:
        if asset_id is None:
            return None
        asset = self.assets.get_asset(asset_id)
        if asset is None or asset.run_id != run_id:
            return None
        return asset

    def _edit_source_assets(self, run_id: str, payload: EditToolRequest) -> list[Asset]:
        if payload.operation == EditOperation.CONCAT:
            if len(payload.source_asset_ids) < 2:
                raise ValueError("source_asset_ids must include at least two assets for concat")
            assets = [self._asset_for_run(run_id, asset_id) for asset_id in payload.source_asset_ids]
        else:
            source_asset_id = payload.source_asset_id or (
                payload.source_asset_ids[0] if payload.source_asset_ids else None
            )
            if source_asset_id is None:
                latest = self._latest_asset_by_type(run_id, "rendered_video") or self._latest_asset_by_type(
                    run_id,
                    "edited_video",
                )
                assets = [latest]
            else:
                assets = [self._asset_for_run(run_id, source_asset_id)]
        if any(asset is None for asset in assets):
            raise ValueError("source asset does not belong to this run")
        resolved_assets = [asset for asset in assets if asset is not None]
        for asset in resolved_assets:
            if not _is_video_asset(asset):
                raise ValueError("edit tools require video source assets")
        return resolved_assets

    def _edit_subtitle_asset(self, run_id: str, payload: EditToolRequest) -> Asset | None:
        if payload.operation != EditOperation.SUBTITLE_BURN_IN:
            return None
        asset = self._asset_for_run(run_id, payload.subtitle_asset_id)
        if asset is None:
            asset = self._latest_asset_by_type(run_id, "subtitle_file")
        if asset is None or asset.asset_type != "subtitle_file":
            raise ValueError("subtitle_asset_id is required for subtitle_burn_in")
        return asset

    def _run_edit_requests(
        self,
        *,
        run: ProductionRun,
        payload: EditToolRequest,
        source_paths: list[Path],
        subtitle_path: Path | None,
        temp_root: Path,
    ) -> list[FfmpegEditResult]:
        if payload.operation == EditOperation.EXPORT_VERSIONS:
            if not payload.variants:
                raise ValueError("variants are required for export_versions")
            results = []
            for variant in payload.variants:
                width, height, fps, profile_key = self._edit_dimensions(
                    run=run,
                    payload=payload,
                    variant=variant.model_dump(),
                    use_profile=True,
                )
                operation = "watermark" if (variant.watermark_text or payload.watermark_text) else "auto_reframe"
                result = self.edit_worker.run(
                    FfmpegEditRequest(
                        operation=operation,
                        source_paths=[source_paths[0]],
                        output_path=temp_root / variant.filename,
                        width=width,
                        height=height,
                        fps=fps,
                        fit=variant.fit or payload.fit,
                        watermark_text=variant.watermark_text or payload.watermark_text,
                        watermark_position=payload.watermark_position,
                        watermark_opacity=payload.watermark_opacity,
                        preset=payload.preset,
                        crf=payload.crf,
                        audio_bitrate=payload.audio_bitrate,
                    )
                )
                result.metadata.update(
                    {
                        "variant_label": variant.label,
                        "render_profile_key": profile_key,
                        **variant.asset_metadata,
                    }
                )
                results.append(result)
            return results

        if payload.operation == EditOperation.WATERMARK and not payload.watermark_text:
            raise ValueError("watermark_text is required for watermark")
        width, height, fps, profile_key = self._edit_dimensions(
            run=run,
            payload=payload,
            variant=None,
            use_profile=payload.operation == EditOperation.AUTO_REFRAME
            or bool(payload.render_profile_key),
        )
        filename = payload.filename or _default_edit_filename(payload.operation)
        result = self.edit_worker.run(
            FfmpegEditRequest(
                operation=payload.operation.value,
                source_paths=source_paths
                if payload.operation == EditOperation.CONCAT
                else [source_paths[0]],
                output_path=temp_root / filename,
                subtitle_path=subtitle_path,
                start_seconds=payload.start_seconds,
                duration_seconds=payload.duration_seconds,
                cover_time_seconds=payload.cover_time_seconds,
                width=width,
                height=height,
                fps=fps,
                fit=payload.fit,
                watermark_text=payload.watermark_text,
                watermark_position=payload.watermark_position,
                watermark_opacity=payload.watermark_opacity,
                preset=payload.preset,
                crf=payload.crf,
                audio_bitrate=payload.audio_bitrate,
            )
        )
        result.metadata.update({"render_profile_key": profile_key})
        return [result]

    def _edit_dimensions(
        self,
        *,
        run: ProductionRun,
        payload: EditToolRequest,
        variant: dict[str, Any] | None,
        use_profile: bool,
    ) -> tuple[int | None, int | None, int | None, str | None]:
        variant = variant or {}
        profile_key = variant.get("render_profile_key") or payload.render_profile_key
        profile = resolve_render_profile(
            target_platform=run.input_payload.get("target_platform"),
            aspect_ratio=run.input_payload.get("aspect_ratio"),
            style_snapshot=run.input_payload.get("style_preset_snapshot") or {},
            render_profile_key=profile_key,
        )
        should_use_profile = use_profile or bool(profile_key)
        width = variant.get("width") or payload.width or (profile["width"] if should_use_profile else None)
        height = variant.get("height") or payload.height or (
            profile["height"] if should_use_profile else None
        )
        fps = variant.get("fps") or payload.fps or (profile["fps"] if should_use_profile else None)
        return width, height, fps, profile.get("profile_key") if should_use_profile else None

    def _resolve_provider_media_url(
        self,
        *,
        run_id: str,
        direct_url: str | None,
        asset_id: str | None,
        expires_in_seconds: int,
        public_base_url: str | None,
        label: str,
    ) -> str:
        if direct_url:
            return direct_url
        if not asset_id:
            raise ValueError(f"{label} requires either a direct URL or an asset id")
        asset = self._asset_for_run(run_id, asset_id)
        if asset is None:
            raise ValueError(f"{label} asset does not belong to this run")
        if label == "audio":
            source_url = asset.asset_metadata.get("source_audio_url")
        elif label == "person_image":
            source_url = asset.asset_metadata.get("source_image_url")
        else:
            source_url = None
        if isinstance(source_url, str) and source_url:
            return source_url
        access_link = self.create_asset_access_link(
            asset.asset_id,
            expires_in_seconds=expires_in_seconds,
        )
        if access_link is None:
            raise ValueError(f"{label} asset access link could not be created")
        base_url = (public_base_url or settings.public_base_url or "").strip()
        if not base_url:
            raise ValueError(
                f"{label} asset requires PUBLIC_BASE_URL or payload.public_base_url"
            )
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", access_link.url.lstrip("/"))

    def _process_stage_tool_step_background_job(
        self,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> BackgroundJob:
        if job.status not in {
            BackgroundJobStatus.QUEUED,
            BackgroundJobStatus.RUNNING,
            BackgroundJobStatus.FAILED,
        }:
            return job
        running = self.background_jobs.update_job(
            job.job_id,
            status=BackgroundJobStatus.RUNNING,
            progress=10,
            locked_by=worker_id,
            started_at=_job_now(),
            lease_expires_at=_job_now() + timedelta(seconds=_JOB_LEASE_SECONDS),
        )
        if running is None:
            raise ValueError("background job disappeared before stage tool step")

        capability = running.request_payload.get("capability")
        if capability == "subtitle_generation":
            result_payload = self._run_subtitle_stage_tool_step(running)
        elif capability == "media_analysis":
            result_payload = self._run_media_analysis_stage_tool_step(running)
        elif capability == "video_composition":
            result_payload = self._run_compose_stage_tool_step(running)
        elif capability == "storage":
            result_payload = self._run_export_stage_tool_step(running)
        else:
            raise ValueError(f"unsupported stage tool step capability: {capability}")

        completed_payload = {**running.result_payload, **result_payload}
        updated = self.background_jobs.update_job(
            running.job_id,
            status=BackgroundJobStatus.COMPLETED,
            progress=100,
            result_payload=completed_payload,
        )
        if updated is None:
            raise ValueError("background job disappeared after stage tool step")
        self._record_background_job_usage(updated, operation="job_completed")
        self.events.append_event(
            run_id=updated.run_id,
            event_type="job.completed",
            source_type=EventSourceType.SYSTEM,
            source_id=updated.job_id,
            stage_key=running.request_payload.get("stage_key"),
            stage_run_id=updated.stage_run_id,
            message="Stage execution tool step completed.",
            payload=completed_payload,
        )
        self.session.commit()
        return updated

    def _run_subtitle_stage_tool_step(self, job: BackgroundJob) -> dict:
        result = self.generate_subtitles(
            job.run_id,
            SubtitleGenerateRequest(
                stage_run_id=job.stage_run_id,
                filename=f"{job.job_id}-subtitles.srt",
                asset_metadata=_stage_tool_asset_metadata(job),
            ),
        )
        if result is None:
            raise ValueError("run not found while generating subtitles")
        invocation, asset = result
        return {
            "tool_invocation_id": invocation.invocation_id,
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type,
            "storage_url": asset.storage_url,
        }

    def _run_media_analysis_stage_tool_step(self, job: BackgroundJob) -> dict:
        result = self.analyze_source_media(
            job.run_id,
            MediaAnalysisRequest(
                stage_run_id=job.stage_run_id,
                max_frames_per_video=int(
                    job.request_payload.get("max_frames_per_video") or 5
                ),
                provider_options={
                    "manifest_step_key": job.request_payload.get("manifest_step_key"),
                    "tool_step_id": job.request_payload.get("tool_step_id"),
                },
            ),
        )
        if result is None:
            raise ValueError("run not found while analyzing source media")
        return {
            "media_analysis_report_artifact_id": result.artifact.artifact_id,
            "video_analysis_brief_artifact_id": result.video_analysis_brief.artifact_id,
            "frame_asset_ids": [asset.asset_id for asset in result.frame_assets],
            "tool_invocation_id": result.report.get("tool_invocation_id"),
        }

    def _run_compose_stage_tool_step(self, job: BackgroundJob) -> dict:
        avatar_asset = self._latest_asset_by_type(job.run_id, "avatar_video_clip")
        if avatar_asset is None:
            raise ValueError("avatar_video_clip asset is required before compose")
        narration_asset = self._latest_asset_by_type(job.run_id, "tts_audio")
        subtitle_asset = self._latest_asset_by_type(job.run_id, "subtitle_file")
        music_asset = self._latest_asset_by_type(job.run_id, "background_music")
        result = self.compose_video(
            job.run_id,
            ComposeVideoRequest(
                stage_run_id=job.stage_run_id,
                avatar_video_asset_id=avatar_asset.asset_id,
                narration_audio_asset_id=narration_asset.asset_id if narration_asset else None,
                subtitle_asset_id=subtitle_asset.asset_id if subtitle_asset else None,
                music_asset_id=music_asset.asset_id if music_asset else None,
                filename=f"{job.job_id}-final.mp4",
                asset_metadata={
                    **_stage_tool_asset_metadata(job),
                    "avatar_video_asset_id": avatar_asset.asset_id,
                },
            ),
        )
        if result is None:
            raise ValueError("run not found while composing stage video")
        invocation, asset, artifact = result
        return {
            "tool_invocation_id": invocation.invocation_id,
            "render_asset_id": asset.asset_id,
            "render_summary_artifact_id": artifact.artifact_id,
            "compose_inputs": {
                "avatar_video_asset_id": avatar_asset.asset_id,
                "narration_audio_asset_id": narration_asset.asset_id if narration_asset else None,
                "subtitle_asset_id": subtitle_asset.asset_id if subtitle_asset else None,
                "music_asset_id": music_asset.asset_id if music_asset else None,
            },
        }

    def _run_export_stage_tool_step(self, job: BackgroundJob) -> dict:
        render_asset = self._latest_asset_by_type(job.run_id, "rendered_video")
        if render_asset is None:
            raise ValueError("rendered_video asset is required before export")
        access_link = self.create_asset_access_link(render_asset.asset_id)
        artifact = self.artifacts.append_artifact(
            run_id=job.run_id,
            stage_run_id=job.stage_run_id,
            stage_key="export",
            artifact_type="publish_package",
            payload={
                "version": "1.0",
                "mode": "stage_execution_manifest",
                "background_job_id": job.job_id,
                "manifest_step_key": job.request_payload.get("manifest_step_key"),
                "render_asset_id": render_asset.asset_id,
                "final_video_url": access_link.url if access_link else None,
                "workspace_url": f"/workspace?run_id={job.run_id}",
            },
        )
        self.events.append_event(
            run_id=job.run_id,
            event_type="job.publish_package_created",
            source_type=EventSourceType.SYSTEM,
            source_id=job.job_id,
            stage_run_id=job.stage_run_id,
            message="Stage execution export package created.",
            payload={
                "publish_package_artifact_id": artifact.artifact_id,
                "render_asset_id": render_asset.asset_id,
            },
        )
        return {
            "publish_package_artifact_id": artifact.artifact_id,
            "render_asset_id": render_asset.asset_id,
            "final_video_url": access_link.url if access_link else None,
        }

    def _submit_avatar_background_job(
        self,
        job: BackgroundJob,
        *,
        worker_id: str,
    ) -> BackgroundJob:
        running = self.background_jobs.update_job(
            job.job_id,
            status=BackgroundJobStatus.RUNNING,
            progress=10,
            locked_by=worker_id,
            started_at=_job_now(),
            lease_expires_at=_job_now() + timedelta(seconds=_JOB_LEASE_SECONDS),
        )
        if running is None:
            raise ValueError("background job disappeared before submit")
        payload = AvatarVideoFromAssetsRequest.model_validate(running.request_payload)
        invocation = self.submit_avatar_task_from_assets(running.run_id, payload)
        if invocation is None:
            raise ValueError("run not found for background job")
        result_payload = {
            **running.result_payload,
            "tool_invocation_id": invocation.invocation_id,
            "external_task_id": invocation.external_task_id,
            "provider_status": invocation.response_payload.get("provider_status"),
        }
        updated = self.background_jobs.update_job(
            running.job_id,
            status=BackgroundJobStatus.WAITING_PROVIDER,
            progress=40,
            result_payload=result_payload,
            lease_expires_at=_job_now() + timedelta(seconds=_JOB_LEASE_SECONDS),
        )
        if updated is None:
            raise ValueError("background job disappeared after submit")
        self.events.append_event(
            run_id=updated.run_id,
            event_type="job.waiting_provider",
            source_type=EventSourceType.SYSTEM,
            source_id=updated.job_id,
            stage_run_id=updated.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message="Background avatar job submitted to provider.",
            payload=result_payload,
        )
        self.session.commit()
        return updated

    def _poll_avatar_background_job(self, job: BackgroundJob) -> BackgroundJob:
        invocation_id = job.result_payload.get("tool_invocation_id")
        if not invocation_id:
            raise ValueError("background job has no tool_invocation_id to poll")
        current_invocation = self.tool_invocations.get_invocation(invocation_id)
        if current_invocation is None:
            raise ValueError("tool invocation not found for background job")
        if current_invocation.status in {
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.CANCELLED,
            ToolInvocationStatus.ARCHIVED,
        }:
            invocation = current_invocation
        else:
            invocation = self.poll_avatar_task(invocation_id)
        if invocation is None:
            raise ValueError("tool invocation not found for background job")
        result_payload = {
            **job.result_payload,
            "tool_invocation_id": invocation.invocation_id,
            "external_task_id": invocation.external_task_id,
            "provider_status": invocation.response_payload.get("provider_status"),
            "latest_status": invocation.response_payload.get("latest_status"),
            "video_url": invocation.response_payload.get("video_url"),
        }
        if invocation.status == ToolInvocationStatus.FAILED:
            return self._fail_background_job(
                job,
                error_code=invocation.error_code or "provider_failed",
                error_message=invocation.error_message or "provider task failed",
                result_payload=result_payload,
            )
        if invocation.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.ARCHIVED}:
            result_payload = self._finalize_avatar_background_job(
                job=job,
                invocation=invocation,
                result_payload=result_payload,
            )
            updated = self.background_jobs.update_job(
                job.job_id,
                status=BackgroundJobStatus.COMPLETED,
                progress=100,
                result_payload=result_payload,
            )
            if updated is None:
                raise ValueError("background job disappeared after poll")
            self._record_background_job_usage(updated, operation="job_completed")
            self.events.append_event(
                run_id=updated.run_id,
                event_type="job.completed",
                source_type=EventSourceType.SYSTEM,
                source_id=updated.job_id,
                stage_run_id=updated.stage_run_id,
                tool_invocation_id=invocation.invocation_id,
                message="Background avatar job completed.",
                payload=result_payload,
            )
            self.session.commit()
            return updated
        updated = self.background_jobs.update_job(
            job.job_id,
            status=BackgroundJobStatus.WAITING_PROVIDER,
            progress=max(job.progress, 60),
            result_payload=result_payload,
            lease_expires_at=_job_now() + timedelta(seconds=_JOB_LEASE_SECONDS),
        )
        if updated is None:
            raise ValueError("background job disappeared after poll")
        self.session.commit()
        return updated

    def _finalize_avatar_background_job(
        self,
        *,
        job: BackgroundJob,
        invocation: ToolInvocation,
        result_payload: dict,
    ) -> dict:
        payload = AvatarVideoFromAssetsRequest.model_validate(job.request_payload)
        finalized_payload = dict(result_payload)
        archived_asset = invocation.response_payload.get("archived_asset")
        avatar_asset = None
        if payload.auto_archive:
            if archived_asset:
                avatar_asset = self.assets.get_asset(archived_asset.get("asset_id"))
            else:
                _, avatar_asset = self._archive_completed_avatar_job(
                    job=job,
                    invocation=invocation,
                    video_url=result_payload.get("video_url"),
                )
            if avatar_asset is not None:
                finalized_payload["archived_avatar_asset_id"] = avatar_asset.asset_id
                finalized_payload["archived_avatar_storage_url"] = avatar_asset.storage_url

        if payload.auto_compose and avatar_asset is not None:
            compose_payload = self._compose_completed_avatar_job(
                job=job,
                payload=payload,
                avatar_asset=avatar_asset,
            )
            finalized_payload.update(compose_payload)
        return finalized_payload

    def _archive_completed_avatar_job(
        self,
        *,
        job: BackgroundJob,
        invocation: ToolInvocation,
        video_url: str | None,
    ) -> tuple[ToolInvocation, Asset]:
        result = self.archive_avatar_task(
            invocation.invocation_id,
            AvatarVideoArchiveRequest(
                filename=f"{job.job_id}-avatar.mp4",
                video_url=video_url,
                asset_metadata={
                    "mode": "provider_avatar_job",
                    "background_job_id": job.job_id,
                },
            ),
        )
        if result is None:
            raise ValueError("avatar invocation could not be archived")
        updated_invocation, avatar_asset = result
        self.events.append_event(
            run_id=job.run_id,
            event_type="job.avatar_archived",
            source_type=EventSourceType.SYSTEM,
            source_id=job.job_id,
            stage_run_id=job.stage_run_id,
            tool_invocation_id=updated_invocation.invocation_id,
            message="Background avatar job archived provider video.",
            payload={
                "job_id": job.job_id,
                "avatar_asset_id": avatar_asset.asset_id,
                "storage_url": avatar_asset.storage_url,
            },
        )
        self.session.commit()
        return updated_invocation, avatar_asset

    def _compose_completed_avatar_job(
        self,
        *,
        job: BackgroundJob,
        payload: AvatarVideoFromAssetsRequest,
        avatar_asset: Asset,
    ) -> dict:
        compose_stage_run_id = self._stage_run_id(job.run_id, "compose") or job.stage_run_id
        compose_warnings: list[dict] = []
        narration_asset = self._optional_compose_asset(
            job=job,
            asset=self._asset_for_run(job.run_id, payload.audio_asset_id),
            role="narration_audio",
            warnings=compose_warnings,
        )
        subtitle_asset = self._optional_compose_asset(
            job=job,
            asset=self._latest_asset_by_type(job.run_id, "subtitle_file"),
            role="subtitle_file",
            warnings=compose_warnings,
        )
        music_asset = self._optional_compose_asset(
            job=job,
            asset=self._latest_asset_by_type(job.run_id, "background_music"),
            role="background_music",
            warnings=compose_warnings,
        )
        result = self.compose_video(
            job.run_id,
            ComposeVideoRequest(
                stage_run_id=compose_stage_run_id,
                avatar_video_asset_id=avatar_asset.asset_id,
                narration_audio_asset_id=narration_asset.asset_id if narration_asset else None,
                subtitle_asset_id=subtitle_asset.asset_id if subtitle_asset else None,
                music_asset_id=music_asset.asset_id if music_asset else None,
                filename=f"{job.job_id}-final.mp4",
                width=payload.compose_width,
                height=payload.compose_height,
                fps=payload.compose_fps,
                crf=payload.compose_crf,
                preset=payload.compose_preset,
                bgm_volume=payload.bgm_volume,
                asset_metadata={
                    "mode": "provider_avatar_job",
                    "background_job_id": job.job_id,
                    "avatar_video_asset_id": avatar_asset.asset_id,
                    "compose_warnings": compose_warnings,
                },
            ),
        )
        if result is None:
            raise ValueError("run not found while composing avatar job")
        _compose_invocation, render_asset, render_summary = result
        access_link = self.create_asset_access_link(render_asset.asset_id)
        publish_package = self.artifacts.append_artifact(
            run_id=job.run_id,
            stage_run_id=self._stage_run_id(job.run_id, "export") or compose_stage_run_id,
            stage_key="export",
            artifact_type="publish_package",
            payload={
                "version": "1.0",
                "mode": "provider_avatar_job",
                "background_job_id": job.job_id,
                "render_asset_id": render_asset.asset_id,
                "final_video_url": access_link.url if access_link else None,
                "workspace_url": f"/workspace?run_id={job.run_id}",
            },
        )
        self.events.append_event(
            run_id=job.run_id,
            event_type="job.final_composed",
            source_type=EventSourceType.SYSTEM,
            source_id=job.job_id,
            stage_run_id=compose_stage_run_id,
            message="Background avatar job composed final video.",
            payload={
                "job_id": job.job_id,
                "avatar_asset_id": avatar_asset.asset_id,
                "render_asset_id": render_asset.asset_id,
                "render_summary_artifact_id": render_summary.artifact_id,
                "publish_package_artifact_id": publish_package.artifact_id,
            },
        )
        self.session.commit()
        return {
            "render_asset_id": render_asset.asset_id,
            "render_summary_artifact_id": render_summary.artifact_id,
            "publish_package_artifact_id": publish_package.artifact_id,
            "final_video_url": access_link.url if access_link else None,
            "compose_inputs": {
                "avatar_video_asset_id": avatar_asset.asset_id,
                "narration_audio_asset_id": narration_asset.asset_id if narration_asset else None,
                "subtitle_asset_id": subtitle_asset.asset_id if subtitle_asset else None,
                "music_asset_id": music_asset.asset_id if music_asset else None,
            },
            "compose_warnings": compose_warnings,
        }

    def _optional_compose_asset(
        self,
        *,
        job: BackgroundJob,
        asset: Asset | None,
        role: str,
        warnings: list[dict],
    ) -> Asset | None:
        if asset is None:
            return None
        exists = getattr(self.storage, "exists", None)
        if exists is None or exists(asset.storage_url):
            return asset
        warning = {
            "code": "optional_compose_asset_missing",
            "role": role,
            "asset_id": asset.asset_id,
            "storage_url": asset.storage_url,
        }
        warnings.append(warning)
        self.events.append_event(
            run_id=job.run_id,
            event_type="job.optional_asset_missing",
            source_type=EventSourceType.SYSTEM,
            source_id=job.job_id,
            stage_run_id=job.stage_run_id,
            severity=EventSeverity.WARNING,
            message="Optional compose asset missing; skipped.",
            payload=warning,
        )
        return None

    def _append_usage_entry(
        self,
        *,
        run_id: str,
        source_type: str,
        source_id: str,
        operation: str,
        stage_run_id: str | None = None,
        provider: str | None = None,
        tool_key: str | None = None,
        estimated_cost: float | None = None,
        actual_cost: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
        asset_bytes: int | None = None,
        usage_unit: str | None = None,
        usage_quantity: float | None = None,
        entry_metadata: dict[str, Any] | None = None,
    ) -> UsageLedgerEntry:
        entry = self.usage_ledger.append_entry(
            run_id=run_id,
            stage_run_id=stage_run_id,
            source_type=source_type,
            source_id=source_id,
            provider=provider,
            tool_key=tool_key,
            operation=operation,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            asset_bytes=asset_bytes,
            usage_unit=usage_unit,
            usage_quantity=usage_quantity,
            entry_metadata=entry_metadata,
        )
        self._sync_run_usage_totals(run_id)
        self.events.append_event(
            run_id=run_id,
            event_type="usage.ledger_entry_created",
            source_type=EventSourceType.SYSTEM,
            source_id=entry.ledger_entry_id,
            stage_run_id=stage_run_id,
            message="Usage ledger entry created.",
            payload={
                "ledger_entry_id": entry.ledger_entry_id,
                "source_type": source_type,
                "source_id": source_id,
                "operation": operation,
                "estimated_cost": estimated_cost,
                "actual_cost": actual_cost,
            },
        )
        return entry

    def _sync_run_usage_totals(self, run_id: str) -> None:
        record = self._get_run_record(run_id)
        if record is None:
            return
        totals = _usage_totals(self.usage_ledger.list_entries(run_id))
        record.estimated_cost = totals["estimated_cost"]
        record.actual_cost = totals["actual_cost"]
        self.session.flush()

    def _usage_summary_for_run(self, run: ProductionRun) -> UsageSummary:
        entries = self.usage_ledger.list_entries(run.run_id)
        totals = _usage_totals(entries)
        budget_limit = _cost_budget_limit(run.input_payload)
        budget_basis = max(totals["actual_cost"], totals["estimated_cost"])
        budget_remaining = (
            round(budget_limit - budget_basis, 6) if budget_limit is not None else None
        )
        if budget_limit is None:
            budget_status = "not_configured"
        elif budget_basis > budget_limit:
            budget_status = "exceeded"
        else:
            budget_status = "ok"
        return UsageSummary(
            run_id=run.run_id,
            ledger_entry_count=len(entries),
            estimated_cost=totals["estimated_cost"],
            actual_cost=totals["actual_cost"],
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            duration_ms=totals["duration_ms"],
            asset_bytes=totals["asset_bytes"],
            budget_limit=budget_limit,
            budget_remaining=budget_remaining,
            budget_status=budget_status,
            by_source_type=_usage_group_totals(entries, "source_type"),
            by_provider=_usage_group_totals(entries, "provider"),
            by_tool_key=_usage_group_totals(entries, "tool_key"),
            entries=entries,
        )

    def _assert_intent_downgrade_acknowledged(
        self,
        run_id: str,
        *,
        execution_plan_artifact: Artifact,
        accepted: bool,
    ) -> None:
        """Refuse a silent downgrade of the quality the run declared (T75).

        The capability *is* available, so ``can_execute`` is True and nothing else
        would stop the run — which is exactly why the question has to be asked
        here. Starting without an acknowledgement is a refusal, not a warning.
        """
        plan = execution_plan_artifact.payload
        if not plan.get("requires_intent_acknowledgement"):
            return
        downgrades = plan.get("intent_downgrades", [])
        record = self._get_run_record(run_id)
        if record is None:
            return
        if not accepted:
            self.events.append_event(
                run_id=record.id,
                event_type="run.start_blocked",
                source_type=EventSourceType.RUN,
                source_id=record.id,
                stage_key="preflight",
                severity=EventSeverity.WARNING,
                message="Run start blocked: the declared quality mode cannot be met.",
                payload={
                    "code": "intent_downgrade_not_acknowledged",
                    "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                    "execution_policy": plan.get("execution_policy"),
                    "intent_downgrades": downgrades,
                },
            )
            self.session.commit()
            raise RunGateError(
                "intent_downgrade_not_acknowledged",
                (
                    "the run declared a quality mode the selector cannot meet; "
                    "confirm the downgrade to start anyway"
                ),
                details={
                    "execution_policy": plan.get("execution_policy"),
                    "intent_downgrades": downgrades,
                    "resolution": {
                        "action": "restart_with_acknowledgement",
                        "body": {"accept_intent_downgrade": True},
                    },
                },
            )
        self.events.append_event(
            run_id=record.id,
            event_type="run.intent_downgrade_accepted",
            source_type=EventSourceType.RUN,
            source_id=record.id,
            stage_key="preflight",
            severity=EventSeverity.WARNING,
            message="Run started with an acknowledged quality downgrade.",
            payload={
                "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                "execution_policy": plan.get("execution_policy"),
                "intent_downgrades": downgrades,
            },
        )

    def _assert_cost_reservation_allows(
        self,
        run_id: str,
        *,
        execution_plan_artifact: Artifact,
    ) -> None:
        """Stop the run before its first charge when the plan cannot be afforded (T76).

        Budget checks used to happen one dispatch at a time, so the cheapest way
        to discover an unaffordable run was to spend money on the first few steps
        and fail later. The execution plan already reserves the whole total, so
        this gate turns that number into a refusal *before* anything is charged —
        and records the verdict even when it passes, so a run without a budget is
        visibly unguarded rather than silently unguarded.
        """
        run = self.runs.get_run(run_id)
        if run is None:
            return
        reservation = execution_plan_artifact.payload.get("cost_reservation") or {}
        reserved_cost = float(reservation.get("reserved_cost") or 0.0)
        # V8.3: the team's ceiling is checked before the run's own budget. A run
        # that can afford itself inside a team that has run out of allowance is
        # still not affordable, and reporting the team number first is the more
        # actionable of the two.
        team_quota = self._assert_team_quota_allows(
            run_id,
            estimated_cost=reserved_cost,
            operation="cost_reservation",
            additional_runs=1,
        )
        budget_limit = declared_budget_limit(run.input_payload)
        if budget_limit is None:
            budget_status = "not_configured"
        elif reserved_cost > budget_limit:
            budget_status = "exceeds_budget"
        else:
            budget_status = "within_budget"
        record = self._get_run_record(run_id)

        if budget_status == "exceeds_budget":
            if record is not None:
                self.events.append_event(
                    run_id=record.id,
                    event_type="run.start_blocked",
                    source_type=EventSourceType.RUN,
                    source_id=record.id,
                    stage_key="preflight",
                    severity=EventSeverity.WARNING,
                    message="Run start blocked: the reserved cost exceeds the budget.",
                    payload={
                        "code": "cost_reservation_exceeds_budget",
                        "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                        "reserved_cost": reserved_cost,
                        "budget_limit": budget_limit,
                        "currency": reservation.get("currency"),
                        "line_items": reservation.get("line_items", []),
                    },
                )
                self.session.commit()
            raise RunGateError(
                "cost_reservation_exceeds_budget",
                (
                    "the execution plan reserves more than the run's cost budget; "
                    "raise the budget or lower the quality mode before starting"
                ),
                details={
                    "reserved_cost": reserved_cost,
                    "budget_limit": budget_limit,
                    "currency": reservation.get("currency"),
                    "paid_providers": reservation.get("paid_providers", []),
                    "line_items": reservation.get("line_items", []),
                },
            )

        if record is not None:
            self.events.append_event(
                run_id=record.id,
                event_type="run.cost_reservation_recorded",
                source_type=EventSourceType.RUN,
                source_id=record.id,
                stage_key="preflight",
                severity=EventSeverity.INFO,
                message=f"Cost reservation {budget_status}.",
                payload={
                    "execution_plan_artifact_id": execution_plan_artifact.artifact_id,
                    "reserved_cost": reserved_cost,
                    "budget_limit": budget_limit,
                    "budget_status": budget_status,
                    "currency": reservation.get("currency"),
                    "paid_providers": reservation.get("paid_providers", []),
                    # ``None`` means the team had no ceiling in force. Recorded
                    # explicitly so the log distinguishes "unguarded" from "the
                    # guard was never consulted".
                    "team_quota": team_quota,
                },
            )

    def _paid_provider_consents(self, run: ProductionRun) -> dict[str, Any]:
        consents = (run.input_payload or {}).get("paid_provider_consents") or {}
        return consents if isinstance(consents, dict) else {}

    def _assert_paid_provider_confirmed(
        self,
        run: ProductionRun,
        *,
        provider: str,
        tool_key: str | None,
        capability: str | None,
        estimated_cost: float,
        operation: str,
    ) -> None:
        """Refuse the first real charge to a paid provider until a human agrees (T76).

        Granularity is per run and per provider: a run that has already been told
        "yes, pay DashScope" is not asked again, but a run nobody has confirmed
        cannot quietly spend money just because no budget was ever configured.
        """
        if self._paid_provider_consents(run).get(provider):
            return
        record = self._get_run_record(run.run_id)
        details: dict[str, Any] = {
            "provider": provider,
            "tool_key": tool_key,
            "capability": capability,
            "operation": operation,
            "estimated_cost": round(float(estimated_cost), 6),
            "resolution": {
                "action": "confirm_paid_provider",
                "endpoint": f"/api/runs/{run.run_id}/provider-consents",
                "body": {"provider": provider, "accept": True},
            },
        }
        if record is not None:
            self.events.append_event(
                run_id=record.id,
                event_type="run.paid_provider_confirmation_required",
                source_type=EventSourceType.RUN,
                source_id=record.id,
                stage_key=operation,
                severity=EventSeverity.WARNING,
                message=f"Paid provider {provider} requires first-use confirmation.",
                payload=details,
            )
            self.session.commit()
        raise RunGateError(
            "paid_provider_confirmation_required",
            (
                f"{provider} is a paid provider and this run has not confirmed it yet; "
                "confirm before the first real charge"
            ),
            details=details,
        )

    def confirm_paid_provider(
        self,
        run_id: str,
        *,
        provider: str,
        accept: bool = True,
        note: str | None = None,
    ) -> ProductionRun | None:
        """Record (or retract) a paid-provider confirmation for a run."""
        record = self._get_run_record(run_id)
        if record is None:
            return None
        payload = dict(record.input_payload or {})
        consents = dict(payload.get("paid_provider_consents") or {})
        if accept:
            consents[provider] = True
        else:
            consents.pop(provider, None)
        payload["paid_provider_consents"] = consents
        # input_payload is a plain JSON column, so it must be reassigned for
        # SQLAlchemy to notice the mutation.
        record.input_payload = payload
        self.events.append_event(
            run_id=record.id,
            event_type=(
                "run.paid_provider_confirmed"
                if accept
                else "run.paid_provider_confirmation_revoked"
            ),
            source_type=EventSourceType.RUN,
            source_id=record.id,
            stage_key="preflight",
            severity=EventSeverity.WARNING,
            message=(
                f"Paid provider {provider} confirmed."
                if accept
                else f"Paid provider {provider} confirmation revoked."
            ),
            payload={"provider": provider, "note": note},
        )
        self.session.commit()
        return self.runs.get_run(run_id)

    def _assert_budget_allows(
        self,
        run_id: str,
        *,
        estimated_cost: float | None,
        operation: str,
        provider: str | None = None,
        tool_key: str | None = None,
        capability: str | None = None,
    ) -> None:
        run = self.runs.get_run(run_id)
        if run is None:
            return
        additional = max(0.0, estimated_cost or 0.0)
        # V8.3: a team ceiling applies to every paid dispatch, not just to
        # starting a run, so a team that has spent out stops spending mid-run.
        self._assert_team_quota_allows(
            run_id,
            estimated_cost=additional,
            operation=operation,
        )
        budget_limit = declared_budget_limit(run.input_payload)
        if budget_limit is not None:
            current = self._usage_summary_for_run(run)
            projected = max(current.estimated_cost, current.actual_cost) + additional
            if projected > budget_limit:
                raise RunGateError(
                    "cost_budget_exceeded",
                    (
                        f"cost budget exceeded for {operation}: "
                        f"projected {projected:.6f} > budget {budget_limit:.6f}"
                    ),
                    details={
                        "operation": operation,
                        "projected_cost": round(projected, 6),
                        "budget_limit": budget_limit,
                        "additional_estimated_cost": round(additional, 6),
                        "current_estimated_cost": current.estimated_cost,
                        "current_actual_cost": current.actual_cost,
                    },
                )
        if provider and additional > 0.0:
            self._assert_paid_provider_confirmed(
                run,
                provider=provider,
                tool_key=tool_key,
                capability=capability,
                estimated_cost=additional,
                operation=operation,
            )

    def _estimate_adapter_cost(
        self,
        adapter: object,
        *,
        capability: str,
        payload: dict[str, Any],
    ) -> float:
        estimate = getattr(adapter, "estimate_cost", None)
        if estimate is None:
            return 0.0
        try:
            value = estimate(capability, payload)
        except TypeError:
            value = estimate(payload)
        return round(float(value or 0.0), 6)

    def _record_tool_invocation_usage(
        self,
        invocation: ToolInvocation,
        *,
        operation: str,
        asset: Asset | None = None,
    ) -> UsageLedgerEntry:
        token_counts = _token_counts_for_invocation(invocation)
        actual_cost = invocation.cost if invocation.cost is not None else 0.0
        return self._append_usage_entry(
            run_id=invocation.run_id,
            stage_run_id=invocation.stage_run_id,
            source_type="tool_invocation",
            source_id=invocation.invocation_id,
            provider=invocation.provider,
            tool_key=invocation.tool_key,
            operation=operation,
            estimated_cost=invocation.cost or 0.0,
            actual_cost=actual_cost,
            input_tokens=token_counts["input_tokens"],
            output_tokens=token_counts["output_tokens"],
            duration_ms=invocation.duration_ms or (asset.duration_ms if asset else None),
            asset_bytes=asset.file_size if asset else None,
            usage_unit="asset_bytes" if asset and asset.file_size is not None else None,
            usage_quantity=float(asset.file_size) if asset and asset.file_size is not None else None,
            entry_metadata={
                "status": invocation.status.value,
                "asset_id": asset.asset_id if asset else None,
                "asset_type": asset.asset_type if asset else None,
                "external_task_id": invocation.external_task_id,
            },
        )

    def _record_asset_usage(
        self,
        asset: Asset,
        *,
        operation: str,
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UsageLedgerEntry:
        return self._append_usage_entry(
            run_id=asset.run_id,
            stage_run_id=asset.stage_run_id,
            source_type="asset",
            source_id=source_id or asset.asset_id,
            provider=asset.provider,
            tool_key=asset.tool_key,
            operation=operation,
            estimated_cost=0.0,
            actual_cost=0.0,
            duration_ms=asset.duration_ms,
            asset_bytes=asset.file_size,
            usage_unit="asset_bytes" if asset.file_size is not None else None,
            usage_quantity=float(asset.file_size) if asset.file_size is not None else None,
            entry_metadata={
                "asset_id": asset.asset_id,
                "asset_type": asset.asset_type,
                **(metadata or {}),
            },
        )

    def _record_background_job_usage(
        self,
        job: BackgroundJob,
        *,
        operation: str,
        estimated_cost: float | None = 0.0,
        actual_cost: float | None = 0.0,
    ) -> UsageLedgerEntry:
        return self._append_usage_entry(
            run_id=job.run_id,
            stage_run_id=job.stage_run_id,
            source_type="background_job",
            source_id=job.job_id,
            provider=job.request_payload.get("provider"),
            tool_key=job.request_payload.get("tool_key"),
            operation=operation,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            duration_ms=_job_duration_ms(job),
            asset_bytes=_job_result_asset_bytes(self, job),
            usage_unit="job",
            usage_quantity=1.0,
            entry_metadata={
                "job_type": job.job_type,
                "status": job.status.value,
                "attempt": job.attempt,
                "manifest_step_key": job.request_payload.get("manifest_step_key"),
            },
        )

    def _state_action_result(
        self,
        record: ProductionRunRecord,
        payload: RunStateActionRequest,
        *,
        before_status: str,
        before_stage_key: str | None,
        message: str,
        result_payload: dict[str, Any] | None = None,
    ) -> RunStateActionResult:
        run = self.runs.get_run(record.id)
        if run is None:
            raise ValueError("run disappeared during state action")
        return RunStateActionResult(
            action=payload.action,
            run_id=record.id,
            status="completed",
            message=message,
            transition={
                "from_status": before_status,
                "to_status": run.status.value,
                "from_stage_key": before_stage_key,
                "to_stage_key": run.current_stage_key,
                "action": payload.action.value,
            },
            run=run,
            result_payload=result_payload or {},
        )

    def _state_action_event_payload(
        self,
        payload: RunStateActionRequest,
        before_status: str,
        before_stage_key: str | None,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "action": payload.action.value,
            "reason": payload.reason,
            "from_status": before_status,
            "from_stage_key": before_stage_key,
            "state_transition_table": run_state_transition_table(),
            **extra,
        }

    def _resume_paused_run(
        self,
        record: ProductionRunRecord,
        payload: RunStateActionRequest,
        before_status: str,
        before_stage_key: str | None,
    ) -> None:
        stage = self._stage_record(record, record.current_stage_key)
        self._append_run_event(
            record,
            "run.resumed",
            message="Run resumed by state action.",
            payload=self._state_action_event_payload(payload, before_status, before_stage_key),
        )
        if stage is None:
            record.status = RunStatus.COMPLETED.value
            record.current_stage_key = None
            self._append_run_event(
                record,
                "run.completed",
                message="Run completed while resuming without a current stage.",
                payload={"status": record.status},
            )
            return
        if stage.status in {StageStatus.WAITING_REVIEW.value, StageStatus.REJECTED.value}:
            if stage.status == StageStatus.REJECTED.value:
                stage.status = StageStatus.WAITING_REVIEW.value
                self._append_stage_event(
                    record,
                    stage,
                    "stage.review_reopened",
                    message="Rejected stage reopened for review.",
                    payload={"status": stage.status, "action": payload.action.value},
                )
            record.status = RunStatus.WAITING_REVIEW.value
            return
        record.status = RunStatus.RUNNING.value
        self._advance_until_gate(record)

    def _cancel_run_record(
        self,
        record: ProductionRunRecord,
        payload: RunStateActionRequest,
        before_status: str,
        before_stage_key: str | None,
    ) -> None:
        stage = self._stage_record(record, record.current_stage_key)
        if stage is not None and stage.status not in {
            StageStatus.COMPLETED.value,
            StageStatus.SKIPPED.value,
            StageStatus.CANCELLED.value,
        }:
            stage.status = StageStatus.CANCELLED.value
            self._append_stage_event(
                record,
                stage,
                "stage.cancelled",
                message="Current stage cancelled by run state action.",
                payload={"status": stage.status, "action": payload.action.value},
            )
        record.status = RunStatus.CANCELLED.value
        self._append_run_event(
            record,
            "run.cancelled",
            message="Run cancelled by state action.",
            payload=self._state_action_event_payload(payload, before_status, before_stage_key),
        )

    def _cancel_unfinished_jobs(
        self,
        run_id: str,
        *,
        reason: str,
        stage_run_ids: set[str] | None = None,
    ) -> list[str]:
        cancelled: list[str] = []
        for job in self.background_jobs.list_jobs(run_id):
            if stage_run_ids is not None and job.stage_run_id not in stage_run_ids:
                continue
            if job.status.value not in UNFINISHED_JOB_STATUSES:
                continue
            taxonomy_fields = _error_classification_fields(
                classify_error(
                    error_code="cancelled_by_state_action",
                    error_message=reason,
                    provider=job.request_payload.get("provider"),
                    tool_key=job.request_payload.get("tool_key"),
                    status=BackgroundJobStatus.CANCELLED.value,
                )
            )
            updated = self.background_jobs.update_job(
                job.job_id,
                status=BackgroundJobStatus.CANCELLED,
                progress=100,
                error_code="cancelled_by_state_action",
                error_message=reason,
                **taxonomy_fields,
            )
            if updated is None:
                continue
            cancelled.append(updated.job_id)
            self._record_background_job_usage(updated, operation="job_cancelled")
            self.events.append_event(
                run_id=run_id,
                event_type="job.cancelled",
                source_type=EventSourceType.SYSTEM,
                source_id=updated.job_id,
                stage_run_id=updated.stage_run_id,
                message="Background job cancelled by run state action.",
                payload={"job_id": updated.job_id, "reason": reason, **taxonomy_fields},
            )
        return cancelled

    def _reset_run_to_stage(
        self,
        record: ProductionRunRecord,
        *,
        stage_key: str,
        payload: RunStateActionRequest,
        before_status: str,
        before_stage_key: str | None,
        artifact_type: str | None = None,
        artifact_id: str | None = None,
    ) -> tuple[list[str], list[str], list[str]]:
        """Return ``(affected_stage_keys, invalidated_stage_keys, superseded_request_ids)``."""
        ordered_stages = self._ordered_stage_records(record)
        target_index = self._stage_index(ordered_stages, stage_key)
        affected = ordered_stages[target_index:]
        affected_stage_ids = {stage.id for stage in affected}
        # A rerun invalidates the pending review of the stage itself, not only
        # the ones downstream of it.
        superseded_requests = self.reviews.supersede_requests_for_stage_run(
            stage_run_ids=sorted(affected_stage_ids)
        )
        cancelled_jobs = self._cancel_unfinished_jobs(
            record.id,
            reason=f"{stage_key} rerun requested.",
            stage_run_ids=affected_stage_ids,
        )
        for stage in affected:
            previous_status = stage.status
            stage.status = StageStatus.PENDING.value
            stage.attempt = (stage.attempt or 1) + 1
            stage.error_code = None
            stage.error_message = None
            self._append_stage_event(
                record,
                stage,
                "stage.reset_for_rerun",
                message="Stage reset for rerun.",
                payload={
                    "action": payload.action.value,
                    "previous_status": previous_status,
                    "status": stage.status,
                    "attempt": stage.attempt,
                },
            )
        record.current_stage_key = stage_key
        record.status = RunStatus.RUNNING.value
        invalidated = self.invalidate_downstream_stages(
            record,
            source_stage_key=stage_key,
            reason=(
                f"Stage {stage_key} was rerun; downstream outputs derived from the "
                "previous version must be regenerated."
            ),
            cause=f"rerun:{payload.action.value}",
        )
        self._append_run_event(
            record,
            "run.rerun_requested",
            message="Run reset to an earlier stage.",
            payload=self._state_action_event_payload(
                payload,
                before_status,
                before_stage_key,
                stage_key=stage_key,
                affected_stage_keys=[stage.stage_key for stage in affected],
                invalidated_stage_keys=invalidated,
                superseded_review_request_ids=superseded_requests,
                cancelled_job_ids=cancelled_jobs,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
            ),
        )
        return [stage.stage_key for stage in affected], invalidated, superseded_requests

    def _skip_optional_stage(
        self,
        record: ProductionRunRecord,
        *,
        stage_key: str,
        payload: RunStateActionRequest,
        before_status: str,
        before_stage_key: str | None,
    ) -> list[str]:
        if record.current_stage_key != stage_key:
            raise ValueError("skip_optional_stage can only skip the current stage")
        ordered_stages = self._ordered_stage_records(record)
        stage_index = self._stage_index(ordered_stages, stage_key)
        stage = ordered_stages[stage_index]
        pipeline = self._pipeline_for_run_record(record)
        stage_definition = {item.stage_key: item for item in pipeline.stages}.get(stage_key)
        if stage_definition is None:
            raise ValueError(f"stage not found in pipeline: {stage_key}")
        if not stage_definition.skippable:
            raise ValueError(f"stage is not skippable: {stage_key}")

        reason = payload.reason or self._state_action_payload_str(payload.payload, "reason")
        if stage_definition.skip_policy.get("reason_required") and not reason:
            raise ValueError("reason is required to skip this stage")
        if (
            stage_definition.skip_policy.get("allowed_when") == "render_summary_exists"
            and self._current_artifact_by_type(record.id, "render_summary") is None
        ):
            raise ValueError("render_summary is required before skipping this stage")

        stage.status = StageStatus.SKIPPED.value
        created_artifact_ids: list[str] = []
        for output in stage_definition.output_artifacts:
            artifact = self.artifacts.append_artifact(
                run_id=record.id,
                stage_run_id=stage.id,
                stage_key=stage.stage_key,
                artifact_type=output.artifact_type,
                payload=self._skipped_stage_artifact_payload(
                    artifact_type=output.artifact_type,
                    stage_key=stage.stage_key,
                    reason=reason,
                ),
                producer="run_state_machine",
                lineage={
                    "action": payload.action.value,
                    "reason": reason,
                },
            )
            created_artifact_ids.append(artifact.artifact_id)
            self._append_stage_event(
                record,
                stage,
                "artifact.created",
                message="Skipped stage placeholder artifact created.",
                payload={
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "version": artifact.version,
                    "skipped": True,
                },
            )

        self._append_stage_event(
            record,
            stage,
            "stage.skipped",
            message="Skippable stage skipped by state action.",
            payload={"status": stage.status, "action": payload.action.value, "reason": reason},
        )
        self._append_run_event(
            record,
            "run.stage_skipped",
            message="Run skipped an optional stage.",
            payload=self._state_action_event_payload(
                payload,
                before_status,
                before_stage_key,
                stage_key=stage_key,
                created_artifact_ids=created_artifact_ids,
            ),
        )

        if stage_index + 1 >= len(ordered_stages):
            record.current_stage_key = None
            record.status = RunStatus.COMPLETED.value
            self._append_run_event(
                record,
                "run.completed",
                message="Run completed after skipping final stage.",
                payload={"status": record.status},
            )
            return created_artifact_ids

        record.current_stage_key = ordered_stages[stage_index + 1].stage_key
        record.status = RunStatus.RUNNING.value
        self._advance_until_gate(record)
        return created_artifact_ids

    @staticmethod
    def _skipped_stage_artifact_payload(
        *,
        artifact_type: str,
        stage_key: str,
        reason: str | None,
    ) -> dict[str, Any]:
        if artifact_type == "review_notes":
            return {
                "version": "1.0",
                "stage_key": stage_key,
                "status": "skipped",
                "notes": [
                    {
                        "type": "skip",
                        "reason": reason or "Stage skipped by run state action.",
                    }
                ],
            }
        return {
            "stage_key": stage_key,
            "status": "placeholder",
            "skipped": True,
            "reason": reason,
        }

    @staticmethod
    def _state_action_payload_str(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _state_action_stage_key(
        self,
        payload: RunStateActionRequest,
        *,
        default: str | None = None,
    ) -> str:
        stage_key = payload.stage_key or self._state_action_payload_str(payload.payload, "stage_key") or default
        if not stage_key:
            raise ValueError("stage_key is required for this run state action")
        return stage_key

    def _state_action_artifact_type(self, payload: RunStateActionRequest) -> str:
        artifact_type = payload.artifact_type or self._state_action_payload_str(payload.payload, "artifact_type")
        if not artifact_type:
            raise ValueError("artifact_type is required for this run state action")
        return artifact_type

    def _stage_key_for_output_artifact(
        self,
        record: ProductionRunRecord,
        artifact_type: str,
    ) -> str:
        pipeline = self._pipeline_for_run_record(record)
        for stage in pipeline.stages:
            if artifact_type in stage.output_artifact_types:
                return stage.stage_key
        raise ValueError(f"artifact_type is not produced by this pipeline: {artifact_type}")

    def _stage_record(
        self,
        record: ProductionRunRecord,
        stage_key: str | None,
    ) -> StageRunRecord | None:
        if stage_key is None:
            return None
        return next(
            (stage for stage in self._ordered_stage_records(record) if stage.stage_key == stage_key),
            None,
        )

    @staticmethod
    def _stage_index(stages: list[StageRunRecord], stage_key: str) -> int:
        index = next(
            (position for position, stage in enumerate(stages) if stage.stage_key == stage_key),
            None,
        )
        if index is None:
            raise ValueError(f"stage not found: {stage_key}")
        return index

    def _record_manifest_action_event(
        self,
        result: ManifestActionResult,
        *,
        stage_run_id: str | None = None,
        manifest_step_key: str | None = None,
    ) -> None:
        self.events.append_event(
            run_id=result.run_id,
            event_type="manifest_action.executed",
            source_type=EventSourceType.SYSTEM,
            source_id=result.action.value,
            stage_run_id=stage_run_id,
            message=result.message,
            payload={
                "action": result.action.value,
                "status": result.status,
                "manifest_step_key": manifest_step_key,
                "result_keys": sorted(result.result_payload),
                "refresh": result.refresh,
            },
        )
        self.session.commit()

    def _manifest_action_stage_run_id(
        self,
        run_id: str,
        *,
        request: ManifestActionRequest,
        action_payload: dict[str, Any],
        default_stage_key: str,
    ) -> str | None:
        stage_key = (
            request.stage_key
            or self._manifest_action_payload_str(action_payload, "stage_key")
            or default_stage_key
        )
        return (
            request.stage_run_id
            or self._manifest_action_payload_str(action_payload, "stage_run_id")
            or self._stage_run_id(run_id, stage_key)
        )

    @staticmethod
    def _manifest_action_payload_str(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _manifest_action_payload_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")  # noqa: TRY004
        return dict(value)

    def _manifest_action_spoken_text(self, run_id: str, *, max_chars: int = 600) -> str:
        spoken_script = self.artifacts.current_payloads(run_id).get("spoken_script") or {}
        if not isinstance(spoken_script, dict):
            return ""
        full_text = str(spoken_script.get("full_text") or "").strip()
        if not full_text:
            full_text = " ".join(
                str(segment.get("text") or segment.get("caption") or "").strip()
                for segment in spoken_script.get("segments") or []
                if isinstance(segment, dict)
                and str(segment.get("text") or segment.get("caption") or "").strip()
            )
        if max_chars > 3 and len(full_text) > max_chars:
            return full_text[: max_chars - 3].rstrip() + "..."
        return full_text

    def _default_person_image_prompt(self, run: ProductionRun) -> str:
        input_payload = run.input_payload or {}
        brand = input_payload.get("brand_name") or "奶茶品牌"
        product = input_payload.get("product_name") or "冲泡奶茶"
        script_hint = self._manifest_action_spoken_text(run.run_id, max_chars=120)
        return (
            "Photorealistic portrait for AI talking-avatar video. One Chinese female college "
            "student in a cozy dorm room, wearing a soft knit cardigan, seated at a desk. "
            "Front-facing upper body, mouth clearly visible, natural friendly smile, centered "
            "face, balanced lighting. "
            f"Product context: {brand} {product}. "
            f"{'Script mood: ' + script_hint + '. ' if script_hint else ''}"
            "Warm autumn milk tea ad feeling, clean background, no text, no logo, no watermark, high detail."
        )

    def _manifest_action_job(self, run_id: str, job_id: str | None) -> BackgroundJob:
        if not job_id:
            raise ValueError("job_id is required for this manifest action")
        job = self.background_jobs.get_job(job_id)
        if job is None:
            raise ValueError("background job not found")
        if job.run_id != run_id:
            raise ValueError("background job does not belong to this run")
        return job

    def _latest_asset_by_type(self, run_id: str, asset_type: str) -> Asset | None:
        matches = [asset for asset in self.assets.list_assets(run_id) if asset.asset_type == asset_type]
        return matches[-1] if matches else None

    def _latest_person_image_asset(self, run_id: str) -> Asset | None:
        assets = self.assets.list_assets(run_id)
        role_matches = [
            asset
            for asset in assets
            if asset.asset_metadata.get("role") == "person_image"
        ]
        if role_matches:
            return role_matches[-1]
        image_matches = [
            asset
            for asset in assets
            if asset.asset_type in {"person_image", "reference_image"}
            and (asset.mime_type or "").startswith("image/")
        ]
        return image_matches[-1] if image_matches else None

    def _stage_run_id(self, run_id: str, stage_key: str) -> str | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        for stage in run.stages:
            if stage.stage_key == stage_key:
                return stage.stage_run_id
        return None

    def _fail_background_job(
        self,
        job: BackgroundJob,
        *,
        error_code: str,
        error_message: str,
        result_payload: dict | None = None,
        allow_auto_retry: bool | None = None,
    ) -> BackgroundJob:
        """Resolve a failure into one of three dispositions (V8.1).

        1. Transient error + retryable job type + attempts left → back into the
           queue with exponential backoff (``job.retry_scheduled``).
        2. Transient error but retries exhausted → dead letter
           (``job.dead_lettered``), replayable via the requeue endpoint.
        3. Anything non-transient (auth/input/runtime/quality, and every risk
           gate) → plain ``failed`` terminal state (``job.failed``).

        ``allow_auto_retry=None`` derives the retryable-job-type decision from
        ``job_type``: only local ``stage_tool_step`` work resubmits itself.
        Avatar submission is a paid provider call, so it never retries
        unattended — an operator presses retry deliberately.
        """
        classification = classify_error(
            error_code=error_code,
            error_message=error_message,
            provider=job.request_payload.get("provider"),
            tool_key=job.request_payload.get("tool_key"),
            status=BackgroundJobStatus.FAILED.value,
        )
        taxonomy_fields = _error_classification_fields(classification)
        retry_eligible = classification.retryable and (
            job.job_type == "stage_tool_step" if allow_auto_retry is None else bool(allow_auto_retry)
        )
        failure_payload = {
            **(result_payload or job.result_payload),
            "error": {
                "error_code": error_code,
                "error_message": error_message,
                **taxonomy_fields,
            },
        }

        if retry_eligible and job.attempt < job.max_attempts:
            backoff_seconds = min(
                settings.worker_retry_backoff_base_seconds * (2 ** (job.attempt - 1)),
                settings.worker_retry_backoff_cap_seconds,
            )
            updated = self.background_jobs.update_job(
                job.job_id,
                status=BackgroundJobStatus.QUEUED,
                progress=0,
                attempt=job.attempt + 1,
                result_payload=failure_payload,
                error_code=error_code,
                error_message=error_message,
                locked_by=None,
                lease_expires_at=None,
                next_run_at=_job_now() + timedelta(seconds=backoff_seconds),
                **taxonomy_fields,
            )
            if updated is None:
                raise ValueError("background job disappeared while scheduling retry")
            self._record_background_job_usage(updated, operation="job_retry_scheduled")
            self.events.append_event(
                run_id=updated.run_id,
                event_type="job.retry_scheduled",
                source_type=EventSourceType.SYSTEM,
                source_id=updated.job_id,
                stage_run_id=updated.stage_run_id,
                severity=EventSeverity.WARNING,
                message="Background job failed; a retry is scheduled with backoff.",
                payload={
                    "job_id": updated.job_id,
                    "job_type": updated.job_type,
                    "error_code": error_code,
                    "error_message": error_message,
                    "attempt": updated.attempt,
                    "next_attempt": updated.attempt + 1,
                    "backoff_seconds": backoff_seconds,
                    **taxonomy_fields,
                },
            )
            self.session.commit()
            return updated

        terminal_status = (
            BackgroundJobStatus.DEAD if retry_eligible else BackgroundJobStatus.FAILED
        )
        updated = self.background_jobs.update_job(
            job.job_id,
            status=terminal_status,
            progress=100,
            result_payload=failure_payload,
            error_code=error_code,
            error_message=error_message,
            locked_by=None,
            lease_expires_at=None,
            clear_next_run_at=True,
            **taxonomy_fields,
        )
        if updated is None:
            raise ValueError("background job disappeared while failing")
        self._record_background_job_usage(
            updated,
            operation="job_dead_lettered" if terminal_status == BackgroundJobStatus.DEAD else "job_failed",
        )
        self.events.append_event(
            run_id=updated.run_id,
            event_type=(
                "job.dead_lettered"
                if terminal_status == BackgroundJobStatus.DEAD
                else "job.failed"
            ),
            source_type=EventSourceType.SYSTEM,
            source_id=updated.job_id,
            stage_run_id=updated.stage_run_id,
            severity=EventSeverity.ERROR,
            message=(
                "Background job exhausted its retries and moved to the dead letter queue."
                if terminal_status == BackgroundJobStatus.DEAD
                else "Background job failed."
            ),
            payload={
                "job_id": updated.job_id,
                "job_type": updated.job_type,
                "error_code": error_code,
                "error_message": error_message,
                "attempt": updated.attempt,
                "max_attempts": updated.max_attempts,
                **taxonomy_fields,
            },
        )
        self.session.commit()
        return updated

    def _write_asset_to_temp(self, asset: Asset, root: Path, default_filename: str) -> Path:
        stored_filename = asset.asset_metadata.get("filename")
        suffix = Path(stored_filename or default_filename).suffix or Path(default_filename).suffix
        path = root / f"{asset.asset_id}{suffix}"
        path.write_bytes(self.storage.read_bytes(asset.storage_url))
        return path

    def _create_generated_asset(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        asset_type: str,
        provider: str,
        tool_key: str,
        filename: str,
        content: bytes,
        mime_type: str,
        duration_ms: int | None,
        asset_metadata: dict,
        width: int | None = None,
        height: int | None = None,
    ) -> Asset:
        asset_id = f"asset_{uuid4().hex[:12]}"
        safe_filename = self._safe_filename(filename)
        object_key = f"runs/{run_id}/{asset_id}/{safe_filename}"
        stored = self.storage.put_bytes(
            object_key=object_key,
            content=content,
            mime_type=mime_type,
        )
        return self.assets.create_asset(
            asset_id=asset_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            asset_type=asset_type,
            provider=provider,
            tool_key=tool_key,
            storage_url=stored.storage_url,
            mime_type=mime_type,
            duration_ms=duration_ms,
            width=width,
            height=height,
            file_size=stored.file_size,
            checksum=stored.checksum,
            asset_metadata={
                **asset_metadata,
                "filename": safe_filename,
                "object_key": stored.object_key,
            },
        )

    def _record_generated_asset_event(
        self,
        invocation: ToolInvocation,
        asset: Asset,
        message: str,
    ) -> None:
        self._record_tool_invocation_usage(
            invocation,
            operation=_usage_operation_for_asset(asset.asset_type),
            asset=asset,
        )
        self.events.append_event(
            run_id=invocation.run_id,
            event_type="tool.completed",
            source_type=EventSourceType.TOOL,
            source_id=invocation.invocation_id,
            stage_run_id=invocation.stage_run_id,
            tool_invocation_id=invocation.invocation_id,
            message=message,
            payload={
                "tool_key": invocation.tool_key,
                "provider": invocation.provider,
                "asset_id": asset.asset_id,
                "asset_type": asset.asset_type,
                "storage_url": asset.storage_url,
            },
        )

    def _safe_filename(self, filename: str) -> str:
        safe_name = Path(filename).name.strip()
        return safe_name or "asset.bin"

    def _source_media_entries_for_analysis(
        self,
        run_id: str,
        *,
        source_asset_ids: list[str],
        source_artifact_type: str,
    ) -> list[dict[str, Any]]:
        if source_asset_ids:
            entries = []
            for asset_id in source_asset_ids:
                asset = self._asset_for_run(run_id, asset_id)
                if asset is None:
                    entries.append({"asset_id": asset_id, "missing": True})
                    continue
                entries.append(
                    {
                        "asset_id": asset.asset_id,
                        "asset_type": asset.asset_type,
                        "media_type": asset.asset_metadata.get("media_type"),
                        "label": asset.asset_metadata.get("label")
                        or asset.asset_metadata.get("filename")
                        or asset.asset_id,
                        "role": asset.asset_metadata.get("role"),
                    }
                )
            return entries

        source_artifact = self._current_artifact_by_type(run_id, source_artifact_type)
        if source_artifact is None and source_artifact_type == "source_asset_manifest":
            source_artifact = self._current_artifact_by_type(run_id, "source_media_review")
        if source_artifact is None:
            return []
        entries = source_artifact.payload.get("source_assets") or []
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def _archive_analysis_frames(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        source_asset: Asset,
        source_index: int,
        analysis: FfmpegMediaAnalysisResult,
        include_frame_assets: bool,
    ) -> list[Asset]:
        if not include_frame_assets:
            return []
        frame_assets: list[Asset] = []
        for frame_index, frame in enumerate(analysis.frames, start=1):
            uploaded = self.upload_asset(
                run_id,
                AssetUpload(
                    asset_type="analysis_frame",
                    filename=(
                        f"{source_asset.asset_id}-analysis-frame-{frame_index:04d}.jpg"
                    ),
                    content_base64=base64.b64encode(frame.path.read_bytes()).decode("ascii"),
                    stage_run_id=stage_run_id,
                    provider="ffmpeg",
                    tool_key="ffmpeg_media_probe",
                    mime_type="image/jpeg",
                    width=frame.width,
                    height=frame.height,
                    asset_metadata={
                        "source_asset_id": source_asset.asset_id,
                        "source_index": source_index,
                        "frame_index": frame_index,
                        "timestamp_seconds": frame.timestamp_seconds,
                        "timestamp_formatted": frame.timestamp_formatted,
                        "role": "analysis_frame",
                    },
                ),
            )
            if uploaded is not None:
                frame_assets.append(uploaded)
        return frame_assets

    def _transcribe_sources(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        payload: MediaAnalysisRequest,
        source_reports: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], ToolInvocation | None]:
        """Transcribe the first addressable audio source, or say why not.

        The ASR engine fetches audio by URL itself, so a deployment-relative
        storage path cannot be handed to it. Absent an explicit
        ``provider_options.asr.audio_url`` override, the honest outcome is an
        *unavailable* transcript with the reason recorded — never an empty
        string that reads like a silent video. Every attempt, success or
        failure, lands in the tool invocation ledger alongside the probe.
        """
        unavailable: dict[str, Any] = {
            "status": "unavailable",
            "text": None,
            "segments": [],
            "engine": None,
            "model": None,
            "source_asset_id": None,
            "reason": None,
        }
        if not payload.transcribe_audio:
            unavailable["reason"] = (
                "transcription disabled by request (transcribe_audio=false)."
            )
            return unavailable, None

        asr_options = payload.provider_options.get("asr") or {}
        overrides = asr_options.get("audio_urls") or {}
        single_override = asr_options.get("audio_url")
        adapter_ready = self.asr_adapter.check_availability()
        if not adapter_ready:
            unavailable["reason"] = (
                "ASR engine is not configured (DASHSCOPE_API_KEY missing); no "
                "transcript was generated."
            )
            return unavailable, None
        candidate = next(
            (
                report
                for report in source_reports
                if report.get("has_audio")
                and (overrides.get(report.get("asset_id")) or single_override)
            ),
            None,
        )
        if candidate is None:
            unavailable["reason"] = (
                "ASR is configured but no analyzed source had a "
                "provider-reachable audio URL; local storage paths cannot be "
                "fetched by the provider. Pass provider_options.asr.audio_url "
                "(or audio_urls keyed by asset id) to enable transcription."
            )
            return unavailable, None

        audio_url = overrides.get(candidate.get("asset_id")) or single_override
        source_asset_id = candidate.get("asset_id")
        request_payload = {
            "audio_url": audio_url,
            "source_asset_id": source_asset_id,
            "model": asr_options.get("model"),
            "language": asr_options.get("language"),
        }
        try:
            result = self.asr_adapter.transcribe(
                {
                    "audio_url": audio_url,
                    "language": asr_options.get("language"),
                    "model": asr_options.get("model"),
                    "provider_options": asr_options.get("provider_options") or {},
                }
            )
        except (RuntimeError, ValueError) as exc:
            unavailable["source_asset_id"] = source_asset_id
            unavailable["reason"] = f"ASR transcription failed: {exc}"
            failed_invocation = self.tool_invocations.create_invocation(
                run_id=run_id,
                stage_run_id=stage_run_id,
                tool_key=self.asr_adapter.tool_key,
                provider=self.asr_adapter.provider_key,
                request_payload=request_payload,
                response_payload={"status": "failed", "error": str(exc)[-500:]},
                status=ToolInvocationStatus.FAILED,
                cost=0.0,
            )
            return unavailable, failed_invocation

        invocation = self.tool_invocations.create_invocation(
            run_id=run_id,
            stage_run_id=stage_run_id,
            tool_key=self.asr_adapter.tool_key,
            provider=self.asr_adapter.provider_key,
            request_payload=request_payload,
            response_payload={
                "status": "succeeded",
                "text_chars": len(result["text"]),
                "model": result["model"],
            },
            status=ToolInvocationStatus.SUCCEEDED,
            cost=0.0,
        )
        return (
            {
                "status": "available",
                "text": result["text"],
                "segments": result["segments"],
                "engine": self.asr_adapter.provider_key,
                "model": result["model"],
                "source_asset_id": source_asset_id,
                "reason": None,
            },
            invocation,
        )

    @staticmethod
    def _media_source_report(
        *,
        source_entry: dict[str, Any],
        asset: Asset,
        analysis: FfmpegMediaAnalysisResult,
        frame_assets: list[Asset],
    ) -> dict[str, Any]:
        frame_samples = [
            {
                "asset_id": frame_asset.asset_id,
                "timestamp_seconds": frame_asset.asset_metadata.get("timestamp_seconds"),
                "timestamp_formatted": frame_asset.asset_metadata.get("timestamp_formatted"),
                "width": frame_asset.width,
                "height": frame_asset.height,
                "storage_url": frame_asset.storage_url,
            }
            for frame_asset in frame_assets
        ]
        return {
            "source_id": source_entry.get("source_id"),
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type,
            "media_type": source_entry.get("media_type") or asset.asset_metadata.get("media_type"),
            "label": source_entry.get("label") or asset.asset_metadata.get("filename") or asset.asset_id,
            "role": source_entry.get("role") or asset.asset_metadata.get("role"),
            "filename": asset.asset_metadata.get("filename"),
            "mime_type": asset.mime_type,
            "duration_seconds": analysis.duration_seconds,
            "width": analysis.width,
            "height": analysis.height,
            "frame_rate": analysis.frame_rate,
            "video_codec": analysis.video_codec,
            "audio_codecs": analysis.audio_codecs,
            "has_video": analysis.has_video,
            "has_audio": analysis.has_audio,
            "file_size": asset.file_size,
            "checksum": asset.checksum,
            "frame_samples": frame_samples,
            "probe": _compact_probe(analysis.probe),
            "warnings": list(analysis.warnings),
            # V1.1 advanced analysis. Scene cuts carry the detector's own
            # threshold so a report can be reproduced; audio energy rides with
            # its window size for the same reason.
            "scene_cut_count": len(analysis.scene_cuts),
            "scene_boundary_count": len(analysis.scene_boundaries),
            "scene_cuts": [
                {
                    "index": cut.index,
                    "timestamp_seconds": cut.timestamp_seconds,
                    "timestamp_formatted": cut.timestamp_formatted,
                    "score": cut.score,
                }
                for cut in analysis.scene_cuts
            ],
            "scene_detection_threshold": analysis.scene_detection_threshold,
            "audio_energy": DatabaseStore._audio_energy_report(analysis.audio_energy)
            if analysis.audio_energy is not None
            else None,
        }

    @staticmethod
    def _audio_energy_report(analysis: Any) -> dict[str, Any]:
        """Flatten the kit's audio analysis into report payload shape.

        Statistics travel whole; only the segment rows are bounded (the kit
        already truncates, the flag is forwarded so a reader can tell).
        """
        return {
            "sample_interval_seconds": analysis.sample_interval_seconds,
            "duration_seconds": analysis.duration_seconds,
            "mean_rms_db": analysis.mean_rms_db,
            "peak_rms_db": analysis.peak_rms_db,
            "dynamic_range_db": analysis.dynamic_range_db,
            "silence_seconds": analysis.silence_seconds,
            "silence_ratio": analysis.silence_ratio,
            "segments": [
                {
                    "kind": segment.kind,
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "duration_seconds": segment.duration_seconds,
                    "peak_rms_db": segment.peak_rms_db,
                }
                for segment in analysis.segments
            ],
            "segments_truncated": analysis.segments_truncated,
            "warnings": list(analysis.warnings),
        }

    @staticmethod
    def _analysis_scene_summary(
        *,
        source_index: int,
        asset: Asset,
        analysis: FfmpegMediaAnalysisResult,
        frame_assets: list[Asset],
    ) -> list[dict[str, Any]]:
        """Build the scene entries for one source asset.

        With detector output, the source tiles into the boundaries FFmpeg
        found — every candidate clip window can then be a subrange of exactly
        one scene. Without it (detector off, no video stream, or no cuts), the
        full-asset entry stays so downstream clip planning keeps working, with
        a note that says *why* instead of a stale "planned next".
        """
        duration_seconds = analysis.duration_seconds or 0.0
        frame_ids_by_time = [
            (frame.asset_metadata.get("timestamp_seconds") or 0.0, frame.asset_id)
            for frame in frame_assets
        ]

        def frames_within(start: float, end: float) -> list[str]:
            return [
                asset_id
                for timestamp, asset_id in frame_ids_by_time
                if start - 1e-6 <= timestamp <= end + 1e-6
            ]

        if analysis.scene_boundaries:
            return [
                {
                    "scene_id": f"scene_{source_index:03d}_{boundary.index:03d}",
                    "source_asset_id": asset.asset_id,
                    "start_seconds": boundary.start_seconds,
                    "end_seconds": boundary.end_seconds,
                    "duration_seconds": boundary.duration_seconds,
                    "detection_method": boundary.detection_method,
                    "confidence": boundary.confidence,
                    "scene_detection_threshold": analysis.scene_detection_threshold,
                    "representative_frame_asset_ids": frames_within(
                        boundary.start_seconds, boundary.end_seconds
                    ),
                }
                for boundary in analysis.scene_boundaries
            ]
        return [
            {
                "scene_id": f"scene_{source_index:03d}_full",
                "source_asset_id": asset.asset_id,
                "start_seconds": 0.0,
                "end_seconds": duration_seconds,
                "duration_seconds": duration_seconds,
                "detection_method": "ffprobe_full_asset_interval_sampling",
                "confidence": None,
                "scene_detection_threshold": None,
                "representative_frame_asset_ids": [
                    frame.asset_id for frame in frame_assets
                ],
                "notes": [
                    {
                        "type": "analysis_scope",
                        "message": (
                            "Scene-boundary detection produced no cuts for this "
                            "source; the whole asset is offered as a single scene."
                        ),
                    }
                ],
            }
        ]

    @staticmethod
    def _media_analysis_summary(
        *,
        requested_source_count: int,
        source_reports: list[dict[str, Any]],
        frame_assets: list[Asset],
        transcript: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        durations = [
            float(report["duration_seconds"])
            for report in source_reports
            if report.get("duration_seconds") is not None
        ]
        transcript = transcript or {}
        audio_reports = [
            report["audio_energy"] for report in source_reports if report.get("audio_energy")
        ]
        # A dB average only makes sense across sources measured the same way;
        # when window sizes differ the honest aggregate is None (each source
        # already carries its own audio_energy block), not an invented mean.
        window_sizes = {
            report.get("sample_interval_seconds") for report in audio_reports
        }
        comparable_audio = len(window_sizes) == 1
        return {
            "status": "completed",
            "requested_source_count": requested_source_count,
            "analyzed_source_count": len(source_reports),
            "skipped_source_count": max(0, requested_source_count - len(source_reports)),
            "total_duration_seconds": round(sum(durations), 3),
            "frame_asset_count": len(frame_assets),
            "has_transcript": transcript.get("status") == "available",
            "transcript_engine": transcript.get("engine"),
            "scene_cut_count": sum(
                len(report.get("scene_cuts") or []) for report in source_reports
            ),
            "scene_boundary_count": sum(
                report.get("scene_boundary_count") or 0 for report in source_reports
            ),
            "audio_analyzed_source_count": len(audio_reports),
            "audio": {
                "analyzed_source_count": len(audio_reports),
                "mean_rms_db": round(
                    sum(
                        report["mean_rms_db"]
                        for report in audio_reports
                        if report.get("mean_rms_db") is not None
                    )
                    / max(
                        1,
                        sum(
                            1
                            for report in audio_reports
                            if report.get("mean_rms_db") is not None
                        ),
                    ),
                    3,
                )
                if audio_reports and comparable_audio
                else None,
                "peak_rms_db": max(
                    (
                        report["peak_rms_db"]
                        for report in audio_reports
                        if report.get("peak_rms_db") is not None
                    ),
                    default=None,
                ),
                "silence_ratio": round(
                    sum(
                        report["silence_ratio"] or 0.0 for report in audio_reports
                    )
                    / max(1, len(audio_reports)),
                    4,
                )
                if audio_reports
                else None,
            },
            "source_asset_ids": [report["asset_id"] for report in source_reports],
            "media_types": sorted(
                {
                    str(report.get("media_type") or report.get("asset_type"))
                    for report in source_reports
                    if report.get("media_type") or report.get("asset_type")
                }
            ),
        }

    @staticmethod
    def _video_analysis_brief_payload(
        *,
        run_id: str,
        run_input: dict[str, Any],
        report_artifact: Artifact,
        report_payload: dict[str, Any],
    ) -> dict[str, Any]:
        transcript = report_payload.get("transcript") or {}
        representative_frame_asset_ids = [
            frame.get("asset_id")
            for frame in report_payload.get("frame_samples", [])
            if frame.get("asset_id")
        ]
        candidate_windows = [
            {
                "scene_id": scene.get("scene_id"),
                "source_asset_id": scene.get("source_asset_id"),
                "start_seconds": scene.get("start_seconds"),
                "end_seconds": scene.get("end_seconds"),
                "duration_seconds": scene.get("duration_seconds"),
                "basis": scene.get("detection_method")
                or "full_asset_interval_sampling",
            }
            for scene in report_payload.get("scenes", [])
        ]
        limitations = []
        if transcript.get("status") != "available":
            limitations.append(
                {
                    "code": "transcript_unavailable",
                    "message": "当前分析未包含 ASR 转写，候选片段只能先基于媒体元数据和抽帧人工判断。",
                }
            )
        # V1.1: this limitation is only true when it is true. With detector
        # output the windows are real scene spans, and claiming they are
        # "pending" would tell the reviewer to distrust data they can trust.
        if not any(
            scene.get("detection_method") == "ffmpeg_scene_detection"
            for scene in report_payload.get("scenes", [])
        ):
            limitations.append(
                {
                    "code": "scene_boundary_detection_pending",
                    "message": "当前分析未产出精确镜头边界（检测器未启用或未检出切点），候选窗口为整段资产。",
                }
            )
        return {
            "version": "1.0",
            "run_id": run_id,
            "summary": {
                **report_payload.get("media_summary", {}),
                "media_analysis_report_artifact_id": report_artifact.artifact_id,
            },
            "source_assets": [
                {
                    "asset_id": source.get("asset_id"),
                    "label": source.get("label"),
                    "media_type": source.get("media_type"),
                    "duration_seconds": source.get("duration_seconds"),
                    "has_audio": source.get("has_audio"),
                    "has_video": source.get("has_video"),
                }
                for source in report_payload.get("source_assets", [])
            ],
            "clip_planning_inputs": {
                "target_duration_seconds": run_input.get("target_duration_seconds"),
                "target_platform": run_input.get("target_platform"),
                "aspect_ratio": run_input.get("aspect_ratio"),
                "transcript_available": transcript.get("status") == "available",
                "scene_boundary_available": any(
                    scene.get("detection_method") == "ffmpeg_scene_detection"
                    for scene in report_payload.get("scenes", [])
                ),
                "candidate_windows": candidate_windows,
                "representative_frame_asset_ids": representative_frame_asset_ids,
            },
            "limitations": limitations,
        }

    @staticmethod
    def _source_media_type(
        *,
        media_type: str | None,
        mime_type: str | None,
        source_name: str,
    ) -> str:
        if media_type in {"video", "audio", "image", "document", "other"}:
            return media_type
        normalized_mime = (mime_type or mimetypes.guess_type(source_name)[0] or "").lower()
        if normalized_mime.startswith("video/"):
            return "video"
        if normalized_mime.startswith("audio/"):
            return "audio"
        if normalized_mime.startswith("image/"):
            return "image"
        if (
            normalized_mime.startswith("text/")
            or normalized_mime
            in {
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-powerpoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
        ):
            return "document"
        suffix = Path(urllib.parse.urlparse(source_name).path or source_name).suffix.lower()
        if suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            return "video"
        if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac"}:
            return "audio"
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            return "image"
        if suffix in {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md"}:
            return "document"
        return "other"

    @staticmethod
    def _source_asset_type(media_type: str) -> str:
        return {
            "video": "source_video",
            "audio": "source_audio",
            "image": "source_image",
            "document": "source_document",
        }.get(media_type, "source_file")

    @staticmethod
    def _merged_source_rights(default_rights: dict[str, Any], rights: dict[str, Any]) -> dict[str, Any]:
        return {**default_rights, **rights}

    def _avatar_video_content(
        self,
        payload: AvatarVideoArchiveRequest,
        video_url: str | None,
    ) -> bytes:
        if payload.video_content_base64:
            try:
                return base64.b64decode(payload.video_content_base64, validate=True)
            except binascii.Error as exc:
                raise ValueError("video_content_base64 is not valid base64") from exc
        if not video_url:
            raise ValueError("video_url or video_content_base64 is required to archive avatar video")
        try:
            with urllib.request.urlopen(video_url, timeout=60) as response:
                return response.read()
        except urllib.error.URLError as exc:
            raise ValueError(f"could not download avatar video: {exc}") from exc

    def _append_run_event(
        self,
        record: ProductionRunRecord,
        event_type: str,
        *,
        message: str,
        payload: dict,
    ) -> None:
        self.events.append_event(
            run_id=record.id,
            event_type=event_type,
            source_type=EventSourceType.RUN,
            source_id=record.id,
            message=message,
            payload=payload,
        )

    def _append_stage_event(
        self,
        record: ProductionRunRecord,
        stage: StageRunRecord,
        event_type: str,
        *,
        message: str,
        payload: dict,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        self.events.append_event(
            run_id=record.id,
            event_type=event_type,
            source_type=EventSourceType.STAGE,
            source_id=stage.id,
            stage_key=stage.stage_key,
            stage_run_id=stage.id,
            severity=severity,
            message=message,
            payload=payload,
        )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# The pre-T74 endpoint used its own vocabulary; map it onto the review kernel.
_APPROVAL_DECISION_MAP = {
    "approved": ReviewDecisionType.APPROVE,
    "approved_with_changes": ReviewDecisionType.APPROVE_WITH_CHANGES,
    "rejected": ReviewDecisionType.REJECT,
    "cancelled": ReviewDecisionType.CANCEL,
}


def _artifact_refs_payload(refs: Any) -> list[dict]:
    """Event payloads must stay JSON serializable; pydantic refs are not."""
    payload: list[dict] = []
    for ref in refs or []:
        if isinstance(ref, dict):
            payload.append(dict(ref))
        else:
            payload.append(ref.model_dump())
    return payload


def _approval_request_decision(value: str) -> ReviewDecisionType | None:
    return _APPROVAL_DECISION_MAP.get(value)


def _sort_datetime(value: datetime | None) -> datetime:
    return as_utc(value) or datetime.min.replace(tzinfo=UTC)


def _tool_invocation_status(status: str | None) -> ToolInvocationStatus:
    if status == ToolInvocationStatus.SUCCEEDED.value:
        return ToolInvocationStatus.SUCCEEDED
    if status == ToolInvocationStatus.FAILED.value:
        return ToolInvocationStatus.FAILED
    if status == ToolInvocationStatus.CANCELLED.value:
        return ToolInvocationStatus.CANCELLED
    return ToolInvocationStatus.RUNNING


def _normalize_provider_callback(
    *,
    provider: str,
    payload: ProviderCallbackRequest,
) -> dict[str, Any]:
    raw_payload = payload.raw_payload or {}
    output = raw_payload.get("output") if isinstance(raw_payload.get("output"), dict) else {}
    provider_status = (
        payload.provider_status
        or payload.status
        or _string_value(raw_payload, "task_status")
        or _string_value(raw_payload, "status")
        or _string_value(output, "task_status")
        or _string_value(output, "status")
    )
    status = _normalize_provider_status(provider_status)
    video_url = (
        payload.video_url
        or _string_value(raw_payload, "video_url")
        or _string_value(output, "video_url")
    )
    error_code = (
        payload.error_code
        or _string_value(raw_payload, "error_code")
        or _string_value(raw_payload, "code")
        or _string_value(output, "error_code")
        or _string_value(output, "code")
    )
    error_message = (
        payload.error_message
        or _string_value(raw_payload, "error_message")
        or _string_value(raw_payload, "message")
        or _string_value(output, "error_message")
        or _string_value(output, "message")
    )
    return {
        "provider": provider,
        "external_task_id": payload.external_task_id,
        "status": status,
        "provider_status": provider_status,
        "video_url": video_url,
        "error_code": error_code,
        "error_message": error_message,
        "raw_response": raw_payload,
    }


def _normalize_provider_status(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"succeeded", "success", "completed", "complete", "finished"}:
        return ToolInvocationStatus.SUCCEEDED.value
    if normalized in {"failed", "failure", "error"}:
        return ToolInvocationStatus.FAILED.value
    if normalized in {"cancelled", "canceled", "cancel"}:
        return ToolInvocationStatus.CANCELLED.value
    return ToolInvocationStatus.RUNNING.value


def _string_value(payload: object, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _provider_error_code(normalized: dict[str, Any]) -> str | None:
    for payload in (normalized, normalized.get("raw_response"), normalized.get("latest_status")):
        if not isinstance(payload, dict):
            continue
        direct = payload.get("code") or payload.get("error_code") or payload.get("errorCode")
        if isinstance(direct, str) and direct:
            return direct
        output = payload.get("output")
        if isinstance(output, dict):
            nested = output.get("code") or output.get("error_code") or output.get("errorCode")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _error_classification_fields(classification: ErrorClassification) -> dict[str, Any]:
    return {
        "error_category": classification.category.value,
        "error_taxonomy_code": classification.code,
        "error_retryable": classification.retryable,
        "error_user_message": classification.user_message,
        "error_remediation": classification.remediation,
    }


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _is_dispatchable_manifest_step(step: dict) -> bool:
    return (
        step.get("auto_dispatch") is True
        and step.get("status") == "planned"
        and bool(step.get("tool_key"))
    )


def _is_stage_ready_for_manifest_dispatch(
    task: dict,
    stage_statuses: dict[str, str],
) -> bool:
    return stage_statuses.get(task.get("stage_key")) in {
        StageStatus.RUNNING.value,
        StageStatus.WAITING_REVIEW.value,
        StageStatus.APPROVED.value,
        StageStatus.COMPLETED.value,
    }


def _manifest_step_key(
    *,
    manifest_artifact: Artifact,
    task: dict,
    step: dict,
) -> str:
    return ":".join(
        [
            manifest_artifact.artifact_id,
            str(task.get("task_id") or task.get("stage_key")),
            str(step.get("step_id") or step.get("capability")),
        ]
    )


def _manifest_job_payload(
    *,
    task: dict,
    step: dict,
    manifest_step_key: str,
) -> dict:
    return {
        "manifest_step_key": manifest_step_key,
        "stage_task_id": task.get("task_id"),
        "stage_key": task.get("stage_key"),
        "tool_step_id": step.get("step_id"),
        "capability": step.get("capability"),
        "tool_key": step.get("tool_key"),
        "provider": step.get("provider"),
        "decision_id": step.get("decision_id"),
        "dispatch_target": step.get("dispatch_target"),
    }


# V8.1 worker scaling: a queue is the job's role affinity — which worker type
# claims it — and priority is a 0-99 ordering where smaller runs first. Cheap
# analysis work defaults ahead of expensive composition so the render lane does
# not starve the analysis lane under load.
_JOB_QUEUE_BY_CAPABILITY = {
    "media_analysis": "analysis",
    "subtitle_generation": "media",
    "video_composition": "render",
    "storage": "render",
}
_JOB_PRIORITY_BY_CAPABILITY = {
    "media_analysis": 40,
    "subtitle_generation": 50,
    "video_composition": 60,
    "storage": 60,
}


# Lease renewed wherever the processor takes or holds a job (RUNNING /
# WAITING_PROVIDER transitions). The claim path uses the worker's
# ``--lease-seconds``; this constant bounds the API-driven process path
# (``POST /jobs/{id}/process``) which never went through a claim.
_JOB_LEASE_SECONDS = 300


def _naive_utc_now() -> datetime:
    """Naive UTC, the shape every stored timestamp has (see ``store_utc``).

    A window boundary has to be compared against ``usage_ledger_entries`` and
    ``production_runs`` columns, which hold naive UTC on both MySQL and SQLite;
    a tz-aware boundary would compare differently on each.

    Named apart from ``_utc_now`` on purpose: that one returns an *ISO string*
    for embedding in JSON payloads, and the two differ by exactly the thing that
    is easy to get wrong — a name collision here made ``_job_now()`` hand a
    string to ``+ timedelta``.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _job_now() -> datetime:
    return _naive_utc_now()


def _quota_override(input_payload: dict[str, Any]) -> dict[str, Any]:
    """The recorded quota exemption on a run, normalised to a dict.

    ``input_payload`` is free-form JSON written by earlier versions of this
    code, so every read defends against the field being absent or something
    other than an object.
    """
    override = (input_payload or {}).get("quota_override") or {}
    return override if isinstance(override, dict) else {}


def _queue_for_job(job_type: str, request_payload: dict) -> str:
    if job_type == "avatar_from_assets":
        return "provider"
    if job_type == "stage_tool_step":
        return _JOB_QUEUE_BY_CAPABILITY.get(request_payload.get("capability"), "media")
    return "media"


def _priority_for_job(job_type: str, request_payload: dict) -> int:
    if job_type == "stage_tool_step":
        return _JOB_PRIORITY_BY_CAPABILITY.get(request_payload.get("capability"), 50)
    return 50


def _background_job_preview(job: BackgroundJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status.value,
        "progress": job.progress,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "error_category": job.error_category,
        "error_taxonomy_code": job.error_taxonomy_code,
        "error_retryable": job.error_retryable,
        "error_user_message": job.error_user_message,
        "error_remediation": job.error_remediation,
    }


def _dispatch_preview_summary(steps: list[dict]) -> dict:
    statuses: dict[str, int] = {}
    for step in steps:
        status = step.get("status") or "unknown"
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "total_steps": len(steps),
        "dispatchable_steps": sum(1 for step in steps if step.get("dispatchable") is True),
        "queued_steps": sum(
            1
            for step in steps
            if step.get("job") is not None
            and step.get("status") not in {"completed", "failed", "cancelled"}
        ),
        "completed_steps": statuses.get(BackgroundJobStatus.COMPLETED.value, 0),
        "existing_output_steps": statuses.get("existing_output", 0),
        "waiting_stage_steps": statuses.get("waiting_stage", 0),
        "waiting_input_steps": statuses.get("waiting_input", 0),
        "failed_steps": statuses.get(BackgroundJobStatus.FAILED.value, 0),
        "statuses": statuses,
    }


def _stage_tool_asset_metadata(job: BackgroundJob) -> dict:
    return {
        "mode": "stage_execution_manifest",
        "background_job_id": job.job_id,
        "manifest_step_key": job.request_payload.get("manifest_step_key"),
        "stage_task_id": job.request_payload.get("stage_task_id"),
        "tool_step_id": job.request_payload.get("tool_step_id"),
    }


def _compact_probe(probe: dict[str, Any]) -> dict[str, Any]:
    format_data = probe.get("format") or {}
    return {
        "format": {
            "format_name": format_data.get("format_name"),
            "duration": format_data.get("duration"),
            "bit_rate": format_data.get("bit_rate"),
            "size": format_data.get("size"),
        },
        "streams": [
            {
                "index": stream.get("index"),
                "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "duration": stream.get("duration"),
                "bit_rate": stream.get("bit_rate"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
            }
            for stream in probe.get("streams", [])
            if isinstance(stream, dict)
        ],
    }


def _usage_operation_for_asset(asset_type: str) -> str:
    return {
        "tts_audio": "generate_tts_audio",
        "subtitle_file": "generate_subtitles",
        "background_music": "generate_music",
        "reference_image": "generate_image",
        "rendered_video": "compose_video",
        "avatar_video_clip": "archive_avatar_video",
    }.get(asset_type, f"generate_{asset_type}")


def _usage_totals(entries: list[UsageLedgerEntry]) -> dict[str, float | int]:
    return {
        "estimated_cost": round(
            sum(entry.estimated_cost or 0.0 for entry in entries),
            6,
        ),
        "actual_cost": round(sum(entry.actual_cost or 0.0 for entry in entries), 6),
        "input_tokens": sum(entry.input_tokens or 0 for entry in entries),
        "output_tokens": sum(entry.output_tokens or 0 for entry in entries),
        "duration_ms": sum(entry.duration_ms or 0 for entry in entries),
        "asset_bytes": sum(entry.asset_bytes or 0 for entry in entries),
    }


def _observability_spend(entry: UsageLedgerEntry) -> float:
    """The money this entry cost, for panel display (V8.2).

    Providers report the final price on callback, so ``actual_cost`` wins
    when it is present; an entry that only ever got an estimate (a queued
    or pre-confirmation row) shows that estimate instead of pretending the
    spend was zero.
    """
    actual = float(entry.actual_cost or 0.0)
    if actual:
        return actual
    return float(entry.estimated_cost or 0.0)


def _observability_cost(entries: list[UsageLedgerEntry]) -> ObservabilityCost:
    def _group_total(key: str) -> dict[str, float]:
        groups: dict[str, float] = {}
        for entry in entries:
            value = getattr(entry, key) or "unknown"
            groups[str(value)] = groups.get(str(value), 0.0) + _observability_spend(entry)
        return {name: round(total, 6) for name, total in sorted(groups.items())}

    return ObservabilityCost(
        estimated_total=round(sum(float(entry.estimated_cost or 0.0) for entry in entries), 6),
        actual_total=round(sum(float(entry.actual_cost or 0.0) for entry in entries), 6),
        by_provider=_group_total("provider"),
        by_tool_key=_group_total("tool_key"),
        by_source_type=_group_total("source_type"),
    )


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """Nearest-rank percentile over an already-sorted sample."""
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, round(quantile * len(sorted_values)) - 1))
    return sorted_values[index]


def _observability_latency(entries: list[UsageLedgerEntry]) -> ObservabilityLatency:
    samples = sorted(
        float(entry.duration_ms) for entry in entries if entry.duration_ms is not None
    )
    if not samples:
        return ObservabilityLatency()
    return ObservabilityLatency(
        sample_count=len(samples),
        avg_ms=round(sum(samples) / len(samples), 1),
        p50_ms=round(_percentile(samples, 0.50), 1),
        p95_ms=round(_percentile(samples, 0.95), 1),
    )


def _usage_group_totals(
    entries: list[UsageLedgerEntry],
    key: str,
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[UsageLedgerEntry]] = {}
    for entry in entries:
        value = getattr(entry, key)
        if value is None:
            value = "unknown"
        groups.setdefault(str(value), []).append(entry)
    return {
        group_key: {
            "ledger_entry_count": len(group_entries),
            **_usage_totals(group_entries),
        }
        for group_key, group_entries in sorted(groups.items())
    }


def _render_target_settings(graph: dict[str, Any], runtime: str) -> dict[str, Any]:
    for target in graph.get("render_targets") or []:
        if target.get("runtime") == runtime:
            return target.get("settings") or {}
    raise ValueError(f"composition graph does not declare {runtime} render settings")


def _artifact_reference(artifact: Artifact | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "version": artifact.version,
    }


def _is_video_asset(asset: Asset) -> bool:
    return (asset.mime_type or "").startswith("video/") or asset.asset_type in {
        "rendered_video",
        "edited_video",
        "avatar_video_clip",
        "source_video",
    }


def _default_edit_filename(operation: EditOperation) -> str:
    if operation == EditOperation.COVER_GENERATION:
        return "cover.jpg"
    return f"{operation.value.replace('_', '-')}.mp4"


def _dashboard_job_summary(job: BackgroundJobRecord) -> DashboardJobSummary:
    return DashboardJobSummary(
        job_id=job.id,
        run_id=job.run_id,
        stage_run_id=job.stage_run_id,
        job_type=job.job_type,
        status=BackgroundJobStatus(job.status),
        progress=job.progress,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        error_code=job.error_code,
        error_message=job.error_message,
        error_category=job.error_category,
        error_user_message=job.error_user_message,
        updated_at=job.updated_at,
    )


def _dashboard_review_item(
    run: ProductionRunRecord,
    stage: StageRunRecord,
    *,
    review_request: ReviewRequest | None = None,
    invalidated: bool = False,
) -> DashboardReviewItem:
    return DashboardReviewItem(
        run_id=run.id,
        project_id=run.project_id,
        stage_run_id=stage.id,
        stage_key=stage.stage_key,
        stage_status=StageStatus(stage.status),
        run_status=RunStatus(run.status),
        requires_human_review=stage.requires_human_review,
        updated_at=stage.updated_at,
        workspace_url=(
            f"/workspace?review_request_id={review_request.review_request_id}"
            if review_request is not None
            else f"/workspace?run_id={run.id}"
        ),
        review_request_id=(
            review_request.review_request_id if review_request is not None else None
        ),
        iteration=review_request.iteration if review_request is not None else None,
        review_status=review_request.status if review_request is not None else None,
        current_decision=(
            review_request.current_decision if review_request is not None else None
        ),
        assignee_user_id=(
            review_request.assignee_user_id if review_request is not None else None
        ),
        due_at=review_request.due_at if review_request is not None else None,
        overdue=bool(review_request.overdue) if review_request is not None else False,
        invalidated=invalidated,
    )


def _dashboard_run_matches(record: ProductionRunRecord, query: str) -> bool:
    input_payload = record.input_payload or {}
    values = [
        record.id,
        record.pipeline_key,
        record.status,
        record.current_stage_key,
        record.target_platform,
        input_payload.get("brand_name"),
        input_payload.get("product_name"),
        input_payload.get("raw_script"),
    ]
    return any(query in str(value).lower() for value in values if value)


def _dashboard_payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dashboard_script_preview(value: Any, *, max_length: int = 120) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def _dashboard_sort_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _run_board_artifact_summary(record: ArtifactRecord) -> RunBoardArtifactSummary:
    title, summary = _run_board_artifact_text(record)
    return RunBoardArtifactSummary(
        artifact_id=record.id,
        artifact_type=record.artifact_type,
        version=record.version,
        stage_key=record.stage_key,
        schema_key=record.schema_key,
        producer=record.producer,
        is_current=record.is_current,
        created_at=record.created_at,
        title=title,
        summary=summary,
    )


def _run_board_asset_summary(record: AssetRecord) -> RunBoardAssetSummary:
    return RunBoardAssetSummary(
        asset_id=record.id,
        asset_type=record.asset_type,
        stage_run_id=record.stage_run_id,
        provider=record.provider,
        tool_key=record.tool_key,
        mime_type=record.mime_type,
        duration_ms=record.duration_ms,
        width=record.width,
        height=record.height,
        file_size=record.file_size,
        created_at=record.created_at,
        previewable=_run_board_asset_previewable(record),
        download_url=f"/api/assets/{record.id}/download",
    )


def _run_board_artifact_text(record: ArtifactRecord) -> tuple[str | None, str | None]:
    payload = record.payload or {}
    summary_payload = payload.get("summary")
    title = payload.get("title") or payload.get("name")
    if title is None and isinstance(summary_payload, dict):
        title = summary_payload.get("title")
    summary_candidates = [
        payload.get("summary") if isinstance(payload.get("summary"), str) else None,
        payload.get("status"),
        payload.get("message"),
        payload.get("render_asset_id"),
        payload.get("quality_score"),
    ]
    summary = next((str(value) for value in summary_candidates if value is not None), None)
    return str(title) if title else None, summary


def _run_board_asset_previewable(record: AssetRecord) -> bool:
    mime_type = (record.mime_type or "").lower()
    if mime_type.startswith(("video/", "audio/", "image/")):
        return True
    return record.asset_type in {
        "rendered_video",
        "edited_video",
        "avatar_video_clip",
        "cover_image",
        "html_canvas_preview",
    }


def _group_by_stage_key(
    items: list[RunBoardArtifactSummary],
) -> dict[str, list[RunBoardArtifactSummary]]:
    grouped: dict[str, list[RunBoardArtifactSummary]] = defaultdict(list)
    for item in items:
        grouped[item.stage_key].append(item)
    return grouped


def _group_by_stage_run_id(items: list[Any]) -> dict[str | None, list[Any]]:
    grouped: dict[str | None, list[Any]] = defaultdict(list)
    for item in items:
        grouped[getattr(item, "stage_run_id", None)].append(item)
    return grouped


def _run_board_input_maps(
    dispatch_preview: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    issues_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    actions_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in dispatch_preview.get("steps", []):
        stage_key = step.get("stage_key") or "unknown"
        status = step.get("status")
        if status in {"waiting_input", "waiting_stage", "blocked", "unsupported"}:
            issues_by_stage[stage_key].append(
                {
                    "manifest_step_key": step.get("manifest_step_key"),
                    "stage_key": stage_key,
                    "capability": step.get("capability"),
                    "tool_key": step.get("tool_key"),
                    "status": status,
                    "reason": step.get("reason"),
                    "missing_inputs": step.get("missing_inputs", []),
                }
            )
        actions_by_stage[stage_key].extend(_run_board_actions_for_step(step))
    return dict(issues_by_stage), {
        stage_key: _dedupe_run_board_actions(actions)
        for stage_key, actions in actions_by_stage.items()
    }


def _run_board_actions_for_step(step: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    stage_key = step.get("stage_key")
    if step.get("dispatchable") is True:
        actions.append(
            {
                "action": "dispatch_ready_steps",
                "label": "派发就绪步骤",
                "stage_key": stage_key,
                "manifest_step_key": step.get("manifest_step_key"),
            }
        )
    job = step.get("job") or {}
    if job.get("status") == BackgroundJobStatus.FAILED.value:
        actions.append(
            {
                "action": "retry_job",
                "label": "重试失败任务",
                "stage_key": stage_key,
                "job_id": job.get("job_id"),
            }
        )
    missing = set(step.get("missing_inputs") or [])
    if "person_image" in missing:
        actions.extend(
            [
                {
                    "action": "generate_person_image",
                    "label": "生成人物图",
                    "stage_key": stage_key,
                    "manifest_step_key": step.get("manifest_step_key"),
                },
                {
                    "action": "upload_person_image",
                    "label": "上传人物图",
                    "stage_key": stage_key,
                    "manifest_step_key": step.get("manifest_step_key"),
                },
            ]
        )
    if "tts_audio" in missing:
        actions.append(
            {
                "action": "generate_tts",
                "label": "生成口播音频",
                "stage_key": stage_key,
                "manifest_step_key": step.get("manifest_step_key"),
            }
        )
    if "source_media" in missing:
        actions.append(
            {
                "action": "upload_source_media",
                "label": "上传源素材",
                "stage_key": stage_key,
                "manifest_step_key": step.get("manifest_step_key"),
            }
        )
    return actions


def _dedupe_run_board_actions(actions: Any) -> list[dict[str, Any]]:
    seen = set()
    deduped = []
    for action in actions:
        key = (
            action.get("action"),
            action.get("stage_key"),
            action.get("manifest_step_key"),
            action.get("job_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _run_board_decision_log(artifact_records: list[ArtifactRecord]) -> list[dict[str, Any]]:
    decisions = []
    for record in artifact_records:
        if record.artifact_type != "execution_plan" or not record.is_current:
            continue
        for decision in (record.payload or {}).get("decision_log", []):
            decisions.append(
                {
                    **decision,
                    "artifact_id": record.id,
                    "artifact_version": record.version,
                }
            )
    return decisions


def _run_board_final_outputs(
    assets: list[RunBoardAssetSummary],
) -> list[RunBoardAssetSummary]:
    output_types = {
        "rendered_video",
        "edited_video",
        "cover_image",
        "html_canvas_preview",
    }
    return [
        asset
        for asset in assets
        if asset.asset_type in output_types
        or (asset.mime_type or "").startswith("video/")
    ]


def _run_board_version_groups(
    assets: list[RunBoardAssetSummary],
) -> list[RunBoardVersionGroup]:
    labels = {
        "rendered_video": "渲染成片",
        "edited_video": "编辑版本",
        "cover_image": "封面图",
        "html_canvas_preview": "HTML/Canvas 预览",
        "other_video": "其他视频",
    }
    grouped: dict[str, list[RunBoardAssetSummary]] = defaultdict(list)
    for asset in assets:
        key = asset.asset_type if asset.asset_type in labels else "other_video"
        grouped[key].append(asset)
    return [
        RunBoardVersionGroup(
            group_key=key,
            label=labels.get(key, key),
            assets=group_assets,
        )
        for key, group_assets in grouped.items()
    ]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _cost_budget_limit(input_payload: dict[str, Any]) -> float | None:
    """Backwards-compatible alias for the shared budget reader."""

    return declared_budget_limit(input_payload)


def _token_counts_for_invocation(invocation: ToolInvocation) -> dict[str, int | None]:
    usage = invocation.response_payload.get("usage") or {}
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    if input_tokens is None:
        input_tokens = _rough_token_count(invocation.request_payload)
    if output_tokens is None:
        output_tokens = _rough_token_count(invocation.response_payload)
    return {
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
    }


def _rough_token_count(value: Any) -> int | None:
    texts: list[str] = []

    def collect(candidate: Any) -> None:
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped:
                texts.append(stripped)
            return
        if isinstance(candidate, dict):
            for nested in candidate.values():
                collect(nested)
            return
        if isinstance(candidate, list):
            for nested in candidate:
                collect(nested)

    collect(value)
    if not texts:
        return None
    return max(1, round(sum(len(text) for text in texts) / 4))


def _job_duration_ms(job: BackgroundJob) -> int | None:
    if job.created_at is None or job.updated_at is None:
        return None
    return max(0, round((job.updated_at - job.created_at).total_seconds() * 1000))


def _job_result_asset_bytes(store: DatabaseStore, job: BackgroundJob) -> int | None:
    for key in ["asset_id", "render_asset_id", "avatar_asset_id"]:
        asset_id = job.result_payload.get(key)
        if not isinstance(asset_id, str):
            continue
        asset = store.assets.get_asset(asset_id)
        if asset is not None:
            return asset.file_size
    archived = job.result_payload.get("archived_asset")
    if isinstance(archived, dict) and isinstance(archived.get("file_size"), int):
        return archived["file_size"]
    return None


def _build_srt(segments: list[dict], *, max_chars_per_caption: int) -> str:
    entries = []
    for index, segment in enumerate(segments, start=1):
        start = _seconds_to_srt_timestamp(float(segment.get("start_seconds", 0)))
        end = _seconds_to_srt_timestamp(float(segment.get("end_seconds", 0)))
        caption = str(segment.get("caption") or segment.get("text") or "").strip()
        entries.append(
            "\n".join(
                [
                    str(index),
                    f"{start} --> {end}",
                    _wrap_caption(caption, max_chars_per_caption=max_chars_per_caption),
                ]
            )
        )
    return "\n\n".join(entries) + "\n"


def _seconds_to_srt_timestamp(seconds: float) -> str:
    milliseconds_total = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_value, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d},{milliseconds:03d}"


def _wrap_caption(caption: str, *, max_chars_per_caption: int) -> str:
    if len(caption) <= max_chars_per_caption:
        return caption
    return "\n".join(
        caption[index : index + max_chars_per_caption]
        for index in range(0, len(caption), max_chars_per_caption)
    )
