"""Dependency-free, optimistic-concurrency SQLite case repository."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from claimdone_api.ai.core import NarrativeInput, compose_neutral_narrative
from claimdone_api.audit import (
    ObservabilityLogEvent,
    build_gate_audit_event,
    build_state_change_event,
    emit_redacted_log,
    validate_redacted_metadata,
)
from claimdone_api.contracts import (
    AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND,
    CONTRACT_VERSION,
    ActorType,
    AllowedTool,
    AuditEvent,
    AuditEventType,
    CaseState,
    ClaimPacket,
    ClarificationAnswerRequest,
    ClarificationStatus,
    ClarificationView,
    ClarificationWorkflowEvent,
    CounterpartyKnown,
    EvidenceField,
    EvidenceItem,
    EvidenceKind,
    FactStatus,
    GateDecision,
    GateId,
    GateReasonCode,
    GateWorkflowEvent,
    OperationalFailureWorkflowEvent,
    PlanStepWorkflowEvent,
    PortalDraftFields,
    PortalFillWorkflowEvent,
    PortalSessionView,
    PortalState,
    PortalVariant,
    ProvenanceRef,
    ProviderCallWorkflowEvent,
    ProviderFailureCategory,
    ProviderModelId,
    RenderedPortalSnapshot,
    RequiredClaimField,
    RetryWorkflowEvent,
    SandboxReceipt,
    StateWorkflowEvent,
    ToolCallStatus,
    ToolCallWorkflowEvent,
    ToolInvocation,
    TranscriptConfirmationView,
    VerificationAttempt,
    VerificationAttemptSeries,
    VerificationRepairMetadata,
    VerificationState,
    VerificationWorkflowEvent,
    WorkflowEventEnvelope,
    WorkflowEventKind,
    WorkflowOperation,
    WorkflowSnapshot,
    validate_case_transition,
    validate_workflow_event_order,
)
from claimdone_api.gates import (
    MAX_CLARIFICATION_ROUNDS,
    AdviceCategory,
    ClarificationQuestion,
    CompletenessResult,
    ModelExtraction,
    ModelOutputEnvelope,
    ModelSafetySignal,
    OutputContractResult,
    OutputContractRun,
    ProvenanceResult,
    RequestedAction,
    SafetyInput,
    ToolAuthorityContext,
    compute_missing_required_fields,
    evaluate_g2,
    evaluate_g3,
    evaluate_g4,
    evaluate_g5,
    evaluate_g6,
    evaluate_g7,
    evaluate_g8,
    make_gate_decision,
)

from .models import (
    AnalysisWorkflowCommand,
    AnalysisWorkflowResult,
    AuthorityCapabilityRecord,
    CaseRecord,
    CaseSnapshot,
    HumanApprovalCommand,
    HumanApprovalResult,
    IntakeDisclosureCommand,
    JsonObject,
    ObservabilityMetricsSnapshot,
    OutputContractAttempt,
    PortalRunRecord,
    PortalRunStartCommand,
    PortalRunStartResult,
    PortalWriteFinalizeCommand,
    PortalWriteFinalizeResult,
    ProviderUsageLedgerRecord,
    ProviderWorkflowEmission,
    SandboxReceiptRecord,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    TerminalProviderFailureCommand,
    TerminalProviderFailureResult,
    TranscriptionOutcomeCommand,
    TranscriptRecord,
    TranscriptTransitionResult,
    VerificationAttemptCommand,
    VerificationAttemptResult,
    validate_portal_state,
)

if TYPE_CHECKING:
    from claimdone_api.media import CaseMediaStore

SCHEMA_VERSION = 7
DEFAULT_BUSY_TIMEOUT_MS = 5_000
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
MAX_OBSERVABILITY_EVENT_ROWS = 4_096
CANONICAL_AUTHORITY_APPLICATION_ID = 0x43444E31
LEGACY_AUTHORITY_APPLICATION_ID = 0x43444C31
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_GATE_DECISIONS_ADAPTER: TypeAdapter[tuple[GateDecision, ...]] = TypeAdapter(
    tuple[GateDecision, ...]
)
_VERIFICATION_ATTEMPTS_ADAPTER: TypeAdapter[tuple[VerificationAttempt, ...]] = TypeAdapter(
    tuple[VerificationAttempt, ...]
)
_OBSERVABILITY_LOGGER = logging.getLogger("claimdone.observability")


@dataclass(slots=True)
class _InitializationLockEntry:
    lock: Lock
    users: int = 0


_INITIALIZATION_LOCKS_GUARD = Lock()
_INITIALIZATION_LOCKS: dict[Path, _InitializationLockEntry] = {}


@contextmanager
def _repository_initialization_lock(path: Path) -> Iterator[None]:
    """Serialize same-process identity checks and schema/WAL initialization per DB."""

    key = path.resolve(strict=False)
    with _INITIALIZATION_LOCKS_GUARD:
        entry = _INITIALIZATION_LOCKS.setdefault(
            key,
            _InitializationLockEntry(lock=Lock()),
        )
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _INITIALIZATION_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _INITIALIZATION_LOCKS.get(key) is entry:
                del _INITIALIZATION_LOCKS[key]


type AppendableWorkflowEvent = (
    ClarificationWorkflowEvent
    | PlanStepWorkflowEvent
    | ToolCallWorkflowEvent
    | PortalFillWorkflowEvent
    | VerificationWorkflowEvent
    | RetryWorkflowEvent
    | OperationalFailureWorkflowEvent
    | ProviderCallWorkflowEvent
)


class PersistenceError(RuntimeError):
    """Base class for expected repository failures."""


class CaseRecordNotFoundError(PersistenceError):
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        super().__init__(f"Case record not found: {case_id}")


class CaseRecordVersionConflictError(PersistenceError):
    def __init__(self, case_id: str, expected_version: int, current_version: int) -> None:
        self.case_id = case_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Case {case_id} is at version {current_version}, expected {expected_version}"
        )


class UnsupportedSchemaVersionError(PersistenceError):
    """Raised instead of silently opening a newer database schema."""


class PersistedDataIntegrityError(PersistenceError):
    """Raised when persisted JSON disagrees with canonical contracts."""


class IncompatiblePersistedContractError(PersistenceError):
    """Refuse to relabel major-version demo data during a schema migration."""

    def __init__(self) -> None:
        super().__init__(
            "Persisted demo data is incompatible with the current canonical contracts; "
            "stop the services and run `make reset`, then start again."
        )


class TranscriptStateError(PersistenceError):
    """Raised when transcript identity, version, or confirmation state is stale."""


class AuthorityCapabilityError(PersistenceError):
    """Raised when digest-only capability metadata is invalid or stale."""


class WorkflowAtomicityError(PersistenceError):
    """Raised when a critical workflow sequence is incomplete or cross-bound incorrectly."""


class AuthorityModeMismatchError(PersistenceError):
    """Refuse to open a database created by a different authority boundary."""


def _enum_sql_values(values: type[StrEnum]) -> str:
    return ", ".join(f"'{value.value}'" for value in values)


_CASE_STATE_VALUES = _enum_sql_values(CaseState)
_PORTAL_STATE_VALUES = _enum_sql_values(PortalState)
_GATE_ID_VALUES = _enum_sql_values(GateId)
_AUDIT_EVENT_TYPE_VALUES = _enum_sql_values(AuditEventType)
_WORKFLOW_EVENT_KIND_VALUES = _enum_sql_values(WorkflowEventKind)
_WORKFLOW_OPERATION_VALUES = _enum_sql_values(WorkflowOperation)
_PROVIDER_MODEL_ID_VALUES = _enum_sql_values(ProviderModelId)
_PROVIDER_FAILURE_VALUES = _enum_sql_values(ProviderFailureCategory)
_ANALYSIS_GATE_SEQUENCE = (
    GateId.G0_INTAKE,
    GateId.G1_PRIVACY,
    GateId.G2_OUTPUT_CONTRACT,
    GateId.G3_SAFETY_SCOPE,
    GateId.G4_PROVENANCE,
    GateId.G5_COMPLETENESS,
)
_CANONICAL_PACKET_REQUIRED_STATES = frozenset(
    {
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.VERIFYING,
        CaseState.REVIEW,
        CaseState.HUMAN_APPROVED,
    }
)
_CANONICAL_PACKET_FORBIDDEN_STATES = frozenset(
    {
        CaseState.CREATED,
        CaseState.DISCLOSED,
        CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
        CaseState.RECEIPT,
    }
)
_AWAITING_CLARIFICATION_PLAN = (
    (
        AllowedTool.INSPECT_EVIDENCE,
        "Inspect only the approved evidence inventory",
    ),
    (
        AllowedTool.CHECK_REQUIRED_FIELDS,
        "Use the deterministic required-field result",
    ),
    (
        AllowedTool.ASK_CLARIFICATION,
        "Ask the single clarification accepted by G5",
    ),
)
_BLOCKED_PLAN = _AWAITING_CLARIFICATION_PLAN[:2]
_READY_TO_FILL_PLAN = (
    (
        AllowedTool.INSPECT_EVIDENCE,
        "Inspect only the approved evidence inventory",
    ),
    (
        AllowedTool.CHECK_REQUIRED_FIELDS,
        "Use the deterministic required-field result",
    ),
    (AllowedTool.INSPECT_FORM, "Inspect only the local sandbox form"),
    (AllowedTool.FILL_UNTIL_REVIEW, "Fill the sandbox only until review"),
    (
        AllowedTool.VERIFY_RENDERED_FIELDS,
        "Verify rendered fields before human review",
    ),
)
_QUESTION_BY_FIELD = {
    RequiredClaimField.INCIDENT_DATE: "What was the date of the incident?",
    RequiredClaimField.INCIDENT_TIME: "What time did the incident happen?",
    RequiredClaimField.LOCATION: "Where did the incident happen?",
    RequiredClaimField.CLAIMANT_NAME: "What is the claimant's name?",
    RequiredClaimField.POLICY_REFERENCE: "What is the demo policy number?",
    RequiredClaimField.VEHICLE_REGISTRATION: "What is the demo vehicle registration?",
    RequiredClaimField.COUNTERPARTY_KNOWN: "Is the other party known?",
}
_TEXT_CLARIFIABLE_FIELDS = frozenset(_QUESTION_BY_FIELD)
_WORKFLOW_KIND_BY_AUDIT_EVENT_TYPE = {
    event_type: kind for kind, event_type in AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND.items()
}
_REQUIRED_SCHEMA_TABLES = (
    "cases",
    "audit_events",
    "gate_decisions",
    "case_media_handles",
    "workflow_events",
    "case_transcripts",
    "provider_usage_ledger",
    "authority_capabilities",
    "sandbox_receipts",
    "sandbox_receipt_authority",
    "case_intake_authority",
    "case_transcript_authority",
    "case_packet_authority",
    "portal_run_authority",
    "portal_session_authority",
    "verification_attempt_authority",
)


@dataclass(frozen=True, slots=True)
class _AnalysisAuthority:
    """Repository-derived authority used to validate one atomic command."""

    effective_gates: tuple[GateDecision, ...]
    provenance: ProvenanceResult | None
    completeness: CompletenessResult | None


@dataclass(frozen=True, slots=True)
class _ExpectedPacketAuthority:
    """Replay-derived immutable packet shape at one case version."""

    bound_version: int
    created_at: datetime
    state: CaseState
    plan_events: tuple[tuple[int, AllowedTool], ...]
    safe_plan: tuple[tuple[AllowedTool, str], ...]
    clarification_close: ClarificationWorkflowEvent | None = None


_MIGRATION_1 = (
    f"""
    CREATE TABLE cases (
        case_id TEXT PRIMARY KEY NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        state TEXT NOT NULL CHECK (state IN ({_CASE_STATE_VALUES})),
        portal_state TEXT NOT NULL CHECK (portal_state IN ({_PORTAL_STATE_VALUES})),
        redacted_metadata_json TEXT NOT NULL CHECK (json_valid(redacted_metadata_json)),
        claim_packet_json TEXT
            CHECK (claim_packet_json IS NULL OR json_valid(claim_packet_json)),
        intake_summary_json TEXT
            CHECK (intake_summary_json IS NULL OR json_valid(intake_summary_json)),
        active_clarification_json TEXT
            CHECK (
                active_clarification_json IS NULL OR json_valid(active_clarification_json)
            ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        occurred_at TEXT NOT NULL,
        event_json TEXT NOT NULL CHECK (json_valid(event_json))
    )
    """,
    """
    CREATE INDEX audit_events_case_sequence_idx
    ON audit_events(case_id, sequence)
    """,
    f"""
    CREATE TABLE gate_decisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        gate_id TEXT NOT NULL CHECK (gate_id IN ({_GATE_ID_VALUES})),
        decided_at TEXT NOT NULL,
        decision_json TEXT NOT NULL CHECK (json_valid(decision_json))
    )
    """,
    """
    CREATE INDEX gate_decisions_case_sequence_idx
    ON gate_decisions(case_id, sequence)
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE case_media_handles (
        case_id TEXT PRIMARY KEY NOT NULL
            REFERENCES cases(case_id) ON DELETE CASCADE,
        storage_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
)

_WORKFLOW_SOURCE_TYPE_CHECK = " OR ".join(
    f"(event_kind = '{kind.value}' AND source_audit_event_type = '{event_type.value}')"
    for kind, event_type in AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND.items()
)

_CASES_V3 = f"""
CREATE TABLE cases_v3 (
    case_id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    state TEXT NOT NULL CHECK (state IN ({_CASE_STATE_VALUES})),
    portal_state TEXT NOT NULL CHECK (portal_state IN ({_PORTAL_STATE_VALUES})),
    redacted_metadata_json TEXT NOT NULL CHECK (json_valid(redacted_metadata_json)),
    claim_packet_json TEXT CHECK (claim_packet_json IS NULL OR json_valid(claim_packet_json)),
    intake_summary_json TEXT CHECK (intake_summary_json IS NULL OR json_valid(intake_summary_json)),
    active_clarification_json TEXT
        CHECK (active_clarification_json IS NULL OR json_valid(active_clarification_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_MIGRATION_3 = (
    """
    CREATE UNIQUE INDEX audit_events_projection_source_idx
    ON audit_events(sequence, event_id, case_id)
    """,
    f"""
    CREATE TABLE workflow_events (
        source_audit_sequence INTEGER PRIMARY KEY NOT NULL,
        source_audit_event_id TEXT NOT NULL UNIQUE,
        source_audit_event_type TEXT NOT NULL
            CHECK (source_audit_event_type IN ({_AUDIT_EVENT_TYPE_VALUES})),
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        event_id TEXT NOT NULL UNIQUE,
        event_kind TEXT NOT NULL CHECK (event_kind IN ({_WORKFLOW_EVENT_KIND_VALUES})),
        event_json TEXT NOT NULL CHECK (json_valid(event_json)),
        FOREIGN KEY (source_audit_sequence, source_audit_event_id, case_id)
            REFERENCES audit_events(sequence, event_id, case_id) ON DELETE CASCADE,
        CHECK ({_WORKFLOW_SOURCE_TYPE_CHECK}),
        CHECK (json_extract(event_json, '$.eventId') IS event_id),
        CHECK (json_extract(event_json, '$.caseId') IS case_id),
        CHECK (
            json_extract(event_json, '$.sourceAuditEventId') IS source_audit_event_id
        ),
        CHECK (
            json_extract(event_json, '$.sourceAuditEventType') IS source_audit_event_type
        ),
        CHECK (
            json_extract(event_json, '$.sourceAuditSequence') IS source_audit_sequence
        ),
        CHECK (json_extract(event_json, '$.cursor') IS source_audit_sequence),
        CHECK (json_extract(event_json, '$.event.kind') IS event_kind)
    )
    """,
    """
    CREATE INDEX workflow_events_case_cursor_idx
    ON workflow_events(case_id, source_audit_sequence)
    """,
    """
    CREATE UNIQUE INDEX workflow_events_provider_source_idx
    ON workflow_events(source_audit_sequence, case_id)
    """,
    """
    CREATE TABLE case_transcripts (
        transcript_id TEXT PRIMARY KEY NOT NULL,
        case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version >= 1),
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 1),
        transcript_sha256 TEXT NOT NULL
            CHECK (
                length(transcript_sha256) = 64
                AND transcript_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        local_ref TEXT NOT NULL
            CHECK (
                length(local_ref) = 47
                AND local_ref LIKE 'transcript-%.txt'
            ),
        confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        CHECK (
            (confirmed = 0 AND confirmed_at IS NULL)
            OR (confirmed = 1 AND confirmed_at IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE provider_usage_ledger (
        source_audit_sequence INTEGER PRIMARY KEY NOT NULL,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        operation TEXT NOT NULL CHECK (operation IN ({_WORKFLOW_OPERATION_VALUES})),
        model_id TEXT NOT NULL CHECK (model_id IN ({_PROVIDER_MODEL_ID_VALUES})),
        provider_mode TEXT NOT NULL CHECK (provider_mode IN ('mock', 'live')),
        call_sequence INTEGER NOT NULL CHECK (call_sequence BETWEEN 1 AND 40),
        retry_attempt INTEGER NOT NULL CHECK (retry_attempt IN (0, 1)),
        duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
        status TEXT NOT NULL CHECK (status IN ('succeeded', 'retry_scheduled', 'failed')),
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
        estimated_cost_micros INTEGER
            CHECK (estimated_cost_micros IS NULL OR estimated_cost_micros >= 0),
        currency TEXT CHECK (currency IS NULL OR currency = 'USD'),
        pricing_snapshot_id TEXT
            CHECK (pricing_snapshot_id IS NULL OR length(pricing_snapshot_id) > 0),
        failure_category TEXT
            CHECK (failure_category IS NULL OR failure_category IN ({_PROVIDER_FAILURE_VALUES})),
        occurred_at TEXT NOT NULL,
        FOREIGN KEY (source_audit_sequence, case_id)
            REFERENCES workflow_events(source_audit_sequence, case_id) ON DELETE CASCADE,
        CHECK (
            (input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL)
            OR (
                input_tokens IS NOT NULL
                AND output_tokens IS NOT NULL
                AND total_tokens = input_tokens + output_tokens
            )
        ),
        CHECK (
            (estimated_cost_micros IS NULL AND currency IS NULL AND pricing_snapshot_id IS NULL)
            OR (
                estimated_cost_micros IS NOT NULL
                AND currency = 'USD'
                AND pricing_snapshot_id IS NOT NULL
            )
        ),
        CHECK (
            (status = 'succeeded' AND failure_category IS NULL)
            OR (status <> 'succeeded' AND failure_category IS NOT NULL)
        ),
        CHECK (
            status = 'succeeded'
            OR (
                input_tokens IS NULL
                AND output_tokens IS NULL
                AND total_tokens IS NULL
                AND estimated_cost_micros IS NULL
                AND currency IS NULL
                AND pricing_snapshot_id IS NULL
            )
        )
    )
    """,
    """
    CREATE INDEX provider_usage_case_cursor_idx
    ON provider_usage_ledger(case_id, source_audit_sequence)
    """,
    """
    CREATE TABLE authority_capabilities (
        capability_digest BLOB PRIMARY KEY NOT NULL
            CHECK (typeof(capability_digest) = 'blob' AND length(capability_digest) = 32),
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('human', 'agent')),
        purpose TEXT NOT NULL CHECK (purpose IN ('portal_run', 'human_approve')),
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 1),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        revoked_at TEXT,
        CHECK (
            (role = 'agent' AND purpose = 'portal_run')
            OR (role = 'human' AND purpose = 'human_approve')
        ),
        CHECK (consumed_at IS NULL OR revoked_at IS NULL)
    )
    """,
    """
    CREATE INDEX authority_capabilities_case_open_idx
    ON authority_capabilities(case_id, purpose, consumed_at, revoked_at, expires_at)
    """,
    """
    CREATE TABLE sandbox_receipts (
        case_id TEXT PRIMARY KEY NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
        created_at TEXT NOT NULL,
        CHECK (json_extract(receipt_json, '$.caseId') IS case_id),
        CHECK (json_extract(receipt_json, '$.redacted') IS 1),
        CHECK (json_extract(receipt_json, '$.sandboxOnly') IS 1),
        CHECK (json_extract(receipt_json, '$.submittedToRealInsurer') IS 0)
    )
    """,
)

_MIGRATION_4 = (
    """
    CREATE TABLE case_intake_authority (
        case_id TEXT PRIMARY KEY NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 2),
        storage_name TEXT NOT NULL UNIQUE
            REFERENCES case_media_handles(storage_name) ON DELETE CASCADE,
        manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
        manifest_sha256 TEXT NOT NULL
            CHECK (
                length(manifest_sha256) = 64
                AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        g0_gate_sequence INTEGER NOT NULL UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        g1_gate_sequence INTEGER NOT NULL UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        CHECK (g0_gate_sequence < g1_gate_sequence)
    )
    """,
    """
    CREATE TABLE case_transcript_authority (
        case_id TEXT PRIMARY KEY NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 3),
        intake_manifest_sha256 TEXT NOT NULL
            CHECK (
                length(intake_manifest_sha256) = 64
                AND intake_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        transcript_id TEXT NOT NULL UNIQUE,
        transcript_local_ref TEXT NOT NULL UNIQUE,
        transcript_sha256 TEXT NOT NULL
            CHECK (
                length(transcript_sha256) = 64
                AND transcript_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        provider_source_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES workflow_events(source_audit_sequence) ON DELETE CASCADE,
        manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
        manifest_sha256 TEXT NOT NULL
            CHECK (
                length(manifest_sha256) = 64
                AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at TEXT NOT NULL
    )
    """,
)

_MIGRATION_5 = (
    """
    CREATE TABLE case_packet_authority (
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 2),
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        packet_json TEXT NOT NULL CHECK (json_valid(packet_json)),
        packet_sha256 TEXT NOT NULL
            CHECK (
                length(packet_sha256) = 64
                AND packet_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        effective_gates_json TEXT NOT NULL CHECK (json_valid(effective_gates_json)),
        effective_gates_sha256 TEXT NOT NULL
            CHECK (
                length(effective_gates_sha256) = 64
                AND effective_gates_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at TEXT NOT NULL,
        PRIMARY KEY (case_id, bound_case_version)
    )
    """,
)

_MIGRATION_6 = (
    """
    ALTER TABLE authority_capabilities
    ADD COLUMN portal_variant TEXT
        CHECK (
            (
                role = 'agent'
                AND purpose = 'portal_run'
                AND portal_variant IS NULL
            )
            OR (
                role = 'human'
                AND purpose = 'human_approve'
                AND portal_variant IS NOT NULL
                AND portal_variant IN ('A', 'B')
            )
        )
    """,
    """
    CREATE TABLE sandbox_receipt_authority (
        case_id TEXT PRIMARY KEY NOT NULL
            REFERENCES sandbox_receipts(case_id) ON DELETE CASCADE,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        bound_review_case_version INTEGER NOT NULL
            CHECK (bound_review_case_version >= 1),
        human_capability_digest BLOB NOT NULL UNIQUE
            REFERENCES authority_capabilities(capability_digest) ON DELETE CASCADE
            CHECK (
                typeof(human_capability_digest) = 'blob'
                AND length(human_capability_digest) = 32
            ),
        portal_variant TEXT NOT NULL CHECK (portal_variant IN ('A', 'B')),
        approval_id TEXT NOT NULL UNIQUE,
        receipt_id TEXT NOT NULL UNIQUE,
        receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
        receipt_sha256 TEXT NOT NULL
            CHECK (
                length(receipt_sha256) = 64
                AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        g9_gate_sequence INTEGER NOT NULL UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        g10_gate_sequence INTEGER NOT NULL UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        human_approval_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        human_approved_state_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        receipt_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        receipt_state_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        CHECK (g9_gate_sequence < g10_gate_sequence),
        CHECK (
            human_approval_audit_sequence
            < human_approved_state_audit_sequence
            AND human_approved_state_audit_sequence < receipt_audit_sequence
            AND receipt_audit_sequence < receipt_state_audit_sequence
        ),
        CHECK (json_extract(receipt_json, '$.caseId') IS case_id),
        CHECK (json_extract(receipt_json, '$.variant') IS portal_variant),
        CHECK (json_extract(receipt_json, '$.approvalId') IS approval_id),
        CHECK (json_extract(receipt_json, '$.receiptId') IS receipt_id),
        CHECK (json_extract(receipt_json, '$.redacted') IS 1),
        CHECK (json_extract(receipt_json, '$.sandboxOnly') IS 1),
        CHECK (json_extract(receipt_json, '$.submittedToRealInsurer') IS 0)
    )
    """,
)

_MIGRATION_7 = (
    """
    CREATE TABLE portal_run_authority (
        run_id TEXT PRIMARY KEY NOT NULL,
        case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id) ON DELETE CASCADE,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        agent_capability_digest BLOB NOT NULL UNIQUE
            REFERENCES authority_capabilities(capability_digest) ON DELETE CASCADE
            CHECK (
                typeof(agent_capability_digest) = 'blob'
                AND length(agent_capability_digest) = 32
            ),
        control_digest BLOB NOT NULL UNIQUE
            CHECK (typeof(control_digest) = 'blob' AND length(control_digest) = 32),
        portal_variant TEXT NOT NULL CHECK (portal_variant IN ('A', 'B')),
        ready_case_version INTEGER NOT NULL CHECK (ready_case_version >= 1),
        g6_case_version INTEGER NOT NULL CHECK (g6_case_version = ready_case_version + 1),
        terminal_case_version INTEGER
            CHECK (
                terminal_case_version IS NULL
                OR terminal_case_version = g6_case_version
                OR terminal_case_version = g6_case_version + 1
            ),
        invocation_json TEXT NOT NULL CHECK (json_valid(invocation_json)),
        invocation_sha256 TEXT NOT NULL
            CHECK (
                length(invocation_sha256) = 64
                AND invocation_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        g6_context_json TEXT NOT NULL CHECK (json_valid(g6_context_json)),
        g6_context_sha256 TEXT NOT NULL
            CHECK (
                length(g6_context_sha256) = 64
                AND g6_context_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        prestage_session_json TEXT NOT NULL CHECK (json_valid(prestage_session_json)),
        prestage_session_sha256 TEXT NOT NULL
            CHECK (
                length(prestage_session_sha256) = 64
                AND prestage_session_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        g6_gate_sequence INTEGER NOT NULL UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        g6_state_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        g7_gate_sequence INTEGER UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        tool_terminal_audit_sequence INTEGER UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        portal_fill_audit_sequence INTEGER UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        g7_state_audit_sequence INTEGER UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        rejected_summary_json TEXT
            CHECK (rejected_summary_json IS NULL OR json_valid(rejected_summary_json)),
        rejected_summary_sha256 TEXT
            CHECK (
                rejected_summary_sha256 IS NULL
                OR (
                    length(rejected_summary_sha256) = 64
                    AND rejected_summary_sha256 NOT GLOB '*[^0-9a-f]*'
                )
            ),
        status TEXT NOT NULL
            CHECK (
                status IN (
                    'filling', 'blocked_g6', 'verifying', 'blocked_g7',
                    'review', 'blocked_g8'
                )
            ),
        created_at TEXT NOT NULL,
        terminal_at TEXT,
        UNIQUE (case_id, run_id),
        CHECK (
            (status IN ('filling', 'blocked_g6') AND g7_gate_sequence IS NULL)
            OR (status NOT IN ('filling', 'blocked_g6') AND g7_gate_sequence IS NOT NULL)
        ),
        CHECK (
            (status = 'filling' AND terminal_case_version IS NULL AND terminal_at IS NULL)
            OR (
                status = 'blocked_g6'
                AND terminal_case_version = g6_case_version
                AND terminal_at IS NOT NULL
            )
            OR (
                status NOT IN ('filling', 'blocked_g6')
                AND terminal_case_version = g6_case_version + 1
                AND terminal_case_version IS NOT NULL
                AND terminal_at IS NOT NULL
            )
        ),
        CHECK ((rejected_summary_json IS NULL) = (rejected_summary_sha256 IS NULL)),
        CHECK (json_extract(invocation_json, '$.invocationId') IS run_id),
        CHECK (json_extract(prestage_session_json, '$.caseId') IS case_id),
        CHECK (json_extract(prestage_session_json, '$.variant') IS portal_variant),
        CHECK (json_extract(prestage_session_json, '$.state') IS 'draft')
    )
    """,
    """
    CREATE TABLE portal_session_authority (
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        checkpoint_number INTEGER NOT NULL CHECK (checkpoint_number IN (1, 2)),
        run_id TEXT NOT NULL,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        checkpoint_kind TEXT NOT NULL CHECK (checkpoint_kind IN ('reviewed', 'repair')),
        portal_version INTEGER NOT NULL CHECK (portal_version >= 1),
        session_json TEXT NOT NULL CHECK (json_valid(session_json)),
        session_sha256 TEXT NOT NULL
            CHECK (
                length(session_sha256) = 64
                AND session_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        rendered_snapshot_json TEXT NOT NULL CHECK (json_valid(rendered_snapshot_json)),
        rendered_snapshot_sha256 TEXT NOT NULL
            CHECK (
                length(rendered_snapshot_sha256) = 64
                AND rendered_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        source_attempt_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (case_id, checkpoint_number),
        UNIQUE (run_id, checkpoint_number),
        UNIQUE (case_id, portal_version),
        FOREIGN KEY (case_id, run_id)
            REFERENCES portal_run_authority(case_id, run_id)
            ON DELETE CASCADE,
        CHECK (
            (checkpoint_number = 1 AND checkpoint_kind = 'reviewed' AND source_attempt_id IS NULL)
            OR (
                checkpoint_number = 2
                AND checkpoint_kind = 'repair'
                AND source_attempt_id IS NOT NULL
            )
        ),
        CHECK (json_extract(session_json, '$.caseId') IS case_id),
        CHECK (json_extract(session_json, '$.version') IS portal_version),
        CHECK (json_extract(session_json, '$.state') IS 'review'),
        CHECK (json_extract(rendered_snapshot_json, '$.caseId') IS case_id),
        CHECK (json_extract(rendered_snapshot_json, '$.version') IS portal_version),
        CHECK (json_extract(rendered_snapshot_json, '$.state') IS 'review')
    )
    """,
    """
    CREATE TABLE verification_attempt_authority (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        authority_version INTEGER NOT NULL CHECK (authority_version = 1),
        attempt_number INTEGER NOT NULL CHECK (attempt_number IN (1, 2)),
        bound_case_version INTEGER NOT NULL CHECK (bound_case_version >= 1),
        portal_checkpoint_number INTEGER NOT NULL CHECK (portal_checkpoint_number IN (1, 2)),
        attempt_json TEXT NOT NULL CHECK (json_valid(attempt_json)),
        attempt_sha256 TEXT NOT NULL
            CHECK (
                length(attempt_sha256) = 64
                AND attempt_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        rendered_snapshot_json TEXT NOT NULL CHECK (json_valid(rendered_snapshot_json)),
        rendered_snapshot_sha256 TEXT NOT NULL
            CHECK (
                length(rendered_snapshot_sha256) = 64
                AND rendered_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        screenshot_sha256 TEXT NOT NULL
            CHECK (
                length(screenshot_sha256) = 64
                AND screenshot_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        snapshot_requested_at TEXT NOT NULL,
        snapshot_received_at TEXT NOT NULL,
        final INTEGER NOT NULL CHECK (final IN (0, 1)),
        g8_gate_sequence INTEGER UNIQUE
            REFERENCES gate_decisions(sequence) ON DELETE CASCADE,
        verification_audit_sequence INTEGER NOT NULL UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        state_audit_sequence INTEGER UNIQUE
            REFERENCES audit_events(sequence) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        UNIQUE (case_id, attempt_number),
        FOREIGN KEY (case_id, run_id)
            REFERENCES portal_run_authority(case_id, run_id)
            ON DELETE CASCADE,
        FOREIGN KEY (case_id, portal_checkpoint_number)
            REFERENCES portal_session_authority(case_id, checkpoint_number)
            ON DELETE CASCADE,
        CHECK ((final = 1) = (g8_gate_sequence IS NOT NULL)),
        CHECK ((final = 1) = (state_audit_sequence IS NOT NULL)),
        CHECK (json_extract(attempt_json, '$.attemptId') IS attempt_id),
        CHECK (json_extract(attempt_json, '$.caseId') IS case_id),
        CHECK (json_extract(attempt_json, '$.attemptNumber') IS attempt_number),
        CHECK (json_extract(attempt_json, '$.final') IS final),
        CHECK (json_extract(rendered_snapshot_json, '$.caseId') IS case_id)
    )
    """,
    """
    CREATE INDEX verification_attempt_case_number_idx
    ON verification_attempt_authority(case_id, attempt_number)
    """,
)

_MEDIA_STORAGE_NAME = re.compile(r"^case-[a-f0-9]{32}$")
_AUDIO_LOCAL_REF = re.compile(r"^audio-[a-f0-9]{32}\.wav$")
_TRANSCRIPT_LOCAL_REF = re.compile(r"^transcript-[a-f0-9]{32}\.txt$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPABILITY_TTL = timedelta(seconds=120)
_G6_SAFE_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "drag",
        "move",
        "scroll",
        "keypress",
        "type",
        "wait",
        "screenshot",
    }
)


def _transcript_identity_from_summary(
    case_id: str,
    summary: JsonObject,
) -> tuple[str, str, str]:
    statement = summary.get("statement")
    audio = summary.get("audio")
    text = summary.get("text")
    if not isinstance(statement, dict) or not isinstance(audio, dict) or text is not None:
        raise ValueError("Pending transcript requires an audio intake summary")
    audio_ref = audio.get("fileId")
    audio_media_type = audio.get("mediaType")
    audio_digest = audio.get("sha256")
    if (
        not isinstance(audio_ref, str)
        or _AUDIO_LOCAL_REF.fullmatch(audio_ref) is None
        or audio_media_type != "audio/wav"
        or not isinstance(audio_digest, str)
        or _SHA256.fullmatch(audio_digest) is None
    ):
        raise ValueError("Persisted audio reference is invalid")
    local_ref = statement.get("fileId")
    digest = statement.get("sha256")
    media_type = statement.get("mediaType")
    if (
        not isinstance(local_ref, str)
        or _TRANSCRIPT_LOCAL_REF.fullmatch(local_ref) is None
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or media_type != "text/plain"
    ):
        raise ValueError("Persisted transcript reference is invalid")
    identity_digest = hashlib.sha256(
        f"claimdone-transcript-v1\0{case_id}\0{local_ref}\0{digest}".encode()
    ).hexdigest()
    return f"transcript-{identity_digest[:32]}", local_ref, digest


def _dump_json_object(value: JsonObject | dict[str, str]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _dump_json_value(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _dump_contract(value: BaseModel) -> str:
    return _dump_json_value(cast(JsonValue, value.model_dump(mode="json", by_alias=True)))


def _authority_sha256(namespace: str, payload: str) -> str:
    return hashlib.sha256(
        f"claimdone-{namespace}-v1\0".encode() + payload.encode("utf-8")
    ).hexdigest()


def _verification_authority_sha256(
    *,
    attempt_json: str,
    rendered_json: str,
    screenshot_sha256: str,
    requested_at: datetime,
    received_at: datetime,
) -> str:
    payload = "\0".join(
        (
            attempt_json,
            rendered_json,
            screenshot_sha256,
            _dump_aware_datetime(requested_at, "snapshot requested_at"),
            _dump_aware_datetime(received_at, "snapshot received_at"),
        )
    )
    return _authority_sha256("verification-attempt", payload)


def _rebind_claim_packet(
    packet: ClaimPacket,
    *,
    state: CaseState,
    portal_state: PortalState,
    gates: tuple[GateDecision, ...],
    verification: BaseModel | JsonObject,
) -> ClaimPacket:
    data = packet.model_dump(mode="json", by_alias=True)
    data.update(
        {
            "state": state.value,
            "portalState": portal_state.value,
            "gateDecisions": [gate.model_dump(mode="json", by_alias=True) for gate in gates],
            "verification": (
                verification.model_dump(mode="json", by_alias=True)
                if isinstance(verification, BaseModel)
                else verification
            ),
        }
    )
    return ClaimPacket.model_validate(data)


def _canonical_claim_portal_fields(packet: ClaimPacket) -> PortalDraftFields:
    claim = packet.claim.model_dump(mode="json", by_alias=True)
    return PortalDraftFields.model_validate(
        {
            "incidentDate": claim["incidentDate"],
            "incidentTime": claim["incidentTime"],
            "location": claim["location"],
            "claimantName": claim["claimantName"],
            "policyReference": claim["policyReference"],
            "vehicleRegistration": claim["vehicleRegistration"],
            "counterpartyKnown": claim["counterpartyKnown"],
            "narrative": claim["narrative"],
            "attachments": claim["attachments"],
        }
    )


def _validate_prestage_session(
    session: PortalSessionView,
    *,
    case_id: str,
    variant: PortalVariant,
    attachments: tuple[str, ...],
) -> None:
    fields = session.fields
    if (
        session.case_id != case_id
        or session.variant is not variant
        or session.state is not PortalState.DRAFT
        or session.version != 1
        or fields.incident_date != ""
        or fields.incident_time != ""
        or fields.location != ""
        or fields.claimant_name != ""
        or fields.policy_reference != ""
        or fields.vehicle_registration != ""
        or fields.counterparty_known != ""
        or fields.narrative != ""
        or fields.attachments != attachments
    ):
        raise WorkflowAtomicityError(
            "Prestage authority requires empty scalars and exact ordered attachments"
        )


def _rejected_g7_summary(payload: object, decision: GateDecision) -> JsonObject:
    if type(payload) is dict:
        field_count = len(cast(dict[object, object], payload))
        recognized = len(
            set(cast(dict[object, object], payload))
            & {
                "incidentDate",
                "incidentTime",
                "location",
                "claimantName",
                "policyReference",
                "vehicleRegistration",
                "counterpartyKnown",
                "narrative",
                "attachments",
            }
        )
        payload_type = "object"
    else:
        field_count = 0
        recognized = 0
        payload_type = "other"
    return {
        "payloadType": payload_type,
        "fieldCount": field_count,
        "recognizedFieldCount": recognized,
        "reasonCodes": [reason.value for reason in decision.reason_codes],
    }


def _load_json_object(value: str) -> JsonObject:
    return _JSON_OBJECT_ADAPTER.validate_json(value)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise PersistedDataIntegrityError("Persisted timestamps must include a timezone")
    return parsed


def _dump_aware_datetime(value: datetime, field: str) -> str:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.isoformat()


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PersistedDataIntegrityError(f"Persisted {field} must be text")
    return value


def _require_integer(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise PersistedDataIntegrityError(f"Persisted {field} must be an integer")
    return value


def _bounded_observability_sum(
    values: tuple[int | None, ...],
    field: str,
) -> int:
    """Sum persisted counters without accepting nulls or oversized aggregates."""

    if any(type(value) is not int or value < 0 for value in values):
        raise PersistedDataIntegrityError(f"Persisted {field} contains an invalid counter")
    total = sum(cast(int, value) for value in values)
    if total > SQLITE_MAX_INTEGER:
        raise PersistedDataIntegrityError(f"Persisted {field} exceeds the SQLite integer bound")
    return total


def _validate_provider_metric_sequence(
    provider: tuple[ProviderUsageLedgerRecord, ...],
) -> None:
    """Accept only cursor-ordered provider histories canonical V1 can write."""

    by_operation: dict[WorkflowOperation, list[ProviderUsageLedgerRecord]] = {
        operation: [] for operation in WorkflowOperation
    }
    previous_cursor = 0
    terminal_cursor: int | None = None
    operation_phase = 0
    for item in provider:
        if item.source_audit_sequence <= previous_cursor:
            raise PersistedDataIntegrityError(
                "Persisted provider cursors are not strictly increasing"
            )
        if terminal_cursor is not None:
            raise PersistedDataIntegrityError("Persisted provider call follows a terminal failure")
        if item.operation is WorkflowOperation.TRANSCRIPTION:
            if operation_phase != 0:
                raise PersistedDataIntegrityError(
                    "Persisted transcription telemetry follows a later operation"
                )
        elif item.operation is WorkflowOperation.EXTRACTION:
            if operation_phase > 1:
                raise PersistedDataIntegrityError(
                    "Persisted extraction telemetry is not one contiguous phase"
                )
            operation_phase = 1
        else:
            operation_phase = 2
        previous_cursor = item.source_audit_sequence
        by_operation[item.operation].append(item)
        if item.status == "failed":
            terminal_cursor = item.source_audit_sequence

    transcription = tuple(by_operation[WorkflowOperation.TRANSCRIPTION])
    if transcription:
        only = transcription[0]
        if len(transcription) != 1 or (
            only.call_sequence != 1
            or only.retry_attempt != 0
            or only.status not in {"failed", "succeeded"}
        ):
            raise PersistedDataIntegrityError(
                "Persisted transcription telemetry is not one canonical call"
            )

    extraction = tuple(by_operation[WorkflowOperation.EXTRACTION])
    if extraction:
        first = extraction[0]
        initial_shape = (
            first.call_sequence == 1
            and first.retry_attempt == 0
            and first.status in {"failed", "succeeded"}
        )
        if len(extraction) == 1:
            if not initial_shape:
                raise PersistedDataIntegrityError("Persisted extraction initial call is invalid")
        elif len(extraction) == 3:
            retry, final = extraction[1:]
            if (
                not initial_shape
                or first.status != "succeeded"
                or retry.status != "retry_scheduled"
                or retry.call_sequence != first.call_sequence
                or retry.retry_attempt != 1
                or retry.model_id is not first.model_id
                or retry.provider_mode != first.provider_mode
                or retry.duration_ms != first.duration_ms
                or retry.failure_category is not ProviderFailureCategory.INVALID_RESPONSE
                or retry.source_audit_sequence <= first.source_audit_sequence
                or final.status not in {"failed", "succeeded"}
                or final.call_sequence != first.call_sequence + 1
                or final.retry_attempt != 1
                or final.model_id is not first.model_id
                or final.provider_mode != first.provider_mode
                or final.source_audit_sequence <= retry.source_audit_sequence
            ):
                raise PersistedDataIntegrityError(
                    "Persisted extraction retry chronology is invalid"
                )
        else:
            raise PersistedDataIntegrityError("Persisted extraction telemetry exceeds one V1 retry")

    for operation in (
        WorkflowOperation.COMPUTER_USE,
        WorkflowOperation.VERIFICATION,
    ):
        turns = tuple(by_operation[operation])
        if not turns:
            continue
        model_id = turns[0].model_id
        provider_mode = turns[0].provider_mode
        for expected_call_sequence, item in enumerate(turns, start=1):
            if (
                item.call_sequence != expected_call_sequence
                or item.retry_attempt != 0
                or item.status not in {"failed", "succeeded"}
                or item.model_id is not model_id
                or item.provider_mode != provider_mode
                or (item.status == "failed" and expected_call_sequence != len(turns))
            ):
                raise PersistedDataIntegrityError(
                    "Persisted multi-turn provider chronology is invalid"
                )

    if any(
        item.status == "retry_scheduled" and item.operation is not WorkflowOperation.EXTRACTION
        for item in provider
    ):
        raise PersistedDataIntegrityError(
            "Persisted provider retry belongs to a non-retryable operation"
        )


def _completed_tool_metric_events(
    workflow: tuple[SequencedWorkflowEvent, ...],
) -> tuple[ToolCallWorkflowEvent, ...]:
    """Return unique terminal tool calls after validating optional start events."""

    started: dict[str, tuple[int, AllowedTool]] = {}
    terminal: set[str] = set()
    completed: list[ToolCallWorkflowEvent] = []
    for item in workflow:
        event = item.envelope.event
        if not isinstance(event, ToolCallWorkflowEvent):
            continue
        identity = event.invocation_id
        if event.status is ToolCallStatus.STARTED:
            if identity in started or identity in terminal:
                raise PersistedDataIntegrityError("Persisted tool invocation start is duplicated")
            started[identity] = (event.sequence, event.tool)
            continue
        if identity in terminal:
            raise PersistedDataIntegrityError("Persisted terminal tool invocation is duplicated")
        origin = started.get(identity)
        if origin is not None and origin != (event.sequence, event.tool):
            raise PersistedDataIntegrityError(
                "Persisted terminal tool invocation changed its identity"
            )
        terminal.add(identity)
        completed.append(event)
    return tuple(completed)


def _validate_snapshot(case_id: str, state: CaseState, snapshot: CaseSnapshot) -> None:
    validate_redacted_metadata(snapshot.redacted_metadata)
    validate_portal_state(state, snapshot.portal_state)
    packet = snapshot.claim_packet
    if packet is None:
        return
    if packet.case_id != case_id:
        raise ValueError("ClaimPacket caseId must match the persisted case")
    if packet.state is not state:
        raise ValueError("ClaimPacket state must match the persisted CaseState")
    if packet.portal_state is not snapshot.portal_state:
        raise ValueError("ClaimPacket portalState must match the persisted PortalState")


def _validate_audit_projection_binding(
    audit: AuditEvent,
    envelope: WorkflowEventEnvelope,
) -> None:
    if (
        envelope.source_audit_event_id != audit.event_id
        or envelope.source_audit_event_type is not audit.event_type
        or envelope.case_id != audit.case_id
        or envelope.occurred_at != audit.occurred_at
    ):
        raise PersistedDataIntegrityError(
            "Persisted workflow source identity disagrees with its audit event"
        )
    expected_kind = _WORKFLOW_KIND_BY_AUDIT_EVENT_TYPE.get(audit.event_type)
    if expected_kind is None or envelope.event.kind is not expected_kind:
        raise PersistedDataIntegrityError(
            "Persisted workflow kind does not match its audit event type"
        )
    event = envelope.event
    if isinstance(event, StateWorkflowEvent):
        if (
            event.actor is not audit.actor
            or event.from_state is not audit.from_state
            or event.to_state is not audit.to_state
        ):
            raise PersistedDataIntegrityError(
                "Persisted state projection disagrees with its audit event"
            )
    elif isinstance(event, GateWorkflowEvent) and (
        event.decision.decided_at != audit.occurred_at
        or event.decision.reason_codes != audit.reason_codes
    ):
        raise PersistedDataIntegrityError(
            "Persisted gate projection disagrees with its audit event"
        )


def _gate_decision_from_row(
    row: sqlite3.Row,
    *,
    label: str,
) -> tuple[str, GateDecision]:
    case_id = _require_string(row["case_id"], f"{label} case id")
    decision = GateDecision.model_validate_json(_require_string(row["decision_json"], label))
    if decision.gate_id.value != _require_string(
        row["gate_id"], f"{label} id"
    ) or decision.decided_at != _parse_datetime(
        _require_string(row["decided_at"], f"{label} decided_at")
    ):
        raise PersistedDataIntegrityError("Persisted gate columns disagree with canonical JSON")
    return case_id, decision


def _validate_projected_gate_and_state_histories(
    *,
    case_states: dict[str, CaseState],
    gate_decisions_by_case: dict[str, list[GateDecision]],
    workflows_by_sequence: dict[int, WorkflowEventEnvelope],
) -> None:
    ordered_workflows = tuple(
        workflows_by_sequence[sequence] for sequence in sorted(workflows_by_sequence)
    )
    validate_workflow_event_order(ordered_workflows)

    projected_gates_by_case: dict[str, list[GateDecision]] = {}
    for envelope in ordered_workflows:
        if isinstance(envelope.event, GateWorkflowEvent):
            projected_gates_by_case.setdefault(envelope.case_id, []).append(envelope.event.decision)
    if gate_decisions_by_case != projected_gates_by_case:
        raise PersistedDataIntegrityError(
            "Persisted gate decisions disagree with their workflow projections"
        )

    replayed_states = {case_id: CaseState.CREATED for case_id in case_states}
    for envelope in ordered_workflows:
        event = envelope.event
        if not isinstance(event, StateWorkflowEvent):
            continue
        replayed = replayed_states.get(envelope.case_id)
        if replayed is None or event.from_state is not replayed:
            raise PersistedDataIntegrityError("Persisted state workflow history is not contiguous")
        validate_case_transition(replayed, event.to_state)
        replayed_states[envelope.case_id] = event.to_state
    if any(replayed_states[case_id] is not state for case_id, state in case_states.items()):
        raise PersistedDataIntegrityError(
            "Persisted case state disagrees with replayed workflow history"
        )


def _validate_provider_usage_binding(
    envelope: WorkflowEventEnvelope,
    record: ProviderUsageLedgerRecord | None,
) -> None:
    event = envelope.event
    if not isinstance(
        event,
        ProviderCallWorkflowEvent | RetryWorkflowEvent | OperationalFailureWorkflowEvent,
    ):
        if record is not None:
            raise PersistedDataIntegrityError(
                "Non-provider workflow event has provider usage telemetry"
            )
        return
    if record is None:
        raise PersistedDataIntegrityError("Provider workflow event is missing usage telemetry")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_micros: int | None = None
    currency: str | None = None
    pricing_snapshot_id: str | None = None
    failure_category: ProviderFailureCategory | None = None
    if isinstance(event, ProviderCallWorkflowEvent):
        status = "succeeded"
        if event.usage is not None:
            input_tokens = event.usage.input_tokens
            output_tokens = event.usage.output_tokens
            total_tokens = event.usage.total_tokens
        if event.cost is not None:
            estimated_cost_micros = event.cost.estimated_cost_micros
            currency = event.cost.currency
            pricing_snapshot_id = event.cost.pricing_snapshot_id
    elif isinstance(event, RetryWorkflowEvent):
        status = "retry_scheduled"
        failure_category = event.failure.category
    else:
        status = "failed"
        failure_category = event.failure.category

    if (
        record.source_audit_sequence != envelope.source_audit_sequence
        or record.case_id != envelope.case_id
        or record.operation is not event.operation
        or record.model_id is not event.model_id
        or record.provider_mode != event.provider_mode
        or record.call_sequence != event.call_sequence
        or record.retry_attempt != event.retry_attempt
        or record.duration_ms != event.duration_ms
        or record.status != status
        or record.input_tokens != input_tokens
        or record.output_tokens != output_tokens
        or record.total_tokens != total_tokens
        or record.estimated_cost_micros != estimated_cost_micros
        or record.currency != currency
        or record.pricing_snapshot_id != pricing_snapshot_id
        or record.failure_category is not failure_category
        or record.occurred_at != envelope.occurred_at
    ):
        raise PersistedDataIntegrityError(
            "Persisted provider usage disagrees with its workflow event"
        )


class SqliteCaseRepository:
    """Store case snapshots with atomic state events and compare-and-swap versions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        media_root: str | Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if type(self) is not SqliteCaseRepository:
            raise TypeError("SqliteCaseRepository cannot be subclassed")
        self._configure(
            database_path,
            media_root=media_root,
            busy_timeout_ms=busy_timeout_ms,
            authority_application_id=CANONICAL_AUTHORITY_APPLICATION_ID,
        )

    @classmethod
    def _open_legacy_backend(
        cls,
        database_path: str | Path,
        *,
        media_root: str | Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> SqliteCaseRepository:
        """Construct the exact backend type for the isolated dev-only wrapper."""

        if cls is not SqliteCaseRepository:
            raise TypeError("Legacy backend construction requires the exact repository type")
        instance = object.__new__(cls)
        instance._configure(
            database_path,
            media_root=media_root,
            busy_timeout_ms=busy_timeout_ms,
            authority_application_id=LEGACY_AUTHORITY_APPLICATION_ID,
        )
        return instance

    def _configure(
        self,
        database_path: str | Path,
        *,
        media_root: str | Path | None,
        busy_timeout_ms: int,
        authority_application_id: int,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._authority_application_id = authority_application_id
        selected_media_root = (
            Path(media_root)
            if media_root is not None
            else self.database_path.parent / f"{self.database_path.stem}-media"
        )
        self.initialize()
        from claimdone_api.media import (
            CaseMediaStore,
            MediaStorageError,
            UnsafeStoragePath,
        )

        media_store: CaseMediaStore | None = None
        try:
            with self._write_connection() as connection:
                if self.is_canonical_authority:
                    # Revalidate inside the write-reserving transaction so a
                    # concurrent intake cannot appear between the DB preflight
                    # and the choice to open-only or initialize media storage.
                    self._preflight_canonical_payloads(
                        connection,
                        legacy=False,
                        verify_media=False,
                    )
                require_existing = self._has_persisted_intake_authority(connection)
                try:
                    media_store = CaseMediaStore(
                        selected_media_root,
                        require_existing=require_existing,
                    )
                except (MediaStorageError, UnsafeStoragePath) as error:
                    if require_existing:
                        raise PersistedDataIntegrityError(
                            "Persisted intake authority has no valid owned media root"
                        ) from error
                    raise
                self.__media_store = media_store
                if self.is_canonical_authority:
                    self._preflight_canonical_payloads(
                        connection,
                        legacy=False,
                        verify_media=True,
                    )
        except BaseException:
            if media_store is not None:
                media_store.close()
            raise

    def _has_persisted_intake_authority(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        if not self._table_exists(connection, "case_intake_authority"):
            return False
        return (
            connection.execute("SELECT 1 FROM case_intake_authority LIMIT 1").fetchone() is not None
        )

    @property
    def is_canonical_authority(self) -> bool:
        return self._authority_application_id == CANONICAL_AUTHORITY_APPLICATION_ID

    @property
    def media_store(self) -> CaseMediaStore:
        """Return the exact repository-owned store selected by composition."""

        return self.__media_store

    def _require_canonical_authority_mode(self) -> None:
        if self.is_canonical_authority is not True:
            raise AuthorityModeMismatchError(
                "Canonical workflow authority is unavailable in legacy mode"
            )

    def _connect(self, *, foreign_keys: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _execute_initialization_statement(
        self,
        connection: sqlite3.Connection,
        statement: str,
    ) -> sqlite3.Cursor:
        """Retry transient SQLite lock errors only during repository initialization."""

        deadline = monotonic() + max((self.busy_timeout_ms / 1_000) * 2, 0.1)
        while True:
            try:
                return connection.execute(statement)
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", None)
                base_code = None if code is None else int(code) & 0xFF
                locked = base_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
                    marker in str(error).lower() for marker in ("busy", "locked")
                )
                remaining = deadline - monotonic()
                if not locked or remaining <= 0:
                    raise
                sleep(min(0.01, remaining))

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        """Hold one WAL snapshot across a composite canonical read."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()

    def initialize(self) -> None:
        """Create or atomically migrate the schema and validate canonical payloads."""

        with _repository_initialization_lock(self.database_path):
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        """Initialize while holding the process-local lock for this database path."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        expected_application_id = self._authority_application_id
        fresh_unmarked = False
        with closing(self._connect()) as connection:
            application_id_row = self._execute_initialization_statement(
                connection,
                "PRAGMA application_id",
            ).fetchone()
            version_row = self._execute_initialization_statement(
                connection,
                "PRAGMA user_version",
            ).fetchone()
            if application_id_row is None or version_row is None:
                raise PersistenceError("SQLite did not report repository identity")
            application_id = int(application_id_row[0])
            existing_version = int(version_row[0])
            if application_id == 0:
                if existing_version != 0 or not self._user_schema_is_empty(connection):
                    raise AuthorityModeMismatchError(
                        "An existing unmarked database cannot be adopted as repository authority"
                    )
                fresh_unmarked = True
            elif application_id != expected_application_id:
                raise AuthorityModeMismatchError(
                    "Repository authority mode does not match the persisted database"
                )
            version = existing_version
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"Database schema {version} is newer than supported version {SCHEMA_VERSION}"
                )
            self._require_no_foreign_key_violations(connection)
            if version > 0:
                self._preflight_canonical_payloads(
                    connection,
                    legacy=version < SCHEMA_VERSION,
                    verify_media=False,
                )
            if version == SCHEMA_VERSION:
                self._enable_wal(connection)
                return

        # SQLite ignores PRAGMA foreign_keys changes inside a transaction. The
        # cases-table rebuild therefore uses one dedicated connection whose FK
        # mode is disabled before BEGIN and is restored before it is closed.
        with closing(self._connect(foreign_keys=False)) as connection:
            mode = connection.execute("PRAGMA foreign_keys").fetchone()
            if mode is None or int(mode[0]) != 0:
                raise PersistenceError("SQLite foreign keys could not be disabled for migration")
            self._execute_initialization_statement(
                connection,
                "BEGIN EXCLUSIVE",
            )
            try:
                application_row = connection.execute("PRAGMA application_id").fetchone()
                version_row = connection.execute("PRAGMA user_version").fetchone()
                if application_row is None or version_row is None:
                    raise PersistenceError("SQLite lost its repository identity")
                application_id = int(application_row[0])
                version = int(version_row[0])
                if fresh_unmarked:
                    if application_id == expected_application_id and version == SCHEMA_VERSION:
                        # A different process completed the same authority claim
                        # while this opener waited for the exclusive lock.
                        fresh_unmarked = False
                    elif (
                        application_id != 0
                        or version != 0
                        or not self._user_schema_is_empty(connection)
                    ):
                        raise AuthorityModeMismatchError(
                            "Fresh repository identity changed before initialization"
                        )
                elif application_id != expected_application_id:
                    raise AuthorityModeMismatchError(
                        "Repository authority mode changed during initialization"
                    )
                if version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Database schema {version} is newer than supported version "
                        f"{SCHEMA_VERSION}"
                    )
                if version > 0:
                    self._preflight_canonical_payloads(
                        connection,
                        legacy=True,
                        verify_media=False,
                    )
                if version == 0:
                    for statement in _MIGRATION_1:
                        connection.execute(statement)
                    version = 1
                    connection.execute("PRAGMA user_version = 1")
                if version == 1:
                    for statement in _MIGRATION_2:
                        connection.execute(statement)
                    version = 2
                    connection.execute("PRAGMA user_version = 2")
                if version == 2:
                    self._migrate_v2_to_v3(connection)
                    version = 3
                    connection.execute("PRAGMA user_version = 3")
                if version == 3:
                    self._migrate_v3_to_v4(connection)
                    version = 4
                    connection.execute("PRAGMA user_version = 4")
                if version == 4:
                    self._migrate_v4_to_v5(connection)
                    version = 5
                    connection.execute("PRAGMA user_version = 5")
                if version == 5:
                    self._migrate_v5_to_v6(connection)
                    version = 6
                    connection.execute("PRAGMA user_version = 6")
                if version == 6:
                    self._migrate_v6_to_v7(connection)
                    version = 7
                    connection.execute("PRAGMA user_version = 7")
                if version != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(f"Unsupported database schema: {version}")
                self._require_no_foreign_key_violations(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise PersistenceError("SQLite integrity check failed during migration")
                if fresh_unmarked:
                    connection.execute(f"PRAGMA application_id = {expected_application_id}")
                # Validate the fully migrated view before committing any DDL,
                # schema version, or fresh authority identity.  A rejected
                # legacy payload must leave the source database untouched.
                self._preflight_canonical_payloads(
                    connection,
                    legacy=False,
                    verify_media=False,
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

        with closing(self._connect()) as connection:
            application_row = connection.execute("PRAGMA application_id").fetchone()
            if application_row is None or int(application_row[0]) != expected_application_id:
                raise AuthorityModeMismatchError(
                    "Repository authority identity did not survive initialization"
                )
            self._require_no_foreign_key_violations(connection)
            self._preflight_canonical_payloads(
                connection,
                legacy=False,
                verify_media=False,
            )
            self._enable_wal(connection)

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Enable the persistent journal mode only after all rejectable validation."""

        journal_mode_row = self._execute_initialization_statement(
            connection,
            "PRAGMA journal_mode = WAL",
        ).fetchone()
        if journal_mode_row is None or str(journal_mode_row[0]).lower() != "wal":
            raise PersistenceError("SQLite database could not enter WAL mode")
        connection.execute("PRAGMA synchronous = NORMAL")

    @staticmethod
    def _user_schema_is_empty(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')
            LIMIT 1
            """
        ).fetchone()
        return row is None

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _require_no_foreign_key_violations(connection: sqlite3.Connection) -> None:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise PersistedDataIntegrityError("Persisted foreign-key integrity is invalid")

    def _preflight_canonical_payloads(
        self,
        connection: sqlite3.Connection,
        *,
        legacy: bool,
        verify_media: bool,
    ) -> None:
        """Validate every canonical JSON root without rewriting its version."""

        try:
            if not legacy:
                missing_tables = tuple(
                    table
                    for table in _REQUIRED_SCHEMA_TABLES
                    if not self._table_exists(connection, table)
                )
                if missing_tables:
                    raise PersistedDataIntegrityError(
                        "Current persistence schema is missing required tables"
                    )

            cases_by_id: dict[str, CaseRecord] = {}
            audits_by_sequence: dict[int, AuditEvent] = {}
            gate_decisions_by_case: dict[str, list[GateDecision]] = {}
            workflows_by_sequence: dict[int, WorkflowEventEnvelope] = {}
            transcripts: list[TranscriptRecord] = []
            capabilities: list[AuthorityCapabilityRecord] = []
            provider_usage_by_sequence: dict[int, ProviderUsageLedgerRecord] = {}
            workflow_table_exists = self._table_exists(connection, "workflow_events")
            transcript_table_exists = self._table_exists(connection, "case_transcripts")

            if self._table_exists(connection, "cases"):
                for row in connection.execute("SELECT * FROM cases ORDER BY case_id"):
                    case = self._row_to_case(row)
                    cases_by_id[case.case_id] = case
            if self._table_exists(connection, "audit_events"):
                for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
                    sequence = _require_integer(row["sequence"], "audit sequence")
                    audit = AuditEvent.model_validate_json(
                        _require_string(row["event_json"], "audit event")
                    )
                    if (
                        audit.event_id != _require_string(row["event_id"], "audit event id")
                        or audit.case_id != _require_string(row["case_id"], "audit case id")
                        or audit.occurred_at
                        != _parse_datetime(_require_string(row["occurred_at"], "audit occurred_at"))
                    ):
                        raise PersistedDataIntegrityError(
                            "Persisted audit columns disagree with canonical JSON"
                        )
                    audits_by_sequence[sequence] = audit
            if self._table_exists(connection, "gate_decisions"):
                for row in connection.execute("SELECT * FROM gate_decisions ORDER BY sequence"):
                    case_id, decision = _gate_decision_from_row(
                        row,
                        label="gate decision",
                    )
                    gate_decisions_by_case.setdefault(case_id, []).append(decision)
            if workflow_table_exists:
                for row in connection.execute(
                    "SELECT * FROM workflow_events ORDER BY source_audit_sequence"
                ):
                    envelope = WorkflowEventEnvelope.model_validate_json(
                        _require_string(row["event_json"], "workflow event")
                    )
                    source_sequence = _require_integer(
                        row["source_audit_sequence"],
                        "workflow source sequence",
                    )
                    if (
                        envelope.cursor != source_sequence
                        or envelope.source_audit_sequence != source_sequence
                        or envelope.source_audit_event_id
                        != _require_string(
                            row["source_audit_event_id"],
                            "workflow source event id",
                        )
                        or envelope.source_audit_event_type.value
                        != _require_string(
                            row["source_audit_event_type"],
                            "workflow source event type",
                        )
                        or envelope.case_id != _require_string(row["case_id"], "workflow case id")
                        or envelope.event_id
                        != _require_string(row["event_id"], "workflow event id")
                        or envelope.event.kind.value
                        != _require_string(row["event_kind"], "workflow event kind")
                    ):
                        raise PersistedDataIntegrityError(
                            "Persisted workflow columns disagree with canonical JSON"
                        )
                    source = audits_by_sequence.get(source_sequence)
                    if source is None:
                        raise PersistedDataIntegrityError(
                            "Persisted workflow source audit event is missing"
                        )
                    if (
                        source.event_id != envelope.source_audit_event_id
                        or source.case_id != envelope.case_id
                        or source.event_type is not envelope.source_audit_event_type
                        or source.occurred_at != envelope.occurred_at
                    ):
                        raise PersistedDataIntegrityError(
                            "Persisted workflow source identity is invalid"
                        )
                    _validate_audit_projection_binding(source, envelope)
                    workflows_by_sequence[source_sequence] = envelope
            if transcript_table_exists:
                for row in connection.execute("SELECT * FROM case_transcripts ORDER BY case_id"):
                    transcripts.append(self._row_to_transcript(row))
            if self._table_exists(connection, "provider_usage_ledger"):
                for row in connection.execute(
                    "SELECT * FROM provider_usage_ledger ORDER BY source_audit_sequence"
                ):
                    provider_record = self._row_to_provider_usage(row)
                    provider_usage_by_sequence[provider_record.source_audit_sequence] = (
                        provider_record
                    )
            if self._table_exists(connection, "authority_capabilities"):
                for row in connection.execute(
                    "SELECT * FROM authority_capabilities ORDER BY case_id, purpose"
                ):
                    capability = self._row_to_capability(
                        row,
                        allow_legacy_human_variant=legacy,
                    )
                    capabilities.append(capability)
                    if not self.is_canonical_authority:
                        bound_case = cases_by_id.get(capability.case_id)
                        if (
                            bound_case is None
                            or capability.bound_case_version > bound_case.version
                            or capability.issued_at < bound_case.created_at
                        ):
                            raise PersistedDataIntegrityError(
                                "Persisted capability is not bound to an existing case version"
                            )
            receipt_authority_exists = self._table_exists(
                connection,
                "sandbox_receipt_authority",
            )
            if receipt_authority_exists:
                self._validate_all_receipt_authority(connection)
            elif self._table_exists(connection, "sandbox_receipts"):
                for row in connection.execute("SELECT * FROM sandbox_receipts ORDER BY case_id"):
                    receipt = SandboxReceipt.model_validate_json(
                        _require_string(row["receipt_json"], "sandbox receipt")
                    )
                    receipt_case_id = _require_string(row["case_id"], "receipt case id")
                    receipt_created_at = _parse_datetime(
                        _require_string(row["created_at"], "receipt created_at")
                    )
                    bound_case = cases_by_id.get(receipt_case_id)
                    consumed_human_rows = connection.execute(
                        """
                        SELECT * FROM authority_capabilities
                        WHERE case_id = ? AND role = 'human'
                          AND purpose = 'human_approve' AND consumed_at IS NOT NULL
                        """,
                        (receipt_case_id,),
                    ).fetchall()
                    consumed_human = (
                        None
                        if len(consumed_human_rows) != 1
                        else self._row_to_capability(
                            consumed_human_rows[0],
                            allow_legacy_human_variant=legacy,
                        )
                    )
                    if receipt.case_id != receipt_case_id or (
                        self.is_canonical_authority
                        and (
                            bound_case is None
                            or bound_case.state is not CaseState.RECEIPT
                            or receipt.version != bound_case.version
                            or receipt_created_at != bound_case.updated_at
                            or receipt.approved_at < bound_case.created_at
                            or receipt.rendered_at != receipt_created_at
                            or consumed_human is None
                            or consumed_human.bound_case_version != bound_case.version - 2
                            or consumed_human.consumed_at is None
                            or not (
                                consumed_human.issued_at
                                < consumed_human.consumed_at
                                < receipt.approved_at
                                < receipt.rendered_at
                            )
                            or not receipt.approval_id.startswith(
                                f"approval-{receipt.variant.value.lower()}-"
                            )
                        )
                    ):
                        raise PersistedDataIntegrityError(
                            "Persisted receipt case authority is invalid"
                        )

            if workflow_table_exists:
                for sequence, audit in audits_by_sequence.items():
                    if audit.event_type not in _WORKFLOW_KIND_BY_AUDIT_EVENT_TYPE:
                        continue
                    required_envelope = workflows_by_sequence.get(sequence)
                    if required_envelope is None:
                        raise PersistedDataIntegrityError(
                            "Persisted audit event is missing its workflow projection"
                        )
                    _validate_audit_projection_binding(audit, required_envelope)

                for sequence in sorted(workflows_by_sequence):
                    envelope = workflows_by_sequence[sequence]
                    _validate_provider_usage_binding(
                        envelope,
                        provider_usage_by_sequence.get(sequence),
                    )
                if any(
                    sequence not in workflows_by_sequence for sequence in provider_usage_by_sequence
                ):
                    raise PersistedDataIntegrityError(
                        "Persisted provider usage has no workflow event"
                    )

                _validate_projected_gate_and_state_histories(
                    case_states={case_id: case.state for case_id, case in cases_by_id.items()},
                    gate_decisions_by_case=gate_decisions_by_case,
                    workflows_by_sequence=workflows_by_sequence,
                )

            transcripts_by_case = {transcript.case_id: transcript for transcript in transcripts}
            for transcript_record in transcripts:
                bound_case = cases_by_id.get(transcript_record.case_id)
                if bound_case is None or bound_case.snapshot.intake_summary is None:
                    raise PersistedDataIntegrityError(
                        "Persisted transcript has no bound intake summary"
                    )
                derived_id, derived_ref, derived_hash = _transcript_identity_from_summary(
                    transcript_record.case_id,
                    bound_case.snapshot.intake_summary,
                )
                if (
                    transcript_record.transcript_id != derived_id
                    or transcript_record.local_ref != derived_ref
                    or transcript_record.transcript_sha256 != derived_hash
                ):
                    raise PersistedDataIntegrityError(
                        "Persisted transcript identity disagrees with its intake summary"
                    )
            if transcript_table_exists and any(
                case.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
                and case.case_id not in transcripts_by_case
                for case in cases_by_id.values()
            ):
                raise PersistedDataIntegrityError(
                    "Case awaiting transcript confirmation has no bound transcript"
                )

            canonical_authority_tables_exist = self._table_exists(
                connection,
                "case_intake_authority",
            )
            if self.is_canonical_authority and (not legacy or canonical_authority_tables_exist):
                version_origins_by_case: dict[str, dict[int, datetime]] = {}
                for case in cases_by_id.values():
                    self._validate_canonical_case_snapshot(case)
                    version_origins_by_case[case.case_id] = self._validate_canonical_case_replay(
                        connection,
                        case,
                        tuple(
                            workflows_by_sequence[sequence]
                            for sequence in sorted(workflows_by_sequence)
                            if workflows_by_sequence[sequence].case_id == case.case_id
                        ),
                        audits_by_sequence=audits_by_sequence,
                    )
                    history = tuple(gate_decisions_by_case.get(case.case_id, ()))
                    if case.state is CaseState.CREATED:
                        if history:
                            raise WorkflowAtomicityError(
                                "A created canonical case cannot have gate history"
                            )
                    elif case.state in {
                        CaseState.DISCLOSED,
                        CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                    }:
                        self._require_passed_g0_g1_history(connection, case)
                    elif case.state in {
                        CaseState.ANALYZING,
                        CaseState.AWAITING_CLARIFICATION,
                    }:
                        self._validate_analysis_history(case, history)
                for capability in capabilities:
                    bound_case = cases_by_id.get(capability.case_id)
                    if bound_case is None:
                        raise PersistedDataIntegrityError("Persisted capability has no bound case")
                    self._validate_capability_case_binding(
                        capability,
                        version_origins=version_origins_by_case[capability.case_id],
                    )
                authority_case_ids = {
                    _require_string(row["case_id"], "intake authority case id")
                    for row in connection.execute("SELECT case_id FROM case_intake_authority")
                }
                handle_created_at_by_case: dict[str, datetime] = {}
                for row in connection.execute("SELECT case_id, created_at FROM case_media_handles"):
                    handle_case_id = _require_string(
                        row["case_id"],
                        "media handle case id",
                    )
                    handle_created_at_by_case[handle_case_id] = _parse_datetime(
                        _require_string(row["created_at"], "media handle created_at")
                    )
                handle_case_ids = set(handle_created_at_by_case)
                required_authority_case_ids = {
                    case.case_id
                    for case in cases_by_id.values()
                    if case.state is not CaseState.CREATED
                    or case.snapshot.intake_summary is not None
                }
                if (
                    authority_case_ids != handle_case_ids
                    or not required_authority_case_ids.issubset(authority_case_ids)
                ):
                    raise PersistedDataIntegrityError(
                        "Canonical cases lost their intake authority binding"
                    )
                transcript_authority_case_ids = {
                    _require_string(row["case_id"], "transcript authority case id")
                    for row in connection.execute("SELECT case_id FROM case_transcript_authority")
                }
                if transcript_authority_case_ids != set(transcripts_by_case):
                    raise PersistedDataIntegrityError(
                        "Canonical transcripts lost their authority binding"
                    )
                for case_id in sorted(authority_case_ids):
                    bound_case = cases_by_id.get(case_id)
                    if bound_case is None:
                        raise PersistedDataIntegrityError("Intake authority has no bound case")
                    manifest, manifest_digest = self._require_intake_authority(
                        connection,
                        bound_case,
                        verify_media=verify_media,
                    )
                    handle_created_at = handle_created_at_by_case[case_id]
                    if not (bound_case.created_at <= handle_created_at <= bound_case.updated_at):
                        raise PersistedDataIntegrityError(
                            "Media handle timestamp falls outside its case lifetime"
                        )
                    has_transcript = case_id in transcript_authority_case_ids
                    statement = bound_case.snapshot.intake_summary
                    statement_value = None if statement is None else statement.get("statement")
                    if (
                        manifest.get("inputMode") == "audio"
                        and statement_value is not None
                        and not has_transcript
                    ):
                        raise PersistedDataIntegrityError(
                            "Audio statement lost its transcript authority"
                        )
                    if has_transcript:
                        if manifest.get("inputMode") != "audio":
                            raise PersistedDataIntegrityError(
                                "Transcript authority is not bound to audio intake"
                            )
                        transcript = self._require_transcript_authority(
                            connection,
                            bound_case,
                            intake_manifest_digest=manifest_digest,
                            verify_media=verify_media,
                        )
                        self._validate_transcript_case_binding(
                            connection,
                            bound_case,
                            transcript,
                        )
                if self._table_exists(connection, "case_packet_authority"):
                    for case in cases_by_id.values():
                        self._require_current_packet_authority(connection, case)
                elif any(case.snapshot.claim_packet is not None for case in cases_by_id.values()):
                    raise WorkflowAtomicityError(
                        "Pre-v5 canonical data cannot retain mutable ClaimPackets"
                    )
                if self._table_exists(connection, "portal_run_authority"):
                    self._validate_all_cu_verification_authority(
                        connection,
                        cases_by_id=cases_by_id,
                    )
        except (
            PersistedDataIntegrityError,
            TranscriptStateError,
            WorkflowAtomicityError,
            ValidationError,
            ValueError,
            TypeError,
            KeyError,
        ) as error:
            if legacy:
                raise IncompatiblePersistedContractError() from error
            raise PersistedDataIntegrityError(
                "Persisted canonical JSON does not match the current contracts"
            ) from error

    def _validate_all_cu_verification_authority(
        self,
        connection: sqlite3.Connection,
        *,
        cases_by_id: dict[str, CaseRecord],
    ) -> None:
        """Replay every retained G6-G8 proof and reject missing or forged authority."""

        run_rows = connection.execute(
            "SELECT * FROM portal_run_authority ORDER BY case_id"
        ).fetchall()
        run_case_ids = {_require_string(row["case_id"], "portal run case id") for row in run_rows}
        g6_case_ids = {
            _require_string(row["case_id"], "G6 case id")
            for row in connection.execute(
                "SELECT case_id FROM gate_decisions WHERE gate_id = ?",
                (GateId.G6_TOOL_AUTHORITY.value,),
            )
        }
        if run_case_ids != g6_case_ids:
            raise WorkflowAtomicityError("G6 history and portal run authority disagree")
        child_case_ids = {
            _require_string(row["case_id"], "CU child case id")
            for table in ("portal_session_authority", "verification_attempt_authority")
            for row in connection.execute(f"SELECT case_id FROM {table}")
        }
        if not child_case_ids.issubset(run_case_ids):
            raise WorkflowAtomicityError("CU child authority has no portal run")

        def packet_at(current: CaseRecord, version: int) -> ClaimPacket:
            packet_row = connection.execute(
                """
                SELECT * FROM case_packet_authority
                WHERE case_id = ? AND bound_case_version = ?
                """,
                (current.case_id, version),
            ).fetchone()
            if packet_row is None:
                raise WorkflowAtomicityError("CU authority lost a packet checkpoint")
            history = self._read_gate_decisions(connection, case_id=current.case_id)
            return self._validate_packet_authority_row(
                packet_row,
                current=current,
                history=history,
            )[2]

        def workflow_at(sequence: int, label: str) -> WorkflowEventEnvelope:
            workflow_row = connection.execute(
                """
                SELECT * FROM workflow_events WHERE source_audit_sequence = ?
                """,
                (sequence,),
            ).fetchone()
            if workflow_row is None:
                raise WorkflowAtomicityError(f"{label} lost its workflow projection")
            return self._workflow_envelope_from_row(workflow_row, label=label)

        for row in run_rows:
            run = self._row_to_portal_run(connection, row)
            current = cases_by_id.get(run.case_id)
            if current is None:
                raise WorkflowAtomicityError("Portal run has no case")
            ready_packet = packet_at(current, run.ready_case_version)
            if (
                ready_packet.state is not CaseState.READY_TO_FILL
                or tuple(decision.gate_id for decision in ready_packet.gate_decisions)
                != _ANALYSIS_GATE_SEQUENCE
                or any(not decision.passed for decision in ready_packet.gate_decisions)
            ):
                raise WorkflowAtomicityError("Portal run has no exact READY packet")
            _validate_prestage_session(
                run.prestage_session,
                case_id=run.case_id,
                variant=run.portal_variant,
                attachments=ready_packet.claim.attachments,
            )
            ready_packet_row = connection.execute(
                """
                SELECT created_at FROM case_packet_authority
                WHERE case_id = ? AND bound_case_version = ?
                """,
                (run.case_id, run.ready_case_version),
            ).fetchone()
            capability_row = connection.execute(
                "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
                (row["agent_capability_digest"],),
            ).fetchone()
            if ready_packet_row is None or capability_row is None:
                raise WorkflowAtomicityError("Portal run lost its prestage chronology")
            ready_packet_at = _parse_datetime(
                _require_string(ready_packet_row["created_at"], "READY packet created_at")
            )
            capability = self._row_to_capability(capability_row)
            if capability.consumed_at is None or not (
                ready_packet_at
                <= run.prestage_session.updated_at
                <= capability.consumed_at
                < run.created_at
            ):
                raise WorkflowAtomicityError("Portal run prestage chronology is invalid")
            context = _load_json_object(_require_string(row["g6_context_json"], "G6 context"))
            if set(context) != {
                "caseId",
                "portalVariant",
                "currentUrl",
                "action",
                "proposedActionNumber",
                "elapsedSeconds",
            }:
                raise WorkflowAtomicityError("Persisted G6 context shape is invalid")
            elapsed_seconds = context["elapsedSeconds"]
            if type(elapsed_seconds) is not float:
                raise WorkflowAtomicityError("Persisted G6 elapsed time is invalid")
            filling_input = _rebind_claim_packet(
                ready_packet,
                state=CaseState.FILLING,
                portal_state=PortalState.DRAFT,
                gates=ready_packet.gate_decisions,
                verification=ready_packet.verification,
            )
            recomputed_g6 = evaluate_g6(
                run.invocation.model_dump(mode="json", by_alias=True),
                context=ToolAuthorityContext(
                    packet=filling_input,
                    case_state=CaseState.FILLING,
                    portal_variant=PortalVariant(
                        _require_string(context["portalVariant"], "G6 variant")
                    ),
                    current_url=_require_string(context["currentUrl"], "G6 URL"),
                    action=_require_string(context["action"], "G6 action"),
                    proposed_action_number=_require_integer(
                        context["proposedActionNumber"],
                        "G6 proposed action number",
                    ),
                    elapsed_seconds=elapsed_seconds,
                ),
                decided_at=run.g6_decision.decided_at,
            ).decision
            if (
                context["caseId"] != run.case_id
                or context["portalVariant"] != run.portal_variant.value
                or recomputed_g6 != run.g6_decision
                or packet_at(current, run.g6_case_version).gate_decisions[-1] != run.g6_decision
            ):
                raise WorkflowAtomicityError("Persisted G6 no longer recomputes")

            session_rows = connection.execute(
                """
                SELECT * FROM portal_session_authority
                WHERE case_id = ? ORDER BY checkpoint_number
                """,
                (run.case_id,),
            ).fetchall()
            attempt_rows = connection.execute(
                """
                SELECT * FROM verification_attempt_authority
                WHERE case_id = ? ORDER BY attempt_number
                """,
                (run.case_id,),
            ).fetchall()
            for expected_checkpoint, session_row in enumerate(session_rows, start=1):
                expected_kind = "reviewed" if expected_checkpoint == 1 else "repair"
                if (
                    _require_string(session_row["case_id"], "portal checkpoint case id")
                    != run.case_id
                    or _require_string(session_row["run_id"], "portal checkpoint run id")
                    != run.run_id
                    or _require_integer(
                        session_row["checkpoint_number"],
                        "portal checkpoint number",
                    )
                    != expected_checkpoint
                    or _require_string(
                        session_row["checkpoint_kind"],
                        "portal checkpoint kind",
                    )
                    != expected_kind
                ):
                    raise WorkflowAtomicityError(
                        "Portal checkpoint is not bound to its run position"
                    )
            for expected_attempt, attempt_row in enumerate(attempt_rows, start=1):
                if (
                    _require_string(attempt_row["case_id"], "verification case id")
                    != run.case_id
                    or _require_string(attempt_row["run_id"], "verification run id")
                    != run.run_id
                    or _require_integer(
                        attempt_row["attempt_number"],
                        "verification attempt number",
                    )
                    != expected_attempt
                ):
                    raise WorkflowAtomicityError(
                        "Verification attempt is not bound to its run position"
                    )
            if run.status == "filling":
                if (
                    current.state is not CaseState.FILLING
                    or current.version != run.g6_case_version
                    or session_rows
                    or attempt_rows
                    or any(
                        row[column] is not None
                        for column in (
                            "g7_gate_sequence",
                            "tool_terminal_audit_sequence",
                            "portal_fill_audit_sequence",
                            "g7_state_audit_sequence",
                            "rejected_summary_json",
                            "rejected_summary_sha256",
                        )
                    )
                ):
                    raise WorkflowAtomicityError("Open G6 run authority is invalid")
                continue
            if run.status == "blocked_g6":
                if (
                    current.state is not CaseState.BLOCKED
                    or current.version != run.g6_case_version
                    or run.terminal_case_version != run.g6_case_version
                    or run.terminal_at != run.created_at
                    or session_rows
                    or attempt_rows
                    or any(
                        row[column] is not None
                        for column in (
                            "g7_gate_sequence",
                            "tool_terminal_audit_sequence",
                            "portal_fill_audit_sequence",
                            "g7_state_audit_sequence",
                            "rejected_summary_json",
                            "rejected_summary_sha256",
                        )
                    )
                ):
                    raise WorkflowAtomicityError("Blocked G6 run authority is invalid")
                continue

            g7_sequence_raw = row["g7_gate_sequence"]
            if g7_sequence_raw is None or run.terminal_case_version is None:
                raise WorkflowAtomicityError("Terminal portal run lost G7")
            g7_row = connection.execute(
                "SELECT * FROM gate_decisions WHERE sequence = ?",
                (_require_integer(g7_sequence_raw, "G7 gate sequence"),),
            ).fetchone()
            if g7_row is None:
                raise WorkflowAtomicityError("Terminal portal run lost G7")
            g7_case_id, g7 = _gate_decision_from_row(g7_row, label="G7 decision")
            filling_packet = packet_at(current, run.g6_case_version)
            tool_sequence = _require_integer(
                row["tool_terminal_audit_sequence"],
                "terminal tool sequence",
            )
            tool_event = workflow_at(tool_sequence, "terminal tool").event
            state_sequence = _require_integer(
                row["g7_state_audit_sequence"],
                "G7 state sequence",
            )
            state_event = workflow_at(state_sequence, "G7 state").event
            if (
                g7_case_id != run.case_id
                or g7.gate_id is not GateId.G7_PORTAL_WRITE
                or g7.decided_at != run.terminal_at
                or not isinstance(tool_event, ToolCallWorkflowEvent)
                or tool_event.invocation_id != run.invocation.invocation_id
                or tool_event.sequence != run.invocation.sequence
                or tool_event.tool is not run.invocation.tool
                or not isinstance(state_event, StateWorkflowEvent)
                or state_event.from_state is not CaseState.FILLING
                or state_event.to_state
                is not (CaseState.VERIFYING if g7.passed else CaseState.BLOCKED)
                or packet_at(current, run.terminal_case_version).gate_decisions[-1] != g7
            ):
                raise WorkflowAtomicityError("Persisted G7 event binding is invalid")

            if g7.passed:
                if (
                    run.status not in {"verifying", "review", "blocked_g8"}
                    or tool_event.status is not ToolCallStatus.SUCCEEDED
                    or len(session_rows) not in {1, 2}
                    or row["rejected_summary_json"] is not None
                    or row["rejected_summary_sha256"] is not None
                    or row["portal_fill_audit_sequence"] is None
                ):
                    raise WorkflowAtomicityError("Passed G7 authority is incomplete")
                reviewed_session, reviewed_rendered = self._portal_session_from_row(
                    session_rows[0]
                )
                reviewed_checkpoint_at = _parse_datetime(
                    _require_string(
                        session_rows[0]["created_at"],
                        "reviewed checkpoint created_at",
                    )
                )
                portal_event = workflow_at(
                    _require_integer(
                        row["portal_fill_audit_sequence"],
                        "portal fill sequence",
                    ),
                    "portal fill",
                ).event
                recomputed_g7 = evaluate_g7(
                    reviewed_session.fields.model_dump(mode="json", by_alias=True),
                    packet=filling_packet,
                    case_state=CaseState.FILLING,
                    portal_state=PortalState.DRAFT,
                    decided_at=g7.decided_at,
                ).decision
                if (
                    recomputed_g7 != g7
                    or reviewed_session.variant is not run.portal_variant
                    or reviewed_session.version != run.prestage_session.version + 2
                    or _require_integer(
                        session_rows[0]["portal_version"],
                        "reviewed checkpoint portal version",
                    )
                    != reviewed_session.version
                    or session_rows[0]["source_attempt_id"] is not None
                    or run.terminal_at is None
                    or reviewed_checkpoint_at != run.terminal_at
                    or not (
                        run.created_at
                        <= reviewed_session.updated_at
                        <= reviewed_rendered.rendered_at
                        <= reviewed_checkpoint_at
                    )
                    or not isinstance(portal_event, PortalFillWorkflowEvent)
                    or portal_event.variant is not run.portal_variant
                    or portal_event.portal_version != reviewed_session.version
                    or portal_event.written_fields != tuple(RequiredClaimField)
                ):
                    raise WorkflowAtomicityError("Passed G7 no longer recomputes")
            else:
                summary_json = row["rejected_summary_json"]
                summary_hash = row["rejected_summary_sha256"]
                if (
                    run.status != "blocked_g7"
                    or tool_event.status is not ToolCallStatus.BLOCKED
                    or session_rows
                    or attempt_rows
                    or row["portal_fill_audit_sequence"] is not None
                    or not isinstance(summary_json, str)
                    or not isinstance(summary_hash, str)
                ):
                    raise WorkflowAtomicityError("Rejected G7 authority is invalid")
                summary = _load_json_object(summary_json)
                if (
                    set(summary)
                    != {
                        "payloadType",
                        "fieldCount",
                        "recognizedFieldCount",
                        "reasonCodes",
                    }
                    or summary.get("payloadType") not in {"object", "other"}
                    or type(summary.get("fieldCount")) is not int
                    or type(summary.get("recognizedFieldCount")) is not int
                    or summary.get("reasonCodes") != [reason.value for reason in g7.reason_codes]
                    or _authority_sha256("g7-rejected-summary", summary_json) != summary_hash
                ):
                    raise WorkflowAtomicityError("Rejected G7 summary is invalid")
                continue

            attempts = tuple(
                self._verification_attempt_from_row(attempt_row) for attempt_row in attempt_rows
            )
            if not attempts and len(session_rows) != 1:
                raise WorkflowAtomicityError(
                    "G7 checkpoint cannot introduce an unverified repair"
                )
            if attempts:
                if tuple(attempt.attempt_number for attempt in attempts) != tuple(
                    range(1, len(attempts) + 1)
                ):
                    raise WorkflowAtomicityError("Verification attempts are not contiguous")
                for attempt_row, attempt in zip(attempt_rows, attempts, strict=True):
                    bound_version = _require_integer(
                        attempt_row["bound_case_version"],
                        "attempt bound case version",
                    )
                    if (
                        bound_version
                        != run.terminal_case_version + attempt.attempt_number
                        or _require_integer(
                            attempt_row["portal_checkpoint_number"],
                            "attempt portal checkpoint number",
                        )
                        != attempt.attempt_number
                    ):
                        raise WorkflowAtomicityError(
                            "Verification attempt cursor binding is invalid"
                        )
                    input_packet = packet_at(current, bound_version - 1)
                    rendered = RenderedPortalSnapshot.model_validate_json(
                        _require_string(
                            attempt_row["rendered_snapshot_json"],
                            "verification rendered snapshot",
                        )
                    )
                    requested_at = _parse_datetime(
                        _require_string(
                            attempt_row["snapshot_requested_at"],
                            "snapshot requested_at",
                        )
                    )
                    received_at = _parse_datetime(
                        _require_string(
                            attempt_row["snapshot_received_at"],
                            "snapshot received_at",
                        )
                    )
                    created_at = _parse_datetime(
                        _require_string(attempt_row["created_at"], "attempt created_at")
                    )
                    recomputed = evaluate_g8(
                        input_packet,
                        rendered,
                        expected_variant=run.portal_variant,
                        expected_portal_version=attempt.portal_version,
                        snapshot_requested_at=requested_at,
                        snapshot_received_at=received_at,
                        model_reported_mismatch=attempt.report.model_reported_mismatch,
                        verified_at=cast(datetime, attempt.report.verified_at),
                        decided_at=(
                            cast(GateDecision, attempt.gate_decision).decided_at
                            if attempt.final
                            else created_at
                        ),
                    )
                    verification_event = workflow_at(
                        _require_integer(
                            attempt_row["verification_audit_sequence"],
                            "verification event sequence",
                        ),
                        "verification attempt",
                    ).event
                    if (
                        recomputed.report != attempt.report
                        or (attempt.final and recomputed.decision != attempt.gate_decision)
                        or not isinstance(verification_event, VerificationWorkflowEvent)
                        or verification_event.attempt_number != attempt.attempt_number
                        or verification_event.status is not attempt.report.status
                        or verification_event.deterministic_match
                        is not attempt.report.deterministic_match
                        or verification_event.model_reported_mismatch
                        is not attempt.report.model_reported_mismatch
                        or verification_event.repair_used is not (attempt.attempt_number == 2)
                        or verification_event.final is not attempt.final
                    ):
                        raise WorkflowAtomicityError("Verification attempt no longer recomputes")
                    if attempt.final:
                        gate_row = connection.execute(
                            "SELECT * FROM gate_decisions WHERE sequence = ?",
                            (
                                _require_integer(
                                    attempt_row["g8_gate_sequence"],
                                    "G8 gate sequence",
                                ),
                            ),
                        ).fetchone()
                        if gate_row is None or _gate_decision_from_row(
                            gate_row, label="G8 decision"
                        ) != (run.case_id, attempt.gate_decision):
                            raise WorkflowAtomicityError("Final attempt lost G8")
                        final_state = workflow_at(
                            _require_integer(
                                attempt_row["state_audit_sequence"],
                                "G8 state sequence",
                            ),
                            "G8 state",
                        ).event
                        if (
                            not isinstance(final_state, StateWorkflowEvent)
                            or final_state.from_state is not CaseState.VERIFYING
                            or final_state.to_state
                            is not (
                                CaseState.REVIEW
                                if cast(GateDecision, attempt.gate_decision).passed
                                else CaseState.BLOCKED
                            )
                        ):
                            raise WorkflowAtomicityError("Final G8 state is invalid")
                expected_session_count = 2 if len(attempts) == 2 else 1
                if len(session_rows) != expected_session_count:
                    raise WorkflowAtomicityError(
                        "Verification attempts disagree with portal checkpoints"
                    )
                if len(attempts) == 2:
                    first_attempt, second_attempt = attempts
                    repair = first_attempt.repair
                    if repair is None:
                        raise WorkflowAtomicityError(
                            "Second checkpoint lost its repair authorization"
                        )
                    repaired_session, repaired_rendered = self._portal_session_from_row(
                        session_rows[1]
                    )
                    second_rendered = RenderedPortalSnapshot.model_validate_json(
                        _require_string(
                            attempt_rows[1]["rendered_snapshot_json"],
                            "second verification rendered snapshot",
                        )
                    )
                    second_requested_at = _parse_datetime(
                        _require_string(
                            attempt_rows[1]["snapshot_requested_at"],
                            "second snapshot requested_at",
                        )
                    )
                    first_created_at = _parse_datetime(
                        _require_string(
                            attempt_rows[0]["created_at"],
                            "first attempt created_at",
                        )
                    )
                    second_created_at = _parse_datetime(
                        _require_string(
                            attempt_rows[1]["created_at"],
                            "second attempt created_at",
                        )
                    )
                    repair_checkpoint_at = _parse_datetime(
                        _require_string(
                            session_rows[1]["created_at"],
                            "repair checkpoint created_at",
                        )
                    )
                    base_values = reviewed_session.fields.model_dump(
                        mode="json",
                        by_alias=False,
                    )
                    repaired_values = repaired_session.fields.model_dump(
                        mode="json",
                        by_alias=False,
                    )
                    expected_values = _canonical_claim_portal_fields(
                        filling_packet
                    ).model_dump(mode="json", by_alias=False)
                    for field_name, base_value in base_values.items():
                        if field_name == repair.field.value:
                            if repaired_values[field_name] != expected_values[field_name]:
                                raise WorkflowAtomicityError(
                                    "Repair checkpoint target is not canonical"
                                )
                        elif repaired_values[field_name] != base_value:
                            raise WorkflowAtomicityError(
                                "Repair checkpoint changed a non-target value"
                            )
                    if (
                        _require_string(
                            session_rows[1]["source_attempt_id"],
                            "repair source attempt id",
                        )
                        != first_attempt.attempt_id
                        or repaired_session.variant is not run.portal_variant
                        or repaired_session.version != reviewed_session.version + 1
                        or repaired_session.version != repair.to_portal_version
                        or second_attempt.portal_version != repaired_session.version
                        or repaired_rendered != second_rendered
                        or repair_checkpoint_at != second_created_at
                        or repaired_session.updated_at < first_created_at
                        or repaired_session.updated_at > second_requested_at
                    ):
                        raise WorkflowAtomicityError(
                            "Repair checkpoint authority binding is invalid"
                        )
                if attempts[-1].final:
                    VerificationAttemptSeries.model_validate(
                        {
                            "contractVersion": CONTRACT_VERSION,
                            "caseId": run.case_id,
                            "attempts": attempts,
                        }
                    )
                    expected_status = (
                        "review"
                        if cast(GateDecision, attempts[-1].gate_decision).passed
                        else "blocked_g8"
                    )
                    if run.status != expected_status:
                        raise WorkflowAtomicityError("Final G8 did not close the run")
                elif len(attempts) != 1 or run.status != "verifying":
                    raise WorkflowAtomicityError("Repairable attempt chain is invalid")
            elif run.status != "verifying":
                raise WorkflowAtomicityError("G7 run status has no verification authority")

            expected_states = {
                "verifying": {CaseState.VERIFYING},
                "review": {
                    CaseState.REVIEW,
                    CaseState.HUMAN_APPROVED,
                    CaseState.RECEIPT,
                },
                "blocked_g8": {CaseState.BLOCKED},
            }
            if current.state not in expected_states[run.status]:
                raise WorkflowAtomicityError("Case state disagrees with CU run status")
            assert run.terminal_case_version is not None
            verification_terminal_version = run.terminal_case_version + len(attempts)
            authority_version_offset = {
                CaseState.VERIFYING: 0,
                CaseState.BLOCKED: 0,
                CaseState.REVIEW: 0,
                CaseState.HUMAN_APPROVED: 1,
                CaseState.RECEIPT: 2,
            }[current.state]
            if current.version != verification_terminal_version + authority_version_offset:
                raise WorkflowAtomicityError("Case version disagrees with CU authority cursor")

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        """Rebuild the parent table, then create and backfill v3 projections."""

        connection.execute(_CASES_V3)
        columns = (
            "case_id, version, state, portal_state, redacted_metadata_json, "
            "claim_packet_json, intake_summary_json, active_clarification_json, "
            "created_at, updated_at"
        )
        connection.execute(f"INSERT INTO cases_v3 ({columns}) SELECT {columns} FROM cases")
        before = connection.execute("SELECT COUNT(*) FROM cases").fetchone()
        after = connection.execute("SELECT COUNT(*) FROM cases_v3").fetchone()
        if before is None or after is None or int(before[0]) != int(after[0]):
            raise PersistenceError("Cases-table migration did not preserve every row")
        connection.execute("DROP TABLE cases")
        connection.execute("ALTER TABLE cases_v3 RENAME TO cases")
        for statement in _MIGRATION_3:
            connection.execute(statement)
        self._backfill_v3_workflow_events(connection)
        self._backfill_v3_pending_transcripts(connection)

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        """Add authority storage without blessing unbound canonical intake data."""

        if self.is_canonical_authority:
            unsafe_case = connection.execute(
                """
                SELECT 1
                FROM cases
                WHERE state <> ?
                   OR intake_summary_json IS NOT NULL
                   OR claim_packet_json IS NOT NULL
                   OR active_clarification_json IS NOT NULL
                LIMIT 1
                """,
                (CaseState.CREATED.value,),
            ).fetchone()
            authority_child_tables = (
                "audit_events",
                "gate_decisions",
                "case_media_handles",
                "workflow_events",
                "case_transcripts",
                "provider_usage_ledger",
                "authority_capabilities",
                "sandbox_receipts",
            )
            populated_child = any(
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                for table in authority_child_tables
            )
            if unsafe_case is not None or populated_child:
                raise IncompatiblePersistedContractError()
        for statement in _MIGRATION_4:
            connection.execute(statement)

    def _migrate_v4_to_v5(self, connection: sqlite3.Connection) -> None:
        """Add packet authority without adopting mutable historical packets."""

        if self.is_canonical_authority:
            packet = connection.execute(
                "SELECT 1 FROM cases WHERE claim_packet_json IS NOT NULL LIMIT 1"
            ).fetchone()
            if packet is not None:
                raise IncompatiblePersistedContractError()
        for statement in _MIGRATION_5:
            connection.execute(statement)

    def _migrate_v5_to_v6(self, connection: sqlite3.Connection) -> None:
        """Add immutable receipt authority without guessing historical human intent."""

        unsafe_human_authority = connection.execute(
            """
            SELECT 1 FROM authority_capabilities WHERE role = 'human'
            UNION ALL
            SELECT 1 FROM sandbox_receipts
            UNION ALL
            SELECT 1 FROM cases WHERE state IN (?, ?)
            UNION ALL
            SELECT 1 FROM gate_decisions WHERE gate_id IN (?, ?)
            UNION ALL
            SELECT 1
            FROM audit_events
            WHERE json_extract(event_json, '$.eventType') IN (?, ?)
               OR (
                    json_extract(event_json, '$.eventType') = ?
                    AND (
                        json_extract(event_json, '$.fromState') IN (?, ?)
                        OR json_extract(event_json, '$.toState') IN (?, ?)
                    )
               )
            LIMIT 1
            """,
            (
                CaseState.HUMAN_APPROVED.value,
                CaseState.RECEIPT.value,
                GateId.G9_HUMAN_APPROVAL.value,
                GateId.G10_RECEIPT_REDACTION.value,
                AuditEventType.HUMAN_APPROVAL.value,
                AuditEventType.RECEIPT.value,
                AuditEventType.CASE_STATE_CHANGED.value,
                CaseState.HUMAN_APPROVED.value,
                CaseState.RECEIPT.value,
                CaseState.HUMAN_APPROVED.value,
                CaseState.RECEIPT.value,
            ),
        ).fetchone()
        if unsafe_human_authority is not None:
            raise IncompatiblePersistedContractError()
        for statement in _MIGRATION_6:
            connection.execute(statement)

    def _migrate_v6_to_v7(self, connection: sqlite3.Connection) -> None:
        """Add CU authority only when no historical CU intent must be guessed."""

        unsafe_cu_authority = connection.execute(
            """
            SELECT 1
            FROM cases
            WHERE state IN (?, ?, ?, ?, ?)
            UNION ALL
            SELECT 1
            FROM gate_decisions
            WHERE gate_id IN (?, ?, ?, ?, ?)
            UNION ALL
            SELECT 1
            FROM workflow_events
            WHERE event_kind IN (?, ?, ?)
            LIMIT 1
            """,
            (
                CaseState.FILLING.value,
                CaseState.VERIFYING.value,
                CaseState.REVIEW.value,
                CaseState.HUMAN_APPROVED.value,
                CaseState.RECEIPT.value,
                GateId.G6_TOOL_AUTHORITY.value,
                GateId.G7_PORTAL_WRITE.value,
                GateId.G8_VERIFICATION.value,
                GateId.G9_HUMAN_APPROVAL.value,
                GateId.G10_RECEIPT_REDACTION.value,
                WorkflowEventKind.TOOL_CALL.value,
                WorkflowEventKind.PORTAL_FILL.value,
                WorkflowEventKind.VERIFICATION.value,
            ),
        ).fetchone()
        if self.is_canonical_authority and unsafe_cu_authority is not None:
            raise IncompatiblePersistedContractError()
        for statement in _MIGRATION_7:
            connection.execute(statement)

    def _backfill_v3_workflow_events(self, connection: sqlite3.Connection) -> None:
        decisions_by_case: dict[str, list[GateDecision]] = {}
        for row in connection.execute(
            "SELECT case_id, decision_json FROM gate_decisions ORDER BY sequence"
        ):
            case_id = _require_string(row["case_id"], "gate case id")
            decisions_by_case.setdefault(case_id, []).append(
                GateDecision.model_validate_json(
                    _require_string(row["decision_json"], "gate decision")
                )
            )
        gate_offsets: dict[str, int] = {}
        for row in connection.execute(
            "SELECT sequence, event_json FROM audit_events ORDER BY sequence"
        ):
            sequence = _require_integer(row["sequence"], "audit sequence")
            audit = AuditEvent.model_validate_json(
                _require_string(row["event_json"], "audit event")
            )
            if audit.event_type is AuditEventType.CASE_STATE_CHANGED:
                if audit.from_state is None or audit.to_state is None:
                    raise IncompatiblePersistedContractError()
                event: StateWorkflowEvent | GateWorkflowEvent = StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": audit.actor,
                        "fromState": audit.from_state,
                        "toState": audit.to_state,
                    }
                )
            elif audit.event_type is AuditEventType.GATE_DECISION:
                offset = gate_offsets.get(audit.case_id, 0)
                decisions = decisions_by_case.get(audit.case_id, [])
                if offset >= len(decisions):
                    raise IncompatiblePersistedContractError()
                decision = decisions[offset]
                gate_offsets[audit.case_id] = offset + 1
                if (
                    decision.decided_at != audit.occurred_at
                    or decision.reason_codes != audit.reason_codes
                ):
                    raise IncompatiblePersistedContractError()
                event = GateWorkflowEvent.model_validate(
                    {"kind": WorkflowEventKind.GATE, "decision": decision}
                )
            elif audit.event_type in {
                AuditEventType.HUMAN_APPROVAL,
                AuditEventType.RECEIPT,
                AuditEventType.RESET,
            }:
                continue
            else:
                # Pre-v3 audit rows do not contain enough data to recreate the
                # closed workflow payload for these event types.
                raise IncompatiblePersistedContractError()
            self._insert_workflow_projection(
                connection,
                audit_sequence=sequence,
                audit=audit,
                event=event,
                projection_event_id=f"projection-migrated-{sequence}",
            )
        if any(
            gate_offsets.get(case_id, 0) != len(decisions)
            for case_id, decisions in decisions_by_case.items()
        ):
            raise IncompatiblePersistedContractError()

    def _backfill_v3_pending_transcripts(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT case_id, version, intake_summary_json, updated_at
            FROM cases
            WHERE state = ?
            ORDER BY case_id
            """,
            (CaseState.AWAITING_TRANSCRIPT_CONFIRMATION.value,),
        ).fetchall()
        for row in rows:
            case_id = _require_string(row["case_id"], "case id")
            summary_raw = row["intake_summary_json"]
            if summary_raw is None:
                raise IncompatiblePersistedContractError()
            summary = _load_json_object(_require_string(summary_raw, "intake summary"))
            transcript_id, local_ref, digest = _transcript_identity_from_summary(
                case_id,
                summary,
            )
            connection.execute(
                """
                INSERT INTO case_transcripts (
                    transcript_id, case_id, version, bound_case_version,
                    transcript_sha256, local_ref, confirmed, created_at, confirmed_at
                ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, NULL)
                """,
                (
                    transcript_id,
                    case_id,
                    _require_integer(row["version"], "case version"),
                    digest,
                    local_ref,
                    _require_string(row["updated_at"], "updated_at"),
                ),
            )

    def bind_case_media_handle(
        self,
        *,
        case_id: str,
        storage_name: str,
        created_at: datetime,
    ) -> None:
        """Legacy-only split writer; canonical intake binds ownership atomically."""

        if self.is_canonical_authority:
            raise WorkflowAtomicityError("Canonical media handles require commit_intake_disclosure")
        if _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None:
            raise ValueError("Media storage name is not an owned canonical handle")
        timestamp = _dump_aware_datetime(created_at, "media handle created_at")
        with self._write_connection() as connection:
            if (
                connection.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone()
                is None
            ):
                raise CaseRecordNotFoundError(case_id)
            connection.execute(
                """
                INSERT INTO case_media_handles (case_id, storage_name, created_at)
                VALUES (?, ?, ?)
                """,
                (case_id, storage_name, timestamp),
            )

    def get_case_media_handle(self, case_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT storage_name FROM case_media_handles WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        storage_name = _require_string(row["storage_name"], "media storage name")
        if _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None:
            raise PersistedDataIntegrityError("Persisted media handle is invalid")
        return storage_name

    def unbind_case_media_handle(self, case_id: str, storage_name: str) -> bool:
        """Legacy-only split cleanup for the isolated dev boundary."""

        if self.is_canonical_authority:
            raise WorkflowAtomicityError(
                "Canonical media authority cannot be removed independently"
            )
        if _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None:
            raise ValueError("Media storage name is not an owned canonical handle")
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM case_media_handles
                WHERE case_id = ? AND storage_name = ?
                """,
                (case_id, storage_name),
            )
            return cursor.rowcount == 1

    def list_case_media_handles(self) -> tuple[tuple[str, str], ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT case_id, storage_name
                FROM case_media_handles
                ORDER BY case_id ASC
                """
            ).fetchall()
        result: list[tuple[str, str]] = []
        for row in rows:
            case_id = _require_string(row["case_id"], "media case id")
            storage_name = _require_string(row["storage_name"], "media storage name")
            if _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None:
                raise PersistedDataIntegrityError("Persisted media handle is invalid")
            result.append((case_id, storage_name))
        return tuple(result)

    def create_case(
        self,
        *,
        case_id: str,
        redacted_metadata: dict[str, str],
        created_at: datetime,
    ) -> CaseRecord:
        snapshot = CaseSnapshot(
            portal_state=PortalState.DRAFT,
            redacted_metadata=dict(redacted_metadata),
            claim_packet=None,
            intake_summary=None,
            active_clarification=None,
        )
        _validate_snapshot(case_id, CaseState.CREATED, snapshot)
        timestamp = _dump_aware_datetime(created_at, "created_at")
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO cases (
                    case_id, version, state, portal_state, redacted_metadata_json,
                    claim_packet_json, intake_summary_json, active_clarification_json,
                    created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    case_id,
                    CaseState.CREATED.value,
                    PortalState.DRAFT.value,
                    _dump_json_object(redacted_metadata),
                    timestamp,
                    timestamp,
                ),
            )
        record = self.get_case(case_id)
        if record is None:
            raise PersistenceError("Created case could not be read back")
        return record

    def get_case(self, case_id: str) -> CaseRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return None if row is None else self._row_to_case(row)

    def resolve_portal_run(
        self,
        run_id: str,
        control_digest: bytes,
    ) -> PortalRunRecord | None:
        """Resolve an uncertain commit without reopening or mutating authority."""

        self._require_canonical_authority_mode()
        if type(run_id) is not str or _IDENTIFIER.fullmatch(run_id) is None:
            raise ValueError("run_id must be a canonical identifier")
        self._validate_digest(control_digest)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM portal_run_authority
                WHERE run_id = ? OR control_digest = ?
                """,
                (run_id, control_digest),
            ).fetchone()
            if row is None:
                return None
            if (
                _require_string(row["run_id"], "portal run id") != run_id
                or row["control_digest"] != control_digest
            ):
                raise AuthorityCapabilityError("Portal run recovery identity is invalid")
            return self._row_to_portal_run(connection, row)

    def resolve_verification_attempt(
        self,
        *,
        case_id: str,
        run_id: str,
        control_digest: bytes,
        attempt_id: str,
    ) -> VerificationAttemptResult | None:
        """Resolve an uncertain G8 commit through server-internal run authority."""

        self._require_canonical_authority_mode()
        for identifier, label in (
            (case_id, "case_id"),
            (run_id, "run_id"),
            (attempt_id, "attempt_id"),
        ):
            if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
                raise ValueError(f"{label} must be a canonical identifier")
        self._validate_digest(control_digest)
        with self._read_connection() as connection:
            run_row = connection.execute(
                """
                SELECT * FROM portal_run_authority
                WHERE run_id = ? OR control_digest = ?
                """,
                (run_id, control_digest),
            ).fetchone()
            if run_row is None:
                return None
            if (
                _require_string(run_row["run_id"], "verification run id") != run_id
                or run_row["control_digest"] != control_digest
                or _require_string(run_row["case_id"], "verification run case id")
                != case_id
            ):
                raise AuthorityCapabilityError(
                    "Verification recovery identity is invalid"
                )
            self._row_to_portal_run(connection, run_row)
            attempt_row = connection.execute(
                "SELECT * FROM verification_attempt_authority WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt_row is None:
                return None
            if (
                _require_string(attempt_row["case_id"], "verification case id")
                != case_id
                or _require_string(attempt_row["run_id"], "verification attempt run id")
                != run_id
            ):
                raise AuthorityCapabilityError(
                    "Verification recovery identity is invalid"
                )
            current_row = connection.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current_row is None:
                raise CaseRecordNotFoundError(case_id)
            current = self._row_to_case(current_row)
            bound_version = _require_integer(
                attempt_row["bound_case_version"],
                "verification bound case version",
            )
            if bound_version > current.version:
                raise WorkflowAtomicityError(
                    "Verification recovery is ahead of its case cursor"
                )
            return VerificationAttemptResult(
                case=current,
                attempt=self._verification_attempt_from_row(attempt_row),
            )

    def start_portal_run(
        self,
        command: PortalRunStartCommand,
    ) -> PortalRunStartResult:
        """Consume one READY capability and persist G6 plus its state atomically."""

        self._require_canonical_authority_mode()
        if type(command) is not PortalRunStartCommand:
            raise TypeError("command must be a PortalRunStartCommand")
        self._require_expected_version(
            command.expected_case_version,
            "Portal run expected_case_version",
        )
        for identifier, label in (
            (command.case_id, "case_id"),
            (command.run_id, "run_id"),
        ):
            if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
                raise ValueError(f"{label} must be a canonical identifier")
        self._validate_digest(command.capability_digest)
        self._validate_digest(command.control_digest)
        if not isinstance(command.portal_variant, PortalVariant):
            raise ValueError("portal_variant must be canonical")
        invocation = ToolInvocation.model_validate(command.invocation_payload)
        if invocation.invocation_id != command.run_id:
            raise WorkflowAtomicityError("Portal run identity must equal invocation identity")
        if (
            type(command.current_url) is not str
            or type(command.action) is not str
            or type(command.proposed_action_number) is not int
            or type(command.elapsed_seconds) is not float
        ):
            raise TypeError("Portal run G6 inputs must use exact scalar types")
        consumed_at = _parse_datetime(
            _dump_aware_datetime(command.consumed_at, "capability consumed_at")
        )
        updated_at = _parse_datetime(
            _dump_aware_datetime(command.updated_at, "portal run updated_at")
        )
        if consumed_at >= updated_at:
            raise ValueError("Portal run update must strictly follow capability consumption")

        invocation_json = _dump_contract(invocation)
        expected_url = (
            f"http://127.0.0.1:3000/sandbox/{command.portal_variant.value}/cases/{command.case_id}"
        )
        context_payload: JsonObject = {
            "caseId": command.case_id,
            "portalVariant": command.portal_variant.value,
            "currentUrl": expected_url if command.current_url == expected_url else "",
            "action": command.action if command.action in _G6_SAFE_ACTIONS else "",
            "proposedActionNumber": command.proposed_action_number,
            "elapsedSeconds": command.elapsed_seconds,
        }
        context_json = _dump_json_object(context_payload)
        prestage_json = _dump_contract(command.prestage_session)
        expected_hashes = (
            _authority_sha256("portal-invocation", invocation_json),
            _authority_sha256("g6-context", context_json),
            _authority_sha256("portal-prestage", prestage_json),
        )

        with self._write_connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM portal_run_authority
                WHERE run_id = ? OR control_digest = ?
                """,
                (command.run_id, command.control_digest),
            ).fetchone()
            if existing is not None:
                if (
                    _require_string(existing["run_id"], "portal run id") != command.run_id
                    or existing["control_digest"] != command.control_digest
                    or existing["agent_capability_digest"] != command.capability_digest
                    or _require_string(existing["case_id"], "portal run case id") != command.case_id
                    or _require_string(existing["portal_variant"], "portal run variant")
                    != command.portal_variant.value
                    or (
                        _require_string(existing["invocation_sha256"], "invocation digest"),
                        _require_string(existing["g6_context_sha256"], "G6 context digest"),
                        _require_string(existing["prestage_session_sha256"], "prestage digest"),
                    )
                    != expected_hashes
                ):
                    raise AuthorityCapabilityError("Portal run recovery identity is invalid")
                existing_run = self._row_to_portal_run(connection, existing)
                capability_row = connection.execute(
                    "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
                    (command.capability_digest,),
                ).fetchone()
                if capability_row is None:
                    raise AuthorityCapabilityError("Portal run recovery identity is invalid")
                capability = self._row_to_capability(capability_row)
                if (
                    existing_run.ready_case_version != command.expected_case_version
                    or existing_run.created_at != updated_at
                    or capability.consumed_at != consumed_at
                ):
                    raise AuthorityCapabilityError("Portal run recovery identity is invalid")
                current_row = connection.execute(
                    "SELECT * FROM cases WHERE case_id = ?",
                    (command.case_id,),
                ).fetchone()
                if current_row is None:
                    raise CaseRecordNotFoundError(command.case_id)
                current = self._row_to_case(current_row)
                return PortalRunStartResult(
                    case=current,
                    run=existing_run,
                )

            current = self._require_current(
                connection,
                command.case_id,
                command.expected_case_version,
            )
            self._require_current_packet_authority(connection, current)
            packet = current.snapshot.claim_packet
            if (
                current.state is not CaseState.READY_TO_FILL
                or packet is None
                or packet.state is not CaseState.READY_TO_FILL
                or packet.portal_state is not PortalState.DRAFT
                or tuple(decision.gate_id for decision in packet.gate_decisions)
                != _ANALYSIS_GATE_SEQUENCE
                or any(not decision.passed for decision in packet.gate_decisions)
                or packet.claim.missing_required_fields
            ):
                raise WorkflowAtomicityError("G6 requires one complete READY packet")
            _validate_prestage_session(
                command.prestage_session,
                case_id=current.case_id,
                variant=command.portal_variant,
                attachments=packet.claim.attachments,
            )
            if not (
                current.updated_at
                <= command.prestage_session.updated_at
                <= consumed_at
                < updated_at
            ):
                raise WorkflowAtomicityError("Prestage chronology is invalid")

            capability_row = connection.execute(
                """
                SELECT * FROM authority_capabilities
                WHERE capability_digest = ?
                """,
                (command.capability_digest,),
            ).fetchone()
            if capability_row is None:
                raise AuthorityCapabilityError("Portal capability is invalid")
            capability = self._row_to_capability(capability_row)
            if (
                capability.role != "agent"
                or capability.purpose != "portal_run"
                or capability.portal_variant is not None
                or capability.case_id != current.case_id
                or capability.bound_case_version != current.version
                or capability.consumed_at is not None
                or capability.revoked_at is not None
                or consumed_at < capability.issued_at
                or consumed_at >= capability.expires_at
            ):
                raise AuthorityCapabilityError("Portal capability is invalid")
            consumed = connection.execute(
                """
                UPDATE authority_capabilities
                SET consumed_at = ?
                WHERE capability_digest = ?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (
                    _dump_aware_datetime(consumed_at, "capability consumed_at"),
                    command.capability_digest,
                ),
            )
            if consumed.rowcount != 1:
                raise AuthorityCapabilityError("Portal capability is invalid")

            filling_packet = _rebind_claim_packet(
                packet,
                state=CaseState.FILLING,
                portal_state=PortalState.DRAFT,
                gates=packet.gate_decisions,
                verification=packet.verification,
            )
            g6 = evaluate_g6(
                command.invocation_payload,
                context=ToolAuthorityContext(
                    packet=filling_packet,
                    case_state=CaseState.FILLING,
                    portal_variant=command.portal_variant,
                    current_url=command.current_url,
                    action=command.action,
                    proposed_action_number=command.proposed_action_number,
                    elapsed_seconds=command.elapsed_seconds,
                ),
                decided_at=updated_at,
            ).decision
            target = CaseState.FILLING if g6.passed else CaseState.BLOCKED
            target_packet = _rebind_claim_packet(
                filling_packet,
                state=target,
                portal_state=PortalState.DRAFT,
                gates=(*packet.gate_decisions, g6),
                verification=packet.verification,
            )
            target_snapshot = replace(
                current.snapshot,
                portal_state=PortalState.DRAFT,
                claim_packet=target_packet,
                active_clarification=None,
            )
            _validate_snapshot(current.case_id, target, target_snapshot)
            validate_case_transition(current.state, target)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=target_snapshot,
                updated_at=updated_at,
            )
            g6_sequence = self._insert_authority_gate(
                connection,
                case_id=current.case_id,
                decision=g6,
            )
            self._insert_packet_authority(
                connection,
                case_id=current.case_id,
                bound_case_version=current.version + 1,
                packet=target_packet,
                created_at=updated_at,
            )
            state_audit = build_state_change_event(
                case_id=current.case_id,
                current=current.state,
                target=target,
                actor=ActorType.AGENT if g6.passed else ActorType.SYSTEM,
                occurred_at=updated_at,
            )
            state_sequence = self._insert_audit_event(connection, state_audit)
            self._insert_workflow_projection(
                connection,
                audit_sequence=state_sequence,
                audit=state_audit,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": state_audit.actor,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            connection.execute(
                """
                INSERT INTO portal_run_authority (
                    run_id, case_id, authority_version, agent_capability_digest,
                    control_digest, portal_variant, ready_case_version,
                    g6_case_version, terminal_case_version,
                    invocation_json, invocation_sha256,
                    g6_context_json, g6_context_sha256,
                    prestage_session_json, prestage_session_sha256,
                    g6_gate_sequence, g6_state_audit_sequence,
                    status, created_at, terminal_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.run_id,
                    current.case_id,
                    command.capability_digest,
                    command.control_digest,
                    command.portal_variant.value,
                    current.version,
                    current.version + 1,
                    None if g6.passed else current.version + 1,
                    invocation_json,
                    expected_hashes[0],
                    context_json,
                    expected_hashes[1],
                    prestage_json,
                    expected_hashes[2],
                    g6_sequence,
                    state_sequence,
                    "filling" if g6.passed else "blocked_g6",
                    _dump_aware_datetime(updated_at, "portal run created_at"),
                    None
                    if g6.passed
                    else _dump_aware_datetime(updated_at, "portal run terminal_at"),
                ),
            )
            final_case = self._require_current(
                connection,
                current.case_id,
                current.version + 1,
            )
            row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE run_id = ?",
                (command.run_id,),
            ).fetchone()
            if row is None:
                raise WorkflowAtomicityError("Atomic G6 lost its run authority")
            return PortalRunStartResult(
                case=final_case,
                run=self._row_to_portal_run(connection, row),
            )

    def preflight_portal_write(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        run_id: str,
        control_digest: bytes,
        fields_payload: object,
        decided_at: datetime,
    ) -> GateDecision:
        """Recompute G7 without allocating a gate or mutating any authority."""

        self._require_canonical_authority_mode()
        self._validate_digest(control_digest)
        _dump_aware_datetime(decided_at, "G7 preflight decided_at")
        with self._read_connection() as connection:
            current = self._require_current(
                connection,
                case_id,
                expected_case_version,
            )
            row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["control_digest"] != control_digest:
                raise AuthorityCapabilityError("Portal run authority is invalid")
            run = self._row_to_portal_run(connection, row)
            self._require_open_portal_run(current, run)
            self._require_current_packet_authority(connection, current)
            packet = current.snapshot.claim_packet
            if packet is None:
                raise WorkflowAtomicityError("Portal run lost its ClaimPacket")
            return evaluate_g7(
                fields_payload,
                packet=packet,
                case_state=current.state,
                portal_state=current.snapshot.portal_state,
                decided_at=decided_at,
            ).decision

    def finalize_portal_write(
        self,
        command: PortalWriteFinalizeCommand,
    ) -> PortalWriteFinalizeResult:
        """Close one full write with terminal tool, G7, session, and state authority.

        This mutation is intentionally not retried after an uncertain commit.
        Recover with ``resolve_portal_run(run_id, control_digest)`` instead.
        """

        self._require_canonical_authority_mode()
        if type(command) is not PortalWriteFinalizeCommand:
            raise TypeError("command must be a PortalWriteFinalizeCommand")
        self._require_expected_version(
            command.expected_case_version,
            "Portal write expected_case_version",
        )
        self._validate_digest(command.control_digest)
        if (
            type(command.duration_ms) is not int
            or command.duration_ms < 0
            or type(command.run_id) is not str
            or _IDENTIFIER.fullmatch(command.run_id) is None
        ):
            raise ValueError("Portal write metadata is invalid")
        completed_at = _parse_datetime(
            _dump_aware_datetime(command.completed_at, "portal write completed_at")
        )

        with self._write_connection() as connection:
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_case_version,
            )
            row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE run_id = ?",
                (command.run_id,),
            ).fetchone()
            if row is None or row["control_digest"] != command.control_digest:
                raise AuthorityCapabilityError("Portal run authority is invalid")
            run = self._row_to_portal_run(connection, row)
            self._require_open_portal_run(current, run)
            self._require_current_packet_authority(connection, current)
            packet = current.snapshot.claim_packet
            if packet is None:
                raise WorkflowAtomicityError("Portal run lost its ClaimPacket")
            if completed_at <= current.updated_at:
                raise WorkflowAtomicityError("G7 must strictly follow the G6 mutation")

            result = evaluate_g7(
                command.fields_payload,
                packet=packet,
                case_state=current.state,
                portal_state=current.snapshot.portal_state,
                decided_at=completed_at,
            )
            decision = result.decision
            target = CaseState.VERIFYING if decision.passed else CaseState.BLOCKED
            target_portal_state = PortalState.REVIEW if decision.passed else PortalState.DRAFT
            rejected_json: str | None = None
            rejected_hash: str | None = None
            portal_checkpoint_values: tuple[str, str, str, str] | None = None
            if decision.passed:
                portal_session = command.portal_session
                rendered_snapshot = command.rendered_snapshot
                prestage = run.prestage_session
                if (
                    portal_session is None
                    or rendered_snapshot is None
                    or result.fields is None
                    or portal_session.case_id != current.case_id
                    or portal_session.variant is not run.portal_variant
                    or portal_session.state is not PortalState.REVIEW
                    or portal_session.version != prestage.version + 2
                    or portal_session.fields != result.fields
                    or rendered_snapshot.case_id != current.case_id
                    or rendered_snapshot.variant is not run.portal_variant
                    or rendered_snapshot.state is not PortalState.REVIEW
                    or rendered_snapshot.version != portal_session.version
                    or not (
                        current.updated_at
                        <= portal_session.updated_at
                        <= rendered_snapshot.rendered_at
                        <= completed_at
                    )
                ):
                    raise WorkflowAtomicityError(
                        "Reviewed portal checkpoint is not bound to the G7 result"
                    )
                session_json = _dump_contract(portal_session)
                rendered_json = _dump_contract(rendered_snapshot)
                portal_checkpoint_values = (
                    session_json,
                    _authority_sha256("portal-session", session_json),
                    rendered_json,
                    _authority_sha256("portal-rendered", rendered_json),
                )
            else:
                if command.portal_session is not None or command.rendered_snapshot is not None:
                    raise WorkflowAtomicityError(
                        "A rejected G7 candidate cannot persist portal values"
                    )
                rejected_json = _dump_json_object(
                    _rejected_g7_summary(command.fields_payload, decision)
                )
                rejected_hash = _authority_sha256("g7-rejected-summary", rejected_json)

            target_packet = _rebind_claim_packet(
                packet,
                state=target,
                portal_state=target_portal_state,
                gates=(*packet.gate_decisions, decision),
                verification=packet.verification,
            )
            target_snapshot = replace(
                current.snapshot,
                portal_state=target_portal_state,
                claim_packet=target_packet,
                active_clarification=None,
            )
            _validate_snapshot(current.case_id, target, target_snapshot)
            validate_case_transition(current.state, target)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=target_snapshot,
                updated_at=completed_at,
            )
            g7_sequence = self._insert_authority_gate(
                connection,
                case_id=current.case_id,
                decision=decision,
            )
            terminal_tool = ToolCallWorkflowEvent.model_validate(
                {
                    "kind": WorkflowEventKind.TOOL_CALL,
                    "invocationId": run.invocation.invocation_id,
                    "sequence": run.invocation.sequence,
                    "tool": run.invocation.tool,
                    "status": (
                        ToolCallStatus.SUCCEEDED if decision.passed else ToolCallStatus.BLOCKED
                    ),
                    "durationMs": command.duration_ms,
                }
            )
            terminal_envelope = self._insert_redacted_workflow_event(
                connection,
                case_id=current.case_id,
                event=terminal_tool,
                actor=ActorType.AGENT,
                occurred_at=completed_at,
            )
            portal_fill_sequence: int | None = None
            if decision.passed:
                assert command.portal_session is not None
                portal_envelope = self._insert_redacted_workflow_event(
                    connection,
                    case_id=current.case_id,
                    event=PortalFillWorkflowEvent.model_validate(
                        {
                            "kind": WorkflowEventKind.PORTAL_FILL,
                            "variant": run.portal_variant,
                            "portalVersion": command.portal_session.version,
                            "writtenFields": tuple(RequiredClaimField),
                        }
                    ),
                    actor=ActorType.AGENT,
                    occurred_at=completed_at,
                )
                portal_fill_sequence = portal_envelope.source_audit_sequence
            self._insert_packet_authority(
                connection,
                case_id=current.case_id,
                bound_case_version=current.version + 1,
                packet=target_packet,
                created_at=completed_at,
            )
            state_actor = ActorType.AGENT if decision.passed else ActorType.SYSTEM
            state_audit = build_state_change_event(
                case_id=current.case_id,
                current=current.state,
                target=target,
                actor=state_actor,
                occurred_at=completed_at,
            )
            state_sequence = self._insert_audit_event(connection, state_audit)
            self._insert_workflow_projection(
                connection,
                audit_sequence=state_sequence,
                audit=state_audit,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": state_actor,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            if portal_checkpoint_values is not None:
                assert command.portal_session is not None
                connection.execute(
                    """
                    INSERT INTO portal_session_authority (
                        case_id, checkpoint_number, run_id, authority_version,
                        checkpoint_kind, portal_version,
                        session_json, session_sha256,
                        rendered_snapshot_json, rendered_snapshot_sha256,
                        source_attempt_id, created_at
                    ) VALUES (?, 1, ?, 1, 'reviewed', ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        current.case_id,
                        run.run_id,
                        command.portal_session.version,
                        *portal_checkpoint_values,
                        _dump_aware_datetime(completed_at, "portal checkpoint created_at"),
                    ),
                )
            updated = connection.execute(
                """
                UPDATE portal_run_authority
                SET terminal_case_version = ?, g7_gate_sequence = ?,
                    tool_terminal_audit_sequence = ?, portal_fill_audit_sequence = ?,
                    g7_state_audit_sequence = ?, rejected_summary_json = ?,
                    rejected_summary_sha256 = ?, status = ?, terminal_at = ?
                WHERE run_id = ? AND status = 'filling'
                """,
                (
                    current.version + 1,
                    g7_sequence,
                    terminal_envelope.source_audit_sequence,
                    portal_fill_sequence,
                    state_sequence,
                    rejected_json,
                    rejected_hash,
                    "verifying" if decision.passed else "blocked_g7",
                    _dump_aware_datetime(completed_at, "portal run terminal_at"),
                    run.run_id,
                ),
            )
            if updated.rowcount != 1:
                raise WorkflowAtomicityError("Portal run was already terminal")
            final_case = self._require_current(
                connection,
                current.case_id,
                current.version + 1,
            )
            final_row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            if final_row is None:
                raise WorkflowAtomicityError("Atomic G7 lost its run authority")
            return PortalWriteFinalizeResult(
                case=final_case,
                run=self._row_to_portal_run(connection, final_row),
            )

    def record_verification_attempt(
        self,
        command: VerificationAttemptCommand,
    ) -> VerificationAttemptResult:
        """Persist one bounded attempt; only a final attempt allocates immutable G8."""

        self._require_canonical_authority_mode()
        if type(command) is not VerificationAttemptCommand:
            raise TypeError("command must be a VerificationAttemptCommand")
        self._require_expected_version(
            command.expected_case_version,
            "Verification expected_case_version",
        )
        self._validate_digest(command.control_digest)
        for identifier, label in (
            (command.case_id, "case_id"),
            (command.run_id, "run_id"),
            (command.attempt_id, "attempt_id"),
        ):
            if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
                raise ValueError(f"{label} must be a canonical identifier")
        if (
            type(command.screenshot_sha256) is not str
            or _SHA256.fullmatch(command.screenshot_sha256) is None
            or type(command.model_reported_mismatch) is not bool
            or type(command.final) is not bool
            or not isinstance(command.rendered_snapshot, RenderedPortalSnapshot)
            or (
                command.repaired_session is not None
                and not isinstance(command.repaired_session, PortalSessionView)
            )
        ):
            raise ValueError("Verification metadata is invalid")
        requested_at = _parse_datetime(
            _dump_aware_datetime(
                command.snapshot_requested_at,
                "snapshot_requested_at",
            )
        )
        received_at = _parse_datetime(
            _dump_aware_datetime(
                command.snapshot_received_at,
                "snapshot_received_at",
            )
        )
        verified_at = _parse_datetime(_dump_aware_datetime(command.verified_at, "verified_at"))
        decided_at = _parse_datetime(_dump_aware_datetime(command.decided_at, "G8 decided_at"))

        with self._write_connection() as connection:
            existing_attempt_row = connection.execute(
                "SELECT * FROM verification_attempt_authority WHERE attempt_id = ?",
                (command.attempt_id,),
            ).fetchone()
            if existing_attempt_row is not None:
                run_row = connection.execute(
                    "SELECT * FROM portal_run_authority WHERE run_id = ?",
                    (command.run_id,),
                ).fetchone()
                if run_row is None or run_row["control_digest"] != command.control_digest:
                    raise AuthorityCapabilityError(
                        "Verification recovery identity is invalid"
                    )
                run = self._row_to_portal_run(connection, run_row)
                attempt = self._verification_attempt_from_row(existing_attempt_row)
                checkpoint_number = _require_integer(
                    existing_attempt_row["portal_checkpoint_number"],
                    "verification checkpoint number",
                )
                persisted_repair_json: str | None = None
                if checkpoint_number == 2:
                    checkpoint_row = connection.execute(
                        """
                        SELECT * FROM portal_session_authority
                        WHERE case_id = ? AND checkpoint_number = 2
                        """,
                        (command.case_id,),
                    ).fetchone()
                    if checkpoint_row is None:
                        raise WorkflowAtomicityError(
                            "Verification retry lost its repair checkpoint"
                        )
                    self._portal_session_from_row(checkpoint_row)
                    persisted_repair_json = _require_string(
                        checkpoint_row["session_json"],
                        "verification repair session",
                    )
                expected_repair_json = (
                    None
                    if command.repaired_session is None
                    else _dump_contract(command.repaired_session)
                )
                if (
                    run.case_id != command.case_id
                    or _require_string(
                        existing_attempt_row["case_id"],
                        "verification retry case id",
                    )
                    != command.case_id
                    or _require_string(
                        existing_attempt_row["run_id"],
                        "verification retry run id",
                    )
                    != command.run_id
                    or _require_integer(
                        existing_attempt_row["bound_case_version"],
                        "verification retry bound version",
                    )
                    != command.expected_case_version + 1
                    or checkpoint_number != attempt.attempt_number
                    or _require_string(
                        existing_attempt_row["rendered_snapshot_json"],
                        "verification retry rendered snapshot",
                    )
                    != _dump_contract(command.rendered_snapshot)
                    or _require_string(
                        existing_attempt_row["screenshot_sha256"],
                        "verification retry screenshot digest",
                    )
                    != command.screenshot_sha256
                    or _parse_datetime(
                        _require_string(
                            existing_attempt_row["snapshot_requested_at"],
                            "verification retry requested_at",
                        )
                    )
                    != requested_at
                    or _parse_datetime(
                        _require_string(
                            existing_attempt_row["snapshot_received_at"],
                            "verification retry received_at",
                        )
                    )
                    != received_at
                    or attempt.report.model_reported_mismatch
                    is not command.model_reported_mismatch
                    or attempt.report.verified_at != verified_at
                    or _parse_datetime(
                        _require_string(
                            existing_attempt_row["created_at"],
                            "verification retry decided_at",
                        )
                    )
                    != decided_at
                    or attempt.final is not command.final
                    or persisted_repair_json != expected_repair_json
                ):
                    raise AuthorityCapabilityError(
                        "Verification retry does not match the committed attempt"
                    )
                current_row = connection.execute(
                    "SELECT * FROM cases WHERE case_id = ?",
                    (command.case_id,),
                ).fetchone()
                if current_row is None:
                    raise CaseRecordNotFoundError(command.case_id)
                current = self._row_to_case(current_row)
                if current.version < command.expected_case_version + 1:
                    raise WorkflowAtomicityError(
                        "Verification retry is ahead of its case cursor"
                    )
                return VerificationAttemptResult(case=current, attempt=attempt)

            current = self._require_current(
                connection,
                command.case_id,
                command.expected_case_version,
            )
            run_row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE run_id = ?",
                (command.run_id,),
            ).fetchone()
            if run_row is None or run_row["control_digest"] != command.control_digest:
                raise AuthorityCapabilityError("Verification run authority is invalid")
            run = self._row_to_portal_run(connection, run_row)
            if (
                current.state is not CaseState.VERIFYING
                or run.case_id != current.case_id
                or run.status != "verifying"
                or decided_at <= current.updated_at
                or requested_at < current.updated_at
            ):
                raise WorkflowAtomicityError("Verification run is not open")
            self._require_current_packet_authority(connection, current)
            packet = current.snapshot.claim_packet
            if (
                packet is None
                or packet.state is not CaseState.VERIFYING
                or packet.portal_state is not PortalState.REVIEW
                or tuple(decision.gate_id for decision in packet.gate_decisions)[-2:]
                != (GateId.G6_TOOL_AUTHORITY, GateId.G7_PORTAL_WRITE)
                or any(not decision.passed for decision in packet.gate_decisions)
            ):
                raise WorkflowAtomicityError("Verification packet authority is invalid")

            prior_rows = connection.execute(
                """
                SELECT * FROM verification_attempt_authority
                WHERE case_id = ? ORDER BY attempt_number
                """,
                (current.case_id,),
            ).fetchall()
            prior_attempts = tuple(self._verification_attempt_from_row(row) for row in prior_rows)
            attempt_number = len(prior_attempts) + 1
            if attempt_number not in {1, 2}:
                raise WorkflowAtomicityError("Verification attempt limit is closed")
            if any(attempt.final for attempt in prior_attempts):
                raise WorkflowAtomicityError("Final verification cannot be reopened")

            checkpoint_number = 1
            session_row = connection.execute(
                """
                SELECT * FROM portal_session_authority
                WHERE case_id = ? AND checkpoint_number = 1
                """,
                (current.case_id,),
            ).fetchone()
            if session_row is None:
                raise WorkflowAtomicityError("Verification lost its reviewed portal session")
            base_session, _base_rendered = self._portal_session_from_row(session_row)
            expected_version = base_session.version
            repaired_from_attempt_id: str | None = None

            if attempt_number == 1:
                if command.repaired_session is not None:
                    raise WorkflowAtomicityError(
                        "The first attempt cannot introduce a repaired session"
                    )
            else:
                first = prior_attempts[0]
                repair = first.repair
                repaired = command.repaired_session
                if (
                    not command.final
                    or repair is None
                    or repaired is None
                    or repaired.case_id != current.case_id
                    or repaired.variant is not run.portal_variant
                    or repaired.state is not PortalState.REVIEW
                    or repaired.version != repair.to_portal_version
                    or repaired.updated_at < current.updated_at
                    or repaired.updated_at > requested_at
                ):
                    raise WorkflowAtomicityError("Second attempt has no exact repair authority")
                base_values = base_session.fields.model_dump(mode="json", by_alias=False)
                repaired_values = repaired.fields.model_dump(mode="json", by_alias=False)
                expected_values = _canonical_claim_portal_fields(packet).model_dump(
                    mode="json",
                    by_alias=False,
                )
                for field_name, value in base_values.items():
                    if field_name == repair.field.value:
                        if repaired_values[field_name] != expected_values[field_name]:
                            raise WorkflowAtomicityError(
                                "Repair target is not the canonical packet value"
                            )
                    elif repaired_values[field_name] != value:
                        raise WorkflowAtomicityError("Repair changed a non-target portal value")
                first_rendered = RenderedPortalSnapshot.model_validate_json(
                    _require_string(
                        prior_rows[0]["rendered_snapshot_json"],
                        "first rendered snapshot",
                    )
                )
                old_rendered_values = first_rendered.fields.model_dump(
                    mode="json", by_alias=False
                )
                new_rendered_values = command.rendered_snapshot.fields.model_dump(
                    mode="json", by_alias=False
                )
                for field_name, value in old_rendered_values.items():
                    if (
                        field_name != repair.field.value
                        and new_rendered_values[field_name] != value
                    ):
                        raise WorkflowAtomicityError("Repair changed a non-target rendered value")
                session_json = _dump_contract(repaired)
                rendered_checkpoint_json = _dump_contract(command.rendered_snapshot)
                connection.execute(
                    """
                    INSERT INTO portal_session_authority (
                        case_id, checkpoint_number, run_id, authority_version,
                        checkpoint_kind, portal_version,
                        session_json, session_sha256,
                        rendered_snapshot_json, rendered_snapshot_sha256,
                        source_attempt_id, created_at
                    ) VALUES (?, 2, ?, 1, 'repair', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current.case_id,
                        run.run_id,
                        repaired.version,
                        session_json,
                        _authority_sha256("portal-session", session_json),
                        rendered_checkpoint_json,
                        _authority_sha256(
                            "portal-rendered",
                            rendered_checkpoint_json,
                        ),
                        first.attempt_id,
                        _dump_aware_datetime(decided_at, "repair checkpoint created_at"),
                    ),
                )
                checkpoint_number = 2
                expected_version = repaired.version
                repaired_from_attempt_id = first.attempt_id

            rendered = command.rendered_snapshot
            if (
                rendered.case_id != current.case_id
                or rendered.variant is not run.portal_variant
                or rendered.state is not PortalState.REVIEW
                or rendered.version != expected_version
            ):
                raise WorkflowAtomicityError("Rendered snapshot identity is invalid")
            verification = evaluate_g8(
                packet,
                rendered,
                expected_variant=run.portal_variant,
                expected_portal_version=expected_version,
                snapshot_requested_at=requested_at,
                snapshot_received_at=received_at,
                model_reported_mismatch=command.model_reported_mismatch,
                verified_at=verified_at,
                decided_at=decided_at,
            )

            repair_metadata: VerificationRepairMetadata | None = None
            gate: GateDecision | None = verification.decision if command.final else None
            if not command.final:
                report = verification.report
                non_matching = tuple(
                    item for item in report.field_results if item.status.value != "match"
                )
                if (
                    attempt_number != 1
                    or report.deterministic_match is not False
                    or report.model_reported_mismatch
                    or report.actual_attachment_ids != report.expected_attachment_ids
                    or len(non_matching) != 1
                ):
                    raise WorkflowAtomicityError(
                        "Only one deterministic scalar mismatch is repairable"
                    )
                mismatch = non_matching[0]
                repair_metadata = VerificationRepairMetadata.model_validate(
                    {
                        "repairNumber": 1,
                        "field": mismatch.field,
                        "sourceRefs": mismatch.source_refs,
                        "fromPortalVersion": expected_version,
                        "toPortalVersion": expected_version + 1,
                    }
                )

            attempt = VerificationAttempt.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "attemptId": command.attempt_id,
                    "caseId": current.case_id,
                    "attemptNumber": attempt_number,
                    "caseState": CaseState.VERIFYING,
                    "portalVersion": expected_version,
                    "report": verification.report,
                    "final": command.final,
                    "repair": repair_metadata,
                    "repairedFromAttemptId": repaired_from_attempt_id,
                    "gateDecision": gate,
                }
            )
            if attempt_number == 2:
                VerificationAttemptSeries.model_validate(
                    {
                        "contractVersion": CONTRACT_VERSION,
                        "caseId": current.case_id,
                        "attempts": (*prior_attempts, attempt),
                    }
                )

            if command.final:
                target = CaseState.REVIEW if verification.decision.passed else CaseState.BLOCKED
                target_packet = _rebind_claim_packet(
                    packet,
                    state=target,
                    portal_state=PortalState.REVIEW,
                    gates=(*packet.gate_decisions, verification.decision),
                    verification=verification.report,
                )
            else:
                target = CaseState.VERIFYING
                target_packet = packet
            target_snapshot = replace(
                current.snapshot,
                portal_state=PortalState.REVIEW,
                claim_packet=target_packet,
                active_clarification=None,
            )
            _validate_snapshot(current.case_id, target, target_snapshot)
            if command.final:
                validate_case_transition(current.state, target)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=target_snapshot,
                updated_at=decided_at,
            )
            g8_sequence: int | None = None
            if command.final:
                g8_sequence = self._insert_authority_gate(
                    connection,
                    case_id=current.case_id,
                    decision=verification.decision,
                )
            verification_envelope = self._insert_redacted_workflow_event(
                connection,
                case_id=current.case_id,
                event=VerificationWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.VERIFICATION,
                        "attemptNumber": attempt_number,
                        "status": verification.report.status,
                        "deterministicMatch": verification.report.deterministic_match,
                        "modelReportedMismatch": verification.report.model_reported_mismatch,
                        "repairUsed": attempt_number == 2,
                        "final": command.final,
                    }
                ),
                actor=ActorType.SYSTEM,
                occurred_at=decided_at,
            )
            self._insert_packet_authority(
                connection,
                case_id=current.case_id,
                bound_case_version=current.version + 1,
                packet=target_packet,
                created_at=decided_at,
            )
            state_sequence: int | None = None
            if command.final:
                state_audit = build_state_change_event(
                    case_id=current.case_id,
                    current=current.state,
                    target=target,
                    actor=ActorType.SYSTEM,
                    occurred_at=decided_at,
                )
                state_sequence = self._insert_audit_event(connection, state_audit)
                self._insert_workflow_projection(
                    connection,
                    audit_sequence=state_sequence,
                    audit=state_audit,
                    event=StateWorkflowEvent.model_validate(
                        {
                            "kind": WorkflowEventKind.STATE,
                            "actor": ActorType.SYSTEM,
                            "fromState": current.state,
                            "toState": target,
                        }
                    ),
                )

            attempt_json = _dump_contract(attempt)
            rendered_json = _dump_contract(rendered)
            connection.execute(
                """
                INSERT INTO verification_attempt_authority (
                    attempt_id, case_id, run_id, authority_version,
                    attempt_number, bound_case_version, portal_checkpoint_number,
                    attempt_json, attempt_sha256,
                    rendered_snapshot_json, rendered_snapshot_sha256,
                    screenshot_sha256, snapshot_requested_at, snapshot_received_at,
                    final, g8_gate_sequence, verification_audit_sequence,
                    state_audit_sequence, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    current.case_id,
                    run.run_id,
                    attempt_number,
                    current.version + 1,
                    checkpoint_number,
                    attempt_json,
                    _verification_authority_sha256(
                        attempt_json=attempt_json,
                        rendered_json=rendered_json,
                        screenshot_sha256=command.screenshot_sha256,
                        requested_at=requested_at,
                        received_at=received_at,
                    ),
                    rendered_json,
                    _authority_sha256("verification-rendered", rendered_json),
                    command.screenshot_sha256,
                    _dump_aware_datetime(requested_at, "snapshot requested_at"),
                    _dump_aware_datetime(received_at, "snapshot received_at"),
                    int(command.final),
                    g8_sequence,
                    verification_envelope.source_audit_sequence,
                    state_sequence,
                    _dump_aware_datetime(decided_at, "verification created_at"),
                ),
            )
            if command.final:
                updated = connection.execute(
                    """
                    UPDATE portal_run_authority
                    SET status = ?
                    WHERE run_id = ? AND status = 'verifying'
                    """,
                    (
                        "review" if verification.decision.passed else "blocked_g8",
                        run.run_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkflowAtomicityError("Verification run was already terminal")
            final_case = self._require_current(
                connection,
                current.case_id,
                current.version + 1,
            )
            return VerificationAttemptResult(case=final_case, attempt=attempt)

    @classmethod
    def _row_to_portal_run(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> PortalRunRecord:
        run_id = _require_string(row["run_id"], "portal run id")
        case_id = _require_string(row["case_id"], "portal run case id")
        invocation_json = _require_string(row["invocation_json"], "portal invocation")
        context_json = _require_string(row["g6_context_json"], "G6 context")
        prestage_json = _require_string(row["prestage_session_json"], "prestage session")
        invocation = ToolInvocation.model_validate_json(invocation_json)
        prestage = PortalSessionView.model_validate_json(prestage_json)
        variant = PortalVariant(_require_string(row["portal_variant"], "portal variant"))
        ready_version = _require_integer(row["ready_case_version"], "ready case version")
        g6_version = _require_integer(row["g6_case_version"], "G6 case version")
        terminal_raw = row["terminal_case_version"]
        terminal_version = (
            None
            if terminal_raw is None
            else _require_integer(terminal_raw, "terminal case version")
        )
        created_at = _parse_datetime(_require_string(row["created_at"], "run created_at"))
        terminal_at_raw = row["terminal_at"]
        terminal_at = (
            None
            if terminal_at_raw is None
            else _parse_datetime(_require_string(terminal_at_raw, "run terminal_at"))
        )
        status = _require_string(row["status"], "portal run status")
        control_digest = row["control_digest"]
        capability_digest = row["agent_capability_digest"]
        if (
            _require_integer(row["authority_version"], "portal run authority version") != 1
            or type(control_digest) is not bytes
            or len(control_digest) != 32
            or type(capability_digest) is not bytes
            or len(capability_digest) != 32
            or invocation.invocation_id != run_id
            or prestage.case_id != case_id
            or prestage.variant is not variant
            or prestage.state is not PortalState.DRAFT
            or invocation_json != _dump_contract(invocation)
            or prestage_json != _dump_contract(prestage)
            or context_json != _dump_json_object(_load_json_object(context_json))
            or _authority_sha256("portal-invocation", invocation_json)
            != _require_string(row["invocation_sha256"], "invocation digest")
            or _authority_sha256("g6-context", context_json)
            != _require_string(row["g6_context_sha256"], "G6 context digest")
            or _authority_sha256("portal-prestage", prestage_json)
            != _require_string(row["prestage_session_sha256"], "prestage digest")
            or g6_version != ready_version + 1
            or (terminal_version is None) is not (terminal_at is None)
            or (terminal_at is not None and terminal_at < created_at)
        ):
            raise WorkflowAtomicityError("Persisted portal run authority is invalid")

        gate_row = connection.execute(
            "SELECT * FROM gate_decisions WHERE sequence = ?",
            (_require_integer(row["g6_gate_sequence"], "G6 gate sequence"),),
        ).fetchone()
        if gate_row is None:
            raise WorkflowAtomicityError("Portal run lost its G6 gate")
        gate_case_id, g6 = _gate_decision_from_row(gate_row, label="G6 decision")
        capability_row = connection.execute(
            "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
            (capability_digest,),
        ).fetchone()
        if capability_row is None:
            raise WorkflowAtomicityError("Portal run lost its agent capability")
        capability = cls._row_to_capability(capability_row)
        state_row = connection.execute(
            "SELECT * FROM audit_events WHERE sequence = ?",
            (_require_integer(row["g6_state_audit_sequence"], "G6 state sequence"),),
        ).fetchone()
        if state_row is None:
            raise WorkflowAtomicityError("Portal run lost its G6 state event")
        state_audit = AuditEvent.model_validate_json(
            _require_string(state_row["event_json"], "G6 state audit")
        )
        expected_target = CaseState.FILLING if g6.passed else CaseState.BLOCKED
        if (
            gate_case_id != case_id
            or g6.gate_id is not GateId.G6_TOOL_AUTHORITY
            or g6.decided_at != created_at
            or capability.case_id != case_id
            or capability.role != "agent"
            or capability.purpose != "portal_run"
            or capability.portal_variant is not None
            or capability.bound_case_version != ready_version
            or capability.consumed_at is None
            or capability.revoked_at is not None
            or not capability.issued_at <= capability.consumed_at < created_at
            or state_audit.case_id != case_id
            or state_audit.event_type is not AuditEventType.CASE_STATE_CHANGED
            or state_audit.from_state is not CaseState.READY_TO_FILL
            or state_audit.to_state is not expected_target
            or state_audit.occurred_at != created_at
            or (status == "blocked_g6") is not (not g6.passed)
            or (status == "filling") is not (g6.passed and terminal_version is None)
        ):
            raise WorkflowAtomicityError("Persisted G6 authority binding is invalid")
        return PortalRunRecord(
            run_id=run_id,
            case_id=case_id,
            portal_variant=variant,
            ready_case_version=ready_version,
            g6_case_version=g6_version,
            terminal_case_version=terminal_version,
            status=status,
            invocation=invocation,
            g6_decision=g6,
            prestage_session=prestage,
            created_at=created_at,
            terminal_at=terminal_at,
        )

    @staticmethod
    def _require_open_portal_run(
        current: CaseRecord,
        run: PortalRunRecord,
    ) -> None:
        if (
            run.case_id != current.case_id
            or run.status != "filling"
            or run.g6_case_version != current.version
            or current.state is not CaseState.FILLING
            or not run.g6_decision.passed
        ):
            raise WorkflowAtomicityError("Portal run is not open")

    @staticmethod
    def _portal_session_from_row(
        row: sqlite3.Row,
    ) -> tuple[PortalSessionView, RenderedPortalSnapshot]:
        session_json = _require_string(row["session_json"], "portal session")
        rendered_json = _require_string(
            row["rendered_snapshot_json"],
            "portal rendered snapshot",
        )
        session = PortalSessionView.model_validate_json(session_json)
        rendered = RenderedPortalSnapshot.model_validate_json(rendered_json)
        case_id = _require_string(row["case_id"], "portal session case id")
        portal_version = _require_integer(row["portal_version"], "portal version")
        if (
            _require_integer(row["authority_version"], "portal session authority version") != 1
            or session_json != _dump_contract(session)
            or rendered_json != _dump_contract(rendered)
            or _authority_sha256("portal-session", session_json)
            != _require_string(row["session_sha256"], "portal session digest")
            or _authority_sha256("portal-rendered", rendered_json)
            != _require_string(row["rendered_snapshot_sha256"], "rendered digest")
            or session.case_id != case_id
            or rendered.case_id != case_id
            or session.state is not PortalState.REVIEW
            or rendered.state is not PortalState.REVIEW
            or session.variant is not rendered.variant
            or session.version != portal_version
            or rendered.version != portal_version
        ):
            raise WorkflowAtomicityError("Persisted portal checkpoint is invalid")
        return session, rendered

    @staticmethod
    def _verification_attempt_from_row(row: sqlite3.Row) -> VerificationAttempt:
        attempt_json = _require_string(row["attempt_json"], "verification attempt")
        rendered_json = _require_string(
            row["rendered_snapshot_json"],
            "verification rendered snapshot",
        )
        attempt = VerificationAttempt.model_validate_json(attempt_json)
        rendered = RenderedPortalSnapshot.model_validate_json(rendered_json)
        requested_at = _parse_datetime(
            _require_string(row["snapshot_requested_at"], "snapshot requested_at")
        )
        received_at = _parse_datetime(
            _require_string(row["snapshot_received_at"], "snapshot received_at")
        )
        created_at = _parse_datetime(_require_string(row["created_at"], "verification created_at"))
        screenshot_sha256 = _require_string(
            row["screenshot_sha256"],
            "verification screenshot digest",
        )
        if (
            _require_integer(row["authority_version"], "attempt authority version") != 1
            or attempt_json != _dump_contract(attempt)
            or rendered_json != _dump_contract(rendered)
            or _verification_authority_sha256(
                attempt_json=attempt_json,
                rendered_json=rendered_json,
                screenshot_sha256=screenshot_sha256,
                requested_at=requested_at,
                received_at=received_at,
            )
            != _require_string(row["attempt_sha256"], "attempt digest")
            or _authority_sha256("verification-rendered", rendered_json)
            != _require_string(row["rendered_snapshot_sha256"], "verification rendered digest")
            or _SHA256.fullmatch(screenshot_sha256) is None
            or attempt.attempt_id != _require_string(row["attempt_id"], "attempt id")
            or attempt.case_id != _require_string(row["case_id"], "attempt case id")
            or attempt.attempt_number != _require_integer(row["attempt_number"], "attempt number")
            or attempt.portal_version != rendered.version
            or attempt.case_id != rendered.case_id
            or attempt.final is not bool(_require_integer(row["final"], "attempt final"))
            or not requested_at <= rendered.rendered_at <= received_at <= created_at
        ):
            raise WorkflowAtomicityError("Persisted verification attempt is invalid")
        return attempt

    def _snapshot_cu_components(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> tuple[PortalSessionView | None, VerificationAttemptSeries | None]:
        if current.state in {CaseState.RECEIPT, CaseState.HUMAN_APPROVED}:
            return None, None
        run_row = connection.execute(
            "SELECT * FROM portal_run_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if run_row is None:
            return None, None
        run = self._row_to_portal_run(connection, run_row)
        session_row = connection.execute(
            """
            SELECT * FROM portal_session_authority
            WHERE case_id = ? ORDER BY checkpoint_number DESC LIMIT 1
            """,
            (current.case_id,),
        ).fetchone()
        portal_session = (
            run.prestage_session
            if session_row is None
            else self._portal_session_from_row(session_row)[0]
        )
        attempt_rows = connection.execute(
            """
            SELECT * FROM verification_attempt_authority
            WHERE case_id = ? ORDER BY attempt_number
            """,
            (current.case_id,),
        ).fetchall()
        attempts = tuple(self._verification_attempt_from_row(row) for row in attempt_rows)
        series = None
        if attempts and attempts[-1].final:
            series = VerificationAttemptSeries.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "caseId": current.case_id,
                    "attempts": attempts,
                }
            )
        return portal_session, series

    def get_workflow_snapshot(
        self,
        case_id: str,
        *,
        request_id: str,
    ) -> WorkflowSnapshot:
        """Read every canonical snapshot component from one WAL snapshot."""

        self._require_canonical_authority_mode()
        if type(request_id) is not str or _IDENTIFIER.fullmatch(request_id) is None:
            raise ValueError("request_id must be a canonical identifier")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                raise CaseRecordNotFoundError(case_id)
            current = self._row_to_case(row)
            transcript = self._get_transcript_confirmation_view_in_connection(
                connection,
                current,
            )
            receipt_record = self._get_sandbox_receipt_in_connection(
                connection,
                case_id,
            )
            portal_session, verification_attempts = self._snapshot_cu_components(
                connection,
                current,
            )
            return WorkflowSnapshot.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "requestId": request_id,
                    "case": {
                        "contractVersion": CONTRACT_VERSION,
                        "caseId": current.case_id,
                        "state": current.state,
                        "version": current.version,
                        "createdAt": current.created_at,
                        "updatedAt": current.updated_at,
                    },
                    "claimPacket": current.snapshot.claim_packet,
                    "transcriptConfirmation": transcript,
                    "clarification": current.snapshot.active_clarification,
                    "portalSession": portal_session,
                    "verificationAttempts": verification_attempts,
                    "receipt": None if receipt_record is None else receipt_record.receipt,
                }
            )

    def commit_intake_disclosure(
        self,
        command: IntakeDisclosureCommand,
    ) -> CaseRecord:
        """Stage media, recompute G0/G1, and bind all authority in one CAS."""

        self._require_canonical_authority_mode()
        from claimdone_api.media import (
            IntakeRequest,
            PrivacyReview,
            prepare_g1,
            start_intake,
        )

        if type(command) is not IntakeDisclosureCommand:
            raise TypeError("command must be an IntakeDisclosureCommand")
        if type(command.case_id) is not str or _IDENTIFIER.fullmatch(command.case_id) is None:
            raise TypeError("Intake case_id must be an exact canonical identifier")
        if type(command.expected_version) is not int or command.expected_version < 1:
            raise TypeError("Intake expected_version must be an exact positive integer")
        if type(command.request) is not IntakeRequest:
            raise TypeError("Intake request must use the exact canonical type")
        if type(command.privacy_review) is not PrivacyReview:
            raise TypeError("Privacy review must use the exact canonical type")
        timestamps = (
            command.g0_decided_at,
            command.g1_decided_at,
            command.updated_at,
        )
        if any(type(value) is not datetime for value in timestamps):
            raise TypeError("Intake timestamps must use exact datetime values")
        if any(value.utcoffset() is None for value in timestamps):
            raise WorkflowAtomicityError("Intake timestamps must include a timezone")
        if tuple(sorted(timestamps)) != timestamps:
            raise WorkflowAtomicityError("Intake timestamps must be monotonic")

        with closing(self._connect()) as connection:
            preliminary = self._require_current(
                connection,
                command.case_id,
                command.expected_version,
            )
            if preliminary.state is not CaseState.CREATED:
                raise WorkflowAtomicityError("Canonical intake requires a pristine CREATED case")
            if command.g0_decided_at < preliminary.updated_at:
                raise WorkflowAtomicityError(
                    "G0 cannot be decided before the current case version exists"
                )

        start = start_intake(
            self.__media_store,
            command.request,
            decided_at=command.g0_decided_at,
        )
        if not start.decision.passed or start.session is None:
            raise WorkflowAtomicityError("Canonical intake cannot commit a failed G0")
        session = start.session
        try:
            privacy = prepare_g1(
                self.__media_store,
                session,
                command.privacy_review,
                decided_at=command.g1_decided_at,
            )
            if not privacy.decision.passed or privacy.prepared is None:
                raise WorkflowAtomicityError("Canonical intake cannot commit a failed G1")
        except BaseException:
            with suppress(Exception):
                self.__media_store.delete_case(session.handle)
            raise
        try:
            return self._commit_staged_intake_disclosure(
                command=command,
                session=session,
                prepared=privacy.prepared,
                statement=session.text,
                staged_g0=start.decision,
                staged_g1=privacy.decision,
            )
        except Exception as error:
            # The random handle belongs solely to this attempt until its DB
            # transaction succeeds. Preserve it on uncertain SQLite commit errors.
            if not isinstance(error, sqlite3.Error):
                with suppress(Exception):
                    self.__media_store.delete_case(session.handle)
            raise

    def _commit_staged_intake_disclosure(
        self,
        *,
        command: IntakeDisclosureCommand,
        session: object,
        prepared: object,
        statement: object | None,
        staged_g0: GateDecision,
        staged_g1: GateDecision,
    ) -> CaseRecord:
        from claimdone_api.media import (
            IntakeSession,
            PreparedMedia,
            StoredAssetRef,
            evaluate_g1,
            validate_g0,
        )

        if (
            type(session) is not IntakeSession
            or type(prepared) is not PreparedMedia
            or (statement is not None and type(statement) is not StoredAssetRef)
        ):
            raise TypeError("Staged intake values must use exact canonical media types")
        with self._write_connection() as connection:
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_version,
            )
            if command.g0_decided_at < current.updated_at:
                raise WorkflowAtomicityError(
                    "G0 cannot be decided before the current case version exists"
                )
            if (
                current.state is not CaseState.CREATED
                or current.snapshot.intake_summary is not None
                or current.snapshot.claim_packet is not None
                or current.snapshot.active_clarification is not None
                or current.snapshot.portal_state is not PortalState.DRAFT
                or self._read_gate_decisions(connection, case_id=current.case_id)
                or connection.execute(
                    "SELECT 1 FROM case_media_handles WHERE case_id = ?",
                    (current.case_id,),
                ).fetchone()
                is not None
                or connection.execute(
                    "SELECT 1 FROM case_intake_authority WHERE case_id = ?",
                    (current.case_id,),
                ).fetchone()
                is not None
            ):
                raise WorkflowAtomicityError("Canonical intake requires a pristine CREATED case")

            recomputed_g0 = validate_g0(
                command.request,
                decided_at=command.g0_decided_at,
            )
            recomputed_g1 = evaluate_g1(
                session,
                command.privacy_review,
                decided_at=command.g1_decided_at,
            )
            if (
                recomputed_g0.validated is None
                or not recomputed_g0.decision.passed
                or recomputed_g0.decision != staged_g0
                or not recomputed_g1.passed
                or recomputed_g1 != staged_g1
            ):
                raise WorkflowAtomicityError(
                    "Staged intake disagrees with canonical G0/G1 recomputation"
                )
            summary, manifest = self._verified_intake_manifest(
                case_id=current.case_id,
                bound_case_version=current.version + 1,
                request=command.request,
                review=command.privacy_review,
                validated=recomputed_g0.validated,
                session=session,
                prepared=prepared,
                statement=statement,
            )
            manifest_json = _dump_json_object(manifest)
            manifest_digest = hashlib.sha256(
                b"claimdone-intake-authority-v1\0" + manifest_json.encode("utf-8")
            ).hexdigest()
            snapshot = replace(current.snapshot, intake_summary=summary)
            target = CaseState.DISCLOSED
            _validate_snapshot(current.case_id, target, snapshot)
            connection.execute(
                """
                INSERT INTO case_media_handles (case_id, storage_name, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    current.case_id,
                    session.handle.storage_name,
                    _dump_aware_datetime(command.updated_at, "media handle created_at"),
                ),
            )
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=command.updated_at,
            )
            gate_sequences: list[int] = []
            for decision in (recomputed_g0.decision, recomputed_g1):
                gate_sequences.append(
                    self._insert_gate_decision_row(
                        connection,
                        case_id=current.case_id,
                        decision=decision,
                    )
                )
                audit = build_gate_audit_event(
                    case_id=current.case_id,
                    decision=decision,
                    actor=ActorType.SYSTEM,
                )
                audit_sequence = self._insert_audit_event(connection, audit)
                self._insert_workflow_projection(
                    connection,
                    audit_sequence=audit_sequence,
                    audit=audit,
                    event=GateWorkflowEvent.model_validate(
                        {"kind": WorkflowEventKind.GATE, "decision": decision}
                    ),
                )
            connection.execute(
                """
                INSERT INTO case_intake_authority (
                    case_id, authority_version, bound_case_version, storage_name,
                    manifest_json, manifest_sha256, g0_gate_sequence,
                    g1_gate_sequence, created_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current.case_id,
                    current.version + 1,
                    session.handle.storage_name,
                    manifest_json,
                    manifest_digest,
                    gate_sequences[0],
                    gate_sequences[1],
                    _dump_aware_datetime(command.updated_at, "intake authority created_at"),
                ),
            )
            event = build_state_change_event(
                case_id=current.case_id,
                current=current.state,
                target=target,
                actor=ActorType.HUMAN,
                occurred_at=command.updated_at,
            )
            audit_sequence = self._insert_audit_event(connection, event)
            self._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.HUMAN,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            return self._require_current(
                connection,
                current.case_id,
                current.version + 1,
            )

    def _verified_intake_manifest(
        self,
        *,
        case_id: str,
        bound_case_version: int,
        request: object,
        review: object,
        validated: object,
        session: object,
        prepared: object,
        statement: object | None,
    ) -> tuple[JsonObject, JsonObject]:
        """Verify every staged byte and derive the only canonical manifest."""

        from claimdone_api.media import (
            ExifDecision,
            IntakeRequest,
            IntakeSession,
            PreparedMedia,
            PrivacyReview,
            StoredAssetRef,
            expected_model_image_bytes,
        )
        from claimdone_api.media.types import ValidatedIntake

        if (
            type(request) is not IntakeRequest
            or type(review) is not PrivacyReview
            or type(validated) is not ValidatedIntake
            or type(session) is not IntakeSession
            or type(prepared) is not PreparedMedia
            or (statement is not None and type(statement) is not StoredAssetRef)
        ):
            raise TypeError("Canonical intake manifest received a non-canonical value")
        if (
            session.handle != prepared.handle
            or _MEDIA_STORAGE_NAME.fullmatch(session.handle.storage_name) is None
            or len(session.images) != 3
            or len(prepared.model_images) != 3
        ):
            raise WorkflowAtomicityError("Staged media shape is invalid")

        choice_by_id = {choice.input_id: choice.decision for choice in review.exif_choices}

        def asset_json(asset: StoredAssetRef) -> dict[str, str]:
            return {
                "fileId": asset.file_id,
                "mediaType": asset.media_type,
                "sha256": asset.sha256,
            }

        images: list[JsonObject] = []
        summary_images: list[JsonObject] = []
        for validated_image, stored_image, model_asset in zip(
            validated.images,
            session.images,
            prepared.model_images,
            strict=True,
        ):
            if (
                stored_image.input_id != validated_image.input_id
                or stored_image.image_format is not validated_image.image_format
                or stored_image.source.media_type != validated_image.media_type
                or stored_image.source.sha256 != validated_image.sha256
                or not isinstance(
                    choice_by_id.get(stored_image.input_id),
                    ExifDecision,
                )
                or model_asset.media_type != stored_image.source.media_type
            ):
                raise WorkflowAtomicityError("Staged image metadata is not G0/G1-bound")
            source_bytes = self.__media_store.read_bytes(
                session.handle,
                stored_image.source,
            )
            if source_bytes != validated_image.content:
                raise WorkflowAtomicityError("Staged source image bytes changed after G0")
            model_ref = StoredAssetRef(
                file_id=model_asset.local_ref,
                media_type=model_asset.media_type,
                sha256=model_asset.sha256,
            )
            if self.__media_store.path_for(session.handle, model_ref) != model_asset.path:
                raise WorkflowAtomicityError("Model image path is not store-owned")
            expected = expected_model_image_bytes(
                source_bytes,
                image_format=validated_image.image_format.value,
                decision=choice_by_id[stored_image.input_id],
            )
            if self.__media_store.read_bytes(session.handle, model_ref) != expected:
                raise WorkflowAtomicityError("Provider-visible image bytes violate G1")
            source_json = asset_json(stored_image.source)
            model_json = asset_json(model_ref)
            images.append(
                cast(
                    JsonObject,
                    {
                        "inputId": stored_image.input_id,
                        "imageFormat": stored_image.image_format.value,
                        "source": source_json,
                        "model": model_json,
                    },
                )
            )
            summary_images.append(
                cast(
                    JsonObject,
                    {
                        "inputId": stored_image.input_id,
                        "source": model_json,
                        "imageFormat": stored_image.image_format.value,
                    },
                )
            )

        text_json: dict[str, str] | None = None
        audio_json: dict[str, str] | None = None
        if validated.normalized_text is not None:
            if session.text is None or session.audio is not None or prepared.audio is not None:
                raise WorkflowAtomicityError("Text intake mode changed after G0")
            text_bytes = self.__media_store.read_bytes(session.handle, session.text)
            if text_bytes != validated.normalized_text.encode("utf-8"):
                raise WorkflowAtomicityError("Stored text changed after G0")
            if statement != session.text or prepared.text != validated.normalized_text:
                raise WorkflowAtomicityError("Statement is not the exact G0 text")
            text_json = asset_json(session.text)
            statement_json: dict[str, str] | None = asset_json(session.text)
            transcript_state = "not_applicable"
            input_mode = "text"
        else:
            if validated.audio is None or session.audio is None or session.text is not None:
                raise WorkflowAtomicityError("Audio intake mode changed after G0")
            audio_bytes = self.__media_store.read_bytes(session.handle, session.audio)
            if (
                audio_bytes != validated.audio.content
                or prepared.audio is None
                or prepared.audio.local_ref != session.audio.file_id
                or prepared.audio.media_type != session.audio.media_type
                or prepared.audio.sha256 != session.audio.sha256
                or self.__media_store.path_for(session.handle, session.audio) != prepared.audio.path
            ):
                raise WorkflowAtomicityError("Stored audio changed after G0/G1")
            audio_json = asset_json(session.audio)
            if statement is not None or prepared.text is not None:
                raise WorkflowAtomicityError(
                    "Audio intake cannot carry transcript content before transcription"
                )
            statement_json = None
            transcript_state = "awaiting_transcription"
            input_mode = "audio"

        choices = [
            cast(
                JsonObject,
                {"inputId": image.input_id, "decision": choice_by_id[image.input_id].value},
            )
            for image in session.images
        ]
        duration = session.audio_duration_seconds
        summary = cast(
            JsonObject,
            {
                "images": summary_images,
                "text": text_json,
                "audio": audio_json,
                "statement": statement_json,
                "exifDecisions": [choice["decision"] for choice in choices],
                "audioDurationNumerator": None if duration is None else duration.numerator,
                "audioDurationDenominator": None if duration is None else duration.denominator,
            },
        )
        manifest = cast(
            JsonObject,
            {
                "authorityVersion": 1,
                "caseId": case_id,
                "boundCaseVersion": bound_case_version,
                "storageName": session.handle.storage_name,
                "inputOrder": [image.input_id for image in session.images],
                "images": images,
                "inputMode": input_mode,
                "consents": {
                    "sandboxAcknowledged": request.consents.sandbox_acknowledged,
                    "imageRightsConfirmed": request.consents.image_rights_confirmed,
                    "dataProcessingApproved": request.consents.data_processing_approved,
                },
                "text": text_json,
                "audio": audio_json,
                "statement": statement_json,
                "exifChoices": choices,
                "modelCopyApproved": review.model_copy_approved,
                "transcriptState": transcript_state,
                "intakeSummary": summary,
            },
        )
        return summary, manifest

    def commit_transcription_outcome(
        self,
        command: TranscriptionOutcomeCommand,
    ) -> TranscriptTransitionResult:
        """Bind one successful provider transcript to audio authority atomically."""

        self._require_canonical_authority_mode()
        from claimdone_api.ai import TranscriptionSuccess
        from claimdone_api.media import CaseHandle

        if type(command) is not TranscriptionOutcomeCommand:
            raise TypeError("command must be a TranscriptionOutcomeCommand")
        if type(command.case_id) is not str or _IDENTIFIER.fullmatch(command.case_id) is None:
            raise TypeError("Transcription case_id must be an exact canonical identifier")
        if type(command.expected_version) is not int or command.expected_version < 1:
            raise TypeError("Transcription expected_version must be an exact positive integer")
        if type(command.occurred_at) is not datetime or type(command.updated_at) is not datetime:
            raise TypeError("Transcription timestamps must use exact datetime values")
        if type(command.outcome) is not TranscriptionSuccess:
            raise TypeError("Transcription outcome must use the exact canonical type")
        transcript_text = command.outcome.transcript
        if type(transcript_text) is not str:
            raise TypeError("Transcript output must be exact text")
        normalized = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFC", transcript_text),
        ).strip()
        if not normalized or normalized != transcript_text or len(normalized) > 4_000:
            raise WorkflowAtomicityError("Transcript output is not canonically normalized")
        event = command.outcome.telemetry.to_success_event()
        self._require_canonical_contract(event, "ProviderCallWorkflowEvent")
        if (
            type(event) is not ProviderCallWorkflowEvent
            or event.operation is not WorkflowOperation.TRANSCRIPTION
            or event.retry_attempt != 0
            or event.call_sequence != 1
            or command.occurred_at.utcoffset() is None
            or command.updated_at.utcoffset() is None
            or command.occurred_at > command.updated_at
        ):
            raise WorkflowAtomicityError(
                "Transcription outcome requires one successful transcription provider event"
            )

        with closing(self._connect()) as connection:
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_version,
            )
            manifest, intake_digest = self._require_intake_authority(connection, current)
            if (
                current.state is not CaseState.DISCLOSED
                or manifest.get("inputMode") != "audio"
                or command.occurred_at < current.updated_at
            ):
                raise WorkflowAtomicityError(
                    "Transcription requires a disclosed canonical audio intake"
                )
            storage_name = cast(str, manifest["storageName"])
        handle = CaseHandle(storage_name=storage_name)
        transcript_ref = self.__media_store.write_bytes(
            handle,
            normalized.encode("utf-8"),
            role="transcript",
            suffix=".txt",
            media_type="text/plain",
        )
        try:
            with self._write_connection() as connection:
                current = self._require_current(
                    connection,
                    command.case_id,
                    command.expected_version,
                )
                manifest, current_intake_digest = self._require_intake_authority(
                    connection,
                    current,
                )
                if (
                    current.state is not CaseState.DISCLOSED
                    or manifest.get("inputMode") != "audio"
                    or current_intake_digest != intake_digest
                    or manifest.get("storageName") != storage_name
                    or connection.execute(
                        "SELECT 1 FROM case_transcripts WHERE case_id = ?",
                        (current.case_id,),
                    ).fetchone()
                    is not None
                    or connection.execute(
                        "SELECT 1 FROM case_transcript_authority WHERE case_id = ?",
                        (current.case_id,),
                    ).fetchone()
                    is not None
                ):
                    raise WorkflowAtomicityError(
                        "Transcription authority is stale or already consumed"
                    )
                if self.__media_store.read_bytes(handle, transcript_ref) != normalized.encode(
                    "utf-8"
                ):
                    raise WorkflowAtomicityError("Stored transcript bytes changed")
                summary = current.snapshot.intake_summary
                if summary is None:
                    raise WorkflowAtomicityError("Audio intake summary is missing")
                statement_json: JsonObject = cast(
                    JsonObject,
                    {
                        "fileId": transcript_ref.file_id,
                        "mediaType": transcript_ref.media_type,
                        "sha256": transcript_ref.sha256,
                    },
                )
                next_summary = dict(summary)
                next_summary["statement"] = statement_json
                transcript_id, local_ref, digest = _transcript_identity_from_summary(
                    current.case_id,
                    next_summary,
                )
                target = CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
                snapshot = replace(current.snapshot, intake_summary=next_summary)
                _validate_snapshot(current.case_id, target, snapshot)
                self._update_case_row(
                    connection,
                    current=current,
                    state=target,
                    snapshot=snapshot,
                    updated_at=command.updated_at,
                )
                connection.execute(
                    """
                    INSERT INTO case_transcripts (
                        transcript_id, case_id, version, bound_case_version,
                        transcript_sha256, local_ref, confirmed, created_at, confirmed_at
                    ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, NULL)
                    """,
                    (
                        transcript_id,
                        current.case_id,
                        current.version + 1,
                        digest,
                        local_ref,
                        _dump_aware_datetime(command.updated_at, "transcript created_at"),
                    ),
                )
                provider_envelope = self._insert_redacted_workflow_event(
                    connection,
                    case_id=current.case_id,
                    event=event,
                    actor=ActorType.AGENT,
                    occurred_at=command.occurred_at,
                )
                transcript_manifest = cast(
                    JsonObject,
                    {
                        "authorityVersion": 1,
                        "caseId": current.case_id,
                        "boundCaseVersion": current.version + 1,
                        "storageName": storage_name,
                        "intakeManifestSha256": intake_digest,
                        "transcriptId": transcript_id,
                        "transcript": statement_json,
                        "providerSourceAuditSequence": provider_envelope.source_audit_sequence,
                    },
                )
                transcript_manifest_json = _dump_json_object(transcript_manifest)
                transcript_manifest_digest = hashlib.sha256(
                    b"claimdone-transcript-authority-v1\0"
                    + transcript_manifest_json.encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO case_transcript_authority (
                        case_id, authority_version, bound_case_version,
                        intake_manifest_sha256, transcript_id, transcript_local_ref,
                        transcript_sha256, provider_source_audit_sequence,
                        manifest_json, manifest_sha256, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current.case_id,
                        current.version + 1,
                        intake_digest,
                        transcript_id,
                        local_ref,
                        digest,
                        provider_envelope.source_audit_sequence,
                        transcript_manifest_json,
                        transcript_manifest_digest,
                        _dump_aware_datetime(
                            command.updated_at,
                            "transcript authority created_at",
                        ),
                    ),
                )
                state_event = build_state_change_event(
                    case_id=current.case_id,
                    current=current.state,
                    target=target,
                    actor=ActorType.SYSTEM,
                    occurred_at=command.updated_at,
                )
                audit_sequence = self._insert_audit_event(connection, state_event)
                self._insert_workflow_projection(
                    connection,
                    audit_sequence=audit_sequence,
                    audit=state_event,
                    event=StateWorkflowEvent.model_validate(
                        {
                            "kind": WorkflowEventKind.STATE,
                            "actor": ActorType.SYSTEM,
                            "fromState": current.state,
                            "toState": target,
                        }
                    ),
                )
                case = self._require_current(
                    connection,
                    current.case_id,
                    current.version + 1,
                )
                transcript = self._require_transcript(connection, current.case_id)
            return TranscriptTransitionResult(case=case, transcript=transcript)
        except Exception as error:
            # A SQLite error may mean COMMIT outcome is unknown; in that case
            # retain bytes so a committed authority never points at a missing file.
            if not isinstance(error, sqlite3.Error):
                with suppress(Exception):
                    self.__media_store.delete_asset(handle, transcript_ref)
            raise

    def begin_text_analysis(
        self,
        *,
        case_id: str,
        expected_version: int,
        updated_at: datetime,
    ) -> CaseRecord:
        """Enter analysis only for an authority-bound text intake, without deltas."""

        self._require_canonical_authority_mode()
        self._require_expected_version(
            expected_version,
            "Text analysis expected_version",
        )
        target = CaseState.ANALYZING
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_version)
            manifest, _digest = self._require_intake_authority(connection, current)
            self._require_passed_g0_g1_history(connection, current)
            if (
                current.state is not CaseState.DISCLOSED
                or manifest.get("inputMode") != "text"
                or updated_at.utcoffset() is None
                or updated_at < current.updated_at
            ):
                raise WorkflowAtomicityError(
                    "Text analysis requires a disclosed canonical text intake"
                )
            validate_case_transition(current.state, target)
            snapshot = current.snapshot
            _validate_snapshot(case_id, target, snapshot)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=updated_at,
            )
            event = build_state_change_event(
                case_id=case_id,
                current=current.state,
                target=target,
                actor=ActorType.SYSTEM,
                occurred_at=updated_at,
            )
            audit_sequence = self._insert_audit_event(connection, event)
            self._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.SYSTEM,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            return self._require_current(connection, case_id, current.version + 1)

    def commit_analysis_workflow(
        self,
        command: AnalysisWorkflowCommand,
    ) -> AnalysisWorkflowResult:
        """Commit one deterministic analysis/clarification boundary atomically.

        Audit cursors are always allocated in this order: provider attempts,
        canonical gates, visible plan steps, clarification lifecycle, and any
        real state transition. The case version advances exactly once; a new
        clarification round never forges a self-transition event.
        """

        self._require_canonical_authority_mode()
        if not isinstance(command, AnalysisWorkflowCommand):
            raise TypeError("command must be an AnalysisWorkflowCommand")
        self._validate_analysis_command_shape(command)
        with self._write_connection() as connection:
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_version,
            )
            intake_manifest, intake_digest = self._require_intake_authority(
                connection,
                current,
            )
            if intake_manifest.get("inputMode") == "audio":
                self._require_transcript_authority(
                    connection,
                    current,
                    intake_manifest_digest=intake_digest,
                )
            self._require_current_packet_authority(connection, current)
            existing_gates = self._read_gate_decisions(
                connection,
                case_id=command.case_id,
            )
            snapshot = self._validate_analysis_command(
                connection,
                current,
                command,
                existing_gates=existing_gates,
            )
            self._update_case_row(
                connection,
                current=current,
                state=command.target,
                snapshot=snapshot,
                updated_at=command.updated_at,
            )

            emitted: list[WorkflowEventEnvelope] = []
            for emission in command.provider_events:
                emitted.append(
                    self._insert_redacted_workflow_event(
                        connection,
                        case_id=command.case_id,
                        event=emission.event,
                        actor=ActorType.AGENT,
                        occurred_at=emission.occurred_at,
                    )
                )
            for decision in command.gate_decisions:
                self._insert_gate_decision_row(
                    connection,
                    case_id=command.case_id,
                    decision=decision,
                )
                audit = build_gate_audit_event(
                    case_id=command.case_id,
                    decision=decision,
                    actor=ActorType.SYSTEM,
                )
                audit_sequence = self._insert_audit_event(connection, audit)
                emitted.append(
                    self._insert_workflow_projection(
                        connection,
                        audit_sequence=audit_sequence,
                        audit=audit,
                        event=GateWorkflowEvent.model_validate(
                            {"kind": WorkflowEventKind.GATE, "decision": decision}
                        ),
                    )
                )
            for plan_event in command.plan_steps:
                emitted.append(
                    self._insert_redacted_workflow_event(
                        connection,
                        case_id=command.case_id,
                        event=plan_event,
                        actor=ActorType.AGENT,
                        occurred_at=command.updated_at,
                    )
                )
            for clarification_event in command.clarification_events:
                emitted.append(
                    self._insert_redacted_workflow_event(
                        connection,
                        case_id=command.case_id,
                        event=clarification_event,
                        actor=ActorType.SYSTEM,
                        occurred_at=command.updated_at,
                    )
                )

            if command.claim_packet is not None:
                self._insert_packet_authority(
                    connection,
                    case_id=command.case_id,
                    bound_case_version=current.version + 1,
                    packet=command.claim_packet,
                    created_at=command.updated_at,
                )

            if current.state is not command.target:
                state_audit = build_state_change_event(
                    case_id=command.case_id,
                    current=current.state,
                    target=command.target,
                    actor=ActorType.SYSTEM,
                    occurred_at=command.updated_at,
                )
                state_sequence = self._insert_audit_event(connection, state_audit)
                emitted.append(
                    self._insert_workflow_projection(
                        connection,
                        audit_sequence=state_sequence,
                        audit=state_audit,
                        event=StateWorkflowEvent.model_validate(
                            {
                                "kind": WorkflowEventKind.STATE,
                                "actor": ActorType.SYSTEM,
                                "fromState": current.state,
                                "toState": command.target,
                            }
                        ),
                    )
                )
            case = self._require_current(
                connection,
                command.case_id,
                current.version + 1,
            )
        return AnalysisWorkflowResult(case=case, workflow_events=tuple(emitted))

    def commit_terminal_provider_failure(
        self,
        command: TerminalProviderFailureCommand,
    ) -> TerminalProviderFailureResult:
        """Persist one terminal provider failure and failed state atomically.

        Any completed attempt-zero/retry prefix precedes the operational event
        and its ledger row, then the failed-state projection. No GateDecision
        is generated by this boundary.
        """

        self._require_canonical_authority_mode()
        if not isinstance(command, TerminalProviderFailureCommand):
            raise TypeError("command must be a TerminalProviderFailureCommand")
        self._validate_terminal_provider_command_shape(command)
        target = CaseState.FAILED
        with self._write_connection() as connection:
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_version,
            )
            intake_manifest, intake_digest = self._require_intake_authority(
                connection,
                current,
            )
            if intake_manifest.get("inputMode") == "audio" and not (
                current.state is CaseState.DISCLOSED
                and command.event.operation is WorkflowOperation.TRANSCRIPTION
            ):
                self._require_transcript_authority(
                    connection,
                    current,
                    intake_manifest_digest=intake_digest,
                )
            self._require_current_packet_authority(connection, current)
            snapshot = self._validate_terminal_provider_failure(
                connection,
                current,
                command,
                intake_manifest=intake_manifest,
            )
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=command.occurred_at,
            )
            emitted: list[WorkflowEventEnvelope] = []
            for emission in command.provider_events:
                emitted.append(
                    self._insert_redacted_workflow_event(
                        connection,
                        case_id=command.case_id,
                        event=emission.event,
                        actor=ActorType.AGENT,
                        occurred_at=emission.occurred_at,
                    )
                )
            emitted.append(
                self._insert_redacted_workflow_event(
                    connection,
                    case_id=command.case_id,
                    event=command.event,
                    actor=ActorType.SYSTEM,
                    occurred_at=command.occurred_at,
                )
            )
            if snapshot.claim_packet is not None:
                self._insert_packet_authority(
                    connection,
                    case_id=command.case_id,
                    bound_case_version=current.version + 1,
                    packet=snapshot.claim_packet,
                    created_at=command.occurred_at,
                )
            state_audit = build_state_change_event(
                case_id=command.case_id,
                current=current.state,
                target=target,
                actor=ActorType.SYSTEM,
                occurred_at=command.occurred_at,
            )
            state_sequence = self._insert_audit_event(connection, state_audit)
            emitted.append(
                self._insert_workflow_projection(
                    connection,
                    audit_sequence=state_sequence,
                    audit=state_audit,
                    event=StateWorkflowEvent.model_validate(
                        {
                            "kind": WorkflowEventKind.STATE,
                            "actor": ActorType.SYSTEM,
                            "fromState": current.state,
                            "toState": target,
                        }
                    ),
                )
            )
            case = self._require_current(
                connection,
                command.case_id,
                current.version + 1,
            )
        return TerminalProviderFailureResult(
            case=case,
            workflow_events=tuple(emitted),
        )

    def append_workflow_event(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        event: AppendableWorkflowEvent,
        actor: ActorType,
        occurred_at: datetime,
    ) -> WorkflowEventEnvelope:
        """Reject split workflow truth; dedicated commands own every event append."""

        self._require_expected_version(
            expected_case_version,
            "Workflow event expected_case_version",
        )
        _dump_aware_datetime(occurred_at, "workflow occurred_at")
        if event.kind in {WorkflowEventKind.STATE, WorkflowEventKind.GATE}:
            raise ValueError(
                "State and gate workflow projections require their atomic mutation paths"
            )
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_case_version)
            if occurred_at < current.updated_at:
                raise WorkflowAtomicityError(
                    "Generic workflow event timestamps cannot predate the case version"
                )
            raise WorkflowAtomicityError(
                "Generic workflow event appends are closed; use a dedicated atomic command"
            )

    def list_audit_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedAuditEvent, ...]:
        self._validate_page(after, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_json
                FROM audit_events
                WHERE case_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (case_id, after, limit),
            ).fetchall()
        return tuple(
            SequencedAuditEvent(
                sequence=_require_integer(row["sequence"], "audit sequence"),
                event=AuditEvent.model_validate_json(
                    _require_string(row["event_json"], "audit event")
                ),
            )
            for row in rows
        )

    def list_gate_decisions(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedGateDecision, ...]:
        self._validate_page(after, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, decision_json
                FROM gate_decisions
                WHERE case_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (case_id, after, limit),
            ).fetchall()
        return tuple(
            SequencedGateDecision(
                sequence=_require_integer(row["sequence"], "gate sequence"),
                decision=GateDecision.model_validate_json(
                    _require_string(row["decision_json"], "gate decision")
                ),
            )
            for row in rows
        )

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        """Replay completed redacted events after a database-owned cursor."""

        self._validate_page(after, limit)
        try:
            with self._read_connection() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM cases WHERE case_id = ?",
                        (case_id,),
                    ).fetchone()
                    is None
                ):
                    raise CaseRecordNotFoundError(case_id)
                if self.is_canonical_authority:
                    self._validate_all_receipt_authority(
                        connection,
                        selected_case_id=case_id,
                    )
                self._validate_case_workflow_source_bindings(
                    connection,
                    case_id=case_id,
                )
                rows = self._read_workflow_event_rows(
                    connection,
                    case_id=case_id,
                    after=after,
                    limit=limit,
                )
            result = tuple(
                SequencedWorkflowEvent(
                    sequence=_require_integer(
                        row["source_audit_sequence"],
                        "workflow sequence",
                    ),
                    envelope=WorkflowEventEnvelope.model_validate_json(
                        _require_string(row["event_json"], "workflow event")
                    ),
                )
                for row in rows
            )
            validate_workflow_event_order(tuple(item.envelope for item in result))
            if any(item.sequence != item.envelope.cursor for item in result):
                raise ValueError("Persisted workflow cursor does not match its row")
            return result
        except CaseRecordNotFoundError:
            raise
        except PersistedDataIntegrityError as error:
            emit_redacted_log(
                _OBSERVABILITY_LOGGER,
                ObservabilityLogEvent.WORKFLOW_REPLAY_REJECTED,
                fields={"caseId": case_id, "cursor": after},
                error=error,
            )
            raise
        except (ValidationError, ValueError, TypeError, KeyError, IndexError) as error:
            emit_redacted_log(
                _OBSERVABILITY_LOGGER,
                ObservabilityLogEvent.WORKFLOW_REPLAY_REJECTED,
                fields={"caseId": case_id, "cursor": after},
                error=error,
            )
            raise PersistedDataIntegrityError(
                "Persisted workflow event projection is invalid"
            ) from None

    def _validate_case_workflow_source_bindings(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: str,
    ) -> None:
        """Validate both sides of every projected source in one WAL snapshot."""

        case_row = connection.execute(
            "SELECT state FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case_row is None:
            raise PersistedDataIntegrityError("Persisted workflow case state is missing")
        case_state = CaseState(_require_string(case_row["state"], "workflow case state"))

        audits_by_sequence: dict[int, AuditEvent] = {}
        for row in connection.execute(
            "SELECT * FROM audit_events WHERE case_id = ? ORDER BY sequence",
            (case_id,),
        ):
            sequence = _require_integer(row["sequence"], "workflow source audit sequence")
            audit = AuditEvent.model_validate_json(
                _require_string(row["event_json"], "workflow source audit")
            )
            if (
                audit.event_id != _require_string(row["event_id"], "workflow source audit id")
                or audit.case_id != _require_string(row["case_id"], "workflow source audit case id")
                or audit.case_id != case_id
                or audit.occurred_at
                != _parse_datetime(
                    _require_string(
                        row["occurred_at"],
                        "workflow source audit occurred_at",
                    )
                )
            ):
                raise PersistedDataIntegrityError(
                    "Persisted workflow source audit columns are invalid"
                )
            audits_by_sequence[sequence] = audit

        projected_sequences: set[int] = set()
        workflows_by_sequence: dict[int, WorkflowEventEnvelope] = {}
        for row in connection.execute(
            "SELECT * FROM workflow_events WHERE case_id = ? ORDER BY source_audit_sequence",
            (case_id,),
        ):
            envelope = self._workflow_envelope_from_row(
                row,
                label="workflow projection",
            )
            sequence = envelope.source_audit_sequence
            source = audits_by_sequence.get(sequence)
            if source is None or source.event_type not in _WORKFLOW_KIND_BY_AUDIT_EVENT_TYPE:
                raise PersistedDataIntegrityError(
                    "Persisted workflow projection lost its source audit"
                )
            _validate_audit_projection_binding(source, envelope)
            projected_sequences.add(sequence)
            workflows_by_sequence[sequence] = envelope

        required_sequences = {
            sequence
            for sequence, audit in audits_by_sequence.items()
            if audit.event_type in _WORKFLOW_KIND_BY_AUDIT_EVENT_TYPE
        }
        if projected_sequences != required_sequences:
            raise PersistedDataIntegrityError(
                "Persisted workflow source audits and projections are incomplete"
            )

        gate_decisions_by_case: dict[str, list[GateDecision]] = {}
        for row in connection.execute(
            "SELECT * FROM gate_decisions WHERE case_id = ? ORDER BY sequence",
            (case_id,),
        ):
            row_case_id, decision = _gate_decision_from_row(
                row,
                label="workflow gate decision",
            )
            if row_case_id != case_id:
                raise PersistedDataIntegrityError(
                    "Persisted workflow gate decision belongs to another case"
                )
            gate_decisions_by_case.setdefault(row_case_id, []).append(decision)

        _validate_projected_gate_and_state_histories(
            case_states={case_id: case_state},
            gate_decisions_by_case=gate_decisions_by_case,
            workflows_by_sequence=workflows_by_sequence,
        )

    def _read_workflow_event_rows(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: str,
        after: int,
        limit: int,
    ) -> list[sqlite3.Row]:
        """Read event rows from an already case-bound read transaction."""

        return connection.execute(
            """
            SELECT source_audit_sequence, event_json
            FROM workflow_events
            WHERE case_id = ? AND source_audit_sequence > ?
            ORDER BY source_audit_sequence ASC
            LIMIT ?
            """,
            (case_id, after, limit),
        ).fetchall()

    def list_provider_usage(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[ProviderUsageLedgerRecord, ...]:
        self._validate_page(after, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM provider_usage_ledger
                WHERE case_id = ? AND source_audit_sequence > ?
                ORDER BY source_audit_sequence ASC
                LIMIT ?
                """,
                (case_id, after, limit),
            ).fetchall()
        try:
            return tuple(self._row_to_provider_usage(row) for row in rows)
        except (PersistedDataIntegrityError, ValueError, TypeError) as error:
            emit_redacted_log(
                _OBSERVABILITY_LOGGER,
                ObservabilityLogEvent.PROVIDER_USAGE_REJECTED,
                fields={"caseId": case_id, "cursor": after},
                error=error,
            )
            raise PersistedDataIntegrityError(
                "Persisted provider usage telemetry is invalid"
            ) from None

    def get_observability_metrics(
        self,
        case_id: str,
    ) -> ObservabilityMetricsSnapshot:
        """Aggregate only canonical persisted events and provider ledger rows.

        This is a read projection, never a second telemetry writer.  Provider
        calls exclude the synthetic ``retry_scheduled`` row so request duration
        is not double-counted.  Tool metrics use terminal tool-call events only.
        """

        self._require_canonical_authority_mode()
        try:
            workflow_rows, provider_rows = self._read_observability_rows(case_id)
            if (
                len(workflow_rows) > MAX_OBSERVABILITY_EVENT_ROWS
                or len(provider_rows) > MAX_OBSERVABILITY_EVENT_ROWS
            ):
                raise PersistedDataIntegrityError(
                    "Persisted observability history exceeds the V1 bound"
                )
            workflow = tuple(
                SequencedWorkflowEvent(
                    sequence=_require_integer(
                        row["source_audit_sequence"],
                        "workflow sequence",
                    ),
                    envelope=WorkflowEventEnvelope.model_validate_json(
                        _require_string(row["event_json"], "workflow event")
                    ),
                )
                for row in workflow_rows
            )
            validate_workflow_event_order(tuple(item.envelope for item in workflow))
            if any(
                item.sequence != item.envelope.cursor or item.envelope.case_id != case_id
                for item in workflow
            ) or len({item.envelope.event_id for item in workflow}) != len(workflow):
                raise PersistedDataIntegrityError(
                    "Persisted workflow identity does not match its row"
                )

            provider = tuple(self._row_to_provider_usage(row) for row in provider_rows)
            _validate_provider_metric_sequence(provider)
            provider_by_sequence = {item.source_audit_sequence: item for item in provider}
            if len(provider_by_sequence) != len(provider):
                raise PersistedDataIntegrityError(
                    "Persisted provider usage contains duplicate cursors"
                )
            for item in workflow:
                _validate_provider_usage_binding(
                    item.envelope,
                    provider_by_sequence.get(item.sequence),
                )
            workflow_sequences = {item.sequence for item in workflow}
            if any(sequence not in workflow_sequences for sequence in provider_by_sequence):
                raise PersistedDataIntegrityError("Persisted provider usage has no workflow event")

            requests = tuple(item for item in provider if item.status in {"failed", "succeeded"})
            retries = tuple(item for item in provider if item.status == "retry_scheduled")
            usage_reported = tuple(item for item in requests if item.total_tokens is not None)
            costed = tuple(item for item in requests if item.estimated_cost_micros is not None)
            terminal_tools = _completed_tool_metric_events(workflow)
            model_ids = tuple(dict.fromkeys(item.model_id for item in requests))
            estimated_cost = (
                None
                if not costed
                else _bounded_observability_sum(
                    tuple(item.estimated_cost_micros for item in costed),
                    "estimated cost",
                )
            )
            pricing_snapshot_ids = tuple(
                dict.fromkeys(cast(str, item.pricing_snapshot_id) for item in costed)
            )
            return ObservabilityMetricsSnapshot(
                case_id=case_id,
                through_cursor=workflow[-1].sequence if workflow else 0,
                provider_request_count=len(requests),
                provider_request_duration_ms=_bounded_observability_sum(
                    tuple(item.duration_ms for item in requests),
                    "provider request duration",
                ),
                retry_count=len(retries),
                model_ids=model_ids,
                usage_reported_request_count=len(usage_reported),
                input_tokens=_bounded_observability_sum(
                    tuple(item.input_tokens for item in usage_reported),
                    "input tokens",
                ),
                output_tokens=_bounded_observability_sum(
                    tuple(item.output_tokens for item in usage_reported),
                    "output tokens",
                ),
                total_tokens=_bounded_observability_sum(
                    tuple(item.total_tokens for item in usage_reported),
                    "total tokens",
                ),
                costed_request_count=len(costed),
                estimated_cost_micros=estimated_cost,
                currency="USD" if costed else None,
                pricing_snapshot_ids=pricing_snapshot_ids,
                tool_call_count=len(terminal_tools),
                tool_duration_ms=_bounded_observability_sum(
                    tuple(event.duration_ms for event in terminal_tools),
                    "tool duration",
                ),
            )
        except CaseRecordNotFoundError:
            raise
        except Exception as error:
            emit_redacted_log(
                _OBSERVABILITY_LOGGER,
                ObservabilityLogEvent.OBSERVABILITY_METRICS_REJECTED,
                fields={"caseId": case_id},
                error=error,
            )
            raise PersistedDataIntegrityError(
                "Persisted observability metrics are invalid"
            ) from None

    def _read_observability_rows(
        self,
        case_id: str,
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Read both ledgers from one WAL snapshot before deriving metrics."""

        with self._read_connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM cases WHERE case_id = ?",
                    (case_id,),
                ).fetchone()
                is None
            ):
                raise CaseRecordNotFoundError(case_id)
            self._validate_all_receipt_authority(
                connection,
                selected_case_id=case_id,
            )
            self._validate_case_workflow_source_bindings(
                connection,
                case_id=case_id,
            )
            workflow_rows = connection.execute(
                """
                SELECT source_audit_sequence, event_json
                FROM workflow_events
                WHERE case_id = ?
                ORDER BY source_audit_sequence ASC
                LIMIT ?
                """,
                (case_id, MAX_OBSERVABILITY_EVENT_ROWS + 1),
            ).fetchall()
            provider_rows = connection.execute(
                """
                SELECT *
                FROM provider_usage_ledger
                WHERE case_id = ?
                ORDER BY source_audit_sequence ASC
                LIMIT ?
                """,
                (case_id, MAX_OBSERVABILITY_EVENT_ROWS + 1),
            ).fetchall()
        return workflow_rows, provider_rows

    def confirm_transcript_and_transition(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        transcript_id: str,
        transcript_sha256: str,
        updated_at: datetime,
    ) -> TranscriptTransitionResult:
        """Confirm exactly the displayed transcript and enter analyzing once."""

        self._require_canonical_authority_mode()
        self._require_expected_version(
            expected_case_version,
            "Transcript confirmation expected_case_version",
        )
        if _IDENTIFIER.fullmatch(transcript_id) is None:
            raise ValueError("transcript_id is invalid")
        if _SHA256.fullmatch(transcript_sha256) is None:
            raise ValueError("transcript_sha256 is invalid")
        target = CaseState.ANALYZING
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_case_version)
            if current.state is not CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
                raise TranscriptStateError("Case is not awaiting transcript confirmation")
            self._require_passed_g0_g1_history(connection, current)
            self._validate_transcript_snapshot_authority(current)
            _manifest, intake_digest = self._require_intake_authority(
                connection,
                current,
            )
            transcript = self._require_transcript_authority(
                connection,
                current,
                intake_manifest_digest=intake_digest,
            )
            summary = current.snapshot.intake_summary
            assert summary is not None
            try:
                derived_id, derived_ref, derived_hash = _transcript_identity_from_summary(
                    case_id,
                    summary,
                )
            except ValueError as error:
                raise TranscriptStateError(
                    "Transcript confirmation is not bound to a canonical audio intake summary"
                ) from error
            if (
                transcript.transcript_id != derived_id
                or transcript.local_ref != derived_ref
                or transcript.transcript_sha256 != derived_hash
                or transcript_id != derived_id
                or transcript_sha256 != derived_hash
                or transcript.version != 1
                or transcript.bound_case_version != current.version
                or transcript.confirmed
                or updated_at < current.updated_at
            ):
                raise TranscriptStateError("Transcript confirmation is stale or mismatched")
            validate_case_transition(current.state, target)
            event = build_state_change_event(
                case_id=case_id,
                current=current.state,
                target=target,
                actor=ActorType.HUMAN,
                occurred_at=updated_at,
            )
            snapshot = current.snapshot
            _validate_snapshot(case_id, target, snapshot)
            cursor = connection.execute(
                """
                UPDATE case_transcripts
                SET version = version + 1, confirmed = 1, confirmed_at = ?
                WHERE case_id = ? AND version = ? AND confirmed = 0
                """,
                (
                    _dump_aware_datetime(updated_at, "transcript confirmed_at"),
                    case_id,
                    1,
                ),
            )
            if cursor.rowcount != 1:
                raise TranscriptStateError("Transcript was already confirmed")
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=updated_at,
            )
            audit_sequence = self._insert_audit_event(connection, event)
            self._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": event.actor,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            case = self._require_current(connection, case_id, current.version + 1)
            confirmed = self._require_transcript(connection, case_id)
        return TranscriptTransitionResult(case=case, transcript=confirmed)

    def get_transcript(self, case_id: str) -> TranscriptRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM case_transcripts WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return None if row is None else self._row_to_transcript(row)

    def get_transcript_confirmation_view(
        self,
        case_id: str,
    ) -> TranscriptConfirmationView | None:
        """Return only authority-checked transcript content shown to a human."""

        self._require_canonical_authority_mode()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                raise CaseRecordNotFoundError(case_id)
            current = self._row_to_case(row)
            return self._get_transcript_confirmation_view_in_connection(
                connection,
                current,
            )

    def _get_transcript_confirmation_view_in_connection(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> TranscriptConfirmationView | None:
        """Project transcript authority using the caller's established read view."""

        if current.state is not CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
            return None
        from claimdone_api.media import CaseHandle, StoredAssetRef

        manifest, intake_digest = self._require_intake_authority(
            connection,
            current,
        )
        transcript = self._require_transcript_authority(
            connection,
            current,
            intake_manifest_digest=intake_digest,
        )
        summary = current.snapshot.intake_summary
        assert summary is not None
        statement = summary.get("statement")
        if type(statement) is not dict:
            raise TranscriptStateError("Active transcript statement is invalid")
        ref = StoredAssetRef(
            file_id=cast(str, statement.get("fileId")),
            media_type=cast(str, statement.get("mediaType")),
            sha256=cast(str, statement.get("sha256")),
        )
        text = self.__media_store.read_bytes(
            CaseHandle(storage_name=cast(str, manifest["storageName"])),
            ref,
        ).decode("utf-8")
        return TranscriptConfirmationView.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": current.case_id,
                "transcriptId": transcript.transcript_id,
                "transcriptSha256": transcript.transcript_sha256,
                "text": text,
                "version": current.version,
                "confirmed": False,
            }
        )

    def issue_authority_capability(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        digest: bytes,
        role: str,
        purpose: str,
        portal_variant: PortalVariant | None = None,
        issued_at: datetime,
        expires_at: datetime,
    ) -> AuthorityCapabilityRecord:
        """Persist only a 32-byte verifier and revoke older open peers."""

        self._require_expected_version(
            expected_case_version,
            "Capability expected_case_version",
        )
        self._validate_capability_values(
            digest,
            role,
            purpose,
            portal_variant,
            issued_at,
            expires_at,
        )
        issued = _dump_aware_datetime(issued_at, "capability issued_at")
        expires = _dump_aware_datetime(expires_at, "capability expires_at")
        with self._write_connection() as connection:
            bound_case = self._require_current(
                connection,
                case_id,
                expected_case_version,
            )
            if issued_at < bound_case.updated_at:
                raise ValueError("Capability issued_at cannot predate its bound case version")
            for row in connection.execute(
                """
                SELECT * FROM authority_capabilities
                WHERE case_id = ? AND purpose = ?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (case_id, purpose),
            ):
                current = self._row_to_capability(row)
                if issued_at < current.issued_at:
                    raise ValueError("Capability issued_at cannot precede an open capability")
            connection.execute(
                """
                UPDATE authority_capabilities
                SET revoked_at = ?
                WHERE case_id = ? AND purpose = ?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (issued, case_id, purpose),
            )
            connection.execute(
                """
                INSERT INTO authority_capabilities (
                    capability_digest, case_id, role, purpose, bound_case_version,
                    issued_at, expires_at, consumed_at, revoked_at, portal_variant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    digest,
                    case_id,
                    role,
                    purpose,
                    expected_case_version,
                    issued,
                    expires,
                    None if portal_variant is None else portal_variant.value,
                ),
            )
            return self._require_capability(connection, digest)

    def get_authority_capability(
        self,
        digest: bytes,
    ) -> AuthorityCapabilityRecord | None:
        self._validate_digest(digest)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
                (digest,),
            ).fetchone()
        return None if row is None else self._row_to_capability(row)

    def revoke_authority_capability(self, digest: bytes, *, revoked_at: datetime) -> bool:
        self._validate_digest(digest)
        timestamp = _dump_aware_datetime(revoked_at, "capability revoked_at")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return False
            current = self._row_to_capability(row)
            if current.consumed_at is not None or current.revoked_at is not None:
                return False
            if revoked_at < current.issued_at:
                raise ValueError("capability revoked_at cannot precede issued_at")
            cursor = connection.execute(
                """
                UPDATE authority_capabilities
                SET revoked_at = ?
                WHERE capability_digest = ?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (timestamp, digest),
            )
            return cursor.rowcount == 1

    def approve_human_and_create_receipt(
        self,
        command: HumanApprovalCommand,
    ) -> HumanApprovalResult:
        """Consume one human capability and close G9/G10 in one transaction."""

        self._require_canonical_authority_mode()
        self._require_expected_version(
            command.expected_case_version,
            "Approval expected_case_version",
        )
        self._validate_digest(command.capability_digest)
        for identifier, label in (
            (command.approval_id, "approval_id"),
            (command.receipt_id, "receipt_id"),
        ):
            if type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None:
                raise ValueError(f"{label} must be a canonical identifier")
        if not command.approval_id.startswith(f"approval-{command.portal_variant.value.lower()}-"):
            raise ValueError("approval_id must bind the trusted portal variant")
        consumed_at = _parse_datetime(
            _dump_aware_datetime(command.consumed_at, "capability consumed_at")
        )
        approved_at = _parse_datetime(_dump_aware_datetime(command.approved_at, "approved_at"))
        rendered_at = _parse_datetime(_dump_aware_datetime(command.rendered_at, "rendered_at"))
        if not consumed_at < approved_at < rendered_at:
            raise ValueError(
                "Approval timestamps must be strictly ordered: consume, approve, receipt"
            )

        with self._write_connection() as connection:
            capability_row = connection.execute(
                "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
                (command.capability_digest,),
            ).fetchone()
            if capability_row is None:
                raise AuthorityCapabilityError("Approval capability is invalid")
            capability = self._row_to_capability(capability_row)

            # Keep role classification ahead of state, version-lifetime, and expiry
            # details so an agent capability never becomes an authority oracle.
            if capability.role == "agent":
                raise AuthorityCapabilityError("Agent capability is forbidden")
            if capability.role != "human" or capability.purpose != "human_approve":
                raise AuthorityCapabilityError("Approval capability role is invalid")
            if (
                capability.case_id != command.case_id
                or capability.bound_case_version != command.expected_case_version
                or capability.portal_variant is not command.portal_variant
                or capability.consumed_at is not None
                or capability.revoked_at is not None
                or consumed_at <= capability.issued_at
                or consumed_at >= capability.expires_at
            ):
                raise AuthorityCapabilityError("Approval capability is invalid")
            self._preflight_canonical_payloads(
                connection,
                legacy=False,
                verify_media=True,
            )
            current = self._require_current(
                connection,
                command.case_id,
                command.expected_case_version,
            )
            if current.state is not CaseState.REVIEW:
                raise AuthorityCapabilityError("Approval requires the review state")
            if current.snapshot.portal_state is not PortalState.REVIEW:
                raise WorkflowAtomicityError("Review case lost its portal review state")
            run_row = connection.execute(
                "SELECT * FROM portal_run_authority WHERE case_id = ?",
                (current.case_id,),
            ).fetchone()
            if run_row is None:
                raise WorkflowAtomicityError("Review case lost its portal run authority")
            run = self._row_to_portal_run(connection, run_row)
            session_row = connection.execute(
                """
                SELECT * FROM portal_session_authority
                WHERE case_id = ? ORDER BY checkpoint_number DESC LIMIT 1
                """,
                (current.case_id,),
            ).fetchone()
            if session_row is None:
                raise WorkflowAtomicityError("Review case lost its portal session authority")
            portal_session, _rendered = self._portal_session_from_row(session_row)
            if (
                run.status != "review"
                or run.portal_variant is not command.portal_variant
                or portal_session.variant is not command.portal_variant
            ):
                raise AuthorityCapabilityError("Approval portal variant is invalid")
            self._validate_canonical_case_snapshot(current)
            self._require_current_packet_authority(connection, current)
            packet = current.snapshot.claim_packet
            if (
                packet is None
                or packet.state is not CaseState.REVIEW
                or packet.portal_state is not PortalState.REVIEW
                or not packet.verification.review_allowed
                or packet.verification.status is not VerificationState.VERIFIED
                or packet.verification.verified_at is None
                or packet.verification.verified_at > consumed_at
                or tuple(decision.gate_id for decision in packet.gate_decisions)
                != tuple(GateId(f"G{index}") for index in range(9))
                or any(not decision.passed for decision in packet.gate_decisions)
            ):
                raise WorkflowAtomicityError(
                    "Human approval requires the exact passed review authority"
                )
            if self._get_sandbox_receipt_in_connection(connection, current.case_id) is not None:
                raise AuthorityCapabilityError("Approval capability is invalid")

            consumed = connection.execute(
                """
                UPDATE authority_capabilities
                SET consumed_at = ?
                WHERE capability_digest = ?
                  AND role = 'human' AND purpose = 'human_approve'
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (
                    _dump_aware_datetime(consumed_at, "capability consumed_at"),
                    command.capability_digest,
                ),
            )
            if consumed.rowcount != 1:
                raise AuthorityCapabilityError("Approval capability is invalid")

            g9 = make_gate_decision(
                GateId.G9_HUMAN_APPROVAL,
                decided_at=approved_at,
            )
            human_packet_payload = packet.model_dump(mode="json", by_alias=True)
            human_packet_payload["state"] = CaseState.HUMAN_APPROVED.value
            human_packet_payload["portalState"] = PortalState.HUMAN_APPROVED.value
            human_packet_payload["gateDecisions"] = [
                *human_packet_payload["gateDecisions"],
                g9.model_dump(mode="json", by_alias=True),
            ]
            human_packet = ClaimPacket.model_validate(human_packet_payload)
            human_snapshot = replace(
                current.snapshot,
                portal_state=PortalState.HUMAN_APPROVED,
                claim_packet=human_packet,
                active_clarification=None,
            )
            _validate_snapshot(current.case_id, CaseState.HUMAN_APPROVED, human_snapshot)

            g9_sequence = self._insert_authority_gate(
                connection,
                case_id=current.case_id,
                decision=g9,
            )
            self._insert_packet_authority(
                connection,
                case_id=current.case_id,
                bound_case_version=current.version + 1,
                packet=human_packet,
                created_at=approved_at,
            )
            approval_audit = self._authority_audit_event(
                case_id=current.case_id,
                event_type=AuditEventType.HUMAN_APPROVAL,
                actor=ActorType.HUMAN,
                occurred_at=approved_at,
            )
            approval_audit_sequence = self._insert_audit_event(
                connection,
                approval_audit,
            )
            self._update_case_row(
                connection,
                current=current,
                state=CaseState.HUMAN_APPROVED,
                snapshot=human_snapshot,
                updated_at=approved_at,
            )
            approval_state_audit = build_state_change_event(
                case_id=current.case_id,
                current=CaseState.REVIEW,
                target=CaseState.HUMAN_APPROVED,
                actor=ActorType.HUMAN,
                occurred_at=approved_at,
            )
            approval_state_sequence = self._insert_audit_event(
                connection,
                approval_state_audit,
            )
            self._insert_workflow_projection(
                connection,
                audit_sequence=approval_state_sequence,
                audit=approval_state_audit,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.HUMAN,
                        "fromState": CaseState.REVIEW,
                        "toState": CaseState.HUMAN_APPROVED,
                    }
                ),
            )
            human_approved = self._require_current(
                connection,
                current.case_id,
                current.version + 1,
            )

            g10 = make_gate_decision(
                GateId.G10_RECEIPT_REDACTION,
                decided_at=rendered_at,
            )
            g10_sequence = self._insert_authority_gate(
                connection,
                case_id=current.case_id,
                decision=g10,
            )
            receipt = SandboxReceipt.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "receiptId": command.receipt_id,
                    "caseId": current.case_id,
                    "approvalId": command.approval_id,
                    "variant": command.portal_variant,
                    "state": PortalState.RECEIPT,
                    "version": current.version + 2,
                    "environment": "sandbox",
                    "sandboxOnly": True,
                    "submittedToRealInsurer": False,
                    "humanApproved": True,
                    "redacted": True,
                    "summary": {
                        "completedFieldCount": len(packet.claim.field_provenance) - 1,
                        "attachmentCount": len(packet.claim.attachments),
                        "verificationPassed": True,
                        "finalActionOwner": "human",
                    },
                    "approvedAt": approved_at,
                    "renderedAt": rendered_at,
                }
            )
            receipt_snapshot = replace(
                human_approved.snapshot,
                portal_state=PortalState.RECEIPT,
                claim_packet=None,
                active_clarification=None,
            )
            _validate_snapshot(current.case_id, CaseState.RECEIPT, receipt_snapshot)
            receipt_audit = self._authority_audit_event(
                case_id=current.case_id,
                event_type=AuditEventType.RECEIPT,
                actor=ActorType.SYSTEM,
                occurred_at=rendered_at,
            )
            receipt_audit_sequence = self._insert_audit_event(
                connection,
                receipt_audit,
            )
            self._update_case_row(
                connection,
                current=human_approved,
                state=CaseState.RECEIPT,
                snapshot=receipt_snapshot,
                updated_at=rendered_at,
            )
            receipt_state_audit = build_state_change_event(
                case_id=current.case_id,
                current=CaseState.HUMAN_APPROVED,
                target=CaseState.RECEIPT,
                actor=ActorType.SYSTEM,
                occurred_at=rendered_at,
            )
            receipt_state_sequence = self._insert_audit_event(
                connection,
                receipt_state_audit,
            )
            self._insert_workflow_projection(
                connection,
                audit_sequence=receipt_state_sequence,
                audit=receipt_state_audit,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.SYSTEM,
                        "fromState": CaseState.HUMAN_APPROVED,
                        "toState": CaseState.RECEIPT,
                    }
                ),
            )
            receipt_json = receipt.model_dump_json(by_alias=True)
            connection.execute(
                """
                INSERT INTO sandbox_receipts (case_id, receipt_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    current.case_id,
                    receipt_json,
                    _dump_aware_datetime(rendered_at, "receipt created_at"),
                ),
            )
            connection.execute(
                """
                INSERT INTO sandbox_receipt_authority (
                    case_id, authority_version, bound_review_case_version,
                    human_capability_digest, portal_variant, approval_id, receipt_id,
                    receipt_json, receipt_sha256, g9_gate_sequence, g10_gate_sequence,
                    human_approval_audit_sequence,
                    human_approved_state_audit_sequence,
                    receipt_audit_sequence, receipt_state_audit_sequence, created_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current.case_id,
                    current.version,
                    command.capability_digest,
                    command.portal_variant.value,
                    command.approval_id,
                    command.receipt_id,
                    receipt_json,
                    hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                    g9_sequence,
                    g10_sequence,
                    approval_audit_sequence,
                    approval_state_sequence,
                    receipt_audit_sequence,
                    receipt_state_sequence,
                    _dump_aware_datetime(rendered_at, "receipt authority created_at"),
                ),
            )
            final_case = self._require_current(
                connection,
                current.case_id,
                current.version + 2,
            )
            receipt_record = self._get_sandbox_receipt_in_connection(
                connection,
                current.case_id,
            )
            if receipt_record is None:
                raise WorkflowAtomicityError("Atomic approval lost its receipt")
            return HumanApprovalResult(case=final_case, receipt=receipt_record)

    def _insert_authority_gate(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: str,
        decision: GateDecision,
    ) -> int:
        gate_sequence = self._insert_gate_decision_row(
            connection,
            case_id=case_id,
            decision=decision,
        )
        audit = build_gate_audit_event(
            case_id=case_id,
            decision=decision,
            actor=ActorType.SYSTEM,
        )
        audit_sequence = self._insert_audit_event(connection, audit)
        self._insert_workflow_projection(
            connection,
            audit_sequence=audit_sequence,
            audit=audit,
            event=GateWorkflowEvent.model_validate(
                {
                    "kind": WorkflowEventKind.GATE,
                    "decision": decision,
                }
            ),
        )
        return gate_sequence

    @staticmethod
    def _authority_audit_event(
        *,
        case_id: str,
        event_type: AuditEventType,
        actor: ActorType,
        occurred_at: datetime,
    ) -> AuditEvent:
        return AuditEvent.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "eventId": f"event_{uuid4().hex}",
                "caseId": case_id,
                "eventType": event_type,
                "actor": actor,
                "occurredAt": occurred_at,
                "fromState": None,
                "toState": None,
                "reasonCodes": (),
                "details": (),
            }
        )

    def get_sandbox_receipt(self, case_id: str) -> SandboxReceiptRecord | None:
        """Read only; AUTH owns the later atomic receipt insertion path."""

        with self._read_connection() as connection:
            return self._get_sandbox_receipt_in_connection(connection, case_id)

    def _get_sandbox_receipt_in_connection(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> SandboxReceiptRecord | None:
        if not self._table_exists(connection, "sandbox_receipt_authority"):
            raise PersistedDataIntegrityError("Persisted receipt authority is missing")
        try:
            records = self._validate_all_receipt_authority(connection)
        except PersistedDataIntegrityError:
            raise
        except (ValidationError, ValueError, TypeError, KeyError, IndexError) as error:
            raise PersistedDataIntegrityError(
                "Persisted sandbox receipt authority is invalid"
            ) from error
        return records.get(case_id)

    def _validate_all_receipt_authority(
        self,
        connection: sqlite3.Connection,
        *,
        selected_case_id: str | None = None,
    ) -> dict[str, SandboxReceiptRecord]:
        """Require one exact immutable authority projection for every final receipt."""

        receipt_query = "SELECT case_id FROM sandbox_receipts"
        authority_query = "SELECT * FROM sandbox_receipt_authority"
        final_case_query = "SELECT case_id FROM cases WHERE state = ?"
        intermediate_query = "SELECT 1 FROM cases WHERE state = ? LIMIT 1"
        scope_parameters: tuple[str, ...] = ()
        if selected_case_id is not None:
            receipt_query += " WHERE case_id = ?"
            authority_query += " WHERE case_id = ?"
            final_case_query += " AND case_id = ?"
            intermediate_query = "SELECT 1 FROM cases WHERE state = ? AND case_id = ? LIMIT 1"
            scope_parameters = (selected_case_id,)
        authority_query += " ORDER BY case_id"
        receipt_case_ids = {
            _require_string(row["case_id"], "receipt case id")
            for row in connection.execute(receipt_query, scope_parameters)
        }
        authority_rows = connection.execute(
            authority_query,
            scope_parameters,
        ).fetchall()
        authority_case_ids = {
            _require_string(row["case_id"], "receipt authority case id") for row in authority_rows
        }
        final_case_ids = {
            _require_string(row["case_id"], "final receipt case id")
            for row in connection.execute(
                final_case_query,
                (CaseState.RECEIPT.value, *scope_parameters),
            )
        }
        has_intermediate_final = connection.execute(
            intermediate_query,
            (CaseState.HUMAN_APPROVED.value, *scope_parameters),
        ).fetchone()
        if (
            has_intermediate_final is not None
            or receipt_case_ids != authority_case_ids
            or receipt_case_ids != final_case_ids
        ):
            raise PersistedDataIntegrityError("Persisted final cases lost their receipt authority")

        expected_gate_rows: set[tuple[int, str, str]] = set()
        expected_human_audits: set[tuple[int, str]] = set()
        expected_receipt_audits: set[tuple[int, str]] = set()
        expected_approval_states: set[tuple[int, str]] = set()
        expected_receipt_states: set[tuple[int, str]] = set()
        expected_consumptions: set[tuple[bytes, str]] = set()
        records: dict[str, SandboxReceiptRecord] = {}
        for authority_row in authority_rows:
            authority_case_id = _require_string(
                authority_row["case_id"],
                "receipt authority case id",
            )
            record = self._validate_receipt_authority_row(
                connection,
                authority_row=authority_row,
            )
            records[authority_case_id] = record
            g9_sequence = _require_integer(
                authority_row["g9_gate_sequence"],
                "G9 authority sequence",
            )
            g10_sequence = _require_integer(
                authority_row["g10_gate_sequence"],
                "G10 authority sequence",
            )
            expected_gate_rows.update(
                {
                    (g9_sequence, authority_case_id, GateId.G9_HUMAN_APPROVAL.value),
                    (
                        g10_sequence,
                        authority_case_id,
                        GateId.G10_RECEIPT_REDACTION.value,
                    ),
                }
            )
            expected_human_audits.add(
                (
                    _require_integer(
                        authority_row["human_approval_audit_sequence"],
                        "human approval audit sequence",
                    ),
                    authority_case_id,
                )
            )
            expected_approval_states.add(
                (
                    _require_integer(
                        authority_row["human_approved_state_audit_sequence"],
                        "human-approved state audit sequence",
                    ),
                    authority_case_id,
                )
            )
            expected_receipt_audits.add(
                (
                    _require_integer(
                        authority_row["receipt_audit_sequence"],
                        "receipt audit sequence",
                    ),
                    authority_case_id,
                )
            )
            expected_receipt_states.add(
                (
                    _require_integer(
                        authority_row["receipt_state_audit_sequence"],
                        "receipt state audit sequence",
                    ),
                    authority_case_id,
                )
            )
            digest = authority_row["human_capability_digest"]
            self._validate_digest(digest)
            expected_consumptions.add((digest, authority_case_id))

        gate_query = "SELECT sequence, case_id, gate_id FROM gate_decisions WHERE gate_id IN (?, ?)"
        gate_parameters: tuple[str, ...] = (
            GateId.G9_HUMAN_APPROVAL.value,
            GateId.G10_RECEIPT_REDACTION.value,
        )
        if selected_case_id is not None:
            gate_query += " AND case_id = ?"
            gate_parameters = (*gate_parameters, selected_case_id)
        actual_gate_rows = {
            (
                _require_integer(row["sequence"], "final gate sequence"),
                _require_string(row["case_id"], "final gate case id"),
                _require_string(row["gate_id"], "final gate id"),
            )
            for row in connection.execute(gate_query, gate_parameters)
        }
        actual_human_audits = self._audit_sequence_case_set(
            connection,
            event_type=AuditEventType.HUMAN_APPROVAL,
            case_id=selected_case_id,
        )
        actual_receipt_audits = self._audit_sequence_case_set(
            connection,
            event_type=AuditEventType.RECEIPT,
            case_id=selected_case_id,
        )
        actual_approval_states = self._state_audit_sequence_case_set(
            connection,
            from_state=CaseState.REVIEW,
            to_state=CaseState.HUMAN_APPROVED,
            case_id=selected_case_id,
        )
        actual_receipt_states = self._state_audit_sequence_case_set(
            connection,
            from_state=CaseState.HUMAN_APPROVED,
            to_state=CaseState.RECEIPT,
            case_id=selected_case_id,
        )
        consumption_query = """
            SELECT capability_digest, case_id
            FROM authority_capabilities
            WHERE role = 'human' AND purpose = 'human_approve'
              AND consumed_at IS NOT NULL
        """
        consumption_parameters: tuple[str, ...] = ()
        if selected_case_id is not None:
            consumption_query += " AND case_id = ?"
            consumption_parameters = (selected_case_id,)
        actual_consumptions = {
            (
                bytes(row["capability_digest"]),
                _require_string(row["case_id"], "consumed capability case id"),
            )
            for row in connection.execute(
                consumption_query,
                consumption_parameters,
            )
        }
        if (
            actual_gate_rows != expected_gate_rows
            or actual_human_audits != expected_human_audits
            or actual_receipt_audits != expected_receipt_audits
            or actual_approval_states != expected_approval_states
            or actual_receipt_states != expected_receipt_states
            or actual_consumptions != expected_consumptions
        ):
            raise PersistedDataIntegrityError(
                "Persisted receipt boundary contains missing or additional authority rows"
            )
        return records

    def _validate_receipt_authority_row(
        self,
        connection: sqlite3.Connection,
        *,
        authority_row: sqlite3.Row,
    ) -> SandboxReceiptRecord:
        case_id = _require_string(authority_row["case_id"], "receipt authority case id")
        if _require_integer(authority_row["authority_version"], "receipt authority version") != 1:
            raise PersistedDataIntegrityError("Persisted receipt authority version is invalid")
        receipt_row = connection.execute(
            "SELECT receipt_json, created_at FROM sandbox_receipts WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        case_row = connection.execute(
            "SELECT * FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if receipt_row is None or case_row is None:
            raise PersistedDataIntegrityError("Persisted receipt authority lost its parent")
        try:
            receipt_json = _require_string(receipt_row["receipt_json"], "sandbox receipt")
            authority_json = _require_string(
                authority_row["receipt_json"],
                "receipt authority JSON",
            )
            receipt = SandboxReceipt.model_validate_json(receipt_json)
            authority_receipt = SandboxReceipt.model_validate_json(authority_json)
            created_at = _parse_datetime(
                _require_string(receipt_row["created_at"], "receipt created_at")
            )
            authority_created_at = _parse_datetime(
                _require_string(authority_row["created_at"], "receipt authority created_at")
            )
            current = self._row_to_case(case_row)
            digest = authority_row["human_capability_digest"]
            self._validate_digest(digest)
            capability = self._require_capability(connection, digest)
            variant = PortalVariant(
                _require_string(authority_row["portal_variant"], "receipt portal variant")
            )
            bound_review_version = _require_integer(
                authority_row["bound_review_case_version"],
                "receipt bound review version",
            )
            receipt_digest = _require_string(
                authority_row["receipt_sha256"],
                "receipt authority SHA-256",
            )
            if (
                receipt_json != authority_json
                or receipt != authority_receipt
                or receipt_digest != hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
                or receipt.case_id != case_id
                or receipt.variant is not variant
                or receipt.approval_id
                != _require_string(authority_row["approval_id"], "authority approval id")
                or receipt.receipt_id
                != _require_string(authority_row["receipt_id"], "authority receipt id")
                or not receipt.approval_id.startswith(f"approval-{variant.value.lower()}-")
                or current.state is not CaseState.RECEIPT
                or current.snapshot.portal_state is not PortalState.RECEIPT
                or receipt.version != current.version
                or bound_review_version != current.version - 2
                or current.updated_at != receipt.rendered_at
                or created_at != receipt.rendered_at
                or authority_created_at != receipt.rendered_at
                or capability.case_id != case_id
                or capability.role != "human"
                or capability.purpose != "human_approve"
                or capability.portal_variant is not variant
                or capability.bound_case_version != bound_review_version
                or capability.consumed_at is None
                or capability.revoked_at is not None
                or not (
                    capability.issued_at
                    < capability.consumed_at
                    < receipt.approved_at
                    < receipt.rendered_at
                )
                or capability.consumed_at >= capability.expires_at
            ):
                raise ValueError("Receipt authority binding is inconsistent")

            g9 = self._require_receipt_gate(
                connection,
                sequence=_require_integer(
                    authority_row["g9_gate_sequence"],
                    "G9 authority sequence",
                ),
                case_id=case_id,
                gate_id=GateId.G9_HUMAN_APPROVAL,
                occurred_at=receipt.approved_at,
            )
            g10 = self._require_receipt_gate(
                connection,
                sequence=_require_integer(
                    authority_row["g10_gate_sequence"],
                    "G10 authority sequence",
                ),
                case_id=case_id,
                gate_id=GateId.G10_RECEIPT_REDACTION,
                occurred_at=receipt.rendered_at,
            )
            self._require_gate_workflow_authority(
                connection,
                case_id=case_id,
                expected=(g9, g10),
            )
            approval_audit = self._require_receipt_audit(
                connection,
                sequence=_require_integer(
                    authority_row["human_approval_audit_sequence"],
                    "human approval audit sequence",
                ),
                case_id=case_id,
                event_type=AuditEventType.HUMAN_APPROVAL,
                actor=ActorType.HUMAN,
                occurred_at=receipt.approved_at,
                from_state=None,
                to_state=None,
            )
            approval_state = self._require_receipt_audit(
                connection,
                sequence=_require_integer(
                    authority_row["human_approved_state_audit_sequence"],
                    "human-approved state audit sequence",
                ),
                case_id=case_id,
                event_type=AuditEventType.CASE_STATE_CHANGED,
                actor=ActorType.HUMAN,
                occurred_at=receipt.approved_at,
                from_state=CaseState.REVIEW,
                to_state=CaseState.HUMAN_APPROVED,
            )
            receipt_audit = self._require_receipt_audit(
                connection,
                sequence=_require_integer(
                    authority_row["receipt_audit_sequence"],
                    "receipt audit sequence",
                ),
                case_id=case_id,
                event_type=AuditEventType.RECEIPT,
                actor=ActorType.SYSTEM,
                occurred_at=receipt.rendered_at,
                from_state=None,
                to_state=None,
            )
            receipt_state = self._require_receipt_audit(
                connection,
                sequence=_require_integer(
                    authority_row["receipt_state_audit_sequence"],
                    "receipt state audit sequence",
                ),
                case_id=case_id,
                event_type=AuditEventType.CASE_STATE_CHANGED,
                actor=ActorType.SYSTEM,
                occurred_at=receipt.rendered_at,
                from_state=CaseState.HUMAN_APPROVED,
                to_state=CaseState.RECEIPT,
            )
            self._require_state_workflow_authority(
                connection,
                sequence=_require_integer(
                    authority_row["human_approved_state_audit_sequence"],
                    "human-approved state audit sequence",
                ),
                audit=approval_state,
            )
            self._require_state_workflow_authority(
                connection,
                sequence=_require_integer(
                    authority_row["receipt_state_audit_sequence"],
                    "receipt state audit sequence",
                ),
                audit=receipt_state,
            )
            del approval_audit, receipt_audit
        except (ValidationError, ValueError, TypeError) as error:
            raise PersistedDataIntegrityError(
                "Persisted sandbox receipt authority is invalid"
            ) from error
        return SandboxReceiptRecord(receipt=receipt, created_at=created_at)

    @staticmethod
    def _audit_sequence_case_set(
        connection: sqlite3.Connection,
        *,
        event_type: AuditEventType,
        case_id: str | None = None,
    ) -> set[tuple[int, str]]:
        query = """
            SELECT sequence, case_id FROM audit_events
            WHERE json_extract(event_json, '$.eventType') = ?
        """
        parameters: tuple[str, ...] = (event_type.value,)
        if case_id is not None:
            query += " AND case_id = ?"
            parameters = (event_type.value, case_id)
        return {
            (
                _require_integer(row["sequence"], "authority audit sequence"),
                _require_string(row["case_id"], "authority audit case id"),
            )
            for row in connection.execute(query, parameters)
        }

    @staticmethod
    def _state_audit_sequence_case_set(
        connection: sqlite3.Connection,
        *,
        from_state: CaseState,
        to_state: CaseState,
        case_id: str | None = None,
    ) -> set[tuple[int, str]]:
        query = """
            SELECT sequence, case_id FROM audit_events
            WHERE json_extract(event_json, '$.eventType') = ?
              AND json_extract(event_json, '$.fromState') = ?
              AND json_extract(event_json, '$.toState') = ?
        """
        parameters: tuple[str, ...] = (
            AuditEventType.CASE_STATE_CHANGED.value,
            from_state.value,
            to_state.value,
        )
        if case_id is not None:
            query += " AND case_id = ?"
            parameters = (*parameters, case_id)
        return {
            (
                _require_integer(row["sequence"], "state authority sequence"),
                _require_string(row["case_id"], "state authority case id"),
            )
            for row in connection.execute(query, parameters)
        }

    @staticmethod
    def _require_receipt_gate(
        connection: sqlite3.Connection,
        *,
        sequence: int,
        case_id: str,
        gate_id: GateId,
        occurred_at: datetime,
    ) -> GateDecision:
        row = connection.execute(
            "SELECT * FROM gate_decisions WHERE sequence = ?",
            (sequence,),
        ).fetchone()
        if row is None:
            raise PersistedDataIntegrityError("Receipt authority gate is missing")
        decision = GateDecision.model_validate_json(
            _require_string(row["decision_json"], "receipt authority gate")
        )
        if (
            _require_string(row["case_id"], "receipt gate case id") != case_id
            or _require_string(row["gate_id"], "receipt gate id") != gate_id.value
            or _parse_datetime(_require_string(row["decided_at"], "receipt gate decided_at"))
            != occurred_at
            or decision.gate_id is not gate_id
            or decision.decided_at != occurred_at
            or not decision.deterministic_passed
            or decision.model_blocked
            or not decision.passed
            or decision.reason_codes
            or decision.evidence_refs
        ):
            raise PersistedDataIntegrityError("Receipt authority gate is invalid")
        return decision

    @staticmethod
    def _require_receipt_audit(
        connection: sqlite3.Connection,
        *,
        sequence: int,
        case_id: str,
        event_type: AuditEventType,
        actor: ActorType,
        occurred_at: datetime,
        from_state: CaseState | None,
        to_state: CaseState | None,
    ) -> AuditEvent:
        row = connection.execute(
            "SELECT * FROM audit_events WHERE sequence = ?",
            (sequence,),
        ).fetchone()
        if row is None:
            raise PersistedDataIntegrityError("Receipt authority audit is missing")
        audit = AuditEvent.model_validate_json(
            _require_string(row["event_json"], "receipt authority audit")
        )
        if (
            audit.event_id != _require_string(row["event_id"], "receipt audit id")
            or audit.case_id != _require_string(row["case_id"], "receipt audit case id")
            or audit.occurred_at
            != _parse_datetime(_require_string(row["occurred_at"], "receipt audit occurred_at"))
            or audit.case_id != case_id
            or audit.event_type is not event_type
            or audit.actor is not actor
            or audit.occurred_at != occurred_at
            or audit.from_state is not from_state
            or audit.to_state is not to_state
            or audit.reason_codes
            or audit.details
        ):
            raise PersistedDataIntegrityError("Receipt authority audit is invalid")
        return audit

    @staticmethod
    def _require_state_workflow_authority(
        connection: sqlite3.Connection,
        *,
        sequence: int,
        audit: AuditEvent,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM workflow_events WHERE source_audit_sequence = ?",
            (sequence,),
        ).fetchone()
        if row is None:
            raise PersistedDataIntegrityError("Receipt state projection is missing")
        envelope = SqliteCaseRepository._workflow_envelope_from_row(
            row,
            label="receipt state projection",
        )
        _validate_audit_projection_binding(audit, envelope)
        event = envelope.event
        if (
            not isinstance(event, StateWorkflowEvent)
            or event.actor is not audit.actor
            or event.from_state is not audit.from_state
            or event.to_state is not audit.to_state
        ):
            raise PersistedDataIntegrityError("Receipt state projection is invalid")

    @staticmethod
    def _require_gate_workflow_authority(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        expected: tuple[GateDecision, GateDecision],
    ) -> None:
        actual: list[GateDecision] = []
        for row in connection.execute(
            """
            SELECT * FROM workflow_events
            WHERE case_id = ? AND event_kind = ?
            ORDER BY source_audit_sequence
            """,
            (case_id, WorkflowEventKind.GATE.value),
        ):
            envelope = SqliteCaseRepository._workflow_envelope_from_row(
                row,
                label="receipt gate projection",
            )
            if isinstance(
                envelope.event, GateWorkflowEvent
            ) and envelope.event.decision.gate_id in {
                GateId.G9_HUMAN_APPROVAL,
                GateId.G10_RECEIPT_REDACTION,
            }:
                source_audit = SqliteCaseRepository._require_receipt_audit(
                    connection,
                    sequence=envelope.source_audit_sequence,
                    case_id=case_id,
                    event_type=AuditEventType.GATE_DECISION,
                    actor=ActorType.SYSTEM,
                    occurred_at=envelope.event.decision.decided_at,
                    from_state=None,
                    to_state=None,
                )
                _validate_audit_projection_binding(source_audit, envelope)
                actual.append(envelope.event.decision)
        if tuple(actual) != expected:
            raise PersistedDataIntegrityError("Receipt gate projections are invalid")

    @staticmethod
    def _workflow_envelope_from_row(
        row: sqlite3.Row,
        *,
        label: str,
    ) -> WorkflowEventEnvelope:
        envelope = WorkflowEventEnvelope.model_validate_json(
            _require_string(row["event_json"], label)
        )
        source_sequence = _require_integer(
            row["source_audit_sequence"],
            f"{label} source sequence",
        )
        if (
            envelope.source_audit_sequence != source_sequence
            or envelope.cursor != source_sequence
            or envelope.source_audit_event_id
            != _require_string(row["source_audit_event_id"], f"{label} source id")
            or envelope.source_audit_event_type.value
            != _require_string(row["source_audit_event_type"], f"{label} source type")
            or envelope.case_id != _require_string(row["case_id"], f"{label} case id")
            or envelope.event_id != _require_string(row["event_id"], f"{label} event id")
            or envelope.event.kind.value
            != _require_string(row["event_kind"], f"{label} event kind")
        ):
            raise PersistedDataIntegrityError(f"Persisted {label} columns are invalid")
        return envelope

    def delete_case(self, case_id: str) -> bool:
        if self.is_canonical_authority:
            raise AuthorityModeMismatchError(
                "Canonical deletion requires delete_case_and_resources"
            )
        with self._write_connection() as connection:
            cursor = connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
            return cursor.rowcount > 0

    def delete_case_and_resources(self, case_id: str) -> bool:
        """Serialize canonical deletion with intake and remove the exact owned handle."""

        self._require_canonical_authority_mode()
        with self._write_connection() as connection:
            return self._delete_case_and_resources_in_connection(connection, case_id)

    def _delete_case_and_resources_in_connection(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> bool:
        case_row = connection.execute(
            "SELECT 1 FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case_row is None:
            return False
        handle_row = connection.execute(
            "SELECT storage_name FROM case_media_handles WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if handle_row is not None:
            from claimdone_api.media import CaseHandle

            storage_name = _require_string(
                handle_row["storage_name"],
                "media storage name",
            )
            if _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None:
                raise PersistedDataIntegrityError("Persisted media handle is invalid")
            self.__media_store.delete_case(CaseHandle(storage_name=storage_name))
        cursor = connection.execute(
            "DELETE FROM cases WHERE case_id = ?",
            (case_id,),
        )
        if cursor.rowcount != 1:
            raise PersistenceError("Reserved case deletion lost its database row")
        return True

    def reset_cases(self) -> int:
        """Delete cases without resetting AUTOINCREMENT history cursors."""

        if self.is_canonical_authority:
            raise AuthorityModeMismatchError("Canonical reset cannot bypass exact media cleanup")
        with self._write_connection() as connection:
            count_row = connection.execute("SELECT COUNT(*) FROM cases").fetchone()
            count = int(count_row[0]) if count_row is not None else 0
            connection.execute("DELETE FROM cases")
            return count

    def _require_case(self, case_id: str) -> CaseRecord:
        record = self.get_case(case_id)
        if record is None:
            raise CaseRecordNotFoundError(case_id)
        return record

    def _require_current(
        self,
        connection: sqlite3.Connection,
        case_id: str,
        expected_version: int,
    ) -> CaseRecord:
        self._require_expected_version(expected_version, "expected_version")
        row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            raise CaseRecordNotFoundError(case_id)
        current = self._row_to_case(row)
        if current.version != expected_version:
            raise CaseRecordVersionConflictError(case_id, expected_version, current.version)
        return current

    @staticmethod
    def _require_expected_version(value: object, label: str) -> int:
        if type(value) is not int:
            raise TypeError(f"{label} must be an exact positive SQLite int64 integer")
        exact = value
        if exact < 1 or exact > SQLITE_MAX_INTEGER:
            raise TypeError(f"{label} must be an exact positive SQLite int64 integer")
        return exact

    @staticmethod
    def _validate_canonical_case_snapshot(current: CaseRecord) -> None:
        """Bind every persisted snapshot payload to its canonical case state/version."""

        snapshot = current.snapshot
        if current.state is CaseState.CREATED:
            if snapshot.intake_summary is not None:
                raise WorkflowAtomicityError(
                    "A created canonical case cannot expose persisted intake"
                )
        elif snapshot.intake_summary is None:
            raise WorkflowAtomicityError("A non-created canonical case requires persisted intake")

        if current.state in _CANONICAL_PACKET_REQUIRED_STATES and snapshot.claim_packet is None:
            raise WorkflowAtomicityError(f"{current.state.value} requires a canonical ClaimPacket")
        if (
            current.state in _CANONICAL_PACKET_FORBIDDEN_STATES
            and snapshot.claim_packet is not None
        ):
            raise WorkflowAtomicityError(f"{current.state.value} cannot retain a ClaimPacket")

        active_payload = snapshot.active_clarification
        if current.state is not CaseState.AWAITING_CLARIFICATION:
            if active_payload is not None:
                raise WorkflowAtomicityError(
                    "Only awaiting_clarification may retain an active clarification"
                )
            return
        if active_payload is None:
            raise WorkflowAtomicityError("awaiting_clarification requires an active clarification")
        try:
            active = ClarificationView.model_validate(active_payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowAtomicityError(
                "Persisted active clarification is not canonical"
            ) from error
        if (
            active.case_id != current.case_id
            or active.expected_version != current.version
            or active.requested_at != current.updated_at
            or not current.created_at <= active.requested_at <= current.updated_at
        ):
            raise WorkflowAtomicityError(
                "Active clarification is not bound to the current case version"
            )

    def _validate_canonical_case_replay(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
        workflows: tuple[WorkflowEventEnvelope, ...],
        *,
        audits_by_sequence: dict[int, AuditEvent],
    ) -> dict[int, datetime]:
        """Derive every authorized case mutation and authority boundary from history."""

        replayed_state = CaseState.CREATED
        replayed_version = 1
        version_origins = {1: current.created_at}
        intake_mode = self._replay_intake_mode(connection, current)
        last_event_at = current.created_at
        last_mutation_at = current.created_at
        pending_clarification: ClarificationWorkflowEvent | None = None
        pending_clarification_close: ClarificationWorkflowEvent | None = None
        pending_requested_at: datetime | None = None
        last_clarification_round = 0
        clarification_closed = False
        pending_plan: list[PlanStepWorkflowEvent] = []
        pending_plan_at: datetime | None = None
        pending_clarification_at: datetime | None = None
        pending_provider: list[
            tuple[
                ProviderCallWorkflowEvent | RetryWorkflowEvent | OperationalFailureWorkflowEvent,
                datetime,
            ]
        ] = []
        pending_intake_gates: list[GateDecision] = []
        boundary_stage = 0
        expected_packet_authorities: list[_ExpectedPacketAuthority] = []
        last_plan_events: tuple[tuple[int, AllowedTool], ...] | None = None
        last_safe_plan: tuple[tuple[AllowedTool, str], ...] | None = None
        analysis_targets = {
            CaseState.AWAITING_CLARIFICATION,
            CaseState.READY_TO_FILL,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
        }

        for index, envelope in enumerate(workflows):
            occurred_at = envelope.occurred_at
            if (
                occurred_at < current.created_at
                or occurred_at > current.updated_at
                or occurred_at < last_event_at
                or occurred_at < last_mutation_at
            ):
                raise WorkflowAtomicityError(
                    "Canonical workflow chronology falls outside its case version"
                )
            last_event_at = occurred_at
            event = envelope.event
            audit = audits_by_sequence.get(envelope.source_audit_sequence)
            if audit is None:
                raise WorkflowAtomicityError("Workflow replay lost its source audit authority")
            self._validate_replay_actor(event, audit.actor)
            if isinstance(event, GateWorkflowEvent):
                gate_states = {
                    GateId.G0_INTAKE: frozenset({CaseState.CREATED}),
                    GateId.G1_PRIVACY: frozenset({CaseState.CREATED}),
                    GateId.G2_OUTPUT_CONTRACT: frozenset(
                        {CaseState.ANALYZING, CaseState.AWAITING_CLARIFICATION}
                    ),
                    GateId.G3_SAFETY_SCOPE: frozenset(
                        {CaseState.ANALYZING, CaseState.AWAITING_CLARIFICATION}
                    ),
                    GateId.G4_PROVENANCE: frozenset(
                        {CaseState.ANALYZING, CaseState.AWAITING_CLARIFICATION}
                    ),
                    GateId.G5_COMPLETENESS: frozenset(
                        {CaseState.ANALYZING, CaseState.AWAITING_CLARIFICATION}
                    ),
                    GateId.G6_TOOL_AUTHORITY: frozenset({CaseState.READY_TO_FILL}),
                    GateId.G7_PORTAL_WRITE: frozenset({CaseState.FILLING}),
                    GateId.G8_VERIFICATION: frozenset({CaseState.VERIFYING}),
                    GateId.G9_HUMAN_APPROVAL: frozenset({CaseState.REVIEW}),
                    GateId.G10_RECEIPT_REDACTION: frozenset({CaseState.HUMAN_APPROVED}),
                }
                allowed_state = replayed_state in gate_states[event.decision.gate_id]
                if not allowed_state:
                    raise WorkflowAtomicityError(
                        "Persisted gate was not authorized by the replayed case state"
                    )
                if boundary_stage > 1:
                    raise WorkflowAtomicityError(
                        "Gate event appears after its atomic boundary stage"
                    )
                boundary_stage = 1
                if replayed_state is CaseState.CREATED:
                    pending_intake_gates.append(event.decision)
                continue

            if isinstance(
                event,
                ProviderCallWorkflowEvent | RetryWorkflowEvent | OperationalFailureWorkflowEvent,
            ):
                required_state = {
                    WorkflowOperation.TRANSCRIPTION: CaseState.DISCLOSED,
                    WorkflowOperation.EXTRACTION: CaseState.ANALYZING,
                    WorkflowOperation.COMPUTER_USE: CaseState.FILLING,
                    WorkflowOperation.VERIFICATION: CaseState.VERIFYING,
                }[event.operation]
                if replayed_state is not required_state:
                    raise WorkflowAtomicityError(
                        "Provider telemetry was not authorized by the replayed case state"
                    )
                if boundary_stage != 0:
                    raise WorkflowAtomicityError(
                        "Provider telemetry appears after gates or plan events"
                    )
                pending_provider.append((event, occurred_at))
                if isinstance(event, OperationalFailureWorkflowEvent):
                    next_envelope = None if index + 1 == len(workflows) else workflows[index + 1]
                    if (
                        next_envelope is None
                        or next_envelope.occurred_at != occurred_at
                        or not isinstance(next_envelope.event, StateWorkflowEvent)
                        or next_envelope.event.from_state is not replayed_state
                        or next_envelope.event.to_state is not CaseState.FAILED
                    ):
                        raise WorkflowAtomicityError(
                            "Operational failure must be immediately closed by FAILED"
                        )
                    boundary_stage = 4
                continue

            if isinstance(event, ToolCallWorkflowEvent):
                if (
                    replayed_state is not CaseState.FILLING
                    or boundary_stage != 1
                    or event.status is ToolCallStatus.STARTED
                ):
                    raise WorkflowAtomicityError("Tool event has no terminal G7 writer boundary")
                boundary_stage = 2
                continue

            if isinstance(event, PortalFillWorkflowEvent):
                if replayed_state is not CaseState.FILLING or boundary_stage != 2:
                    raise WorkflowAtomicityError("Portal fill has no successful G7 writer boundary")
                boundary_stage = 3
                continue

            if isinstance(event, VerificationWorkflowEvent):
                if replayed_state is not CaseState.VERIFYING:
                    raise WorkflowAtomicityError("Verification event has no VERIFYING authority")
                if event.final:
                    if boundary_stage != 1:
                        raise WorkflowAtomicityError("Final verification requires its G8 gate")
                    boundary_stage = 2
                    continue
                if (
                    boundary_stage != 0
                    or event.attempt_number != 1
                    or last_plan_events is None
                    or last_safe_plan is None
                ):
                    raise WorkflowAtomicityError(
                        "Repairable verification is not one closed attempt"
                    )
                replayed_version += 1
                version_origins[replayed_version] = occurred_at
                last_mutation_at = occurred_at
                expected_packet_authorities.append(
                    _ExpectedPacketAuthority(
                        bound_version=replayed_version,
                        created_at=occurred_at,
                        state=CaseState.VERIFYING,
                        plan_events=last_plan_events,
                        safe_plan=last_safe_plan,
                    )
                )
                continue

            if isinstance(event, PlanStepWorkflowEvent):
                if replayed_state not in {
                    CaseState.ANALYZING,
                    CaseState.AWAITING_CLARIFICATION,
                }:
                    raise WorkflowAtomicityError(
                        "Plan event was not authorized by the replayed case state"
                    )
                if boundary_stage not in {1, 2}:
                    raise WorkflowAtomicityError("Plan events require the completed gate stage")
                if event.sequence != len(pending_plan) + 1 or (
                    pending_plan_at is not None and occurred_at != pending_plan_at
                ):
                    raise WorkflowAtomicityError("Plan events are not one exact atomic sequence")
                pending_plan.append(event)
                pending_plan_at = occurred_at
                boundary_stage = 2
                continue

            if isinstance(event, ClarificationWorkflowEvent):
                if replayed_state not in {
                    CaseState.ANALYZING,
                    CaseState.AWAITING_CLARIFICATION,
                }:
                    raise WorkflowAtomicityError(
                        "Clarification event was not authorized by the replayed case state"
                    )
                if boundary_stage not in {2, 3}:
                    raise WorkflowAtomicityError(
                        "Clarification events require the completed plan stage"
                    )
                if pending_clarification_at is not None and occurred_at != pending_clarification_at:
                    raise WorkflowAtomicityError(
                        "Clarification lifecycle events are not one atomic timestamp"
                    )
                pending_clarification_at = occurred_at
                boundary_stage = 3
                if clarification_closed:
                    raise WorkflowAtomicityError(
                        "Clarification lifecycle continued after it was closed"
                    )
                if event.status is ClarificationStatus.REQUESTED:
                    if (
                        pending_clarification is not None
                        or event.round != last_clarification_round + 1
                    ):
                        raise WorkflowAtomicityError("Clarification requests are not contiguous")
                    pending_clarification = event
                    pending_requested_at = occurred_at
                    last_clarification_round = event.round
                    next_event = None if index + 1 == len(workflows) else workflows[index + 1].event
                    initial_transition = (
                        isinstance(next_event, StateWorkflowEvent)
                        and next_event.from_state is replayed_state
                        and next_event.to_state is CaseState.AWAITING_CLARIFICATION
                    )
                    if not initial_transition:
                        if replayed_state is not CaseState.AWAITING_CLARIFICATION:
                            raise WorkflowAtomicityError(
                                "Initial clarification request requires its state transition"
                            )
                        self._validate_provider_replay_batch(
                            replayed_state,
                            replayed_state,
                            tuple(pending_provider),
                            mutation_at=occurred_at,
                        )
                        if (
                            pending_plan_at != occurred_at
                            or pending_clarification_at != occurred_at
                        ):
                            raise WorkflowAtomicityError(
                                "Clarification packet mutation timestamps are not exact"
                            )
                        plan_events, safe_plan = self._consume_replayed_plan(
                            pending_plan,
                            target=CaseState.AWAITING_CLARIFICATION,
                        )
                        replayed_version += 1
                        version_origins[replayed_version] = occurred_at
                        last_mutation_at = occurred_at
                        expected_packet_authorities.append(
                            _ExpectedPacketAuthority(
                                bound_version=replayed_version,
                                created_at=occurred_at,
                                state=CaseState.AWAITING_CLARIFICATION,
                                plan_events=plan_events,
                                safe_plan=safe_plan,
                                clarification_close=pending_clarification_close,
                            )
                        )
                        last_plan_events = plan_events
                        last_safe_plan = safe_plan
                        pending_plan.clear()
                        pending_plan_at = None
                        pending_clarification_at = None
                        pending_clarification_close = None
                        pending_provider.clear()
                        boundary_stage = 0
                else:
                    if (
                        pending_clarification is None
                        or event.round != pending_clarification.round
                        or event.field is not pending_clarification.field
                    ):
                        raise WorkflowAtomicityError(
                            "Clarification close event has no exact requested predecessor"
                        )
                    pending_clarification = None
                    pending_requested_at = None
                    pending_clarification_close = event
                    if event.status is ClarificationStatus.EXHAUSTED:
                        clarification_closed = True
                continue

            if isinstance(event, StateWorkflowEvent):
                if event.from_state is not replayed_state:
                    raise WorkflowAtomicityError(
                        "State history is not contiguous during version replay"
                    )
                if event.from_state is CaseState.CREATED and event.to_state is CaseState.DISCLOSED:
                    self._validate_replayed_intake_gate_boundary(
                        connection,
                        current=current,
                        decisions=tuple(pending_intake_gates),
                    )
                self._validate_state_replay_boundary(
                    event,
                    actor=audit.actor,
                    boundary_stage=boundary_stage,
                    intake_mode=intake_mode,
                )
                self._validate_provider_replay_batch(
                    event.from_state,
                    event.to_state,
                    tuple(pending_provider),
                    mutation_at=occurred_at,
                )
                if pending_plan_at is not None and pending_plan_at != occurred_at:
                    raise WorkflowAtomicityError(
                        "Plan events do not share their packet mutation timestamp"
                    )
                if pending_clarification_at is not None and pending_clarification_at != occurred_at:
                    raise WorkflowAtomicityError(
                        "Clarification events do not share their state mutation timestamp"
                    )
                replayed_version += 1
                version_origins[replayed_version] = occurred_at
                last_mutation_at = occurred_at
                creates_analysis_packet = bool(pending_plan) and (
                    event.from_state in {CaseState.ANALYZING, CaseState.AWAITING_CLARIFICATION}
                    and event.to_state in analysis_targets
                )
                if creates_analysis_packet:
                    plan_events, safe_plan = self._consume_replayed_plan(
                        pending_plan,
                        target=event.to_state,
                    )
                    expected_packet_authorities.append(
                        _ExpectedPacketAuthority(
                            bound_version=replayed_version,
                            created_at=occurred_at,
                            state=event.to_state,
                            plan_events=plan_events,
                            safe_plan=safe_plan,
                            clarification_close=pending_clarification_close,
                        )
                    )
                    last_plan_events = plan_events
                    last_safe_plan = safe_plan
                elif event.from_state is CaseState.AWAITING_CLARIFICATION or (
                    event.from_state is CaseState.ANALYZING
                    and event.to_state
                    in {
                        CaseState.AWAITING_CLARIFICATION,
                        CaseState.READY_TO_FILL,
                        CaseState.EMERGENCY_STOPPED,
                    }
                ):
                    raise WorkflowAtomicityError(
                        "Packet-producing analysis mutation has no exact plan events"
                    )
                elif (event.from_state, event.to_state) in {
                    (CaseState.READY_TO_FILL, CaseState.FILLING),
                    (CaseState.READY_TO_FILL, CaseState.BLOCKED),
                    (CaseState.FILLING, CaseState.VERIFYING),
                    (CaseState.FILLING, CaseState.BLOCKED),
                    (CaseState.VERIFYING, CaseState.REVIEW),
                    (CaseState.VERIFYING, CaseState.BLOCKED),
                    (CaseState.REVIEW, CaseState.HUMAN_APPROVED),
                }:
                    if last_plan_events is None or last_safe_plan is None:
                        raise WorkflowAtomicityError(
                            "Authority transition has no prior packet plan authority"
                        )
                    expected_packet_authorities.append(
                        _ExpectedPacketAuthority(
                            bound_version=replayed_version,
                            created_at=occurred_at,
                            state=event.to_state,
                            plan_events=last_plan_events,
                            safe_plan=last_safe_plan,
                        )
                    )
                elif pending_plan:
                    raise WorkflowAtomicityError(
                        "Plan events were not consumed by an analysis packet mutation"
                    )
                elif (
                    event.from_state in {CaseState.FILLING, CaseState.VERIFYING}
                    and event.to_state is CaseState.FAILED
                ):
                    if last_plan_events is None or last_safe_plan is None:
                        raise WorkflowAtomicityError(
                            "Terminal packet mutation has no prior packet plan authority"
                        )
                    expected_packet_authorities.append(
                        _ExpectedPacketAuthority(
                            bound_version=replayed_version,
                            created_at=occurred_at,
                            state=CaseState.FAILED,
                            plan_events=last_plan_events,
                            safe_plan=last_safe_plan,
                        )
                    )
                pending_plan.clear()
                pending_plan_at = None
                pending_clarification_at = None
                pending_clarification_close = None
                pending_provider.clear()
                pending_intake_gates.clear()
                boundary_stage = 0
                replayed_state = event.to_state

        if (
            pending_plan
            or pending_provider
            or pending_intake_gates
            or pending_clarification_at is not None
            or pending_clarification_close is not None
            or boundary_stage != 0
        ):
            raise WorkflowAtomicityError(
                "Workflow events were not consumed by a complete atomic mutation"
            )

        if (
            replayed_state is not current.state
            or replayed_version != current.version
            or last_mutation_at != current.updated_at
        ):
            raise WorkflowAtomicityError(
                "Case version or updated_at disagrees with replayed mutations"
            )

        active_payload = current.snapshot.active_clarification
        if current.state is CaseState.AWAITING_CLARIFICATION:
            if pending_clarification is None or pending_requested_at is None:
                raise WorkflowAtomicityError(
                    "Active clarification has no persisted requested lifecycle event"
                )
            active = ClarificationView.model_validate(active_payload)
            if (
                pending_clarification.round != active.round
                or pending_clarification.field is not active.field
                or pending_requested_at != active.requested_at
            ):
                raise WorkflowAtomicityError(
                    "Active clarification disagrees with its requested event"
                )
        elif pending_clarification is not None:
            raise WorkflowAtomicityError(
                "Closed case state retains an open clarification lifecycle"
            )

        if self._table_exists(connection, "case_packet_authority"):
            rows = connection.execute(
                """
                SELECT bound_case_version, created_at, packet_json
                FROM case_packet_authority
                WHERE case_id = ?
                ORDER BY bound_case_version
                """,
                (current.case_id,),
            ).fetchall()
            if len(rows) != len(expected_packet_authorities):
                raise WorkflowAtomicityError(
                    "Packet authority versions disagree with replayed packet mutations"
                )
            for row, expected in zip(rows, expected_packet_authorities, strict=True):
                stored_packet = ClaimPacket.model_validate_json(
                    _require_string(row["packet_json"], "packet authority JSON")
                )
                packet_plan_events = tuple(
                    (step.sequence, step.tool) for step in stored_packet.plan.steps
                )
                packet_safe_plan = tuple(
                    (step.tool, step.reason) for step in stored_packet.plan.steps
                )
                if (
                    _require_integer(
                        row["bound_case_version"],
                        "packet bound version",
                    )
                    != expected.bound_version
                    or _parse_datetime(
                        _require_string(
                            row["created_at"],
                            "packet authority created_at",
                        )
                    )
                    != expected.created_at
                    or stored_packet.case_id != current.case_id
                    or stored_packet.state is not expected.state
                    or packet_plan_events != expected.plan_events
                    or packet_safe_plan != expected.safe_plan
                ):
                    raise WorkflowAtomicityError(
                        "Historical packet content disagrees with replayed authority"
                    )
                self._validate_replayed_clarification_close(
                    expected.clarification_close,
                    target=expected.state,
                    packet=stored_packet,
                )
        return version_origins

    @classmethod
    def _validate_replayed_clarification_close(
        cls,
        close: ClarificationWorkflowEvent | None,
        *,
        target: CaseState,
        packet: ClaimPacket,
    ) -> None:
        if close is None:
            return
        manual_handoff = False
        gates = packet.gate_decisions
        if gates and gates[-1].gate_id is GateId.G5_COMPLETENESS:
            if len(gates) < 2 or gates[-2].gate_id is not GateId.G4_PROVENANCE:
                raise WorkflowAtomicityError(
                    "Clarification close lost its deterministic G4/G5 boundary"
                )
            g4, g5 = gates[-2:]
            provenance = evaluate_g4(packet, decided_at=g4.decided_at)
            completeness = cls._derive_completeness(
                provenance,
                completed_rounds=close.round,
                decided_at=g5.decided_at,
            )
            if provenance.decision != g4 or completeness.decision != g5:
                raise WorkflowAtomicityError(
                    "Clarification close disagrees with deterministic G4/G5 authority"
                )
            manual_handoff = completeness.manual_handoff
        expected_status = ClarificationStatus.CONFIRMED
        if (
            target is CaseState.BLOCKED
            and manual_handoff
            and close.round == MAX_CLARIFICATION_ROUNDS
        ):
            expected_status = ClarificationStatus.EXHAUSTED
        if close.status is not expected_status:
            raise WorkflowAtomicityError(
                "Clarification close status disagrees with its deterministic target"
            )

    @staticmethod
    def _validate_replayed_intake_gate_boundary(
        connection: sqlite3.Connection,
        *,
        current: CaseRecord,
        decisions: tuple[GateDecision, ...],
    ) -> None:
        if tuple(decision.gate_id for decision in decisions) != (
            GateId.G0_INTAKE,
            GateId.G1_PRIVACY,
        ) or any(not decision.passed for decision in decisions):
            raise WorkflowAtomicityError(
                "CREATED to DISCLOSED requires exactly the passed G0/G1 pair"
            )
        authority = connection.execute(
            """
            SELECT g0_gate_sequence, g1_gate_sequence
            FROM case_intake_authority
            WHERE case_id = ?
            """,
            (current.case_id,),
        ).fetchone()
        if authority is None:
            raise WorkflowAtomicityError("Intake gate replay has no immutable intake authority")
        sequences = (
            _require_integer(authority["g0_gate_sequence"], "G0 sequence"),
            _require_integer(authority["g1_gate_sequence"], "G1 sequence"),
        )
        rows = connection.execute(
            """
            SELECT sequence, decision_json
            FROM gate_decisions
            WHERE case_id = ? AND sequence IN (?, ?)
            ORDER BY sequence
            """,
            (current.case_id, *sequences),
        ).fetchall()
        referenced = tuple(
            GateDecision.model_validate_json(
                _require_string(row["decision_json"], "intake gate decision")
            )
            for row in rows
        )
        if (
            tuple(_require_integer(row["sequence"], "intake gate sequence") for row in rows)
            != sequences
            or referenced != decisions
        ):
            raise WorkflowAtomicityError(
                "Replayed G0/G1 pair disagrees with immutable intake authority"
            )

    @staticmethod
    def _replay_intake_mode(
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> str | None:
        row = connection.execute(
            "SELECT manifest_json FROM case_intake_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if row is None:
            if current.state is CaseState.CREATED:
                return None
            raise WorkflowAtomicityError("Canonical workflow replay has no bound intake authority")
        manifest = _load_json_object(_require_string(row["manifest_json"], "intake manifest"))
        mode = manifest.get("inputMode")
        if mode not in {"text", "audio"}:
            raise WorkflowAtomicityError("Canonical workflow replay has an invalid intake mode")
        return cast(str, mode)

    @staticmethod
    def _validate_replay_actor(
        event: StateWorkflowEvent | GateWorkflowEvent | AppendableWorkflowEvent,
        actor: ActorType,
    ) -> None:
        if isinstance(event, StateWorkflowEvent):
            if event.actor is not actor:
                raise WorkflowAtomicityError(
                    "State workflow actor disagrees with its audit authority"
                )
            return
        expected = (
            ActorType.AGENT
            if isinstance(
                event,
                ProviderCallWorkflowEvent
                | RetryWorkflowEvent
                | PlanStepWorkflowEvent
                | ToolCallWorkflowEvent
                | PortalFillWorkflowEvent,
            )
            else ActorType.SYSTEM
        )
        if actor is not expected:
            raise WorkflowAtomicityError(
                "Workflow event actor is not authorized for its event family"
            )

    @staticmethod
    def _validate_state_replay_boundary(
        event: StateWorkflowEvent,
        *,
        actor: ActorType,
        boundary_stage: int,
        intake_mode: str | None,
    ) -> None:
        transition = (event.from_state, event.to_state)
        system = ActorType.SYSTEM
        allowed: dict[tuple[CaseState, CaseState], tuple[ActorType, frozenset[int]]] = {
            (CaseState.CREATED, CaseState.DISCLOSED): (
                ActorType.HUMAN,
                frozenset({1}),
            ),
            (CaseState.DISCLOSED, CaseState.ANALYZING): (system, frozenset({0})),
            (
                CaseState.DISCLOSED,
                CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
            ): (system, frozenset({0})),
            (CaseState.DISCLOSED, CaseState.FAILED): (system, frozenset({4})),
            (
                CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                CaseState.ANALYZING,
            ): (ActorType.HUMAN, frozenset({0})),
            (
                CaseState.ANALYZING,
                CaseState.AWAITING_CLARIFICATION,
            ): (system, frozenset({3})),
            (CaseState.ANALYZING, CaseState.READY_TO_FILL): (
                system,
                frozenset({2}),
            ),
            (CaseState.ANALYZING, CaseState.BLOCKED): (
                system,
                frozenset({1, 2}),
            ),
            (CaseState.ANALYZING, CaseState.EMERGENCY_STOPPED): (
                system,
                frozenset({2}),
            ),
            (CaseState.ANALYZING, CaseState.FAILED): (system, frozenset({4})),
            (
                CaseState.AWAITING_CLARIFICATION,
                CaseState.READY_TO_FILL,
            ): (system, frozenset({3})),
            (CaseState.AWAITING_CLARIFICATION, CaseState.BLOCKED): (
                system,
                frozenset({3}),
            ),
            (CaseState.READY_TO_FILL, CaseState.FILLING): (
                ActorType.AGENT,
                frozenset({1}),
            ),
            (CaseState.READY_TO_FILL, CaseState.BLOCKED): (
                system,
                frozenset({1}),
            ),
            (CaseState.FILLING, CaseState.VERIFYING): (
                ActorType.AGENT,
                frozenset({3}),
            ),
            (CaseState.FILLING, CaseState.BLOCKED): (
                system,
                frozenset({2}),
            ),
            (CaseState.VERIFYING, CaseState.REVIEW): (
                system,
                frozenset({2}),
            ),
            (CaseState.VERIFYING, CaseState.BLOCKED): (
                system,
                frozenset({2}),
            ),
            (CaseState.FILLING, CaseState.FAILED): (system, frozenset({4})),
            (CaseState.VERIFYING, CaseState.FAILED): (system, frozenset({4})),
            (CaseState.REVIEW, CaseState.HUMAN_APPROVED): (
                ActorType.HUMAN,
                frozenset({1}),
            ),
            (CaseState.HUMAN_APPROVED, CaseState.RECEIPT): (
                system,
                frozenset({1}),
            ),
        }
        authority = allowed.get(transition)
        if (
            authority is None
            or actor is not authority[0]
            or event.actor is not authority[0]
            or boundary_stage not in authority[1]
            or (
                event.from_state is CaseState.DISCLOSED
                and (
                    (intake_mode == "text" and event.to_state is not CaseState.ANALYZING)
                    or (
                        intake_mode == "audio"
                        and event.to_state
                        not in {
                            CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                            CaseState.FAILED,
                        }
                    )
                    or intake_mode not in {"text", "audio"}
                )
            )
        ):
            raise WorkflowAtomicityError("State transition has no exact canonical writer boundary")

    @staticmethod
    def _consume_replayed_plan(
        pending_plan: list[PlanStepWorkflowEvent],
        *,
        target: CaseState,
    ) -> tuple[
        tuple[tuple[int, AllowedTool], ...],
        tuple[tuple[AllowedTool, str], ...],
    ]:
        safe_plan = {
            CaseState.AWAITING_CLARIFICATION: _AWAITING_CLARIFICATION_PLAN,
            CaseState.READY_TO_FILL: _READY_TO_FILL_PLAN,
            CaseState.BLOCKED: _BLOCKED_PLAN,
            CaseState.EMERGENCY_STOPPED: _BLOCKED_PLAN,
        }.get(target)
        if safe_plan is None:
            raise WorkflowAtomicityError("Packet plan has no authorized replay target")
        plan_events = tuple((event.sequence, event.tool) for event in pending_plan)
        expected_events = tuple(
            (index, tool) for index, (tool, _reason) in enumerate(safe_plan, start=1)
        )
        if plan_events != expected_events:
            raise WorkflowAtomicityError(
                "Plan events disagree with the deterministic target-state plan"
            )
        return plan_events, safe_plan

    @staticmethod
    def _validate_provider_replay_batch(
        from_state: CaseState,
        to_state: CaseState,
        events: tuple[
            tuple[
                ProviderCallWorkflowEvent | RetryWorkflowEvent | OperationalFailureWorkflowEvent,
                datetime,
            ],
            ...,
        ],
        *,
        mutation_at: datetime,
    ) -> None:
        event_values = tuple(event for event, _occurred_at in events)
        event_times = tuple(occurred_at for _event, occurred_at in events)
        if tuple(sorted(event_times)) != event_times or any(
            occurred_at > mutation_at for occurred_at in event_times
        ):
            raise WorkflowAtomicityError(
                "Provider telemetry timestamps escape their atomic boundary"
            )

        def successful_extraction() -> bool:
            if len(event_values) == 1:
                only = event_values[0]
                return (
                    isinstance(only, ProviderCallWorkflowEvent)
                    and only.operation is WorkflowOperation.EXTRACTION
                    and only.call_sequence == 1
                    and only.retry_attempt == 0
                )
            if len(event_values) != 3:
                return False
            first, retry, final = event_values
            return (
                isinstance(first, ProviderCallWorkflowEvent)
                and isinstance(retry, RetryWorkflowEvent)
                and isinstance(final, ProviderCallWorkflowEvent)
                and first.operation is WorkflowOperation.EXTRACTION
                and first.call_sequence == 1
                and first.retry_attempt == 0
                and retry.call_sequence == first.call_sequence
                and retry.failure.category is ProviderFailureCategory.INVALID_RESPONSE
                and retry.model_id is first.model_id
                and retry.provider_mode == first.provider_mode
                and retry.duration_ms == first.duration_ms
                and final.operation is WorkflowOperation.EXTRACTION
                and final.call_sequence == first.call_sequence + 1
                and final.retry_attempt == 1
                and final.model_id is first.model_id
                and final.provider_mode == first.provider_mode
            )

        def terminal_failure(operation: WorkflowOperation) -> bool:
            if len(event_values) == 1:
                only = event_values[0]
                return (
                    isinstance(only, OperationalFailureWorkflowEvent)
                    and only.operation is operation
                    and only.call_sequence == 1
                    and only.retry_attempt == 0
                )
            if operation is not WorkflowOperation.EXTRACTION or len(event_values) != 3:
                return False
            first, retry, failed = event_values
            return (
                isinstance(first, ProviderCallWorkflowEvent)
                and isinstance(retry, RetryWorkflowEvent)
                and isinstance(failed, OperationalFailureWorkflowEvent)
                and first.operation is WorkflowOperation.EXTRACTION
                and first.call_sequence == 1
                and first.retry_attempt == 0
                and retry.call_sequence == first.call_sequence
                and retry.failure.category is ProviderFailureCategory.INVALID_RESPONSE
                and retry.model_id is first.model_id
                and retry.provider_mode == first.provider_mode
                and retry.duration_ms == first.duration_ms
                and failed.operation is WorkflowOperation.EXTRACTION
                and failed.call_sequence == first.call_sequence + 1
                and failed.retry_attempt == 1
                and failed.model_id is first.model_id
                and failed.provider_mode == first.provider_mode
            )

        valid = False
        if (
            from_state is CaseState.DISCLOSED
            and to_state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
            and len(event_values) == 1
        ):
            only = event_values[0]
            valid = (
                isinstance(only, ProviderCallWorkflowEvent)
                and only.operation is WorkflowOperation.TRANSCRIPTION
                and only.call_sequence == 1
                and only.retry_attempt == 0
            )
        elif from_state is CaseState.ANALYZING and to_state in {
            CaseState.AWAITING_CLARIFICATION,
            CaseState.READY_TO_FILL,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
        }:
            valid = successful_extraction()
        elif to_state is CaseState.FAILED:
            operation_by_state = {
                CaseState.DISCLOSED: WorkflowOperation.TRANSCRIPTION,
                CaseState.ANALYZING: WorkflowOperation.EXTRACTION,
                CaseState.FILLING: WorkflowOperation.COMPUTER_USE,
                CaseState.VERIFYING: WorkflowOperation.VERIFICATION,
            }
            operation = operation_by_state.get(from_state)
            valid = (
                operation is not None
                and terminal_failure(operation)
                and bool(event_values)
                and isinstance(
                    event_values[-1],
                    OperationalFailureWorkflowEvent,
                )
                and event_times[-1] == mutation_at
            )
        else:
            valid = not event_values
        if not valid:
            raise WorkflowAtomicityError(
                "Provider telemetry does not form one authorized atomic boundary"
            )

    @staticmethod
    def _validate_capability_case_binding(
        capability: AuthorityCapabilityRecord,
        *,
        version_origins: dict[int, datetime],
    ) -> None:
        origin = version_origins.get(capability.bound_case_version)
        next_origin = version_origins.get(capability.bound_case_version + 1)
        consumed_at = capability.consumed_at
        if (
            origin is None
            or capability.issued_at < origin
            or (next_origin is not None and capability.issued_at >= next_origin)
            or (
                consumed_at is not None
                and (
                    consumed_at < origin or (next_origin is not None and consumed_at >= next_origin)
                )
            )
        ):
            raise PersistedDataIntegrityError(
                "Persisted capability falls outside its bound case-version lifetime"
            )

    @staticmethod
    def _require_state_transition_time(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        from_state: CaseState,
        to_state: CaseState,
    ) -> datetime:
        matches: list[datetime] = []
        for row in connection.execute(
            """
            SELECT event_json
            FROM workflow_events
            WHERE case_id = ? AND event_kind = ?
            ORDER BY source_audit_sequence
            """,
            (case_id, WorkflowEventKind.STATE.value),
        ):
            envelope = WorkflowEventEnvelope.model_validate_json(
                _require_string(row["event_json"], "state workflow event")
            )
            event = envelope.event
            if (
                isinstance(event, StateWorkflowEvent)
                and event.from_state is from_state
                and event.to_state is to_state
            ):
                matches.append(envelope.occurred_at)
        if len(matches) != 1:
            raise WorkflowAtomicityError(
                "Canonical authority has no unique originating state transition"
            )
        return matches[0]

    @staticmethod
    def _effective_packet_gates(
        history: tuple[GateDecision, ...],
    ) -> tuple[GateDecision, ...]:
        if len(history) < 4 or tuple(item.gate_id for item in history[:4]) != (
            GateId.G0_INTAKE,
            GateId.G1_PRIVACY,
            GateId.G2_OUTPUT_CONTRACT,
            GateId.G3_SAFETY_SCOPE,
        ):
            raise WorkflowAtomicityError("Packet authority requires the canonical G0-G3 prefix")
        tail = history[4:]
        authority_start = next(
            (
                index
                for index, decision in enumerate(tail)
                if decision.gate_id
                in {
                    GateId.G6_TOOL_AUTHORITY,
                    GateId.G7_PORTAL_WRITE,
                    GateId.G8_VERIFICATION,
                    GateId.G9_HUMAN_APPROVAL,
                    GateId.G10_RECEIPT_REDACTION,
                }
            ),
            len(tail),
        )
        analysis_tail = tail[:authority_start]
        authority_tail = tail[authority_start:]
        if any(
            decision.gate_id
            is not (GateId.G4_PROVENANCE if index % 2 == 0 else GateId.G5_COMPLETENESS)
            for index, decision in enumerate(analysis_tail)
        ):
            raise WorkflowAtomicityError("Packet authority requires canonical G4/G5 history pairs")
        expected_authority = (
            GateId.G6_TOOL_AUTHORITY,
            GateId.G7_PORTAL_WRITE,
            GateId.G8_VERIFICATION,
            GateId.G9_HUMAN_APPROVAL,
            GateId.G10_RECEIPT_REDACTION,
        )
        if (
            tuple(decision.gate_id for decision in authority_tail)
            != expected_authority[: len(authority_tail)]
        ):
            raise WorkflowAtomicityError("Packet authority requires the canonical G6-G10 suffix")
        if authority_tail and (
            len(analysis_tail) < 2
            or analysis_tail[-2].gate_id is not GateId.G4_PROVENANCE
            or analysis_tail[-1].gate_id is not GateId.G5_COMPLETENESS
            or not analysis_tail[-2].passed
            or not analysis_tail[-1].passed
        ):
            raise WorkflowAtomicityError(
                "Packet authority requires canonical G4/G5 history pairs; "
                "G6 and later require the final pair to pass"
            )
        if not analysis_tail:
            return (*history[:4], *authority_tail)
        effective_tail = (
            analysis_tail[-2:]
            if analysis_tail[-1].gate_id is GateId.G5_COMPLETENESS
            else analysis_tail[-1:]
        )
        return (*history[:4], *effective_tail, *authority_tail)

    @classmethod
    def _validate_packet_recomputations(
        cls,
        packet: ClaimPacket,
        history: tuple[GateDecision, ...],
        effective: tuple[GateDecision, ...],
    ) -> None:
        cls._validate_neutral_narrative(packet)
        if len(effective) < 5:
            return
        g4 = effective[4]
        provenance = evaluate_g4(packet, decided_at=g4.decided_at)
        if provenance.decision != g4:
            raise WorkflowAtomicityError(
                "Persisted packet no longer satisfies its deterministic G4"
            )
        if len(effective) < 6:
            return
        g5 = effective[5]
        completed_rounds = max(
            0,
            sum(decision.gate_id is GateId.G5_COMPLETENESS for decision in history) - 1,
        )
        completeness = cls._derive_completeness(
            provenance,
            completed_rounds=completed_rounds,
            decided_at=g5.decided_at,
        )
        if completeness.decision != g5:
            raise WorkflowAtomicityError(
                "Persisted packet no longer satisfies its deterministic G5"
            )

    def _insert_packet_authority(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: str,
        bound_case_version: int,
        packet: ClaimPacket,
        created_at: datetime,
    ) -> None:
        history = self._read_gate_decisions(connection, case_id=case_id)
        effective = self._effective_packet_gates(history)
        if packet.gate_decisions != effective:
            raise WorkflowAtomicityError(
                "ClaimPacket gates disagree with current persisted gate authority"
            )
        self._validate_packet_recomputations(packet, history, effective)
        packet_json = _dump_json_value(
            cast(JsonValue, packet.model_dump(mode="json", by_alias=True))
        )
        gates_json = _dump_json_value(
            cast(
                JsonValue,
                [decision.model_dump(mode="json", by_alias=True) for decision in effective],
            )
        )
        packet_digest = hashlib.sha256(
            b"claimdone-packet-authority-v1\0" + packet_json.encode("utf-8")
        ).hexdigest()
        gates_digest = hashlib.sha256(
            b"claimdone-packet-gates-v1\0" + gates_json.encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO case_packet_authority (
                case_id, bound_case_version, authority_version,
                packet_json, packet_sha256, effective_gates_json,
                effective_gates_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                bound_case_version,
                packet_json,
                packet_digest,
                gates_json,
                gates_digest,
                _dump_aware_datetime(created_at, "packet authority created_at"),
            ),
        )

    def _validate_packet_authority_row(
        self,
        row: sqlite3.Row,
        *,
        current: CaseRecord,
        history: tuple[GateDecision, ...],
    ) -> tuple[int, datetime, ClaimPacket, tuple[GateDecision, ...]]:
        """Validate one immutable packet row, including historical authorities."""

        packet_json = _require_string(row["packet_json"], "packet authority JSON")
        packet_digest = _require_string(row["packet_sha256"], "packet authority digest")
        gates_json = _require_string(
            row["effective_gates_json"],
            "packet gate authority JSON",
        )
        gates_digest = _require_string(
            row["effective_gates_sha256"],
            "packet gate authority digest",
        )
        stored_packet = ClaimPacket.model_validate_json(packet_json)
        effective = _GATE_DECISIONS_ADAPTER.validate_json(gates_json)
        canonical_packet_json = _dump_json_value(
            cast(JsonValue, stored_packet.model_dump(mode="json", by_alias=True))
        )
        canonical_gates_json = _dump_json_value(
            cast(
                JsonValue,
                [decision.model_dump(mode="json", by_alias=True) for decision in effective],
            )
        )
        bound_version = _require_integer(row["bound_case_version"], "packet bound version")
        created_at = _parse_datetime(
            _require_string(row["created_at"], "packet authority created_at")
        )
        if (
            _require_integer(row["authority_version"], "packet authority version") != 1
            or _require_string(row["case_id"], "packet authority case id") != current.case_id
            or bound_version < 2
            or bound_version > current.version
            or created_at < current.created_at
            or created_at > current.updated_at
            or packet_json != canonical_packet_json
            or gates_json != canonical_gates_json
            or _SHA256.fullmatch(packet_digest) is None
            or _SHA256.fullmatch(gates_digest) is None
            or hashlib.sha256(
                b"claimdone-packet-authority-v1\0" + packet_json.encode("utf-8")
            ).hexdigest()
            != packet_digest
            or hashlib.sha256(
                b"claimdone-packet-gates-v1\0" + gates_json.encode("utf-8")
            ).hexdigest()
            != gates_digest
            or stored_packet.case_id != current.case_id
            or stored_packet.gate_decisions != effective
        ):
            raise WorkflowAtomicityError("Persisted ClaimPacket authority is invalid")

        matching_history = tuple(
            prefix
            for end in range(4, len(history) + 1)
            if (prefix := history[:end])[-1].decided_at <= created_at
            and self._effective_packet_gates(prefix) == effective
        )
        if not matching_history:
            raise WorkflowAtomicityError("Packet authority gates have no bound persisted history")
        recomputation_error: WorkflowAtomicityError | None = None
        for prefix in matching_history:
            try:
                self._validate_packet_recomputations(stored_packet, prefix, effective)
            except WorkflowAtomicityError as error:
                recomputation_error = error
            else:
                break
        else:
            raise WorkflowAtomicityError(
                "Packet authority fails deterministic gate recomputation"
            ) from recomputation_error
        return bound_version, created_at, stored_packet, effective

    def _validate_retained_packet_authorities(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> None:
        """Revalidate every retained packet row, not only the current version."""

        rows = connection.execute(
            """
            SELECT * FROM case_packet_authority
            WHERE case_id = ?
            ORDER BY bound_case_version ASC
            """,
            (current.case_id,),
        ).fetchall()
        if not rows:
            return
        history = self._read_gate_decisions(connection, case_id=current.case_id)
        self._effective_packet_gates(history)
        prior_version = 1
        prior_created_at = current.created_at
        for row in rows:
            bound_version, created_at, _packet, _effective = self._validate_packet_authority_row(
                row,
                current=current,
                history=history,
            )
            if bound_version <= prior_version or created_at < prior_created_at:
                raise WorkflowAtomicityError(
                    "Packet authority bounds are not chronologically monotonic"
                )
            prior_version = bound_version
            prior_created_at = created_at

    def _require_current_packet_authority(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> None:
        self._validate_retained_packet_authorities(connection, current)
        packet = current.snapshot.claim_packet
        row = connection.execute(
            """
            SELECT * FROM case_packet_authority
            WHERE case_id = ? AND bound_case_version = ?
            """,
            (current.case_id, current.version),
        ).fetchone()
        if packet is None:
            if row is not None:
                raise WorkflowAtomicityError(
                    "Packet authority exists without a current ClaimPacket"
                )
            return
        if row is None:
            raise WorkflowAtomicityError("Current ClaimPacket has no immutable authority")
        history = self._read_gate_decisions(connection, case_id=current.case_id)
        bound_version, _created_at, stored_packet, effective = self._validate_packet_authority_row(
            row,
            current=current,
            history=history,
        )
        if (
            bound_version != current.version
            or stored_packet != packet
            or stored_packet.state is not current.state
            or self._effective_packet_gates(history) != effective
        ):
            raise WorkflowAtomicityError("Current ClaimPacket authority is invalid")
        self._validate_packet_recomputations(stored_packet, history, effective)

    def _require_intake_authority(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
        *,
        verify_media: bool = True,
    ) -> tuple[JsonObject, str]:
        """Revalidate persisted authority, owned handles, and every staged byte."""

        from claimdone_api.media import (
            CaseHandle,
            ExifDecision,
            MediaStorageError,
            StoredAssetRef,
            UnsafeStoragePath,
            expected_model_image_bytes,
        )

        row = connection.execute(
            """
            SELECT * FROM case_intake_authority WHERE case_id = ?
            """,
            (current.case_id,),
        ).fetchone()
        if row is None:
            raise WorkflowAtomicityError("Case has no canonical intake authority")
        authority_version = _require_integer(row["authority_version"], "authority version")
        bound_version = _require_integer(row["bound_case_version"], "bound case version")
        authority_created_at = _parse_datetime(
            _require_string(row["created_at"], "intake authority created_at")
        )
        storage_name = _require_string(row["storage_name"], "authority storage name")
        manifest_json = _require_string(row["manifest_json"], "intake manifest")
        manifest_digest = _require_string(
            row["manifest_sha256"],
            "intake manifest digest",
        )
        if (
            authority_version != 1
            or bound_version != 2
            or bound_version > current.version
            or authority_created_at
            != self._require_state_transition_time(
                connection,
                case_id=current.case_id,
                from_state=CaseState.CREATED,
                to_state=CaseState.DISCLOSED,
            )
            or _MEDIA_STORAGE_NAME.fullmatch(storage_name) is None
            or _SHA256.fullmatch(manifest_digest) is None
            or hashlib.sha256(
                b"claimdone-intake-authority-v1\0" + manifest_json.encode("utf-8")
            ).hexdigest()
            != manifest_digest
        ):
            raise WorkflowAtomicityError("Persisted intake authority identity is invalid")
        manifest = _load_json_object(manifest_json)
        if _dump_json_object(manifest) != manifest_json:
            raise WorkflowAtomicityError("Intake manifest is not canonical JSON")
        summary = manifest.get("intakeSummary")
        if (
            manifest.get("authorityVersion") != 1
            or manifest.get("caseId") != current.case_id
            or manifest.get("boundCaseVersion") != bound_version
            or manifest.get("storageName") != storage_name
            or type(summary) is not dict
        ):
            raise WorkflowAtomicityError("Intake manifest is not bound to the case snapshot")
        current_summary = current.snapshot.intake_summary
        if type(current_summary) is not dict:
            raise WorkflowAtomicityError("Case lost its intake summary")
        handle_row = connection.execute(
            "SELECT storage_name, created_at FROM case_media_handles WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if (
            handle_row is None
            or _require_string(handle_row["storage_name"], "media handle") != storage_name
            or _parse_datetime(_require_string(handle_row["created_at"], "media handle created_at"))
            != authority_created_at
        ):
            raise WorkflowAtomicityError("Intake authority lost its owned media handle")

        g0_sequence = _require_integer(row["g0_gate_sequence"], "G0 sequence")
        g1_sequence = _require_integer(row["g1_gate_sequence"], "G1 sequence")
        gate_rows = connection.execute(
            """
            SELECT sequence, case_id, decision_json
            FROM gate_decisions
            WHERE case_id = ?
            ORDER BY sequence
            LIMIT 2
            """,
            (current.case_id,),
        ).fetchall()
        if len(gate_rows) != 2:
            raise WorkflowAtomicityError("Intake authority lost its G0/G1 decisions")
        decisions = tuple(
            GateDecision.model_validate_json(
                _require_string(gate_row["decision_json"], "authority gate")
            )
            for gate_row in gate_rows
        )
        if (
            tuple(_require_integer(item["sequence"], "gate sequence") for item in gate_rows)
            != (g0_sequence, g1_sequence)
            or any(
                _require_string(item["case_id"], "gate case id") != current.case_id
                for item in gate_rows
            )
            or tuple(decision.gate_id for decision in decisions)
            != (GateId.G0_INTAKE, GateId.G1_PRIVACY)
            or any(not decision.passed for decision in decisions)
            or decisions[0].decided_at < current.created_at
            or decisions[0].decided_at > decisions[1].decided_at
            or decisions[1].decided_at > authority_created_at
        ):
            raise WorkflowAtomicityError("Intake authority G0/G1 binding is invalid")

        images = manifest.get("images")
        input_order = manifest.get("inputOrder")
        choices = manifest.get("exifChoices")
        if (
            type(images) is not list
            or len(images) != 3
            or type(input_order) is not list
            or type(choices) is not list
            or len(choices) != 3
            or manifest.get("modelCopyApproved") is not True
            or manifest.get("consents")
            != {
                "sandboxAcknowledged": True,
                "imageRightsConfirmed": True,
                "dataProcessingApproved": True,
            }
        ):
            raise WorkflowAtomicityError("Intake authority policy manifest is invalid")
        handle = CaseHandle(storage_name=storage_name)

        def read_owned(ref: StoredAssetRef) -> bytes:
            try:
                return self.__media_store.read_bytes(handle, ref)
            except (MediaStorageError, UnsafeStoragePath) as error:
                raise WorkflowAtomicityError(
                    "Intake authority media bytes are missing or altered"
                ) from error

        def ref_from(value: object) -> StoredAssetRef:
            if type(value) is not dict:
                raise WorkflowAtomicityError("Authority asset reference is invalid")
            file_id = value.get("fileId")
            media_type = value.get("mediaType")
            digest = value.get("sha256")
            if (
                type(file_id) is not str
                or type(media_type) is not str
                or type(digest) is not str
                or _SHA256.fullmatch(digest) is None
            ):
                raise WorkflowAtomicityError("Authority asset reference is invalid")
            return StoredAssetRef(
                file_id=file_id,
                media_type=media_type,
                sha256=digest,
            )

        observed_order: list[str] = []
        for index, image in enumerate(images):
            choice_value = choices[index]
            if type(image) is not dict or type(choice_value) is not dict:
                raise WorkflowAtomicityError("Authority image entry is invalid")
            input_id = image.get("inputId")
            if (
                type(input_id) is not str
                or input_id != f"image-{index + 1}"
                or choice_value.get("inputId") != input_id
                or image.get("imageFormat") not in {"JPEG", "PNG"}
            ):
                raise WorkflowAtomicityError("Authority image ordering is invalid")
            decision_value = choice_value.get("decision")
            if type(decision_value) is not str:
                raise WorkflowAtomicityError("Authority EXIF choice is invalid")
            try:
                choice = ExifDecision(decision_value)
            except (TypeError, ValueError) as error:
                raise WorkflowAtomicityError("Authority EXIF choice is invalid") from error
            source_ref = ref_from(image.get("source"))
            model_ref = ref_from(image.get("model"))
            if verify_media:
                source = read_owned(source_ref)
                try:
                    expected = expected_model_image_bytes(
                        source,
                        image_format=cast(str, image.get("imageFormat")),
                        decision=choice,
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise WorkflowAtomicityError(
                        "Authority source image no longer satisfies G1"
                    ) from error
                if read_owned(model_ref) != expected:
                    raise WorkflowAtomicityError("Authority model bytes no longer satisfy G1")
            observed_order.append(input_id)
        if input_order != observed_order:
            raise WorkflowAtomicityError("Authority input order changed")

        input_mode = manifest.get("inputMode")
        text_value = manifest.get("text")
        audio_value = manifest.get("audio")
        statement_value = manifest.get("statement")
        if input_mode == "text":
            text_ref = ref_from(text_value)
            if (
                audio_value is not None
                or statement_value != text_value
                or current_summary != summary
            ):
                raise WorkflowAtomicityError("Text authority mode is inconsistent")
            if verify_media:
                try:
                    read_owned(text_ref).decode("utf-8")
                except UnicodeDecodeError as error:
                    raise WorkflowAtomicityError(
                        "Text authority bytes are not canonical UTF-8"
                    ) from error
            if manifest.get("transcriptState") != "not_applicable":
                raise WorkflowAtomicityError("Text authority has transcript state")
        elif input_mode == "audio":
            audio_ref = ref_from(audio_value)
            base_current_summary = dict(current_summary)
            base_current_summary["statement"] = None
            if (
                text_value is not None
                or statement_value is not None
                or audio_ref.media_type != "audio/wav"
                or manifest.get("transcriptState") != "awaiting_transcription"
                or base_current_summary != summary
            ):
                raise WorkflowAtomicityError("Audio authority mode is inconsistent")
            if verify_media:
                read_owned(audio_ref)
        else:
            raise WorkflowAtomicityError("Intake authority input mode is invalid")
        return manifest, manifest_digest

    def _require_transcript_authority(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
        *,
        intake_manifest_digest: str,
        verify_media: bool = True,
    ) -> TranscriptRecord:
        """Verify transcript bytes, provider telemetry, snapshot, and version binding."""

        from claimdone_api.media import (
            CaseHandle,
            MediaStorageError,
            StoredAssetRef,
            UnsafeStoragePath,
        )

        row = connection.execute(
            "SELECT * FROM case_transcript_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if row is None:
            raise TranscriptStateError("Case has no canonical transcript authority")
        manifest_json = _require_string(row["manifest_json"], "transcript manifest")
        manifest_digest = _require_string(
            row["manifest_sha256"],
            "transcript manifest digest",
        )
        manifest = _load_json_object(manifest_json)
        bound_version = _require_integer(row["bound_case_version"], "bound case version")
        authority_created_at = _parse_datetime(
            _require_string(row["created_at"], "transcript authority created_at")
        )
        transcript_id = _require_string(row["transcript_id"], "transcript id")
        local_ref = _require_string(row["transcript_local_ref"], "transcript local ref")
        digest = _require_string(row["transcript_sha256"], "transcript digest")
        provider_sequence = _require_integer(
            row["provider_source_audit_sequence"],
            "transcription provider sequence",
        )
        if (
            _require_integer(row["authority_version"], "transcript authority version") != 1
            or bound_version != 3
            or bound_version > current.version
            or authority_created_at
            != self._require_state_transition_time(
                connection,
                case_id=current.case_id,
                from_state=CaseState.DISCLOSED,
                to_state=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
            )
            or _SHA256.fullmatch(digest) is None
            or _SHA256.fullmatch(manifest_digest) is None
            or _require_string(
                row["intake_manifest_sha256"],
                "bound intake manifest digest",
            )
            != intake_manifest_digest
            or _dump_json_object(manifest) != manifest_json
            or hashlib.sha256(
                b"claimdone-transcript-authority-v1\0" + manifest_json.encode("utf-8")
            ).hexdigest()
            != manifest_digest
            or manifest.get("authorityVersion") != 1
            or manifest.get("caseId") != current.case_id
            or manifest.get("boundCaseVersion") != bound_version
            or manifest.get("intakeManifestSha256") != intake_manifest_digest
            or manifest.get("transcriptId") != transcript_id
            or manifest.get("providerSourceAuditSequence") != provider_sequence
        ):
            raise TranscriptStateError("Persisted transcript authority identity is invalid")
        statement = manifest.get("transcript")
        if (
            type(statement) is not dict
            or statement.get("fileId") != local_ref
            or statement.get("mediaType") != "text/plain"
            or statement.get("sha256") != digest
            or current.snapshot.intake_summary is None
            or current.snapshot.intake_summary.get("statement") != statement
        ):
            raise TranscriptStateError("Transcript authority lost its statement binding")
        intake_row = connection.execute(
            "SELECT storage_name FROM case_intake_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if intake_row is None:
            raise TranscriptStateError("Transcript authority lost its intake handle")
        storage_name = _require_string(intake_row["storage_name"], "intake storage name")
        if manifest.get("storageName") != storage_name:
            raise TranscriptStateError("Transcript authority changed its storage handle")
        ref = StoredAssetRef(
            file_id=local_ref,
            media_type="text/plain",
            sha256=digest,
        )
        if verify_media:
            try:
                transcript_text = self.__media_store.read_bytes(
                    CaseHandle(storage_name=storage_name),
                    ref,
                ).decode("utf-8")
            except (
                MediaStorageError,
                UnsafeStoragePath,
                UnicodeDecodeError,
                ValueError,
                TypeError,
            ) as error:
                raise TranscriptStateError("Transcript authority bytes are invalid") from error
            normalized = re.sub(
                r"\s+",
                " ",
                unicodedata.normalize("NFC", transcript_text),
            ).strip()
            if normalized != transcript_text or not transcript_text or len(transcript_text) > 4_000:
                raise TranscriptStateError("Transcript authority text is not normalized")
        workflow_row = connection.execute(
            """
            SELECT event_json FROM workflow_events
            WHERE source_audit_sequence = ? AND case_id = ?
            """,
            (provider_sequence, current.case_id),
        ).fetchone()
        if workflow_row is None:
            raise TranscriptStateError("Transcript authority lost provider telemetry")
        envelope = WorkflowEventEnvelope.model_validate_json(
            _require_string(workflow_row["event_json"], "transcription workflow event")
        )
        if (
            type(envelope.event) is not ProviderCallWorkflowEvent
            or envelope.event.operation is not WorkflowOperation.TRANSCRIPTION
            or envelope.event.retry_attempt != 0
            or envelope.event.status != "succeeded"
        ):
            raise TranscriptStateError("Transcript authority provider telemetry is invalid")
        transcript = self._require_transcript(connection, current.case_id)
        if (
            transcript.transcript_id != transcript_id
            or transcript.local_ref != local_ref
            or transcript.transcript_sha256 != digest
            or transcript.bound_case_version != bound_version
            or transcript.created_at != authority_created_at
        ):
            raise TranscriptStateError("Transcript record disagrees with its authority")
        return transcript

    @classmethod
    def _require_passed_g0_g1_history(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> tuple[GateDecision, GateDecision]:
        history = cls._read_gate_decisions(connection, case_id=current.case_id)
        if (
            len(history) != 2
            or tuple(decision.gate_id for decision in history)
            != (GateId.G0_INTAKE, GateId.G1_PRIVACY)
            or any(not decision.passed for decision in history)
            or history[0].decided_at > history[1].decided_at
            or history[1].decided_at > current.updated_at
        ):
            raise WorkflowAtomicityError(
                "Transcript authority requires the exact persisted passed G0/G1 prefix"
            )
        return history

    @staticmethod
    def _validate_transcript_snapshot_authority(current: CaseRecord) -> None:
        snapshot = current.snapshot
        if snapshot.intake_summary is None:
            raise TranscriptStateError("Transcript authority requires persisted intake")
        if snapshot.claim_packet is not None or snapshot.active_clarification is not None:
            raise TranscriptStateError(
                "Transcript authority cannot carry a ClaimPacket or clarification"
            )
        if snapshot.portal_state is not PortalState.DRAFT:
            raise TranscriptStateError("Transcript authority requires draft portal state")

    def _validate_transcript_case_binding(
        self,
        connection: sqlite3.Connection,
        current: CaseRecord,
        transcript: TranscriptRecord,
    ) -> None:
        """Bind pending/confirmed transcript metadata to the current case state."""

        if transcript.created_at > current.updated_at:
            raise TranscriptStateError("Transcript cannot postdate the current case")
        if current.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
            if (
                transcript.confirmed
                or transcript.bound_case_version != current.version
                or transcript.created_at != current.updated_at
            ):
                raise TranscriptStateError(
                    "Awaiting transcript state requires its current pending transcript"
                )
            return
        if (
            not transcript.confirmed
            or transcript.bound_case_version != 3
            or transcript.bound_case_version >= current.version
            or transcript.confirmed_at is None
            or transcript.confirmed_at > current.updated_at
            or transcript.confirmed_at
            != self._require_state_transition_time(
                connection,
                case_id=current.case_id,
                from_state=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                to_state=CaseState.ANALYZING,
            )
        ):
            raise TranscriptStateError(
                "A transcript outside confirmation state must be confirmed and prior-bound"
            )

    @classmethod
    def _validate_analysis_command(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        existing_gates: tuple[GateDecision, ...],
    ) -> CaseSnapshot:
        if command.case_id != current.case_id:
            raise WorkflowAtomicityError("Analysis command caseId is not current")
        allowed_current = {
            CaseState.ANALYZING,
            CaseState.AWAITING_CLARIFICATION,
        }
        allowed_target = {
            CaseState.AWAITING_CLARIFICATION,
            CaseState.READY_TO_FILL,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
        }
        if current.state not in allowed_current or command.target not in allowed_target:
            raise WorkflowAtomicityError(
                "Analysis commands require analyzing/awaiting_clarification and a closed target"
            )
        same_clarification_state = (
            current.state is CaseState.AWAITING_CLARIFICATION
            and command.target is CaseState.AWAITING_CLARIFICATION
        )
        if not same_clarification_state:
            try:
                validate_case_transition(current.state, command.target)
            except ValueError as error:
                raise WorkflowAtomicityError(str(error)) from error
        if command.updated_at.utcoffset() is None:
            raise WorkflowAtomicityError("Analysis updatedAt must include a timezone")
        if command.updated_at < current.updated_at:
            raise WorkflowAtomicityError("Analysis updatedAt cannot move backwards")
        if type(command.gate_decisions) is not tuple or any(
            not isinstance(decision, GateDecision) for decision in command.gate_decisions
        ):
            raise WorkflowAtomicityError("Analysis gates must be canonical GateDecision values")
        if type(command.provider_events) is not tuple or any(
            not isinstance(emission, ProviderWorkflowEmission)
            for emission in command.provider_events
        ):
            raise WorkflowAtomicityError(
                "Analysis provider events must use ProviderWorkflowEmission"
            )
        if type(command.plan_steps) is not tuple or any(
            not isinstance(event, PlanStepWorkflowEvent) for event in command.plan_steps
        ):
            raise WorkflowAtomicityError("Analysis plan events must be canonical")
        if type(command.clarification_events) is not tuple or any(
            not isinstance(event, ClarificationWorkflowEvent)
            for event in command.clarification_events
        ):
            raise WorkflowAtomicityError("Clarification events must be canonical")

        packet = command.claim_packet
        if (
            command.target
            in {
                CaseState.AWAITING_CLARIFICATION,
                CaseState.READY_TO_FILL,
            }
            and packet is None
        ):
            raise WorkflowAtomicityError(
                f"{command.target.value} requires a target-state ClaimPacket"
            )
        if current.snapshot.claim_packet is not None and packet is None:
            raise WorkflowAtomicityError(
                "A case with a stored ClaimPacket requires its target-state packet"
            )
        authority = cls._validate_analysis_authority(
            connection,
            current,
            command,
            existing_gates=existing_gates,
        )
        effective_gates = authority.effective_gates
        cls._validate_analysis_clarification(
            current,
            command,
            completeness=authority.completeness,
        )
        if packet is not None:
            if packet.gate_decisions != effective_gates:
                raise WorkflowAtomicityError(
                    "ClaimPacket gates must equal the bound prior prefix plus new decisions"
                )
            if packet.verification.status is not VerificationState.PENDING:
                raise WorkflowAtomicityError("Analysis targets require pending verification")
            if packet.portal_state is not PortalState.DRAFT:
                raise WorkflowAtomicityError("Analysis targets require draft portal state")
            if command.target is CaseState.READY_TO_FILL and packet.claim.missing_required_fields:
                raise WorkflowAtomicityError("ready_to_fill cannot retain missing required fields")
            expected_plan = tuple((step.sequence, step.tool) for step in packet.plan.steps)
            supplied_plan = tuple((event.sequence, event.tool) for event in command.plan_steps)
            allowed_tools = {
                CaseState.AWAITING_CLARIFICATION: _AWAITING_CLARIFICATION_PLAN,
                CaseState.READY_TO_FILL: _READY_TO_FILL_PLAN,
                CaseState.BLOCKED: _BLOCKED_PLAN,
                CaseState.EMERGENCY_STOPPED: _BLOCKED_PLAN,
            }[command.target]
            packet_plan = tuple((step.tool, step.reason) for step in packet.plan.steps)
            if packet_plan != allowed_tools or supplied_plan != expected_plan:
                raise WorkflowAtomicityError(
                    "Plan-step events must exactly match the target-state safe plan"
                )
        elif command.plan_steps:
            raise WorkflowAtomicityError("Plan-step events require a ClaimPacket")

        if command.active_clarification is not None and not isinstance(
            command.active_clarification, ClarificationView
        ):
            raise WorkflowAtomicityError(
                "active clarification must be a canonical ClarificationView"
            )
        active_payload = (
            None
            if command.active_clarification is None
            else command.active_clarification.model_dump(mode="json", by_alias=True)
        )
        snapshot = replace(
            current.snapshot,
            portal_state=(current.snapshot.portal_state if packet is None else packet.portal_state),
            claim_packet=packet,
            active_clarification=active_payload,
        )
        try:
            _validate_snapshot(command.case_id, command.target, snapshot)
        except ValueError as error:
            raise WorkflowAtomicityError(str(error)) from error
        return snapshot

    @staticmethod
    def _validate_analysis_command_shape(command: AnalysisWorkflowCommand) -> None:
        if type(command.case_id) is not str or _IDENTIFIER.fullmatch(command.case_id) is None:
            raise WorkflowAtomicityError("Analysis command caseId is invalid")
        if type(command.expected_version) is not int or command.expected_version < 1:
            raise WorkflowAtomicityError(
                "Analysis expectedVersion must be a positive strict integer"
            )
        if not isinstance(command.target, CaseState):
            raise WorkflowAtomicityError("Analysis target must be a canonical CaseState")
        if type(command.updated_at) is not datetime or command.updated_at.utcoffset() is None:
            raise WorkflowAtomicityError("Analysis updatedAt must be an aware datetime object")
        if type(command.gate_decisions) is not tuple or any(
            not isinstance(decision, GateDecision) for decision in command.gate_decisions
        ):
            raise WorkflowAtomicityError("Analysis gates must be canonical GateDecision values")
        for decision in command.gate_decisions:
            SqliteCaseRepository._require_canonical_contract(
                decision,
                "GateDecision",
            )
        if type(command.provider_events) is not tuple or any(
            not isinstance(emission, ProviderWorkflowEmission)
            or type(emission.occurred_at) is not datetime
            for emission in command.provider_events
        ):
            raise WorkflowAtomicityError(
                "Analysis provider events must use typed, datetime-bound emissions"
            )
        for emission in command.provider_events:
            SqliteCaseRepository._require_canonical_contract(
                emission.event,
                "provider event",
            )
        if type(command.plan_steps) is not tuple or any(
            not isinstance(event, PlanStepWorkflowEvent) for event in command.plan_steps
        ):
            raise WorkflowAtomicityError("Analysis plan events must be canonical")
        for plan_event in command.plan_steps:
            SqliteCaseRepository._require_canonical_contract(
                plan_event,
                "plan-step event",
            )
        if type(command.clarification_events) is not tuple or any(
            not isinstance(event, ClarificationWorkflowEvent)
            for event in command.clarification_events
        ):
            raise WorkflowAtomicityError("Clarification events must be canonical")
        for clarification_event in command.clarification_events:
            SqliteCaseRepository._require_canonical_contract(
                clarification_event,
                "clarification event",
            )
        if command.claim_packet is not None and not isinstance(command.claim_packet, ClaimPacket):
            raise WorkflowAtomicityError("claim_packet must be canonical or null")
        if command.claim_packet is not None:
            SqliteCaseRepository._require_canonical_contract(
                command.claim_packet,
                "ClaimPacket",
            )
        if command.active_clarification is not None and not isinstance(
            command.active_clarification, ClarificationView
        ):
            raise WorkflowAtomicityError(
                "active clarification must be a canonical ClarificationView"
            )
        if command.active_clarification is not None:
            SqliteCaseRepository._require_canonical_contract(
                command.active_clarification,
                "ClarificationView",
            )
        if command.clarification_answer is not None and not isinstance(
            command.clarification_answer, ClarificationAnswerRequest
        ):
            raise WorkflowAtomicityError("clarification_answer must be canonical or null")
        if command.clarification_answer is not None:
            SqliteCaseRepository._require_canonical_contract(
                command.clarification_answer,
                "ClarificationAnswerRequest",
            )
        SqliteCaseRepository._validate_approved_evidence_shape(command.approved_evidence)
        SqliteCaseRepository._validate_output_contract_attempt_shape(command.g2_attempts)
        if command.safety_input is not None:
            SqliteCaseRepository._validate_safety_input(command.safety_input)

    @staticmethod
    def _validate_terminal_provider_command_shape(
        command: TerminalProviderFailureCommand,
    ) -> None:
        if type(command.case_id) is not str or _IDENTIFIER.fullmatch(command.case_id) is None:
            raise WorkflowAtomicityError("Provider failure caseId is invalid")
        if type(command.expected_version) is not int or command.expected_version < 1:
            raise WorkflowAtomicityError(
                "Provider failure expectedVersion must be a positive strict integer"
            )
        if not isinstance(command.event, OperationalFailureWorkflowEvent):
            raise WorkflowAtomicityError(
                "Terminal provider command requires OperationalFailureWorkflowEvent"
            )
        SqliteCaseRepository._require_canonical_contract(
            command.event,
            "OperationalFailureWorkflowEvent",
        )
        if type(command.occurred_at) is not datetime or command.occurred_at.utcoffset() is None:
            raise WorkflowAtomicityError(
                "Provider failure occurredAt must be an aware datetime object"
            )
        if type(command.provider_events) is not tuple or any(
            not isinstance(emission, ProviderWorkflowEmission)
            or type(emission.occurred_at) is not datetime
            for emission in command.provider_events
        ):
            raise WorkflowAtomicityError(
                "Provider failure prefix must use typed, datetime-bound emissions"
            )
        for emission in command.provider_events:
            SqliteCaseRepository._require_canonical_contract(
                emission.event,
                "provider failure prefix event",
            )
        if command.claim_packet is not None and not isinstance(command.claim_packet, ClaimPacket):
            raise WorkflowAtomicityError("claim_packet must be canonical or null")
        if command.claim_packet is not None:
            SqliteCaseRepository._require_canonical_contract(
                command.claim_packet,
                "ClaimPacket",
            )
        SqliteCaseRepository._validate_approved_evidence_shape(command.approved_evidence)
        SqliteCaseRepository._validate_output_contract_attempt_shape(command.g2_attempts)

    @staticmethod
    def _validate_approved_evidence_shape(
        evidence: tuple[EvidenceItem, ...],
    ) -> None:
        if type(evidence) is not tuple or any(
            not isinstance(item, EvidenceItem) for item in evidence
        ):
            raise WorkflowAtomicityError(
                "Approved evidence must be a tuple of canonical EvidenceItem values"
            )
        for item in evidence:
            SqliteCaseRepository._require_canonical_contract(
                item,
                "approved EvidenceItem",
            )

    @staticmethod
    def _validate_output_contract_attempt_shape(
        attempts: tuple[OutputContractAttempt, ...],
    ) -> None:
        if type(attempts) is not tuple or any(
            not isinstance(attempt, OutputContractAttempt)
            or not isinstance(attempt.envelope, ModelOutputEnvelope)
            or type(attempt.decided_at) is not datetime
            or attempt.decided_at.utcoffset() is None
            for attempt in attempts
        ):
            raise WorkflowAtomicityError(
                "G2 attempts must be raw, timezone-bound OutputContractAttempt values"
            )
        for index, attempt in enumerate(attempts):
            envelope = attempt.envelope
            if (
                type(envelope.payload) not in {str, bytes, type(None)}
                or type(envelope.refusal) is not bool
                or type(envelope.truncated) is not bool
                or type(envelope.attempt) is not int
                or envelope.attempt != index
            ):
                raise WorkflowAtomicityError(
                    "G2 raw envelopes must use strict payload, flags, and contiguous attempts"
                )
        if len(attempts) > 2:
            raise WorkflowAtomicityError("G2 allows at most one retry")

    @staticmethod
    def _require_canonical_contract(
        value: BaseModel,
        label: str,
    ) -> None:
        try:
            canonical = type(value).model_validate(value.model_dump(mode="json", by_alias=True))
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowAtomicityError(f"{label} is not canonical") from error
        if canonical != value:
            raise WorkflowAtomicityError(f"{label} changed during canonical validation")

    @staticmethod
    def _validate_output_contract_run(run: OutputContractRun) -> None:
        if type(run.attempts) is not tuple or any(
            not isinstance(result, OutputContractResult)
            or type(result.retry_allowed) is not bool
            or type(result.attempt) is not int
            or not isinstance(result.decision, GateDecision)
            or (
                result.extraction is not None and not isinstance(result.extraction, ModelExtraction)
            )
            for result in run.attempts
        ):
            raise WorkflowAtomicityError("G2 run contains a non-canonical attempt")
        for result in run.attempts:
            SqliteCaseRepository._require_canonical_contract(
                result.decision,
                "G2 GateDecision",
            )
            if result.extraction is not None:
                SqliteCaseRepository._require_canonical_contract(
                    result.extraction,
                    "G2 ModelExtraction",
                )
        try:
            canonical = OutputContractRun(attempts=run.attempts)
        except (ValueError, TypeError) as error:
            raise WorkflowAtomicityError("G2 run is not canonical") from error
        if canonical != run:
            raise WorkflowAtomicityError("G2 run changed during canonical validation")

    @staticmethod
    def _recompute_output_contract_run(
        attempts: tuple[OutputContractAttempt, ...],
        approved_evidence: tuple[EvidenceItem, ...],
    ) -> OutputContractRun:
        run = OutputContractRun()
        try:
            for attempt in attempts:
                result = evaluate_g2(
                    attempt.envelope,
                    approved_evidence=approved_evidence,
                    run=run,
                    decided_at=attempt.decided_at,
                )
                run = run.append(result)
        except (TypeError, ValueError) as error:
            raise WorkflowAtomicityError(
                "Raw G2 attempts do not form one canonical initial/retry run"
            ) from error
        SqliteCaseRepository._validate_output_contract_run(run)
        return run

    @staticmethod
    def _validate_safety_input(value: SafetyInput) -> None:
        if not isinstance(value, SafetyInput):
            raise WorkflowAtomicityError("safety_input must be SafetyInput or null")
        if any(
            type(flag) is not bool
            for flag in (
                value.injury_reported,
                value.immediate_danger,
                value.portal_is_sandbox,
                value.real_credentials_present,
            )
        ):
            raise WorkflowAtomicityError("SafetyInput booleans must be strict")
        if type(value.advice_categories) is not tuple or any(
            not isinstance(item, AdviceCategory) for item in value.advice_categories
        ):
            raise WorkflowAtomicityError("SafetyInput advice categories are invalid")
        if type(value.requested_actions) is not tuple or any(
            not isinstance(item, RequestedAction) for item in value.requested_actions
        ):
            raise WorkflowAtomicityError("SafetyInput requested actions are invalid")
        if value.model_signal is not None and not isinstance(
            value.model_signal,
            ModelSafetySignal,
        ):
            raise WorkflowAtomicityError("SafetyInput model signal is invalid")
        if type(value.evidence_refs) is not tuple or any(
            type(reference) is not str or _IDENTIFIER.fullmatch(reference) is None
            for reference in value.evidence_refs
        ):
            raise WorkflowAtomicityError("SafetyInput evidence refs are invalid")

    @classmethod
    def _validate_initial_extraction_binding(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        packet: ClaimPacket,
        extraction: ModelExtraction,
        approved_evidence: tuple[EvidenceItem, ...],
    ) -> None:
        """Bind G2 output to the persisted intake and deterministic narrative."""

        cls._validate_approved_evidence_binding(
            connection,
            current,
            approved_evidence,
        )

        packet_non_narrative_facts = tuple(
            fact for fact in packet.facts if fact.field is not EvidenceField.NARRATIVE
        )
        extraction_non_narrative_facts = tuple(
            fact for fact in extraction.facts if fact.field is not EvidenceField.NARRATIVE
        )
        packet_claim = packet.claim.model_dump(mode="json", by_alias=False)
        extraction_claim = extraction.claim.model_dump(mode="json", by_alias=False)
        packet_claim.pop("narrative")
        extraction_claim.pop("narrative")
        packet_claim["field_provenance"] = tuple(
            item
            for item in packet.claim.field_provenance
            if item.field is not RequiredClaimField.NARRATIVE
        )
        extraction_claim["field_provenance"] = tuple(
            item
            for item in extraction.claim.field_provenance
            if item.field is not RequiredClaimField.NARRATIVE
        )
        packet_claim["missing_required_fields"] = tuple(
            field
            for field in packet.claim.missing_required_fields
            if field is not RequiredClaimField.NARRATIVE
        )
        extraction_claim["missing_required_fields"] = tuple(
            field
            for field in extraction.claim.missing_required_fields
            if field is not RequiredClaimField.NARRATIVE
        )
        if (
            packet.evidence != approved_evidence
            or extraction.evidence != approved_evidence
            or packet.provenance != extraction.provenance
            or packet_non_narrative_facts != extraction_non_narrative_facts
            or packet_claim != extraction_claim
        ):
            raise WorkflowAtomicityError(
                "ClaimPacket non-narrative extraction fields must equal final canonical G2"
            )
        cls._validate_neutral_narrative(packet)

    @classmethod
    def _validate_approved_evidence_binding(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        approved_evidence: tuple[EvidenceItem, ...],
    ) -> None:
        """Bind every provider-visible evidence value to persisted intake authority."""

        authority_row = connection.execute(
            "SELECT manifest_json FROM case_intake_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        summary = current.snapshot.intake_summary
        if authority_row is None or summary is None:
            raise WorkflowAtomicityError("Initial analysis requires canonical intake authority")
        manifest = _load_json_object(
            _require_string(authority_row["manifest_json"], "intake manifest")
        )
        images = manifest.get("images")
        statement = summary.get("statement")
        if type(images) is not list or len(images) != 3 or type(statement) is not dict:
            raise WorkflowAtomicityError(
                "Persisted intake must contain exactly three images and one statement"
            )

        bound_images: list[tuple[str, str, str]] = []
        for image in images:
            if type(image) is not dict:
                raise WorkflowAtomicityError("Persisted image identity is invalid")
            source = image.get("model")
            if type(source) is not dict:
                raise WorkflowAtomicityError("Persisted image source identity is invalid")
            file_id = source.get("fileId")
            media_type = source.get("mediaType")
            digest = source.get("sha256")
            if (
                type(file_id) is not str
                or type(media_type) is not str
                or type(digest) is not str
                or _IDENTIFIER.fullmatch(file_id) is None
                or media_type not in {"image/jpeg", "image/png"}
                or _SHA256.fullmatch(digest) is None
            ):
                raise WorkflowAtomicityError("Persisted image source identity is invalid")
            bound_images.append((file_id, media_type, digest))

        if len(approved_evidence) != 4:
            raise WorkflowAtomicityError(
                "Approved evidence must contain only the staged three images and statement"
            )
        approved_images = tuple(
            item for item in approved_evidence if item.kind is EvidenceKind.IMAGE
        )
        if tuple(
            (item.local_ref, item.media_type, item.sha256) for item in approved_images
        ) != tuple(bound_images):
            raise WorkflowAtomicityError(
                "Approved image evidence does not match the G1 model-copy manifest"
            )
        if any(not item.model_copy_approved for item in approved_evidence):
            raise WorkflowAtomicityError(
                "Every provider-visible evidence item must be approved for model copy"
            )

        statement_file = statement.get("fileId")
        statement_media = statement.get("mediaType")
        statement_digest = statement.get("sha256")
        if (
            type(statement_file) is not str
            or _IDENTIFIER.fullmatch(statement_file) is None
            or statement_media != "text/plain"
            or type(statement_digest) is not str
            or _SHA256.fullmatch(statement_digest) is None
        ):
            raise WorkflowAtomicityError("Persisted statement identity is invalid")
        text_evidence = tuple(
            item for item in approved_evidence if item.kind is not EvidenceKind.IMAGE
        )
        if len(text_evidence) != 1:
            raise WorkflowAtomicityError(
                "Initial G2 extraction requires exactly one staged statement"
            )
        evidence = text_evidence[0]
        if (
            evidence.local_ref != statement_file
            or evidence.media_type != statement_media
            or evidence.sha256 != statement_digest
            or evidence.text is None
            or hashlib.sha256(evidence.text.encode("utf-8")).hexdigest() != statement_digest
        ):
            raise WorkflowAtomicityError(
                "Extracted statement content does not match its persisted content identity"
            )

        transcript_row = connection.execute(
            "SELECT * FROM case_transcripts WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if transcript_row is None:
            if manifest.get("audio") is not None:
                raise WorkflowAtomicityError(
                    "Audio intake cannot enter extraction without a bound transcript"
                )
            if (
                evidence.kind is not EvidenceKind.USER_STATEMENT
                or evidence.transcript_confirmed is not None
            ):
                raise WorkflowAtomicityError("Text intake must bind to user-statement evidence")
        else:
            transcript = cls._row_to_transcript(transcript_row)
            if (
                not transcript.confirmed
                or transcript.local_ref != evidence.local_ref
                or transcript.transcript_sha256 != evidence.sha256
                or evidence.kind is not EvidenceKind.TRANSCRIPT
                or evidence.transcript_confirmed is not True
            ):
                raise WorkflowAtomicityError(
                    "Transcript evidence must match the confirmed persisted transcript identity"
                )

    @staticmethod
    def _validate_neutral_narrative(packet: ClaimPacket) -> None:
        try:
            result = compose_neutral_narrative(
                NarrativeInput(
                    facts=packet.facts,
                    provenance=packet.provenance,
                    evidence=packet.evidence,
                )
            )
        except ValueError as error:
            raise WorkflowAtomicityError(
                "Claim narrative cannot be composed from canonical supported facts"
            ) from error
        if packet.claim.narrative != result.text:
            raise WorkflowAtomicityError(
                "Claim narrative must equal the deterministic neutral narrative"
            )
        narrative_facts = tuple(
            fact for fact in packet.facts if fact.field is EvidenceField.NARRATIVE
        )
        narrative_provenance = tuple(
            item
            for item in packet.claim.field_provenance
            if item.field is RequiredClaimField.NARRATIVE
        )
        if result.text is None:
            if narrative_facts or narrative_provenance:
                raise WorkflowAtomicityError(
                    "An empty neutral narrative cannot retain narrative support"
                )
            return
        if (
            len(narrative_facts) != 1
            or narrative_facts[0].value != result.text
            or narrative_facts[0].status is not FactStatus.USER_STATED
            or narrative_facts[0].source_refs != result.source_refs
            or narrative_facts[0].confidence is not None
            or len(narrative_provenance) != 1
            or narrative_provenance[0].source_refs != result.source_refs
        ):
            raise WorkflowAtomicityError(
                "Neutral narrative fact and claim provenance must equal its exact source union"
            )

    @staticmethod
    def _validate_safety_binding(
        packet: ClaimPacket,
        safety_input: SafetyInput,
    ) -> None:
        """Bind deterministically represented G3 facts to the final extraction."""

        if (
            packet.scope.environment != "sandbox"
            or safety_input.portal_is_sandbox is not True
            or safety_input.real_credentials_present is not False
        ):
            raise WorkflowAtomicityError(
                "G3 portal scope must remain the fixed credential-free sandbox boundary"
            )

        provenance_by_id = {reference.provenance_id: reference for reference in packet.provenance}
        evidence_by_id = {item.evidence_id: item for item in packet.evidence}
        values: dict[EvidenceField, tuple[bool, tuple[str, ...]]] = {}
        for field in (EvidenceField.INJURY_STATUS, EvidenceField.IMMEDIATE_DANGER):
            facts = tuple(
                fact
                for fact in packet.facts
                if fact.field is field and fact.status is FactStatus.USER_STATED
            )
            if (
                len(facts) != 1
                or type(facts[0].value) is not bool
                or not facts[0].source_refs
                or any(
                    not SqliteCaseRepository._is_authoritative_safety_source(
                        source,
                        provenance_by_id=provenance_by_id,
                        evidence_by_id=evidence_by_id,
                    )
                    for source in facts[0].source_refs
                )
            ):
                raise WorkflowAtomicityError(
                    f"G3 requires one user-stated, authoritative boolean {field.value} fact"
                )
            values[field] = (facts[0].value, facts[0].source_refs)
        if (
            safety_input.injury_reported is not values[EvidenceField.INJURY_STATUS][0]
            or safety_input.immediate_danger is not values[EvidenceField.IMMEDIATE_DANGER][0]
        ):
            raise WorkflowAtomicityError(
                "G3 injury and danger flags must equal the final G2 extraction"
            )
        expected_refs = tuple(
            dict.fromkeys(
                (
                    *values[EvidenceField.INJURY_STATUS][1],
                    *values[EvidenceField.IMMEDIATE_DANGER][1],
                )
            )
        )
        if safety_input.evidence_refs != expected_refs:
            raise WorkflowAtomicityError(
                "G3 evidence refs must equal the injury/danger provenance union"
            )

    @staticmethod
    def _is_authoritative_safety_source(
        source_ref: str,
        *,
        provenance_by_id: dict[str, ProvenanceRef],
        evidence_by_id: dict[str, EvidenceItem],
    ) -> bool:
        reference = provenance_by_id.get(source_ref)
        if reference is None:
            return False
        evidence = evidence_by_id.get(reference.evidence_id)
        if evidence is None or evidence.model_copy_approved is not True:
            return False
        if evidence.kind is EvidenceKind.USER_STATEMENT:
            return True
        if evidence.kind is EvidenceKind.TRANSCRIPT:
            return evidence.transcript_confirmed is True and reference.user_confirmed is True
        if evidence.kind is EvidenceKind.CLARIFICATION:
            return reference.user_confirmed is True
        return False

    @classmethod
    def _validate_clarification_answer_delta(
        cls,
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
    ) -> None:
        """Permit one exact answer-bound evidence/fact/claim mutation only."""

        answer = command.clarification_answer
        prior = current.snapshot.claim_packet
        target = command.claim_packet
        try:
            active = ClarificationView.model_validate(current.snapshot.active_clarification)
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowAtomicityError(
                "Clarification continuation requires a canonical active view"
            ) from error
        if answer is None or prior is None or target is None:
            raise WorkflowAtomicityError(
                "Clarification continuation requires its exact answer and packet delta"
            )
        if (
            answer.case_id != current.case_id
            or answer.clarification_id != active.clarification_id
            or answer.field is not active.field
            or answer.round != active.round
            or answer.expected_version != current.version
        ):
            raise WorkflowAtomicityError(
                "Clarification answer must bind the active id, field, round, and version"
            )
        if answer.field in {
            RequiredClaimField.ATTACHMENTS,
            RequiredClaimField.NARRATIVE,
        }:
            raise WorkflowAtomicityError(
                "Attachments and free-form narratives cannot be supplied by clarification text"
            )
        if target.scope != prior.scope:
            raise WorkflowAtomicityError("Clarification cannot change the immutable ClaimScope")

        raw_answer = answer.answer
        digest = hashlib.sha256(raw_answer.encode("utf-8")).hexdigest()
        identity = hashlib.sha256(
            (
                "claimdone-clarification-v1\0"
                f"{current.case_id}\0{active.clarification_id}\0"
                f"{active.round}\0{digest}"
            ).encode()
        ).hexdigest()
        expected_evidence_id = f"clarification-{identity[:32]}"
        expected_local_ref = f"clarification-{identity[:32]}.txt"
        expected_provenance_id = f"provenance-{identity[:32]}"
        expected_fact_id = f"fact-{identity[:32]}"

        if (
            len(target.evidence) != len(prior.evidence) + 1
            or target.evidence[:-1] != prior.evidence
        ):
            raise WorkflowAtomicityError(
                "Clarification may append exactly one evidence item and preserve its prefix"
            )
        appended_evidence = target.evidence[-1]
        if (
            appended_evidence.evidence_id != expected_evidence_id
            or appended_evidence.kind is not EvidenceKind.CLARIFICATION
            or appended_evidence.local_ref != expected_local_ref
            or appended_evidence.media_type != "text/plain"
            or appended_evidence.sha256 != digest
            or appended_evidence.text != raw_answer
            or appended_evidence.model_copy_approved is not True
            or appended_evidence.transcript_confirmed is not None
        ):
            raise WorkflowAtomicityError(
                "Appended clarification evidence must equal the exact answer identity"
            )
        if (
            len(target.provenance) != len(prior.provenance) + 1
            or target.provenance[:-1] != prior.provenance
        ):
            raise WorkflowAtomicityError(
                "Clarification may append exactly one provenance ref and preserve its prefix"
            )
        appended_provenance = target.provenance[-1]
        if (
            appended_provenance.provenance_id != expected_provenance_id
            or appended_provenance.evidence_id != expected_evidence_id
            or appended_provenance.locator != "clarification answer"
            or appended_provenance.user_confirmed is not True
        ):
            raise WorkflowAtomicityError(
                "Appended clarification provenance must bind the exact answer evidence"
            )

        claim_value, fact_value = cls._parse_clarification_answer(
            answer.field,
            raw_answer,
        )
        evidence_field = EvidenceField(answer.field.value)
        target_facts = tuple(fact for fact in target.facts if fact.field is evidence_field)
        if (
            len(target_facts) != 1
            or target_facts[0].fact_id != expected_fact_id
            or target_facts[0].value != fact_value
            or target_facts[0].status is not FactStatus.USER_STATED
            or target_facts[0].source_refs != (expected_provenance_id,)
            or target_facts[0].confidence is not None
        ):
            raise WorkflowAtomicityError(
                "Clarification must replace only its target field with one answer-bound fact"
            )
        immutable_prior_facts = tuple(
            fact
            for fact in prior.facts
            if fact.field not in {evidence_field, EvidenceField.NARRATIVE}
        )
        immutable_target_facts = tuple(
            fact
            for fact in target.facts
            if fact.field not in {evidence_field, EvidenceField.NARRATIVE}
        )
        if immutable_target_facts != immutable_prior_facts:
            raise WorkflowAtomicityError(
                "Clarification cannot change facts outside its field and derived narrative"
            )

        claim_attributes = (
            RequiredClaimField.INCIDENT_DATE,
            RequiredClaimField.INCIDENT_TIME,
            RequiredClaimField.LOCATION,
            RequiredClaimField.CLAIMANT_NAME,
            RequiredClaimField.POLICY_REFERENCE,
            RequiredClaimField.VEHICLE_REGISTRATION,
            RequiredClaimField.COUNTERPARTY_KNOWN,
            RequiredClaimField.ATTACHMENTS,
        )
        for field in claim_attributes:
            if field is answer.field:
                continue
            if getattr(target.claim, field.value) != getattr(prior.claim, field.value):
                raise WorkflowAtomicityError("Clarification cannot change unrelated claim fields")
        if getattr(target.claim, answer.field.value) != claim_value:
            raise WorkflowAtomicityError(
                "Clarification claim value must equal the canonical parsed answer"
            )

        immutable_prior_provenance = tuple(
            item
            for item in prior.claim.field_provenance
            if item.field not in {answer.field, RequiredClaimField.NARRATIVE}
        )
        immutable_target_provenance = tuple(
            item
            for item in target.claim.field_provenance
            if item.field not in {answer.field, RequiredClaimField.NARRATIVE}
        )
        if immutable_target_provenance != immutable_prior_provenance:
            raise WorkflowAtomicityError(
                "Clarification cannot change unrelated claim-field provenance"
            )
        target_field_provenance = tuple(
            item for item in target.claim.field_provenance if item.field is answer.field
        )
        if len(target_field_provenance) != 1 or target_field_provenance[0].source_refs != (
            expected_provenance_id,
        ):
            raise WorkflowAtomicityError(
                "Clarification target field must use only its answer provenance"
            )
        cls._validate_neutral_narrative(target)

    @staticmethod
    def _parse_clarification_answer(
        field: RequiredClaimField,
        raw_answer: str,
    ) -> tuple[object, str]:
        value = raw_answer.strip()
        try:
            if field is RequiredClaimField.INCIDENT_DATE:
                parsed_date = date.fromisoformat(value)
                return parsed_date, parsed_date.isoformat()
            if field is RequiredClaimField.INCIDENT_TIME:
                parsed_time = time.fromisoformat(value)
                return parsed_time, parsed_time.isoformat()
            if field is RequiredClaimField.COUNTERPARTY_KNOWN:
                parsed_counterparty = CounterpartyKnown(value.lower())
                return parsed_counterparty, parsed_counterparty.value
        except ValueError as error:
            raise WorkflowAtomicityError(
                f"Clarification answer is invalid for {field.value}"
            ) from error
        if field in {
            RequiredClaimField.LOCATION,
            RequiredClaimField.CLAIMANT_NAME,
            RequiredClaimField.POLICY_REFERENCE,
            RequiredClaimField.VEHICLE_REGISTRATION,
        }:
            return value, value
        raise WorkflowAtomicityError(f"Clarification field {field.value} has no safe text parser")

    @classmethod
    def _validate_analysis_authority(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        existing_gates: tuple[GateDecision, ...],
    ) -> _AnalysisAuthority:
        """Derive, rather than trust, every G2-G5 decision and target state."""

        cls._validate_analysis_history(current, existing_gates)
        if current.state is CaseState.ANALYZING:
            return cls._validate_initial_analysis_authority(
                connection,
                current,
                command,
                existing_gates=existing_gates,
            )
        return cls._validate_clarification_authority(current, command)

    @classmethod
    def _validate_initial_analysis_authority(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        existing_gates: tuple[GateDecision, ...],
    ) -> _AnalysisAuthority:
        if command.clarification_answer is not None:
            raise WorkflowAtomicityError("Initial analysis cannot contain a clarification answer")
        cls._validate_approved_evidence_binding(
            connection,
            current,
            command.approved_evidence,
        )
        run = cls._recompute_output_contract_run(
            command.g2_attempts,
            command.approved_evidence,
        )
        final = run.final_result
        if final is None:
            raise WorkflowAtomicityError("Initial analysis requires a final G2 result")
        emitted = command.gate_decisions
        if not emitted or emitted[0] != final.decision:
            raise WorkflowAtomicityError(
                "The emitted G2 decision must equal the final canonical G2 result"
            )
        cls._validate_analysis_provider_events(current, command, g2_run=run)

        authoritative: list[GateDecision] = [final.decision]
        provenance: ProvenanceResult | None = None
        completeness: CompletenessResult | None = None
        packet = command.claim_packet
        if not final.decision.passed:
            if packet is not None or command.safety_input is not None:
                raise WorkflowAtomicityError(
                    "A failed G2 cannot expose a ClaimPacket or downstream safety input"
                )
            expected_target = CaseState.BLOCKED
        else:
            if final.extraction is None or packet is None:
                raise WorkflowAtomicityError(
                    "A passed G2 requires its extracted target ClaimPacket"
                )
            cls._validate_initial_extraction_binding(
                connection,
                current,
                packet,
                final.extraction,
                command.approved_evidence,
            )
            safety_input = command.safety_input
            if safety_input is None:
                raise WorkflowAtomicityError("A passed G2 requires authoritative G3 input")
            cls._validate_safety_binding(packet, safety_input)
            g3_event = cls._require_emitted_gate(emitted, 1, GateId.G3_SAFETY_SCOPE)
            safety = evaluate_g3(safety_input, decided_at=g3_event.decided_at)
            if g3_event != safety.decision:
                raise WorkflowAtomicityError(
                    "The emitted G3 decision must equal recomputed SafetyInput"
                )
            authoritative.append(safety.decision)
            if not safety.decision.passed:
                expected_target = (
                    CaseState.EMERGENCY_STOPPED if safety.emergency_stop else CaseState.BLOCKED
                )
            else:
                g4_event = cls._require_emitted_gate(emitted, 2, GateId.G4_PROVENANCE)
                provenance = evaluate_g4(packet, decided_at=g4_event.decided_at)
                if g4_event != provenance.decision:
                    raise WorkflowAtomicityError(
                        "The emitted G4 decision must equal recomputed packet provenance"
                    )
                authoritative.append(provenance.decision)
                if not provenance.decision.passed and not cls._is_exclusive_g4_conflict(provenance):
                    expected_target = CaseState.BLOCKED
                else:
                    g5_event = cls._require_emitted_gate(
                        emitted,
                        3,
                        GateId.G5_COMPLETENESS,
                    )
                    completeness = cls._derive_completeness(
                        provenance,
                        completed_rounds=0,
                        decided_at=g5_event.decided_at,
                    )
                    if g5_event != completeness.decision:
                        raise WorkflowAtomicityError(
                            "The emitted G5 decision must equal recomputed completeness"
                        )
                    authoritative.append(completeness.decision)
                    expected_target = cls._target_for_completeness(completeness)

        cls._validate_derived_analysis_target(
            current,
            command,
            expected_target=expected_target,
            authoritative=tuple(authoritative),
        )
        return _AnalysisAuthority(
            effective_gates=(*existing_gates, *authoritative),
            provenance=provenance,
            completeness=completeness,
        )

    @classmethod
    def _validate_clarification_authority(
        cls,
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
    ) -> _AnalysisAuthority:
        if command.g2_attempts or command.approved_evidence or command.safety_input is not None:
            raise WorkflowAtomicityError(
                "Clarification continuation cannot rerun G2/G3 or a provider"
            )
        cls._validate_analysis_provider_events(current, command, g2_run=None)
        cls._validate_clarification_answer_delta(current, command)
        packet = command.claim_packet
        prior_packet = current.snapshot.claim_packet
        if packet is None or prior_packet is None:
            raise WorkflowAtomicityError(
                "Clarification continuation requires prior and target ClaimPackets"
            )
        stored_active = ClarificationView.model_validate(current.snapshot.active_clarification)
        emitted = command.gate_decisions
        g4_event = cls._require_emitted_gate(emitted, 0, GateId.G4_PROVENANCE)
        provenance = evaluate_g4(packet, decided_at=g4_event.decided_at)
        if g4_event != provenance.decision:
            raise WorkflowAtomicityError(
                "The emitted G4 decision must equal recomputed clarification provenance"
            )
        authoritative: list[GateDecision] = [provenance.decision]
        completeness: CompletenessResult | None = None
        if not provenance.decision.passed and not cls._is_exclusive_g4_conflict(provenance):
            expected_target = CaseState.BLOCKED
        else:
            g5_event = cls._require_emitted_gate(emitted, 1, GateId.G5_COMPLETENESS)
            completeness = cls._derive_completeness(
                provenance,
                completed_rounds=stored_active.round,
                decided_at=g5_event.decided_at,
            )
            if g5_event != completeness.decision:
                raise WorkflowAtomicityError(
                    "The emitted G5 decision must equal recomputed clarification completeness"
                )
            authoritative.append(completeness.decision)
            expected_target = cls._target_for_completeness(completeness)

        cls._validate_derived_analysis_target(
            current,
            command,
            expected_target=expected_target,
            authoritative=tuple(authoritative),
        )
        return _AnalysisAuthority(
            effective_gates=(*prior_packet.gate_decisions[:4], *authoritative),
            provenance=provenance,
            completeness=completeness,
        )

    @staticmethod
    def _require_emitted_gate(
        emitted: tuple[GateDecision, ...],
        index: int,
        gate_id: GateId,
    ) -> GateDecision:
        if index >= len(emitted) or emitted[index].gate_id is not gate_id:
            raise WorkflowAtomicityError(
                f"Analysis requires authoritative {gate_id.value} at emitted position {index}"
            )
        return emitted[index]

    @staticmethod
    def _validate_derived_analysis_target(
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        expected_target: CaseState,
        authoritative: tuple[GateDecision, ...],
    ) -> None:
        if command.gate_decisions != authoritative:
            raise WorkflowAtomicityError(
                "Analysis gate suffix must equal the complete authoritative result chain"
            )
        if command.target is not expected_target:
            raise WorkflowAtomicityError(
                f"Authoritative gates require target {expected_target.value}"
            )
        emitted_times = tuple(decision.decided_at for decision in authoritative)
        if tuple(sorted(emitted_times)) != emitted_times:
            raise WorkflowAtomicityError("New analysis gate timestamps must be monotonic")
        if emitted_times[0] < current.updated_at or emitted_times[-1] > command.updated_at:
            raise WorkflowAtomicityError(
                "New analysis gate timestamps must fall within the atomic command window"
            )

    @classmethod
    def _validate_analysis_history(
        cls,
        current: CaseRecord,
        history: tuple[GateDecision, ...],
    ) -> None:
        timestamps = tuple(decision.decided_at for decision in history)
        if tuple(sorted(timestamps)) != timestamps or (
            timestamps and timestamps[-1] > current.updated_at
        ):
            raise WorkflowAtomicityError(
                "Persisted analysis gate history has a non-monotonic chronology"
            )
        if current.state is CaseState.ANALYZING:
            if tuple(decision.gate_id for decision in history) != _ANALYSIS_GATE_SEQUENCE[
                :2
            ] or any(not decision.passed for decision in history):
                raise WorkflowAtomicityError(
                    "Initial analysis requires exactly one persisted passed G0/G1 prefix"
                )
            return

        packet = current.snapshot.claim_packet
        active_payload = current.snapshot.active_clarification
        if packet is None or active_payload is None:
            raise WorkflowAtomicityError(
                "Clarification history requires its packet and active view"
            )
        try:
            active = ClarificationView.model_validate(active_payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowAtomicityError(
                "Persisted active clarification is not canonical"
            ) from error
        if len(history) < 6 or (len(history) - 4) % 2:
            raise WorkflowAtomicityError(
                "Clarification history must contain a G0-G3 prefix and complete G4/G5 pairs"
            )
        prefix = history[:4]
        if tuple(decision.gate_id for decision in prefix) != _ANALYSIS_GATE_SEQUENCE[:4] or any(
            not decision.passed for decision in prefix
        ):
            raise WorkflowAtomicityError(
                "Clarification history requires one immutable passed G0-G3 prefix"
            )
        pairs = tuple((history[index], history[index + 1]) for index in range(4, len(history), 2))
        if len(pairs) != active.round:
            raise WorkflowAtomicityError(
                "Clarification history pair count must equal the active round"
            )
        for g4, g5 in pairs:
            if g4.gate_id is not GateId.G4_PROVENANCE or g5.gate_id is not GateId.G5_COMPLETENESS:
                raise WorkflowAtomicityError(
                    "Clarification history may contain only complete G4/G5 pairs"
                )
            g4_continues = g4.passed or g4.reason_codes == (GateReasonCode.G4_CONFLICTING_SOURCES,)
            if not g4_continues or g5.reason_codes != (GateReasonCode.G5_REQUIRED_FIELD_MISSING,):
                raise WorkflowAtomicityError(
                    "Only passed G4 or exclusive G4 conflict plus clarifiable G5 may continue"
                )
        latest = (*prefix, *pairs[-1])
        if packet.gate_decisions != latest:
            raise WorkflowAtomicityError(
                "Clarification packet must equal the latest exact persisted G0-G5 set"
            )
        latest_g4, latest_g5 = pairs[-1]
        provenance = evaluate_g4(packet, decided_at=latest_g4.decided_at)
        completeness = cls._derive_completeness(
            provenance,
            completed_rounds=active.round - 1,
            decided_at=latest_g5.decided_at,
        )
        accepted = completeness.accepted_question
        if (
            provenance.decision != latest_g4
            or completeness.decision != latest_g5
            or accepted is None
            or active.field is not accepted.field
            or active.question != accepted.text
        ):
            raise WorkflowAtomicityError(
                "Active clarification is not bound to deterministic G4/G5 authority"
            )

    @staticmethod
    def _is_exclusive_g4_conflict(provenance: ProvenanceResult) -> bool:
        return provenance.decision.reason_codes == (
            GateReasonCode.G4_CONFLICTING_SOURCES,
        ) and bool(provenance.conflicting_fields)

    @staticmethod
    def _derive_completeness(
        provenance: ProvenanceResult,
        *,
        completed_rounds: int,
        decided_at: datetime,
    ) -> CompletenessResult:
        blockers = set(compute_missing_required_fields(provenance.claim)) | set(
            provenance.conflicting_fields
        )
        ordered = tuple(field for field in RequiredClaimField if field in blockers)
        questions: tuple[ClarificationQuestion, ...] = ()
        if (
            ordered
            and ordered[0] in _TEXT_CLARIFIABLE_FIELDS
            and completed_rounds < MAX_CLARIFICATION_ROUNDS
        ):
            field = ordered[0]
            questions = (ClarificationQuestion(field, _QUESTION_BY_FIELD[field]),)
        return evaluate_g5(
            provenance,
            proposed_questions=questions,
            completed_rounds=completed_rounds,
            decided_at=decided_at,
        )

    @staticmethod
    def _target_for_completeness(completeness: CompletenessResult) -> CaseState:
        if completeness.decision.passed:
            return CaseState.READY_TO_FILL
        if completeness.accepted_question is not None:
            return CaseState.AWAITING_CLARIFICATION
        if completeness.manual_handoff:
            return CaseState.BLOCKED
        if (
            completeness.blocking_fields
            and completeness.accepted_question is None
            and GateReasonCode.G5_QUESTION_INVALID in completeness.decision.reason_codes
        ):
            return CaseState.BLOCKED
        raise WorkflowAtomicityError(
            "Deterministic fixed-question G5 produced no authorized target"
        )

    @staticmethod
    def _validate_analysis_provider_events(
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        g2_run: OutputContractRun | None,
    ) -> None:
        emissions = command.provider_events
        if current.state is CaseState.AWAITING_CLARIFICATION:
            if emissions or g2_run is not None:
                raise WorkflowAtomicityError(
                    "Clarification continuation is deterministic and cannot call a provider"
                )
            return
        if g2_run is None:
            raise WorkflowAtomicityError("Initial provider telemetry requires its G2 run")
        attempts = g2_run.attempts
        expected_emissions = 1 if len(attempts) == 1 else 3
        if len(attempts) not in {1, 2} or len(emissions) != expected_emissions:
            raise WorkflowAtomicityError(
                "Provider emissions must exactly match one or two completed G2 attempts"
            )
        if len(emissions) not in {1, 3}:
            raise WorkflowAtomicityError(
                "Analysis requires one provider call or call/retry/call telemetry"
            )
        events = tuple(emission.event for emission in emissions)
        if any(
            not isinstance(event, ProviderCallWorkflowEvent | RetryWorkflowEvent)
            for event in events
        ):
            raise WorkflowAtomicityError(
                "Analysis provider emissions allow only provider-call and retry events"
            )
        occurred = tuple(emission.occurred_at for emission in emissions)
        if any(value.utcoffset() is None for value in occurred):
            raise WorkflowAtomicityError("Provider timestamps must include a timezone")
        if tuple(sorted(occurred)) != occurred:
            raise WorkflowAtomicityError("Provider timestamps must be monotonic")
        first_gate_at = command.gate_decisions[0].decided_at
        if occurred[0] < current.updated_at or occurred[-1] > first_gate_at:
            raise WorkflowAtomicityError(
                "Provider events must precede gates inside the atomic command window"
            )
        if any(event.operation is not WorkflowOperation.EXTRACTION for event in events):
            raise WorkflowAtomicityError("Analysis provider events must be extraction calls")

        if len(events) == 1:
            event = events[0]
            if (
                not isinstance(event, ProviderCallWorkflowEvent)
                or event.call_sequence != 1
                or event.retry_attempt != 0
            ):
                raise WorkflowAtomicityError(
                    "A single analysis provider event must be the initial successful call"
                )
            if attempts[0].attempt != 0 or attempts[0].decision != command.gate_decisions[0]:
                raise WorkflowAtomicityError(
                    "The single provider call must bind the final attempt-zero G2 decision"
                )
            if emissions[0].occurred_at > attempts[0].decision.decided_at:
                raise WorkflowAtomicityError("G2 cannot predate its completed provider call")
            return

        first, retry, succeeded = events
        if (
            not isinstance(first, ProviderCallWorkflowEvent)
            or not isinstance(retry, RetryWorkflowEvent)
            or not isinstance(succeeded, ProviderCallWorkflowEvent)
        ):
            raise WorkflowAtomicityError(
                "Retry telemetry must be provider_call(attempt0), retry, provider_call(attempt1)"
            )
        if retry.failure.category is not ProviderFailureCategory.INVALID_RESPONSE:
            raise WorkflowAtomicityError(
                "The analysis retry is authorized only for a deterministic invalid response"
            )
        if (
            first.call_sequence != 1
            or first.retry_attempt != 0
            or retry.call_sequence != first.call_sequence
            or retry.model_id is not first.model_id
            or retry.provider_mode != first.provider_mode
            or succeeded.call_sequence != first.call_sequence + 1
            or succeeded.retry_attempt != 1
            or succeeded.model_id is not first.model_id
            or succeeded.provider_mode != first.provider_mode
            or retry.duration_ms != first.duration_ms
        ):
            raise WorkflowAtomicityError(
                "Retry and successful provider telemetry must be contiguous and identically bound"
            )
        initial_attempt, final_attempt = attempts
        if (
            initial_attempt.attempt != 0
            or initial_attempt.decision.passed
            or not initial_attempt.retry_allowed
            or final_attempt.attempt != 1
            or final_attempt.decision != command.gate_decisions[0]
        ):
            raise WorkflowAtomicityError(
                "Retry telemetry requires the exact canonical failed-then-final G2 run"
            )
        if (
            emissions[0].occurred_at > initial_attempt.decision.decided_at
            or initial_attempt.decision.decided_at > emissions[1].occurred_at
            or emissions[2].occurred_at > final_attempt.decision.decided_at
        ):
            raise WorkflowAtomicityError(
                "Provider, retry, and G2 attempt timestamps must be causally ordered"
            )

    @staticmethod
    def _validate_analysis_clarification(
        current: CaseRecord,
        command: AnalysisWorkflowCommand,
        *,
        completeness: CompletenessResult | None,
    ) -> None:
        stored_active: ClarificationView | None = None
        if current.snapshot.active_clarification is not None:
            try:
                stored_active = ClarificationView.model_validate(
                    current.snapshot.active_clarification
                )
            except ValidationError as error:
                raise WorkflowAtomicityError(
                    "Persisted active clarification is not canonical"
                ) from error
        if current.state is CaseState.ANALYZING and stored_active is not None:
            raise WorkflowAtomicityError("analyzing cannot retain an active clarification")
        if current.state is CaseState.AWAITING_CLARIFICATION:
            if stored_active is None or stored_active.expected_version != current.version:
                raise WorkflowAtomicityError(
                    "awaiting_clarification requires its version-bound active view"
                )
            if current.snapshot.claim_packet is None:
                raise WorkflowAtomicityError(
                    "awaiting_clarification requires its stored ClaimPacket"
                )

        active = command.active_clarification
        events = command.clarification_events
        if command.target is CaseState.AWAITING_CLARIFICATION:
            accepted = None if completeness is None else completeness.accepted_question
            if active is None or accepted is None:
                raise WorkflowAtomicityError(
                    "awaiting_clarification requires the exact G5-accepted question"
                )
            if (
                active.case_id != current.case_id
                or active.expected_version != current.version + 1
                or active.requested_at != command.updated_at
                or active.status is not ClarificationStatus.REQUESTED
                or active.field is not accepted.field
                or active.question != accepted.text
            ):
                raise WorkflowAtomicityError(
                    "ClarificationView must bind the fixed server question, version, and timestamp"
                )
            if current.state is CaseState.ANALYZING:
                if (
                    active.round != 1
                    or len(events) != 1
                    or (
                        events[0].status is not ClarificationStatus.REQUESTED
                        or events[0].field is not active.field
                        or events[0].round != active.round
                    )
                ):
                    raise WorkflowAtomicityError(
                        "Initial clarification must request exactly round one"
                    )
            else:
                assert stored_active is not None
                if (
                    stored_active.round >= MAX_CLARIFICATION_ROUNDS
                    or active.round != stored_active.round + 1
                ):
                    raise WorkflowAtomicityError(
                        "Clarification continuation must advance by one bounded round"
                    )
                if len(events) != 2 or (
                    events[0].status is not ClarificationStatus.CONFIRMED
                    or events[0].field is not stored_active.field
                    or events[0].round != stored_active.round
                    or events[1].status is not ClarificationStatus.REQUESTED
                    or events[1].field is not active.field
                    or events[1].round != active.round
                ):
                    raise WorkflowAtomicityError(
                        "Clarification continuation must confirm old and request new round"
                    )
            return

        if active is not None:
            raise WorkflowAtomicityError(
                "A closed analysis target cannot expose an active clarification"
            )
        if current.state is CaseState.ANALYZING:
            if events:
                raise WorkflowAtomicityError(
                    "An analyzing case has no clarification lifecycle to close"
                )
            return
        assert stored_active is not None
        expected_status = ClarificationStatus.CONFIRMED
        if (
            command.target is CaseState.BLOCKED
            and completeness is not None
            and completeness.manual_handoff
            and stored_active.round == MAX_CLARIFICATION_ROUNDS
        ):
            expected_status = ClarificationStatus.EXHAUSTED
        if len(events) != 1 or (
            events[0].status is not expected_status
            or events[0].field is not stored_active.field
            or events[0].round != stored_active.round
        ):
            raise WorkflowAtomicityError(
                "Clarification close event must match the stored active view"
            )

    @classmethod
    def _validate_terminal_provider_failure(
        cls,
        connection: sqlite3.Connection,
        current: CaseRecord,
        command: TerminalProviderFailureCommand,
        *,
        intake_manifest: JsonObject,
    ) -> CaseSnapshot:
        if command.case_id != current.case_id:
            raise WorkflowAtomicityError("Provider failure caseId is not current")
        if not isinstance(command.event, OperationalFailureWorkflowEvent):
            raise WorkflowAtomicityError(
                "Terminal provider command requires OperationalFailureWorkflowEvent"
            )
        if command.occurred_at.utcoffset() is None:
            raise WorkflowAtomicityError("Provider failure timestamp must include a timezone")
        if command.occurred_at < current.updated_at:
            raise WorkflowAtomicityError("Provider failure timestamp cannot move backwards")
        required_state = {
            WorkflowOperation.TRANSCRIPTION: CaseState.DISCLOSED,
            WorkflowOperation.EXTRACTION: CaseState.ANALYZING,
            WorkflowOperation.COMPUTER_USE: CaseState.FILLING,
            WorkflowOperation.VERIFICATION: CaseState.VERIFYING,
        }[command.event.operation]
        if current.state is not required_state:
            raise WorkflowAtomicityError(
                f"{command.event.operation.value} provider failure requires "
                f"case state {required_state.value}"
            )
        if (
            command.event.operation is WorkflowOperation.TRANSCRIPTION
            and intake_manifest.get("inputMode") != "audio"
        ):
            raise WorkflowAtomicityError(
                "Transcription provider failure requires canonical audio intake"
            )
        if command.event.operation is WorkflowOperation.EXTRACTION:
            cls._validate_approved_evidence_binding(
                connection,
                current,
                command.approved_evidence,
            )
            g2_run = cls._recompute_output_contract_run(
                command.g2_attempts,
                command.approved_evidence,
            )
        else:
            if command.approved_evidence or command.g2_attempts:
                raise WorkflowAtomicityError(
                    "Only extraction provider failures may carry G2 inputs"
                )
            g2_run = OutputContractRun()
        prefix = command.provider_events
        if type(prefix) is not tuple or any(
            not isinstance(emission, ProviderWorkflowEmission) for emission in prefix
        ):
            raise WorkflowAtomicityError(
                "Provider failure prefix must use ProviderWorkflowEmission"
            )
        if not prefix:
            if command.event.call_sequence != 1 or command.event.retry_attempt != 0:
                raise WorkflowAtomicityError(
                    "A first-call terminal failure must use callSequence one and retryAttempt zero"
                )
            if command.event.operation is WorkflowOperation.EXTRACTION and g2_run.attempts:
                raise WorkflowAtomicityError(
                    "A first-call extraction failure requires an empty canonical G2 run"
                )
        else:
            if len(prefix) != 2:
                raise WorkflowAtomicityError(
                    "Terminal retry prefix must be provider_call(attempt0) then retry"
                )
            first_emission, retry_emission = prefix
            first = first_emission.event
            retry = retry_emission.event
            if not isinstance(first, ProviderCallWorkflowEvent) or not isinstance(
                retry, RetryWorkflowEvent
            ):
                raise WorkflowAtomicityError(
                    "Terminal retry prefix must be provider_call(attempt0) then retry"
                )
            if (
                first_emission.occurred_at.utcoffset() is None
                or retry_emission.occurred_at.utcoffset() is None
                or first_emission.occurred_at < current.updated_at
                or retry_emission.occurred_at < first_emission.occurred_at
                or retry_emission.occurred_at > command.occurred_at
            ):
                raise WorkflowAtomicityError("Terminal retry prefix timestamps must be monotonic")
            if (
                first.operation is not WorkflowOperation.EXTRACTION
                or first.call_sequence != 1
                or first.retry_attempt != 0
                or retry.operation is not WorkflowOperation.EXTRACTION
                or retry.failure.category is not ProviderFailureCategory.INVALID_RESPONSE
                or retry.call_sequence != first.call_sequence
                or retry.model_id is not first.model_id
                or retry.provider_mode != first.provider_mode
                or retry.duration_ms != first.duration_ms
                or command.event.operation is not WorkflowOperation.EXTRACTION
                or command.event.call_sequence != first.call_sequence + 1
                or command.event.retry_attempt != 1
                or command.event.model_id is not first.model_id
                or command.event.provider_mode != first.provider_mode
            ):
                raise WorkflowAtomicityError(
                    "Terminal retry prefix and attempt-one failure are not exactly bound"
                )
            run = g2_run
            if len(run.attempts) != 1:
                raise WorkflowAtomicityError(
                    "A terminal extraction retry requires its single failed G2 attempt"
                )
            failed_attempt = run.attempts[0]
            if (
                failed_attempt.attempt != 0
                or failed_attempt.decision.passed
                or not failed_attempt.retry_allowed
                or failed_attempt.extraction is not None
                or first_emission.occurred_at > failed_attempt.decision.decided_at
                or failed_attempt.decision.decided_at > retry_emission.occurred_at
            ):
                raise WorkflowAtomicityError(
                    "Terminal retry telemetry must equal and follow its canonical failed G2 attempt"
                )
        try:
            validate_case_transition(current.state, CaseState.FAILED)
        except ValueError as error:
            raise WorkflowAtomicityError(str(error)) from error

        current_packet = current.snapshot.claim_packet
        target_packet = command.claim_packet
        if (current_packet is None) is not (target_packet is None):
            raise WorkflowAtomicityError("Provider failure must preserve ClaimPacket presence")
        if current_packet is not None and target_packet is not None:
            current_json = current_packet.model_dump(mode="json", by_alias=True)
            target_json = target_packet.model_dump(mode="json", by_alias=True)
            current_json["state"] = CaseState.FAILED.value
            if target_json != current_json:
                raise WorkflowAtomicityError(
                    "Provider failure may change only ClaimPacket.state to failed"
                )

        snapshot = replace(
            current.snapshot,
            claim_packet=target_packet,
            active_clarification=None,
        )
        try:
            _validate_snapshot(current.case_id, CaseState.FAILED, snapshot)
        except ValueError as error:
            raise WorkflowAtomicityError(str(error)) from error
        return snapshot

    @staticmethod
    def _validate_state_event(
        event: AuditEvent,
        *,
        current: CaseRecord,
        target: CaseState,
    ) -> None:
        if event.case_id != current.case_id:
            raise ValueError("State audit event must belong to the mutated case")
        if event.event_type is not AuditEventType.CASE_STATE_CHANGED:
            raise ValueError("State mutation requires a case_state_changed audit event")
        if event.from_state is not current.state or event.to_state is not target:
            raise ValueError("State audit event must match the persisted transition")

    @staticmethod
    def _insert_gate_decision_row(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        decision: GateDecision,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO gate_decisions (
                case_id, gate_id, decided_at, decision_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                case_id,
                decision.gate_id.value,
                decision.decided_at.isoformat(),
                decision.model_dump_json(by_alias=True),
            ),
        )
        if cursor.lastrowid is None:
            raise PersistenceError("Gate decision sequence was not allocated")
        return int(cursor.lastrowid)

    @staticmethod
    def _read_gate_decisions(
        connection: sqlite3.Connection,
        *,
        case_id: str,
    ) -> tuple[GateDecision, ...]:
        rows = connection.execute(
            """
            SELECT decision_json
            FROM gate_decisions
            WHERE case_id = ?
            ORDER BY sequence ASC
            """,
            (case_id,),
        ).fetchall()
        try:
            return tuple(
                GateDecision.model_validate_json(
                    _require_string(row["decision_json"], "gate decision")
                )
                for row in rows
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise PersistedDataIntegrityError("Persisted gate history is invalid") from error

    def _insert_redacted_workflow_event(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: str,
        event: AppendableWorkflowEvent,
        actor: ActorType,
        occurred_at: datetime,
    ) -> WorkflowEventEnvelope:
        audit_type = AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND[event.kind]
        audit = AuditEvent.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "eventId": f"event_{uuid4().hex}",
                "caseId": case_id,
                "eventType": audit_type,
                "actor": actor,
                "occurredAt": occurred_at,
                "fromState": None,
                "toState": None,
                "reasonCodes": (),
                "details": (),
            }
        )
        audit_sequence = self._insert_audit_event(connection, audit)
        return self._insert_workflow_projection(
            connection,
            audit_sequence=audit_sequence,
            audit=audit,
            event=event,
        )

    @staticmethod
    def _insert_audit_event(connection: sqlite3.Connection, event: AuditEvent) -> int:
        if event.details:
            raise ValueError("Backend workflow audit events must not contain free-form details")
        cursor = connection.execute(
            """
            INSERT INTO audit_events (event_id, case_id, occurred_at, event_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.case_id,
                _dump_aware_datetime(event.occurred_at, "audit occurred_at"),
                event.model_dump_json(by_alias=True),
            ),
        )
        if cursor.lastrowid is None:
            raise PersistenceError("SQLite did not assign an audit sequence")
        return int(cursor.lastrowid)

    def _insert_workflow_projection(
        self,
        connection: sqlite3.Connection,
        *,
        audit_sequence: int,
        audit: AuditEvent,
        event: StateWorkflowEvent | GateWorkflowEvent | AppendableWorkflowEvent,
        projection_event_id: str | None = None,
    ) -> WorkflowEventEnvelope:
        expected_type = AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND[event.kind]
        if audit.event_type is not expected_type:
            raise ValueError("Workflow event type does not match its audit truth")
        if audit.case_id == "" or audit_sequence < 1:
            raise ValueError("Workflow projection requires canonical source identity")
        selected_event_id = projection_event_id or f"workflow_{uuid4().hex}"
        envelope = WorkflowEventEnvelope.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "eventId": selected_event_id,
                "caseId": audit.case_id,
                "sourceAuditEventId": audit.event_id,
                "sourceAuditEventType": audit.event_type,
                "sourceAuditSequence": audit_sequence,
                "cursor": audit_sequence,
                "occurredAt": audit.occurred_at,
                "event": event,
            }
        )
        connection.execute(
            """
            INSERT INTO workflow_events (
                source_audit_sequence, source_audit_event_id,
                source_audit_event_type, case_id, event_id, event_kind, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_sequence,
                audit.event_id,
                audit.event_type.value,
                audit.case_id,
                envelope.event_id,
                event.kind.value,
                envelope.model_dump_json(by_alias=True),
            ),
        )
        self._insert_provider_usage_projection(
            connection,
            source_audit_sequence=audit_sequence,
            case_id=audit.case_id,
            occurred_at=audit.occurred_at,
            event=event,
        )
        return envelope

    @staticmethod
    def _insert_provider_usage_projection(
        connection: sqlite3.Connection,
        *,
        source_audit_sequence: int,
        case_id: str,
        occurred_at: datetime,
        event: StateWorkflowEvent | GateWorkflowEvent | AppendableWorkflowEvent,
    ) -> None:
        """Project every provider attempt outcome without request or response content."""

        if isinstance(event, ProviderCallWorkflowEvent):
            status = "succeeded"
            failure_category: str | None = None
            usage = event.usage
            cost = event.cost
        elif isinstance(event, RetryWorkflowEvent):
            status = "retry_scheduled"
            failure_category = event.failure.category.value
            usage = None
            cost = None
        elif isinstance(event, OperationalFailureWorkflowEvent):
            status = "failed"
            failure_category = event.failure.category.value
            usage = None
            cost = None
        else:
            return
        connection.execute(
            """
            INSERT INTO provider_usage_ledger (
                source_audit_sequence, case_id, operation, model_id, provider_mode,
                call_sequence, retry_attempt, duration_ms, status,
                input_tokens, output_tokens, total_tokens,
                estimated_cost_micros, currency, pricing_snapshot_id,
                failure_category, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_audit_sequence,
                case_id,
                event.operation.value,
                event.model_id.value,
                event.provider_mode,
                event.call_sequence,
                event.retry_attempt,
                event.duration_ms,
                status,
                None if usage is None else usage.input_tokens,
                None if usage is None else usage.output_tokens,
                None if usage is None else usage.total_tokens,
                None if cost is None else cost.estimated_cost_micros,
                None if cost is None else cost.currency,
                None if cost is None else cost.pricing_snapshot_id,
                failure_category,
                _dump_aware_datetime(occurred_at, "provider usage occurred_at"),
            ),
        )

    @classmethod
    def _update_case_row(
        cls,
        connection: sqlite3.Connection,
        *,
        current: CaseRecord,
        state: CaseState,
        snapshot: CaseSnapshot,
        updated_at: datetime,
    ) -> None:
        updated_at_value = _dump_aware_datetime(updated_at, "updated_at")
        if updated_at < current.updated_at:
            raise ValueError("updated_at cannot move backwards")
        cls._require_capabilities_before_next_version(
            connection,
            current=current,
            next_origin=updated_at,
        )
        transcript_row = connection.execute(
            "SELECT * FROM case_transcripts WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        if transcript_row is not None:
            transcript = cls._row_to_transcript(transcript_row)
            if snapshot.intake_summary is None:
                raise TranscriptStateError(
                    "A case with a bound transcript must retain its intake summary"
                )
            try:
                derived_id, derived_ref, derived_hash = _transcript_identity_from_summary(
                    current.case_id,
                    snapshot.intake_summary,
                )
            except ValueError as error:
                raise TranscriptStateError(
                    "A case update cannot invalidate its bound transcript summary"
                ) from error
            if (
                transcript.transcript_id != derived_id
                or transcript.local_ref != derived_ref
                or transcript.transcript_sha256 != derived_hash
            ):
                raise TranscriptStateError(
                    "A case update cannot replace its bound transcript identity"
                )

        claim_packet_json = (
            None
            if snapshot.claim_packet is None
            else snapshot.claim_packet.model_dump_json(by_alias=True)
        )
        intake_summary_json = (
            None if snapshot.intake_summary is None else _dump_json_object(snapshot.intake_summary)
        )
        clarification_json = (
            None
            if snapshot.active_clarification is None
            else _dump_json_object(snapshot.active_clarification)
        )
        cursor = connection.execute(
            """
            UPDATE cases
            SET version = version + 1,
                state = ?,
                portal_state = ?,
                redacted_metadata_json = ?,
                claim_packet_json = ?,
                intake_summary_json = ?,
                active_clarification_json = ?,
                updated_at = ?
            WHERE case_id = ? AND version = ?
            """,
            (
                state.value,
                snapshot.portal_state.value,
                _dump_json_object(snapshot.redacted_metadata),
                claim_packet_json,
                intake_summary_json,
                clarification_json,
                updated_at_value,
                current.case_id,
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise CaseRecordVersionConflictError(
                current.case_id,
                current.version,
                current.version + 1,
            )

    @classmethod
    def _require_capabilities_before_next_version(
        cls,
        connection: sqlite3.Connection,
        *,
        current: CaseRecord,
        next_origin: datetime,
    ) -> None:
        """Keep current-version capabilities inside [origin, next_origin)."""

        for row in connection.execute(
            """
            SELECT * FROM authority_capabilities
            WHERE case_id = ? AND bound_case_version = ?
            """,
            (current.case_id, current.version),
        ):
            capability = cls._row_to_capability(row)
            if capability.issued_at >= next_origin or (
                capability.consumed_at is not None and capability.consumed_at >= next_origin
            ):
                raise WorkflowAtomicityError(
                    "Case mutation must strictly follow every current-version "
                    "capability issue and consumption timestamp"
                )

    @staticmethod
    def _validate_page(after: int, limit: int) -> None:
        if after < 0:
            raise ValueError("after must be non-negative")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")

    @staticmethod
    def _validate_transcript_identity(
        transcript_id: str,
        transcript_sha256: str,
        local_ref: str,
    ) -> None:
        if _IDENTIFIER.fullmatch(transcript_id) is None:
            raise ValueError("transcript_id is invalid")
        if _SHA256.fullmatch(transcript_sha256) is None:
            raise ValueError("transcript_sha256 is invalid")
        if _TRANSCRIPT_LOCAL_REF.fullmatch(local_ref) is None:
            raise ValueError("Transcript local_ref is not an owned transcript handle")

    @staticmethod
    def _validate_digest(digest: bytes) -> None:
        if type(digest) is not bytes or len(digest) != 32:
            raise ValueError("Capability digest must be exactly 32 bytes")

    @classmethod
    def _validate_capability_values(
        cls,
        digest: bytes,
        role: str,
        purpose: str,
        portal_variant: PortalVariant | None,
        issued_at: datetime,
        expires_at: datetime,
        *,
        allow_legacy_human_variant: bool = False,
    ) -> None:
        cls._validate_digest(digest)
        allowed = {("agent", "portal_run"), ("human", "human_approve")}
        if (role, purpose) not in allowed:
            raise ValueError("Capability role and purpose are not an allowed pair")
        if (role == "agent" and portal_variant is not None) or (
            role == "human"
            and not isinstance(portal_variant, PortalVariant)
            and not (allow_legacy_human_variant and portal_variant is None)
        ):
            raise ValueError("Capability portal variant does not match its role")
        _dump_aware_datetime(issued_at, "capability issued_at")
        _dump_aware_datetime(expires_at, "capability expires_at")
        lifetime = expires_at - issued_at
        if lifetime <= timedelta(0) or lifetime > _CAPABILITY_TTL:
            raise ValueError("Capability TTL must be positive and at most 120 seconds")

    def _require_transcript(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> TranscriptRecord:
        row = connection.execute(
            "SELECT * FROM case_transcripts WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise TranscriptStateError("Case has no pending transcript")
        return self._row_to_transcript(row)

    @staticmethod
    def _row_to_transcript(row: sqlite3.Row) -> TranscriptRecord:
        confirmed_value = _require_integer(row["confirmed"], "transcript confirmation")
        if confirmed_value not in {0, 1}:
            raise PersistedDataIntegrityError("Persisted transcript confirmation is invalid")
        confirmed_at_raw = row["confirmed_at"]
        record = TranscriptRecord(
            transcript_id=_require_string(row["transcript_id"], "transcript id"),
            case_id=_require_string(row["case_id"], "transcript case id"),
            version=_require_integer(row["version"], "transcript version"),
            bound_case_version=_require_integer(
                row["bound_case_version"],
                "transcript bound case version",
            ),
            transcript_sha256=_require_string(
                row["transcript_sha256"],
                "transcript SHA-256",
            ),
            local_ref=_require_string(row["local_ref"], "transcript local ref"),
            confirmed=bool(confirmed_value),
            created_at=_parse_datetime(_require_string(row["created_at"], "transcript created_at")),
            confirmed_at=(
                None
                if confirmed_at_raw is None
                else _parse_datetime(_require_string(confirmed_at_raw, "transcript confirmed_at"))
            ),
        )
        SqliteCaseRepository._validate_transcript_identity(
            record.transcript_id,
            record.transcript_sha256,
            record.local_ref,
        )
        if record.version < 1 or record.bound_case_version < 1:
            raise PersistedDataIntegrityError("Persisted transcript version metadata is invalid")
        if (record.confirmed and record.version != 2) or (
            not record.confirmed and record.version != 1
        ):
            raise PersistedDataIntegrityError(
                "Persisted transcript version does not match confirmation state"
            )
        if record.confirmed is (record.confirmed_at is None):
            raise PersistedDataIntegrityError("Transcript confirmation timestamp is inconsistent")
        if record.confirmed_at is not None and record.confirmed_at < record.created_at:
            raise PersistedDataIntegrityError("Transcript confirmation timestamp precedes creation")
        return record

    def _require_capability(
        self,
        connection: sqlite3.Connection,
        digest: bytes,
    ) -> AuthorityCapabilityRecord:
        row = connection.execute(
            "SELECT * FROM authority_capabilities WHERE capability_digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise AuthorityCapabilityError("Capability was not persisted")
        return self._row_to_capability(row)

    @staticmethod
    def _row_to_capability(
        row: sqlite3.Row,
        *,
        allow_legacy_human_variant: bool = False,
    ) -> AuthorityCapabilityRecord:
        digest = row["capability_digest"]
        if type(digest) is not bytes or len(digest) != 32:
            raise PersistedDataIntegrityError("Persisted capability digest is invalid")
        consumed_raw = row["consumed_at"]
        revoked_raw = row["revoked_at"]
        try:
            variant_raw = row["portal_variant"]
        except IndexError:
            variant_raw = None
        try:
            portal_variant = (
                None
                if variant_raw is None
                else PortalVariant(_require_string(variant_raw, "capability portal variant"))
            )
        except ValueError as error:
            raise PersistedDataIntegrityError(
                "Persisted capability portal variant is invalid"
            ) from error
        record = AuthorityCapabilityRecord(
            digest=digest,
            case_id=_require_string(row["case_id"], "capability case id"),
            role=_require_string(row["role"], "capability role"),
            purpose=_require_string(row["purpose"], "capability purpose"),
            portal_variant=portal_variant,
            bound_case_version=_require_integer(
                row["bound_case_version"],
                "capability bound case version",
            ),
            issued_at=_parse_datetime(_require_string(row["issued_at"], "capability issued_at")),
            expires_at=_parse_datetime(_require_string(row["expires_at"], "capability expires_at")),
            consumed_at=(
                None
                if consumed_raw is None
                else _parse_datetime(_require_string(consumed_raw, "capability consumed_at"))
            ),
            revoked_at=(
                None
                if revoked_raw is None
                else _parse_datetime(_require_string(revoked_raw, "capability revoked_at"))
            ),
        )
        try:
            SqliteCaseRepository._validate_capability_values(
                record.digest,
                record.role,
                record.purpose,
                record.portal_variant,
                record.issued_at,
                record.expires_at,
                allow_legacy_human_variant=allow_legacy_human_variant,
            )
        except ValueError as error:
            raise PersistedDataIntegrityError("Persisted capability metadata is invalid") from error
        if record.bound_case_version < 1:
            raise PersistedDataIntegrityError("Persisted capability case version is invalid")
        if (record.consumed_at is not None and record.consumed_at < record.issued_at) or (
            record.revoked_at is not None and record.revoked_at < record.issued_at
        ):
            raise PersistedDataIntegrityError(
                "Persisted capability lifecycle timestamps are invalid"
            )
        if record.consumed_at is not None and record.consumed_at > record.expires_at:
            raise PersistedDataIntegrityError(
                "Persisted capability cannot be consumed after expiry"
            )
        return record

    @staticmethod
    def _row_to_provider_usage(row: sqlite3.Row) -> ProviderUsageLedgerRecord:
        failure_raw = row["failure_category"]
        record = ProviderUsageLedgerRecord(
            source_audit_sequence=_require_integer(
                row["source_audit_sequence"],
                "provider source sequence",
            ),
            case_id=_require_string(row["case_id"], "provider case id"),
            operation=WorkflowOperation(_require_string(row["operation"], "provider operation")),
            model_id=ProviderModelId(_require_string(row["model_id"], "provider model id")),
            provider_mode=_require_string(row["provider_mode"], "provider mode"),
            call_sequence=_require_integer(row["call_sequence"], "provider call sequence"),
            retry_attempt=_require_integer(row["retry_attempt"], "provider retry attempt"),
            duration_ms=_require_integer(row["duration_ms"], "provider duration"),
            status=_require_string(row["status"], "provider status"),
            input_tokens=(
                None
                if row["input_tokens"] is None
                else _require_integer(row["input_tokens"], "provider input tokens")
            ),
            output_tokens=(
                None
                if row["output_tokens"] is None
                else _require_integer(row["output_tokens"], "provider output tokens")
            ),
            total_tokens=(
                None
                if row["total_tokens"] is None
                else _require_integer(row["total_tokens"], "provider total tokens")
            ),
            estimated_cost_micros=(
                None
                if row["estimated_cost_micros"] is None
                else _require_integer(
                    row["estimated_cost_micros"],
                    "provider estimated cost",
                )
            ),
            currency=(
                None
                if row["currency"] is None
                else _require_string(row["currency"], "provider currency")
            ),
            pricing_snapshot_id=(
                None
                if row["pricing_snapshot_id"] is None
                else _require_string(
                    row["pricing_snapshot_id"],
                    "provider pricing snapshot",
                )
            ),
            failure_category=(
                None
                if failure_raw is None
                else ProviderFailureCategory(
                    _require_string(failure_raw, "provider failure category")
                )
            ),
            occurred_at=_parse_datetime(
                _require_string(row["occurred_at"], "provider occurred_at")
            ),
        )
        if record.provider_mode not in {"mock", "live"}:
            raise PersistedDataIntegrityError("Persisted provider mode is invalid")
        if not 1 <= record.call_sequence <= 40:
            raise PersistedDataIntegrityError("Persisted provider call sequence is invalid")
        if record.retry_attempt not in {0, 1} or record.duration_ms < 0:
            raise PersistedDataIntegrityError("Persisted provider retry or duration is invalid")

        token_values = (
            record.input_tokens,
            record.output_tokens,
            record.total_tokens,
        )
        if any(value is not None and value < 0 for value in token_values):
            raise PersistedDataIntegrityError("Persisted provider token usage is invalid")
        all_tokens_none = all(value is None for value in token_values)
        all_tokens_present = all(value is not None for value in token_values)
        if not (all_tokens_none or all_tokens_present):
            raise PersistedDataIntegrityError("Persisted provider token usage is incomplete")
        if (
            record.total_tokens is not None
            and record.input_tokens is not None
            and record.output_tokens is not None
            and record.total_tokens != record.input_tokens + record.output_tokens
        ):
            raise PersistedDataIntegrityError("Persisted provider token total is invalid")

        cost_values = (
            record.estimated_cost_micros,
            record.currency,
            record.pricing_snapshot_id,
        )
        all_cost_none = all(value is None for value in cost_values)
        all_cost_present = all(value is not None for value in cost_values)
        if not (all_cost_none or all_cost_present):
            raise PersistedDataIntegrityError("Persisted provider cost metadata is incomplete")
        if (record.estimated_cost_micros is not None and record.estimated_cost_micros < 0) or (
            record.currency is not None and record.currency != "USD"
        ):
            raise PersistedDataIntegrityError("Persisted provider cost metadata is invalid")
        if (
            record.pricing_snapshot_id is not None
            and _IDENTIFIER.fullmatch(record.pricing_snapshot_id) is None
        ):
            raise PersistedDataIntegrityError("Persisted provider pricing snapshot is invalid")

        if record.status == "succeeded":
            if record.failure_category is not None:
                raise PersistedDataIntegrityError(
                    "Succeeded provider usage cannot contain a failure"
                )
        elif record.status in {"retry_scheduled", "failed"}:
            if record.failure_category is None:
                raise PersistedDataIntegrityError(
                    "Failed provider usage requires a failure category"
                )
            if not all_tokens_none or not all_cost_none:
                raise PersistedDataIntegrityError(
                    "Provider failure telemetry cannot contain usage or cost"
                )
        else:
            raise PersistedDataIntegrityError("Persisted provider status is invalid")

        if record.operation is not WorkflowOperation.EXTRACTION and record.retry_attempt:
            raise PersistedDataIntegrityError("Only extraction may persist provider retries")
        if record.provider_mode == "mock":
            if record.model_id is not ProviderModelId.DETERMINISTIC_MOCK:
                raise PersistedDataIntegrityError(
                    "Mock provider usage requires the deterministic model"
                )
        elif record.operation is WorkflowOperation.TRANSCRIPTION:
            if record.model_id is not ProviderModelId.TRANSCRIBE:
                raise PersistedDataIntegrityError(
                    "Transcription usage requires the transcription model"
                )
        elif record.model_id is not ProviderModelId.SOL:
            raise PersistedDataIntegrityError("Live non-transcription usage requires gpt-5.6-sol")
        return record

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> CaseRecord:
        case_id = _require_string(row["case_id"], "case_id")
        state = CaseState(_require_string(row["state"], "state"))
        portal_state = PortalState(_require_string(row["portal_state"], "portal_state"))
        metadata_value = _load_json_object(
            _require_string(row["redacted_metadata_json"], "redacted metadata")
        )
        if any(not isinstance(value, str) for value in metadata_value.values()):
            raise PersistedDataIntegrityError("Redacted metadata summaries must be strings")
        redacted_metadata = cast(dict[str, str], metadata_value)
        validate_redacted_metadata(redacted_metadata)

        packet_raw = row["claim_packet_json"]
        packet = (
            None
            if packet_raw is None
            else ClaimPacket.model_validate_json(_require_string(packet_raw, "ClaimPacket"))
        )
        intake_raw = row["intake_summary_json"]
        clarification_raw = row["active_clarification_json"]
        snapshot = CaseSnapshot(
            portal_state=portal_state,
            redacted_metadata=redacted_metadata,
            claim_packet=packet,
            intake_summary=(
                None
                if intake_raw is None
                else _load_json_object(_require_string(intake_raw, "intake summary"))
            ),
            active_clarification=(
                None
                if clarification_raw is None
                else _load_json_object(_require_string(clarification_raw, "clarification"))
            ),
        )
        _validate_snapshot(case_id, state, snapshot)
        created_at = _parse_datetime(_require_string(row["created_at"], "created_at"))
        updated_at = _parse_datetime(_require_string(row["updated_at"], "updated_at"))
        if created_at > updated_at:
            raise PersistedDataIntegrityError("Persisted case created_at cannot follow updated_at")
        return CaseRecord(
            case_id=case_id,
            version=_require_integer(row["version"], "version"),
            state=state,
            snapshot=snapshot,
            created_at=created_at,
            updated_at=updated_at,
        )
