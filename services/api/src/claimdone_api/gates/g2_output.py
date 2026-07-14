"""G2 strict JSON/output-contract validation with one controlled retry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from claimdone_api.contracts import (
    ClaimPacket,
    EvidenceItem,
    GateDecision,
    GateId,
    GateReasonCode,
)

from .registry import make_gate_decision


@dataclass(frozen=True, slots=True)
class ModelOutputEnvelope:
    """Provider-normalized response state for one initial or retry attempt."""

    payload: str | bytes | None
    refusal: bool
    truncated: bool
    attempt: int


@dataclass(frozen=True, slots=True)
class OutputContractResult:
    """A G2 decision and packet exposed only when every check passes."""

    decision: GateDecision
    packet: ClaimPacket | None
    retry_allowed: bool
    attempt: int


class DuplicateJsonKey(ValueError):
    """Raised when JSON text tries to shadow an earlier object member."""


def evaluate_g2(
    envelope: ModelOutputEnvelope,
    *,
    approved_evidence: tuple[EvidenceItem, ...],
    decided_at: datetime | None = None,
) -> OutputContractResult:
    """Fail closed on transport, schema, reference, and retry-budget violations."""

    reasons: set[GateReasonCode] = set()
    if envelope.refusal is not False:
        reasons.add(GateReasonCode.G2_REFUSAL)
    if envelope.truncated is not False:
        reasons.add(GateReasonCode.G2_OUTPUT_TRUNCATED)

    packet = _strict_packet(envelope.payload)
    if packet is None:
        reasons.add(GateReasonCode.G2_SCHEMA_INVALID)
    elif not _matches_approved_evidence(packet, approved_evidence):
        reasons.add(GateReasonCode.G2_REFERENCE_MISSING)

    valid_attempt = type(envelope.attempt) is int and envelope.attempt in {0, 1}
    failed_before_budget = bool(reasons)
    if not valid_attempt or (failed_before_budget and envelope.attempt == 1):
        reasons.add(GateReasonCode.G2_RETRY_EXHAUSTED)

    decision = make_gate_decision(
        GateId.G2_OUTPUT_CONTRACT,
        deterministic_reasons=tuple(reasons),
        evidence_refs=(
            tuple(reference.provenance_id for reference in packet.provenance)
            if packet is not None and not reasons
            else ()
        ),
        decided_at=decided_at,
    )
    retry_allowed = not decision.passed and envelope.attempt == 0 and valid_attempt
    return OutputContractResult(
        decision=decision,
        packet=packet if decision.passed else None,
        retry_allowed=retry_allowed,
        attempt=envelope.attempt,
    )


def _strict_packet(payload: str | bytes | None) -> ClaimPacket | None:
    if not isinstance(payload, str | bytes) or type(payload) not in {str, bytes}:
        return None
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
        if type(value) is not dict:
            return None
        return ClaimPacket.model_validate(value)
    except (DuplicateJsonKey, UnicodeDecodeError, ValueError, TypeError, ValidationError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is forbidden: {value}")


def _matches_approved_evidence(
    packet: ClaimPacket,
    approved_evidence: tuple[EvidenceItem, ...],
) -> bool:
    if any(not isinstance(item, EvidenceItem) for item in approved_evidence):
        return False
    if any(item.model_copy_approved is not True for item in approved_evidence):
        return False
    approved_by_id = {item.evidence_id: item for item in approved_evidence}
    if len(approved_by_id) != len(approved_evidence):
        return False
    packet_by_id = {item.evidence_id: item for item in packet.evidence}
    if any(item.model_copy_approved is not True for item in packet_by_id.values()):
        return False
    if set(packet_by_id) != set(approved_by_id):
        return False
    return all(
        packet_by_id[evidence_id] == approved
        for evidence_id, approved in approved_by_id.items()
    )
