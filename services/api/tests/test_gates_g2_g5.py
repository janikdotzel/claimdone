"""Authority and negative-path coverage for the immutable G0-G5 registry."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    ClaimPacket,
    EvidenceItem,
    FactStatus,
    GateDecision,
    GateId,
    GateReasonCode,
    RequiredClaimField,
)
from claimdone_api.gates import (
    G0_TO_G5_REGISTRY,
    AdviceCategory,
    ClarificationQuestion,
    ClarificationSubflow,
    ClarificationSubflowError,
    G2RunError,
    GateOrderError,
    ModelExtraction,
    ModelOutputEnvelope,
    ModelSafetySignal,
    OutputContractRun,
    RequestedAction,
    SafetyInput,
    compute_missing_required_fields,
    evaluate_g2,
    evaluate_g3,
    evaluate_g4,
    evaluate_g5,
    make_gate_decision,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
DECIDED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)


def happy_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def happy_packet() -> ClaimPacket:
    return ClaimPacket.model_validate(happy_data())


def happy_payload() -> str:
    return HAPPY_PATH.read_text(encoding="utf-8")


def extraction_data(source: dict[str, Any] | None = None) -> dict[str, Any]:
    data = source or happy_data()
    return {
        key: deepcopy(data[key])
        for key in ("contractVersion", "evidence", "provenance", "facts", "claim")
    }


def extraction_payload(source: dict[str, Any] | None = None) -> str:
    return json.dumps(extraction_data(source))


def safe_input(**updates: object) -> SafetyInput:
    values: dict[str, object] = {
        "injury_reported": False,
        "immediate_danger": False,
        "portal_is_sandbox": True,
        "real_credentials_present": False,
        "advice_categories": (),
        "requested_actions": (),
        "model_signal": ModelSafetySignal.SAFE,
        "evidence_refs": ("prov-statement",),
    }
    values.update(updates)
    return SafetyInput(**values)  # type: ignore[arg-type]


def complete_g4_data(source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add explicit canonical facts for every populated writable field."""

    data = deepcopy(source or happy_data())
    source_refs = {
        item["field"]: item["sourceRefs"] for item in data["claim"]["fieldProvenance"]
    }
    wire_names = {
        RequiredClaimField.INCIDENT_DATE: "incidentDate",
        RequiredClaimField.INCIDENT_TIME: "incidentTime",
        RequiredClaimField.LOCATION: "location",
        RequiredClaimField.CLAIMANT_NAME: "claimantName",
        RequiredClaimField.POLICY_REFERENCE: "policyReference",
        RequiredClaimField.VEHICLE_REGISTRATION: "vehicleRegistration",
        RequiredClaimField.COUNTERPARTY_KNOWN: "counterpartyKnown",
        RequiredClaimField.NARRATIVE: "narrative",
    }
    fact_fields = {fact["field"] for fact in data["facts"]}
    for field, wire_name in wire_names.items():
        value = data["claim"][wire_name]
        if value is None or field.value in fact_fields:
            continue
        data["facts"].append(
            {
                "factId": f"fact-canonical-{field.value.replace('_', '-')}",
                "field": field.value,
                "value": value,
                "status": "user_stated",
                "sourceRefs": source_refs[field.value],
                "confidence": None,
            }
        )
    return data


def g4_packet(source: dict[str, Any] | None = None) -> ClaimPacket:
    return ClaimPacket.model_validate(complete_g4_data(source))


