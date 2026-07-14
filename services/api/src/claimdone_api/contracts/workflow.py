"""Sanitized, persistable workflow-event contracts."""

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    ContractModel,
    ContractVersion,
    ExactlyOne,
    Identifier,
    OneOrTwo,
    OneToThree,
    StrictBoolean,
    StrictInteger,
    WireAwareDatetime,
    ZeroOrOne,
)
from .enums import (
    AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND,
    ActorType,
    AllowedTool,
    AuditEventType,
    CaseState,
    ClarificationStatus,
    PortalVariant,
    ProviderFailureCategory,
    ProviderModelId,
    RequiredClaimField,
    ToolCallStatus,
    VerificationState,
    WorkflowEventKind,
    WorkflowOperation,
)
from .models import GateDecision
from .state_machine import InvalidCaseTransition, validate_case_transition

ClarificationRound = OneToThree
VerificationAttemptNumber = OneOrTwo


class ProviderFailure(ContractModel):
    """Sanitized provider outcome that is operational metadata, never a gate."""

    category: ProviderFailureCategory
    retryable: StrictBoolean
    terminal: StrictBoolean

    @model_validator(mode="after")
    def validate_retry_authority(self) -> Self:
        if self.retryable and self.terminal:
            raise ValueError("A terminal provider failure cannot be retryable")
        always_terminal = {
            ProviderFailureCategory.QUOTA_EXHAUSTED,
            ProviderFailureCategory.BILLING_LIMIT,
            ProviderFailureCategory.RATE_LIMITED,
            ProviderFailureCategory.AUTHENTICATION_FAILED,
            ProviderFailureCategory.PERMISSION_DENIED,
            ProviderFailureCategory.MODEL_NOT_FOUND,
            ProviderFailureCategory.INVALID_REQUEST,
            ProviderFailureCategory.CANCELLED,
        }
        controlled_retry = {
            ProviderFailureCategory.TIMEOUT,
            ProviderFailureCategory.PROVIDER_UNAVAILABLE,
            ProviderFailureCategory.INVALID_RESPONSE,
        }
        if self.category in always_terminal and (self.retryable or not self.terminal):
            raise ValueError("This provider failure category must be terminal and non-retryable")
        if self.category in controlled_retry and self.retryable is self.terminal:
            raise ValueError("Controlled-retry failures must be either retryable or terminal")
        return self


class StateWorkflowEvent(ContractModel):
    """Validated case-state transition without a free-form detail payload."""

    kind: Literal[WorkflowEventKind.STATE]
    actor: ActorType
    from_state: CaseState
    to_state: CaseState

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        try:
            validate_case_transition(self.from_state, self.to_state)
        except InvalidCaseTransition as error:
            raise ValueError(str(error)) from error
        if self.to_state is CaseState.HUMAN_APPROVED and self.actor is not ActorType.HUMAN:
            raise ValueError("Only a human actor may transition to human_approved")
        return self


class GateWorkflowEvent(ContractModel):
    """Immutable deterministic gate outcome."""

    kind: Literal[WorkflowEventKind.GATE]
    decision: GateDecision


class ClarificationWorkflowEvent(ContractModel):
    """Content-free clarification lifecycle event with an explicit finite round."""

    kind: Literal[WorkflowEventKind.CLARIFICATION]
    round: ClarificationRound
    field: RequiredClaimField
    status: ClarificationStatus


class PlanStepWorkflowEvent(ContractModel):
    """Sanitized plan selection; reasons and arguments are not persisted."""

    kind: Literal[WorkflowEventKind.PLAN_STEP]
    sequence: Annotated[StrictInteger, Field(ge=1, le=40)]
    tool: AllowedTool


class ToolCallWorkflowEvent(ContractModel):
    """Sanitized tool-call lifecycle event without arguments or outputs."""

    kind: Literal[WorkflowEventKind.TOOL_CALL]
    invocation_id: Identifier
    sequence: Annotated[StrictInteger, Field(ge=1, le=40)]
    tool: AllowedTool
    status: ToolCallStatus


