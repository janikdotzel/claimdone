"""G5 deterministic required-field and bounded clarification engine."""

from dataclasses import dataclass
from datetime import datetime

from claimdone_api.contracts import (
    ClaimData,
    GateDecision,
    GateId,
    GateReasonCode,
    RequiredClaimField,
)

from .registry import make_gate_decision

MAX_CLARIFICATION_ROUNDS = 3


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    field: RequiredClaimField
    text: str


@dataclass(frozen=True, slots=True)
class CompletenessResult:
    decision: GateDecision
    blocking_fields: tuple[RequiredClaimField, ...]
    accepted_question: ClarificationQuestion | None
    rounds_remaining: int
    manual_handoff: bool


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


def evaluate_g5(
    claim: ClaimData,
    *,
    conflicting_fields: tuple[RequiredClaimField, ...],
    proposed_questions: tuple[ClarificationQuestion, ...],
    completed_rounds: int,
    decided_at: datetime | None = None,
) -> CompletenessResult:
    """Allow one question for the highest-priority real blocker, at most three times."""

    reasons: set[GateReasonCode] = set()
    recomputed_missing = compute_missing_required_fields(claim)
    valid_conflicts = tuple(
        field for field in conflicting_fields if isinstance(field, RequiredClaimField)
    )
    conflict_input_valid = (
        len(valid_conflicts) == len(conflicting_fields)
        and len(set(valid_conflicts)) == len(valid_conflicts)
    )
    if not conflict_input_valid:
        reasons.add(GateReasonCode.G5_QUESTION_INVALID)
    blocker_set = set(recomputed_missing) | set(valid_conflicts)
    blocking_fields = tuple(field for field in RequiredClaimField if field in blocker_set)
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
        blocking_fields=blocking_fields,
        accepted_question=accepted_question,
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
    return (
        isinstance(question, ClarificationQuestion)
        and question.field is next_field
        and type(question.text) is str
        and bool(question.text.strip())
        and len(question.text) <= 512
    )