def incomplete_packet(
    field: RequiredClaimField = RequiredClaimField.LOCATION,
) -> ClaimPacket:
    data = happy_data()
    wire_name = {
        RequiredClaimField.INCIDENT_DATE: "incidentDate",
        RequiredClaimField.INCIDENT_TIME: "incidentTime",
        RequiredClaimField.LOCATION: "location",
        RequiredClaimField.CLAIMANT_NAME: "claimantName",
        RequiredClaimField.POLICY_REFERENCE: "policyReference",
        RequiredClaimField.VEHICLE_REGISTRATION: "vehicleRegistration",
        RequiredClaimField.NARRATIVE: "narrative",
    }[field]
    data["state"] = "awaiting_clarification"
    data["portalState"] = "draft"
    data["gateDecisions"] = []
    data["claim"][wire_name] = None
    data["claim"]["missingRequiredFields"] = [field.value]
    data["claim"]["fieldProvenance"] = [
        item
        for item in data["claim"]["fieldProvenance"]
        if item["field"] != field.value
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
    return g4_packet(data)


@pytest.mark.parametrize(
    ("envelope", "inventory_transform", "reason"),
    [
        (
            ModelOutputEnvelope(extraction_payload(), True, False, 0),
            lambda items: items,
            GateReasonCode.G2_REFUSAL,
        ),
        (
            ModelOutputEnvelope(extraction_payload(), False, True, 0),
            lambda items: items,
            GateReasonCode.G2_OUTPUT_TRUNCATED,
        ),
        (
            ModelOutputEnvelope("{}", False, False, 0),
            lambda items: items,
            GateReasonCode.G2_SCHEMA_INVALID,
        ),
        (
            ModelOutputEnvelope(extraction_payload(), False, False, 0),
            lambda items: items[:-1],
            GateReasonCode.G2_REFERENCE_MISSING,
        ),
        (
            ModelOutputEnvelope("{}", False, False, 1),
            lambda items: items,
            GateReasonCode.G2_RETRY_EXHAUSTED,
        ),
    ],
)
def test_every_g2_reason_blocks_output(
    envelope: ModelOutputEnvelope,
    inventory_transform: object,
    reason: GateReasonCode,
) -> None:
    packet = happy_packet()
    assert callable(inventory_transform)
    result = evaluate_g2(
        envelope,
        approved_evidence=inventory_transform(packet.evidence),
        decided_at=DECIDED_AT,
    )

    assert not result.decision.passed
    assert reason in result.decision.reason_codes
    assert result.extraction is None


def test_g2_accepts_only_strict_duplicate_free_json_and_exact_inventory() -> None:
    packet = happy_packet()
    result = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), False, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )
    assert result.decision.passed
    assert result.extraction == ModelExtraction.model_validate(extraction_data())
    assert not result.retry_allowed

    unknown = extraction_data()
    unknown["unknownField"] = True
    duplicate = extraction_payload().replace(
        '"contractVersion": "3.0.0",',
        '"contractVersion": "3.0.0", "contractVersion": "3.0.0",',
        1,
    )
    for payload in (json.dumps(unknown), duplicate, "[1, 2]", b"\xff"):
        rejected = evaluate_g2(
            ModelOutputEnvelope(payload, False, False, 0),
            approved_evidence=packet.evidence,
            decided_at=DECIDED_AT,
        )
        assert rejected.decision.reason_codes == (GateReasonCode.G2_SCHEMA_INVALID,)


@pytest.mark.parametrize("confirmed", [False, None])
def test_g2_rejects_unconfirmed_transcript_in_payload_and_approved_catalog(
    confirmed: bool | None,
) -> None:
    unconfirmed = extraction_data()
    transcript = next(
        item for item in unconfirmed["evidence"] if item["kind"] == "user_statement"
    )
    transcript["kind"] = "transcript"
    transcript["transcriptConfirmed"] = confirmed
    approved_catalog = tuple(
        EvidenceItem.model_validate(item) for item in unconfirmed["evidence"]
    )

    with pytest.raises(ValidationError, match="human confirmation"):
        ModelExtraction.model_validate(unconfirmed)
    rejected_payload = evaluate_g2(
        ModelOutputEnvelope(json.dumps(unconfirmed), False, False, 0),
        approved_evidence=approved_catalog,
        decided_at=DECIDED_AT,
    )
    assert rejected_payload.decision.reason_codes == (
        GateReasonCode.G2_SCHEMA_INVALID,
    )

    confirmed_payload = deepcopy(unconfirmed)
    confirmed_transcript = next(
        item
        for item in confirmed_payload["evidence"]
        if item["kind"] == "transcript"
    )
    confirmed_transcript["transcriptConfirmed"] = True
    rejected_catalog = evaluate_g2(
        ModelOutputEnvelope(json.dumps(confirmed_payload), False, False, 0),
        approved_evidence=approved_catalog,
        decided_at=DECIDED_AT,
    )
    assert rejected_catalog.decision.reason_codes == (
        GateReasonCode.G2_REFERENCE_MISSING,
    )


