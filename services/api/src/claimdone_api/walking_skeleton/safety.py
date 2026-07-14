"""Conservative deterministic safety signals for the no-live-AI demo."""

import re

from claimdone_api.gates import (
    AdviceCategory,
    ModelSafetySignal,
    RequestedAction,
    SafetyInput,
)

_INJURY_TERM = (
    r"(?:injur(?:y|ies|ed)|hurt|bleed(?:ing|s)?|unconscious|wound(?:ed|s)?|"
    r"verletzt(?:e|en|er|em|es)?|verletzung(?:en)?|verwundet(?:e|en|er|em|es)?|"
    r"blutet|blutend|bewusstlos)"
)
_DANGER_TERM = (
    r"(?:fire|emergenc(?:y|ies)|danger|ambulance|"
    r"gefahr|notfall|feuer|krankenwagen|rettungsdienst)"
)
_INJURY = re.compile(rf"\b{_INJURY_TERM}\b", re.IGNORECASE)
_DANGER = re.compile(rf"\b{_DANGER_TERM}\b", re.IGNORECASE)

# Remove only closed, direct negations. Indirect uncertainty such as
# "not sure whether someone was injured" deliberately remains blocking.
_NEGATED_SAFETY_PHRASES = (
    re.compile(
        rf"\b(?:no|without)\s+(?:one\s+|any\s+)?{_INJURY_TERM}"
        rf"\s+(?:and|or)\s+(?:no\s+)?{_DANGER_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bkein(?:e|en|er|em|es)?\s+{_INJURY_TERM}"
        rf"\s+(?:und|oder)\s+(?:kein(?:e|en|er|em|es)?\s+)?{_DANGER_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:nobody|no\s+one)\s+(?:was\s+|is\s+|got\s+)?{_INJURY_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bniemand\s+(?:wurde\s+|ist\s+)?{_INJURY_TERM}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:no|without)\s+(?:one\s+|any\s+)?(?:{_INJURY_TERM}|{_DANGER_TERM})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bnot\s+(?:an?\s+)?(?:{_INJURY_TERM}|{_DANGER_TERM}|in\s+danger)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:kein(?:e|en|er|em|es)?|ohne)\s+"
        rf"(?:akut(?:e|en|er|em|es)?\s+)?(?:{_INJURY_TERM}|{_DANGER_TERM})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bnicht\s+(?:{_INJURY_TERM}|in\s+gefahr)\b",
        re.IGNORECASE,
    ),
)

_REAL_PORTAL = re.compile(
    r"\b(?:real|live|production)\s+(?:insurer|insurance)(?:\s+(?:website|portal))?\b|"
    r"\b(?:echte[nsr]?|reale[nsr]?|produktive[nsr]?)\s+"
    r"(?:versicherer|versicherung)(?:sportal|sseite|\s+portal)?\b",
    re.IGNORECASE,
)
_REAL_CREDENTIALS = re.compile(
    r"\b(?:real credentials|production credentials|echte zugangsdaten|produktive zugangsdaten)\b",
    re.IGNORECASE,
)
_LEGAL = re.compile(
    r"\b(?:legal|lawyer|liability|liable|rechtlich|anwalt|haftung|schuld)\b",
    re.IGNORECASE,
)
_PAYMENT = re.compile(
    r"\b(?:coverage|covered|payment|payout|damage amount|"
    r"deckung|gedeckt|zahlung|auszahlung|schadensh(?:oe|ö)he)\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(?:submit|approve|send|pay|book|contact|accept|"
    r"einreichen|absenden|senden|genehmigen|bezahlen|buchen|kontaktieren|akzeptieren)\b",
    re.IGNORECASE,
)


def _without_direct_negations(statement: str) -> str:
    safety_text = statement
    for pattern in _NEGATED_SAFETY_PHRASES:
        safety_text = pattern.sub(" ", safety_text)
    return safety_text


def deterministic_safety_input(
    statement: str,
    evidence_refs: tuple[str, ...],
    *,
    model_signal: ModelSafetySignal = ModelSafetySignal.SAFE,
) -> SafetyInput:
    """Map closed DE/EN phrases to add-only deterministic G3 inputs."""

    safety_text = _without_direct_negations(statement)
    advice: list[AdviceCategory] = []
    if _LEGAL.search(statement):
        advice.append(AdviceCategory.LEGAL)
    if _PAYMENT.search(statement):
        advice.append(AdviceCategory.COVERAGE)
    requested = (RequestedAction.SUBMIT,) if _ACTION.search(statement) else ()
    return SafetyInput(
        injury_reported=_INJURY.search(safety_text) is not None,
        immediate_danger=_DANGER.search(safety_text) is not None,
        portal_is_sandbox=_REAL_PORTAL.search(statement) is None,
        real_credentials_present=_REAL_CREDENTIALS.search(statement) is not None,
        advice_categories=tuple(advice),
        requested_actions=requested,
        model_signal=model_signal,
        evidence_refs=evidence_refs,
    )
