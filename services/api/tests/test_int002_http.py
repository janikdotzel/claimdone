"""Closed HTTP adapter tests for the three canonical INT-002 mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claimdone_api.cases import create_int002_router
from claimdone_api.cases.errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.cases.int002_errors import (
    workflow_gate_blocked,
    workflow_internal_error,
    workflow_version_conflict,
)
from claimdone_api.computer_use.portal import PortalGatewayError
from claimdone_api.computer_use.ports import BrowserOperationTimeout
from claimdone_api.contracts import (
    ClarificationAnswerRequest,
    GateId,
    GateReasonCode,
    WorkflowSnapshot,
)
from claimdone_api.gates.registry import make_gate_decision
from claimdone_api.media import ExifDecision, IntakeRequest

CASE_ID = "case-int002-http"
CLARIFICATION_ID = "clarification-int002-http"


@dataclass(frozen=True, slots=True)
class IntakeCall:
    case_id: str
    expected_version: int
    request: IntakeRequest
    exif_decisions: tuple[ExifDecision, ...]


@dataclass(frozen=True, slots=True)
class AnswerCall:
    case_id: str
    clarification_id: str
    request: ClarificationAnswerRequest


@dataclass(slots=True)
class FakeInt002Service:
    snapshot: WorkflowSnapshot
    failure: Exception | None = None
    intake_calls: list[IntakeCall] = field(default_factory=list)
    answer_calls: list[AnswerCall] = field(default_factory=list)
    run_calls: list[tuple[str, int]] = field(default_factory=list)

    def submit_intake(
        self,
        case_id: str,
        *,
        expected_version: int,
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> WorkflowSnapshot:
        self.intake_calls.append(IntakeCall(case_id, expected_version, request, exif_decisions))
        return self._result()

    def answer_clarification(
        self,
        case_id: str,
        clarification_id: str,
        request: ClarificationAnswerRequest,
    ) -> WorkflowSnapshot:
        self.answer_calls.append(AnswerCall(case_id, clarification_id, request))
        return self._result()

    def run_to_review(
        self,
        case_id: str,
        *,
        expected_version: int,
    ) -> WorkflowSnapshot:
        self.run_calls.append((case_id, expected_version))
        return self._result()

    def _result(self) -> WorkflowSnapshot:
        if self.failure is not None:
            raise self.failure
        return self.snapshot


def _snapshot() -> WorkflowSnapshot:
    return WorkflowSnapshot.model_validate(
        {
            "contractVersion": "4.0.0",
            "requestId": "request-int002-http",
            "case": {
                "contractVersion": "4.0.0",
                "caseId": CASE_ID,
                "state": "created",
                "version": 1,
                "createdAt": "2026-07-15T08:00:00Z",
                "updatedAt": "2026-07-15T08:00:00Z",
            },
            "claimPacket": None,
            "transcriptConfirmation": None,
            "clarification": None,
            "portalSession": None,
            "verificationAttempts": None,
            "receipt": None,
        }
    )


def _client() -> tuple[TestClient, FakeInt002Service]:
    service = FakeInt002Service(_snapshot())
    app = FastAPI()
    app.include_router(create_int002_router(service))
    return TestClient(app), service


def _multipart_parts(
    *,
    statement: bool = True,
    audio: bool = False,
    expected_version: str = "1",
    sandbox: str = "true",
    decisions: tuple[str, ...] = ("retain", "strip", "retain"),
    image_count: int = 3,
    extra: tuple[str, str] | None = None,
) -> list[tuple[str, tuple[str | None, bytes | str, str | None]]]:
    parts: list[tuple[str, tuple[str | None, bytes | str, str | None]]] = [
        ("expectedVersion", (None, expected_version, None)),
        ("sandboxAcknowledged", (None, sandbox, None)),
        ("imageRightsConfirmed", (None, "true", None)),
        ("dataProcessingApproved", (None, "true", None)),
    ]
    if statement:
        parts.append(("statementText", (None, "  Exact statement.  ", None)))
    if audio:
        parts.append(("audio", ("statement.wav", b"RIFF-staged", "audio/wav")))
    parts.extend(("exifDecisions", (None, decision, None)) for decision in decisions)
    images = (
        ("one.png", b"image-one", "image/png"),
        ("two.png", b"image-two", "image/png"),
        ("three.png", b"image-three", "image/png"),
    )
    parts.extend(("images", image) for image in images[:image_count])
    if extra is not None:
        parts.append((extra[0], (None, extra[1], None)))
    return parts


def _answer_body() -> dict[str, object]:
    return {
        "answer": "14:30:00",
        "caseId": CASE_ID,
        "clarificationId": CLARIFICATION_ID,
        "contractVersion": "4.0.0",
        "expectedVersion": 4,
        "field": "incident_time",
        "round": 1,
    }


def _assert_closed_error(
    response: Any,
    *,
    status_code: int,
    code: str,
) -> dict[str, Any]:
    assert response.status_code == status_code
    body = cast(dict[str, Any], response.json())
    assert set(body) == {"error"}
    detail = cast(dict[str, Any], body["error"])
    assert set(detail) == {
        "code",
        "currentVersion",
        "fieldErrors",
        "gateDecision",
        "message",
        "reasonCodes",
    }
    assert detail["code"] == code
    return detail


def test_intake_parses_closed_ordered_multipart_into_media_types() -> None:
    client, service = _client()

    response = client.post(
        f"/api/cases/{CASE_ID}/intake",
        files=_multipart_parts(),
    )

    assert response.status_code == 200
    assert WorkflowSnapshot.model_validate(response.json()) == service.snapshot
    assert len(service.intake_calls) == 1
    call = service.intake_calls[0]
    assert call.case_id == CASE_ID
    assert call.expected_version == 1
    assert tuple(image.content for image in call.request.images) == (
        b"image-one",
        b"image-two",
        b"image-three",
    )
    assert tuple(image.media_type for image in call.request.images) == (
        "image/png",
        "image/png",
        "image/png",
    )
    assert call.request.text == "  Exact statement.  "
    assert call.request.audio is None
    assert call.request.consents.sandbox_acknowledged is True
    assert call.request.consents.image_rights_confirmed is True
    assert call.request.consents.data_processing_approved is True
    assert call.exif_decisions == (
        ExifDecision.RETAIN,
        ExifDecision.STRIP,
        ExifDecision.RETAIN,
    )


def test_intake_accepts_exactly_one_audio_mode_without_text() -> None:
    client, service = _client()

    response = client.post(
        f"/api/cases/{CASE_ID}/intake",
        files=_multipart_parts(statement=False, audio=True),
    )

    assert response.status_code == 200
    request = service.intake_calls[0].request
    assert request.text is None
    assert request.audio is not None
    assert request.audio.content == b"RIFF-staged"
    assert request.audio.media_type == "audio/wav"


@pytest.mark.parametrize(
    "parts",
    (
        _multipart_parts(statement=True, audio=True),
        _multipart_parts(image_count=2),
        _multipart_parts(decisions=("retain", "invalid", "retain")),
        _multipart_parts(expected_version="01"),
        _multipart_parts(sandbox="TRUE"),
        _multipart_parts(extra=("attackerField", "do-not-reflect-this-value")),
    ),
)
def test_intake_rejects_noncanonical_multipart_without_calling_service(
    parts: list[tuple[str, tuple[str | None, bytes | str, str | None]]],
) -> None:
    client, service = _client()

    response = client.post(f"/api/cases/{CASE_ID}/intake", files=parts)

    detail = _assert_closed_error(
        response,
        status_code=422,
        code="INTAKE_FORM_INVALID",
    )
    assert not service.intake_calls
    assert "do-not-reflect-this-value" not in response.text
    assert detail["gateDecision"] is None
    assert detail["reasonCodes"] == []


def test_intake_rejects_duplicate_singleton_part() -> None:
    client, service = _client()
    parts = [
        *_multipart_parts(),
        ("expectedVersion", (None, "1", None)),
    ]

    response = client.post(f"/api/cases/{CASE_ID}/intake", files=parts)

    _assert_closed_error(response, status_code=422, code="INTAKE_FORM_INVALID")
    assert not service.intake_calls


def test_answer_forwards_the_full_canonical_request() -> None:
    client, service = _client()

    response = client.post(
        f"/api/cases/{CASE_ID}/clarifications/{CLARIFICATION_ID}/answer",
        json=_answer_body(),
    )

    assert response.status_code == 200
    assert len(service.answer_calls) == 1
    call = service.answer_calls[0]
    assert call.case_id == CASE_ID
    assert call.clarification_id == CLARIFICATION_ID
    assert call.request == ClarificationAnswerRequest.model_validate(_answer_body())


@pytest.mark.parametrize(
    "mutation",
    (
        {"unexpected": "secret-value"},
        {"expectedVersion": "4"},
        {"contractVersion": "3.0.0"},
        {"round": True},
    ),
)
def test_answer_rejects_noncanonical_json_with_safe_envelope(
    mutation: dict[str, object],
) -> None:
    client, service = _client()
    body = _answer_body()
    body.update(mutation)

    response = client.post(
        f"/api/cases/{CASE_ID}/clarifications/{CLARIFICATION_ID}/answer",
        json=body,
    )

    _assert_closed_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert not service.answer_calls
    assert "secret-value" not in response.text


def test_answer_binds_body_identity_to_both_path_identifiers() -> None:
    client, service = _client()
    body = _answer_body()
    body["caseId"] = "case-other"

    response = client.post(
        f"/api/cases/{CASE_ID}/clarifications/{CLARIFICATION_ID}/answer",
        json=body,
    )

    _assert_closed_error(
        response,
        status_code=422,
        code="REQUEST_IDENTITY_MISMATCH",
    )
    assert not service.answer_calls


def test_run_accepts_only_the_closed_versioned_json_and_forwards_version() -> None:
    client, service = _client()

    response = client.post(
        f"/api/cases/{CASE_ID}/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    assert response.status_code == 200
    assert service.run_calls == [(CASE_ID, 5)]


@pytest.mark.parametrize(
    "body",
    (
        {"contractVersion": "4.0.0", "expectedVersion": "5"},
        {"contractVersion": "4.0.0", "expectedVersion": 5, "extra": True},
        {"contractVersion": "3.0.0", "expectedVersion": 5},
        {"contractVersion": "4.0.0"},
    ),
)
def test_run_rejects_noncanonical_json(body: dict[str, object]) -> None:
    client, service = _client()

    response = client.post(f"/api/cases/{CASE_ID}/run", json=body)

    _assert_closed_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert not service.run_calls


def test_expected_service_failure_preserves_only_safe_version_metadata() -> None:
    client, service = _client()
    service.failure = workflow_version_conflict(current_version=9)

    response = client.post(
        f"/api/cases/{CASE_ID}/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    detail = _assert_closed_error(
        response,
        status_code=409,
        code="WORKFLOW_VERSION_CONFLICT",
    )
    assert detail["currentVersion"] == 9
    assert detail["fieldErrors"] == []


def test_gate_failure_envelope_binds_reasons_to_the_canonical_decision() -> None:
    client, service = _client()
    decision = make_gate_decision(
        GateId.G5_COMPLETENESS,
        deterministic_reasons=(GateReasonCode.G5_REQUIRED_FIELD_MISSING,),
    )
    service.failure = workflow_gate_blocked(decision)

    response = client.post(
        f"/api/cases/{CASE_ID}/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    detail = _assert_closed_error(
        response,
        status_code=422,
        code="WORKFLOW_GATE_BLOCKED",
    )
    assert detail["reasonCodes"] == ["G5_REQUIRED_FIELD_MISSING"]
    assert detail["gateDecision"]["reasonCodes"] == detail["reasonCodes"]
    assert detail["fieldErrors"] == [
        {
            "field": "workflow",
            "message": "A deterministic gate blocked this request.",
            "reasonCode": "G5_REQUIRED_FIELD_MISSING",
        }
    ]


@pytest.mark.parametrize(
    ("failure", "status_code", "code", "current_version"),
    (
        (CaseNotFoundError("private-case-id"), 404, "WORKFLOW_CASE_NOT_FOUND", None),
        (
            CaseVersionConflictError("private-case-id", 5, 8),
            409,
            "WORKFLOW_VERSION_CONFLICT",
            8,
        ),
        (
            CaseSnapshotValidationError("private persisted value"),
            409,
            "WORKFLOW_STATE_CONFLICT",
            None,
        ),
        (
            PortalGatewayError("private-operation", 503),
            502,
            "INT002_COMPOSITION_FAILED",
            None,
        ),
        (
            BrowserOperationTimeout("private browser timeout"),
            502,
            "INT002_COMPOSITION_FAILED",
            None,
        ),
    ),
)
def test_domain_and_local_upstream_errors_map_without_reflecting_values(
    failure: Exception,
    status_code: int,
    code: str,
    current_version: int | None,
) -> None:
    client, service = _client()
    service.failure = failure

    response = client.post(
        f"/api/cases/{CASE_ID}/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    detail = _assert_closed_error(
        response,
        status_code=status_code,
        code=code,
    )
    assert detail["currentVersion"] == current_version
    assert "private" not in response.text


def test_unexpected_service_failure_is_redacted_to_closed_500() -> None:
    client, service = _client()
    service.failure = RuntimeError("sensitive caller-controlled value")

    response = client.post(
        f"/api/cases/{CASE_ID}/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    _assert_closed_error(
        response,
        status_code=500,
        code=workflow_internal_error().code,
    )
    assert "sensitive caller-controlled value" not in response.text


def test_malformed_json_and_invalid_path_are_closed_before_service_call() -> None:
    client, service = _client()

    malformed = client.post(
        f"/api/cases/{CASE_ID}/run",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    invalid_path = client.post(
        "/api/cases/%20case-int002-http/run",
        json={"contractVersion": "4.0.0", "expectedVersion": 5},
    )

    _assert_closed_error(
        malformed,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    _assert_closed_error(
        invalid_path,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert not service.run_calls
