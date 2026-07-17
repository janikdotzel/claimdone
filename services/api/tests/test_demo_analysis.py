"""Deterministic authority coverage for the canonical INT-002 demo analysis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import claimdone_api.demo.service as demo_service
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    AllowedTool,
    CaseState,
    ClaimPacket,
    ClarificationAnswerRequest,
    ClarificationView,
    EvidenceItem,
    FactStatus,
    GateId,
    GateReasonCode,
    ProviderCallWorkflowEvent,
    ProviderModelId,
    RequiredClaimField,
    WorkflowOperation,
)
from claimdone_api.demo import (
    INT002_CLARIFICATION_QUESTION,
    INT002_FIXTURE_VERSION,
    INT002_IMAGE_FIXTURES,
    INT002_INCIDENT_TIME,
    INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST,
    INT002_SYNTHETIC_STATEMENT_SHA256,
    INT002_SYNTHETIC_STATEMENT_TEXT,
    ApprovedDemoIntake,
    BoundDemoClarification,
    ConfirmedSyntheticStatement,
    DemoAnalysisInputError,
    DemoAnalysisRequest,
    DemoAnalysisResult,
    DemoClarificationResolution,
    ReconstructedDemoContinuation,
    analyze_int002_demo,
    reconstruct_int002_clarification,
)
from claimdone_api.gates import ModelExtraction, make_gate_decision

BASE_TIME = datetime(2026, 7, 15, 12, tzinfo=UTC)
SECOND_ROUND_TIME = BASE_TIME + timedelta(hours=1)
CASE_ID = "case-int002-demo-001"
INITIAL_VERSION = 4
RESOLVED_VERSION = INITIAL_VERSION + 1
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def gate_clock(gate_id: GateId) -> datetime:
    return BASE_TIME + timedelta(seconds=int(gate_id.value[1:]))


def second_gate_clock(gate_id: GateId) -> datetime:
    return SECOND_ROUND_TIME + timedelta(seconds=int(gate_id.value[1:]))


def deterministic_id(seed: str) -> str:
    return f"clarification-{seed[:32]}"


def image(index: int, *, run_tag: str = "default") -> EvidenceItem:
    if 1 <= index <= len(INT002_IMAGE_FIXTURES):
        expected = INT002_IMAGE_FIXTURES[index - 1]
        evidence_id = expected.semantic_id
        digest = expected.sha256
    else:
        evidence_id = f"int002-image-extra-{index}"
        digest = "f" * 64
    return EvidenceItem.model_validate(
        {
            "evidenceId": evidence_id,
            "kind": "image",
            "localRef": f"model-{run_tag}-{index}.png",
            "mediaType": "image/png",
            "sha256": digest,
            "text": None,
            "modelCopyApproved": True,
        }
    )


def statement(
    *,
    text: str = INT002_SYNTHETIC_STATEMENT_TEXT,
    confirmed: bool = True,
    run_tag: str = "default",
) -> ConfirmedSyntheticStatement:
    return ConfirmedSyntheticStatement(
        evidence=EvidenceItem.model_validate(
            {
                "evidenceId": f"evidence-statement-{run_tag}",
                "kind": "user_statement",
                "localRef": f"statement-{run_tag}.txt",
                "mediaType": "text/plain",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "text": text,
                "modelCopyApproved": True,
            }
        ),
        confirmed=confirmed,
    )


def approved_intake(
    *,
    images: tuple[EvidenceItem, ...] | None = None,
    confirmed_statement: ConfirmedSyntheticStatement | None = None,
    run_tag: str = "default",
) -> ApprovedDemoIntake:
    return ApprovedDemoIntake(
        images=(
            images
            if images is not None
            else tuple(image(index, run_tag=run_tag) for index in range(1, 4))
        ),
        statement=(
            confirmed_statement if confirmed_statement is not None else statement(run_tag=run_tag)
        ),
        g0_decision=make_gate_decision(GateId.G0_INTAKE, decided_at=BASE_TIME),
        g1_decision=make_gate_decision(
            GateId.G1_PRIVACY,
            decided_at=BASE_TIME + timedelta(seconds=1),
        ),
    )


def first_round(
    *,
    intake: ApprovedDemoIntake | None = None,
    case_id: str = CASE_ID,
    case_version: int = INITIAL_VERSION,
) -> DemoAnalysisResult:
    return analyze_int002_demo(
        DemoAnalysisRequest(
            case_id=case_id,
            case_version=case_version,
            intake=intake if intake is not None else approved_intake(),
        ),
        clock=gate_clock,
        clarification_id_factory=deterministic_id,
    )


def resolution(
    initial: DemoAnalysisResult,
    *,
    clarification: BoundDemoClarification | None = None,
    answer: str = INT002_INCIDENT_TIME,
    clarification_id: str | None = None,
    prior_packet: ClaimPacket | None = None,
) -> DemoClarificationResolution:
    selected = clarification or initial.clarification
    assert selected is not None
    view = selected.view
    return DemoClarificationResolution(
        clarification=selected,
        answer=ClarificationAnswerRequest.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": view.case_id,
                "clarificationId": clarification_id or view.clarification_id,
                "field": view.field.value,
                "round": view.round,
                "expectedVersion": view.expected_version,
                "answer": answer,
            }
        ),
        prior_packet=prior_packet or initial.packet,
    )


def resolved_round(
    initial: DemoAnalysisResult,
    *,
    selected_resolution: DemoClarificationResolution | None = None,
    case_version: int | None = None,
    intake: ApprovedDemoIntake | None = None,
) -> DemoAnalysisResult:
    selected = initial.clarification
    assert selected is not None
    return analyze_int002_demo(
        DemoAnalysisRequest(
            case_id=initial.packet.case_id,
            case_version=(selected.view.expected_version if case_version is None else case_version),
            intake=intake if intake is not None else approved_intake(),
            clarification_resolution=(
                selected_resolution if selected_resolution is not None else resolution(initial)
            ),
        ),
        clock=second_gate_clock,
        clarification_id_factory=deterministic_id,
    )


def test_fixture_constants_match_committed_int002_manifest_and_statement() -> None:
    manifest_path = REPOSITORY_ROOT / "fixtures" / "int002" / "manifest.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    image_entries = cast(list[dict[str, Any]], manifest["images"])

    assert manifest["schemaVersion"] == 1
    assert manifest["fixtureId"] == INT002_FIXTURE_VERSION
    assert manifest["synthetic"] is True
    assert tuple(
        (entry["semanticId"], entry["filename"], entry["sha256"]) for entry in image_entries
    ) == tuple(
        (fixture.semantic_id, fixture.filename, fixture.sha256) for fixture in INT002_IMAGE_FIXTURES
    )
    statement_entry = cast(dict[str, Any], manifest["statement"])
    assert statement_entry["normalization"] == "strip_one_terminal_newline"
    statement_bytes = (manifest_path.parent / statement_entry["filename"]).read_bytes()
    assert statement_bytes.endswith(b"\n")
    normalized_statement = statement_bytes[:-1]
    assert normalized_statement.decode("utf-8") == INT002_SYNTHETIC_STATEMENT_TEXT
    assert hashlib.sha256(normalized_statement).hexdigest() == statement_entry["sha256"]
    assert statement_entry["sha256"] == INT002_SYNTHETIC_STATEMENT_SHA256


def test_first_round_emits_exactly_one_incident_time_clarification() -> None:
    intake = approved_intake()
    result = first_round(intake=intake)

    assert result.packet.state is CaseState.AWAITING_CLARIFICATION
    assert result.packet.claim.incident_time is None
    assert result.packet.claim.missing_required_fields == (RequiredClaimField.INCIDENT_TIME,)
    assert result.clarification is not None
    assert result.clarification.view.field is RequiredClaimField.INCIDENT_TIME
    assert result.clarification.view.question == "What time did the incident happen?"
    assert result.clarification.view.question == INT002_CLARIFICATION_QUESTION
    assert result.clarification.view.round == 1
    assert result.clarification.view.expected_version == RESOLVED_VERSION
    assert tuple(decision.gate_id for decision in result.packet.gate_decisions) == tuple(
        GateId(f"G{index}") for index in range(6)
    )
    assert all(decision.passed for decision in result.packet.gate_decisions[:5])
    assert result.packet.gate_decisions[5].reason_codes == (
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
    )
    assert [step.tool for step in result.packet.plan.steps] == [
        AllowedTool.INSPECT_EVIDENCE,
        AllowedTool.CHECK_REQUIRED_FIELDS,
        AllowedTool.ASK_CLARIFICATION,
    ]
    assert sum(step.tool is AllowedTool.ASK_CLARIFICATION for step in result.packet.plan.steps) == 1
    assert result.packet.evidence == (*intake.images, intake.statement.evidence)
    assert len(result.packet.evidence) == 4
    assert tuple(item.evidence_id for item in result.packet.evidence[:3]) == tuple(
        item.semantic_id for item in INT002_IMAGE_FIXTURES
    )
    assert tuple(item.sha256 for item in result.packet.evidence[:3]) == tuple(
        item.sha256 for item in INT002_IMAGE_FIXTURES
    )
    assert result.round_kind == "initial"
    assert tuple(item.gate_id for item in result.new_gate_decisions) == (
        GateId.G2_OUTPUT_CONTRACT,
        GateId.G3_SAFETY_SCOPE,
        GateId.G4_PROVENANCE,
        GateId.G5_COMPLETENESS,
    )
    assert result.provider_call_count == 0
    assert result.external_provider_call_count == 0
    assert result.mock_provider_event_count == 1
    assert result.execution.fixture_version == INT002_FIXTURE_VERSION
    assert result.initial_persistence is not None
    persistence = result.initial_persistence
    assert len(persistence.g2_attempts) == 1
    assert len(persistence.provider_events) == 1
    attempt = persistence.g2_attempts[0]
    assert attempt.decided_at == result.packet.gate_decisions[2].decided_at
    payload = attempt.envelope.payload
    assert type(payload) is str
    assert payload not in repr(persistence)
    extraction = ModelExtraction.model_validate_json(payload)
    assert extraction.evidence == result.packet.evidence
    assert extraction.provenance == result.packet.provenance
    assert extraction.facts == result.packet.facts
    assert extraction.claim == result.packet.claim
    emission = persistence.provider_events[0]
    assert emission.occurred_at == attempt.decided_at
    assert isinstance(emission.event, ProviderCallWorkflowEvent)
    assert emission.event.operation is WorkflowOperation.EXTRACTION
    assert emission.event.model_id is ProviderModelId.DETERMINISTIC_MOCK
    assert emission.event.provider_mode == "mock"
    assert emission.event.usage is None
    assert emission.event.cost is None
    assert INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST is True


def test_confirmed_clarification_preserves_prefix_and_emits_only_g4_g5() -> None:
    initial = first_round()
    result = resolved_round(initial)

    assert result.clarification is None
    assert result.packet.state is CaseState.READY_TO_FILL
    assert result.packet.claim.incident_time is not None
    assert result.packet.claim.incident_time.isoformat() == INT002_INCIDENT_TIME
    assert result.packet.claim.missing_required_fields == ()
    assert result.packet.gate_decisions[:4] == initial.packet.gate_decisions[:4]
    assert tuple(item.gate_id for item in result.new_gate_decisions) == (
        GateId.G4_PROVENANCE,
        GateId.G5_COMPLETENESS,
    )
    assert result.packet.gate_decisions[4:] == result.new_gate_decisions
    assert result.packet.gate_decisions[4].decided_at == SECOND_ROUND_TIME + timedelta(seconds=4)
    assert result.packet.gate_decisions[5].decided_at == SECOND_ROUND_TIME + timedelta(seconds=5)
    assert all(decision.passed for decision in result.packet.gate_decisions)
    assert [step.tool for step in result.packet.plan.steps] == [
        AllowedTool.INSPECT_EVIDENCE,
        AllowedTool.CHECK_REQUIRED_FIELDS,
        AllowedTool.INSPECT_FORM,
        AllowedTool.FILL_UNTIL_REVIEW,
        AllowedTool.VERIFY_RENDERED_FIELDS,
    ]
    assert AllowedTool.READ_RECEIPT not in {step.tool for step in result.packet.plan.steps}
    assert result.packet.plan.agent_can_submit is False
    assert result.round_kind == "clarification"
    assert result.provider_call_count == 0
    assert result.external_provider_call_count == 0
    assert result.mock_provider_event_count == 0
    assert result.initial_persistence is None


def test_clarification_round_never_reruns_g2_or_g3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = first_round()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"G2/G3 reran: {args!r} {kwargs!r}")

    monkeypatch.setattr(demo_service, "evaluate_g2", forbidden)
    monkeypatch.setattr(demo_service, "evaluate_g3", forbidden)

    result = resolved_round(initial)

    assert result.packet.gate_decisions[:4] == initial.packet.gate_decisions[:4]


def test_clarification_reconstructs_from_reopened_persisted_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intake = approved_intake()
    initial = first_round(intake=intake)
    assert initial.clarification is not None
    database = tmp_path / "clarification-restart.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE persisted (view_json TEXT NOT NULL, packet_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO persisted VALUES (?, ?)",
            (
                initial.clarification.view.model_dump_json(by_alias=True),
                initial.packet.model_dump_json(by_alias=True),
            ),
        )
    del initial, intake

    with sqlite3.connect(database) as reopened:
        row = reopened.execute("SELECT view_json, packet_json FROM persisted").fetchone()
    assert row is not None
    restored_view = ClarificationView.model_validate_json(row[0])
    restored_packet = ClaimPacket.model_validate_json(row[1])
    restored = reconstruct_int002_clarification(
        view=restored_view,
        prior_packet=restored_packet,
    )
    assert isinstance(restored, ReconstructedDemoContinuation)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"G2/G3 reran after restart: {args!r} {kwargs!r}")

    monkeypatch.setattr(demo_service, "evaluate_g2", forbidden)
    monkeypatch.setattr(demo_service, "evaluate_g3", forbidden)
    view = restored.clarification.view
    result = analyze_int002_demo(
        DemoAnalysisRequest(
            case_id=restored.prior_packet.case_id,
            case_version=view.expected_version,
            intake=restored.intake,
            clarification_resolution=DemoClarificationResolution(
                clarification=restored.clarification,
                answer=ClarificationAnswerRequest.model_validate(
                    {
                        "contractVersion": CONTRACT_VERSION,
                        "caseId": view.case_id,
                        "clarificationId": view.clarification_id,
                        "field": view.field.value,
                        "round": view.round,
                        "expectedVersion": view.expected_version,
                        "answer": INT002_INCIDENT_TIME,
                    }
                ),
                prior_packet=restored.prior_packet,
            ),
        ),
        clock=second_gate_clock,
        clarification_id_factory=deterministic_id,
    )

    assert result.packet.state is CaseState.READY_TO_FILL
    assert result.packet.gate_decisions[:4] == restored_packet.gate_decisions[:4]


def test_canonical_clarification_identity_matches_repository_formula() -> None:
    initial = first_round()
    selected = resolution(initial)
    result = resolved_round(initial, selected_resolution=selected)
    raw_answer = selected.answer.answer
    digest = hashlib.sha256(raw_answer.encode()).hexdigest()
    identity = hashlib.sha256(
        (
            "claimdone-clarification-v1\0"
            f"{CASE_ID}\0{selected.answer.clarification_id}\0"
            f"{selected.answer.round}\0{digest}"
        ).encode()
    ).hexdigest()
    suffix = identity[:32]

    appended_evidence = result.packet.evidence[-1]
    appended_provenance = result.packet.provenance[-1]
    answer_fact = next(fact for fact in result.packet.facts if fact.field.value == "incident_time")
    assert appended_evidence.evidence_id == f"clarification-{suffix}"
    assert appended_evidence.local_ref == f"clarification-{suffix}.txt"
    assert appended_evidence.sha256 == digest
    assert appended_provenance.provenance_id == f"provenance-{suffix}"
    assert appended_provenance.evidence_id == appended_evidence.evidence_id
    assert answer_fact.fact_id == f"fact-{suffix}"
    assert answer_fact.source_refs == (appended_provenance.provenance_id,)


def test_exactly_three_ordered_attachment_identities_are_preserved() -> None:
    intake = approved_intake()
    result = first_round(intake=intake)

    assert result.packet.claim.attachments == tuple(image.local_ref for image in intake.images)
    packet_images = tuple(item for item in result.packet.evidence if item.kind.value == "image")
    assert packet_images == intake.images
    assert tuple(item.local_ref for item in packet_images) == result.packet.claim.attachments


def test_abstract_image_fixtures_never_create_observed_damage_facts() -> None:
    result = first_round()

    assert all(fact.status is not FactStatus.OBSERVED for fact in result.packet.facts)
    assert {
        fact.field.value
        for fact in result.packet.facts
        if fact.field.value in {"visible_damage", "impact_area"}
    } == set()


def test_two_identical_runs_have_identical_semantic_output() -> None:
    first = first_round()
    second = first_round()

    assert first.execution.semantic_sha256 == second.execution.semantic_sha256
    assert first.packet.model_dump(mode="json", by_alias=True) == second.packet.model_dump(
        mode="json",
        by_alias=True,
    )
    assert first.clarification == second.clarification

    first_resolved = resolved_round(first)
    second_resolved = resolved_round(second)
    assert first_resolved.execution.semantic_sha256 == (second_resolved.execution.semantic_sha256)
    assert first_resolved.packet == second_resolved.packet


def test_semantic_digest_normalizes_run_owned_case_media_and_clarification_ids() -> None:
    intake_a = approved_intake(run_tag="run-a")
    intake_b = approved_intake(run_tag="run-b")
    initial_a = first_round(
        intake=intake_a,
        case_id="case-int002-semantic-a",
    )
    initial_b = first_round(
        intake=intake_b,
        case_id="case-int002-semantic-b",
    )

    assert initial_a.packet != initial_b.packet
    assert initial_a.clarification is not None
    assert initial_b.clarification is not None
    assert (
        initial_a.clarification.view.clarification_id
        != initial_b.clarification.view.clarification_id
    )
    assert tuple(item.local_ref for item in initial_a.packet.evidence) != tuple(
        item.local_ref for item in initial_b.packet.evidence
    )
    assert initial_a.packet.claim.attachments != initial_b.packet.claim.attachments
    assert initial_a.execution.semantic_sha256 == initial_b.execution.semantic_sha256

    resolved_a = resolved_round(initial_a, intake=intake_a)
    resolved_b = resolved_round(initial_b, intake=intake_b)
    assert resolved_a.packet.evidence[-1].evidence_id != resolved_b.packet.evidence[-1].evidence_id
    assert (
        resolved_a.packet.provenance[-1].provenance_id
        != resolved_b.packet.provenance[-1].provenance_id
    )
    incident_a = next(
        fact for fact in resolved_a.packet.facts if fact.field.value == "incident_time"
    )
    incident_b = next(
        fact for fact in resolved_b.packet.facts if fact.field.value == "incident_time"
    )
    assert incident_a.fact_id != incident_b.fact_id
    assert resolved_a.execution.semantic_sha256 == resolved_b.execution.semantic_sha256


def test_semantic_digest_changes_for_value_digest_and_evidence_order() -> None:
    initial = first_round()
    assert initial.clarification is not None
    baseline = initial.execution.semantic_sha256
    assert baseline == demo_service._semantic_digest(
        initial.packet,
        initial.clarification,
        mock_provider_event_count=1,
    )

    value_data = initial.packet.model_dump(mode="json", by_alias=True)
    value_data["claim"]["location"] = "Demo Street 2, Berlin"
    for fact in value_data["facts"]:
        if fact["field"] == "location":
            fact["value"] = "Demo Street 2, Berlin"
    value_packet = ClaimPacket.model_validate(value_data)

    digest_data = initial.packet.model_dump(mode="json", by_alias=True)
    digest_data["evidence"][0]["sha256"] = "0" * 64
    digest_packet = ClaimPacket.model_validate(digest_data)

    order_data = initial.packet.model_dump(mode="json", by_alias=True)
    order_data["evidence"][0], order_data["evidence"][1] = (
        order_data["evidence"][1],
        order_data["evidence"][0],
    )
    order_data["claim"]["attachments"][0], order_data["claim"]["attachments"][1] = (
        order_data["claim"]["attachments"][1],
        order_data["claim"]["attachments"][0],
    )
    expected_ids = order_data["verification"]["expectedAttachmentIds"]
    expected_ids[0], expected_ids[1] = expected_ids[1], expected_ids[0]
    order_packet = ClaimPacket.model_validate(order_data)

    for changed in (value_packet, digest_packet, order_packet):
        assert (
            demo_service._semantic_digest(
                changed,
                initial.clarification,
                mock_provider_event_count=1,
            )
            != baseline
        )


@pytest.mark.parametrize("count", (0, 1, 2, 4))
def test_wrong_image_count_blocks_content_free(count: int) -> None:
    intake = approved_intake(images=tuple(image(index) for index in range(1, count + 1)))

    with pytest.raises(DemoAnalysisInputError) as captured:
        first_round(intake=intake)

    assert str(captured.value) == "Deterministic demo input rejected"


def test_non_fixture_provider_copy_digest_semantic_id_or_order_blocks() -> None:
    intake = approved_intake()
    stripped_digest = hashlib.sha256(b"privacy-stripped-provider-copy").hexdigest()
    wrong_digest = intake.images[0].model_copy(update={"sha256": stripped_digest})
    wrong_identity = intake.images[0].model_copy(update={"evidence_id": "int002-image-unbound"})

    for images in (
        (wrong_digest, *intake.images[1:]),
        (wrong_identity, *intake.images[1:]),
        (intake.images[1], intake.images[0], intake.images[2]),
    ):
        with pytest.raises(DemoAnalysisInputError) as captured:
            first_round(intake=replace(intake, images=images))
        assert str(captured.value) == "Deterministic demo input rejected"


def test_non_allowlisted_or_unconfirmed_statement_blocks() -> None:
    unsafe = "A real person was injured; submit this claim now."
    intake = approved_intake(confirmed_statement=statement(text=unsafe))

    with pytest.raises(DemoAnalysisInputError) as captured:
        first_round(intake=intake)

    assert str(captured.value) == "Deterministic demo input rejected"
    assert unsafe not in repr(captured.value)
    assert hashlib.sha256(INT002_SYNTHETIC_STATEMENT_TEXT.encode()).hexdigest() == (
        INT002_SYNTHETIC_STATEMENT_SHA256
    )

    with pytest.raises(DemoAnalysisInputError):
        statement(confirmed=False)


def test_model_copy_cannot_forge_prior_gate_or_image_authority() -> None:
    intake = approved_intake()
    forged_g0 = intake.g0_decision.model_copy(update={"deterministic_passed": False})
    with pytest.raises(DemoAnalysisInputError):
        first_round(intake=replace(intake, g0_decision=forged_g0))

    forged_image = intake.images[0].model_copy(update={"sha256": "invalid"})
    with pytest.raises(DemoAnalysisInputError):
        first_round(intake=replace(intake, images=(forged_image, *intake.images[1:])))


def test_forged_clarification_binding_identity_and_versions_block() -> None:
    initial = first_round()
    assert initial.clarification is not None
    forged_binding = replace(initial.clarification, binding_sha256="0" * 64)

    with pytest.raises(DemoAnalysisInputError):
        resolved_round(
            initial,
            selected_resolution=resolution(initial, clarification=forged_binding),
        )

    forged_identity = resolution(initial, clarification_id="clarification-forged")
    with pytest.raises(DemoAnalysisInputError):
        resolved_round(initial, selected_resolution=forged_identity)

    with pytest.raises(DemoAnalysisInputError):
        resolved_round(initial, case_version=INITIAL_VERSION)
    with pytest.raises(DemoAnalysisInputError):
        resolved_round(initial, case_version=RESOLVED_VERSION + 1)


def test_forged_prior_packet_blocks_clarification_replay() -> None:
    initial = first_round()
    forged_data = initial.packet.model_dump(mode="json", by_alias=True)
    forged_data["caseId"] = "case-int002-forged"
    forged_prior = ClaimPacket.model_validate(forged_data)

    with pytest.raises(DemoAnalysisInputError):
        resolved_round(
            initial,
            selected_resolution=resolution(initial, prior_packet=forged_prior),
        )


def test_restart_reconstruction_rejects_internal_media_and_provenance_rebinding() -> None:
    initial = first_round()
    assert initial.clarification is not None

    changed_evidence = initial.packet.evidence[0].model_copy(
        update={"local_ref": "unbound-runtime-copy.png"}
    )
    local_ref_mismatch = initial.packet.model_copy(
        update={"evidence": (changed_evidence, *initial.packet.evidence[1:])}
    )

    reversed_attachments = tuple(reversed(initial.packet.claim.attachments))
    attachment_mismatch = initial.packet.model_copy(
        update={
            "claim": initial.packet.claim.model_copy(update={"attachments": reversed_attachments})
        }
    )

    provenance_data = initial.packet.model_dump(mode="json", by_alias=True)
    provenance_data["provenance"][0]["evidenceId"] = initial.packet.evidence[1].evidence_id
    provenance_rebinding = ClaimPacket.model_validate(provenance_data)

    for forged_prior in (local_ref_mismatch, attachment_mismatch, provenance_rebinding):
        with pytest.raises(DemoAnalysisInputError):
            reconstruct_int002_clarification(
                view=initial.clarification.view,
                prior_packet=forged_prior,
            )


def test_recomputed_binding_cannot_authorize_mutated_prior_claim() -> None:
    intake = approved_intake()
    initial = first_round(intake=intake)
    assert initial.clarification is not None
    forged_data = initial.packet.model_dump(mode="json", by_alias=True)
    forged_data["claim"]["location"] = "Munich"
    for fact in forged_data["facts"]:
        if fact["field"] == "location":
            fact["value"] = "Munich"
    forged_prior = ClaimPacket.model_validate(forged_data)
    rebound = replace(
        initial.clarification,
        binding_sha256=demo_service._clarification_binding(
            intake,
            initial.clarification.view,
            forged_prior,
        ),
    )

    with pytest.raises(DemoAnalysisInputError) as captured:
        resolved_round(
            initial,
            selected_resolution=resolution(
                initial,
                clarification=rebound,
                prior_packet=forged_prior,
            ),
            intake=intake,
        )
    assert str(captured.value) == "Deterministic demo input rejected"
    with pytest.raises(DemoAnalysisInputError):
        reconstruct_int002_clarification(
            view=initial.clarification.view,
            prior_packet=forged_prior,
        )


def test_only_exact_canonical_clarification_answer_is_accepted() -> None:
    initial = first_round()

    for value in ("14:30", " 14:30:00", "14:30:01"):
        with pytest.raises(DemoAnalysisInputError):
            resolved_round(
                initial,
                selected_resolution=resolution(initial, answer=value),
            )


def test_initial_version_cannot_overflow_sqlite_int64() -> None:
    request = DemoAnalysisRequest(
        case_id=CASE_ID,
        case_version=(1 << 63) - 1,
        intake=approved_intake(),
    )

    with pytest.raises(DemoAnalysisInputError):
        analyze_int002_demo(
            request,
            clock=gate_clock,
            clarification_id_factory=deterministic_id,
        )