def test_g2_external_inventory_detects_self_consistent_but_invented_evidence() -> None:
    packet = happy_packet()
    data = extraction_data()
    data["evidence"][0]["sha256"] = "f" * 64
    invented = json.dumps(data)

    result = evaluate_g2(
        ModelOutputEnvelope(invented, False, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G2_REFERENCE_MISSING,)


def test_g2_rejects_evidence_that_was_not_approved_for_model_use() -> None:
    data = happy_data()
    data["evidence"][0]["modelCopyApproved"] = False
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(data), False, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G2_REFERENCE_MISSING,)


def test_g2_allows_exactly_one_retry_and_stops_after_budget() -> None:
    evidence = happy_packet().evidence
    initial = evaluate_g2(
        ModelOutputEnvelope("{}", False, False, 0),
        approved_evidence=evidence,
        decided_at=DECIDED_AT,
    )
    run = OutputContractRun().append(initial)
    retry_success = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), False, False, 1),
        approved_evidence=evidence,
        run=run,
        decided_at=DECIDED_AT + timedelta(seconds=2),
    )
    completed_run = run.append(retry_success)
    forbidden_third = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), False, False, 2),
        approved_evidence=evidence,
        run=completed_run,
        decided_at=DECIDED_AT + timedelta(seconds=3),
    )

    assert initial.retry_allowed
    assert retry_success.decision.passed and not retry_success.retry_allowed
    assert completed_run.attempts == (initial, retry_success)
    final_result = completed_run.final_result
    assert final_result is not None
    assert final_result == retry_success
    assert not completed_run.attempts[0].decision.passed
    assert forbidden_third.decision.reason_codes == (GateReasonCode.G2_RETRY_EXHAUSTED,)
    assert forbidden_third.extraction is None
    with pytest.raises(G2RunError):
        completed_run.append(forbidden_third)

    authoritative: tuple[GateDecision, ...] = ()
    for offset, gate_id in enumerate((GateId.G0_INTAKE, GateId.G1_PRIVACY)):
        authoritative = G0_TO_G5_REGISTRY.append(
            authoritative,
            make_gate_decision(gate_id, decided_at=DECIDED_AT + timedelta(seconds=offset)),
        )
    authoritative = G0_TO_G5_REGISTRY.append(
        authoritative, final_result.decision
    )
    assert tuple(decision.gate_id for decision in authoritative) == (
        GateId.G0_INTAKE,
        GateId.G1_PRIVACY,
        GateId.G2_OUTPUT_CONTRACT,
    )


def test_g2_refusal_is_terminal_and_never_retryable() -> None:
    packet = happy_packet()
    refusal = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), True, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )

    assert refusal.decision.reason_codes == (GateReasonCode.G2_REFUSAL,)
    assert refusal.retry_allowed is False
    assert OutputContractRun().append(refusal).final_result == refusal


def test_g2_retry_cannot_be_started_without_the_failed_run() -> None:
    result = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), False, False, 1),
        approved_evidence=happy_packet().evidence,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G2_RETRY_EXHAUSTED,)
    assert result.extraction is None


def test_g2_model_authored_authority_fields_never_survive() -> None:
    packet = happy_packet()
    result = evaluate_g2(
        ModelOutputEnvelope(happy_payload(), False, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G2_SCHEMA_INVALID,)
    assert result.extraction is None


def test_g2_multiple_failures_follow_registered_reason_priority() -> None:
    result = evaluate_g2(
        ModelOutputEnvelope(extraction_payload(), True, True, 1),
        approved_evidence=(),
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (
        GateReasonCode.G2_REFUSAL,
        GateReasonCode.G2_OUTPUT_TRUNCATED,
        GateReasonCode.G2_REFERENCE_MISSING,
        GateReasonCode.G2_RETRY_EXHAUSTED,
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"injury_reported": True}, GateReasonCode.G3_INJURY_OR_EMERGENCY),
        ({"immediate_danger": True}, GateReasonCode.G3_INJURY_OR_EMERGENCY),
        ({"portal_is_sandbox": False}, GateReasonCode.G3_REAL_PORTAL),
        ({"real_credentials_present": True}, GateReasonCode.G3_REAL_PORTAL),
        (
            {"advice_categories": (AdviceCategory.LEGAL,)},
            GateReasonCode.G3_LEGAL_OR_LIABILITY,
        ),
        (
            {"advice_categories": (AdviceCategory.COVERAGE,)},
            GateReasonCode.G3_PAYMENT_OR_COVERAGE,
        ),
        (
            {"requested_actions": (RequestedAction.SUBMIT,)},
            GateReasonCode.G3_SUBMISSION_ACTION,
        ),
        (
            {"model_signal": ModelSafetySignal.UNCERTAIN},
            GateReasonCode.G3_MODEL_UNCERTAIN,
        ),
    ],
)
def test_every_g3_reason_blocks(updates: dict[str, object], reason: GateReasonCode) -> None:
    result = evaluate_g3(safe_input(**updates), decided_at=DECIDED_AT)

    assert not result.decision.passed
    assert reason in result.decision.reason_codes


