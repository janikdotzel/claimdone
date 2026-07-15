"""Canonical v1 -> v4 -> v5 -> v9 composition for the INT-002 demo."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import JsonValue

from claimdone_api.authority import AuthorityService
from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.cases.int002_errors import (
    composition_conflict,
    composition_failed,
    fixture_rejected,
    workflow_gate_blocked,
)
from claimdone_api.computer_use.portal import (
    PortalGateway,
    PortalGatewayError,
    RenderedCapture,
    SemanticPortalBrowser,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    AllowedTool,
    CaseState,
    ClaimData,
    ClarificationAnswerRequest,
    ClarificationStatus,
    ClarificationView,
    ClarificationWorkflowEvent,
    EvidenceItem,
    GateId,
    GateReasonCode,
    PlanStep,
    PlanStepWorkflowEvent,
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
    VerificationFieldStatus,
    WorkflowSnapshot,
)
from claimdone_api.demo import (
    INT002_CLARIFICATION_QUESTION,
    INT002_IMAGE_FIXTURES,
    INT002_INCIDENT_TIME,
    INT002_SYNTHETIC_STATEMENT_SHA256,
    INT002_SYNTHETIC_STATEMENT_TEXT,
    ApprovedDemoIntake,
    ConfirmedSyntheticStatement,
    DemoAnalysisInputError,
    DemoAnalysisRequest,
    DemoClarificationResolution,
    analyze_int002_demo,
    reconstruct_int002_clarification,
)
from claimdone_api.media import ExifChoice, ExifDecision, IntakeRequest, PrivacyReview
from claimdone_api.persistence import (
    AnalysisWorkflowCommand,
    CaseRecord,
    IntakeDisclosureCommand,
    PortalRunRecord,
    PortalRunStartCommand,
    PortalWriteFinalizeCommand,
    VerificationAttemptCommand,
)
from claimdone_api.persistence.models import SequencedWorkflowEvent

_RUN_VERSION = 5
_FAULT_FIELD = RequiredClaimField.INCIDENT_TIME
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_request_id() -> str:
    return f"request-{uuid4().hex}"


class Int002WorkflowService:
    """Compose existing authorities without duplicating any deterministic gate."""

    def __init__(
        self,
        cases: CaseService,
        authority: AuthorityService,
        portal: PortalGateway,
        browser_factory: Callable[[], SemanticPortalBrowser],
        *,
        control_token: str,
        now: Callable[[], datetime] = _utc_now,
        request_id_factory: Callable[[], str] = _new_request_id,
    ) -> None:
        if type(cases) is not CaseService or type(authority) is not AuthorityService:
            raise TypeError("INT-002 requires the canonical case and authority services")
        portal_methods = (
            "setup_run",
            "read_session",
            "read_rendered",
            "inject_render_fault",
            "repair_render_fault",
            "release_run",
            "abort_run",
        )
        if any(not callable(getattr(portal, method, None)) for method in portal_methods):
            raise TypeError("INT-002 requires the closed portal gateway")
        if not callable(browser_factory) or not callable(now) or not callable(request_id_factory):
            raise TypeError("INT-002 factories must be callable")
        if (
            type(control_token) is not str
            or not 32 <= len(control_token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in control_token)
        ):
            raise ValueError("Portal control token must be 32-512 visible ASCII characters")
        self._cases = cases
        self._authority = authority
        self._portal = portal
        self._browser_factory = browser_factory
        self._control_token = control_token.encode("ascii")
        self._now = now
        self._request_id_factory = request_id_factory

    def create_case(
        self,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> CaseRecord:
        """Keep the canonical create route usable through this composed service."""

        return self._cases.create_case(metadata)

    def get_workflow_snapshot(self, case_id: str) -> WorkflowSnapshot:
        return self._cases.get_workflow_snapshot(
            case_id,
            request_id=self._request_id_factory(),
        )

    def submit_intake(
        self,
        case_id: str,
        *,
        expected_version: int,
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> WorkflowSnapshot:
        """Commit G0/G1 and analysis, recovering every uncertain intake cursor."""

        current = self._cases.get_case(case_id)
        if type(expected_version) is not int or expected_version != 1:
            raise CaseVersionConflictError(case_id, expected_version, current.version)
        self._validate_fixture_request(request, exif_decisions)
        if current.state is CaseState.AWAITING_CLARIFICATION and current.version == 4:
            self._assert_intake_retry_target(current)
            return self.get_workflow_snapshot(case_id)
        if current.state is CaseState.CREATED and current.version == 1:
            g0_at = self._strictly_after(current.updated_at)
            g1_at = self._strictly_after(g0_at)
            disclosed_at = self._strictly_after(g1_at)
            current = self._cases.commit_intake_disclosure(
                IntakeDisclosureCommand(
                    case_id=case_id,
                    expected_version=current.version,
                    request=request,
                    privacy_review=PrivacyReview(
                        exif_choices=tuple(
                            ExifChoice(
                                input_id=f"image-{index}",
                                decision=decision,
                            )
                            for index, decision in enumerate(exif_decisions, start=1)
                        ),
                        model_copy_approved=request.consents.data_processing_approved,
                        audit_fields=(),
                    ),
                    g0_decided_at=g0_at,
                    g1_decided_at=g1_at,
                    updated_at=disclosed_at,
                )
            )
        if current.state is CaseState.DISCLOSED and current.version == 2:
            current = self._cases.begin_text_analysis(
                case_id,
                expected_version=current.version,
            )
        if current.state is not CaseState.ANALYZING or current.version != 3:
            raise CaseVersionConflictError(case_id, expected_version, current.version)
        intake = self._approved_demo_intake(current)
        clock = self._gate_clock(current.updated_at)
        try:
            result = analyze_int002_demo(
                DemoAnalysisRequest(
                    case_id=case_id,
                    case_version=current.version,
                    intake=intake,
                ),
                clock=clock,
                clarification_id_factory=self._clarification_id,
            )
        except DemoAnalysisInputError as error:
            raise fixture_rejected() from error
        clarification = result.clarification
        persistence = result.initial_persistence
        if clarification is None or persistence is None:
            raise composition_failed()
        committed = self._cases.commit_analysis_workflow(
            AnalysisWorkflowCommand(
                case_id=case_id,
                expected_version=current.version,
                target=CaseState.AWAITING_CLARIFICATION,
                claim_packet=result.packet,
                active_clarification=clarification.view,
                clarification_answer=None,
                approved_evidence=result.packet.evidence,
                g2_attempts=persistence.g2_attempts,
                safety_input=persistence.safety_input,
                gate_decisions=result.new_gate_decisions,
                provider_events=persistence.provider_events,
                plan_steps=self._plan_events(result.packet.plan.steps),
                clarification_events=(
                    self._clarification_event(
                        ClarificationStatus.REQUESTED,
                        clarification.view,
                    ),
                ),
                updated_at=clarification.view.requested_at,
            )
        ).case
        if committed.version != 4 or committed.state is not CaseState.AWAITING_CLARIFICATION:
            raise composition_failed()
        return self.get_workflow_snapshot(case_id)

    def answer_clarification(
        self,
        case_id: str,
        clarification_id: str,
        request: ClarificationAnswerRequest,
    ) -> WorkflowSnapshot:
        """Resolve only the persisted incident-time clarification and commit READY v5."""

        if request.case_id != case_id or request.clarification_id != clarification_id:
            raise CaseSnapshotValidationError(
                "Clarification request identity does not match the selected case"
            )
        current = self._cases.get_case(case_id)
        if current.state is CaseState.READY_TO_FILL and current.version == 5:
            self._validate_answer_retry(current, request)
            return self.get_workflow_snapshot(case_id)
        if current.version != request.expected_version:
            raise CaseVersionConflictError(
                case_id,
                request.expected_version,
                current.version,
            )
        if current.state is not CaseState.AWAITING_CLARIFICATION or current.version != 4:
            raise CaseSnapshotValidationError(
                "INT-002 clarification requires the version 4 active request"
            )
        packet = current.snapshot.claim_packet
        raw_view = current.snapshot.active_clarification
        if packet is None or raw_view is None:
            raise composition_conflict(current_version=current.version)
        try:
            view = ClarificationView.model_validate(raw_view)
            continuation = reconstruct_int002_clarification(
                view=view,
                prior_packet=packet,
            )
            result = analyze_int002_demo(
                DemoAnalysisRequest(
                    case_id=case_id,
                    case_version=current.version,
                    intake=continuation.intake,
                    clarification_resolution=DemoClarificationResolution(
                        clarification=continuation.clarification,
                        answer=request,
                        prior_packet=continuation.prior_packet,
                    ),
                ),
                clock=self._gate_clock(current.updated_at),
                clarification_id_factory=self._clarification_id,
            )
        except (DemoAnalysisInputError, ValueError) as error:
            raise CaseSnapshotValidationError(
                "The clarification answer is not bound to the active request"
            ) from error
        updated_at = result.new_gate_decisions[-1].decided_at
        committed = self._cases.commit_analysis_workflow(
            AnalysisWorkflowCommand(
                case_id=case_id,
                expected_version=current.version,
                target=CaseState.READY_TO_FILL,
                claim_packet=result.packet,
                active_clarification=None,
                clarification_answer=request,
                approved_evidence=(),
                g2_attempts=(),
                safety_input=None,
                gate_decisions=result.new_gate_decisions,
                provider_events=(),
                plan_steps=self._plan_events(result.packet.plan.steps),
                clarification_events=(
                    self._clarification_event(ClarificationStatus.CONFIRMED, view),
                ),
                updated_at=updated_at,
            )
        ).case
        if committed.version != 5 or committed.state is not CaseState.READY_TO_FILL:
            raise composition_failed()
        return self.get_workflow_snapshot(case_id)

    def run_to_review(
        self,
        case_id: str,
        *,
        expected_version: int,
    ) -> WorkflowSnapshot:
        """Fill Portal A, prove one narrow repair, and stop at REVIEW v9."""

        if type(expected_version) is not int or expected_version != _RUN_VERSION:
            current = self._cases.get_case(case_id)
            raise CaseVersionConflictError(case_id, expected_version, current.version)
        current = self._cases.get_case(case_id)
        run_id = self._run_id(case_id)
        if current.state is CaseState.REVIEW and current.version == 9:
            self._release_portal_run(case_id, run_id)
            snapshot = self.get_workflow_snapshot(case_id)
            self._assert_final_review(snapshot)
            return snapshot
        if (
            current.version not in {5, 6, 7, 8}
            or current.state
            not in {CaseState.READY_TO_FILL, CaseState.FILLING, CaseState.VERIFYING}
        ):
            raise CaseVersionConflictError(case_id, expected_version, current.version)
        packet = current.snapshot.claim_packet
        if packet is None:
            raise composition_conflict(current_version=current.version)
        expected_fields = self._portal_fields(packet.claim)
        control_digest = self._control_digest(case_id, run_id)
        run = self._cases.resolve_portal_run(run_id, control_digest)

        if current.state is CaseState.READY_TO_FILL:
            if run is not None:
                raise composition_conflict(current_version=current.version)
            run = self._start_run(
                current,
                expected_fields=expected_fields,
                run_id=run_id,
                control_digest=control_digest,
            )
            current = self._cases.get_case(case_id)
        if run is None:
            raise composition_conflict(current_version=current.version)
        if run.case_id != case_id or run.portal_variant is not PortalVariant.A:
            raise composition_conflict(current_version=current.version)

        if current.state is CaseState.FILLING:
            current = self._finish_portal_write(
                current,
                run=run,
                expected_fields=expected_fields,
                control_digest=control_digest,
            )
        if current.state is CaseState.VERIFYING and current.version == 7:
            current = self._record_fault_attempt(
                current,
                run=run,
                expected_fields=expected_fields,
                control_digest=control_digest,
            )
        if current.state is CaseState.VERIFYING and current.version == 8:
            current = self._repair_and_verify(
                current,
                run=run,
                expected_fields=expected_fields,
                control_digest=control_digest,
            )
        if current.state is not CaseState.REVIEW or current.version != 9:
            raise composition_failed()
        self._release_portal_run(case_id, run_id)
        snapshot = self.get_workflow_snapshot(case_id)
        self._assert_final_review(snapshot)
        return snapshot

    def delete_case(self, case_id: str) -> None:
        self._cases.delete_case(case_id)

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        return self._cases.list_workflow_events(case_id, after=after, limit=limit)

    def _start_run(
        self,
        current: CaseRecord,
        *,
        expected_fields: PortalRunExpectedFields,
        run_id: str,
        control_digest: bytes,
    ) -> PortalRunRecord:
        capability = self._authority.issue_agent_capability(
            current.case_id,
            expected_version=current.version,
        )
        setup = self._setup_command(current.case_id, run_id, expected_fields)
        prestage = self._portal.setup_run(setup)
        self._assert_portal_session(
            prestage,
            expected_fields=expected_fields,
            expected_state=PortalState.DRAFT,
            expected_version=1,
            scalars_empty=True,
        )
        packet = current.snapshot.claim_packet
        if packet is None:
            raise composition_conflict(current_version=current.version)
        fill_step = tuple(
            step
            for step in packet.plan.steps
            if step.tool is AllowedTool.FILL_UNTIL_REVIEW
        )
        if len(fill_step) != 1:
            raise composition_conflict(current_version=current.version)
        consumed_at = self._strictly_after(
            max(current.updated_at, capability.issued_at, prestage.updated_at)
        )
        updated_at = self._strictly_after(consumed_at)
        invocation = {
            "contractVersion": CONTRACT_VERSION,
            "invocationId": run_id,
            "sequence": fill_step[0].sequence,
            "tool": AllowedTool.FILL_UNTIL_REVIEW.value,
            "arguments": {},
        }
        try:
            result = self._cases.start_portal_run(
                PortalRunStartCommand(
                    case_id=current.case_id,
                    expected_case_version=current.version,
                    run_id=run_id,
                    capability_digest=capability.digest,
                    control_digest=control_digest,
                    portal_variant=PortalVariant.A,
                    invocation_payload=invocation,
                    current_url=self._case_url(current.case_id),
                    action="click",
                    proposed_action_number=1,
                    elapsed_seconds=0.0,
                    prestage_session=prestage,
                    consumed_at=consumed_at,
                    updated_at=updated_at,
                )
            )
        except Exception:
            recovered = self._cases.resolve_portal_run(run_id, control_digest)
            if recovered is not None:
                return recovered
            # Leave the exact prestage active. Portal setup is idempotent for this
            # unchanged command, so a retry can issue a fresh capability and resume.
            raise
        if result.case.state is not CaseState.FILLING or not result.run.g6_decision.passed:
            with suppress(PortalGatewayError):
                self._portal.abort_run(self._release_command(current.case_id, run_id))
            raise workflow_gate_blocked(result.run.g6_decision, status_code=409)
        return result.run

    def _finish_portal_write(
        self,
        current: CaseRecord,
        *,
        run: PortalRunRecord,
        expected_fields: PortalRunExpectedFields,
        control_digest: bytes,
    ) -> CaseRecord:
        preflight_at = self._strictly_after(current.updated_at)
        decision = self._cases.preflight_portal_write(
            case_id=current.case_id,
            expected_case_version=current.version,
            run_id=run.run_id,
            control_digest=control_digest,
            fields_payload=expected_fields.model_dump(mode="json", by_alias=True),
            decided_at=preflight_at,
        )
        if not decision.passed:
            raise workflow_gate_blocked(decision)
        session = self._ensure_review_session(
            current.case_id,
            run.run_id,
            expected_fields,
        )
        rendered = self._portal.read_rendered(current.case_id, PortalVariant.A)
        if rendered.version != 3 or rendered.fields != session.fields:
            raise composition_failed()
        completed_at = self._strictly_after(
            max(current.updated_at, session.updated_at, rendered.rendered_at)
        )
        result = self._cases.finalize_portal_write(
            PortalWriteFinalizeCommand(
                case_id=current.case_id,
                expected_case_version=current.version,
                run_id=run.run_id,
                control_digest=control_digest,
                fields_payload=expected_fields.model_dump(mode="json", by_alias=True),
                duration_ms=0,
                completed_at=completed_at,
                portal_session=session,
                rendered_snapshot=rendered,
            )
        )
        if result.case.state is not CaseState.VERIFYING or result.case.version != 7:
            raise composition_failed()
        return result.case

    def _record_fault_attempt(
        self,
        current: CaseRecord,
        *,
        run: PortalRunRecord,
        expected_fields: PortalRunExpectedFields,
        control_digest: bytes,
    ) -> CaseRecord:
        attempt_id = self._attempt_id(run.run_id, 1)
        recovered = self._cases.resolve_verification_attempt(
            case_id=current.case_id,
            run_id=run.run_id,
            control_digest=control_digest,
            attempt_id=attempt_id,
        )
        if recovered is not None:
            return recovered.case
        session = self._ensure_review_session(
            current.case_id,
            run.run_id,
            expected_fields,
        )
        rendered = self._portal.read_rendered(current.case_id, PortalVariant.A)
        if rendered.fields == session.fields:
            self._portal.inject_render_fault(
                PortalRunRenderFaultInjection.model_validate(
                    {
                        "contractVersion": CONTRACT_VERSION,
                        "runId": run.run_id,
                        "caseId": current.case_id,
                        "variant": PortalVariant.A.value,
                        "expectedVersion": 3,
                        "field": _FAULT_FIELD.value,
                    }
                )
            )
        capture = self._capture(current.case_id)
        self._assert_single_fault(capture, expected_fields)
        verified_at = self._strictly_after(max(current.updated_at, capture.received_at))
        decided_at = self._strictly_after(verified_at)
        result = self._cases.record_verification_attempt(
            VerificationAttemptCommand(
                case_id=current.case_id,
                expected_case_version=current.version,
                run_id=run.run_id,
                control_digest=control_digest,
                attempt_id=attempt_id,
                rendered_snapshot=capture.snapshot,
                screenshot_sha256=capture.screenshot_sha256,
                snapshot_requested_at=capture.requested_at,
                snapshot_received_at=capture.received_at,
                model_reported_mismatch=False,
                verified_at=verified_at,
                decided_at=decided_at,
                final=False,
            )
        )
        repair = result.attempt.repair
        if (
            result.case.version != 8
            or repair is None
            or repair.field is not _FAULT_FIELD
            or repair.from_portal_version != 3
            or repair.to_portal_version != 4
        ):
            raise composition_failed()
        return result.case

    def _repair_and_verify(
        self,
        current: CaseRecord,
        *,
        run: PortalRunRecord,
        expected_fields: PortalRunExpectedFields,
        control_digest: bytes,
    ) -> CaseRecord:
        first_id = self._attempt_id(run.run_id, 1)
        first = self._cases.resolve_verification_attempt(
            case_id=current.case_id,
            run_id=run.run_id,
            control_digest=control_digest,
            attempt_id=first_id,
        )
        if first is None or first.attempt.repair is None:
            raise composition_conflict(current_version=current.version)
        final_id = self._attempt_id(run.run_id, 2)
        recovered = self._cases.resolve_verification_attempt(
            case_id=current.case_id,
            run_id=run.run_id,
            control_digest=control_digest,
            attempt_id=final_id,
        )
        if recovered is not None:
            return recovered.case
        session = self._ensure_review_session(
            current.case_id,
            run.run_id,
            expected_fields,
            allow_repaired=True,
        )
        if session.version == 3:
            rendered = self._portal.read_rendered(current.case_id, PortalVariant.A)
            if rendered.fields == session.fields:
                self._portal.inject_render_fault(
                    PortalRunRenderFaultInjection.model_validate(
                        {
                            "contractVersion": CONTRACT_VERSION,
                            "runId": run.run_id,
                            "caseId": current.case_id,
                            "variant": PortalVariant.A.value,
                            "expectedVersion": 3,
                            "field": first.attempt.repair.field.value,
                        }
                    )
                )
            else:
                self._assert_single_fault_snapshot(rendered, expected_fields)
            repaired = self._portal.repair_render_fault(
                PortalRunRenderFaultRepair.model_validate(
                    {
                        "contractVersion": CONTRACT_VERSION,
                        "runId": run.run_id,
                        "caseId": current.case_id,
                        "variant": PortalVariant.A.value,
                        "expectedVersion": 3,
                        "field": first.attempt.repair.field.value,
                    }
                )
            )
        else:
            repaired = session
        self._assert_portal_session(
            repaired,
            expected_fields=expected_fields,
            expected_state=PortalState.REVIEW,
            expected_version=4,
        )
        capture = self._capture(current.case_id)
        if capture.snapshot.version != 4 or capture.snapshot.fields != repaired.fields:
            raise composition_failed()
        verified_at = self._strictly_after(max(current.updated_at, capture.received_at))
        decided_at = self._strictly_after(verified_at)
        result = self._cases.record_verification_attempt(
            VerificationAttemptCommand(
                case_id=current.case_id,
                expected_case_version=current.version,
                run_id=run.run_id,
                control_digest=control_digest,
                attempt_id=final_id,
                rendered_snapshot=capture.snapshot,
                screenshot_sha256=capture.screenshot_sha256,
                snapshot_requested_at=capture.requested_at,
                snapshot_received_at=capture.received_at,
                model_reported_mismatch=False,
                verified_at=verified_at,
                decided_at=decided_at,
                final=True,
                repaired_session=repaired,
            )
        )
        if result.case.version != 9 or result.case.state is not CaseState.REVIEW:
            raise composition_failed()
        return result.case

    def _ensure_review_session(
        self,
        case_id: str,
        run_id: str,
        expected_fields: PortalRunExpectedFields,
        *,
        allow_repaired: bool = False,
    ) -> PortalSessionView:
        try:
            session = self._portal.read_session(case_id, PortalVariant.A)
        except PortalGatewayError as error:
            if error.status_code != 404:
                raise
            session = self._portal.setup_run(
                self._setup_command(case_id, run_id, expected_fields)
            )
            self._assert_portal_session(
                session,
                expected_fields=expected_fields,
                expected_state=PortalState.DRAFT,
                expected_version=1,
                scalars_empty=True,
            )
        if allow_repaired and session.version == 4:
            self._assert_portal_session(
                session,
                expected_fields=expected_fields,
                expected_state=PortalState.REVIEW,
                expected_version=4,
            )
            return session
        browser: SemanticPortalBrowser | None = None
        try:
            if session.version in {1, 2} and session.state is PortalState.DRAFT:
                browser = self._browser_factory()
                browser.open_case(case_id)
                if session.version == 1:
                    browser.fill_expected_fields(expected_fields)
                    browser.save_draft()
                    session = self._portal.read_session(case_id, PortalVariant.A)
                    self._assert_portal_session(
                        session,
                        expected_fields=expected_fields,
                        expected_state=PortalState.DRAFT,
                        expected_version=2,
                    )
                browser.continue_to_review()
                session = self._portal.read_session(case_id, PortalVariant.A)
        finally:
            if browser is not None:
                browser.close()
        self._assert_portal_session(
            session,
            expected_fields=expected_fields,
            expected_state=PortalState.REVIEW,
            expected_version=3,
        )
        return session

    def _capture(self, case_id: str) -> RenderedCapture:
        browser = self._browser_factory()
        try:
            return browser.capture_rendered_values(case_id, PortalVariant.A)
        finally:
            browser.close()

    def _approved_demo_intake(self, disclosed: CaseRecord) -> ApprovedDemoIntake:
        summary = disclosed.snapshot.intake_summary
        if not isinstance(summary, dict):
            raise composition_failed()
        try:
            raw_images_value = summary["images"]
            if not isinstance(raw_images_value, list):
                raise ValueError
            raw_images = raw_images_value
            if len(raw_images) != 3:
                raise ValueError
            parsed_images: list[EvidenceItem] = []
            for raw, fixture in zip(raw_images, INT002_IMAGE_FIXTURES, strict=True):
                if not isinstance(raw, dict):
                    raise ValueError
                source_value = raw.get("source")
                if not isinstance(source_value, dict):
                    raise ValueError
                source = source_value
                parsed_images.append(
                    EvidenceItem.model_validate(
                        {
                            "evidenceId": fixture.semantic_id,
                            "kind": "image",
                            "localRef": source.get("fileId"),
                            "mediaType": source.get("mediaType"),
                            "sha256": source.get("sha256"),
                            "text": None,
                            "modelCopyApproved": True,
                            "transcriptConfirmed": None,
                        }
                    )
                )
            images = tuple(parsed_images)
            statement_value = summary["statement"]
            if not isinstance(statement_value, dict):
                raise ValueError
            statement = statement_value
            statement_evidence = EvidenceItem.model_validate(
                {
                    "evidenceId": "int002-statement",
                    "kind": "user_statement",
                    "localRef": statement["fileId"],
                    "mediaType": statement["mediaType"],
                    "sha256": statement["sha256"],
                    "text": INT002_SYNTHETIC_STATEMENT_TEXT,
                    "modelCopyApproved": True,
                    "transcriptConfirmed": None,
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            raise composition_failed() from error
        if statement_evidence.sha256 != INT002_SYNTHETIC_STATEMENT_SHA256:
            raise fixture_rejected()
        gates = self._cases.list_gate_decisions(disclosed.case_id, limit=2)
        if len(gates) != 2:
            raise composition_failed()
        return ApprovedDemoIntake(
            images=images,
            statement=ConfirmedSyntheticStatement(
                evidence=statement_evidence,
                confirmed=True,
            ),
            g0_decision=gates[0].decision,
            g1_decision=gates[1].decision,
        )

    @staticmethod
    def _validate_fixture_request(
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> None:
        if (
            type(request) is not IntakeRequest
            or request.text != INT002_SYNTHETIC_STATEMENT_TEXT
            or request.audio is not None
            or request.consents.sandbox_acknowledged is not True
            or request.consents.image_rights_confirmed is not True
            or request.consents.data_processing_approved is not True
            or exif_decisions != (ExifDecision.RETAIN,) * 3
            or len(request.images) != 3
        ):
            raise fixture_rejected()
        for upload, fixture in zip(request.images, INT002_IMAGE_FIXTURES, strict=True):
            if (
                upload.media_type != "image/png"
                or hashlib.sha256(upload.content).hexdigest() != fixture.sha256
            ):
                raise fixture_rejected()

    def _assert_intake_retry_target(self, current: CaseRecord) -> None:
        packet = current.snapshot.claim_packet
        raw_view = current.snapshot.active_clarification
        self._approved_demo_intake(current)
        try:
            view = ClarificationView.model_validate(raw_view)
        except (TypeError, ValueError) as error:
            raise composition_conflict(current_version=current.version) from error
        decisions = () if packet is None else packet.gate_decisions
        if (
            packet is None
            or packet.state is not CaseState.AWAITING_CLARIFICATION
            or packet.claim.incident_time is not None
            or tuple(decision.gate_id for decision in decisions)
            != tuple(GateId(f"G{index}") for index in range(6))
            or any(not decision.passed for decision in decisions[:5])
            or decisions[5].passed
            or decisions[5].reason_codes
            != (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)
            or view.case_id != current.case_id
            or view.field is not RequiredClaimField.INCIDENT_TIME
            or view.round != 1
            or view.question != INT002_CLARIFICATION_QUESTION
            or view.expected_version != current.version
        ):
            raise composition_conflict(current_version=current.version)

    def _require_version(self, case_id: str, expected_version: int) -> CaseRecord:
        current = self._cases.get_case(case_id)
        if (
            type(expected_version) is not int
            or expected_version < 1
            or expected_version > _SQLITE_MAX_INTEGER
            or current.version != expected_version
        ):
            raise CaseVersionConflictError(case_id, expected_version, current.version)
        return current

    def _gate_clock(self, floor: datetime) -> Callable[[GateId], datetime]:
        cursor = floor

        def clock(_gate_id: GateId) -> datetime:
            nonlocal cursor
            cursor = self._strictly_after(cursor)
            return cursor

        return clock

    def _strictly_after(self, floor: datetime) -> datetime:
        value = self._now()
        if type(value) is not datetime or value.utcoffset() is None:
            raise ValueError("INT-002 clock must return aware datetimes")
        return value if value > floor else floor + timedelta(microseconds=1)

    @staticmethod
    def _clarification_id(seed: str) -> str:
        if type(seed) is not str or len(seed) < 32:
            raise ValueError("Clarification seed is invalid")
        return f"clarification-{seed[:32]}"

    @staticmethod
    def _plan_events(steps: tuple[PlanStep, ...]) -> tuple[PlanStepWorkflowEvent, ...]:
        return tuple(
            PlanStepWorkflowEvent.model_validate(
                {
                    "kind": "plan_step",
                    "sequence": step.sequence,
                    "tool": step.tool,
                }
            )
            for step in steps
        )

    @staticmethod
    def _clarification_event(
        status: ClarificationStatus,
        view: ClarificationView,
    ) -> ClarificationWorkflowEvent:
        return ClarificationWorkflowEvent.model_validate(
            {
                "kind": "clarification",
                "round": view.round,
                "field": view.field,
                "status": status,
            }
        )

    @staticmethod
    def _validate_answer_retry(current: CaseRecord, request: ClarificationAnswerRequest) -> None:
        packet = current.snapshot.claim_packet
        digest = hashlib.sha256(request.answer.encode()).hexdigest()
        identity = hashlib.sha256(
            (
                "claimdone-clarification-v1\0"
                f"{request.case_id}\0{request.clarification_id}\0{request.round}\0{digest}"
            ).encode()
        ).hexdigest()[:32]
        if (
            request.expected_version != 4
            or request.field is not _FAULT_FIELD
            or request.round != 1
            or request.answer != INT002_INCIDENT_TIME
            or packet is None
            or not any(
                item.evidence_id == f"clarification-{identity}"
                for item in packet.evidence
            )
        ):
            raise CaseSnapshotValidationError(
                "Clarification retry does not match the committed answer"
            )

    @staticmethod
    def _portal_fields(claim: ClaimData) -> PortalRunExpectedFields:
        try:
            if claim.incident_date is None or claim.incident_time is None:
                raise ValueError
            return PortalRunExpectedFields.model_validate(
                {
                    "incidentDate": claim.incident_date.isoformat(),
                    "incidentTime": claim.incident_time.isoformat(),
                    "location": claim.location,
                    "claimantName": claim.claimant_name,
                    "policyReference": claim.policy_reference,
                    "vehicleRegistration": claim.vehicle_registration,
                    "counterpartyKnown": claim.counterparty_known.value,
                    "narrative": claim.narrative,
                    "attachments": claim.attachments,
                }
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise composition_failed() from error

    @staticmethod
    def _run_id(case_id: str) -> str:
        digest = hashlib.sha256(
            b"claimdone-int002-run-v1\0" + case_id.encode() + b"\0" + str(_RUN_VERSION).encode()
        ).hexdigest()
        return f"run-{digest[:32]}"

    def _control_digest(self, case_id: str, run_id: str) -> bytes:
        return hmac.new(
            self._control_token,
            (
                "claimdone-int002-control-v1\0"
                f"{case_id}\0{_RUN_VERSION}\0{run_id}"
            ).encode(),
            hashlib.sha256,
        ).digest()

    @staticmethod
    def _attempt_id(run_id: str, number: int) -> str:
        digest = hashlib.sha256(
            f"claimdone-int002-attempt-v1\0{run_id}\0{number}".encode()
        ).hexdigest()
        return f"attempt-{digest[:32]}"

    @staticmethod
    def _setup_command(
        case_id: str,
        run_id: str,
        expected_fields: PortalRunExpectedFields,
    ) -> PortalRunSetup:
        return PortalRunSetup.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "runId": run_id,
                "caseId": case_id,
                "variant": PortalVariant.A.value,
                "expectedFields": expected_fields,
            }
        )

    @staticmethod
    def _case_url(case_id: str) -> str:
        return f"http://127.0.0.1:3000/sandbox/A/cases/{case_id}"

    @staticmethod
    def _release_command(case_id: str, run_id: str) -> PortalRunRelease:
        return PortalRunRelease.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "runId": run_id,
                "caseId": case_id,
                "variant": PortalVariant.A.value,
            }
        )

    def _release_portal_run(self, case_id: str, run_id: str) -> None:
        command = self._release_command(case_id, run_id)
        last_error: PortalGatewayError | None = None
        for _attempt in range(2):
            try:
                self._portal.release_run(command)
                return
            except PortalGatewayError as error:
                last_error = error
        if last_error is None:  # pragma: no cover - the bounded loop always runs
            raise composition_failed()
        if last_error.status_code in {404, 409}:
            try:
                self._portal.read_session(case_id, PortalVariant.A)
            except PortalGatewayError as read_error:
                if read_error.status_code == 404:
                    return
        raise last_error

    @staticmethod
    def _assert_portal_session(
        session: PortalSessionView,
        *,
        expected_fields: PortalRunExpectedFields,
        expected_state: PortalState,
        expected_version: int,
        scalars_empty: bool = False,
    ) -> None:
        if (
            session.variant is not PortalVariant.A
            or session.state is not expected_state
            or session.version != expected_version
            or session.fields.attachments != expected_fields.attachments
        ):
            raise composition_failed()
        values = session.fields.model_dump(mode="json", by_alias=False)
        expected = expected_fields.model_dump(mode="json", by_alias=False)
        if scalars_empty:
            if any(value != "" for key, value in values.items() if key != "attachments"):
                raise composition_failed()
        elif values != expected:
            raise composition_failed()

    @staticmethod
    def _assert_single_fault(
        capture: RenderedCapture,
        expected_fields: PortalRunExpectedFields,
    ) -> None:
        Int002WorkflowService._assert_single_fault_snapshot(
            capture.snapshot,
            expected_fields,
        )

    @staticmethod
    def _assert_single_fault_snapshot(
        snapshot: RenderedPortalSnapshot,
        expected_fields: PortalRunExpectedFields,
    ) -> None:
        actual = snapshot.fields.model_dump(mode="json", by_alias=False)
        expected = expected_fields.model_dump(mode="json", by_alias=False)
        mismatches = tuple(key for key, value in actual.items() if value != expected[key])
        if snapshot.version != 3 or mismatches != (_FAULT_FIELD.value,):
            raise composition_failed()

    @staticmethod
    def _assert_final_review(snapshot: WorkflowSnapshot) -> None:
        packet = snapshot.claim_packet
        series = snapshot.verification_attempts
        portal = snapshot.portal_session
        if (
            snapshot.case.state is not CaseState.REVIEW
            or snapshot.case.version != 9
            or snapshot.receipt is not None
            or packet is None
            or portal is None
            or series is None
            or portal.variant is not PortalVariant.A
            or portal.version != 4
            or tuple(decision.gate_id for decision in packet.gate_decisions)
            != tuple(GateId(f"G{index}") for index in range(9))
            or any(
                not decision.passed
                or not decision.deterministic_passed
                or decision.model_blocked
                or decision.reason_codes
                for decision in packet.gate_decisions
            )
            or len(series.attempts) != 2
        ):
            raise composition_failed()
        first, second = series.attempts
        non_matches = tuple(
            result
            for result in first.report.field_results
            if result.status is not VerificationFieldStatus.MATCH
        )
        if (
            first.final
            or first.gate_decision is not None
            or first.repair is None
            or len(non_matches) != 1
            or non_matches[0].field is not first.repair.field
            or not second.final
            or second.gate_decision is None
            or not second.gate_decision.passed
            or second.repaired_from_attempt_id != first.attempt_id
            or second.portal_version != first.repair.to_portal_version
        ):
            raise composition_failed()
