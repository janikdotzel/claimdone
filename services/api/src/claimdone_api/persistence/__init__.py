"""SQLite persistence surface for ClaimDone cases."""

from .models import (
    AuthorityCapabilityRecord,
    CaseRecord,
    CaseSnapshot,
    ProviderUsageLedgerRecord,
    SandboxReceiptRecord,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    TranscriptRecord,
    TranscriptTransitionResult,
    portal_state_after_transition,
    validate_portal_state,
)
from .sqlite import (
    AuthorityCapabilityError,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    IncompatiblePersistedContractError,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
    TranscriptStateError,
    UnsupportedSchemaVersionError,
)

__all__ = [
    "AuthorityCapabilityError",
    "AuthorityCapabilityRecord",
    "CaseRecord",
    "CaseRecordNotFoundError",
    "CaseRecordVersionConflictError",
    "CaseSnapshot",
    "IncompatiblePersistedContractError",
    "PersistedDataIntegrityError",
    "ProviderUsageLedgerRecord",
    "SandboxReceiptRecord",
    "SequencedAuditEvent",
    "SequencedGateDecision",
    "SequencedWorkflowEvent",
    "SqliteCaseRepository",
    "TranscriptRecord",
    "TranscriptStateError",
    "TranscriptTransitionResult",
    "UnsupportedSchemaVersionError",
    "portal_state_after_transition",
    "validate_portal_state",
]
