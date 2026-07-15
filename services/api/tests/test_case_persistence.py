"""SQLite, concurrency, workflow, cleanup, and reset tests for BE-001."""

import json
import sqlite3
import subprocess
import sys
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
    CaseVersionConflictError,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    AuditEvent,
    AuditEventType,
    CaseState,
    GateDecision,
    GateId,
)
from claimdone_api.media import PersistentCaseMediaCleaner
from claimdone_api.persistence import (
    AuthorityModeMismatchError,
    IncompatiblePersistedContractError,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
    UnsupportedSchemaVersionError,
    WorkflowAtomicityError,
)
from claimdone_api.walking_skeleton.legacy_boundary import (
    LegacyWalkingCaseBoundary,
    LegacyWalkingRepository,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"

_CROSS_PROCESS_INITIALIZER = r"""
import json
import sys
import time
from pathlib import Path

mode, database_value, media_value, ready_value, worker_id, source_value = sys.argv[1:]
sys.path.insert(0, source_value)

from claimdone_api.persistence import SqliteCaseRepository
from claimdone_api.walking_skeleton.legacy_boundary import LegacyWalkingRepository

ready_dir = Path(ready_value)
ready_file = ready_dir / f"ready-{worker_id}"
original = SqliteCaseRepository._execute_initialization_statement
synchronized = [False]

def execute_after_peer(repository, connection, statement):
    if not synchronized[0] and statement == "BEGIN EXCLUSIVE":
        synchronized[0] = True
        ready_file.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 15
        while len(tuple(ready_dir.glob("ready-*"))) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("peer initializer did not reach the migration barrier")
            time.sleep(0.001)
    return original(repository, connection, statement)

SqliteCaseRepository._execute_initialization_statement = execute_after_peer
try:
    repository_type = (
        SqliteCaseRepository if mode == "canonical" else LegacyWalkingRepository
    )
    repository_type(database_value, media_root=Path(media_value))
except Exception as error:
    print(json.dumps(["error", type(error).__name__, str(error)]))
else:
    print(json.dumps(["ok", mode, ""]))
"""


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
) -> tuple[CaseService, SqliteCaseRepository]:
    ids = iter(case_ids or ["case-persistence-001"])
    repository = SqliteCaseRepository(database_path)
    return (
        CaseService(
            repository,
            now=lambda: NOW,
            case_id_factory=lambda: next(ids),
        ),
        repository,
    )


