"""Schema-v7 G6-G8 authority, replay, repair, and migration tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from test_workflow_atomic import NOW, _analysis_case, _initial_command

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    CaseState,
    GateId,
    GateReasonCode,
    PortalSessionView,
    PortalVariant,
    RenderedPortalSnapshot,
    RequiredClaimField,
)
from claimdone_api.gates import canonical_portal_case_url
from claimdone_api.persistence import (
    AuthorityCapabilityError,
    CaseRecordVersionConflictError,
    HumanApprovalCommand,
    IncompatiblePersistedContractError,
    PersistedDataIntegrityError,
    PortalRunStartCommand,
    PortalWriteFinalizeCommand,
    SqliteCaseRepository,
    VerificationAttemptCommand,
    VerificationAttemptResult,
    WorkflowAtomicityError,
)

_SCALAR_REPAIR_FIELDS = tuple(
    field for field in RequiredClaimField if field is not RequiredClaimField.ATTACHMENTS
)
_MISMATCH_CANDIDATES: dict[RequiredClaimField, tuple[str, str]] = {
    RequiredClaimField.INCIDENT_DATE: ("1900-01-01", "1900-01-02"),
    RequiredClaimField.INCIDENT_TIME: ("00:00", "00:01"),
    RequiredClaimField.LOCATION: ("Falscher Ort", "Anderer Ort"),
    RequiredClaimField.CLAIMANT_NAME: ("Andere Person", "Noch eine Person"),
    RequiredClaimField.POLICY_REFERENCE: ("POL-FALSCH", "POL-ANDERS"),
    RequiredClaimField.VEHICLE_REGISTRATION: ("XX-YY-999", "ZZ-AA-111"),
    RequiredClaimField.COUNTERPARTY_KNOWN: ("yes", "no"),
    RequiredClaimField.NARRATIVE: ("Abweichende Schilderung", "Andere Schilderung"),
}


def _different_scalar_value(field: RequiredClaimField, canonical: object) -> str:
    first, second = _MISMATCH_CANDIDATES[field]
    return first if first != canonical else second


def _ready_repository(database_path: Path) -> tuple[SqliteCaseRepository, Any]:
    service, repository, prefix, analyzing = _analysis_case(database_path)
    ready = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix, target=CaseState.READY_TO_FILL)
    ).case
    return repository, ready


def _portal_fields(packet: Any) -> dict[str, Any]:
    claim = packet.claim.model_dump(mode="json", by_alias=True)
    return {
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


def _portal_session(
    *,
    case_id: str,
    variant: PortalVariant,
    version: int,
    fields: dict[str, Any],
    updated_at: Any,
    state: str,
) -> PortalSessionView:
    return PortalSessionView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": case_id,
            "variant": variant,
            "state": state,
            "version": version,
            "fields": fields,
            "updatedAt": updated_at,
            "auditCount": 0,
        }
    )


def _rendered(
    session: PortalSessionView,
    *,
    fields: dict[str, Any],
    rendered_at: Any,
) -> RenderedPortalSnapshot:
    return RenderedPortalSnapshot.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": session.case_id,
            "variant": session.variant,
            "state": "review",
            "version": session.version,
            "fields": fields,
            "renderedAt": rendered_at,
        }
    )


def _start_and_fill(
    database_path: Path,
) -> tuple[SqliteCaseRepository, Any, PortalSessionView, bytes, str]:
    repository, ready = _ready_repository(database_path)
    packet = ready.snapshot.claim_packet
    assert packet is not None
    issued_at = ready.updated_at + timedelta(microseconds=1)
    capability_digest = b"a" * 32
    control_digest = b"c" * 32
    run_id = "run-cu-001"
    repository.issue_authority_capability(
        case_id=ready.case_id,
        expected_case_version=ready.version,
        digest=capability_digest,
        role="agent",
        purpose="portal_run",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=30),
    )
    prestage_at = issued_at + timedelta(microseconds=1)
    empty_fields = {
        "incidentDate": "",
        "incidentTime": "",
        "location": "",
        "claimantName": "",
        "policyReference": "",
        "vehicleRegistration": "",
        "counterpartyKnown": "",
        "narrative": "",
        "attachments": list(packet.claim.attachments),
    }
    prestage = _portal_session(
        case_id=ready.case_id,
        variant=PortalVariant.A,
        version=1,
        fields=empty_fields,
        updated_at=prestage_at,
        state="draft",
    )
    consumed_at = prestage_at + timedelta(microseconds=1)
    filling_at = consumed_at + timedelta(microseconds=1)
    fill_step = next(step for step in packet.plan.steps if step.tool.value == "fill_until_review")
    start_command = PortalRunStartCommand(
        case_id=ready.case_id,
        expected_case_version=ready.version,
        run_id=run_id,
        capability_digest=capability_digest,
        control_digest=control_digest,
        portal_variant=PortalVariant.A,
        invocation_payload={
            "contractVersion": CONTRACT_VERSION,
            "invocationId": run_id,
            "sequence": fill_step.sequence,
            "tool": "fill_until_review",
            "arguments": {},
        },
        current_url=canonical_portal_case_url(ready.case_id, PortalVariant.A),
        action="click",
        proposed_action_number=1,
        elapsed_seconds=0.25,
        prestage_session=prestage,
        consumed_at=consumed_at,
        updated_at=filling_at,
    )
    started = repository.start_portal_run(start_command)
    repeated = repository.start_portal_run(start_command)
    assert repeated == started
    filling = started.case
    filling_packet = filling.snapshot.claim_packet
    assert filling_packet is not None
    reviewed_fields = _portal_fields(filling_packet)
    reviewed_at = filling_at + timedelta(microseconds=1)
    reviewed = _portal_session(
        case_id=ready.case_id,
        variant=PortalVariant.A,
        version=3,
        fields=reviewed_fields,
        updated_at=reviewed_at,
        state="review",
    )
    g7_rendered = _rendered(
        reviewed,
        fields=reviewed_fields,
        rendered_at=reviewed_at + timedelta(microseconds=1),
    )
    verifying = repository.finalize_portal_write(
        PortalWriteFinalizeCommand(
            case_id=ready.case_id,
            expected_case_version=filling.version,
            run_id=run_id,
            control_digest=control_digest,
            fields_payload=reviewed_fields,
            duration_ms=10,
            completed_at=g7_rendered.rendered_at + timedelta(microseconds=1),
            portal_session=reviewed,
            rendered_snapshot=g7_rendered,
        )
    ).case
    return repository, verifying, reviewed, control_digest, run_id


def _start_only(
    database_path: Path,
    *,
    current_url_override: str | None = None,
    prestage_version: int = 1,
    control_digest: bytes = b"k" * 32,
    run_id: str = "run-rejected-g7",
) -> tuple[SqliteCaseRepository, Any, bytes, str]:
    repository, ready = _ready_repository(database_path)
    packet = ready.snapshot.claim_packet
    assert packet is not None
    issued_at = ready.updated_at + timedelta(microseconds=1)
    capability_digest = b"j" * 32
    repository.issue_authority_capability(
        case_id=ready.case_id,
        expected_case_version=ready.version,
        digest=capability_digest,
        role="agent",
        purpose="portal_run",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=30),
    )
    prestage_at = issued_at + timedelta(microseconds=1)
    prestage = _portal_session(
        case_id=ready.case_id,
        variant=PortalVariant.A,
        version=prestage_version,
        fields={
            "incidentDate": "",
            "incidentTime": "",
            "location": "",
            "claimantName": "",
            "policyReference": "",
            "vehicleRegistration": "",
            "counterpartyKnown": "",
            "narrative": "",
            "attachments": list(packet.claim.attachments),
        },
        updated_at=prestage_at,
        state="draft",
    )
    fill_step = next(step for step in packet.plan.steps if step.tool.value == "fill_until_review")
    consumed_at = prestage_at + timedelta(microseconds=1)
    filling = repository.start_portal_run(
        PortalRunStartCommand(
            case_id=ready.case_id,
            expected_case_version=ready.version,
            run_id=run_id,
            capability_digest=capability_digest,
            control_digest=control_digest,
            portal_variant=PortalVariant.A,
            invocation_payload={
                "contractVersion": CONTRACT_VERSION,
                "invocationId": run_id,
                "sequence": fill_step.sequence,
                "tool": "fill_until_review",
                "arguments": {},
            },
            current_url=(
                canonical_portal_case_url(ready.case_id, PortalVariant.A)
                if current_url_override is None
                else current_url_override
            ),
            action="click",
            proposed_action_number=1,
            elapsed_seconds=0.1,
            prestage_session=prestage,
            consumed_at=consumed_at,
            updated_at=consumed_at + timedelta(microseconds=1),
        )
    ).case
    return repository, filling, control_digest, run_id


def _successful_g7_command(
    filling: Any,
    *,
    control_digest: bytes,
    run_id: str,
) -> tuple[PortalWriteFinalizeCommand, PortalSessionView]:
    packet = filling.snapshot.claim_packet
    assert packet is not None
    fields = _portal_fields(packet)
    reviewed_at = filling.updated_at + timedelta(microseconds=1)
    reviewed = _portal_session(
        case_id=filling.case_id,
        variant=PortalVariant.A,
        version=3,
        fields=fields,
        updated_at=reviewed_at,
        state="review",
    )
    rendered = _rendered(
        reviewed,
        fields=fields,
        rendered_at=reviewed_at + timedelta(microseconds=1),
    )
    return (
        PortalWriteFinalizeCommand(
            case_id=filling.case_id,
            expected_case_version=filling.version,
            run_id=run_id,
            control_digest=control_digest,
            fields_payload=fields,
            duration_ms=5,
            completed_at=rendered.rendered_at + timedelta(microseconds=1),
            portal_session=reviewed,
            rendered_snapshot=rendered,
        ),
        reviewed,
    )


def _g7_authority_counts(
    repository: SqliteCaseRepository,
    case_id: str,
) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(repository.database_path) as connection:
        version_row = connection.execute(
            "SELECT version FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        assert version_row is not None
        g7_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM gate_decisions WHERE case_id = ? AND gate_id = 'G7'",
                (case_id,),
            ).fetchone()[0]
        )
        event_counts = {
            str(kind): int(count)
            for kind, count in connection.execute(
                """
                SELECT event_kind, COUNT(*) FROM workflow_events
                WHERE case_id = ? AND event_kind IN ('tool_call', 'portal_fill', 'state')
                GROUP BY event_kind
                """,
                (case_id,),
            )
        }
    return (
        int(version_row[0]),
        g7_count,
        event_counts.get("tool_call", 0),
        event_counts.get("portal_fill", 0),
        event_counts.get("state", 0),
    )


def _g8_authority_counts(
    repository: SqliteCaseRepository,
    case_id: str,
) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(repository.database_path) as connection:
        version_row = connection.execute(
            "SELECT version FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        assert version_row is not None
        g8_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM gate_decisions WHERE case_id = ? AND gate_id = 'G8'",
                (case_id,),
            ).fetchone()[0]
        )
        attempt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM verification_attempt_authority WHERE case_id = ?",
                (case_id,),
            ).fetchone()[0]
        )
        verification_event_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM workflow_events
                WHERE case_id = ? AND event_kind = 'verification'
                """,
                (case_id,),
            ).fetchone()[0]
        )
        state_event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE case_id = ? AND event_kind = 'state'",
                (case_id,),
            ).fetchone()[0]
        )
    return (
        int(version_row[0]),
        g8_count,
        attempt_count,
        verification_event_count,
        state_event_count,
    )


