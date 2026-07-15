"""Focused integration tests for the canonical INT-002 backend composition."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest

from claimdone_api.authority import AuthorityService
from claimdone_api.cases import CaseService
from claimdone_api.cases.int002_errors import Int002HttpError
from claimdone_api.computer_use.portal import PortalGatewayError, RenderedCapture
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ClarificationAnswerRequest,
    PortalDraftFields,
    PortalRunExpectedFields,
    PortalRunRelease,
    PortalRunRenderFaultInjection,
    PortalRunRenderFaultRepair,
    PortalRunSetup,
    PortalSessionView,
    PortalState,
    PortalVariant,
    RenderedPortalSnapshot,
    RequiredClaimField,
)
from claimdone_api.demo import INT002_INCIDENT_TIME, INT002_SYNTHETIC_STATEMENT_TEXT
from claimdone_api.int002.service import Int002WorkflowService
from claimdone_api.media import ExifDecision, ImageUpload, IntakeConsents, IntakeRequest
from claimdone_api.persistence import (
    PortalRunStartCommand,
    PortalWriteFinalizeCommand,
    SqliteCaseRepository,
    VerificationAttemptCommand,
    VerificationAttemptResult,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.generate_int002_fixtures import (  # noqa: E402
    build_png,
    load_manifest,
    manifest_path,
)

BASE_TIME = datetime(2026, 7, 15, 12, tzinfo=UTC)
CASE_ID = "case-int002-composition-001"
CONTROL_TOKEN = "control-token-for-int002-tests-0001"


class StepClock:
    def __init__(self) -> None:
        self._value = BASE_TIME

    def __call__(self) -> datetime:
        self._value += timedelta(milliseconds=1)
        return self._value


class RequestIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"request-int002-{self._value}"


class SecretFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._value:043d}"


class FakePortalGateway:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self.command: PortalRunSetup | None = None
        self.session: PortalSessionView | None = None
        self.fault_active = False
        self.released = False
        self.release_failures_remaining = 0

    def setup_run(self, command: PortalRunSetup) -> PortalSessionView:
        if self.command is not None:
            assert command == self.command
            assert self.session is not None
            assert self.session.version == 1
            assert self.session.state is PortalState.DRAFT
            return self.session
        self.command = command
        self.session = self._view(
            version=1,
            state=PortalState.DRAFT,
            fields=PortalDraftFields.model_validate(
                {
                    "incidentDate": "",
                    "incidentTime": "",
                    "location": "",
                    "claimantName": "",
                    "policyReference": "",
                    "vehicleRegistration": "",
                    "counterpartyKnown": "",
                    "narrative": "",
                    "attachments": command.expected_fields.attachments,
                }
            ),
        )
        return self.session

    def read_session(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> PortalSessionView:
        assert case_id == CASE_ID
        assert variant is PortalVariant.A
        if self.session is None:
            raise PortalGatewayError("read-session", 404)
        return self.session

    def read_rendered(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> RenderedPortalSnapshot:
        session = self.read_session(case_id, variant)
        assert session.state is PortalState.REVIEW
        fields = session.fields
        if self.fault_active:
            payload = fields.model_dump(mode="json", by_alias=True)
            payload["incidentTime"] = "00:00:00"
            fields = PortalDraftFields.model_validate(payload)
        return RenderedPortalSnapshot.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": case_id,
                "variant": variant.value,
                "state": PortalState.REVIEW.value,
                "version": session.version,
                "fields": fields,
                "renderedAt": self._clock(),
            }
        )

    def inject_render_fault(self, command: PortalRunRenderFaultInjection) -> None:
        assert command.case_id == CASE_ID
        assert command.expected_version == 3
        assert command.field == "incident_time"
        assert not self.fault_active
        self.fault_active = True

    def repair_render_fault(
        self,
        command: PortalRunRenderFaultRepair,
    ) -> PortalSessionView:
        assert command.case_id == CASE_ID
        assert command.expected_version == 3
        assert command.field == "incident_time"
        assert self.fault_active
        assert self.session is not None
        self.fault_active = False
        self.session = self._view(
            version=4,
            state=PortalState.REVIEW,
            fields=self.session.fields,
        )
        return self.session

    def release_run(self, command: PortalRunRelease) -> None:
        assert command.case_id == CASE_ID
        assert self.session is not None and self.session.version == 4
        assert not self.fault_active
        if self.release_failures_remaining:
            self.release_failures_remaining -= 1
            raise PortalGatewayError("release", 503)
        self.released = True

    def abort_run(self, _command: PortalRunRelease) -> None:
        self.command = None
        self.session = None

    def close(self) -> None:
        return None

    def restart(self) -> None:
        self.command = None
        self.session = None
        self.fault_active = False
        self.released = False

    def save(self, fields: PortalRunExpectedFields) -> None:
        assert self.session is not None and self.session.version == 1
        self.session = self._view(
            version=2,
            state=PortalState.DRAFT,
            fields=PortalDraftFields.model_validate(
                fields.model_dump(mode="json", by_alias=True)
            ),
        )

    def review(self) -> None:
        assert self.session is not None and self.session.version == 2
        self.session = self._view(
            version=3,
            state=PortalState.REVIEW,
            fields=self.session.fields,
        )

    def _view(
        self,
        *,
        version: int,
        state: PortalState,
        fields: PortalDraftFields,
    ) -> PortalSessionView:
        return PortalSessionView.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": CASE_ID,
                "variant": PortalVariant.A.value,
                "state": state.value,
                "version": version,
                "fields": fields,
                "updatedAt": self._clock(),
                "auditCount": version,
            }
        )


class FakeSemanticBrowser:
    def __init__(
        self,
        portal: FakePortalGateway,
        clock: Callable[[], datetime],
    ) -> None:
        self._portal = portal
        self._clock = clock
        self._fields: PortalRunExpectedFields | None = None
        self._opened = False

    def open_case(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        assert case_id == CASE_ID
        assert variant is PortalVariant.A
        assert timeout_seconds > 0
        self._opened = True

    def fill_expected_fields(
        self,
        fields: PortalRunExpectedFields,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        assert self._opened and timeout_seconds > 0
        self._fields = fields

    def save_draft(self, *, timeout_seconds: float = 5.0) -> None:
        assert self._opened and timeout_seconds > 0 and self._fields is not None
        self._portal.save(self._fields)

    def continue_to_review(self, *, timeout_seconds: float = 5.0) -> None:
        assert self._opened and timeout_seconds > 0
        self._portal.review()

    def capture_rendered_values(
        self,
        case_id: str | None = None,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = 4.5,
    ) -> RenderedCapture:
        assert case_id == CASE_ID
        assert variant is PortalVariant.A
        assert timeout_seconds > 0
        requested_at = self._clock()
        snapshot = self._portal.read_rendered(CASE_ID, PortalVariant.A)
        screenshot_sha256 = hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
        return RenderedCapture(
            snapshot=snapshot,
            screenshot_sha256=screenshot_sha256,
            requested_at=requested_at,
            received_at=self._clock(),
        )

    def close(self) -> None:
        self._opened = False


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[SqliteCaseRepository]:
    value = SqliteCaseRepository(tmp_path / "cases.db", media_root=tmp_path / "media")
    try:
        yield value
    finally:
        value.media_store.close()


@pytest.fixture
def composed(
    repository: SqliteCaseRepository,
) -> tuple[Int002WorkflowService, CaseService, FakePortalGateway]:
    clock = StepClock()
    cases = CaseService(
        repository,
        now=clock,
        case_id_factory=lambda: CASE_ID,
    )
    authority = AuthorityService(
        repository,
        now=clock,
        secret_factory=SecretFactory(),
    )
    portal = FakePortalGateway(clock)
    service = Int002WorkflowService(
        cases,
        authority,
        portal,
        lambda: FakeSemanticBrowser(portal, clock),
        control_token=CONTROL_TOKEN,
        now=clock,
        request_id_factory=RequestIdFactory(),
    )
    return service, cases, portal


def _intake() -> IntakeRequest:
    manifest = load_manifest(manifest_path())
    return IntakeRequest(
        images=tuple(
            ImageUpload(content=build_png(image), media_type="image/png")
            for image in manifest.images
        ),
        text=INT002_SYNTHETIC_STATEMENT_TEXT,
        audio=None,
        consents=IntakeConsents(
            sandbox_acknowledged=True,
            image_rights_confirmed=True,
            data_processing_approved=True,
        ),
    )


def _advance_to_ready(
    service: Int002WorkflowService,
    cases: CaseService,
) -> None:
    created = cases.create_case()
    awaiting = service.submit_intake(
        created.case_id,
        expected_version=1,
        request=_intake(),
        exif_decisions=(ExifDecision.RETAIN,) * 3,
    )
    clarification = awaiting.clarification
    assert clarification is not None
    ready = service.answer_clarification(
        CASE_ID,
        clarification.clarification_id,
        ClarificationAnswerRequest.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": CASE_ID,
                "clarificationId": clarification.clarification_id,
                "field": clarification.field.value,
                "round": clarification.round,
                "expectedVersion": clarification.expected_version,
                "answer": INT002_INCIDENT_TIME,
            }
        ),
    )
    assert ready.case.version == 5


def test_full_backend_composition_stops_at_verified_review_v9(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
) -> None:
    service, cases, portal = composed
    created = cases.create_case()

    awaiting = service.submit_intake(
        created.case_id,
        expected_version=created.version,
        request=_intake(),
        exif_decisions=(ExifDecision.RETAIN,) * 3,
    )
    assert awaiting.case.version == 4
    assert awaiting.case.state == "awaiting_clarification"
    assert awaiting.clarification is not None
    clarification = awaiting.clarification

    ready = service.answer_clarification(
        CASE_ID,
        clarification.clarification_id,
        ClarificationAnswerRequest.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": CASE_ID,
                "clarificationId": clarification.clarification_id,
                "field": clarification.field.value,
                "round": clarification.round,
                "expectedVersion": clarification.expected_version,
                "answer": INT002_INCIDENT_TIME,
            }
        ),
    )
    assert ready.case.version == 5
    assert ready.case.state == "ready_to_fill"

    review = service.run_to_review(CASE_ID, expected_version=5)

    assert review.case.version == 9
    assert review.case.state == "review"
    assert review.receipt is None
    assert review.claim_packet is not None
    assert tuple(gate.gate_id.value for gate in review.claim_packet.gate_decisions) == tuple(
        f"G{index}" for index in range(9)
    )
    assert all(gate.passed for gate in review.claim_packet.gate_decisions)
    assert review.verification_attempts is not None
    assert len(review.verification_attempts.attempts) == 2
    first, second = review.verification_attempts.attempts
    assert not first.final and first.repair is not None
    assert first.repair.field is RequiredClaimField.INCIDENT_TIME
    assert second.final and second.gate_decision is not None
    assert second.gate_decision.passed
    assert portal.released

    retried = service.run_to_review(CASE_ID, expected_version=5)
    assert retried.case.version == 9
    assert retried.verification_attempts == review.verification_attempts


def test_fixture_mismatch_is_rejected_before_case_mutation(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
) -> None:
    service, cases, portal = composed
    created = cases.create_case()
    intake = _intake()
    invalid = IntakeRequest(
        images=intake.images,
        text=f"{INT002_SYNTHETIC_STATEMENT_TEXT} changed",
        audio=None,
        consents=intake.consents,
    )

    with pytest.raises(Int002HttpError) as captured:
        service.submit_intake(
            CASE_ID,
            expected_version=1,
            request=invalid,
            exif_decisions=(ExifDecision.RETAIN,) * 3,
        )

    assert captured.value.code == "INT002_FIXTURE_REJECTED"
    assert cases.get_case(CASE_ID) == created
    assert portal.command is None


def test_intake_recovers_v2_v3_and_lost_v4_response_without_duplicate_gates(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, cases, _portal = composed
    cases.create_case()
    original_begin = cases.begin_text_analysis
    original_commit = cases.commit_analysis_workflow
    original_snapshot = service.get_workflow_snapshot

    def lose_before_v3(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("simulated v2 response loss")

    monkeypatch.setattr(cases, "begin_text_analysis", lose_before_v3)
    with pytest.raises(RuntimeError, match="simulated v2"):
        service.submit_intake(
            CASE_ID,
            expected_version=1,
            request=_intake(),
            exif_decisions=(ExifDecision.RETAIN,) * 3,
        )
    assert cases.get_case(CASE_ID).version == 2

    def lose_before_v4(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("simulated v3 response loss")

    monkeypatch.setattr(cases, "begin_text_analysis", original_begin)
    monkeypatch.setattr(cases, "commit_analysis_workflow", lose_before_v4)
    with pytest.raises(RuntimeError, match="simulated v3"):
        service.submit_intake(
            CASE_ID,
            expected_version=1,
            request=_intake(),
            exif_decisions=(ExifDecision.RETAIN,) * 3,
        )
    assert cases.get_case(CASE_ID).version == 3

    def lose_v4_response(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("simulated v4 response loss")

    monkeypatch.setattr(cases, "commit_analysis_workflow", original_commit)
    monkeypatch.setattr(service, "get_workflow_snapshot", lose_v4_response)
    with pytest.raises(RuntimeError, match="simulated v4"):
        service.submit_intake(
            CASE_ID,
            expected_version=1,
            request=_intake(),
            exif_decisions=(ExifDecision.RETAIN,) * 3,
        )
    assert cases.get_case(CASE_ID).version == 4

    monkeypatch.setattr(service, "get_workflow_snapshot", original_snapshot)
    recovered = service.submit_intake(
        CASE_ID,
        expected_version=1,
        request=_intake(),
        exif_decisions=(ExifDecision.RETAIN,) * 3,
    )
    assert recovered.case.version == 4
    assert tuple(
        item.decision.gate_id.value for item in cases.list_gate_decisions(CASE_ID)
    ) == tuple(f"G{index}" for index in range(6))


def test_portal_setup_retry_recovers_before_sqlite_g6_commit(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, cases, portal = composed
    _advance_to_ready(service, cases)
    original_start = cases.start_portal_run

    def lose_g6_commit(_command: PortalRunStartCommand) -> Never:
        raise RuntimeError("simulated pre-G6 failure")

    monkeypatch.setattr(cases, "start_portal_run", lose_g6_commit)
    with pytest.raises(RuntimeError, match="pre-G6"):
        service.run_to_review(CASE_ID, expected_version=5)
    assert cases.get_case(CASE_ID).version == 5
    assert portal.session is not None and portal.session.version == 1

    monkeypatch.setattr(cases, "start_portal_run", original_start)
    review = service.run_to_review(CASE_ID, expected_version=5)
    assert review.case.version == 9
    assert portal.released


@pytest.mark.parametrize("recovery_version", (6, 7, 8))
def test_portal_restart_rehydrates_persisted_run_without_weakening_gates(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
    monkeypatch: pytest.MonkeyPatch,
    recovery_version: int,
) -> None:
    service, cases, portal = composed
    _advance_to_ready(service, cases)
    original_finalize = cases.finalize_portal_write
    original_record = cases.record_verification_attempt

    def lose_v6_finalize(_command: PortalWriteFinalizeCommand) -> Never:
        raise RuntimeError("simulated v6 interruption")

    def lose_v7_attempt(_command: VerificationAttemptCommand) -> Never:
        raise RuntimeError("simulated v7 interruption")

    def lose_v8_final_attempt(
        command: VerificationAttemptCommand,
    ) -> VerificationAttemptResult:
        if command.final:
            raise RuntimeError("simulated v8 interruption")
        return original_record(command)

    if recovery_version == 6:
        monkeypatch.setattr(cases, "finalize_portal_write", lose_v6_finalize)
    elif recovery_version == 7:
        monkeypatch.setattr(cases, "record_verification_attempt", lose_v7_attempt)
    else:
        monkeypatch.setattr(cases, "record_verification_attempt", lose_v8_final_attempt)

    with pytest.raises(RuntimeError, match=f"v{recovery_version} interruption"):
        service.run_to_review(CASE_ID, expected_version=5)
    assert cases.get_case(CASE_ID).version == recovery_version

    portal.restart()
    monkeypatch.setattr(cases, "finalize_portal_write", original_finalize)
    monkeypatch.setattr(cases, "record_verification_attempt", original_record)
    review = service.run_to_review(CASE_ID, expected_version=5)

    assert review.case.version == 9
    assert review.claim_packet is not None
    assert tuple(
        decision.gate_id.value for decision in review.claim_packet.gate_decisions
    ) == tuple(f"G{index}" for index in range(9))
    assert all(decision.deterministic_passed for decision in review.claim_packet.gate_decisions)
    assert portal.released


def test_v9_retry_retries_failed_portal_release(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
) -> None:
    service, cases, portal = composed
    _advance_to_ready(service, cases)
    portal.release_failures_remaining = 1

    review = service.run_to_review(CASE_ID, expected_version=5)
    assert review.case.version == 9
    assert portal.released

    retried = service.run_to_review(CASE_ID, expected_version=5)
    assert retried == review.model_copy(update={"request_id": retried.request_id})
    assert portal.released


def test_persistent_v9_release_failure_is_not_reported_as_success(
    composed: tuple[Int002WorkflowService, CaseService, FakePortalGateway],
) -> None:
    service, cases, portal = composed
    _advance_to_ready(service, cases)
    portal.release_failures_remaining = 2

    with pytest.raises(PortalGatewayError, match="release"):
        service.run_to_review(CASE_ID, expected_version=5)
    assert cases.get_case(CASE_ID).version == 9
    assert not portal.released

    recovered = service.run_to_review(CASE_ID, expected_version=5)
    assert recovered.case.version == 9
    assert portal.released
