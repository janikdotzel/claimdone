"""Immutable G0-G5 registry, reason ordering, and decision construction."""

from dataclasses import dataclass
from datetime import UTC, datetime

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    GateDecision,
    GateId,
    GateReasonCode,
)


class GateOrderError(ValueError):
    """Raised when decision events do not form a valid contiguous gate prefix."""


@dataclass(frozen=True, slots=True)
class GateSpec:
    """Stable metadata for one deterministic gate."""

    gate_id: GateId
    order: int
    name: str
    reason_priority: tuple[GateReasonCode, ...]
    model_may_add_block: bool = False


@dataclass(frozen=True, slots=True)
class GateRegistry:
    """An immutable ordered registry that appends immutable decision events."""

    specs: tuple[GateSpec, ...]

    def __post_init__(self) -> None:
        if tuple(spec.order for spec in self.specs) != tuple(range(len(self.specs))):
            raise ValueError("Gate registry orders must be contiguous and zero-based")
        gate_ids = tuple(spec.gate_id for spec in self.specs)
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("Gate registry IDs must be unique")
        for spec in self.specs:
            if len(set(spec.reason_priority)) != len(spec.reason_priority):
                raise ValueError("Gate reason priority cannot contain duplicates")
            prefix = f"{spec.gate_id.value}_"
            if any(not reason.value.startswith(prefix) for reason in spec.reason_priority):
                raise ValueError("Gate reason priority contains a foreign reason code")
            expected_reasons = {
                reason for reason in GateReasonCode if reason.value.startswith(prefix)
            }
            if set(spec.reason_priority) != expected_reasons:
                raise ValueError(
                    f"Gate {spec.gate_id.value} must register every reason code exactly once"
                )

    def spec_for(self, gate_id: GateId) -> GateSpec:
        for spec in self.specs:
            if spec.gate_id is gate_id:
                return spec
        raise KeyError(f"Gate {gate_id.value} is not registered in the G0-G5 pipeline")

    def validate_history(self, history: tuple[GateDecision, ...]) -> None:
        """Require a contiguous prefix and stop the pipeline at its first failure."""

        if len(history) > len(self.specs):
            raise GateOrderError("Gate history is longer than the registry")
        for index, decision in enumerate(history):
            expected = self.specs[index].gate_id
            if decision.gate_id is not expected:
                raise GateOrderError(
                    f"Expected {expected.value} at position {index}, got {decision.gate_id.value}"
                )
            if index < len(history) - 1 and not decision.passed:
                raise GateOrderError("No decision may follow a failed gate")
            if index > 0 and decision.decided_at < history[index - 1].decided_at:
                raise GateOrderError("Gate decision timestamps must be non-decreasing")

    def append(
        self,
        history: tuple[GateDecision, ...],
        decision: GateDecision,
    ) -> tuple[GateDecision, ...]:
        """Return a new history; the original tuple and events remain untouched."""

        self.validate_history(history)
        if history and not history[-1].passed:
            raise GateOrderError("A failed gate terminates the pipeline")
        if len(history) >= len(self.specs):
            raise GateOrderError("Every registered gate already has a decision")
        expected = self.specs[len(history)].gate_id
        if decision.gate_id is not expected:
            raise GateOrderError(
                f"Expected next gate {expected.value}, got {decision.gate_id.value}"
            )
        next_history = (*history, decision)
        self.validate_history(next_history)
        return next_history


