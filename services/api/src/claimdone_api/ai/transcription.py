"""Bounded OpenAI transcription adapter with no provider or app retry."""

from __future__ import annotations

import re
import unicodedata
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from time import monotonic
from typing import Literal

from claimdone_api.contracts import (
    ProviderFailure,
    ProviderFailureCategory,
    ProviderModelId,
    WorkflowOperation,
)

from .config import (
    MAX_TRANSCRIPT_CHARACTERS,
    MAX_TRANSCRIPTION_BYTES,
    ProviderConfig,
    ProviderMode,
)
from .failures import AIInputError, AIInputErrorCode, classify_provider_exception
from .ports import OpenAIClientPort
from .telemetry import ProviderCallStatus, ProviderCallTelemetry, elapsed_milliseconds

CANONICAL_AUDIO_MEDIA_TYPE: Literal["audio/wav"] = "audio/wav"
CANONICAL_AUDIO_FILENAME: Literal["claimdone-audio.wav"] = "claimdone-audio.wav"
MAX_TRANSCRIPTION_SECONDS = 60.0
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class OwnedAudio:
    """Local-owned audio bytes; the original upload filename never crosses this seam."""

    content: bytes = field(repr=False)
    media_type: str = CANONICAL_AUDIO_MEDIA_TYPE
    filename: str = CANONICAL_AUDIO_FILENAME

    def __post_init__(self) -> None:
        _validate_owned_audio(self)


@dataclass(frozen=True, slots=True)
class TranscriptionSuccess:
    transcript: str = field(repr=False)
    telemetry: ProviderCallTelemetry

    def __post_init__(self) -> None:
        if not self.transcript or len(self.transcript) > MAX_TRANSCRIPT_CHARACTERS:
            raise ValueError("Successful transcription requires bounded non-empty text")
        if (
            self.telemetry.operation is not WorkflowOperation.TRANSCRIPTION
            or self.telemetry.status is not ProviderCallStatus.SUCCEEDED
        ):
            raise ValueError("Transcription success requires successful transcription telemetry")


@dataclass(frozen=True, slots=True)
class TranscriptionFailure:
    failure: ProviderFailure
    telemetry: ProviderCallTelemetry

    def __post_init__(self) -> None:
        if (
            self.telemetry.operation is not WorkflowOperation.TRANSCRIPTION
            or self.telemetry.status is not ProviderCallStatus.FAILED
        ):
            raise ValueError("Transcription failure requires failed transcription telemetry")
        if not self.failure.terminal or self.failure.retryable:
            raise ValueError("V1 transcription failures are terminal and never retried")


type TranscriptionResult = TranscriptionSuccess | TranscriptionFailure


class OpenAITranscriber:
    """Make exactly one bounded audio call and return content-free failure metadata."""

    def __init__(
        self,
        client: OpenAIClientPort,
        config: ProviderConfig,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if config.mode is not ProviderMode.LIVE:
            raise ValueError("OpenAITranscriber requires live provider mode")
        self._client = client
        self._config = config
        self._clock = clock

    def transcribe(
        self,
        audio: OwnedAudio,
        *,
        call_sequence: int = 1,
    ) -> TranscriptionResult:
        if not isinstance(audio, OwnedAudio):
            raise AIInputError(AIInputErrorCode.INVALID_AUDIO)
        _validate_owned_audio(audio)
        _validate_call_sequence(call_sequence)

        started = self._clock()
        try:
            response = self._client.audio.transcriptions.create(
                model=self._config.transcription_model,
                file=(audio.filename, audio.content, audio.media_type),
                response_format="text",
                timeout=self._config.transcription_timeout_seconds,
            )
        except Exception as error:
            telemetry = self._telemetry(
                call_sequence=call_sequence,
                started=started,
                status=ProviderCallStatus.FAILED,
            )
            return TranscriptionFailure(
                failure=classify_provider_exception(error),
                telemetry=telemetry,
            )

        normalized = _normalize_transcript(response)
        if normalized is None:
            telemetry = self._telemetry(
                call_sequence=call_sequence,
                started=started,
                status=ProviderCallStatus.FAILED,
            )
            return TranscriptionFailure(
                failure=ProviderFailure.model_validate(
                    {
                        "category": ProviderFailureCategory.INVALID_RESPONSE.value,
                        "retryable": False,
                        "terminal": True,
                    }
                ),
                telemetry=telemetry,
            )
        telemetry = self._telemetry(
            call_sequence=call_sequence,
            started=started,
            status=ProviderCallStatus.SUCCEEDED,
        )
        return TranscriptionSuccess(transcript=normalized, telemetry=telemetry)

    def _telemetry(
        self,
        *,
        call_sequence: int,
        started: float,
        status: ProviderCallStatus,
    ) -> ProviderCallTelemetry:
        return ProviderCallTelemetry(
            operation=WorkflowOperation.TRANSCRIPTION,
            model_id=ProviderModelId(self._config.transcription_model),
            provider_mode=self._config.mode,
            call_sequence=call_sequence,
            retry_attempt=0,
            duration_ms=elapsed_milliseconds(started, self._clock()),
            status=status,
        )


def _validate_owned_audio(audio: OwnedAudio) -> None:
    if (
        type(audio.content) is not bytes
        or not audio.content
        or audio.media_type != CANONICAL_AUDIO_MEDIA_TYPE
        or audio.filename != CANONICAL_AUDIO_FILENAME
        or not audio.content.startswith(b"RIFF")
        or audio.content[8:12] != b"WAVE"
    ):
        raise AIInputError(AIInputErrorCode.INVALID_AUDIO)
    if len(audio.content) > MAX_TRANSCRIPTION_BYTES:
        raise AIInputError(AIInputErrorCode.AUDIO_TOO_LARGE)
    try:
        with wave.open(BytesIO(audio.content), "rb") as candidate:
            channels = candidate.getnchannels()
            sample_width = candidate.getsampwidth()
            sample_rate = candidate.getframerate()
            frame_count = candidate.getnframes()
            compression = candidate.getcomptype()
            frames = candidate.readframes(frame_count)
    except (EOFError, wave.Error) as error:
        raise AIInputError(AIInputErrorCode.INVALID_AUDIO) from error
    if (
        compression != "NONE"
        or channels < 1
        or sample_width not in {1, 2, 3, 4}
        or sample_rate < 1
        or frame_count < 1
        or len(frames) != frame_count * channels * sample_width
        or frame_count / sample_rate > MAX_TRANSCRIPTION_SECONDS
    ):
        raise AIInputError(AIInputErrorCode.INVALID_AUDIO)


def _normalize_transcript(value: object) -> str | None:
    if type(value) is not str:
        return None
    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not normalized or len(normalized) > MAX_TRANSCRIPT_CHARACTERS:
        return None
    return normalized


def _validate_call_sequence(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 40:
        raise ValueError("callSequence must be an integer from 1 through 40")
