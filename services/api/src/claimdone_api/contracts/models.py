"""Canonical ClaimDone domain, gate, verification, and audit contracts."""

from datetime import date, time
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .base import (
    AlwaysFalse,
    AlwaysTrue,
    Confidence,
    ContractModel,
    ContractVersion,
    ExactlyThree,
    Identifier,
    JsonScalar,
    NonEmptyText,
    Sha256Digest,
    ShortText,
    StrictBoolean,
    StrictInteger,
)
from .enums import (
    ActorType,
    AllowedTool,
    AuditEventType,
    CaseState,
    CounterpartyKnown,
    EvidenceField,
    EvidenceKind,
    FactStatus,
    GateId,
    GateReasonCode,
    PortalState,
    RequiredClaimField,
    VerificationFieldStatus,
    VerificationState,
)
from .state_machine import InvalidCaseTransition, validate_case_transition

NonEmptyIdentifiers = Annotated[tuple[Identifier, ...], Field(min_length=1)]
ExactlyThreeIdentifiers = Annotated[tuple[Identifier, ...], Field(min_length=3, max_length=3)]
REVIEW_GATE_SEQUENCE = (
    GateId.G0_INTAKE,
    GateId.G1_PRIVACY,
    GateId.G2_OUTPUT_CONTRACT,
    GateId.G3_SAFETY_SCOPE,
    GateId.G4_PROVENANCE,
    GateId.G5_COMPLETENESS,
    GateId.G6_TOOL_AUTHORITY,
    GateId.G7_PORTAL_WRITE,
    GateId.G8_VERIFICATION,
)
HUMAN_APPROVED_GATE_SEQUENCE = (*REVIEW_GATE_SEQUENCE, GateId.G9_HUMAN_APPROVAL)
RECEIPT_GATE_SEQUENCE = (*HUMAN_APPROVED_GATE_SEQUENCE, GateId.G10_RECEIPT_REDACTION)
REQUIRED_GATE_SEQUENCE_BY_CASE_STATE: dict[CaseState, tuple[GateId, ...]] = {
    CaseState.REVIEW: REVIEW_GATE_SEQUENCE,
    CaseState.HUMAN_APPROVED: HUMAN_APPROVED_GATE_SEQUENCE,
    CaseState.RECEIPT: RECEIPT_GATE_SEQUENCE,
}


class ClaimScope(ContractModel):
    """Immutable sandbox and final-action authority boundary."""

    environment: Literal["sandbox"]
    scenario: Literal["two_vehicle_rear_end_no_injury"]
    agent_can_submit: AlwaysFalse
    final_action_owner: Literal["human"]


class EvidenceItem(ContractModel):
    """A local, content-addressed evidence input approved for the active case."""

    evidence_id: Identifier
    kind: EvidenceKind
    local_ref: Identifier
    media_type: Literal["image/jpeg", "image/png", "text/plain"]
    sha256: Sha256Digest
    text: NonEmptyText | None
    model_copy_approved: StrictBoolean

    @model_validator(mode="after")
    def validate_kind_payload(self) -> Self:
        if self.kind is EvidenceKind.IMAGE:
            if self.media_type not in {"image/jpeg", "image/png"} or self.text is not None:
                raise ValueError("Image evidence must use an image media type and contain no text")
        elif self.media_type != "text/plain" or self.text is None:
            raise ValueError("Text evidence must use text/plain and contain text")
        return self


class ProvenanceRef(ContractModel):
    """Stable pointer from a fact or claim field to one evidence item."""

    provenance_id: Identifier
    evidence_id: Identifier
    locator: ShortText | None
    user_confirmed: StrictBoolean


class EvidenceFact(ContractModel):
    """A bounded fact with explicit support status and source references."""

    fact_id: Identifier
    field: EvidenceField
    value: JsonScalar
    status: FactStatus
    source_refs: tuple[Identifier, ...]
    confidence: Confidence | None

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        supported = {FactStatus.OBSERVED, FactStatus.USER_STATED}
        if self.status in supported and (self.value is None or not self.source_refs):
            raise ValueError("Observed and user-stated facts require a value and provenance")
        if self.status in {
            FactStatus.UNKNOWN,
            FactStatus.NOT_SUPPORTED,
        } and (self.value is not None or self.confidence is not None):
            raise ValueError("Unknown and not-supported facts cannot carry a value or confidence")
        if self.status is FactStatus.OBSERVED and self.confidence is None:
            raise ValueError("Observed facts require confidence")
        if self.status is not FactStatus.OBSERVED and self.confidence is not None:
            raise ValueError("Only observed facts may carry confidence")
        return self


