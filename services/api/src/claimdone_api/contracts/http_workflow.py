"""Closed HTTP workflow roots for canonical frontend/backend exchange."""

from copy import deepcopy
from typing import Annotated, Literal, Self, cast

from pydantic import (
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from .base import (
    ContractModel,
    ContractVersion,
    Identifier,
    OneToThree,
    ShortText,
    StrictInteger,
    WireAwareDatetime,
)
from .enums import (
    CaseState,
    ClarificationStatus,
    PortalState,
    RequiredClaimField,
    VerificationState,
)
from .models import ClaimPacket
from .portal import PortalSessionView, SandboxReceipt
from .transcript import TranscriptConfirmationView
from .verification_attempts import VerificationAttemptSeries

ExactUserAnswer = Annotated[StrictStr, Field(min_length=1, max_length=4_000)]
_TERMINAL_STOP_STATES = frozenset(
    {
        CaseState.BLOCKED,
        CaseState.EMERGENCY_STOPPED,
        CaseState.ABANDONED,
        CaseState.FAILED,
    }
)
_PRE_EXTRACTION_STATES = frozenset(
    {
        CaseState.CREATED,
        CaseState.DISCLOSED,
        CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
    }
)
_PACKET_REQUIRED_STATES = frozenset(
    {
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.VERIFYING,
        CaseState.REVIEW,
        CaseState.HUMAN_APPROVED,
    }
)
_PORTAL_ALLOWED_STATES = frozenset(
    {
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.VERIFYING,
        CaseState.REVIEW,
        *_TERMINAL_STOP_STATES,
    }
)
_VERIFICATION_ALLOWED_STATES = frozenset(
    {
        CaseState.VERIFYING,
        CaseState.REVIEW,
        *_TERMINAL_STOP_STATES,
    }
)
_PORTAL_CLAIM_FIELD_NAMES = frozenset(
    {
        "incident_date",
        "incident_time",
        "location",
        "claimant_name",
        "policy_reference",
        "vehicle_registration",
        "counterparty_known",
        "narrative",
        "attachments",
    }
)

type _Presence = Literal["null", "required", "optional"]
type _PortalPresence = Literal[
    "null", "draft", "draft_optional", "review", "optional"
]


def _without_metadata(schema: JsonSchemaValue) -> JsonSchemaValue:
    copied = deepcopy(schema)
    copied.pop("title", None)
    copied.pop("description", None)
    return copied


def _non_null_schema(schema: JsonSchemaValue) -> JsonSchemaValue:
    options = schema.get("anyOf")
    if isinstance(options, list):
        non_null = [
            option
            for option in options
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        if len(non_null) == 1:
            return cast(JsonSchemaValue, deepcopy(non_null[0]))
    if schema.get("type") != "null":
        return deepcopy(schema)
    raise ValueError("Expected a nullable schema with exactly one non-null branch")


def _refined_object_schema(
    schema: JsonSchemaValue,
    properties: dict[str, JsonSchemaValue],
) -> JsonSchemaValue:
    return {
        "allOf": [
            _non_null_schema(schema),
            {
                "properties": properties,
                "required": list(properties),
                "type": "object",
            },
        ]
    }


def _nullable_schema(schema: JsonSchemaValue) -> JsonSchemaValue:
    return {"anyOf": [schema, {"type": "null"}]}


def _presence_schema(
    source: JsonSchemaValue,
    presence: _Presence,
    *,
    refined: JsonSchemaValue | None = None,
) -> JsonSchemaValue:
    if presence == "null":
        return {"type": "null"}
    non_null = refined if refined is not None else _non_null_schema(source)
    return non_null if presence == "required" else _nullable_schema(non_null)


def _snapshot_schema_variant(
    base_schema: JsonSchemaValue,
    *,
    state: CaseState,
    packet: _Presence,
    portal: _PortalPresence,
    verification: _Presence,
) -> JsonSchemaValue:
    variant = _without_metadata(base_schema)
    properties = cast(dict[str, JsonSchemaValue], variant["properties"])
    source_properties = cast(dict[str, JsonSchemaValue], base_schema["properties"])
    camel_case = "claimPacket" in source_properties
    packet_key = "claimPacket" if camel_case else "claim_packet"
    transcript_key = "transcriptConfirmation" if camel_case else "transcript_confirmation"
    portal_key = "portalSession" if camel_case else "portal_session"
    verification_key = "verificationAttempts" if camel_case else "verification_attempts"
    portal_state_key = "portalState" if camel_case else "portal_state"

    properties["case"] = _refined_object_schema(
        source_properties["case"],
        {"state": {"const": state.value, "type": "string"}},
    )

    packet_properties: dict[str, JsonSchemaValue] = {
        "state": {"const": state.value, "type": "string"}
    }
    if state in {
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.ANALYZING,
    }:
        packet_properties[portal_state_key] = {"const": PortalState.DRAFT.value}
    elif state in {CaseState.VERIFYING, CaseState.REVIEW}:
        packet_properties[portal_state_key] = {"const": PortalState.REVIEW.value}
    elif state is CaseState.HUMAN_APPROVED:
        packet_properties[portal_state_key] = {
            "const": PortalState.HUMAN_APPROVED.value
        }
    refined_packet = _refined_object_schema(
        source_properties[packet_key], packet_properties
    )
    properties[packet_key] = _presence_schema(
        source_properties[packet_key], packet, refined=refined_packet
    )

    transcript_presence: _Presence = (
        "required"
        if state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
        else "null"
    )
    properties[transcript_key] = _presence_schema(
        source_properties[transcript_key], transcript_presence
    )
    clarification_presence: _Presence = (
        "required" if state is CaseState.AWAITING_CLARIFICATION else "null"
    )
    properties["clarification"] = _presence_schema(
        source_properties["clarification"], clarification_presence
    )

    if portal == "null":
        properties[portal_key] = {"type": "null"}
    elif portal == "optional":
        properties[portal_key] = deepcopy(source_properties[portal_key])
    else:
        portal_state = "draft" if portal == "draft_optional" else portal
        refined_portal = _refined_object_schema(
            source_properties[portal_key],
            {"state": {"const": portal_state, "type": "string"}},
        )
        properties[portal_key] = (
            _nullable_schema(refined_portal)
            if portal == "draft_optional"
            else refined_portal
        )

    properties[verification_key] = _presence_schema(
        source_properties[verification_key], verification
    )
    receipt_presence: _Presence = (
        "required" if state is CaseState.RECEIPT else "null"
    )
    properties["receipt"] = _presence_schema(
        source_properties["receipt"], receipt_presence
    )
    return variant


def _snapshot_schema_variants(base_schema: JsonSchemaValue) -> list[JsonSchemaValue]:
    variants: list[JsonSchemaValue] = []
    for state in CaseState:
        if state in _TERMINAL_STOP_STATES:
            variants.append(
                _snapshot_schema_variant(
                    base_schema,
                    state=state,
                    packet="null",
                    portal="optional",
                    verification="null",
                )
            )
            variants.append(
                _snapshot_schema_variant(
                    base_schema,
                    state=state,
                    packet="required",
                    portal="optional",
                    verification="optional",
                )
            )
            continue

        packet: _Presence
        if state in _PACKET_REQUIRED_STATES:
            packet = "required"
        elif state in _PRE_EXTRACTION_STATES or state is CaseState.RECEIPT:
            packet = "null"
        else:
            packet = "optional"

        portal: _PortalPresence = "null"
        if state in {CaseState.READY_TO_FILL, CaseState.FILLING}:
            portal = "draft_optional"
        elif state in {CaseState.VERIFYING, CaseState.REVIEW}:
            portal = "review"

        verification: _Presence = "null"
        if state is CaseState.VERIFYING:
            verification = "optional"
        elif state is CaseState.REVIEW:
            verification = "required"

        variants.append(
            _snapshot_schema_variant(
                base_schema,
                state=state,
                packet=packet,
                portal=portal,
                verification=verification,
            )
        )
    return variants


class WorkflowCaseView(ContractModel):
    """Versioned case identity and state without legacy free-form snapshots."""

    contract_version: ContractVersion
    case_id: Identifier
    state: CaseState
    version: Annotated[StrictInteger, Field(ge=1)]
    created_at: WireAwareDatetime
    updated_at: WireAwareDatetime

    @model_validator(mode="after")
    def require_monotonic_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updatedAt cannot precede createdAt")
        return self


class ClarificationView(ContractModel):
    """One active typed clarification without the user's answer value."""

    contract_version: ContractVersion
    clarification_id: Identifier
    case_id: Identifier
    field: RequiredClaimField
    round: OneToThree
    question: ShortText
    status: Literal[ClarificationStatus.REQUESTED]
    expected_version: Annotated[StrictInteger, Field(ge=1)]
    requested_at: WireAwareDatetime


class ClarificationAnswerRequest(ContractModel):
    """Exact user answer bound to the clarification and optimistic case version."""

    model_config = ConfigDict(str_strip_whitespace=False)

    contract_version: ContractVersion
    case_id: Identifier
    clarification_id: Identifier
    field: RequiredClaimField
    round: OneToThree
    expected_version: Annotated[StrictInteger, Field(ge=1)]
    answer: ExactUserAnswer

    @field_validator("answer")
    @classmethod
    def reject_blank_answer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer cannot be empty or whitespace-only")
        return value


class WorkflowSnapshot(ContractModel):
    """Canonical HTTP snapshot with state-bound, closed nested payloads."""

    contract_version: ContractVersion
    request_id: Identifier
    case: WorkflowCaseView
    claim_packet: ClaimPacket | None
    transcript_confirmation: TranscriptConfirmationView | None
    clarification: ClarificationView | None
    portal_session: PortalSessionView | None
    verification_attempts: VerificationAttemptSeries | None
    receipt: SandboxReceipt | None

    @model_validator(mode="after")
    def bind_snapshot_to_case_state(self) -> Self:
        case_id = self.case.case_id
        state = self.case.state

        if self.claim_packet is not None:
            if self.claim_packet.case_id != case_id:
                raise ValueError("ClaimPacket.caseId must match WorkflowCaseView.caseId")
            if self.claim_packet.state is not state:
                raise ValueError("ClaimPacket.state must match WorkflowCaseView.state")
        if self.transcript_confirmation is not None:
            if self.transcript_confirmation.case_id != case_id:
                raise ValueError("Transcript confirmation caseId must match the workflow case")
            if self.transcript_confirmation.version != self.case.version:
                raise ValueError("Transcript confirmation version must equal case.version")
        if self.clarification is not None:
            if self.clarification.case_id != case_id:
                raise ValueError("Clarification caseId must match the workflow case")
            if self.clarification.expected_version != self.case.version:
                raise ValueError("Clarification expectedVersion must equal case.version")
            if not (
                self.case.created_at
                <= self.clarification.requested_at
                <= self.case.updated_at
            ):
                raise ValueError("Clarification requestedAt must fall within the case lifetime")
        if self.portal_session is not None and self.portal_session.case_id != case_id:
            raise ValueError("PortalSessionView.caseId must match the workflow case")
        if (
            self.verification_attempts is not None
            and self.verification_attempts.case_id != case_id
        ):
            raise ValueError("VerificationAttemptSeries.caseId must match the workflow case")
        if self.receipt is not None and self.receipt.case_id != case_id:
            raise ValueError("SandboxReceipt.caseId must match the workflow case")

        if state is CaseState.RECEIPT:
            if self.receipt is None:
                raise ValueError("receipt state requires a redacted SandboxReceipt")
            if any(
                value is not None
                for value in (
                    self.claim_packet,
                    self.transcript_confirmation,
                    self.clarification,
                    self.portal_session,
                    self.verification_attempts,
                )
            ):
                raise ValueError("receipt state exposes only the redacted SandboxReceipt")
            return self
        if self.receipt is not None:
            raise ValueError("SandboxReceipt is allowed only in receipt state")

        if state in _PACKET_REQUIRED_STATES and self.claim_packet is None:
            raise ValueError(f"{state.value} requires a ClaimPacket")
        if state in _PRE_EXTRACTION_STATES and self.claim_packet is not None:
            raise ValueError("Pre-extraction states cannot expose a ClaimPacket")

        if state in _TERMINAL_STOP_STATES and (
            self.transcript_confirmation is not None or self.clarification is not None
        ):
            raise ValueError("Terminal stop states cannot expose active actions")

        if state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
            if self.transcript_confirmation is None:
                raise ValueError("awaiting_transcript_confirmation requires an active transcript")
        elif self.transcript_confirmation is not None:
            raise ValueError("An active transcript is allowed only while awaiting confirmation")

        if state is CaseState.AWAITING_CLARIFICATION:
            if self.clarification is None:
                raise ValueError("awaiting_clarification requires an active clarification")
        elif self.clarification is not None:
            raise ValueError("An active clarification is allowed only while awaiting clarification")

        if self.portal_session is not None:
            if state not in _PORTAL_ALLOWED_STATES:
                raise ValueError(f"{state.value} cannot expose a PortalSessionView")
            expected_portal_state: PortalState | None = None
            if state in {CaseState.READY_TO_FILL, CaseState.FILLING}:
                expected_portal_state = PortalState.DRAFT
            elif state in {CaseState.VERIFYING, CaseState.REVIEW}:
                expected_portal_state = PortalState.REVIEW
            if (
                expected_portal_state is not None
                and self.portal_session.state is not expected_portal_state
            ):
                raise ValueError(
                    f"{state.value} requires portal state {expected_portal_state.value}"
                )
        if state in {CaseState.VERIFYING, CaseState.REVIEW} and self.portal_session is None:
            raise ValueError(f"{state.value} requires a PortalSessionView")

        if self.verification_attempts is not None:
            if state not in _VERIFICATION_ALLOWED_STATES:
                raise ValueError(f"{state.value} cannot expose VerificationAttemptSeries")
            if self.claim_packet is None:
                raise ValueError("VerificationAttemptSeries requires a bound ClaimPacket")

        if (
            self.portal_session is not None
            and self.claim_packet is not None
            and self.claim_packet.portal_state is not self.portal_session.state
        ):
            raise ValueError("ClaimPacket.portalState must match PortalSessionView.state")
        if self.portal_session is not None and self.verification_attempts is not None:
            final_attempt = self.verification_attempts.attempts[-1]
            if final_attempt.portal_version != self.portal_session.version:
                raise ValueError(
                    "Final verification portalVersion must match portal session version"
                )

        if state is CaseState.REVIEW:
            if (
                self.claim_packet is None
                or self.portal_session is None
                or self.verification_attempts is None
            ):
                raise ValueError(
                    "review requires ClaimPacket, portal review, and completed verification"
                )
            final_attempt = self.verification_attempts.attempts[-1]
            final_gate = final_attempt.gate_decision
            packet_g8 = self.claim_packet.gate_decisions[-1]
            canonical_claim_fields = self.claim_packet.claim.model_dump(
                mode="json",
                by_alias=False,
                include=set(_PORTAL_CLAIM_FIELD_NAMES),
            )
            rendered_portal_fields = self.portal_session.fields.model_dump(
                mode="json", by_alias=False
            )
            if (
                self.claim_packet.portal_state is not PortalState.REVIEW
                or self.portal_session.state is not PortalState.REVIEW
                or rendered_portal_fields != canonical_claim_fields
                or not final_attempt.final
                or final_attempt.report.status is not VerificationState.VERIFIED
                or not final_attempt.report.review_allowed
                or final_gate is None
                or not final_gate.passed
                or final_attempt.report != self.claim_packet.verification
                or final_gate != packet_g8
            ):
                raise ValueError(
                    "review requires exact portal values and a final successful G8 verification"
                )

        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        base = handler.resolve_ref_schema(handler(core_schema))
        base_schema = deepcopy(base)
        return {
            "description": base_schema.get("description", cls.__doc__),
            "oneOf": _snapshot_schema_variants(base_schema),
            "title": base_schema.get("title", cls.__name__),
        }
