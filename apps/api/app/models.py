from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

ItemT = TypeVar("ItemT")


def strip_optional_text(value: Any) -> Any:
    """Trim surrounding whitespace *before* the field constraints judge it.

    ``mode="before"`` is the point: without it a name of ``"   "`` satisfies
    ``min_length=1`` and gets stored blank, and a slug typed with a stray trailing
    space is rejected by the pattern for a reason the caller cannot see. Applied
    to ids too — an id never legitimately carries padding, and trimming turns a
    confusing "not found" into a match.
    """
    return value.strip() if isinstance(value, str) else value


class RunStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class EventSourceType(StrEnum):
    RUN = "run"
    STAGE = "stage"
    TOOL = "tool"
    SYSTEM = "system"
    REVIEW = "review"


class EventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ToolInvocationStatus(StrEnum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class BackgroundJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PROVIDER = "waiting_provider"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"


class EditOperation(StrEnum):
    TRIM = "trim"
    CONCAT = "concat"
    SUBTITLE_BURN_IN = "subtitle_burn_in"
    AUTO_REFRAME = "auto_reframe"
    WATERMARK = "watermark"
    COVER_GENERATION = "cover_generation"
    EXPORT_VERSIONS = "export_versions"


class ProviderCredentialStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ProviderSecretSource(StrEnum):
    """Where a credential's secret material actually lives (V8.4).

    Reported rather than inferred by the client, because the interesting states
    are the ones a boolean cannot express: ``mixed`` is a credential half-migrated
    into the vault, and ``locked`` is one whose ciphertext the running key cannot
    open (a rotation that has not finished). Both are invisible from the outside
    and both change what the operator should do next.
    """

    NONE = "none"
    INLINE = "inline"
    VAULT = "vault"
    MIXED = "mixed"
    LOCKED = "locked"


class ReviewRequestStatus(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ReviewDecisionType(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_CHANGES = "approve_with_changes"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"
    REOPEN = "reopen"
    CANCEL = "cancel"


class ReviewCommentSeverity(StrEnum):
    CRITICAL = "critical"
    SUGGESTION = "suggestion"
    NITPICK = "nitpick"


class ReviewCommentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReviewActionKind(StrEnum):
    RERUN_STAGE = "rerun_stage"
    RERUN_ARTIFACT = "rerun_artifact"
    REGENERATE_MEDIA = "regenerate_media"
    REANALYZE_SOURCE = "reanalyze_source"
    MANUAL = "manual"


class ReviewActionStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DISMISSED = "dismissed"


class ReviewReasonCode(StrEnum):
    CONTENT_ISSUE = "content_issue"
    QUALITY_ISSUE = "quality_issue"
    RIGHTS_ISSUE = "rights_issue"
    BUDGET_ISSUE = "budget_issue"
    STYLE_ISSUE = "style_issue"
    ROUND_LIMIT_OVERRIDE = "round_limit_override"
    OTHER = "other"


class ReviewArtifactRef(BaseModel):
    artifact_id: str
    artifact_type: str
    version: int


class StageInvalidation(BaseModel):
    invalidated_at: datetime | None = None
    invalidated_by_stage_key: str | None = None
    invalidation_reason: str | None = None


class ReviewDecision(BaseModel):
    decision_id: str
    review_request_id: str
    run_id: str
    sequence: int
    round: int
    decision: ReviewDecisionType
    actor_user_id: str | None = None
    actor_display_name: str | None = None
    comment: str | None = None
    reason_code: str | None = None
    artifact_refs: list[ReviewArtifactRef] = Field(default_factory=list)
    created_at: datetime | None = None


class ReviewComment(BaseModel):
    comment_id: str
    review_request_id: str
    run_id: str
    stage_key: str
    artifact_id: str | None = None
    artifact_version: int | None = None
    author_user_id: str | None = None
    author_display_name: str | None = None
    severity: ReviewCommentSeverity
    body: str
    status: ReviewCommentStatus
    resolved_by_decision_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReviewActionItem(BaseModel):
    action_item_id: str
    review_request_id: str
    decision_id: str
    run_id: str
    kind: ReviewActionKind
    stage_key: str | None = None
    artifact_type: str | None = None
    stage_run_id: str | None = None
    instruction: str
    linked_comment_ids: list[str] = Field(default_factory=list)
    result_action: str | None = None
    status: ReviewActionStatus
    produced_artifact_id: str | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None


class ReviewAttachment(BaseModel):
    """An evidence asset mounted on a review request, with an asset snapshot."""

    attachment_id: str
    review_request_id: str
    run_id: str
    asset_id: str
    note: str | None = None
    added_by_user_id: str | None = None
    added_by_display_name: str | None = None
    # Quoted: ``Asset`` is defined later in this module (the review block comes
    # first); the model_rebuild() call after its definition resolves this.
    asset: "Asset | None" = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReviewRequest(BaseModel):
    review_request_id: str
    project_id: str
    run_id: str
    stage_run_id: str
    stage_key: str
    pipeline_key: str
    iteration: int = 1
    status: ReviewRequestStatus
    current_decision: ReviewDecisionType | None = None
    gate_type: str = "human_review"
    gates_stage_key: str | None = None
    artifact_scope: list[ReviewArtifactRef] = Field(default_factory=list)
    review_focus: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    assignee_user_id: str | None = None
    due_at: datetime | None = None
    overdue: bool = False
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReviewRequestDetail(ReviewRequest):
    decisions: list[ReviewDecision] = Field(default_factory=list)
    comments: list[ReviewComment] = Field(default_factory=list)
    action_items: list[ReviewActionItem] = Field(default_factory=list)
    attachments: list[ReviewAttachment] = Field(default_factory=list)
    workspace_url: str = ""
    run_board_url: str = ""


class ReviewInboxItem(BaseModel):
    review_request_id: str
    project_id: str
    run_id: str
    stage_run_id: str
    stage_key: str
    pipeline_key: str
    iteration: int = 1
    status: ReviewRequestStatus
    current_decision: ReviewDecisionType | None = None
    assignee_user_id: str | None = None
    due_at: datetime | None = None
    overdue: bool = False
    opened_at: datetime | None = None
    open_critical_comments: int = 0
    open_action_items: int = 0
    workspace_url: str = ""


class ReviewInbox(BaseModel):
    project_id: str | None = None
    assignee_user_id: str | None = None
    total: int = 0
    overdue_total: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    items: list[ReviewInboxItem] = Field(default_factory=list)


class ReviewRequestCreate(BaseModel):
    stage_key: str = Field(min_length=1, max_length=120)
    artifact_refs: list[ReviewArtifactRef] | None = None
    assignee_user_id: str | None = None
    due_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)
    override_round_limit: bool = False


class ReviewActionItemInput(BaseModel):
    kind: ReviewActionKind
    stage_key: str | None = Field(default=None, max_length=120)
    artifact_type: str | None = Field(default=None, max_length=120)
    instruction: str = Field(min_length=1)
    linked_comment_ids: list[str] = Field(default_factory=list)


class SubmitReviewDecision(BaseModel):
    decision: ReviewDecisionType
    comment: str | None = None
    reason_code: ReviewReasonCode | None = None
    artifact_refs: list[ReviewArtifactRef] | None = None
    action_items: list[ReviewActionItemInput] = Field(default_factory=list)
    actor_user_id: str | None = None
    actor_display_name: str | None = None
    override_round_limit: bool = False


class CreateReviewComment(BaseModel):
    body: str = Field(min_length=1)
    severity: ReviewCommentSeverity = ReviewCommentSeverity.SUGGESTION
    artifact_id: str | None = None
    artifact_version: int | None = None
    author_user_id: str | None = None
    author_display_name: str | None = None


class UpdateReviewComment(BaseModel):
    status: ReviewCommentStatus


class CreateReviewAttachment(BaseModel):
    asset_id: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)
    added_by_user_id: str | None = None
    added_by_display_name: str | None = None


class AssignReviewRequest(BaseModel):
    assignee_user_id: str | None = None
    due_at: datetime | None = None


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str | None = None
    default_language: str = "zh-CN"
    default_aspect_ratio: str = "9:16"


class ProjectTeamAssign(BaseModel):
    """Which team's quota this project's spend counts against.

    ``team_id`` has no default on purpose: the two intents — "move it to this
    team" and "take it off every team" — are both legitimate, and an omitted
    field would have to guess. Sending ``null`` explicitly means *the default
    team*, which is also what a project created the ordinary way gets.
    """

    team_id: str | None


class Project(BaseModel):
    project_id: str
    title: str
    description: str | None = None
    default_language: str
    default_aspect_ratio: str
    default_style_preset_id: str | None = None
    team_id: str | None = None


class ProjectDashboardProjectSummary(BaseModel):
    project: Project
    run_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    latest_run_id: str | None = None
    latest_run_status: RunStatus | None = None
    latest_run_updated_at: datetime | None = None


class RunCreate(BaseModel):
    pipeline_key: str = "avatar_spokesperson"
    raw_script: str = Field(min_length=1)
    brand_name: str | None = None
    product_name: str | None = None
    target_platform: str = "douyin"
    target_duration_seconds: int = Field(default=15, ge=5, le=300)
    aspect_ratio: str = "9:16"
    style_key: str | None = None
    cost_budget: float | None = Field(default=None, ge=0)
    quality_mode: str | None = Field(
        default=None,
        pattern="^(local_preview|provider_audio|provider_avatar)$",
        description=(
            "Declared quality intent for the run. When set, the execution plan "
            "flags any capability it can only satisfy on the other runtime tier "
            "instead of silently falling back to it."
        ),
    )


class StartRunRequest(BaseModel):
    """Acknowledgement carried by a run start when the plan reports a downgrade."""

    accept_intent_downgrade: bool = False


class ProviderConsentRequest(BaseModel):
    """First-use confirmation for a paid provider on a specific run (T76)."""

    provider: str = Field(min_length=1)
    accept: bool = True
    note: str | None = Field(default=None, max_length=500)


class RunCopyRequest(BaseModel):
    target_project_id: str | None = None
    copy_reason: str | None = Field(default=None, max_length=500)
    overrides: dict[str, Any] = Field(default_factory=dict)


class DashboardJobSummary(BaseModel):
    job_id: str
    run_id: str
    stage_run_id: str | None = None
    job_type: str
    status: BackgroundJobStatus
    progress: int = Field(ge=0, le=100)
    attempt: int = 1
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    error_category: str | None = None
    error_user_message: str | None = None
    updated_at: datetime | None = None


class DashboardReviewItem(BaseModel):
    run_id: str
    project_id: str
    stage_run_id: str | None = None
    stage_key: str
    stage_status: StageStatus
    run_status: RunStatus
    requires_human_review: bool = True
    updated_at: datetime | None = None
    workspace_url: str
    review_request_id: str | None = None
    iteration: int | None = None
    review_status: ReviewRequestStatus | None = None
    # ``approve`` and ``approve_with_changes`` both close a request as approved;
    # the decision is what tells them apart in the queue.
    current_decision: ReviewDecisionType | None = None
    assignee_user_id: str | None = None
    due_at: datetime | None = None
    overdue: bool = False
    invalidated: bool = False


class DashboardRunSummary(BaseModel):
    run_id: str
    project_id: str
    pipeline_key: str
    status: RunStatus
    current_stage_key: str | None = None
    target_platform: str | None = None
    target_duration_seconds: int | None = None
    brand_name: str | None = None
    product_name: str | None = None
    script_preview: str | None = None
    stage_counts: dict[str, int] = Field(default_factory=dict)
    job_counts: dict[str, int] = Field(default_factory=dict)
    artifact_count: int = 0
    asset_count: int = 0
    failed_job_count: int = 0
    waiting_review_stage_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    workspace_url: str


class ProjectDashboardMetrics(BaseModel):
    total_runs: int = 0
    active_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    cancelled_runs: int = 0
    waiting_review_runs: int = 0
    pending_reviews: int = 0
    overdue_reviews: int = 0
    invalidated_stages: int = 0
    queued_jobs: int = 0
    running_jobs: int = 0
    waiting_provider_jobs: int = 0
    failed_jobs: int = 0


class ProjectDashboard(BaseModel):
    project: Project
    metrics: ProjectDashboardMetrics
    status_counts: dict[str, int] = Field(default_factory=dict)
    pipeline_counts: dict[str, int] = Field(default_factory=dict)
    runs: list[DashboardRunSummary] = Field(default_factory=list)
    failed_jobs: list[DashboardJobSummary] = Field(default_factory=list)
    review_queue: list[DashboardReviewItem] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


# --- observability (V8.2) -------------------------------------------------


class ObservabilityCost(BaseModel):
    """Money and tokens the project's runs have consumed, from the ledger."""

    estimated_total: float = 0.0
    actual_total: float = 0.0
    by_provider: dict[str, float] = Field(default_factory=dict)
    by_tool_key: dict[str, float] = Field(default_factory=dict)
    by_source_type: dict[str, float] = Field(default_factory=dict)


class ObservabilityLatency(BaseModel):
    """Tool invocation durations (``duration_ms``), percentiles over all samples."""

    sample_count: int = 0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0


class ObservabilityJobs(BaseModel):
    by_status: dict[str, int] = Field(default_factory=dict)
    by_queue: dict[str, int] = Field(default_factory=dict)
    dead_letter: int = 0
    top_error_codes: dict[str, int] = Field(default_factory=dict)


class ObservabilityRecentError(BaseModel):
    event_type: str
    message: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None


class ObservabilityEvents(BaseModel):
    errors_by_type: dict[str, int] = Field(default_factory=dict)
    recent_errors: list[ObservabilityRecentError] = Field(default_factory=list)


class ProjectObservability(BaseModel):
    """One request's worth of "is this project healthy" (V8.2).

    All aggregates are computed Python-side over the project's own rows —
    the same convention the dashboard uses; every source column is indexed.
    """

    project_id: str
    generated_at: datetime | None = None
    cost: ObservabilityCost = Field(default_factory=ObservabilityCost)
    latency: ObservabilityLatency = Field(default_factory=ObservabilityLatency)
    jobs: ObservabilityJobs = Field(default_factory=ObservabilityJobs)
    events: ObservabilityEvents = Field(default_factory=ObservabilityEvents)


class BrandKitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    logo_asset_id: str | None = Field(default=None, max_length=64)
    color_palette: dict[str, Any] = Field(default_factory=dict)
    typography: dict[str, Any] = Field(default_factory=dict)
    safe_area: dict[str, Any] = Field(default_factory=dict)
    brand_voice: dict[str, Any] = Field(default_factory=dict)
    banned_terms: list[str] = Field(default_factory=list)
    brand_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=32)


class BrandKitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    logo_asset_id: str | None = Field(default=None, max_length=64)
    color_palette: dict[str, Any] | None = None
    typography: dict[str, Any] | None = None
    safe_area: dict[str, Any] | None = None
    brand_voice: dict[str, Any] | None = None
    banned_terms: list[str] | None = None
    brand_metadata: dict[str, Any] | None = None
    status: str | None = Field(default=None, max_length=32)


class BrandKit(BaseModel):
    brand_kit_id: str
    tenant_id: str
    project_id: str
    name: str
    description: str | None = None
    logo_asset_id: str | None = None
    color_palette: dict[str, Any] = Field(default_factory=dict)
    typography: dict[str, Any] = Field(default_factory=dict)
    safe_area: dict[str, Any] = Field(default_factory=dict)
    brand_voice: dict[str, Any] = Field(default_factory=dict)
    banned_terms: list[str] = Field(default_factory=list)
    brand_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StylePresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    brand_kit_id: str | None = Field(default=None, max_length=64)
    style_key: str | None = Field(default=None, max_length=120)
    is_default: bool = False
    aspect_ratio: str | None = Field(default=None, max_length=16)
    subtitle_style: dict[str, Any] = Field(default_factory=dict)
    visual_style: dict[str, Any] = Field(default_factory=dict)
    tone: dict[str, Any] = Field(default_factory=dict)
    prompt_guidelines: dict[str, Any] = Field(default_factory=dict)
    render_defaults: dict[str, Any] = Field(default_factory=dict)
    provider_preferences: dict[str, Any] = Field(default_factory=dict)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    preset_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=32)


class StylePresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    brand_kit_id: str | None = Field(default=None, max_length=64)
    style_key: str | None = Field(default=None, max_length=120)
    is_default: bool | None = None
    aspect_ratio: str | None = Field(default=None, max_length=16)
    subtitle_style: dict[str, Any] | None = None
    visual_style: dict[str, Any] | None = None
    tone: dict[str, Any] | None = None
    prompt_guidelines: dict[str, Any] | None = None
    render_defaults: dict[str, Any] | None = None
    provider_preferences: dict[str, Any] | None = None
    guardrails: dict[str, Any] | None = None
    preset_metadata: dict[str, Any] | None = None
    status: str | None = Field(default=None, max_length=32)


class StylePreset(BaseModel):
    style_preset_id: str
    tenant_id: str
    project_id: str
    brand_kit_id: str | None = None
    name: str
    description: str | None = None
    style_key: str | None = None
    is_default: bool = False
    aspect_ratio: str | None = None
    subtitle_style: dict[str, Any] = Field(default_factory=dict)
    visual_style: dict[str, Any] = Field(default_factory=dict)
    tone: dict[str, Any] = Field(default_factory=dict)
    prompt_guidelines: dict[str, Any] = Field(default_factory=dict)
    render_defaults: dict[str, Any] = Field(default_factory=dict)
    provider_preferences: dict[str, Any] = Field(default_factory=dict)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    preset_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StageRun(BaseModel):
    stage_run_id: str | None = None
    stage_key: str
    order: int
    status: StageStatus
    requires_human_review: bool
    output_artifact_types: list[str]
    invalidation: StageInvalidation | None = None


class ProductionRun(BaseModel):
    run_id: str
    project_id: str
    pipeline_key: str
    pipeline_version_id: str | None = None
    pipeline_version: str | None = None
    pipeline_dsl_version: str | None = None
    pipeline_definition_snapshot: dict[str, Any] | None = None
    status: RunStatus
    current_stage_key: str | None = None
    input_payload: dict[str, Any]
    estimated_cost: float | None = None
    actual_cost: float | None = None
    stages: list[StageRun]


class RunCopyResult(BaseModel):
    source_run_id: str
    run: ProductionRun
    workspace_url: str


class ProviderCredentialCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=160)
    project_id: str | None = None
    credential_type: str = Field(default="api_key", max_length=64)
    status: ProviderCredentialStatus = ProviderCredentialStatus.ACTIVE
    secret_values: dict[str, str] = Field(default_factory=dict)
    quota: dict[str, Any] = Field(default_factory=dict)
    credential_metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderCredentialUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=160)
    project_id: str | None = None
    status: ProviderCredentialStatus | None = None
    secret_values: dict[str, str] | None = None
    quota: dict[str, Any] | None = None
    credential_metadata: dict[str, Any] | None = None


class ProviderCredential(BaseModel):
    credential_id: str
    tenant_id: str
    project_id: str | None = None
    provider: str
    label: str | None = None
    credential_type: str
    status: ProviderCredentialStatus
    secret_names: list[str]
    secret_preview: dict[str, str]
    #: Where the values come from, and whether the current key can open them.
    #: ``encrypted`` is the one-liner for "this credential is fully in the vault".
    secret_source: ProviderSecretSource = ProviderSecretSource.NONE
    encrypted: bool = False
    quota: dict[str, Any] = Field(default_factory=dict)
    credential_metadata: dict[str, Any] = Field(default_factory=dict)
    last_check_status: str | None = None
    last_check_message: str | None = None
    last_checked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EncryptedSecretSummary(BaseModel):
    """A vault entry, described without its ciphertext.

    The vault is not enumerable through the API — there is no "list all secrets"
    endpoint, deliberately — but a rotation has to report what it touched, and
    this is the shape it reports.
    """

    secret_id: str
    name: str
    algorithm: str
    key_version: int
    rotated_at: datetime | None = None


class ProviderCredentialRotation(BaseModel):
    """The outcome of re-sealing one credential's secrets.

    ``sealed`` counts legacy inline values moved into the vault, ``resealed``
    counts vault references encrypted again under the current key version, and
    ``locked`` counts references the current key cannot open. A caller that sees a
    non-zero ``locked`` knows the previous key is still the one that sealed them.
    """

    credential: ProviderCredential
    sealed: int = 0
    resealed: int = 0
    locked: int = 0
    secrets: list[EncryptedSecretSummary] = Field(default_factory=list)


class ProviderCredentialCheckResult(BaseModel):
    credential: ProviderCredential
    ok: bool
    provider: str
    message: str
    env_var_names: list[str] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)
    tool_statuses: list[dict[str, Any]] = Field(default_factory=list)


class ProviderCallbackRequest(BaseModel):
    external_task_id: str = Field(min_length=1, max_length=255)
    status: str | None = None
    provider_status: str | None = None
    video_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approved|approved_with_changes|rejected|cancelled)$")
    comment: str | None = None


class Artifact(BaseModel):
    artifact_id: str
    run_id: str
    stage_key: str
    artifact_type: str
    version: int
    schema_version: str = "1.0"
    schema_key: str | None = None
    producer: str | None = None
    lineage: dict[str, Any] | None = None
    derived_from: list[dict[str, Any]] | None = None
    is_current: bool = True
    payload: dict[str, Any]


class AssetUpload(BaseModel):
    asset_type: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)
    stage_run_id: str | None = None
    provider: str | None = None
    tool_key: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class Asset(BaseModel):
    asset_id: str
    run_id: str
    stage_run_id: str | None = None
    asset_type: str
    provider: str | None = None
    tool_key: str | None = None
    storage_url: str
    mime_type: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    checksum: str | None = None
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


# Resolve the quoted ``Asset`` reference in ReviewAttachment now that Asset exists.
ReviewAttachment.model_rebuild()


ASSET_COLLECTION_KIND_PATTERN = "^(project_asset|brand_asset|person|voice|music|clip)$"


class AssetProvenance(BaseModel):
    """Where a library entry came from.

    Kept next to the entry rather than on the asset, because the same asset can be
    reused under different provenance claims (a generated take promoted as an owned
    brand voice, for instance).
    """

    source: str = Field(default="uploaded", pattern="^(uploaded|generated|imported)$")
    provider: str | None = Field(default=None, max_length=120)
    tool_key: str | None = Field(default=None, max_length=120)
    source_run_id: str | None = Field(default=None, max_length=64)
    source_asset_id: str | None = Field(default=None, max_length=64)
    origin_note: str | None = None


class AssetLicense(BaseModel):
    """Usage rights carried by a library entry."""

    license_type: str = Field(
        default="unknown",
        pattern="^(owned|licensed|commissioned|cc0|unknown)$",
    )
    holder: str | None = Field(default=None, max_length=160)
    usage_scope: str | None = None
    expires_at: datetime | None = None
    restrictions: list[str] = Field(default_factory=list)


class AssetCollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="project_asset", pattern=ASSET_COLLECTION_KIND_PATTERN)
    description: str | None = None
    source_run_id: str | None = Field(default=None, max_length=64)
    is_default: bool = False
    collection_metadata: dict[str, Any] = Field(default_factory=dict)


class AssetCollectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    is_default: bool | None = None
    status: str | None = Field(default=None, max_length=32)
    collection_metadata: dict[str, Any] | None = None


