"""Deterministic neutral narrative and G5-bound visible planning."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time

from claimdone_api.contracts import (
    AllowedTool,
    EvidenceFact,
    EvidenceField,
    EvidenceItem,
    EvidenceKind,
    FactStatus,
    PlanStep,
    ProvenanceRef,
    ToolPlan,
)
from claimdone_api.gates import CompletenessResult

_SUPPORTED_STATUSES = frozenset({FactStatus.OBSERVED, FactStatus.USER_STATED})
_NARRATIVE_FIELD_ORDER = (
    EvidenceField.INCIDENT_DATE,
    EvidenceField.INCIDENT_TIME,
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
        EvidenceField.COUNTERPARTY_KNOWN,
    }
)
_LABEL_BY_FIELD = {
    EvidenceField.INCIDENT_DATE: "the incident date as",
    EvidenceField.INCIDENT_TIME: "the incident time as",
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
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$")
_COLLISION_TYPE_TEXT = {
    "rear_end": "a rear-end collision",
    "front_impact": "a front-impact collision",
    "side_impact": "a side-impact collision",
}
_VISIBLE_DAMAGE_TEXT = {
    "front_bumper_dent": "a dent in the front bumper",
    "front_bumper_scrape": "a scrape on the front bumper",
    "rear_bumper_dent": "a dent in the rear bumper",
    "rear_bumper_scrape": "a scrape on the rear bumper",
    "none_visible": "no visible damage",
}
_IMPACT_AREA_TEXT = {
    "front_bumper": "the front bumper",
    "front_left_door": "the front-left door",
    "front_right_door": "the front-right door",
    "rear_bumper": "the rear bumper",
    "rear_left_door": "the rear-left door",
    "rear_right_door": "the rear-right door",
}
_COUNTERPARTY_TEXT = {
    "yes": "yes",
    "no": "no",
    "unknown": "unknown",
}


@dataclass(frozen=True, slots=True)
class NarrativeInput:
    """Canonical facts bound to the exact provenance and evidence inventory."""

    facts: tuple[EvidenceFact, ...] = field(repr=False)
    provenance: tuple[ProvenanceRef, ...] = field(repr=False)
    evidence: tuple[EvidenceItem, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.facts) is not tuple or any(
            not isinstance(fact, EvidenceFact) for fact in self.facts
        ):
            raise ValueError("Narrative facts must be canonical EvidenceFact values")
        if type(self.provenance) is not tuple or any(
            not isinstance(reference, ProvenanceRef) for reference in self.provenance
        ):
            raise ValueError("Narrative provenance must use canonical ProvenanceRef values")
        if type(self.evidence) is not tuple or any(
            not isinstance(item, EvidenceItem) for item in self.evidence
        ):
            raise ValueError("Narrative evidence must use canonical EvidenceItem values")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        provenance_ids = tuple(reference.provenance_id for reference in self.provenance)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("Narrative evidence IDs must be unique")
        if len(set(provenance_ids)) != len(provenance_ids):
            raise ValueError("Narrative provenance IDs must be unique")
        if any(reference.evidence_id not in evidence_ids for reference in self.provenance):
            raise ValueError("Narrative provenance must reference canonical evidence")


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


def compose_neutral_narrative(request: NarrativeInput) -> NarrativeResult:
    """Render fixed sentences from allowlisted values with canonical support."""

    if not isinstance(request, NarrativeInput):
        raise ValueError("Narrative composition requires a canonical bound input")
    provenance = {reference.provenance_id: reference for reference in request.provenance}
    evidence = {item.evidence_id: item for item in request.evidence}
    by_field: dict[EvidenceField, list[EvidenceFact]] = defaultdict(list)
    for fact in request.facts:
        if fact.status not in _SUPPORTED_STATUSES or not fact.source_refs:
            continue
        if not _sources_support_status(fact, provenance=provenance, evidence=evidence):
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
    for evidence_field in _NARRATIVE_FIELD_ORDER:
        candidates = by_field.get(evidence_field, [])
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
        sentences.append(f"{lead} {_LABEL_BY_FIELD[evidence_field]} {value}.")
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
    if fact.field is EvidenceField.COUNTERPARTY_KNOWN:
        if type(value) is bool:
            return "yes" if value else "no"
        return _COUNTERPARTY_TEXT.get(value) if type(value) is str else None
    if type(value) is not str:
        return None
    if fact.field is EvidenceField.COLLISION_TYPE:
        return _COLLISION_TYPE_TEXT.get(value)
    if fact.field is EvidenceField.VISIBLE_DAMAGE:
        return _VISIBLE_DAMAGE_TEXT.get(value)
    if fact.field is EvidenceField.IMPACT_AREA:
        return _IMPACT_AREA_TEXT.get(value)
    if fact.field is EvidenceField.INCIDENT_DATE:
        return value if _valid_date(value) else None
    if fact.field is EvidenceField.INCIDENT_TIME:
        return value if _valid_time(value) else None
    return None


def _valid_date(value: str) -> bool:
    if _DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_time(value: str) -> bool:
    if _TIME.fullmatch(value) is None:
        return False
    try:
        time.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _sources_support_status(
    fact: EvidenceFact,
    *,
    provenance: dict[str, ProvenanceRef],
    evidence: dict[str, EvidenceItem],
) -> bool:
    if len(set(fact.source_refs)) != len(fact.source_refs):
        return False
    for source_ref in fact.source_refs:
        reference = provenance.get(source_ref)
        if reference is None:
            return False
        item = evidence.get(reference.evidence_id)
        if item is None or item.model_copy_approved is not True:
            return False
        if fact.status is FactStatus.OBSERVED:
            if item.kind is not EvidenceKind.IMAGE:
                return False
            continue
        if item.kind is EvidenceKind.USER_STATEMENT:
            continue
        if (
            item.kind is EvidenceKind.TRANSCRIPT
            and item.transcript_confirmed is True
            and reference.user_confirmed is True
        ):
            continue
        return False
    return True
