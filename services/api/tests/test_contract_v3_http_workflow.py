"""Closed HTTP workflow roots and cross-payload authority tests."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ClarificationAnswerRequest,
    ClarificationStatus,
    ClarificationView,
    GateId,
    GateReasonCode,
    WorkflowCaseView,
    WorkflowSnapshot,
)
from claimdone_api.gates.registry import make_gate_decision

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
CREATED_AT = "2026-07-14T12:00:00Z"
UPDATED_AT = "2026-07-14T12:00:20Z"
CASE_ID = "case-happy-001"
DIGEST = "a" * 64


def happy_packet_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def packet_for_state(state: str) -> dict[str, Any]:
    packet = happy_packet_data()
    packet["state"] = state
    if state in {
        "analyzing",
        "awaiting_clarification",
        "ready_to_fill",
        "filling",
    }:
        packet["portalState"] = "draft"
    elif state in {"verifying", "review"}:
        packet["portalState"] = "review"
    elif state == "human_approved":
        packet["portalState"] = "human_approved"
        packet["gateDecisions"].append(
            make_gate_decision(
                GateId.G9_HUMAN_APPROVAL,
                decided_at=datetime(2026, 7, 14, 12, 0, 10, tzinfo=UTC),
            ).model_dump(mode="json", by_alias=True)
        )
    return packet


def case_data(
    state: str,
    *,
    case_id: str = CASE_ID,
    version: int = 7,
) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "caseId": case_id,
        "state": state,
        "version": version,
        "createdAt": CREATED_AT,
        "updatedAt": UPDATED_AT,
    }


def snapshot_data(state: str, *, version: int = 7) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "requestId": "request-001",
        "case": case_data(state, version=version),
        "claimPacket": None,
        "transcriptConfirmation": None,
        "clarification": None,
        "portalSession": None,
        "verificationAttempts": None,
        "receipt": None,
    }


def transcript_data(*, case_id: str = CASE_ID, version: int = 7) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "caseId": case_id,
        "transcriptId": "transcript-001",
        "transcriptSha256": DIGEST,
        "text": "Synthetic transcript",
        "version": version,
        "confirmed": False,
    }


def clarification_data(
    *,
    case_id: str = CASE_ID,
    expected_version: int = 7,
) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "clarificationId": "clarification-001",
        "caseId": case_id,
        "field": "incident_date",
        "round": 1,
        "question": "What was the incident date?",
        "status": "requested",
        "expectedVersion": expected_version,
        "requestedAt": "2026-07-14T12:00:10Z",
    }


def portal_data(
    *,
    case_id: str = CASE_ID,
    version: int = 3,
    state: str = "review",
) -> dict[str, Any]:
    claim = happy_packet_data()["claim"]
    return {
        "contractVersion": CONTRACT_VERSION,
        "caseId": case_id,
        "variant": "A",
        "state": state,
        "version": version,
        "fields": {
            "incidentDate": claim["incidentDate"],
            "incidentTime": claim["incidentTime"],
            "location": claim["location"],
            "claimantName": claim["claimantName"],
            "policyReference": claim["policyReference"],
            "vehicleRegistration": claim["vehicleRegistration"],
            "counterpartyKnown": claim["counterpartyKnown"],
            "narrative": claim["narrative"],
            "attachments": claim["attachments"],
        },
        "updatedAt": "2026-07-14T12:00:15Z",
        "auditCount": 4,
    }


def verification_series_data(
    *,
    case_id: str = CASE_ID,
    portal_version: int = 3,
) -> dict[str, Any]:
    packet = happy_packet_data()
    return {
        "contractVersion": CONTRACT_VERSION,
        "caseId": case_id,
        "attempts": [
            {
                "contractVersion": CONTRACT_VERSION,
                "attemptId": "verification-001",
                "caseId": case_id,
                "attemptNumber": 1,
                "caseState": "verifying",
                "portalVersion": portal_version,
                "report": packet["verification"],
                "final": True,
                "repair": None,
                "repairedFromAttemptId": None,
                "gateDecision": packet["gateDecisions"][-1],
            }
        ],
    }


def failed_verification_series_data() -> dict[str, Any]:
    series = verification_series_data()
    attempt = series["attempts"][0]
    report = attempt["report"]
    location = next(
        result for result in report["fieldResults"] if result["field"] == "location"
    )
    location["actual"] = "Berln"
    location["status"] = "mismatch"
    report["status"] = "mismatch"
    report["deterministicMatch"] = False
    report["reviewAllowed"] = False
    attempt["gateDecision"] = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(GateReasonCode.G8_FIELD_MISMATCH,),
        decided_at=datetime(2026, 7, 14, 12, 0, 9, tzinfo=UTC),
    ).model_dump(mode="json", by_alias=True)
    return series


def blocked_attachment_snapshot_data() -> dict[str, Any]:
    data = snapshot_data("blocked")
    packet = happy_packet_data()
    packet["state"] = "blocked"
    packet["portalState"] = "review"
    report = deepcopy(packet["verification"])
    report["status"] = "mismatch"
    report["deterministicMatch"] = False
    report["actualAttachmentIds"] = [
        "alternate-ref-1",
        "alternate-ref-2",
        "alternate-ref-3",
    ]
    report["actualAttachmentCount"] = 3
    report["reviewAllowed"] = False
    gate = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(GateReasonCode.G8_ATTACHMENT_MISMATCH,),
        decided_at=datetime(2026, 7, 14, 12, 0, 8, tzinfo=UTC),
    ).model_dump(mode="json", by_alias=True)
    packet["verification"] = deepcopy(report)
    packet["gateDecisions"][-1] = deepcopy(gate)
    series = verification_series_data()
    series["attempts"][0]["report"] = deepcopy(report)
    series["attempts"][0]["gateDecision"] = deepcopy(gate)
    data["claimPacket"] = packet
    data["portalSession"] = portal_data()
    data["verificationAttempts"] = series
    return data


def review_snapshot_data() -> dict[str, Any]:
    data = snapshot_data("review")
    data["claimPacket"] = happy_packet_data()
    data["portalSession"] = portal_data()
    data["verificationAttempts"] = verification_series_data()
    return data


def receipt_data(*, case_id: str = CASE_ID) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "receiptId": "receipt-001",
        "caseId": case_id,
        "approvalId": "approval-001",
        "variant": "A",
        "state": "receipt",
        "version": 2,
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
        "approvedAt": "2026-07-14T12:00:17Z",
        "renderedAt": "2026-07-14T12:00:18Z",
    }


def receipt_packet_data() -> dict[str, Any]:
    packet = happy_packet_data()
    packet["state"] = "receipt"
    packet["portalState"] = "receipt"
    packet["gateDecisions"].extend(
        [
            make_gate_decision(
                GateId.G9_HUMAN_APPROVAL,
                decided_at=datetime(2026, 7, 14, 12, 0, 10, tzinfo=UTC),
            ).model_dump(mode="json", by_alias=True),
            make_gate_decision(
                GateId.G10_RECEIPT_REDACTION,
                decided_at=datetime(2026, 7, 14, 12, 0, 11, tzinfo=UTC),
            ).model_dump(mode="json", by_alias=True),
        ]
    )
    return packet


def test_workflow_case_view_is_closed_versioned_and_timestamp_ordered() -> None:
    case = WorkflowCaseView.model_validate(case_data("created"))
    assert case.version == 7

    for field, value in (
        ("version", True),
        ("contractVersion", "3.0.0"),
        ("contractVersion", "2.1.0"),
        ("contractVersion", "2.0.0"),
    ):
        unsafe = case_data("created")
        unsafe[field] = value
        with pytest.raises(ValidationError):
            WorkflowCaseView.model_validate(unsafe)

    reversed_time = case_data("created")
    reversed_time["updatedAt"] = "2026-07-14T11:59:59Z"
    with pytest.raises(ValidationError, match="cannot precede"):
        WorkflowCaseView.model_validate(reversed_time)

    with pytest.raises(ValidationError, match="extra"):
        WorkflowCaseView.model_validate({**case_data("created"), "intakeSummary": {}})


def test_transcript_is_active_iff_confirmation_state_and_binds_case_version() -> None:
    data = snapshot_data("awaiting_transcript_confirmation")
    data["transcriptConfirmation"] = transcript_data()
    assert WorkflowSnapshot.model_validate(data).transcript_confirmation is not None

    missing = snapshot_data("awaiting_transcript_confirmation")
    with pytest.raises(ValidationError, match="requires an active transcript"):
        WorkflowSnapshot.model_validate(missing)

    wrong_state = snapshot_data("analyzing")
    wrong_state["transcriptConfirmation"] = transcript_data()
    with pytest.raises(ValidationError, match="only while awaiting confirmation"):
        WorkflowSnapshot.model_validate(wrong_state)

    for field, value in (("caseId", "case-other"), ("version", 6), ("version", True)):
        unsafe = deepcopy(data)
        unsafe["transcriptConfirmation"][field] = value
        with pytest.raises(ValidationError):
            WorkflowSnapshot.model_validate(unsafe)


@pytest.mark.parametrize(
    "state",
    ["created", "disclosed", "awaiting_transcript_confirmation"],
)
def test_pre_extraction_states_never_expose_a_claim_packet(state: str) -> None:
    packet = packet_for_state(state)
    packet["portalState"] = "draft"
    data = snapshot_data(state)
    data["claimPacket"] = packet
    if state == "awaiting_transcript_confirmation":
        data["transcriptConfirmation"] = transcript_data()
    with pytest.raises(ValidationError, match="Pre-extraction"):
        WorkflowSnapshot.model_validate(data)


def test_clarification_is_active_iff_clarification_state_and_binds_case_version_time() -> None:
    data = snapshot_data("awaiting_clarification")
    data["claimPacket"] = packet_for_state("awaiting_clarification")
    data["clarification"] = clarification_data()
    clarification = WorkflowSnapshot.model_validate(data).clarification
    assert clarification is not None
    assert clarification.status is ClarificationStatus.REQUESTED

    missing = snapshot_data("awaiting_clarification")
    missing["claimPacket"] = packet_for_state("awaiting_clarification")
    with pytest.raises(ValidationError, match="requires an active clarification"):
        WorkflowSnapshot.model_validate(missing)

    for field, value in (
        ("caseId", "case-other"),
        ("expectedVersion", 6),
        ("expectedVersion", True),
        ("round", True),
        ("requestedAt", "2026-07-14T12:00:21Z"),
    ):
        unsafe = deepcopy(data)
        unsafe["clarification"][field] = value
        with pytest.raises(ValidationError):
            WorkflowSnapshot.model_validate(unsafe)

    wrong_state = snapshot_data("ready_to_fill")
    wrong_state["claimPacket"] = packet_for_state("ready_to_fill")
    wrong_state["clarification"] = clarification_data()
    with pytest.raises(ValidationError, match="only while awaiting clarification"):
        WorkflowSnapshot.model_validate(wrong_state)

    with pytest.raises(ValidationError, match="extra"):
        ClarificationView.model_validate({**clarification_data(), "answer": "secret"})


def test_clarification_answer_preserves_exact_whitespace_and_is_fully_bound() -> None:
    exact_answer = "  14 July 2026\n"
    request = ClarificationAnswerRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": CASE_ID,
            "clarificationId": "clarification-001",
            "field": "incident_date",
            "round": 1,
            "expectedVersion": 7,
            "answer": exact_answer,
        }
    )
    assert request.answer == exact_answer
    assert request.model_dump(mode="json", by_alias=True)["answer"] == exact_answer

    for field, value in (
        ("answer", ""),
        ("answer", " \t\n"),
        ("answer", "x" * 4_001),
        ("answer", True),
        ("round", True),
        ("expectedVersion", True),
        ("contractVersion", "3.0.0"),
        ("contractVersion", "2.1.0"),
        ("contractVersion", "2.0.0"),
    ):
        unsafe = request.model_dump(mode="json", by_alias=True)
        unsafe[field] = value
        with pytest.raises(ValidationError):
            ClarificationAnswerRequest.model_validate(unsafe)

    with pytest.raises(ValidationError, match="extra"):
        ClarificationAnswerRequest.model_validate(
            {
                **request.model_dump(mode="json", by_alias=True),
                "normalizedAnswer": "2026-07-14",
            }
        )


def test_verifying_snapshot_may_have_portal_review_without_completed_series() -> None:
    data = snapshot_data("verifying")
    data["claimPacket"] = packet_for_state("verifying")
    data["portalSession"] = portal_data()
    snapshot = WorkflowSnapshot.model_validate(data)
    assert snapshot.portal_session is not None
    assert snapshot.verification_attempts is None


def test_verification_attempt_attachment_ids_bind_to_packet_and_rendered_portal() -> None:
    wrong_expected = snapshot_data("verifying")
    wrong_expected["claimPacket"] = packet_for_state("verifying")
    wrong_expected["portalSession"] = portal_data()
    wrong_expected["verificationAttempts"] = verification_series_data()
    report = wrong_expected["verificationAttempts"]["attempts"][0]["report"]
    replacement = ["alternate-ref-1", "alternate-ref-2", "alternate-ref-3"]
    report["expectedAttachmentIds"] = replacement
    report["actualAttachmentIds"] = replacement
    wrong_expected["portalSession"]["fields"]["attachments"] = replacement

    with pytest.raises(ValidationError, match="expectedAttachmentIds must match"):
        WorkflowSnapshot.model_validate(wrong_expected)

    wrong_actual = snapshot_data("verifying")
    wrong_actual["claimPacket"] = packet_for_state("verifying")
    wrong_actual["portalSession"] = portal_data()
    wrong_actual["verificationAttempts"] = verification_series_data()
    wrong_actual["portalSession"]["fields"]["attachments"] = [
        "local-ref-2",
        "local-ref-1",
        "local-ref-3",
    ]

    with pytest.raises(ValidationError, match="actualAttachmentIds must match"):
        WorkflowSnapshot.model_validate(wrong_actual)


def test_blocked_g8_allows_reported_rendered_attachment_divergence_only() -> None:
    valid = WorkflowSnapshot.model_validate(blocked_attachment_snapshot_data())
    assert valid.case.state == "blocked"
    assert valid.portal_session is not None
    assert valid.verification_attempts is not None
    final = valid.verification_attempts.attempts[-1]
    assert final.report.actual_attachment_ids != valid.portal_session.fields.attachments
    assert final.gate_decision is not None
    assert final.gate_decision.reason_codes == (
        GateReasonCode.G8_ATTACHMENT_MISMATCH,
    )

    noncanonical_raw_portal = blocked_attachment_snapshot_data()
    noncanonical_raw_portal["portalSession"]["fields"]["location"] = "Forged raw portal"
    with pytest.raises(ValidationError, match="canonical raw portal values"):
        WorkflowSnapshot.model_validate(noncanonical_raw_portal)

    detached_attempt = blocked_attachment_snapshot_data()
    detached_attempt["claimPacket"]["verification"]["actualAttachmentIds"] = [
        "packet-only-ref-1",
        "packet-only-ref-2",
        "packet-only-ref-3",
    ]
    with pytest.raises(ValidationError, match="exact final failed G8 authority"):
        WorkflowSnapshot.model_validate(detached_attempt)

    wrong_reason = blocked_attachment_snapshot_data()
    inconsistent_gate = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(GateReasonCode.G8_FIELD_MISMATCH,),
        decided_at=datetime(2026, 7, 14, 12, 0, 8, tzinfo=UTC),
    ).model_dump(mode="json", by_alias=True)
    wrong_reason["claimPacket"]["gateDecisions"][-1] = deepcopy(inconsistent_gate)
    wrong_reason["verificationAttempts"]["attempts"][-1]["gateDecision"] = deepcopy(
        inconsistent_gate
    )
    with pytest.raises(ValidationError, match="reasons must be derived"):
        WorkflowSnapshot.model_validate(wrong_reason)


def test_blocked_g8_requires_complete_clean_passed_gate_prefix() -> None:
    only_g8 = blocked_attachment_snapshot_data()
    only_g8["claimPacket"]["gateDecisions"] = [
        deepcopy(only_g8["claimPacket"]["gateDecisions"][-1])
    ]
    with pytest.raises(ValidationError, match="exact passed G0-G7 gate prefix"):
        WorkflowSnapshot.model_validate(only_g8)

    defective_prefix = blocked_attachment_snapshot_data()
    defective_prefix["claimPacket"]["gateDecisions"][3] = make_gate_decision(
        GateId.G3_SAFETY_SCOPE,
        deterministic_reasons=(GateReasonCode.G3_REAL_PORTAL,),
        model_blocked=True,
        decided_at=datetime(2026, 7, 14, 12, 0, 3, tzinfo=UTC),
    ).model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError, match="exact passed G0-G7 gate prefix"):
        WorkflowSnapshot.model_validate(defective_prefix)


def test_review_requires_bound_packet_portal_and_final_successful_g8() -> None:
    valid = WorkflowSnapshot.model_validate(review_snapshot_data())
    assert valid.case.state == "review"
    assert valid.verification_attempts is not None
    assert valid.verification_attempts.attempts[-1].gate_decision is not None

    for field in ("claimPacket", "portalSession", "verificationAttempts"):
        unsafe = review_snapshot_data()
        unsafe[field] = None
        with pytest.raises(ValidationError, match="review requires"):
            WorkflowSnapshot.model_validate(unsafe)

    failed = review_snapshot_data()
    failed["verificationAttempts"] = failed_verification_series_data()
    with pytest.raises(ValidationError, match="final successful G8"):
        WorkflowSnapshot.model_validate(failed)

    wrong_portal_state = review_snapshot_data()
    wrong_portal_state["portalSession"]["state"] = "draft"
    with pytest.raises(ValidationError):
        WorkflowSnapshot.model_validate(wrong_portal_state)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("location", "Berln", "exact portal values"),
        (
            "attachments",
            ["local-ref-1", "local-ref-2", "wrong-ref"],
            "actualAttachmentIds must match",
        ),
    ],
)
def test_review_rejects_portal_values_not_exactly_equal_to_packet(
    field: str,
    value: object,
    error: str,
) -> None:
    unsafe = review_snapshot_data()
    unsafe["portalSession"]["fields"][field] = value
    with pytest.raises(ValidationError, match=error):
        WorkflowSnapshot.model_validate(unsafe)


@pytest.mark.parametrize(
    "mutation",
    [
        "packet_case",
        "packet_state",
        "portal_case",
        "series_case",
        "portal_version",
        "nested_contract_version",
    ],
)
def test_review_rejects_nested_case_state_and_version_mismatch(mutation: str) -> None:
    unsafe = review_snapshot_data()
    if mutation == "packet_case":
        unsafe["claimPacket"]["caseId"] = "case-other"
    elif mutation == "packet_state":
        unsafe["claimPacket"]["state"] = "verifying"
    elif mutation == "portal_case":
        unsafe["portalSession"]["caseId"] = "case-other"
    elif mutation == "series_case":
        unsafe["verificationAttempts"]["caseId"] = "case-other"
        unsafe["verificationAttempts"]["attempts"][0]["caseId"] = "case-other"
    elif mutation == "portal_version":
        unsafe["portalSession"]["version"] = 4
    elif mutation == "nested_contract_version":
        unsafe["claimPacket"]["contractVersion"] = "3.0.0"
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(mutation)
    with pytest.raises(ValidationError):
        WorkflowSnapshot.model_validate(unsafe)


def test_receipt_snapshot_is_exclusive_redacted_and_case_bound() -> None:
    data = snapshot_data("receipt", version=9)
    data["receipt"] = receipt_data()
    snapshot = WorkflowSnapshot.model_validate(data)
    assert snapshot.receipt is not None and snapshot.receipt.redacted

    missing = snapshot_data("receipt", version=9)
    with pytest.raises(ValidationError, match="requires a redacted"):
        WorkflowSnapshot.model_validate(missing)

    for field, value in (
        ("claimPacket", receipt_packet_data()),
        ("portalSession", portal_data()),
        ("verificationAttempts", verification_series_data()),
    ):
        unsafe = deepcopy(data)
        unsafe[field] = value
        with pytest.raises(ValidationError, match="exposes only"):
            WorkflowSnapshot.model_validate(unsafe)

    wrong_case = deepcopy(data)
    wrong_case["receipt"]["caseId"] = "case-other"
    with pytest.raises(ValidationError, match="must match"):
        WorkflowSnapshot.model_validate(wrong_case)

    non_receipt = snapshot_data("human_approved")
    non_receipt["receipt"] = receipt_data()
    with pytest.raises(ValidationError, match="only in receipt"):
        WorkflowSnapshot.model_validate(non_receipt)


@pytest.mark.parametrize(
    "state",
    ["blocked", "emergency_stopped", "abandoned", "failed"],
)
def test_terminal_stop_snapshots_expose_no_active_actions(state: str) -> None:
    assert WorkflowSnapshot.model_validate(snapshot_data(state)).case.state == state

    unsafe = snapshot_data(state)
    unsafe["clarification"] = clarification_data()
    with pytest.raises(ValidationError, match="Terminal stop"):
        WorkflowSnapshot.model_validate(unsafe)


@pytest.mark.parametrize(
    "state",
    [
        "awaiting_clarification",
        "ready_to_fill",
        "filling",
        "verifying",
        "review",
        "human_approved",
    ],
)
def test_workflow_states_that_depend_on_extraction_require_a_packet(state: str) -> None:
    data = snapshot_data(state)
    if state == "awaiting_clarification":
        data["clarification"] = clarification_data()
    if state in {"verifying", "review"}:
        data["portalSession"] = portal_data()
    if state == "review":
        data["verificationAttempts"] = verification_series_data()
    with pytest.raises(ValidationError, match="requires a ClaimPacket"):
        WorkflowSnapshot.model_validate(data)


def test_portal_and_verification_payloads_follow_the_state_matrix() -> None:
    ready = snapshot_data("ready_to_fill")
    ready["claimPacket"] = packet_for_state("ready_to_fill")
    ready["portalSession"] = portal_data(state="draft")
    assert WorkflowSnapshot.model_validate(ready).portal_session is not None

    created_with_portal_and_series = snapshot_data("created")
    created_with_portal_and_series["portalSession"] = portal_data()
    created_with_portal_and_series["verificationAttempts"] = verification_series_data()
    with pytest.raises(ValidationError, match="cannot expose a PortalSessionView"):
        WorkflowSnapshot.model_validate(created_with_portal_and_series)

    created_with_series = snapshot_data("created")
    created_with_series["verificationAttempts"] = verification_series_data()
    with pytest.raises(ValidationError, match="cannot expose VerificationAttemptSeries"):
        WorkflowSnapshot.model_validate(created_with_series)

    approved_with_portal = snapshot_data("human_approved")
    approved_with_portal["claimPacket"] = packet_for_state("human_approved")
    approved_with_portal["portalSession"] = portal_data()
    with pytest.raises(ValidationError, match="cannot expose a PortalSessionView"):
        WorkflowSnapshot.model_validate(approved_with_portal)

    orphan_series = snapshot_data("failed")
    orphan_series["verificationAttempts"] = verification_series_data()
    with pytest.raises(ValidationError, match="requires a bound ClaimPacket"):
        WorkflowSnapshot.model_validate(orphan_series)

    verifying_with_draft = snapshot_data("verifying")
    verifying_with_draft["claimPacket"] = packet_for_state("verifying")
    verifying_with_draft["portalSession"] = portal_data(state="draft")
    with pytest.raises(ValidationError, match="requires portal state review"):
        WorkflowSnapshot.model_validate(verifying_with_draft)


def test_workflow_snapshot_is_closed_and_rejects_bool_case_version() -> None:
    with pytest.raises(ValidationError, match="extra"):
        WorkflowSnapshot.model_validate(
            {**snapshot_data("created"), "rawIntakeSummary": {"statement": "secret"}}
        )

    unsafe = snapshot_data("created")
    unsafe["case"]["version"] = True
    with pytest.raises(ValidationError):
        WorkflowSnapshot.model_validate(unsafe)

    for old_version in ("3.0.0", "2.1.0", "2.0.0", "1.0.0"):
        old = snapshot_data("created")
        old["contractVersion"] = old_version
        with pytest.raises(ValidationError):
            WorkflowSnapshot.model_validate(old)
