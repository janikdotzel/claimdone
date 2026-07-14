"""Factories that keep persisted audit events canonical and content-free."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    AuditEvent,
    AuditEventType,
    CaseState,
    GateDecision,
)


def _new_event_id() -> str:
    return f"event_{uuid4().hex}"


def build_state_change_event(
    *,
    case_id: str,
    current: CaseState,
    target: CaseState,
    actor: ActorType,
    occurred_at: datetime,
    event_id_factory: Callable[[], str] = _new_event_id,
) -> AuditEvent:
    """Build a state-only event without claim values or free-form details."""

    return AuditEvent.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "eventId": event_id_factory(),
            "caseId": case_id,
            "eventType": AuditEventType.CASE_STATE_CHANGED,
            "actor": actor,
            "occurredAt": occurred_at,
            "fromState": current,
            "toState": target,
            "reasonCodes": (),
            "details": (),
        }
    )


def build_gate_audit_event(
    *,
    case_id: str,
    decision: GateDecision,
    actor: ActorType,
    event_id_factory: Callable[[], str] = _new_event_id,
) -> AuditEvent:
    """Mirror a gate result into the audit stream without evidence content."""

    return AuditEvent.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "eventId": event_id_factory(),
            "caseId": case_id,
            "eventType": AuditEventType.GATE_DECISION,
            "actor": actor,
            "occurredAt": decision.decided_at,
            "fromState": None,
            "toState": None,
            "reasonCodes": decision.reason_codes,
            "details": (),
        }
    )
