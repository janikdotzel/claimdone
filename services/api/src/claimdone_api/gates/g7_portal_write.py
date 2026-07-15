"""G7 full-payload portal-write authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from claimdone_api.contracts import (
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    GateReasonCode,
    PortalDraftFields,
    PortalState,
    RequiredClaimField,
)

from .registry import make_gate_decision

_WIRE_FIELD_BY_REQUIRED = {
    RequiredClaimField.INCIDENT_DATE: "incidentDate",
    RequiredClaimField.INCIDENT_TIME: "incidentTime",
    RequiredClaimField.LOCATION: "location",
    RequiredClaimField.CLAIMANT_NAME: "claimantName",
    RequiredClaimField.POLICY_REFERENCE: "policyReference",
    RequiredClaimField.VEHICLE_REGISTRATION: "vehicleRegistration",
    RequiredClaimField.COUNTERPARTY_KNOWN: "counterpartyKnown",
    RequiredClaimField.NARRATIVE: "narrative",
    RequiredClaimField.ATTACHMENTS: "attachments",
}
_PORTAL_FIELDS = frozenset(_WIRE_FIELD_BY_REQUIRED.values())
_SCALAR_FIELDS = tuple(
    field for field in RequiredClaimField if field is not RequiredClaimField.ATTACHMENTS
)


@dataclass(frozen=True, slots=True, repr=False)
class PortalWriteResult:
    """Immutable G7 outcome; rejected untrusted values are never retained."""

    decision: GateDecision
    fields: PortalDraftFields | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.decision.gate_id is not GateId.G7_PORTAL_WRITE:
            raise ValueError("PortalWriteResult requires a G7 decision")
        if self.decision.passed is not (self.fields is not None):
            raise ValueError("Only a passing G7 result may expose portal fields")


class PortalWriteInputError(ValueError):
    """A content-free failure for a forged or structurally invalid trusted packet."""

    def __init__(self) -> None:
        super().__init__("G7 portal-write input is invalid")


def evaluate_g7(
    fields_payload: object,
    *,
    packet: ClaimPacket,
    case_state: CaseState,
    portal_state: PortalState,
    decided_at: datetime | None = None,
) -> PortalWriteResult:
    """Authorize one exact atomic write derived entirely from the canonical packet."""

    canonical_packet = _revalidate_packet(packet)
    reasons: set[GateReasonCode] = set()
    payload = fields_payload if type(fields_payload) is dict else None
    if payload is None or set(payload) != _PORTAL_FIELDS:
        reasons.add(GateReasonCode.G7_FIELD_NOT_ALLOWED)

    expected = _canonical_portal_payload(canonical_packet)
    if payload is not None:
        for required_field in _SCALAR_FIELDS:
            wire_field = _WIRE_FIELD_BY_REQUIRED[required_field]
            if wire_field not in payload:
                continue
            actual_value = payload[wire_field]
            expected_value = expected[wire_field]
            if type(actual_value) is not str or actual_value != expected_value:
                reasons.add(GateReasonCode.G7_VALUE_NOT_FROM_PACKET)

        if "attachments" in payload:
            attachments = payload["attachments"]
            if (
                type(attachments) is not list
                or any(type(item) is not str for item in attachments)
                or tuple(attachments) != canonical_packet.claim.attachments
            ):
                reasons.add(GateReasonCode.G7_ATTACHMENT_MISMATCH)

    provenance_refs = _canonical_provenance_refs(canonical_packet)
    if provenance_refs is None:
        reasons.add(GateReasonCode.G7_PROVENANCE_MISSING)

    if (
        case_state is not CaseState.FILLING
        or portal_state is not PortalState.DRAFT
        or canonical_packet.state is not CaseState.FILLING
        or canonical_packet.portal_state is not PortalState.DRAFT
    ):
        reasons.add(GateReasonCode.G7_FIELD_NOT_EDITABLE)

    parsed_fields: PortalDraftFields | None = None
    if payload is not None and not reasons.intersection(
        {
            GateReasonCode.G7_FIELD_NOT_ALLOWED,
            GateReasonCode.G7_VALUE_NOT_FROM_PACKET,
            GateReasonCode.G7_ATTACHMENT_MISMATCH,
        }
    ):
        try:
            parsed_fields = PortalDraftFields.model_validate(payload)
        except ValidationError:
            reasons.add(GateReasonCode.G7_VALUE_NOT_FROM_PACKET)

    decision = make_gate_decision(
        GateId.G7_PORTAL_WRITE,
        deterministic_reasons=tuple(reasons),
        evidence_refs=provenance_refs or (),
        decided_at=decided_at,
    )
    return PortalWriteResult(
        decision=decision,
        fields=parsed_fields if decision.passed else None,
    )


def _revalidate_packet(packet: ClaimPacket) -> ClaimPacket:
    if not isinstance(packet, ClaimPacket):
        raise PortalWriteInputError from None
    try:
        return ClaimPacket.model_validate(packet.model_dump(mode="json", by_alias=True))
    except (ValidationError, TypeError, ValueError):
        raise PortalWriteInputError from None


def _canonical_portal_payload(packet: ClaimPacket) -> dict[str, object]:
    claim = packet.claim.model_dump(mode="json", by_alias=True)
    return {
        wire_field: claim[wire_field]
        for wire_field in _PORTAL_FIELDS
    }


def _canonical_provenance_refs(packet: ClaimPacket) -> tuple[str, ...] | None:
    entries = packet.claim.field_provenance
    if type(entries) is not tuple or len(entries) != len(RequiredClaimField):
        return None
    fields = tuple(entry.field for entry in entries)
    if len(set(fields)) != len(fields) or set(fields) != set(RequiredClaimField):
        return None
    entry_by_field = {entry.field: entry for entry in entries}

    provenance = packet.provenance
    if type(provenance) is not tuple:
        return None
    known_refs = tuple(reference.provenance_id for reference in provenance)
    if len(set(known_refs)) != len(known_refs):
        return None
    known = set(known_refs)

    refs: list[str] = []
    for required_field in RequiredClaimField:
        entry = entry_by_field[required_field]
        source_refs = entry.source_refs
        if (
            type(source_refs) is not tuple
            or not source_refs
            or len(set(source_refs)) != len(source_refs)
            or any(reference not in known for reference in source_refs)
        ):
            return None
        refs.extend(source_refs)
    return tuple(dict.fromkeys(refs))
