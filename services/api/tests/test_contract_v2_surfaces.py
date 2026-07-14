"""V2 authority, portal, tool, provider, and event contract tests."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND,
    CONTRACT_VERSION,
    AuditEvent,
    GateDecision,
    GateId,
    GateReasonCode,
    PlanStep,
    PortalDraftFields,
    PortalReviewFields,
    PortalSessionView,
    ProviderFailure,
    RenderedPortalSnapshot,
    SandboxReceipt,
    ToolInvocation,
    TranscriptConfirmationRequest,
    TranscriptConfirmationView,
    WorkflowEventEnvelope,
    WorkflowEventKind,
    validate_workflow_event_order,
)
from claimdone_api.gates.registry import (
    G0_TO_G5_REGISTRY,
    G0_TO_G10_REGISTRY,
    make_gate_decision,
)

NOW = "2026-07-14T12:00:00Z"
DIGEST = "a" * 64


def empty_draft() -> dict[str, object]:
    return {
        "incidentDate": "",
        "incidentTime": "",
        "location": "",
        "claimantName": "",
        "policyReference": "",
        "vehicleRegistration": "",
        "counterpartyKnown": "",
        "narrative": "",
        "attachments": [],
    }


def clarification_envelope(cursor: int = 1) -> dict[str, object]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "eventId": f"projection-{cursor}",
        "caseId": "case-1",
        "sourceAuditEventId": f"audit-{cursor}",
        "sourceAuditEventType": "clarification",
        "sourceAuditSequence": cursor,
        "cursor": cursor,
        "occurredAt": NOW,
        "event": {
            "kind": "clarification",
            "round": 1,
            "field": "incident_date",
            "status": "requested",
        },
    }


def test_transcript_confirmation_is_bound_to_hash_version_and_true_boolean() -> None:
    view = TranscriptConfirmationView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "transcriptId": "transcript-1",
            "transcriptSha256": DIGEST,
            "text": "Synthetic transcript",
            "version": 2,
            "confirmed": False,
        }
    )
    request = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "transcriptId": view.transcript_id,
            "transcriptSha256": view.transcript_sha256,
            "expectedVersion": view.version,
            "confirmed": True,
        }
    )
    assert request.confirmed is True

    for key, value in (("confirmed", False), ("expectedVersion", True)):
        unsafe = request.model_dump(mode="json", by_alias=True)
        unsafe[key] = value
        with pytest.raises(ValidationError):
            TranscriptConfirmationRequest.model_validate(unsafe)
    with pytest.raises(ValidationError, match="extra"):
        TranscriptConfirmationRequest.model_validate(
            {**request.model_dump(mode="json", by_alias=True), "prompt": "ignored"}
        )


def test_tool_invocation_arguments_are_exactly_empty_and_plan_has_no_arguments() -> None:
    invocation = ToolInvocation.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "invocationId": "trusted-invocation-1",
            "sequence": 1,
            "tool": "inspect_form",
            "arguments": {},
        }
    )
    assert invocation.arguments.model_dump() == {}

    for forbidden in (
        {"caseId": "case-1"},
        {"url": "https://example.test"},
        {"value": "claim value"},
    ):
        data = invocation.model_dump(mode="json", by_alias=True)
        data["arguments"] = forbidden
        with pytest.raises(ValidationError, match="extra"):
            ToolInvocation.model_validate(data)

    with pytest.raises(ValidationError, match="arguments"):
        PlanStep.model_validate(
            {
                "sequence": 1,
                "tool": "inspect_form",
                "reason": "Read only the sandbox form",
                "arguments": {},
            }
        )


def test_portal_draft_preserves_empty_raw_controls_but_review_fields_are_complete() -> None:
    raw = empty_draft()
    raw["location"] = "  Berlin  "
    draft = PortalDraftFields.model_validate(raw)
    assert draft.location == "  Berlin  "
    assert draft.attachments == ()

    snapshot = RenderedPortalSnapshot.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": "case-1",
            "variant": "A",
            "state": "review",
            "version": 4,
            "fields": empty_draft(),
            "renderedAt": NOW,
        }
    )
    assert snapshot.fields.attachments == ()

    with pytest.raises(ValidationError):
        PortalReviewFields.model_validate(empty_draft())


def test_receipt_has_only_redacted_summary_and_session_cannot_project_receipt() -> None:
    with pytest.raises(ValidationError, match="state"):
        PortalSessionView.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": "case-1",
                "variant": "A",
                "state": "receipt",
                "version": 7,
                "fields": empty_draft(),
                "updatedAt": NOW,
                "auditCount": 4,
            }
        )

    receipt_data = {
        "contractVersion": CONTRACT_VERSION,
        "receiptId": "receipt-1",
        "caseId": "case-1",
        "approvalId": "approval-1",
        "variant": "A",
        "state": "receipt",
        "version": 8,
        "environment": "sandbox",
        "sandboxOnly": True,
        "submittedToRealInsurer": False,
        "humanApproved": True,
        "redacted": True,
        "summary": {
            "completedFieldCount": 8,
            "attachmentCount": 3,
            "verificationPassed": True,
            "finalActionOwner": "human",
        },
        "approvedAt": NOW,
        "renderedAt": "2026-07-14T12:00:01Z",
    }
    assert SandboxReceipt.model_validate(receipt_data).redacted is True
    unsafe = deepcopy(receipt_data)
    unsafe["summary"]["claimantName"] = "Ada"  # type: ignore[index]
    with pytest.raises(ValidationError, match="extra"):
        SandboxReceipt.model_validate(unsafe)


def test_workflow_events_are_closed_redacted_projections_with_monotonic_cursors() -> None:
    first = WorkflowEventEnvelope.model_validate(clarification_envelope(1))
    third = WorkflowEventEnvelope.model_validate(clarification_envelope(3))
    validate_workflow_event_order((first, third))
    assert set(AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND) == set(WorkflowEventKind)
    assert len(set(AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND.values())) == len(WorkflowEventKind)
    with pytest.raises(ValueError, match="increasing"):
        validate_workflow_event_order((third, first))

    for sensitive_key in ("prompt", "response", "toolArgs", "details", "claimantName"):
        unsafe = clarification_envelope()
        unsafe_event = unsafe["event"]
        assert isinstance(unsafe_event, dict)
        unsafe_event[sensitive_key] = "sensitive"
        with pytest.raises(ValidationError, match="extra"):
            WorkflowEventEnvelope.model_validate(unsafe)

    for bad_round in (True, 1.0, 0, 4):
        unsafe = clarification_envelope()
        unsafe_event = unsafe["event"]
        assert isinstance(unsafe_event, dict)
        unsafe_event["round"] = bad_round
        with pytest.raises(ValidationError):
            WorkflowEventEnvelope.model_validate(unsafe)

    for cursor in (True, 0):
        unsafe = clarification_envelope()
        unsafe["cursor"] = cursor
        unsafe["sourceAuditSequence"] = cursor
        with pytest.raises(ValidationError):
            WorkflowEventEnvelope.model_validate(unsafe)

    mismatched_type = clarification_envelope()
    mismatched_type["sourceAuditEventType"] = "gate_decision"
    with pytest.raises(ValidationError, match="workflow event kind"):
        WorkflowEventEnvelope.model_validate(mismatched_type)

    with pytest.raises(ValidationError, match="Only gate_decision"):
        AuditEvent.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "eventId": "audit-provider-failure",
                "caseId": "case-1",
                "eventType": "operational_failure",
                "actor": "system",
                "occurredAt": NOW,
                "fromState": None,
                "toState": None,
                "reasonCodes": ["G2_RETRY_EXHAUSTED"],
                "details": [],
            }
        )


@pytest.mark.parametrize(
    "category",
    [
        "quota_exhausted",
        "billing_limit",
        "rate_limited",
        "authentication_failed",
        "permission_denied",
        "model_not_found",
        "invalid_request",
        "cancelled",
    ],
)
def test_terminal_provider_failures_can_never_be_retryable(category: str) -> None:
    failure = ProviderFailure.model_validate(
        {"category": category, "retryable": False, "terminal": True}
    )
    assert failure.terminal is True
    with pytest.raises(ValidationError, match="terminal"):
        ProviderFailure.model_validate({"category": category, "retryable": True, "terminal": False})


def test_provider_call_event_closes_model_identity_retry_and_cost_currency() -> None:
    event = clarification_envelope()
    event["sourceAuditEventType"] = "provider_call"
    event["event"] = {
        "kind": "provider_call",
        "operation": "extraction",
        "modelId": "gpt-5.6-sol",
        "providerMode": "live",
        "callSequence": 1,
        "retryAttempt": 0,
        "durationMs": 250,
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "cost": {
            "estimatedCostMicros": 42,
            "currency": "USD",
            "pricingSnapshotId": "openai-pricing-2026-07-14",
        },
        "status": "succeeded",
    }
    assert WorkflowEventEnvelope.model_validate(event).event.kind == "provider_call"

    for path, value in (
        (("modelId",), "gpt-5.6"),
        (("callSequence",), True),
        (("retryAttempt",), 1.0),
        (("cost", "currency"), "EUR"),
    ):
        unsafe = deepcopy(event)
        target = unsafe["event"]
        assert isinstance(target, dict)
        if len(path) == 1:
            target[path[0]] = value
        else:
            nested = target[path[0]]
            assert isinstance(nested, dict)
            nested[path[1]] = value
        with pytest.raises(ValidationError):
            WorkflowEventEnvelope.model_validate(unsafe)


def test_retry_event_is_one_extraction_retry_and_never_a_clarification_round() -> None:
    envelope = clarification_envelope()
    envelope["sourceAuditEventType"] = "retry"
    envelope["event"] = {
        "kind": "retry",
        "operation": "extraction",
        "retryAttempt": 1,
        "failure": {
            "category": "timeout",
            "retryable": True,
            "terminal": False,
        },
    }
    assert WorkflowEventEnvelope.model_validate(envelope).event.kind == "retry"

    for key, value in (("operation", "transcription"), ("retryAttempt", True)):
        unsafe = deepcopy(envelope)
        unsafe_event = unsafe["event"]
        assert isinstance(unsafe_event, dict)
        unsafe_event[key] = value
        with pytest.raises(ValidationError):
            WorkflowEventEnvelope.model_validate(unsafe)
    unsafe = deepcopy(envelope)
    unsafe_event = unsafe["event"]
    assert isinstance(unsafe_event, dict)
    unsafe_event["round"] = 1
    with pytest.raises(ValidationError, match="extra"):
        WorkflowEventEnvelope.model_validate(unsafe)


def test_full_gate_registry_keeps_prefix_and_g8_model_reason_authoritative() -> None:
    assert tuple(spec.gate_id for spec in G0_TO_G10_REGISTRY.specs) == tuple(GateId)[:11]
    assert G0_TO_G5_REGISTRY.specs == G0_TO_G10_REGISTRY.specs[:6]
    decision = make_gate_decision(GateId.G8_VERIFICATION, model_blocked=True)
    assert decision.reason_codes == (GateReasonCode.G8_MODEL_MISMATCH,)
    assert decision.deterministic_passed is True
    assert decision.passed is False

    contradictory = decision.model_dump(mode="json", by_alias=True)
    contradictory["reasonCodes"] = ["G8_FIELD_MISMATCH"]
    with pytest.raises(ValidationError, match="deterministicPassed"):
        GateDecision.model_validate(contradictory)

    for gate_id in (
        GateId.G6_TOOL_AUTHORITY,
        GateId.G7_PORTAL_WRITE,
        GateId.G8_VERIFICATION,
        GateId.G9_HUMAN_APPROVAL,
        GateId.G10_RECEIPT_REDACTION,
    ):
        assert make_gate_decision(gate_id).passed
    with pytest.raises(KeyError):
        make_gate_decision(GateId.G11_RELEASE)

    deterministic_and_model = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(GateReasonCode.G8_FIELD_MISMATCH,),
        model_blocked=True,
    )
    assert deterministic_and_model.reason_codes == (
        GateReasonCode.G8_FIELD_MISMATCH,
        GateReasonCode.G8_MODEL_MISMATCH,
    )
    assert deterministic_and_model.deterministic_passed is False
    assert deterministic_and_model.passed is False