class AssetCollectionItemAdd(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    role: str = Field(default="reference", min_length=1, max_length=120)
    note: str | None = None
    provenance: AssetProvenance = Field(default_factory=AssetProvenance)
    license: AssetLicense = Field(default_factory=AssetLicense)


class AssetCollectionItemUpdate(BaseModel):
    role: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = None
    license: AssetLicense | None = None
    status: str | None = Field(default=None, max_length=32)


class AssetCollectionItem(BaseModel):
    item_id: str
    collection_id: str
    asset_id: str
    role: str
    note: str | None = None
    provenance: AssetProvenance = Field(default_factory=AssetProvenance)
    license: AssetLicense = Field(default_factory=AssetLicense)
    status: str
    asset: Asset | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AssetCollection(BaseModel):
    collection_id: str
    tenant_id: str
    project_id: str
    kind: str
    name: str
    description: str | None = None
    source_run_id: str | None = None
    is_default: bool = False
    status: str
    collection_metadata: dict[str, Any] = Field(default_factory=dict)
    item_count: int = 0
    items: list[AssetCollectionItem] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectAssetLibrary(BaseModel):
    project_id: str
    collections: list[AssetCollection] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)


class AssetCollectionImportRequest(BaseModel):
    """Promote a run's assets into a collection so later runs can reuse them."""

    run_id: str = Field(min_length=1, max_length=64)
    asset_ids: list[str] = Field(default_factory=list)
    asset_types: list[str] = Field(default_factory=list)
    role: str = Field(default="reference", min_length=1, max_length=120)
    note: str | None = None
    license: AssetLicense = Field(default_factory=AssetLicense)
    include_removed: bool = False


class AssetCollectionImportResult(BaseModel):
    collection_id: str
    run_id: str
    imported_count: int
    added_item_ids: list[str] = Field(default_factory=list)
    skipped_asset_ids: list[str] = Field(default_factory=list)


class AssetReuseRequest(BaseModel):
    """Pull one library entry back into a target run as a run-owned asset."""

    item_id: str = Field(min_length=1, max_length=64)
    asset_type: str | None = Field(default=None, min_length=1, max_length=120)
    stage_run_id: str | None = Field(default=None, max_length=120)
    note: str | None = None


class AssetReuseResult(BaseModel):
    run_id: str
    asset: Asset
    source_item_id: str
    source_collection_id: str
    reused_from_asset_id: str
    provenance: AssetProvenance = Field(default_factory=AssetProvenance)
    license: AssetLicense = Field(default_factory=AssetLicense)


# --- V6.5 team / permission -------------------------------------------------


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    status: str | None = Field(default=None, max_length=32)


class Team(BaseModel):
    team_id: str
    tenant_id: str
    name: str
    slug: str
    description: str | None = None
    is_default: bool = False
    status: str
    member_count: int = 0
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TeamMemberAdd(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(default="member", max_length=32)
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=255)
    note: str | None = None


class TeamMemberUpdate(BaseModel):
    role: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=32)
    note: str | None = None


class TeamMembership(BaseModel):
    membership_id: str
    team_id: str
    user_id: str
    role: str
    status: str
    user_name: str | None = None
    user_email: str | None = None
    invited_by: str | None = None
    note: str | None = None
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectMemberAdd(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(default="editor", max_length=32)
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=255)
    note: str | None = None


class ProjectMemberUpdate(BaseModel):
    role: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=32)
    note: str | None = None


class ProjectMembership(BaseModel):
    membership_id: str
    project_id: str
    user_id: str
    role: str
    status: str
    user_name: str | None = None
    user_email: str | None = None
    invited_by: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActorProfile(BaseModel):
    """Who the API thinks is calling, and what they may do here."""

    actor_id: str
    display_name: str | None = None
    tenant_id: str
    source: str
    team_id: str | None = None
    team_role: str | None = None
    permissions: list[str] = Field(default_factory=list)


class PermissionMatrix(BaseModel):
    permissions: list[str] = Field(default_factory=list)
    team_roles: list[dict[str, Any]] = Field(default_factory=list)
    project_roles: list[dict[str, Any]] = Field(default_factory=list)


class AuditLogEntry(BaseModel):
    audit_id: str
    tenant_id: str
    actor_id: str | None = None
    action: str
    entity_type: str
    entity_id: str | None = None
    project_id: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# --- V7.1 API contract -------------------------------------------------------


class ErrorCodeDefinition(BaseModel):
    """One entry of the published error catalog (``GET /api/error-codes``)."""

    code: str
    status_code: int
    category: str
    retryable: bool
    description: str


class ErrorDetail(BaseModel):
    """The body of a failure.

    ``code`` is stable and safe to branch on; ``message`` is for humans and may
    change. Gate failures add their own fields next to these two, which is why
    extra keys are allowed rather than rejected.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """The single failure shape every endpoint returns (V7.1)."""

    detail: ErrorDetail


class Page(BaseModel, Generic[ItemT]):
    """The paginated envelope new V7+ list endpoints return.

    Existing list endpoints keep returning bare arrays so the shipped Web pages
    do not change; they expose the same pagination inputs and an
    ``X-Total-Count`` header instead. Anything added from V7 onwards returns
    ``Page`` so an SDK has one shape to parse.
    """

    items: list[ItemT] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class IdempotencyKeyEntry(BaseModel):
    """A recorded idempotent write, exposed for SDK debugging."""

    idempotency_key: str
    actor_id: str
    method: str
    path: str
    state: str
    status_code: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


# --- V7.2 programmatic credentials ------------------------------------------


class ApiKeyCreate(BaseModel):
    """Request body for issuing a key.

    ``scopes`` narrows the key *below* the owner's own permissions; omitting it
    means "whatever the owner may do, and nothing they may not". Listing a scope
    the owner does not hold is refused rather than silently ignored, because a
    key that quietly grants less than the caller asked for is worse than an error.
    """

    name: str = Field(min_length=1, max_length=160)
    scopes: list[str] | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKey(BaseModel):
    """A key as it is listed afterwards — ``api_key`` plaintext is never included."""

    api_key_id: str
    tenant_id: str
    actor_id: str
    name: str
    key_prefix: str
    scopes: list[str] | None = None
    status: str
    created_by: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApiKeyCreated(ApiKey):
    """The response to issuing a key: the only place the plaintext ever appears."""

    api_key: str


# --- V1.1 username-password login ---------------------------------------------


class LoginRequest(BaseModel):
    """Request body for ``POST /api/auth/login``."""

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class LoginUser(BaseModel):
    """The user a session belongs to, as the login and introspection report it."""

    user_id: str
    email: str
    name: str
    tenant_id: str
    role: str
    status: str


class LoginResult(BaseModel):
    """The response to a successful login.

    ``token`` is the only place the session plaintext ever appears; every later
    call presents it as ``Authorization: Bearer <token>``.
    """

    session_id: str
    token: str
    token_prefix: str
    expires_at: datetime | None = None
    user: LoginUser


class AuthSessionInfo(BaseModel):
    """The introspection view of a live session — the plaintext never returns."""

    session_id: str
    token_prefix: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    user: LoginUser


class LogoutResult(BaseModel):
    """The response to ``POST /api/auth/logout``; revoking twice is not an error."""

    session_id: str
    revoked: bool


class PasswordSetRequest(BaseModel):
    """Request body for setting a user's login password.

    The floor is deliberately small: eight characters is the least a password
    can be expected to be, and anything stricter belongs to the deployment's
    own account policy, not to a schema this API cannot see past.
    """

    password: str = Field(min_length=8, max_length=1024)
    email: str | None = Field(default=None, min_length=3, max_length=255)


# --- V7.3 outbound webhooks -------------------------------------------------


class WebhookSubscriptionStatus(StrEnum):
    """``paused`` is a manual stop; ``disabled`` is what auto-pause writes.

    They are kept apart so a look at the row says *why* it stopped: a subscriber
    that tripped the failure threshold must not be indistinguishable from one an
    operator switched off on purpose.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class WebhookEventDefinition(BaseModel):
    """One published event name, from ``GET /api/webhook-events``."""

    event_type: str
    description: str
    sources: list[str] = Field(default_factory=list)


