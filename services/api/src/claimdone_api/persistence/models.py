"""Immutable values exchanged with the SQLite repository."""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from pydantic import JsonValue

from claimdone_api.contracts import (
    AuditEvent,
    CaseState,
    ClaimPacket,
    GateDecision,
    PortalState,
)

type JsonObject = dict[str, JsonValue]

_PORTAL_STATES_BY_CASE_STATE = MappingProxyType(
    {
        CaseState.CREATED: frozenset({PortalState.DRAFT}),
        CaseState.DISCLOSED: frozenset({PortalState.DRAFT}),
        CaseState.ANALYZING: frozenset({PortalState.DRAFT}),
        CaseState.AWAITING_TRANSCRIPT_CONFIRMATION: frozenset({PortalState.DRAFT}),
        CaseState.AWAITING_CLARIFICATION: frozenset({PortalState.DRAFT}),
        CaseState.READY_TO_FILL: frozenset({PortalState.DRAFT}),
        CaseState.FILLING: frozenset({PortalState.DRAFT}),
        CaseState.VERIFYING: frozenset({PortalState.REVIEW}),
        CaseState.REVIEW: frozenset({PortalState.REVIEW}),
        CaseState.BLOCKED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.HUMAN_APPROVED: frozenset({PortalState.HUMAN_APPROVED}),
        CaseState.RECEIPT: frozenset({PortalState.RECEIPT}),
        CaseState.EMERGENCY_STOPPED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.ABANDONED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.FAILED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
    }
)


def validate_portal_state(case_state: CaseState, portal_state: PortalState) -> None:
    """Keep persisted portal state aligned with the canonical case contract."""

    allowed = _PORTAL_STATES_BY_CASE_STATE[case_state]
    if portal_state not in allowed:
        values = ", ".join(sorted(value.value for value in allowed))
        raise ValueError(
            f"Case state {case_state.value} requires portal state in: {values}"
        )


def portal_state_after_transition(
    current: PortalState,
    target: CaseState,
) -> PortalState:
    """Derive the only safe portal projection when no ClaimPacket exists yet."""

    allowed = _PORTAL_STATES_BY_CASE_STATE[target]
    if current in allowed:
        return current
    if len(allowed) == 1:
        return next(iter(allowed))
    return PortalState.DRAFT if current is PortalState.DRAFT else PortalState.REVIEW


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """Versioned case payloads persisted alongside the state machine."""

    portal_state: PortalState
    redacted_metadata: dict[str, str]
    claim_packet: ClaimPacket | None
    intake_summary: JsonObject | None
    active_clarification: JsonObject | None


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """Complete persisted backend case."""

    case_id: str
    version: int
    state: CaseState
    snapshot: CaseSnapshot
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SequencedAuditEvent:
    """Canonical audit event with a database-owned cursor."""

    sequence: int
    event: AuditEvent


@dataclass(frozen=True, slots=True)
class SequencedGateDecision:
    """Immutable gate decision with a database-owned history cursor."""

    sequence: int
    decision: GateDecision
