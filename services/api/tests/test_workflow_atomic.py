"""Atomic analysis, clarification, and terminal provider-failure persistence."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    CaseState,
    ClaimPacket,
    ClarificationStatus,
    ClarificationView,
    ClarificationWorkflowEvent,
    GateDecision,
    GateId,
    GateReasonCode,
    OperationalFailureWorkflowEvent,
    PlanStepWorkflowEvent,
    ProviderCallWorkflowEvent,
    RetryWorkflowEvent,
    TranscriptConfirmationRequest,
)
from claimdone_api.persistence import (
    AnalysisWorkflowCommand,
    PersistedDataIntegrityError,
    ProviderWorkflowEmission,
    SqliteCaseRepository,
    TerminalProviderFailureCommand,
    WorkflowAtomicityError,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
GATE_SEQUENCE = (
    GateId.G0_INTAKE,
    GateId.G1_PRIVACY,
    GateId.G2_OUTPUT_CONTRACT,
    GateId.G3_SAFETY_SCOPE,
    GateId.G4_PROVENANCE,
    GateId.G5_COMPLETENESS,
)
TRANSCRIPT_DIGEST = "a" * 64


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
            "sha256": TRANSCRIPT_DIGEST,
        },
    }


def _gate(
    gate_id: GateId,
    offset: int,
    *reasons: GateReasonCode,
) -> GateDecision:
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": gate_id,
            "deterministicPassed": not reasons,
            "modelBlocked": False,
            "passed": not reasons,
            "reasonCodes": reasons,
            "evidenceRefs": (),
            "decidedAt": NOW + timedelta(seconds=offset),
        }
    )


def _provider_call(
    *,
    call_sequence: int = 1,
    retry_attempt: int = 0,
) -> ProviderCallWorkflowEvent:
    return ProviderCallWorkflowEvent.model_validate(
        {
            "kind": "provider_call",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": 20,
            "status": "succeeded",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            "cost": None,
        }
    )


def _retry() -> RetryWorkflowEvent:
    return RetryWorkflowEvent.model_validate(
        {
            "kind": "retry",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": 1,
            "retryAttempt": 1,
            "durationMs": 20,
            "failure": {
                "category": "invalid_response",
                "retryable": True,
                "terminal": False,
            },
        }
    )


def _operational_failure(
    category: str = "quota_exhausted",
    *,
    call_sequence: int = 1,
    retry_attempt: int = 0,
) -> OperationalFailureWorkflowEvent:
    return OperationalFailureWorkflowEvent.model_validate(
        {
            "kind": "operational_failure",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": 30,
            "failure": {
                "category": category,
                "retryable": False,
                "terminal": True,
            },
        }
    )


def _packet(
    case_id: str,
    state: CaseState,
    gates: tuple[GateDecision, ...],
    *,
    missing: bool,
) -> ClaimPacket:
    data = cast(
        dict[str, Any],
        json.loads(HAPPY_PATH.read_text(encoding="utf-8")),
    )
    data["caseId"] = case_id
    data["state"] = state.value
    data["portalState"] = "draft"
    data["gateDecisions"] = [
        gate.model_dump(mode="json", by_alias=True) for gate in gates
    ]
    plan: list[tuple[str, str]] = [
        ("inspect_evidence", "Inspect only the approved evidence inventory"),
        (
            "check_required_fields",
            "Use the deterministic required-field result",
        ),
    ]
    if state is CaseState.AWAITING_CLARIFICATION:
        plan.append(
            ("ask_clarification", "Ask the single clarification accepted by G5")
        )
    elif state is CaseState.READY_TO_FILL:
        plan.extend(
            (
                ("inspect_form", "Inspect only the local sandbox form"),
                ("fill_until_review", "Fill the sandbox only until review"),
                (
                    "verify_rendered_fields",
                    "Verify rendered fields before human review",
                ),
            )
        )
    data["plan"] = {
        "agentCanSubmit": False,
        "steps": [
            {"sequence": index, "tool": tool, "reason": reason}
            for index, (tool, reason) in enumerate(plan, start=1)
        ],
    }
    data["verification"] = {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": (),
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }
    claim = cast(dict[str, Any], data["claim"])
    if missing:
        claim["incidentTime"] = None
        claim["missingRequiredFields"] = ["incident_time"]
        claim["fieldProvenance"] = [
            item
            for item in cast(list[dict[str, Any]], claim["fieldProvenance"])
            if item["field"] != "incident_time"
        ]
    return ClaimPacket.model_validate(data)


def _plan_events(packet: ClaimPacket) -> tuple[PlanStepWorkflowEvent, ...]:
    return tuple(
        PlanStepWorkflowEvent.model_validate(
            {"kind": "plan_step", "sequence": step.sequence, "tool": step.tool}
        )
        for step in packet.plan.steps
    )


def _clarification_event(
    status: ClarificationStatus,
    *,
    round_number: int,
    field: str = "incident_time",
) -> ClarificationWorkflowEvent:
    return ClarificationWorkflowEvent.model_validate(
        {
            "kind": "clarification",
            "round": round_number,
            "field": field,
            "status": status,
        }
    )


def _clarification_view(
    case_id: str,
    version: int,
    *,
    round_number: int,
    requested_at: datetime,
) -> ClarificationView:
    return ClarificationView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "clarificationId": f"clarification-{round_number}",
            "caseId": case_id,
            "field": "incident_time",
            "round": round_number,
            "question": "Wann ereignete sich der Vorfall?",
            "status": "requested",
            "expectedVersion": version,
            "requestedAt": requested_at,
        }
    )


def _analysis_case(
    database_path: Path,
) -> tuple[
    CaseService,
    SqliteCaseRepository,
    tuple[GateDecision, GateDecision],
    Any,
]:
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-atomic",
    )
    case = service.create_case()
    prefix = (
        _gate(GateId.G0_INTAKE, 0),
        _gate(GateId.G1_PRIVACY, 0),
    )
    for decision in prefix:
        case = service.record_gate_decision(
            case.case_id,
            expected_version=case.version,
            decision=decision,
        )
    case = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.DISCLOSED,
    )
    case = service.transition_case(
        case.case_id,
        expected_version=case.version,
        target=CaseState.ANALYZING,
    )
    return service, repository, prefix, case


def _initial_command(
    case: Any,
    prefix: tuple[GateDecision, GateDecision],
    *,
    target: CaseState = CaseState.AWAITING_CLARIFICATION,
    retry: bool = False,
) -> AnalysisWorkflowCommand:
    emitted: tuple[GateDecision, ...]
    if target is CaseState.BLOCKED:
        emitted = (
            _gate(GateId.G2_OUTPUT_CONTRACT, 2),
            _gate(
                GateId.G3_SAFETY_SCOPE,
                3,
                GateReasonCode.G3_INJURY_OR_EMERGENCY,
            ),
        )
        packet = None
    else:
        g5 = (
            _gate(GateId.G5_COMPLETENESS, 5)
            if target is CaseState.READY_TO_FILL
            else _gate(
                GateId.G5_COMPLETENESS,
                5,
                GateReasonCode.G5_REQUIRED_FIELD_MISSING,
            )
        )
        emitted = (
            _gate(GateId.G2_OUTPUT_CONTRACT, 2),
            _gate(GateId.G3_SAFETY_SCOPE, 3),
            _gate(GateId.G4_PROVENANCE, 4),
            g5,
        )
        packet = _packet(
            case.case_id,
            target,
            (*prefix, *emitted),
            missing=target is CaseState.AWAITING_CLARIFICATION,
        )
    updated_at = NOW + timedelta(seconds=10)
    active = (
        _clarification_view(
            case.case_id,
            case.version + 1,
            round_number=1,
            requested_at=updated_at,
        )
        if target is CaseState.AWAITING_CLARIFICATION
        else None
    )
    provider_events = (
        (
            ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(_retry(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(
                _provider_call(call_sequence=2, retry_attempt=1),
                NOW + timedelta(seconds=1),
            ),
        )
        if retry
        else (
            ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),
        )
    )
    return AnalysisWorkflowCommand(
        case_id=case.case_id,
        expected_version=case.version,
        target=target,
        claim_packet=packet,
        active_clarification=active,
        gate_decisions=emitted,
        provider_events=provider_events,
        plan_steps=() if packet is None else _plan_events(packet),
        clarification_events=(
            _clarification_event(ClarificationStatus.REQUESTED, round_number=1),
        )
        if active is not None
        else (),
        updated_at=updated_at,
    )


def _continuation_command(
    case: Any,
    *,
    target: CaseState,
) -> AnalysisWorkflowCommand:
    prior_packet = case.snapshot.claim_packet
    assert prior_packet is not None
    active = ClarificationView.model_validate(case.snapshot.active_clarification)
    base_offset = int((case.updated_at - NOW).total_seconds())
    g4 = _gate(GateId.G4_PROVENANCE, base_offset + 1)
    if target is CaseState.READY_TO_FILL:
        g5 = _gate(GateId.G5_COMPLETENESS, base_offset + 2)
    elif target is CaseState.BLOCKED:
        g5 = _gate(
            GateId.G5_COMPLETENESS,
            base_offset + 2,
            GateReasonCode.G5_REQUIRED_FIELD_MISSING,
            GateReasonCode.G5_CLARIFICATION_LIMIT,
        )
    else:
        g5 = _gate(
            GateId.G5_COMPLETENESS,
            base_offset + 2,
            GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        )
    emitted = (g4, g5)
    effective = (*prior_packet.gate_decisions[:4], *emitted)
    updated_at = NOW + timedelta(seconds=base_offset + 3)
    packet = _packet(
        case.case_id,
        target,
        effective,
        missing=target is not CaseState.READY_TO_FILL,
    )
    clarification_events: tuple[ClarificationWorkflowEvent, ...]
    if target is CaseState.AWAITING_CLARIFICATION:
        next_round = active.round + 1
        next_active = _clarification_view(
            case.case_id,
            case.version + 1,
            round_number=next_round,
            requested_at=updated_at,
        )
        clarification_events = (
            _clarification_event(
                ClarificationStatus.CONFIRMED,
                round_number=active.round,
            ),
            _clarification_event(
                ClarificationStatus.REQUESTED,
                round_number=next_round,
            ),
        )
    else:
        next_active = None
        clarification_events = (
            _clarification_event(
                ClarificationStatus.EXHAUSTED
                if target is CaseState.BLOCKED
                else ClarificationStatus.CONFIRMED,
                round_number=active.round,
            ),
        )
    return AnalysisWorkflowCommand(
        case_id=case.case_id,
        expected_version=case.version,
        target=target,
        claim_packet=packet,
        active_clarification=next_active,
        gate_decisions=emitted,
        provider_events=(),
        plan_steps=_plan_events(packet),
        clarification_events=clarification_events,
        updated_at=updated_at,
    )


def _counts(repository: SqliteCaseRepository, case_id: str) -> tuple[int, int, int, int]:
    return (
        len(repository.list_audit_events(case_id)),
        len(repository.list_gate_decisions(case_id)),
        len(repository.list_workflow_events(case_id)),
        len(repository.list_provider_usage(case_id)),
    )


def test_initial_analysis_commits_one_version_in_redacted_cursor_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.db"
    service, repository, prefix, current = _analysis_case(database_path)
    command = _initial_command(current, prefix)

    result = service.commit_analysis_workflow(command)

    assert result.case.version == current.version + 1
    assert result.case.state is CaseState.AWAITING_CLARIFICATION
    assert [item.event.kind for item in result.workflow_events] == [
        "provider_call",
        "gate",
        "gate",
        "gate",
        "gate",
        *("plan_step" for _step in cast(ClaimPacket, command.claim_packet).plan.steps),
        "clarification",
        "state",
    ]
    assert [item.decision.gate_id for item in repository.list_gate_decisions(current.case_id)] == [
        *GATE_SEQUENCE,
    ]
    assert len(repository.list_provider_usage(current.case_id)) == 1
    stored_active = cast(dict[str, Any], result.case.snapshot.active_clarification)
    assert "answer" not in stored_active
    assert ClarificationView.model_validate(stored_active) == command.active_clarification
    persisted = database_path.read_text(encoding="utf-8", errors="ignore")
    for forbidden in ("providerMessage", "rawAnswer", "mediaPath"):
        assert forbidden not in persisted


def test_initial_retry_persists_call_retry_call_and_three_ledger_rows(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "retry.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL, retry=True)

    result = service.commit_analysis_workflow(command)

    assert [event.event.kind for event in result.workflow_events[:3]] == [
        "provider_call",
        "retry",
        "provider_call",
    ]
    usage = repository.list_provider_usage(current.case_id)
    assert [(item.call_sequence, item.retry_attempt, item.status) for item in usage] == [
        (1, 0, "succeeded"),
        (1, 1, "retry_scheduled"),
        (2, 1, "succeeded"),
    ]


def test_round_two_and_three_are_same_state_cas_without_provider_or_state_event(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "rounds.db")
    round_one = service.commit_analysis_workflow(_initial_command(current, prefix)).case

    round_two_result = service.commit_analysis_workflow(
        _continuation_command(round_one, target=CaseState.AWAITING_CLARIFICATION)
    )
    round_two = round_two_result.case
    round_three_result = service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.AWAITING_CLARIFICATION)
    )
    round_three = round_three_result.case

    for result, expected_round in ((round_two_result, 2), (round_three_result, 3)):
        kinds = [event.event.kind for event in result.workflow_events]
        assert "provider_call" not in kinds
        assert "retry" not in kinds
        assert "state" not in kinds
        assert kinds[-2:] == ["clarification", "clarification"]
        active = ClarificationView.model_validate(result.case.snapshot.active_clarification)
        assert active.round == expected_round
        assert active.expected_version == result.case.version
    assert round_three.version == round_one.version + 2
    assert len(repository.list_provider_usage(current.case_id)) == 1

    blocked = service.commit_analysis_workflow(
        _continuation_command(round_three, target=CaseState.BLOCKED)
    )
    assert blocked.case.state is CaseState.BLOCKED
    assert blocked.case.snapshot.active_clarification is None
    assert [event.event.kind for event in blocked.workflow_events] == [
        "gate",
        "gate",
        "plan_step",
        "plan_step",
        "clarification",
        "state",
    ]
    blocked_packet = cast(ClaimPacket, blocked.case.snapshot.claim_packet)
    assert [(step.tool.value, step.reason) for step in blocked_packet.plan.steps] == [
        ("inspect_evidence", "Inspect only the approved evidence inventory"),
        ("check_required_fields", "Use the deterministic required-field result"),
    ]
    assert cast(ClarificationWorkflowEvent, blocked.workflow_events[-2].event).status is (
        ClarificationStatus.EXHAUSTED
    )


def test_clarification_answer_rechecks_only_g4_g5_and_reaches_ready(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "ready.db")
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case

    result = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    )

    assert result.case.state is CaseState.READY_TO_FILL
    assert result.case.snapshot.active_clarification is None
    kinds = [event.event.kind for event in result.workflow_events]
    assert kinds[:2] == ["gate", "gate"]
    assert "provider_call" not in kinds
    assert kinds[-2:] == ["clarification", "state"]
    assert len(repository.list_provider_usage(current.case_id)) == 1
    latest = repository.list_gate_decisions(current.case_id)[-2:]
    assert [item.decision.gate_id for item in latest] == [
        GateId.G4_PROVENANCE,
        GateId.G5_COMPLETENESS,
    ]
    assert all(item.decision.passed for item in latest)


def test_analysis_commit_preserves_confirmed_transcript_and_bound_summary(
    tmp_path: Path,
) -> None:
    repository = SqliteCaseRepository(tmp_path / "transcript-analysis.db")
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-atomic",
    )
    case = service.create_case()
    prefix = (
        _gate(GateId.G0_INTAKE, 0),
        _gate(GateId.G1_PRIVACY, 0),
    )
    for decision in prefix:
        case = service.record_gate_decision(
            case.case_id,
            expected_version=case.version,
            decision=decision,
        )
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
    pending = repository.get_transcript(case.case_id)
    assert pending is not None
    confirmation = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": pending.transcript_id,
            "transcriptSha256": pending.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    analyzing = service.confirm_transcript(
        waiting.case_id,
        expected_case_version=waiting.version,
        confirmation=confirmation,
    ).case
    confirmed = repository.get_transcript(case.case_id)
    assert confirmed is not None and confirmed.confirmed is True
    bound_summary = analyzing.snapshot.intake_summary

    result = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix, target=CaseState.READY_TO_FILL)
    )

    assert repository.get_transcript(case.case_id) == confirmed
    assert result.case.snapshot.intake_summary == bound_summary


@pytest.mark.parametrize("fault", ["ledger", "projection"])
def test_analysis_faults_roll_back_case_gates_audit_workflow_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / f"{fault}.db")
    command = _initial_command(current, prefix)
    before = _counts(repository, current.case_id)
    if fault == "ledger":

        def fail_ledger(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected ledger fault")

        monkeypatch.setattr(repository, "_insert_provider_usage_projection", fail_ledger)
    else:
        original = repository._insert_workflow_projection
        calls = 0

        def fail_projection(*args: object, **kwargs: object) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected projection fault")
            return cast(Any, original)(*args, **kwargs)

        monkeypatch.setattr(repository, "_insert_workflow_projection", fail_projection)

    with pytest.raises(RuntimeError, match="injected"):
        service.commit_analysis_workflow(command)

    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_analysis_stale_service_cas_and_concurrent_commands_have_one_winner(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "concurrent.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    barrier = Barrier(2)

    def commit_once() -> str:
        barrier.wait()
        try:
            service.commit_analysis_workflow(command)
        except CaseVersionConflictError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: commit_once(), range(2)))
    assert sorted(outcomes) == ["committed", "stale"]
    assert service.get_case(current.case_id).version == current.version + 1
    assert [item.decision.gate_id for item in repository.list_gate_decisions(current.case_id)] == [
        *GATE_SEQUENCE,
    ]

    with pytest.raises(CaseVersionConflictError):
        service.commit_analysis_workflow(command)


def test_gate_matrix_rejects_missing_failed_and_clarifiable_block_without_writes(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "gate-matrix.db")
    base = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    before = _counts(repository, current.case_id)

    for invalid in (
        replace(base, gate_decisions=base.gate_decisions[:-1]),
        _initial_command(current, prefix, target=CaseState.AWAITING_CLARIFICATION),
    ):
        if invalid.target is CaseState.AWAITING_CLARIFICATION:
            packet = cast(ClaimPacket, invalid.claim_packet).model_copy(
                update={"state": CaseState.BLOCKED}
            )
            invalid = replace(
                invalid,
                target=CaseState.BLOCKED,
                claim_packet=packet,
                active_clarification=None,
                plan_steps=(),
                clarification_events=(),
            )
        with pytest.raises(CaseSnapshotValidationError):
            service.commit_analysis_workflow(invalid)
        assert service.get_case(current.case_id) == current
        assert _counts(repository, current.case_id) == before

    failed_g5 = _gate(
        GateId.G5_COMPLETENESS,
        5,
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
    )
    failed_ready_gates = (*base.gate_decisions[:-1], failed_g5)
    failed_ready_packet = _packet(
        current.case_id,
        CaseState.READY_TO_FILL,
        (*prefix, *failed_ready_gates),
        missing=True,
    )
    with pytest.raises(CaseSnapshotValidationError):
        service.commit_analysis_workflow(
            replace(
                base,
                gate_decisions=failed_ready_gates,
                claim_packet=failed_ready_packet,
                plan_steps=_plan_events(failed_ready_packet),
            )
        )
    assert _counts(repository, current.case_id) == before


def test_awaiting_plan_rejects_fill_tool_and_free_reason_text(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "plan-authority.db")
    command = _initial_command(current, prefix)
    packet = cast(ClaimPacket, command.claim_packet)
    ready_packet = _packet(
        current.case_id,
        CaseState.READY_TO_FILL,
        packet.gate_decisions,
        missing=False,
    )
    fill_plan_packet = packet.model_copy(update={"plan": ready_packet.plan})
    fill_command = replace(
        command,
        claim_packet=fill_plan_packet,
        plan_steps=_plan_events(fill_plan_packet),
    )
    bad_step = packet.plan.steps[0].model_copy(update={"reason": "untrusted portal text"})
    bad_plan = packet.plan.model_copy(
        update={"steps": (bad_step, *packet.plan.steps[1:])}
    )
    reason_packet = packet.model_copy(update={"plan": bad_plan})
    reason_command = replace(
        command,
        claim_packet=reason_packet,
        plan_steps=_plan_events(reason_packet),
    )

    for invalid in (fill_command, reason_command):
        with pytest.raises(CaseSnapshotValidationError, match="safe plan"):
            service.commit_analysis_workflow(invalid)
        assert service.get_case(current.case_id) == current
        assert repository.list_provider_usage(current.case_id) == ()


def test_blocked_plan_rejects_fill_or_clarification_authority(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "blocked-plan.db")
    round_one = service.commit_analysis_workflow(_initial_command(current, prefix)).case
    round_two = service.commit_analysis_workflow(
        _continuation_command(round_one, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    round_three = service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    command = _continuation_command(round_three, target=CaseState.BLOCKED)
    packet = cast(ClaimPacket, command.claim_packet)
    ready_plan = _packet(
        round_three.case_id,
        CaseState.READY_TO_FILL,
        packet.gate_decisions,
        missing=False,
    ).plan
    unsafe_packet = packet.model_copy(update={"plan": ready_plan})
    invalid = replace(
        command,
        claim_packet=unsafe_packet,
        plan_steps=_plan_events(unsafe_packet),
    )
    before = _counts(repository, round_three.case_id)

    with pytest.raises(CaseSnapshotValidationError, match="safe plan"):
        service.commit_analysis_workflow(invalid)

    assert service.get_case(round_three.case_id) == round_three
    assert _counts(repository, round_three.case_id) == before


@pytest.mark.parametrize("tampering", ["case", "attachments", "passed"])
def test_model_copy_cannot_bypass_canonical_command_validation(
    tmp_path: Path,
    tampering: str,
) -> None:
    service, repository, prefix, current = _analysis_case(
        tmp_path / f"model-copy-{tampering}.db"
    )
    command = _initial_command(current, prefix)
    packet = cast(ClaimPacket, command.claim_packet)
    if tampering == "case":
        invalid = replace(
            command,
            claim_packet=packet.model_copy(update={"case_id": "case-forged"}),
        )
    elif tampering == "attachments":
        claim = packet.claim.model_copy(update={"attachments": ("one", "two")})
        invalid = replace(
            command,
            claim_packet=packet.model_copy(update={"claim": claim}),
        )
    else:
        gates = (
            command.gate_decisions[0].model_copy(update={"passed": False}),
            *command.gate_decisions[1:],
        )
        invalid = replace(command, gate_decisions=gates)

    before = _counts(repository, current.case_id)
    with pytest.raises(CaseSnapshotValidationError, match="canonical|caseId"):
        service.commit_analysis_workflow(invalid)
    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_forged_latest_gate_prefix_and_tampered_json_fail_closed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    service, repository, prefix, current = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case
    command = _continuation_command(waiting, target=CaseState.READY_TO_FILL)

    forged = _gate(GateId.G0_INTAKE, 11)
    later_service = CaseService(
        repository,
        now=lambda: NOW + timedelta(seconds=11),
        case_id_factory=lambda: "unused-case-id",
    )
    mutated = later_service.record_gate_decision(
        waiting.case_id,
        expected_version=waiting.version,
        decision=forged,
    )
    with pytest.raises(CaseSnapshotValidationError, match="latest persisted"):
        service.commit_analysis_workflow(replace(command, expected_version=mutated.version))
    assert service.get_case(waiting.case_id) == mutated

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE gate_decisions SET decision_json = '{}' WHERE sequence = "
            "(SELECT MAX(sequence) FROM gate_decisions)"
        )
    with pytest.raises(PersistedDataIntegrityError, match="gate history"):
        repository.commit_analysis_workflow(replace(command, expected_version=mutated.version))


def test_quota_failure_is_atomic_nonretrying_and_never_creates_a_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "quota.db"
    service, repository, _prefix, current = _analysis_case(database_path)
    before_gates = repository.list_gate_decisions(current.case_id)
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )

    result = service.commit_terminal_provider_failure(command)

    assert result.case.state is CaseState.FAILED
    assert result.case.version == current.version + 1
    assert [event.event.kind for event in result.workflow_events] == [
        "operational_failure",
        "state",
    ]
    assert repository.list_gate_decisions(current.case_id) == before_gates
    usage = repository.list_provider_usage(current.case_id)
    assert len(usage) == 1
    assert usage[0].failure_category is not None
    assert usage[0].failure_category.value == "quota_exhausted"
    assert usage[0].status == "failed"


def test_terminal_provider_operation_must_match_current_workflow_state(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "operation-state.db")
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case
    command = TerminalProviderFailureCommand(
        case_id=waiting.case_id,
        expected_version=waiting.version,
        event=_operational_failure(),
        provider_events=(),
        claim_packet=None,
        occurred_at=waiting.updated_at + timedelta(seconds=1),
    )
    before = _counts(repository, waiting.case_id)

    with pytest.raises(CaseSnapshotValidationError, match="requires case state analyzing"):
        service.commit_terminal_provider_failure(command)

    assert service.get_case(waiting.case_id) == waiting
    assert _counts(repository, waiting.case_id) == before


def test_second_call_terminal_failure_persists_complete_retry_prefix(
    tmp_path: Path,
) -> None:
    service, repository, _prefix, current = _analysis_case(tmp_path / "terminal-retry.db")
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(
            "provider_unavailable",
            call_sequence=2,
            retry_attempt=1,
        ),
        provider_events=(
            ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(_retry(), NOW + timedelta(seconds=1)),
        ),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )

    result = service.commit_terminal_provider_failure(command)

    assert [event.event.kind for event in result.workflow_events] == [
        "provider_call",
        "retry",
        "operational_failure",
        "state",
    ]
    assert [item.status for item in repository.list_provider_usage(current.case_id)] == [
        "succeeded",
        "retry_scheduled",
        "failed",
    ]
    assert repository.list_gate_decisions(current.case_id)[-1].decision.gate_id is GateId.G1_PRIVACY


@pytest.mark.parametrize("fault", ["ledger", "projection"])
def test_terminal_failure_faults_roll_back_every_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    service, repository, _prefix, current = _analysis_case(tmp_path / f"terminal-{fault}.db")
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )
    before = _counts(repository, current.case_id)
    if fault == "ledger":

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected ledger failure")

        monkeypatch.setattr(repository, "_insert_provider_usage_projection", fail)
    else:

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(repository, "_insert_workflow_projection", fail)

    with pytest.raises(RuntimeError, match="injected"):
        service.commit_terminal_provider_failure(command)
    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_generic_operational_append_and_analysis_append_cannot_split_boundaries(
    tmp_path: Path,
) -> None:
    _service, repository, _prefix, current = _analysis_case(tmp_path / "generic.db")
    for event in (_operational_failure(), _provider_call()):
        with pytest.raises(WorkflowAtomicityError, match="atomic"):
            repository.append_workflow_event(
                case_id=current.case_id,
                expected_case_version=current.version,
                event=event,
                actor=ActorType.SYSTEM,
                occurred_at=NOW + timedelta(seconds=1),
            )
    assert repository.list_provider_usage(current.case_id) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_version": True},
        {"target": True},
        {"updated_at": "2026-07-14T12:00:10Z"},
        {"provider_events": [ProviderWorkflowEmission(_provider_call(), NOW)]},
        {"active_clarification": {"round": True}},
    ],
)
def test_command_shape_rejects_bool_and_invalid_types_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
) -> None:
    _service, repository, prefix, current = _analysis_case(tmp_path / "strict.db")
    command = _initial_command(current, prefix)

    def must_not_query(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("CAS lookup must not run for an invalid command shape")

    monkeypatch.setattr(repository, "_require_current", must_not_query)
    invalid = replace(command, **cast(Any, mutation))
    with pytest.raises(WorkflowAtomicityError):
        repository.commit_analysis_workflow(invalid)


@pytest.mark.parametrize("mutation", ["version", "event"])
def test_terminal_command_shape_rejects_invalid_values_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _service, repository, _prefix, current = _analysis_case(
        tmp_path / f"terminal-strict-{mutation}.db"
    )
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )
    invalid = (
        replace(command, expected_version=True)
        if mutation == "version"
        else replace(
            command,
            event=command.event.model_copy(update={"retry_attempt": True}),
        )
    )

    def must_not_query(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("CAS lookup must not run for an invalid command shape")

    monkeypatch.setattr(repository, "_require_current", must_not_query)
    with pytest.raises(WorkflowAtomicityError):
        repository.commit_terminal_provider_failure(invalid)


def test_contract_rejects_boolean_clarification_round() -> None:
    with pytest.raises(ValidationError):
        ClarificationWorkflowEvent.model_validate(
            {
                "kind": "clarification",
                "round": True,
                "field": "incident_time",
                "status": "requested",
            }
        )
