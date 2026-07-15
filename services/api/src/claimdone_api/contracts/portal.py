"""Typed local-sandbox portal views and redacted receipt contracts."""

import re
from datetime import date, time
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from .base import (
    AlwaysFalse,
    AlwaysTrue,
    ContractModel,
    ContractVersion,
    ExactIdentifier,
    ExactlyEight,
    ExactlyThree,
    ExactlyThreeAttachmentIdentifiers,
    Identifier,
    NonEmptyText,
    ShortText,
    StrictInteger,
    UpToThreeAttachmentIdentifiers,
    WireAwareDatetime,
    WireDate,
    WireTime,
)
from .enums import CounterpartyKnown, PortalState, PortalVariant

DraftDateText = Annotated[StrictStr, Field(max_length=10)]
DraftTimeText = Annotated[StrictStr, Field(max_length=21)]
DraftShortText = Annotated[StrictStr, Field(max_length=512)]
DraftNarrativeText = Annotated[StrictStr, Field(max_length=4_000)]
PortalRunDateText = Annotated[
    StrictStr,
    Field(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$"),
]
PortalRunTimeText = Annotated[
    StrictStr,
    Field(
        min_length=8,
        max_length=15,
        pattern=r"^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$",
    ),
]
PortalRunShortText = Annotated[StrictStr, Field(min_length=1, max_length=512)]
PortalRunNarrativeText = Annotated[StrictStr, Field(min_length=1, max_length=4_000)]
_PORTAL_RUN_ATTACHMENT_PATTERN = re.compile(
    r"^(?:model-[a-f0-9]{32}\.(?:jpg|png)|"
    r"asset-demo-[a-z0-9]+(?:-[a-z0-9]+)*)$"
)


def _require_portal_run_attachment_identifier(value: object) -> object:
    if type(value) is not str or _PORTAL_RUN_ATTACHMENT_PATTERN.fullmatch(value) is None:
        raise ValueError("portal run attachment identifier is not server-approved")
    return value


PortalRunAttachmentIdentifier = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=128,
        pattern=_PORTAL_RUN_ATTACHMENT_PATTERN.pattern,
    ),
    BeforeValidator(_require_portal_run_attachment_identifier),
]


def _require_unique_portal_run_attachments(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(value)) != len(value):
        raise ValueError("portal run attachment identifiers must be unique")
    return value


PortalRunAttachmentIdentifiers = Annotated[
    tuple[PortalRunAttachmentIdentifier, ...],
    Field(min_length=3, max_length=3, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_require_unique_portal_run_attachments),
]
PortalScalarField = Literal[
    "incident_date",
    "incident_time",
    "location",
    "claimant_name",
    "policy_reference",
    "vehicle_registration",
    "counterparty_known",
    "narrative",
]
_PORTAL_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PORTAL_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$")


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
    attachments: UpToThreeAttachmentIdentifiers


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
    attachments: ExactlyThreeAttachmentIdentifiers


class PortalRunExpectedFields(PortalWireModel):
    """Complete, byte-preserving values bound to one local portal run."""

    incident_date: PortalRunDateText
    incident_time: PortalRunTimeText
    location: PortalRunShortText
    claimant_name: PortalRunShortText
    policy_reference: PortalRunShortText
    vehicle_registration: PortalRunShortText
    counterparty_known: CounterpartyKnown
    narrative: PortalRunNarrativeText
    attachments: PortalRunAttachmentIdentifiers

    @model_validator(mode="after")
    def require_review_complete_values(self) -> Self:
        values = (
            self.incident_date,
            self.incident_time,
            self.location,
            self.claimant_name,
            self.policy_reference,
            self.vehicle_registration,
            self.narrative,
        )
        if any(not value.strip() for value in values):
            raise ValueError("expected portal scalar values must be complete")
        if _PORTAL_DATE_PATTERN.fullmatch(self.incident_date) is None:
            raise ValueError("expected portal incidentDate must use YYYY-MM-DD")
        if _PORTAL_TIME_PATTERN.fullmatch(self.incident_time) is None:
            raise ValueError("expected portal incidentTime must include seconds")
        try:
            date.fromisoformat(self.incident_date)
            time.fromisoformat(self.incident_time)
        except ValueError as error:
            raise ValueError("expected portal date and time must be valid") from error
        return self


class PortalRunSetup(PortalWireModel):
    """Server-only setup command for one packet-bound local portal run."""

    contract_version: ContractVersion
    run_id: ExactIdentifier
    case_id: ExactIdentifier
    variant: PortalVariant
    expected_fields: PortalRunExpectedFields


class PortalRunRelease(PortalWireModel):
    """Identity binding used to release or abort one local portal run."""

    contract_version: ContractVersion
    run_id: ExactIdentifier
    case_id: ExactIdentifier
    variant: PortalVariant


class PortalRunRenderFaultInjection(PortalWireModel):
    """Arm one non-sensitive scalar render mismatch for local verification."""

    contract_version: ContractVersion
    run_id: ExactIdentifier
    case_id: ExactIdentifier
    variant: PortalVariant
    expected_version: Annotated[StrictInteger, Field(ge=1)]
    field: PortalScalarField


class PortalRunRenderFaultRepair(PortalWireModel):
    """Close the one render mismatch bound to a local verification run."""

    contract_version: ContractVersion
    run_id: ExactIdentifier
    case_id: ExactIdentifier
    variant: PortalVariant
    expected_version: Annotated[StrictInteger, Field(ge=1)]
    field: PortalScalarField


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
