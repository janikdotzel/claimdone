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
    PortalVariant,
    ProviderCallWorkflowEvent,
    RetryWorkflowEvent,
    SandboxReceipt,
    StateWorkflowEvent,
    TranscriptConfirmationRequest,
    WorkflowEventEnvelope,
)
from claimdone_api.persistence import (
    CaseRecordVersionConflictError,
    IncompatiblePersistedContractError,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
    TranscriptStateError,
    WorkflowAtomicityError,
)
from claimdone_api.walking_skeleton.legacy_boundary import (
    LegacyWalkingCaseBoundary,
    LegacyWalkingRepository,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
DIGEST = "a" * 64
HAPPY_PATH = Path(__file__).resolve().parents[3] / "contracts/examples/happy_path.json"


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
            PRAGMA application_id = 1128549937;
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


def _legacy_service(
    path: Path,
) -> tuple[LegacyWalkingCaseBoundary, LegacyWalkingRepository]:
    repository = LegacyWalkingRepository(path)
    service = LegacyWalkingCaseBoundary(
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


def _insert_provider_projection(
    repository: SqliteCaseRepository,
    case_id: str,
    event: ProviderCallWorkflowEvent | RetryWorkflowEvent,
    *,
    occurred_at: datetime,
) -> WorkflowEventEnvelope:
    """Exercise the projection transaction without reopening the closed generic API."""

    with repository._write_connection() as connection:
        return repository._insert_redacted_workflow_event(
            connection,
            case_id=case_id,
            event=event,
            actor=ActorType.AGENT,
            occurred_at=occurred_at,
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


def test_literal_v2_unbound_authority_is_rejected_without_relabeling(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v2.db"
    _create_literal_v2_database(database_path)

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT state, version FROM cases WHERE case_id='case-v2'"
        ).fetchone() == ("disclosed", 3)


def test_v2_pending_transcript_without_authority_is_rejected(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v2-pending.db"
    _create_literal_v2_database(database_path, pending_transcript=True)

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


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


def test_current_schema_rejects_v3_claim_packet_without_relabel_or_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "persisted-contract-v3.db"
    repository = SqliteCaseRepository(database_path)
    repository.create_case(
        case_id="case-v3-persisted",
        redacted_metadata={},
        created_at=NOW,
    )
    packet = cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))
    packet.update(
        {
            "contractVersion": "3.0.0",
            "caseId": "case-v3-persisted",
            "state": "created",
            "portalState": "draft",
            "gateDecisions": [],
            "verification": {
                "status": "pending",
                "deterministicMatch": None,
                "modelReportedMismatch": False,
                "fieldResults": [],
                "expectedAttachmentCount": 3,
                "actualAttachmentCount": None,
                "reviewAllowed": False,
                "verifiedAt": None,
            },
        }
    )
    packet_json = json.dumps(
        packet,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET claim_packet_json = ? WHERE case_id = ?",
            (packet_json, "case-v3-persisted"),
        )
        version_before = connection.execute("PRAGMA user_version").fetchone()
        dump_before = tuple(connection.iterdump())
    assert version_before == (6,)

    with pytest.raises(
        PersistedDataIntegrityError,
        match="does not match the current contracts",
    ) as captured:
        SqliteCaseRepository(database_path)
    assert captured.value.__cause__ is not None
    assert "contractVersion" in str(captured.value.__cause__)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == version_before
        assert tuple(connection.iterdump()) == dump_before
        persisted = json.loads(
            str(
                connection.execute(
                    "SELECT claim_packet_json FROM cases WHERE case_id = ?",
                    ("case-v3-persisted",),
                ).fetchone()[0]
            )
        )
    assert persisted["contractVersion"] == "3.0.0"
    assert "expectedAttachmentIds" not in persisted["verification"]
    assert "actualAttachmentIds" not in persisted["verification"]


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
            "CREATE TABLE case_transcripts ("
            "case_id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL)"
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


def test_closed_generic_events_and_provider_projection_are_atomic_and_redacted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "events.db"
    service, repository = _legacy_service(database_path)
    record = service.create_case()
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    clarification = _clarification_event()
    provider = _provider_event()
    for closed in (clarification, provider, _retry_event()):
        with pytest.raises(WorkflowAtomicityError, match="atomic command"):
            repository.append_workflow_event(
                case_id=record.case_id,
                expected_case_version=record.version,
                event=closed,
                actor=ActorType.SYSTEM,
                occurred_at=NOW + timedelta(seconds=2),
            )
    provider_envelope = _insert_provider_projection(
        repository._backend,
        record.case_id,
        provider,
        occurred_at=NOW + timedelta(seconds=3),
    )
    retry_envelope = _insert_provider_projection(
        repository._backend,
        record.case_id,
        _retry_event(),
        occurred_at=NOW + timedelta(seconds=4),
    )
    replay = repository.list_workflow_events(
        record.case_id,
        after=repository.list_workflow_events(record.case_id)[-3].sequence,
    )
    assert tuple(item.envelope for item in replay) == (
        provider_envelope,
        retry_envelope,
    )
    assert service.get_case(record.case_id).version == record.version
    reopened = LegacyWalkingRepository(database_path)
    usage = reopened.list_provider_usage(record.case_id)
    assert tuple(item.source_audit_sequence for item in usage) == (
        provider_envelope.cursor,
        retry_envelope.cursor,
    )
    assert tuple(item.status for item in usage) == (
        "succeeded",
        "retry_scheduled",
    )
    assert tuple(item.call_sequence for item in usage) == (1, 2)
    assert tuple(
        None if item.failure_category is None else item.failure_category.value
        for item in usage
    ) == (None, "timeout")
    assert usage[0].total_tokens == 15
    assert usage[0].estimated_cost_micros == 42
    assert usage[1].total_tokens is None
    assert usage[1].estimated_cost_micros is None
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
    service, repository = _legacy_service(tmp_path / "atomic.db")
    created = service.create_case()

    def fail_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(repository._backend, "_insert_workflow_projection", fail_projection)
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
        _insert_provider_projection(
            repository,
            created.case_id,
            _provider_event(),
            occurred_at=NOW,
        )

    assert repository.list_audit_events(created.case_id) == ()
    assert repository.list_workflow_events(created.case_id) == ()
    assert repository.list_provider_usage(created.case_id) == ()


def test_provider_ledger_corruption_is_wrapped_as_integrity_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger-corrupt.db"
    service, repository = _legacy_service(database_path)
    case = service.create_case()
    _insert_provider_projection(
        repository._backend,
        case.case_id,
        _provider_event(),
        occurred_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE provider_usage_ledger SET total_tokens = 999")

    with pytest.raises(PersistedDataIntegrityError, match="provider usage"):
        repository.list_provider_usage(case.case_id)


@pytest.mark.parametrize("tampering", ("cost", "missing", "unexpected"))
def test_reopen_rejects_provider_ledger_source_mismatches(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"ledger-source-{tampering}.db"
    service, repository = _legacy_service(database_path)
    case = service.create_case()
    case = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.DISCLOSED,
    )
    provider = _insert_provider_projection(
        repository._backend,
        case.case_id,
        _provider_event(),
        occurred_at=NOW + timedelta(seconds=1),
    )

    with sqlite3.connect(database_path) as connection:
        if tampering == "cost":
            connection.execute(
                "UPDATE provider_usage_ledger SET estimated_cost_micros = 777 "
                "WHERE source_audit_sequence = ?",
                (provider.cursor,),
            )
        elif tampering == "missing":
            connection.execute(
                "DELETE FROM provider_usage_ledger WHERE source_audit_sequence = ?",
                (provider.cursor,),
            )
        else:
            state_row = connection.execute(
                "SELECT source_audit_sequence FROM workflow_events "
                "WHERE event_kind = 'state'"
            ).fetchone()
            assert state_row is not None
            state_sequence = int(state_row[0])
            connection.execute(
                """
                INSERT INTO provider_usage_ledger (
                    source_audit_sequence, case_id, operation, model_id, provider_mode,
                    call_sequence, retry_attempt, duration_ms, status, input_tokens,
                    output_tokens, total_tokens, estimated_cost_micros, currency,
                    pricing_snapshot_id, failure_category, occurred_at
                )
                SELECT ?, case_id, operation, model_id, provider_mode,
                    call_sequence, retry_attempt, duration_ms, status, input_tokens,
                    output_tokens, total_tokens, estimated_cost_micros, currency,
                    pricing_snapshot_id, failure_category, occurred_at
                FROM provider_usage_ledger
                WHERE source_audit_sequence = ?
                """,
                (state_sequence, provider.cursor),
            )

    with pytest.raises(PersistedDataIntegrityError):
        LegacyWalkingRepository(database_path)


def test_workflow_corruption_is_wrapped_as_integrity_error(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.db"
    service, repository = _legacy_service(database_path)
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


@pytest.mark.parametrize("tampering", ("state_target", "gate_id", "missing"))
def test_reopen_rejects_audit_projection_source_tampering(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"projection-source-{tampering}.db"
    service, _repository = _legacy_service(database_path)
    case = service.create_case()
    if tampering == "gate_id":
        legacy_service = service
        g0 = _gate()
        g1_data = g0.model_dump(mode="json", by_alias=True)
        g1_data.update(
            {
                "gateId": GateId.G1_PRIVACY,
                "decidedAt": NOW + timedelta(seconds=1),
            }
        )
        case = legacy_service.commit_gate_phase(
            case.case_id,
            expected_version=case.version,
            decisions=(g0, GateDecision.model_validate(g1_data)),
        )
    transition_service = service
    case = transition_service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.DISCLOSED,
    )

    with sqlite3.connect(database_path) as connection:
        if tampering == "state_target":
            connection.execute(
                "UPDATE workflow_events "
                "SET event_json = json_set(event_json, '$.event.toState', 'abandoned') "
                "WHERE event_kind = 'state'"
            )
        elif tampering == "gate_id":
            connection.execute(
                "UPDATE workflow_events "
                "SET event_json = json_set(event_json, '$.event.decision.gateId', 'G1') "
                "WHERE event_kind = 'gate'"
            )
        else:
            connection.execute("DELETE FROM workflow_events WHERE event_kind = 'state'")

    with pytest.raises(PersistedDataIntegrityError):
        LegacyWalkingRepository(database_path)


def test_transcript_confirmation_binds_case_id_version_hash_and_stores_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "transcript.db"
    service, repository = _legacy_service(database_path)
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

    monkeypatch.setattr(repository._backend, "_insert_workflow_projection", fail_projection)
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


def test_transcript_identity_is_derived_at_save_confirm_and_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "transcript-source-binding.db"
    service, repository = _legacy_service(database_path)
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
    event = build_state_change_event(
        case_id=case.case_id,
        current=case.state,
        target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
        actor=ActorType.SYSTEM,
        occurred_at=NOW + timedelta(seconds=1),
        event_id_factory=lambda: "audit-forged-transcript",
    )
    forged_id = f"transcript-{'f' * 32}"
    transcript_ref = f"transcript-{'3' * 32}.txt"
    forged_ref = f"transcript-{'4' * 32}.txt"
    identity = hashlib.sha256(
        f"claimdone-transcript-v1\0{case.case_id}\0{transcript_ref}\0{DIGEST}".encode()
    ).hexdigest()
    derived_transcript_id = f"transcript-{identity[:32]}"
    for transcript_id, transcript_hash, local_ref in (
        (forged_id, DIGEST, transcript_ref),
        (derived_transcript_id, "b" * 64, transcript_ref),
        (derived_transcript_id, DIGEST, forged_ref),
    ):
        with pytest.raises(TranscriptStateError, match="mismatched"):
            repository.save_pending_transcript_and_transition(
                case_id=case.case_id,
                expected_case_version=case.version,
                transcript_id=transcript_id,
                transcript_sha256=transcript_hash,
                local_ref=local_ref,
                updated_at=event.occurred_at,
            )
    assert service.get_case(case.case_id) == case
    assert repository.get_transcript(case.case_id) is None

    waiting = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
    )
    transcript = repository.get_transcript(case.case_id)
    assert transcript is not None
    confirmation = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": case.case_id,
            "transcriptId": transcript.transcript_id,
            "transcriptSha256": transcript.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE case_transcripts SET transcript_sha256 = ? WHERE case_id = ?",
            ("b" * 64, case.case_id),
        )

    with pytest.raises(TranscriptStateError, match="stale"):
        service.confirm_transcript(
            case.case_id,
            expected_case_version=waiting.version,
            confirmation=confirmation,
        )
    with pytest.raises(PersistedDataIntegrityError):
        LegacyWalkingRepository(database_path)


@pytest.mark.parametrize("tampering", ("changed", "removed"))
def test_case_update_cannot_invalidate_bound_transcript_summary(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"transcript-write-binding-{tampering}.db"
    service, repository = _legacy_service(database_path)
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
    waiting = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
    )
    transcript = repository.get_transcript(case.case_id)
    assert transcript is not None

    replacement: dict[str, Any] | None = _pending_summary()
    if tampering == "changed":
        assert replacement is not None
        statement = cast(dict[str, Any], replacement["statement"])
        statement["sha256"] = "b" * 64
    else:
        replacement = None
    with pytest.raises(TranscriptStateError, match="bound transcript"):
        service.save_intake_summary(
            case.case_id,
            expected_version=waiting.version,
            summary=replacement,
        )

    assert service.get_case(case.case_id) == waiting
    assert repository.get_transcript(case.case_id) == transcript
    reopened = LegacyWalkingRepository(database_path)
    assert reopened.get_case(case.case_id) == waiting
    assert reopened.get_transcript(case.case_id) == transcript


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
        portal_variant=PortalVariant.A,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    second = repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=second_digest,
        role="human",
        purpose="human_approve",
        portal_variant=PortalVariant.A,
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
            portal_variant=PortalVariant.A,
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


def test_capability_consumption_after_expiry_is_rejected_but_late_revocation_is_allowed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capability-expiry.db"
    service, repository = _service(database_path)
    case = service.create_case()
    revocation_digest = hashlib.sha256(b"late-administrative-revocation").digest()
    consumption_digest = hashlib.sha256(b"late-consumption").digest()

    repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=revocation_digest,
        role="agent",
        purpose="portal_run",
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    assert repository.revoke_authority_capability(
        revocation_digest,
        revoked_at=NOW + timedelta(seconds=31),
    )
    revoked = repository.get_authority_capability(revocation_digest)
    assert revoked is not None
    assert revoked.revoked_at == NOW + timedelta(seconds=31)

    repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=consumption_digest,
        role="human",
        purpose="human_approve",
        portal_variant=PortalVariant.A,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE authority_capabilities SET consumed_at = ? "
            "WHERE capability_digest = ?",
            ((NOW + timedelta(seconds=31)).isoformat(), consumption_digest),
        )

    with pytest.raises(PersistedDataIntegrityError, match="after expiry"):
        repository.get_authority_capability(consumption_digest)
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("tampering", ("future_version", "before_case"))
def test_capability_authority_cannot_escape_case_version_or_lifetime(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"capability-authority-{tampering}.db"
    service, repository = _service(database_path)
    case = service.create_case()
    digest = hashlib.sha256(tampering.encode()).digest()
    repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=digest,
        role="agent",
        purpose="portal_run",
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    with sqlite3.connect(database_path) as connection:
        if tampering == "future_version":
            connection.execute(
                "UPDATE authority_capabilities SET bound_case_version = ? "
                "WHERE capability_digest = ?",
                (case.version + 1, digest),
            )
        else:
            connection.execute(
                "UPDATE authority_capabilities SET issued_at = ? "
                "WHERE capability_digest = ?",
                ((NOW - timedelta(seconds=1)).isoformat(), digest),
            )

    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


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

    with pytest.raises(PersistedDataIntegrityError, match="receipt authority"):
        repository.get_sandbox_receipt(case.case_id)
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


def test_canonical_receipt_must_bind_to_current_receipt_case_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "receipt-case-binding.db"
    service, _repository = _service(database_path)
    case = service.create_case()
    receipt = _receipt(case.case_id)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO sandbox_receipts (case_id, receipt_json, created_at) "
            "VALUES (?, ?, ?)",
            (
                case.case_id,
                receipt.model_dump_json(by_alias=True),
                receipt.rendered_at.isoformat(),
            ),
        )

    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


def test_case_delete_cascades_every_v3_child_projection(tmp_path: Path) -> None:
    database_path = tmp_path / "cascade.db"
    service, repository = _legacy_service(database_path)
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
    _insert_provider_projection(
        repository._backend,
        case.case_id,
        _provider_event(),
        occurred_at=NOW,
    )
    repository.issue_authority_capability(
        case_id=case.case_id,
        expected_case_version=case.version,
        digest=hashlib.sha256(b"cascade-capability").digest(),
        role="human",
        purpose="human_approve",
        portal_variant=PortalVariant.A,
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
            "sandbox_receipt_authority",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
