from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from apps.api.app.actor import (
    ActorContext,
    api_key_scopes,
    embed_session,
    resolve_actor,
    session_identity,
)
from apps.api.app.config import settings
from apps.api.app.db import get_session
from apps.api.app.db_store import DatabaseStore, EmbedMintOutcome
from apps.api.app.embed import embed_url
from apps.api.app.errors import ApiError, ErrorCode, error_catalog_table
from apps.api.app.models import (
    ActorProfile,
    ApiKey,
    ApiKeyCreate,
    ApiKeyCreated,
    ApprovalRequest,
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
    AssignReviewRequest,
    AuditLogEntry,
    AuthSessionInfo,
    AvatarVideoArchiveRequest,
    AvatarVideoFromAssetsRequest,
    AvatarVideoSubmitRequest,
    BackgroundJobStatus,
    BrandKit,
    BrandKitCreate,
    BrandKitUpdate,
    ComposeVideoRequest,
    CompositionGraphBuildRequest,
    CreateReviewAttachment,
    CreateReviewComment,
    DeploymentBackupRequest,
    DeploymentOverview,
    DeploymentSettings,
    DeploymentSettingsUpdate,
    EditToolRequest,
    EmbedContext,
    EmbedSessionCreate,
    EmbedSessionDescription,
    EmbedSessionIssue,
    ErrorCodeDefinition,
    EventCreate,
    EventSourceType,
    ExternalIntegration,
    ExternalIntegrationCreate,
    ExternalIntegrationUpdate,
    HtmlCanvasPreviewRequest,
    IdempotencyKeyEntry,
    ImageGenerateRequest,
    LoginRequest,
    LoginResult,
    LoginUser,
    LogoutResult,
    MaintenanceModeRequest,
    ManifestActionRequest,
    ManifestActionResult,
    MediaAnalysisRequest,
    MediaAnalysisResult,
    MusicGenerateRequest,
    MvpSampleRequest,
    MvpSampleResult,
    Page,
    PasswordSetRequest,
    PermissionMatrix,
    Project,
    ProjectAssetLibrary,
    ProjectCreate,
    ProjectDashboard,
    ProjectDashboardProjectSummary,
    ProjectMemberAdd,
    ProjectMembership,
    ProjectMemberUpdate,
    ProjectObservability,
    ProjectTeamAssign,
    ProviderCallbackRequest,
    ProviderCallbackResult,
    ProviderConsentRequest,
    ProviderCredential,
    ProviderCredentialCheckResult,
    ProviderCredentialCreate,
    ProviderCredentialRotation,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    QualityGateRequest,
    QuotaOverride,
    QuotaOverrideRequest,
    ReviewAttachment,
    ReviewComment,
    ReviewInbox,
    ReviewRequest,
    ReviewRequestCreate,
    ReviewRequestDetail,
    ReviewRequestStatus,
    RunBoard,
    RunCopyRequest,
    RunCopyResult,
    RunCreate,
    RunDecisionLog,
    RunStateActionRequest,
    RunStateActionResult,
    RunStatus,
    ScriptScreeningParseRequest,
    ScriptScreeningParseResult,
    ScriptScreeningRunCreateRequest,
    ScriptScreeningRunCreateResult,
    SourceMediaIngestRequest,
    SourceMediaIngestResult,
    StartRunRequest,
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
    TeamQuotaUpdate,
    TeamUpdate,
    TtsGenerateRequest,
    UpdateReviewComment,
    UsageSummary,
    WebhookDelivery,
    WebhookEventDefinition,
    WebhookSubscription,
    WebhookSubscriptionCreate,
    WebhookSubscriptionUpdate,
)
from apps.api.app.mvp_sample import MvpSampleRunner
from apps.api.app.passwords import hash_password
from apps.api.app.permissions import (
    coerce_project_role,
    coerce_team_role,
    permission_matrix,
)
from apps.api.app.repositories import (
    ApiKeyOutcome,
    AssetReuseOutcome,
    CollectionAddOutcome,
    CollectionImportOutcome,
    ExternalIntegrationOutcome,
    ProjectMembershipOutcome,
    ProjectTeamOutcome,
    ReviewAttachmentOutcome,
    TeamMembershipOutcome,
    WebhookSubscriptionOutcome,
)
from apps.api.app.review_gate import ReviewGateError
from apps.api.app.run_gate import RunGateError
from apps.api.app.secrets import signature_matches
from apps.api.app.sorting import (
    API_KEY_SORT,
    AUDIT_LOG_SORT,
    EXTERNAL_INTEGRATION_SORT,
    IDEMPOTENCY_KEY_SORT,
    WEBHOOK_DELIVERY_SORT,
    WEBHOOK_SUBSCRIPTION_SORT,
    SortValidationError,
)
from apps.api.app.webhooks import webhook_event_catalog
from packages.orchestration_kit.error_taxonomy import error_taxonomy_table
from packages.orchestration_kit.state_machine import run_state_transition_table
from packages.pipeline_kit.loader import PipelineLoader
from packages.render_profile_kit import get_render_profile, list_render_profiles
from packages.tool_kit.contracts import ToolStatus

router = APIRouter()
loader = PipelineLoader()
SessionDep = Annotated[Session, Depends(get_session)]


def get_store(session: SessionDep) -> DatabaseStore:
    return DatabaseStore(session)


StoreDep = Annotated[DatabaseStore, Depends(get_store)]


def get_actor(request: Request) -> ActorContext:
    """The request identity, as resolved from the trusted header or the fallback."""
    return resolve_actor(request)


ActorDep = Annotated[ActorContext, Depends(get_actor)]


def _review_gate_http_error(exc: ReviewGateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_payload())


def _run_gate_http_error(exc: RunGateError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_payload())


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "script2video-studio-api"}


@router.get("/error-taxonomy")
def list_error_taxonomy() -> list[dict]:
    return error_taxonomy_table()


@router.get("/error-codes", response_model=list[ErrorCodeDefinition])
def list_error_codes() -> list[ErrorCodeDefinition]:
    """The published API error contract (V7.1).

    ``error-taxonomy`` above classifies *provider* failures; this one lists the
    codes the API itself can return, so an SDK can discover them without reading
    source.
    """
    return [ErrorCodeDefinition(**entry) for entry in error_catalog_table()]


@router.get("/render-profiles")
def list_profiles() -> list[dict]:
    return list_render_profiles()


@router.get("/render-profiles/{profile_key}")
def get_profile(profile_key: str) -> dict:
    profile = get_render_profile(profile_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="render profile not found")
    return profile


@router.get("/pipelines")
def list_pipelines() -> list[dict]:
    return [pipeline.model_dump() for pipeline in loader.list_pipelines()]


@router.get("/tools")
def list_tools(
    store: StoreDep,
    capability: str | None = None,
    provider: str | None = None,
    status: ToolStatus | None = None,
) -> list[dict]:
    return store.list_tools(capability=capability, provider=provider, status=status)


@router.get("/tools/capabilities")
def list_tool_capabilities(store: StoreDep) -> list[dict]:
    return store.list_tool_capabilities()


@router.get("/tools/providers")
def list_tool_providers(store: StoreDep) -> list[dict]:
    return store.list_tool_providers()


@router.get("/tools/{tool_key}")
def get_tool(tool_key: str, store: StoreDep) -> dict:
    tool = store.get_tool(tool_key)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    return tool


@router.post("/provider-credentials", response_model=ProviderCredential)
def create_provider_credential(
    payload: ProviderCredentialCreate,
    store: StoreDep,
    actor: ActorDep,
) -> ProviderCredential:
    return store.create_provider_credential(payload, actor_id=actor.actor_id)


@router.get("/provider-credentials", response_model=list[ProviderCredential])
def list_provider_credentials(
    store: StoreDep,
    actor: ActorDep,
    provider: str | None = None,
    status: ProviderCredentialStatus | None = None,
) -> list[ProviderCredential]:
    return store.list_provider_credentials(
        provider=provider, status=status, tenant_id=actor.tenant_id
    )