class FieldProvenance(ContractModel):
    """All source pointers supporting one portal-writable claim field."""

    field: RequiredClaimField
    source_refs: NonEmptyIdentifiers


class ClaimData(ContractModel):
    """The complete draft payload and deterministic required-field result."""

    incident_date: date | None
    incident_time: time | None
    location: ShortText | None
    claimant_name: ShortText | None
    policy_reference: ShortText | None
    vehicle_registration: ShortText | None
    counterparty_known: CounterpartyKnown
    narrative: NonEmptyText | None
    attachments: ExactlyThreeIdentifiers
    missing_required_fields: tuple[RequiredClaimField, ...]
    field_provenance: tuple[FieldProvenance, ...]

    @model_validator(mode="after")
    def validate_completeness_and_provenance(self) -> Self:
        nullable_fields = {
            RequiredClaimField.INCIDENT_DATE: self.incident_date,
            RequiredClaimField.INCIDENT_TIME: self.incident_time,
            RequiredClaimField.LOCATION: self.location,
            RequiredClaimField.CLAIMANT_NAME: self.claimant_name,
            RequiredClaimField.POLICY_REFERENCE: self.policy_reference,
            RequiredClaimField.VEHICLE_REGISTRATION: self.vehicle_registration,
            RequiredClaimField.NARRATIVE: self.narrative,
        }
        expected_missing = {field for field, value in nullable_fields.items() if value is None}
        listed_missing = set(self.missing_required_fields)
        if len(listed_missing) != len(self.missing_required_fields):
            raise ValueError("missingRequiredFields cannot contain duplicates")
        if listed_missing != expected_missing:
            raise ValueError("missingRequiredFields must exactly match null required fields")

        provenance_fields = [entry.field for entry in self.field_provenance]
        if len(set(provenance_fields)) != len(provenance_fields):
            raise ValueError("Each claim field may have only one provenance entry")
        expected_provenance = set(RequiredClaimField) - expected_missing
        if set(provenance_fields) != expected_provenance:
            raise ValueError(
                "Every populated required field must have exactly one provenance entry"
            )
        return self


class PlanStep(ContractModel):
    """One visible, bounded tool selection."""

    sequence: Annotated[StrictInteger, Field(ge=1, le=40)]
    tool: AllowedTool
    reason: ShortText


