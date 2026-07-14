"""Dependency-free, optimistic-concurrency SQLite case repository."""

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from claimdone_api.audit import validate_redacted_metadata
from claimdone_api.contracts import (
    AuditEvent,
    AuditEventType,
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    PortalState,
    validate_case_transition,
)

from .models import (
    CaseRecord,
    CaseSnapshot,
    JsonObject,
    SequencedAuditEvent,
    SequencedGateDecision,
    validate_portal_state,
)

SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


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


def _enum_sql_values(
    values: type[CaseState] | type[PortalState] | type[GateId],
) -> str:
    return ", ".join(f"'{value.value}'" for value in values)


_CASE_STATE_VALUES = _enum_sql_values(CaseState)
_PORTAL_STATE_VALUES = _enum_sql_values(PortalState)
_GATE_ID_VALUES = _enum_sql_values(GateId)

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

_MEDIA_STORAGE_NAME = re.compile(r"^case-[a-f0-9]{32}$")


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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
        """Create or validate the schema and configure WAL once per database."""

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
            if version == SCHEMA_VERSION:
                return

        with self._write_connection() as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0]) if version_row is not None else -1
            if version == 0:
                for statement in _MIGRATION_1:
                    connection.execute(statement)
                version = 1
                connection.execute("PRAGMA user_version = 1")
            if version == 1:
                for statement in _MIGRATION_2:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(f"Unsupported database schema: {version}")

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
            if connection.execute(
                "SELECT 1 FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone() is None:
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
            self._insert_audit_event(connection, event)
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
            self._insert_audit_event(connection, event)
        return self._require_case(case_id)

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
    def _insert_audit_event(connection: sqlite3.Connection, event: AuditEvent) -> None:
        if event.details:
            raise ValueError(
                "Backend workflow audit events must not contain free-form details"
            )
        connection.execute(
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
            None
            if snapshot.intake_summary is None
            else _dump_json_object(snapshot.intake_summary)
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
