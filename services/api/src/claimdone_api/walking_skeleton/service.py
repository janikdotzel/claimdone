"""INT-001 orchestration from local intake to sandbox review."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from pydantic import ValidationError

from claimdone_api.cases import CaseView
from claimdone_api.cases.errors import CaseNotFoundError, CaseVersionConflictError
from claimdone_api.contracts import (
    TERMINAL_CASE_STATES,
    ActorType,
    CaseState,
    ClaimPacket,
    EvidenceKind,
    GateDecision,
    GateReasonCode,
    PortalState,
    RequiredClaimField,
    VerificationState,
)
from claimdone_api.gates import (
    ClarificationQuestion,
    ModelExtraction,
    ModelOutputEnvelope,
    ModelSafetySignal,
    evaluate_g2,
    evaluate_g3,
    evaluate_g4,
    evaluate_g5,
)
from claimdone_api.media import (
    CaseHandle,
    CaseMediaStore,
    ExifChoice,
    ExifDecision,
    IntakeRequest,
    IntakeSession,
    MediaStorageError,
    PreparedMedia,
    PrivacyReview,
    StoredAssetRef,
    UnsafeStoragePath,
    prepare_g1,
    start_intake,
    store_transcript,
    validate_g0,
)
from claimdone_api.persistence import CaseRecord

from .errors import FlowError, PortalUnavailableError
from .intake_state import PersistedIntake, persisted_intake
from .legacy_boundary import LegacyWalkingCaseBoundary, LegacyWalkingRepository
from .mock_extractor import StatementSource, deterministic_extraction
from .models import (
    ClarificationView,
    FlowPhase,
    FlowResponse,
    PortalDraftFields,
    PortalView,
)
from .packet_factory import build_packet
from .portal import PortalPort
from .safety import deterministic_safety_input

_TIME_ANSWER = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_QUESTION = "What time did the staged incident occur? Use HH:MM."
_SYNTHETIC_AUDIO_FIXTURE_SHA256 = (
    "c0ca2899a565ec085e64438fc58496c0debacdcc9e8602f3af275b7b56108820"
)
_SYNTHETIC_AUDIO_TRANSCRIPT = (
    "A staged second vehicle contacted the rear of the demo vehicle in Berlin."
)
_UNSUPPORTED_AUDIO_TRANSCRIPT = (
    "No transcript is available for this non-fixture synthetic audio."
)


@dataclass(frozen=True, slots=True)
class GateRunBlocked(RuntimeError):
    decisions: tuple[GateDecision, ...]

    @property
    def decision(self) -> GateDecision:
        return self.decisions[-1]


@dataclass(slots=True)
class _LockEntry:
    lock: Lock
    users: int = 0


class WalkingSkeletonService:
    """Run the deterministic demo while preserving all gate authority."""

    def __init__(
        self,
        *,
        cases: LegacyWalkingCaseBoundary,
        repository: LegacyWalkingRepository,
        media_store: CaseMediaStore,
        portal: PortalPort,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        request_id_factory: Callable[[], str] = lambda: f"request-{uuid4().hex}",
        clarification_id_factory: Callable[[], str] = (lambda: f"clarification-{uuid4().hex}"),
    ) -> None:
        if repository.media_store is not media_store:
            raise ValueError(
                "Walking service must use the exact repository-owned media store"
            )
        if cases.repository is not repository:
            raise ValueError(
                "Walking service case boundary must use the exact repository"
            )
        self._cases = cases
        self._repository = repository
        self._media_store = media_store
        self._portal = portal
        self._now = now
        self._request_id_factory = request_id_factory
        self._clarification_id_factory = clarification_id_factory
        self._locks_guard = Lock()
        self._case_locks: dict[str, _LockEntry] = {}

    def assert_intake_precondition(self, case_id: str, expected_version: int) -> None:
        current = self._require_version(case_id, expected_version)
        if current.state is not CaseState.CREATED:
            raise FlowError(
                "INTAKE_NOT_AVAILABLE",
                "Intake is only available for a newly created case.",
                409,
                current_version=current.version,
            )

    def reset_demo(self) -> int:
        return self._cases.reset_demo()

    def intake(
        self,
        case_id: str,
        *,
        expected_version: int,
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> FlowResponse:
        with self._case_lock(case_id):
            try:
                return self._intake_locked(
                    case_id,
                    expected_version=expected_version,
                    request=request,
                    exif_decisions=exif_decisions,
                )
            except CaseVersionConflictError as error:
                raise FlowError(
                    "CASE_VERSION_CONFLICT",
                    "The case changed since it was loaded.",
                    409,
                    current_version=error.current_version,
                ) from error

    def _intake_locked(
        self,
        case_id: str,
        *,
        expected_version: int,
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> FlowResponse:
        current = self._require_version(case_id, expected_version)
        if current.state is not CaseState.CREATED:
            raise FlowError(
                "INTAKE_NOT_AVAILABLE",
                "Intake is only available for a newly created case.",
                409,
                current_version=current.version,
            )
        stale_storage_name = self._repository.get_case_media_handle(case_id)
        if stale_storage_name is not None:
            self._media_store.delete_case(CaseHandle(storage_name=stale_storage_name))
            self._repository.unbind_case_media_handle(case_id, stale_storage_name)

        start = start_intake(self._media_store, request)
        if not start.decision.passed or start.session is None:
            failed = self._record_decisions(current, (start.decision,))
            raise self._gate_error(start.decision, failed.version)
        session = start.session
        review = PrivacyReview(
            exif_choices=tuple(
                ExifChoice(input_id=image.input_id, decision=decision)
                for image, decision in zip(
                    session.images,
                    exif_decisions,
                    strict=False,
                )
            ),
            model_copy_approved=request.consents.data_processing_approved,
            audit_fields=(),
        )
        privacy = prepare_g1(self._media_store, session, review)
        if not privacy.decision.passed or privacy.prepared is None:
            self._media_store.delete_case(session.handle)
            failed = self._record_decisions(
                current,
                (start.decision, privacy.decision),
            )
            raise self._gate_error(privacy.decision, failed.version)

        record = current
        try:
            record = self._record_decisions(
                record,
                (start.decision, privacy.decision),
            )
            record = self._cases.transition_case(
                case_id,
                expected_version=record.version,
                target=CaseState.DISCLOSED,
                actor=ActorType.SYSTEM,
            )
            self._repository.bind_case_media_handle(
                case_id=case_id,
                storage_name=session.handle.storage_name,
                created_at=self._aware_now(),
            )
            statement_ref, statement = self._statement_for_initial(
                session,
                privacy.prepared,
            )
            summary = persisted_intake(
                session,
                statement=statement_ref,
                exif_decisions=exif_decisions,
            )
            record = self._cases.save_intake_summary(
                case_id,
                expected_version=record.version,
                summary=summary.model_dump(mode="json", by_alias=True),
            )
            if (
                statement.kind is EvidenceKind.TRANSCRIPT
                and not statement.user_confirmed
            ):
                record = self._cases.transition_case(
                    case_id,
                    expected_version=record.version,
                    target=CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
                    actor=ActorType.SYSTEM,
                )
                raise FlowError(
                    "TRANSCRIPT_CONFIRMATION_REQUIRED",
                    "Confirm the transcript before analysis can begin.",
                    409,
                    current_version=record.version,
                )
            record = self._cases.transition_case(
                case_id,
                expected_version=record.version,
                target=CaseState.ANALYZING,
                actor=ActorType.SYSTEM,
            )
            extraction = deterministic_extraction(
                privacy.prepared,
                statement,
                incident_time=None,
            )
            try:
                decisions = self._run_g2_to_g5(
                    extraction,
                    statement,
                    prefix=(start.decision, privacy.decision),
                    expect_clarification=True,
                )
            except GateRunBlocked as blocked:
                record = self._record_decisions(record, blocked.decisions[2:])
                record, finalization_error = self._terminalize_and_release(
                    record,
                    self._terminal_state_for(blocked.decision),
                    session.handle,
                )
                raise self._gate_error(blocked.decision, record.version) from (
                    finalization_error or blocked
                )
            record = self._record_decisions(record, decisions[2:])
            packet = build_packet(
                case_id=case_id,
                state=CaseState.AWAITING_CLARIFICATION,
                portal_state=PortalState.DRAFT,
                extraction=extraction,
                gate_decisions=decisions,
            )
            record = self._cases.transition_case(
                case_id,
                expected_version=record.version,
                target=CaseState.AWAITING_CLARIFICATION,
                actor=ActorType.SYSTEM,
                claim_packet=packet,
            )
            clarification_id = self._clarification_id_factory()
            expected_answer_version = record.version + 1
            clarification = ClarificationView.model_validate(
                {
                    "clarificationId": clarification_id,
                    "field": RequiredClaimField.INCIDENT_TIME.value,
                    "question": _QUESTION,
                    "expectedVersion": expected_answer_version,
                }
            )
            record = self._cases.save_active_clarification(
                case_id,
                expected_version=record.version,
                clarification=clarification.model_dump(mode="json", by_alias=True),
            )
            return FlowResponse.model_validate(
                {
                    "requestId": self._request_id_factory(),
                    "case": CaseView.from_record(record),
                    "draftRevision": record.version,
                    "gateHistory": decisions,
                    "phase": FlowPhase.AWAITING_CLARIFICATION,
                    "clarification": clarification,
                    "portal": None,
                }
            )
        except FlowError:
            raise
        except Exception as error:
            _record, finalization_error = self._terminalize_and_release(
                record,
                CaseState.FAILED,
                session.handle,
            )
            if finalization_error is not None:
                error.add_note("Terminal state or owned-media cleanup also failed.")
            raise

    def answer(
        self,
        case_id: str,
        clarification_id: str,
        *,
        expected_version: int,
        answer: str,
    ) -> FlowResponse:
        with self._case_lock(case_id):
            try:
                return self._answer_locked(
                    case_id,
                    clarification_id,
                    expected_version=expected_version,
                    answer=answer,
                )
            except CaseVersionConflictError as error:
                raise FlowError(
                    "CASE_VERSION_CONFLICT",
                    "The case changed since it was loaded.",
                    409,
                    current_version=error.current_version,
                ) from error

    def _answer_locked(
        self,
        case_id: str,
        clarification_id: str,
        *,
        expected_version: int,
        answer: str,
    ) -> FlowResponse:
        current = self._require_version(case_id, expected_version)
        active_payload = current.snapshot.active_clarification
        if current.state is not CaseState.AWAITING_CLARIFICATION or active_payload is None:
            raise FlowError(
                "CLARIFICATION_NOT_ACTIVE",
                "This clarification is no longer active.",
                409,
                current_version=current.version,
            )
        try:
            active = ClarificationView.model_validate(active_payload)
        except ValidationError as error:
            raise FlowError(
                "CASE_STATE_INVALID",
                "The persisted clarification state is invalid.",
                409,
                current_version=current.version,
            ) from error
        if (
            active.expected_version != current.version
            or clarification_id != active.clarification_id
        ):
            raise FlowError(
                "CLARIFICATION_NOT_ACTIVE",
                "The clarification identifier is not active for this case.",
                409,
                current_version=current.version,
            )
        if _TIME_ANSWER.fullmatch(answer) is None:
            raise FlowError(
                "CLARIFICATION_ANSWER_INVALID",
                "Answer must be a valid 24-hour time in HH:MM format.",
                422,
                current_version=current.version,
            )
        try:
            persisted = PersistedIntake.model_validate(current.snapshot.intake_summary)
        except ValidationError as error:
            raise FlowError(
                "CASE_STATE_INVALID",
                "The persisted intake state is invalid.",
                409,
                current_version=current.version,
            ) from error
        storage_name = self._repository.get_case_media_handle(case_id)
        if storage_name is None:
            raise FlowError(
                "CASE_MEDIA_MISSING",
                "The case media mapping is unavailable.",
                409,
                current_version=current.version,
            )
        handle = CaseHandle(storage_name=storage_name)
        try:
            session = persisted.to_session(handle)
            g0 = self._rerun_g0(session, persisted)
        except ValueError as error:
            raise FlowError(
                "CASE_STATE_INVALID",
                "The persisted intake state is internally inconsistent.",
                409,
                current_version=current.version,
            ) from error
        except (MediaStorageError, UnsafeStoragePath) as error:
            raise FlowError(
                "CASE_MEDIA_MISSING",
                "Stored case media is missing or failed its integrity check.",
                409,
                current_version=current.version,
            ) from error
        if not g0.passed:
            failed = self._record_decisions(current, (g0,))
            failed, finalization_error = self._terminalize_and_release(
                failed,
                self._terminal_state_for(g0),
                handle,
            )
            raise self._gate_error(g0, failed.version) from finalization_error
        try:
            privacy = prepare_g1(
                self._media_store,
                session,
                PrivacyReview(
                    exif_choices=tuple(
                        ExifChoice(input_id=image.input_id, decision=decision)
                        for image, decision in zip(
                            session.images,
                            persisted.exif_decisions,
                            strict=True,
                        )
                    ),
                    model_copy_approved=True,
                    audit_fields=(),
                ),
            )
        except (MediaStorageError, UnsafeStoragePath) as error:
            raise FlowError(
                "CASE_MEDIA_MISSING",
                "Stored case media is missing or failed its integrity check.",
                409,
                current_version=current.version,
            ) from error
        if not privacy.decision.passed or privacy.prepared is None:
            failed = self._record_decisions(current, (g0, privacy.decision))
            failed, finalization_error = self._terminalize_and_release(
                failed,
                self._terminal_state_for(privacy.decision),
                handle,
            )
            raise self._gate_error(privacy.decision, failed.version) from finalization_error

        try:
            clarification_ref = self._media_store.write_bytes(
                handle,
                answer.encode("utf-8"),
                role="text",
                suffix=".txt",
                media_type="text/plain",
            )
            statement = self._statement_from_persisted(persisted, handle)
        except (MediaStorageError, UnsafeStoragePath) as error:
            raise FlowError(
                "CASE_MEDIA_MISSING",
                "Stored case media is missing or failed its integrity check.",
                409,
                current_version=current.version,
            ) from error
        extraction = deterministic_extraction(
            privacy.prepared,
            statement,
            incident_time=f"{answer}:00",
            clarification_ref=clarification_ref,
        )
        try:
            decisions = self._run_g2_to_g5(
                extraction,
                statement,
                prefix=(g0, privacy.decision),
                expect_clarification=False,
            )
        except GateRunBlocked as blocked:
            failed = self._record_decisions(current, blocked.decisions)
            failed, finalization_error = self._terminalize_and_release(
                failed,
                self._terminal_state_for(blocked.decision),
                handle,
            )
            raise self._gate_error(blocked.decision, failed.version) from (
                finalization_error or blocked
            )
        record = self._record_decisions(current, decisions)
        record = self._cases.save_active_clarification(
            case_id,
            expected_version=record.version,
            clarification=None,
        )
        packet = build_packet(
            case_id=case_id,
            state=CaseState.READY_TO_FILL,
            portal_state=PortalState.DRAFT,
            extraction=extraction,
            gate_decisions=decisions,
        )
        record = self._cases.transition_case(
            case_id,
            expected_version=record.version,
            target=CaseState.READY_TO_FILL,
            claim_packet=packet,
        )
        filling_packet = self._packet_state(packet, CaseState.FILLING, PortalState.DRAFT)
        record = self._cases.transition_case(
            case_id,
            expected_version=record.version,
            target=CaseState.FILLING,
            claim_packet=filling_packet,
        )
        portal_fields = self._portal_fields(extraction)
        try:
            review_url, rendered = self._portal.fill_to_review(case_id, portal_fields)
            if rendered.fields != portal_fields:
                raise PortalUnavailableError(
                    "Rendered portal fields differ from the authoritative draft"
                )
        except PortalUnavailableError as error:
            portal_cleanup_error = self._cleanup_portal_case(case_id)
            failed, finalization_error = self._terminalize_and_release(
                record,
                CaseState.FAILED,
                handle,
            )
            raise FlowError(
                "PORTAL_UNAVAILABLE",
                "The sandbox portal could not reach review.",
                502,
                current_version=failed.version,
            ) from (portal_cleanup_error or finalization_error or error)

        try:
            verifying_packet = self._packet_state(
                filling_packet,
                CaseState.VERIFYING,
                PortalState.REVIEW,
            )
            record = self._cases.transition_case(
                case_id,
                expected_version=record.version,
                target=CaseState.VERIFYING,
                claim_packet=verifying_packet,
            )
            portal = PortalView.model_validate(
                {
                    "reviewUrl": review_url,
                    "renderedValues": rendered.model_dump(mode="json", by_alias=True),
                    "verificationState": VerificationState.PENDING,
                }
            )
            return FlowResponse.model_validate(
                {
                    "requestId": self._request_id_factory(),
                    "case": CaseView.from_record(record),
                    "draftRevision": record.version,
                    "gateHistory": decisions,
                    "phase": FlowPhase.REVIEW,
                    "clarification": None,
                    "portal": portal,
                }
            )
        except Exception as error:
            portal_cleanup_error = self._cleanup_portal_case(case_id)
            failed, finalization_error = self._terminalize_and_release(
                record,
                CaseState.FAILED,
                handle,
            )
            raise FlowError(
                "PORTAL_COMMIT_FAILED",
                "The sandbox review could not be committed safely.",
                502,
                current_version=failed.version,
            ) from (portal_cleanup_error or finalization_error or error)

    def _run_g2_to_g5(
        self,
        extraction: ModelExtraction,
        statement: StatementSource,
        *,
        prefix: tuple[GateDecision, GateDecision],
        expect_clarification: bool,
    ) -> tuple[GateDecision, ...]:
        g2 = evaluate_g2(
            ModelOutputEnvelope(
                payload=extraction.model_dump_json(by_alias=True),
                refusal=False,
                truncated=False,
                attempt=0,
            ),
            approved_evidence=extraction.evidence,
        )
        if not g2.decision.passed or g2.extraction is None:
            raise GateRunBlocked((*prefix, g2.decision))
        safety = evaluate_g3(
            deterministic_safety_input(
                statement.text,
                tuple(reference.provenance_id for reference in extraction.provenance),
                model_signal=(
                    ModelSafetySignal.UNCERTAIN
                    if statement.safety_uncertain
                    else ModelSafetySignal.SAFE
                ),
            )
        )
        if not safety.decision.passed:
            raise GateRunBlocked((*prefix, g2.decision, safety.decision))
        packet_for_g4 = build_packet(
            case_id="case-gate-evaluation",
            state=CaseState.ANALYZING,
            portal_state=PortalState.DRAFT,
            extraction=g2.extraction,
            gate_decisions=(*prefix, g2.decision, safety.decision),
        )
        g4 = evaluate_g4(packet_for_g4)
        if not g4.decision.passed:
            raise GateRunBlocked(
                (*prefix, g2.decision, safety.decision, g4.decision)
            )
        questions = (
            (
                ClarificationQuestion(
                    field=RequiredClaimField.INCIDENT_TIME,
                    text=_QUESTION,
                ),
            )
            if expect_clarification
            else ()
        )
        g5 = evaluate_g5(
            g4,
            proposed_questions=questions,
            completed_rounds=0 if expect_clarification else 1,
        )
        if expect_clarification:
            if g5.decision.passed or g5.accepted_question is None:
                raise RuntimeError("Initial deterministic mock must require one clarification")
        elif not g5.decision.passed:
            raise GateRunBlocked(
                (*prefix, g2.decision, safety.decision, g4.decision, g5.decision)
            )
        return (*prefix, g2.decision, safety.decision, g4.decision, g5.decision)

    def _record_decisions(
        self,
        record: CaseRecord,
        decisions: tuple[GateDecision, ...],
    ) -> CaseRecord:
        return self._cases.commit_gate_phase(
            record.case_id,
            expected_version=record.version,
            decisions=decisions,
        )

    def _require_version(self, case_id: str, expected_version: int) -> CaseRecord:
        try:
            current = self._cases.get_case(case_id)
        except CaseNotFoundError as error:
            raise FlowError("CASE_NOT_FOUND", "The case does not exist.", 404) from error
        if current.version != expected_version:
            raise FlowError(
                "CASE_VERSION_CONFLICT",
                "The case changed since it was loaded.",
                409,
                current_version=current.version,
            )
        return current

    def _statement_for_initial(
        self,
        session: IntakeSession,
        prepared: PreparedMedia,
    ) -> tuple[StoredAssetRef, StatementSource]:
        if prepared.text is not None and session.text is not None:
            return session.text, StatementSource(
                local_ref=session.text.file_id,
                sha256=session.text.sha256,
                text=prepared.text,
                kind=EvidenceKind.USER_STATEMENT,
                user_confirmed=True,
                safety_uncertain=False,
            )
        if prepared.audio is None or session.audio is None:
            raise RuntimeError("Validated intake did not preserve one statement mode")
        fixture_audio = session.audio.sha256 == _SYNTHETIC_AUDIO_FIXTURE_SHA256
        transcript = (
            _SYNTHETIC_AUDIO_TRANSCRIPT
            if fixture_audio
            else _UNSUPPORTED_AUDIO_TRANSCRIPT
        )
        transcript_ref = store_transcript(self._media_store, session, transcript)
        return transcript_ref, StatementSource(
            local_ref=transcript_ref.file_id,
            sha256=transcript_ref.sha256,
            text=transcript,
            kind=EvidenceKind.TRANSCRIPT,
            user_confirmed=False,
            safety_uncertain=not fixture_audio,
        )

    def _statement_from_persisted(
        self,
        persisted: PersistedIntake,
        handle: CaseHandle,
    ) -> StatementSource:
        statement_ref = persisted.statement.to_ref()
        text = self._media_store.read_bytes(
            handle,
            statement_ref,
        ).decode("utf-8")
        is_user_statement = persisted.text is not None
        return StatementSource(
            local_ref=statement_ref.file_id,
            sha256=statement_ref.sha256,
            text=text,
            kind=(
                EvidenceKind.USER_STATEMENT
                if is_user_statement
                else EvidenceKind.TRANSCRIPT
            ),
            user_confirmed=is_user_statement,
            safety_uncertain=(
                persisted.audio is not None
                and persisted.audio.sha256 != _SYNTHETIC_AUDIO_FIXTURE_SHA256
            ),
        )

    def _rerun_g0(
        self,
        session: IntakeSession,
        persisted: PersistedIntake,
    ) -> GateDecision:
        from claimdone_api.media import AudioUpload, ImageUpload, IntakeConsents

        images = tuple(
            ImageUpload(
                content=self._media_store.read_bytes(session.handle, image.source),
                media_type=image.source.media_type,
            )
            for image in session.images
        )
        text = None
        audio = None
        if session.text is not None:
            text = self._media_store.read_bytes(session.handle, session.text).decode("utf-8")
        elif session.audio is not None:
            audio = AudioUpload(
                content=self._media_store.read_bytes(session.handle, session.audio),
                media_type=session.audio.media_type,
            )
        del persisted
        return validate_g0(
            IntakeRequest(
                images=images,
                text=text,
                audio=audio,
                consents=IntakeConsents(True, True, True),
            )
        ).decision

    @staticmethod
    def _portal_fields(extraction: ModelExtraction) -> PortalDraftFields:
        claim = extraction.claim.model_dump(mode="json", by_alias=True)
        claim["attachments"] = tuple(claim["attachments"])
        return PortalDraftFields.model_validate(
            {
                key: claim[key]
                for key in (
                    "incidentDate",
                    "incidentTime",
                    "location",
                    "claimantName",
                    "policyReference",
                    "vehicleRegistration",
                    "counterpartyKnown",
                    "narrative",
                    "attachments",
                )
            }
        )

    @staticmethod
    def _packet_state(
        packet: ClaimPacket,
        state: CaseState,
        portal: PortalState,
    ) -> ClaimPacket:
        body = packet.model_dump(mode="json", by_alias=True)
        body["state"] = state.value
        body["portalState"] = portal.value
        return ClaimPacket.model_validate(body)

    def _terminalize_case(
        self,
        record: CaseRecord,
        target: CaseState,
    ) -> CaseRecord:
        if record.state in TERMINAL_CASE_STATES:
            return record
        current = record
        if current.snapshot.active_clarification is not None:
            current = self._cases.save_active_clarification(
                current.case_id,
                expected_version=current.version,
                clarification=None,
            )
        if current.snapshot.claim_packet is not None:
            current = self._cases.save_claim_packet(
                current.case_id,
                expected_version=current.version,
                claim_packet=None,
            )
        if current.snapshot.intake_summary is not None:
            current = self._cases.save_intake_summary(
                current.case_id,
                expected_version=current.version,
                summary=None,
            )
        return self._cases.transition_case(
            current.case_id,
            expected_version=current.version,
            target=target,
            actor=ActorType.SYSTEM,
        )

    def _release_media_handle(self, case_id: str, handle: CaseHandle) -> None:
        self._media_store.delete_case(handle)
        self._repository.unbind_case_media_handle(case_id, handle.storage_name)

    def _cleanup_portal_case(self, case_id: str) -> Exception | None:
        try:
            self._portal.cleanup_case(case_id)
        except Exception as error:
            return error
        return None

    def _terminalize_and_release(
        self,
        record: CaseRecord,
        target: CaseState,
        handle: CaseHandle,
    ) -> tuple[CaseRecord, Exception | None]:
        finalization_error: Exception | None = None
        current = record
        try:
            current = self._terminalize_case(record, target)
        except Exception as error:
            finalization_error = error
            with suppress(Exception):
                current = self._cases.get_case(record.case_id)
                current = self._terminalize_case(current, target)
                finalization_error = None
        try:
            self._release_media_handle(record.case_id, handle)
        except Exception as error:
            if finalization_error is None:
                finalization_error = error
        with suppress(Exception):
            current = self._cases.get_case(record.case_id)
        return current, finalization_error

    @staticmethod
    def _terminal_state_for(decision: GateDecision) -> CaseState:
        if GateReasonCode.G3_INJURY_OR_EMERGENCY in decision.reason_codes:
            return CaseState.EMERGENCY_STOPPED
        return CaseState.BLOCKED

    @staticmethod
    def _gate_error(decision: GateDecision, version: int | None) -> FlowError:
        return FlowError(
            code="DETERMINISTIC_GATE_BLOCKED",
            message=f"Deterministic gate {decision.gate_id.value} blocked the flow.",
            status_code=422,
            current_version=version,
            gate_decision=decision,
        )

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("WalkingSkeletonService clock must be timezone-aware")
        return value

    @contextmanager
    def _case_lock(self, case_id: str) -> Iterator[None]:
        with self._locks_guard:
            entry = self._case_locks.get(case_id)
            if entry is None:
                entry = _LockEntry(lock=Lock())
                self._case_locks[case_id] = entry
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._case_locks.get(case_id) is entry:
                    del self._case_locks[case_id]

    def _retained_lock_count(self) -> int:
        with self._locks_guard:
            return len(self._case_locks)
