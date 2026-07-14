"""Strict camelCase HTTP models for the INT-001 flow."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from claimdone_api.cases.models import ApiModel, CaseView
from claimdone_api.contracts import (
    GateDecision,
    RequiredClaimField,
    VerificationState,
)


class FlowPhase(StrEnum):
    AWAITING_CLARIFICATION = "awaiting_clarification"
    REVIEW = "review"


ClarificationIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^clarification-[a-f0-9]{32}$",
    ),
]


class ClarificationView(ApiModel):
    clarification_id: ClarificationIdentifier
    field: Literal[RequiredClaimField.INCIDENT_TIME]
    question: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    expected_version: Annotated[int, Field(ge=1)]


class PortalView(ApiModel):
    review_url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    rendered_values: dict[str, JsonValue]
    verification_state: Literal[VerificationState.PENDING]


class FlowResponse(ApiModel):
    request_id: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^request-[a-f0-9]{32}$",
        ),
    ]
    case: CaseView
    draft_revision: Annotated[int, Field(ge=1)]
    gate_history: Annotated[tuple[GateDecision, ...], Field(min_length=6, max_length=6)]
    phase: FlowPhase
    clarification: ClarificationView | None
    portal: PortalView | None

    @model_validator(mode="after")
    def validate_phase_payload(self) -> "FlowResponse":
        if self.draft_revision != self.case.version:
            raise ValueError("draftRevision must equal the authoritative case version")
        if self.phase is FlowPhase.AWAITING_CLARIFICATION:
            if self.clarification is None or self.portal is not None:
                raise ValueError("Clarification phase requires only a clarification payload")
            if self.clarification.expected_version != self.case.version:
                raise ValueError("Clarification expectedVersion must equal case.version")
        elif self.clarification is not None or self.portal is None:
            raise ValueError("Review phase requires only a portal payload")
        return self


class ClarificationAnswerRequest(ApiModel):
    expected_version: Annotated[int, Field(ge=1)]
    answer: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class DemoResetResponse(ApiModel):
    deleted_cases: Annotated[int, Field(ge=0)]


class PortalDraftFields(ApiModel):
    incident_date: str
    incident_time: str
    location: str
    claimant_name: str
    policy_reference: str
    vehicle_registration: str
    counterparty_known: str
    narrative: str
    attachments: tuple[str, ...]


class PortalSessionView(ApiModel):
    case_id: str
    variant: Literal["A"]
    state: str
    version: Annotated[int, Field(ge=1)]
    fields: PortalDraftFields
    audit_count: Annotated[int, Field(ge=0)]
    updated_at: str


class RenderedPortalValues(ApiModel):
    case_id: str
    state: Literal["review"]
    fields: PortalDraftFields
    rendered_at: str
