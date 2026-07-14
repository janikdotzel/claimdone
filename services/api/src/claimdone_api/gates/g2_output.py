"""G2 strict JSON/output-contract validation with one controlled retry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, ValidationError, model_validator

from claimdone_api.contracts import (
    ClaimData,
    EvidenceFact,
    EvidenceItem,
    EvidenceKind,
    GateDecision,
    GateId,
    GateReasonCode,
    ProvenanceRef,
)
from claimdone_api.contracts.base import ContractModel, ContractVersion

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
    """A G2 decision and extraction exposed only when every check passes."""

    decision: GateDecision
    extraction: ModelExtraction | None
    retry_allowed: bool
    attempt: int


class ModelExtraction(ContractModel):
    """Model-owned facts only; workflow, authority, gates, and verification are absent."""

    contract_version: ContractVersion
    evidence: Annotated[tuple[EvidenceItem, ...], Field(min_length=4)]
    provenance: Annotated[tuple[ProvenanceRef, ...], Field(min_length=1)]
    facts: tuple[EvidenceFact, ...]
    claim: ClaimData

    @model_validator(mode="after")
    def validate_extraction_references(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("Extraction evidence IDs must be unique")
        images = tuple(item for item in self.evidence if item.kind is EvidenceKind.IMAGE)
        if len(images) != 3:
            raise ValueError("Extraction requires exactly three image evidence items")
        if tuple(item.local_ref for item in images) != self.claim.attachments:
            raise ValueError("Extraction attachments must match image local refs")

        provenance_ids = tuple(reference.provenance_id for reference in self.provenance)
        if len(set(provenance_ids)) != len(provenance_ids):
            raise ValueError("Extraction provenance IDs must be unique")
        if any(reference.evidence_id not in evidence_ids for reference in self.provenance):
            raise ValueError("Extraction provenance must reference existing evidence")
        known_provenance = set(provenance_ids)
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("Extraction fact IDs must be unique")
        if any(
            source not in known_provenance for fact in self.facts for source in fact.source_refs
        ):
            raise ValueError("Extraction facts must reference existing provenance")
        if any(
            source not in known_provenance
            for field in self.claim.field_provenance
            for source in field.source_refs
        ):
            raise ValueError("Extraction claim fields must reference existing provenance")
        return self


class G2RunError(ValueError):
    """Raised when immutable G2 attempts are recorded out of scope or order."""


@dataclass(frozen=True, slots=True)
class OutputContractRun:
    """Immutable diagnostic attempts; only its final result enters gate history."""

    attempts: tuple[OutputContractResult, ...] = ()

    def __post_init__(self) -> None:
        if len(self.attempts) > 2:
            raise G2RunError("G2 allows one initial attempt and one retry")
        for index, result in enumerate(self.attempts):
            if result.decision.gate_id is not GateId.G2_OUTPUT_CONTRACT:
                raise G2RunError("A G2 run may contain only G2 decisions")
            if result.attempt != index:
                raise G2RunError("G2 attempts must be contiguous and zero-based")
            if (result.extraction is not None) is not result.decision.passed:
                raise G2RunError("G2 extraction exposure must match the decision")
            expected_retry = index == 0 and not result.decision.passed
            if result.retry_allowed is not expected_retry:
                raise G2RunError("G2 retry authority must be derived from the first result")
            if index:
                previous = self.attempts[index - 1]
                if previous.decision.passed or not previous.retry_allowed:
                    raise G2RunError("A final G2 attempt cannot be followed")
                if result.decision.decided_at < previous.decision.decided_at:
                    raise G2RunError("G2 attempt timestamps must be non-decreasing")

    def append(self, result: OutputContractResult) -> OutputContractRun:
        if len(self.attempts) >= 2:
            raise G2RunError("G2 allows one initial attempt and one retry")
        if result.attempt != len(self.attempts):
            raise G2RunError("G2 attempts must be contiguous and zero-based")
        if self.attempts:
            previous = self.attempts[-1]
            if previous.decision.passed or not previous.retry_allowed:
                raise G2RunError("The previous G2 attempt is final")
        return OutputContractRun(attempts=(*self.attempts, result))

    def accepts(self, attempt: object) -> bool:
        if type(attempt) is not int or attempt != len(self.attempts):
            return False
        if len(self.attempts) >= 2:
            return False
        return not self.attempts or (
            not self.attempts[-1].decision.passed
            and self.attempts[-1].retry_allowed
        )

    @property
    def final_result(self) -> OutputContractResult | None:
        if not self.attempts or self.attempts[-1].retry_allowed:
            return None
        return self.attempts[-1]


class DuplicateJsonKey(ValueError):
    """Raised when JSON text tries to shadow an earlier object member."""


def evaluate_g2(
    envelope: ModelOutputEnvelope,
    *,
    approved_evidence: tuple[EvidenceItem, ...],
    run: OutputContractRun | None = None,
    decided_at: datetime | None = None,
) -> OutputContractResult:
    """Fail closed on transport, schema, reference, and retry-budget violations."""

    reasons: set[GateReasonCode] = set()
    if envelope.refusal is not False:
        reasons.add(GateReasonCode.G2_REFUSAL)
    if envelope.truncated is not False:
        reasons.add(GateReasonCode.G2_OUTPUT_TRUNCATED)

    extraction = _strict_extraction(envelope.payload)
    if extraction is None:
        reasons.add(GateReasonCode.G2_SCHEMA_INVALID)
    elif not _matches_approved_evidence(extraction, approved_evidence):
        reasons.add(GateReasonCode.G2_REFERENCE_MISSING)

    active_run = run or OutputContractRun()
    valid_attempt = active_run.accepts(envelope.attempt)
    failed_before_budget = bool(reasons)
    if not valid_attempt or (failed_before_budget and envelope.attempt == 1):
        reasons.add(GateReasonCode.G2_RETRY_EXHAUSTED)

    decision = make_gate_decision(
        GateId.G2_OUTPUT_CONTRACT,
        deterministic_reasons=tuple(reasons),
        evidence_refs=(
            tuple(reference.provenance_id for reference in extraction.provenance)
            if extraction is not None and not reasons
            else ()
        ),
        decided_at=decided_at,
    )
    retry_allowed = not decision.passed and envelope.attempt == 0 and valid_attempt
    return OutputContractResult(
        decision=decision,
        extraction=extraction if decision.passed else None,
        retry_allowed=retry_allowed,
        attempt=envelope.attempt,
    )


def _strict_extraction(payload: str | bytes | None) -> ModelExtraction | None:
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
        return ModelExtraction.model_validate(value)
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
    extraction: ModelExtraction,
    approved_evidence: tuple[EvidenceItem, ...],
) -> bool:
    if any(not isinstance(item, EvidenceItem) for item in approved_evidence):
        return False
    if any(item.model_copy_approved is not True for item in approved_evidence):
        return False
    approved_by_id = {item.evidence_id: item for item in approved_evidence}
    if len(approved_by_id) != len(approved_evidence):
        return False
    extracted_by_id = {item.evidence_id: item for item in extraction.evidence}
    if any(item.model_copy_approved is not True for item in extracted_by_id.values()):
        return False
    if set(extracted_by_id) != set(approved_by_id):
        return False
    return all(
        extracted_by_id[evidence_id] == approved
        for evidence_id, approved in approved_by_id.items()
    )
