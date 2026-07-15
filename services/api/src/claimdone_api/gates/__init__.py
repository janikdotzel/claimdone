"""Deterministic ClaimDone gate registry and G2-G8 evaluators."""

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
from .g6_tool_authority import (
    CANONICAL_PORTAL_ORIGIN,
    MAX_G6_ACTIONS,
    MAX_G6_SECONDS,
    ToolAuthorityContext,
    ToolAuthorityResult,
    canonical_portal_case_url,
    evaluate_g6,
)
from .g7_portal_write import PortalWriteInputError, PortalWriteResult, evaluate_g7
from .g8_verification import (
    MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS,
    VerificationInputError,
    VerificationResult,
    evaluate_g8,
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
    "CANONICAL_PORTAL_ORIGIN",
    "G0_TO_G5_REGISTRY",
    "G0_TO_G10_REGISTRY",
    "MAX_CLARIFICATION_ROUNDS",
    "MAX_G6_ACTIONS",
    "MAX_G6_SECONDS",
    "MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS",
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
    "PortalWriteInputError",
    "PortalWriteResult",
    "ProvenanceResult",
    "RequestedAction",
    "SafetyInput",
    "SafetyResult",
    "ToolAuthorityContext",
    "ToolAuthorityResult",
    "VerificationInputError",
    "VerificationResult",
    "canonical_portal_case_url",
    "compute_missing_required_fields",
    "evaluate_g2",
    "evaluate_g3",
    "evaluate_g4",
    "evaluate_g5",
    "evaluate_g6",
    "evaluate_g7",
    "evaluate_g8",
    "make_gate_decision",
]
