"""Conservative deterministic safety signals for the no-live-AI demo."""

import re

from claimdone_api.gates import (
    AdviceCategory,
    ModelSafetySignal,
    RequestedAction,
    SafetyInput,
)

_INJURY = re.compile(r"\b(?:injur(?:y|ed)|hurt|bleeding|unconscious)\b", re.IGNORECASE)
_DANGER = re.compile(r"\b(?:fire|emergency|danger|ambulance)\b", re.IGNORECASE)
_LEGAL = re.compile(r"\b(?:legal|lawyer|liability|liable)\b", re.IGNORECASE)
_PAYMENT = re.compile(r"\b(?:coverage|covered|payment|payout|damage amount)\b", re.IGNORECASE)
_ACTION = re.compile(r"\b(?:submit|approve|send|pay|book|contact|accept)\b", re.IGNORECASE)


def deterministic_safety_input(statement: str, evidence_refs: tuple[str, ...]) -> SafetyInput:
    advice: list[AdviceCategory] = []
    if _LEGAL.search(statement):
        advice.append(AdviceCategory.LEGAL)
    if _PAYMENT.search(statement):
        advice.append(AdviceCategory.COVERAGE)
    requested = (RequestedAction.SUBMIT,) if _ACTION.search(statement) else ()
    return SafetyInput(
        injury_reported=_INJURY.search(statement) is not None,
        immediate_danger=_DANGER.search(statement) is not None,
        portal_is_sandbox=True,
        real_credentials_present=False,
        advice_categories=tuple(advice),
        requested_actions=requested,
        model_signal=ModelSafetySignal.SAFE,
        evidence_refs=evidence_refs,
    )