class ToolPlan(ContractModel):
    """Ordered plan whose type surface cannot express submission authority."""

    agent_can_submit: AlwaysFalse
    steps: Annotated[tuple[PlanStep, ...], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        sequence = tuple(step.sequence for step in self.steps)
        if sequence != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("Plan steps must use contiguous one-based sequence numbers")
        return self


class GateDecision(ContractModel):
    """Immutable gate event; model signals can only add a block."""

    contract_version: ContractVersion
    gate_id: GateId
    deterministic_passed: StrictBoolean
    model_blocked: StrictBoolean
    passed: StrictBoolean
    reason_codes: tuple[GateReasonCode, ...]
    evidence_refs: tuple[Identifier, ...]
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def prevent_override(self) -> Self:
        expected_passed = self.deterministic_passed and not self.model_blocked
        if self.passed is not expected_passed:
            raise ValueError("passed must equal deterministicPassed AND NOT modelBlocked")
        if self.passed and self.reason_codes:
            raise ValueError("A passed gate cannot contain blocking reason codes")
        if not self.passed and not self.reason_codes:
            raise ValueError("A failed gate requires at least one reason code")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Gate reason codes cannot contain duplicates")
        expected_prefix = f"{self.gate_id.value}_"
        if any(not reason.value.startswith(expected_prefix) for reason in self.reason_codes):
            raise ValueError("Every reason code must belong to the selected gate")
        if self.model_blocked and self.gate_id not in {
            GateId.G3_SAFETY_SCOPE,
            GateId.G8_VERIFICATION,
        }:
            raise ValueError("Only G3 and G8 may receive an additional model block")
        return self


class VerificationFieldResult(ContractModel):
    """Fresh rendered-value comparison for one required portal field."""

    field: RequiredClaimField
    expected: JsonScalar
    actual: JsonScalar
    status: VerificationFieldStatus
    source_refs: NonEmptyIdentifiers

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        values_match = type(self.expected) is type(self.actual) and self.expected == self.actual
        if self.status is VerificationFieldStatus.MATCH and not values_match:
            raise ValueError("A match result requires equal values of the same type")
        if self.status is VerificationFieldStatus.MISMATCH and values_match:
            raise ValueError("A mismatch result requires different values")
        if self.status is VerificationFieldStatus.MISSING and (
            self.actual is not None or self.expected is None
        ):
            raise ValueError("A missing result requires a non-null expected value and null actual")
        return self


class VerificationReport(ContractModel):
    """Deterministic comparison plus an optional additional model block."""

    status: VerificationState
    deterministic_match: StrictBoolean | None
    model_reported_mismatch: StrictBoolean
    field_results: tuple[VerificationFieldResult, ...]
    expected_attachment_count: ExactlyThree
    actual_attachment_count: Annotated[StrictInteger, Field(ge=0)] | None
    review_allowed: StrictBoolean
    verified_at: AwareDatetime | None

    @model_validator(mode="after")
    def prevent_verification_override(self) -> Self:
        field_names = [result.field for result in self.field_results]
        if len(set(field_names)) != len(field_names):
            raise ValueError("Verification field results must be unique")
        required_fields = set(RequiredClaimField) - {RequiredClaimField.ATTACHMENTS}
        fields_complete = set(field_names) == required_fields
        field_mismatch = any(
            result.status is not VerificationFieldStatus.MATCH for result in self.field_results
        )
        fields_match = fields_complete and all(
            result.status is VerificationFieldStatus.MATCH for result in self.field_results
        )
        attachments_evaluated = self.actual_attachment_count is not None
        attachments_match = self.actual_attachment_count == self.expected_attachment_count
        attachment_mismatch = attachments_evaluated and not attachments_match
        deterministic_inputs_complete = fields_complete and attachments_evaluated
        if deterministic_inputs_complete:
            derived_deterministic_match = fields_match and attachments_match
            if self.deterministic_match is not derived_deterministic_match:
                raise ValueError(
                    "deterministicMatch must be derived from all fields and attachments"
                )
        else:
            if self.deterministic_match is True:
                raise ValueError("Partial verification cannot set deterministicMatch to true")
            observed_mismatch = field_mismatch or attachment_mismatch
            if self.deterministic_match is False and not observed_mismatch:
                raise ValueError("Partial verification can fail only after observing a mismatch")
            if self.deterministic_match is None and observed_mismatch:
                raise ValueError("An observed deterministic mismatch must be recorded as false")
        expected_review_allowed = (
            self.status is VerificationState.VERIFIED
            and self.deterministic_match is True
            and not self.model_reported_mismatch
            and fields_match
            and attachments_match
        )
        if self.review_allowed is not expected_review_allowed:
            raise ValueError("reviewAllowed must be derived from deterministic verification")
        if self.status is VerificationState.PENDING:
            if (
                self.deterministic_match is not None
                or self.verified_at is not None
                or self.field_results
                or self.actual_attachment_count is not None
                or self.model_reported_mismatch
            ):
                raise ValueError(
                    "Pending verification cannot contain results, signals, or a timestamp"
                )
        elif self.verified_at is None:
            raise ValueError("Completed verification requires verifiedAt")
        if self.status is VerificationState.VERIFIED and not expected_review_allowed:
            raise ValueError("Verified status requires every deterministic and model check to pass")
        if self.status is VerificationState.MISMATCH:
            has_mismatch = (
                self.deterministic_match is False
                or self.model_reported_mismatch
                or field_mismatch
                or attachment_mismatch
            )
            if not has_mismatch:
                raise ValueError("Mismatch status requires at least one mismatch signal")
        return self


class AuditDetail(ContractModel):
    """A deliberately redacted audit attribute."""

    key: Identifier
    value_summary: ShortText
    redacted: AlwaysTrue


class AuditEvent(ContractModel):
    """Redacted event suitable for persistence and the UI event strip."""

    contract_version: ContractVersion
    event_id: Identifier
    case_id: Identifier
    event_type: AuditEventType
    actor: ActorType
    occurred_at: AwareDatetime
    from_state: CaseState | None
    to_state: CaseState | None
    reason_codes: tuple[GateReasonCode, ...]
    details: tuple[AuditDetail, ...]

    @model_validator(mode="after")
    def validate_state_event(self) -> Self:
        has_from = self.from_state is not None
        has_to = self.to_state is not None
        if has_from is not has_to:
            raise ValueError("fromState and toState must either both be present or both be null")
        if self.event_type is AuditEventType.CASE_STATE_CHANGED:
            if self.from_state is None or self.to_state is None:
                raise ValueError("State-change events require fromState and toState")
            try:
                validate_case_transition(self.from_state, self.to_state)
            except InvalidCaseTransition as error:
                raise ValueError(str(error)) from error
        elif has_from:
            raise ValueError("Only case_state_changed events may carry state transitions")
        if self.to_state is CaseState.HUMAN_APPROVED and self.actor is not ActorType.HUMAN:
            raise ValueError("Only a human actor may transition a case to human_approved")
        if self.event_type is AuditEventType.HUMAN_APPROVAL and self.actor is not ActorType.HUMAN:
            raise ValueError("human_approval events require a human actor")
        return self


class ClaimPacket(ContractModel):
    """Canonical evidence-linked claim draft consumed by every subsystem."""

    contract_version: ContractVersion
    case_id: Identifier
    state: CaseState
    portal_state: PortalState
    scope: ClaimScope
    evidence: Annotated[tuple[EvidenceItem, ...], Field(min_length=4)]
    provenance: Annotated[tuple[ProvenanceRef, ...], Field(min_length=1)]
    facts: tuple[EvidenceFact, ...]
    claim: ClaimData
    plan: ToolPlan
    gate_decisions: tuple[GateDecision, ...]
    verification: VerificationReport

    @model_validator(mode="after")
    def validate_cross_references_and_state(self) -> Self:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("Evidence IDs must be unique")
        images = tuple(item for item in self.evidence if item.kind is EvidenceKind.IMAGE)
        if len(images) != 3:
            raise ValueError("ClaimPacket requires exactly three image evidence items")
        if tuple(item.local_ref for item in images) != self.claim.attachments:
            raise ValueError("Claim attachments must exactly match the three image local refs")

        provenance_ids = tuple(reference.provenance_id for reference in self.provenance)
        if len(set(provenance_ids)) != len(provenance_ids):
            raise ValueError("Provenance IDs must be unique")
        if any(reference.evidence_id not in evidence_ids for reference in self.provenance):
            raise ValueError("Every provenance entry must reference existing evidence")
        known_provenance = set(provenance_ids)
        if any(
            source not in known_provenance for fact in self.facts for source in fact.source_refs
        ):
            raise ValueError("Every fact source must reference existing provenance")
        if any(
            source not in known_provenance
            for field in self.claim.field_provenance
            for source in field.source_refs
        ):
            raise ValueError("Every claim-field source must reference existing provenance")

        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("Fact IDs must be unique")
        gate_ids = tuple(decision.gate_id for decision in self.gate_decisions)
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("A ClaimPacket may contain at most one decision per gate")
        required_gate_sequence = REQUIRED_GATE_SEQUENCE_BY_CASE_STATE.get(self.state)
        if required_gate_sequence is not None and gate_ids != required_gate_sequence:
            expected = ", ".join(gate.value for gate in required_gate_sequence)
            raise ValueError(
                f"Case state {self.state.value} requires exact passed gate sequence: {expected}"
            )
        if any(not decision.passed for decision in self.gate_decisions) and self.state in {
            CaseState.REVIEW,
            CaseState.HUMAN_APPROVED,
            CaseState.RECEIPT,
        }:
            raise ValueError("Review and later states cannot contain a failed gate decision")

        if (
            self.state
            in {
                CaseState.REVIEW,
                CaseState.HUMAN_APPROVED,
                CaseState.RECEIPT,
            }
            and not self.verification.review_allowed
        ):
            raise ValueError("Review and later states require successful verification")
        allowed_portal_states = {
            CaseState.CREATED: {PortalState.DRAFT},
            CaseState.DISCLOSED: {PortalState.DRAFT},
            CaseState.ANALYZING: {PortalState.DRAFT},
            CaseState.AWAITING_CLARIFICATION: {PortalState.DRAFT},
            CaseState.READY_TO_FILL: {PortalState.DRAFT},
            CaseState.FILLING: {PortalState.DRAFT},
            CaseState.VERIFYING: {PortalState.REVIEW},
            CaseState.REVIEW: {PortalState.REVIEW},
            CaseState.BLOCKED: {PortalState.DRAFT, PortalState.REVIEW},
            CaseState.HUMAN_APPROVED: {PortalState.HUMAN_APPROVED},
            CaseState.RECEIPT: {PortalState.RECEIPT},
            CaseState.EMERGENCY_STOPPED: {PortalState.DRAFT, PortalState.REVIEW},
            CaseState.ABANDONED: {PortalState.DRAFT, PortalState.REVIEW},
            CaseState.FAILED: {PortalState.DRAFT, PortalState.REVIEW},
        }[self.state]
        if self.portal_state not in allowed_portal_states:
            allowed = ", ".join(sorted(state.value for state in allowed_portal_states))
            raise ValueError(f"Case state {self.state.value} requires portal state in: {allowed}")
        return self
