"""Lossy metadata redaction used before any database write."""

import re
from collections.abc import Iterable, Mapping, Sequence
from math import isfinite

from pydantic import JsonValue

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