def _legacy_service(
    database_path: Path,
    *,
    case_ids: list[str] | None = None,
    cleaner: RecordingCleaner | None = None,
) -> tuple[LegacyWalkingCaseBoundary, LegacyWalkingRepository]:
    ids = iter(case_ids or ["case-persistence-001"])
    repository = LegacyWalkingRepository(database_path)
    return (
        LegacyWalkingCaseBoundary(
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


def _run_cross_process_initializers(
    *,
    modes: tuple[Literal["canonical", "legacy"], Literal["canonical", "legacy"]],
    database_path: Path,
    media_root: Path,
    ready_dir: Path,
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    ready_dir.mkdir()
    processes = tuple(
        subprocess.Popen(
            (
                sys.executable,
                "-c",
                _CROSS_PROCESS_INITIALIZER,
                mode,
                str(database_path),
                str(media_root),
                str(ready_dir),
                str(index),
                str(REPOSITORY_ROOT / "services" / "api" / "src"),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index, mode in enumerate(modes)
    )
    outcomes: list[tuple[str, str, str]] = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            payload = json.loads(stdout)
            assert isinstance(payload, list) and len(payload) == 3
            outcomes.append((str(payload[0]), str(payload[1]), str(payload[2])))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    assert len(outcomes) == 2
    return outcomes[0], outcomes[1]


def _database_identity_and_dump(
    database_path: Path,
) -> tuple[int, int, tuple[str, ...]]:
    with sqlite3.connect(database_path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
        dump = tuple(connection.iterdump())
    assert application_id is not None
    assert user_version is not None
    return int(application_id[0]), int(user_version[0]), dump


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
    assert schema_version == (5,)
    assert {str(row[0]) for row in table_rows} >= {
        "cases",
        "audit_events",
        "gate_decisions",
        "case_media_handles",
        "workflow_events",
        "case_transcripts",
        "provider_usage_ledger",
        "authority_capabilities",
        "sandbox_receipts",
        "case_intake_authority",
        "case_transcript_authority",
        "case_packet_authority",
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
        connection.execute("DROP TABLE case_packet_authority")
        connection.execute("DROP TABLE case_transcript_authority")
        connection.execute("DROP TABLE case_intake_authority")
        connection.execute("DROP TABLE sandbox_receipts")
        connection.execute("DROP TABLE authority_capabilities")
        connection.execute("DROP TABLE provider_usage_ledger")
        connection.execute("DROP TABLE case_transcripts")
        connection.execute("DROP TABLE workflow_events")
        connection.execute("DROP INDEX audit_events_projection_source_idx")
        connection.execute("DROP TABLE case_media_handles")
        connection.execute("PRAGMA user_version = 1")

    upgraded = SqliteCaseRepository(database_path)

    assert upgraded.get_case("case-before-media-mapping") is not None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'case_media_handles'"
        ).fetchone() == ("case_media_handles",)


def test_newer_schema_version_is_never_downgraded(tmp_path: Path) -> None:
    database_path = tmp_path / "future.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA application_id = 1128549937")
        connection.execute("PRAGMA user_version = 6")

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than supported"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        schema_version = connection.execute("PRAGMA user_version").fetchone()
        case_table = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'cases'"
        ).fetchone()
    assert schema_version == (6,)
    assert case_table is None


def test_unmarked_current_databases_are_never_adopted_by_either_authority(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.db"
    canonical = SqliteCaseRepository(canonical_path)
    canonical.create_case(
        case_id="case-unmarked-canonical",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(canonical_path) as connection:
        connection.execute("PRAGMA application_id = 0")

    with pytest.raises(AuthorityModeMismatchError, match="unmarked database"):
        SqliteCaseRepository(canonical_path)
    with sqlite3.connect(canonical_path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)

    legacy_path = tmp_path / "legacy.db"
    legacy = LegacyWalkingRepository(legacy_path)
    legacy.create_case(
        case_id="case-unmarked-legacy",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("PRAGMA application_id = 0")

    with pytest.raises(AuthorityModeMismatchError, match="unmarked database"):
        LegacyWalkingRepository(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_fresh_identity_requires_an_empty_user_schema_and_claims_no_media_root(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unmarked.db"
    media_root = tmp_path / "unclaimed-media"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")

    with pytest.raises(AuthorityModeMismatchError, match="cannot be adopted"):
        SqliteCaseRepository(database_path, media_root=media_root)

    assert not media_root.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'unrelated'"
        ).fetchone() == ("unrelated",)


def test_invalid_current_payload_claims_neither_identity_nor_selected_media_root(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid.db"
    repository = SqliteCaseRepository(database_path)
    created = repository.create_case(
        case_id="case-invalid-preflight",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        connection.execute(
            "UPDATE cases SET state = 'ready_to_fill' WHERE case_id = ?",
            (created.case_id,),
        )
    untouched_parent = tmp_path / "must-not-be-touched"
    unclaimed_root = untouched_parent / "media"

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path, media_root=unclaimed_root)

    assert not unclaimed_root.exists()
    assert not untouched_parent.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == application_id


@pytest.mark.parametrize("attempt", range(20))
def test_canonical_and_legacy_initializers_cannot_race_to_claim_one_database(
    tmp_path: Path,
    attempt: int,
) -> None:
    database_path = tmp_path / f"raced-{attempt}.db"
    media_root = tmp_path / f"raced-media-{attempt}"
    barrier = Barrier(2)

    def construct(mode: Literal["canonical", "legacy"]) -> object:
        barrier.wait()
        try:
            if mode == "canonical":
                return SqliteCaseRepository(database_path, media_root=media_root)
            return LegacyWalkingRepository(database_path, media_root=media_root)
        except AuthorityModeMismatchError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(construct, ("canonical", "legacy"))
        )

    winners = tuple(
        result
        for result in results
        if isinstance(result, SqliteCaseRepository | LegacyWalkingRepository)
    )
    losers = tuple(
        result for result in results if isinstance(result, AuthorityModeMismatchError)
    )
    assert len(winners) == 1
    assert len(losers) == 1
    with sqlite3.connect(database_path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        assert application_id in {(1128549937,), (1128549425,)}
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


@pytest.mark.parametrize("mode", ("canonical", "legacy"))
def test_same_mode_first_open_is_idempotent_across_processes(
    tmp_path: Path,
    mode: Literal["canonical", "legacy"],
) -> None:
    outcomes = _run_cross_process_initializers(
        modes=(mode, mode),
        database_path=tmp_path / f"same-mode-{mode}.db",
        media_root=tmp_path / f"same-mode-{mode}-media",
        ready_dir=tmp_path / f"same-mode-{mode}-ready",
    )

    assert outcomes == (("ok", mode, ""), ("ok", mode, ""))


def test_opposite_modes_remain_exclusive_across_processes(tmp_path: Path) -> None:
    database_path = tmp_path / "opposite-mode.db"
    outcomes = _run_cross_process_initializers(
        modes=("canonical", "legacy"),
        database_path=database_path,
        media_root=tmp_path / "opposite-mode-media",
        ready_dir=tmp_path / "opposite-mode-ready",
    )

    successes = tuple(outcome for outcome in outcomes if outcome[0] == "ok")
    failures = tuple(outcome for outcome in outcomes if outcome[0] == "error")
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][1] == "AuthorityModeMismatchError"
    with sqlite3.connect(database_path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        assert application_id in {(1128549937,), (1128549425,)}
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_rejected_v4_to_v5_migration_rolls_back_schema_and_content(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-v4.db"
    repository = SqliteCaseRepository(database_path)
    created = repository.create_case(
        case_id="case-invalid-v4",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE case_packet_authority")
        connection.execute("PRAGMA user_version = 4")
        connection.execute(
            "UPDATE cases SET intake_summary_json = '{}' WHERE case_id = ?",
            (created.case_id,),
        )
    before = _database_identity_and_dump(database_path)

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    after = _database_identity_and_dump(database_path)
    assert after == before
    assert after[1] == 4
    assert all("case_packet_authority" not in statement for statement in after[2])


def test_v3_canonical_migration_rejects_every_populated_authority_child(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v3-child.db"
    repository = SqliteCaseRepository(database_path)
    created = repository.create_case(
        case_id="case-v3-child",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE case_packet_authority")
        connection.execute("DROP TABLE case_transcript_authority")
        connection.execute("DROP TABLE case_intake_authority")
        connection.execute(
            "INSERT INTO case_media_handles (case_id, storage_name, created_at) "
            "VALUES (?, ?, ?)",
            (created.case_id, f"case-{'d' * 32}", NOW.isoformat()),
        )
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute(
            "SELECT storage_name FROM case_media_handles WHERE case_id = ?",
            (created.case_id,),
        ).fetchone() == (f"case-{'d' * 32}",)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'case_intake_authority'"
        ).fetchone() is None


def test_media_cleaner_and_case_service_require_one_repository_owned_store(
    tmp_path: Path,
) -> None:
    first = SqliteCaseRepository(tmp_path / "first.db")
    second = SqliteCaseRepository(tmp_path / "second.db")

    with pytest.raises(ValueError, match="repository-owned"):
        PersistentCaseMediaCleaner(first, second.media_store)

    first_cleaner = PersistentCaseMediaCleaner(first, first.media_store)
    with pytest.raises(TypeError, match="exact repository and media store"):
        CaseService(second, resource_cleaner=first_cleaner)


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
    assert not hasattr(repository, "replace_snapshot")
    assert repository.get_case(created.case_id) == created


def test_persisted_case_creation_cannot_follow_its_latest_update(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reversed-case-lifetime.db"
    repository = SqliteCaseRepository(database_path)
    created = repository.create_case(
        case_id="case-reversed-lifetime",
        redacted_metadata={},
        created_at=NOW,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET created_at = ? WHERE case_id = ?",
            (
                (created.updated_at + timedelta(seconds=1)).isoformat(),
                created.case_id,
            ),
        )

    with pytest.raises(PersistedDataIntegrityError, match="cannot follow"):
        repository.get_case(created.case_id)
    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


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

    assert repository.get_case("case-persistence-001") is None
    assert repository.get_case("case-noncanonical") is None
    assert not hasattr(service, "replace_redacted_metadata")
    assert not hasattr(repository, "replace_snapshot")
    assert pii_like_key.encode("utf-8") not in database_path.read_bytes()


def test_invalid_transition_is_rejected_without_mutating_case(tmp_path: Path) -> None:
    service, repository = _service(tmp_path / "cases.db")
    created = service.create_case()

    with pytest.raises(WorkflowAtomicityError, match="no canonical intake authority"):
        repository.begin_text_analysis(
            case_id=created.case_id,
            expected_version=created.version,
            updated_at=NOW,
        )

    assert not hasattr(service, "transition_case")
    assert not hasattr(repository, "transition_case")
    unchanged = service.get_case(created.case_id)
    assert unchanged.version == 1
    assert unchanged.state is CaseState.CREATED
    assert repository.list_audit_events(created.case_id) == ()


def test_production_repository_closes_all_generic_analysis_authority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "authority.db"
    service, repository = _service(database_path)
    disclosed = service.create_case()

    for writer in (
        "replace_redacted_metadata",
        "save_intake_summary",
        "save_active_clarification",
        "save_claim_packet",
        "save_pending_transcript_and_transition",
    ):
        assert not hasattr(service, writer)

    assert not hasattr(repository, "transition_case")
    assert not hasattr(service, "transition_case")

    assert not hasattr(service, "record_gate_decision")
    assert not hasattr(repository, "record_gate_decision")
    assert not hasattr(service, "commit_deterministic_gate_batch")
    assert not hasattr(repository, "commit_deterministic_gate_batch")
    assert not hasattr(repository, "replace_snapshot")
    assert not hasattr(repository, "save_pending_transcript_and_transition")
    with pytest.raises(WorkflowAtomicityError, match="commit_intake_disclosure"):
        repository.bind_case_media_handle(
            case_id=disclosed.case_id,
            storage_name=f"case-{'a' * 32}",
            created_at=NOW,
        )
    assert service.get_case(disclosed.case_id) == disclosed
    assert repository.list_gate_decisions(disclosed.case_id) == ()
    assert repository.list_audit_events(disclosed.case_id) == ()


def test_canonical_repository_rejects_legacy_database_mode(tmp_path: Path) -> None:
    database_path = tmp_path / "ready-authority.db"
    legacy_service, _legacy_repository = _legacy_service(database_path)
    case = legacy_service.create_case()
    for target in (
        CaseState.DISCLOSED,
        CaseState.ANALYZING,
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
    ):
        case = legacy_service.transition_case(
            case.case_id,
            expected_version=case.version,
            target=target,
        )

    with pytest.raises(AuthorityModeMismatchError, match="authority mode"):
        SqliteCaseRepository(database_path)

    assert not isinstance(_legacy_repository, SqliteCaseRepository)
    with pytest.raises(TypeError, match="exact canonical repository"):
        CaseService(_legacy_repository._backend)


def test_portal_state_cannot_diverge_from_case_state(tmp_path: Path) -> None:
    service, repository = _service(tmp_path / "cases.db")
    created = service.create_case()

    assert not hasattr(repository, "replace_snapshot")
    assert not hasattr(service, "replace_redacted_metadata")
    assert not hasattr(service, "set_portal_state")

    before = (
        repository.list_audit_events(created.case_id),
        repository.list_gate_decisions(created.case_id),
        repository.list_workflow_events(created.case_id),
    )
    with pytest.raises(AttributeError):
        object.__getattribute__(repository, "replace_snapshot")
    with pytest.raises(AttributeError):
        object.__getattribute__(service, "replace_redacted_metadata")
    unchanged = service.get_case(created.case_id)
    assert unchanged == created
    assert (
        repository.list_audit_events(created.case_id),
        repository.list_gate_decisions(created.case_id),
        repository.list_workflow_events(created.case_id),
    ) == before


def test_stale_compare_and_swap_returns_current_version(tmp_path: Path) -> None:
    service, _ = _legacy_service(tmp_path / "cases.db")
    created = service.create_case()
    winner = service.save_intake_summary(
        created.case_id,
        expected_version=created.version,
        summary={"winner": "first"},
    )

    with pytest.raises(CaseVersionConflictError) as captured:
        service.save_intake_summary(
            created.case_id,
            expected_version=created.version,
            summary={"winner": "stale"},
        )

    assert captured.value.expected_version == 1
    assert captured.value.current_version == 2
    assert service.get_case(created.case_id) == winner
    assert winner.snapshot.intake_summary == {"winner": "first"}


def test_stale_transition_conflict_precedes_revalidation(tmp_path: Path) -> None:
    service, repository = _legacy_service(tmp_path / "cases.db")
    created = service.create_case()
    disclosed = service.save_intake_summary(
        created.case_id,
        expected_version=created.version,
        summary={"winner": True},
    )

    with pytest.raises(CaseVersionConflictError) as captured:
        service.save_intake_summary(
            created.case_id,
            expected_version=created.version,
            summary={"stale": True},
        )

    assert captured.value.current_version == disclosed.version
    assert service.get_case(created.case_id) == disclosed
    assert repository.list_audit_events(created.case_id) == ()


def test_two_real_parallel_updates_have_exactly_one_winner(tmp_path: Path) -> None:
    service, _ = _legacy_service(tmp_path / "cases.db")
    created = service.create_case()
    start = Barrier(2)

    def update(label: str) -> Literal["winner", "conflict"]:
        start.wait(timeout=5)
        try:
            service.save_intake_summary(
                created.case_id,
                expected_version=created.version,
                summary={"label": label},
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
    assert persisted.snapshot.intake_summary in ({"label": "alpha"}, {"label": "beta"})


def test_audit_and_gate_cursors_are_monotone_and_cascade_on_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    cleaner = RecordingCleaner()
    service, repository = _legacy_service(database_path, cleaner=cleaner)
    record = service.create_case()
    record = service.commit_gate_phase(
        record.case_id,
        expected_version=record.version,
        decisions=(
            _gate_decision(GateId.G0_INTAKE),
            _gate_decision(GateId.G1_PRIVACY),
        ),
    )
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.DISCLOSED,
    )
    record = service.transition_case(
        record.case_id,
        expected_version=record.version,
        target=CaseState.ANALYZING,
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


def test_generic_analysis_snapshot_writers_are_not_exposed(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path / "cases.db",
        case_ids=["case-happy-001"],
    )
    record = service.create_case()
    assert not hasattr(service, "save_intake_summary")
    assert not hasattr(service, "save_active_clarification")
    assert not hasattr(service, "save_claim_packet")
    assert not hasattr(service, "transition_case")
    assert service.get_case(record.case_id) == record


def test_demo_reset_is_idempotent_and_preserves_monotone_cursors(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    cleaner = RecordingCleaner()
    service, repository = _legacy_service(
        database_path,
        case_ids=["case-reset-001", "case-reset-002", "case-reset-003"],
        cleaner=cleaner,
    )
    first = service.create_case()
    second = service.create_case()
    first = service.commit_gate_phase(
        first.case_id,
        expected_version=first.version,
        decisions=(
            _gate_decision(GateId.G0_INTAKE),
            _gate_decision(GateId.G1_PRIVACY),
        ),
    )
    first = service.transition_case(
        first.case_id,
        expected_version=first.version,
        target=CaseState.DISCLOSED,
    )
    prior_audit_sequence = repository.list_audit_events(first.case_id)[-1].sequence
    prior_gate_sequence = repository.list_gate_decisions(first.case_id)[-1].sequence

    assert service.reset_demo() == 2
    assert service.reset_demo() == 0
    assert cleaner.reset_count == 2
    assert repository.get_case(first.case_id) is None
    assert repository.get_case(second.case_id) is None

    third = service.create_case()
    third = service.commit_gate_phase(
        third.case_id,
        expected_version=third.version,
        decisions=(
            _gate_decision(GateId.G0_INTAKE),
            _gate_decision(GateId.G1_PRIVACY),
        ),
    )
    service.transition_case(
        third.case_id,
        expected_version=third.version,
        target=CaseState.DISCLOSED,
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
