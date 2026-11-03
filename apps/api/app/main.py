import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.app.api_keys import enforce_api_key_identity
from apps.api.app.auth import enforce_session_identity
from apps.api.app.config import settings
from apps.api.app.db_store import DatabaseStore
from apps.api.app.embed import csp_frame_ancestors
from apps.api.app.embed_identity import enforce_embed_identity
from apps.api.app.errors import ApiError, ErrorCode, code_for_status, error_payload
from apps.api.app.idempotency import (
    IdempotencyRecorderMiddleware,
    IdempotentReplay,
    enforce_idempotency,
)
from apps.api.app.models import ErrorEnvelope, ExternalIntegrationStatus
from apps.api.app.observability import (
    METRICS_CONTENT_TYPE,
    ObservabilityMiddleware,
    configure_logging,
    render_metrics,
)
from apps.api.app.permission_policy import enforce_api_permissions, enforce_maintenance_mode
from apps.api.app.permissions import PermissionDenied
from apps.api.app.routes import get_store, router
from apps.api.app.run_gate import RunGateError
from apps.api.app.tenant_scope import enforce_tenant_scope
from apps.api.app.version import APP_VERSION

logger = logging.getLogger(__name__)

# The product ships one roadmap version at a time, so the API version tracks the
# roadmap rather than inventing a second scheme: V0-V6 done -> 0.7, V7 -> 0.8,
# V8 -> 0.9. The URL prefix stays ``/api`` on purpose; renaming it to ``/api/v1``
# would break every existing caller and all five Web pages for no real gain, so
# the version is published through the OpenAPI ``info.version`` instead.
#
# The literal lives in ``version.py`` because the deployment posture report needs
# it too, and that module sits below both this one and the store. Re-exported
# here under the name the tests and the app factory already use.
API_VERSION = APP_VERSION
API_DESCRIPTION = (
    "Clean-room script-to-video production API.\n\n"
    "**Error contract (V7.1).** Every failure returns one envelope — "
    '`{"detail": {"code", "message", ...details}}` — with a code from the '
    "catalog published at `/api/error-codes`.\n\n"
    "**Idempotency.** Any mutating request may carry an `Idempotency-Key` header; "
    "a retry with the same key and body replays the recorded response and sets "
    "`Idempotency-Replayed: true`.\n\n"
    "**Pagination.** List endpoints accept `limit`/`offset`; endpoints that "
    "predate the V7 `Page` envelope return a bare array plus an "
    "`X-Total-Count` header."
)


def workspace_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "review.html"


def dashboard_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "dashboard.html"


def run_board_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "run_board.html"


def asset_library_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "asset_library.html"


def team_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "team.html"


def embed_html_path() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "embed.html"