def _attempt_command(
    *,
    case: Any,
    run_id: str,
    control_digest: bytes,
    attempt_id: str,
    rendered: RenderedPortalSnapshot,
    final: bool,
    repaired_session: PortalSessionView | None = None,
) -> VerificationAttemptCommand:
    requested_at = case.updated_at + timedelta(microseconds=1)
    rendered = rendered.model_copy(update={"rendered_at": requested_at + timedelta(microseconds=1)})
    received_at = rendered.rendered_at + timedelta(microseconds=1)
    verified_at = received_at + timedelta(microseconds=1)
    return VerificationAttemptCommand(
        case_id=case.case_id,
        expected_case_version=case.version,
        run_id=run_id,
        control_digest=control_digest,
        attempt_id=attempt_id,
        rendered_snapshot=rendered,
        screenshot_sha256="d" * 64,
        snapshot_requested_at=requested_at,
        snapshot_received_at=received_at,
        model_reported_mismatch=False,
        verified_at=verified_at,
        decided_at=verified_at + timedelta(microseconds=1),
        final=final,
        repaired_session=repaired_session,
    )


def _completed_repair_repository(
    database_path: Path,
) -> tuple[SqliteCaseRepository, Any]:
    repository, verifying, reviewed, control, run_id = _start_and_fill(database_path)
    mismatch_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    mismatch_fields["location"] = "Falscher Ort"
    first = repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-tamper-repair-1",
            rendered=_rendered(
                reviewed,
                fields=mismatch_fields,
                rendered_at=verifying.updated_at,
            ),
            final=False,
        )
    )
    repair_at = first.case.updated_at + timedelta(microseconds=1)
    repaired = _portal_session(
        case_id=reviewed.case_id,
        variant=reviewed.variant,
        version=4,
        fields=reviewed.fields.model_dump(mode="json", by_alias=True),
        updated_at=repair_at,
        state="review",
    )
    final = repository.record_verification_attempt(
        _attempt_command(
            case=first.case,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-tamper-repair-2",
            rendered=_rendered(
                repaired,
                fields=repaired.fields.model_dump(mode="json", by_alias=True),
                rendered_at=repair_at,
            ),
            final=True,
            repaired_session=repaired,
        )
    ).case
    return repository, final


