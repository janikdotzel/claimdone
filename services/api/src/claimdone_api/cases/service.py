"""Validated case workflow and persistence orchestration."""

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
    validate_case_transition,
)
from claimdone_api.contracts.state_machine import InvalidCaseTransition
from claimdone_api.persistence import (
    CaseRecord,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    CaseSnapshot,
    SequencedAuditEvent,
    SequencedGateDecision,
    SqliteCaseRepository,
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
    def _version_conflict(error: CaseRecordVersionConflictError) -> CaseVersionConflictError:
        return CaseVersionConflictError(
            error.case_id,
            error.expected_version,
            error.current_version,
        )