def create_app() -> FastAPI:
    # Opt-in structured logging (V8.2): the default stays plain text so
    # existing deployments and shell pipelines see no change.
    if settings.log_format == "json":
        configure_logging(format="json", level=settings.log_level)
    app = FastAPI(
        title="Script2Video Studio API",
        version=API_VERSION,
        description=API_DESCRIPTION,
    )

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/workspace")

    # The Web pages are kept out of the schema on purpose: they serve HTML, not
    # JSON, and leaving them in would have an SDK generator emit methods that
    # return a page. That also makes the spec and the permission tables line up
    # exactly — everything documented under ``/api`` is governed by one of them
    # (see ``tests/test_openapi_contract.py``).
    @app.get("/workspace", response_class=FileResponse, include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(workspace_html_path())

    @app.get("/dashboard", response_class=FileResponse, include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(dashboard_html_path())

    @app.get("/run-board", response_class=FileResponse, include_in_schema=False)
    def run_board() -> FileResponse:
        return FileResponse(run_board_html_path())

    @app.get("/asset-library", response_class=FileResponse, include_in_schema=False)
    def asset_library() -> FileResponse:
        return FileResponse(asset_library_html_path())

    @app.get("/team", response_class=FileResponse, include_in_schema=False)
    def team() -> FileResponse:
        return FileResponse(team_html_path())

    # --- embeddable workspace (V7.4) -----------------------------------------
    # The embed shell is served twice — bare, and pinned to one run — but it is the
    # same file: which view to draw is the page's own business, and the *path* run
    # id has to agree with the token anyway (that check lives in the API, because
    # only the API can see the token).
    #
    # The integration id is in the path for one reason: this document request
    # carries no credential at all. The token travels in the URL fragment, and a
    # fragment is never sent to the server, so this is the only request in the
    # whole flow that has nothing else to look the row up by. Without it there is
    # no way to emit ``frame-ancestors``, and the page would either be un-frameable
    # or frameable by anyone.
    def _embed_page(integration_id: str, store: DatabaseStore) -> FileResponse:
        integration = store.find_external_integration(integration_id)
        origins = (
            integration.allowed_origins
            if integration is not None
            and integration.status is ExternalIntegrationStatus.ACTIVE
            else []
        )
        return FileResponse(
            embed_html_path(),
            headers={
                # An unknown or disabled integration degrades to ``'none'``: the
                # shell still renders so the browser shows our own error state
                # instead of an empty frame, but no site may keep it embedded.
                "Content-Security-Policy": csp_frame_ancestors(origins),
                # The directive above is a function of the row, so a cached copy
                # would be a stale security decision. The file is a static shell —
                # re-fetching it costs nothing worth the risk.
                "Cache-Control": "no-store",
            },
        )

    @app.get(
        "/embed/{integration_id}",
        response_class=FileResponse,
        include_in_schema=False,
    )
    def embed_workspace(
        integration_id: str,
        store: Annotated[DatabaseStore, Depends(get_store)],
    ) -> FileResponse:
        return _embed_page(integration_id, store)

    @app.get(
        "/embed/{integration_id}/run/{run_id}",
        response_class=FileResponse,
        include_in_schema=False,
    )
    def embed_run_board(
        integration_id: str,
        run_id: str,
        store: Annotated[DatabaseStore, Depends(get_store)],
    ) -> FileResponse:
        return _embed_page(integration_id, store)

    # The whole permission policy lives in one table; attaching it here means a
    # route cannot silently opt out of enforcement by forgetting a parameter.
    #
    # Order is load-bearing. Identity is resolved first (V7.2): a bearer key has
    # to become the request's actor *before* anything evaluates a permission, or
    # a key-authenticated caller would be judged as the anonymous default actor.
    # The login-session token (V1.1) is resolved immediately after: it is the
    # same kind of bearer credential, ranked below the key (a platform
    # credential outranks a browser login) and above the header (a token the
    # user itself proved outranks whatever a gateway claims). The embed token
    # (V7.4) follows, and before permissions for exactly the same reason: an
    # embed session *is* an actor, so judging it first would refuse every call
    # it is supposed to be allowed to make. The tenant scope guard (V1.1 T97)
    # comes next, still ahead of permissions: a row addressed by the path must
    # belong to the caller's tenant or the request is answered with the
    # handler's own 404 — before a permission check could turn the same id into
    # a 403 that confirms the row exists. Permission enforcement follows, and
    # idempotency sits last so an unauthorised caller is refused before it can
    # claim a key.
    #
    # Maintenance mode (V8.5) slots between permissions and idempotency, and both
    # neighbours are the reason. After permissions, because a caller who would be
    # refused anyway deserves to hear *that* — "you lack project:write" is about
    # their request and is actionable, while "the deployment is in maintenance" is
    # about us and sends them looking for the wrong problem. Before idempotency,
    # because a refusal that changed nothing must not claim a key: doing so would
    # turn the retry after the maintenance window into a replay of a 503.
    #
    # ``responses`` documents the shared failure envelope on every operation. It
    # is not decoration: without it the schema advertises FastAPI's default
    # ``HTTPValidationError`` for 422, while the app actually returns the V7.1
    # envelope — an SDK generated from the spec would parse the wrong shape.
    app.include_router(
        router,
        prefix="/api",
        dependencies=[
            Depends(enforce_api_key_identity),
            Depends(enforce_session_identity),
            Depends(enforce_embed_identity),
            Depends(enforce_tenant_scope),
            Depends(enforce_api_permissions),
            Depends(enforce_maintenance_mode),
            Depends(enforce_idempotency),
        ],
        responses={
            400: {"model": ErrorEnvelope, "description": "请求本身不合法或幂等键不合法"},
            401: {
                "model": ErrorEnvelope,
                "description": "API key、登录会话或嵌入会话令牌不可用（V7.2 / V7.4 / V1.1）",
            },
            403: {"model": ErrorEnvelope, "description": "身份有效但权限不足"},
            404: {"model": ErrorEnvelope, "description": "目标资源不存在"},
            409: {"model": ErrorEnvelope, "description": "与当前状态冲突（含幂等键冲突）"},
            422: {"model": ErrorEnvelope, "description": "请求未通过校验，detail.errors 给出逐项原因"},
            500: {"model": ErrorEnvelope, "description": "服务内部错误"},
            503: {
                "model": ErrorEnvelope,
                "description": "服务暂不可用（维护模式写请求暂停 / 生产密钥未配置）",
            },
        },
    )

    # Settles the idempotency claim once the response is known. This has to be a
    # middleware: a route (and therefore a dependency) cannot see its own
    # finished response.
    app.add_middleware(IdempotencyRecorderMiddleware)
    # Observability sits outermost (added last): its numbers must include the
    # idempotency recorder's own work, and the request id it assigns should be
    # visible to everything below (V8.2).
    app.add_middleware(ObservabilityMiddleware)

    # Prometheus scrape endpoint (V8.2). Deliberately an app-level route, not
    # ``/api``: the consumer is a scraper, not an API caller, so it needs no
    # actor identity and must not enter the OpenAPI document the permission
    # contract is pinned against (same reasoning as the HTML pages above).
    @app.get("/metrics", include_in_schema=False)
    def metrics(store: Annotated[DatabaseStore, Depends(get_store)]) -> Response:
        return Response(content=render_metrics(store), media_type=METRICS_CONTENT_TYPE)

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_payload()})

    @app.exception_handler(IdempotentReplay)
    async def idempotent_replay_handler(
        _request: Request,
        exc: IdempotentReplay,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.body,
            headers={"Idempotency-Replayed": "true"},
        )

    @app.exception_handler(RunGateError)
    async def run_gate_error_handler(_request: Request, exc: RunGateError) -> JSONResponse:
        """Expose risk-gate refusals as structured 4xx payloads on every route.

        The T75/T76 gates can be raised deep inside adapter calls, so mapping them
        centrally keeps the ``{"code", "message", ...details}`` contract identical
        no matter which endpoint the caller used.
        """

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_payload()})

    @app.exception_handler(PermissionDenied)
    async def permission_denied_handler(
        _request: Request,
        exc: PermissionDenied,
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_payload()})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """Normalise the plain ``raise HTTPException(404, "x not found")`` shape.

        Routes still raise the readable form; this is the one place that turns it
        into ``{"code": "not_found", "message": "x not found"}``, so a client
        reads the same field for every failure. A ``detail`` that is already a
        code-bearing dict (the gates) passes through untouched.
        """

        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            payload = detail
        else:
            payload = error_payload(code_for_status(exc.status_code), str(detail))
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": payload},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": error_payload(
                    ErrorCode.VALIDATION_ERROR,
                    "request failed validation",
                    errors=jsonable_encoder(exc.errors()),
                )
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        """Last resort: never leak a stack trace, but keep the envelope shape."""

        logger.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": error_payload(ErrorCode.INTERNAL_ERROR, "internal server error")
            },
        )

    return app


app = create_app()