def test_blocked_g6_persists_terminal_authority_and_reopens(tmp_path: Path) -> None:
    repository, blocked, control, run_id = _start_only(
        tmp_path / "blocked-g6.db",
        current_url_override="https://example.invalid/not-the-sandbox",
    )
    assert blocked.state is CaseState.BLOCKED
    packet = blocked.snapshot.claim_packet
    assert packet is not None
    assert packet.gate_decisions[-1].passed is False
    run = repository.resolve_portal_run(run_id, control)
    assert run is not None
    assert run.status == "blocked_g6"
    assert run.terminal_case_version == run.g6_case_version == blocked.version
    snapshot = repository.get_workflow_snapshot(
        blocked.case_id,
        request_id="request-blocked-g6",
    )
    assert snapshot.portal_session is not None
    assert snapshot.portal_session.version == 1

    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    assert reopened.get_case(blocked.case_id) == blocked
    assert reopened.resolve_portal_run(run_id, control) == run


def test_g6_rejects_non_v1_prestage_without_consuming_authority(tmp_path: Path) -> None:
    database_path = tmp_path / "wrong-prestage-version.db"
    with pytest.raises(WorkflowAtomicityError):
        _start_only(database_path, prestage_version=2)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portal_run_authority"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_capabilities WHERE consumed_at IS NOT NULL"
        ).fetchone() == (0,)
        assert connection.execute("SELECT state FROM cases").fetchone() == (
            CaseState.READY_TO_FILL.value,
        )
    reopened = SqliteCaseRepository(database_path)
    ready = reopened.get_case("case-atomic")
    assert ready is not None and ready.state is CaseState.READY_TO_FILL


