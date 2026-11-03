from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    PROVIDER = "provider"
    AUTH = "auth"
    QUOTA = "quota"
    INPUT = "input"
    NETWORK = "network"
    RUNTIME = "runtime"
    QUALITY = "quality"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorTaxonomyDefinition:
    category: ErrorCategory
    title: str
    retryable: bool
    user_message: str
    remediation: str
    examples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "title": self.title,
            "retryable": self.retryable,
            "user_message": self.user_message,
            "remediation": self.remediation,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class ErrorClassification:
    category: ErrorCategory
    code: str
    retryable: bool
    user_message: str
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_category": self.category.value,
            "error_taxonomy_code": self.code,
            "error_retryable": self.retryable,
            "error_user_message": self.user_message,
            "error_remediation": self.remediation,
        }


ERROR_TAXONOMY: tuple[ErrorTaxonomyDefinition, ...] = (
    ErrorTaxonomyDefinition(
        category=ErrorCategory.PROVIDER,
        title="Provider failure",
        retryable=True,
        user_message="外部生成服务返回失败或结果不可用。",
        remediation="先查看 provider 控制台的任务详情；如果任务是临时失败，可直接重试；如果 provider 返回明确参数错误，请按参数提示修正后再重试。",
        examples=("provider_failed", "HTTP 500", "provider task failed"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.AUTH,
        title="Authentication or access",
        retryable=False,
        user_message="认证失败或缺少必要的 API Key。",
        remediation="检查对应 provider 的 API Key 是否已写入 .env，并在修改后重启 api/worker 容器。",
        examples=("DASHSCOPE_API_KEY is required", "HTTP 401", "unauthorized"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.QUOTA,
        title="Quota or rate limit",
        retryable=True,
        user_message="provider 配额、余额或频率限制不足。",
        remediation="等待限流窗口恢复，或在 provider 控制台补充额度/开通权限后重试。",
        examples=("quota exceeded", "rate limit", "HTTP 429"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.INPUT,
        title="Input or validation",
        retryable=False,
        user_message="输入参数、素材引用或当前状态不满足要求。",
        remediation="按错误详情补齐素材、修正参数或重新生成执行方案，再重新派发该步骤。",
        examples=("InvalidParameter", "missing required", "asset does not belong to this run"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.NETWORK,
        title="Network",
        retryable=True,
        user_message="网络请求失败、超时或临时连接异常。",
        remediation="确认容器可以访问 provider 和素材 URL；网络恢复后可直接重试。",
        examples=("URLError", "timeout", "connection reset"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.RUNTIME,
        title="Runtime or local dependency",
        retryable=False,
        user_message="本地运行环境或文件依赖不可用。",
        remediation="检查容器内依赖、文件路径、字体/ffmpeg 等运行时资源，修复后重启相关服务并重试。",
        examples=("FileNotFoundError", "ffmpeg binary is required", "No such file or directory"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.QUALITY,
        title="Quality gate",
        retryable=False,
        user_message="生成结果未通过质量检查。",
        remediation="调整素材、脚本、画面参数或字幕配置后重新生成；不要直接发布未通过质检的结果。",
        examples=("quality gate failed", "black frame", "subtitle overflow"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.CANCELLED,
        title="Cancelled",
        retryable=False,
        user_message="任务已被取消。",
        remediation="如需继续，请恢复 run 或重新排队相关步骤。",
        examples=("cancelled_by_state_action", "cancelled"),
    ),
    ErrorTaxonomyDefinition(
        category=ErrorCategory.UNKNOWN,
        title="Unknown",
        retryable=False,
        user_message="错误尚未归类。",
        remediation="查看原始错误详情和事件日志；如果经常出现，请补充新的错误分类规则。",
        examples=("unexpected exception",),
    ),
)


_DEFINITIONS_BY_CATEGORY = {definition.category: definition for definition in ERROR_TAXONOMY}


def classify_error(
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    provider: str | None = None,
    tool_key: str | None = None,
    status: str | None = None,
) -> ErrorClassification:
    haystack = _normalized_text(error_code, error_message, provider, tool_key, status)
    http_status = _extract_http_status(haystack)

    if _contains(haystack, "cancelled", "canceled", "cancelled_by_state_action"):
        return _classification(ErrorCategory.CANCELLED, "cancelled.task_cancelled")

    if http_status in {401, 403} or _contains(
        haystack,
        "api_key",
        "api key",
        "unauthorized",
        "authentication",
        "forbidden",
        "permission denied",
        "access denied",
        "invalid key",
        "missing key",
    ):
        return _classification(ErrorCategory.AUTH, "auth.missing_or_invalid_credential")

    if http_status == 429 or _contains(
        haystack,
        "quota",
        "rate limit",
        "too many requests",
        "insufficient balance",
        "insufficient credit",
        "billing",
        "limit exceeded",
    ):
        return _classification(ErrorCategory.QUOTA, "quota.exceeded_or_rate_limited")

    if _contains(
        haystack,
        "urlerror",
        "request failed",
        "http request failed",
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "temporary failure",
        "name or service not known",
        "getaddrinfo",
        "ssl",
    ):
        return _classification(ErrorCategory.NETWORK, "network.request_failed")

    if _contains(
        haystack,
        "invalidparameter",
        "invalid parameter",
        "validation",
        "missing required",
        "required before",
        " is required",
        "must be",
        "unsupported",
        "does not belong",
        "not found",
        "cannot ",
        "content_base64 is not valid",
        "asset link is invalid",
    ) or http_status == 400:
        return _classification(ErrorCategory.INPUT, "input.invalid_or_missing")

    if _contains(
        haystack,
        "quality",
        "black frame",
        "static frame",
        "silent audio",
        "subtitle overflow",
        "font missing",
        "resolution mismatch",
        "failed qa",
    ):
        return _classification(ErrorCategory.QUALITY, "quality.gate_failed")

    if _contains(
        haystack,
        "filenotfounderror",
        "no such file or directory",
        "runtimeerror",
        "oserror",
        "notimplementederror",
        "ffmpeg",
        "ffprobe",
        "binary is required",
        "disappeared",
    ):
        return _classification(ErrorCategory.RUNTIME, "runtime.dependency_or_file_missing")

    if (
        http_status is not None
        and http_status >= 500
        or _contains(haystack, "provider_failed", "provider failed", "provider task failed", "dashscope http")
    ):
        return _classification(ErrorCategory.PROVIDER, "provider.remote_failure")

    return _classification(ErrorCategory.UNKNOWN, "unknown.unclassified")


def error_taxonomy_table() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in ERROR_TAXONOMY]


def _classification(category: ErrorCategory, code: str) -> ErrorClassification:
    definition = _DEFINITIONS_BY_CATEGORY[category]
    return ErrorClassification(
        category=category,
        code=code,
        retryable=definition.retryable,
        user_message=definition.user_message,
        remediation=definition.remediation,
    )


def _normalized_text(*parts: str | None) -> str:
    return " ".join(part for part in parts if part).lower()


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _extract_http_status(text: str) -> int | None:
    match = re.search(r"\bhttp\s+(\d{3})\b", text)
    if not match:
        return None
    return int(match.group(1))
