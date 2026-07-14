"""G4 deterministic evidence, provenance, confidence, and narrative checks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from claimdone_api.contracts import (
    ClaimData,
    ClaimPacket,
    EvidenceFact,
    EvidenceField,
    EvidenceItem,
    EvidenceKind,
    FactStatus,
    GateDecision,
    GateId,
    GateReasonCode,
    ProvenanceRef,
    RequiredClaimField,
)

from .registry import make_gate_decision

PROVENANCE_CONFIDENCE_THRESHOLD = 0.80

type FieldValue = str | int | float | bool | None | tuple[str, ...]

_SUPPORTED_STATUSES = frozenset({FactStatus.OBSERVED, FactStatus.USER_STATED})
_SENSITIVE_IMAGE_FIELDS = frozenset(
    {
        RequiredClaimField.LOCATION,
        RequiredClaimField.CLAIMANT_NAME,
        RequiredClaimField.POLICY_REFERENCE,
        RequiredClaimField.VEHICLE_REGISTRATION,
    }
)


@dataclass(frozen=True, slots=True)
class ProvenanceResult:
    decision: GateDecision
    claim: ClaimData
    writable_fields: tuple[RequiredClaimField, ...]
    blocked_fields: tuple[RequiredClaimField, ...]
    conflicting_fields: tuple[RequiredClaimField, ...]

    def __post_init__(self) -> None:
        if self.decision.gate_id is not GateId.G4_PROVENANCE:
            raise ValueError("ProvenanceResult requires a G4 decision")
        active_fields = tuple(
            field
            for field in RequiredClaimField
            if field not in set(self.claim.missing_required_fields)
        )
        if len(set(self.conflicting_fields)) != len(self.conflicting_fields):
            raise ValueError("G4 conflicting fields must be unique")
        if RequiredClaimField.ATTACHMENTS in self.conflicting_fields:
            raise ValueError("Attachments cannot be represented as scalar fact conflicts")
        if self.decision.passed:
            if self.writable_fields != active_fields or self.blocked_fields:
                raise ValueError("A passed G4 must expose every populated field as writable")
            if self.conflicting_fields:
                raise ValueError("A passed G4 cannot contain conflict fields")
        elif self.writable_fields or self.blocked_fields != active_fields:
            raise ValueError("A failed G4 is a transaction-wide write barrier")


def evaluate_g4(
    packet: ClaimPacket,
    *,
    decided_at: datetime | None = None,
) -> ProvenanceResult:
    """Derive all G4 inputs from the complete packet; callers cannot select a subset."""

    claim_json = packet.claim.model_dump(mode="json", by_alias=False)
    missing_fields = set(packet.claim.missing_required_fields)
    active_fields = tuple(field for field in RequiredClaimField if field not in missing_fields)
    claim_sources = {
        entry.field: tuple(entry.source_refs) for entry in packet.claim.field_provenance
    }
    provenance_by_id = {reference.provenance_id: reference for reference in packet.provenance}
    evidence_by_id = {item.evidence_id: item for item in packet.evidence}
    reasons, audited_conflicts = _audit_complete_fact_inventory(
        packet.facts,
        provenance_by_id=provenance_by_id,
        evidence_by_id=evidence_by_id,
    )
    field_reasons_by_field: dict[RequiredClaimField, set[GateReasonCode]] = {}
    valid_evidence_refs: list[str] = []
    for field in active_fields:
        expected_sources = claim_sources.get(field, ())
        field_reasons = _evaluate_field(
            field,
            canonical_value=_canonical_value(field, claim_json),
            expected_sources=expected_sources,
            facts=packet.facts,
            provenance_by_id=provenance_by_id,
            evidence_by_id=evidence_by_id,
        )
        field_reasons_by_field[field] = field_reasons
        reasons.update(field_reasons)
        if not field_reasons:
            valid_evidence_refs.extend(expected_sources)

    audited_conflict_values = {field.value for field in audited_conflicts}
    conflicting_fields = tuple(
        field
        for field in RequiredClaimField
        if field is not RequiredClaimField.ATTACHMENTS
        if field.value in audited_conflict_values
        or (
            field in field_reasons_by_field
            and GateReasonCode.G4_CONFLICTING_SOURCES
            in field_reasons_by_field[field]
        )
    )
    # G4 is a transaction-wide write barrier. A global bad fact or any field
    # failure makes every populated field non-writable until the packet is rerun.
    writable_fields = active_fields if not reasons else ()
    blocked_fields = () if not reasons else active_fields

    decision = make_gate_decision(
        GateId.G4_PROVENANCE,
        deterministic_reasons=tuple(reasons),
        evidence_refs=tuple(dict.fromkeys(valid_evidence_refs)),
        decided_at=decided_at,
    )
    return ProvenanceResult(
        decision=decision,
        claim=packet.claim,
        writable_fields=writable_fields,
        blocked_fields=blocked_fields,
        conflicting_fields=conflicting_fields,
    )


def _audit_complete_fact_inventory(
    facts: tuple[EvidenceFact, ...],
    *,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> tuple[set[GateReasonCode], set[EvidenceField]]:
    """Inspect every packet fact, including facts no caller chose to expose."""

    reasons: set[GateReasonCode] = set()
    supported_by_field: dict[EvidenceField, list[EvidenceFact]] = defaultdict(list)
    for fact in facts:
        sources_valid = (
            bool(fact.source_refs)
            and len(set(fact.source_refs)) == len(fact.source_refs)
            and all(
                _source_kind(source, provenance_by_id, evidence_by_id) is not None
                for source in fact.source_refs
            )
        )
        if fact.status in _SUPPORTED_STATUSES and not sources_valid:
            reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
        if fact.status not in _SUPPORTED_STATUSES:
            if fact.field is EvidenceField.NARRATIVE:
                reasons.add(GateReasonCode.G4_NARRATIVE_UNSUPPORTED)
            else:
                reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
            continue
        supported_by_field[fact.field].append(fact)
        if fact.status is FactStatus.OBSERVED and (
            fact.confidence is None
            or fact.confidence < PROVENANCE_CONFIDENCE_THRESHOLD
        ):
            reasons.add(GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD)

    conflicting_fields: set[EvidenceField] = set()
    for field, field_facts in supported_by_field.items():
        if len({_strict_value_key(fact.value) for fact in field_facts}) > 1:
            reasons.add(GateReasonCode.G4_CONFLICTING_SOURCES)
            conflicting_fields.add(field)
    return reasons, conflicting_fields


def _evaluate_field(
    field: RequiredClaimField,
    *,
    canonical_value: FieldValue,
    expected_sources: tuple[str, ...],
    facts: tuple[EvidenceFact, ...],
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> set[GateReasonCode]:
    reasons: set[GateReasonCode] = set()
    if not expected_sources or len(set(expected_sources)) != len(expected_sources):
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
        return reasons
    source_kinds = {
        source: _source_kind(source, provenance_by_id, evidence_by_id)
        for source in expected_sources
    }
    if any(kind is None for kind in source_kinds.values()):
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)

    if field in _SENSITIVE_IMAGE_FIELDS and EvidenceKind.IMAGE in source_kinds.values():
        reasons.add(GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE)

    if field is RequiredClaimField.ATTACHMENTS:
        if (
            len(expected_sources) != 3
            or set(source_kinds.values()) != {EvidenceKind.IMAGE}
            or not _attachments_match_sources(
                canonical_value,
                expected_sources=expected_sources,
                provenance_by_id=provenance_by_id,
                evidence_by_id=evidence_by_id,
            )
        ):
            reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
        return reasons

    field_facts = tuple(fact for fact in facts if fact.field.value == field.value)
    if any(not set(fact.source_refs) <= set(expected_sources) for fact in field_facts):
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)

    if field is RequiredClaimField.NARRATIVE:
        reasons.update(
            _evaluate_narrative(
                canonical_value,
                field_facts=field_facts,
                expected_sources=expected_sources,
            )
        )
        return reasons

    supported_facts = tuple(fact for fact in field_facts if fact.status in _SUPPORTED_STATUSES)
    if len(supported_facts) != len(field_facts):
        reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
    if not supported_facts:
        reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
    else:
        values = {_strict_value_key(fact.value) for fact in supported_facts}
        if len(values) > 1:
            reasons.add(GateReasonCode.G4_CONFLICTING_SOURCES)
        if not all(_same_value(fact.value, canonical_value) for fact in supported_facts):
            reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)

    fact_sources = {source for fact in field_facts for source in fact.source_refs}
    if fact_sources != set(expected_sources):
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
    return reasons


def _evaluate_narrative(
    canonical_value: FieldValue,
    *,
    field_facts: tuple[EvidenceFact, ...],
    expected_sources: tuple[str, ...],
) -> set[GateReasonCode]:
    reasons: set[GateReasonCode] = set()
    if type(canonical_value) is not str or not canonical_value.strip():
        return {GateReasonCode.G4_NARRATIVE_UNSUPPORTED}

    if not field_facts or any(
        fact.status not in _SUPPORTED_STATUSES
        or not _same_value(fact.value, canonical_value)
        for fact in field_facts
    ):
        reasons.add(GateReasonCode.G4_NARRATIVE_UNSUPPORTED)
    narrative_sources = {source for fact in field_facts for source in fact.source_refs}
    if narrative_sources != set(expected_sources):
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
    return reasons


def _attachments_match_sources(
    canonical_value: FieldValue,
    *,
    expected_sources: tuple[str, ...],
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> bool:
    if not isinstance(canonical_value, tuple):
        return False
    local_refs: list[str] = []
    for source in expected_sources:
        reference = provenance_by_id.get(source)
        if reference is None:
            return False
        evidence = evidence_by_id.get(reference.evidence_id)
        if evidence is None or evidence.kind is not EvidenceKind.IMAGE:
            return False
        local_refs.append(evidence.local_ref)
    return tuple(local_refs) == canonical_value


def _canonical_value(field: RequiredClaimField, claim_json: dict[str, object]) -> FieldValue:
    value = claim_json[field.value]
    if field is RequiredClaimField.ATTACHMENTS:
        if not isinstance(value, list) or any(type(item) is not str for item in value):
            raise ValueError("Canonical attachments must be a JSON string array")
        return tuple(value)
    if value is None or type(value) in {str, int, float, bool}:
        return cast(FieldValue, value)
    raise ValueError("Canonical claim value is not a supported strict field value")


def _same_value(left: object, right: FieldValue) -> bool:
    return type(left) is type(right) and left == right


def _strict_value_key(value: object) -> tuple[str, object]:
    return (type(value).__name__, value)


def _source_kind(
    source_ref: str,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> EvidenceKind | None:
    reference = provenance_by_id.get(source_ref)
    if reference is None:
        return None
    evidence = evidence_by_id.get(reference.evidence_id)
    if evidence is None or evidence.model_copy_approved is not True:
        return None
    return evidence.kind