def test_g3_safe_model_cannot_override_deterministic_failure() -> None:
    result = evaluate_g3(
        safe_input(
            injury_reported=True,
            portal_is_sandbox=False,
            model_signal=ModelSafetySignal.SAFE,
        ),
        decided_at=DECIDED_AT,
    )

    assert not result.decision.deterministic_passed
    assert not result.decision.model_blocked
    assert not result.decision.passed
    assert result.emergency_stop


def test_g3_model_can_only_add_a_block_and_uncertain_is_blocked() -> None:
    safe = evaluate_g3(safe_input(), decided_at=DECIDED_AT)
    blocked = evaluate_g3(
        safe_input(model_signal=ModelSafetySignal.BLOCKED), decided_at=DECIDED_AT
    )

    assert safe.decision.passed
    assert blocked.decision.deterministic_passed
    assert blocked.decision.model_blocked
    assert blocked.decision.reason_codes == (GateReasonCode.G3_MODEL_UNCERTAIN,)


def test_g3_malformed_boundary_signals_fail_closed() -> None:
    malformed = safe_input(
        injury_reported=cast(bool, 0),
        portal_is_sandbox=cast(bool, 1),
        requested_actions=cast(tuple[RequestedAction, ...], ("unknown",)),
        model_signal=cast(ModelSafetySignal, "unknown"),
    )

    result = evaluate_g3(malformed, decided_at=DECIDED_AT)

    assert not result.decision.passed
    assert result.decision.reason_codes == (
        GateReasonCode.G3_INJURY_OR_EMERGENCY,
        GateReasonCode.G3_REAL_PORTAL,
        GateReasonCode.G3_SUBMISSION_ACTION,
        GateReasonCode.G3_MODEL_UNCERTAIN,
    )


def test_g4_accepts_complete_supported_provenance_at_threshold() -> None:
    data = complete_g4_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["confidence"] = 0.80
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert result.decision.passed
    assert result.writable_fields == tuple(RequiredClaimField)
    assert result.blocked_fields == result.conflicting_fields == ()


def test_g4_requires_explicit_narrative_fact_even_with_legitimate_sources() -> None:
    result = evaluate_g4(happy_packet(), decided_at=DECIDED_AT)

    assert GateReasonCode.G4_NARRATIVE_UNSUPPORTED in result.decision.reason_codes
    assert not result.writable_fields


def test_g4_provenance_missing_blocks() -> None:
    data = complete_g4_data()
    location_entry = next(
        item
        for item in data["claim"]["fieldProvenance"]
        if item["field"] == RequiredClaimField.LOCATION.value
    )
    location_entry["sourceRefs"] = ["prov-image-1"]
    verification_entry = next(
        item
        for item in data["verification"]["fieldResults"]
        if item["field"] == RequiredClaimField.LOCATION.value
    )
    verification_entry["sourceRefs"] = ["prov-image-1"]
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_PROVENANCE_MISSING in result.decision.reason_codes
    assert RequiredClaimField.LOCATION not in result.writable_fields


@pytest.mark.parametrize(
    "field",
    [
        RequiredClaimField.LOCATION,
        RequiredClaimField.CLAIMANT_NAME,
        RequiredClaimField.POLICY_REFERENCE,
        RequiredClaimField.VEHICLE_REGISTRATION,
    ],
)
def test_g4_forbids_sensitive_identity_inference_from_images(
    field: RequiredClaimField,
) -> None:
    data = complete_g4_data()
    field_entry = next(
        item for item in data["claim"]["fieldProvenance"] if item["field"] == field.value
    )
    field_entry["sourceRefs"] = ["prov-image-1"]
    verification_entry = next(
        item
        for item in data["verification"]["fieldResults"]
        if item["field"] == field.value
    )
    verification_entry["sourceRefs"] = ["prov-image-1"]
    canonical_fact = next(fact for fact in data["facts"] if fact["field"] == field.value)
    canonical_fact["sourceRefs"] = ["prov-image-1"]
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE in result.decision.reason_codes
    assert field not in result.writable_fields


