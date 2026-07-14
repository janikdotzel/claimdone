import json
import warnings
from copy import deepcopy
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient
from pydantic import ValidationError

from claimdone_api.contracts import AuditEvent, ClaimPacket, GateDecision

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
MISMATCH_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "mismatch.json"
CONTRACT_APP = FastAPI()


@CONTRACT_APP.post("/packet")
def accept_packet(packet: ClaimPacket) -> dict[str, str]:
    return {"caseId": packet.case_id}


def happy_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def gate_data() -> dict[str, Any]:
    return {
        "contractVersion": "1.0.0",
        "gateId": "G3",
        "deterministicPassed": True,
        "modelBlocked": False,
        "passed": True,
        "reasonCodes": [],
        "evidenceRefs": ["prov-statement"],
        "decidedAt": "2026-07-14T12:00:00Z",
    }


def audit_data() -> dict[str, Any]:
    return {
        "contractVersion": "1.0.0",
        "eventId": "audit-1",
        "caseId": "case-1",
        "eventType": "case_state_changed",
        "actor": "human",
        "occurredAt": "2026-07-14T12:00:00Z",
        "fromState": "review",
        "toState": "human_approved",
        "reasonCodes": [],
        "details": [],
    }


def test_json_wire_strings_validate_without_python_coercion() -> None:
    source = HAPPY_PATH.read_text(encoding="utf-8")

    packet = ClaimPacket.model_validate_json(source)

    assert packet.claim.incident_date == date(2026, 7, 14)
    assert packet.claim.incident_time == time(14, 30)
    assert packet.verification.verified_at is not None
    assert packet.verification.verified_at.utcoffset() is not None


def test_fastapi_parsed_json_dict_validates_without_unsafe_coercion() -> None:
    packet = ClaimPacket.model_validate(happy_data())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        response = FastAPITestClient(CONTRACT_APP).post("/packet", json=happy_data())

    assert packet.claim.incident_date == date(2026, 7, 14)
    assert response.status_code == 200
    assert response.json() == {"caseId": "case-happy-001"}


@pytest.mark.parametrize(
    "path",
    [
        ("claim", "incidentDate"),
        ("claim", "incidentTime"),
        ("gateDecisions", 0, "decidedAt"),
    ],
)
def test_fastapi_rejects_numeric_temporal_wire_values(path: tuple[str | int, ...]) -> None:
    data: Any = happy_data()
    target: Any = data
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = 0

    response = FastAPITestClient(CONTRACT_APP).post("/packet", json=data)

    assert response.status_code == 422


def test_temporal_fields_accept_exact_python_objects_for_internal_calls() -> None:
    data = happy_data()
    data["claim"]["incidentDate"] = date(2026, 7, 14)
    data["claim"]["incidentTime"] = time(14, 30)
    data["gateDecisions"][0]["decidedAt"] = datetime(2026, 7, 14, 12, tzinfo=UTC)

    packet = ClaimPacket.model_validate(data)

    assert packet.claim.incident_date == date(2026, 7, 14)
    assert packet.claim.incident_time == time(14, 30)
    assert packet.gate_decisions[0].decided_at == datetime(2026, 7, 14, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("path", "coerced_value"),
    [
        (("plan", "steps", 0, "sequence"), "1"),
        (("evidence", 0, "modelCopyApproved"), 1),
        (("facts", 0, "confidence"), "0.94"),
        (("scope", "agentCanSubmit"), 0),
        (("verification", "expectedAttachmentCount"), 3.0),
    ],
)
def test_unwanted_json_coercion_is_rejected(
    path: tuple[str | int, ...], coerced_value: object
) -> None:
    data: Any = deepcopy(happy_data())
    target: Any = data
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = coerced_value

    with pytest.raises(ValidationError):
        ClaimPacket.model_validate_json(json.dumps(data))


def test_agent_submission_boundary_rejects_true() -> None:
    data = happy_data()
    data["scope"]["agentCanSubmit"] = True

    with pytest.raises(ValidationError):
        ClaimPacket.model_validate_json(json.dumps(data))

    data = happy_data()
    data["plan"]["agentCanSubmit"] = True
    with pytest.raises(ValidationError):
        ClaimPacket.model_validate_json(json.dumps(data))


def test_wire_contract_rejects_snake_case_field_names() -> None:
    data = happy_data()
    data["contract_version"] = data.pop("contractVersion")

    with pytest.raises(ValidationError):
        ClaimPacket.model_validate(data)


def test_gate_evidence_refs_must_reference_existing_provenance() -> None:
    data = happy_data()
    data["gateDecisions"][0]["evidenceRefs"] = ["prov-does-not-exist"]

    with pytest.raises(ValidationError, match="gate evidence source"):
        ClaimPacket.model_validate(data)


def test_verification_source_refs_must_reference_existing_provenance() -> None:
    data = happy_data()
    data["verification"]["fieldResults"][0]["sourceRefs"] = ["prov-does-not-exist"]

    with pytest.raises(ValidationError, match="verification source"):
        ClaimPacket.model_validate(data)


def test_verification_expected_value_must_match_claim_data() -> None:
    data = happy_data()
    location_result = next(
        result for result in data["verification"]["fieldResults"] if result["field"] == "location"
    )
    location_result.update({"expected": "Munich", "actual": "Munich"})

    with pytest.raises(ValidationError, match="expected value must exactly match"):
        ClaimPacket.model_validate(data)


