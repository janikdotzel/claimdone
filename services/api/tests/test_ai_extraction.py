"""Fake Responses tests for multimodal extraction and G2-owned retry."""

from __future__ import annotations

import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Literal

import httpx
import openai
import pytest

from claimdone_api.ai import (
    EXTRACTION_INSTRUCTIONS,
    RETRY_INSTRUCTIONS,
    AIInputError,
    AIInputErrorCode,
    ExtractionBlocked,
    ExtractionInput,
    ExtractionProviderFailure,
    ExtractionRunner,
    ExtractionSuccess,
    OwnedImage,
    ProviderConfig,
)
from claimdone_api.ai.ports import ResponseInputMessage, ResponseTextConfig
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    EvidenceItem,
    EvidenceKind,
    GateReasonCode,
    ProviderFailureCategory,
    ProviderModelId,
    WorkflowOperation,
)
from claimdone_api.gates import ModelExtraction

DECIDED_AT = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ResponseCall:
    model: str
    instructions: str
    input: list[ResponseInputMessage]
    text: ResponseTextConfig
    max_output_tokens: int
    store: bool
    timeout: float


class FakeResponsesAPI:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[ResponseCall] = []
        self.parse_calls = 0

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
        self.calls.append(
            ResponseCall(
                model=model,
                instructions=instructions,
                input=input,
                text=text,
                max_output_tokens=max_output_tokens,
                store=store,
                timeout=timeout,
            )
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def parse(
        self,
        *,
        model: str,
        instructions: str,
        input: list[ResponseInputMessage],
        text_format: type[ModelExtraction],
        max_output_tokens: int,
        store: bool,
        timeout: float,
    ) -> object:
        """Simulate SDK eager parsing; production must never enter this path."""

        self.parse_calls += 1
        raise ValueError("SDK eager Structured Output parse rejected raw JSON before G2")


class UnusedTranscriptionsAPI:
    def create(
        self,
        *,
        model: str,
        file: tuple[str, bytes, str],
        response_format: Literal["text"],
        timeout: float,
    ) -> object:
        raise AssertionError("Extraction tests must not call transcription")


class FakeAudioAPI:
    def __init__(self) -> None:
        self._transcriptions = UnusedTranscriptionsAPI()

    @property
    def transcriptions(self) -> UnusedTranscriptionsAPI:
        return self._transcriptions


class FakeExtractionClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.responses_api = FakeResponsesAPI(outcomes)
        self._audio = FakeAudioAPI()

    @property
    def responses(self) -> FakeResponsesAPI:
        return self.responses_api

    @property
    def audio(self) -> FakeAudioAPI:
        return self._audio


@dataclass(frozen=True, slots=True)
class FakeResponseError:
    code: str
    message: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status: str | None
    output_text: object = field(default=None, repr=False)
    output: list[object] = field(default_factory=list, repr=False)
    incomplete_details: object | None = None
    error: object | None = field(default=None, repr=False)
    usage: object | None = None
    request_id: str = field(default="req-private-marker", repr=False)


class FixedClock:
    def __init__(self, values: list[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def image_content(index: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes([index]) * 16


def image_evidence(
    index: int,
    content: bytes,
    *,
    approved: bool = True,
    media_type: str = "image/png",
) -> EvidenceItem:
    return EvidenceItem.model_validate(
        {
            "evidenceId": f"image-{index}",
            "kind": "image",
            "localRef": f"owned-image-{index}.png",
            "mediaType": media_type,
            "sha256": sha256(content).hexdigest(),
            "text": None,
            "modelCopyApproved": approved,
        }
    )


def statement_evidence(
    *,
    kind: EvidenceKind = EvidenceKind.USER_STATEMENT,
    confirmed: bool | None = None,
    approved: bool = True,
    text: str = "Rear impact near the demo park.",
) -> EvidenceItem:
    data: dict[str, object] = {
        "evidenceId": "statement-1",
        "kind": kind.value,
        "localRef": "owned-statement-1.txt",
        "mediaType": "text/plain",
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "modelCopyApproved": approved,
    }
    if kind is EvidenceKind.TRANSCRIPT:
        data["transcriptConfirmed"] = confirmed
    return EvidenceItem.model_validate(data)


def extraction_input(
    *,
    kind: EvidenceKind = EvidenceKind.USER_STATEMENT,
    confirmed: bool | None = None,
) -> ExtractionInput:
    images = tuple(
        OwnedImage(
            evidence=image_evidence(index, content),
            content=content,
        )
        for index, content in (
            (1, image_content(1)),
            (2, image_content(2)),
            (3, image_content(3)),
        )
    )
    return ExtractionInput(
        images=images,
        statement=statement_evidence(kind=kind, confirmed=confirmed),
    )


def model_extraction(request: ExtractionInput) -> ModelExtraction:
    provenance = (
        *(
            {
                "provenanceId": f"prov-image-{index}",
                "evidenceId": image.evidence.evidence_id,
                "locator": f"approved image {index}",
                "userConfirmed": False,
            }
            for index, image in enumerate(request.images, start=1)
        ),
        {
            "provenanceId": "prov-statement",
            "evidenceId": request.statement.evidence_id,
            "locator": "approved statement",
            "userConfirmed": request.statement.kind is EvidenceKind.TRANSCRIPT,
        },
    )
    return ModelExtraction.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "evidence": request.approved_evidence,
            "provenance": provenance,
            "facts": (
                {
                    "factId": "fact-counterparty",
                    "field": "counterparty_known",
                    "value": "unknown",
                    "status": "user_stated",
                    "sourceRefs": ("prov-statement",),
                    "confidence": None,
                },
            ),
            "claim": {
                "incidentDate": None,
                "incidentTime": None,
                "location": None,
                "claimantName": None,
                "policyReference": None,
                "vehicleRegistration": None,
                "counterpartyKnown": "unknown",
                "narrative": None,
                "attachments": tuple(image.evidence.local_ref for image in request.images),
                "missingRequiredFields": (
                    "incident_date",
                    "incident_time",
                    "location",
                    "claimant_name",
                    "policy_reference",
                    "vehicle_registration",
                    "narrative",
                ),
                "fieldProvenance": (
                    {
                        "field": "counterparty_known",
                        "sourceRefs": ("prov-statement",),
                    },
                    {
                        "field": "attachments",
                        "sourceRefs": ("prov-image-1", "prov-image-2", "prov-image-3"),
                    },
                ),
            },
        }
    )


def completed_response(extraction: ModelExtraction) -> FakeResponse:
    return FakeResponse(
        status="completed",
        output_text=extraction.model_dump_json(by_alias=True),
        output=[
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text"}],
            }
        ],
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )


def runner(client: FakeExtractionClient, clock_values: list[float]) -> ExtractionRunner:
    return ExtractionRunner(
        client,
        ProviderConfig(),
        clock=FixedClock(clock_values),
        decision_clock=lambda: DECIDED_AT,
    )


def test_extraction_uses_exact_multimodal_structured_call_and_passes_g2() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    client = FakeExtractionClient([completed_response(expected)])

    result = runner(client, [10.0, 10.02]).run(request, call_sequence_start=7)

    assert isinstance(result, ExtractionSuccess)
    assert CONTRACT_VERSION == "3.0.0"
    assert result.extraction == expected
    assert result.g2_run.final_result is not None
    assert result.g2_run.final_result.decision.passed
    assert len(client.responses_api.calls) == 1
    call = client.responses_api.calls[0]
    assert call.model == "gpt-5.6-sol"
    assert call.text["format"]["type"] == "json_schema"
    assert call.text["format"]["strict"] is True
    assert call.text["format"]["name"] == "ClaimDoneModelExtraction"
    assert call.store is False
    assert call.timeout == 45.0
    assert call.max_output_tokens == 8_000
    statement_text = request.statement.text
    assert statement_text is not None
    assert statement_text not in call.instructions
    assert "observed" in call.instructions
    assert "user_stated" in call.instructions
    assert "unknown" in call.instructions
    assert "not_supported" in call.instructions
    for forbidden_image_inference in (
        "identity",
        "policy number",
        "address or location",
        "registration",
        "VIN",
        "liability",
        "cost",
    ):
        assert forbidden_image_inference in call.instructions
    parts = call.input[0]["content"]
    assert parts[0]["type"] == "input_text"
    assert statement_text in parts[0]["text"]
    image_parts = [part for part in parts if part["type"] == "input_image"]
    assert len(image_parts) == 3
    assert all(part["image_url"].startswith("data:image/png;base64,") for part in image_parts)
    telemetry = result.telemetry[0]
    assert telemetry.call_sequence == 7
    assert telemetry.retry_attempt == 0
    assert telemetry.duration_ms == 20
    assert telemetry.usage is not None
    event = telemetry.to_success_event()
    assert event.operation is WorkflowOperation.EXTRACTION
    assert event.model_id is ProviderModelId.SOL
    assert event.usage is not None and event.usage.total_tokens == 150
    assert statement_text not in repr(result)
    assert "req-private-marker" not in repr(result)


def test_confirmed_transcript_is_allowed_and_copied_exactly() -> None:
    request = extraction_input(kind=EvidenceKind.TRANSCRIPT, confirmed=True)
    expected = model_extraction(request)
    client = FakeExtractionClient([completed_response(expected)])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    assert result.extraction.evidence[-1].transcript_confirmed is True


@pytest.mark.parametrize("confirmed", [False, None])
def test_unconfirmed_transcript_fails_before_any_client_call(
    confirmed: bool | None,
) -> None:
    client = FakeExtractionClient([RuntimeError("must not be called")])
    valid = extraction_input(kind=EvidenceKind.TRANSCRIPT, confirmed=True)
    unconfirmed = statement_evidence(
        kind=EvidenceKind.TRANSCRIPT,
        confirmed=confirmed,
    )
    object.__setattr__(valid, "statement", unconfirmed)

    with pytest.raises(AIInputError) as captured:
        runner(client, []).run(valid)

    assert captured.value.code is AIInputErrorCode.TRANSCRIPT_NOT_CONFIRMED
    assert client.responses_api.calls == []


def test_wrong_inventory_and_unapproved_evidence_fail_before_client_call() -> None:
    request = extraction_input()
    client = FakeExtractionClient([RuntimeError("must not be called")])

    with pytest.raises(AIInputError) as wrong_count:
        ExtractionInput(images=request.images[:2], statement=request.statement)
    assert wrong_count.value.code is AIInputErrorCode.INVALID_EVIDENCE_INVENTORY

    content = image_content(1)
    with pytest.raises(AIInputError) as unapproved:
        OwnedImage(
            evidence=image_evidence(1, content, approved=False),
            content=content,
        )
    assert unapproved.value.code is AIInputErrorCode.EVIDENCE_NOT_APPROVED
    assert client.responses_api.calls == []


def test_image_provider_byte_limit_is_rechecked_before_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claimdone_api.ai import extraction

    content = image_content(1)
    monkeypatch.setattr(extraction, "MAX_PROVIDER_IMAGE_BYTES", len(content) - 1)

    with pytest.raises(AIInputError) as captured:
        OwnedImage(evidence=image_evidence(1, content), content=content)

    assert captured.value.code is AIInputErrorCode.IMAGE_TOO_LARGE


def test_raw_audio_disguised_as_image_and_nonstatement_text_fail_before_call() -> None:
    output = BytesIO()
    with wave.open(output, "wb") as candidate:
        candidate.setnchannels(1)
        candidate.setsampwidth(2)
        candidate.setframerate(8)
        candidate.writeframes(b"\x00\x00" * 8)
    raw_audio = output.getvalue()

    with pytest.raises(AIInputError) as raw_audio_error:
        OwnedImage(
            evidence=image_evidence(1, raw_audio, media_type="image/jpeg"),
            content=raw_audio,
        )
    assert raw_audio_error.value.code is AIInputErrorCode.INVALID_IMAGE

    request = extraction_input()
    clarification = EvidenceItem.model_validate(
        {
            "evidenceId": "clarification-1",
            "kind": "clarification",
            "localRef": "owned-clarification.txt",
            "mediaType": "text/plain",
            "sha256": sha256(b"14:30").hexdigest(),
            "text": "14:30",
            "modelCopyApproved": True,
        }
    )
    with pytest.raises(AIInputError) as wrong_text:
        ExtractionInput(images=request.images, statement=clarification)
    assert wrong_text.value.code is AIInputErrorCode.INVALID_EVIDENCE_INVENTORY


def test_tampered_statement_content_address_fails_before_call() -> None:
    request = extraction_input()
    client = FakeExtractionClient([RuntimeError("must not be called")])
    tampered = request.statement.model_copy(update={"text": "Tampered after approval"})
    object.__setattr__(request, "statement", tampered)

    with pytest.raises(AIInputError) as captured:
        runner(client, []).run(request)

    assert captured.value.code is AIInputErrorCode.INVALID_EVIDENCE_INVENTORY
    assert client.responses_api.calls == []


def test_schema_failure_gets_exactly_one_app_retry_then_succeeds() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    client = FakeExtractionClient(
        [FakeResponse(status="completed", output_text="{}"), completed_response(expected)]
    )

    result = runner(client, [1.0, 1.01, 2.0, 2.03]).run(request)

    assert isinstance(result, ExtractionSuccess)
    assert len(client.responses_api.calls) == 2
    assert client.responses_api.parse_calls == 0
    assert result.g2_run.attempts[0].retry_allowed is True
    assert result.g2_run.attempts[0].decision.reason_codes == (
        GateReasonCode.G2_SCHEMA_INVALID,
    )
    assert RETRY_INSTRUCTIONS not in client.responses_api.calls[0].instructions
    assert RETRY_INSTRUCTIONS in client.responses_api.calls[1].instructions
    assert tuple(item.retry_attempt for item in result.telemetry) == (0, 1)
    retry_event = result.telemetry[0].to_retry_event(result.g2_run)
    assert retry_event.operation is WorkflowOperation.EXTRACTION
    assert retry_event.model_id is ProviderModelId.SOL
    assert retry_event.provider_mode == "live"
    assert retry_event.call_sequence == 1
    assert retry_event.retry_attempt == 1
    assert retry_event.duration_ms == 10
    assert retry_event.failure.category is ProviderFailureCategory.INVALID_RESPONSE
    assert retry_event.failure.retryable and not retry_event.failure.terminal
    second_call_event = result.telemetry[1].to_success_event()
    assert second_call_event.call_sequence == 2
    assert second_call_event.retry_attempt == 1


def test_raw_create_strict_schema_matches_pinned_sdk_transform_without_eager_parse() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    request = extraction_input()
    expected = model_extraction(request)
    client = FakeExtractionClient([completed_response(expected)])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    schema = client.responses_api.calls[0].text["format"]["schema"]
    assert schema == to_strict_json_schema(ModelExtraction)
    evidence_schema = schema["$defs"]
    assert isinstance(evidence_schema, dict)
    evidence_item = evidence_schema["EvidenceItem"]
    assert isinstance(evidence_item, dict)
    assert "transcriptConfirmed" in evidence_item["required"]
    properties = evidence_item["properties"]
    assert isinstance(properties, dict)
    transcript_confirmed = properties["transcriptConfirmed"]
    assert isinstance(transcript_confirmed, dict)
    assert "default" not in transcript_confirmed
    assert client.responses_api.parse_calls == 0


def test_truncated_output_gets_one_g2_owned_retry() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    client = FakeExtractionClient(
        [
            FakeResponse(
                status="incomplete",
                output_text=f'{{"contractVersion":"{CONTRACT_VERSION}"',
                incomplete_details={"reason": "max_output_tokens"},
                output=[{"type": "message", "status": "incomplete", "content": []}],
            ),
            completed_response(expected),
        ]
    )

    result = runner(client, [1.0, 1.01, 2.0, 2.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    assert GateReasonCode.G2_OUTPUT_TRUNCATED in result.g2_run.attempts[0].decision.reason_codes
    assert len(client.responses_api.calls) == 2


def test_reference_mismatch_gets_one_g2_owned_retry() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    mismatched_evidence = (
        *expected.evidence[:-1],
        expected.evidence[-1].model_copy(update={"text": "Different statement"}),
    )
    mismatched_data: dict[str, object] = {
        "contractVersion": CONTRACT_VERSION,
        "evidence": mismatched_evidence,
        "provenance": expected.provenance,
        "facts": expected.facts,
        "claim": expected.claim,
    }
    mismatched = ModelExtraction.model_validate(mismatched_data)
    client = FakeExtractionClient(
        [completed_response(mismatched), completed_response(expected)]
    )

    result = runner(client, [1.0, 1.01, 2.0, 2.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    assert result.g2_run.attempts[0].decision.reason_codes == (
        GateReasonCode.G2_REFERENCE_MISSING,
    )
    assert len(client.responses_api.calls) == 2


def test_second_contract_failure_is_terminal_and_never_gets_third_call() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(status="completed", output_text="{}"),
            FakeResponse(status="completed", output_text="{}"),
        ]
    )

    result = runner(client, [1.0, 1.01, 2.0, 2.01]).run(request)

    assert isinstance(result, ExtractionBlocked)
    assert len(client.responses_api.calls) == 2
    assert GateReasonCode.G2_RETRY_EXHAUSTED in (
        result.g2_run.attempts[-1].decision.reason_codes
    )


def test_typed_refusal_is_g2_refusal_and_is_never_retried() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(
                status="completed",
                output_text=None,
                output=[
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [
                            {"type": "refusal", "refusal": "private refusal detail"}
                        ],
                    }
                ],
            )
        ]
    )

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionBlocked)
    assert GateReasonCode.G2_REFUSAL in result.g2_run.attempts[0].decision.reason_codes
    assert len(client.responses_api.calls) == 1
    assert "private refusal detail" not in repr(result)


