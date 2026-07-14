"""Closed, immutable configuration for ClaimDone provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from claimdone_api.contracts import ProviderModelId

DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 45.0
DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_TOKENS = 8_000
MAX_PROVIDER_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 4_000


class ProviderMode(StrEnum):
    """Closed provider execution modes used by sanitized telemetry."""

    MOCK = "mock"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Exact model, timeout, and retry policy for the V1 provider surface."""

    mode: ProviderMode = ProviderMode.LIVE
    extraction_model: str = ProviderModelId.SOL.value
    transcription_model: str = ProviderModelId.TRANSCRIBE.value
    extraction_timeout_seconds: float = DEFAULT_EXTRACTION_TIMEOUT_SECONDS
    transcription_timeout_seconds: float = DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    sdk_max_retries: int = 0
    extraction_retry_limit: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ProviderMode):
            raise ValueError("Provider mode must be the closed mock or live enum")
        if self.mode is ProviderMode.LIVE:
            expected_extraction = ProviderModelId.SOL.value
            expected_transcription = ProviderModelId.TRANSCRIBE.value
        else:
            expected_extraction = ProviderModelId.DETERMINISTIC_MOCK.value
            expected_transcription = ProviderModelId.DETERMINISTIC_MOCK.value
        if type(self.extraction_model) is not str or self.extraction_model != expected_extraction:
            raise ValueError(f"Extraction model must be exactly {expected_extraction}")
        if (
            type(self.transcription_model) is not str
            or self.transcription_model != expected_transcription
        ):
            raise ValueError(f"Transcription model must be exactly {expected_transcription}")
        _require_bounded_timeout(
            self.extraction_timeout_seconds,
            label="Extraction timeout",
        )
        _require_bounded_timeout(
            self.transcription_timeout_seconds,
            label="Transcription timeout",
        )
        if (
            type(self.max_output_tokens) is not int
            or not 512 <= self.max_output_tokens <= 16_384
        ):
            raise ValueError("maxOutputTokens must be an integer from 512 through 16384")
        if type(self.sdk_max_retries) is not int or self.sdk_max_retries != 0:
            raise ValueError("OpenAI SDK retries must remain disabled")
        if (
            type(self.extraction_retry_limit) is not int
            or self.extraction_retry_limit != 1
        ):
            raise ValueError("The app owns exactly one extraction retry")


def _require_bounded_timeout(value: object, *, label: str) -> None:
    numeric = cast(int | float, value) if type(value) in {int, float} else None
    if numeric is None or not 1.0 <= float(numeric) <= 120.0:
        raise ValueError(f"{label} must be a finite number from 1 through 120 seconds")
