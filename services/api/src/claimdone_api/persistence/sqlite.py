"""Dependency-free, optimistic-concurrency SQLite case repository."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from claimdone_api.audit import validate_redacted_metadata
from claimdone_api.contracts import (
    AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND,
    CONTRACT_VERSION,
    ActorType,
    AuditEvent,
    AuditEventType,
    CaseState,
    ClaimPacket,
    ClarificationWorkflowEvent,
    GateDecision,
    GateId,
    GateWorkflowEvent,
    OperationalFailureWorkflowEvent,
    PlanStepWorkflowEvent,
    PortalFillWorkflowEvent,
    PortalState,
    ProviderCallWorkflowEvent,
    ProviderFailureCategory,
    ProviderModelId,
    RetryWorkflowEvent,
    SandboxReceipt,
    StateWorkflowEvent,
    ToolCallWorkflowEvent,
    VerificationWorkflowEvent,
    WorkflowEventEnvelope,
    WorkflowEventKind,
    WorkflowOperation,
    validate_case_transition,
    validate_workflow_event_order,
)

from .models import (
    AuthorityCapabilityRecord,
    CaseRecord,
    CaseSnapshot,
    JsonObject,
    ProviderUsageLedgerRecord,
    SandboxReceiptRecord,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    TranscriptRecord,
    TranscriptTransitionResult,
    validate_portal_state,
)

SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

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

_MEDIA_STORAGE_NAME = re.compile(r"^case-[a-f0-9]{32}$")
_AUDIO_LOCAL_REF = re.compile(r"^audio-[a-f0-9]{32}\.wav$")
_TRANSCRIPT_LOCAL_REF = re.compile(r"^transcript-[a-f0-9]{32}\.txt$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPABILITY_TTL = timedelta(seconds=120)


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


class SqliteCaseRepository:
    """Store case snapshots with atomic state events and compare-and-swap versions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.initialize()

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

    def initialize(self) -> None:
        """Create or atomically migrate the schema and validate canonical payloads."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            journal_mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if journal_mode_row is None or str(journal_mode_row[0]).lower() != "wal":
                raise PersistenceError("SQLite database could not enter WAL mode")
            connection.execute("PRAGMA synchronous = NORMAL")
            version_row = connection.execute("PRAGMA user_version").fetchone()
            if version_row is None:
                raise PersistenceError("SQLite did not report a schema version")
            version = int(version_row[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"Database schema {version} is newer than supported version {SCHEMA_VERSION}"
                )
            self._require_no_foreign_key_violations(connection)
            if version > 0:
                self._preflight_canonical_payloads(
                    connection,
                    legacy=version < SCHEMA_VERSION,
                )
            if version == SCHEMA_VERSION:
                return

        # SQLite ignores PRAGMA foreign_keys changes inside a transaction. The
        # cases-table rebuild therefore uses one dedicated connection whose FK
        # mode is disabled before BEGIN and is restored before it is closed.
        with closing(self._connect(foreign_keys=False)) as connection:
            mode = connection.execute("PRAGMA foreign_keys").fetchone()
            if mode is None or int(mode[0]) != 0:
                raise PersistenceError("SQLite foreign keys could not be disabled for migration")
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("PRAGMA user_version").fetchone()
                version = int(row[0]) if row is not None else -1
                if version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Database schema {version} is newer than supported version "
                        f"{SCHEMA_VERSION}"
                    )
                if version > 0:
                    self._preflight_canonical_payloads(connection, legacy=True)
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
                if version != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(f"Unsupported database schema: {version}")
                self._require_no_foreign_key_violations(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise PersistenceError("SQLite integrity check failed during migration")
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

        with closing(self._connect()) as connection:
            self._require_no_foreign_key_violations(connection)
            self._preflight_canonical_payloads(connection, legacy=False)

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
    ) -> None:
        """Validate every canonical JSON root without rewriting its version."""

        try:
            if self._table_exists(connection, "cases"):
                for row in connection.execute("SELECT * FROM cases ORDER BY case_id"):
                    self._row_to_case(row)
            if self._table_exists(connection, "audit_events"):
                for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
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
            if self._table_exists(connection, "gate_decisions"):
                for row in connection.execute("SELECT * FROM gate_decisions ORDER BY sequence"):
                    decision = GateDecision.model_validate_json(
                        _require_string(row["decision_json"], "gate decision")
                    )
                    if decision.gate_id.value != _require_string(
                        row["gate_id"], "gate id"
                    ) or decision.decided_at != _parse_datetime(
                        _require_string(row["decided_at"], "gate decided_at")
                    ):
                        raise PersistedDataIntegrityError(
                            "Persisted gate columns disagree with canonical JSON"
                        )
            if self._table_exists(connection, "workflow_events"):
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
                    source_row = connection.execute(
                        "SELECT event_json FROM audit_events WHERE sequence = ?",
                        (source_sequence,),
                    ).fetchone()
                    if source_row is None:
                        raise PersistedDataIntegrityError(
                            "Persisted workflow source audit event is missing"
                        )
                    source = AuditEvent.model_validate_json(
                        _require_string(source_row["event_json"], "source audit event")
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
            if self._table_exists(connection, "case_transcripts"):
                for row in connection.execute("SELECT * FROM case_transcripts ORDER BY case_id"):
                    self._row_to_transcript(row)
            if self._table_exists(connection, "provider_usage_ledger"):
                for row in connection.execute(
                    "SELECT * FROM provider_usage_ledger ORDER BY source_audit_sequence"
                ):
                    self._row_to_provider_usage(row)
            if self._table_exists(connection, "authority_capabilities"):
                for row in connection.execute(
                    "SELECT * FROM authority_capabilities ORDER BY case_id, purpose"
                ):
                    self._row_to_capability(row)
            if self._table_exists(connection, "sandbox_receipts"):
                for row in connection.execute("SELECT * FROM sandbox_receipts ORDER BY case_id"):
                    receipt = SandboxReceipt.model_validate_json(
                        _require_string(row["receipt_json"], "sandbox receipt")
                    )
                    if receipt.case_id != _require_string(row["case_id"], "receipt case id"):
                        raise PersistedDataIntegrityError(
                            "Persisted receipt case identity is invalid"
                        )
                    _parse_datetime(_require_string(row["created_at"], "receipt created_at"))
        except (
            PersistedDataIntegrityError,
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
        """Persist one opaque owned media handle for a case exactly once."""

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
        """Remove only the exact opaque mapping selected by the caller."""

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

    def replace_snapshot(
        self,
        *,
        case_id: str,
        expected_version: int,
        snapshot: CaseSnapshot,
        updated_at: datetime,
    ) -> CaseRecord:
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_version)
            _validate_snapshot(case_id, current.state, snapshot)
            self._update_case_row(
                connection,
                current=current,
                state=current.state,
                snapshot=snapshot,
                updated_at=updated_at,
            )
        return self._require_case(case_id)

    def transition_case(
        self,
        *,
        case_id: str,
        expected_version: int,
        target: CaseState,
        snapshot: CaseSnapshot,
        event: AuditEvent,
        updated_at: datetime,
    ) -> CaseRecord:
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_version)
            if target is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION or (
                current.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
                and target is CaseState.ANALYZING
            ):
                raise TranscriptStateError(
                    "Transcript state transitions require the atomic transcript methods"
                )
            validate_case_transition(current.state, target)
            self._validate_state_event(event, current=current, target=target)
            _validate_snapshot(case_id, target, snapshot)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=updated_at,
            )
            audit_sequence = self._insert_audit_event(connection, event)
            state_event = StateWorkflowEvent.model_validate(
                {
                    "kind": WorkflowEventKind.STATE,
                    "actor": event.actor,
                    "fromState": current.state,
                    "toState": target,
                }
            )
            self._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=state_event,
            )
        return self._require_case(case_id)

    def record_gate_decision(
        self,
        *,
        case_id: str,
        expected_version: int,
        decision: GateDecision,
        event: AuditEvent,
        updated_at: datetime,
    ) -> CaseRecord:
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_version)
            if event.case_id != case_id or event.event_type is not AuditEventType.GATE_DECISION:
                raise ValueError("Gate audit event must belong to the mutated case")
            if event.occurred_at != decision.decided_at:
                raise ValueError("Gate audit event timestamp must match GateDecision")
            if event.reason_codes != decision.reason_codes:
                raise ValueError("Gate audit reasonCodes must match GateDecision")
            self._update_case_row(
                connection,
                current=current,
                state=current.state,
                snapshot=current.snapshot,
                updated_at=updated_at,
            )
            connection.execute(
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
            audit_sequence = self._insert_audit_event(connection, event)
            gate_event = GateWorkflowEvent.model_validate(
                {"kind": WorkflowEventKind.GATE, "decision": decision}
            )
            self._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=gate_event,
            )
        return self._require_case(case_id)

    def append_workflow_event(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        event: AppendableWorkflowEvent,
        actor: ActorType,
        occurred_at: datetime,
    ) -> WorkflowEventEnvelope:
        """Append redacted audit truth and its projection without authorizing state."""

        _dump_aware_datetime(occurred_at, "workflow occurred_at")
        if event.kind in {WorkflowEventKind.STATE, WorkflowEventKind.GATE}:
            raise ValueError(
                "State and gate workflow projections require their atomic mutation paths"
            )
        with self._write_connection() as connection:
            self._require_current(connection, case_id, expected_case_version)
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
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT source_audit_sequence, event_json
                FROM workflow_events
                WHERE case_id = ? AND source_audit_sequence > ?
                ORDER BY source_audit_sequence ASC
                LIMIT ?
                """,
                (case_id, after, limit),
            ).fetchall()
        try:
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
        except (ValidationError, ValueError, TypeError) as error:
            raise PersistedDataIntegrityError(
                "Persisted workflow event projection is invalid"
            ) from error

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
            raise PersistedDataIntegrityError(
                "Persisted provider usage telemetry is invalid"
            ) from error

    def save_pending_transcript_and_transition(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        transcript_id: str,
        transcript_sha256: str,
        local_ref: str,
        snapshot: CaseSnapshot,
        event: AuditEvent,
        updated_at: datetime,
    ) -> TranscriptTransitionResult:
        """Atomically bind content-free transcript metadata and enter confirmation."""

        self._validate_transcript_identity(transcript_id, transcript_sha256, local_ref)
        target = CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_case_version)
            if current.state is not CaseState.DISCLOSED:
                raise TranscriptStateError("Pending transcripts require a disclosed case")
            validate_case_transition(current.state, target)
            self._validate_state_event(event, current=current, target=target)
            _validate_snapshot(case_id, target, snapshot)
            self._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=updated_at,
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
                    current.version + 1,
                    transcript_sha256,
                    local_ref,
                    _dump_aware_datetime(updated_at, "transcript created_at"),
                ),
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
            transcript = self._require_transcript(connection, case_id)
        return TranscriptTransitionResult(case=case, transcript=transcript)

    def confirm_transcript_and_transition(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        transcript_id: str,
        transcript_sha256: str,
        snapshot: CaseSnapshot,
        event: AuditEvent,
        updated_at: datetime,
    ) -> TranscriptTransitionResult:
        """Confirm exactly the displayed transcript and enter analyzing once."""

        if _IDENTIFIER.fullmatch(transcript_id) is None:
            raise ValueError("transcript_id is invalid")
        if _SHA256.fullmatch(transcript_sha256) is None:
            raise ValueError("transcript_sha256 is invalid")
        target = CaseState.ANALYZING
        with self._write_connection() as connection:
            current = self._require_current(connection, case_id, expected_case_version)
            if current.state is not CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
                raise TranscriptStateError("Case is not awaiting transcript confirmation")
            transcript = self._require_transcript(connection, case_id)
            if (
                transcript.transcript_id != transcript_id
                or transcript.transcript_sha256 != transcript_sha256
                or transcript.version != 1
                or transcript.bound_case_version != current.version
                or transcript.confirmed
            ):
                raise TranscriptStateError("Transcript confirmation is stale or mismatched")
            validate_case_transition(current.state, target)
            self._validate_state_event(event, current=current, target=target)
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

    def issue_authority_capability(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        digest: bytes,
        role: str,
        purpose: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> AuthorityCapabilityRecord:
        """Persist only a 32-byte verifier and revoke older open peers."""

        self._validate_capability_values(digest, role, purpose, issued_at, expires_at)
        issued = _dump_aware_datetime(issued_at, "capability issued_at")
        expires = _dump_aware_datetime(expires_at, "capability expires_at")
        with self._write_connection() as connection:
            self._require_current(connection, case_id, expected_case_version)
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
                    issued_at, expires_at, consumed_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    digest,
                    case_id,
                    role,
                    purpose,
                    expected_case_version,
                    issued,
                    expires,
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

    def get_sandbox_receipt(self, case_id: str) -> SandboxReceiptRecord | None:
        """Read only; AUTH owns the later atomic receipt insertion path."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT receipt_json, created_at FROM sandbox_receipts WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            receipt = SandboxReceipt.model_validate_json(
                _require_string(row["receipt_json"], "sandbox receipt")
            )
            created_at = _parse_datetime(_require_string(row["created_at"], "created_at"))
            if receipt.case_id != case_id:
                raise ValueError("Receipt caseId does not match its persistence key")
        except (ValidationError, ValueError, TypeError) as error:
            raise PersistedDataIntegrityError("Persisted sandbox receipt is invalid") from error
        return SandboxReceiptRecord(receipt=receipt, created_at=created_at)

    def delete_case(self, case_id: str) -> bool:
        with self._write_connection() as connection:
            cursor = connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
            return cursor.rowcount > 0

    def reset_cases(self) -> int:
        """Delete cases without resetting AUTOINCREMENT history cursors."""

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
        row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            raise CaseRecordNotFoundError(case_id)
        current = self._row_to_case(row)
        if current.version != expected_version:
            raise CaseRecordVersionConflictError(case_id, expected_version, current.version)
        return current

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

    @staticmethod
    def _update_case_row(
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
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        cls._validate_digest(digest)
        allowed = {("agent", "portal_run"), ("human", "human_approve")}
        if (role, purpose) not in allowed:
            raise ValueError("Capability role and purpose are not an allowed pair")
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
    def _row_to_capability(row: sqlite3.Row) -> AuthorityCapabilityRecord:
        digest = row["capability_digest"]
        if type(digest) is not bytes or len(digest) != 32:
            raise PersistedDataIntegrityError("Persisted capability digest is invalid")
        consumed_raw = row["consumed_at"]
        revoked_raw = row["revoked_at"]
        record = AuthorityCapabilityRecord(
            digest=digest,
            case_id=_require_string(row["case_id"], "capability case id"),
            role=_require_string(row["role"], "capability role"),
            purpose=_require_string(row["purpose"], "capability purpose"),
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
                record.issued_at,
                record.expires_at,
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
        if record.pricing_snapshot_id == "":
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
            raise PersistedDataIntegrityError(
                "Live non-transcription usage requires gpt-5.6-sol"
            )
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
        return CaseRecord(
            case_id=case_id,
            version=_require_integer(row["version"], "version"),
            state=state,
            snapshot=snapshot,
            created_at=_parse_datetime(_require_string(row["created_at"], "created_at")),
            updated_at=_parse_datetime(_require_string(row["updated_at"], "updated_at")),
        )
