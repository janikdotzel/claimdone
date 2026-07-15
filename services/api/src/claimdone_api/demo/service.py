"""Canonical provider-free G0-G5 composition for the INT-002 synthetic demo."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from claimdone_api.ai.core import NarrativeInput, build_visible_tool_plan, compose_neutral_narrative
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    AllowedTool,
    CaseState,
    ClaimData,
    ClaimPacket,
    ClaimScope,
    ClarificationAnswerRequest,
    ClarificationStatus,
    ClarificationView,
    CounterpartyKnown,
    EvidenceFact,
    EvidenceField,
    EvidenceItem,
    EvidenceKind,
    FactStatus,
    FieldProvenance,
    GateDecision,
    GateId,
    GateReasonCode,
    PlanStep,
    PortalState,
    ProvenanceRef,
    ProviderCallWorkflowEvent,
    ProviderModelId,
    RequiredClaimField,
    ToolPlan,
    VerificationReport,
    WorkflowOperation,
)
from claimdone_api.gates import (
    G0_TO_G5_REGISTRY,
    ClarificationQuestion,
    ModelExtraction,
    ModelOutputEnvelope,
    SafetyInput,
    evaluate_g2,
    evaluate_g3,
    evaluate_g4,
    evaluate_g5,
)
from claimdone_api.persistence.models import OutputContractAttempt, ProviderWorkflowEmission

from .fixture import (
    INT002_CLARIFICATION_QUESTION,
    INT002_FIXTURE_VERSION,
    INT002_IMAGE_FIXTURES,
    INT002_INCIDENT_TIME,
    INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST,
    INT002_SYNTHETIC_STATEMENT_SHA256,
    INT002_SYNTHETIC_STATEMENT_TEXT,
)
from .models import (
    ApprovedDemoIntake,
    BoundDemoClarification,
    ClarificationIdFactory,
    ConfirmedSyntheticStatement,
    DemoAnalysisInputError,
    DemoAnalysisRequest,
    DemoAnalysisResult,
    DemoClarificationResolution,
    DemoExecutionProof,
    DemoInitialPersistenceInputs,
    GateClock,
    ReconstructedDemoContinuation,
    reject_demo_input,
)

_IMAGE_PROVENANCE = ("prov-int002-image-1", "prov-int002-image-2", "prov-int002-image-3")
_STATEMENT_PROVENANCE = "prov-int002-statement"
_QUESTION_FIELD = RequiredClaimField.INCIDENT_TIME
_SQLITE_INT64_MAX = (1 << 63) - 1

_ANALYSIS_PLAN = ToolPlan.model_validate(
    {
        "agentCanSubmit": False,
        "steps": (
            PlanStep.model_validate(
                {
                    "sequence": 1,
                    "tool": AllowedTool.INSPECT_EVIDENCE.value,
                    "reason": "Inspect only the approved evidence inventory",
                }
            ),
            PlanStep.model_validate(
                {
                    "sequence": 2,
                    "tool": AllowedTool.CHECK_REQUIRED_FIELDS.value,
                    "reason": "Use the deterministic required-field result",
                }
            ),
        ),
    }
)
_SCOPE = ClaimScope.model_validate(
    {
        "environment": "sandbox",
        "scenario": "two_vehicle_rear_end_no_injury",
        "agentCanSubmit": False,
        "finalActionOwner": "human",
    }
)


def analyze_int002_demo(
    request: DemoAnalysisRequest,
    *,
    clock: GateClock,
    clarification_id_factory: ClarificationIdFactory,
) -> DemoAnalysisResult:
    """Compose one deterministic round and fail closed on untrusted input."""

    if (
        not isinstance(request, DemoAnalysisRequest)
        or not callable(clock)
        or not callable(clarification_id_factory)
    ):
        raise reject_demo_input()
    try:
        _validate_approved_intake(request.intake)
        if request.clarification_resolution is None:
            return _analyze_initial(
                request,
                clock=clock,
                clarification_id_factory=clarification_id_factory,
            )
        return _analyze_clarification(request, clock=clock)
    except DemoAnalysisInputError:
        raise
    except (TypeError, ValueError):
        raise reject_demo_input() from None


def reconstruct_int002_clarification(
    *,
    view: ClarificationView,
    prior_packet: ClaimPacket,
) -> ReconstructedDemoContinuation:
    """Rebuild the complete continuation from canonical persisted values after restart.

    The caller needs only ``view`` and ``prior_packet`` loaded from the repository.
    The approved intake is reconstructed and fixture-validated from the canonical
    packet; no previously returned in-memory object is an authority.
    """

    if not isinstance(view, ClarificationView) or not isinstance(prior_packet, ClaimPacket):
        raise reject_demo_input()
    try:
        intake = _reconstruct_approved_intake(prior_packet)
        request = DemoAnalysisRequest(
            case_id=prior_packet.case_id,
            case_version=view.expected_version,
            intake=intake,
        )
        _validate_prior_packet(request, prior_packet)
        _validate_persisted_clarification_view(view, prior_packet)
        clarification = BoundDemoClarification(
            view=view,
            binding_sha256=_clarification_binding(intake, view, prior_packet),
        )
        return ReconstructedDemoContinuation(
            intake=intake,
            clarification=clarification,
            prior_packet=prior_packet,
        )
    except DemoAnalysisInputError:
        raise
    except (TypeError, ValueError):
        raise reject_demo_input() from None


def _analyze_initial(
    request: DemoAnalysisRequest,
    *,
    clock: GateClock,
    clarification_id_factory: ClarificationIdFactory,
) -> DemoAnalysisResult:
    extraction = _build_initial_extraction(request.intake)
    if extraction.evidence != (*request.intake.images, request.intake.statement.evidence):
        raise reject_demo_input()

    envelope = ModelOutputEnvelope(
        payload=extraction.model_dump_json(by_alias=True),
        refusal=False,
        truncated=False,
        attempt=0,
    )
    g2 = evaluate_g2(
        envelope,
        approved_evidence=extraction.evidence,
        decided_at=_gate_time(clock, GateId.G2_OUTPUT_CONTRACT),
    )
    if not g2.decision.passed or g2.extraction != extraction:
        raise reject_demo_input()

    safety_input = SafetyInput(
        injury_reported=False,
        immediate_danger=False,
        portal_is_sandbox=True,
        real_credentials_present=False,
        advice_categories=(),
        requested_actions=(),
        model_signal=None,
        evidence_refs=(_STATEMENT_PROVENANCE,),
    )
    g3 = evaluate_g3(
        safety_input,
        decided_at=_gate_time(clock, GateId.G3_SAFETY_SCOPE),
    )
    if not g3.decision.passed or g3.emergency_stop:
        raise reject_demo_input()

    prefix = _gate_prefix(request.intake, g2.decision, g3.decision)
    provisional = _packet_from_extraction(
        request=request,
        extraction=extraction,
        state=CaseState.ANALYZING,
        plan=_ANALYSIS_PLAN,
        gate_decisions=prefix,
    )
    g4 = evaluate_g4(provisional, decided_at=_gate_time(clock, GateId.G4_PROVENANCE))
    if not g4.decision.passed:
        raise reject_demo_input()
    g5 = evaluate_g5(
        g4,
        proposed_questions=(ClarificationQuestion(_QUESTION_FIELD, INT002_CLARIFICATION_QUESTION),),
        completed_rounds=0,
        decided_at=_gate_time(clock, GateId.G5_COMPLETENESS),
    )
    _validate_initial_g5(g5.decision)
    history = G0_TO_G5_REGISTRY.append(prefix, g4.decision)
    history = G0_TO_G5_REGISTRY.append(history, g5.decision)
    packet = _packet_from_extraction(
        request=request,
        extraction=extraction,
        state=CaseState.AWAITING_CLARIFICATION,
        plan=build_visible_tool_plan(g5),
        gate_decisions=history,
    )

    clarification_id = clarification_id_factory(
        _clarification_seed(request.case_id, request.intake)
    )
    if type(clarification_id) is not str:
        raise reject_demo_input()
    view = ClarificationView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "clarificationId": clarification_id,
            "caseId": request.case_id,
            "field": _QUESTION_FIELD.value,
            "round": 1,
            "question": INT002_CLARIFICATION_QUESTION,
            "status": ClarificationStatus.REQUESTED.value,
            "expectedVersion": _next_case_version(request.case_version),
            "requestedAt": g5.decision.decided_at,
        }
    )
    clarification = BoundDemoClarification(
        view=view,
        binding_sha256=_clarification_binding(request.intake, view, packet),
    )
    provider_event = ProviderCallWorkflowEvent.model_validate(
        {
            "kind": "provider_call",
            "operation": WorkflowOperation.EXTRACTION.value,
            "modelId": ProviderModelId.DETERMINISTIC_MOCK.value,
            "providerMode": "mock",
            "callSequence": 1,
            "retryAttempt": 0,
            "durationMs": 0,
            "status": "succeeded",
            "usage": None,
            "cost": None,
        }
    )
    initial_persistence = DemoInitialPersistenceInputs(
        g2_attempts=(
            OutputContractAttempt(
                envelope=envelope,
                decided_at=g2.decision.decided_at,
            ),
        ),
        safety_input=safety_input,
        provider_events=(
            ProviderWorkflowEmission(
                event=provider_event,
                occurred_at=g2.decision.decided_at,
            ),
        ),
    )
    return _result(
        packet,
        clarification=clarification,
        round_kind="initial",
        new_gate_decisions=history[2:],
        initial_persistence=initial_persistence,
    )


def _analyze_clarification(
    request: DemoAnalysisRequest,
    *,
    clock: GateClock,
) -> DemoAnalysisResult:
    resolution = request.clarification_resolution
    if resolution is None:
        raise reject_demo_input()
    _validate_resolution(request, resolution)
    prior = resolution.prior_packet
    appended_evidence, appended_provenance, answer_fact = _clarification_answer_delta(
        resolution.answer
    )
    evidence = (*prior.evidence, appended_evidence)
    provenance = (*prior.provenance, appended_provenance)
    facts_without_derived = tuple(
        fact
        for fact in prior.facts
        if fact.field not in {EvidenceField.INCIDENT_TIME, EvidenceField.NARRATIVE}
    )
    facts_without_narrative = (*facts_without_derived, answer_fact)
    narrative = compose_neutral_narrative(
        NarrativeInput(
            facts=facts_without_narrative,
            provenance=provenance,
            evidence=evidence,
        )
    )
    if narrative.text is None:
        raise reject_demo_input()
    facts = (
        *facts_without_narrative,
        _fact(
            fact_id="fact-neutral-narrative-2",
            field=EvidenceField.NARRATIVE,
            value=narrative.text,
            status=FactStatus.USER_STATED,
            sources=narrative.source_refs,
        ),
    )

    claim_data = prior.claim.model_dump(mode="json", by_alias=True)
    claim_data["incidentTime"] = INT002_INCIDENT_TIME
    claim_data["narrative"] = narrative.text
    claim_data["missingRequiredFields"] = ()
    claim_data["fieldProvenance"] = (
        *(
            item
            for item in prior.claim.field_provenance
            if item.field not in {_QUESTION_FIELD, RequiredClaimField.NARRATIVE}
        ),
        FieldProvenance.model_validate(
            {
                "field": _QUESTION_FIELD.value,
                "sourceRefs": (appended_provenance.provenance_id,),
            }
        ),
        FieldProvenance.model_validate(
            {
                "field": RequiredClaimField.NARRATIVE.value,
                "sourceRefs": narrative.source_refs,
            }
        ),
    )
    claim = ClaimData.model_validate(claim_data)
    provisional_data = prior.model_dump(mode="json", by_alias=True)
    provisional_data.update(
        {
            "state": CaseState.READY_TO_FILL.value,
            "evidence": evidence,
            "provenance": provenance,
            "facts": facts,
            "claim": claim,
            "verification": _pending_verification(claim),
        }
    )
    provisional = ClaimPacket.model_validate(provisional_data)
    g4 = evaluate_g4(provisional, decided_at=_gate_time(clock, GateId.G4_PROVENANCE))
    if (
        not g4.decision.passed
        or g4.decision.decided_at < resolution.clarification.view.requested_at
    ):
        raise reject_demo_input()
    g5 = evaluate_g5(
        g4,
        proposed_questions=(),
        completed_rounds=resolution.clarification.view.round,
        decided_at=_gate_time(clock, GateId.G5_COMPLETENESS),
    )
    if not g5.decision.passed or g5.blocking_fields or g5.accepted_question is not None:
        raise reject_demo_input()
    emitted = (g4.decision, g5.decision)
    final_data = provisional.model_dump(mode="json", by_alias=True)
    final_data["plan"] = build_visible_tool_plan(g5)
    final_data["gateDecisions"] = (*prior.gate_decisions[:4], *emitted)
    packet = ClaimPacket.model_validate(final_data)
    if packet.gate_decisions[:4] != prior.gate_decisions[:4]:
        raise reject_demo_input()
    return _result(
        packet,
        clarification=None,
        round_kind="clarification",
        new_gate_decisions=emitted,
        initial_persistence=None,
    )


def _reconstruct_approved_intake(prior: ClaimPacket) -> ApprovedDemoIntake:
    """Derive the exact G0/G1-approved fixture intake from one canonical packet."""

    if not _packet_roundtrips(prior) or len(prior.evidence) != 4 or len(prior.gate_decisions) < 2:
        raise reject_demo_input()
    images = prior.evidence[:3]
    statement_evidence = prior.evidence[3]
    confirmations = tuple(
        reference
        for reference in prior.provenance
        if reference.provenance_id == _STATEMENT_PROVENANCE
        and reference.evidence_id == statement_evidence.evidence_id
        and reference.user_confirmed is True
    )
    if len(confirmations) != 1:
        raise reject_demo_input()
    intake = ApprovedDemoIntake(
        images=images,
        statement=ConfirmedSyntheticStatement(
            evidence=statement_evidence,
            confirmed=True,
        ),
        g0_decision=prior.gate_decisions[0],
        g1_decision=prior.gate_decisions[1],
    )
    _validate_approved_intake(intake)
    return intake


def _validate_approved_intake(intake: ApprovedDemoIntake) -> None:
    images = intake.images
    if len(images) != 3:
        raise reject_demo_input()
    if INT002_PROVIDER_COPY_MUST_RETAIN_DIGEST is not True:
        raise RuntimeError("INT-002 V1 provider-copy policy is not closed")
    # V1 intentionally requires the approved provider copies to retain the exact
    # committed fixture bytes.  A privacy transform that changes a digest must
    # fail closed rather than silently changing the staged demo evidence.
    for image, expected in zip(images, INT002_IMAGE_FIXTURES, strict=True):
        if (
            not _evidence_roundtrips(image)
            or image.kind is not EvidenceKind.IMAGE
            or image.media_type != "image/png"
            or image.evidence_id != expected.semantic_id
            or image.sha256 != expected.sha256
            or image.model_copy_approved is not True
        ):
            raise reject_demo_input()
    if (
        len({image.evidence_id for image in images}) != 3
        or len({image.local_ref for image in images}) != 3
    ):
        raise reject_demo_input()

    statement = intake.statement.evidence
    if (
        not _evidence_roundtrips(statement)
        or statement.kind is not EvidenceKind.USER_STATEMENT
        or statement.media_type != "text/plain"
        or statement.text != INT002_SYNTHETIC_STATEMENT_TEXT
        or statement.sha256 != INT002_SYNTHETIC_STATEMENT_SHA256
        or statement.model_copy_approved is not True
        or statement.transcript_confirmed is not None
        or statement.evidence_id in {image.evidence_id for image in images}
        or statement.local_ref in {image.local_ref for image in images}
    ):
        raise reject_demo_input()

    g0, g1 = intake.g0_decision, intake.g1_decision
    if (
        not _gate_decision_roundtrips(g0)
        or not _gate_decision_roundtrips(g1)
        or g0.gate_id is not GateId.G0_INTAKE
        or g1.gate_id is not GateId.G1_PRIVACY
        or not g0.passed
        or not g1.passed
        or g0.evidence_refs
        or g1.evidence_refs
        or g1.decided_at < g0.decided_at
    ):
        raise reject_demo_input()


def _validate_resolution(
    request: DemoAnalysisRequest,
    resolution: DemoClarificationResolution,
) -> None:
    context = resolution.clarification
    view = context.view
    answer = resolution.answer
    prior = resolution.prior_packet
    if (
        not _clarification_view_roundtrips(view)
        or not _clarification_answer_roundtrips(answer)
        or not _packet_roundtrips(prior)
        or context.binding_sha256 != _clarification_binding(request.intake, view, prior)
    ):
        raise reject_demo_input()
    _validate_prior_packet(request, prior)
    _validate_persisted_clarification_view(view, prior)
    if (
        view.case_id != request.case_id
        or request.case_version != view.expected_version
        or answer.case_id != view.case_id
        or answer.clarification_id != view.clarification_id
        or answer.field is not view.field
        or answer.round != view.round
        or answer.expected_version != view.expected_version
        or answer.answer != INT002_INCIDENT_TIME
    ):
        raise reject_demo_input()


def _validate_persisted_clarification_view(
    view: ClarificationView,
    prior: ClaimPacket,
) -> None:
    if (
        not _clarification_view_roundtrips(view)
        or view.case_id != prior.case_id
        or view.field is not _QUESTION_FIELD
        or view.round != 1
        or view.question != INT002_CLARIFICATION_QUESTION
        or view.status is not ClarificationStatus.REQUESTED
        or view.expected_version < 2
        or view.requested_at != prior.gate_decisions[-1].decided_at
    ):
        raise reject_demo_input()


def _validate_prior_packet(request: DemoAnalysisRequest, prior: ClaimPacket) -> None:
    extraction = _build_initial_extraction(request.intake)
    expected_evidence = (*request.intake.images, request.intake.statement.evidence)
    expected_gate_ids = tuple(GateId(f"G{index}") for index in range(6))
    if (
        prior.case_id != request.case_id
        or prior.state is not CaseState.AWAITING_CLARIFICATION
        or prior.portal_state is not PortalState.DRAFT
        or prior.scope != _SCOPE
        or prior.evidence != expected_evidence
        or prior.gate_decisions[:2] != (request.intake.g0_decision, request.intake.g1_decision)
        or tuple(item.gate_id for item in prior.gate_decisions) != expected_gate_ids
        or not all(item.passed for item in prior.gate_decisions[:5])
        or prior.gate_decisions[5].reason_codes != (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)
    ):
        raise reject_demo_input()
    try:
        G0_TO_G5_REGISTRY.validate_history(prior.gate_decisions)
    except ValueError as error:
        raise reject_demo_input() from error

    g2, g3 = prior.gate_decisions[2], prior.gate_decisions[3]
    if (
        not _gate_decision_roundtrips(g2)
        or not _gate_decision_roundtrips(g3)
        or g2.reason_codes
        or g2.evidence_refs != tuple(reference.provenance_id for reference in extraction.provenance)
        or g3.reason_codes
        or g3.evidence_refs != (_STATEMENT_PROVENANCE,)
    ):
        raise reject_demo_input()
    provisional = _packet_from_extraction(
        request=request,
        extraction=extraction,
        state=CaseState.ANALYZING,
        plan=_ANALYSIS_PLAN,
        gate_decisions=prior.gate_decisions[:4],
    )
    recomputed_g4 = evaluate_g4(
        provisional,
        decided_at=prior.gate_decisions[4].decided_at,
    )
    recomputed_g5 = evaluate_g5(
        recomputed_g4,
        proposed_questions=(ClarificationQuestion(_QUESTION_FIELD, INT002_CLARIFICATION_QUESTION),),
        completed_rounds=0,
        decided_at=prior.gate_decisions[5].decided_at,
    )
    if (
        recomputed_g4.decision != prior.gate_decisions[4]
        or recomputed_g5.decision != prior.gate_decisions[5]
        or recomputed_g5.accepted_question is None
    ):
        raise reject_demo_input()
    expected_packet = _packet_from_extraction(
        request=request,
        extraction=extraction,
        state=CaseState.AWAITING_CLARIFICATION,
        plan=build_visible_tool_plan(recomputed_g5),
        gate_decisions=prior.gate_decisions,
    )
    if prior != expected_packet:
        raise reject_demo_input()


def _clarification_answer_delta(
    answer: ClarificationAnswerRequest,
) -> tuple[EvidenceItem, ProvenanceRef, EvidenceFact]:
    raw_answer = answer.answer
    digest = hashlib.sha256(raw_answer.encode()).hexdigest()
    identity = hashlib.sha256(
        (
            "claimdone-clarification-v1\0"
            f"{answer.case_id}\0{answer.clarification_id}\0{answer.round}\0{digest}"
        ).encode()
    ).hexdigest()
    suffix = identity[:32]
    evidence_id = f"clarification-{suffix}"
    provenance_id = f"provenance-{suffix}"
    evidence = EvidenceItem.model_validate(
        {
            "evidenceId": evidence_id,
            "kind": EvidenceKind.CLARIFICATION.value,
            "localRef": f"clarification-{suffix}.txt",
            "mediaType": "text/plain",
            "sha256": digest,
            "text": raw_answer,
            "modelCopyApproved": True,
            "transcriptConfirmed": None,
        }
    )
    provenance = ProvenanceRef.model_validate(
        {
            "provenanceId": provenance_id,
            "evidenceId": evidence_id,
            "locator": "clarification answer",
            "userConfirmed": True,
        }
    )
    fact = _fact(
        fact_id=f"fact-{suffix}",
        field=EvidenceField.INCIDENT_TIME,
        value=INT002_INCIDENT_TIME,
        status=FactStatus.USER_STATED,
        sources=(provenance_id,),
    )
    return evidence, provenance, fact


def _build_initial_extraction(intake: ApprovedDemoIntake) -> ModelExtraction:
    evidence = (*intake.images, intake.statement.evidence)
    provenance = (
        *(
            ProvenanceRef.model_validate(
                {
                    "provenanceId": provenance_id,
                    "evidenceId": image.evidence_id,
                    "locator": f"approved synthetic image {index}",
                    "userConfirmed": False,
                }
            )
            for index, (image, provenance_id) in enumerate(
                zip(intake.images, _IMAGE_PROVENANCE, strict=True),
                start=1,
            )
        ),
        ProvenanceRef.model_validate(
            {
                "provenanceId": _STATEMENT_PROVENANCE,
                "evidenceId": intake.statement.evidence.evidence_id,
                "locator": "confirmed versioned synthetic statement",
                "userConfirmed": True,
            }
        ),
    )
    fixed_values: tuple[tuple[EvidenceField, object], ...] = (
        (EvidenceField.INCIDENT_DATE, "2026-07-14"),
        (EvidenceField.LOCATION, "Demo Street 1, Berlin"),
        (EvidenceField.CLAIMANT_NAME, "Demo Claimant"),
        (EvidenceField.POLICY_REFERENCE, "DEMO-POLICY-001"),
        (EvidenceField.VEHICLE_REGISTRATION, "DEMO-CD-1"),
        (EvidenceField.COUNTERPARTY_KNOWN, CounterpartyKnown.YES.value),
    )
    facts: list[EvidenceFact] = [
        *(
            _fact(
                fact_id=f"fact-int002-{field.value.replace('_', '-')}",
                field=field,
                value=value,
                status=FactStatus.USER_STATED,
                sources=(_STATEMENT_PROVENANCE,),
            )
            for field, value in fixed_values
        ),
        _fact(
            fact_id="fact-int002-vehicle-count",
            field=EvidenceField.VEHICLE_COUNT,
            value=2,
            status=FactStatus.USER_STATED,
            sources=(_STATEMENT_PROVENANCE,),
        ),
        _fact(
            fact_id="fact-int002-collision-type",
            field=EvidenceField.COLLISION_TYPE,
            value="rear_end",
            status=FactStatus.USER_STATED,
            sources=(_STATEMENT_PROVENANCE,),
        ),
        _fact(
            fact_id="fact-int002-injury-status",
            field=EvidenceField.INJURY_STATUS,
            value=False,
            status=FactStatus.USER_STATED,
            sources=(_STATEMENT_PROVENANCE,),
        ),
        _fact(
            fact_id="fact-int002-immediate-danger",
            field=EvidenceField.IMMEDIATE_DANGER,
            value=False,
            status=FactStatus.USER_STATED,
            sources=(_STATEMENT_PROVENANCE,),
        ),
    ]
    narrative = compose_neutral_narrative(
        NarrativeInput(facts=tuple(facts), provenance=provenance, evidence=evidence)
    )
    if narrative.text is None:
        raise reject_demo_input()
    facts.append(
        _fact(
            fact_id="fact-int002-neutral-narrative",
            field=EvidenceField.NARRATIVE,
            value=narrative.text,
            status=FactStatus.USER_STATED,
            sources=narrative.source_refs,
        )
    )
    statement_sources = (_STATEMENT_PROVENANCE,)
    claim_sources: dict[RequiredClaimField, tuple[str, ...]] = {
        RequiredClaimField.INCIDENT_DATE: statement_sources,
        RequiredClaimField.LOCATION: statement_sources,
        RequiredClaimField.CLAIMANT_NAME: statement_sources,
        RequiredClaimField.POLICY_REFERENCE: statement_sources,
        RequiredClaimField.VEHICLE_REGISTRATION: statement_sources,
        RequiredClaimField.COUNTERPARTY_KNOWN: statement_sources,
        RequiredClaimField.NARRATIVE: narrative.source_refs,
        RequiredClaimField.ATTACHMENTS: _IMAGE_PROVENANCE,
    }
    claim = ClaimData.model_validate(
        {
            "incidentDate": "2026-07-14",
            "incidentTime": None,
            "location": "Demo Street 1, Berlin",
            "claimantName": "Demo Claimant",
            "policyReference": "DEMO-POLICY-001",
            "vehicleRegistration": "DEMO-CD-1",
            "counterpartyKnown": CounterpartyKnown.YES.value,
            "narrative": narrative.text,
            "attachments": tuple(image.local_ref for image in intake.images),
            "missingRequiredFields": (_QUESTION_FIELD.value,),
            "fieldProvenance": tuple(
                FieldProvenance.model_validate({"field": field.value, "sourceRefs": sources})
                for field in RequiredClaimField
                if (sources := claim_sources.get(field)) is not None
            ),
        }
    )
    return ModelExtraction.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "evidence": evidence,
            "provenance": provenance,
            "facts": tuple(facts),
            "claim": claim,
        }
    )


def _fact(
    *,
    fact_id: str,
    field: EvidenceField,
    value: object,
    status: FactStatus,
    sources: tuple[str, ...],
    confidence: float | None = None,
) -> EvidenceFact:
    return EvidenceFact.model_validate(
        {
            "factId": fact_id,
            "field": field.value,
            "value": value,
            "status": status.value,
            "sourceRefs": sources,
            "confidence": confidence,
        }
    )


def _gate_prefix(
    intake: ApprovedDemoIntake,
    g2: GateDecision,
    g3: GateDecision,
) -> tuple[GateDecision, ...]:
    history: tuple[GateDecision, ...] = ()
    for decision in (intake.g0_decision, intake.g1_decision, g2, g3):
        history = G0_TO_G5_REGISTRY.append(history, decision)
    return history


def _packet_from_extraction(
    *,
    request: DemoAnalysisRequest,
    extraction: ModelExtraction,
    state: CaseState,
    plan: ToolPlan,
    gate_decisions: tuple[GateDecision, ...],
) -> ClaimPacket:
    return ClaimPacket.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": request.case_id,
            "state": state.value,
            "portalState": PortalState.DRAFT.value,
            "scope": _SCOPE,
            "evidence": extraction.evidence,
            "provenance": extraction.provenance,
            "facts": extraction.facts,
            "claim": extraction.claim,
            "plan": plan,
            "gateDecisions": gate_decisions,
            "verification": _pending_verification(extraction.claim),
        }
    )


def _pending_verification(claim: ClaimData) -> VerificationReport:
    return VerificationReport.model_validate(
        {
            "status": "pending",
            "deterministicMatch": None,
            "modelReportedMismatch": False,
            "fieldResults": (),
            "expectedAttachmentCount": 3,
            "expectedAttachmentIds": claim.attachments,
            "actualAttachmentCount": None,
            "actualAttachmentIds": None,
            "reviewAllowed": False,
            "verifiedAt": None,
        }
    )


def _validate_initial_g5(decision: GateDecision) -> None:
    if (
        decision.gate_id is not GateId.G5_COMPLETENESS
        or decision.passed
        or decision.reason_codes != (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)
    ):
        raise reject_demo_input()


def _gate_time(clock: GateClock, gate_id: GateId) -> datetime:
    value = clock(gate_id)
    if type(value) is not datetime or value.utcoffset() is None:
        raise reject_demo_input()
    return value


def _next_case_version(current: int) -> int:
    if type(current) is not int or current < 1 or current >= _SQLITE_INT64_MAX:
        raise reject_demo_input()
    return current + 1


def _clarification_seed(case_id: str, intake: ApprovedDemoIntake) -> str:
    payload = {
        "caseId": case_id,
        "fixtureVersion": INT002_FIXTURE_VERSION,
        "images": [
            {
                "evidenceId": image.evidence_id,
                "localRef": image.local_ref,
                "mediaType": image.media_type,
                "sha256": image.sha256,
            }
            for image in intake.images
        ],
        "statementEvidenceId": intake.statement.evidence.evidence_id,
        "statementLocalRef": intake.statement.evidence.local_ref,
        "statementSha256": intake.statement.evidence.sha256,
    }
    return _sha256_json(payload)


def _clarification_binding(
    intake: ApprovedDemoIntake,
    view: ClarificationView,
    prior_packet: ClaimPacket,
) -> str:
    return _sha256_json(
        {
            "seed": _clarification_seed(view.case_id, intake),
            "view": view.model_dump(mode="json", by_alias=True),
            "priorPacketSha256": _sha256_json(prior_packet.model_dump(mode="json", by_alias=True)),
        }
    )


def _result(
    packet: ClaimPacket,
    *,
    clarification: BoundDemoClarification | None,
    round_kind: Literal["initial", "clarification"],
    new_gate_decisions: tuple[GateDecision, ...],
    initial_persistence: DemoInitialPersistenceInputs | None,
) -> DemoAnalysisResult:
    mock_event_count: Literal[0, 1] = 1 if initial_persistence is not None else 0
    return DemoAnalysisResult(
        packet=packet,
        clarification=clarification,
        execution=DemoExecutionProof(
            mode="deterministic_demo_fixture",
            fixture_version=INT002_FIXTURE_VERSION,
            external_provider_call_count=0,
            mock_provider_event_count=mock_event_count,
            semantic_sha256=_semantic_digest(
                packet,
                clarification,
                mock_provider_event_count=mock_event_count,
            ),
        ),
        round_kind=round_kind,
        new_gate_decisions=new_gate_decisions,
        initial_persistence=initial_persistence,
    )


def _semantic_digest(
    packet: ClaimPacket,
    clarification: BoundDemoClarification | None,
    *,
    mock_provider_event_count: Literal[0, 1],
) -> str:
    evidence_positions = {
        item.evidence_id: index for index, item in enumerate(packet.evidence, start=1)
    }
    provenance_positions = {
        item.provenance_id: index for index, item in enumerate(packet.provenance, start=1)
    }
    attachment_positions = {
        item.local_ref: evidence_positions[item.evidence_id]
        for item in packet.evidence
        if item.kind is EvidenceKind.IMAGE
    }

    evidence_data = [
        {
            "position": index,
            "kind": item.kind.value,
            "mediaType": item.media_type,
            "sha256": item.sha256,
            "text": item.text,
            "modelCopyApproved": item.model_copy_approved,
            "transcriptConfirmed": item.transcript_confirmed,
        }
        for index, item in enumerate(packet.evidence, start=1)
    ]
    provenance_data = [
        {
            "position": index,
            "evidencePosition": evidence_positions[item.evidence_id],
            "locator": item.locator,
            "userConfirmed": item.user_confirmed,
        }
        for index, item in enumerate(packet.provenance, start=1)
    ]
    fact_data = [
        {
            "position": index,
            "field": item.field.value,
            "value": item.value,
            "status": item.status.value,
            "sourcePositions": _semantic_positions(
                item.source_refs,
                provenance_positions,
            ),
            "confidence": item.confidence,
        }
        for index, item in enumerate(packet.facts, start=1)
    ]

    claim_data = packet.claim.model_dump(mode="json", by_alias=True)
    claim_data["attachmentPositions"] = _semantic_positions(
        packet.claim.attachments,
        attachment_positions,
    )
    claim_data.pop("attachments")
    claim_data["fieldProvenance"] = [
        {
            "field": item.field.value,
            "sourcePositions": _semantic_positions(
                item.source_refs,
                provenance_positions,
            ),
        }
        for item in packet.claim.field_provenance
    ]

    gate_data = [
        {
            "gateId": item.gate_id.value,
            "deterministicPassed": item.deterministic_passed,
            "modelBlocked": item.model_blocked,
            "passed": item.passed,
            "reasonCodes": tuple(reason.value for reason in item.reason_codes),
            "evidencePositions": _semantic_positions(
                item.evidence_refs,
                provenance_positions,
            ),
        }
        for item in packet.gate_decisions
    ]

    verification_data = packet.verification.model_dump(mode="json", by_alias=True)
    verification_data.pop("verifiedAt")
    verification_data["expectedAttachmentPositions"] = _semantic_positions(
        packet.verification.expected_attachment_ids,
        attachment_positions,
    )
    verification_data.pop("expectedAttachmentIds")
    actual_ids = packet.verification.actual_attachment_ids
    verification_data["actualAttachmentPositions"] = (
        None if actual_ids is None else _semantic_positions(actual_ids, attachment_positions)
    )
    verification_data.pop("actualAttachmentIds")
    verification_data["fieldResults"] = [
        {
            "field": item.field.value,
            "expected": item.expected,
            "actual": item.actual,
            "status": item.status.value,
            "sourcePositions": _semantic_positions(
                item.source_refs,
                provenance_positions,
            ),
        }
        for item in packet.verification.field_results
    ]

    packet_data = {
        "contractVersion": packet.contract_version,
        "state": packet.state.value,
        "portalState": packet.portal_state.value,
        "scope": packet.scope.model_dump(mode="json", by_alias=True),
        "evidence": evidence_data,
        "provenance": provenance_data,
        "facts": fact_data,
        "claim": claim_data,
        "plan": packet.plan.model_dump(mode="json", by_alias=True),
        "gateDecisions": gate_data,
        "verification": verification_data,
    }
    clarification_data: dict[str, Any] | None = None
    if clarification is not None:
        view = clarification.view
        clarification_data = {
            "contractVersion": view.contract_version,
            "field": view.field.value,
            "round": view.round,
            "question": view.question,
            "status": view.status.value,
        }
    return _sha256_json(
        {
            "fixtureVersion": INT002_FIXTURE_VERSION,
            "packet": packet_data,
            "clarification": clarification_data,
            "externalProviderCallCount": 0,
            "mockProviderEventCount": mock_provider_event_count,
        }
    )


def _semantic_positions(
    values: tuple[str, ...],
    positions: dict[str, int],
) -> tuple[int, ...]:
    try:
        return tuple(positions[value] for value in values)
    except KeyError as error:
        raise ValueError("Invalid deterministic semantic reference") from error


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _evidence_roundtrips(value: EvidenceItem) -> bool:
    try:
        return EvidenceItem.model_validate(value.model_dump(mode="json", by_alias=True)) == value
    except ValueError:
        return False


def _gate_decision_roundtrips(value: GateDecision) -> bool:
    try:
        return GateDecision.model_validate(value.model_dump(mode="json", by_alias=True)) == value
    except ValueError:
        return False


def _clarification_view_roundtrips(value: ClarificationView) -> bool:
    try:
        return (
            ClarificationView.model_validate(value.model_dump(mode="json", by_alias=True)) == value
        )
    except ValueError:
        return False


def _clarification_answer_roundtrips(value: ClarificationAnswerRequest) -> bool:
    try:
        return (
            ClarificationAnswerRequest.model_validate(value.model_dump(mode="json", by_alias=True))
            == value
        )
    except ValueError:
        return False


def _packet_roundtrips(value: ClaimPacket) -> bool:
    try:
        return ClaimPacket.model_validate(value.model_dump(mode="json", by_alias=True)) == value
    except ValueError:
        return False
