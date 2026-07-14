"""ClaimDone AI provider adapters and deterministic workflow core."""

from .config import (
    DEFAULT_EXTRACTION_TIMEOUT_SECONDS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS,
    MAX_PROVIDER_IMAGE_BYTES,
    MAX_TRANSCRIPT_CHARACTERS,
    MAX_TRANSCRIPTION_BYTES,
    ProviderConfig,
    ProviderMode,
)
from .core import NarrativeResult, build_visible_tool_plan, compose_neutral_narrative
from .extraction import (
    EXTRACTION_INSTRUCTIONS,
    RETRY_INSTRUCTIONS,
    ExtractionBlocked,
    ExtractionInput,
    ExtractionProviderFailure,
    ExtractionResult,
    ExtractionRunner,
    ExtractionSuccess,
    OwnedImage,
)
from .failures import (
    AIInputError,
    AIInputErrorCode,
    classify_provider_exception,
    classify_response_error_code,
)
from .ports import OpenAIClientPort, create_openai_client
from .telemetry import ProviderCallStatus, ProviderCallTelemetry
from .transcription import (
    CANONICAL_AUDIO_FILENAME,
    CANONICAL_AUDIO_MEDIA_TYPE,
    MAX_TRANSCRIPTION_SECONDS,
    OpenAITranscriber,
    OwnedAudio,
    TranscriptionFailure,
    TranscriptionResult,
    TranscriptionSuccess,
)

__all__ = [
    "CANONICAL_AUDIO_FILENAME",
    "CANONICAL_AUDIO_MEDIA_TYPE",
    "DEFAULT_EXTRACTION_TIMEOUT_SECONDS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS",
    "EXTRACTION_INSTRUCTIONS",
    "MAX_PROVIDER_IMAGE_BYTES",
    "MAX_TRANSCRIPTION_BYTES",
    "MAX_TRANSCRIPTION_SECONDS",
    "MAX_TRANSCRIPT_CHARACTERS",
    "RETRY_INSTRUCTIONS",
    "AIInputError",
    "AIInputErrorCode",
    "ExtractionBlocked",
    "ExtractionInput",
    "ExtractionProviderFailure",
    "ExtractionResult",
    "ExtractionRunner",
    "ExtractionSuccess",
    "NarrativeResult",
    "OpenAIClientPort",
    "OpenAITranscriber",
    "OwnedAudio",
    "OwnedImage",
    "ProviderCallStatus",
    "ProviderCallTelemetry",
    "ProviderConfig",
    "ProviderMode",
    "TranscriptionFailure",
    "TranscriptionResult",
    "TranscriptionSuccess",
    "build_visible_tool_plan",
    "classify_provider_exception",
    "classify_response_error_code",
    "compose_neutral_narrative",
    "create_openai_client",
]