G0_TO_G5_REGISTRY = GateRegistry(
    specs=(
        GateSpec(
            GateId.G0_INTAKE,
            0,
            "Intake",
            (
                GateReasonCode.G0_IMAGE_COUNT_INVALID,
                GateReasonCode.G0_IMAGE_TYPE_INVALID,
                GateReasonCode.G0_IMAGE_TOO_LARGE,
                GateReasonCode.G0_INPUT_MODE_INVALID,
                GateReasonCode.G0_AUDIO_TOO_LONG,
                GateReasonCode.G0_CONSENT_MISSING,
            ),
        ),
        GateSpec(
            GateId.G1_PRIVACY,
            1,
            "Privacy",
            (
                GateReasonCode.G1_EXIF_UNREVIEWED,
                GateReasonCode.G1_MODEL_COPY_NOT_APPROVED,
                GateReasonCode.G1_SENSITIVE_LOG_DATA,
            ),
        ),
        GateSpec(
            GateId.G2_OUTPUT_CONTRACT,
            2,
            "Output contract",
            (
                GateReasonCode.G2_REFUSAL,
                GateReasonCode.G2_OUTPUT_TRUNCATED,
                GateReasonCode.G2_SCHEMA_INVALID,
                GateReasonCode.G2_REFERENCE_MISSING,
                GateReasonCode.G2_RETRY_EXHAUSTED,
            ),
        ),
        GateSpec(
            GateId.G3_SAFETY_SCOPE,
            3,
            "Safety and scope",
            (
                GateReasonCode.G3_INJURY_OR_EMERGENCY,
                GateReasonCode.G3_REAL_PORTAL,
                GateReasonCode.G3_LEGAL_OR_LIABILITY,
                GateReasonCode.G3_PAYMENT_OR_COVERAGE,
                GateReasonCode.G3_SUBMISSION_ACTION,
                GateReasonCode.G3_MODEL_UNCERTAIN,
            ),
            model_may_add_block=True,
        ),
        GateSpec(
            GateId.G4_PROVENANCE,
            4,
            "Evidence and provenance",
            (
                GateReasonCode.G4_PROVENANCE_MISSING,
                GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE,
                GateReasonCode.G4_FACT_NOT_WRITABLE,
                GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD,
                GateReasonCode.G4_CONFLICTING_SOURCES,
                GateReasonCode.G4_NARRATIVE_UNSUPPORTED,
            ),
        ),
        GateSpec(
            GateId.G5_COMPLETENESS,
            5,
            "Completeness",
            (
                GateReasonCode.G5_REQUIRED_FIELD_MISSING,
                GateReasonCode.G5_QUESTION_INVALID,
                GateReasonCode.G5_CLARIFICATION_LIMIT,
            ),
        ),
    )
)


def make_gate_decision(
    gate_id: GateId,
    *,
    deterministic_reasons: tuple[GateReasonCode, ...] = (),
    model_blocked: bool = False,
    evidence_refs: tuple[str, ...] = (),
    decided_at: datetime | None = None,
) -> GateDecision:
    """Build a decision without accepting a caller-provided pass/override flag."""

    spec = G0_TO_G5_REGISTRY.spec_for(gate_id)
    known_reasons = set(spec.reason_priority)
    supplied_reasons = set(deterministic_reasons)
    if not supplied_reasons <= known_reasons:
        raise ValueError("Decision contains a reason not registered for this gate")
    if GateReasonCode.G3_MODEL_UNCERTAIN in supplied_reasons:
        raise ValueError("The G3 model reason must be supplied through model_blocked")
    if type(model_blocked) is not bool:
        raise TypeError("model_blocked must be an exact boolean")
    if model_blocked and not spec.model_may_add_block:
        raise ValueError("This gate does not allow model-added blocks")

    reasons = set(supplied_reasons)
    if model_blocked:
        reasons.add(GateReasonCode.G3_MODEL_UNCERTAIN)
    ordered_reasons = tuple(reason for reason in spec.reason_priority if reason in reasons)
    deterministic_passed = not supplied_reasons
    passed = deterministic_passed and not model_blocked
    timestamp = decided_at or datetime.now(UTC)
    if timestamp.utcoffset() is None:
        raise ValueError("Gate decision timestamp must be timezone-aware")
    unique_evidence_refs = tuple(dict.fromkeys(evidence_refs))
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": gate_id.value,
            "deterministicPassed": deterministic_passed,
            "modelBlocked": model_blocked,
            "passed": passed,
            "reasonCodes": [reason.value for reason in ordered_reasons],
            "evidenceRefs": list(unique_evidence_refs),
            "decidedAt": timestamp,
        }
    )
