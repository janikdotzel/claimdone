"""G5 deterministic required-field and bounded clarification engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from claimdone_api.contracts import (
    ClaimData,
    GateDecision,
    GateId,
    GateReasonCode,
    RequiredClaimField,
)

from .g4_provenance import ProvenanceResult
from .registry import make_gate_decision

MAX_CLARIFICATION_ROUNDS = 3


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    field: RequiredClaimField
    text: str


@dataclass(frozen=True, slots=True)
class CompletenessResult:
    decision: GateDecision
    provenance_result: ProvenanceResult
    blocking_fields: tuple[RequiredClaimField, ...]
    conflicting_fields: tuple[RequiredClaimField, ...]
    accepted_question: ClarificationQuestion | None
    completed_rounds: int
    rounds_remaining: int
    manual_handoff: bool

    def __post_init__(self) -> None:
        if self.decision.gate_id is not GateId.G5_COMPLETENESS:
            raise ValueError("CompletenessResult requires a G5 decision")
        if self.decision.decided_at < self.provenance_result.decision.decided_at:
            raise ValueError("G5 cannot predate its G4 input")
        if self.conflicting_fields != self.provenance_result.conflicting_fields:
            raise ValueError("G5 conflict fields must be copied exactly from G4")
        expected_blockers = _derived_blocking_fields(self.provenance_result)
        if self.blocking_fields != expected_blockers:
            raise ValueError("G5 blockers must be recomputed from G4 and its claim")
        if type(self.completed_rounds) is not int or self.completed_rounds < -1:
            raise ValueError("G5 completed rounds use -1 only for invalid input")
        expected_remaining = (
            max(0, MAX_CLARIFICATION_ROUNDS - self.completed_rounds)
            if self.completed_rounds >= 0
            else 0
        )
        if self.rounds_remaining != expected_remaining:
            raise ValueError("G5 remaining rounds must be derived")
        limit_reached = (
            self.completed_rounds < 0
            or self.completed_rounds >= MAX_CLARIFICATION_ROUNDS
        )
        if self.manual_handoff is not (bool(expected_blockers) and limit_reached):
            raise ValueError("G5 manual handoff must be derived")

        if self.accepted_question is not None and not _accepted_question_matches(
            self.accepted_question,
            next_field=expected_blockers[0] if expected_blockers else None,
            limit_reached=limit_reached,
        ):
            raise ValueError("G5 accepted question is not bound to the first blocker")

        reasons = set(self.decision.reason_codes)
        if expected_blockers:
            if self.decision.passed or GateReasonCode.G5_REQUIRED_FIELD_MISSING not in reasons:
                raise ValueError("G5 blockers require a failed required-field decision")
            if (
                self.accepted_question is None
                and not limit_reached
                and GateReasonCode.G5_QUESTION_INVALID not in reasons
            ):
                raise ValueError("An unasked available clarification must be invalid")
            if self.accepted_question is not None and reasons != {
                GateReasonCode.G5_REQUIRED_FIELD_MISSING
            }:
                raise ValueError("An accepted clarification has exactly one blocking reason")
        elif self.accepted_question is not None:
            raise ValueError("A complete claim cannot accept a clarification question")
        elif GateReasonCode.G5_REQUIRED_FIELD_MISSING in reasons:
            raise ValueError("A complete claim cannot report a required-field blocker")


class ClarificationSubflowError(ValueError):
    """Raised when a G4 conflict diagnostic is detached or replayed."""


@dataclass(frozen=True, slots=True)
class ClarificationSubflow:
    """One immutable G4-conflict to G5-question diagnostic, outside gate history."""

    trigger: ProvenanceResult
    completed_rounds: int
    diagnostic: CompletenessResult | None = None

    def __post_init__(self) -> None:
        if self.trigger.decision.gate_id is not GateId.G4_PROVENANCE:
            raise ClarificationSubflowError("Clarification must be triggered by G4")
        if self.trigger.decision.passed or not self.trigger.conflicting_fields:
            raise ClarificationSubflowError(
                "Clarification requires a failed G4 with derived conflict fields"
            )
        if (
            type(self.completed_rounds) is not int
            or self.completed_rounds < 0
            or self.completed_rounds >= MAX_CLARIFICATION_ROUNDS
        ):
            raise ClarificationSubflowError("Clarification round is outside the budget")
        if self.diagnostic is not None:
            self._validate_diagnostic(self.diagnostic)

    def append(self, result: CompletenessResult) -> ClarificationSubflow:
        if self.diagnostic is not None:
            raise ClarificationSubflowError("A G4 attempt permits exactly one G5 question")
        self._validate_diagnostic(result)
        return ClarificationSubflow(
            trigger=self.trigger,
            completed_rounds=self.completed_rounds,
            diagnostic=result,
        )

    def _validate_diagnostic(self, result: CompletenessResult) -> None:
        if result.decision.gate_id is not GateId.G5_COMPLETENESS:
            raise ClarificationSubflowError("Clarification diagnostic must be G5")
        if result.provenance_result != self.trigger:
            raise ClarificationSubflowError("G5 diagnostic is not bound to this G4 result")
        if result.decision.decided_at < self.trigger.decision.decided_at:
            raise ClarificationSubflowError(
                "G5 diagnostic cannot predate its G4 trigger"
            )
        if result.completed_rounds != self.completed_rounds:
            raise ClarificationSubflowError("G5 diagnostic uses the wrong round")
        if result.accepted_question is None or result.manual_handoff:
            raise ClarificationSubflowError("G5 diagnostic must accept exactly one question")
        if (
            not result.blocking_fields
            or result.accepted_question.field is not result.blocking_fields[0]
        ):
            raise ClarificationSubflowError("Question must target the first derived blocker")


def compute_missing_required_fields(claim: ClaimData) -> tuple[RequiredClaimField, ...]:
    """Recompute missing fields from values instead of trusting a model/UI flag."""

    value_by_field: dict[RequiredClaimField, object] = {
        RequiredClaimField.INCIDENT_DATE: claim.incident_date,
        RequiredClaimField.INCIDENT_TIME: claim.incident_time,
        RequiredClaimField.LOCATION: claim.location,
        RequiredClaimField.CLAIMANT_NAME: claim.claimant_name,
        RequiredClaimField.POLICY_REFERENCE: claim.policy_reference,
        RequiredClaimField.VEHICLE_REGISTRATION: claim.vehicle_registration,
        RequiredClaimField.COUNTERPARTY_KNOWN: claim.counterparty_known,
        RequiredClaimField.NARRATIVE: claim.narrative,
        RequiredClaimField.ATTACHMENTS: claim.attachments,
    }
    return tuple(
        field
        for field in RequiredClaimField
        if value_by_field[field] is None
        or (field is RequiredClaimField.ATTACHMENTS and len(claim.attachments) != 3)
    )


def _derived_blocking_fields(
    provenance_result: ProvenanceResult,
) -> tuple[RequiredClaimField, ...]:
    blocker_set = set(compute_missing_required_fields(provenance_result.claim)) | set(
        provenance_result.conflicting_fields
    )
    return tuple(field for field in RequiredClaimField if field in blocker_set)


def evaluate_g5(
    provenance_result: ProvenanceResult,
    *,
    proposed_questions: tuple[ClarificationQuestion, ...],
    completed_rounds: int,
    decided_at: datetime | None = None,
) -> CompletenessResult:
    """Use G4-derived conflicts and recomputed missing fields; accept no caller subset."""

    reasons: set[GateReasonCode] = set()
    if provenance_result.decision.gate_id is not GateId.G4_PROVENANCE:
        raise ValueError("G5 requires an authoritative G4 result")
    derived_conflicts = provenance_result.conflicting_fields
    if len(set(derived_conflicts)) != len(derived_conflicts):
        raise ValueError("G4 conflict fields must be unique")
    if not provenance_result.decision.passed and not derived_conflicts:
        raise ValueError("Only a G4 conflict failure may enter G5 diagnostics")
    blocking_fields = _derived_blocking_fields(provenance_result)
    if blocking_fields:
        reasons.add(GateReasonCode.G5_REQUIRED_FIELD_MISSING)

    rounds_valid = type(completed_rounds) is int and completed_rounds >= 0
    limit_reached = not rounds_valid or completed_rounds >= MAX_CLARIFICATION_ROUNDS
    if not rounds_valid:
        reasons.add(GateReasonCode.G5_QUESTION_INVALID)
    elif blocking_fields and limit_reached:
        reasons.add(GateReasonCode.G5_CLARIFICATION_LIMIT)

    question_valid = _question_is_valid(
        proposed_questions,
        next_field=blocking_fields[0] if blocking_fields else None,
        limit_reached=limit_reached,
    )
    if not question_valid:
        reasons.add(GateReasonCode.G5_QUESTION_INVALID)
    accepted_question = proposed_questions[0] if question_valid and proposed_questions else None
    decision = make_gate_decision(
        GateId.G5_COMPLETENESS,
        deterministic_reasons=tuple(reasons),
        decided_at=decided_at,
    )
    rounds_remaining = (
        max(0, MAX_CLARIFICATION_ROUNDS - completed_rounds)
        if rounds_valid
        else 0
    )
    return CompletenessResult(
        decision=decision,
        provenance_result=provenance_result,
        blocking_fields=blocking_fields,
        conflicting_fields=derived_conflicts,
        accepted_question=accepted_question,
        completed_rounds=completed_rounds if rounds_valid else -1,
        rounds_remaining=rounds_remaining,
        manual_handoff=bool(blocking_fields) and limit_reached,
    )


def _question_is_valid(
    questions: tuple[ClarificationQuestion, ...],
    *,
    next_field: RequiredClaimField | None,
    limit_reached: bool,
) -> bool:
    if next_field is None:
        return not questions
    if limit_reached:
        return not questions
    if len(questions) != 1:
        return False
    question = questions[0]
    return _accepted_question_matches(
        question,
        next_field=next_field,
        limit_reached=limit_reached,
    )


def _accepted_question_matches(
    question: object,
    *,
    next_field: RequiredClaimField | None,
    limit_reached: bool,
) -> bool:
    return (
        not limit_reached
        and next_field is not None
        and isinstance(question, ClarificationQuestion)
        and question.field is next_field
        and type(question.text) is str
        and bool(question.text.strip())
        and len(question.text) <= 512
    )