@router.get("/provider-credentials/{credential_id}", response_model=ProviderCredential)
def get_provider_credential(
    credential_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ProviderCredential:
    credential = store.get_provider_credential(credential_id, tenant_id=actor.tenant_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="provider credential not found")
    return credential


@router.patch("/provider-credentials/{credential_id}", response_model=ProviderCredential)
def update_provider_credential(
    credential_id: str,
    payload: ProviderCredentialUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> ProviderCredential:
    credential = store.update_provider_credential(
        credential_id, payload, actor_id=actor.actor_id
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="provider credential not found")
    return credential


@router.post(
    "/provider-credentials/{credential_id}/check",
    response_model=ProviderCredentialCheckResult,
)
def check_provider_credential(
    credential_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ProviderCredentialCheckResult:
    result = store.check_provider_credential(credential_id, actor_id=actor.actor_id)
    if result is None:
        raise HTTPException(status_code=404, detail="provider credential not found")
    return result


@router.post(
    "/provider-credentials/{credential_id}/rotate",
    response_model=ProviderCredentialRotation,
)
def rotate_provider_credential(
    credential_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ProviderCredentialRotation:
    """Seal this credential's secrets under the configured key (V8.4).

    Idempotent, and the endpoint that retires pre-V8.4 inline secrets as well as
    the one that completes a master-key rotation.
    """
    rotation = store.rotate_provider_credential(credential_id, actor_id=actor.actor_id)
    if rotation is None:
        raise HTTPException(status_code=404, detail="provider credential not found")
    return rotation


@router.post("/provider-callbacks/{provider}", response_model=ProviderCallbackResult)
async def handle_provider_callback(
    provider: str,
    payload: ProviderCallbackRequest,
    request: Request,
    store: StoreDep,
) -> ProviderCallbackResult:
    # The caller is the upstream provider, not a platform identity, so this
    # endpoint cannot be permission-gated — there is no actor to evaluate. What
    # can be checked is that the message came from whoever holds the shared
    # secret, which is the same `t=...,v1=...` scheme the outbound webhooks use.
    _verify_provider_callback(request, await request.body())
    try:
        return store.handle_provider_callback(provider, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _verify_provider_callback(request: Request, body: bytes) -> None:
    """Enforce the configured callback signature, if one is configured.

    An unset ``PROVIDER_CALLBACK_SECRET`` accepts the request: the platform
    cannot decide on the upstream's behalf whether it signs, and every local run
    would otherwise need a secret to complete a callback. The moment a secret is
    set, though, an unsigned callback is refused rather than merely logged.
    """
    secret = settings.provider_callback_secret
    if not secret:
        return
    if not signature_matches(
        secret,
        header=request.headers.get("X-S2V-Signature"),
        body=body,
        tolerance_seconds=settings.provider_callback_tolerance_seconds,
    ):
        # One message for missing, malformed, stale and wrong-key: telling them
        # apart would turn the endpoint into an oracle for guessing the secret.
        raise ApiError(
            ErrorCode.PROVIDER_CALLBACK_INVALID,
            "provider callback signature is missing, invalid or stale",
        )


@router.post("/mvp/sample-runs", response_model=MvpSampleResult)
def create_mvp_sample_run(payload: MvpSampleRequest, store: StoreDep) -> MvpSampleResult:
    try:
        return MvpSampleRunner(store).run(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/projects", response_model=Project)
def create_project(
    payload: ProjectCreate, store: StoreDep, actor: ActorDep
) -> Project:
    # T97: the project is created in the caller's tenant. For the default actor
    # that is the default tenant — byte-identical to the old behaviour.
    return store.create_project(payload, tenant_id=actor.tenant_id)


@router.get("/projects", response_model=list[ProjectDashboardProjectSummary])
def list_projects(store: StoreDep, actor: ActorDep) -> list[ProjectDashboardProjectSummary]:
    return store.list_project_summaries(tenant_id=actor.tenant_id)


@router.post("/script-screening-room/parse", response_model=ScriptScreeningParseResult)
def parse_script_screening_room(
    payload: ScriptScreeningParseRequest,
    store: StoreDep,
) -> ScriptScreeningParseResult:
    try:
        return store.parse_script_screening_room(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: str, store: StoreDep) -> Project:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("/projects/{project_id}/dashboard", response_model=ProjectDashboard)
def get_project_dashboard(
    project_id: str,
    store: StoreDep,
    status: RunStatus | None = None,
    search: str | None = None,
    limit: int = 50,
) -> ProjectDashboard:
    dashboard = store.project_dashboard(
        project_id,
        status=status,
        search=search,
        limit=limit,
    )
    if dashboard is None:
        raise HTTPException(status_code=404, detail="project not found")
    return dashboard


@router.get("/projects/{project_id}/observability", response_model=ProjectObservability)
def get_project_observability(
    project_id: str,
    store: StoreDep,
) -> ProjectObservability:
    """Cost / latency / error picture of one project (V8.2 observability)."""
    summary = store.project_observability(project_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="project not found")
    return summary


@router.post("/projects/{project_id}/brand-kits", response_model=BrandKit)
def create_brand_kit(
    project_id: str,
    payload: BrandKitCreate,
    store: StoreDep,
) -> BrandKit:
    brand_kit = store.create_brand_kit(project_id, payload)
    if brand_kit is None:
        raise HTTPException(status_code=404, detail="project not found")
    return brand_kit


@router.get("/projects/{project_id}/brand-kits", response_model=list[BrandKit])
def list_brand_kits(project_id: str, store: StoreDep) -> list[BrandKit]:
    brand_kits = store.list_brand_kits(project_id)
    if brand_kits is None:
        raise HTTPException(status_code=404, detail="project not found")
    return brand_kits


@router.get("/brand-kits/{brand_kit_id}", response_model=BrandKit)
def get_brand_kit(brand_kit_id: str, store: StoreDep) -> BrandKit:
    brand_kit = store.get_brand_kit(brand_kit_id)
    if brand_kit is None:
        raise HTTPException(status_code=404, detail="brand kit not found")
    return brand_kit


@router.patch("/brand-kits/{brand_kit_id}", response_model=BrandKit)
def update_brand_kit(
    brand_kit_id: str,
    payload: BrandKitUpdate,
    store: StoreDep,
) -> BrandKit:
    brand_kit = store.update_brand_kit(brand_kit_id, payload)
    if brand_kit is None:
        raise HTTPException(status_code=404, detail="brand kit not found")
    return brand_kit


@router.post("/projects/{project_id}/style-presets", response_model=StylePreset)
def create_style_preset(
    project_id: str,
    payload: StylePresetCreate,
    store: StoreDep,
) -> StylePreset:
    try:
        style_preset = store.create_style_preset(project_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if style_preset is None:
        raise HTTPException(status_code=404, detail="project not found")
    return style_preset


@router.get("/projects/{project_id}/style-presets", response_model=list[StylePreset])
def list_style_presets(project_id: str, store: StoreDep) -> list[StylePreset]:
    style_presets = store.list_style_presets(project_id)
    if style_presets is None:
        raise HTTPException(status_code=404, detail="project not found")
    return style_presets


@router.post(
    "/projects/{project_id}/style-presets/{style_preset_id}/default",
    response_model=StylePreset,
)
def set_project_default_style_preset(
    project_id: str,
    style_preset_id: str,
    store: StoreDep,
) -> StylePreset:
    style_preset = store.set_project_default_style_preset(project_id, style_preset_id)
    if style_preset is None:
        raise HTTPException(status_code=404, detail="style preset not found for project")
    return style_preset


@router.get("/style-presets/{style_preset_id}", response_model=StylePreset)
def get_style_preset(style_preset_id: str, store: StoreDep) -> StylePreset:
    style_preset = store.get_style_preset(style_preset_id)
    if style_preset is None:
        raise HTTPException(status_code=404, detail="style preset not found")
    return style_preset


@router.patch("/style-presets/{style_preset_id}", response_model=StylePreset)
def update_style_preset(
    style_preset_id: str,
    payload: StylePresetUpdate,
    store: StoreDep,
) -> StylePreset:
    try:
        style_preset = store.update_style_preset(style_preset_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if style_preset is None:
        raise HTTPException(status_code=404, detail="style preset not found")
    return style_preset


@router.post(
    "/projects/{project_id}/script-screening-room/runs",
    response_model=ScriptScreeningRunCreateResult,
)
def create_script_screening_room_run(
    project_id: str,
    payload: ScriptScreeningRunCreateRequest,
    store: StoreDep,
) -> ScriptScreeningRunCreateResult:
    try:
        pipeline = loader.get_pipeline(payload.pipeline_key)
        result = store.create_script_screening_room_run(
            project_id=project_id,
            payload=payload,
            pipeline=pipeline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="project not found")
    return result


@router.post("/projects/{project_id}/asset-collections", response_model=AssetCollection)
def create_asset_collection(
    project_id: str,
    payload: AssetCollectionCreate,
    store: StoreDep,
) -> AssetCollection:
    collection = store.create_asset_collection(project_id, payload)
    if collection is None:
        raise HTTPException(status_code=404, detail="project not found")
    return collection


@router.get("/projects/{project_id}/asset-collections", response_model=list[AssetCollection])
def list_asset_collections(
    project_id: str,
    store: StoreDep,
    kind: str | None = None,
    include_archived: bool = False,
) -> list[AssetCollection]:
    collections = store.list_asset_collections(
        project_id,
        kind=kind,
        include_archived=include_archived,
    )
    if collections is None:
        raise HTTPException(status_code=404, detail="project not found")
    return collections


@router.get("/projects/{project_id}/asset-library", response_model=ProjectAssetLibrary)
def get_project_asset_library(project_id: str, store: StoreDep) -> ProjectAssetLibrary:
    library = store.project_asset_library(project_id)
    if library is None:
        raise HTTPException(status_code=404, detail="project not found")
    return library


@router.get("/asset-collections/{collection_id}", response_model=AssetCollection)
def get_asset_collection(
    collection_id: str,
    store: StoreDep,
    with_items: bool = True,
) -> AssetCollection:
    collection = store.get_asset_collection(collection_id, with_items=with_items)
    if collection is None:
        raise HTTPException(status_code=404, detail="asset collection not found")
    return collection


@router.patch("/asset-collections/{collection_id}", response_model=AssetCollection)
def update_asset_collection(
    collection_id: str,
    payload: AssetCollectionUpdate,
    store: StoreDep,
) -> AssetCollection:
    collection = store.update_asset_collection(collection_id, payload)
    if collection is None:
        raise HTTPException(status_code=404, detail="asset collection not found")
    return collection


@router.get(
    "/asset-collections/{collection_id}/items",
    response_model=list[AssetCollectionItem],
)
def list_asset_collection_items(
    collection_id: str,
    store: StoreDep,
    include_removed: bool = False,
    role: str | None = None,
) -> list[AssetCollectionItem]:
    items = store.list_asset_collection_items(
        collection_id,
        include_removed=include_removed,
        role=role,
    )
    if items is None:
        raise HTTPException(status_code=404, detail="asset collection not found")
    return items


@router.post("/asset-collections/{collection_id}/items", response_model=AssetCollectionItem)
def add_asset_collection_item(
    collection_id: str,
    payload: AssetCollectionItemAdd,
    store: StoreDep,
) -> AssetCollectionItem:
    outcome, item = store.add_asset_collection_item(collection_id, payload)
    if outcome is CollectionAddOutcome.COLLECTION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="asset collection not found")
    if outcome is CollectionAddOutcome.ASSET_NOT_FOUND:
        raise HTTPException(status_code=404, detail="asset not found")
    assert item is not None  # the two not-found outcomes returned above
    return item


@router.post(
    "/asset-collections/{collection_id}/import-from-run",
    response_model=AssetCollectionImportResult,
)
def import_run_assets_into_collection(
    collection_id: str,
    payload: AssetCollectionImportRequest,
    store: StoreDep,
) -> AssetCollectionImportResult:
    outcome, result = store.import_run_assets_into_collection(collection_id, payload)
    if outcome is CollectionImportOutcome.COLLECTION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="asset collection not found")
    if outcome is CollectionImportOutcome.RUN_NOT_FOUND:
        raise HTTPException(status_code=404, detail="run not found")
    assert result is not None  # the two not-found outcomes returned above
    return result


@router.patch("/asset-library/items/{item_id}", response_model=AssetCollectionItem)
def update_asset_collection_item(
    item_id: str,
    payload: AssetCollectionItemUpdate,
    store: StoreDep,
) -> AssetCollectionItem:
    item = store.update_asset_collection_item(item_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="asset library item not found")
    return item


@router.delete("/asset-library/items/{item_id}", response_model=AssetCollectionItem)
def remove_asset_collection_item(item_id: str, store: StoreDep) -> AssetCollectionItem:
    item = store.remove_asset_collection_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="asset library item not found")
    return item


@router.post("/runs/{run_id}/asset-library/reuse", response_model=AssetReuseResult)
def reuse_asset_from_library(
    run_id: str,
    payload: AssetReuseRequest,
    store: StoreDep,
) -> AssetReuseResult:
    outcome, result = store.reuse_asset_into_run(run_id, payload)
    if outcome is AssetReuseOutcome.ITEM_NOT_FOUND:
        raise HTTPException(status_code=404, detail="asset library item not found")
    if outcome is AssetReuseOutcome.RUN_NOT_FOUND:
        raise HTTPException(status_code=404, detail="run not found")
    assert result is not None  # the two not-found outcomes returned above
    return result


# --- V6.5 teams, members and permissions ------------------------------------


@router.get("/me", response_model=ActorProfile)
def get_me(request: Request, store: StoreDep, actor: ActorDep) -> ActorProfile:
    """Who am I, and what may I do *here*.

    With an API key the reported permissions are the narrowed set, so a caller
    debugging a 403 sees the credential's real reach rather than its owner's.
    """
    return store.actor_profile(actor, key_scopes=api_key_scopes(request))


@router.get("/permissions", response_model=PermissionMatrix)
def get_permission_matrix() -> PermissionMatrix:
    return PermissionMatrix(**permission_matrix())


@router.get("/audit-logs", response_model=list[AuditLogEntry])
def list_audit_logs(
    response: Response,
    store: StoreDep,
    actor: ActorDep,
    actor_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    project_id: str | None = None,
    entity_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    sort: str | None = None,
) -> list[AuditLogEntry]:
    """Newest-first audit trail.

    The body stays a bare array so the shipped team page does not change; the
    total row count rides along in ``X-Total-Count`` instead, which is the V7.1
    pagination convention for endpoints that predate the ``Page`` envelope.
    """
    try:
        sort_clauses = AUDIT_LOG_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    filters = {
        "tenant_id": actor.tenant_id,
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "project_id": project_id,
        "entity_id": entity_id,
    }
    entries = store.list_audit_logs(**filters, limit=limit, offset=offset, sort=sort_clauses)
    response.headers["X-Total-Count"] = str(store.count_audit_logs(**filters))
    return entries


@router.get("/idempotency-keys", response_model=Page[IdempotencyKeyEntry])
def list_idempotency_keys(
    store: StoreDep,
    actor: ActorDep,
    state: str | None = None,
    actor_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> Page[IdempotencyKeyEntry]:
    """Recorded idempotent writes, so an SDK can audit what it retried (V7.1)."""
    try:
        sort_clauses = IDEMPOTENCY_KEY_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    items, total = store.list_idempotency_keys(
        tenant_id=actor.tenant_id,
        state=state,
        actor_id=actor_id,
        limit=limit,
        offset=offset,
        sort=sort_clauses,
    )
    return Page[IdempotencyKeyEntry](
        items=items,
        total=total,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )


# --- V7.2 programmatic credentials ------------------------------------------


@router.post("/api-keys", response_model=ApiKeyCreated)
def create_api_key(
    payload: ApiKeyCreate,
    store: StoreDep,
    actor: ActorDep,
) -> ApiKeyCreated:
    """Issue a credential for the calling identity.

    A key always belongs to whoever asks for it — there is deliberately no way to
    mint one for somebody else, so a credential can never exceed the reach of a
    human who already had that reach. ``scopes`` may narrow it further and is
    refused (rather than trimmed) if it names a permission the owner lacks.

    The plaintext is returned here and nowhere else. Only a salted digest is
    stored, so a key that is lost is revoked and replaced, never recovered.
    """
    issued = store.create_api_key(payload, actor)
    if issued.outcome is ApiKeyOutcome.INVALID_SCOPE:
        raise ApiError(ErrorCode.BAD_REQUEST, issued.detail or "invalid scope")
    if issued.outcome is ApiKeyOutcome.SCOPE_EXCEEDS_OWNER:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            issued.detail or "requested scopes exceed the owner's permissions",
        )
    assert issued.api_key is not None and issued.plaintext is not None
    return ApiKeyCreated(**issued.api_key.model_dump(), api_key=issued.plaintext)


@router.get("/api-keys", response_model=Page[ApiKey])
def list_api_keys(
    store: StoreDep,
    actor: ActorDep,
    actor_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> Page[ApiKey]:
    """Issued credentials, newest first, never including the plaintext."""
    try:
        sort_clauses = API_KEY_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    items, total = store.list_api_keys(
        tenant_id=actor.tenant_id,
        actor_id=actor_id,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort_clauses,
    )
    return Page[ApiKey](
        items=items,
        total=total,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )


@router.post("/api-keys/{api_key_id}/revoke", response_model=ApiKey)
def revoke_api_key(api_key_id: str, store: StoreDep, actor: ActorDep) -> ApiKey:
    """Revoke a credential. Idempotent: revoking twice is not an error.

    Revocation is a state change rather than a delete, so the audit trail keeps
    pointing at a row that still exists after the credential stops working.
    """
    key = store.revoke_api_key(api_key_id, actor)
    if key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    return key


# --- V1.1 username-password login ---------------------------------------------


def _login_user(user) -> LoginUser:
    return LoginUser(
        user_id=user.id,
        email=user.email,
        name=user.name,
        tenant_id=user.tenant_id,
        role=user.role,
        status=user.status,
    )


def _require_session(request: Request):
    """The verified login session behind this request, or a 401.

    The three ``/api/auth`` session endpoints are open endpoints — the token is
    the credential, like the embed bootstrap — but open does not mean
    anonymous: a request that arrives without a verified session has nothing
    to introspect or revoke, and "nothing" is a 401, not a default identity.
    """
    identity = session_identity(request)
    if identity is None:
        raise ApiError(
            ErrorCode.SESSION_INVALID,
            "this endpoint requires a login session token",
        )
    return identity


@router.post("/auth/login", response_model=LoginResult)
def login(payload: LoginRequest, store: StoreDep) -> LoginResult:
    """Prove who you are with a password; get a bearer session token back.

    Every refusal is the same 401 ``auth_invalid_credentials`` — unknown email,
    no password set, wrong password, inactive account — so the endpoint is not
    a user oracle. The token travels in the ``Authorization: Bearer`` header on
    later calls and sits below the API key in the identity priority: a request
    presenting both resolves as the key.
    """
    user = store.authenticate_password(payload.email, payload.password)
    if user is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_CREDENTIALS,
            "the credentials are wrong or the account is not available",
        )
    record, plaintext = store.create_login_session(user)
    return LoginResult(
        session_id=record.id,
        token=plaintext,
        token_prefix=record.token_prefix,
        expires_at=record.expires_at,
        user=_login_user(user),
    )


@router.post("/auth/logout", response_model=LogoutResult)
def logout(request: Request, store: StoreDep) -> LogoutResult:
    """Revoke the session the request was authenticated with.

    Idempotent like every other revocation here: the second call reports
    ``revoked: false`` rather than erroring, because the outcome the caller
    cares about — "this token no longer works" — is already true.
    """
    identity = _require_session(request)
    record = store.revoke_session(identity.session_id, actor=resolve_actor(request))
    if record is None:
        raise ApiError(
            ErrorCode.SESSION_INVALID,
            "the session token is unknown, revoked or expired",
        )
    return LogoutResult(session_id=record.id, revoked=record.revoked_at is not None)


@router.get("/auth/session", response_model=AuthSessionInfo)
def auth_session(request: Request) -> AuthSessionInfo:
    """Introspect the current login session.

    The session token is the credential: this is the endpoint a client calls to
    answer "am I still logged in, and as whom". The plaintext never returns —
    only the prefix, so a client can label the token it holds.
    """
    identity = _require_session(request)
    record = identity.record
    return AuthSessionInfo(
        session_id=record.id,
        token_prefix=record.token_prefix,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        user=_login_user(identity.user),
    )


@router.post("/users/{user_id}/password", response_model=LoginUser)
def set_user_password(
    user_id: str,
    payload: PasswordSetRequest,
    store: StoreDep,
    actor: ActorDep,
) -> LoginUser:
    """Set (or replace) a user's login password.

    Seating a login identity is an act of tenant administration, which is why
    it rides ``member:manage`` at tenant scope: whoever may manage the roster
    may give a member a way to sign in, and no one else. Setting a password
    invalidates every session the user holds that predates the change.
    """
    user = store.set_user_password(
        user_id,
        hash_password(payload.password),
        actor=actor,
        email=payload.email,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _login_user(user)


# --- V7.3 outbound webhooks ---------------------------------------------------


@router.get("/webhook-events", response_model=list[WebhookEventDefinition])
def list_webhook_events() -> list[WebhookEventDefinition]:
    """The published event vocabulary, before anyone has to subscribe to learn it.

    Mirrors ``/api/error-codes``: static, shipped with the build, and free of
    tenant data, which is why it is an open endpoint.
    """
    return [WebhookEventDefinition(**entry) for entry in webhook_event_catalog()]


def _webhook_outcome_error(outcome: WebhookSubscriptionOutcome, detail: str | None):
    if outcome is WebhookSubscriptionOutcome.UNKNOWN_EVENT:
        return ApiError(ErrorCode.BAD_REQUEST, detail or "unknown webhook event type")
    if outcome is WebhookSubscriptionOutcome.INVALID_URL:
        return ApiError(ErrorCode.BAD_REQUEST, detail or "invalid webhook url")
    return None


@router.post("/webhooks", response_model=WebhookSubscription)
def create_webhook_subscription(
    payload: WebhookSubscriptionCreate,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookSubscription:
    """Register an endpoint to receive published events.

    The signing secret is generated here and shown on every read of the
    subscription afterwards: unlike an API key it authenticates the platform to
    the receiver, so re-displaying it costs nothing and saves a rotation cycle.
    """
    result = store.create_webhook_subscription(payload, actor)
    error = _webhook_outcome_error(result.outcome, result.detail)
    if error is not None:
        raise error
    assert result.subscription is not None
    return result.subscription


@router.get("/webhooks", response_model=Page[WebhookSubscription])
def list_webhook_subscriptions(
    store: StoreDep,
    actor: ActorDep,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> Page[WebhookSubscription]:
    """Registered endpoints for the tenant, including their signing secrets."""
    try:
        sort_clauses = WEBHOOK_SUBSCRIPTION_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    items, total = store.list_webhook_subscriptions(
        tenant_id=actor.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort_clauses,
    )
    return Page[WebhookSubscription](
        items=items,
        total=total,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )


@router.get("/webhooks/{subscription_id}", response_model=WebhookSubscription)
def get_webhook_subscription(
    subscription_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookSubscription:
    subscription = store.get_webhook_subscription(
        subscription_id,
        tenant_id=actor.tenant_id,
    )
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    return subscription


@router.patch("/webhooks/{subscription_id}", response_model=WebhookSubscription)
def update_webhook_subscription(
    subscription_id: str,
    payload: WebhookSubscriptionUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookSubscription:
    """Partial update. Re-activating clears the auto-pause failure count."""
    result = store.update_webhook_subscription(subscription_id, payload, actor)
    if result.outcome is WebhookSubscriptionOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    error = _webhook_outcome_error(result.outcome, result.detail)
    if error is not None:
        raise error
    assert result.subscription is not None
    return result.subscription


@router.delete("/webhooks/{subscription_id}", response_model=WebhookSubscription)
def delete_webhook_subscription(
    subscription_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookSubscription:
    """Delete a subscription and its queued deliveries. The event log is untouched."""
    subscription = store.delete_webhook_subscription(subscription_id, actor)
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    return subscription


@router.post("/webhooks/{subscription_id}/rotate-secret", response_model=WebhookSubscription)
def rotate_webhook_secret(
    subscription_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookSubscription:
    """Replace the signing secret. In-flight attempts may still carry the old one."""
    subscription = store.rotate_webhook_secret(subscription_id, actor)
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    return subscription


@router.get(
    "/webhooks/{subscription_id}/deliveries",
    response_model=Page[WebhookDelivery],
)
def list_webhook_deliveries(
    subscription_id: str,
    store: StoreDep,
    actor: ActorDep,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> Page[WebhookDelivery]:
    """The subscription's outbox: every attempt, its response and its fate."""
    subscription = store.get_webhook_subscription(
        subscription_id,
        tenant_id=actor.tenant_id,
    )
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    try:
        sort_clauses = WEBHOOK_DELIVERY_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    items, total = store.list_webhook_deliveries(
        tenant_id=actor.tenant_id,
        subscription_id=subscription_id,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort_clauses,
    )
    return Page[WebhookDelivery](
        items=items,
        total=total,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )


@router.post("/webhook-deliveries/{delivery_id}/requeue", response_model=WebhookDelivery)
def requeue_webhook_delivery(
    delivery_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> WebhookDelivery:
    """Put a delivery back in the queue with a fresh attempt count.

    The manual-replay equivalent of the worker's own retry: for an ``abandoned``
    delivery this is the only way it ever reaches its endpoint.
    """
    delivery = store.requeue_webhook_delivery(delivery_id, actor)
    if delivery is None:
        raise HTTPException(status_code=404, detail="webhook delivery not found")
    return delivery


@router.get("/teams", response_model=list[Team])
def list_teams(
    store: StoreDep,
    actor: ActorDep,
    include_archived: bool = False,
) -> list[Team]:
    return store.list_teams(
        tenant_id=actor.tenant_id,
        include_archived=include_archived,
    )


@router.post("/teams", response_model=Team)
def create_team(payload: TeamCreate, store: StoreDep, actor: ActorDep) -> Team:
    try:
        return store.create_team(payload, actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/teams/{team_id}", response_model=Team)
def get_team(team_id: str, store: StoreDep, actor: ActorDep) -> Team:
    team = store.get_team(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    return team


@router.patch("/teams/{team_id}", response_model=Team)
def update_team(
    team_id: str,
    payload: TeamUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> Team:
    team = store.update_team(team_id, payload, actor)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    return team


@router.get("/teams/{team_id}/members", response_model=list[TeamMembership])
def list_team_members(
    team_id: str,
    store: StoreDep,
    actor: ActorDep,
    include_removed: bool = False,
) -> list[TeamMembership]:
    members = store.list_team_members(team_id, include_removed=include_removed)
    if members is None:
        raise HTTPException(status_code=404, detail="team not found")
    return members


@router.post("/teams/{team_id}/members", response_model=TeamMembership)
def add_team_member(
    team_id: str,
    payload: TeamMemberAdd,
    store: StoreDep,
    actor: ActorDep,
) -> TeamMembership:
    if coerce_team_role(payload.role) is None:
        raise HTTPException(status_code=400, detail=f"invalid team role '{payload.role}'")
    outcome, membership = store.add_team_member(team_id, payload, actor)
    if outcome is TeamMembershipOutcome.TEAM_NOT_FOUND:
        raise HTTPException(status_code=404, detail="team not found")
    if outcome is TeamMembershipOutcome.INVALID_ROLE:
        raise HTTPException(status_code=400, detail=f"invalid team role '{payload.role}'")
    assert membership is not None  # the two error outcomes returned above
    return membership


@router.patch("/team-memberships/{membership_id}", response_model=TeamMembership)
def update_team_member(
    membership_id: str,
    payload: TeamMemberUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> TeamMembership:
    if payload.role is not None and coerce_team_role(payload.role) is None:
        raise HTTPException(status_code=400, detail=f"invalid team role '{payload.role}'")
    membership = store.update_team_member(membership_id, payload, actor)
    if membership is None:
        raise HTTPException(status_code=404, detail="team membership not found")
    return membership


@router.delete("/team-memberships/{membership_id}", response_model=TeamMembership)
def remove_team_member(
    membership_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> TeamMembership:
    membership = store.remove_team_member(membership_id, actor)
    if membership is None:
        raise HTTPException(status_code=404, detail="team membership not found")
    return membership


# --- V8.3 team quotas -------------------------------------------------------
#
# A quota is a ceiling per team per period, read on the hot path of every paid
# call. These endpoints are therefore all about *reading* it precisely and
# *writing* it rarely — and never about billing: the platform has no
# counterparty, so nothing here totals what anyone owes, only how much of a
# team's allowance is gone.


@router.get("/team-quotas", response_model=list[TeamQuota])
def list_team_quotas(
    store: StoreDep,
    actor: ActorDep,
    include_archived: bool = False,
) -> list[TeamQuota]:
    """Every team in the tenant, each with its quota or the visible lack of one."""
    return store.list_team_quotas(
        tenant_id=actor.tenant_id,
        include_archived=include_archived,
    )


@router.get("/teams/{team_id}/quota", response_model=TeamQuota)
def get_team_quota(
    team_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> TeamQuota:
    quota = store.team_quota(team_id, tenant_id=actor.tenant_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="team not found")
    return quota


@router.put("/teams/{team_id}/quota", response_model=TeamQuota)
def set_team_quota(
    team_id: str,
    payload: TeamQuotaUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> TeamQuota:
    """Create or replace the ceiling. ``PUT`` because a team has at most one.

    A team-wide setting is not a collection, so there is nothing to ``POST``:
    sending the body twice must land on the same state, and it does.
    """
    quota = store.set_team_quota(team_id, payload, actor)
    if quota is None:
        raise HTTPException(status_code=404, detail="team not found")
    return quota


@router.delete("/teams/{team_id}/quota", response_model=TeamQuota)
def delete_team_quota(
    team_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> TeamQuota:
    """Remove the ceiling entirely, back to unguarded.

    The response is the *post-delete* view rather than the removed row: what a
    caller wants to know is what the team is now measured against, and that is
    "nothing". A team that never had a quota is a 404 — there was nothing to
    delete, which is different from "deleted successfully".
    """
    outcome = store.delete_team_quota(team_id, actor)
    if outcome is None:
        raise HTTPException(status_code=404, detail="team not found")
    if outcome is False:
        raise HTTPException(status_code=404, detail="team has no quota")
    quota = store.team_quota(team_id, tenant_id=actor.tenant_id)
    assert quota is not None  # the team existed a statement ago
    return quota


@router.post("/runs/{run_id}/quota-override", response_model=QuotaOverride)
def set_run_quota_override(
    run_id: str,
    payload: QuotaOverrideRequest,
    store: StoreDep,
    actor: ActorDep,
) -> QuotaOverride:
    """Grant (or retract) one run's exemption from the team's hard ceiling.

    Gated on ``quota:manage`` rather than ``run:start`` on purpose: if starting
    a run were enough to exempt it, every member could exempt their own, and a
    hard quota would be a suggestion.
    """
    run = store.confirm_quota_override(
        run_id,
        accept=payload.accept,
        note=payload.note,
        actor=actor,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    override = store.quota_override(run_id)
    assert override is not None  # the run exists, so the read cannot miss
    return override


@router.patch("/projects/{project_id}/team", response_model=Project)
def assign_project_team(
    project_id: str,
    payload: ProjectTeamAssign,
    store: StoreDep,
    actor: ActorDep,
) -> Project:
    """Move a project's spend to a team, or off every team (``team_id: null``).

    The endpoint exists because attribution *is* the quota: a project pointed at
    a team with room spends that team's allowance, so pointing it must be as
    privileged as setting the allowance. ``quota:manage`` is tenant-only, which
    is what stops a project writer from moving a project onto an unguarded team
    to escape a ceiling.
    """
    outcome, project = store.assign_project_team(project_id, payload.team_id, actor)
    if outcome is ProjectTeamOutcome.PROJECT_NOT_FOUND:
        raise HTTPException(status_code=404, detail="project not found")
    if outcome is ProjectTeamOutcome.TEAM_NOT_FOUND:
        raise HTTPException(status_code=404, detail="team not found")
    assert project is not None  # both error outcomes returned above
    return project


@router.get("/projects/{project_id}/members", response_model=list[ProjectMembership])
def list_project_members(
    project_id: str,
    store: StoreDep,
    actor: ActorDep,
    include_removed: bool = False,
) -> list[ProjectMembership]:
    members = store.list_project_members(project_id, include_removed=include_removed)
    if members is None:
        raise HTTPException(status_code=404, detail="project not found")
    return members


@router.post("/projects/{project_id}/members", response_model=ProjectMembership)
def add_project_member(
    project_id: str,
    payload: ProjectMemberAdd,
    store: StoreDep,
    actor: ActorDep,
) -> ProjectMembership:
    if coerce_project_role(payload.role) is None:
        raise HTTPException(status_code=400, detail=f"invalid project role '{payload.role}'")
    outcome, membership = store.add_project_member(project_id, payload, actor)
    if outcome is ProjectMembershipOutcome.PROJECT_NOT_FOUND:
        raise HTTPException(status_code=404, detail="project not found")
    if outcome is ProjectMembershipOutcome.INVALID_ROLE:
        raise HTTPException(status_code=400, detail=f"invalid project role '{payload.role}'")
    assert membership is not None  # the two error outcomes returned above
    return membership


@router.patch("/project-memberships/{membership_id}", response_model=ProjectMembership)
def update_project_member(
    membership_id: str,
    payload: ProjectMemberUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> ProjectMembership:
    if payload.role is not None and coerce_project_role(payload.role) is None:
        raise HTTPException(status_code=400, detail=f"invalid project role '{payload.role}'")
    membership = store.update_project_member(membership_id, payload, actor)
    if membership is None:
        raise HTTPException(status_code=404, detail="project membership not found")
    return membership


@router.delete("/project-memberships/{membership_id}", response_model=ProjectMembership)
def remove_project_member(
    membership_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ProjectMembership:
    membership = store.remove_project_member(membership_id, actor)
    if membership is None:
        raise HTTPException(status_code=404, detail="project membership not found")
    return membership


@router.post("/projects/{project_id}/runs")
def create_run(
    project_id: str,
    payload: RunCreate,
    store: StoreDep,
) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    pipeline = loader.get_pipeline(payload.pipeline_key)
    run = store.create_run(project_id=project_id, payload=payload, pipeline=pipeline)
    return run.model_dump()


@router.get("/projects/{project_id}/runs")
def list_project_runs(
    project_id: str,
    store: StoreDep,
    status: RunStatus | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict]:
    runs = store.list_project_dashboard_runs(
        project_id,
        status=status,
        search=search,
        limit=limit,
    )
    if runs is None:
        raise HTTPException(status_code=404, detail="project not found")
    return [run.model_dump(mode="json") for run in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: str, store: StoreDep) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump()


@router.get("/runs/{run_id}/board", response_model=RunBoard)
def get_run_board(run_id: str, store: StoreDep) -> RunBoard:
    board = store.run_board(run_id)
    if board is None:
        raise HTTPException(status_code=404, detail="run not found")
    return board


@router.post("/runs/{run_id}/copy", response_model=RunCopyResult)
def copy_run(
    run_id: str,
    payload: RunCopyRequest,
    store: StoreDep,
) -> RunCopyResult:
    try:
        result = store.copy_run(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.get("/run-state-transitions")
def list_run_state_transitions() -> list[dict]:
    return run_state_transition_table()


@router.get("/runs/{run_id}/artifacts")
def list_artifacts(run_id: str, store: StoreDep, current_only: bool = False) -> list[dict]:
    artifacts = store.list_artifacts(run_id, current_only=current_only)
    if artifacts is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [artifact.model_dump() for artifact in artifacts]


@router.post("/runs/{run_id}/preflight")
def run_preflight(run_id: str, store: StoreDep) -> dict:
    artifact = store.run_preflight(run_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    return artifact.model_dump()


@router.post("/runs/{run_id}/execution-plan")
def plan_execution(run_id: str, store: StoreDep) -> dict:
    artifact = store.plan_execution(run_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    return artifact.model_dump()


@router.post("/runs/{run_id}/proposal-packet")
def build_proposal_packet(run_id: str, store: StoreDep) -> dict:
    artifact = store.build_proposal_packet(run_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    return artifact.model_dump()


@router.post("/runs/{run_id}/stage-execution-manifest")
def plan_stage_execution(run_id: str, store: StoreDep) -> dict:
    artifact = store.plan_stage_execution(run_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    return artifact.model_dump()


@router.post("/runs/{run_id}/composition-graph")
def build_composition_graph(
    run_id: str,
    store: StoreDep,
    payload: CompositionGraphBuildRequest | None = None,
) -> dict:
    try:
        artifact = store.build_composition_graph(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    return artifact.model_dump()


@router.post("/runs/{run_id}/html-canvas-preview")
def render_html_canvas_preview(
    run_id: str,
    store: StoreDep,
    payload: HtmlCanvasPreviewRequest | None = None,
) -> dict:
    try:
        result = store.render_html_canvas_preview(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset, artifact = result
    return {
        "invocation": invocation.model_dump(),
        "asset": asset.model_dump(),
        "artifact": artifact.model_dump(),
    }


@router.post("/runs/{run_id}/quality-gates")
def run_quality_gates(
    run_id: str,
    store: StoreDep,
    payload: QualityGateRequest | None = None,
) -> dict:
    try:
        result = store.run_quality_gates(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, artifact = result
    return {
        "invocation": invocation.model_dump(),
        "artifact": artifact.model_dump(),
    }


@router.post("/runs/{run_id}/edit-tools")
def run_edit_tool(
    run_id: str,
    payload: EditToolRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.run_edit_tool(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, assets, artifact = result
    return {
        "invocation": invocation.model_dump(),
        "assets": [asset.model_dump() for asset in assets],
        "artifact": artifact.model_dump(),
    }


@router.post("/runs/{run_id}/dispatch-stage-execution")
def dispatch_stage_execution(run_id: str, store: StoreDep) -> list[dict]:
    jobs = store.dispatch_stage_execution_tasks(run_id)
    if jobs is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [job.model_dump() for job in jobs]


@router.get("/runs/{run_id}/dispatch-stage-execution/preview")
def preview_stage_execution_dispatch(run_id: str, store: StoreDep) -> dict:
    preview = store.preview_stage_execution_dispatch(run_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="run not found")
    return preview


@router.post("/runs/{run_id}/manifest-actions", response_model=ManifestActionResult)
def run_manifest_action(
    run_id: str,
    payload: ManifestActionRequest,
    store: StoreDep,
) -> ManifestActionResult:
    try:
        result = store.run_manifest_action(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.post("/runs/{run_id}/state-actions", response_model=RunStateActionResult)
def run_state_action(
    run_id: str,
    payload: RunStateActionRequest,
    store: StoreDep,
) -> RunStateActionResult:
    try:
        result = store.run_state_action(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.post("/runs/{run_id}/assets")
def upload_asset(run_id: str, payload: AssetUpload, store: StoreDep) -> dict:
    try:
        asset = store.upload_asset(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if asset is None:
        raise HTTPException(status_code=404, detail="run not found")
    return asset.model_dump()


@router.post("/runs/{run_id}/source-media", response_model=SourceMediaIngestResult)
def ingest_source_media(
    run_id: str,
    payload: SourceMediaIngestRequest,
    store: StoreDep,
) -> SourceMediaIngestResult:
    try:
        result = store.ingest_source_media(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.post("/runs/{run_id}/media-analysis", response_model=MediaAnalysisResult)
def analyze_source_media(
    run_id: str,
    payload: MediaAnalysisRequest,
    store: StoreDep,
) -> MediaAnalysisResult:
    try:
        result = store.analyze_source_media(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.get("/runs/{run_id}/assets")
def list_assets(run_id: str, store: StoreDep) -> list[dict]:
    assets = store.list_assets(run_id)
    if assets is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [asset.model_dump() for asset in assets]


@router.get("/runs/{run_id}/tool-invocations")
def list_tool_invocations(run_id: str, store: StoreDep) -> list[dict]:
    invocations = store.list_tool_invocations(run_id)
    if invocations is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [invocation.model_dump() for invocation in invocations]


@router.get("/runs/{run_id}/decision-log", response_model=RunDecisionLog)
def get_run_decision_log(
    run_id: str,
    store: StoreDep,
    category: str | None = None,
    include_history: bool = False,
    limit: int = 200,
) -> RunDecisionLog:
    """Why the run made each choice, and what it turned down.

    Read-only by design: a decision log that can be edited is not evidence. The
    default view is one row per ``(category, subject)`` — "what the run currently
    believes" — and ``include_history=true`` returns every append in order.
    """

    log = store.list_run_decisions(
        run_id,
        category=category,
        include_history=include_history,
        limit=limit,
    )
    if log is None:
        raise HTTPException(status_code=404, detail="run not found")
    return log


@router.get("/runs/{run_id}/usage", response_model=UsageSummary)
def get_usage_summary(run_id: str, store: StoreDep) -> UsageSummary:
    summary = store.get_usage_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run not found")
    return summary


@router.get("/runs/{run_id}/usage/ledger")
def list_usage_ledger(run_id: str, store: StoreDep) -> list[dict]:
    entries = store.list_usage_ledger(run_id)
    if entries is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [entry.model_dump() for entry in entries]


@router.post("/runs/{run_id}/tts")
def generate_tts_audio(
    run_id: str,
    payload: TtsGenerateRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.generate_tts_audio(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset = result
    return {"invocation": invocation.model_dump(), "asset": asset.model_dump()}


@router.post("/runs/{run_id}/subtitles")
def generate_subtitles(
    run_id: str,
    payload: SubtitleGenerateRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.generate_subtitles(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset = result
    return {"invocation": invocation.model_dump(), "asset": asset.model_dump()}


@router.post("/runs/{run_id}/music")
def generate_music(
    run_id: str,
    payload: MusicGenerateRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.generate_music(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset = result
    return {"invocation": invocation.model_dump(), "asset": asset.model_dump()}


@router.post("/runs/{run_id}/images")
def generate_image(
    run_id: str,
    payload: ImageGenerateRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.generate_image(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset = result
    return {"invocation": invocation.model_dump(), "asset": asset.model_dump()}


@router.post("/runs/{run_id}/compose")
def compose_video(
    run_id: str,
    payload: ComposeVideoRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.compose_video(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    invocation, asset, artifact = result
    return {
        "invocation": invocation.model_dump(),
        "asset": asset.model_dump(),
        "artifact": artifact.model_dump(),
    }


@router.post("/runs/{run_id}/avatar-tasks")
def submit_avatar_task(
    run_id: str,
    payload: AvatarVideoSubmitRequest,
    store: StoreDep,
) -> dict:
    try:
        invocation = store.submit_avatar_task(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if invocation is None:
        raise HTTPException(status_code=404, detail="run not found")
    return invocation.model_dump()


@router.post("/runs/{run_id}/avatar-tasks/from-assets")
def submit_avatar_task_from_assets(
    run_id: str,
    payload: AvatarVideoFromAssetsRequest,
    store: StoreDep,
) -> dict:
    try:
        invocation = store.submit_avatar_task_from_assets(run_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if invocation is None:
        raise HTTPException(status_code=404, detail="run not found")
    return invocation.model_dump()


@router.post("/runs/{run_id}/jobs/avatar-from-assets")
def enqueue_avatar_from_assets_job(
    run_id: str,
    payload: AvatarVideoFromAssetsRequest,
    store: StoreDep,
) -> dict:
    job = store.enqueue_avatar_from_assets_job(run_id, payload)
    if job is None:
        raise HTTPException(status_code=404, detail="run not found")
    return job.model_dump()


@router.get("/runs/{run_id}/jobs")
def list_background_jobs(run_id: str, store: StoreDep) -> list[dict]:
    jobs = store.list_background_jobs(run_id)
    if jobs is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [job.model_dump() for job in jobs]


@router.get("/jobs/{job_id}")
def get_background_job(job_id: str, store: StoreDep) -> dict:
    job = store.get_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return job.model_dump()


@router.post("/jobs/{job_id}/process")
def process_background_job(job_id: str, store: StoreDep) -> dict:
    job = store.process_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return job.model_dump()


@router.post("/jobs/{job_id}/retry")
def retry_background_job(job_id: str, store: StoreDep) -> dict:
    job = store.retry_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return job.model_dump()


@router.post("/jobs/{job_id}/requeue")
def requeue_background_job(job_id: str, store: StoreDep) -> dict:
    """Replay a dead-lettered job with a fresh attempt count (V8.1)."""
    existing = store.get_background_job(job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="background job not found")
    if existing.status != BackgroundJobStatus.DEAD:
        raise HTTPException(
            status_code=409,
            detail="only dead-lettered background jobs can be requeued",
        )
    requeued = store.requeue_background_job(job_id)
    if requeued is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return requeued.model_dump()


@router.get("/tool-invocations/{invocation_id}")
def get_tool_invocation(invocation_id: str, store: StoreDep) -> dict:
    invocation = store.get_tool_invocation(invocation_id)
    if invocation is None:
        raise HTTPException(status_code=404, detail="tool invocation not found")
    return invocation.model_dump()


@router.post("/tool-invocations/{invocation_id}/poll")
def poll_avatar_task(invocation_id: str, store: StoreDep) -> dict:
    try:
        invocation = store.poll_avatar_task(invocation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if invocation is None:
        raise HTTPException(status_code=404, detail="tool invocation not found")
    return invocation.model_dump()


@router.post("/tool-invocations/{invocation_id}/archive-avatar-video")
def archive_avatar_task(
    invocation_id: str,
    payload: AvatarVideoArchiveRequest,
    store: StoreDep,
) -> dict:
    try:
        result = store.archive_avatar_task(invocation_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="tool invocation not found")
    invocation, asset = result
    return {"invocation": invocation.model_dump(), "asset": asset.model_dump()}


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, store: StoreDep) -> dict:
    asset = store.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return asset.model_dump()


@router.get("/assets/{asset_id}/download")
def download_asset(asset_id: str, store: StoreDep) -> Response:
    result = store.download_asset(asset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="asset not found")
    asset, content = result
    return _asset_response(asset.model_dump(), content)


@router.get("/assets/{asset_id}/access-link")
def create_asset_access_link(
    asset_id: str,
    store: StoreDep,
    expires_in_seconds: int | None = None,
) -> dict:
    link = store.create_asset_access_link(
        asset_id,
        expires_in_seconds=expires_in_seconds,
    )
    if link is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return link.model_dump()


@router.get("/assets/{asset_id}/content")
def get_asset_content(
    asset_id: str,
    expires: int,
    signature: str,
    store: StoreDep,
) -> Response:
    if store.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    result = store.resolve_asset_access_link(asset_id, expires=expires, signature=signature)
    if result is None:
        raise HTTPException(status_code=403, detail="asset link is invalid or expired")
    asset, content = result
    return _asset_response(asset.model_dump(), content)


@router.get("/runs/{run_id}/events")
def list_events(
    run_id: str,
    store: StoreDep,
    source_type: EventSourceType | None = None,
    stage_key: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
) -> list[dict]:
    events = store.list_events(
        run_id,
        source_type=source_type,
        stage_key=stage_key,
        event_type=event_type,
        limit=limit,
    )
    if events is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [event.model_dump() for event in events]


@router.post("/runs/{run_id}/events")
def record_event(run_id: str, payload: EventCreate, store: StoreDep) -> dict:
    event = store.record_event(run_id, payload)
    if event is None:
        raise HTTPException(status_code=404, detail="run not found")
    return event.model_dump()


@router.post("/runs/{run_id}/start")
def start_run(
    run_id: str,
    store: StoreDep,
    actor: ActorDep,
    payload: StartRunRequest | None = None,
) -> dict:
    try:
        run = store.start_run(
            run_id,
            accept_intent_downgrade=bool(payload and payload.accept_intent_downgrade),
            actor_id=actor.actor_id,
            actor_display_name=actor.display_name,
        )
    except RunGateError as exc:
        raise _run_gate_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump()


@router.post("/runs/{run_id}/provider-consents")
def confirm_paid_provider(
    run_id: str,
    payload: ProviderConsentRequest,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Confirm (or retract) first-use consent for a paid provider on this run (T76)."""

    run = store.confirm_paid_provider(
        run_id,
        provider=payload.provider,
        accept=payload.accept,
        note=payload.note,
        actor_id=actor.actor_id,
        actor_display_name=actor.display_name,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run_id": run.run_id,
        "provider": payload.provider,
        "accept": payload.accept,
        "paid_provider_consents": (run.input_payload or {}).get(
            "paid_provider_consents", {}
        ),
    }


@router.post("/runs/{run_id}/stages/{stage_key}/review")
def review_stage(
    run_id: str,
    stage_key: str,
    payload: ApprovalRequest,
    store: StoreDep,
) -> dict:
    """One-click stage review. Kept for compatibility; backed by the review kernel."""
    try:
        run = store.review_stage(run_id, stage_key, payload)
    except ReviewGateError as exc:
        raise _review_gate_http_error(exc) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="run or stage not found")
    return run.model_dump()


@router.get("/runs/{run_id}/review-requests", response_model=list[ReviewRequest])
def list_review_requests(
    run_id: str,
    store: StoreDep,
    status: ReviewRequestStatus | None = None,
    stage_key: str | None = None,
) -> list[ReviewRequest]:
    requests = store.list_review_requests(run_id, status=status, stage_key=stage_key)
    if requests is None:
        raise HTTPException(status_code=404, detail="run not found")
    return requests


@router.post("/runs/{run_id}/review-requests", response_model=ReviewRequestDetail)
def open_review_request(
    run_id: str,
    payload: ReviewRequestCreate,
    store: StoreDep,
) -> ReviewRequestDetail:
    try:
        detail = store.open_review_request(run_id, payload)
    except ReviewGateError as exc:
        raise _review_gate_http_error(exc) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return detail


@router.get("/review-requests/{review_request_id}", response_model=ReviewRequestDetail)
def get_review_request(review_request_id: str, store: StoreDep) -> ReviewRequestDetail:
    detail = store.get_review_request(review_request_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="review request not found")
    return detail


@router.post(
    "/review-requests/{review_request_id}/decisions",
    response_model=ReviewRequestDetail,
)
def submit_review_decision(
    review_request_id: str,
    payload: SubmitReviewDecision,
    store: StoreDep,
) -> ReviewRequestDetail:
    try:
        return store.submit_review_decision(review_request_id, payload)
    except ReviewGateError as exc:
        raise _review_gate_http_error(exc) from exc


@router.post("/review-requests/{review_request_id}/comments", response_model=ReviewComment)
def add_review_comment(
    review_request_id: str,
    payload: CreateReviewComment,
    store: StoreDep,
) -> ReviewComment:
    try:
        comment = store.add_review_comment(review_request_id, payload)
    except ReviewGateError as exc:
        raise _review_gate_http_error(exc) from exc
    if comment is None:
        raise HTTPException(status_code=404, detail="review request not found")
    return comment


@router.patch("/review-comments/{comment_id}", response_model=ReviewComment)
def update_review_comment(
    comment_id: str,
    payload: UpdateReviewComment,
    store: StoreDep,
) -> ReviewComment:
    comment = store.update_review_comment(comment_id, status=payload.status)
    if comment is None:
        raise HTTPException(status_code=404, detail="review comment not found")
    return comment


@router.post(
    "/review-requests/{review_request_id}/attachments",
    response_model=ReviewAttachment,
)
def add_review_attachment(
    review_request_id: str,
    payload: CreateReviewAttachment,
    store: StoreDep,
) -> ReviewAttachment:
    outcome, attachment = store.add_review_attachment(review_request_id, payload)
    if outcome is ReviewAttachmentOutcome.REQUEST_NOT_FOUND:
        raise HTTPException(status_code=404, detail="review request not found")
    if outcome is ReviewAttachmentOutcome.ASSET_NOT_FOUND:
        # Also covers an asset from another project: a 403 would confirm it
        # exists somewhere else.
        raise HTTPException(status_code=404, detail="asset not found")
    assert attachment is not None  # the two not-found outcomes returned above
    return attachment


@router.get(
    "/review-requests/{review_request_id}/attachments",
    response_model=list[ReviewAttachment],
)
def list_review_attachments(
    review_request_id: str,
    store: StoreDep,
) -> list[ReviewAttachment]:
    attachments = store.list_review_attachments(review_request_id)
    if attachments is None:
        raise HTTPException(status_code=404, detail="review request not found")
    return attachments


@router.delete("/review-attachments/{attachment_id}", response_model=ReviewAttachment)
def remove_review_attachment(attachment_id: str, store: StoreDep) -> ReviewAttachment:
    attachment = store.remove_review_attachment(attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="review attachment not found")
    return attachment


@router.post("/review-requests/{review_request_id}/assign", response_model=ReviewRequest)
def assign_review_request(
    review_request_id: str,
    payload: AssignReviewRequest,
    store: StoreDep,
) -> ReviewRequest:
    request = store.assign_review_request(
        review_request_id,
        assignee_user_id=payload.assignee_user_id,
        due_at=payload.due_at,
    )
    if request is None:
        raise HTTPException(status_code=404, detail="review request not found")
    return request


@router.get("/projects/{project_id}/review-inbox", response_model=ReviewInbox)
def project_review_inbox(
    project_id: str,
    store: StoreDep,
    assignee_user_id: str | None = None,
    stage_key: str | None = None,
    status: ReviewRequestStatus | None = None,
) -> ReviewInbox:
    inbox = store.project_review_inbox(
        project_id,
        assignee_user_id=assignee_user_id,
        stage_key=stage_key,
        status=status,
    )
    if inbox is None:
        raise HTTPException(status_code=404, detail="project not found")
    return inbox


def _asset_response(asset: dict, content: bytes) -> Response:
    filename = asset.get("asset_metadata", {}).get("filename") or f"{asset['asset_id']}.bin"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=content, media_type=asset.get("mime_type"), headers=headers)


# --- V7.4 embeddable workspace -------------------------------------------------
#
# Two families meet here. ``/api/external-integrations`` is the tenant's own
# control surface — who may embed us, from where, and the mint call that turns a
# registration into one short-lived link. ``GET
# /api/embed/{integration_id}/session`` is the other end of the flow: it is what
# the embedded page calls to describe itself, and it is open because a session
# token *is* an identity. It answers "what am I" for a caller that has no seat
# here, exactly as ``/api/me`` does for everyone else.


def _integration_outcome_error(
    outcome: ExternalIntegrationOutcome,
    detail: str | None,
) -> ApiError | None:
    """Map the registration vocabulary onto the public error contract.

    ``NOT_FOUND`` is deliberately absent: the routes turn it into the ordinary 404
    themselves, so "you named an id that is not yours" reads exactly like every
    other collection in the API.
    """
    if outcome is ExternalIntegrationOutcome.SLUG_CONFLICT:
        return ApiError(ErrorCode.CONFLICT, detail or "slug is already registered")
    if outcome is ExternalIntegrationOutcome.INVALID_ORIGINS:
        return ApiError(ErrorCode.BAD_REQUEST, detail or "invalid allowed_origins")
    return None


@router.post("/external-integrations", response_model=ExternalIntegration)
def create_external_integration(
    payload: ExternalIntegrationCreate,
    store: StoreDep,
    actor: ActorDep,
) -> ExternalIntegration:
    """Register a host platform that may embed the Studio.

    The framing allowlist is validated at write time rather than at frame time:
    an origin typo would otherwise surface as a blank iframe inside someone
    else's product, which is the least debuggable place to discover it.
    """
    result = store.create_external_integration(payload, actor)
    error = _integration_outcome_error(result.outcome, result.detail)
    if error is not None:
        raise error
    assert result.integration is not None
    return result.integration


@router.get("/external-integrations", response_model=Page[ExternalIntegration])
def list_external_integrations(
    store: StoreDep,
    actor: ActorDep,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> Page[ExternalIntegration]:
    """Registered platforms for the tenant, each with the allowlist it carries."""
    try:
        sort_clauses = EXTERNAL_INTEGRATION_SORT.resolve(sort)
    except SortValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, str(exc)) from None
    items, total = store.list_external_integrations(
        tenant_id=actor.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
        sort=sort_clauses,
    )
    return Page[ExternalIntegration](
        items=items,
        total=total,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )


@router.get("/external-integrations/{integration_id}", response_model=ExternalIntegration)
def get_external_integration(
    integration_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ExternalIntegration:
    integration = store.get_external_integration(integration_id, actor)
    if integration is None:
        raise HTTPException(status_code=404, detail="external integration not found")
    return integration


@router.patch("/external-integrations/{integration_id}", response_model=ExternalIntegration)
def update_external_integration(
    integration_id: str,
    payload: ExternalIntegrationUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> ExternalIntegration:
    """Partial update. Setting ``status`` to ``disabled`` is the revocation lever."""
    result = store.update_external_integration(integration_id, payload, actor)
    if result.outcome is ExternalIntegrationOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="external integration not found")
    error = _integration_outcome_error(result.outcome, result.detail)
    if error is not None:
        raise error
    assert result.integration is not None
    return result.integration


@router.delete("/external-integrations/{integration_id}", response_model=ExternalIntegration)
def delete_external_integration(
    integration_id: str,
    store: StoreDep,
    actor: ActorDep,
) -> ExternalIntegration:
    """Deregister a platform. Links it already minted stop verifying immediately.

    Nothing is cascaded because nothing references the row — embed sessions are
    tokens, not records. Deleting is therefore the strongest revocation short of
    rotating the signing secret, which is why it returns what it removed.
    """
    integration = store.delete_external_integration(integration_id, actor)
    if integration is None:
        raise HTTPException(status_code=404, detail="external integration not found")
    return integration


@router.post(
    "/external-integrations/{integration_id}/embed-sessions",
    response_model=EmbedSessionIssue,
)
def mint_embed_session(
    integration_id: str,
    payload: EmbedSessionCreate,
    request: Request,
    store: StoreDep,
    actor: ActorDep,
) -> EmbedSessionIssue:
    """Turn one registration plus one project into a link that is ready to open.

    Everything that could make the link wrong is settled *here* rather than at
    open time, because at open time the holder can only be told "this link does
    not work" — with no hint which of the many ways it is wrong. So a caller that
    mixed up a project id learns it from a 404 in its own logs now, instead of
    from a screenshot in a support ticket later. That is also why the refusals
    below are not collapsed into one status: they are different bugs on the
    caller's side and deserve to be told apart.

    ``embed_url`` is absolute, so its origin has to come from configuration rather
    than from the request: behind a gateway the ``Host`` header is the internal
    hop, and a link pointing at it would work for us and for nobody else.
    """
    result = store.mint_embed_session(integration_id, payload, actor)
    if result.outcome is EmbedMintOutcome.INTEGRATION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="external integration not found")
    if result.outcome is EmbedMintOutcome.INTEGRATION_DISABLED:
        # 409, not 404: the row exists and the caller may legitimately re-enable
        # it. Reporting "not found" would send them hunting for a typo instead.
        raise ApiError(ErrorCode.CONFLICT, "external integration is disabled")
    if result.outcome is EmbedMintOutcome.PROJECT_NOT_FOUND:
        raise HTTPException(status_code=404, detail="project not found")
    if result.outcome is EmbedMintOutcome.RUN_NOT_FOUND:
        # A run in another project answers exactly like one that does not exist:
        # confirming it exists would leak the shape of another project's runs.
        raise HTTPException(status_code=404, detail="run not found")
    if result.outcome is EmbedMintOutcome.INVALID_REQUEST:
        raise ApiError(
            ErrorCode.BAD_REQUEST,
            result.detail or "invalid embed session request",
        )
    assert result.token is not None and result.session is not None
    session = result.session
    # ``page`` is deliberately left out of the URL. The view to draw is carried by
    # the token, which is signed; a query parameter next to it would be a second,
    # unsigned source of the same fact — free to disagree with the first one and
    # impossible to trust for it.
    base_url = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return EmbedSessionIssue(
        integration_id=integration_id,
        token=result.token,
        embed_url=embed_url(
            result.token,
            base_url=base_url,
            integration_id=integration_id,
            run_id=session.run_id,
        ),
        expires_at=session.expires_at,
        scopes=sorted(session.scopes),
        context=EmbedContext(
            project_id=session.project_id,
            run_id=session.run_id,
            page=session.page,
        ),
    )


@router.get("/embed/{integration_id}/session", response_model=EmbedSessionDescription)
def describe_embed_session(
    integration_id: str,
    request: Request,
    store: StoreDep,
) -> EmbedSessionDescription:
    """A token describing itself: what it is pinned to and what it may do.

    Open by decision, not by omission. "What am I" cannot require a permission —
    that is why ``/api/me`` is open too — and here the question is asked by a
    caller who has no seat to evaluate in the first place. Being open does not
    make it anonymous: identity still comes from ``enforce_embed_identity``, which
    runs as an application-level dependency, so a missing, expired, tampered or
    disabled-integration token is refused with 401 before this function is
    reached.

    The integration id in the path is checked against the token's, and that check
    is not cosmetic. The *document* request that served this page chose its
    ``frame-ancestors`` from the id in the path, while the data rendered below
    comes from the id inside the token. If the two were allowed to disagree, a
    host permitted to frame integration A would be served integration B's pinned
    run inside its own iframe — an embedding boundary crossed without anyone
    having to tamper with a signature.
    """
    session = embed_session(request)
    if session is None:
        # No token, or an API key outranked it (V7.2). Either way this endpoint
        # has nothing to describe, and saying so is not a hint to anyone: the
        # caller already knows which header it sent.
        raise ApiError(ErrorCode.EMBED_SESSION_INVALID, "an embed token is required")
    if session.integration_id != integration_id:
        raise ApiError(
            ErrorCode.EMBED_SESSION_INVALID,
            "the embed token belongs to another integration",
        )
    description = store.describe_embed_session(session)
    if description is None:
        # Unreachable while the identity dependency re-reads the same row on the
        # way in; kept so this endpoint can never hand back ``None`` if that
        # arrangement ever changes.
        raise ApiError(
            ErrorCode.EMBED_SESSION_INVALID,
            "the embed token's integration is no longer active",
        )
    return description


# --- V8.5 deployment posture -------------------------------------------------
#
# The only endpoints here whose subject is the install rather than a tenant's
# data. They exist because two questions were otherwise answerable only by hand:
# "is this box on the schema its own code expects" (a migration-runbook question)
# and "is it accepting writes right now" (an incident question). Turning both into
# reads is what makes the maintenance switch usable at 3am — the alternative is
# shelling into a container to find out.


@router.get("/deployment", response_model=DeploymentOverview)
def get_deployment(store: StoreDep) -> DeploymentOverview:
    """Posture at a glance: versions, migration state, capabilities, the switch.

    Deliberately answerable without any of the write permissions. Whoever is on
    the hook for an incident should be able to see what is running and whether
    writes are suspended, and posture holds nothing worth withholding: the DTO
    carries booleans and version strings, never a configuration value.
    """
    return store.deployment_overview()


@router.patch("/deployment", response_model=DeploymentSettings)
def update_deployment_settings(
    payload: DeploymentSettingsUpdate,
    store: StoreDep,
    actor: ActorDep,
) -> DeploymentSettings:
    """Edit the install's label, environment, retention window and notes.

    Reachable while maintenance mode is on — see ``enforce_maintenance_mode``.
    The exemption is what keeps the switch from being a one-way door: with the
    deployment endpoints closed too, turning maintenance back off would mean a
    database edit, which is precisely the sort of thing maintenance mode exists
    to avoid.
    """
    return store.update_deployment_settings(payload, actor)


@router.post("/deployment/maintenance", response_model=DeploymentSettings)
def set_deployment_maintenance(
    payload: MaintenanceModeRequest,
    store: StoreDep,
    actor: ActorDep,
) -> DeploymentSettings:
    """Suspend (or resume) mutating requests without taking reads down.

    Not a shutdown. Everything that reads keeps working: the pages render, the
    boards load, probes answer, and this endpoint answers. What stops is change —
    which is what makes this safe to leave on for the length of a migration or a
    restore, instead of stopping the container and hoping nobody notices.

    Returns the resulting settings row rather than a summary of the request, so
    the caller learns the effective message (a blank one is replaced by the
    built-in default) and who last touched it.
    """
    return store.set_maintenance_mode(
        enabled=payload.enabled,
        message=payload.message,
        actor=actor,
    )


@router.post("/deployment/backups", response_model=DeploymentSettings)
def record_deployment_backup(
    payload: DeploymentBackupRequest,
    store: StoreDep,
    actor: ActorDep,
) -> DeploymentSettings:
    """Record that a backup was taken, so its age is visible from the API.

    The backup itself happens outside the process (``scripts/backup.sh``): the
    API cannot dump a database it is serving out of. What it can do is hold the
    *fact*, which is what turns "when did we last back this up" into a read
    rather than a shell session — and that is the question that decides whether a
    bad migration is survivable.
    """
    return store.record_backup(payload, actor)
