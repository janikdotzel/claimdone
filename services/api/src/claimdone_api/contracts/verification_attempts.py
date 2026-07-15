"""Immutable bounded verification-attempt and narrow-repair contracts."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    ContractModel,
    ContractVersion,
    ExactlyOne,
    Identifier,
    OneOrTwo,
    StrictBoolean,
    StrictInteger,
)
from .enums import (
    CaseState,
    GateId,
    GateReasonCode,
    RequiredClaimField,
    VerificationFieldStatus,
    VerificationState,
)
from .models import GateDecision, VerificationReport

ScalarRepairField = Literal[
    RequiredClaimField.INCIDENT_DATE,
    RequiredClaimField.INCIDENT_TIME,
    RequiredClaimField.LOCATION,
    RequiredClaimField.CLAIMANT_NAME,
    RequiredClaimField.POLICY_REFERENCE,
    RequiredClaimField.VEHICLE_REGISTRATION,
    RequiredClaimField.COUNTERPARTY_KNOWN,
    RequiredClaimField.NARRATIVE,
]


class VerificationRepairMetadata(ContractModel):
    """Single-field, single-use repair authorization derived from trusted evidence."""

    repair_number: ExactlyOne
    field: ScalarRepairField
    source_refs: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    from_portal_version: Annotated[StrictInteger, Field(ge=1)]
    to_portal_version: Annotated[StrictInteger, Field(ge=2)]

    @model_validator(mode="after")
    def validate_version_increment(self) -> Self:
        if self.to_portal_version != self.from_portal_version + 1:
            raise ValueError("A repair must increment the portal version exactly once")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Repair sourceRefs cannot contain duplicates")
        return self


class VerificationAttempt(ContractModel):
    """One of at most two attempts; only a final attempt may carry G8."""

    contract_version: ContractVersion
    attempt_id: Identifier
    case_id: Identifier
    attempt_number: OneOrTwo
    case_state: Literal[CaseState.VERIFYING]
    portal_version: Annotated[StrictInteger, Field(ge=1)]
    report: VerificationReport
    final: StrictBoolean
    repair: VerificationRepairMetadata | None
    repaired_from_attempt_id: Identifier | None
    gate_decision: GateDecision | None

    @model_validator(mode="after")
    def validate_bounded_repair_and_final_gate(self) -> Self:
        if self.report.status is VerificationState.PENDING:
            raise ValueError("VerificationAttempt requires a completed report")

        if self.final:
            if self.gate_decision is None:
                raise ValueError("A final verification attempt requires its G8 decision")
            if self.gate_decision.gate_id is not GateId.G8_VERIFICATION:
                raise ValueError("Only G8 may finalize a verification attempt")
            if self.gate_decision.passed is not self.report.review_allowed:
                raise ValueError("The final G8 result must match report.reviewAllowed")
            if (
                self.report.verified_at is not None
                and self.gate_decision.decided_at < self.report.verified_at
            ):
                raise ValueError("Final G8 cannot precede its verification report")
            expected_reasons: list[GateReasonCode] = []
            field_results = self.report.field_results
            expected_fields = set(RequiredClaimField) - {RequiredClaimField.ATTACHMENTS}
            present_fields = {result.field for result in field_results}
            if any(result.status is VerificationFieldStatus.MISMATCH for result in field_results):
                expected_reasons.append(GateReasonCode.G8_FIELD_MISMATCH)
            required_missing = (
                present_fields != expected_fields
                or any(result.status is VerificationFieldStatus.MISSING for result in field_results)
                or self.report.actual_attachment_count is None
                or self.report.actual_attachment_ids is None
            )
            if (
                self.report.actual_attachment_ids is not None
                and self.report.actual_attachment_ids != self.report.expected_attachment_ids
            ):
                expected_reasons.append(GateReasonCode.G8_ATTACHMENT_MISMATCH)
            if required_missing:
                expected_reasons.append(GateReasonCode.G8_REQUIRED_FIELD_MISSING)
            if self.report.model_reported_mismatch:
                expected_reasons.append(GateReasonCode.G8_MODEL_MISMATCH)
            if self.gate_decision.reason_codes != tuple(expected_reasons):
                raise ValueError("The final G8 reasons must be derived from the report")
            if not self.report.review_allowed and not expected_reasons:
                raise ValueError("A failed final report must yield a deterministic G8 reason")
        elif self.gate_decision is not None:
            raise ValueError("A non-final repairable attempt cannot emit G8")

        if self.attempt_number == 1:
            if self.repaired_from_attempt_id is not None:
                raise ValueError("The first verification attempt cannot reference an earlier one")
        else:
            if self.repaired_from_attempt_id is None:
                raise ValueError("The second verification attempt must reference attempt one")
            if self.repair is not None:
                raise ValueError("The final second attempt cannot authorize another repair")
            if not self.final:
                raise ValueError("The second and last verification attempt must be final")

        if self.repair is None:
            if not self.final:
                raise ValueError("A non-final attempt requires one narrow repair authorization")
            return self

        if self.attempt_number != 1 or self.final:
            raise ValueError("Only a non-final first attempt may authorize a repair")
        if self.report.status is not VerificationState.MISMATCH:
            raise ValueError("A repair requires a deterministic mismatch report")
        if self.report.deterministic_match is not False:
            raise ValueError("A repair requires deterministicMatch=false")
        if self.report.model_reported_mismatch:
            raise ValueError("A model-only signal cannot authorize a deterministic repair")
        if self.report.actual_attachment_ids != self.report.expected_attachment_ids:
            raise ValueError("Attachment mismatch cannot be repaired through a scalar field")
        expected_fields = set(RequiredClaimField) - {RequiredClaimField.ATTACHMENTS}
        if {result.field for result in self.report.field_results} != expected_fields:
            raise ValueError("Repair authorization requires a complete scalar comparison")
        non_matching = tuple(
            result
            for result in self.report.field_results
            if result.status is not VerificationFieldStatus.MATCH
        )
        if len(non_matching) != 1 or non_matching[0].field is not self.repair.field:
            raise ValueError("Repair must target the only mismatching scalar field")
        if non_matching[0].source_refs != self.repair.source_refs:
            raise ValueError("Repair sourceRefs must match the mismatching field provenance")
        if self.repair.from_portal_version != self.portal_version:
            raise ValueError("Repair must begin at the verified portal version")
        return self


class VerificationAttemptSeries(ContractModel):
    """Complete one- or two-attempt chain proving a narrow single repair."""

    contract_version: ContractVersion
    case_id: Identifier
    attempts: Annotated[tuple[VerificationAttempt, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def validate_attempt_chain(self) -> Self:
        if any(attempt.case_id != self.case_id for attempt in self.attempts):
            raise ValueError("Every verification attempt must belong to series.caseId")
        if len({attempt.attempt_id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("Verification attempt IDs must be unique")
        numbers = tuple(attempt.attempt_number for attempt in self.attempts)
        if numbers != tuple(range(1, len(self.attempts) + 1)):
            raise ValueError("Verification attempts must be contiguous and one-based")

        first = self.attempts[0]
        if len(self.attempts) == 1:
            if not first.final:
                raise ValueError("A non-final first attempt requires its repaired second attempt")
            return self

        second = self.attempts[1]
        if first.final or first.repair is None:
            raise ValueError("A second attempt requires a non-final repair authorization")
        if second.repaired_from_attempt_id != first.attempt_id:
            raise ValueError("The second attempt must reference the first attempt ID")
        if second.portal_version != first.repair.to_portal_version:
            raise ValueError("The repaired attempt must verify the authorized portal version")
        if (
            first.report.verified_at is None
            or second.report.verified_at is None
            or second.report.verified_at <= first.report.verified_at
        ):
            raise ValueError("Repaired verification must not precede the first attempt")

        first_results = {result.field: result for result in first.report.field_results}
        second_results = {result.field: result for result in second.report.field_results}
        if set(first_results) != set(second_results):
            raise ValueError("Repair verification must compare the same field set")
        for field, first_result in first_results.items():
            second_result = second_results[field]
            if (
                type(first_result.expected) is not type(second_result.expected)
                or first_result.expected != second_result.expected
                or first_result.source_refs != second_result.source_refs
            ):
                raise ValueError("Repair cannot change expected values or provenance")
            if field is not first.repair.field and first_result != second_result:
                raise ValueError("Repair cannot change a non-target rendered field")
        if (
            first.report.expected_attachment_count != second.report.expected_attachment_count
            or first.report.actual_attachment_count != second.report.actual_attachment_count
            or first.report.expected_attachment_ids != second.report.expected_attachment_ids
            or first.report.actual_attachment_ids != second.report.actual_attachment_ids
        ):
            raise ValueError("Scalar repair cannot change attachment verification")
        return self
