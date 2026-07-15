"""Trusted ClaimPacket assembly; model output cannot express authority fields."""

import hashlib

from claimdone_api.ai import NarrativeInput, compose_neutral_narrative
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    AllowedTool,
    CaseState,
    ClaimData,
    ClaimPacket,
    EvidenceFact,
    EvidenceField,
    FactStatus,
    FieldProvenance,
    GateDecision,
    PlanStep,
    PortalState,
    RequiredClaimField,
    ToolPlan,
)
from claimdone_api.gates import ModelExtraction


def project_deterministic_narrative(extraction: ModelExtraction) -> ModelExtraction:
    """Discard any model narrative and derive the only packet-safe replacement."""

    non_narrative_facts = tuple(
        fact for fact in extraction.facts if fact.field is not EvidenceField.NARRATIVE
    )
    result = compose_neutral_narrative(
        NarrativeInput(
            facts=non_narrative_facts,
            provenance=extraction.provenance,
            evidence=extraction.evidence,
        )
    )
    facts = non_narrative_facts
    if result.text is not None:
        identity = hashlib.sha256(
            (result.text + "\0" + "\0".join(result.source_refs)).encode("utf-8")
        ).hexdigest()
        facts = (
            *facts,
            EvidenceFact.model_validate(
                {
                    "factId": f"fact-neutral-narrative-{identity[:16]}",
                    "field": EvidenceField.NARRATIVE,
                    "value": result.text,
                    "status": FactStatus.USER_STATED,
                    "sourceRefs": result.source_refs,
                    "confidence": None,
                }
            ),
        )

    claim_json = extraction.claim.model_dump(mode="json", by_alias=True)
    claim_json["narrative"] = result.text
    claim_json["fieldProvenance"] = tuple(
        item
        for item in extraction.claim.field_provenance
        if item.field is not RequiredClaimField.NARRATIVE
    )
    if result.text is not None:
        claim_json["fieldProvenance"] = (
            *claim_json["fieldProvenance"],
            FieldProvenance.model_validate(
                {
                    "field": RequiredClaimField.NARRATIVE,
                    "sourceRefs": result.source_refs,
                }
            ),
        )
    nullable = {
        RequiredClaimField.INCIDENT_DATE: claim_json["incidentDate"],
        RequiredClaimField.INCIDENT_TIME: claim_json["incidentTime"],
        RequiredClaimField.LOCATION: claim_json["location"],
        RequiredClaimField.CLAIMANT_NAME: claim_json["claimantName"],
        RequiredClaimField.POLICY_REFERENCE: claim_json["policyReference"],
        RequiredClaimField.VEHICLE_REGISTRATION: claim_json["vehicleRegistration"],
        RequiredClaimField.NARRATIVE: claim_json["narrative"],
    }
    claim_json["missingRequiredFields"] = tuple(
        field for field in RequiredClaimField if field in nullable and nullable[field] is None
    )
    claim = ClaimData.model_validate(claim_json)
    return ModelExtraction.model_validate(
        {
            "contractVersion": extraction.contract_version,
            "evidence": extraction.evidence,
            "provenance": extraction.provenance,
            "facts": facts,
            "claim": claim,
        }
    )


def build_packet(
    *,
    case_id: str,
    state: CaseState,
    portal_state: PortalState,
    extraction: ModelExtraction,
    gate_decisions: tuple[GateDecision, ...],
) -> ClaimPacket:
    extraction = project_deterministic_narrative(extraction)
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
            "contractVersion": CONTRACT_VERSION,
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
                "expectedAttachmentIds": extraction.claim.attachments,
                "actualAttachmentCount": None,
                "actualAttachmentIds": None,
                "reviewAllowed": False,
                "verifiedAt": None,
            },
        }
    )
