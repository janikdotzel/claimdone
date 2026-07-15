"""Fail-closed redaction used before persistence or structured logging.

Claim metadata and observability data deliberately use different closed
schemas.  Metadata retains structural summaries only.  Observability retains
only bounded counters and reviewed enum values; opaque identifiers are hashed
so they remain correlatable without becoming a value side channel.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from typing import Final, cast

from pydantic import JsonValue

from claimdone_api.contracts import (
    AllowedTool,
    AuditEventType,
    GateId,
    ProviderFailureCategory,
    ProviderModelId,
    WorkflowOperation,
)

# This is a closed schema, not a pattern. Expanding it requires an explicit privacy review
# because keys themselves are persisted and can otherwise become a PII side channel.
CANONICAL_METADATA_KEYS = frozenset(
    {
        "attachmentNames",
        "claimNarrative",
        "claimantName",
        "counterpartyKnown",
        "imageMetadata",
    }
)
_SUMMARY_PATTERN = re.compile(
    r"^(?:null|boolean|integer|number|non-finite-number|unknown|"
    r"text\(length=\d+\)|object\(keys=\d+\)|array\(items=\d+\))$"
)

OBSERVABILITY_REDACTION_VERSION: Final = 1
MAX_OBSERVABILITY_FIELDS: Final = 64
MAX_OBSERVABILITY_DEPTH: Final = 2
MAX_OBSERVABILITY_KEY_BYTES: Final = 128
MAX_OBSERVABILITY_STRING_BYTES: Final = 4_096
SQLITE_MAX_INTEGER: Final = 9_223_372_036_854_775_807

_IDENTIFIER_FIELDS = {
    "caseId": "caseIdHash",
    "eventId": "eventIdHash",
    "invocationId": "invocationIdHash",
    "pricingSnapshotId": "pricingSnapshotIdHash",
    "requestId": "requestIdHash",
    "sourceAuditEventId": "sourceAuditEventIdHash",
}
_CLOSED_STRING_FIELDS: dict[str, frozenset[str]] = {
    "currency": frozenset({"USD"}),
    "eventType": frozenset(item.value for item in AuditEventType),
    "failureCategory": frozenset(item.value for item in ProviderFailureCategory),
    "gateId": frozenset(item.value for item in GateId),
    "method": frozenset({"DELETE", "GET", "OPTIONS", "POST", "PUT"}),
    "modelId": frozenset(item.value for item in ProviderModelId),
    "operation": frozenset(item.value for item in WorkflowOperation),
    "providerMode": frozenset({"live", "mock"}),
    "route": frozenset(
        {
            "analysis",
            "case_create",
            "case_delete",
            "case_snapshot",
            "intake",
            "provider_request",
            "tool_call",
            "transcript_confirmation",
            "workflow_events",
        }
    ),
    "status": frozenset(
        {
            "blocked",
            "client_error",
            "failed",
            "retry_scheduled",
            "server_error",
            "started",
            "succeeded",
        }
    ),
    "tool": frozenset(item.value for item in AllowedTool),
}
_INTEGER_FIELDS = frozenset(
    {
        "callSequence",
        "costedRequestCount",
        "cursor",
        "durationMs",
        "estimatedCostMicros",
        "inputTokens",
        "outputTokens",
        "providerRequestCount",
        "retryAttempt",
        "retryCount",
        "sequence",
        "statusCode",
        "toolCallCount",
        "totalTokens",
        "usageReportedRequestCount",
    }
)
_BOOLEAN_FIELDS = frozenset({"disconnected"})
_NESTED_FIELD_KEYS: dict[str, frozenset[str]] = {
    "cost": frozenset(
        {
            "currency",
            "estimatedCostMicros",
            "pricingSnapshotId",
        }
    ),
    "usage": frozenset({"inputTokens", "outputTokens", "totalTokens"}),
}

type RedactedObservabilityPayload = dict[str, JsonValue]


def validate_metadata_keys(keys: Iterable[str]) -> None:
    """Reject every caller-defined key outside the reviewed metadata schema."""

    if any(key not in CANONICAL_METADATA_KEYS for key in keys):
        raise ValueError("Metadata keys must belong to the canonical allowlist")


def _summarize(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return f"text(length={len(value)})"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number" if isfinite(value) else "non-finite-number"
    if isinstance(value, Mapping):
        return f"object(keys={len(value)})"
    if isinstance(value, Sequence):
        return f"array(items={len(value)})"


def redact_metadata(metadata: Mapping[str, JsonValue] | None) -> dict[str, str]:
    """Discard all values and retain only non-sensitive structural summaries."""

    if metadata is None:
        return {}
    validate_metadata_keys(metadata)
    return {key: _summarize(value) for key, value in sorted(metadata.items())}


def validate_redacted_metadata(metadata: Mapping[str, str]) -> None:
    """Reject data that has not passed through the structural redactor."""

    validate_metadata_keys(metadata)
    if any(_SUMMARY_PATTERN.fullmatch(summary) is None for summary in metadata.values()):
        raise ValueError("Redacted metadata may contain structural summaries only")


def redact_observability_payload(payload: object) -> RedactedObservabilityPayload:
    """Return a bounded closed projection without rendering untrusted values.

    Unknown keys, free-form strings, binary values, sequences, objects and
    malformed reviewed fields are counted but never copied.  Oversized or
    non-dict roots collapse to one fixed fallback.  The function never calls
    ``str`` or ``repr`` on a rejected value, which keeps hostile exception and
    SDK objects out of error logs as well.
    """

    try:
        if type(payload) is not dict or len(payload) > MAX_OBSERVABILITY_FIELDS:
            return _redacted_observability_fallback(truncated=True)

        sanitized, redacted_count, truncated = _redact_observability_fields(
            payload,
            depth=0,
            seen=set(),
            remaining=[MAX_OBSERVABILITY_FIELDS],
            allowed_keys=None,
        )
        return {
            "schemaVersion": OBSERVABILITY_REDACTION_VERSION,
            "redacted": True,
            "redactedFieldCount": redacted_count,
            "truncated": truncated,
            "fields": sanitized,
        }
    except Exception:
        # Concurrent mutation and unusual values are untrusted input too.  Do
        # not render the exception or the partially inspected payload.
        return _redacted_observability_fallback(truncated=True)


def _redact_observability_fields(
    fields: dict[object, object],
    *,
    depth: int,
    seen: set[int],
    remaining: list[int],
    allowed_keys: frozenset[str] | None,
) -> tuple[dict[str, JsonValue], int, bool]:
    if depth > MAX_OBSERVABILITY_DEPTH or id(fields) in seen:
        return {}, max(1, len(fields)), True
    if len(fields) > remaining[0]:
        return {}, max(1, len(fields)), True
    remaining[0] -= len(fields)
    seen.add(id(fields))
    sanitized: dict[str, JsonValue] = {}
    redacted_count = 0
    truncated = False
    for key, value in fields.items():
        if type(key) is not str:
            redacted_count += 1
            continue
        if _bounded_utf8(key, max_bytes=MAX_OBSERVABILITY_KEY_BYTES) is None:
            redacted_count += 1
            truncated = True
            continue
        if allowed_keys is not None and key not in allowed_keys:
            redacted_count += 1
            truncated = truncated or (
                type(value) is dict and id(value) in seen
            ) or _is_oversized_string(value)
            continue

        identifier_alias = _IDENTIFIER_FIELDS.get(key)
        if identifier_alias is not None:
            hashed = _hash_observability_identifier(value)
            if hashed is None:
                redacted_count += 1
                truncated = truncated or _is_oversized_string(value)
            else:
                sanitized[identifier_alias] = hashed
            continue

        allowed_values = _CLOSED_STRING_FIELDS.get(key)
        if allowed_values is not None:
            if type(value) is str and value in allowed_values:
                sanitized[key] = value
            else:
                redacted_count += 1
                truncated = truncated or _is_oversized_string(value)
            continue

        if key in _INTEGER_FIELDS:
            if _valid_observability_integer(key, value):
                sanitized[key] = cast(int, value)
            else:
                redacted_count += 1
            continue

        if key in _BOOLEAN_FIELDS:
            if type(value) is bool:
                sanitized[key] = value
            else:
                redacted_count += 1
            continue

        nested_keys = _NESTED_FIELD_KEYS.get(key)
        if nested_keys is not None:
            if type(value) is not dict or len(value) > MAX_OBSERVABILITY_FIELDS:
                redacted_count += 1
                truncated = True
                continue
            nested, nested_redacted, nested_truncated = _redact_observability_fields(
                value,
                depth=depth + 1,
                seen=seen,
                remaining=remaining,
                allowed_keys=nested_keys,
            )
            sanitized[key] = nested
            redacted_count += nested_redacted
            truncated = truncated or nested_truncated
            continue

        # The key may itself contain a name, policy number or secret.  Do not
        # echo it in the result; only retain that one field was removed.
        redacted_count += 1
        truncated = truncated or _is_oversized_string(value)
    seen.remove(id(fields))
    return sanitized, redacted_count, truncated


def _hash_observability_identifier(value: object) -> str | None:
    if type(value) is not str:
        return None
    encoded = _bounded_utf8(value, max_bytes=MAX_OBSERVABILITY_STRING_BYTES)
    if not encoded or _is_explicitly_sensitive_text(value):
        return None
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _is_oversized_string(value: object) -> bool:
    if type(value) is str:
        return _bounded_utf8(
            value,
            max_bytes=MAX_OBSERVABILITY_STRING_BYTES,
        ) is None
    if type(value) is bytes:
        return len(value) > MAX_OBSERVABILITY_STRING_BYTES
    if type(value) is bytearray:
        return len(value) > MAX_OBSERVABILITY_STRING_BYTES
    if type(value) is memoryview:
        return len(cast(memoryview, value)) > MAX_OBSERVABILITY_STRING_BYTES
    return False


def _is_explicitly_sensitive_text(value: str) -> bool:
    """Add deny-only defense for values that must never become fingerprints."""

    lowered = value.lower()
    return lowered.startswith(("blob:", "data:", "http://", "https://")) or any(
        marker in lowered
        for marker in (
            "api-key=",
            "api_key=",
            "authorization:",
            "basic ",
            "bearer ",
            "secret=",
            "sk-proj-",
            "token=",
        )
    )


def _bounded_utf8(value: str, *, max_bytes: int) -> bytes | None:
    """Encode only already-bounded text and reject malformed Unicode safely."""

    # A UTF-8 code point occupies at least one byte, so the character bound
    # prevents a multi-megabyte allocation before the exact byte check.
    if len(value) > max_bytes:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return encoded if len(encoded) <= max_bytes else None


def _valid_observability_integer(key: str, value: object) -> bool:
    if type(value) is not int or not 0 <= value <= SQLITE_MAX_INTEGER:
        return False
    if key == "statusCode":
        return 100 <= value <= 599
    if key == "retryAttempt":
        return value in {0, 1}
    if key in {"callSequence", "retryCount"}:
        return value <= 40
    return True


def _redacted_observability_fallback(*, truncated: bool) -> RedactedObservabilityPayload:
    return {
        "schemaVersion": OBSERVABILITY_REDACTION_VERSION,
        "redacted": True,
        "redactedFieldCount": 1,
        "truncated": truncated,
        "fields": {},
    }
