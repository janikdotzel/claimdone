"""SQLite persistence surface for ClaimDone cases."""

from .models import (
    CaseRecord,
    CaseSnapshot,
    SequencedAuditEvent,
    SequencedGateDecision,
    portal_state_after_transition,
    validate_portal_state,
)
from .sqlite import (
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    SqliteCaseRepository,
    UnsupportedSchemaVersionError,
)

__all__ = [
    "CaseRecord",
    "CaseRecordNotFoundError",
    "CaseRecordVersionConflictError",
    "CaseSnapshot",
    "SequencedAuditEvent",
    "SequencedGateDecision",
    "SqliteCaseRepository",
    "UnsupportedSchemaVersionError",
    "portal_state_after_transition",
    "validate_portal_state",
]
