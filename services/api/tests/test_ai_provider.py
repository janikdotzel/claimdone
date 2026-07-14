"""Fake-only tests for provider configuration, failures, and transcription."""

from __future__ import annotations

import logging
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Literal, cast

import httpx
import openai
import pytest

from claimdone_api.ai import (
    CANONICAL_AUDIO_FILENAME,
    CANONICAL_AUDIO_MEDIA_TYPE,
    MAX_TRANSCRIPT_CHARACTERS,
    AIInputError,
    AIInputErrorCode,
    OpenAITranscriber,
    OwnedAudio,
    ProviderConfig,
    ProviderMode,
    TranscriptionFailure,
    TranscriptionSuccess,
    classify_provider_exception,
    create_openai_client,
)
from claimdone_api.ai.ports import (
    OpenAIClientPort,
    ResponseInputMessage,
    ResponseTextConfig,
)
from claimdone_api.contracts import (
    ProviderFailureCategory,
    ProviderModelId,
    WorkflowOperation,
)


@dataclass(frozen=True, slots=True)
class TranscriptionCall:
    model: str
    file: tuple[str, bytes, str]
    response_format: Literal["text"]
    timeout: float


class FakeTranscriptionsAPI:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[TranscriptionCall] = []

    def create(
        self,
        *,
        model: str,
        file: tuple[str, bytes, str],
        response_format: Literal["text"],
        timeout: float,
    ) -> object:
        self.calls.append(
            TranscriptionCall(
                model=model,
                file=file,
                response_format=response_format,
                timeout=timeout,
            )
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAudioAPI:
    def __init__(self, transcriptions: FakeTranscriptionsAPI) -> None:
        self._transcriptions = transcriptions

    @property
    def transcriptions(self) -> FakeTranscriptionsAPI:
        return self._transcriptions


class UnusedResponsesAPI:
    def create(
        self,
        *,
        model: str,
        instructions: str,
        input: list[ResponseInputMessage],
        text: ResponseTextConfig,
        max_output_tokens: int,
        store: bool,
        timeout: float,
    ) -> object:
        raise AssertionError("Transcription tests must not call Responses")


class FakeTranscriptionClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.transcriptions_api = FakeTranscriptionsAPI(outcomes)
        self._audio = FakeAudioAPI(self.transcriptions_api)
        self._responses = UnusedResponsesAPI()

    @property
    def audio(self) -> FakeAudioAPI:
        return self._audio

    @property
    def responses(self) -> UnusedResponsesAPI:
        return self._responses


class FixedClock:
    def __init__(self, values: list[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def wav_bytes(*, frame_count: int = 80, sample_rate: int = 80) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as candidate:
        candidate.setnchannels(1)
        candidate.setsampwidth(2)
        candidate.setframerate(sample_rate)
        candidate.writeframes(b"\x00\x00" * frame_count)
    return output.getvalue()


def request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.invalid/v1/test")


def response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=request())


def test_provider_config_is_exact_immutable_and_disables_sdk_retries() -> None:
    config = ProviderConfig()

    assert config.mode is ProviderMode.LIVE
    assert config.extraction_model == "gpt-5.6-sol"
    assert config.transcription_model == "gpt-4o-transcribe"
    assert config.sdk_max_retries == 0
    assert config.extraction_retry_limit == 1
    with pytest.raises(AttributeError):
        config.__setattr__("sdk_max_retries", 1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"extraction_model": "gpt-5.6"}, "gpt-5.6-sol"),
        ({"extraction_model": "latest"}, "gpt-5.6-sol"),
        ({"transcription_model": "whisper-1"}, "gpt-4o-transcribe"),
        ({"sdk_max_retries": 1}, "SDK retries"),
        ({"extraction_retry_limit": 2}, "exactly one"),
        ({"extraction_timeout_seconds": 0.5}, "Extraction timeout"),
        ({"transcription_timeout_seconds": 121.0}, "Transcription timeout"),
        ({"max_output_tokens": 16_385}, "maxOutputTokens"),
    ],
)
def test_provider_config_rejects_aliases_unknown_models_and_unbounded_policy(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProviderConfig(**overrides)  # type: ignore[arg-type]


def test_provider_config_rejects_raw_mode_string_and_binds_mock_model() -> None:
    with pytest.raises(ValueError, match="closed"):
        ProviderConfig(mode=cast(ProviderMode, "live"))
    with pytest.raises(ValueError, match="deterministic"):
        ProviderConfig(mode=ProviderMode.MOCK)

    mock = ProviderConfig(
        mode=ProviderMode.MOCK,
        extraction_model=ProviderModelId.DETERMINISTIC_MOCK.value,
        transcription_model=ProviderModelId.DETERMINISTIC_MOCK.value,
    )
    assert mock.mode is ProviderMode.MOCK


def test_openai_factory_injects_key_and_enforces_bounded_no_retry_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake = FakeTranscriptionClient(["unused"])

    def fake_constructor(**kwargs: object) -> FakeTranscriptionClient:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(openai, "OpenAI", fake_constructor)
    client = create_openai_client(
        api_key="test-only-secret",
        config=ProviderConfig(),
        organization="org-test",
        project="proj-test",
    )

    assert client is fake
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 45.0
    assert captured["organization"] == "org-test"
    assert captured["project"] == "proj-test"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request()), ProviderFailureCategory.TIMEOUT),
        (
            openai.APIConnectionError(request=request()),
            ProviderFailureCategory.PROVIDER_UNAVAILABLE,
        ),
        (
            openai.AuthenticationError(
                "remote secret",
                response=response(401),
                body={"code": "invalid_api_key"},
            ),
            ProviderFailureCategory.AUTHENTICATION_FAILED,
        ),
        (
            openai.PermissionDeniedError(
                "remote secret",
                response=response(403),
                body={"code": "permission_denied"},
            ),
            ProviderFailureCategory.PERMISSION_DENIED,
        ),
        (
            openai.NotFoundError(
                "remote secret",
                response=response(404),
                body={"code": "model_not_found"},
            ),
            ProviderFailureCategory.MODEL_NOT_FOUND,
        ),
        (
            openai.RateLimitError(
                "remote secret",
                response=response(429),
                body={"code": "insufficient_quota"},
            ),
            ProviderFailureCategory.QUOTA_EXHAUSTED,
        ),
        (
            openai.RateLimitError(
                "remote secret",
                response=response(429),
                body={"code": "billing_hard_limit_reached"},
            ),
            ProviderFailureCategory.BILLING_LIMIT,
        ),
        (
            openai.RateLimitError(
                "remote secret",
                response=response(429),
                body={"code": "rate_limit_exceeded"},
            ),
            ProviderFailureCategory.RATE_LIMITED,
        ),
        (
            openai.APIResponseValidationError(
                response(200),
                body={"message": "remote secret"},
            ),
            ProviderFailureCategory.INVALID_RESPONSE,
        ),
        (RuntimeError("remote secret"), ProviderFailureCategory.PROVIDER_UNAVAILABLE),
    ],
)
def test_provider_exception_classification_is_closed_terminal_and_redacted(
    error: Exception,
    expected: ProviderFailureCategory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    failure = classify_provider_exception(error)

    assert failure.category is expected
    assert failure.terminal is True
    assert failure.retryable is False
    assert "remote secret" not in repr(failure)
    assert "remote secret" not in caplog.text


def test_content_policy_error_uses_exact_closed_category() -> None:
    error = openai.BadRequestError(
        "remote image policy detail",
        response=response(400),
        body={"code": "image_content_policy_violation"},
    )

    failure = classify_provider_exception(error)

    assert failure.category is ProviderFailureCategory.CONTENT_FILTERED
    assert failure.terminal and not failure.retryable
    assert "remote image policy detail" not in repr(failure)


def test_transcriber_normalizes_text_and_emits_only_sanitized_telemetry() -> None:
    audio_bytes = wav_bytes()
    client = FakeTranscriptionClient(["  Cafe\u0301\n rear   impact  "])
    typed_client: OpenAIClientPort = client
    transcriber = OpenAITranscriber(
        typed_client,
        ProviderConfig(),
        clock=FixedClock([10.0, 10.025]),
    )

    result = transcriber.transcribe(OwnedAudio(content=audio_bytes), call_sequence=4)

    assert isinstance(result, TranscriptionSuccess)
    assert result.transcript == "Café rear impact"
    assert len(client.transcriptions_api.calls) == 1
    call = client.transcriptions_api.calls[0]
    assert call.model == "gpt-4o-transcribe"
    assert call.file == (
        CANONICAL_AUDIO_FILENAME,
        audio_bytes,
        CANONICAL_AUDIO_MEDIA_TYPE,
    )
    assert call.response_format == "text"
    assert call.timeout == 30.0
    assert result.telemetry.duration_ms == 25
    assert result.telemetry.operation is WorkflowOperation.TRANSCRIPTION
    event = result.telemetry.to_success_event()
    assert event.model_id is ProviderModelId.TRANSCRIBE
    assert event.usage is None
    assert result.transcript not in repr(result)


@pytest.mark.parametrize("response_value", ["", "  \n  ", object(), "x" * 4_001])
def test_transcriber_rejects_empty_non_text_or_oversized_response_without_retry(
    response_value: object,
) -> None:
    client = FakeTranscriptionClient([response_value])
    result = OpenAITranscriber(
        client,
        ProviderConfig(),
        clock=FixedClock([1.0, 1.001]),
    ).transcribe(OwnedAudio(content=wav_bytes()))

    assert isinstance(result, TranscriptionFailure)
    assert result.failure.category is ProviderFailureCategory.INVALID_RESPONSE
    assert len(client.transcriptions_api.calls) == 1
    assert (
        result.telemetry.to_failure_event(result.failure).operation
        is WorkflowOperation.TRANSCRIPTION
    )
    failure_event = result.telemetry.to_failure_event(result.failure)
    assert failure_event.model_id is ProviderModelId.TRANSCRIBE
    assert failure_event.provider_mode == "live"
    assert failure_event.call_sequence == 1
    assert failure_event.retry_attempt == 0
    assert failure_event.duration_ms == 1


def test_transcriber_timeout_is_terminal_and_never_retried() -> None:
    client = FakeTranscriptionClient([openai.APITimeoutError(request())])
    result = OpenAITranscriber(
        client,
        ProviderConfig(),
        clock=FixedClock([2.0, 2.5]),
    ).transcribe(OwnedAudio(content=wav_bytes()))

    assert isinstance(result, TranscriptionFailure)
    assert result.failure.category is ProviderFailureCategory.TIMEOUT
    assert result.failure.terminal and not result.failure.retryable
    assert result.telemetry.duration_ms == 500
    assert len(client.transcriptions_api.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"content": b"not wav"}, AIInputErrorCode.INVALID_AUDIO),
        (
            {"content": wav_bytes(), "media_type": "audio/mpeg"},
            AIInputErrorCode.INVALID_AUDIO,
        ),
        (
            {"content": wav_bytes(), "filename": "user-upload.wav"},
            AIInputErrorCode.INVALID_AUDIO,
        ),
        (
            {"content": wav_bytes(frame_count=61, sample_rate=1)},
            AIInputErrorCode.INVALID_AUDIO,
        ),
    ],
)
def test_owned_audio_rejects_noncanonical_or_unbounded_content_before_call(
    kwargs: dict[str, object],
    expected: AIInputErrorCode,
) -> None:
    client = FakeTranscriptionClient(["must not be called"])

    with pytest.raises(AIInputError) as captured:
        audio = OwnedAudio(**kwargs)  # type: ignore[arg-type]
        OpenAITranscriber(client, ProviderConfig()).transcribe(audio)

    assert captured.value.code is expected
    assert client.transcriptions_api.calls == []


def test_owned_audio_rechecks_byte_limit_without_allocating_large_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claimdone_api.ai import transcription

    content = wav_bytes()
    monkeypatch.setattr(transcription, "MAX_TRANSCRIPTION_BYTES", len(content) - 1)

    with pytest.raises(AIInputError) as captured:
        OwnedAudio(content=content)

    assert captured.value.code is AIInputErrorCode.AUDIO_TOO_LARGE


def test_transcription_result_and_logs_never_render_audio_transcript_or_remote_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    audio_marker = b"private-audio-marker"
    client = FakeTranscriptionClient([RuntimeError("remote-request-id-secret")])
    valid_audio = wav_bytes() + audio_marker

    # Appending bytes after a complete WAV remains valid and demonstrates repr redaction.
    result = OpenAITranscriber(
        client,
        ProviderConfig(),
        clock=FixedClock([1.0, 1.1]),
    ).transcribe(OwnedAudio(content=valid_audio))

    assert isinstance(result, TranscriptionFailure)
    assert "private-audio-marker" not in repr(result)
    assert "remote-request-id-secret" not in repr(result)
    assert "remote-request-id-secret" not in caplog.text
    assert MAX_TRANSCRIPT_CHARACTERS == 4_000
