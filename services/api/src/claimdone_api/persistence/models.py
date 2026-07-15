"""Immutable values exchanged with the SQLite repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import JsonValue

from claimdone_api.contracts import (
    AuditEvent,
    CaseState,
    ClaimPacket,
    ClarificationAnswerRequest,
    ClarificationView,
    ClarificationWorkflowEvent,
    EvidenceItem,
    GateDecision,
    OperationalFailureWorkflowEvent,
    PlanStepWorkflowEvent,
    PortalSessionView,
    PortalState,
    PortalVariant,
    ProviderCallWorkflowEvent,
    ProviderFailureCategory,
    ProviderModelId,
    RenderedPortalSnapshot,
    RetryWorkflowEvent,
    SandboxReceipt,
    ToolInvocation,
    VerificationAttempt,
    WorkflowEventEnvelope,
    WorkflowOperation,
)
from claimdone_api.gates import ModelOutputEnvelope, SafetyInput

if TYPE_CHECKING:
    from claimdone_api.ai import TranscriptionSuccess
    from claimdone_api.media.types import IntakeRequest, PrivacyReview

type JsonObject = dict[str, JsonValue]
type AnalysisProviderWorkflowEvent = ProviderCallWorkflowEvent | RetryWorkflowEvent

_PORTAL_STATES_BY_CASE_STATE = MappingProxyType(
    {
        CaseState.CREATED: frozenset({PortalState.DRAFT}),
        CaseState.DISCLOSED: frozenset({PortalState.DRAFT}),
        CaseState.ANALYZING: frozenset({PortalState.DRAFT}),
        CaseState.AWAITING_TRANSCRIPT_CONFIRMATION: frozenset({PortalState.DRAFT}),
        CaseState.AWAITING_CLARIFICATION: frozenset({PortalState.DRAFT}),
        CaseState.READY_TO_FILL: frozenset({PortalState.DRAFT}),
        CaseState.FILLING: frozenset({PortalState.DRAFT}),
        CaseState.VERIFYING: frozenset({PortalState.REVIEW}),
        CaseState.REVIEW: frozenset({PortalState.REVIEW}),
        CaseState.BLOCKED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.HUMAN_APPROVED: frozenset({PortalState.HUMAN_APPROVED}),
        CaseState.RECEIPT: frozenset({PortalState.RECEIPT}),
        CaseState.EMERGENCY_STOPPED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.ABANDONED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
        CaseState.FAILED: frozenset({PortalState.DRAFT, PortalState.REVIEW}),
    }
)


def validate_portal_state(case_state: CaseState, portal_state: PortalState) -> None:
    """Keep persisted portal state aligned with the canonical case contract."""

    allowed = _PORTAL_STATES_BY_CASE_STATE[case_state]
    if portal_state not in allowed:
        values = ", ".join(sorted(value.value for value in allowed))
        raise ValueError(f"Case state {case_state.value} requires portal state in: {values}")


def portal_state_after_transition(
    current: PortalState,
    target: CaseState,
) -> PortalState:
    """Derive the only safe portal projection when no ClaimPacket exists yet."""

    allowed = _PORTAL_STATES_BY_CASE_STATE[target]
    if current in allowed:
        return current
    if len(allowed) == 1:
        return next(iter(allowed))
    return PortalState.DRAFT if current is PortalState.DRAFT else PortalState.REVIEW


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """Versioned case payloads persisted alongside the state machine."""

    portal_state: PortalState
    redacted_metadata: dict[str, str]
    claim_packet: ClaimPacket | None
    intake_summary: JsonObject | None
    active_clarification: JsonObject | None


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """Complete persisted backend case."""

    case_id: str
    version: int
    state: CaseState
    snapshot: CaseSnapshot
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderWorkflowEmission:
    """One value-free provider projection with its actual occurrence time."""

    event: AnalysisProviderWorkflowEvent
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OutputContractAttempt:
    """One raw, non-persisted model response with its deterministic decision time."""

    envelope: ModelOutputEnvelope = field(repr=False)
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class IntakeDisclosureCommand:
    """Raw intake inputs staged and committed by the canonical repository only."""

    case_id: str
    expected_version: int
    request: IntakeRequest = field(repr=False)
    privacy_review: PrivacyReview = field(repr=False)
    g0_decided_at: datetime
    g1_decided_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptionOutcomeCommand:
    """One provider transcript bound to a prior canonical audio authority."""

    case_id: str
    expected_version: int
    outcome: TranscriptionSuccess = field(repr=False)
    occurred_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisWorkflowCommand:
    """Closed analysis outcome committed as one case-version mutation.

    The repository emits provider telemetry, gates, plan steps, clarification
    lifecycle, and the state transition in that deterministic cursor order.
    ``gate_decisions`` contains only decisions newly emitted in this commit;
    the target ClaimPacket carries the complete current G0-G5 set.
    """

    case_id: str
    expected_version: int
    target: CaseState
    claim_packet: ClaimPacket | None
    active_clarification: ClarificationView | None
    clarification_answer: ClarificationAnswerRequest | None
    approved_evidence: tuple[EvidenceItem, ...]
    g2_attempts: tuple[OutputContractAttempt, ...]
    safety_input: SafetyInput | None
    gate_decisions: tuple[GateDecision, ...]
    provider_events: tuple[ProviderWorkflowEmission, ...]
    plan_steps: tuple[PlanStepWorkflowEvent, ...]
    clarification_events: tuple[ClarificationWorkflowEvent, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisWorkflowResult:
    """Case plus every redacted projection produced by one analysis commit."""

    case: CaseRecord
    workflow_events: tuple[WorkflowEventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class TerminalProviderFailureCommand:
    """One terminal provider outcome; it has no deterministic-gate surface."""

    case_id: str
    expected_version: int
    event: OperationalFailureWorkflowEvent
    provider_events: tuple[ProviderWorkflowEmission, ...]
    approved_evidence: tuple[EvidenceItem, ...]
    g2_attempts: tuple[OutputContractAttempt, ...]
    claim_packet: ClaimPacket | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TerminalProviderFailureResult:
    """Failed case and the operational/state projections committed with it."""

    case: CaseRecord
    workflow_events: tuple[WorkflowEventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class SequencedAuditEvent:
    """Canonical audit event with a database-owned cursor."""

    sequence: int
    event: AuditEvent


@dataclass(frozen=True, slots=True)
class SequencedGateDecision:
    """Immutable gate decision with a database-owned history cursor."""

    sequence: int
    decision: GateDecision


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """Content-free transcript state; text remains in the owned media store."""

    transcript_id: str
    case_id: str
    version: int
    bound_case_version: int
    transcript_sha256: str
    local_ref: str
    confirmed: bool
    created_at: datetime
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TranscriptTransitionResult:
    """Case and transcript written by one SQLite transaction."""

    case: CaseRecord
    transcript: TranscriptRecord


@dataclass(frozen=True, slots=True)
class AuthorityCapabilityRecord:
    """Digest-only local authority capability metadata."""

    digest: bytes = field(repr=False)
    case_id: str
    role: str
    purpose: str
    portal_variant: PortalVariant | None
    bound_case_version: int
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class HumanApprovalCommand:
    """Secret-free command for the one atomic AUTH approval boundary."""

    case_id: str
    expected_case_version: int
    capability_digest: bytes = field(repr=False)
    portal_variant: PortalVariant
    approval_id: str
    receipt_id: str
    consumed_at: datetime
    approved_at: datetime
    rendered_at: datetime


@dataclass(frozen=True, slots=True)
class HumanApprovalResult:
    """Final receipt state produced only after the human capability is consumed."""

    case: CaseRecord
    receipt: SandboxReceiptRecord


@dataclass(frozen=True, slots=True)
class PortalRunStartCommand:
    """Secret-free request to consume one agent capability and open G6.

    ``control_digest`` is a globally unique recovery identifier derived for
    exactly this run by the trusted caller.  It is neither a root secret nor a
    reusable signing/HMAC key, and every later command must echo it exactly.
    """

    case_id: str
    expected_case_version: int
    run_id: str
    capability_digest: bytes = field(repr=False)
    control_digest: bytes = field(repr=False)
    portal_variant: PortalVariant
    invocation_payload: object = field(repr=False)
    current_url: str = field(repr=False)
    action: str = field(repr=False)
    proposed_action_number: int
    elapsed_seconds: float
    prestage_session: PortalSessionView = field(repr=False)
    consumed_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PortalRunRecord:
    """Read-only digest-bound status for uncertain-commit recovery."""

    run_id: str
    case_id: str
    portal_variant: PortalVariant
    ready_case_version: int
    g6_case_version: int
    terminal_case_version: int | None
    status: str
    invocation: ToolInvocation
    g6_decision: GateDecision
    prestage_session: PortalSessionView
    created_at: datetime
    terminal_at: datetime | None


@dataclass(frozen=True, slots=True)
class PortalRunStartResult:
    """Case and immutable run authority committed by the G6 transaction."""

    case: CaseRecord
    run: PortalRunRecord


@dataclass(frozen=True, slots=True)
class PortalWriteFinalizeCommand:
    """One full candidate write closed by deterministic G7 authority.

    ``control_digest`` must be the persisted per-run recovery digest from G6.
    """

    case_id: str
    expected_case_version: int
    run_id: str
    control_digest: bytes = field(repr=False)
    fields_payload: object = field(repr=False)
    duration_ms: int
    completed_at: datetime
    portal_session: PortalSessionView | None = field(default=None, repr=False)
    rendered_snapshot: RenderedPortalSnapshot | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PortalWriteFinalizeResult:
    """Case and terminal run status produced by one G7 transaction."""

    case: CaseRecord
    run: PortalRunRecord


@dataclass(frozen=True, slots=True)
class VerificationAttemptCommand:
    """Trusted freshness inputs for one independently recomputed attempt.

    ``control_digest`` must be the persisted per-run recovery digest from G6.
    """

    case_id: str
    expected_case_version: int
    run_id: str
    control_digest: bytes = field(repr=False)
    attempt_id: str
    rendered_snapshot: RenderedPortalSnapshot = field(repr=False)
    screenshot_sha256: str
    snapshot_requested_at: datetime
    snapshot_received_at: datetime
    model_reported_mismatch: bool
    verified_at: datetime
    decided_at: datetime
    final: bool
    repaired_session: PortalSessionView | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class VerificationAttemptResult:
    """Case and canonical attempt created by one G8/repair transaction."""

    case: CaseRecord
    attempt: VerificationAttempt


@dataclass(frozen=True, slots=True)
class ProviderUsageLedgerRecord:
    """Queryable, content-free provider telemetry bound to one workflow cursor."""

    source_audit_sequence: int
    case_id: str
    operation: WorkflowOperation
    model_id: ProviderModelId
    provider_mode: str
    call_sequence: int
    retry_attempt: int
    duration_ms: int
    status: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost_micros: int | None
    currency: str | None
    pricing_snapshot_id: str | None
    failure_category: ProviderFailureCategory | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ObservabilityMetricsSnapshot:
    """Content-free aggregate derived from canonical persisted projections."""

    case_id: str
    through_cursor: int
    provider_request_count: int
    provider_request_duration_ms: int
    retry_count: int
    model_ids: tuple[ProviderModelId, ...]
    usage_reported_request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    costed_request_count: int
    estimated_cost_micros: int | None
    currency: str | None
    pricing_snapshot_ids: tuple[str, ...]
    tool_call_count: int
    tool_duration_ms: int

    def __post_init__(self) -> None:
        counters = (
            self.through_cursor,
            self.provider_request_count,
            self.provider_request_duration_ms,
            self.retry_count,
            self.usage_reported_request_count,
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.costed_request_count,
            self.tool_call_count,
            self.tool_duration_ms,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("Observability counters must be non-negative integers")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Observability total tokens must be derived")
        if self.usage_reported_request_count > self.provider_request_count:
            raise ValueError("Usage count cannot exceed provider request count")
        if self.costed_request_count > self.provider_request_count:
            raise ValueError("Cost count cannot exceed provider request count")
        if self.retry_count > self.provider_request_count:
            raise ValueError("Retry count cannot exceed provider request count")
        if bool(self.model_ids) is not (self.provider_request_count > 0):
            raise ValueError("Model IDs must be present exactly when requests exist")
        if (self.estimated_cost_micros is None and self.costed_request_count > 0) or (
            self.estimated_cost_micros is not None and self.costed_request_count == 0
        ):
            raise ValueError("Estimated cost presence must match costed request count")
        if self.costed_request_count == 0:
            if self.currency is not None or self.pricing_snapshot_ids:
                raise ValueError("Uncosted metrics cannot name currency or pricing")
        elif self.currency != "USD" or not self.pricing_snapshot_ids:
            raise ValueError("Costed metrics require USD and pricing provenance")
        if len(set(self.pricing_snapshot_ids)) != len(self.pricing_snapshot_ids) or any(
            type(snapshot_id) is not str or not snapshot_id
            for snapshot_id in self.pricing_snapshot_ids
        ):
            raise ValueError("Pricing snapshot IDs must be unique non-empty strings")
        if self.estimated_cost_micros is not None and (
            type(self.estimated_cost_micros) is not int or self.estimated_cost_micros < 0
        ):
            raise ValueError("Estimated cost must be a non-negative integer")
        if len(set(self.model_ids)) != len(self.model_ids) or any(
            not isinstance(model_id, ProviderModelId) for model_id in self.model_ids
        ):
            raise ValueError("Observability model IDs must be unique closed values")


@dataclass(frozen=True, slots=True)
class SequencedWorkflowEvent:
    """Canonical redacted projection with its database-owned replay cursor."""

    sequence: int
    envelope: WorkflowEventEnvelope


@dataclass(frozen=True, slots=True)
class SandboxReceiptRecord:
    """Validated redacted receipt persisted only by the later AUTH transaction."""

    receipt: SandboxReceipt
    created_at: datetime