def test_content_filter_incomplete_is_operational_failure_without_retry() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(
                status="incomplete",
                output_text=None,
                incomplete_details={"reason": "content_filter"},
            )
        ]
    )

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category is ProviderFailureCategory.CONTENT_FILTERED
    assert result.g2_run.attempts == ()
    assert len(client.responses_api.calls) == 1


def test_unknown_incomplete_reason_is_invalid_response_without_retry() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(
                status="incomplete",
                output_text=None,
                incomplete_details={"reason": "future_unknown_reason"},
            )
        ]
    )

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category is ProviderFailureCategory.INVALID_RESPONSE
    assert len(client.responses_api.calls) == 1


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (openai.APITimeoutError(httpx.Request("POST", "https://api.invalid")), "timeout"),
        (
            openai.RateLimitError(
                "private quota detail",
                response=httpx.Response(
                    429,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "insufficient_quota"},
            ),
            "quota_exhausted",
        ),
        (
            openai.AuthenticationError(
                "private auth detail",
                response=httpx.Response(
                    401,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "invalid_api_key"},
            ),
            "authentication_failed",
        ),
        (
            openai.PermissionDeniedError(
                "private permission detail",
                response=httpx.Response(
                    403,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "permission_denied"},
            ),
            "permission_denied",
        ),
        (
            openai.NotFoundError(
                "private model detail",
                response=httpx.Response(
                    404,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "model_not_found"},
            ),
            "model_not_found",
        ),
        (
            openai.RateLimitError(
                "private billing detail",
                response=httpx.Response(
                    429,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "billing_hard_limit_reached"},
            ),
            "billing_limit",
        ),
        (
            openai.RateLimitError(
                "private rate detail",
                response=httpx.Response(
                    429,
                    request=httpx.Request("POST", "https://api.invalid"),
                ),
                body={"code": "rate_limit_exceeded"},
            ),
            "rate_limited",
        ),
    ],
)
def test_provider_exceptions_never_use_app_retry(
    error: Exception,
    category: str,
) -> None:
    request = extraction_input()
    client = FakeExtractionClient([error])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category.value == category
    assert result.failure.terminal and not result.failure.retryable
    assert len(client.responses_api.calls) == 1
    failure_event = result.telemetry[-1].to_failure_event(result.failure)
    assert failure_event.failure == result.failure
    assert failure_event.model_id is ProviderModelId.SOL
    assert failure_event.provider_mode == "live"
    assert failure_event.call_sequence == 1
    assert failure_event.retry_attempt == 0
    assert failure_event.duration_ms == 10


