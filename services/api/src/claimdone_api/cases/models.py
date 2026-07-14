"""Closed camelCase-only transport models for the Case API."""

from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
)

from claimdone_api.contracts import (
    CaseState,
    ClaimPacket,
    GateDecision,
    GateReasonCode,
    PortalState,
)
from claimdone_api.contracts.base import to_camel
from claimdone_api.persistence import CaseRecord

CaseIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        strip_whitespace=True,
    ),
]
MetadataKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    ),
]


class ApiModel(BaseModel):
    """Forbid Python field names on the public JSON boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class CreateCaseRequest(ApiModel):
    """Optional non-persisted raw metadata; values are redacted by the service."""

    metadata: dict[MetadataKey, JsonValue] = Field(default_factory=dict)


class CaseView(ApiModel):
    """Persisted case projection including an optional canonical ClaimPacket."""

    case_id: CaseIdentifier
    version: Annotated[int, Field(ge=1)]
    state: CaseState
    portal_state: PortalState
    redacted_metadata: dict[str, str]
    claim_packet: ClaimPacket | None
    intake_summary: dict[str, JsonValue] | None
    active_clarification: dict[str, JsonValue] | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @classmethod
    def from_record(cls, record: CaseRecord) -> "CaseView":
        """Convert an internal record through the public alias-only boundary."""

        return cls.model_validate(
            {
                "caseId": record.case_id,
                "version": record.version,
                "state": record.state,
                "portalState": record.snapshot.portal_state,
                "redactedMetadata": record.snapshot.redacted_metadata,
                "claimPacket": record.snapshot.claim_packet,
                "intakeSummary": record.snapshot.intake_summary,
                "activeClarification": record.snapshot.active_clarification,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
            }
        )


class FieldError(ApiModel):
    field: str
    reason_code: GateReasonCode | None
    message: str


class ErrorDetail(ApiModel):
    code: str
    message: str
    reason_codes: tuple[GateReasonCode, ...] = ()
    field_errors: tuple[FieldError, ...] = ()
    gate_decision: GateDecision | None = None
    current_version: int | None = None


class ErrorEnvelope(ApiModel):
    error: ErrorDetail


def error_envelope(
    *,
    code: str,
    message: str,
    current_version: int | None = None,
) -> ErrorEnvelope:
    return ErrorEnvelope.model_validate(
        {
            "error": {
                "code": code,
                "message": message,
                "reasonCodes": (),
                "fieldErrors": (),
                "gateDecision": None,
                "currentVersion": current_version,
            }
        }
    )
