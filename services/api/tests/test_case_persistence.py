"""SQLite, concurrency, workflow, cleanup, and reset tests for BE-001."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseSnapshotValidationError,
    CaseVersionConflictError,
    InvalidCaseStateTransitionError,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    AuditEvent,
    AuditEventType,
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    PortalState,
)
from claimdone_api.persistence import SqliteCaseRepository, UnsupportedSchemaVersionError

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"


@dataclass
class RecordingCleaner:
    deleted_case_ids: list[str] = field(default_factory=list)
    reset_count: int = 0

    def delete_case_resources(self, case_id: str) -> None:
        self.deleted_case_ids.append(case_id)

    def reset_resources(self) -> None:
        self.reset_count += 1


def _gate_decision(gate_id: GateId, *, offset_seconds: int = 0) -> GateDecision:
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": gate_id,
            "deterministicPassed": True,
            "modelBlocked": False,
            "passed": True,
            "reasonCodes": (),
            "evidenceRefs": (),
            "decidedAt": NOW + timedelta(seconds=offset_seconds),
        }
    )


def _service(
    database_path: Path,
    *,
    case_ids: list[str] | None = None,
    cleaner: RecordingCleaner | None = None,
) -> tuple[CaseService, SqliteCaseRepository]:
    ids = iter(case_ids or ["case-persistence-001"])
    repository = SqliteCaseRepository(database_path)
    return (
        CaseService(
            repository,
            resource_cleaner=cleaner,
            now=lambda: NOW,
            case_id_factory=lambda: next(ids),
        ),
        repository,
    )


def _table_count(database_path: Path, table: str) -> int:
    if table not in {"cases", "audit_events", "gate_decisions"}:
        raise ValueError("Unexpected table name")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_schema_migration_enables_wal_and_is_repeatable(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"

    first = SqliteCaseRepository(database_path, busy_timeout_ms=2_500)
    second = SqliteCaseRepository(database_path, busy_timeout_ms=2_500)

    assert first.database_path == second.database_path
    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        schema_version = connection.execute("PRAGMA user_version").fetchone()
        table_rows = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    assert journal_mode == ("wal",)
    assert schema_version == (2,)
    assert {str(row[0]) for row in table_rows} >= {
        "cases",
        "audit_events",
        "gate_decisions",
        "case_media_handles",
    }


def test_schema_version_one_upgrades_without_losing_cases(tmp_path: Path) -> None:
    database_path = tmp_path / "version-one.db"
    repository = SqliteCaseRepository(database_path)
    repository.create_case(
        case_id="case-before-media-mapping",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE case_media_handles")
        connection.execute("PRAGMA user_version = 1")

    upgraded = SqliteCaseRepository(database_path)

    assert upgraded.get_case("case-before-media-mapping") is not None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'case_media_handles'"
        ).fetchone() == ("case_media_handles",)


def test_newer_schema_version_is_never_downgraded(tmp_path: Path) -> None:
    database_path = tmp_path / "future.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than supported"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        schema_version = connection.execute("PRAGMA user_version").fetchone()
        case_table = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'cases'"
        ).fetchone()
    assert schema_version == (3,)
    assert case_table is None


def test_repository_rejects_naive_timestamps_before_persisting(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    repository = SqliteCaseRepository(database_path)

    with pytest.raises(ValueError, match="created_at must include a timezone"):
        repository.create_case(
            case_id="case-naive",
            redacted_metadata={},
            created_at=datetime(2026, 7, 14, 12),
        )

    assert repository.get_case("case-naive") is None

    created = repository.create_case(
        case_id="case-aware",
        redacted_metadata={},
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="updated_at must include a timezone"):
        repository.replace_snapshot(
            case_id=created.case_id,
            expected_version=created.version,
            snapshot=created.snapshot,
            updated_at=datetime(2026, 7, 14, 13),
        )
    assert repository.get_case(created.case_id) == created


def test_repository_rejects_unredacted_metadata_before_persisting(tmp_path: Path) -> None:
    repository = SqliteCaseRepository(tmp_path / "cases.db")

    with pytest.raises(ValueError, match="structural summaries only"):
        repository.create_case(
            case_id="case-unredacted",
            redacted_metadata={"claimantName": "Ada Lovelace"},
            created_at=NOW,
        )

    assert repository.get_case("case-unredacted") is None


def test_noncanonical_pii_like_metadata_key_is_rejected_at_every_write_boundary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cases.db"
    service, repository = _service(database_path)
    pii_like_key = "claimant_Janik_Dotzel"

    with pytest.raises(ValueError, match="canonical allowlist"):
        service.create_case({pii_like_key: "private value"})
    with pytest.raises(ValueError, match="canonical allowlist"):
        repository.create_case(
            case_id="case-noncanonical",
            redacted_metadata={pii_like_key: "text(length=13)"},
            created_at=NOW,
        )

    existing = repository.create_case(
        case_id="case-existing",
        redacted_metadata={"claimantName": "text(length=4)"},
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="canonical allowlist"):
        service.replace_redacted_metadata(
            existing.case_id,
            expected_version=existing.version,
            metadata={pii_like_key: "private value"},
        )

    assert repository.get_case("case-persistence-001") is None
    assert repository.get_case("case-noncanonical") is None
    assert repository.get_case(existing.case_id) == existing
    assert pii_like_key.encode("utf-8") not in database_path.read_bytes()


def test_invalid_transition_is_rejected_without_mutating_case(tmp_path: Path) -> None:
    service, repository = _service(tmp_path / "cases.db")
    created = service.create_case()

    with pytest.raises(InvalidCaseStateTransitionError) as captured:
        service.transition_case(
            created.case_id,
            expected_version=created.version,
            target=CaseState.ANALYZING,
        )

    assert captured.value.current is CaseState.CREATED
    assert captured.value.target is CaseState.ANALYZING
    unchanged = service.get_case(created.case_id)
    assert unchanged.version == 1
    assert unchanged.state is CaseState.CREATED
    assert repository.list_audit_events(created.case_id) == ()


def test_portal_state_cannot_diverge_from_case_state(tmp_path: Path) -> None:
    service, _ = _service(tmp_path / "cases.db")
    created = service.create_case()

    with pytest.raises(CaseSnapshotValidationError, match="requires portal state"):
        service.set_portal_state(
            created.case_id,
            expected_version=created.version,
            portal_state=PortalState.RECEIPT,
        )

    unchanged = service.get_case(created.case_id)
    assert unchanged.version == created.version
    assert unchanged.snapshot.portal_state is PortalState.DRAFT


def test_stale_compare_and_swap_returns_current_version(tmp_path: Path) -> None:
    service, _ = _service(tmp_path / "cases.db")
    created = service.create_case()
    winner = service.save_intake_summary(
        created.case_id,
        expected_version=created.version,
        summary={"source": "first"},
    )

    with pytest.raises(CaseVersionConflictError) as captured:
        service.save_intake_summary(
            created.case_id,
            expected_version=created.version,
            summary={"source": "stale"},
        )

    assert captured.value.expected_version == 1
    assert captured.value.current_version == 2
    assert service.get_case(created.case_id) == winner
    assert winner.snapshot.intake_summary == {"source": "first"}


def test_stale_transition_conflict_precedes_revalidation(tmp_path: Path) -> None:
    service, repository = _service(tmp_path / "cases.db")
    created = service.create_case()
    disclosed = service.transition_case(
        created.case_id,
        expected_version=created.version,
        target=CaseState.DISCLOSED,
    )

    with pytest.raises(CaseVersionConflictError) as captured:
        service.transition_case(
            created.case_id,
            expected_version=created.version,
            target=CaseState.DISCLOSED,
        )

    assert captured.value.current_version == disclosed.version
    assert service.get_case(created.case_id) == disclosed
    assert len(repository.list_audit_events(created.case_id)) == 1


def test_two_real_parallel_updates_have_exactly_one_winner(tmp_path: Path) -> None:
    service, _ = _service(tmp_path / "cases.db")
    created = service.create_case()
    start = Barrier(2)

    def update(label: str) -> Literal["winner", "conflict"]:
        start.wait(timeout=5)
        try:
            service.save_intake_summary(
                created.case_id,
                expected_version=created.version,
                summary={"writer": label},
            )
        except CaseVersionConflictError:
            return "conflict"
        return "winner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(update, ("alpha", "beta")))

    assert outcomes.count("winner") == 1
    assert outcomes.count("conflict") == 1
    persisted = service.get_case(created.case_id)
    assert persisted.version == 2
    assert persisted.snapshot.intake_summary in (
        {"writer": "alpha"},
        {"writer": "beta"},
    )


def test_audit_and_gate_cursors_are_monotone_and_cascade_on_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    cleaner = RecordingCleaner()
    service, repository = _service(database_path, cleaner=cleaner)
    record = service.create_case()
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    record = service.record_gate_decision(
        record.case_id,
        expected_version=record.version,
        decision=_gate_decision(GateId.G0_INTAKE),
    )
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.ANALYZING,
    )
    service.record_gate_decision(
        record.case_id,
        expected_version=record.version,
        decision=_gate_decision(GateId.G1_PRIVACY, offset_seconds=1),
    )

    audit_events = repository.list_audit_events(record.case_id)
    gate_decisions = repository.list_gate_decisions(record.case_id)
    audit_sequences = tuple(item.sequence for item in audit_events)
    gate_sequences = tuple(item.sequence for item in gate_decisions)
    assert audit_sequences == tuple(sorted(audit_sequences))
    assert len(set(audit_sequences)) == len(audit_sequences) == 4
    assert gate_sequences == tuple(sorted(gate_sequences))
    assert len(set(gate_sequences)) == len(gate_sequences) == 2
    assert repository.list_audit_events(
        record.case_id,
        after=audit_sequences[1],
    ) == audit_events[2:]
    assert repository.list_gate_decisions(
        record.case_id,
        after=gate_sequences[0],
    ) == gate_decisions[1:]

    service.delete_case(record.case_id)

    assert cleaner.deleted_case_ids == [record.case_id]
    assert repository.get_case(record.case_id) is None
    assert _table_count(database_path, "cases") == 0
    assert _table_count(database_path, "audit_events") == 0
    assert _table_count(database_path, "gate_decisions") == 0


def test_optional_claim_packet_round_trips_at_matching_state(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path / "cases.db",
        case_ids=["case-happy-001"],
    )
    record = service.create_case()
    for target in (
        CaseState.DISCLOSED,
        CaseState.ANALYZING,
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.VERIFYING,
        CaseState.REVIEW,
    ):
        record = service.transition_case(
            record.case_id,
            expected_version=record.version,
            target=target,
        )
    assert record.snapshot.portal_state is PortalState.REVIEW
    packet = ClaimPacket.model_validate_json(HAPPY_PATH.read_text(encoding="utf-8"))

    stored = service.save_claim_packet(
        record.case_id,
        expected_version=record.version,
        claim_packet=packet,
    )

    assert stored.state is CaseState.REVIEW
    assert stored.snapshot.portal_state is PortalState.REVIEW
    assert stored.snapshot.claim_packet == packet
    assert service.get_case(record.case_id) == stored


def test_demo_reset_is_idempotent_and_preserves_monotone_cursors(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    cleaner = RecordingCleaner()
    service, repository = _service(
        database_path,
        case_ids=["case-reset-001", "case-reset-002", "case-reset-003"],
        cleaner=cleaner,
    )
    first = service.create_case()
    second = service.create_case()
    first = service.transition_case(
        first.case_id,
        expected_version=first.version,
        target=CaseState.DISCLOSED,
    )
    first = service.record_gate_decision(
        first.case_id,
        expected_version=first.version,
        decision=_gate_decision(GateId.G0_INTAKE),
    )
    prior_audit_sequence = repository.list_audit_events(first.case_id)[-1].sequence
    prior_gate_sequence = repository.list_gate_decisions(first.case_id)[-1].sequence

    assert service.reset_demo() == 2
    assert service.reset_demo() == 0
    assert cleaner.reset_count == 2
    assert repository.get_case(first.case_id) is None
    assert repository.get_case(second.case_id) is None

    third = service.create_case()
    third = service.transition_case(
        third.case_id,
        expected_version=third.version,
        target=CaseState.DISCLOSED,
    )
    service.record_gate_decision(
        third.case_id,
        expected_version=third.version,
        decision=_gate_decision(GateId.G0_INTAKE, offset_seconds=2),
        actor=ActorType.SYSTEM,
    )

    assert repository.list_audit_events(third.case_id)[0].sequence > prior_audit_sequence
    assert repository.list_gate_decisions(third.case_id)[0].sequence > prior_gate_sequence
    assert _table_count(database_path, "cases") == 1


def test_raw_claim_values_never_enter_redacted_metadata_or_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    service, repository = _service(database_path)
    raw_value = "Janik Secret Claim Narrative"
    raw_metadata: dict[str, JsonValue] = {
        "claimNarrative": raw_value,
        "imageMetadata": {"gps": "48.1351,11.5820"},
    }
    created = service.create_case(raw_metadata)
    transitioned = service.transition_case(
        created.case_id,
        expected_version=created.version,
        target=CaseState.DISCLOSED,
    )
    service.record_gate_decision(
        transitioned.case_id,
        expected_version=transitioned.version,
        decision=_gate_decision(GateId.G0_INTAKE),
    )

    with sqlite3.connect(database_path) as connection:
        metadata_json = str(
            connection.execute(
                "SELECT redacted_metadata_json FROM cases WHERE case_id = ?",
                (created.case_id,),
            ).fetchone()[0]
        )
        audit_json = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT event_json FROM audit_events WHERE case_id = ? ORDER BY sequence",
                (created.case_id,),
            ).fetchall()
        )
    assert json.loads(metadata_json) == {
        "claimNarrative": "text(length=28)",
        "imageMetadata": "object(keys=1)",
    }
    assert raw_value not in metadata_json
    assert raw_value not in audit_json
    assert "48.1351" not in metadata_json
    assert "48.1351" not in audit_json
    assert all(event.event.details == () for event in repository.list_audit_events(created.case_id))


def test_contract_rejects_free_form_audit_details_before_repository_write(
    tmp_path: Path,
) -> None:
    service, repository = _service(tmp_path / "cases.db")
    created = service.create_case()
    with pytest.raises(ValidationError, match="details"):
        AuditEvent.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "eventId": "event-unsafe-detail",
                "caseId": created.case_id,
                "eventType": AuditEventType.CASE_STATE_CHANGED,
                "actor": ActorType.SYSTEM,
                "occurredAt": NOW,
                "fromState": CaseState.CREATED,
                "toState": CaseState.DISCLOSED,
                "reasonCodes": (),
                "details": (
                    {
                        "key": "claimantName",
                        "valueSummary": "Ada Lovelace",
                        "redacted": True,
                    },
                ),
            }
        )

    unchanged = repository.get_case(created.case_id)
    assert unchanged == created
    assert repository.list_audit_events(created.case_id) == ()
