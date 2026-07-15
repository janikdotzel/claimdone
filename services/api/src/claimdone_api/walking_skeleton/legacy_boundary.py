"""Explicitly non-production persistence boundary for the retired demo flow.

The canonical repository intentionally has no generic gate writer and closes
all authority-bearing snapshot and state mutations.  The old walking skeleton
is retained only for opt-in development regression tests, so its compatibility
writers live on distinct types that the default application never constructs.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from claimdone_api.audit import (
    build_gate_audit_event,
    build_state_change_event,
    redact_metadata,
)
from claimdone_api.cases.errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.cases.ports import CaseResourceCleaner, NoOpCaseResourceCleaner
from claimdone_api.contracts import (
    ActorType,
    AuditEvent,
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    GateWorkflowEvent,
    PortalState,
    StateWorkflowEvent,
    TranscriptConfirmationRequest,
    WorkflowEventKind,
    validate_case_transition,
)
from claimdone_api.media.storage import CaseMediaStore
from claimdone_api.persistence import (
    CaseRecord,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    CaseSnapshot,
    SequencedAuditEvent,
    SequencedGateDecision,
    SequencedWorkflowEvent,
    SqliteCaseRepository,
    TranscriptStateError,
    TranscriptTransitionResult,
    WorkflowAtomicityError,
    portal_state_after_transition,
    validate_portal_state,
)
from claimdone_api.persistence.models import JsonObject
from claimdone_api.persistence.sqlite import (
    _transcript_identity_from_summary,
    _validate_snapshot,
)

_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

_LEGACY_GATE_SEQUENCE = (
    GateId.G0_INTAKE,
    GateId.G1_PRIVACY,
    GateId.G2_OUTPUT_CONTRACT,
    GateId.G3_SAFETY_SCOPE,
    GateId.G4_PROVENANCE,
    GateId.G5_COMPLETENESS,
)


class LegacyWalkingRepository:
    """Dev/test-only repository with the retired multi-commit semantics."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        media_root: str | Path | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._backend = SqliteCaseRepository._open_legacy_backend(
            database_path,
            media_root=media_root,
            busy_timeout_ms=busy_timeout_ms,
        )

    @property
    def database_path(self) -> Path:
        return self._backend.database_path

    @property
    def is_canonical_authority(self) -> bool:
        return False

    @property
    def media_store(self) -> CaseMediaStore:
        return self._backend.media_store

    def get_case_media_handle(self, case_id: str) -> str | None:
        return self._backend.get_case_media_handle(case_id)

    def list_case_media_handles(self) -> tuple[tuple[str, str], ...]:
        return self._backend.list_case_media_handles()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._backend, name)

    def replace_snapshot(
        self,
        *,
        case_id: str,
        expected_version: int,
        snapshot: CaseSnapshot,
        updated_at: datetime,
    ) -> CaseRecord:
        with self._backend._write_connection() as connection:
            current = self._backend._require_current(connection, case_id, expected_version)
            _validate_snapshot(case_id, current.state, snapshot)
            self._backend._update_case_row(
                connection,
                current=current,
                state=current.state,
                snapshot=snapshot,
                updated_at=updated_at,
            )
        return self._backend._require_case(case_id)

    def transition_case(
        self,
        *,
        case_id: str,
        expected_version: int,
        target: CaseState,
        snapshot: CaseSnapshot,
        event: AuditEvent,
        updated_at: datetime,
    ) -> CaseRecord:
        with self._backend._write_connection() as connection:
            current = self._backend._require_current(connection, case_id, expected_version)
            if target is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION or (
                current.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
                and target is CaseState.ANALYZING
            ):
                raise TranscriptStateError(
                    "Transcript state transitions require the atomic transcript methods"
                )
            validate_case_transition(current.state, target)
            self._backend._validate_state_event(event, current=current, target=target)
            _validate_snapshot(case_id, target, snapshot)
            self._backend._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=snapshot,
                updated_at=updated_at,
            )
            audit_sequence = self._backend._insert_audit_event(connection, event)
            state_event = StateWorkflowEvent.model_validate(
                {
                    "kind": WorkflowEventKind.STATE,
                    "actor": event.actor,
                    "fromState": current.state,
                    "toState": target,
                }
            )
            self._backend._insert_workflow_projection(
                connection,
                audit_sequence=audit_sequence,
                audit=event,
                event=state_event,
            )
        return self._backend._require_case(case_id)

    def save_pending_transcript_and_transition(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        transcript_id: str,
        transcript_sha256: str,
        local_ref: str,
        updated_at: datetime,
    ) -> TranscriptTransitionResult:
        self._backend._validate_transcript_identity(
            transcript_id,
            transcript_sha256,
            local_ref,
        )
        target = CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
        with self._backend._write_connection() as connection:
            current = self._backend._require_current(
                connection,
                case_id,
                expected_case_version,
            )
            if current.state is not CaseState.DISCLOSED:
                raise TranscriptStateError("Pending transcript requires disclosed legacy case")
            summary = current.snapshot.intake_summary
            if summary is None:
                raise TranscriptStateError("Pending transcript summary is missing")
            derived_id, derived_ref, derived_hash = _transcript_identity_from_summary(
                case_id,
                summary,
            )
            if (transcript_id, local_ref, transcript_sha256) != (
                derived_id,
                derived_ref,
                derived_hash,
            ):
                raise TranscriptStateError("Pending transcript identity is mismatched")
            validate_case_transition(current.state, target)
            event = build_state_change_event(
                case_id=case_id,
                current=current.state,
                target=target,
                actor=ActorType.SYSTEM,
                occurred_at=updated_at,
            )
            self._backend._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=current.snapshot,
                updated_at=updated_at,
            )
            connection.execute(
                """
                INSERT INTO case_transcripts (
                    transcript_id, case_id, version, bound_case_version,
                    transcript_sha256, local_ref, confirmed, created_at, confirmed_at
                ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, NULL)
                """,
                (
                    transcript_id,
                    case_id,
                    current.version + 1,
                    transcript_sha256,
                    local_ref,
                    updated_at.isoformat(),
                ),
            )
            sequence = self._backend._insert_audit_event(connection, event)
            self._backend._insert_workflow_projection(
                connection,
                audit_sequence=sequence,
                audit=event,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.SYSTEM,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            case = self._backend._require_current(
                connection,
                case_id,
                current.version + 1,
            )
            transcript = self._backend._require_transcript(connection, case_id)
        return TranscriptTransitionResult(case=case, transcript=transcript)

    def confirm_transcript_and_transition(
        self,
        *,
        case_id: str,
        expected_case_version: int,
        transcript_id: str,
        transcript_sha256: str,
        updated_at: datetime,
    ) -> TranscriptTransitionResult:
        target = CaseState.ANALYZING
        with self._backend._write_connection() as connection:
            current = self._backend._require_current(
                connection,
                case_id,
                expected_case_version,
            )
            if current.state is not CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
                raise TranscriptStateError("Legacy case is not awaiting transcript confirmation")
            transcript = self._backend._require_transcript(connection, case_id)
            if (
                transcript.transcript_id != transcript_id
                or transcript.transcript_sha256 != transcript_sha256
                or transcript.version != 1
                or transcript.bound_case_version != current.version
                or transcript.confirmed
            ):
                raise TranscriptStateError("Legacy transcript confirmation is stale")
            validate_case_transition(current.state, target)
            connection.execute(
                """
                UPDATE case_transcripts
                SET version = 2, confirmed = 1, confirmed_at = ?
                WHERE case_id = ? AND version = 1 AND confirmed = 0
                """,
                (updated_at.isoformat(), case_id),
            )
            self._backend._update_case_row(
                connection,
                current=current,
                state=target,
                snapshot=current.snapshot,
                updated_at=updated_at,
            )
            event = build_state_change_event(
                case_id=case_id,
                current=current.state,
                target=target,
                actor=ActorType.HUMAN,
                occurred_at=updated_at,
            )
            sequence = self._backend._insert_audit_event(connection, event)
            self._backend._insert_workflow_projection(
                connection,
                audit_sequence=sequence,
                audit=event,
                event=StateWorkflowEvent.model_validate(
                    {
                        "kind": WorkflowEventKind.STATE,
                        "actor": ActorType.HUMAN,
                        "fromState": current.state,
                        "toState": target,
                    }
                ),
            )
            case = self._backend._require_current(
                connection,
                case_id,
                current.version + 1,
            )
            transcript = self._backend._require_transcript(connection, case_id)
        return TranscriptTransitionResult(case=case, transcript=transcript)

    def commit_gate_phase(
        self,
        *,
        case_id: str,
        expected_version: int,
        decisions: tuple[GateDecision, ...],
        updated_at: datetime,
    ) -> CaseRecord:
        """Persist server-derived legacy gates inside the dev-only boundary."""

        if not decisions or type(decisions) is not tuple:
            raise WorkflowAtomicityError("Legacy gate phase cannot be empty")
        for decision in decisions:
            if not isinstance(decision, GateDecision):
                raise WorkflowAtomicityError("Legacy gate phase requires canonical decisions")
            self._backend._require_canonical_contract(decision, "GateDecision")
        with self._backend._write_connection() as connection:
            current = self._backend._require_current(connection, case_id, expected_version)
            history = self._backend._read_gate_decisions(connection, case_id=case_id)
            self._validate_gate_phase(
                current,
                history=history,
                decisions=decisions,
                updated_at=updated_at,
            )
            self._backend._update_case_row(
                connection,
                current=current,
                state=current.state,
                snapshot=current.snapshot,
                updated_at=updated_at,
            )
            for decision in decisions:
                self._backend._insert_gate_decision_row(
                    connection,
                    case_id=case_id,
                    decision=decision,
                )
                audit = build_gate_audit_event(
                    case_id=case_id,
                    decision=decision,
                    actor=ActorType.SYSTEM,
                )
                audit_sequence = self._backend._insert_audit_event(connection, audit)
                self._backend._insert_workflow_projection(
                    connection,
                    audit_sequence=audit_sequence,
                    audit=audit,
                    event=GateWorkflowEvent.model_validate(
                        {"kind": WorkflowEventKind.GATE, "decision": decision}
                    ),
                )
        return self._backend._require_case(case_id)

    @staticmethod
    def _validate_gate_phase(
        current: CaseRecord,
        *,
        history: tuple[GateDecision, ...],
        decisions: tuple[GateDecision, ...],
        updated_at: datetime,
    ) -> None:
        expected: tuple[GateId, ...]
        if current.state is CaseState.CREATED and (not history or not history[-1].passed):
            expected = _LEGACY_GATE_SEQUENCE[:2]
        elif (
            current.state is CaseState.ANALYZING
            and len(history) >= 2
            and tuple(decision.gate_id for decision in history[-2:])
            == _LEGACY_GATE_SEQUENCE[:2]
            and all(decision.passed for decision in history[-2:])
        ):
            expected = _LEGACY_GATE_SEQUENCE[2:]
        elif (
            current.state is CaseState.AWAITING_CLARIFICATION
            and len(history) >= len(_LEGACY_GATE_SEQUENCE)
            and tuple(
                decision.gate_id
                for decision in history[-len(_LEGACY_GATE_SEQUENCE) :]
            )
            == _LEGACY_GATE_SEQUENCE
            and all(
                decision.passed
                for decision in history[-len(_LEGACY_GATE_SEQUENCE) : -1]
            )
        ):
            expected = _LEGACY_GATE_SEQUENCE
        else:
            raise WorkflowAtomicityError("Legacy gate phase has no authorized demo state")
        if tuple(decision.gate_id for decision in decisions) != expected[: len(decisions)]:
            raise WorkflowAtomicityError("Legacy gate phase must be an exact phase prefix")
        if any(not decision.passed for decision in decisions[:-1]):
            raise WorkflowAtomicityError("A failed legacy gate must terminate its phase")
        if decisions[-1].passed and len(decisions) != len(expected):
            raise WorkflowAtomicityError("A passing legacy gate phase must be complete")
        prior_time = history[-1].decided_at if history else current.updated_at
        times = tuple(decision.decided_at for decision in decisions)
        if tuple(sorted(times)) != times or times[0] < prior_time or times[-1] > updated_at:
            raise WorkflowAtomicityError(
                "Legacy gate timestamps must be monotonic and commit-bound"
            )


class LegacyWalkingCaseBoundary:
    """Dev/test-only service surface consumed solely by WalkingSkeletonService."""

    def __init__(
        self,
        repository: LegacyWalkingRepository,
        *,
        resource_cleaner: CaseResourceCleaner | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        case_id_factory: Callable[[], str] = lambda: f"case_{uuid4().hex}",
    ) -> None:
        if type(repository) is not LegacyWalkingRepository:
            raise TypeError("Legacy boundary requires the exact legacy repository wrapper")
        self._repository = repository
        self._resource_cleaner = resource_cleaner or NoOpCaseResourceCleaner()
        self._now = now
        self._case_id_factory = case_id_factory

    @property
    def repository(self) -> LegacyWalkingRepository:
        return self._repository

    def create_case(self, metadata: Mapping[str, JsonValue] | None = None) -> CaseRecord:
        case_id = self._case_id_factory()
        if _CASE_ID_PATTERN.fullmatch(case_id) is None:
            raise ValueError("case_id_factory returned an invalid canonical identifier")
        return cast(
            CaseRecord,
            self._repository.create_case(
                case_id=case_id,
                redacted_metadata=redact_metadata(metadata),
                created_at=self._aware_now(),
            ),
        )

    def get_case(self, case_id: str) -> CaseRecord:
        record = cast(CaseRecord | None, self._repository.get_case(case_id))
        if record is None:
            raise CaseNotFoundError(case_id)
        return record

    def delete_case(self, case_id: str) -> None:
        self._resource_cleaner.delete_case_resources(case_id)
        self._repository.delete_case(case_id)

    def reset_demo(self) -> int:
        self._resource_cleaner.reset_resources()
        return cast(int, self._repository.reset_cases())

    def list_audit_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedAuditEvent, ...]:
        self.get_case(case_id)
        return cast(
            tuple[SequencedAuditEvent, ...],
            self._repository.list_audit_events(case_id, after=after, limit=limit),
        )

    def list_gate_decisions(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedGateDecision, ...]:
        self.get_case(case_id)
        return cast(
            tuple[SequencedGateDecision, ...],
            self._repository.list_gate_decisions(case_id, after=after, limit=limit),
        )

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        self.get_case(case_id)
        return cast(
            tuple[SequencedWorkflowEvent, ...],
            self._repository.list_workflow_events(case_id, after=after, limit=limit),
        )

    def transition_case(
        self,
        case_id: str,
        *,
        expected_version: int,
        target: CaseState,
        actor: ActorType = ActorType.SYSTEM,
        claim_packet: ClaimPacket | None = None,
    ) -> CaseRecord:
        """Retain the retired generic state writer only inside the dev boundary."""

        current = self._get_case_for_update(case_id, expected_version)
        try:
            validate_case_transition(current.state, target)
        except ValueError as error:
            raise CaseSnapshotValidationError(str(error)) from error
        snapshot = current.snapshot
        if snapshot.claim_packet is not None and claim_packet is None:
            raise CaseSnapshotValidationError(
                "A legacy transition with a stored packet requires its target packet"
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
        occurred_at = max(self._aware_now(), current.updated_at)
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
                    updated_at=occurred_at,
                ).case
            event = build_state_change_event(
                case_id=case_id,
                current=current.state,
                target=target,
                actor=actor,
                occurred_at=occurred_at,
            )
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
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def set_portal_state(
        self,
        case_id: str,
        *,
        expected_version: int,
        portal_state: PortalState,
    ) -> CaseRecord:
        """Retain the retired split portal writer only in the dev boundary."""

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

    def confirm_transcript(
        self,
        case_id: str,
        *,
        expected_case_version: int,
        confirmation: TranscriptConfirmationRequest,
    ) -> TranscriptTransitionResult:
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
        self._get_case_for_update(case_id, expected_case_version)
        try:
            return self._repository.confirm_transcript_and_transition(
                case_id=case_id,
                expected_case_version=expected_case_version,
                transcript_id=confirmation.transcript_id,
                transcript_sha256=confirmation.transcript_sha256,
                updated_at=self._aware_now(),
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error

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
            raise CaseVersionConflictError(case_id, expected_version, current.version)
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
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("Legacy boundary clock must be timezone-aware")
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
        if snapshot.intake_summary is None:
            raise CaseSnapshotValidationError("Transcript intake summary is missing")
        try:
            return _transcript_identity_from_summary(case_id, snapshot.intake_summary)
        except ValueError as error:
            raise CaseSnapshotValidationError(str(error)) from error

    @staticmethod
    def _version_conflict(
        error: CaseRecordVersionConflictError,
    ) -> CaseVersionConflictError:
        return CaseVersionConflictError(
            error.case_id,
            error.expected_version,
            error.current_version,
        )

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

    def commit_gate_phase(
        self,
        case_id: str,
        *,
        expected_version: int,
        decisions: tuple[GateDecision, ...],
    ) -> CaseRecord:
        self._get_case_for_update(case_id, expected_version)
        decided_at = decisions[-1].decided_at if decisions else self._aware_now()
        try:
            return self._repository.commit_gate_phase(
                case_id=case_id,
                expected_version=expected_version,
                decisions=decisions,
                updated_at=max(self._aware_now(), decided_at),
            )
        except CaseRecordNotFoundError as error:
            raise CaseNotFoundError(case_id) from error
        except CaseRecordVersionConflictError as error:
            raise self._version_conflict(error) from error
        except WorkflowAtomicityError as error:
            raise CaseSnapshotValidationError(str(error)) from error
