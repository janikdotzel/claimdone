"""G4 deterministic evidence, provenance, confidence, and narrative checks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import cast

from claimdone_api.contracts import (
    ClaimPacket,
    EvidenceFact,
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

_SENSITIVE_IMAGE_FIELDS = frozenset(
    {
        RequiredClaimField.LOCATION,
        RequiredClaimField.CLAIMANT_NAME,
        RequiredClaimField.POLICY_REFERENCE,
        RequiredClaimField.VEHICLE_REGISTRATION,
    }
)


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    """One assertion bound to a ClaimPacket fact or approved direct user evidence."""

    fact_id: str | None
    field: RequiredClaimField
    value: FieldValue
    status: FactStatus
    source_refs: tuple[str, ...]
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ProvenanceResult:
    decision: GateDecision
    writable_fields: tuple[RequiredClaimField, ...]


def evaluate_g4(
    packet: ClaimPacket,
    *,
    field_evidence: tuple[object, ...],
    decided_at: datetime | None = None,
) -> ProvenanceResult:
    """Check every proposed write against its canonical value and approved sources."""

    reasons: set[GateReasonCode] = set()
    grouped: dict[RequiredClaimField, list[FieldEvidence]] = defaultdict(list)
    malformed_evidence = False
    malformed_fields: set[RequiredClaimField] = set()
    for support in field_evidence:
        if not isinstance(support, FieldEvidence) or not isinstance(
            support.field, RequiredClaimField
        ):
            malformed_evidence = True
            continue
        if type(support.source_refs) is not tuple or any(
            type(source) is not str for source in support.source_refs
        ):
            malformed_evidence = True
            malformed_fields.add(support.field)
            continue
        if not _is_field_value(support.value):
            malformed_evidence = True
            malformed_fields.add(support.field)
        grouped[support.field].append(support)
    if malformed_evidence:
        reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)

    claim_json = packet.claim.model_dump(mode="json", by_alias=False)
    missing_fields = set(packet.claim.missing_required_fields)
    active_fields = tuple(field for field in RequiredClaimField if field not in missing_fields)
    claim_sources = {
        entry.field: tuple(entry.source_refs) for entry in packet.claim.field_provenance
    }
    provenance_by_id = {reference.provenance_id: reference for reference in packet.provenance}
    evidence_by_id = {item.evidence_id: item for item in packet.evidence}
    facts_by_id = {fact.fact_id: fact for fact in packet.facts}
    known_provenance = set(provenance_by_id)
    writable: list[RequiredClaimField] = []
    used_known_refs: list[str] = []

    for field in active_fields:
        supports = tuple(grouped.get(field, ()))
        if not supports:
            reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
            continue
        expected_sources = claim_sources.get(field, ())
        supplied_sources = tuple(
            source for support in supports for source in support.source_refs
        )
        sources_valid = (
            bool(expected_sources)
            and all(support.source_refs for support in supports)
            and all(
                len(set(support.source_refs)) == len(support.source_refs)
                for support in supports
            )
            and set(supplied_sources) == set(expected_sources)
            and set(supplied_sources) <= known_provenance
            and all(
                _source_kind(source, provenance_by_id, evidence_by_id) is not None
                for source in supplied_sources
            )
        )
        if sources_valid:
            used_known_refs.extend(supplied_sources)
        canonical_value = _canonical_value(field, claim_json)
        field_reasons = _reasons_for_field(
            field,
            supports=supports,
            canonical_value=canonical_value,
            sources_valid=sources_valid,
            provenance_by_id=provenance_by_id,
            evidence_by_id=evidence_by_id,
            facts_by_id=facts_by_id,
        )
        if field in malformed_fields:
            field_reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
        reasons.update(field_reasons)
        if not field_reasons:
            writable.append(field)

    if any(field in missing_fields for field in grouped):
        reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)

    decision = make_gate_decision(
        GateId.G4_PROVENANCE,
        deterministic_reasons=tuple(reasons),
        evidence_refs=tuple(dict.fromkeys(used_known_refs)),
        decided_at=decided_at,
    )
    return ProvenanceResult(decision=decision, writable_fields=tuple(writable))


def _reasons_for_field(
    field: RequiredClaimField,
    *,
    supports: tuple[FieldEvidence, ...],
    canonical_value: FieldValue,
    sources_valid: bool,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
    facts_by_id: Mapping[str, EvidenceFact],
) -> set[GateReasonCode]:
    """Derive field-local reasons so writable_fields cannot inherit global state."""

    reasons: set[GateReasonCode] = set()
    if not sources_valid:
        reasons.add(GateReasonCode.G4_PROVENANCE_MISSING)
    if field is RequiredClaimField.NARRATIVE:
        if any(
            support.status not in {FactStatus.OBSERVED, FactStatus.USER_STATED}
            for support in supports
        ):
            reasons.add(GateReasonCode.G4_NARRATIVE_UNSUPPORTED)
    else:
        supported = tuple(
            support
            for support in supports
            if support.status in {FactStatus.OBSERVED, FactStatus.USER_STATED}
        )
        if len({_strict_value_key(support.value) for support in supported}) > 1:
            reasons.add(GateReasonCode.G4_CONFLICTING_SOURCES)
        if not any(_same_value(support.value, canonical_value) for support in supported):
            reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
        if len(supported) != len(supports):
            reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
    seen_fact_ids: set[str] = set()
    for support in supports:
        binding_reason = _validate_support_binding(
            field,
            support=support,
            canonical_value=canonical_value,
            provenance_by_id=provenance_by_id,
            evidence_by_id=evidence_by_id,
            facts_by_id=facts_by_id,
        )
        if binding_reason is not None:
            reasons.add(binding_reason)
        if support.fact_id is not None:
            if support.fact_id in seen_fact_ids:
                reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
            seen_fact_ids.add(support.fact_id)
        if support.status is FactStatus.OBSERVED and (
            type(support.confidence) is not float
            or support.confidence < PROVENANCE_CONFIDENCE_THRESHOLD
            or support.confidence > 1.0
        ):
            reasons.add(GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD)
        if support.status is FactStatus.USER_STATED and support.confidence is not None:
            reasons.add(GateReasonCode.G4_FACT_NOT_WRITABLE)
    if field in _SENSITIVE_IMAGE_FIELDS and any(
        _source_is_image(source, provenance_by_id, evidence_by_id)
        for support in supports
        for source in support.source_refs
    ):
        reasons.add(GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE)
    return reasons


def _validate_support_binding(
    field: RequiredClaimField,
    *,
    support: FieldEvidence,
    canonical_value: FieldValue,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
    facts_by_id: Mapping[str, EvidenceFact],
) -> GateReasonCode | None:
    """Bind assertions to canonical facts or narrowly allowed direct user evidence."""

    if support.fact_id is not None:
        if type(support.fact_id) is not str:
            return GateReasonCode.G4_FACT_NOT_WRITABLE
        fact = facts_by_id.get(support.fact_id)
        if fact is None:
            return GateReasonCode.G4_PROVENANCE_MISSING
        if (
            fact.value != support.value
            or type(fact.value) is not type(support.value)
            or fact.status is not support.status
            or fact.source_refs != support.source_refs
            or fact.confidence != support.confidence
            or type(fact.confidence) is not type(support.confidence)
        ):
            return GateReasonCode.G4_FACT_NOT_WRITABLE
        if field is not RequiredClaimField.NARRATIVE and fact.field.value != field.value:
            return GateReasonCode.G4_FACT_NOT_WRITABLE
        return None

    source_kinds = {
        _source_kind(source, provenance_by_id, evidence_by_id)
        for source in support.source_refs
    }
    if None in source_kinds:
        return GateReasonCode.G4_PROVENANCE_MISSING
    if field is RequiredClaimField.ATTACHMENTS:
        allowed = source_kinds == {EvidenceKind.IMAGE}
    else:
        allowed = source_kinds <= {
            EvidenceKind.USER_STATEMENT,
            EvidenceKind.TRANSCRIPT,
            EvidenceKind.CLARIFICATION,
        }
    if (
        not allowed
        or support.status is not FactStatus.USER_STATED
        or support.confidence is not None
        or not _same_value(support.value, canonical_value)
    ):
        return GateReasonCode.G4_FACT_NOT_WRITABLE
    return None


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
    if isinstance(value, tuple):
        return ("tuple", tuple(repr(item) for item in value))
    if type(value) in {str, int, float, bool} or value is None:
        return (type(value).__name__, value)
    return (f"invalid-{type(value).__name__}", repr(value)[:200])


def _is_field_value(value: object) -> bool:
    if value is None or type(value) in {str, int, bool}:
        return True
    if type(value) is float:
        return isfinite(value)
    return type(value) is tuple and all(type(item) is str for item in value)


def _source_is_image(
    source_ref: str,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> bool:
    return _source_kind(source_ref, provenance_by_id, evidence_by_id) is EvidenceKind.IMAGE


def _source_kind(
    source_ref: str,
    provenance_by_id: Mapping[str, ProvenanceRef],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> EvidenceKind | None:
    reference = provenance_by_id.get(source_ref)
    evidence_id = getattr(reference, "evidence_id", None)
    evidence = evidence_by_id.get(evidence_id) if isinstance(evidence_id, str) else None
    if evidence is None or evidence.model_copy_approved is not True:
        return None
    kind = getattr(evidence, "kind", None)
    return kind if isinstance(kind, EvidenceKind) else None