@pytest.mark.parametrize("status", [FactStatus.UNKNOWN, FactStatus.NOT_SUPPORTED])
def test_g4_unknown_and_not_supported_facts_are_never_writable(status: FactStatus) -> None:
    data = complete_g4_data()
    date_fact = next(fact for fact in data["facts"] if fact["factId"] == "fact-date")
    date_fact.update({"value": None, "status": status.value, "confidence": None})
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes
    assert RequiredClaimField.INCIDENT_DATE not in result.writable_fields


def test_g4_observed_confidence_below_point_eight_blocks_write() -> None:
    data = complete_g4_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["confidence"] = 0.799999
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD in result.decision.reason_codes
    assert RequiredClaimField.NARRATIVE not in result.writable_fields


def test_g4_accepts_no_caller_selected_fact_subset() -> None:
    with pytest.raises(TypeError):
        evaluate_g4(  # type: ignore[call-arg]
            g4_packet(), field_evidence=(), decided_at=DECIDED_AT
        )


def test_g4_omitted_canonical_fact_fails_closed() -> None:
    data = complete_g4_data()
    data["facts"] = [fact for fact in data["facts"] if fact["field"] != "location"]

    result = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)

    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes
    assert GateReasonCode.G4_PROVENANCE_MISSING in result.decision.reason_codes


def test_g4_audits_unmapped_unknown_facts_in_the_full_inventory() -> None:
    data = complete_g4_data()
    data["facts"].append(
        {
            "factId": "fact-unlisted-unknown",
            "field": "vehicle_count",
            "value": None,
            "status": "unknown",
            "sourceRefs": [],
            "confidence": None,
        }
    )

    result = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)

    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes
    assert not result.writable_fields


def test_g4_conflicting_supported_values_block_fill() -> None:
    data = complete_g4_data()
    data["facts"].append(
        {
            "factId": "fact-location-munich",
            "field": "location",
            "value": "Munich",
            "status": "user_stated",
            "sourceRefs": ["prov-statement"],
            "confidence": None,
        }
    )
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_CONFLICTING_SOURCES in result.decision.reason_codes
    assert RequiredClaimField.LOCATION not in result.writable_fields
    assert result.conflicting_fields == (RequiredClaimField.LOCATION,)


def test_g4_narrative_uses_only_observed_or_user_stated_support() -> None:
    data = complete_g4_data()
    narrative = next(fact for fact in data["facts"] if fact["field"] == "narrative")
    narrative.update({"value": None, "status": "unknown", "confidence": None})
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(packet, decided_at=DECIDED_AT)

    assert GateReasonCode.G4_NARRATIVE_UNSUPPORTED in result.decision.reason_codes
    assert RequiredClaimField.NARRATIVE not in result.writable_fields


def test_g4_narrative_requires_exact_canonical_text_and_source_union() -> None:
    data = complete_g4_data()
    narrative = next(fact for fact in data["facts"] if fact["field"] == "narrative")
    narrative["value"] = "A different narrative"
    mismatch = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)
    assert GateReasonCode.G4_NARRATIVE_UNSUPPORTED in mismatch.decision.reason_codes

    data = complete_g4_data()
    narrative = next(fact for fact in data["facts"] if fact["field"] == "narrative")
    narrative["sourceRefs"] = ["prov-statement"]
    incomplete_sources = evaluate_g4(
        ClaimPacket.model_validate(data), decided_at=DECIDED_AT
    )
    assert GateReasonCode.G4_PROVENANCE_MISSING in incomplete_sources.decision.reason_codes


