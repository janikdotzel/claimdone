"""Deterministic ClaimDone gate registry and G2-G5 evaluators."""

from .g2_output import (
    G2RunError,
    ModelExtraction,
    ModelOutputEnvelope,
    OutputContractResult,
    OutputContractRun,
    evaluate_g2,
)
from .g3_safety import (
    AdviceCategory,
    ModelSafetySignal,
    RequestedAction,
    SafetyInput,
    SafetyResult,
    evaluate_g3,
)
from .g4_provenance import (
    PROVENANCE_CONFIDENCE_THRESHOLD,
    ProvenanceResult,
    evaluate_g4,
)
from .g5_completeness import (
    MAX_CLARIFICATION_ROUNDS,
    ClarificationQuestion,
    ClarificationSubflow,
    ClarificationSubflowError,
    CompletenessResult,
    compute_missing_required_fields,
    evaluate_g5,
)
from .registry import (
    G0_TO_G5_REGISTRY,
    G0_TO_G10_REGISTRY,
    GateOrderError,
    GateRegistry,
    GateSpec,
    make_gate_decision,
)

__all__ = [
    "G0_TO_G5_REGISTRY",
    "G0_TO_G10_REGISTRY",
    "MAX_CLARIFICATION_ROUNDS",
    "PROVENANCE_CONFIDENCE_THRESHOLD",
    "AdviceCategory",
    "ClarificationQuestion",
    "ClarificationSubflow",
    "ClarificationSubflowError",
    "CompletenessResult",
    "G2RunError",
    "GateOrderError",
    "GateRegistry",
    "GateSpec",
    "ModelExtraction",
    "ModelOutputEnvelope",
    "ModelSafetySignal",
    "OutputContractResult",
    "OutputContractRun",
    "ProvenanceResult",
    "RequestedAction",
    "SafetyInput",
    "SafetyResult",
    "compute_missing_required_fields",
    "evaluate_g2",
    "evaluate_g3",
    "evaluate_g4",
    "evaluate_g5",
    "make_gate_decision",
]
