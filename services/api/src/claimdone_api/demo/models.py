"""Strict, content-safe inputs and outputs for the deterministic INT-002 demo."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from claimdone_api.contracts import (
    AllowedTool,
    CaseState,
    ClaimPacket,
    ClarificationAnswerRequest,
    ClarificationView,
    EvidenceItem,
    GateDecision,
    GateId,
    GateReasonCode,
    ProviderCallWorkflowEvent,
    ProviderModelId,
    WorkflowOperation,
)
from claimdone_api.gates import ModelExtraction, ModelOutputEnvelope, SafetyInput
from claimdone_api.persistence.models import OutputContractAttempt, ProviderWorkflowEmission

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REJECTED = "Deterministic demo input rejected"
_SQLITE_INT64_MAX = (1 << 63) - 1

type GateClock = Callable[[GateId], datetime]
type ClarificationIdFactory = Callable[[str], str]


class DemoAnalysisInputError(ValueError):
    """Content-free rejection at the deterministic demo trust boundary."""


def reject_demo_input() -> DemoAnalysisInputError:
    """Return the one value-free error used for every caller-controlled failure."""

    return DemoAnalysisInputError(_REJECTED)


@dataclass(frozen=True, slots=True)
class ConfirmedSyntheticStatement:
    """One staged text artifact plus its explicit human confirmation."""

    evidence: EvidenceItem = field(repr=False)
    confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceItem) or self.confirmed is not True:
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class ApprovedDemoIntake:
    """Already-gated local media passed from the canonical G0/G1 boundary."""

    images: tuple[EvidenceItem, ...] = field(repr=False)
    statement: ConfirmedSyntheticStatement = field(repr=False)
    g0_decision: GateDecision
    g1_decision: GateDecision

    def __post_init__(self) -> None:
        if type(self.images) is not tuple or any(
            not isinstance(image, EvidenceItem) for image in self.images
        ):
            raise reject_demo_input()
        if not isinstance(self.statement, ConfirmedSyntheticStatement):
            raise reject_demo_input()
        if not isinstance(self.g0_decision, GateDecision) or not isinstance(
            self.g1_decision, GateDecision
        ):
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class BoundDemoClarification:
    """Persistable request plus an integrity binding to its exact intake context."""

    view: ClarificationView
    binding_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.view, ClarificationView):
            raise reject_demo_input()
        if type(self.binding_sha256) is not str or _SHA256.fullmatch(self.binding_sha256) is None:
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class DemoClarificationResolution:
    """A canonical answer bound to the exact previously issued clarification."""

    clarification: BoundDemoClarification
    answer: ClarificationAnswerRequest = field(repr=False)
    prior_packet: ClaimPacket = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.clarification, BoundDemoClarification)
            or not isinstance(self.answer, ClarificationAnswerRequest)
            or not isinstance(self.prior_packet, ClaimPacket)
        ):
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class ReconstructedDemoContinuation:
    """Restart-safe continuation derived only from canonical persisted values."""

    intake: ApprovedDemoIntake = field(repr=False)
    clarification: BoundDemoClarification
    prior_packet: ClaimPacket = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.intake, ApprovedDemoIntake)
            or not isinstance(self.clarification, BoundDemoClarification)
            or not isinstance(self.prior_packet, ClaimPacket)
            or self.clarification.view.case_id != self.prior_packet.case_id
            or self.prior_packet.evidence != (*self.intake.images, self.intake.statement.evidence)
            or self.prior_packet.gate_decisions[:2]
            != (self.intake.g0_decision, self.intake.g1_decision)
        ):
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class DemoAnalysisRequest:
    """Strict request for either the first or resolved deterministic analysis round."""

    case_id: str
    case_version: int
    intake: ApprovedDemoIntake = field(repr=False)
    clarification_resolution: DemoClarificationResolution | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.case_id) is not str or _IDENTIFIER.fullmatch(self.case_id) is None:
            raise reject_demo_input()
        if (
            type(self.case_version) is not int
            or self.case_version < 1
            or self.case_version > _SQLITE_INT64_MAX
        ):
            raise reject_demo_input()
        if not isinstance(self.intake, ApprovedDemoIntake):
            raise reject_demo_input()
        if self.clarification_resolution is not None and not isinstance(
            self.clarification_resolution, DemoClarificationResolution
        ):
            raise reject_demo_input()


@dataclass(frozen=True, slots=True)
class DemoInitialPersistenceInputs:
    """Content-bearing, command-ready inputs for the canonical initial commit.

    ``g2_attempts`` contains the raw model envelope used for deterministic G2
    recomputation.  Never log this object, and never persist or serialize the raw
    envelope payload directly; only the canonical repository may consume it as an
    ephemeral command input.
    """

    g2_attempts: tuple[OutputContractAttempt] = field(repr=False)
    safety_input: SafetyInput = field(repr=False)
    provider_events: tuple[ProviderWorkflowEmission]

    def __post_init__(self) -> None:
        if (
            type(self.g2_attempts) is not tuple
            or len(self.g2_attempts) != 1
            or not isinstance(self.g2_attempts[0], OutputContractAttempt)
            or not isinstance(self.g2_attempts[0].envelope, ModelOutputEnvelope)
            or not isinstance(self.safety_input, SafetyInput)
            or type(self.provider_events) is not tuple
            or len(self.provider_events) != 1
            or not isinstance(self.provider_events[0], ProviderWorkflowEmission)
        ):
            raise ValueError("Invalid deterministic persistence inputs")
        attempt = self.g2_attempts[0]
        envelope = attempt.envelope
        emission = self.provider_events[0]
        event = emission.event
        if (
            type(envelope.payload) is not str
            or envelope.refusal is not False
            or envelope.truncated is not False
            or type(envelope.attempt) is not int
            or envelope.attempt != 0
            or attempt.decided_at.utcoffset() is None
            or not isinstance(event, ProviderCallWorkflowEvent)
            or event.operation is not WorkflowOperation.EXTRACTION
            or event.model_id is not ProviderModelId.DETERMINISTIC_MOCK
            or event.provider_mode != "mock"
            or event.call_sequence != 1
            or event.retry_attempt != 0
            or event.duration_ms != 0
            or event.status != "succeeded"
            or event.usage is not None
            or event.cost is not None
            or emission.occurred_at != attempt.decided_at
        ):
            raise ValueError("Invalid deterministic persistence inputs")


@dataclass(frozen=True, slots=True)
class DemoExecutionProof:
    """Value-free proof that the provider-free fixture path produced this result."""

    mode: Literal["deterministic_demo_fixture"]
    fixture_version: Literal["claimdone-int002-main-v1"]
    external_provider_call_count: Literal[0]
    mock_provider_event_count: Literal[0, 1]
    semantic_sha256: str

    def __post_init__(self) -> None:
        if self.mode != "deterministic_demo_fixture":
            raise ValueError("Invalid deterministic execution mode")
        if self.fixture_version != "claimdone-int002-main-v1":
            raise ValueError("Invalid deterministic fixture version")
        if (
            type(self.external_provider_call_count) is not int
            or self.external_provider_call_count != 0
        ):
            raise ValueError("Deterministic demo execution cannot call a provider")
        if type(
            self.mock_provider_event_count
        ) is not int or self.mock_provider_event_count not in {0, 1}:
            raise ValueError("Invalid deterministic mock telemetry count")
        if type(self.semantic_sha256) is not str or _SHA256.fullmatch(self.semantic_sha256) is None:
            raise ValueError("Invalid deterministic semantic digest")


@dataclass(frozen=True, slots=True)
class DemoAnalysisResult:
    """Canonical G0-G5 packet and its optional single clarification."""

    packet: ClaimPacket
    clarification: BoundDemoClarification | None
    execution: DemoExecutionProof
    round_kind: Literal["initial", "clarification"]
    new_gate_decisions: tuple[GateDecision, ...]
    initial_persistence: DemoInitialPersistenceInputs | None

    def __post_init__(self) -> None:
        if not isinstance(self.packet, ClaimPacket) or not isinstance(
            self.execution, DemoExecutionProof
        ):
            raise ValueError("Invalid deterministic demo result")
        if type(self.new_gate_decisions) is not tuple or any(
            not isinstance(item, GateDecision) for item in self.new_gate_decisions
        ):
            raise ValueError("Invalid deterministic demo result")
        if self.clarification is not None and not isinstance(
            self.clarification, BoundDemoClarification
        ):
            raise ValueError("Invalid deterministic demo result")
        try:
            revalidated = ClaimPacket.model_validate(
                self.packet.model_dump(mode="json", by_alias=True)
            )
        except ValueError as error:
            raise ValueError("Invalid deterministic demo result") from error
        if revalidated != self.packet:
            raise ValueError("Invalid deterministic demo result")

        expected_gates = tuple(GateId(f"G{index}") for index in range(6))
        if tuple(decision.gate_id for decision in self.packet.gate_decisions) != expected_gates:
            raise ValueError("Invalid deterministic demo result")
        if self.packet.state is CaseState.AWAITING_CLARIFICATION:
            expected_reason = (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)
            ask_count = sum(
                step.tool is AllowedTool.ASK_CLARIFICATION for step in self.packet.plan.steps
            )
            if (
                self.clarification is None
                or self.clarification.view.case_id != self.packet.case_id
                or self.clarification.view.field.value != "incident_time"
                or self.round_kind != "initial"
                or tuple(item.gate_id for item in self.new_gate_decisions) != expected_gates[2:]
                or self.new_gate_decisions != self.packet.gate_decisions[2:]
                or not all(decision.passed for decision in self.packet.gate_decisions[:5])
                or self.packet.gate_decisions[5].reason_codes != expected_reason
                or ask_count != 1
                or self.initial_persistence is None
                or self.execution.mock_provider_event_count != 1
            ):
                raise ValueError("Invalid deterministic demo result")
            persistence = self.initial_persistence
            attempt = persistence.g2_attempts[0]
            payload = attempt.envelope.payload
            if type(payload) is not str:
                raise ValueError("Invalid deterministic demo result")
            try:
                extraction = ModelExtraction.model_validate_json(payload)
            except ValueError as error:
                raise ValueError("Invalid deterministic demo result") from error
            safety = persistence.safety_input
            if (
                attempt.decided_at != self.packet.gate_decisions[2].decided_at
                or extraction.evidence != self.packet.evidence
                or extraction.provenance != self.packet.provenance
                or extraction.facts != self.packet.facts
                or extraction.claim != self.packet.claim
                or safety.injury_reported is not False
                or safety.immediate_danger is not False
                or safety.portal_is_sandbox is not True
                or safety.real_credentials_present is not False
                or safety.advice_categories
                or safety.requested_actions
                or safety.model_signal is not None
                or safety.evidence_refs != self.packet.gate_decisions[3].evidence_refs
            ):
                raise ValueError("Invalid deterministic demo result")
        elif self.packet.state is CaseState.READY_TO_FILL:
            if (
                self.clarification is not None
                or self.round_kind != "clarification"
                or tuple(item.gate_id for item in self.new_gate_decisions) != expected_gates[4:]
                or self.new_gate_decisions != self.packet.gate_decisions[4:]
                or not all(decision.passed for decision in self.packet.gate_decisions)
                or self.packet.claim.missing_required_fields
                or any(
                    step.tool is AllowedTool.ASK_CLARIFICATION for step in self.packet.plan.steps
                )
                or self.initial_persistence is not None
                or self.execution.mock_provider_event_count != 0
            ):
                raise ValueError("Invalid deterministic demo result")
        else:
            raise ValueError("Invalid deterministic demo result")

    @property
    def provider_call_count(self) -> Literal[0]:
        """Compatibility alias for the external network-call invariant."""

        return self.execution.external_provider_call_count

    @property
    def external_provider_call_count(self) -> Literal[0]:
        """Expose that the deterministic mock never performs an external request."""

        return self.execution.external_provider_call_count

    @property
    def mock_provider_event_count(self) -> Literal[0, 1]:
        """Expose the canonical value-free mock telemetry cardinality."""

        return self.execution.mock_provider_event_count
