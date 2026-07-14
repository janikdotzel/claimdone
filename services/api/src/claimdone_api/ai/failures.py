"""Content-free provider and local-input failure classification."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

import openai

from claimdone_api.contracts import ProviderFailure, ProviderFailureCategory


class AIInputErrorCode(StrEnum):
    INVALID_AUDIO = "invalid_audio"
    AUDIO_TOO_LARGE = "audio_too_large"
    INVALID_IMAGE = "invalid_image"
    IMAGE_TOO_LARGE = "image_too_large"
    INVALID_EVIDENCE_INVENTORY = "invalid_evidence_inventory"
    EVIDENCE_NOT_APPROVED = "evidence_not_approved"
    TRANSCRIPT_NOT_CONFIRMED = "transcript_not_confirmed"


class AIInputError(ValueError):
    """Sanitized fail-before-call error with no uploaded content in its message."""

    def __init__(self, code: AIInputErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def terminal_provider_failure(category: ProviderFailureCategory) -> ProviderFailure:
    """Create the terminal, non-retryable failure used by all V1 provider calls."""

    return ProviderFailure.model_validate(
        {
            "category": category.value,
            "retryable": False,
            "terminal": True,
        }
    )


def classify_provider_exception(error: Exception) -> ProviderFailure:
    """Reduce SDK and transport exceptions to a closed category without their text."""

    codes = _safe_error_codes(error)
    if isinstance(error, openai.ContentFilterFinishReasonError) or _is_content_code(codes):
        category = content_filtered_category()
    elif isinstance(error, openai.APITimeoutError | TimeoutError):
        category = ProviderFailureCategory.TIMEOUT
    elif isinstance(error, openai.AuthenticationError):
        category = ProviderFailureCategory.AUTHENTICATION_FAILED
    elif isinstance(error, openai.PermissionDeniedError):
        category = ProviderFailureCategory.PERMISSION_DENIED
    elif isinstance(error, openai.NotFoundError) or _is_model_not_found_code(codes):
        category = ProviderFailureCategory.MODEL_NOT_FOUND
    elif isinstance(error, openai.RateLimitError):
        category = _rate_limit_category(codes)
    elif _is_quota_code(codes):
        category = ProviderFailureCategory.QUOTA_EXHAUSTED
    elif _is_billing_code(codes):
        category = ProviderFailureCategory.BILLING_LIMIT
    elif isinstance(
        error,
        openai.APIResponseValidationError | openai.LengthFinishReasonError,
    ):
        category = ProviderFailureCategory.INVALID_RESPONSE
    elif isinstance(error, openai.BadRequestError | openai.UnprocessableEntityError):
        category = ProviderFailureCategory.INVALID_REQUEST
    elif isinstance(error, openai.APIConnectionError | openai.InternalServerError):
        category = ProviderFailureCategory.PROVIDER_UNAVAILABLE
    elif isinstance(error, openai.APIStatusError):
        category = _status_category(error.status_code)
    else:
        category = ProviderFailureCategory.PROVIDER_UNAVAILABLE
    return terminal_provider_failure(category)


def classify_response_error_code(code: object) -> ProviderFailure:
    """Classify a failed Responses object without retaining its remote message."""

    normalized = code.lower() if type(code) is str else ""
    codes = frozenset({normalized}) if normalized else frozenset()
    if _is_content_code(codes):
        category = content_filtered_category()
    elif _is_quota_code(codes):
        category = ProviderFailureCategory.QUOTA_EXHAUSTED
    elif _is_billing_code(codes):
        category = ProviderFailureCategory.BILLING_LIMIT
    elif normalized == "rate_limit_exceeded":
        category = ProviderFailureCategory.RATE_LIMITED
    elif _is_model_not_found_code(codes):
        category = ProviderFailureCategory.MODEL_NOT_FOUND
    elif normalized == "server_error":
        category = ProviderFailureCategory.PROVIDER_UNAVAILABLE
    elif normalized:
        category = ProviderFailureCategory.INVALID_REQUEST
    else:
        category = ProviderFailureCategory.INVALID_RESPONSE
    return terminal_provider_failure(category)


def content_filtered_category() -> ProviderFailureCategory:
    """Bridge the current enum and the coordinated additive OBS contract update."""

    try:
        return ProviderFailureCategory("content_filtered")
    except ValueError:
        return ProviderFailureCategory.INVALID_REQUEST


def _status_category(status_code: int) -> ProviderFailureCategory:
    if status_code == 401:
        return ProviderFailureCategory.AUTHENTICATION_FAILED
    if status_code == 403:
        return ProviderFailureCategory.PERMISSION_DENIED
    if status_code == 404:
        return ProviderFailureCategory.MODEL_NOT_FOUND
    if status_code == 429:
        return ProviderFailureCategory.RATE_LIMITED
    if status_code >= 500:
        return ProviderFailureCategory.PROVIDER_UNAVAILABLE
    return ProviderFailureCategory.INVALID_REQUEST


def _rate_limit_category(codes: frozenset[str]) -> ProviderFailureCategory:
    if _is_quota_code(codes):
        return ProviderFailureCategory.QUOTA_EXHAUSTED
    if _is_billing_code(codes):
        return ProviderFailureCategory.BILLING_LIMIT
    return ProviderFailureCategory.RATE_LIMITED


def _safe_error_codes(error: Exception) -> frozenset[str]:
    values: set[str] = set()
    for attribute in ("code", "type"):
        value = getattr(error, attribute, None)
        if type(value) is str:
            values.add(value.lower())
    body = getattr(error, "body", None)
    _collect_mapping_codes(body, values)
    return frozenset(values)


def _collect_mapping_codes(value: object, output: set[str]) -> None:
    if not isinstance(value, Mapping):
        return
    for key in ("code", "type"):
        candidate = value.get(key)
        if type(candidate) is str:
            output.add(candidate.lower())
    nested = value.get("error")
    if isinstance(nested, Mapping):
        _collect_mapping_codes(nested, output)


def _is_quota_code(codes: frozenset[str]) -> bool:
    return any("quota" in code or code == "insufficient_quota" for code in codes)


def _is_billing_code(codes: frozenset[str]) -> bool:
    return any("billing" in code or "credit" in code for code in codes)


def _is_content_code(codes: frozenset[str]) -> bool:
    return any("content_filter" in code or "content_policy" in code for code in codes)


def _is_model_not_found_code(codes: frozenset[str]) -> bool:
    return any(code in {"model_not_found", "invalid_model"} for code in codes)