@pytest.mark.parametrize("g7_passes", (True, False), ids=("success", "blocked"))
def test_g7_lost_response_resolves_exactly_once_without_blind_rewrite(
    tmp_path: Path,
    g7_passes: bool,
) -> None:
    repository, filling, control, run_id = _start_only(
        tmp_path / f"g7-lost-{'success' if g7_passes else 'blocked'}.db"
    )
    if g7_passes:
        command, reviewed = _successful_g7_command(
            filling,
            control_digest=control,
            run_id=run_id,
        )
    else:
        packet = filling.snapshot.claim_packet
        assert packet is not None
        rejected_fields = _portal_fields(packet)
        rejected_fields["location"] = "Rejected G7 candidate"
        command = PortalWriteFinalizeCommand(
            case_id=filling.case_id,
            expected_case_version=filling.version,
            run_id=run_id,
            control_digest=control,
            fields_payload=rejected_fields,
            duration_ms=5,
            completed_at=filling.updated_at + timedelta(microseconds=1),
        )
        reviewed = None

    before = _g7_authority_counts(repository, filling.case_id)
    committed = repository.finalize_portal_write(command)
    after_commit = _g7_authority_counts(repository, filling.case_id)
    assert after_commit[0] == before[0] + 1
    assert after_commit[1] == before[1] + 1
    assert after_commit[2] == before[2] + 1
    assert after_commit[3] == before[3] + int(g7_passes)
    assert after_commit[4] == before[4] + 1
    assert committed.run.status == ("verifying" if g7_passes else "blocked_g7")

    # Simulate losing the commit response. Recovery is an authenticated read;
    # callers must not replay the non-idempotent portal write mutation.
    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    _other_repository, _other_case, other_control, _other_run = _start_only(
        tmp_path / f"g7-cross-{'success' if g7_passes else 'blocked'}.db",
        control_digest=b"q" * 32,
        run_id=f"run-cross-{'success' if g7_passes else 'blocked'}",
    )
    with pytest.raises(AuthorityCapabilityError):
        reopened.resolve_portal_run(run_id, other_control)
    assert _g7_authority_counts(reopened, filling.case_id) == after_commit

    resolved = reopened.resolve_portal_run(run_id, control)
    assert resolved == committed.run
    assert reopened.get_case(filling.case_id) == committed.case
    assert _g7_authority_counts(reopened, filling.case_id) == after_commit
    snapshot = reopened.get_workflow_snapshot(
        filling.case_id,
        request_id=f"request-g7-resolved-{'success' if g7_passes else 'blocked'}",
    )
    if reviewed is not None:
        assert snapshot.portal_session == reviewed