class WebhookSubscriptionCreate(BaseModel):
    """Request body for registering an endpoint.

    ``event_types`` is validated against the published catalog: an unknown name
    is refused rather than stored, because a subscription that silently matches
    nothing looks identical to a working one that simply has not fired yet.
    """

    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=500)
    event_types: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=2000)
    status: str = Field(default="active", pattern="^(active|paused)$")


class WebhookSubscriptionUpdate(BaseModel):
    """Partial update. Omitted fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    event_types: list[str] | None = None
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern="^(active|paused)$")


class WebhookSubscription(BaseModel):
    """A registered endpoint, including the secret needed to verify deliveries.

    The secret is returned on every read rather than once at creation, unlike an
    API key. The difference is who holds it: an API key authenticates its owner
    *to* the platform, so a leak is a breach, whereas a signing secret only lets
    its holder *verify* messages the platform already sends to it. Hiding it
    would just force subscribers to rotate on every deployment.
    """

    subscription_id: str
    tenant_id: str
    name: str
    url: str
    secret: str
    event_types: list[str] = Field(default_factory=list)
    status: str
    description: str | None = None
    created_by: str | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WebhookDelivery(BaseModel):
    """One queued or attempted delivery, exposed for debugging and replay."""

    delivery_id: str
    tenant_id: str
    subscription_id: str
    event_id: str
    event_type: str
    run_id: str | None = None
    status: str
    attempt: int = 0
    max_attempts: int = 6
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None
    response_status: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SourceMediaFileInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)
    mime_type: str | None = None
    media_type: str | None = Field(
        default=None,
        pattern="^(video|audio|image|document|other)$",
    )
    label: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, max_length=120)
    rights: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class SourceMediaLinkInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    media_type: str | None = Field(
        default=None,
        pattern="^(video|audio|image|document|other)$",
    )
    mime_type: str | None = None
    label: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, max_length=120)
    rights: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class SourceMediaIngestRequest(BaseModel):
    stage_key: str = Field(default="source_ingest", min_length=1, max_length=120)
    stage_run_id: str | None = Field(default=None, max_length=120)
    files: list[SourceMediaFileInput] = Field(default_factory=list)
    links: list[SourceMediaLinkInput] = Field(default_factory=list)
    source_context: dict[str, Any] = Field(default_factory=dict)
    default_rights: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class SourceMediaIngestResult(BaseModel):
    artifact: Artifact
    source_media_review: Artifact
    assets: list[Asset]
    links: list[dict[str, Any]]
    manifest: dict[str, Any]


class MediaAnalysisRequest(BaseModel):
    stage_key: str = Field(default="analysis", min_length=1, max_length=120)
    stage_run_id: str | None = Field(default=None, max_length=120)
    source_asset_ids: list[str] = Field(default_factory=list)
    source_artifact_type: str = Field(default="source_asset_manifest", max_length=120)
    max_frames_per_video: int = Field(default=5, ge=0, le=20)
    include_frame_assets: bool = True
    # V1.1 advanced analysis switches. All default on so an existing caller
    # gets the deeper report for free; every knob is a *detector* knob, and the
    # value actually used is echoed back in the report payload.
    detect_scenes: bool = True
    scene_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    analyze_audio: bool = True
    audio_window_seconds: float = Field(default=0.5, gt=0.0, le=10.0)
    transcribe_audio: bool = True
    provider_options: dict[str, Any] = Field(default_factory=dict)


class MediaAnalysisResult(BaseModel):
    artifact: Artifact
    video_analysis_brief: Artifact
    frame_assets: list[Asset]
    report: dict[str, Any]
    brief: dict[str, Any]


class ScriptScreeningParseRequest(BaseModel):
    raw_script: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    brand_name: str | None = Field(default=None, max_length=160)
    product_name: str | None = Field(default=None, max_length=160)
    target_platform: str | None = Field(default=None, max_length=80)
    target_duration_seconds: int | None = Field(default=None, ge=5, le=300)
    aspect_ratio: str = "9:16"
    default_language: str = "zh-CN"
    style_key: str | None = Field(default=None, max_length=120)
    parser_options: dict[str, Any] = Field(default_factory=dict)


class ScriptScreeningParseResult(BaseModel):
    scene_plan: dict[str, Any]
    spoken_script: dict[str, Any]
    shot_list: dict[str, Any]
    run_input: dict[str, Any]


class ScriptScreeningRunCreateRequest(ScriptScreeningParseRequest):
    pipeline_key: str = "avatar_spokesperson"
    cost_budget: float | None = Field(default=None, ge=0)


class ScriptScreeningRunCreateResult(BaseModel):
    project: Project
    run: ProductionRun
    scene_plan_artifact: Artifact
    spoken_script_artifact: Artifact
    shot_list_artifact: Artifact
    parse_result: ScriptScreeningParseResult


class ToolInvocation(BaseModel):
    invocation_id: str
    run_id: str
    stage_run_id: str | None = None
    tool_key: str
    provider: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    status: ToolInvocationStatus
    cost: float | None = None
    duration_ms: int | None = None
    external_task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_category: str | None = None
    error_taxonomy_code: str | None = None
    error_retryable: bool | None = None
    error_user_message: str | None = None
    error_remediation: str | None = None


class BackgroundJob(BaseModel):
    job_id: str
    run_id: str
    stage_run_id: str | None = None
    job_type: str
    status: BackgroundJobStatus
    progress: int = Field(ge=0, le=100)
    request_payload: dict[str, Any]
    result_payload: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    error_category: str | None = None
    error_taxonomy_code: str | None = None
    error_retryable: bool | None = None
    error_user_message: str | None = None
    error_remediation: str | None = None
    attempt: int = 1
    max_attempts: int = 3
    locked_by: str | None = None
    queue: str = "media"
    priority: int = Field(default=50, ge=0, le=99)
    started_at: datetime | None = None
    lease_expires_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BackgroundJobCreate(BaseModel):
    stage_run_id: str | None = None
    job_type: str = Field(min_length=1, max_length=120)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)
    priority: int = Field(default=50, ge=0, le=99)


class UsageLedgerEntry(BaseModel):
    ledger_entry_id: str
    run_id: str
    stage_run_id: str | None = None
    source_type: str
    source_id: str
    provider: str | None = None
    tool_key: str | None = None
    operation: str
    estimated_cost: float | None = None
    actual_cost: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    asset_bytes: int | None = None
    usage_unit: str | None = None
    usage_quantity: float | None = None
    entry_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class UsageSummary(BaseModel):
    run_id: str
    ledger_entry_count: int
    estimated_cost: float
    actual_cost: float
    input_tokens: int
    output_tokens: int
    duration_ms: int
    asset_bytes: int
    budget_limit: float | None = None
    budget_remaining: float | None = None
    budget_status: str
    by_source_type: dict[str, dict[str, float | int]] = Field(default_factory=dict)
    by_provider: dict[str, dict[str, float | int]] = Field(default_factory=dict)
    by_tool_key: dict[str, dict[str, float | int]] = Field(default_factory=dict)
    entries: list[UsageLedgerEntry] = Field(default_factory=list)


class ManifestActionName(StrEnum):
    DISPATCH_READY_STEPS = "dispatch_ready_steps"
    GENERATE_PERSON_IMAGE = "generate_person_image"
    UPLOAD_PERSON_IMAGE = "upload_person_image"
    GENERATE_TTS = "generate_tts"
    ANALYZE_SOURCE_MEDIA = "analyze_source_media"
    PROCESS_JOB = "process_job"
    RETRY_JOB = "retry_job"


class ManifestActionRequest(BaseModel):
    action: ManifestActionName
    stage_key: str | None = Field(default=None, max_length=120)
    stage_run_id: str | None = Field(default=None, max_length=120)
    manifest_step_key: str | None = Field(default=None, max_length=240)
    job_id: str | None = Field(default=None, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)


class ManifestActionResult(BaseModel):
    action: ManifestActionName
    run_id: str
    status: str
    message: str
    result_payload: dict[str, Any] = Field(default_factory=dict)
    refresh: dict[str, bool] = Field(default_factory=dict)


class RunStateActionName(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RERUN_STAGE = "rerun_stage"
    RERUN_ARTIFACT = "rerun_artifact"
    SKIP_OPTIONAL_STAGE = "skip_optional_stage"


class RunStateActionRequest(BaseModel):
    action: RunStateActionName
    stage_key: str | None = Field(default=None, max_length=120)
    artifact_type: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunStateActionResult(BaseModel):
    action: RunStateActionName
    run_id: str
    status: str
    message: str
    transition: dict[str, Any] = Field(default_factory=dict)
    run: ProductionRun
    result_payload: dict[str, Any] = Field(default_factory=dict)


class AvatarVideoSubmitRequest(BaseModel):
    stage_run_id: str | None = None
    shot_id: str | None = None
    image_url: str = Field(min_length=1)
    audio_url: str = Field(min_length=1)
    face_bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    ext_bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    model: str = "wan2.2-s2v"
    duration_ms: int | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class AvatarVideoFromAssetsRequest(BaseModel):
    stage_run_id: str | None = None
    shot_id: str | None = None
    person_image_asset_id: str | None = None
    person_image_url: str | None = Field(default=None, min_length=1, max_length=2000)
    audio_asset_id: str | None = None
    audio_url: str | None = Field(default=None, min_length=1, max_length=2000)
    face_bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    ext_bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    model: str = "wan2.2-s2v"
    public_base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    access_link_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    auto_archive: bool = True
    auto_compose: bool = True
    compose_width: int = Field(default=1080, ge=2)
    compose_height: int = Field(default=1920, ge=2)
    compose_fps: int = Field(default=30, ge=1, le=120)
    compose_crf: int = Field(default=22, ge=0, le=51)
    compose_preset: str = "medium"
    bgm_volume: float = Field(default=0.18, ge=0.0, le=1.0)
    provider_options: dict[str, Any] = Field(default_factory=dict)


class AvatarVideoArchiveRequest(BaseModel):
    filename: str = Field(default="avatar-video.mp4", min_length=1, max_length=255)
    video_url: str | None = None
    video_content_base64: str | None = None
    mime_type: str = "video/mp4"
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class TtsGenerateRequest(BaseModel):
    stage_run_id: str | None = None
    segment_id: str | None = None
    text: str = Field(min_length=1, max_length=600)
    voice: str = "Cherry"
    model: str = "qwen3-tts-flash"
    language_type: str = "Auto"
    filename: str = Field(default="voiceover.wav", min_length=1, max_length=255)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class SubtitleGenerateRequest(BaseModel):
    stage_run_id: str | None = None
    source_artifact_type: str = "spoken_script"
    segments: list[dict[str, Any]] | None = None
    filename: str = Field(default="subtitles.srt", min_length=1, max_length=255)
    max_chars_per_caption: int = Field(default=24, ge=8, le=80)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class MusicGenerateRequest(BaseModel):
    stage_run_id: str | None = None
    prompt: str = Field(min_length=1, max_length=1000)
    music_length_ms: int = Field(default=15000, ge=10000, le=300000)
    model_id: str | None = None
    force_instrumental: bool = True
    output_format: str = "mp3_44100_128"
    filename: str = Field(default="background-music.mp3", min_length=1, max_length=255)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class ImageGenerateRequest(BaseModel):
    stage_run_id: str | None = None
    prompt: str = Field(min_length=1, max_length=1500)
    model: str | None = Field(default=None, max_length=120)
    size: str | None = Field(default=None, max_length=40)
    n: int = Field(default=1, ge=1, le=6)
    prompt_extend: bool = True
    watermark: bool = False
    negative_prompt: str | None = Field(default=None, max_length=500)
    seed: int | None = None
    filename: str = Field(default="generated-image.png", min_length=1, max_length=255)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class ComposeVideoRequest(BaseModel):
    stage_run_id: str | None = None
    avatar_video_asset_id: str
    narration_audio_asset_id: str | None = None
    subtitle_asset_id: str | None = None
    music_asset_id: str | None = None
    filename: str = Field(default="final.mp4", min_length=1, max_length=255)
    render_profile_key: str | None = Field(default=None, max_length=120)
    width: int | None = Field(default=None, ge=2)
    height: int | None = Field(default=None, ge=2)
    fps: int | None = Field(default=None, ge=1, le=120)
    crf: int | None = Field(default=None, ge=0, le=51)
    preset: str | None = None
    video_bitrate: str | None = Field(default=None, max_length=32)
    audio_bitrate: str | None = Field(default=None, max_length=32)
    bgm_volume: float | None = Field(default=None, ge=0.0, le=1.0)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class CompositionGraphBuildRequest(BaseModel):
    stage_run_id: str | None = None
    primary_video_asset_id: str | None = None
    avatar_video_asset_id: str | None = None
    narration_audio_asset_id: str | None = None
    subtitle_asset_id: str | None = None
    music_asset_id: str | None = None
    runtime: str | None = Field(default="ffmpeg", max_length=64)
    render_profile_key: str | None = Field(default=None, max_length=120)
    width: int | None = Field(default=None, ge=2)
    height: int | None = Field(default=None, ge=2)
    fps: int | None = Field(default=None, ge=1, le=120)
    crf: int | None = Field(default=None, ge=0, le=51)
    preset: str | None = None
    video_bitrate: str | None = Field(default=None, max_length=32)
    audio_bitrate: str | None = Field(default=None, max_length=32)
    bgm_volume: float | None = Field(default=None, ge=0.0, le=1.0)
    graph_metadata: dict[str, Any] = Field(default_factory=dict)


class HtmlCanvasPreviewRequest(BaseModel):
    stage_run_id: str | None = None
    composition_graph_artifact_id: str | None = Field(default=None, max_length=64)
    graph_request: CompositionGraphBuildRequest | None = None
    filename: str = Field(default="canvas-preview.html", min_length=1, max_length=255)
    title: str = Field(default="Script2Video Canvas Preview", min_length=1, max_length=160)
    preview_time_seconds: float = Field(default=0.0, ge=0.0)
    include_debug_overlay: bool = True
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class QualityGateRequest(BaseModel):
    stage_run_id: str | None = None
    render_asset_id: str | None = Field(default=None, max_length=64)
    render_summary_artifact_id: str | None = Field(default=None, max_length=64)
    composition_graph_artifact_id: str | None = Field(default=None, max_length=64)
    checks: list[str] | None = None
    min_score: int = Field(default=80, ge=0, le=100)
    expected_audio: bool | None = None


class EditVariantRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    width: int | None = Field(default=None, ge=2)
    height: int | None = Field(default=None, ge=2)
    fps: int | None = Field(default=None, ge=1, le=120)
    fit: str = Field(default="cover", max_length=32)
    watermark_text: str | None = Field(default=None, max_length=160)
    render_profile_key: str | None = Field(default=None, max_length=120)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class EditToolRequest(BaseModel):
    stage_run_id: str | None = None
    operation: EditOperation
    source_asset_id: str | None = Field(default=None, max_length=64)
    source_asset_ids: list[str] = Field(default_factory=list)
    subtitle_asset_id: str | None = Field(default=None, max_length=64)
    filename: str | None = Field(default=None, max_length=255)
    start_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float | None = Field(default=None, gt=0.0)
    cover_time_seconds: float = Field(default=0.0, ge=0.0)
    width: int | None = Field(default=None, ge=2)
    height: int | None = Field(default=None, ge=2)
    fps: int | None = Field(default=None, ge=1, le=120)
    fit: str = Field(default="cover", max_length=32)
    watermark_text: str | None = Field(default=None, max_length=160)
    watermark_position: str = Field(default="bottom_right", max_length=32)
    watermark_opacity: float = Field(default=0.72, ge=0.0, le=1.0)
    render_profile_key: str | None = Field(default=None, max_length=120)
    preset: str = "veryfast"
    crf: int = Field(default=23, ge=0, le=51)
    audio_bitrate: str = Field(default="128k", max_length=32)
    variants: list[EditVariantRequest] = Field(default_factory=list)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class AssetAccessLink(BaseModel):
    asset_id: str
    url: str
    expires_at: datetime


class MvpReferenceAssetInput(BaseModel):
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    content_base64: str | None = Field(default=None, min_length=1)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    mime_type: str | None = None
    description: str | None = Field(default=None, max_length=500)


class MvpSampleRequest(BaseModel):
    title: str = Field(default="MVP spokesperson sample", min_length=1, max_length=160)
    raw_script: str = Field(min_length=1)
    brand_name: str | None = None
    product_name: str | None = None
    person_image: MvpReferenceAssetInput | None = None
    product_image: MvpReferenceAssetInput | None = None
    brand_logo: MvpReferenceAssetInput | None = None
    target_platform: str = "douyin"
    target_duration_seconds: int = Field(default=15, ge=5, le=60)
    aspect_ratio: str = "9:16"
    width: int = Field(default=1080, ge=180)
    height: int = Field(default=1920, ge=320)
    fps: int = Field(default=30, ge=12, le=60)
    crf: int = Field(default=24, ge=0, le=51)
    preset: str = "veryfast"
    include_music: bool = True
    quality_mode: str = Field(default="local_preview", pattern="^(local_preview|provider_audio|provider_avatar)$")
    tts_voice: str = "Cherry"
    music_prompt: str | None = None
    avatar_image_prompt: str | None = Field(default=None, max_length=1500)
    avatar_image_model: str | None = Field(default=None, max_length=120)
    avatar_image_size: str | None = Field(default=None, max_length=40)
    public_base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    use_configured_planner: bool = False
    access_link_ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class MvpSampleResult(BaseModel):
    project: Project
    run: ProductionRun
    render_asset: Asset
    render_summary: Artifact
    asset_manifest: Artifact
    publish_package: Artifact
    final_video_url: str
    workspace_url: str
    avatar_job: BackgroundJob | None = None
    assets: dict[str, Asset]


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=160)
    source_type: EventSourceType
    source_id: str | None = None
    stage_key: str | None = None
    stage_run_id: str | None = None
    tool_invocation_id: str | None = None
    severity: EventSeverity = EventSeverity.INFO
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventLog(BaseModel):
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    source_type: EventSourceType
    source_id: str | None = None
    stage_key: str | None = None
    stage_run_id: str | None = None
    tool_invocation_id: str | None = None
    severity: EventSeverity
    message: str | None = None
    payload: dict[str, Any]
    created_at: datetime


class ProviderCallbackResult(BaseModel):
    matched: bool
    provider: str
    external_task_id: str
    invocation: ToolInvocation | None = None
    job: BackgroundJob | None = None
    event: EventLog | None = None
    message: str


class RunBoardArtifactSummary(BaseModel):
    artifact_id: str
    artifact_type: str
    version: int
    stage_key: str
    schema_key: str | None = None
    producer: str | None = None
    is_current: bool = True
    created_at: datetime | None = None
    title: str | None = None
    summary: str | None = None


class RunBoardAssetSummary(BaseModel):
    asset_id: str
    asset_type: str
    stage_run_id: str | None = None
    provider: str | None = None
    tool_key: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    created_at: datetime | None = None
    previewable: bool = False
    download_url: str


class RunBoardStageLane(BaseModel):
    stage: StageRun
    current: bool = False
    artifacts: list[RunBoardArtifactSummary] = Field(default_factory=list)
    assets: list[RunBoardAssetSummary] = Field(default_factory=list)
    jobs: list[BackgroundJob] = Field(default_factory=list)
    tool_invocations: list[ToolInvocation] = Field(default_factory=list)
    input_issues: list[dict[str, Any]] = Field(default_factory=list)
    repair_actions: list[dict[str, Any]] = Field(default_factory=list)
    review_request: ReviewRequest | None = None
    open_comment_count: int = 0
    open_action_item_count: int = 0


class RunBoardVersionGroup(BaseModel):
    group_key: str
    label: str
    assets: list[RunBoardAssetSummary] = Field(default_factory=list)


class RunBoard(BaseModel):
    run: ProductionRun
    project: Project
    metrics: dict[str, Any] = Field(default_factory=dict)
    stage_lanes: list[RunBoardStageLane] = Field(default_factory=list)
    artifact_timeline: list[RunBoardArtifactSummary] = Field(default_factory=list)
    decision_log: list[dict[str, Any]] = Field(default_factory=list)
    usage_summary: UsageSummary
    job_log: list[BackgroundJob] = Field(default_factory=list)
    cost_log: list[UsageLedgerEntry] = Field(default_factory=list)
    event_log: list[EventLog] = Field(default_factory=list)
    input_issues: list[dict[str, Any]] = Field(default_factory=list)
    repair_actions: list[dict[str, Any]] = Field(default_factory=list)
    final_outputs: list[RunBoardAssetSummary] = Field(default_factory=list)
    version_groups: list[RunBoardVersionGroup] = Field(default_factory=list)
    review_requests: list[ReviewRequest] = Field(default_factory=list)
    review_timeline: list[ReviewDecision] = Field(default_factory=list)
    invalidated_stages: list[str] = Field(default_factory=list)
    intent_downgrades: list[dict[str, Any]] = Field(default_factory=list)
    requires_intent_acknowledgement: bool = False
    cost_reservation: dict[str, Any] = Field(default_factory=dict)
    paid_provider_consents: dict[str, bool] = Field(default_factory=dict)
    paid_providers_pending: list[str] = Field(default_factory=list)
    workspace_url: str
    dashboard_url: str


# --- V7.4 embeddable workspace ----------------------------------------------
#
# ``models`` stays free of app imports (it is the pure DTO layer), so the page
# names and scope literals are repeated here as patterns. The authoritative
# constants live in ``apps.api.app.embed``; ``EmbedSessionCreate`` accepts exactly
# the shapes those constants describe.


class ExternalIntegrationStatus(StrEnum):
    """``disabled`` is the real revocation lever (V7.4).

    Embed sessions are stateless, so there is no per-token kill switch; disabling
    the integration stops every link it ever minted, immediately. It is kept
    distinct from any "paused" wording because the meaning is stronger: the
    platform is no longer trusted to frame our pages at all.
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class ExternalIntegrationCreate(BaseModel):
    """Register a host platform that may embed the Studio."""

    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern="^[a-z0-9][a-z0-9-]*$",
    )
    description: str | None = Field(default=None, max_length=2000)
    allowed_origins: list[str] = Field(default_factory=list)
    status: ExternalIntegrationStatus = ExternalIntegrationStatus.ACTIVE

    @field_validator("name", "slug", "description", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return strip_optional_text(value)


class ExternalIntegrationUpdate(BaseModel):
    """Partial update. Omitted fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    allowed_origins: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return strip_optional_text(value)


class ExternalIntegrationCapabilities(BaseModel):
    """The embed contract, published so a host can self-check before minting."""

    pages: list[str] = Field(default_factory=list)
    default_scopes: list[str] = Field(default_factory=list)
    allowed_scopes: list[str] = Field(default_factory=list)
    default_ttl_seconds: int = 900
    max_ttl_seconds: int = 86400
    embed_path: str = "/embed"


class ExternalIntegration(BaseModel):
    integration_id: str
    tenant_id: str
    name: str
    slug: str
    description: str | None = None
    allowed_origins: list[str] = Field(default_factory=list)
    status: ExternalIntegrationStatus
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    capabilities: ExternalIntegrationCapabilities | None = None


class EmbedSessionCreate(BaseModel):
    """Request body for minting an embed link.

    ``actor_id`` is the end user *in the host system*. We trust whoever holds
    ``integration:manage`` to state who that is, the same trust the
    ``X-Actor-Id`` header extends to an internal gateway — but only for identity.
    What the session may **do** is decided by ``scopes`` (capped by the embed
    allowlist), never by the string.
    """

    project_id: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    actor_name: str | None = Field(default=None, max_length=160)
    page: str = Field(default="workspace", pattern="^(workspace|run-board)$")
    scopes: list[str] | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)

    @field_validator("project_id", "actor_id", "run_id", "actor_name", "page", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return strip_optional_text(value)


class EmbedContext(BaseModel):
    """What a session is pinned to. The server enforces all three."""

    project_id: str
    run_id: str | None = None
    page: str = "workspace"


class EmbedSessionIssue(BaseModel):
    """A freshly minted link. The token itself is the credential."""

    integration_id: str
    token: str
    embed_url: str
    expires_at: datetime
    scopes: list[str] = Field(default_factory=list)
    context: EmbedContext


class EmbedSessionDescription(BaseModel):
    """The answer to ``GET /api/embed/{integration_id}/session`` — a token describing itself.

    Deliberately no seat, team role or permission list: an embed session has no
    seat, and reporting an empty one would invite callers to treat this like
    ``/api/me``.
    """

    actor_id: str
    display_name: str | None = None
    source: str = "embed_session"
    integration_id: str
    integration_name: str | None = None
    context: EmbedContext
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    remaining_seconds: int = 0


# --- V8.3 team quota / billing ----------------------------------------------
#
# A quota is a ceiling per team per period, evaluated against the spend the
# platform already records in ``usage_ledger_entries``. It is deliberately *not*
# an invoice: the platform has no counterparty, so nothing here totals what a
# customer owes — only how much of the team's allowance is gone.


class TeamQuotaPeriod(StrEnum):
    """How the measurement window is derived.

    The window is computed from the period at read time, never stored: a stored
    start/end would need a rollover job, and a deployment without that job would
    quietly stop enforcing at the month boundary.
    """

    CALENDAR_MONTH = "calendar_month"
    ROLLING_30D = "rolling_30d"
    LIFETIME = "lifetime"


class TeamQuotaEnforcement(StrEnum):
    """What a breach does.

    ``hard`` refuses the call that would cross the line; ``soft`` records a
    warning event and lets it through, which is what a team wants while it is
    still calibrating a number it cannot yet defend.
    """

    HARD = "hard"
    SOFT = "soft"


class TeamQuotaUpdate(BaseModel):
    """Body for the upsert. Every limit is optional and ``null`` means unlimited."""

    period: TeamQuotaPeriod = TeamQuotaPeriod.CALENDAR_MONTH
    limit_cost: float | None = Field(default=None, ge=0)
    limit_runs: int | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=0)
    enforcement: TeamQuotaEnforcement = TeamQuotaEnforcement.HARD
    status: str = Field(default="active", pattern="^(active|paused)$")
    note: str | None = None


class TeamQuotaUsage(BaseModel):
    """What the team actually used inside the current window.

    ``spent_cost`` is per-entry ``actual_cost`` when present and falls back to
    ``estimated_cost``, matching the V8.2 panel: a queued or not-yet-callback'd
    call has spent *something*, and reporting zero would let a team spend past
    its ceiling while every row still said "no charge yet".
    """

    period: TeamQuotaPeriod
    window_start: datetime | None = None
    window_end: datetime
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    spent_cost: float = 0.0
    run_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_remaining: float | None = None
    runs_remaining: int | None = None
    tokens_remaining: int | None = None
    status: str = "not_configured"
    exceeded_dimensions: list[str] = Field(default_factory=list)


class TeamQuota(BaseModel):
    """A team's quota row plus the window it is being measured against."""

    quota_id: str | None = None
    tenant_id: str
    team_id: str
    team_name: str | None = None
    is_default_team: bool = False
    period: TeamQuotaPeriod
    limit_cost: float | None = None
    limit_runs: int | None = None
    limit_tokens: int | None = None
    enforcement: TeamQuotaEnforcement
    status: str
    #: False when no row exists (or every limit is null) — a team with no quota
    #: is visibly unguarded rather than silently unguarded.
    configured: bool = False
    created_by: str | None = None
    note: str | None = None
    usage: TeamQuotaUsage
    created_at: datetime | None = None
    updated_at: datetime | None = None


class QuotaOverrideRequest(BaseModel):
    """Approve (or retract) a one-run exemption from the team's hard ceiling."""

    accept: bool = True
    note: str | None = None


class QuotaOverride(BaseModel):
    run_id: str
    accepted: bool
    accepted_by: str | None = None
    #: ISO-8601 recorded on the run's payload, echoed back as stored — the
    #: exemption lives in free-form JSON, so re-parsing it here would invent a
    #: precision the column does not have.
    accepted_at: str | None = None
    note: str | None = None


# --- deployment posture (V8.5) -------------------------------------------------


class MaintenanceState(BaseModel):
    """The only slice of the settings row a request guard reads.

    Deliberately narrow: the guard runs on every mutating request, so it must not
    drag the whole settings row (and its audit trail) through a hot path.
    """

    enabled: bool = False
    message: str | None = None


class DeploymentSettings(BaseModel):
    """The install's persisted settings row.

    ``configured`` is ``False`` only when the singleton is missing — a state the
    migration excludes, but which a hand-built database (a test fixture, a
    restore from a partial dump) can still reach. Reading defaults is always
    better than failing, and saying so out loud beats pretending the row exists.
    """

    settings_id: str = "default"
    deployment_name: str | None = None
    environment: str
    maintenance_mode: bool = False
    maintenance_message: str | None = None
    maintenance_updated_at: datetime | None = None
    maintenance_updated_by: str | None = None
    backup_retention_days: int = 14
    last_backup_at: datetime | None = None
    last_backup_id: str | None = None
    notes: str | None = None
    configured: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeploymentSettingsUpdate(BaseModel):
    """Editable metadata. Absent fields are left alone; the row is not replaced.

    ``environment`` is accepted here even though it defaults from ``APP_ENV``:
    the label an operator prints on a dashboard and the value the platform
    branches on are allowed to differ (``staging`` vs ``dev``), and forcing them
    to be the same field would only make one of the two lie.
    """

    model_config = ConfigDict(extra="forbid")

    deployment_name: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, max_length=32)
    backup_retention_days: int | None = Field(default=None, ge=0, le=3650)
    notes: str | None = Field(default=None, max_length=4000)


