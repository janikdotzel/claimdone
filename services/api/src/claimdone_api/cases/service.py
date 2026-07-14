"""Validated case workflow and persistence orchestration."""

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from claimdone_api.audit import (
    build_gate_audit_event,
    build_state_change_event,
    redact_metadata,
)
from claimdone_api.contracts import (
    ActorType,
    CaseState,
    ClaimPacket,
    GateDecision,
    PortalState,
    TranscriptConfirmationRequest,
    validate_case_transition,
)
from claimdone_api.contracts.state_machine import InvalidCaseTransition
from claimdone_api.persistence import (
    AnalysisWorkflowCommand,
    AnalysisWorkflowResult,
    CaseRecord,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    CaseSnapshot,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    SqliteCaseRepository,
    TerminalProviderFailureCommand,
    TerminalProviderFailureResult,
    TranscriptTransitionResult,
    WorkflowAtomicityError,
    portal_state_after_transition,
    validate_portal_state,
)
from claimdone_api.persistence.models import JsonObject

from .errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
    InvalidCaseStateTransitionError,
)
from .ports import CaseResourceCleaner, NoOpCaseResourceCleaner
from .reset import DemoResetService

_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_case_id() -> str:
    return f"case_{uuid4().hex}"


class CaseService:
    """Only public path for validated case-state transitions."""

    def __init__(
        self,
        repository: SqliteCaseRepository,
        *,
        resource_cleaner: CaseResourceCleaner | None = None,
        now: Callable[[], datetime] = _utc_now,
        case_id_factory: Callable[[], str] = _new_case_id,
    ) -> None:
        self._repository = repository
        self._resource_cleaner = resource_cleaner or NoOpCaseResourceCleaner()
        self._reset_service = DemoResetService(repository, self._resource_cleaner)
        self._now = now
        self._case_id_factory = case_id_factory

    def create_case(self, metadata: Mapping[str, JsonValue] | None = None) -> CaseRecord:
        case_id = self._case_id_factory()
        if _CASE_ID_PATTERN.fullmatch(case_id) is None:
            raise ValueError("case_id_factory returned an invalid canonical identifier")
        return self._repository.create_case(
            case_id=case_id,
            redacted_metadata=redact_metadata(metadata),
            created_at=self._aware_now(),
        )

    def get_case(self, case_id: str) -> CaseRecord:
        record = self._repository.get_case(case_id)
        if record is None:
            raise CaseNotFoundError(case_id)
        return record

    def delete_case(self, case_id: str) -> None:
        """Idempotently remove external data before the cascading database row."""

        self._resource_cleaner.delete_case_resources(case_id)
        self._repository.delete_case(case_id)

    def transition_case(
        self,
        case_id: str,
        *,
        expected_version: int,
        target: CaseState,
        actor: ActorType = ActorType.SYSTEM,
        claim_packet: ClaimPacket | None = None,
    ) -> CaseRecord:
        current = self._get_case_for_update(case_id, expected_version)
        try:
            validate_case_transition(current.state, target)
        except InvalidCaseTransition as error:
            raise InvalidCaseStateTransitionError(current.state, target) from error

        snapshot = current.snapshot
        if snapshot.claim_packet is not None and claim_packet is None:
            raise CaseSnapshotValidationError(
                "A state transition with a stored ClaimPacket requires its target-state snapshot"
            )
        if claim_packet is not None:
            self._validate_claim_packet(case_id, target, claim_packet)
            snapshot = replace(
                snapshot,
                portal_state=claim_packet.portal_state,
                claim_packet=claim_packet,
            )
        else:
            snapshot = replace(
                snapshot,
                portal_state=portal_state_after_transition(snapshot.portal_state, target),
            )

        occurred_at = self._aware_now()
        event = build_state_change_event(
            case_id=case_id,
            current=current.state,
            target=target,
            actor=actor,
            occurred_at=occurred_at,
        )
        try:
            if target is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
                transcript_id, local_ref, digest = self._pending_transcript_identity(
                    case_id,
                    snapshot,
                )
                return self._repository.save_pending_transcript_and_transition(
                    case_id=case_id,
                    expected_case_version=expected_version,
                    transcript_id=transcript_id,
                    transcript_sha256=digest,
                    local_ref=local_ref,
                    snapshot=snapshot,
                    event=event,
                    updated_at=occurred_at,
                ).case
            return self._repository.transition_case(
                case_id=case_id,
                expected_version=expected_version,
                target=target,
                snapshot=snapshot,
                event=event,
                updated_at=occurred_at,
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except InvalidCaseTransition as error:
            raise InvalidCaseStateTransitionError(current.state, target) from error

    def confirm_transcript(
        self,
        case_id: str,
        *,
        expected_case_version: int,
        confirmation: TranscriptConfirmationRequest,
    ) -> TranscriptTransitionResult:
        """Bind explicit human confirmation and enter analysis atomically."""

        if confirmation.case_id != case_id:
            raise CaseSnapshotValidationError(
                "Transcript confirmation caseId must match the selected case"
            )
        if confirmation.expected_version != expected_case_version:
            raise CaseVersionConflictError(
                case_id,
                confirmation.expected_version,
                expected_case_version,
            )
        current = self._get_case_for_update(case_id, expected_case_version)
        target = CaseState.ANALYZING
        try:
            validate_case_transition(current.state, target)
        except InvalidCaseTransition as error:
            raise InvalidCaseStateTransitionError(current.state, target) from error
        snapshot = replace(
            current.snapshot,
            portal_state=portal_state_after_transition(
                current.snapshot.portal_state,
                target,
            ),
        )
        occurred_at = self._aware_now()
        event = build_state_change_event(
            case_id=case_id,
            current=current.state,
            target=target,
            actor=ActorType.HUMAN,
            occurred_at=occurred_at,
        )
        try:
            return self._repository.confirm_transcript_and_transition(
                case_id=case_id,
                expected_case_version=expected_case_version,
                transcript_id=confirmation.transcript_id,
                transcript_sha256=confirmation.transcript_sha256,
                snapshot=snapshot,
                event=event,
                updated_at=occurred_at,
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error

    def save_intake_summary(
        self,
        case_id: str,
        *,
        expected_version: int,
        summary: Mapping[str, JsonValue] | None,
    ) -> CaseRecord:
        validated = self._validate_json_object(summary)
        return self._replace_snapshot(
            case_id,
            expected_version=expected_version,
            transform=lambda snapshot: replace(snapshot, intake_summary=validated),
        )

    def save_active_clarification(
        self,
        case_id: str,
        *,
        expected_version: int,
        clarification: Mapping[str, JsonValue] | None,
    ) -> CaseRecord:
        validated = self._validate_json_object(clarification)
        return self._replace_snapshot(
            case_id,
            expected_version=expected_version,
            transform=lambda snapshot: replace(
                snapshot,
                active_clarification=validated,
            ),
        )

    def replace_redacted_metadata(
        self,
        case_id: str,
        *,
        expected_version: int,
        metadata: Mapping[str, JsonValue],
    ) -> CaseRecord:
        redacted = redact_metadata(metadata)
        return self._replace_snapshot(
            case_id,
            expected_version=expected_version,
            transform=lambda snapshot: replace(snapshot, redacted_metadata=redacted),
        )

    def save_claim_packet(
        self,
        case_id: str,
        *,
        expected_version: int,
        claim_packet: ClaimPacket | None,
    ) -> CaseRecord:
        current = self._get_case_for_update(case_id, expected_version)
        if claim_packet is not None:
            self._validate_claim_packet(case_id, current.state, claim_packet)
        snapshot = replace(
            current.snapshot,
            portal_state=(
                current.snapshot.portal_state
                if claim_packet is None
                else claim_packet.portal_state
            ),
            claim_packet=claim_packet,
        )
        return self._replace_known_snapshot(
            case_id,
            expected_version=expected_version,
            snapshot=snapshot,
        )

    def set_portal_state(
        self,
        case_id: str,
        *,
        expected_version: int,
        portal_state: PortalState,
    ) -> CaseRecord:
        current = self._get_case_for_update(case_id, expected_version)
        try:
            validate_portal_state(current.state, portal_state)
        except ValueError as error:
            raise CaseSnapshotValidationError(str(error)) from error
        if (
            current.snapshot.claim_packet is not None
            and current.snapshot.claim_packet.portal_state is not portal_state
        ):
            raise CaseSnapshotValidationError(
                "PortalState cannot diverge from the stored ClaimPacket"
            )
        return self._replace_known_snapshot(
            case_id,
            expected_version=expected_version,
            snapshot=replace(current.snapshot, portal_state=portal_state),
        )

    def record_gate_decision(
        self,
        case_id: str,
        *,
        expected_version: int,
        decision: GateDecision,
        actor: ActorType = ActorType.SYSTEM,
    ) -> CaseRecord:
        self._get_case_for_update(case_id, expected_version)
        event = build_gate_audit_event(
            case_id=case_id,
            decision=decision,
            actor=actor,
        )
        try:
            return self._repository.record_gate_decision(
                case_id=case_id,
                expected_version=expected_version,
                decision=decision,
                event=event,
                updated_at=self._aware_now(),
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error

    def list_audit_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedAuditEvent, ...]:
        self.get_case(case_id)
        return self._repository.list_audit_events(case_id, after=after, limit=limit)

    def list_gate_decisions(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedGateDecision, ...]:
        self.get_case(case_id)
        return self._repository.list_gate_decisions(case_id, after=after, limit=limit)

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        self.get_case(case_id)
        return self._repository.list_workflow_events(case_id, after=after, limit=limit)

    def commit_analysis_workflow(
        self,
        command: AnalysisWorkflowCommand,
    ) -> AnalysisWorkflowResult:
        """Expose the repository's single-CAS analysis authority boundary."""

        try:
            return self._repository.commit_analysis_workflow(command)
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(command.case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def commit_terminal_provider_failure(
        self,
        command: TerminalProviderFailureCommand,
    ) -> TerminalProviderFailureResult:
        """Expose the provider-failure boundary without a split transition path."""

        try:
            return self._repository.commit_terminal_provider_failure(command)
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(command.case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def reset_demo(self) -> int:
        return self._reset_service.reset()

    def _replace_snapshot(
        self,
        case_id: str,
        *,
        expected_version: int,
        transform: Callable[[CaseSnapshot], CaseSnapshot],
    ) -> CaseRecord:
        current = self._get_case_for_update(case_id, expected_version)
        return self._replace_known_snapshot(
            case_id,
            expected_version=expected_version,
            snapshot=transform(current.snapshot),
        )

    def _get_case_for_update(self, case_id: str, expected_version: int) -> CaseRecord:
        current = self.get_case(case_id)
        if current.version != expected_version:
            raise CaseVersionConflictError(
                case_id,
                expected_version,
                current.version,
            )
        return current

    def _replace_known_snapshot(
        self,
        case_id: str,
        *,
        expected_version: int,
        snapshot: CaseSnapshot,
    ) -> CaseRecord:
        try:
            return self._repository.replace_snapshot(
                case_id=case_id,
                expected_version=expected_version,
                snapshot=snapshot,
                updated_at=self._aware_now(),
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("CaseService clock must return timezone-aware timestamps")
        return value

    @staticmethod
    def _validate_claim_packet(
        case_id: str,
        state: CaseState,
        claim_packet: ClaimPacket,
    ) -> None:
        if claim_packet.case_id != case_id:
            raise CaseSnapshotValidationError("ClaimPacket caseId does not match the case")
        if claim_packet.state is not state:
            raise CaseSnapshotValidationError("ClaimPacket state does not match CaseState")

    @staticmethod
    def _validate_json_object(
        value: Mapping[str, JsonValue] | None,
    ) -> JsonObject | None:
        if value is None:
            return None
        return _JSON_OBJECT_ADAPTER.validate_python(dict(value), strict=True)

    @staticmethod
    def _pending_transcript_identity(
        case_id: str,
        snapshot: CaseSnapshot,
    ) -> tuple[str, str, str]:
        summary = snapshot.intake_summary
        statement = None if summary is None else summary.get("statement")
        audio = None if summary is None else summary.get("audio")
        text = None if summary is None else summary.get("text")
        if not isinstance(statement, dict) or not isinstance(audio, dict) or text is not None:
            raise CaseSnapshotValidationError(
                "Transcript confirmation requires a persisted audio statement"
            )
        audio_ref = audio.get("fileId")
        audio_media_type = audio.get("mediaType")
        audio_digest = audio.get("sha256")
        if (
            not isinstance(audio_ref, str)
            or not re.fullmatch(r"audio-[a-f0-9]{32}\.wav", audio_ref)
            or audio_media_type != "audio/wav"
            or not isinstance(audio_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", audio_digest)
        ):
            raise CaseSnapshotValidationError("Persisted audio reference is invalid")
        local_ref = statement.get("fileId")
        digest = statement.get("sha256")
        media_type = statement.get("mediaType")
        if (
            not isinstance(local_ref, str)
            or not re.fullmatch(r"transcript-[a-f0-9]{32}\.txt", local_ref)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or media_type != "text/plain"
        ):
            raise CaseSnapshotValidationError("Persisted transcript reference is invalid")
        identity = hashlib.sha256(
            f"claimdone-transcript-v1\0{case_id}\0{local_ref}\0{digest}".encode()
        ).hexdigest()
        return f"transcript-{identity[:32]}", local_ref, digest

    @staticmethod
    def _version_conflict(error: CaseRecordVersionConflictError) -> CaseVersionConflictError:
        return CaseVersionConflictError(
            error.case_id,
            error.expected_version,
            error.current_version,
        )
