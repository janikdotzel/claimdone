"""G3 deterministic safety/scope checks plus a model add-only signal."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from claimdone_api.contracts import GateDecision, GateId, GateReasonCode

from .registry import make_gate_decision


class AdviceCategory(StrEnum):
    LEGAL = "legal"
    LIABILITY = "liability"
    PAYMENT = "payment"
    COVERAGE = "coverage"
    DAMAGE_AMOUNT = "damage_amount"


class RequestedAction(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    SEND = "send"
    PAY = "pay"
    BOOK = "book"
    CONTACT = "contact"
    ACCEPT = "accept"


class ModelSafetySignal(StrEnum):
    SAFE = "safe"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SafetyInput:
    """Provider-independent facts; every deterministic boundary is explicit."""

    injury_reported: bool
    immediate_danger: bool
    portal_is_sandbox: bool
    real_credentials_present: bool
    advice_categories: tuple[AdviceCategory, ...]
    requested_actions: tuple[RequestedAction, ...]
    model_signal: ModelSafetySignal | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SafetyResult:
    decision: GateDecision
    emergency_stop: bool


def evaluate_g3(
    safety: SafetyInput,
    *,
    decided_at: datetime | None = None,
) -> SafetyResult:
    """Evaluate deterministic rules first; SAFE can never clear their reasons."""

    reasons: set[GateReasonCode] = set()
    emergency = safety.injury_reported is not False or safety.immediate_danger is not False
    if emergency:
        reasons.add(GateReasonCode.G3_INJURY_OR_EMERGENCY)
    if safety.portal_is_sandbox is not True or safety.real_credentials_present is not False:
        reasons.add(GateReasonCode.G3_REAL_PORTAL)

    advice = tuple(safety.advice_categories)
    if any(not isinstance(item, AdviceCategory) for item in advice):
        reasons.add(GateReasonCode.G3_LEGAL_OR_LIABILITY)
    if any(item in {AdviceCategory.LEGAL, AdviceCategory.LIABILITY} for item in advice):
        reasons.add(GateReasonCode.G3_LEGAL_OR_LIABILITY)
    if any(
        item in {AdviceCategory.PAYMENT, AdviceCategory.COVERAGE, AdviceCategory.DAMAGE_AMOUNT}
        for item in advice
    ):
        reasons.add(GateReasonCode.G3_PAYMENT_OR_COVERAGE)

    actions = tuple(safety.requested_actions)
    if actions and any(not isinstance(item, RequestedAction) for item in actions):
        reasons.add(GateReasonCode.G3_SUBMISSION_ACTION)
    if any(isinstance(item, RequestedAction) for item in actions):
        reasons.add(GateReasonCode.G3_SUBMISSION_ACTION)

    model_blocked = (
        safety.model_signal is not None
        and safety.model_signal is not ModelSafetySignal.SAFE
    )
    decision = make_gate_decision(
        GateId.G3_SAFETY_SCOPE,
        deterministic_reasons=tuple(reasons),
        model_blocked=model_blocked,
        evidence_refs=safety.evidence_refs,
        decided_at=decided_at,
    )
    return SafetyResult(decision=decision, emergency_stop=emergency)
