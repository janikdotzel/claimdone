"""Lossy metadata redaction used before any database write."""

import re
from collections.abc import Mapping, Sequence
from math import isfinite

from pydantic import JsonValue

_METADATA_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SUMMARY_PATTERN = re.compile(
    r"^(?:null|boolean|integer|number|non-finite-number|unknown|"
    r"text\(length=\d+\)|object\(keys=\d+\)|array\(items=\d+\))$"
)


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
    invalid_keys = [key for key in metadata if _METADATA_KEY_PATTERN.fullmatch(key) is None]
    if invalid_keys:
        raise ValueError("Metadata keys must be stable schema identifiers")
    return {key: _summarize(value) for key, value in sorted(metadata.items())}


def validate_redacted_metadata(metadata: Mapping[str, str]) -> None:
    """Reject data that has not passed through the structural redactor."""

    if any(_METADATA_KEY_PATTERN.fullmatch(key) is None for key in metadata):
        raise ValueError("Redacted metadata keys must be stable schema identifiers")
    if any(_SUMMARY_PATTERN.fullmatch(summary) is None for summary in metadata.values()):
        raise ValueError("Redacted metadata may contain structural summaries only")
