"""Deterministic neutral narrative and G5-bound visible planning."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from claimdone_api.contracts import (
    AllowedTool,
    EvidenceFact,
    EvidenceField,
    FactStatus,
    PlanStep,
    ToolPlan,
)
from claimdone_api.gates import CompletenessResult

_SUPPORTED_STATUSES = frozenset({FactStatus.OBSERVED, FactStatus.USER_STATED})
_NARRATIVE_FIELD_ORDER = (
    EvidenceField.INCIDENT_DATE,
    EvidenceField.INCIDENT_TIME,
    EvidenceField.LOCATION,
    EvidenceField.VEHICLE_COUNT,
    EvidenceField.COLLISION_TYPE,
    EvidenceField.VISIBLE_DAMAGE,
    EvidenceField.IMPACT_AREA,
    EvidenceField.COUNTERPARTY_KNOWN,
)
_IMAGE_OBSERVABLE_FIELDS = frozenset(
    {
        EvidenceField.VEHICLE_COUNT,
        EvidenceField.COLLISION_TYPE,
        EvidenceField.VISIBLE_DAMAGE,
        EvidenceField.IMPACT_AREA,
    }
)
_USER_ONLY_FIELDS = frozenset(
    {
        EvidenceField.INCIDENT_DATE,
        EvidenceField.INCIDENT_TIME,
        EvidenceField.LOCATION,
        EvidenceField.COUNTERPARTY_KNOWN,
    }
)
_LABEL_BY_FIELD = {
    EvidenceField.INCIDENT_DATE: "the incident date as",
    EvidenceField.INCIDENT_TIME: "the incident time as",
    EvidenceField.LOCATION: "the location as",
    EvidenceField.VEHICLE_COUNT: "the vehicle count as",
    EvidenceField.COLLISION_TYPE: "the collision type as",
    EvidenceField.VISIBLE_DAMAGE: "visible damage described as",
    EvidenceField.IMPACT_AREA: "the visible impact area as",
    EvidenceField.COUNTERPARTY_KNOWN: "whether a counterparty is known as",
}
_FORBIDDEN_NARRATIVE_TERMS = re.compile(
    r"(?:liab|fault|negligen|guilt|responsib|other\s+driver\s+caus|legal|lawsuit|"
    r"lawyer|payment|payout|\bpay\b|coverage|compensat|\bcosts?\b|submit|submission|"
    r"approve|accepted|schuld|haft|verantwort|verursach|anwalt|klage|zahlung|"
    r"bezahl|kosten|deckung|einreich|genehmig)",
    flags=re.IGNORECASE,
)
_SAFE_TEXT = re.compile(r"^[\w\s,()/+\-:]+$", flags=re.UNICODE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$")


@dataclass(frozen=True, slots=True)
class NarrativeResult:
    """Neutral text plus the exact supported facts and provenance used to build it."""

    text: str | None
    fact_ids: tuple[str, ...]
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.text is None:
            if self.fact_ids or self.source_refs:
                raise ValueError("An empty narrative cannot claim fact or source support")
        elif not self.text.strip() or not self.fact_ids or not self.source_refs:
            raise ValueError("A narrative requires non-empty fact and source support")
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("Narrative fact IDs must be unique")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Narrative source refs must be unique")


def compose_neutral_narrative(facts: tuple[EvidenceFact, ...]) -> NarrativeResult:
    """Render fixed neutral sentences only from safe observed/user-stated facts."""

    if type(facts) is not tuple or any(not isinstance(fact, EvidenceFact) for fact in facts):
        raise ValueError("Narrative facts must be a tuple of canonical EvidenceFact values")
    by_field: dict[EvidenceField, list[EvidenceFact]] = defaultdict(list)
    for fact in facts:
        if fact.status not in _SUPPORTED_STATUSES or not fact.source_refs:
            continue
        if fact.status is FactStatus.OBSERVED and (
            fact.field not in _IMAGE_OBSERVABLE_FIELDS
            or fact.confidence is None
            or fact.confidence < 0.80
        ):
            continue
        if fact.status is FactStatus.USER_STATED and fact.field not in {
            *_IMAGE_OBSERVABLE_FIELDS,
            *_USER_ONLY_FIELDS,
        }:
            continue
        by_field[fact.field].append(fact)

    sentences: list[str] = []
    fact_ids: list[str] = []
    source_refs: list[str] = []
    for field in _NARRATIVE_FIELD_ORDER:
        candidates = by_field.get(field, [])
        safe_values = [(fact, _safe_fact_value(fact)) for fact in candidates]
        safe_values = [(fact, value) for fact, value in safe_values if value is not None]
        if not safe_values or len({value for _fact, value in safe_values}) != 1:
            continue
        fact, value = safe_values[0]
        assert value is not None
        lead = (
            "The supplied images show"
            if fact.status is FactStatus.OBSERVED
            else "The user reported"
        )
        sentences.append(f"{lead} {_LABEL_BY_FIELD[field]} {value}.")
        fact_ids.append(fact.fact_id)
        source_refs.extend(fact.source_refs)

    if not sentences:
        return NarrativeResult(text=None, fact_ids=(), source_refs=())
    text = " ".join(sentences)
    if _FORBIDDEN_NARRATIVE_TERMS.search(text):
        raise ValueError("Narrative safety invariant failed")
    return NarrativeResult(
        text=text,
        fact_ids=tuple(fact_ids),
        source_refs=tuple(dict.fromkeys(source_refs)),
    )


def build_visible_tool_plan(completeness: CompletenessResult) -> ToolPlan:
    """Build a value-free plan; only G5 may authorize a clarification step."""

    if not isinstance(completeness, CompletenessResult):
        raise ValueError("Planner requires the authoritative G5 completeness result")
    selections: list[tuple[AllowedTool, str]] = [
        (AllowedTool.INSPECT_EVIDENCE, "Inspect only the approved evidence inventory"),
        (AllowedTool.CHECK_REQUIRED_FIELDS, "Use the deterministic required-field result"),
    ]
    if completeness.accepted_question is not None:
        selections.append(
            (
                AllowedTool.ASK_CLARIFICATION,
                "Ask the single clarification accepted by G5",
            )
        )
    elif completeness.decision.passed and not completeness.blocking_fields:
        selections.extend(
            (
                (AllowedTool.INSPECT_FORM, "Inspect only the local sandbox form"),
                (AllowedTool.FILL_UNTIL_REVIEW, "Fill the sandbox only until review"),
                (
                    AllowedTool.VERIFY_RENDERED_FIELDS,
                    "Verify rendered fields before human review",
                ),
            )
        )

    return ToolPlan.model_validate(
        {
            "agentCanSubmit": False,
            "steps": tuple(
                PlanStep.model_validate(
                    {
                        "sequence": sequence,
                        "tool": tool.value,
                        "reason": reason,
                    }
                )
                for sequence, (tool, reason) in enumerate(selections, start=1)
            ),
        }
    )


def _safe_fact_value(fact: EvidenceFact) -> str | None:
    value = fact.value
    if fact.field is EvidenceField.VEHICLE_COUNT:
        return str(value) if type(value) is int and 1 <= value <= 20 else None
    if type(value) is bool:
        normalized = "yes" if value else "no"
    elif type(value) is str:
        normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    else:
        return None
    if not normalized or len(normalized) > 96:
        return None
    if fact.field is EvidenceField.INCIDENT_DATE and _DATE.fullmatch(normalized) is None:
        return None
    if fact.field is EvidenceField.INCIDENT_TIME and _TIME.fullmatch(normalized) is None:
        return None
    if _SAFE_TEXT.fullmatch(normalized) is None or _FORBIDDEN_NARRATIVE_TERMS.search(normalized):
        return None
    return normalized