def test_g4_wrong_field_fact_cannot_authorize_narrative_text() -> None:
    data = complete_g4_data()
    data["facts"] = [fact for fact in data["facts"] if fact["field"] != "narrative"]
    narrative = data["claim"]["narrative"]
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["value"] = narrative
    collision = next(fact for fact in data["facts"] if fact["factId"] == "fact-collision")
    collision["value"] = narrative

    result = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)

    assert GateReasonCode.G4_NARRATIVE_UNSUPPORTED in result.decision.reason_codes
    assert GateReasonCode.G4_PROVENANCE_MISSING in result.decision.reason_codes


def test_g4_fact_id_cannot_be_omitted_or_replaced_with_none() -> None:
    data = complete_g4_data()
    location = next(fact for fact in data["facts"] if fact["field"] == "location")
    location["factId"] = None

    with pytest.raises(ValidationError):
        ClaimPacket.model_validate(data)


def test_vin_cannot_enter_g4_because_it_is_absent_from_the_strict_contract() -> None:
    data = happy_data()
    data["claim"]["vin"] = "INFERRED-FROM-IMAGE"
    with pytest.raises(ValidationError):
        ClaimPacket.model_validate(data)


def test_g5_complete_claim_passes_without_question() -> None:
    provenance = evaluate_g4(g4_packet(), decided_at=DECIDED_AT)
    result = evaluate_g5(
        provenance,
        proposed_questions=(),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )

    assert compute_missing_required_fields(provenance.claim) == ()
    assert result.decision.passed
    assert result.blocking_fields == ()
    assert result.accepted_question is None


def test_g5_missing_field_accepts_only_one_question_for_next_blocker() -> None:
    provenance = evaluate_g4(incomplete_packet(), decided_at=DECIDED_AT)
    assert provenance.decision.passed
    question = ClarificationQuestion(RequiredClaimField.LOCATION, "Where did it happen?")
    result = evaluate_g5(
        provenance,
        proposed_questions=(question,),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)
    assert result.accepted_question == question
    assert result.blocking_fields == (RequiredClaimField.LOCATION,)


@pytest.mark.parametrize(
    "questions",
    [
        (),
        (
            ClarificationQuestion(RequiredClaimField.LOCATION, "Where?"),
            ClarificationQuestion(RequiredClaimField.LOCATION, "Which city?"),
        ),
        (ClarificationQuestion(RequiredClaimField.CLAIMANT_NAME, "Who?"),),
        (ClarificationQuestion(RequiredClaimField.LOCATION, "   "),),
    ],
)
def test_g5_rejects_omitted_multiple_wrong_or_empty_questions(
    questions: tuple[ClarificationQuestion, ...],
) -> None:
    provenance = evaluate_g4(incomplete_packet(), decided_at=DECIDED_AT)
    result = evaluate_g5(
        provenance,
        proposed_questions=questions,
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        GateReasonCode.G5_QUESTION_INVALID,
    )
    assert result.accepted_question is None


def test_g5_conflict_is_a_real_question_target() -> None:
    data = complete_g4_data()
    data["facts"].append(
        {
            "factId": "fact-time-conflict",
            "field": "incident_time",
            "value": "15:00:00",
            "status": "user_stated",
            "sourceRefs": ["prov-statement"],
            "confidence": None,
        }
    )
    provenance = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)
    question = ClarificationQuestion(
        RequiredClaimField.INCIDENT_TIME,
        "Which incident time is correct?",
    )
    result = evaluate_g5(
        provenance,
        proposed_questions=(question,),
        completed_rounds=1,
        decided_at=DECIDED_AT,
    )

    assert result.blocking_fields == (RequiredClaimField.INCIDENT_TIME,)
    assert result.accepted_question == question
    assert result.decision.reason_codes == (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)


def test_g5_third_round_is_last_and_then_manual_handoff_is_mandatory() -> None:
    provenance = evaluate_g4(incomplete_packet(), decided_at=DECIDED_AT)
    question = ClarificationQuestion(RequiredClaimField.LOCATION, "Where did it happen?")
    third = evaluate_g5(
        provenance,
        proposed_questions=(question,),
        completed_rounds=2,
        decided_at=DECIDED_AT,
    )
    exhausted = evaluate_g5(
        provenance,
        proposed_questions=(),
        completed_rounds=3,
        decided_at=DECIDED_AT,
    )

    assert third.accepted_question == question and third.rounds_remaining == 1
    assert GateReasonCode.G5_CLARIFICATION_LIMIT not in third.decision.reason_codes
    assert exhausted.decision.reason_codes == (
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        GateReasonCode.G5_CLARIFICATION_LIMIT,
    )
    assert exhausted.manual_handoff and exhausted.rounds_remaining == 0


