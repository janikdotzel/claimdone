"""Authority and negative-path coverage for the immutable G0-G5 registry."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    ClaimData,
    ClaimPacket,
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
    FieldEvidence,
    GateOrderError,
    ModelOutputEnvelope,
    ModelSafetySignal,
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


def valid_field_evidence(packet: ClaimPacket) -> tuple[FieldEvidence, ...]:
    claim_json = packet.claim.model_dump(mode="json", by_alias=False)
    sources = {entry.field: entry.source_refs for entry in packet.claim.field_provenance}
    missing = set(packet.claim.missing_required_fields)
    result: list[FieldEvidence] = []
    for field in RequiredClaimField:
        if field in missing:
            continue
        value: object = claim_json[field.value]
        if field is RequiredClaimField.ATTACHMENTS:
            assert isinstance(value, list)
            value = tuple(value)
        field_sources = sources[field]
        if field is RequiredClaimField.NARRATIVE:
            matching_facts = tuple(
                fact
                for fact in packet.facts
                if set(fact.source_refs) <= set(field_sources)
                and bool(set(fact.source_refs) & set(field_sources))
            )
        elif field is RequiredClaimField.ATTACHMENTS:
            matching_facts = ()
        else:
            matching_facts = tuple(
                fact
                for fact in packet.facts
                if fact.field.value == field.value
                and set(fact.source_refs) <= set(field_sources)
            )
        covered_sources: set[str] = set()
        for fact in matching_facts:
            covered_sources.update(fact.source_refs)
            result.append(
                FieldEvidence(
                    fact_id=fact.fact_id,
                    field=field,
                    value=fact.value,
                    status=fact.status,
                    source_refs=fact.source_refs,
                    confidence=fact.confidence,
                )
            )
        remaining_sources = tuple(
            source for source in field_sources if source not in covered_sources
        )
        if remaining_sources:
            result.append(
                FieldEvidence(
                    fact_id=None,
                    field=field,
                    value=cast(str | int | float | bool | None | tuple[str, ...], value),
                    status=FactStatus.USER_STATED,
                    source_refs=remaining_sources,
                    confidence=None,
                )
            )
    return tuple(result)


def incomplete_claim(field: RequiredClaimField = RequiredClaimField.LOCATION) -> ClaimData:
    data = deepcopy(happy_data()["claim"])
    wire_name = {
        RequiredClaimField.INCIDENT_DATE: "incidentDate",
        RequiredClaimField.INCIDENT_TIME: "incidentTime",
        RequiredClaimField.LOCATION: "location",
        RequiredClaimField.CLAIMANT_NAME: "claimantName",
        RequiredClaimField.POLICY_REFERENCE: "policyReference",
        RequiredClaimField.VEHICLE_REGISTRATION: "vehicleRegistration",
        RequiredClaimField.NARRATIVE: "narrative",
    }[field]
    data[wire_name] = None
    data["missingRequiredFields"] = [field.value]
    data["fieldProvenance"] = [
        item for item in data["fieldProvenance"] if item["field"] != field.value
    ]
    return ClaimData.model_validate(data)


@pytest.mark.parametrize(
    ("envelope", "inventory_transform", "reason"),
    [
        (
            ModelOutputEnvelope(happy_payload(), True, False, 0),
            lambda items: items,
            GateReasonCode.G2_REFUSAL,
        ),
        (
            ModelOutputEnvelope(happy_payload(), False, True, 0),
            lambda items: items,
            GateReasonCode.G2_OUTPUT_TRUNCATED,
        ),
        (
            ModelOutputEnvelope("{}", False, False, 0),
            lambda items: items,
            GateReasonCode.G2_SCHEMA_INVALID,
        ),
        (
            ModelOutputEnvelope(happy_payload(), False, False, 0),
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
    assert result.packet is None


def test_g2_accepts_only_strict_duplicate_free_json_and_exact_inventory() -> None:
    packet = happy_packet()
    result = evaluate_g2(
        ModelOutputEnvelope(happy_payload(), False, False, 0),
        approved_evidence=packet.evidence,
        decided_at=DECIDED_AT,
    )
    assert result.decision.passed and result.packet == packet
    assert not result.retry_allowed

    unknown = happy_data()
    unknown["unknownField"] = True
    duplicate = happy_payload().replace(
        '"contractVersion": "1.0.0",',
        '"contractVersion": "1.0.0", "contractVersion": "1.0.0",',
        1,
    )
    for payload in (json.dumps(unknown), duplicate, "[1, 2]", b"\xff"):
        rejected = evaluate_g2(
            ModelOutputEnvelope(payload, False, False, 0),
            approved_evidence=packet.evidence,
            decided_at=DECIDED_AT,
        )
        assert rejected.decision.reason_codes == (GateReasonCode.G2_SCHEMA_INVALID,)


def test_g2_external_inventory_detects_self_consistent_but_invented_evidence() -> None:
    packet = happy_packet()
    data = happy_data()
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
        ModelOutputEnvelope(json.dumps(data), False, False, 0),
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
    retry_success = evaluate_g2(
        ModelOutputEnvelope(happy_payload(), False, False, 1),
        approved_evidence=evidence,
        decided_at=DECIDED_AT,
    )
    forbidden_third = evaluate_g2(
        ModelOutputEnvelope(happy_payload(), False, False, 2),
        approved_evidence=evidence,
        decided_at=DECIDED_AT,
    )

    assert initial.retry_allowed
    assert retry_success.decision.passed and not retry_success.retry_allowed
    assert forbidden_third.decision.reason_codes == (GateReasonCode.G2_RETRY_EXHAUSTED,)
    assert forbidden_third.packet is None


def test_g2_multiple_failures_follow_registered_reason_priority() -> None:
    result = evaluate_g2(
        ModelOutputEnvelope(happy_payload(), True, True, 1),
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
    data = happy_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["confidence"] = 0.80
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert result.decision.passed
    assert result.writable_fields == tuple(RequiredClaimField)


def test_g4_provenance_missing_blocks() -> None:
    packet = happy_packet()
    supports = tuple(
        support
        for support in valid_field_evidence(packet)
        if support.field is not RequiredClaimField.LOCATION
    )

    result = evaluate_g4(packet, field_evidence=supports, decided_at=DECIDED_AT)

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
    data = happy_data()
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
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE in result.decision.reason_codes
    assert field not in result.writable_fields


@pytest.mark.parametrize("status", [FactStatus.UNKNOWN, FactStatus.NOT_SUPPORTED])
def test_g4_unknown_and_not_supported_facts_are_never_writable(status: FactStatus) -> None:
    data = happy_data()
    date_fact = next(fact for fact in data["facts"] if fact["factId"] == "fact-date")
    date_fact.update({"value": None, "status": status.value, "confidence": None})
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes
    assert RequiredClaimField.INCIDENT_DATE not in result.writable_fields


def test_g4_observed_confidence_below_point_eight_blocks_write() -> None:
    data = happy_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["confidence"] = 0.799999
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD in result.decision.reason_codes
    assert RequiredClaimField.NARRATIVE not in result.writable_fields


def test_g4_canonical_fact_binding_rejects_confidence_override() -> None:
    data = happy_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage["confidence"] = 0.50
    packet = ClaimPacket.model_validate(data)
    supports = valid_field_evidence(packet)
    forged = tuple(
        FieldEvidence(
            fact_id=support.fact_id,
            field=support.field,
            value=support.value,
            status=support.status,
            source_refs=support.source_refs,
            confidence=0.99,
        )
        if support.fact_id == "fact-damage"
        else support
        for support in supports
    )

    result = evaluate_g4(packet, field_evidence=forged, decided_at=DECIDED_AT)

    assert not result.decision.passed
    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes
    assert RequiredClaimField.NARRATIVE not in result.writable_fields


def test_g4_conflicting_supported_values_block_fill() -> None:
    data = happy_data()
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
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert GateReasonCode.G4_CONFLICTING_SOURCES in result.decision.reason_codes
    assert RequiredClaimField.LOCATION not in result.writable_fields


def test_g4_narrative_uses_only_observed_or_user_stated_support() -> None:
    data = happy_data()
    damage = next(fact for fact in data["facts"] if fact["factId"] == "fact-damage")
    damage.update({"value": None, "status": "unknown", "confidence": None})
    packet = ClaimPacket.model_validate(data)

    result = evaluate_g4(
        packet,
        field_evidence=valid_field_evidence(packet),
        decided_at=DECIDED_AT,
    )

    assert GateReasonCode.G4_NARRATIVE_UNSUPPORTED in result.decision.reason_codes
    assert RequiredClaimField.NARRATIVE not in result.writable_fields


def test_vin_cannot_enter_g4_because_it_is_absent_from_the_strict_contract() -> None:
    packet = happy_packet()
    data = happy_data()
    data["claim"]["vin"] = "INFERRED-FROM-IMAGE"
    with pytest.raises(ValidationError):
        ClaimPacket.model_validate(data)

    malformed = FieldEvidence(
        fact_id=None,
        field=cast(RequiredClaimField, "vin"),
        value="INFERRED-FROM-IMAGE",
        status=FactStatus.OBSERVED,
        source_refs=("prov-image-1",),
        confidence=0.99,
    )
    result = evaluate_g4(
        packet,
        field_evidence=(*valid_field_evidence(packet), malformed),
        decided_at=DECIDED_AT,
    )
    assert GateReasonCode.G4_FACT_NOT_WRITABLE in result.decision.reason_codes


def test_g5_complete_claim_passes_without_question() -> None:
    claim = happy_packet().claim
    result = evaluate_g5(
        claim,
        conflicting_fields=(),
        proposed_questions=(),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )

    assert compute_missing_required_fields(claim) == ()
    assert result.decision.passed
    assert result.blocking_fields == ()
    assert result.accepted_question is None


def test_g5_missing_field_accepts_only_one_question_for_next_blocker() -> None:
    claim = incomplete_claim()
    question = ClarificationQuestion(RequiredClaimField.LOCATION, "Where did it happen?")
    result = evaluate_g5(
        claim,
        conflicting_fields=(),
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
    result = evaluate_g5(
        incomplete_claim(),
        conflicting_fields=(),
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
    question = ClarificationQuestion(
        RequiredClaimField.INCIDENT_TIME,
        "Which incident time is correct?",
    )
    result = evaluate_g5(
        happy_packet().claim,
        conflicting_fields=(RequiredClaimField.INCIDENT_TIME,),
        proposed_questions=(question,),
        completed_rounds=1,
        decided_at=DECIDED_AT,
    )

    assert result.blocking_fields == (RequiredClaimField.INCIDENT_TIME,)
    assert result.accepted_question == question
    assert result.decision.reason_codes == (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)


def test_g5_third_round_is_last_and_then_manual_handoff_is_mandatory() -> None:
    claim = incomplete_claim()
    question = ClarificationQuestion(RequiredClaimField.LOCATION, "Where did it happen?")
    third = evaluate_g5(
        claim,
        conflicting_fields=(),
        proposed_questions=(question,),
        completed_rounds=2,
        decided_at=DECIDED_AT,
    )
    exhausted = evaluate_g5(
        claim,
        conflicting_fields=(),
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
    result = evaluate_g5(
        happy_packet().claim,
        conflicting_fields=(),
        proposed_questions=(
            ClarificationQuestion(RequiredClaimField.LOCATION, "Unnecessary?"),
        ),
        completed_rounds=0,
        decided_at=DECIDED_AT,
    )
    assert result.decision.reason_codes == (GateReasonCode.G5_QUESTION_INVALID,)


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
