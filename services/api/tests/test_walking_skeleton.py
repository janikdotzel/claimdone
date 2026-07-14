"""Deterministic integration tests for INT-001 without HTTP multipart parsing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from PIL import Image

import claimdone_api.walking_skeleton.safety as walking_safety_module
import claimdone_api.walking_skeleton.service as walking_service_module
from claimdone_api.cases import CaseService
from claimdone_api.contracts import (
    CaseState,
    GateId,
    GateReasonCode,
    PortalState,
    VerificationState,
)
from claimdone_api.media import (
    CaseMediaStore,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
    MediaStorageError,
    PersistentCaseMediaCleaner,
)
from claimdone_api.persistence import SqliteCaseRepository
from claimdone_api.walking_skeleton.errors import FlowError, PortalUnavailableError
from claimdone_api.walking_skeleton.models import (
    FlowResponse,
    PortalDraftFields,
    RenderedPortalValues,
)
from claimdone_api.walking_skeleton.service import WalkingSkeletonService


@dataclass
class RecordingPortal:
    fail: bool = False
    mismatch: bool = False
    calls: int = 0
    cleanup_calls: int = 0
    review_cases: set[str] = field(default_factory=set)

    def fill_to_review(
        self,
        case_id: str,
        fields: PortalDraftFields,
    ) -> tuple[str, RenderedPortalValues]:
        self.calls += 1
        if self.fail:
            raise PortalUnavailableError("expected portal failure")
        self.review_cases.add(case_id)
        rendered_fields = fields.model_dump(mode="json", by_alias=True)
        if self.mismatch:
            rendered_fields["narrative"] = "A mismatching rendered value."
        return (
            f"http://127.0.0.1:3000/sandbox/A/cases/{case_id}",
            RenderedPortalValues.model_validate(
                {
                    "caseId": case_id,
                    "state": "review",
                    "fields": rendered_fields,
                    "renderedAt": "2026-07-14T12:00:00Z",
                },
                strict=False,
            ),
        )

    def cleanup_case(self, case_id: str) -> None:
        self.cleanup_calls += 1
        self.review_cases.discard(case_id)


@dataclass(frozen=True)
class Harness:
    service: WalkingSkeletonService
    cases: CaseService
    repository: SqliteCaseRepository
    store: CaseMediaStore
    portal: RecordingPortal
    case_id: str


def image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (3, 2), color=(20, 120, 110))
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def intake_request(*, text: str = "An arbitrary staged note.") -> IntakeRequest:
    return IntakeRequest(
        images=(
            ImageUpload(image_bytes("JPEG"), "image/jpeg"),
            ImageUpload(image_bytes("PNG"), "image/png"),
            ImageUpload(image_bytes("JPEG"), "image/jpeg"),
        ),
        text=text,
        audio=None,
        consents=IntakeConsents(True, True, True),
    )


def make_harness(tmp_path: Path, portal: RecordingPortal | None = None) -> Harness:
    repository = SqliteCaseRepository(tmp_path / "cases.db")
    store = CaseMediaStore(tmp_path / "media")
    cleaner = PersistentCaseMediaCleaner(repository, store)
    cases = CaseService(
        repository,
        resource_cleaner=cleaner,
        case_id_factory=lambda: "case-int001-001",
    )
    selected_portal = portal or RecordingPortal()
    service = WalkingSkeletonService(
        cases=cases,
        repository=repository,
        media_store=store,
        portal=selected_portal,
        request_id_factory=lambda: "request-00000000000000000000000000000001",
        clarification_id_factory=(
            lambda: "clarification-00000000000000000000000000000001"
        ),
    )
    case = cases.create_case()
    return Harness(service, cases, repository, store, selected_portal, case.case_id)


def begin(harness: Harness, *, text: str = "An arbitrary staged note.") -> FlowResponse:
    return harness.service.intake(
        harness.case_id,
        expected_version=1,
        request=intake_request(text=text),
        exif_decisions=(ExifDecision.STRIP,) * 3,
    )


def test_exactly_one_clarification_then_review_stops_at_verifying(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)

    assert intake.phase == "awaiting_clarification"
    assert intake.case.state is CaseState.AWAITING_CLARIFICATION
    assert intake.case.portal_state is PortalState.DRAFT
    assert intake.clarification is not None
    assert intake.clarification.expected_version == intake.case.version
    assert intake.draft_revision == intake.case.version
    assert tuple(item.gate_id for item in intake.gate_history) == tuple(
        GateId(f"G{index}") for index in range(6)
    )
    assert all(item.passed for item in intake.gate_history[:5])
    assert intake.gate_history[5].reason_codes == (
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
    )

    final = harness.service.answer(
        harness.case_id,
        intake.clarification.clarification_id,
        expected_version=intake.case.version,
        answer="14:30",
    )

    assert final.phase == "review"
    assert final.case.state is CaseState.VERIFYING
    assert final.case.portal_state is PortalState.REVIEW
    assert final.case.active_clarification is None
    assert final.portal is not None
    assert final.portal.verification_state is VerificationState.PENDING
    assert final.portal.review_url == (
        f"http://127.0.0.1:3000/sandbox/A/cases/{harness.case_id}"
    )
    assert all(decision.passed for decision in final.gate_history)
    assert tuple(decision.gate_id for decision in final.gate_history) == tuple(
        GateId(f"G{index}") for index in range(6)
    )
    rendered = cast(dict[str, Any], final.portal.rendered_values)
    fields = cast(dict[str, Any], rendered["fields"])
    assert all(attachment.startswith("model-") for attachment in fields["attachments"])
    assert {decision.gate_id for decision in final.gate_history}.isdisjoint(
        {GateId.G6_TOOL_AUTHORITY, GateId.G7_PORTAL_WRITE, GateId.G8_VERIFICATION}
    )


def test_fixed_mock_values_are_bound_only_to_explicit_synthetic_fixture(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    arbitrary_text = "This note contains none of the structured demo fixture values."
    intake = begin(harness, text=arbitrary_text)
    packet = intake.case.claim_packet
    assert packet is not None

    fixture = next(
        item for item in packet.evidence if item.evidence_id == "evidence-synthetic-fixture"
    )
    assert fixture.text is not None
    for expected in (
        "2026-07-14",
        "Demo Street 1, Berlin",
        "Demo Claimant",
        "DEMO-POLICY-001",
        "DEMO-CD-1",
        "counterparty_known=yes",
        "A staged second vehicle contacted the rear of the demo vehicle in Berlin.",
    ):
        assert expected in fixture.text
    fixed_facts = tuple(
        fact for fact in packet.facts if fact.field.value != "incident_time"
    )
    assert fixed_facts
    assert all(fact.source_refs == ("prov-synthetic-fixture",) for fact in fixed_facts)
    assert all("prov-statement" not in fact.source_refs for fact in fixed_facts)


@pytest.mark.parametrize("invalid_kind", ["stale", "fake", "double"])
def test_stale_fake_and_double_answers_are_rejected(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    clarification_id = intake.clarification.clarification_id
    expected_version = intake.case.version
    if invalid_kind == "stale":
        version = expected_version - 1
        selected_id = clarification_id
    elif invalid_kind == "fake":
        version = expected_version
        selected_id = "clarification-ffffffffffffffffffffffffffffffff"
    else:
        harness.service.answer(
            harness.case_id,
            clarification_id,
            expected_version=expected_version,
            answer="14:30",
        )
        version = harness.cases.get_case(harness.case_id).version
        selected_id = clarification_id

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            selected_id,
            expected_version=version,
            answer="14:30",
        )

    assert captured.value.status_code == 409
    if invalid_kind != "double":
        assert harness.portal.calls == 0


@pytest.mark.parametrize("gate", ["G0", "G1"])
def test_g0_and_g1_block_before_mock_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    harness = make_harness(tmp_path)

    def forbidden_mock(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"mock extraction called: {args!r} {kwargs!r}")

    monkeypatch.setattr(walking_service_module, "deterministic_extraction", forbidden_mock)
    request = intake_request()
    if gate == "G0":
        request = IntakeRequest(
            images=request.images[:2],
            text=request.text,
            audio=None,
            consents=request.consents,
        )
        decisions: tuple[ExifDecision, ...] = (ExifDecision.STRIP,) * 3
    else:
        decisions = (ExifDecision.STRIP,) * 2

    with pytest.raises(FlowError) as captured:
        harness.service.intake(
            harness.case_id,
            expected_version=1,
            request=request,
            exif_decisions=decisions,
        )

    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.gate_id.value == gate
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]


def test_error_after_g1_cannot_leave_case_analyzing_or_retain_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)

    def failed_extraction(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"injected extraction failure: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        walking_service_module,
        "deterministic_extraction",
        failed_extraction,
    )

    with pytest.raises(RuntimeError, match="injected extraction failure"):
        begin(harness)

    failed = harness.cases.get_case(harness.case_id)
    assert failed.state is CaseState.FAILED
    assert tuple(
        item.decision.gate_id
        for item in harness.cases.list_gate_decisions(harness.case_id)
    ) == (GateId.G0_INTAKE, GateId.G1_PRIVACY)
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.portal.calls == 0


@pytest.mark.parametrize(
    "statement",
    (
        "I am injured and need help.",
        (
            "Am 07.07.2026 wurde das Demo-Fahrzeug berührt und möglicherweise "
            "wurde jemand verletzt."
        ),
    ),
)
def test_g3_block_is_audited_and_stops_before_g4_g5_and_portal(
    tmp_path: Path,
    statement: str,
) -> None:
    harness = make_harness(tmp_path)

    with pytest.raises(FlowError) as captured:
        begin(harness, text=statement)

    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.gate_id is GateId.G3_SAFETY_SCOPE
    persisted = harness.cases.list_gate_decisions(harness.case_id)
    assert tuple(item.decision.gate_id for item in persisted) == (
        GateId.G0_INTAKE,
        GateId.G1_PRIVACY,
        GateId.G2_OUTPUT_CONTRACT,
        GateId.G3_SAFETY_SCOPE,
    )
    stopped = harness.cases.get_case(harness.case_id)
    assert stopped.state is CaseState.EMERGENCY_STOPPED
    assert stopped.snapshot.claim_packet is None
    assert stopped.snapshot.active_clarification is None
    assert harness.portal.calls == 0
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]

    with pytest.raises(FlowError) as retried:
        harness.service.intake(
            harness.case_id,
            expected_version=stopped.version,
            request=intake_request(text="A harmless staged retry."),
            exif_decisions=(ExifDecision.STRIP,) * 3,
        )

    assert retried.value.code == "INTAKE_NOT_AVAILABLE"
    assert harness.cases.get_case(harness.case_id).state is CaseState.EMERGENCY_STOPPED
    assert harness.cases.get_case(harness.case_id).snapshot.claim_packet is None
    assert harness.portal.calls == 0


@pytest.mark.parametrize(
    "statement",
    (
        "There were no injuries and no danger in this staged event.",
        "Bei diesem Demo-Ereignis gab es keine Verletzungen und keine Gefahr.",
    ),
)
def test_directly_negated_injury_and_danger_do_not_false_positive(
    tmp_path: Path,
    statement: str,
) -> None:
    harness = make_harness(tmp_path)

    response = begin(harness, text=statement)

    assert response.case.state is CaseState.AWAITING_CLARIFICATION
    g3 = next(item for item in response.gate_history if item.gate_id is GateId.G3_SAFETY_SCOPE)
    assert g3.passed


@pytest.mark.parametrize(
    "statement",
    (
        "No injuries and danger remains at the staged scene.",
        "Keine Verletzungen und Gefahr besteht weiterhin am Demo-Ort.",
    ),
)
def test_each_combined_safety_term_requires_its_own_negator(
    tmp_path: Path,
    statement: str,
) -> None:
    harness = make_harness(tmp_path)

    with pytest.raises(FlowError) as captured:
        begin(harness, text=statement)

    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.reason_codes == (
        GateReasonCode.G3_INJURY_OR_EMERGENCY,
    )
    assert harness.cases.get_case(harness.case_id).state is CaseState.EMERGENCY_STOPPED


@pytest.mark.parametrize(
    ("statement", "expected_reasons"),
    (
        (
            "Open my real insurer website and submit it now.",
            (
                GateReasonCode.G3_REAL_PORTAL,
                GateReasonCode.G3_SUBMISSION_ACTION,
            ),
        ),
        (
            "Entscheide die Schuld und bestätige, welche Zahlung gedeckt ist.",
            (
                GateReasonCode.G3_LEGAL_OR_LIABILITY,
                GateReasonCode.G3_PAYMENT_OR_COVERAGE,
            ),
        ),
    ),
)
def test_canonical_scope_requests_are_terminally_blocked(
    tmp_path: Path,
    statement: str,
    expected_reasons: tuple[GateReasonCode, ...],
) -> None:
    harness = make_harness(tmp_path)

    with pytest.raises(FlowError) as captured:
        begin(harness, text=statement)

    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.reason_codes == expected_reasons
    blocked = harness.cases.get_case(harness.case_id)
    assert blocked.state is CaseState.BLOCKED
    assert blocked.snapshot.claim_packet is None
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.portal.calls == 0


def test_answer_gate_block_terminalizes_and_releases_owned_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    original = walking_safety_module.deterministic_safety_input

    def forced_injury(*args: Any, **kwargs: Any) -> Any:
        return replace(original(*args, **kwargs), injury_reported=True)

    monkeypatch.setattr(
        walking_service_module,
        "deterministic_safety_input",
        forced_injury,
    )

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.reason_codes == (
        GateReasonCode.G3_INJURY_OR_EMERGENCY,
    )
    stopped = harness.cases.get_case(harness.case_id)
    assert stopped.state is CaseState.EMERGENCY_STOPPED
    assert stopped.snapshot.claim_packet is None
    assert stopped.snapshot.active_clarification is None
    assert stopped.snapshot.intake_summary is None
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.portal.calls == 0


def test_terminalization_fault_cannot_skip_gate_media_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    original_safety = walking_safety_module.deterministic_safety_input

    def forced_injury(*args: Any, **kwargs: Any) -> Any:
        return replace(original_safety(*args, **kwargs), injury_reported=True)

    def failed_terminalization(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"injected terminalization failure: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        walking_service_module,
        "deterministic_safety_input",
        forced_injury,
    )
    monkeypatch.setattr(harness.service, "_terminalize_case", failed_terminalization)

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.code == "DETERMINISTIC_GATE_BLOCKED"
    assert captured.value.gate_decision is not None
    assert captured.value.gate_decision.gate_id is GateId.G3_SAFETY_SCOPE
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.portal.calls == 0


def test_media_delete_failure_retains_mapping_for_later_owned_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None
    case_path = harness.store.root / storage_name
    original_delete = harness.store.delete_case
    original_safety = walking_safety_module.deterministic_safety_input

    def forced_injury(*args: Any, **kwargs: Any) -> Any:
        return replace(original_safety(*args, **kwargs), injury_reported=True)

    def failed_delete(*args: Any, **kwargs: Any) -> Any:
        raise MediaStorageError(f"injected media delete failure: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        walking_service_module,
        "deterministic_safety_input",
        forced_injury,
    )
    monkeypatch.setattr(harness.store, "delete_case", failed_delete)

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.code == "DETERMINISTIC_GATE_BLOCKED"
    assert isinstance(captured.value.__cause__, MediaStorageError)
    assert harness.cases.get_case(harness.case_id).state is CaseState.EMERGENCY_STOPPED
    assert harness.repository.get_case_media_handle(harness.case_id) == storage_name
    assert case_path.is_dir()

    monkeypatch.setattr(harness.store, "delete_case", original_delete)
    harness.cases.delete_case(harness.case_id)
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not case_path.exists()


def test_initial_g3_delete_failure_keeps_case_specific_media_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)
    original_delete = harness.store.delete_case

    def failed_delete(*args: Any, **kwargs: Any) -> Any:
        raise MediaStorageError(f"injected media delete failure: {args!r} {kwargs!r}")

    monkeypatch.setattr(harness.store, "delete_case", failed_delete)

    with pytest.raises(FlowError) as captured:
        begin(harness, text="Someone may be injured at the staged scene.")

    assert captured.value.code == "DETERMINISTIC_GATE_BLOCKED"
    assert isinstance(captured.value.__cause__, MediaStorageError)
    assert harness.cases.get_case(harness.case_id).state is CaseState.EMERGENCY_STOPPED
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None
    case_path = harness.store.root / storage_name
    assert case_path.is_dir()

    monkeypatch.setattr(harness.store, "delete_case", original_delete)
    harness.cases.delete_case(harness.case_id)
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not case_path.exists()


@pytest.mark.parametrize("failure", ["unavailable", "mismatch"])
def test_portal_failure_never_reaches_verifying_and_clears_clarification(
    tmp_path: Path,
    failure: str,
) -> None:
    portal = RecordingPortal(fail=failure == "unavailable", mismatch=failure == "mismatch")
    harness = make_harness(tmp_path, portal)
    intake = begin(harness)
    assert intake.clarification is not None

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.status_code == 502
    failed = harness.cases.get_case(harness.case_id)
    assert failed.state is CaseState.FAILED
    assert failed.snapshot.active_clarification is None
    assert failed.snapshot.claim_packet is None
    assert failed.snapshot.intake_summary is None
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.portal.cleanup_calls == 1
    assert harness.portal.review_cases == set()


def test_post_review_persistence_failure_compensates_portal_backend_and_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    original_transition = harness.cases.transition_case

    def persisted_then_failed(*args: Any, **kwargs: Any) -> Any:
        transitioned = original_transition(*args, **kwargs)
        if kwargs.get("target") is CaseState.VERIFYING:
            raise RuntimeError("injected failure after verifying persistence")
        return transitioned

    monkeypatch.setattr(harness.cases, "transition_case", persisted_then_failed)

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.code == "PORTAL_COMMIT_FAILED"
    assert captured.value.status_code == 502
    failed = harness.cases.get_case(harness.case_id)
    assert failed.state is CaseState.FAILED
    assert failed.snapshot.claim_packet is None
    assert failed.snapshot.active_clarification is None
    assert failed.snapshot.intake_summary is None
    assert harness.portal.calls == 1
    assert harness.portal.cleanup_calls == 1
    assert harness.portal.review_cases == set()
    assert harness.repository.get_case_media_handle(harness.case_id) is None
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]


def test_media_mapping_survives_restart_and_delete_and_reset_clean_orphans(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    begin(harness)
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None
    case_path = harness.store.root / storage_name
    assert case_path.is_dir()

    restarted_repository = SqliteCaseRepository(tmp_path / "cases.db")
    restarted_store = CaseMediaStore(tmp_path / "media")
    restarted_cleaner = PersistentCaseMediaCleaner(restarted_repository, restarted_store)
    restarted_cases = CaseService(restarted_repository, resource_cleaner=restarted_cleaner)
    restarted_cases.delete_case(harness.case_id)

    assert not case_path.exists()
    assert restarted_repository.get_case_media_handle(harness.case_id) is None
    orphan = restarted_store.create_case()
    assert (restarted_store.root / orphan.storage_name).is_dir()
    assert restarted_cases.reset_demo() == 0
    assert not (restarted_store.root / orphan.storage_name).exists()


def test_corrupt_persisted_clarification_cannot_authorize_answer(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    corrupt = harness.cases.save_active_clarification(
        harness.case_id,
        expected_version=intake.case.version,
        clarification={
            "clarificationId": "clarification-00000000000000000000000000000001",
            "field": "location",
            "question": "Wrong field",
            "expectedVersion": intake.case.version + 1,
        },
    )

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            "clarification-00000000000000000000000000000001",
            expected_version=corrupt.version,
            answer="14:30",
        )

    assert captured.value.code == "CASE_STATE_INVALID"
    assert captured.value.status_code == 409
    assert harness.portal.calls == 0


def test_case_lock_registry_is_empty_after_many_distinct_failures(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    for index in range(50):
        with pytest.raises(FlowError):
            harness.service.intake(
                f"missing-case-{index}",
                expected_version=1,
                request=intake_request(),
                exif_decisions=(ExifDecision.STRIP,) * 3,
            )

    assert harness.service._retained_lock_count() == 0


def test_corrupt_persisted_intake_cannot_reach_gates_or_portal(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    changed = harness.cases.save_intake_summary(
        harness.case_id,
        expected_version=intake.case.version,
        summary={"unexpected": "state"},
    )
    changed = harness.cases.save_active_clarification(
        harness.case_id,
        expected_version=changed.version,
        clarification={
            **intake.clarification.model_dump(mode="json", by_alias=True),
            "expectedVersion": changed.version + 1,
        },
    )

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=changed.version,
            answer="14:30",
        )

    assert captured.value.code == "CASE_STATE_INVALID"
    assert harness.portal.calls == 0


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_missing_or_tampered_media_fails_closed_before_portal(
    tmp_path: Path,
    damage: str,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    assert intake.clarification is not None
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None
    summary = cast(dict[str, Any], intake.case.intake_summary)
    images = cast(list[dict[str, Any]], summary["images"])
    source = cast(dict[str, Any], images[0]["source"])
    source_path = harness.store.root / storage_name / cast(str, source["fileId"])
    if damage == "missing":
        source_path.unlink()
    else:
        source_path.write_bytes(b"altered")

    with pytest.raises(FlowError) as captured:
        harness.service.answer(
            harness.case_id,
            intake.clarification.clarification_id,
            expected_version=intake.case.version,
            answer="14:30",
        )

    assert captured.value.code == "CASE_MEDIA_MISSING"
    assert harness.portal.calls == 0


def test_opaque_media_storage_name_never_crosses_public_or_audit_boundaries(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    intake = begin(harness)
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None

    public_json = intake.model_dump_json(by_alias=True)
    audit_json = "\n".join(
        event.event.model_dump_json(by_alias=True)
        for event in harness.cases.list_audit_events(harness.case_id)
    )
    gate_json = "\n".join(
        gate.decision.model_dump_json(by_alias=True)
        for gate in harness.cases.list_gate_decisions(harness.case_id)
    )

    assert storage_name not in public_json
    assert storage_name not in audit_json
    assert storage_name not in gate_json
    assert "model-" in public_json


def test_parallel_same_version_intakes_leave_no_stuck_mapping_or_orphan(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    start = Barrier(2)
    valid = intake_request()
    invalid = IntakeRequest(
        images=valid.images[:2],
        text=valid.text,
        audio=None,
        consents=valid.consents,
    )

    def run(request: IntakeRequest) -> str:
        start.wait(timeout=5)
        try:
            harness.service.intake(
                harness.case_id,
                expected_version=1,
                request=request,
                exif_decisions=(ExifDecision.STRIP,) * 3,
            )
        except FlowError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(run, (valid, invalid)))

    assert outcomes.count("success") <= 1
    storage_name = harness.repository.get_case_media_handle(harness.case_id)
    case_directories = [
        path for path in harness.store.root.iterdir() if path.name.startswith("case-")
    ]
    if storage_name is None:
        assert case_directories == []
        current = harness.cases.get_case(harness.case_id)
        recovered = harness.service.intake(
            harness.case_id,
            expected_version=current.version,
            request=valid,
            exif_decisions=(ExifDecision.STRIP,) * 3,
        )
        assert recovered.case.state is CaseState.AWAITING_CLARIFICATION
        storage_name = harness.repository.get_case_media_handle(harness.case_id)
    assert storage_name is not None
    remaining_directories = [
        path.name
        for path in harness.store.root.iterdir()
        if path.name.startswith("case-")
    ]
    assert remaining_directories == [storage_name]

    harness.cases.delete_case(harness.case_id)
    assert not [path for path in harness.store.root.iterdir() if path.name.startswith("case-")]
    assert harness.service._retained_lock_count() == 0


def test_created_case_recovers_stale_bound_handle_after_restart(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    stale = harness.store.create_case()
    harness.repository.bind_case_media_handle(
        case_id=harness.case_id,
        storage_name=stale.storage_name,
        created_at=harness.cases.get_case(harness.case_id).created_at,
    )

    response = begin(harness)

    replacement = harness.repository.get_case_media_handle(harness.case_id)
    assert response.case.state is CaseState.AWAITING_CLARIFICATION
    assert replacement is not None and replacement != stale.storage_name
    assert not (harness.store.root / stale.storage_name).exists()
