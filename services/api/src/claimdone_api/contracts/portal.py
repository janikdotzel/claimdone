"""Typed local-sandbox portal views and redacted receipt contracts."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, StrictStr, model_validator

from .base import (
    AlwaysFalse,
    AlwaysTrue,
    ContractModel,
    ContractVersion,
    ExactlyEight,
    ExactlyThree,
    Identifier,
    NonEmptyText,
    ShortText,
    StrictInteger,
    WireAwareDatetime,
    WireDate,
    WireTime,
)
from .enums import CounterpartyKnown, PortalState, PortalVariant

ExactlyThreePortalAttachments = Annotated[tuple[Identifier, ...], Field(min_length=3, max_length=3)]
DraftDateText = Annotated[StrictStr, Field(max_length=10)]
DraftTimeText = Annotated[StrictStr, Field(max_length=21)]
DraftShortText = Annotated[StrictStr, Field(max_length=512)]
DraftNarrativeText = Annotated[StrictStr, Field(max_length=4_000)]


class PortalWireModel(ContractModel):
    """Closed portal wire value that preserves raw whitespace for G8."""

    model_config = ConfigDict(str_strip_whitespace=False)


class PortalDraftFields(PortalWireModel):
    """Closed set of fields writable in the V1 local sandbox portal."""

    incident_date: DraftDateText
    incident_time: DraftTimeText
    location: DraftShortText
    claimant_name: DraftShortText
    policy_reference: DraftShortText
    vehicle_registration: DraftShortText
    counterparty_known: Literal[""] | CounterpartyKnown
    narrative: DraftNarrativeText
    attachments: Annotated[tuple[Identifier, ...], Field(max_length=3)]


class PortalReviewFields(ContractModel):
    """Complete values required on a freshly rendered review page."""

    incident_date: WireDate
    incident_time: WireTime
    location: ShortText
    claimant_name: ShortText
    policy_reference: ShortText
    vehicle_registration: ShortText
    counterparty_known: CounterpartyKnown
    narrative: NonEmptyText
    attachments: ExactlyThreePortalAttachments


class PortalSessionView(ContractModel):
    """Versioned portal state used for optimistic-concurrency checks."""

    contract_version: ContractVersion
    case_id: Identifier
    variant: PortalVariant
    state: Literal[PortalState.DRAFT, PortalState.REVIEW]
    version: Annotated[StrictInteger, Field(ge=1)]
    fields: PortalDraftFields
    updated_at: WireAwareDatetime
    audit_count: Annotated[StrictInteger, Field(ge=0)] | None = None


class RenderedPortalSnapshot(ContractModel):
    """Fresh review-page values consumed by independent verification."""

    contract_version: ContractVersion
    case_id: Identifier
    variant: PortalVariant
    state: Literal[PortalState.REVIEW]
    version: Annotated[StrictInteger, Field(ge=1)]
    fields: PortalDraftFields
    rendered_at: WireAwareDatetime


class SandboxReceipt(ContractModel):
    """Deliberately redacted proof of a local, human-approved sandbox action."""

    contract_version: ContractVersion
    receipt_id: Identifier
    case_id: Identifier
    approval_id: Identifier
    variant: PortalVariant
    state: Literal[PortalState.RECEIPT]
    version: Annotated[StrictInteger, Field(ge=1)]
    environment: Literal["sandbox"]
    sandbox_only: AlwaysTrue
    submitted_to_real_insurer: AlwaysFalse
    human_approved: AlwaysTrue
    redacted: AlwaysTrue
    summary: "SandboxReceiptSummary"
    approved_at: WireAwareDatetime
    rendered_at: WireAwareDatetime

    @model_validator(mode="after")
    def preserve_sandbox_boundary(self) -> Self:
        # Literal/strict aliases enforce every value. The validator keeps the
        # authority invariant explicit in generated documentation and errors.
        if not self.sandbox_only or self.submitted_to_real_insurer:
            raise ValueError("A SandboxReceipt can describe only a local sandbox action")
        if self.rendered_at < self.approved_at:
            raise ValueError("Receipt renderedAt cannot precede human approvedAt")
        return self


class SandboxReceiptSummary(ContractModel):
    """Closed redacted summary containing counts and authority flags only."""

    completed_field_count: ExactlyEight
    attachment_count: ExactlyThree
    verification_passed: AlwaysTrue
    final_action_owner: Literal["human"]
