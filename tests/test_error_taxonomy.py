from packages.orchestration_kit.error_taxonomy import (
    ErrorCategory,
    classify_error,
    error_taxonomy_table,
)


def test_error_taxonomy_table_contains_required_categories() -> None:
    categories = {entry["category"] for entry in error_taxonomy_table()}

    assert {
        "provider",
        "auth",
        "quota",
        "input",
        "network",
        "runtime",
        "quality",
        "cancelled",
    }.issubset(categories)


def test_classify_auth_error_from_missing_api_key() -> None:
    result = classify_error(error_message="DASHSCOPE_API_KEY is required for DashScope TTS")

    assert result.category == ErrorCategory.AUTH
    assert result.code == "auth.missing_or_invalid_credential"
    assert result.retryable is False


def test_classify_quota_error_from_http_429() -> None:
    result = classify_error(error_message="HTTP 429: rate limit exceeded")

    assert result.category == ErrorCategory.QUOTA
    assert result.retryable is True


def test_classify_input_error_from_invalid_parameter() -> None:
    result = classify_error(
        error_message='DashScope HTTP 400: {"code":"InvalidParameter.Ratio"}'
    )

    assert result.category == ErrorCategory.INPUT
    assert result.code == "input.invalid_or_missing"


def test_classify_network_error_from_urlerror() -> None:
    result = classify_error(error_code="URLError", error_message="HTTP request failed: timeout")

    assert result.category == ErrorCategory.NETWORK
    assert result.retryable is True


def test_classify_runtime_error_from_missing_file() -> None:
    result = classify_error(error_code="FileNotFoundError", error_message="No such file or directory")

    assert result.category == ErrorCategory.RUNTIME


def test_classify_cancelled_error() -> None:
    result = classify_error(error_code="cancelled_by_state_action", status="cancelled")

    assert result.category == ErrorCategory.CANCELLED
    assert result.retryable is False