def test_happy_path_replays_raw_portal_and_final_g8(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(tmp_path / "happy.db")
    command = _attempt_command(
        case=verifying,
        run_id=run_id,
        control_digest=control,
        attempt_id="attempt-1",
        rendered=_rendered(
            reviewed,
            fields=reviewed.fields.model_dump(mode="json", by_alias=True),
            rendered_at=verifying.updated_at,
        ),
        final=True,
    )
    before = _g8_authority_counts(repository, verifying.case_id)
    committed = repository.record_verification_attempt(command)
    after_commit = _g8_authority_counts(repository, verifying.case_id)
    repeated = repository.record_verification_attempt(command)
    assert repeated == committed
    assert _g8_authority_counts(repository, verifying.case_id) == after_commit
    assert after_commit[0] == before[0] + 1
    assert after_commit[1] == before[1] + 1
    assert after_commit[2] == before[2] + 1
    assert after_commit[3] == before[3] + 1
    assert after_commit[4] == before[4] + 1
    review = committed.case
    assert review.state is CaseState.REVIEW
    snapshot = repository.get_workflow_snapshot(review.case_id, request_id="request-1")
    assert snapshot.portal_session == reviewed
    assert snapshot.verification_attempts is not None
    assert snapshot.verification_attempts.attempts[-1].gate_decision is not None

    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    replayed = reopened.get_workflow_snapshot(review.case_id, request_id="request-2")
    assert replayed.portal_session == reviewed
    assert replayed.verification_attempts == snapshot.verification_attempts


def test_concurrent_distinct_final_attempt_ids_use_case_cas_exactly_once(
    tmp_path: Path,
) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / "g8-concurrent-final.db"
    )
    rendered = _rendered(
        reviewed,
        fields=reviewed.fields.model_dump(mode="json", by_alias=True),
        rendered_at=verifying.updated_at,
    )
    commands = tuple(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id=f"attempt-concurrent-{suffix}",
            rendered=rendered,
            final=True,
        )
        for suffix in ("a", "b")
    )
    barrier = Barrier(2)

    def execute(command: VerificationAttemptCommand) -> object:
        barrier.wait()
        try:
            return repository.record_verification_attempt(command)
        except CaseRecordVersionConflictError as error:
            return error

    before = _g8_authority_counts(repository, verifying.case_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(execute, commands))
    successes = tuple(
        outcome for outcome in outcomes if isinstance(outcome, VerificationAttemptResult)
    )
    conflicts = tuple(
        outcome
        for outcome in outcomes
        if isinstance(outcome, CaseRecordVersionConflictError)
    )
    assert len(successes) == len(conflicts) == 1
    winner = successes[0]
    winning_command = next(
        command for command in commands if command.attempt_id == winner.attempt.attempt_id
    )
    losing_command = next(command for command in commands if command is not winning_command)

    after = _g8_authority_counts(repository, verifying.case_id)
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1
    assert after[2] == before[2] + 1
    assert after[3] == before[3] + 1
    assert after[4] == before[4] + 1
    assert repository.record_verification_attempt(winning_command) == winner
    with pytest.raises(CaseRecordVersionConflictError):
        repository.record_verification_attempt(losing_command)
    assert _g8_authority_counts(repository, verifying.case_id) == after

    snapshot = repository.get_workflow_snapshot(
        verifying.case_id,
        request_id="request-concurrent-g8",
    )
    assert snapshot.verification_attempts is not None
    assert snapshot.verification_attempts.attempts == (winner.attempt,)
    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    assert reopened.get_case(verifying.case_id) == winner.case


@pytest.mark.parametrize("repair_field", _SCALAR_REPAIR_FIELDS, ids=lambda field: field.value)
def test_every_scalar_repair_is_v3_to_v4_private_then_final_and_replayable(
    tmp_path: Path,
    repair_field: RequiredClaimField,
) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / f"repair-{repair_field.value}.db"
    )
    canonical_fields = reviewed.fields.model_dump(mode="json", by_alias=False)
    mismatch_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    wire_name = reviewed.fields.__class__.model_fields[repair_field.value].alias
    assert wire_name is not None
    mismatch_fields[wire_name] = _different_scalar_value(
        repair_field,
        canonical_fields[repair_field.value],
    )
    first = repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id=f"attempt-repair-{repair_field.value}-1",
            rendered=_rendered(
                reviewed,
                fields=mismatch_fields,
                rendered_at=verifying.updated_at,
            ),
            final=False,
        )
    )
    assert first.attempt.repair is not None
    assert first.attempt.repair.field is repair_field
    assert first.attempt.repair.from_portal_version == 3
    assert first.attempt.repair.to_portal_version == 4
    assert first.case.state is CaseState.VERIFYING
    assert first.case.version == verifying.version + 1
    private = repository.get_workflow_snapshot(
        verifying.case_id,
        request_id=f"request-private-{repair_field.value}",
    )
    assert private.verification_attempts is None

    repair_at = first.case.updated_at + timedelta(microseconds=1)
    repaired = _portal_session(
        case_id=reviewed.case_id,
        variant=reviewed.variant,
        version=4,
        fields=reviewed.fields.model_dump(mode="json", by_alias=True),
        updated_at=repair_at,
        state="review",
    )
    second_command = _attempt_command(
        case=first.case,
        run_id=run_id,
        control_digest=control,
        attempt_id=f"attempt-repair-{repair_field.value}-2",
        rendered=_rendered(
            repaired,
            fields=repaired.fields.model_dump(mode="json", by_alias=True),
            rendered_at=repair_at,
        ),
        final=True,
        repaired_session=repaired,
    )
    before_second = _g8_authority_counts(repository, first.case.case_id)
    second = repository.record_verification_attempt(second_command)
    after_second = _g8_authority_counts(repository, first.case.case_id)
    repeated_second = repository.record_verification_attempt(second_command)
    assert repeated_second == second
    assert _g8_authority_counts(repository, first.case.case_id) == after_second
    assert after_second[0] == before_second[0] + 1
    assert after_second[1] == before_second[1] + 1
    assert after_second[2] == before_second[2] + 1
    assert after_second[3] == before_second[3] + 1
    assert after_second[4] == before_second[4] + 1
    assert second.case.state is CaseState.REVIEW
    snapshot = repository.get_workflow_snapshot(
        second.case.case_id,
        request_id="request-repaired",
    )
    assert snapshot.portal_session is not None
    assert snapshot.portal_session.version == 4
    assert snapshot.verification_attempts is not None
    assert len(snapshot.verification_attempts.attempts) == 2
    assert snapshot.verification_attempts.attempts[0].repair is not None
    assert snapshot.verification_attempts.attempts[0].repair.field is repair_field
    final_attempt = snapshot.verification_attempts.attempts[1]
    assert final_attempt.final
    assert final_attempt.gate_decision is not None
    assert final_attempt.gate_decision.gate_id is GateId.G8_VERIFICATION
    assert final_attempt.gate_decision.passed

    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    assert reopened.get_case(second.case.case_id) == second.case
    replayed = reopened.get_workflow_snapshot(
        second.case.case_id,
        request_id=f"request-replayed-{repair_field.value}",
    )
    assert replayed.portal_session == snapshot.portal_session
    assert replayed.verification_attempts == snapshot.verification_attempts


