"""Focused delegation tests for the CaseService G6-G8 boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Never, cast

import pytest

from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.contracts import GateDecision
from claimdone_api.persistence import (
    AuthorityCapabilityError,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    PersistedDataIntegrityError,
    PortalRunRecord,
    PortalRunStartCommand,
    PortalRunStartResult,
    PortalWriteFinalizeCommand,
    PortalWriteFinalizeResult,
    SqliteCaseRepository,
    VerificationAttemptCommand,
    VerificationAttemptResult,
    WorkflowAtomicityError,
)

CASE_ID = "case-wrapper-001"
RUN_ID = "run-wrapper-001"
ATTEMPT_ID = "attempt-wrapper-001"
CONTROL_DIGEST = b"c" * 32
DECIDED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)

WrapperCall = Callable[[CaseService], object]


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[SqliteCaseRepository]:
    value = SqliteCaseRepository(tmp_path / "cases.db")
    try:
        yield value
    finally:
        value.media_store.close()


@pytest.fixture
def service(repository: SqliteCaseRepository) -> CaseService:
    return CaseService(repository)


def _start_command() -> PortalRunStartCommand:
    return cast(PortalRunStartCommand, SimpleNamespace(case_id=CASE_ID))


def _finalize_command() -> PortalWriteFinalizeCommand:
    return cast(PortalWriteFinalizeCommand, SimpleNamespace(case_id=CASE_ID))


def _attempt_command() -> VerificationAttemptCommand:
    return cast(VerificationAttemptCommand, SimpleNamespace(case_id=CASE_ID))


def _call_start(service: CaseService) -> object:
    return service.start_portal_run(_start_command())


def _call_preflight(service: CaseService) -> object:
    return service.preflight_portal_write(
        case_id=CASE_ID,
        expected_case_version=6,
        run_id=RUN_ID,
        control_digest=CONTROL_DIGEST,
        fields_payload={"location": "Berlin"},
        decided_at=DECIDED_AT,
    )


def _call_finalize(service: CaseService) -> object:
    return service.finalize_portal_write(_finalize_command())


def _call_record(service: CaseService) -> object:
    return service.record_verification_attempt(_attempt_command())


def _call_resolve_run(service: CaseService) -> object:
    return service.resolve_portal_run(RUN_ID, CONTROL_DIGEST)


def _call_resolve_attempt(service: CaseService) -> object:
    return service.resolve_verification_attempt(
        case_id=CASE_ID,
        run_id=RUN_ID,
        control_digest=CONTROL_DIGEST,
        attempt_id=ATTEMPT_ID,
    )


ALL_WRAPPERS: tuple[tuple[str, WrapperCall], ...] = (
    ("start_portal_run", _call_start),
    ("preflight_portal_write", _call_preflight),
    ("finalize_portal_write", _call_finalize),
    ("record_verification_attempt", _call_record),
    ("resolve_portal_run", _call_resolve_run),
    ("resolve_verification_attempt", _call_resolve_attempt),
)

CASE_BOUND_WRAPPERS: tuple[tuple[str, WrapperCall], ...] = (
    ("start_portal_run", _call_start),
    ("preflight_portal_write", _call_preflight),
    ("finalize_portal_write", _call_finalize),
    ("record_verification_attempt", _call_record),
    ("resolve_verification_attempt", _call_resolve_attempt),
)

VERSIONED_WRAPPERS: tuple[tuple[str, WrapperCall], ...] = CASE_BOUND_WRAPPERS[:4]


def test_mutation_wrappers_delegate_exact_commands_and_preflight_inputs(
    service: CaseService,
    repository: SqliteCaseRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_command = _start_command()
    finalize_command = _finalize_command()
    attempt_command = _attempt_command()
    fields_payload = {"location": "Berlin"}
    start_result = cast(PortalRunStartResult, object())
    gate_decision = cast(GateDecision, object())
    finalize_result = cast(PortalWriteFinalizeResult, object())
    attempt_result = cast(VerificationAttemptResult, object())

    def start(command: PortalRunStartCommand) -> PortalRunStartResult:
        assert command is start_command
        return start_result

    def preflight(
        *,
        case_id: str,
        expected_case_version: int,
        run_id: str,
        control_digest: bytes,
        fields_payload: object,
        decided_at: datetime,
    ) -> GateDecision:
        assert case_id == CASE_ID
        assert expected_case_version == 6
        assert run_id == RUN_ID
        assert control_digest == CONTROL_DIGEST
        assert fields_payload is fields_payload_value
        assert decided_at == DECIDED_AT
        return gate_decision

    def finalize(command: PortalWriteFinalizeCommand) -> PortalWriteFinalizeResult:
        assert command is finalize_command
        return finalize_result

    def record(command: VerificationAttemptCommand) -> VerificationAttemptResult:
        assert command is attempt_command
        return attempt_result

    fields_payload_value = fields_payload
    monkeypatch.setattr(repository, "start_portal_run", start)
    monkeypatch.setattr(repository, "preflight_portal_write", preflight)
    monkeypatch.setattr(repository, "finalize_portal_write", finalize)
    monkeypatch.setattr(repository, "record_verification_attempt", record)

    assert service.start_portal_run(start_command) is start_result
    assert (
        service.preflight_portal_write(
            case_id=CASE_ID,
            expected_case_version=6,
            run_id=RUN_ID,
            control_digest=CONTROL_DIGEST,
            fields_payload=fields_payload,
            decided_at=DECIDED_AT,
        )
        is gate_decision
    )
    assert service.finalize_portal_write(finalize_command) is finalize_result
    assert service.record_verification_attempt(attempt_command) is attempt_result


def test_recovery_wrappers_delegate_exact_recovery_identity(
    service: CaseService,
    repository: SqliteCaseRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_result = cast(PortalRunRecord, object())
    attempt_result = cast(VerificationAttemptResult, object())

    def resolve_run(run_id: str, control_digest: bytes) -> PortalRunRecord | None:
        assert run_id == RUN_ID
        assert control_digest == CONTROL_DIGEST
        return run_result

    def resolve_attempt(
        *,
        case_id: str,
        run_id: str,
        control_digest: bytes,
        attempt_id: str,
    ) -> VerificationAttemptResult | None:
        assert case_id == CASE_ID
        assert run_id == RUN_ID
        assert control_digest == CONTROL_DIGEST
        assert attempt_id == ATTEMPT_ID
        return attempt_result

    monkeypatch.setattr(repository, "resolve_portal_run", resolve_run)
    monkeypatch.setattr(repository, "resolve_verification_attempt", resolve_attempt)

    assert service.resolve_portal_run(RUN_ID, CONTROL_DIGEST) is run_result
    assert (
        service.resolve_verification_attempt(
            case_id=CASE_ID,
            run_id=RUN_ID,
            control_digest=CONTROL_DIGEST,
            attempt_id=ATTEMPT_ID,
        )
        is attempt_result
    )


@pytest.mark.parametrize(("repository_method", "call"), ALL_WRAPPERS)
@pytest.mark.parametrize(
    "error_type",
    (AuthorityCapabilityError, PersistedDataIntegrityError, WorkflowAtomicityError),
)
def test_wrappers_translate_authority_and_integrity_failures(
    service: CaseService,
    repository: SqliteCaseRepository,
    monkeypatch: pytest.MonkeyPatch,
    repository_method: str,
    call: WrapperCall,
    error_type: type[Exception],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        raise error_type("repository rejected the command")

    monkeypatch.setattr(repository, repository_method, fail)

    with pytest.raises(
        CaseSnapshotValidationError,
        match="repository rejected the command",
    ):
        call(service)


@pytest.mark.parametrize(("repository_method", "call"), CASE_BOUND_WRAPPERS)
def test_case_bound_wrappers_translate_not_found(
    service: CaseService,
    repository: SqliteCaseRepository,
    monkeypatch: pytest.MonkeyPatch,
    repository_method: str,
    call: WrapperCall,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        raise CaseRecordNotFoundError(CASE_ID)

    monkeypatch.setattr(repository, repository_method, fail)

    with pytest.raises(CaseNotFoundError) as caught:
        call(service)

    assert caught.value.case_id == CASE_ID


@pytest.mark.parametrize(("repository_method", "call"), VERSIONED_WRAPPERS)
def test_versioned_wrappers_translate_compare_and_swap_conflicts(
    service: CaseService,
    repository: SqliteCaseRepository,
    monkeypatch: pytest.MonkeyPatch,
    repository_method: str,
    call: WrapperCall,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        raise CaseRecordVersionConflictError(CASE_ID, 6, 7)

    monkeypatch.setattr(repository, repository_method, fail)

    with pytest.raises(CaseVersionConflictError) as caught:
        call(service)

    assert caught.value.case_id == CASE_ID
    assert caught.value.expected_version == 6
    assert caught.value.current_version == 7
