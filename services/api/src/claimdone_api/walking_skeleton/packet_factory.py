"""Trusted ClaimPacket assembly; model output cannot express authority fields."""

from claimdone_api.contracts import (
    AllowedTool,
    CaseState,
    ClaimPacket,
    GateDecision,
    PlanStep,
    PortalState,
    ToolPlan,
)
from claimdone_api.gates import ModelExtraction


def build_packet(
    *,
    case_id: str,
    state: CaseState,
    portal_state: PortalState,
    extraction: ModelExtraction,
    gate_decisions: tuple[GateDecision, ...],
) -> ClaimPacket:
    plan = ToolPlan.model_validate(
        {
            "agentCanSubmit": False,
            "steps": tuple(
                PlanStep.model_validate(
                    {
                        "sequence": sequence,
                        "tool": tool.value,
                        "reason": reason,
                    }
                )
                for sequence, (tool, reason) in enumerate(
                    (
                        (AllowedTool.INSPECT_EVIDENCE, "Inspect approved staged evidence"),
                        (AllowedTool.CHECK_REQUIRED_FIELDS, "Check every required draft field"),
                        (AllowedTool.ASK_CLARIFICATION, "Ask only for the missing incident time"),
                        (AllowedTool.INSPECT_FORM, "Inspect sandbox Portal A"),
                        (AllowedTool.FILL_UNTIL_REVIEW, "Fill only until review"),
                    ),
                    start=1,
                )
            ),
        }
    )
    return ClaimPacket.model_validate(
        {
            "contractVersion": "1.0.0",
            "caseId": case_id,
            "state": state.value,
            "portalState": portal_state.value,
            "scope": {
                "environment": "sandbox",
                "scenario": "two_vehicle_rear_end_no_injury",
                "agentCanSubmit": False,
                "finalActionOwner": "human",
            },
            "evidence": extraction.evidence,
            "provenance": extraction.provenance,
            "facts": extraction.facts,
            "claim": extraction.claim,
            "plan": plan,
            "gateDecisions": gate_decisions,
            "verification": {
                "status": "pending",
                "deterministicMatch": None,
                "modelReportedMismatch": False,
                "fieldResults": (),
                "expectedAttachmentCount": 3,
                "actualAttachmentCount": None,
                "reviewAllowed": False,
                "verifiedAt": None,
            },
        }
    )