def test_attachment_mismatch_is_not_repairable_and_finally_blocks(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / "attachment-mismatch.db"
    )
    mismatch_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    mismatch_fields["attachments"] = [
        "attachment-forged",
        *mismatch_fields["attachments"][1:],
    ]
    nonfinal_command = _attempt_command(
        case=verifying,
        run_id=run_id,
        control_digest=control,
        attempt_id="attempt-attachment-nonfinal",
        rendered=_rendered(
            reviewed,
            fields=mismatch_fields,
            rendered_at=verifying.updated_at,
        ),
        final=False,
    )
    with pytest.raises(WorkflowAtomicityError):
        repository.record_verification_attempt(nonfinal_command)
    assert repository.get_case(verifying.case_id) == verifying
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_attempt_authority"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM portal_session_authority WHERE checkpoint_number = 2"
        ).fetchone() == (0,)

    blocked_result = repository.record_verification_attempt(
        replace(
            nonfinal_command,
            attempt_id="attempt-attachment-final",
            final=True,
        )
    )
    assert blocked_result.case.state is CaseState.BLOCKED
    assert blocked_result.attempt.final
    assert blocked_result.attempt.repair is None
    assert blocked_result.attempt.gate_decision is not None
    assert blocked_result.attempt.gate_decision.gate_id is GateId.G8_VERIFICATION
    assert not blocked_result.attempt.gate_decision.passed
    assert blocked_result.attempt.gate_decision.reason_codes == (
        GateReasonCode.G8_ATTACHMENT_MISMATCH,
    )
    snapshot = repository.get_workflow_snapshot(
        verifying.case_id,
        request_id="request-attachment-blocked",
    )
    assert snapshot.verification_attempts is not None
    assert snapshot.verification_attempts.attempts == (blocked_result.attempt,)

    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    assert reopened.get_case(blocked_result.case.case_id) == blocked_result.case
    replayed = reopened.get_workflow_snapshot(
        blocked_result.case.case_id,
        request_id="request-attachment-replayed",
    )
    assert replayed.portal_session == snapshot.portal_session
    assert replayed.verification_attempts == snapshot.verification_attempts


def test_nonfinal_attempt_reconciles_idempotently_without_public_leak(
    tmp_path: Path,
) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / "nonfinal-reconciliation.db"
    )
    mismatch_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    mismatch_fields["location"] = "Falscher Ort"
    command = _attempt_command(
        case=verifying,
        run_id=run_id,
        control_digest=control,
        attempt_id="attempt-reconcile-nonfinal",
        rendered=_rendered(
            reviewed,
            fields=mismatch_fields,
            rendered_at=verifying.updated_at,
        ),
        final=False,
    )
    committed = repository.record_verification_attempt(command)
    repeated = repository.record_verification_attempt(command)
    assert repeated == committed
    assert (
        repository.resolve_verification_attempt(
            case_id=verifying.case_id,
            run_id=run_id,
            control_digest=control,
            attempt_id=command.attempt_id,
        )
        == committed
    )
    assert (
        repository.resolve_verification_attempt(
            case_id=verifying.case_id,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-not-committed",
        )
        is None
    )
    with pytest.raises(AuthorityCapabilityError):
        repository.resolve_verification_attempt(
            case_id=verifying.case_id,
            run_id=run_id,
            control_digest=b"x" * 32,
            attempt_id=command.attempt_id,
        )
    with pytest.raises(AuthorityCapabilityError):
        repository.record_verification_attempt(
            replace(command, screenshot_sha256="e" * 64)
        )
    public = repository.get_workflow_snapshot(
        verifying.case_id,
        request_id="request-reconcile-private",
    )
    assert public.verification_attempts is None
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_attempt_authority"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE event_kind = 'verification'"
        ).fetchone() == (1,)

    repository.media_store.close()
    reopened = SqliteCaseRepository(repository.database_path)
    assert (
        reopened.resolve_verification_attempt(
            case_id=verifying.case_id,
            run_id=run_id,
            control_digest=control,
            attempt_id=command.attempt_id,
        )
        == committed
    )


