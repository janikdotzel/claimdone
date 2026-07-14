"""Schema-v3 migration, event projection, transcript, and capability tests."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest

from claimdone_api.audit import build_gate_audit_event, build_state_change_event
from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    CaseState,
    ClarificationWorkflowEvent,
    GateDecision,
    GateId,
    GateWorkflowEvent,
    OperationalFailureWorkflowEvent,
    ProviderCallWorkflowEvent,
    RetryWorkflowEvent,
    SandboxReceipt,
    StateWorkflowEvent,
    TranscriptConfirmationRequest,
)
from claimdone_api.persistence import (
    CaseRecordVersionConflictError,
    IncompatiblePersistedContractError,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
    TranscriptStateError,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
DIGEST = "a" * 64


def _gate() -> GateDecision:
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": "G0",
            "deterministicPassed": True,
            "modelBlocked": False,
            "passed": True,
            "reasonCodes": (),
            "evidenceRefs": (),
            "decidedAt": NOW + timedelta(seconds=1),
        }
    )


def _create_literal_v2_database(
    path: Path,
    *,
    audit_contract_version: str | None = None,
    pending_transcript: bool = False,
) -> None:
    old_states: tuple[str, ...] = (
        "created",
        "disclosed",
        "analyzing",
        "awaiting_clarification",
        "ready_to_fill",
        "filling",
        "verifying",
        "review",
        "blocked",
        "human_approved",
        "receipt",
        "emergency_stopped",
        "abandoned",
        "failed",
    )
    if pending_transcript:
        old_states += ("awaiting_transcript_confirmation",)
    state_values = ", ".join(f"'{state}'" for state in old_states)
    gate_values = ", ".join(f"'{gate.value}'" for gate in GateId)
    state_audit = build_state_change_event(
        case_id="case-v2",
        current=CaseState.CREATED,
        target=CaseState.DISCLOSED,
        actor=ActorType.SYSTEM,
        occurred_at=NOW,
        event_id_factory=lambda: "audit-v2-state",
    )
    gate = _gate()
    gate_audit = build_gate_audit_event(
        case_id="case-v2",
        decision=gate,
        actor=ActorType.SYSTEM,
        event_id_factory=lambda: "audit-v2-gate",
    )
    state_json = state_audit.model_dump(mode="json", by_alias=True)
    if audit_contract_version is not None:
        state_json["contractVersion"] = audit_contract_version

    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            CREATE TABLE cases (
                case_id TEXT PRIMARY KEY NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                state TEXT NOT NULL CHECK (state IN ({state_values})),
                portal_state TEXT NOT NULL CHECK (
                    portal_state IN ('draft','review','human_approved','receipt')
                ),
                redacted_metadata_json TEXT NOT NULL CHECK (json_valid(redacted_metadata_json)),
                claim_packet_json TEXT CHECK (
                    claim_packet_json IS NULL OR json_valid(claim_packet_json)
                ),
                intake_summary_json TEXT CHECK (
                    intake_summary_json IS NULL OR json_valid(intake_summary_json)
                ),
                active_clarification_json TEXT CHECK (
                    active_clarification_json IS NULL OR json_valid(active_clarification_json)
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL,
                event_json TEXT NOT NULL CHECK (json_valid(event_json))
            );
            CREATE INDEX audit_events_case_sequence_idx
            ON audit_events(case_id, sequence);
            CREATE TABLE gate_decisions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                gate_id TEXT NOT NULL CHECK (gate_id IN ({gate_values})),
                decided_at TEXT NOT NULL,
                decision_json TEXT NOT NULL CHECK (json_valid(decision_json))
            );
            CREATE INDEX gate_decisions_case_sequence_idx
            ON gate_decisions(case_id, sequence);
            CREATE TABLE case_media_handles (
                case_id TEXT PRIMARY KEY NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                storage_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        connection.execute(
            """
            INSERT INTO cases VALUES (?, 3, 'disclosed', 'draft', '{}', NULL, NULL, NULL, ?, ?)
            """,
            ("case-v2", NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()),
        )
        connection.execute(
            "INSERT INTO audit_events VALUES (7, ?, ?, ?, ?)",
            (
                state_audit.event_id,
                "case-v2",
                NOW.isoformat(),
                json.dumps(state_json),
            ),
        )
        connection.execute(
            "INSERT INTO audit_events VALUES (9, ?, ?, ?, ?)",
            (
                gate_audit.event_id,
                "case-v2",
                gate.decided_at.isoformat(),
                gate_audit.model_dump_json(by_alias=True),
            ),
        )
        connection.execute(
            "INSERT INTO gate_decisions VALUES (5, ?, ?, ?, ?)",
            (
                "case-v2",
                gate.gate_id.value,
                gate.decided_at.isoformat(),
                gate.model_dump_json(by_alias=True),
            ),
        )
        connection.execute(
            "INSERT INTO case_media_handles VALUES (?, ?, ?)",
            ("case-v2", f"case-{'1' * 32}", NOW.isoformat()),
        )
        if pending_transcript:
            pending_audit = build_state_change_event(
                case_id="case-v2",
                current=CaseState.DISCLOSED,
                target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                actor=ActorType.SYSTEM,
                occurred_at=NOW + timedelta(seconds=2),
                event_id_factory=lambda: "audit-v2-pending-transcript",
            )
            connection.execute(
                "UPDATE cases SET version = 4, state = ?, intake_summary_json = ?, "
                "updated_at = ? WHERE case_id = 'case-v2'",
                (
                    CaseState.AWAITING_TRANSCRIPT_CONFIRMATION.value,
                    json.dumps(_pending_summary()),
                    pending_audit.occurred_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO audit_events VALUES (11, ?, ?, ?, ?)",
                (
                    pending_audit.event_id,
                    "case-v2",
                    pending_audit.occurred_at.isoformat(),
                    pending_audit.model_dump_json(by_alias=True),
                ),
            )


def _service(path: Path) -> tuple[CaseService, SqliteCaseRepository]:
    repository = SqliteCaseRepository(path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-v3",
    )
    return service, repository


def _pending_summary() -> dict[str, Any]:
    return {
        "images": [],
        "text": None,
        "audio": {
            "fileId": f"audio-{'2' * 32}.wav",
            "mediaType": "audio/wav",
            "sha256": "c" * 64,
        },
        "statement": {
            "fileId": f"transcript-{'3' * 32}.txt",
            "mediaType": "text/plain",
            "sha256": DIGEST,
        },
    }


def _clarification_event() -> ClarificationWorkflowEvent:
    return ClarificationWorkflowEvent.model_validate(
        {
            "kind": "clarification",
            "round": 1,
            "field": "incident_date",
            "status": "requested",
        }
    )


def _provider_event() -> ProviderCallWorkflowEvent:
    return ProviderCallWorkflowEvent.model_validate(
        {
            "kind": "provider_call",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": 1,
            "retryAttempt": 0,
            "durationMs": 250,
            "status": "succeeded",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            "cost": {
                "estimatedCostMicros": 42,
                "currency": "USD",
                "pricingSnapshotId": "pricing-2026-07-14",
            },
        }
    )


def _retry_event() -> RetryWorkflowEvent:
    return RetryWorkflowEvent.model_validate(
        {
            "kind": "retry",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": 2,
            "retryAttempt": 1,
            "durationMs": 300,
            "failure": {
                "category": "timeout",
                "retryable": True,
                "terminal": False,
            },
        }
    )


def _operational_failure_event() -> OperationalFailureWorkflowEvent:
    return OperationalFailureWorkflowEvent.model_validate(
        {
            "kind": "operational_failure",
            "operation": "transcription",
            "modelId": "gpt-4o-transcribe",
            "providerMode": "live",
            "callSequence": 3,
            "retryAttempt": 0,
            "durationMs": 400,
            "failure": {
                "category": "content_filtered",
                "retryable": False,
                "terminal": True,
            },
        }
    )


def _receipt(case_id: str) -> SandboxReceipt:
    return SandboxReceipt.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "receiptId": "receipt-v3",
            "caseId": case_id,
            "approvalId": "approval-v3",
            "variant": "A",
            "state": "receipt",
            "version": 8,
            "environment": "sandbox",
            "sandboxOnly": True,
            "submittedToRealInsurer": False,
            "humanApproved": True,
            "redacted": True,
            "summary": {
                "completedFieldCount": 8,
                "attachmentCount": 3,
                "verificationPassed": True,
                "finalActionOwner": "human",
            },
            "approvedAt": NOW,
            "renderedAt": NOW + timedelta(seconds=1),
        }
    )


def test_literal_v2_migration_preserves_rows_cursors_fks_and_backfills_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v2.db"
    _create_literal_v2_database(database_path)

    repository = SqliteCaseRepository(database_path)

    record = repository.get_case("case-v2")
    assert record is not None
    assert (record.version, record.state) == (3, CaseState.DISCLOSED)
    assert repository.get_case_media_handle("case-v2") == f"case-{'1' * 32}"
    assert [item.sequence for item in repository.list_audit_events("case-v2")] == [7, 9]
    assert [item.sequence for item in repository.list_gate_decisions("case-v2")] == [5]
    replay = repository.list_workflow_events("case-v2")
    assert [item.sequence for item in replay] == [7, 9]
    assert [item.envelope.event.kind for item in replay] == ["state", "gate"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        cases_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='cases'"
            ).fetchone()[0]
        )
    assert "awaiting_transcript_confirmation" in cases_sql

    appended = repository.append_workflow_event(
        case_id="case-v2",
        expected_case_version=3,
        event=_clarification_event(),
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert appended.cursor > 9
    assert [item.sequence for item in repository.list_audit_events("case-v2")] == [
        7,
        9,
        appended.cursor,
    ]

    repository.delete_case("case-v2")
    with sqlite3.connect(database_path) as connection:
        for table in (
            "audit_events",
            "gate_decisions",
            "case_media_handles",
            "workflow_events",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_v2_pending_transcript_metadata_is_backfilled_without_content(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v2-pending.db"
    _create_literal_v2_database(database_path, pending_transcript=True)

    repository = SqliteCaseRepository(database_path)

    case = repository.get_case("case-v2")
    transcript = repository.get_transcript("case-v2")
    assert case is not None
    assert case.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
    assert case.version == 4
    assert transcript is not None
    assert transcript.case_id == case.case_id
    assert transcript.bound_case_version == case.version
    assert transcript.transcript_sha256 == DIGEST
    assert transcript.local_ref == f"transcript-{'3' * 32}.txt"
    assert transcript.version == 1
    assert transcript.confirmed is False
    assert [item.sequence for item in repository.list_workflow_events(case.case_id)] == [
        7,
        9,
        11,
    ]


def test_legacy_contract_payload_aborts_without_schema_or_cursor_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_literal_v2_database(database_path, audit_contract_version="1.0.0")

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        sequences = connection.execute(
            "SELECT sequence FROM audit_events ORDER BY sequence"
        ).fetchall()
        assert sequences == [
            (7,),
            (9,),
        ]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='workflow_events'"
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT state, version FROM cases WHERE case_id='case-v2'"
        ).fetchone() == ("disclosed", 3)


def test_persisted_v2_contract_root_is_rejected_unchanged_with_reset_guidance(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "persisted-contract-v2.db"
    _create_literal_v2_database(database_path, audit_contract_version="2.0.0")
    with sqlite3.connect(database_path) as connection:
        root_before = str(
            connection.execute(
                "SELECT event_json FROM audit_events WHERE sequence = 7"
            ).fetchone()[0]
        )
        schema_before = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    assert json.loads(root_before)["contractVersion"] == "2.0.0"

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert (
            str(
                connection.execute(
                    "SELECT event_json FROM audit_events WHERE sequence = 7"
                ).fetchone()[0]
            )
            == root_before
        )
        assert connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall() == schema_before


def test_legacy_naive_timestamp_maps_to_reset_error_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-naive-time.db"
    _create_literal_v2_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET created_at = '2026-07-14T12:00:00' WHERE case_id = 'case-v2'"
        )

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute(
            "SELECT created_at FROM cases WHERE case_id = 'case-v2'"
        ).fetchone() == ("2026-07-14T12:00:00",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'workflow_events'"
            ).fetchone()
            is None
        )


def test_migration_late_ddl_fault_rolls_back_parent_rebuild_and_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fault.db"
    _create_literal_v2_database(database_path)
    with sqlite3.connect(database_path) as connection:
        # This collides only after cases has been copied, dropped, and renamed,
        # and after the new audit source index has been attempted.
        connection.execute(
            "CREATE TABLE workflow_events ("
            "source_audit_sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL)"
        )
        schema_before = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        rows_before = {
            "cases": connection.execute("SELECT * FROM cases").fetchall(),
            "audit": connection.execute("SELECT * FROM audit_events").fetchall(),
            "gates": connection.execute("SELECT * FROM gate_decisions").fetchall(),
            "media": connection.execute("SELECT * FROM case_media_handles").fetchall(),
        }

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert (
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            == schema_before
        )
        assert connection.execute("SELECT * FROM cases").fetchall() == rows_before["cases"]
        assert connection.execute("SELECT * FROM audit_events").fetchall() == rows_before["audit"]
        assert connection.execute("SELECT * FROM gate_decisions").fetchall() == rows_before["gates"]
        assert (
            connection.execute("SELECT * FROM case_media_handles").fetchall()
            == rows_before["media"]
        )
        assert (
            connection.execute("SELECT name FROM sqlite_schema WHERE name = 'cases_v3'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'audit_events_projection_source_idx'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_state_gate_generic_replay_and_provider_ledger_are_atomic_and_redacted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "events.db"
    service, repository = _service(database_path)
    record = service.create_case()
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    gate = _gate()
    record = service.record_gate_decision(
        record.case_id,
        expected_version=record.version,
        decision=gate,
    )
    clarification = _clarification_event()
    first_generic = repository.append_workflow_event(
        case_id=record.case_id,
        expected_case_version=record.version,
        event=clarification,
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=2),
    )
    provider = _provider_event()
    provider_envelope = repository.append_workflow_event(
        case_id=record.case_id,
        expected_case_version=record.version,
        event=provider,
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=3),
    )
    retry_envelope = repository.append_workflow_event(
        case_id=record.case_id,
        expected_case_version=record.version,
        event=_retry_event(),
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=4),
    )
    failure_envelope = repository.append_workflow_event(
        case_id=record.case_id,
        expected_case_version=record.version,
        event=_operational_failure_event(),
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=5),
    )

    replay = repository.list_workflow_events(record.case_id, after=first_generic.cursor)
    assert tuple(item.envelope for item in replay) == (
        provider_envelope,
        retry_envelope,
        failure_envelope,
    )
    assert service.get_case(record.case_id).version == record.version
    reopened = SqliteCaseRepository(database_path)
    usage = reopened.list_provider_usage(record.case_id)
    assert tuple(item.source_audit_sequence for item in usage) == (
        provider_envelope.cursor,
        retry_envelope.cursor,
        failure_envelope.cursor,
    )
    assert tuple(item.status for item in usage) == (
        "succeeded",
        "retry_scheduled",
        "failed",
    )
    assert tuple(item.call_sequence for item in usage) == (1, 2, 3)
    assert tuple(
        None if item.failure_category is None else item.failure_category.value
        for item in usage
    ) == (None, "timeout", "content_filtered")
    assert usage[0].total_tokens == 15
    assert usage[0].estimated_cost_micros == 42
    assert usage[1].total_tokens is None
    assert usage[1].estimated_cost_micros is None
    assert usage[2].total_tokens is None
    assert usage[2].estimated_cost_micros is None
    repository.create_case(
        case_id="case-provider-other",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        persisted_json = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT event_json FROM workflow_events ORDER BY source_audit_sequence"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE provider_usage_ledger SET case_id = 'case-provider-other'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workflow_events "
                "SELECT * FROM workflow_events ORDER BY source_audit_sequence LIMIT 1"
            )
    for forbidden in ('"prompt"', '"response"', '"claimantName"', '"policyReference"'):
        assert forbidden not in persisted_json

    before_audit = repository.list_audit_events(record.case_id)
    with pytest.raises(CaseRecordVersionConflictError):
        repository.append_workflow_event(
            case_id=record.case_id,
            expected_case_version=record.version - 1,
            event=clarification,
            actor=ActorType.SYSTEM,
            occurred_at=NOW + timedelta(seconds=6),
        )
    assert repository.list_audit_events(record.case_id) == before_audit


def test_generic_append_cannot_bypass_state_or_gate_authority(tmp_path: Path) -> None:
    service, repository = _service(tmp_path / "authority-bypass.db")
    case = service.create_case()
    state = StateWorkflowEvent.model_validate(
        {
            "kind": "state",
            "actor": "system",
            "fromState": "created",
            "toState": "disclosed",
        }
    )
    gate = GateWorkflowEvent.model_validate({"kind": "gate", "decision": _gate()})

    for forbidden in (state, gate):
        with pytest.raises(ValueError, match="atomic mutation"):
            repository.append_workflow_event(
                case_id=case.case_id,
                expected_case_version=case.version,
                event=cast(Any, forbidden),
                actor=ActorType.SYSTEM,
                occurred_at=NOW,
            )

    assert service.get_case(case.case_id) == case
    assert repository.list_audit_events(case.case_id) == ()
    assert repository.list_gate_decisions(case.case_id) == ()
    assert repository.list_workflow_events(case.case_id) == ()


def test_projection_failure_rolls_back_case_audit_and_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository = _service(tmp_path / "atomic.db")
    created = service.create_case()

    def fail_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(repository, "_insert_workflow_projection", fail_projection)
    with pytest.raises(RuntimeError, match="injected"):
        service.transition_case(
            created.case_id,
            expected_version=created.version,
            target=CaseState.DISCLOSED,
        )
    assert service.get_case(created.case_id) == created
    assert repository.list_audit_events(created.case_id) == ()
    assert repository.list_workflow_events(created.case_id) == ()


def test_ledger_insert_failure_rolls_back_audit_workflow_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository = _service(tmp_path / "ledger-atomic.db")
    created = service.create_case()

    def fail_ledger(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(repository, "_insert_provider_usage_projection", fail_ledger)
    with pytest.raises(RuntimeError, match="injected ledger"):
        repository.append_workflow_event(
            case_id=created.case_id,
            expected_case_version=created.version,
            event=_provider_event(),
            actor=ActorType.SYSTEM,
            occurred_at=NOW,
        )

    assert repository.list_audit_events(created.case_id) == ()
    assert repository.list_workflow_events(created.case_id) == ()
    assert repository.list_provider_usage(created.case_id) == ()


def test_provider_ledger_corruption_is_wrapped_as_integrity_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger-corrupt.db"
    service, repository = _service(database_path)
    case = service.create_case()
    repository.append_workflow_event(
        case_id=case.case_id,
        expected_case_version=case.version,
        event=_provider_event(),
        actor=ActorType.SYSTEM,
        occurred_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE provider_usage_ledger SET total_tokens = 999")

    with pytest.raises(PersistedDataIntegrityError, match="provider usage"):
        repository.list_provider_usage(case.case_id)


def test_workflow_corruption_is_wrapped_as_integrity_error(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.db"
    service, repository = _service(database_path)
    record = service.create_case()
    service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE workflow_events "
            "SET event_json = json_set(event_json, '$.event.forbiddenValue', 'secret')"
        )

    with pytest.raises(PersistedDataIntegrityError, match="workflow"):
        repository.list_workflow_events(record.case_id)


def test_transcript_confirmation_binds_case_id_version_hash_and_stores_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "transcript.db"
    service, repository = _service(database_path)
    record = service.create_case()
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    record = service.save_intake_summary(
        record.case_id,
        expected_version=record.version,
        summary=_pending_summary(),
    )
    waiting = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
    )
    transcript = repository.get_transcript(record.case_id)
    assert transcript is not None
    assert transcript.bound_case_version == waiting.version
    assert transcript.version == 1
    assert transcript.confirmed is False
    with sqlite3.connect(database_path) as connection:
        transcript_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(case_transcripts)")
        }
    assert transcript_columns == {
        "transcript_id",
        "case_id",
        "version",
        "bound_case_version",
        "transcript_sha256",
        "local_ref",
        "confirmed",
        "created_at",
        "confirmed_at",
    }
    assert {"text", "audio", "content", "transcript_text"}.isdisjoint(transcript_columns)

    wrong_case = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": "case-other",
            "transcriptId": transcript.transcript_id,
            "transcriptSha256": transcript.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    with pytest.raises(CaseSnapshotValidationError, match="caseId"):
        service.confirm_transcript(
            waiting.case_id,
            expected_case_version=waiting.version,
            confirmation=wrong_case,
        )

    wrong_id = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": "transcript-wrong-id",
            "transcriptSha256": transcript.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    with pytest.raises(TranscriptStateError):
        service.confirm_transcript(
            waiting.case_id,
            expected_case_version=waiting.version,
            confirmation=wrong_id,
        )

    wrong_hash = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": transcript.transcript_id,
            "transcriptSha256": "b" * 64,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    with pytest.raises(TranscriptStateError):
        service.confirm_transcript(
            waiting.case_id,
            expected_case_version=waiting.version,
            confirmation=wrong_hash,
        )
    assert service.get_case(waiting.case_id) == waiting

    wrong_case_version = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": transcript.transcript_id,
            "transcriptSha256": transcript.transcript_sha256,
            "expectedVersion": waiting.version - 1,
            "confirmed": True,
        }
    )
    with pytest.raises(CaseVersionConflictError):
        service.confirm_transcript(
            waiting.case_id,
            expected_case_version=waiting.version,
            confirmation=wrong_case_version,
        )

    confirmation = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": transcript.transcript_id,
            "transcriptSha256": transcript.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    audit_before = repository.list_audit_events(waiting.case_id)

    def fail_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected transcript projection failure")

    monkeypatch.setattr(repository, "_insert_workflow_projection", fail_projection)
    with pytest.raises(RuntimeError, match="transcript projection"):
        service.confirm_transcript(
            waiting.case_id,
            expected_case_version=waiting.version,
            confirmation=confirmation,
        )
    assert service.get_case(waiting.case_id) == waiting
    assert repository.get_transcript(waiting.case_id) == transcript
    assert repository.list_audit_events(waiting.case_id) == audit_before
    monkeypatch.undo()

    barrier = Barrier(2)

    def confirm_once() -> str:
        barrier.wait()
        try:
            result = service.confirm_transcript(
                waiting.case_id,
                expected_case_version=waiting.version,
                confirmation=confirmation,
            )
        except CaseVersionConflictError:
            return "stale"
        assert result.case.state is CaseState.ANALYZING
        assert result.transcript.confirmed is True
        assert result.transcript.version == 2
        return "confirmed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: confirm_once(), range(2)))
    assert sorted(outcomes) == ["confirmed", "stale"]


def test_capabilities_store_digest_only_revoke_prior_open_and_cascade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capability.db"
    service, repository = _service(database_path)
    case = service.create_case()
    raw_token = b"cdh1_private_raw_token_never_persist"
    first_digest = hashlib.sha256(raw_token).digest()
    second_digest = hashlib.sha256(b"replacement").digest()

    first = repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=first_digest,
        role="human",
        purpose="human_approve",
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    second = repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=second_digest,
        role="human",
        purpose="human_approve",
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=121),
    )
    assert first.revoked_at is None
    revoked_first = repository.get_authority_capability(first_digest)
    assert revoked_first is not None
    assert revoked_first.revoked_at == NOW + timedelta(seconds=1)
    assert second.revoked_at is None
    assert raw_token not in database_path.read_bytes()

    with pytest.raises(ValueError, match="precede an open"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=hashlib.sha256(b"time-travel").digest(),
            role="human",
            purpose="human_approve",
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="allowed pair"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=hashlib.sha256(b"bad-pair").digest(),
            role="agent",
            purpose="human_approve",
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=b"short",
            role="agent",
            purpose="portal_run",
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="timezone"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=hashlib.sha256(b"naive").digest(),
            role="agent",
            purpose="portal_run",
            issued_at=NOW.replace(tzinfo=None),
            expires_at=(NOW + timedelta(seconds=30)).replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="positive"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=hashlib.sha256(b"zero-ttl").digest(),
            role="agent",
            purpose="portal_run",
            issued_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValueError, match="120"):
        repository.issue_authority_capability(
            case_id=case.case_id,
            expected_case_version=case.version,
            digest=hashlib.sha256(b"too-long").digest(),
            role="agent",
            purpose="portal_run",
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=121),
        )
    with pytest.raises(ValueError, match="precede"):
        repository.revoke_authority_capability(
            second_digest,
            revoked_at=NOW - timedelta(seconds=1),
        )
    assert repository.revoke_authority_capability(
        second_digest,
        revoked_at=NOW + timedelta(seconds=2),
    )
    assert not repository.revoke_authority_capability(
        second_digest,
        revoked_at=NOW + timedelta(seconds=3),
    )
    service.delete_case(case.case_id)
    assert repository.get_authority_capability(second_digest) is None


def test_receipt_surface_is_read_only_and_fails_closed_on_invalid_json(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "receipt.db"
    service, repository = _service(database_path)
    case = service.create_case()
    receipt = _receipt(case.case_id)

    assert not hasattr(repository, "insert_sandbox_receipt")
    invalid_json = receipt.model_dump(mode="json", by_alias=True)
    invalid_json["submittedToRealInsurer"] = True
    missing_boundary_json = receipt.model_dump(mode="json", by_alias=True)
    del missing_boundary_json["redacted"]
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sandbox_receipts (case_id, receipt_json, created_at) VALUES (?, ?, ?)",
                (
                    case.case_id,
                    json.dumps(invalid_json),
                    NOW.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sandbox_receipts (case_id, receipt_json, created_at) VALUES (?, ?, ?)",
                (
                    case.case_id,
                    json.dumps(missing_boundary_json),
                    NOW.isoformat(),
                ),
            )
        connection.execute(
            "INSERT INTO sandbox_receipts (case_id, receipt_json, created_at) VALUES (?, ?, ?)",
            (
                case.case_id,
                receipt.model_dump_json(by_alias=True),
                NOW.isoformat(),
            ),
        )

    stored = repository.get_sandbox_receipt(case.case_id)
    assert stored is not None
    assert stored.receipt == receipt
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE sandbox_receipts "
            "SET receipt_json = json_set(receipt_json, '$.redacted', 0) "
            "WHERE case_id = ?",
            (case.case_id,),
        )

    with pytest.raises(PersistedDataIntegrityError, match="receipt"):
        repository.get_sandbox_receipt(case.case_id)


def test_case_delete_cascades_every_v3_child_projection(tmp_path: Path) -> None:
    database_path = tmp_path / "cascade.db"
    service, repository = _service(database_path)
    case = service.create_case()
    case = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.DISCLOSED,
    )
    case = service.save_intake_summary(
        case.case_id,
        expected_version=case.version,
        summary=_pending_summary(),
    )
    case = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
    )
    repository.append_workflow_event(
        case_id=case.case_id,
        expected_case_version=case.version,
        event=_provider_event(),
        actor=ActorType.SYSTEM,
        occurred_at=NOW,
    )
    repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=hashlib.sha256(b"cascade-capability").digest(),
        role="human",
        purpose="human_approve",
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    receipt = _receipt(case.case_id)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO sandbox_receipts (case_id, receipt_json, created_at) "
            "VALUES (?, ?, ?)",
            (case.case_id, receipt.model_dump_json(by_alias=True), NOW.isoformat()),
        )

    assert repository.delete_case(case.case_id)

    with sqlite3.connect(database_path) as connection:
        for table in (
            "audit_events",
            "gate_decisions",
            "case_media_handles",
            "workflow_events",
            "case_transcripts",
            "provider_usage_ledger",
            "authority_capabilities",
            "sandbox_receipts",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