def test_verification_sources_must_match_claim_field_provenance() -> None:
    data = happy_data()
    incident_date_result = next(
        result
        for result in data["verification"]["fieldResults"]
        if result["field"] == "incident_date"
    )
    incident_date_result["sourceRefs"] = ["prov-statement"]

    with pytest.raises(ValidationError, match="must exactly match ClaimData"):
        ClaimPacket.model_validate(data)


def test_review_requires_no_missing_claim_fields() -> None:
    data = happy_data()
    data["claim"]["location"] = None
    data["claim"]["missingRequiredFields"] = ["location"]
    data["claim"]["fieldProvenance"] = [
        entry for entry in data["claim"]["fieldProvenance"] if entry["field"] != "location"
    ]

    with pytest.raises(ValidationError, match="require no missing required claim fields"):
        ClaimPacket.model_validate(data)


def test_observed_field_mismatch_cannot_claim_deterministic_match() -> None:
    data = cast(dict[str, Any], json.loads(MISMATCH_PATH.read_text(encoding="utf-8")))
    data["verification"]["deterministicMatch"] = True

    with pytest.raises(ValidationError, match="Partial verification"):
        ClaimPacket.model_validate(data)


@pytest.mark.parametrize(
    ("case_state", "portal_state"),
    [("created", "receipt"), ("verifying", "human_approved")],
)
def test_pre_review_case_cannot_claim_later_portal_state(
    case_state: str, portal_state: str
) -> None:
    data = happy_data()
    data["state"] = case_state
    data["portalState"] = portal_state
    data["verification"] = {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": [],
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }

    with pytest.raises(ValidationError, match="requires portal state"):
        ClaimPacket.model_validate(data)


def test_verifying_case_uses_portal_review_for_fresh_comparison() -> None:
    data = happy_data()
    data["state"] = "verifying"
    data["portalState"] = "review"
    data["gateDecisions"] = [
        decision for decision in data["gateDecisions"] if decision["gateId"] != "G8"
    ]
    data["verification"] = {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": [],
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }

    packet = ClaimPacket.model_validate(data)

    assert packet.state.value == "verifying"
    assert packet.portal_state.value == "review"


@pytest.mark.parametrize("case_state", ["blocked", "emergency_stopped", "abandoned", "failed"])
def test_stop_states_reject_human_approved_portal_state(case_state: str) -> None:
    data = happy_data()
    data["state"] = case_state
    data["portalState"] = "human_approved"
    data["verification"] = {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": [],
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }

    with pytest.raises(ValidationError, match="requires portal state"):
        ClaimPacket.model_validate(data)


def test_review_cannot_contain_a_failed_gate_decision() -> None:
    data = happy_data()
    g2_decision = next(decision for decision in data["gateDecisions"] if decision["gateId"] == "G2")
    g2_decision.update(
        {
            "deterministicPassed": False,
            "passed": False,
            "reasonCodes": ["G2_SCHEMA_INVALID"],
        }
    )

    with pytest.raises(ValidationError, match="cannot contain a failed gate"):
        ClaimPacket.model_validate(data)


def test_review_cannot_omit_a_required_gate_decision() -> None:
    data = happy_data()
    data["gateDecisions"] = [
        decision for decision in data["gateDecisions"] if decision["gateId"] != "G7"
    ]

    with pytest.raises(ValidationError, match="requires exact passed gate sequence"):
        ClaimPacket.model_validate(data)


def test_gate_cannot_override_deterministic_failure() -> None:
    data = gate_data()
    data.update(
        {
            "deterministicPassed": False,
            "passed": True,
            "reasonCodes": ["G3_SUBMISSION_ACTION"],
        }
    )

    with pytest.raises(ValidationError, match="deterministicPassed"):
        GateDecision.model_validate_json(json.dumps(data))


def test_model_signal_can_only_add_a_supported_block() -> None:
    data = gate_data()
    data.update(
        {
            "modelBlocked": True,
            "passed": False,
            "reasonCodes": ["G3_MODEL_UNCERTAIN"],
        }
    )

    decision = GateDecision.model_validate_json(json.dumps(data))

    assert decision.deterministic_passed is True
    assert decision.model_blocked is True
    assert decision.passed is False


def test_gate_decision_is_immutable() -> None:
    decision = GateDecision.model_validate_json(json.dumps(gate_data()))

    with pytest.raises(ValidationError, match="frozen"):
        decision.passed = False


def test_only_human_actor_can_transition_to_human_approved() -> None:
    event = AuditEvent.model_validate_json(json.dumps(audit_data()))
    assert event.actor.value == "human"

    data = audit_data()
    data["actor"] = "agent"
    with pytest.raises(ValidationError, match="human actor"):
        AuditEvent.model_validate_json(json.dumps(data))


def test_human_approval_event_rejects_agent_actor() -> None:
    data = audit_data()
    data.update(
        {
            "eventType": "human_approval",
            "actor": "agent",
            "fromState": None,
            "toState": None,
        }
    )

    with pytest.raises(ValidationError, match="human actor"):
        AuditEvent.model_validate_json(json.dumps(data))