def test_g5_extraneous_question_on_complete_claim_is_invalid() -> None:
    provenance = evaluate_g4(g4_packet(), decided_at=DECIDED_AT)
    result = evaluate_g5(
        provenance,
        proposed_questions=(
            ClarificationQuestion(RequiredClaimField.LOCATION, "Unnecessary?"),
        ),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )
    assert result.decision.reason_codes == (GateReasonCode.G5_QUESTION_INVALID,)


def test_g4_conflict_context_cannot_be_omitted_or_replaced_by_caller() -> None:
    data = complete_g4_data()
    data["facts"].append(
        {
            "factId": "fact-location-conflict",
            "field": "location",
            "value": "Munich",
            "status": "user_stated",
            "sourceRefs": ["prov-statement"],
            "confidence": None,
        }
    )
    provenance = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)
    assert provenance.conflicting_fields == (RequiredClaimField.LOCATION,)

    omitted = evaluate_g5(
        provenance,
        proposed_questions=(),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )
    replaced = evaluate_g5(
        provenance,
        proposed_questions=(
            ClarificationQuestion(RequiredClaimField.CLAIMANT_NAME, "Who?"),
        ),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )
    assert omitted.accepted_question is None
    assert replaced.accepted_question is None
    assert GateReasonCode.G5_QUESTION_INVALID in omitted.decision.reason_codes
    assert GateReasonCode.G5_QUESTION_INVALID in replaced.decision.reason_codes

    with pytest.raises(TypeError):
        evaluate_g5(  # type: ignore[call-arg]
            provenance,
            conflicting_fields=(),
            proposed_questions=(),
            completed_rounds=0,
        )


def test_g4_conflicts_on_a_missing_field_still_drive_the_g5_question() -> None:
    data = incomplete_packet().model_dump(mode="json", by_alias=True)
    data["facts"].extend(
        [
            {
                "factId": "fact-location-berlin",
                "field": "location",
                "value": "Berlin",
                "status": "user_stated",
                "sourceRefs": ["prov-statement"],
                "confidence": None,
            },
            {
                "factId": "fact-location-munich",
                "field": "location",
                "value": "Munich",
                "status": "user_stated",
                "sourceRefs": ["prov-statement"],
                "confidence": None,
            },
        ]
    )
    provenance = evaluate_g4(ClaimPacket.model_validate(data), decided_at=DECIDED_AT)
    question = ClarificationQuestion(
        RequiredClaimField.LOCATION, "Which incident location is correct?"
    )
    result = evaluate_g5(
        provenance,
        proposed_questions=(question,),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )

    assert not provenance.decision.passed
    assert provenance.conflicting_fields == (RequiredClaimField.LOCATION,)
    assert result.blocking_fields == (RequiredClaimField.LOCATION,)
    assert result.accepted_question == question