def test_screenshot_digest_tamper_fails_closed_on_reopen(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(tmp_path / "tamper.db")
    review = repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-tamper",
            rendered=_rendered(
                reviewed,
                fields=reviewed.fields.model_dump(mode="json", by_alias=True),
                rendered_at=verifying.updated_at,
            ),
            final=True,
        )
    ).case
    repository.media_store.close()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE verification_attempt_authority SET screenshot_sha256 = ?",
            ("e" * 64,),
        )
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(repository.database_path)
    assert review.state is CaseState.REVIEW


def test_child_run_binding_tamper_fails_closed_on_reopen(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / "child-run-tamper.db"
    )
    repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-child-run-tamper",
            rendered=_rendered(
                reviewed,
                fields=reviewed.fields.model_dump(mode="json", by_alias=True),
                rendered_at=verifying.updated_at,
            ),
            final=True,
        )
    )
    repository.media_store.close()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE portal_session_authority SET run_id = ? WHERE checkpoint_number = 1",
            ("run-forged-other-case",),
        )
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(repository.database_path)


def test_coherent_non_target_repair_tamper_fails_semantic_replay(tmp_path: Path) -> None:
    repository, review = _completed_repair_repository(
        tmp_path / "repair-semantic-tamper.db"
    )
    repository.media_store.close()
    with sqlite3.connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT session_json FROM portal_session_authority WHERE checkpoint_number = 2"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[0]))
        payload["fields"]["claimantName"] = "Coherently forged non-target"
        session_json = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        session_sha256 = hashlib.sha256(
            b"claimdone-portal-session-v1\0" + session_json.encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            UPDATE portal_session_authority
            SET session_json = ?, session_sha256 = ?
            WHERE checkpoint_number = 2
            """,
            (session_json, session_sha256),
        )
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(repository.database_path)
    assert review.state is CaseState.REVIEW


def test_repair_source_attempt_tamper_fails_semantic_replay(tmp_path: Path) -> None:
    repository, _review = _completed_repair_repository(
        tmp_path / "repair-source-tamper.db"
    )
    repository.media_store.close()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE portal_session_authority
            SET source_attempt_id = ?
            WHERE checkpoint_number = 2
            """,
            ("attempt-forged-source",),
        )
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(repository.database_path)


def test_passed_g7_summary_digest_tamper_fails_semantic_replay(tmp_path: Path) -> None:
    repository, _verifying, _reviewed, _control, run_id = _start_and_fill(
        tmp_path / "passed-g7-summary-digest-tamper.db"
    )
    repository.media_store.close()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE portal_run_authority
            SET rejected_summary_sha256 = ?
            WHERE run_id = ?
            """,
            ("e" * 64, run_id),
        )
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(repository.database_path)


def test_rejected_g7_persists_only_content_free_shape_summary(tmp_path: Path) -> None:
    repository, filling, control, run_id = _start_only(tmp_path / "rejected.db")
    packet = filling.snapshot.claim_packet
    assert packet is not None
    candidate = _portal_fields(packet)
    secret_candidate_value = "DO-NOT-PERSIST-CANDIDATE-VALUE"
    candidate["location"] = secret_candidate_value
    result = repository.finalize_portal_write(
        PortalWriteFinalizeCommand(
            case_id=filling.case_id,
            expected_case_version=filling.version,
            run_id=run_id,
            control_digest=control,
            fields_payload=candidate,
            duration_ms=2,
            completed_at=filling.updated_at + timedelta(microseconds=1),
        )
    )
    assert result.case.state is CaseState.BLOCKED
    with sqlite3.connect(repository.database_path) as connection:
        row = connection.execute(
            """
            SELECT rejected_summary_json, rejected_summary_sha256
            FROM portal_run_authority WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        dump = "\n".join(connection.iterdump())
        assert connection.execute("SELECT COUNT(*) FROM portal_session_authority").fetchone() == (
            0,
        )
    assert row is not None and row[0] is not None and len(str(row[1])) == 64
    assert secret_candidate_value not in dump


