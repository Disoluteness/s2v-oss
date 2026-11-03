"""The public error contract (V7.1).

Every failure the API can produce is described by one code, and every failure is
rendered in one envelope::

    {"detail": {"code": "...", "message": "...", ...details}}

Before V7.1 only the three structured gates (``RunGateError``, ``ReviewGateError``,
``PermissionDenied``) spoke that shape. Everything else — a plain 404 from a
lookup, a body that fails validation, an unhandled crash — came back in whatever
shape FastAPI happened to produce, so a client could not branch on a single
field. This module is the fix: one catalog of codes, one ``ApiError`` a route can
raise, and one place the global handlers read to normalise the rest.

The three existing gate exceptions are deliberately left alone. They already
expose ``code`` / ``message`` / ``details`` / ``to_payload``; their payload is a
superset of what ``ApiError`` produces, so the envelope is shared even though the
classes are not.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    REQUEST = "request"
    IDENTITY = "identity"
    IDEMPOTENCY = "idempotency"
    GATE = "gate"
    SERVER = "server"


class ErrorCode(StrEnum):
    """Stable, machine-readable failure codes.

    Clients switch on these; ``message`` is for humans and may change. Adding a
    code is a non-breaking change; renaming one is a breaking change.
    """

    # --- request shape -----------------------------------------------------
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    # --- identity ----------------------------------------------------------
    UNAUTHORIZED = "unauthorized"
    PERMISSION_DENIED = "permission_denied"
    AUTH_INVALID_CREDENTIALS = "auth_invalid_credentials"
    SESSION_INVALID = "session_invalid"
    API_KEY_INVALID = "api_key_invalid"
    EMBED_SESSION_INVALID = "embed_session_invalid"
    PROVIDER_CALLBACK_INVALID = "provider_callback_invalid"
    RATE_LIMITED = "rate_limited"
    # --- idempotency -------------------------------------------------------
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
    # --- domain gates (carry extra fields next to code/message) -------------
    INTENT_DOWNGRADE_NOT_ACKNOWLEDGED = "intent_downgrade_not_acknowledged"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    COST_RESERVATION_EXCEEDS_BUDGET = "cost_reservation_exceeds_budget"
    TEAM_QUOTA_EXCEEDED = "team_quota_exceeded"
    PAID_PROVIDER_CONFIRMATION_REQUIRED = "paid_provider_confirmation_required"
    REVIEW_GATE = "review_gate"
    # --- server ------------------------------------------------------------
    INTERNAL_ERROR = "internal_error"
    BAD_GATEWAY = "bad_gateway"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INSECURE_SECRET_CONFIGURATION = "insecure_secret_configuration"
    MAINTENANCE_MODE = "maintenance_mode"


#: Human-readable summary per code, published by ``GET /api/error-codes``.
_DESCRIPTIONS: dict[ErrorCode, str] = {
    ErrorCode.BAD_REQUEST: "请求本身不合法（参数缺失、取值越界等）",
    ErrorCode.VALIDATION_ERROR: "请求体或参数未通过 schema 校验，errors 字段给出逐项原因",
    ErrorCode.NOT_FOUND: "目标资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "该路径不支持此 HTTP 方法",
    ErrorCode.CONFLICT: "与当前资源状态冲突",
    ErrorCode.PAYLOAD_TOO_LARGE: "请求体超过大小上限",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "不支持的请求内容类型",
    ErrorCode.UNAUTHORIZED: "缺少可用的请求身份",
    ErrorCode.PERMISSION_DENIED: "身份有效但权限不足",
    ErrorCode.AUTH_INVALID_CREDENTIALS: "登录凭据错误（用户不存在、密码不符或账号不可用）",
    ErrorCode.SESSION_INVALID: "登录会话不存在、已吊销或已过期",
    ErrorCode.API_KEY_INVALID: "API key 不存在、已吊销或已过期（V7.2）",
    ErrorCode.EMBED_SESSION_INVALID: "嵌入会话令牌不存在、被篡改或已过期（V7.4）",
    ErrorCode.PROVIDER_CALLBACK_INVALID: "供应商回调签名缺失、不匹配或超出时间容差（V8.4）",
    ErrorCode.RATE_LIMITED: "触发限流",
    ErrorCode.IDEMPOTENCY_CONFLICT: "同一 Idempotency-Key 被用于不同的请求内容",
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: "同一 Idempotency-Key 的原始请求仍在处理中",
    ErrorCode.INTENT_DOWNGRADE_NOT_ACKNOWLEDGED: "声明质量模式发生降级且尚未确认（T75 闸门）",
    ErrorCode.COST_BUDGET_EXCEEDED: "本次调用的预计成本超出预算（T76 闸门）",
    ErrorCode.COST_RESERVATION_EXCEEDS_BUDGET: "整轮成本预留超出预算（T76 闸门）",
    ErrorCode.TEAM_QUOTA_EXCEEDED: "本次调用将使团队在该周期内超出配额（V8.3 闸门）",
    ErrorCode.PAID_PROVIDER_CONFIRMATION_REQUIRED: "首次调用付费 provider 需要人工确认（T76 闸门）",
    ErrorCode.REVIEW_GATE: "审核门禁规则阻止了该操作",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.BAD_GATEWAY: "上游 provider 或外部依赖返回异常",
    ErrorCode.SERVICE_UNAVAILABLE: "服务暂不可用",
    ErrorCode.INSECURE_SECRET_CONFIGURATION: (
        "服务端密钥仍使用开发默认值，拒绝签发签名链接（V8.4）"
    ),
    ErrorCode.MAINTENANCE_MODE: (
        "部署处于维护模式，写请求被暂停；读请求仍然可用（V8.5）"
    ),
}

_CATEGORIES: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.BAD_REQUEST: ErrorCategory.REQUEST,
    ErrorCode.VALIDATION_ERROR: ErrorCategory.REQUEST,
    ErrorCode.NOT_FOUND: ErrorCategory.REQUEST,
    ErrorCode.METHOD_NOT_ALLOWED: ErrorCategory.REQUEST,
    ErrorCode.CONFLICT: ErrorCategory.REQUEST,
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorCategory.REQUEST,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: ErrorCategory.REQUEST,
    ErrorCode.UNAUTHORIZED: ErrorCategory.IDENTITY,
    ErrorCode.PERMISSION_DENIED: ErrorCategory.IDENTITY,
    ErrorCode.AUTH_INVALID_CREDENTIALS: ErrorCategory.IDENTITY,
    ErrorCode.SESSION_INVALID: ErrorCategory.IDENTITY,
    ErrorCode.API_KEY_INVALID: ErrorCategory.IDENTITY,
    ErrorCode.EMBED_SESSION_INVALID: ErrorCategory.IDENTITY,
    ErrorCode.PROVIDER_CALLBACK_INVALID: ErrorCategory.IDENTITY,
    ErrorCode.RATE_LIMITED: ErrorCategory.IDENTITY,
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorCategory.IDEMPOTENCY,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: ErrorCategory.IDEMPOTENCY,
    ErrorCode.INTENT_DOWNGRADE_NOT_ACKNOWLEDGED: ErrorCategory.GATE,
    ErrorCode.COST_BUDGET_EXCEEDED: ErrorCategory.GATE,
    ErrorCode.COST_RESERVATION_EXCEEDS_BUDGET: ErrorCategory.GATE,
    ErrorCode.TEAM_QUOTA_EXCEEDED: ErrorCategory.GATE,
    ErrorCode.PAID_PROVIDER_CONFIRMATION_REQUIRED: ErrorCategory.GATE,
    ErrorCode.REVIEW_GATE: ErrorCategory.GATE,
    ErrorCode.INTERNAL_ERROR: ErrorCategory.SERVER,
    ErrorCode.BAD_GATEWAY: ErrorCategory.SERVER,
    ErrorCode.SERVICE_UNAVAILABLE: ErrorCategory.SERVER,
    ErrorCode.INSECURE_SECRET_CONFIGURATION: ErrorCategory.SERVER,
    ErrorCode.MAINTENANCE_MODE: ErrorCategory.SERVER,
}

_STATUS_CODES: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.SESSION_INVALID: 401,
    ErrorCode.API_KEY_INVALID: 401,
    ErrorCode.EMBED_SESSION_INVALID: 401,
    ErrorCode.PROVIDER_CALLBACK_INVALID: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
    ErrorCode.INTENT_DOWNGRADE_NOT_ACKNOWLEDGED: 400,
    ErrorCode.COST_BUDGET_EXCEEDED: 400,
    ErrorCode.COST_RESERVATION_EXCEEDS_BUDGET: 400,
    ErrorCode.TEAM_QUOTA_EXCEEDED: 400,
    ErrorCode.PAID_PROVIDER_CONFIRMATION_REQUIRED: 400,
    ErrorCode.REVIEW_GATE: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.BAD_GATEWAY: 502,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INSECURE_SECRET_CONFIGURATION: 503,
    ErrorCode.MAINTENANCE_MODE: 503,
}

#: Retrying the identical request can succeed for these; the rest are terminal
#: until the request itself changes. An SDK uses this to decide on auto-retry.
#:
#: ``MAINTENANCE_MODE`` belongs here even though it is a 503: the write is
#: refused *for now*, and a maintenance window ends — which is the whole reason
#: the switch exists rather than the operator simply stopping the process. A
#: client that gave up permanently on the first refusal would have to be
#: restarted by hand when the window closed.
_RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.BAD_GATEWAY,
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.MAINTENANCE_MODE,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
    }
)

#: What a bare status code means when a handler hands us one without a code.
_STATUS_DEFAULTS: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.BAD_GATEWAY,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def code_for_status(status_code: int) -> str:
    """The code a response gets when nothing more specific was supplied."""
    mapped = _STATUS_DEFAULTS.get(status_code)
    if mapped is not None:
        return mapped.value
    if 400 <= status_code < 500:
        return ErrorCode.BAD_REQUEST.value
    if status_code >= 500:
        return ErrorCode.INTERNAL_ERROR.value
    return ErrorCode.BAD_REQUEST.value


def default_status(code: ErrorCode | str) -> int:
    try:
        resolved = ErrorCode(code)
    except ValueError:
        return 400
    return _STATUS_CODES.get(resolved, 400)


def error_payload(code: ErrorCode | str, message: str, **details: Any) -> dict[str, Any]:
    """Build the ``detail`` object the whole API returns on failure."""
    payload: dict[str, Any] = {"code": str(code), "message": message}
    payload.update({key: value for key, value in details.items() if value is not None})
    return payload


class ApiError(Exception):
    """A structured API failure a route or dependency can raise directly."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.message = message
        self.status_code = status_code or default_status(code)
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def error_catalog_table() -> list[dict[str, Any]]:
    """The published catalog, so a client can discover codes without reading code."""
    return [
        {
            "code": code.value,
            "status_code": _STATUS_CODES.get(code, 400),
            "category": _CATEGORIES.get(code, ErrorCategory.REQUEST).value,
            "retryable": code in _RETRYABLE,
            "description": _DESCRIPTIONS.get(code, ""),
        }
        for code in ErrorCode
    ]