def test_g4_to_g5_clarification_is_diagnostic_and_bound_to_one_question() -> None:
    data = complete_g4_data()
    data["facts"].append(
        {
            "factId": "fact-location-conflict",
            "field": "location",
            "value": "Munich",
            "status": "user_stated",
            "sourceRefs": ["prov-statement"],
            "confidence": None,
        }
    )
    g4_conflict = evaluate_g4(
        ClaimPacket.model_validate(data), decided_at=DECIDED_AT + timedelta(seconds=4)
    )
    question = ClarificationQuestion(
        RequiredClaimField.LOCATION, "Which incident location is correct?"
    )
    diagnostic = evaluate_g5(
        g4_conflict,
        proposed_questions=(question,),
        completed_rounds=0,
        decided_at=DECIDED_AT + timedelta(seconds=5),
    )
    flow = ClarificationSubflow(g4_conflict, completed_rounds=0).append(diagnostic)

    assert flow.trigger == g4_conflict
    assert flow.diagnostic == diagnostic
    assert diagnostic.accepted_question == question
    assert diagnostic.blocking_fields[0] is RequiredClaimField.LOCATION
    with pytest.raises(ValueError, match="blockers must be recomputed"):
        replace(
            diagnostic,
            blocking_fields=(RequiredClaimField.CLAIMANT_NAME,),
        )
    with pytest.raises(ValueError, match="accepted question is not bound"):
        replace(
            diagnostic,
            accepted_question=ClarificationQuestion(
                RequiredClaimField.CLAIMANT_NAME, "Who?"
            ),
        )
    with pytest.raises(ClarificationSubflowError):
        flow.append(diagnostic)

    authoritative: tuple[GateDecision, ...] = ()
    for offset, gate_id in enumerate(
        (GateId.G0_INTAKE, GateId.G1_PRIVACY, GateId.G2_OUTPUT_CONTRACT, GateId.G3_SAFETY_SCOPE)
    ):
        authoritative = G0_TO_G5_REGISTRY.append(
            authoritative,
            make_gate_decision(gate_id, decided_at=DECIDED_AT + timedelta(seconds=offset)),
        )
    failed_history = G0_TO_G5_REGISTRY.append(authoritative, g4_conflict.decision)
    with pytest.raises(GateOrderError):
        G0_TO_G5_REGISTRY.append(failed_history, diagnostic.decision)

    resolved_g4 = evaluate_g4(
        g4_packet(), decided_at=DECIDED_AT + timedelta(seconds=4)
    )
    resolved_g5 = evaluate_g5(
        resolved_g4,
        proposed_questions=(),
        completed_rounds=1,
        decided_at=DECIDED_AT + timedelta(seconds=5),
    )
    resolved_history = G0_TO_G5_REGISTRY.append(authoritative, resolved_g4.decision)
    resolved_history = G0_TO_G5_REGISTRY.append(
        resolved_history, resolved_g5.decision
    )
    assert len(resolved_history) == 6
    assert all(decision.passed for decision in resolved_history)


def test_registry_is_contiguous_immutable_and_stops_after_failure() -> None:
    history: tuple[GateDecision, ...] = ()
    for offset, spec in enumerate(G0_TO_G5_REGISTRY.specs):
        decision = make_gate_decision(
            spec.gate_id,
            decided_at=DECIDED_AT + timedelta(seconds=offset),
        )
        previous = history
        history = G0_TO_G5_REGISTRY.append(history, decision)
        assert len(previous) == offset
    assert tuple(decision.gate_id for decision in history) == tuple(
        GateId(f"G{index}") for index in range(6)
    )

    with pytest.raises(FrozenInstanceError):
        G0_TO_G5_REGISTRY.specs[0].order = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        history[0].passed = False

    failed_g0 = make_gate_decision(
        GateId.G0_INTAKE,
        deterministic_reasons=(GateReasonCode.G0_CONSENT_MISSING,),
        decided_at=DECIDED_AT,
    )
    failed_history = G0_TO_G5_REGISTRY.append((), failed_g0)
    with pytest.raises(GateOrderError):
        G0_TO_G5_REGISTRY.append(
            failed_history,
            make_gate_decision(GateId.G1_PRIVACY, decided_at=DECIDED_AT),
        )


def test_registry_rejects_omitted_or_out_of_order_gates() -> None:
    with pytest.raises(GateOrderError):
        G0_TO_G5_REGISTRY.append(
            (),
            make_gate_decision(GateId.G2_OUTPUT_CONTRACT, decided_at=DECIDED_AT),
        )


def test_decision_constructor_owns_priority_and_rejects_pass_overrides() -> None:
    decision = make_gate_decision(
        GateId.G4_PROVENANCE,
        deterministic_reasons=(
            GateReasonCode.G4_NARRATIVE_UNSUPPORTED,
            GateReasonCode.G4_PROVENANCE_MISSING,
            GateReasonCode.G4_CONFLICTING_SOURCES,
        ),
        decided_at=DECIDED_AT,
    )
    assert decision.reason_codes == (
        GateReasonCode.G4_PROVENANCE_MISSING,
        GateReasonCode.G4_CONFLICTING_SOURCES,
        GateReasonCode.G4_NARRATIVE_UNSUPPORTED,
    )

    with pytest.raises(ValidationError):
        GateDecision.model_validate(
            {
                **decision.model_dump(mode="json", by_alias=True),
                "passed": True,
            }
        )
    with pytest.raises(ValueError, match="does not allow model"):
        make_gate_decision(
            GateId.G4_PROVENANCE,
            model_blocked=True,
            decided_at=DECIDED_AT,
        )