def test_run_control_variant_and_human_variant_are_bound(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(tmp_path / "bindings.db")
    with pytest.raises(AuthorityCapabilityError):
        repository.resolve_portal_run(run_id, b"x" * 32)

    other = repository.create_case(
        case_id="case-other-binding",
        redacted_metadata={},
        created_at=verifying.updated_at,
    )
    with pytest.raises(WorkflowAtomicityError):
        repository.record_verification_attempt(
            _attempt_command(
                case=other,
                run_id=run_id,
                control_digest=control,
                attempt_id="attempt-wrong-case",
                rendered=_rendered(
                    reviewed,
                    fields=reviewed.fields.model_dump(mode="json", by_alias=True),
                    rendered_at=other.updated_at,
                ),
                final=True,
            )
        )
    assert repository.get_case(other.case_id) == other

    wrong_variant = _rendered(
        reviewed,
        fields=reviewed.fields.model_dump(mode="json", by_alias=True),
        rendered_at=verifying.updated_at,
    ).model_copy(update={"variant": PortalVariant.B})
    with pytest.raises(WorkflowAtomicityError):
        repository.record_verification_attempt(
            _attempt_command(
                case=verifying,
                run_id=run_id,
                control_digest=control,
                attempt_id="attempt-wrong-variant",
                rendered=wrong_variant,
                final=True,
            )
        )
    assert repository.get_case(verifying.case_id) == verifying

    review = repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-bound-review",
            rendered=_rendered(
                reviewed,
                fields=reviewed.fields.model_dump(mode="json", by_alias=True),
                rendered_at=verifying.updated_at,
            ),
            final=True,
        )
    ).case
    issued_at = review.updated_at + timedelta(microseconds=1)
    human_digest = b"h" * 32
    repository.issue_authority_capability(
        case_id=review.case_id,
        expected_case_version=review.version,
        digest=human_digest,
        role="human",
        purpose="human_approve",
        portal_variant=PortalVariant.B,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=30),
    )
    consumed_at = issued_at + timedelta(microseconds=1)
    with pytest.raises(AuthorityCapabilityError):
        repository.approve_human_and_create_receipt(
            HumanApprovalCommand(
                case_id=review.case_id,
                expected_case_version=review.version,
                capability_digest=human_digest,
                portal_variant=PortalVariant.B,
                approval_id="approval-b-wrong-variant",
                receipt_id="receipt-wrong-variant",
                consumed_at=consumed_at,
                approved_at=consumed_at + timedelta(microseconds=1),
                rendered_at=consumed_at + timedelta(microseconds=2),
            )
        )
    human = repository.get_authority_capability(human_digest)
    assert human is not None and human.consumed_at is None


def test_second_attempt_rejects_non_target_change_and_rolls_back(tmp_path: Path) -> None:
    repository, verifying, reviewed, control, run_id = _start_and_fill(
        tmp_path / "repair-rollback.db"
    )
    mismatch_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    mismatch_fields["location"] = "Falscher Ort"
    first = repository.record_verification_attempt(
        _attempt_command(
            case=verifying,
            run_id=run_id,
            control_digest=control,
            attempt_id="attempt-rollback-1",
            rendered=_rendered(
                reviewed,
                fields=mismatch_fields,
                rendered_at=verifying.updated_at,
            ),
            final=False,
        )
    )
    bad_fields = reviewed.fields.model_dump(mode="json", by_alias=True)
    bad_fields["claimantName"] = "Untrusted change"
    repair_at = first.case.updated_at + timedelta(microseconds=1)
    bad_repair = _portal_session(
        case_id=reviewed.case_id,
        variant=reviewed.variant,
        version=4,
        fields=bad_fields,
        updated_at=repair_at,
        state="review",
    )
    with pytest.raises(WorkflowAtomicityError):
        repository.record_verification_attempt(
            _attempt_command(
                case=first.case,
                run_id=run_id,
                control_digest=control,
                attempt_id="attempt-rollback-2",
                rendered=_rendered(
                    bad_repair,
                    fields=bad_fields,
                    rendered_at=repair_at,
                ),
                final=True,
                repaired_session=bad_repair,
            )
        )
    assert repository.get_case(first.case.case_id) == first.case
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_attempt_authority"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM portal_session_authority").fetchone() == (
            1,
        )


def test_v6_to_v7_collision_rolls_back_schema_and_source_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "collision.db"
    repository = SqliteCaseRepository(database_path)
    repository.create_case(
        case_id="case-v6-collision",
        redacted_metadata={},
        created_at=NOW,
    )
    repository.media_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE verification_attempt_authority")
        connection.execute("DROP TABLE portal_session_authority")
        connection.execute("DROP TABLE portal_run_authority")
        connection.execute("CREATE TABLE portal_session_authority (sentinel TEXT)")
        connection.execute("INSERT INTO portal_session_authority VALUES ('kept')")
        connection.execute("PRAGMA user_version = 6")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        SqliteCaseRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute("SELECT * FROM portal_session_authority").fetchall() == [
            ("kept",)
        ]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'portal_run_authority'"
            ).fetchone()
            is None
        )


def test_v6_unsafe_cu_state_rejects_and_rolls_back(tmp_path: Path) -> None:
    database_path = tmp_path / "unsafe-v6.db"
    repository, ready = _ready_repository(database_path)
    repository.media_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE verification_attempt_authority")
        connection.execute("DROP TABLE portal_session_authority")
        connection.execute("DROP TABLE portal_run_authority")
        connection.execute(
            "UPDATE cases SET state = ?, claim_packet_json = NULL",
            (CaseState.FILLING.value,),
        )
        connection.execute("PRAGMA user_version = 6")
    with pytest.raises(IncompatiblePersistedContractError):
        SqliteCaseRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'portal_run_authority'"
            ).fetchone()
            is None
        )
    assert ready.state is CaseState.READY_TO_FILL