class MaintenanceModeRequest(BaseModel):
    enabled: bool
    message: str | None = Field(default=None, max_length=500)


class DeploymentBackupRequest(BaseModel):
    """Report a completed backup so the switchboard knows how stale it is.

    The backup itself is taken by ``scripts/backup.sh`` on the host — the API
    cannot dump a database it is serving out of — so this endpoint is the
    *bookkeeping* half: it records that one happened. That is what makes
    "when was the last backup" answerable without shelling into the box.
    """

    model_config = ConfigDict(extra="forbid")

    backup_id: str = Field(min_length=1, max_length=128)
    taken_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class AlembicStatus(BaseModel):
    """Whether the running code and the migrated schema agree.

    This is the migration runbook reduced to three fields. ``up_to_date`` is the
    one an operator checks first: a deployment where ``pending`` is non-empty is
    serving requests against a schema its own code expects to be newer.
    """

    head: str | None = None
    current: str | None = None
    pending: list[str] = Field(default_factory=list)
    up_to_date: bool = False


class DeploymentOverview(BaseModel):
    """Everything an operator wants on one screen about *this* install."""

    settings: DeploymentSettings
    app_version: str
    environment: str
    migration: AlembicStatus
    #: Which opt-in capabilities this deployment has switched on (V8.2/V8.4).
    #: Booleans only — the point is posture at a glance, not a config dump: a
    #: value here can never leak a secret, because there are no values.
    capabilities: dict[str, bool]
    table_count: int | None = None