class PortalFillWorkflowEvent(ContractModel):
    """Portal write metadata containing field names but never field values."""

    kind: Literal[WorkflowEventKind.PORTAL_FILL]
    variant: PortalVariant
    portal_version: Annotated[StrictInteger, Field(ge=1)]
    written_fields: Annotated[tuple[RequiredClaimField, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_unique_fields(self) -> Self:
        if len(set(self.written_fields)) != len(self.written_fields):
            raise ValueError("writtenFields cannot contain duplicates")
        return self


class VerificationWorkflowEvent(ContractModel):
    """Value-free verification summary with explicit finality and repair usage."""

    kind: Literal[WorkflowEventKind.VERIFICATION]
    attempt_number: VerificationAttemptNumber
    status: VerificationState
    deterministic_match: StrictBoolean
    model_reported_mismatch: StrictBoolean
    repair_used: StrictBoolean
    final: StrictBoolean

    @model_validator(mode="after")
    def validate_attempt_summary(self) -> Self:
        if self.status is VerificationState.PENDING:
            raise ValueError("A persisted verification event cannot be pending")
        if self.attempt_number == 1 and self.repair_used:
            raise ValueError("The first verification attempt cannot already use a repair")
        if self.attempt_number == 2 and (not self.repair_used or not self.final):
            raise ValueError("The repaired second verification attempt must be final")
        if self.status is VerificationState.VERIFIED and (
            not self.deterministic_match or self.model_reported_mismatch
        ):
            raise ValueError("Verified workflow events require every check to pass")
        if self.status is VerificationState.MISMATCH and (
            self.deterministic_match and not self.model_reported_mismatch
        ):
            raise ValueError("Mismatch workflow events require a mismatch signal")
        return self


class RetryWorkflowEvent(ContractModel):
    """The single V1 controlled retry, separate from clarification rounds."""

    kind: Literal[WorkflowEventKind.RETRY]
    operation: Literal[WorkflowOperation.EXTRACTION,]
    retry_attempt: ExactlyOne
    failure: ProviderFailure

    @model_validator(mode="after")
    def require_retryable_failure(self) -> Self:
        if not self.failure.retryable or self.failure.terminal:
            raise ValueError("A retry event requires a retryable, non-terminal failure")
        return self


class OperationalFailureWorkflowEvent(ContractModel):
    """Sanitized provider failure kept separate from deterministic gate history."""

    kind: Literal[WorkflowEventKind.OPERATIONAL_FAILURE]
    operation: WorkflowOperation
    failure: ProviderFailure

    @model_validator(mode="after")
    def require_terminal_failure(self) -> Self:
        if not self.failure.terminal:
            raise ValueError("Operational failure events require a terminal failure")
        return self


class ProviderUsageSnapshot(ContractModel):
    """Integer-only usage counters with no provider request content."""

    input_tokens: Annotated[StrictInteger, Field(ge=0)]
    output_tokens: Annotated[StrictInteger, Field(ge=0)]
    total_tokens: Annotated[StrictInteger, Field(ge=0)]

    @model_validator(mode="after")
    def derive_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("totalTokens must equal inputTokens + outputTokens")
        return self


class ProviderCallWorkflowEvent(ContractModel):
    """Value-free successful provider-call telemetry for OBS projections."""

    kind: Literal[WorkflowEventKind.PROVIDER_CALL]
    operation: WorkflowOperation
    model_id: ProviderModelId
    provider_mode: Literal["mock", "live"]
    call_sequence: Annotated[StrictInteger, Field(ge=1, le=40)]
    retry_attempt: ZeroOrOne
    duration_ms: Annotated[StrictInteger, Field(ge=0)]
    status: Literal["succeeded"]
    usage: ProviderUsageSnapshot | None = None
    cost: "ProviderCostSnapshot | None" = None

    @model_validator(mode="after")
    def bind_model_to_operation(self) -> Self:
        if self.operation is not WorkflowOperation.EXTRACTION and self.retry_attempt != 0:
            raise ValueError("Only extraction may record the single V1 provider retry")
        if self.provider_mode == "mock":
            if self.model_id is not ProviderModelId.DETERMINISTIC_MOCK:
                raise ValueError("Mock provider calls require the deterministic mock model ID")
            return self
        if self.operation is WorkflowOperation.TRANSCRIPTION:
            if self.model_id is not ProviderModelId.TRANSCRIBE:
                raise ValueError("Live transcription requires gpt-4o-transcribe")
        elif self.model_id not in {
            ProviderModelId.SOL,
            ProviderModelId.TERRA,
            ProviderModelId.LUNA,
        }:
            raise ValueError("Live V1 AI operations require a closed GPT-5.6 model ID")
        return self


class ProviderCostSnapshot(ContractModel):
    """Optional USD estimate bound to an explicit pricing snapshot."""

    estimated_cost_micros: Annotated[StrictInteger, Field(ge=0)]
    currency: Literal["USD"]
    pricing_snapshot_id: Identifier


WorkflowEvent = Annotated[
    StateWorkflowEvent
    | GateWorkflowEvent
    | ClarificationWorkflowEvent
    | PlanStepWorkflowEvent
    | ToolCallWorkflowEvent
    | PortalFillWorkflowEvent
    | VerificationWorkflowEvent
    | RetryWorkflowEvent
    | OperationalFailureWorkflowEvent
    | ProviderCallWorkflowEvent,
    Field(discriminator="kind"),
]


class WorkflowEventEnvelope(ContractModel):
    """Read-only redacted projection bound to canonical audit truth."""

    contract_version: ContractVersion
    event_id: Identifier
    case_id: Identifier
    source_audit_event_id: Identifier
    source_audit_event_type: AuditEventType
    source_audit_sequence: Annotated[StrictInteger, Field(ge=1)]
    cursor: Annotated[StrictInteger, Field(ge=1)]
    occurred_at: WireAwareDatetime
    event: WorkflowEvent

    @model_validator(mode="after")
    def bind_projection_cursor(self) -> Self:
        if self.cursor != self.source_audit_sequence:
            raise ValueError("cursor must equal the canonical sourceAuditSequence")
        expected_type = AUDIT_EVENT_TYPE_BY_WORKFLOW_KIND[self.event.kind]
        if self.source_audit_event_type is not expected_type:
            raise ValueError("sourceAuditEventType must match the workflow event kind")
        return self


def validate_workflow_event_order(
    events: tuple[WorkflowEventEnvelope, ...],
) -> None:
    """Reject replay pages whose database-owned cursors are not strictly monotonic."""

    cursors = tuple(event.cursor for event in events)
    if any(current >= following for current, following in pairwise(cursors)):
        raise ValueError("Workflow event cursors must be strictly increasing")
    source_ids = tuple(event.source_audit_event_id for event in events)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("Workflow events cannot project one audit event more than once")
