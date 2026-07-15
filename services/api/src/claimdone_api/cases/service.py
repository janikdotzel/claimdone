"""Validated case workflow and persistence orchestration."""

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue

from claimdone_api.audit import redact_metadata
from claimdone_api.contracts import (
    CaseState,
    TranscriptConfirmationRequest,
    WorkflowSnapshot,
    validate_case_transition,
)
from claimdone_api.contracts.state_machine import InvalidCaseTransition
from claimdone_api.media import PersistentCaseMediaCleaner
from claimdone_api.persistence import (
    AnalysisWorkflowCommand,
    AnalysisWorkflowResult,
    CaseRecord,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    IntakeDisclosureCommand,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    SqliteCaseRepository,
    TerminalProviderFailureCommand,
    TerminalProviderFailureResult,
    TranscriptionOutcomeCommand,
    TranscriptStateError,
    TranscriptTransitionResult,
    WorkflowAtomicityError,
)

from .errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
    InvalidCaseStateTransitionError,
)
from .reset import DemoResetService

_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


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
        resource_cleaner: PersistentCaseMediaCleaner | None = None,
        now: Callable[[], datetime] = _utc_now,
        case_id_factory: Callable[[], str] = _new_case_id,
    ) -> None:
        if (
            type(self) is not CaseService
            or type(repository) is not SqliteCaseRepository
            or repository.is_canonical_authority is not True
        ):
            raise TypeError("CaseService requires the exact canonical repository type")
        cleaner = resource_cleaner or PersistentCaseMediaCleaner(
            repository,
            repository.media_store,
        )
        if cleaner.repository is not repository or cleaner.store is not repository.media_store:
            raise TypeError(
                "CaseService cleaner must be bound to its exact repository and media store"
            )
        self._repository = repository
        self._resource_cleaner = cleaner
        self._reset_service = DemoResetService(repository, cleaner)
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

    def get_workflow_snapshot(
        self,
        case_id: str,
        *,
        request_id: str,
    ) -> WorkflowSnapshot:
        """Project only the closed canonical frontend workflow contract."""

        if type(request_id) is not str or _CASE_ID_PATTERN.fullmatch(request_id) is None:
            raise ValueError("request_id must be a canonical identifier")
        try:
            return self._repository.get_workflow_snapshot(
                case_id,
                request_id=request_id,
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error

    def delete_case(self, case_id: str) -> None:
        """Idempotently serialize intake, exact media cleanup, and row deletion."""

        self._repository.delete_case_and_resources(case_id)

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
        occurred_at = self._aware_now()
        try:
            return self._repository.confirm_transcript_and_transition(
                case_id=case_id,
                expected_case_version=expected_case_version,
                transcript_id=confirmation.transcript_id,
                transcript_sha256=confirmation.transcript_sha256,
                updated_at=occurred_at,
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except TranscriptStateError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def commit_intake_disclosure(
        self,
        command: IntakeDisclosureCommand,
    ) -> CaseRecord:
        """Expose the only productive G0/G1 + media + disclosure writer."""

        try:
            return self._repository.commit_intake_disclosure(command)
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(command.case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def commit_transcription_outcome(
        self,
        command: TranscriptionOutcomeCommand,
    ) -> TranscriptTransitionResult:
        """Persist one authority-bound provider transcript atomically."""

        try:
            return self._repository.commit_transcription_outcome(command)
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(command.case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except (TranscriptStateError, WorkflowAtomicityError) as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def begin_text_analysis(
        self,
        case_id: str,
        *,
        expected_version: int,
    ) -> CaseRecord:
        """Advance an authority-bound text intake without caller snapshot data."""

        try:
            return self._repository.begin_text_analysis(
                case_id=case_id,
                expected_version=expected_version,
                updated_at=self._aware_now(),
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

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
        try:
            return self._repository.list_workflow_events(
                case_id,
                after=after,
                limit=limit,
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error

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

    def _get_case_for_update(self, case_id: str, expected_version: int) -> CaseRecord:
        if (
            type(expected_version) is not int
            or expected_version < 1
            or expected_version > _SQLITE_MAX_INTEGER
        ):
            raise TypeError(
                "expected_version must be an exact positive SQLite int64 integer"
            )
        current = self.get_case(case_id)
        if current.version != expected_version:
            raise CaseVersionConflictError(
                case_id,
                expected_version,
                current.version,
            )
        return current

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("CaseService clock must return timezone-aware timestamps")
        return value

    @staticmethod
    def _version_conflict(error: CaseRecordVersionConflictError) -> CaseVersionConflictError:
        return CaseVersionConflictError(
            error.case_id,
            error.expected_version,
            error.current_version,
        )