def test_failed_response_code_is_sanitized_and_never_enters_g2() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(
                status="failed",
                error=FakeResponseError(
                    code="image_content_policy_violation",
                    message="private remote policy detail",
                ),
            )
        ]
    )

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category is ProviderFailureCategory.CONTENT_FILTERED
    assert result.g2_run.attempts == ()
    assert "private remote policy detail" not in repr(result)
    assert "req-private-marker" not in repr(result)
    assert len(client.responses_api.calls) == 1


def test_provider_failure_on_second_call_stops_after_g2_authorized_retry() -> None:
    request = extraction_input()
    client = FakeExtractionClient(
        [
            FakeResponse(status="completed", output_text="{}"),
            RuntimeError("private provider outage"),
        ]
    )

    result = runner(client, [1.0, 1.01, 2.0, 2.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category is ProviderFailureCategory.PROVIDER_UNAVAILABLE
    assert len(result.g2_run.attempts) == 1
    assert tuple(item.status.value for item in result.telemetry) == ("succeeded", "failed")
    retry_event = result.telemetry[0].to_retry_event(result.g2_run)
    terminal_event = result.telemetry[1].to_failure_event(result.failure)
    assert retry_event.call_sequence == 1
    assert retry_event.retry_attempt == 1
    assert terminal_event.call_sequence == 2
    assert terminal_event.retry_attempt == 1
    assert terminal_event.duration_ms == 10
    assert len(client.responses_api.calls) == 2


def test_retry_event_requires_deterministic_g2_authority() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    client = FakeExtractionClient([completed_response(expected)])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    with pytest.raises(ValueError, match="did not authorize"):
        result.telemetry[0].to_retry_event(result.g2_run)


def test_invalid_usage_is_dropped_without_weakening_success() -> None:
    request = extraction_input()
    expected = model_extraction(request)
    response = completed_response(expected)
    object.__setattr__(
        response,
        "usage",
        {"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
    )
    client = FakeExtractionClient([response])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionSuccess)
    assert result.telemetry[0].usage is None
    assert result.telemetry[0].to_success_event().usage is None


def test_unknown_or_in_progress_response_status_is_terminal_invalid_response() -> None:
    request = extraction_input()
    client = FakeExtractionClient([FakeResponse(status="in_progress")])

    result = runner(client, [1.0, 1.01]).run(request)

    assert isinstance(result, ExtractionProviderFailure)
    assert result.failure.category is ProviderFailureCategory.INVALID_RESPONSE
    assert len(client.responses_api.calls) == 1
    assert EXTRACTION_INSTRUCTIONS in client.responses_api.calls[0].instructions
