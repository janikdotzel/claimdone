"""Atomic analysis, clarification, and terminal provider-failure persistence."""

import hashlib
import json
import sqlite3
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import ValidationError

from claimdone_api.ai import (
    ProviderCallStatus,
    ProviderCallTelemetry,
    ProviderMode,
    TranscriptionSuccess,
)
from claimdone_api.ai.core import NarrativeInput, compose_neutral_narrative
from claimdone_api.audit import build_gate_audit_event, build_state_change_event
from claimdone_api.cases import CaseService
from claimdone_api.cases.errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    ActorType,
    AuditEvent,
    CaseState,
    ClaimPacket,
    ClarificationAnswerRequest,
    ClarificationStatus,
    ClarificationView,
    ClarificationWorkflowEvent,
    CounterpartyKnown,
    EvidenceFact,
    EvidenceField,
    EvidenceItem,
    FieldProvenance,
    GateDecision,
    GateId,
    GateReasonCode,
    GateWorkflowEvent,
    OperationalFailureWorkflowEvent,
    PlanStepWorkflowEvent,
    PortalFillWorkflowEvent,
    ProvenanceRef,
    ProviderCallWorkflowEvent,
    ProviderModelId,
    RequiredClaimField,
    RetryWorkflowEvent,
    StateWorkflowEvent,
    ToolCallWorkflowEvent,
    TranscriptConfirmationRequest,
    VerificationWorkflowEvent,
    WorkflowEventEnvelope,
    WorkflowEventKind,
    WorkflowOperation,
)
from claimdone_api.gates import (
    AdviceCategory,
    ModelExtraction,
    ModelOutputEnvelope,
    ModelSafetySignal,
    OutputContractRun,
    SafetyInput,
    evaluate_g2,
    evaluate_g3,
    evaluate_g4,
)
from claimdone_api.media import (
    AudioUpload,
    ExifChoice,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
    PrivacyReview,
)
from claimdone_api.persistence import (
    AnalysisWorkflowCommand,
    AuthorityModeMismatchError,
    CaseRecord,
    CaseRecordVersionConflictError,
    IntakeDisclosureCommand,
    OutputContractAttempt,
    PersistedDataIntegrityError,
    ProviderWorkflowEmission,
    SqliteCaseRepository,
    TerminalProviderFailureCommand,
    TranscriptionOutcomeCommand,
    WorkflowAtomicityError,
)
from claimdone_api.walking_skeleton.packet_factory import (
    project_deterministic_narrative,
)

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
SQLITE_INT64_MAX = 9_223_372_036_854_775_807
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
GATE_SEQUENCE = (
    GateId.G0_INTAKE,
    GateId.G1_PRIVACY,
    GateId.G2_OUTPUT_CONTRACT,
    GateId.G3_SAFETY_SCOPE,
    GateId.G4_PROVENANCE,
    GateId.G5_COMPLETENESS,
)
STATEMENT_TEXT = (
    "2026-07-14 14:30:00 Berlin Demo Claimant DEMO-42 DEMO-CD-1; "
    "the counterparty is known, nobody was injured, and there is no immediate danger."
)
TRANSCRIPT_DIGEST = hashlib.sha256(STATEMENT_TEXT.encode("utf-8")).hexdigest()
QUESTION_BY_FIELD = {
    RequiredClaimField.INCIDENT_DATE: "An welchem Datum ereignete sich der Vorfall?",
    RequiredClaimField.INCIDENT_TIME: "Wann ereignete sich der Vorfall?",
    RequiredClaimField.LOCATION: "Wo ereignete sich der Vorfall?",
    RequiredClaimField.CLAIMANT_NAME: "Wie lautet der Name der anspruchstellenden Person?",
    RequiredClaimField.POLICY_REFERENCE: "Wie lautet die Demo-Policennummer?",
    RequiredClaimField.VEHICLE_REGISTRATION: "Wie lautet das Demo-Kennzeichen?",
    RequiredClaimField.COUNTERPARTY_KNOWN: "Ist eine Gegenpartei bekannt?",
}


def _pending_summary() -> dict[str, Any]:
    return {
        "images": [
            {
                "inputId": f"image-{index}",
                "source": {
                    "fileId": f"local-ref-{index}",
                    "mediaType": "image/png" if index == 3 else "image/jpeg",
                    "sha256": character * 64,
                },
                "imageFormat": "png" if index == 3 else "jpeg",
            }
            for index, character in ((1, "a"), (2, "b"), (3, "c"))
        ],
        "text": None,
        "audio": {
            "fileId": f"audio-{'2' * 32}.wav",
            "mediaType": "audio/wav",
            "sha256": "c" * 64,
        },
        "statement": {
            "fileId": f"transcript-{'3' * 32}.txt",
            "mediaType": "text/plain",
            "sha256": TRANSCRIPT_DIGEST,
        },
    }


def _text_summary() -> dict[str, Any]:
    summary = _pending_summary()
    summary["audio"] = None
    return summary


def _gate(
    gate_id: GateId,
    offset: int,
    *reasons: GateReasonCode,
) -> GateDecision:
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": gate_id,
            "deterministicPassed": not reasons,
            "modelBlocked": False,
            "passed": not reasons,
            "reasonCodes": reasons,
            "evidenceRefs": (),
            "decidedAt": NOW + timedelta(seconds=offset),
        }
    )


def _provider_call(
    *,
    call_sequence: int = 1,
    retry_attempt: int = 0,
) -> ProviderCallWorkflowEvent:
    return ProviderCallWorkflowEvent.model_validate(
        {
            "kind": "provider_call",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": 20,
            "status": "succeeded",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            "cost": None,
        }
    )


def _retry() -> RetryWorkflowEvent:
    return RetryWorkflowEvent.model_validate(
        {
            "kind": "retry",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": 1,
            "retryAttempt": 1,
            "durationMs": 20,
            "failure": {
                "category": "invalid_response",
                "retryable": True,
                "terminal": False,
            },
        }
    )


def _timeout_retry() -> RetryWorkflowEvent:
    return RetryWorkflowEvent.model_validate(
        {
            "kind": "retry",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": 1,
            "retryAttempt": 1,
            "durationMs": 20,
            "failure": {
                "category": "timeout",
                "retryable": True,
                "terminal": False,
            },
        }
    )


def _operational_failure(
    category: str = "quota_exhausted",
    *,
    call_sequence: int = 1,
    retry_attempt: int = 0,
) -> OperationalFailureWorkflowEvent:
    return OperationalFailureWorkflowEvent.model_validate(
        {
            "kind": "operational_failure",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": 30,
            "failure": {
                "category": category,
                "retryable": False,
                "terminal": True,
            },
        }
    )


def _transcription_operational_failure() -> OperationalFailureWorkflowEvent:
    return OperationalFailureWorkflowEvent.model_validate(
        {
            "kind": "operational_failure",
            "operation": "transcription",
            "modelId": "gpt-4o-transcribe",
            "providerMode": "live",
            "callSequence": 1,
            "retryAttempt": 0,
            "durationMs": 30,
            "failure": {
                "category": "quota_exhausted",
                "retryable": False,
                "terminal": True,
            },
        }
    )


def _packet(
    case_id: str,
    state: CaseState,
    gates: tuple[GateDecision, ...],
    *,
    missing: bool,
    intake_summary: dict[str, Any] | None = None,
    missing_fields: tuple[RequiredClaimField, ...] | None = None,
    injury_reported: bool = False,
    immediate_danger: bool = False,
) -> ClaimPacket:
    summary = _text_summary() if intake_summary is None else intake_summary
    selected_missing = (
        ((RequiredClaimField.INCIDENT_TIME,) if missing else ())
        if missing_fields is None
        else missing_fields
    )
    image_evidence: list[dict[str, Any]] = []
    for index, image in enumerate(cast(list[dict[str, Any]], summary["images"]), start=1):
        source = cast(dict[str, Any], image.get("source", image))
        image_evidence.append(
            {
                "evidenceId": f"image-{index}",
                "kind": "image",
                "localRef": source["fileId"],
                "mediaType": source["mediaType"],
                "sha256": source["sha256"],
                "text": None,
                "modelCopyApproved": True,
                "transcriptConfirmed": None,
            }
        )
    statement = cast(dict[str, Any], summary["statement"])
    transcript = summary.get("audio") is not None
    evidence_data = [
        *image_evidence,
        {
            "evidenceId": "statement-1",
            "kind": "transcript" if transcript else "user_statement",
            "localRef": statement["fileId"],
            "mediaType": "text/plain",
            "sha256": statement["sha256"],
            "text": STATEMENT_TEXT,
            "modelCopyApproved": True,
            "transcriptConfirmed": True if transcript else None,
        },
    ]
    provenance_data: list[dict[str, Any]] = [
        *(
            {
                "provenanceId": f"prov-image-{index}",
                "evidenceId": f"image-{index}",
                "locator": f"image {index}",
                "userConfirmed": False,
            }
            for index in range(1, 4)
        ),
        {
            "provenanceId": "prov-statement",
            "evidenceId": "statement-1",
            "locator": "confirmed transcript" if transcript else "user statement",
            "userConfirmed": True,
        },
    ]
    fact_specs: list[tuple[str, str, object, str, tuple[str, ...], float | None]] = [
        ("fact-date", "incident_date", "2026-07-14", "user_stated", ("prov-statement",), None),
        ("fact-time", "incident_time", "14:30:00", "user_stated", ("prov-statement",), None),
        ("fact-location", "location", "Berlin", "user_stated", ("prov-statement",), None),
        (
            "fact-claimant",
            "claimant_name",
            "Demo Claimant",
            "user_stated",
            ("prov-statement",),
            None,
        ),
        ("fact-policy", "policy_reference", "DEMO-42", "user_stated", ("prov-statement",), None),
        (
            "fact-registration",
            "vehicle_registration",
            "DEMO-CD-1",
            "user_stated",
            ("prov-statement",),
            None,
        ),
        (
            "fact-counterparty",
            "counterparty_known",
            "yes",
            "user_stated",
            ("prov-statement",),
            None,
        ),
        ("fact-injury", "injury_status", injury_reported, "user_stated", ("prov-statement",), None),
        (
            "fact-danger",
            "immediate_danger",
            immediate_danger,
            "user_stated",
            ("prov-statement",),
            None,
        ),
        ("fact-vehicles", "vehicle_count", 2, "user_stated", ("prov-statement",), None),
        ("fact-collision", "collision_type", "rear_end", "user_stated", ("prov-statement",), None),
        ("fact-damage", "visible_damage", "rear_bumper_dent", "observed", ("prov-image-2",), 0.94),
        ("fact-impact", "impact_area", "rear_bumper", "observed", ("prov-image-2",), 0.93),
    ]
    missing_values = {field.value for field in selected_missing}
    facts = tuple(
        EvidenceFact.model_validate(
            {
                "factId": fact_id,
                "field": field,
                "value": value,
                "status": status,
                "sourceRefs": sources,
                "confidence": confidence,
            }
        )
        for fact_id, field, value, status, sources, confidence in fact_specs
        if field not in missing_values
    )
    evidence = tuple(EvidenceItem.model_validate(item) for item in evidence_data)
    provenance = tuple(ProvenanceRef.model_validate(item) for item in provenance_data)
    narrative = compose_neutral_narrative(
        NarrativeInput(facts=facts, provenance=provenance, evidence=evidence)
    )
    assert narrative.text is not None
    narrative_fact = EvidenceFact.model_validate(
        {
            "factId": "fact-neutral-narrative",
            "field": "narrative",
            "value": narrative.text,
            "status": "user_stated",
            "sourceRefs": narrative.source_refs,
            "confidence": None,
        }
    )
    claim_values: dict[str, Any] = {
        "incidentDate": None
        if RequiredClaimField.INCIDENT_DATE in selected_missing
        else "2026-07-14",
        "incidentTime": None
        if RequiredClaimField.INCIDENT_TIME in selected_missing
        else "14:30:00",
        "location": None if RequiredClaimField.LOCATION in selected_missing else "Berlin",
        "claimantName": None
        if RequiredClaimField.CLAIMANT_NAME in selected_missing
        else "Demo Claimant",
        "policyReference": None
        if RequiredClaimField.POLICY_REFERENCE in selected_missing
        else "DEMO-42",
        "vehicleRegistration": None
        if RequiredClaimField.VEHICLE_REGISTRATION in selected_missing
        else "DEMO-CD-1",
        "counterpartyKnown": "yes",
        "narrative": narrative.text,
        "attachments": tuple(item["localRef"] for item in image_evidence),
        "missingRequiredFields": tuple(field.value for field in selected_missing),
    }
    source_by_field = {
        RequiredClaimField.INCIDENT_DATE: ("prov-statement",),
        RequiredClaimField.INCIDENT_TIME: ("prov-statement",),
        RequiredClaimField.LOCATION: ("prov-statement",),
        RequiredClaimField.CLAIMANT_NAME: ("prov-statement",),
        RequiredClaimField.POLICY_REFERENCE: ("prov-statement",),
        RequiredClaimField.VEHICLE_REGISTRATION: ("prov-statement",),
        RequiredClaimField.COUNTERPARTY_KNOWN: ("prov-statement",),
        RequiredClaimField.NARRATIVE: narrative.source_refs,
        RequiredClaimField.ATTACHMENTS: ("prov-image-1", "prov-image-2", "prov-image-3"),
    }
    claim_values["fieldProvenance"] = tuple(
        {
            "field": field.value,
            "sourceRefs": sources,
        }
        for field, sources in source_by_field.items()
        if field not in selected_missing
    )
    plan: list[tuple[str, str]] = [
        ("inspect_evidence", "Inspect only the approved evidence inventory"),
        (
            "check_required_fields",
            "Use the deterministic required-field result",
        ),
    ]
    if state is CaseState.AWAITING_CLARIFICATION:
        plan.append(("ask_clarification", "Ask the single clarification accepted by G5"))
    elif state is CaseState.READY_TO_FILL:
        plan.extend(
            (
                ("inspect_form", "Inspect only the local sandbox form"),
                ("fill_until_review", "Fill the sandbox only until review"),
                (
                    "verify_rendered_fields",
                    "Verify rendered fields before human review",
                ),
            )
        )
    plan_data = {
        "agentCanSubmit": False,
        "steps": [
            {"sequence": index, "tool": tool, "reason": reason}
            for index, (tool, reason) in enumerate(plan, start=1)
        ],
    }
    verification_data = {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": (),
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }
    return ClaimPacket.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": case_id,
            "state": state,
            "portalState": "draft",
            "scope": {
                "environment": "sandbox",
                "scenario": "two_vehicle_rear_end_no_injury",
                "agentCanSubmit": False,
                "finalActionOwner": "human",
            },
            "evidence": evidence,
            "provenance": provenance,
            "facts": (*facts, narrative_fact),
            "claim": claim_values,
            "plan": plan_data,
            "gateDecisions": gates,
            "verification": verification_data,
        }
    )


def _plan_events(packet: ClaimPacket) -> tuple[PlanStepWorkflowEvent, ...]:
    return tuple(
        PlanStepWorkflowEvent.model_validate(
            {"kind": "plan_step", "sequence": step.sequence, "tool": step.tool}
        )
        for step in packet.plan.steps
    )


def _extraction_for(packet: ClaimPacket) -> ModelExtraction:
    return ModelExtraction.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "evidence": packet.evidence,
            "provenance": packet.provenance,
            "facts": packet.facts,
            "claim": packet.claim,
        }
    )


def _approved_evidence_for(case: Any) -> tuple[EvidenceItem, ...]:
    return _packet(
        case.case_id,
        CaseState.READY_TO_FILL,
        (),
        missing=False,
        intake_summary=cast(dict[str, Any], case.snapshot.intake_summary),
    ).evidence


def _invalid_g2_attempt(*, decided_at: datetime) -> OutputContractAttempt:
    return OutputContractAttempt(
        envelope=ModelOutputEnvelope(
            payload="{",
            refusal=False,
            truncated=False,
            attempt=0,
        ),
        decided_at=decided_at,
    )


def _bind_packet_extraction(
    command: AnalysisWorkflowCommand,
    packet: ClaimPacket,
    *,
    extraction: ModelExtraction | None = None,
) -> AnalysisWorkflowCommand:
    assert command.g2_attempts
    selected = extraction or _extraction_for(packet)
    final_attempt = command.g2_attempts[-1]
    final = OutputContractAttempt(
        envelope=ModelOutputEnvelope(
            payload=selected.model_dump_json(by_alias=True),
            refusal=False,
            truncated=False,
            attempt=final_attempt.envelope.attempt,
        ),
        decided_at=final_attempt.decided_at,
    )
    return replace(
        command,
        claim_packet=packet,
        approved_evidence=packet.evidence,
        g2_attempts=(*command.g2_attempts[:-1], final),
        plan_steps=_plan_events(packet),
    )


def _clarification_event(
    status: ClarificationStatus,
    *,
    round_number: int,
    field: str = "incident_time",
) -> ClarificationWorkflowEvent:
    return ClarificationWorkflowEvent.model_validate(
        {
            "kind": "clarification",
            "round": round_number,
            "field": field,
            "status": status,
        }
    )


def _non_gate_event(event_family: str, *, suffix: str) -> Any:
    if event_family == "provider":
        return _provider_call()
    if event_family == "retry":
        return _retry()
    if event_family == "operational":
        return _operational_failure()
    if event_family == "plan":
        return PlanStepWorkflowEvent.model_validate(
            {"kind": "plan_step", "sequence": 1, "tool": "fill_until_review"}
        )
    if event_family == "clarification":
        return _clarification_event(ClarificationStatus.REQUESTED, round_number=1)
    if event_family == "tool":
        return ToolCallWorkflowEvent.model_validate(
            {
                "kind": "tool_call",
                "invocationId": f"invocation-{suffix}",
                "sequence": 1,
                "tool": "fill_until_review",
                "status": "succeeded",
                "durationMs": 1,
            }
        )
    if event_family == "portal":
        return PortalFillWorkflowEvent.model_validate(
            {
                "kind": "portal_fill",
                "variant": "A",
                "portalVersion": 1,
                "writtenFields": (RequiredClaimField.INCIDENT_DATE,),
            }
        )
    if event_family == "verification":
        return VerificationWorkflowEvent.model_validate(
            {
                "kind": "verification",
                "attemptNumber": 1,
                "status": "verified",
                "deterministicMatch": True,
                "modelReportedMismatch": False,
                "repairUsed": False,
                "final": True,
            }
        )
    raise AssertionError(f"Unknown non-gate event family: {event_family}")


def _expected_actor_for_event_family(event_family: str) -> ActorType:
    return (
        ActorType.SYSTEM
        if event_family in {"operational", "clarification", "verification"}
        else ActorType.AGENT
    )


def _clarification_view(
    case_id: str,
    version: int,
    *,
    round_number: int,
    requested_at: datetime,
    field: RequiredClaimField = RequiredClaimField.INCIDENT_TIME,
) -> ClarificationView:
    return ClarificationView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "clarificationId": f"clarification-{round_number}",
            "caseId": case_id,
            "field": field,
            "round": round_number,
            "question": QUESTION_BY_FIELD[field],
            "status": "requested",
            "expectedVersion": version,
            "requestedAt": requested_at,
        }
    )


def _disclosed_text_case(
    database_path: Path,
) -> tuple[
    CaseService,
    SqliteCaseRepository,
    tuple[GateDecision, GateDecision],
    Any,
]:
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-atomic",
    )
    case = service.create_case()
    request = IntakeRequest(
        images=tuple(
            ImageUpload(content=_image_bytes(index), media_type="image/png")
            for index in range(1, 4)
        ),
        text=STATEMENT_TEXT,
        audio=None,
        consents=IntakeConsents(True, True, True),
    )
    review = PrivacyReview(
        exif_choices=tuple(
            ExifChoice(input_id=f"image-{index}", decision=ExifDecision.STRIP)
            for index in range(1, 4)
        ),
        model_copy_approved=True,
        audit_fields=(),
    )
    case = service.commit_intake_disclosure(
        IntakeDisclosureCommand(
            case_id=case.case_id,
            expected_version=case.version,
            request=request,
            privacy_review=review,
            g0_decided_at=NOW,
            g1_decided_at=NOW,
            updated_at=NOW,
        )
    )
    prefix_values = repository.list_gate_decisions(case.case_id)
    prefix = (prefix_values[0].decision, prefix_values[1].decision)
    return service, repository, prefix, case


def _analysis_case(
    database_path: Path,
) -> tuple[
    CaseService,
    SqliteCaseRepository,
    tuple[GateDecision, GateDecision],
    Any,
]:
    service, repository, prefix, case = _disclosed_text_case(database_path)
    case = service.begin_text_analysis(
        case.case_id,
        expected_version=case.version,
    )
    return service, repository, prefix, case


def _disclosed_audio_case(
    database_path: Path,
) -> tuple[CaseService, SqliteCaseRepository, CaseRecord, list[datetime]]:
    clock = [NOW]
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: clock[0],
        case_id_factory=lambda: "case-audio-authority",
    )
    created = service.create_case()
    disclosed = service.commit_intake_disclosure(
        IntakeDisclosureCommand(
            case_id=created.case_id,
            expected_version=created.version,
            request=IntakeRequest(
                images=tuple(
                    ImageUpload(content=_image_bytes(index), media_type="image/png")
                    for index in range(1, 4)
                ),
                text=None,
                audio=AudioUpload(content=_audio_bytes(), media_type="audio/wav"),
                consents=IntakeConsents(True, True, True),
            ),
            privacy_review=PrivacyReview(
                exif_choices=tuple(
                    ExifChoice(
                        input_id=f"image-{index}",
                        decision=ExifDecision.STRIP,
                    )
                    for index in range(1, 4)
                ),
                model_copy_approved=True,
                audit_fields=(),
            ),
            g0_decided_at=NOW,
            g1_decided_at=NOW,
            updated_at=NOW,
        )
    )
    return service, repository, disclosed, clock


def _audio_transcript_case(
    database_path: Path,
    *,
    confirmed: bool,
) -> tuple[CaseService, SqliteCaseRepository, CaseRecord]:
    service, repository, disclosed, clock = _disclosed_audio_case(database_path)
    outcome = TranscriptionSuccess(
        transcript=STATEMENT_TEXT,
        telemetry=ProviderCallTelemetry(
            operation=WorkflowOperation.TRANSCRIPTION,
            model_id=ProviderModelId.DETERMINISTIC_MOCK,
            provider_mode=ProviderMode.MOCK,
            call_sequence=1,
            retry_attempt=0,
            duration_ms=5,
            status=ProviderCallStatus.SUCCEEDED,
        ),
    )
    waiting_result = service.commit_transcription_outcome(
        TranscriptionOutcomeCommand(
            case_id=disclosed.case_id,
            expected_version=disclosed.version,
            outcome=outcome,
            occurred_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    )
    if not confirmed:
        return service, repository, waiting_result.case
    pending = waiting_result.transcript
    confirmation = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting_result.case.case_id,
            "transcriptId": pending.transcript_id,
            "transcriptSha256": pending.transcript_sha256,
            "expectedVersion": waiting_result.case.version,
            "confirmed": True,
        }
    )
    clock[0] = NOW + timedelta(seconds=2)
    analyzing = service.confirm_transcript(
        waiting_result.case.case_id,
        expected_case_version=waiting_result.case.version,
        confirmation=confirmation,
    ).case
    return service, repository, analyzing


def _image_bytes(seed: int) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), (seed * 20, seed * 30, seed * 40)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


def _audio_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


def _initial_command(
    case: Any,
    prefix: tuple[GateDecision, GateDecision],
    *,
    target: CaseState = CaseState.AWAITING_CLARIFICATION,
    retry: bool = False,
    missing_fields: tuple[RequiredClaimField, ...] | None = None,
) -> AnalysisWorkflowCommand:
    selected_missing = (
        (RequiredClaimField.INCIDENT_TIME,)
        if target is CaseState.AWAITING_CLARIFICATION and missing_fields is None
        else (() if missing_fields is None else missing_fields)
    )
    injury = target is CaseState.EMERGENCY_STOPPED
    g2 = _gate(GateId.G2_OUTPUT_CONTRACT, 2)
    safety_input = SafetyInput(
        injury_reported=injury,
        immediate_danger=False,
        portal_is_sandbox=True,
        real_credentials_present=False,
        advice_categories=(AdviceCategory.LEGAL,) if target is CaseState.BLOCKED else (),
        requested_actions=(),
        model_signal=ModelSafetySignal.SAFE,
        evidence_refs=("prov-statement",),
    )
    safety = evaluate_g3(safety_input, decided_at=NOW + timedelta(seconds=3))
    base_packet = _packet(
        case.case_id,
        target,
        (*prefix, g2, safety.decision),
        missing=bool(selected_missing),
        intake_summary=cast(dict[str, Any], case.snapshot.intake_summary),
        missing_fields=selected_missing,
        injury_reported=injury,
    )
    preliminary_extraction = ModelExtraction.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "evidence": base_packet.evidence,
            "provenance": base_packet.provenance,
            "facts": base_packet.facts,
            "claim": base_packet.claim,
        }
    )
    final_envelope = ModelOutputEnvelope(
        payload=preliminary_extraction.model_dump_json(by_alias=True),
        refusal=False,
        truncated=False,
        attempt=1 if retry else 0,
    )
    g2_run = OutputContractRun()
    if retry:
        failed = evaluate_g2(
            ModelOutputEnvelope(
                payload="{",
                refusal=False,
                truncated=False,
                attempt=0,
            ),
            approved_evidence=base_packet.evidence,
            run=g2_run,
            decided_at=NOW + timedelta(seconds=1),
        )
        g2_run = g2_run.append(failed)
    g2 = evaluate_g2(
        final_envelope,
        approved_evidence=base_packet.evidence,
        run=g2_run,
        decided_at=NOW + timedelta(seconds=2),
    ).decision
    base_packet = ClaimPacket.model_validate(
        base_packet.model_copy(
            update={"gate_decisions": (*prefix, g2, safety.decision)}
        ).model_dump(mode="json", by_alias=True)
    )
    emitted_list = [g2, safety.decision]
    completeness = None
    if safety.decision.passed:
        provenance = evaluate_g4(base_packet, decided_at=NOW + timedelta(seconds=4))
        emitted_list.append(provenance.decision)
        if provenance.decision.passed or provenance.conflicting_fields:
            completeness = SqliteCaseRepository._derive_completeness(
                provenance,
                completed_rounds=0,
                decided_at=NOW + timedelta(seconds=5),
            )
            emitted_list.append(completeness.decision)
    emitted = tuple(emitted_list)
    packet = base_packet.model_copy(update={"gate_decisions": (*prefix, *emitted)})
    packet = ClaimPacket.model_validate(packet.model_dump(mode="json", by_alias=True))
    g2_attempts: tuple[OutputContractAttempt, ...]
    if retry:
        g2_attempts = (
            OutputContractAttempt(
                envelope=ModelOutputEnvelope(
                    payload="{",
                    refusal=False,
                    truncated=False,
                    attempt=0,
                ),
                decided_at=NOW + timedelta(seconds=1),
            ),
            OutputContractAttempt(
                envelope=final_envelope,
                decided_at=g2.decided_at,
            ),
        )
    else:
        g2_attempts = (
            OutputContractAttempt(
                envelope=final_envelope,
                decided_at=g2.decided_at,
            ),
        )
    updated_at = NOW + timedelta(seconds=10)
    active = (
        _clarification_view(
            case.case_id,
            case.version + 1,
            round_number=1,
            requested_at=updated_at,
            field=cast(Any, completeness).accepted_question.field,
        )
        if completeness is not None and completeness.accepted_question is not None
        else None
    )
    provider_events = (
        (
            ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(_retry(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(
                _provider_call(call_sequence=2, retry_attempt=1),
                NOW + timedelta(seconds=1),
            ),
        )
        if retry
        else (ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),)
    )
    return AnalysisWorkflowCommand(
        case_id=case.case_id,
        expected_version=case.version,
        target=target,
        claim_packet=packet,
        active_clarification=active,
        clarification_answer=None,
        approved_evidence=packet.evidence,
        g2_attempts=g2_attempts,
        safety_input=safety_input,
        gate_decisions=emitted,
        provider_events=provider_events,
        plan_steps=() if packet is None else _plan_events(packet),
        clarification_events=(
            _clarification_event(
                ClarificationStatus.REQUESTED,
                round_number=1,
                field=active.field.value,
            ),
        )
        if active is not None
        else (),
        updated_at=updated_at,
    )


def _initial_conflict_command(
    case: Any,
    prefix: tuple[GateDecision, GateDecision],
    *,
    fields: tuple[RequiredClaimField, ...],
) -> AnalysisWorkflowCommand:
    base = _initial_command(case, prefix, target=CaseState.READY_TO_FILL)
    packet = cast(ClaimPacket, base.claim_packet)
    conflicting_value = {
        RequiredClaimField.LOCATION: "Hamburg",
        RequiredClaimField.CLAIMANT_NAME: "Other Demo Claimant",
        RequiredClaimField.POLICY_REFERENCE: "DEMO-CONFLICT",
        RequiredClaimField.VEHICLE_REGISTRATION: "DEMO-CONFLICT-REG",
    }
    extra_facts = tuple(
        EvidenceFact.model_validate(
            {
                "factId": f"fact-conflict-{field.value}",
                "field": field.value,
                "value": conflicting_value[field],
                "status": "user_stated",
                "sourceRefs": ("prov-statement",),
                "confidence": None,
            }
        )
        for field in fields
    )
    template = _packet(
        case.case_id,
        CaseState.AWAITING_CLARIFICATION,
        (),
        missing=False,
    )
    packet_data = packet.model_dump(mode="json", by_alias=True)
    packet_data.update(
        {
            "state": CaseState.AWAITING_CLARIFICATION,
            "facts": (*packet.facts, *extra_facts),
            "plan": template.plan,
            "gateDecisions": (*prefix, *base.gate_decisions[:2]),
        }
    )
    provisional = ClaimPacket.model_validate(packet_data)
    provenance = evaluate_g4(provisional, decided_at=NOW + timedelta(seconds=4))
    assert provenance.decision.reason_codes == (GateReasonCode.G4_CONFLICTING_SOURCES,)
    completeness = SqliteCaseRepository._derive_completeness(
        provenance,
        completed_rounds=0,
        decided_at=NOW + timedelta(seconds=5),
    )
    assert completeness.accepted_question is not None
    emitted = (
        *base.gate_decisions[:2],
        provenance.decision,
        completeness.decision,
    )
    packet_data["gateDecisions"] = (*prefix, *emitted)
    target_packet = ClaimPacket.model_validate(packet_data)
    active = _clarification_view(
        case.case_id,
        case.version + 1,
        round_number=1,
        requested_at=base.updated_at,
        field=completeness.accepted_question.field,
    )
    rebound = _bind_packet_extraction(base, target_packet)
    return replace(
        rebound,
        target=CaseState.AWAITING_CLARIFICATION,
        active_clarification=active,
        gate_decisions=emitted,
        clarification_events=(
            _clarification_event(
                ClarificationStatus.REQUESTED,
                round_number=1,
                field=active.field.value,
            ),
        ),
    )


def _continuation_command(
    case: Any,
    *,
    target: CaseState,
) -> AnalysisWorkflowCommand:
    prior_packet = case.snapshot.claim_packet
    assert prior_packet is not None
    active = ClarificationView.model_validate(case.snapshot.active_clarification)
    base_offset = int((case.updated_at - NOW).total_seconds())
    answer_by_field = {
        RequiredClaimField.INCIDENT_DATE: "2026-07-15",
        RequiredClaimField.INCIDENT_TIME: "15:00:00",
        RequiredClaimField.LOCATION: "Hamburg",
        RequiredClaimField.CLAIMANT_NAME: "Demo Claimant",
        RequiredClaimField.POLICY_REFERENCE: "DEMO-43",
        RequiredClaimField.VEHICLE_REGISTRATION: "DEMO-CD-2",
        RequiredClaimField.COUNTERPARTY_KNOWN: CounterpartyKnown.YES.value,
    }
    raw_answer = answer_by_field[active.field]
    answer = ClarificationAnswerRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": case.case_id,
            "clarificationId": active.clarification_id,
            "field": active.field,
            "round": active.round,
            "expectedVersion": case.version,
            "answer": raw_answer,
        }
    )
    digest = hashlib.sha256(raw_answer.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        (
            "claimdone-clarification-v1\0"
            f"{case.case_id}\0{active.clarification_id}\0{active.round}\0{digest}"
        ).encode()
    ).hexdigest()
    evidence_id = f"clarification-{identity[:32]}"
    provenance_id = f"provenance-{identity[:32]}"
    appended_evidence = EvidenceItem.model_validate(
        {
            "evidenceId": evidence_id,
            "kind": "clarification",
            "localRef": f"clarification-{identity[:32]}.txt",
            "mediaType": "text/plain",
            "sha256": digest,
            "text": raw_answer,
            "modelCopyApproved": True,
            "transcriptConfirmed": None,
        }
    )
    appended_provenance = ProvenanceRef.model_validate(
        {
            "provenanceId": provenance_id,
            "evidenceId": evidence_id,
            "locator": "clarification answer",
            "userConfirmed": True,
        }
    )
    facts = tuple(
        fact
        for fact in prior_packet.facts
        if fact.field
        not in {
            EvidenceField(active.field.value),
            EvidenceField.NARRATIVE,
        }
    )
    facts = (
        *facts,
        EvidenceFact.model_validate(
            {
                "factId": f"fact-{identity[:32]}",
                "field": active.field.value,
                "value": raw_answer.lower()
                if active.field is RequiredClaimField.COUNTERPARTY_KNOWN
                else raw_answer,
                "status": "user_stated",
                "sourceRefs": (provenance_id,),
                "confidence": None,
            }
        ),
    )
    evidence = (*prior_packet.evidence, appended_evidence)
    provenance = (*prior_packet.provenance, appended_provenance)
    narrative = compose_neutral_narrative(
        NarrativeInput(facts=facts, provenance=provenance, evidence=evidence)
    )
    assert narrative.text is not None
    facts = (
        *facts,
        EvidenceFact.model_validate(
            {
                "factId": f"fact-neutral-narrative-{active.round + 1}",
                "field": "narrative",
                "value": narrative.text,
                "status": "user_stated",
                "sourceRefs": narrative.source_refs,
                "confidence": None,
            }
        ),
    )
    alias_by_field = {
        RequiredClaimField.INCIDENT_DATE: "incidentDate",
        RequiredClaimField.INCIDENT_TIME: "incidentTime",
        RequiredClaimField.LOCATION: "location",
        RequiredClaimField.CLAIMANT_NAME: "claimantName",
        RequiredClaimField.POLICY_REFERENCE: "policyReference",
        RequiredClaimField.VEHICLE_REGISTRATION: "vehicleRegistration",
        RequiredClaimField.COUNTERPARTY_KNOWN: "counterpartyKnown",
    }
    claim_data = prior_packet.claim.model_dump(mode="json", by_alias=True)
    claim_data[alias_by_field[active.field]] = (
        raw_answer.lower() if active.field is RequiredClaimField.COUNTERPARTY_KNOWN else raw_answer
    )
    claim_data["narrative"] = narrative.text
    claim_data["missingRequiredFields"] = tuple(
        field.value
        for field in RequiredClaimField
        if field in alias_by_field and claim_data[alias_by_field[field]] is None
    )
    claim_data["fieldProvenance"] = (
        *(
            item
            for item in prior_packet.claim.field_provenance
            if item.field not in {active.field, RequiredClaimField.NARRATIVE}
        ),
        FieldProvenance.model_validate({"field": active.field, "sourceRefs": (provenance_id,)}),
        FieldProvenance.model_validate({"field": "narrative", "sourceRefs": narrative.source_refs}),
    )
    template = _packet(
        case.case_id,
        target,
        (),
        missing=target is not CaseState.READY_TO_FILL,
    )
    packet_data = prior_packet.model_dump(mode="json", by_alias=True)
    packet_data.update(
        {
            "state": target,
            "evidence": evidence,
            "provenance": provenance,
            "facts": facts,
            "claim": claim_data,
            "plan": template.plan,
        }
    )
    provisional = ClaimPacket.model_validate(packet_data)
    provenance_result = evaluate_g4(
        provisional,
        decided_at=NOW + timedelta(seconds=base_offset + 1),
    )
    completeness = SqliteCaseRepository._derive_completeness(
        provenance_result,
        completed_rounds=active.round,
        decided_at=NOW + timedelta(seconds=base_offset + 2),
    )
    emitted = (provenance_result.decision, completeness.decision)
    packet_data["gateDecisions"] = (*prior_packet.gate_decisions[:4], *emitted)
    packet = ClaimPacket.model_validate(packet_data)
    updated_at = NOW + timedelta(seconds=base_offset + 3)
    clarification_events: tuple[ClarificationWorkflowEvent, ...]
    if target is CaseState.AWAITING_CLARIFICATION:
        assert completeness.accepted_question is not None
        next_round = active.round + 1
        next_active = _clarification_view(
            case.case_id,
            case.version + 1,
            round_number=next_round,
            requested_at=updated_at,
            field=completeness.accepted_question.field,
        )
        clarification_events = (
            _clarification_event(
                ClarificationStatus.CONFIRMED,
                round_number=active.round,
                field=active.field.value,
            ),
            _clarification_event(
                ClarificationStatus.REQUESTED,
                round_number=next_round,
                field=completeness.accepted_question.field.value,
            ),
        )
    else:
        next_active = None
        clarification_events = (
            _clarification_event(
                ClarificationStatus.EXHAUSTED
                if target is CaseState.BLOCKED and completeness.manual_handoff
                else ClarificationStatus.CONFIRMED,
                round_number=active.round,
                field=active.field.value,
            ),
        )
    return AnalysisWorkflowCommand(
        case_id=case.case_id,
        expected_version=case.version,
        target=target,
        claim_packet=packet,
        active_clarification=next_active,
        clarification_answer=answer,
        approved_evidence=(),
        g2_attempts=(),
        safety_input=None,
        gate_decisions=emitted,
        provider_events=(),
        plan_steps=_plan_events(packet),
        clarification_events=clarification_events,
        updated_at=updated_at,
    )


def _three_round_blocked_case(
    database_path: Path,
) -> tuple[CaseService, SqliteCaseRepository, CaseRecord]:
    service, repository, prefix, analyzing = _analysis_case(database_path)
    current = service.commit_analysis_workflow(
        _initial_command(
            analyzing,
            prefix,
            missing_fields=(
                RequiredClaimField.INCIDENT_TIME,
                RequiredClaimField.LOCATION,
                RequiredClaimField.CLAIMANT_NAME,
                RequiredClaimField.POLICY_REFERENCE,
            ),
        )
    ).case
    for _round in (2, 3):
        current = service.commit_analysis_workflow(
            _continuation_command(
                current,
                target=CaseState.AWAITING_CLARIFICATION,
            )
        ).case
    blocked = service.commit_analysis_workflow(
        _continuation_command(current, target=CaseState.BLOCKED)
    ).case
    return service, repository, blocked


def _canonical_intake_followup(
    database_path: Path,
    scenario: str,
) -> tuple[SqliteCaseRepository, CaseRecord]:
    if scenario == "audio_failed":
        _service, repository, disclosed, _clock = _disclosed_audio_case(database_path)
        current = repository.commit_terminal_provider_failure(
            TerminalProviderFailureCommand(
                case_id=disclosed.case_id,
                expected_version=disclosed.version,
                event=_transcription_operational_failure(),
                provider_events=(),
                approved_evidence=(),
                g2_attempts=(),
                claim_packet=None,
                occurred_at=disclosed.updated_at + timedelta(seconds=1),
            )
        ).case
    elif scenario == "audio_success":
        _service, repository, current = _audio_transcript_case(
            database_path,
            confirmed=True,
        )
    else:
        _service, repository, _prefix, analyzing = _analysis_case(database_path)
        current = analyzing
        if scenario == "extraction_failed":
            current = repository.commit_terminal_provider_failure(
                TerminalProviderFailureCommand(
                    case_id=analyzing.case_id,
                    expected_version=analyzing.version,
                    event=_operational_failure(),
                    provider_events=(),
                    approved_evidence=_approved_evidence_for(analyzing),
                    g2_attempts=(),
                    claim_packet=None,
                    occurred_at=analyzing.updated_at + timedelta(seconds=1),
                )
            ).case
        else:
            assert scenario == "text_success"
    return repository, current


def _counts(repository: SqliteCaseRepository, case_id: str) -> tuple[int, int, int, int]:
    return (
        len(repository.list_audit_events(case_id)),
        len(repository.list_gate_decisions(case_id)),
        len(repository.list_workflow_events(case_id)),
        len(repository.list_provider_usage(case_id)),
    )


def _rewrite_projection_timestamp(
    connection: sqlite3.Connection,
    *,
    sequence: int,
    occurred_at: datetime,
) -> None:
    audit_row = connection.execute(
        "SELECT event_json FROM audit_events WHERE sequence = ?",
        (sequence,),
    ).fetchone()
    workflow_row = connection.execute(
        "SELECT event_json FROM workflow_events WHERE source_audit_sequence = ?",
        (sequence,),
    ).fetchone()
    assert audit_row is not None and workflow_row is not None
    audit = AuditEvent.model_validate_json(cast(str, audit_row[0])).model_copy(
        update={"occurred_at": occurred_at}
    )
    envelope = WorkflowEventEnvelope.model_validate_json(
        cast(str, workflow_row[0])
    ).model_copy(update={"occurred_at": occurred_at})
    connection.execute(
        "UPDATE audit_events SET occurred_at = ?, event_json = ? WHERE sequence = ?",
        (occurred_at.isoformat(), audit.model_dump_json(by_alias=True), sequence),
    )
    connection.execute(
        "UPDATE workflow_events SET event_json = ? "
        "WHERE source_audit_sequence = ?",
        (envelope.model_dump_json(by_alias=True), sequence),
    )
    connection.execute(
        "UPDATE provider_usage_ledger SET occurred_at = ? "
        "WHERE source_audit_sequence = ?",
        (occurred_at.isoformat(), sequence),
    )


def _swap_projection_sequences(
    connection: sqlite3.Connection,
    first: int,
    second: int,
) -> None:
    maximum = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM audit_events"
    ).fetchone()
    assert maximum is not None
    temporary = int(maximum[0]) + 10_000
    envelope_rows = connection.execute(
        "SELECT source_audit_sequence, event_json FROM workflow_events "
        "WHERE source_audit_sequence IN (?, ?)",
        (first, second),
    ).fetchall()
    envelopes = {
        int(sequence): WorkflowEventEnvelope.model_validate_json(cast(str, event_json))
        for sequence, event_json in envelope_rows
    }
    assert set(envelopes) == {first, second}

    def move(source: int, target: int, envelope: WorkflowEventEnvelope) -> None:
        moved = envelope.model_copy(
            update={"source_audit_sequence": target, "cursor": target}
        )
        connection.execute(
            "UPDATE workflow_events "
            "SET source_audit_sequence = ?, event_json = ? "
            "WHERE source_audit_sequence = ?",
            (target, moved.model_dump_json(by_alias=True), source),
        )
        connection.execute(
            "UPDATE provider_usage_ledger SET source_audit_sequence = ? "
            "WHERE source_audit_sequence = ?",
            (target, source),
        )
        connection.execute(
            "UPDATE audit_events SET sequence = ? WHERE sequence = ?",
            (target, source),
        )

    move(first, temporary, envelopes[first])
    move(second, first, envelopes[second])
    move(temporary, second, envelopes[first])


def _insert_state_transition(
    repository: SqliteCaseRepository,
    connection: sqlite3.Connection,
    *,
    case_id: str,
    from_state: CaseState,
    to_state: CaseState,
    actor: ActorType,
    occurred_at: datetime,
) -> None:
    audit = build_state_change_event(
        case_id=case_id,
        current=from_state,
        target=to_state,
        actor=actor,
        occurred_at=occurred_at,
    )
    sequence = repository._insert_audit_event(connection, audit)
    repository._insert_workflow_projection(
        connection,
        audit_sequence=sequence,
        audit=audit,
        event=StateWorkflowEvent.model_validate(
            {
                "kind": "state",
                "actor": actor,
                "fromState": from_state,
                "toState": to_state,
            }
        ),
    )


def _inject_duplicate_intake_gate(
    database_path: Path,
    repository: SqliteCaseRepository,
    *,
    case_id: str,
    gate_id: GateId,
) -> None:
    decisions = repository.list_gate_decisions(case_id)
    decision = next(item.decision for item in decisions if item.decision.gate_id is gate_id)
    disclosure_sequence = next(
        item.sequence
        for item in repository.list_workflow_events(case_id)
        if isinstance(item.envelope.event, StateWorkflowEvent)
        and item.envelope.event.from_state is CaseState.CREATED
        and item.envelope.event.to_state is CaseState.DISCLOSED
    )
    with repository._write_connection() as connection:
        repository._insert_gate_decision_row(
            connection,
            case_id=case_id,
            decision=decision,
        )
        audit = build_gate_audit_event(
            case_id=case_id,
            decision=decision,
            actor=ActorType.SYSTEM,
        )
        extra_sequence = repository._insert_audit_event(connection, audit)
        repository._insert_workflow_projection(
            connection,
            audit_sequence=extra_sequence,
            audit=audit,
            event=GateWorkflowEvent.model_validate(
                {"kind": "gate", "decision": decision}
            ),
        )
    with sqlite3.connect(database_path) as connection:
        for current_sequence in range(extra_sequence, disclosure_sequence, -1):
            _swap_projection_sequences(
                connection,
                current_sequence - 1,
                current_sequence,
            )


def _swap_intake_gate_order(
    database_path: Path,
    repository: SqliteCaseRepository,
    *,
    case_id: str,
) -> None:
    gate_projections = tuple(
        item
        for item in repository.list_workflow_events(case_id)
        if isinstance(item.envelope.event, GateWorkflowEvent)
        and item.envelope.event.decision.gate_id
        in {GateId.G0_INTAKE, GateId.G1_PRIVACY}
    )[:2]
    assert len(gate_projections) == 2
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT sequence, gate_id, decided_at, decision_json "
            "FROM gate_decisions WHERE case_id = ? ORDER BY sequence LIMIT 2",
            (case_id,),
        ).fetchall()
        assert len(rows) == 2
        first, second = rows
        connection.execute(
            "UPDATE gate_decisions "
            "SET gate_id = ?, decided_at = ?, decision_json = ? WHERE sequence = ?",
            (second[1], second[2], second[3], first[0]),
        )
        connection.execute(
            "UPDATE gate_decisions "
            "SET gate_id = ?, decided_at = ?, decision_json = ? WHERE sequence = ?",
            (first[1], first[2], first[3], second[0]),
        )
        _swap_projection_sequences(
            connection,
            gate_projections[0].sequence,
            gate_projections[1].sequence,
        )


def _remove_g1_intake_gate(
    database_path: Path,
    repository: SqliteCaseRepository,
    *,
    case_id: str,
) -> None:
    g1_projection = next(
        item
        for item in repository.list_workflow_events(case_id)
        if isinstance(item.envelope.event, GateWorkflowEvent)
        and item.envelope.event.decision.gate_id is GateId.G1_PRIVACY
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
            (g1_projection.sequence,),
        )
        connection.execute(
            "DELETE FROM audit_events WHERE sequence = ?",
            (g1_projection.sequence,),
        )
        connection.execute(
            "DELETE FROM gate_decisions WHERE case_id = ? AND gate_id = ?",
            (case_id, GateId.G1_PRIVACY.value),
        )


def test_stale_intake_cas_removes_staged_media_and_commits_no_authority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stale-intake.db"
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-stale-intake",
    )
    created = service.create_case()
    request = IntakeRequest(
        images=tuple(
            ImageUpload(content=_image_bytes(index), media_type="image/png")
            for index in range(1, 4)
        ),
        text=STATEMENT_TEXT,
        audio=None,
        consents=IntakeConsents(True, True, True),
    )
    review = PrivacyReview(
        exif_choices=tuple(
            ExifChoice(input_id=f"image-{index}", decision=ExifDecision.STRIP)
            for index in range(1, 4)
        ),
        model_copy_approved=True,
        audit_fields=(),
    )
    with pytest.raises(CaseVersionConflictError):
        service.commit_intake_disclosure(
            IntakeDisclosureCommand(
                case_id=created.case_id,
                expected_version=created.version + 1,
                request=request,
                privacy_review=review,
                g0_decided_at=NOW,
                g1_decided_at=NOW,
                updated_at=NOW,
            )
        )

    assert service.get_case(created.case_id) == created
    assert _counts(repository, created.case_id) == (0, 0, 0, 0)
    assert repository.get_case_media_handle(created.case_id) is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM case_intake_authority").fetchone() == (0,)
    assert not tuple(
        path
        for path in repository.media_store.root.iterdir()
        if path.name.startswith("case-")
    )


@pytest.mark.parametrize("invalid_version", (True, 1.0))
def test_intake_rejects_non_exact_versions_before_staging_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_version: object,
) -> None:
    repository = SqliteCaseRepository(tmp_path / "strict-intake.db")
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-strict-intake",
    )
    created = service.create_case()
    command = IntakeDisclosureCommand(
        case_id=created.case_id,
        expected_version=created.version,
        request=IntakeRequest(
            images=tuple(
                ImageUpload(content=_image_bytes(index), media_type="image/png")
                for index in range(1, 4)
            ),
            text=STATEMENT_TEXT,
            audio=None,
            consents=IntakeConsents(True, True, True),
        ),
        privacy_review=PrivacyReview(
            exif_choices=tuple(
                ExifChoice(
                    input_id=f"image-{index}",
                    decision=ExifDecision.STRIP,
                )
                for index in range(1, 4)
            ),
            model_copy_approved=True,
            audit_fields=(),
        ),
        g0_decided_at=NOW,
        g1_decided_at=NOW,
        updated_at=NOW,
    )

    def must_not_stage() -> None:
        raise AssertionError("invalid intake reached media staging")

    monkeypatch.setattr(repository.media_store, "create_case", must_not_stage)
    with pytest.raises(TypeError, match="exact positive integer"):
        repository.commit_intake_disclosure(
            replace(command, expected_version=cast(Any, invalid_version))
        )

    assert service.get_case(created.case_id) == created
    assert _counts(repository, created.case_id) == (0, 0, 0, 0)


@pytest.mark.parametrize("invalid_version", (True, 2.0))
def test_transcription_rejects_non_exact_versions_before_writing_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_version: object,
) -> None:
    repository = SqliteCaseRepository(tmp_path / "strict-transcription.db")
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-strict-transcription",
    )
    created = service.create_case()
    disclosed = service.commit_intake_disclosure(
        IntakeDisclosureCommand(
            case_id=created.case_id,
            expected_version=created.version,
            request=IntakeRequest(
                images=tuple(
                    ImageUpload(content=_image_bytes(index), media_type="image/png")
                    for index in range(1, 4)
                ),
                text=None,
                audio=AudioUpload(content=_audio_bytes(), media_type="audio/wav"),
                consents=IntakeConsents(True, True, True),
            ),
            privacy_review=PrivacyReview(
                exif_choices=tuple(
                    ExifChoice(
                        input_id=f"image-{index}",
                        decision=ExifDecision.STRIP,
                    )
                    for index in range(1, 4)
                ),
                model_copy_approved=True,
                audit_fields=(),
            ),
            g0_decided_at=NOW,
            g1_decided_at=NOW,
            updated_at=NOW,
        )
    )
    outcome = TranscriptionSuccess(
        transcript=STATEMENT_TEXT,
        telemetry=ProviderCallTelemetry(
            operation=WorkflowOperation.TRANSCRIPTION,
            model_id=ProviderModelId.DETERMINISTIC_MOCK,
            provider_mode=ProviderMode.MOCK,
            call_sequence=1,
            retry_attempt=0,
            duration_ms=5,
            status=ProviderCallStatus.SUCCEEDED,
        ),
    )
    command = TranscriptionOutcomeCommand(
        case_id=disclosed.case_id,
        expected_version=disclosed.version,
        outcome=outcome,
        occurred_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
    )
    before = _counts(repository, disclosed.case_id)

    def must_not_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid transcription reached media writing")

    monkeypatch.setattr(repository.media_store, "write_bytes", must_not_write)
    with pytest.raises(TypeError, match="exact positive integer"):
        repository.commit_transcription_outcome(
            replace(command, expected_version=cast(Any, invalid_version))
        )

    assert service.get_case(disclosed.case_id) == disclosed
    assert _counts(repository, disclosed.case_id) == before
    assert repository.get_transcript(disclosed.case_id) is None


def test_intake_gate_chronology_cannot_predate_case_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "intake-chronology.db"
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-intake-chronology",
    )
    created = service.create_case()
    request = IntakeRequest(
        images=tuple(
            ImageUpload(content=_image_bytes(index), media_type="image/png")
            for index in range(1, 4)
        ),
        text=STATEMENT_TEXT,
        audio=None,
        consents=IntakeConsents(True, True, True),
    )
    review = PrivacyReview(
        exif_choices=tuple(
            ExifChoice(input_id=f"image-{index}", decision=ExifDecision.STRIP)
            for index in range(1, 4)
        ),
        model_copy_approved=True,
        audit_fields=(),
    )
    before_creation = NOW - timedelta(seconds=1)

    with pytest.raises(CaseSnapshotValidationError, match="G0 cannot be decided"):
        service.commit_intake_disclosure(
            IntakeDisclosureCommand(
                case_id=created.case_id,
                expected_version=created.version,
                request=request,
                privacy_review=review,
                g0_decided_at=before_creation,
                g1_decided_at=before_creation,
                updated_at=NOW,
            )
        )

    assert service.get_case(created.case_id) == created
    assert _counts(repository, created.case_id) == (0, 0, 0, 0)
    assert repository.get_case_media_handle(created.case_id) is None
    assert not tuple(
        path
        for path in repository.media_store.root.iterdir()
        if path.name.startswith("case-")
    )


def test_delete_serializes_with_staged_intake_without_orphaning_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "delete-intake-race.db"
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-delete-intake-race",
    )
    created = service.create_case()
    command = IntakeDisclosureCommand(
        case_id=created.case_id,
        expected_version=created.version,
        request=IntakeRequest(
            images=tuple(
                ImageUpload(content=_image_bytes(index), media_type="image/png")
                for index in range(1, 4)
            ),
            text=STATEMENT_TEXT,
            audio=None,
            consents=IntakeConsents(True, True, True),
        ),
        privacy_review=PrivacyReview(
            exif_choices=tuple(
                ExifChoice(
                    input_id=f"image-{index}",
                    decision=ExifDecision.STRIP,
                )
                for index in range(1, 4)
            ),
            model_copy_approved=True,
            audit_fields=(),
        ),
        g0_decided_at=NOW,
        g1_decided_at=NOW,
        updated_at=NOW,
    )
    delete_has_write_lock = Event()
    intake_is_staged = Event()
    original_delete = repository._delete_case_and_resources_in_connection
    original_commit = repository._commit_staged_intake_disclosure

    def delete_after_intake_stages(
        connection: sqlite3.Connection,
        case_id: str,
    ) -> bool:
        delete_has_write_lock.set()
        assert intake_is_staged.wait(timeout=5)
        return original_delete(connection, case_id)

    def commit_after_delete_locks(**kwargs: Any) -> CaseRecord:
        intake_is_staged.set()
        assert delete_has_write_lock.wait(timeout=5)
        return original_commit(**kwargs)

    monkeypatch.setattr(
        repository,
        "_delete_case_and_resources_in_connection",
        delete_after_intake_stages,
    )
    monkeypatch.setattr(
        repository,
        "_commit_staged_intake_disclosure",
        commit_after_delete_locks,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        deletion = executor.submit(service.delete_case, created.case_id)
        assert delete_has_write_lock.wait(timeout=5)
        intake = executor.submit(service.commit_intake_disclosure, command)
        deletion.result(timeout=5)
        with pytest.raises(CaseNotFoundError):
            intake.result(timeout=5)

    assert repository.get_case(created.case_id) is None
    assert repository.get_case_media_handle(created.case_id) is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM case_media_handles").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM case_intake_authority").fetchone() == (0,)
    assert not tuple(
        path
        for path in repository.media_store.root.iterdir()
        if path.name.startswith("case-")
    )


@pytest.mark.parametrize("operation", ("delete", "reset"))
def test_canonical_split_delete_writers_fail_closed_without_touching_media(
    tmp_path: Path,
    operation: str,
) -> None:
    database_path = tmp_path / f"closed-split-{operation}.db"
    service, repository, _prefix, current = _analysis_case(database_path)
    handle = repository.get_case_media_handle(current.case_id)
    assert handle is not None
    media_before = tuple(sorted(repository.media_store.root.rglob("*")))
    counts_before = _counts(repository, current.case_id)

    with pytest.raises(AuthorityModeMismatchError):
        if operation == "delete":
            repository.delete_case(current.case_id)
        else:
            repository.reset_cases()

    assert service.get_case(current.case_id) == current
    assert repository.get_case_media_handle(current.case_id) == handle
    assert _counts(repository, current.case_id) == counts_before
    assert tuple(sorted(repository.media_store.root.rglob("*"))) == media_before
    assert SqliteCaseRepository(database_path).get_case(current.case_id) == current


@pytest.mark.parametrize(
    ("writer", "invalid_version"),
    (
        ("begin", 2.0),
        ("begin", True),
        ("begin", SQLITE_INT64_MAX + 1),
        ("confirm", 3.0),
        ("confirm", False),
        ("confirm", SQLITE_INT64_MAX + 1),
        ("capability", 4.0),
        ("capability", True),
        ("capability", SQLITE_INT64_MAX + 1),
    ),
)
def test_public_cas_writers_reject_non_exact_int64_before_database_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
    invalid_version: object,
) -> None:
    repository = SqliteCaseRepository(tmp_path / f"strict-public-{writer}.db")

    def must_not_open_database() -> object:
        raise AssertionError("invalid expected version reached database I/O")

    monkeypatch.setattr(repository, "_write_connection", must_not_open_database)
    with pytest.raises(TypeError, match="exact positive SQLite int64"):
        if writer == "begin":
            repository.begin_text_analysis(
                case_id="case-strict-version",
                expected_version=cast(Any, invalid_version),
                updated_at=NOW,
            )
        elif writer == "confirm":
            repository.confirm_transcript_and_transition(
                case_id="case-strict-version",
                expected_case_version=cast(Any, invalid_version),
                transcript_id="transcript-strict-version",
                transcript_sha256="a" * 64,
                updated_at=NOW,
            )
        else:
            repository.issue_authority_capability(
                case_id="case-strict-version",
                expected_case_version=cast(Any, invalid_version),
                digest=hashlib.sha256(b"strict-version").digest(),
                role="agent",
                purpose="portal_run",
                issued_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )


@pytest.mark.parametrize("invalid_version", (True, 1.0, SQLITE_INT64_MAX + 1))
def test_require_current_rejects_non_exact_int64_before_query(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    repository = SqliteCaseRepository(tmp_path / "strict-central-cas.db")

    with pytest.raises(TypeError, match="exact positive SQLite int64"):
        repository._require_current(
            cast(sqlite3.Connection, object()),
            "case-strict-version",
            cast(Any, invalid_version),
        )


@pytest.mark.parametrize("tampering", ("digest", "version", "handle", "model_bytes"))
def test_intake_authority_tampering_fails_closed_on_reopen(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"authority-{tampering}.db"
    _service, repository, _prefix, current = _analysis_case(database_path)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT storage_name, manifest_json FROM case_intake_authority WHERE case_id = ?",
            (current.case_id,),
        ).fetchone()
        assert row is not None
        storage_name = cast(str, row[0])
        manifest = cast(dict[str, Any], json.loads(cast(str, row[1])))
        if tampering == "digest":
            connection.execute(
                "UPDATE case_intake_authority SET manifest_sha256 = ? WHERE case_id = ?",
                ("0" * 64, current.case_id),
            )
        elif tampering == "version":
            connection.execute(
                "UPDATE case_intake_authority SET bound_case_version = ? WHERE case_id = ?",
                (current.version + 100, current.case_id),
            )
        elif tampering == "handle":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE case_media_handles SET storage_name = ? WHERE case_id = ?",
                (f"case-{'f' * 32}", current.case_id),
            )
        else:
            model = cast(dict[str, str], cast(list[dict[str, Any]], manifest["images"])[0]["model"])
            model_path = repository.media_store.root / storage_name / model["fileId"]
            model_path.write_bytes(b"tampered-model-bytes")

    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


def test_reopen_binds_case_state_to_contiguous_workflow_replay(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state-replay.db"
    _service, _repository, _prefix, analyzing = _analysis_case(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET state = 'blocked' WHERE case_id = ?",
            (analyzing.case_id,),
        )

    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


def test_direct_analyzing_to_ready_without_packet_or_gates_fails_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state-ready-tamper.db"
    _service, _repository, _prefix, analyzing = _analysis_case(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET state = 'ready_to_fill' WHERE case_id = ?",
            (analyzing.case_id,),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_one_column_claim_packet_tampering_disagrees_with_immutable_authority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "packet-authority-tamper.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM case_packet_authority WHERE case_id = ?",
            (waiting.case_id,),
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE cases SET claim_packet_json = "
            "json_set(claim_packet_json, '$.claim.narrative', ?) "
            "WHERE case_id = ?",
            ("A forged narrative that was never authorized.", waiting.case_id),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "tampering",
    (
        "packet_json",
        "packet_digest",
        "gates_json",
        "gates_digest",
        "bound_version",
        "created_at",
    ),
)
def test_historical_packet_authority_rows_are_revalidated_on_reopen(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"historical-packet-{tampering}.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    ready = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    ).case
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM case_packet_authority WHERE case_id = ?",
            (ready.case_id,),
        ).fetchone() == (2,)
        row = connection.execute(
            "SELECT packet_json, effective_gates_json "
            "FROM case_packet_authority "
            "WHERE case_id = ? AND bound_case_version = ?",
            (ready.case_id, waiting.version),
        ).fetchone()
        assert row is not None
        if tampering == "packet_json":
            packet_json = json.dumps(
                json.loads(cast(str, row[0])),
                ensure_ascii=False,
                indent=2,
            )
            packet_digest = hashlib.sha256(
                b"claimdone-packet-authority-v1\0" + packet_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE case_packet_authority "
                "SET packet_json = ?, packet_sha256 = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                (packet_json, packet_digest, ready.case_id, waiting.version),
            )
        elif tampering == "packet_digest":
            connection.execute(
                "UPDATE case_packet_authority SET packet_sha256 = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                ("0" * 64, ready.case_id, waiting.version),
            )
        elif tampering == "gates_json":
            gates_json = json.dumps(
                json.loads(cast(str, row[1])),
                ensure_ascii=False,
                indent=2,
            )
            gates_digest = hashlib.sha256(
                b"claimdone-packet-gates-v1\0" + gates_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE case_packet_authority "
                "SET effective_gates_json = ?, effective_gates_sha256 = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                (gates_json, gates_digest, ready.case_id, waiting.version),
            )
        elif tampering == "gates_digest":
            connection.execute(
                "UPDATE case_packet_authority SET effective_gates_sha256 = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                ("0" * 64, ready.case_id, waiting.version),
            )
        elif tampering == "bound_version":
            connection.execute(
                "UPDATE case_packet_authority SET bound_case_version = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                (ready.version + 1, ready.case_id, waiting.version),
            )
        else:
            connection.execute(
                "UPDATE case_packet_authority SET created_at = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                (
                    (ready.updated_at + timedelta(seconds=1)).isoformat(),
                    ready.case_id,
                    waiting.version,
                ),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("tampering", ("delete", "rebind"))
def test_packet_authority_history_must_exactly_match_workflow_versions(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"packet-history-set-{tampering}.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    ready = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    ).case
    with sqlite3.connect(database_path) as connection:
        if tampering == "delete":
            connection.execute(
                "DELETE FROM case_packet_authority "
                "WHERE case_id = ? AND bound_case_version = ?",
                (ready.case_id, waiting.version),
            )
        else:
            connection.execute(
                "UPDATE case_packet_authority SET bound_case_version = ? "
                "WHERE case_id = ? AND bound_case_version = ?",
                (analyzing.version, ready.case_id, waiting.version),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("tampering", ("state", "plan"))
def test_historical_packet_content_must_match_its_replayed_state_and_plan(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"packet-history-content-{tampering}.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    ready = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    ).case
    ready_packet = cast(ClaimPacket, ready.snapshot.claim_packet)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT packet_json FROM case_packet_authority "
            "WHERE case_id = ? AND bound_case_version = ?",
            (ready.case_id, waiting.version),
        ).fetchone()
        assert row is not None
        packet_data = cast(dict[str, Any], json.loads(cast(str, row[0])))
        if tampering == "state":
            packet_data["state"] = CaseState.READY_TO_FILL.value
        else:
            packet_data["plan"] = ready_packet.plan.model_dump(
                mode="json",
                by_alias=True,
            )
        forged = ClaimPacket.model_validate(packet_data)
        packet_json = json.dumps(
            forged.model_dump(mode="json", by_alias=True),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        packet_digest = hashlib.sha256(
            b"claimdone-packet-authority-v1\0" + packet_json.encode("utf-8")
        ).hexdigest()
        connection.execute(
            "UPDATE case_packet_authority "
            "SET packet_json = ?, packet_sha256 = ? "
            "WHERE case_id = ? AND bound_case_version = ?",
            (packet_json, packet_digest, ready.case_id, waiting.version),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_clarification_cannot_forge_unreachable_emergency_transition_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "clarification-forged-emergency.db"
    _service, repository, blocked = _three_round_blocked_case(database_path)
    state_projection = next(
        item
        for item in reversed(repository.list_workflow_events(blocked.case_id))
        if isinstance(item.envelope.event, StateWorkflowEvent)
    )
    state_event = cast(StateWorkflowEvent, state_projection.envelope.event)
    assert state_event.from_state is CaseState.AWAITING_CLARIFICATION
    assert state_event.to_state is CaseState.BLOCKED
    forged_state = state_event.model_copy(
        update={"to_state": CaseState.EMERGENCY_STOPPED}
    )
    forged_envelope = state_projection.envelope.model_copy(
        update={"event": forged_state}
    )
    with sqlite3.connect(database_path) as connection:
        audit_row = connection.execute(
            "SELECT event_json FROM audit_events WHERE sequence = ?",
            (state_projection.sequence,),
        ).fetchone()
        packet_row = connection.execute(
            "SELECT packet_json FROM case_packet_authority "
            "WHERE case_id = ? AND bound_case_version = ?",
            (blocked.case_id, blocked.version),
        ).fetchone()
        assert audit_row is not None and packet_row is not None
        audit = AuditEvent.model_validate_json(cast(str, audit_row[0])).model_copy(
            update={"to_state": CaseState.EMERGENCY_STOPPED}
        )
        packet_data = cast(dict[str, Any], json.loads(cast(str, packet_row[0])))
        packet_data["state"] = CaseState.EMERGENCY_STOPPED.value
        packet = ClaimPacket.model_validate(packet_data)
        packet_json = json.dumps(
            packet.model_dump(mode="json", by_alias=True),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        packet_digest = hashlib.sha256(
            b"claimdone-packet-authority-v1\0" + packet_json.encode("utf-8")
        ).hexdigest()
        connection.execute(
            "UPDATE cases SET state = ?, claim_packet_json = ? WHERE case_id = ?",
            (CaseState.EMERGENCY_STOPPED.value, packet_json, blocked.case_id),
        )
        connection.execute(
            "UPDATE case_packet_authority SET packet_json = ?, packet_sha256 = ? "
            "WHERE case_id = ? AND bound_case_version = ?",
            (packet_json, packet_digest, blocked.case_id, blocked.version),
        )
        connection.execute(
            "UPDATE audit_events SET event_json = ? WHERE sequence = ?",
            (audit.model_dump_json(by_alias=True), state_projection.sequence),
        )
        connection.execute(
            "UPDATE workflow_events SET event_json = ? "
            "WHERE source_audit_sequence = ?",
            (
                forged_envelope.model_dump_json(by_alias=True),
                state_projection.sequence,
            ),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_clarification_exhausted_status_is_bound_to_manual_handoff_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "clarification-forged-confirmed.db"
    _service, repository, blocked = _three_round_blocked_case(database_path)
    close_projection = next(
        item
        for item in reversed(repository.list_workflow_events(blocked.case_id))
        if isinstance(item.envelope.event, ClarificationWorkflowEvent)
    )
    close = cast(ClarificationWorkflowEvent, close_projection.envelope.event)
    assert close.status is ClarificationStatus.EXHAUSTED
    forged_close = _clarification_event(
        ClarificationStatus.CONFIRMED,
        round_number=close.round,
        field=close.field.value,
    )
    forged_envelope = close_projection.envelope.model_copy(
        update={"event": forged_close}
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET event_json = ? "
            "WHERE source_audit_sequence = ?",
            (
                forged_envelope.model_dump_json(by_alias=True),
                close_projection.sequence,
            ),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_legitimate_clarification_exhaustion_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "clarification-exhausted-positive.db"
    _service, _repository, blocked = _three_round_blocked_case(database_path)

    assert SqliteCaseRepository(database_path).get_case(blocked.case_id) == blocked


def test_capability_issue_cannot_predate_its_current_case_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capability-issue-version-origin.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    before = repository.get_case(waiting.case_id)

    with pytest.raises(ValueError, match="bound case version"):
        repository.issue_authority_capability(
            case_id=waiting.case_id,
            expected_case_version=waiting.version,
            digest=hashlib.sha256(b"predates-version").digest(),
            role="agent",
            purpose="portal_run",
            issued_at=waiting.updated_at - timedelta(seconds=1),
            expires_at=waiting.updated_at + timedelta(seconds=29),
        )

    assert repository.get_case(waiting.case_id) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_capabilities"
        ).fetchone() == (0,)


@pytest.mark.parametrize("tampering", ("issued_before_origin", "consumed_after_version"))
def test_capability_reopen_binds_lifecycle_to_bound_version_window(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"capability-replay-window-{tampering}.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    digest = hashlib.sha256(tampering.encode()).digest()
    repository.issue_authority_capability(
        case_id=waiting.case_id,
        expected_case_version=waiting.version,
        digest=digest,
        role="agent",
        purpose="portal_run",
        issued_at=waiting.updated_at + timedelta(seconds=1),
        expires_at=waiting.updated_at + timedelta(seconds=31),
    )
    ready = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    ).case
    with sqlite3.connect(database_path) as connection:
        if tampering == "issued_before_origin":
            connection.execute(
                "UPDATE authority_capabilities SET issued_at = ? "
                "WHERE capability_digest = ?",
                ((waiting.updated_at - timedelta(seconds=1)).isoformat(), digest),
            )
        else:
            connection.execute(
                "UPDATE authority_capabilities SET consumed_at = ? "
                "WHERE capability_digest = ?",
                ((ready.updated_at + timedelta(seconds=1)).isoformat(), digest),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("offset", (timedelta(0), timedelta(microseconds=1)))
def test_case_mutation_rejects_capability_at_or_after_its_next_version_origin(
    tmp_path: Path,
    offset: timedelta,
) -> None:
    database_path = tmp_path / f"capability-next-origin-{offset.microseconds}.db"
    _service, repository, prefix, analyzing = _analysis_case(database_path)
    command = _initial_command(
        analyzing,
        prefix,
        target=CaseState.READY_TO_FILL,
    )
    issued_at = command.updated_at + offset
    digest = hashlib.sha256(f"next-origin-{offset}".encode()).digest()
    repository.issue_authority_capability(
        case_id=analyzing.case_id,
        expected_case_version=analyzing.version,
        digest=digest,
        role="agent",
        purpose="portal_run",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=30),
    )
    before = _counts(repository, analyzing.case_id)

    with pytest.raises(WorkflowAtomicityError, match="strictly follow"):
        repository.commit_analysis_workflow(command)

    assert repository.get_case(analyzing.case_id) == analyzing
    assert _counts(repository, analyzing.case_id) == before
    reopened = SqliteCaseRepository(database_path)
    assert reopened.get_case(analyzing.case_id) == analyzing
    assert reopened.get_authority_capability(digest) is not None


def test_capability_just_before_boundary_can_expire_and_be_revoked_historically(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capability-before-origin-lifecycle.db"
    _service, repository, prefix, analyzing = _analysis_case(database_path)
    command = _initial_command(
        analyzing,
        prefix,
        target=CaseState.READY_TO_FILL,
    )
    historical_digest = hashlib.sha256(b"historical-expired-capability").digest()
    repository.issue_authority_capability(
        case_id=analyzing.case_id,
        expected_case_version=analyzing.version,
        digest=historical_digest,
        role="agent",
        purpose="portal_run",
        issued_at=command.updated_at - timedelta(seconds=2),
        expires_at=command.updated_at - timedelta(seconds=1),
    )
    ready = repository.commit_analysis_workflow(command).case
    current_digest = hashlib.sha256(b"current-version-capability").digest()
    current_issued_at = ready.updated_at + timedelta(seconds=1)
    repository.issue_authority_capability(
        case_id=ready.case_id,
        expected_case_version=ready.version,
        digest=current_digest,
        role="agent",
        purpose="portal_run",
        issued_at=current_issued_at,
        expires_at=current_issued_at + timedelta(seconds=30),
    )

    reopened = SqliteCaseRepository(database_path)
    assert reopened.get_case(ready.case_id) == ready
    historical = reopened.get_authority_capability(historical_digest)
    current = reopened.get_authority_capability(current_digest)
    assert historical is not None and historical.bound_case_version == analyzing.version
    assert historical.revoked_at == current_issued_at
    assert current is not None and current.bound_case_version == ready.version


@pytest.mark.parametrize(
    ("offset", "allowed"),
    (
        (timedelta(microseconds=-1), True),
        (timedelta(0), False),
        (timedelta(microseconds=1), False),
    ),
)
def test_consumed_capability_obeys_the_same_half_open_version_boundary(
    tmp_path: Path,
    offset: timedelta,
    allowed: bool,
) -> None:
    database_path = tmp_path / f"capability-consumed-boundary-{offset}-{allowed}.db"
    _service, repository, prefix, analyzing = _analysis_case(database_path)
    command = _initial_command(
        analyzing,
        prefix,
        target=CaseState.READY_TO_FILL,
    )
    digest = hashlib.sha256(f"consumed-{offset}".encode()).digest()
    repository.issue_authority_capability(
        case_id=analyzing.case_id,
        expected_case_version=analyzing.version,
        digest=digest,
        role="agent",
        purpose="portal_run",
        issued_at=command.updated_at - timedelta(seconds=2),
        expires_at=command.updated_at + timedelta(seconds=30),
    )
    consumed_at = command.updated_at + offset
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE authority_capabilities SET consumed_at = ? "
            "WHERE capability_digest = ?",
            (consumed_at.isoformat(), digest),
        )

    if allowed:
        expected = repository.commit_analysis_workflow(command).case
    else:
        with pytest.raises(WorkflowAtomicityError, match="strictly follow"):
            repository.commit_analysis_workflow(command)
        expected = analyzing

    reopened = SqliteCaseRepository(database_path)
    assert reopened.get_case(analyzing.case_id) == expected
    consumed = reopened.get_authority_capability(digest)
    assert consumed is not None and consumed.consumed_at == consumed_at


def test_capability_issue_and_equal_timestamp_case_mutation_have_one_safe_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capability-mutation-race.db"
    _service, repository, prefix, analyzing = _analysis_case(database_path)
    command = _initial_command(
        analyzing,
        prefix,
        target=CaseState.READY_TO_FILL,
    )
    digest = hashlib.sha256(b"capability-mutation-race").digest()
    barrier = Barrier(2)

    def issue_once() -> str:
        barrier.wait()
        try:
            repository.issue_authority_capability(
                case_id=analyzing.case_id,
                expected_case_version=analyzing.version,
                digest=digest,
                role="agent",
                purpose="portal_run",
                issued_at=command.updated_at,
                expires_at=command.updated_at + timedelta(seconds=30),
            )
        except CaseRecordVersionConflictError:
            return "issue_stale"
        return "issued"

    def mutate_once() -> str:
        barrier.wait()
        try:
            repository.commit_analysis_workflow(command)
        except WorkflowAtomicityError:
            return "mutation_rejected"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        issued_future = executor.submit(issue_once)
        mutated_future = executor.submit(mutate_once)
        outcomes = {issued_future.result(), mutated_future.result()}
    assert outcomes in (
        {"issued", "mutation_rejected"},
        {"committed", "issue_stale"},
    )
    reopened = SqliteCaseRepository(database_path)
    current = reopened.get_case(analyzing.case_id)
    assert current is not None
    assert current.version in {analyzing.version, analyzing.version + 1}


@pytest.mark.parametrize(
    "tampering",
    ("expected_version", "case_id", "requested_at", "question"),
)
def test_active_clarification_tampering_fails_during_canonical_reopen(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"active-clarification-{tampering}.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    json_path: str
    value: object
    if tampering == "expected_version":
        json_path = "$.expectedVersion"
        value = waiting.version + 1
    elif tampering == "case_id":
        json_path = "$.caseId"
        value = "case-forged-clarification"
    elif tampering == "requested_at":
        json_path = "$.requestedAt"
        value = (waiting.updated_at + timedelta(seconds=1)).isoformat()
    else:
        json_path = "$.question"
        value = "A forged clarification question?"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET active_clarification_json = "
            "json_set(active_clarification_json, ?, ?) WHERE case_id = ?",
            (json_path, value, waiting.case_id),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("tampering", ("delete", "mutate"))
def test_active_clarification_requires_immutable_requested_event(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"clarification-event-{tampering}.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    requested = next(
        item
        for item in repository.list_workflow_events(waiting.case_id)
        if isinstance(item.envelope.event, ClarificationWorkflowEvent)
        and item.envelope.event.status is ClarificationStatus.REQUESTED
    )
    with sqlite3.connect(database_path) as connection:
        if tampering == "delete":
            connection.execute(
                "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
                (requested.sequence,),
            )
        else:
            envelope_data = requested.envelope.model_dump(mode="json", by_alias=True)
            cast(dict[str, Any], envelope_data["event"])["field"] = "location"
            forged = WorkflowEventEnvelope.model_validate(envelope_data)
            connection.execute(
                "UPDATE workflow_events SET event_json = ? "
                "WHERE source_audit_sequence = ?",
                (forged.model_dump_json(by_alias=True), requested.sequence),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("tampering", ("version", "updated_at"))
def test_case_version_and_timestamp_must_equal_workflow_replay(
    tmp_path: Path,
    tampering: str,
) -> None:
    database_path = tmp_path / f"case-replay-{tampering}.db"
    _service, _repository, _prefix, analyzing = _analysis_case(database_path)
    with sqlite3.connect(database_path) as connection:
        if tampering == "version":
            connection.execute(
                "UPDATE cases SET version = 37 WHERE case_id = ?",
                (analyzing.case_id,),
            )
        else:
            connection.execute(
                "UPDATE cases SET updated_at = ? WHERE case_id = ?",
                (
                    (analyzing.updated_at + timedelta(days=1)).isoformat(),
                    analyzing.case_id,
                ),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_active_clarification_is_rejected_outside_awaiting_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "active-clarification-state.db"
    _service, _repository, _prefix, analyzing = _analysis_case(database_path)
    active = _clarification_view(
        analyzing.case_id,
        analyzing.version,
        round_number=1,
        requested_at=analyzing.updated_at,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE cases SET active_clarification_json = ? WHERE case_id = ?",
            (active.model_dump_json(by_alias=True), analyzing.case_id),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_analyzing_gate_history_is_revalidated_during_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analyzing-history.db"
    _service, repository, _prefix, analyzing = _analysis_case(database_path)
    forged = _gate(GateId.G2_OUTPUT_CONTRACT, 0)
    with repository._write_connection() as connection:
        repository._insert_gate_decision_row(
            connection,
            case_id=analyzing.case_id,
            decision=forged,
        )
        audit = build_gate_audit_event(
            case_id=analyzing.case_id,
            decision=forged,
            actor=ActorType.SYSTEM,
        )
        audit_sequence = repository._insert_audit_event(connection, audit)
        repository._insert_workflow_projection(
            connection,
            audit_sequence=audit_sequence,
            audit=audit,
            event=GateWorkflowEvent.model_validate(
                {"kind": WorkflowEventKind.GATE, "decision": forged}
            ),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("confirmed_case", (False, True))
def test_transcript_confirmation_metadata_is_bound_to_case_state_on_reopen(
    tmp_path: Path,
    confirmed_case: bool,
) -> None:
    database_path = tmp_path / f"transcript-state-{confirmed_case}.db"
    _service, _repository, current = _audio_transcript_case(
        database_path,
        confirmed=confirmed_case,
    )
    with sqlite3.connect(database_path) as connection:
        if confirmed_case:
            connection.execute(
                "UPDATE case_transcripts SET version = 1, confirmed = 0, "
                "confirmed_at = NULL WHERE case_id = ?",
                (current.case_id,),
            )
        else:
            connection.execute(
                "UPDATE case_transcripts SET version = 2, confirmed = 1, "
                "confirmed_at = created_at WHERE case_id = ?",
                (current.case_id,),
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_audio_statement_cannot_lose_transcript_authority_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "transcript-authority-removed.db"
    _service, _repository, analyzing = _audio_transcript_case(
        database_path,
        confirmed=True,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM case_transcript_authority WHERE case_id = ?",
            (analyzing.case_id,),
        )
        connection.execute(
            "DELETE FROM case_transcripts WHERE case_id = ?",
            (analyzing.case_id,),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_workflow_snapshot_is_consistent_when_transcript_is_confirmed_mid_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, waiting = _audio_transcript_case(
        tmp_path / "snapshot-confirm-race.db",
        confirmed=False,
    )
    pending = repository.get_transcript(waiting.case_id)
    assert pending is not None
    assert pending.confirmed is False
    original = repository._get_transcript_confirmation_view_in_connection
    triggered = False

    def confirm_then_project(
        connection: sqlite3.Connection,
        current: CaseRecord,
    ) -> object:
        nonlocal triggered
        if not triggered:
            triggered = True
            confirmed = repository.confirm_transcript_and_transition(
                case_id=waiting.case_id,
                expected_case_version=waiting.version,
                transcript_id=pending.transcript_id,
                transcript_sha256=pending.transcript_sha256,
                updated_at=NOW + timedelta(seconds=2),
            )
            assert confirmed.case.state is CaseState.ANALYZING
        return original(connection, current)

    monkeypatch.setattr(
        repository,
        "_get_transcript_confirmation_view_in_connection",
        confirm_then_project,
    )

    snapshot = service.get_workflow_snapshot(
        waiting.case_id,
        request_id="request-snapshot-confirm-race",
    )

    assert triggered
    assert snapshot.case.state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION
    assert snapshot.case.version == waiting.version
    assert snapshot.transcript_confirmation is not None
    assert snapshot.transcript_confirmation.version == waiting.version
    assert snapshot.transcript_confirmation.confirmed is False
    latest = repository.get_case(waiting.case_id)
    assert latest is not None
    assert latest.state is CaseState.ANALYZING


def test_workflow_event_listing_is_consistent_when_case_is_deleted_mid_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _prefix, case = _analysis_case(
        tmp_path / "event-delete-race.db"
    )
    expected = repository.list_workflow_events(case.case_id)
    assert expected
    original = repository._read_workflow_event_rows
    triggered = False

    def delete_then_read(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        after: int,
        limit: int,
    ) -> list[sqlite3.Row]:
        nonlocal triggered
        if not triggered:
            triggered = True
            service.delete_case(case_id)
        return original(
            connection,
            case_id=case_id,
            after=after,
            limit=limit,
        )

    monkeypatch.setattr(repository, "_read_workflow_event_rows", delete_then_read)

    actual = service.list_workflow_events(case.case_id)

    assert triggered
    assert actual == expected
    assert repository.get_case(case.case_id) is None


def test_initial_analysis_commits_one_version_in_redacted_cursor_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis.db"
    service, repository, prefix, current = _analysis_case(database_path)
    command = _initial_command(current, prefix)

    result = service.commit_analysis_workflow(command)

    assert result.case.version == current.version + 1
    assert result.case.state is CaseState.AWAITING_CLARIFICATION
    assert [item.event.kind for item in result.workflow_events] == [
        "provider_call",
        "gate",
        "gate",
        "gate",
        "gate",
        *("plan_step" for _step in cast(ClaimPacket, command.claim_packet).plan.steps),
        "clarification",
        "state",
    ]
    assert [item.decision.gate_id for item in repository.list_gate_decisions(current.case_id)] == [
        *GATE_SEQUENCE,
    ]
    assert len(repository.list_provider_usage(current.case_id)) == 1
    stored_active = cast(dict[str, Any], result.case.snapshot.active_clarification)
    assert "answer" not in stored_active
    assert ClarificationView.model_validate(stored_active) == command.active_clarification
    before_closed_writer_probe = _counts(repository, result.case.case_id)
    with pytest.raises(AttributeError):
        object.__getattribute__(service, "replace_redacted_metadata")
    with pytest.raises(AttributeError):
        object.__getattribute__(repository, "replace_snapshot")
    assert service.get_case(result.case.case_id) == result.case
    assert _counts(repository, result.case.case_id) == before_closed_writer_probe
    persisted = database_path.read_text(encoding="utf-8", errors="ignore")
    for forbidden in ("providerMessage", "rawAnswer", "mediaPath"):
        assert forbidden not in persisted


def test_initial_retry_persists_call_retry_call_and_three_ledger_rows(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "retry.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL, retry=True)

    result = service.commit_analysis_workflow(command)

    assert [event.event.kind for event in result.workflow_events[:3]] == [
        "provider_call",
        "retry",
        "provider_call",
    ]
    usage = repository.list_provider_usage(current.case_id)
    assert [(item.call_sequence, item.retry_attempt, item.status) for item in usage] == [
        (1, 0, "succeeded"),
        (1, 1, "retry_scheduled"),
        (2, 1, "succeeded"),
    ]


def test_round_two_and_three_are_same_state_cas_without_provider_or_state_event(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "rounds.db")
    round_one = service.commit_analysis_workflow(
        _initial_command(
            current,
            prefix,
            missing_fields=(
                RequiredClaimField.INCIDENT_TIME,
                RequiredClaimField.LOCATION,
                RequiredClaimField.CLAIMANT_NAME,
                RequiredClaimField.POLICY_REFERENCE,
            ),
        )
    ).case

    round_two_result = service.commit_analysis_workflow(
        _continuation_command(round_one, target=CaseState.AWAITING_CLARIFICATION)
    )
    round_two = round_two_result.case
    round_three_result = service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.AWAITING_CLARIFICATION)
    )
    round_three = round_three_result.case

    for result, expected_round in ((round_two_result, 2), (round_three_result, 3)):
        kinds = [event.event.kind for event in result.workflow_events]
        assert "provider_call" not in kinds
        assert "retry" not in kinds
        assert "state" not in kinds
        assert kinds[-2:] == ["clarification", "clarification"]
        active = ClarificationView.model_validate(result.case.snapshot.active_clarification)
        assert active.round == expected_round
        assert active.expected_version == result.case.version
    assert round_three.version == round_one.version + 2
    assert len(repository.list_provider_usage(current.case_id)) == 1

    blocked = service.commit_analysis_workflow(
        _continuation_command(round_three, target=CaseState.BLOCKED)
    )
    assert blocked.case.state is CaseState.BLOCKED
    assert blocked.case.snapshot.active_clarification is None
    assert [event.event.kind for event in blocked.workflow_events] == [
        "gate",
        "gate",
        "plan_step",
        "plan_step",
        "clarification",
        "state",
    ]
    blocked_packet = cast(ClaimPacket, blocked.case.snapshot.claim_packet)
    assert [(step.tool.value, step.reason) for step in blocked_packet.plan.steps] == [
        ("inspect_evidence", "Inspect only the approved evidence inventory"),
        ("check_required_fields", "Use the deterministic required-field result"),
    ]
    assert cast(ClarificationWorkflowEvent, blocked.workflow_events[-2].event).status is (
        ClarificationStatus.EXHAUSTED
    )


def test_clarification_answer_rechecks_only_g4_g5_and_reaches_ready(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "ready.db")
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case

    result = service.commit_analysis_workflow(
        _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    )

    assert result.case.state is CaseState.READY_TO_FILL
    assert result.case.snapshot.active_clarification is None
    kinds = [event.event.kind for event in result.workflow_events]
    assert kinds[:2] == ["gate", "gate"]
    assert "provider_call" not in kinds
    assert kinds[-2:] == ["clarification", "state"]
    assert len(repository.list_provider_usage(current.case_id)) == 1
    latest = repository.list_gate_decisions(current.case_id)[-2:]
    assert [item.decision.gate_id for item in latest] == [
        GateId.G4_PROVENANCE,
        GateId.G5_COMPLETENESS,
    ]
    assert all(item.decision.passed for item in latest)


def test_free_model_narrative_is_projected_and_never_reused(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "narrative-ok.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    packet = cast(ClaimPacket, command.claim_packet)
    free_text = "The other driver is liable and must pay this claim."
    free_claim_data = packet.claim.model_dump(mode="json", by_alias=True)
    free_claim_data["narrative"] = free_text
    free_claim = type(packet.claim).model_validate(free_claim_data)
    narrative_source = next(
        item.source_refs
        for item in packet.claim.field_provenance
        if item.field is RequiredClaimField.NARRATIVE
    )
    free_fact = EvidenceFact.model_validate(
        {
            "factId": "fact-model-free-narrative",
            "field": "narrative",
            "value": free_text,
            "status": "user_stated",
            "sourceRefs": narrative_source,
            "confidence": None,
        }
    )
    free_extraction = ModelExtraction.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "evidence": packet.evidence,
            "provenance": packet.provenance,
            "facts": (
                *(fact for fact in packet.facts if fact.field is not EvidenceField.NARRATIVE),
                free_fact,
            ),
            "claim": free_claim,
        }
    )
    projected = project_deterministic_narrative(free_extraction)
    assert projected.claim.narrative == packet.claim.narrative
    assert projected.claim.narrative != free_text

    result = service.commit_analysis_workflow(
        _bind_packet_extraction(command, packet, extraction=free_extraction)
    )
    assert cast(ClaimPacket, result.case.snapshot.claim_packet).claim.narrative == (
        packet.claim.narrative
    )

    bad_service, _bad_repository, bad_prefix, bad_current = _analysis_case(
        tmp_path / "narrative-bad.db"
    )
    bad_command = _initial_command(
        bad_current,
        bad_prefix,
        target=CaseState.READY_TO_FILL,
    )
    bad_packet_data = cast(ClaimPacket, bad_command.claim_packet).model_dump(
        mode="json",
        by_alias=True,
    )
    bad_packet_data["claim"]["narrative"] = free_text
    bad_packet_data["facts"] = tuple(
        free_fact.model_dump(mode="json", by_alias=True) if fact["field"] == "narrative" else fact
        for fact in cast(list[dict[str, Any]], bad_packet_data["facts"])
    )
    unsafe_packet = ClaimPacket.model_validate(bad_packet_data)
    with pytest.raises(CaseSnapshotValidationError, match="neutral narrative"):
        bad_service.commit_analysis_workflow(_bind_packet_extraction(bad_command, unsafe_packet))


def test_recomputed_g4_rejects_forged_pass_for_low_confidence_fact(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "g4-forged.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    packet = cast(ClaimPacket, command.claim_packet)
    low_confidence = EvidenceFact.model_validate(
        {
            "factId": "fact-low-confidence-shadow",
            "field": "visible_damage",
            "value": "rear_bumper_dent",
            "status": "observed",
            "sourceRefs": ("prov-image-2",),
            "confidence": 0.20,
        }
    )
    packet_data = packet.model_dump(mode="json", by_alias=True)
    packet_data["facts"] = (*packet.facts, low_confidence)
    forged_packet = ClaimPacket.model_validate(packet_data)

    with pytest.raises(CaseSnapshotValidationError, match="recomputed packet provenance"):
        service.commit_analysis_workflow(_bind_packet_extraction(command, forged_packet))


def test_fixed_question_and_strict_answer_delta_reject_untrusted_changes(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "clarification-auth.db")
    initial = _initial_command(current, prefix)
    active = cast(ClarificationView, initial.active_clarification)
    active_data = active.model_dump(mode="json", by_alias=True)
    active_data["question"] = "What is your portal password?"
    arbitrary_question = replace(
        initial,
        active_clarification=ClarificationView.model_validate(active_data),
    )
    before = _counts(repository, current.case_id)
    with pytest.raises(CaseSnapshotValidationError, match="fixed server question"):
        service.commit_analysis_workflow(arbitrary_question)
    assert _counts(repository, current.case_id) == before

    waiting = service.commit_analysis_workflow(initial).case
    continuation = _continuation_command(waiting, target=CaseState.READY_TO_FILL)
    packet = cast(ClaimPacket, continuation.claim_packet)
    packet_data = packet.model_dump(mode="json", by_alias=True)
    packet_data["claim"]["location"] = "Munich"
    packet_data["facts"] = tuple(
        {**fact, "value": "Munich"} if fact["field"] == "location" else fact
        for fact in cast(list[dict[str, Any]], packet_data["facts"])
    )
    unrelated_delta = ClaimPacket.model_validate(packet_data)
    with pytest.raises(CaseSnapshotValidationError, match="outside its field"):
        service.commit_analysis_workflow(
            replace(
                continuation,
                claim_packet=unrelated_delta,
                plan_steps=_plan_events(unrelated_delta),
            )
        )


def test_emergency_target_and_user_stated_safety_authority_are_derived(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "emergency.db")
    emergency = _initial_command(
        current,
        prefix,
        target=CaseState.EMERGENCY_STOPPED,
    )
    emergency_packet = cast(ClaimPacket, emergency.claim_packet)
    wrong_packet_data = emergency_packet.model_dump(mode="json", by_alias=True)
    wrong_packet_data["state"] = CaseState.BLOCKED
    wrong_target = replace(
        emergency,
        target=CaseState.BLOCKED,
        claim_packet=ClaimPacket.model_validate(wrong_packet_data),
    )
    with pytest.raises(CaseSnapshotValidationError, match="emergency_stopped"):
        service.commit_analysis_workflow(wrong_target)
    assert emergency.safety_input is not None
    forged_safe_input = replace(emergency.safety_input, injury_reported=False)
    with pytest.raises(CaseSnapshotValidationError, match="final G2 extraction"):
        service.commit_analysis_workflow(replace(emergency, safety_input=forged_safe_input))

    result = service.commit_analysis_workflow(emergency)
    assert result.case.state is CaseState.EMERGENCY_STOPPED

    unsafe_service, _unsafe_repository, unsafe_prefix, unsafe_current = _analysis_case(
        tmp_path / "observed-safety.db"
    )
    unsafe = _initial_command(
        unsafe_current,
        unsafe_prefix,
        target=CaseState.READY_TO_FILL,
    )
    unsafe_packet = cast(ClaimPacket, unsafe.claim_packet)
    unsafe_packet_data = unsafe_packet.model_dump(mode="json", by_alias=True)
    unsafe_packet_data["facts"] = tuple(
        {
            **fact,
            "status": "observed",
            "sourceRefs": ("prov-image-1",),
            "confidence": 0.99,
        }
        if fact["field"] == "injury_status"
        else fact
        for fact in cast(list[dict[str, Any]], unsafe_packet_data["facts"])
    )
    observed_image_packet = ClaimPacket.model_validate(unsafe_packet_data)
    with pytest.raises(CaseSnapshotValidationError, match="user-stated"):
        unsafe_service.commit_analysis_workflow(
            _bind_packet_extraction(unsafe, observed_image_packet)
        )


def test_conflict_flow_resolves_repeats_and_limits_only_at_round_three(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "conflict-ready.db")
    one_conflict = service.commit_analysis_workflow(
        _initial_conflict_command(
            current,
            prefix,
            fields=(RequiredClaimField.LOCATION,),
        )
    ).case
    assert cast(ClaimPacket, one_conflict.snapshot.claim_packet).gate_decisions[
        -2
    ].reason_codes == (GateReasonCode.G4_CONFLICTING_SOURCES,)
    ready = service.commit_analysis_workflow(
        _continuation_command(one_conflict, target=CaseState.READY_TO_FILL)
    )
    assert ready.case.state is CaseState.READY_TO_FILL

    repeated_service, _repeated_repository, repeated_prefix, repeated_current = _analysis_case(
        tmp_path / "conflict-limit.db"
    )
    conflicts = (
        RequiredClaimField.LOCATION,
        RequiredClaimField.CLAIMANT_NAME,
        RequiredClaimField.POLICY_REFERENCE,
        RequiredClaimField.VEHICLE_REGISTRATION,
    )
    round_one = repeated_service.commit_analysis_workflow(
        _initial_conflict_command(
            repeated_current,
            repeated_prefix,
            fields=conflicts,
        )
    ).case
    round_two = repeated_service.commit_analysis_workflow(
        _continuation_command(round_one, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    round_three = repeated_service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    blocked = repeated_service.commit_analysis_workflow(
        _continuation_command(round_three, target=CaseState.BLOCKED)
    )
    assert blocked.case.state is CaseState.BLOCKED
    assert cast(ClaimPacket, blocked.case.snapshot.claim_packet).gate_decisions[
        -1
    ].reason_codes == (
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        GateReasonCode.G5_CLARIFICATION_LIMIT,
    )


def test_round_one_cannot_forge_g5_clarification_limit(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "early-limit.db")
    waiting = service.commit_analysis_workflow(
        _initial_conflict_command(
            current,
            prefix,
            fields=(
                RequiredClaimField.LOCATION,
                RequiredClaimField.CLAIMANT_NAME,
            ),
        )
    ).case
    valid = _continuation_command(waiting, target=CaseState.AWAITING_CLARIFICATION)
    base_offset = int((waiting.updated_at - NOW).total_seconds())
    forged_g5 = _gate(
        GateId.G5_COMPLETENESS,
        base_offset + 2,
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        GateReasonCode.G5_CLARIFICATION_LIMIT,
    )
    packet = cast(ClaimPacket, valid.claim_packet)
    blocked_plan = _packet(waiting.case_id, CaseState.BLOCKED, (), missing=True).plan
    packet_data = packet.model_dump(mode="json", by_alias=True)
    packet_data.update(
        {
            "state": CaseState.BLOCKED,
            "plan": blocked_plan,
            "gateDecisions": (
                *packet.gate_decisions[:5],
                forged_g5,
            ),
        }
    )
    forged_packet = ClaimPacket.model_validate(packet_data)
    stored_active = ClarificationView.model_validate(waiting.snapshot.active_clarification)
    invalid = replace(
        valid,
        target=CaseState.BLOCKED,
        claim_packet=forged_packet,
        active_clarification=None,
        gate_decisions=(valid.gate_decisions[0], forged_g5),
        plan_steps=_plan_events(forged_packet),
        clarification_events=(
            _clarification_event(
                ClarificationStatus.EXHAUSTED,
                round_number=stored_active.round,
                field=stored_active.field.value,
            ),
        ),
    )
    with pytest.raises(CaseSnapshotValidationError, match="recomputed clarification completeness"):
        service.commit_analysis_workflow(invalid)


def test_unanswerable_narrative_and_attachment_blockers_fail_closed() -> None:
    packet = _packet(
        "case-unanswerable",
        CaseState.BLOCKED,
        (),
        missing=False,
    )
    narrative_data = packet.model_dump(mode="json", by_alias=True)
    narrative_data["claim"]["narrative"] = None
    narrative_data["claim"]["missingRequiredFields"] = ("narrative",)
    narrative_data["claim"]["fieldProvenance"] = tuple(
        item
        for item in cast(list[dict[str, Any]], narrative_data["claim"]["fieldProvenance"])
        if item["field"] != "narrative"
    )
    narrative_data["facts"] = tuple(
        fact
        for fact in cast(list[dict[str, Any]], narrative_data["facts"])
        if fact["field"] != "narrative"
    )
    narrative_missing = ClaimPacket.model_validate(narrative_data)
    provenance = evaluate_g4(narrative_missing, decided_at=NOW)
    assert provenance.decision.passed
    completeness = SqliteCaseRepository._derive_completeness(
        provenance,
        completed_rounds=0,
        decided_at=NOW,
    )
    assert completeness.accepted_question is None
    assert GateReasonCode.G5_QUESTION_INVALID in completeness.decision.reason_codes
    assert SqliteCaseRepository._target_for_completeness(completeness) is CaseState.BLOCKED

    attachments_data = packet.model_dump(mode="json", by_alias=True)
    attachments_data["claim"]["attachments"] = ("local-ref-1", "local-ref-2")
    attachments_data["claim"]["missingRequiredFields"] = ("attachments",)
    with pytest.raises(ValidationError):
        ClaimPacket.model_validate(attachments_data)


def test_g2_retry_telemetry_is_bound_to_exact_output_contract_run(
    tmp_path: Path,
) -> None:
    service, _repository, prefix, current = _analysis_case(tmp_path / "retry-binding.db")
    command = _initial_command(
        current,
        prefix,
        target=CaseState.READY_TO_FILL,
        retry=True,
    )
    final = command.g2_attempts[-1]
    altered_final = replace(
        final,
        decided_at=NOW + timedelta(seconds=2, milliseconds=1),
    )
    with pytest.raises(CaseSnapshotValidationError, match="final canonical G2"):
        service.commit_analysis_workflow(
            replace(
                command,
                g2_attempts=(*command.g2_attempts[:-1], altered_final),
            )
        )


def test_analysis_commit_preserves_confirmed_transcript_and_bound_summary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "transcript-analysis.db"
    clock = [NOW]
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: clock[0],
        case_id_factory=lambda: "case-atomic",
    )
    case = service.create_case()
    request = IntakeRequest(
        images=tuple(
            ImageUpload(content=_image_bytes(index), media_type="image/png")
            for index in range(1, 4)
        ),
        text=None,
        audio=AudioUpload(content=_audio_bytes(), media_type="audio/wav"),
        consents=IntakeConsents(True, True, True),
    )
    review = PrivacyReview(
        exif_choices=tuple(
            ExifChoice(input_id=f"image-{index}", decision=ExifDecision.STRIP)
            for index in range(1, 4)
        ),
        model_copy_approved=True,
        audit_fields=(),
    )
    case = service.commit_intake_disclosure(
        IntakeDisclosureCommand(
            case_id=case.case_id,
            expected_version=case.version,
            request=request,
            privacy_review=review,
            g0_decided_at=NOW,
            g1_decided_at=NOW,
            updated_at=NOW,
        )
    )
    disclosed = case
    disclosed_summary = cast(dict[str, Any], disclosed.snapshot.intake_summary)
    assert disclosed_summary["statement"] is None
    assert repository.get_transcript(disclosed.case_id) is None
    prefix_values = repository.list_gate_decisions(case.case_id)
    prefix = (prefix_values[0].decision, prefix_values[1].decision)
    outcome = TranscriptionSuccess(
        transcript=STATEMENT_TEXT,
        telemetry=ProviderCallTelemetry(
            operation=WorkflowOperation.TRANSCRIPTION,
            model_id=ProviderModelId.DETERMINISTIC_MOCK,
            provider_mode=ProviderMode.MOCK,
            call_sequence=1,
            retry_attempt=0,
            duration_ms=5,
            status=ProviderCallStatus.SUCCEEDED,
        ),
    )
    waiting_result = service.commit_transcription_outcome(
        TranscriptionOutcomeCommand(
            case_id=case.case_id,
            expected_version=case.version,
            outcome=outcome,
            occurred_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    )
    waiting = waiting_result.case
    pending = waiting_result.transcript
    assert pending is not None
    assert waiting.version == disclosed.version + 1
    assert waiting.snapshot.portal_state is disclosed.snapshot.portal_state
    assert waiting.snapshot.redacted_metadata == disclosed.snapshot.redacted_metadata
    assert waiting.snapshot.claim_packet == disclosed.snapshot.claim_packet
    assert waiting.snapshot.active_clarification == disclosed.snapshot.active_clarification
    waiting_summary = cast(dict[str, Any], waiting.snapshot.intake_summary)
    assert {key: value for key, value in waiting_summary.items() if key != "statement"} == {
        key: value for key, value in disclosed_summary.items() if key != "statement"
    }
    assert waiting_summary["statement"] is not None
    before_closed_writer_probe = _counts(repository, waiting.case_id)
    with pytest.raises(AttributeError):
        object.__getattribute__(service, "replace_redacted_metadata")
    with pytest.raises(AttributeError):
        object.__getattribute__(repository, "replace_snapshot")
    assert service.get_case(waiting.case_id) == waiting
    assert _counts(repository, waiting.case_id) == before_closed_writer_probe
    confirmation = TranscriptConfirmationRequest.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": waiting.case_id,
            "transcriptId": pending.transcript_id,
            "transcriptSha256": pending.transcript_sha256,
            "expectedVersion": waiting.version,
            "confirmed": True,
        }
    )
    clock[0] = NOW + timedelta(seconds=1)
    analyzing = service.confirm_transcript(
        waiting.case_id,
        expected_case_version=waiting.version,
        confirmation=confirmation,
    ).case
    assert analyzing.snapshot == waiting.snapshot
    assert tuple(item.decision for item in repository.list_gate_decisions(case.case_id)) == prefix
    state_events = tuple(
        event
        for item in repository.list_workflow_events(case.case_id)
        if isinstance((event := item.envelope.event), StateWorkflowEvent)
    )
    assert tuple(
        (event.actor, event.from_state, event.to_state) for event in state_events
    ) == (
        (ActorType.HUMAN, CaseState.CREATED, CaseState.DISCLOSED),
        (
            ActorType.SYSTEM,
            CaseState.DISCLOSED,
            CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
        ),
        (
            ActorType.HUMAN,
            CaseState.AWAITING_TRANSCRIPT_CONFIRMATION,
            CaseState.ANALYZING,
        ),
    )
    confirmed = repository.get_transcript(case.case_id)
    assert confirmed is not None and confirmed.confirmed is True
    bound_summary = analyzing.snapshot.intake_summary
    command = _initial_command(analyzing, prefix, target=CaseState.READY_TO_FILL)
    packet = cast(ClaimPacket, command.claim_packet)
    foreign_text = "A different confirmed transcript."
    foreign_evidence_data = packet.evidence[-1].model_dump(mode="json", by_alias=True)
    foreign_evidence_data.update(
        {
            "localRef": "foreign-transcript.txt",
            "sha256": hashlib.sha256(foreign_text.encode("utf-8")).hexdigest(),
            "text": foreign_text,
        }
    )
    foreign_packet_data = packet.model_dump(mode="json", by_alias=True)
    foreign_packet_data["evidence"] = (
        *packet.evidence[:-1],
        EvidenceItem.model_validate(foreign_evidence_data),
    )
    foreign_packet = ClaimPacket.model_validate(foreign_packet_data)
    before = _counts(repository, analyzing.case_id)
    with pytest.raises(CaseSnapshotValidationError, match="persisted content identity"):
        service.commit_analysis_workflow(_bind_packet_extraction(command, foreign_packet))
    assert service.get_case(analyzing.case_id) == analyzing
    assert _counts(repository, analyzing.case_id) == before

    result = service.commit_analysis_workflow(command)

    assert repository.get_transcript(case.case_id) == confirmed
    assert result.case.snapshot.intake_summary == bound_summary


@pytest.mark.parametrize("fault", ["ledger", "projection"])
def test_analysis_faults_roll_back_case_gates_audit_workflow_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / f"{fault}.db")
    command = _initial_command(current, prefix)
    before = _counts(repository, current.case_id)
    if fault == "ledger":

        def fail_ledger(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected ledger fault")

        monkeypatch.setattr(repository, "_insert_provider_usage_projection", fail_ledger)
    else:
        original = repository._insert_workflow_projection
        calls = 0

        def fail_projection(*args: object, **kwargs: object) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected projection fault")
            return cast(Any, original)(*args, **kwargs)

        monkeypatch.setattr(repository, "_insert_workflow_projection", fail_projection)

    with pytest.raises(RuntimeError, match="injected"):
        service.commit_analysis_workflow(command)

    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_analysis_stale_service_cas_and_concurrent_commands_have_one_winner(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "concurrent.db")
    command = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    barrier = Barrier(2)

    def commit_once() -> str:
        barrier.wait()
        try:
            service.commit_analysis_workflow(command)
        except CaseVersionConflictError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: commit_once(), range(2)))
    assert sorted(outcomes) == ["committed", "stale"]
    assert service.get_case(current.case_id).version == current.version + 1
    assert [item.decision.gate_id for item in repository.list_gate_decisions(current.case_id)] == [
        *GATE_SEQUENCE,
    ]

    with pytest.raises(CaseVersionConflictError):
        service.commit_analysis_workflow(command)


def test_gate_matrix_rejects_missing_failed_and_clarifiable_block_without_writes(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "gate-matrix.db")
    base = _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    before = _counts(repository, current.case_id)

    for invalid in (
        replace(base, gate_decisions=base.gate_decisions[:-1]),
        _initial_command(current, prefix, target=CaseState.AWAITING_CLARIFICATION),
    ):
        if invalid.target is CaseState.AWAITING_CLARIFICATION:
            packet = cast(ClaimPacket, invalid.claim_packet).model_copy(
                update={"state": CaseState.BLOCKED}
            )
            invalid = replace(
                invalid,
                target=CaseState.BLOCKED,
                claim_packet=packet,
                active_clarification=None,
                plan_steps=(),
                clarification_events=(),
            )
        with pytest.raises(CaseSnapshotValidationError):
            service.commit_analysis_workflow(invalid)
        assert service.get_case(current.case_id) == current
        assert _counts(repository, current.case_id) == before

    failed_g5 = _gate(
        GateId.G5_COMPLETENESS,
        5,
        GateReasonCode.G5_REQUIRED_FIELD_MISSING,
    )
    failed_ready_gates = (*base.gate_decisions[:-1], failed_g5)
    failed_ready_packet = _packet(
        current.case_id,
        CaseState.READY_TO_FILL,
        (*prefix, *failed_ready_gates),
        missing=True,
    )
    with pytest.raises(CaseSnapshotValidationError):
        service.commit_analysis_workflow(
            replace(
                base,
                gate_decisions=failed_ready_gates,
                claim_packet=failed_ready_packet,
                plan_steps=_plan_events(failed_ready_packet),
            )
        )
    assert _counts(repository, current.case_id) == before


def test_awaiting_plan_rejects_fill_tool_and_free_reason_text(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "plan-authority.db")
    command = _initial_command(current, prefix)
    packet = cast(ClaimPacket, command.claim_packet)
    ready_packet = _packet(
        current.case_id,
        CaseState.READY_TO_FILL,
        packet.gate_decisions,
        missing=False,
    )
    fill_plan_packet = packet.model_copy(update={"plan": ready_packet.plan})
    fill_command = replace(
        command,
        claim_packet=fill_plan_packet,
        plan_steps=_plan_events(fill_plan_packet),
    )
    bad_step = packet.plan.steps[0].model_copy(update={"reason": "untrusted portal text"})
    bad_plan = packet.plan.model_copy(update={"steps": (bad_step, *packet.plan.steps[1:])})
    reason_packet = packet.model_copy(update={"plan": bad_plan})
    reason_command = replace(
        command,
        claim_packet=reason_packet,
        plan_steps=_plan_events(reason_packet),
    )

    for invalid in (fill_command, reason_command):
        with pytest.raises(CaseSnapshotValidationError, match="safe plan"):
            service.commit_analysis_workflow(invalid)
        assert service.get_case(current.case_id) == current
        assert repository.list_provider_usage(current.case_id) == ()


def test_blocked_plan_rejects_fill_or_clarification_authority(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "blocked-plan.db")
    round_one = service.commit_analysis_workflow(
        _initial_command(
            current,
            prefix,
            missing_fields=(
                RequiredClaimField.INCIDENT_TIME,
                RequiredClaimField.LOCATION,
                RequiredClaimField.CLAIMANT_NAME,
                RequiredClaimField.POLICY_REFERENCE,
            ),
        )
    ).case
    round_two = service.commit_analysis_workflow(
        _continuation_command(round_one, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    round_three = service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.AWAITING_CLARIFICATION)
    ).case
    command = _continuation_command(round_three, target=CaseState.BLOCKED)
    packet = cast(ClaimPacket, command.claim_packet)
    ready_plan = _packet(
        round_three.case_id,
        CaseState.READY_TO_FILL,
        (),
        missing=False,
    ).plan
    unsafe_packet = packet.model_copy(update={"plan": ready_plan})
    invalid = replace(
        command,
        claim_packet=unsafe_packet,
        plan_steps=_plan_events(unsafe_packet),
    )
    before = _counts(repository, round_three.case_id)

    with pytest.raises(CaseSnapshotValidationError, match="safe plan"):
        service.commit_analysis_workflow(invalid)

    assert service.get_case(round_three.case_id) == round_three
    assert _counts(repository, round_three.case_id) == before


@pytest.mark.parametrize("tampering", ["case", "attachments", "passed"])
def test_model_copy_cannot_bypass_canonical_command_validation(
    tmp_path: Path,
    tampering: str,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / f"model-copy-{tampering}.db")
    command = _initial_command(current, prefix)
    packet = cast(ClaimPacket, command.claim_packet)
    if tampering == "case":
        invalid = replace(
            command,
            claim_packet=packet.model_copy(update={"case_id": "case-forged"}),
        )
    elif tampering == "attachments":
        claim = packet.claim.model_copy(update={"attachments": ("one", "two")})
        invalid = replace(
            command,
            claim_packet=packet.model_copy(update={"claim": claim}),
        )
    else:
        gates = (
            command.gate_decisions[0].model_copy(update={"passed": False}),
            *command.gate_decisions[1:],
        )
        invalid = replace(command, gate_decisions=gates)

    before = _counts(repository, current.case_id)
    with pytest.raises(CaseSnapshotValidationError, match="canonical|caseId"):
        service.commit_analysis_workflow(invalid)
    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_forged_latest_gate_prefix_and_tampered_json_fail_closed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    service, repository, prefix, current = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case
    command = _continuation_command(waiting, target=CaseState.READY_TO_FILL)

    later_service = CaseService(
        repository,
        now=lambda: NOW + timedelta(seconds=11),
        case_id_factory=lambda: "unused-case-id",
    )
    assert not hasattr(later_service, "record_gate_decision")
    assert not hasattr(repository, "record_gate_decision")
    assert not hasattr(later_service, "commit_deterministic_gate_batch")
    assert not hasattr(repository, "commit_deterministic_gate_batch")
    assert service.get_case(waiting.case_id) == waiting

    foreign_g6 = _gate(GateId.G6_TOOL_AUTHORITY, 10)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO gate_decisions (case_id, gate_id, decided_at, decision_json) "
            "VALUES (?, ?, ?, ?)",
            (
                waiting.case_id,
                foreign_g6.gate_id.value,
                foreign_g6.decided_at.isoformat(),
                foreign_g6.model_dump_json(by_alias=True),
            ),
        )
    with pytest.raises(WorkflowAtomicityError, match="G4/G5 history pairs"):
        repository.commit_analysis_workflow(command)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE gate_decisions SET decision_json = '{}' WHERE sequence = "
            "(SELECT MAX(sequence) FROM gate_decisions)"
        )
    with pytest.raises(PersistedDataIntegrityError, match="gate history"):
        repository.commit_analysis_workflow(command)


def test_quota_failure_is_atomic_nonretrying_and_never_creates_a_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "quota.db"
    service, repository, _prefix, current = _analysis_case(database_path)
    before_gates = repository.list_gate_decisions(current.case_id)
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        approved_evidence=_approved_evidence_for(current),
        g2_attempts=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )

    result = service.commit_terminal_provider_failure(command)

    assert result.case.state is CaseState.FAILED
    assert result.case.version == current.version + 1
    assert [event.event.kind for event in result.workflow_events] == [
        "operational_failure",
        "state",
    ]
    assert repository.list_gate_decisions(current.case_id) == before_gates
    usage = repository.list_provider_usage(current.case_id)
    assert len(usage) == 1
    assert usage[0].failure_category is not None
    assert usage[0].failure_category.value == "quota_exhausted"
    assert usage[0].status == "failed"


def test_terminal_provider_operation_must_match_current_workflow_state(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "operation-state.db")
    waiting = service.commit_analysis_workflow(_initial_command(current, prefix)).case
    command = TerminalProviderFailureCommand(
        case_id=waiting.case_id,
        expected_version=waiting.version,
        event=_operational_failure(),
        provider_events=(),
        approved_evidence=_approved_evidence_for(waiting),
        g2_attempts=(),
        claim_packet=None,
        occurred_at=waiting.updated_at + timedelta(seconds=1),
    )
    before = _counts(repository, waiting.case_id)

    with pytest.raises(CaseSnapshotValidationError, match="requires case state analyzing"):
        service.commit_terminal_provider_failure(command)

    assert service.get_case(waiting.case_id) == waiting
    assert _counts(repository, waiting.case_id) == before


def test_text_intake_rejects_terminal_transcription_failure_writer(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "text-transcription-failure-writer.db"
    _service, repository, _prefix, disclosed = _disclosed_text_case(database_path)
    before = _counts(repository, disclosed.case_id)

    with pytest.raises(WorkflowAtomicityError, match="audio intake"):
        repository.commit_terminal_provider_failure(
            TerminalProviderFailureCommand(
                case_id=disclosed.case_id,
                expected_version=disclosed.version,
                event=_transcription_operational_failure(),
                provider_events=(),
                approved_evidence=(),
                g2_attempts=(),
                claim_packet=None,
                occurred_at=disclosed.updated_at + timedelta(seconds=1),
            )
        )

    assert repository.get_case(disclosed.case_id) == disclosed
    assert _counts(repository, disclosed.case_id) == before
    assert SqliteCaseRepository(database_path).get_case(disclosed.case_id) == disclosed


def test_text_intake_rejects_forged_terminal_transcription_failure_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "text-transcription-failure-reopen.db"
    _service, repository, _prefix, disclosed = _disclosed_text_case(database_path)
    occurred_at = disclosed.updated_at + timedelta(seconds=1)
    with repository._write_connection() as connection:
        connection.execute(
            "UPDATE cases SET version = ?, state = ?, updated_at = ? WHERE case_id = ?",
            (
                disclosed.version + 1,
                CaseState.FAILED.value,
                occurred_at.isoformat(),
                disclosed.case_id,
            ),
        )
        repository._insert_redacted_workflow_event(
            connection,
            case_id=disclosed.case_id,
            event=_transcription_operational_failure(),
            actor=ActorType.SYSTEM,
            occurred_at=occurred_at,
        )
        _insert_state_transition(
            repository,
            connection,
            case_id=disclosed.case_id,
            from_state=CaseState.DISCLOSED,
            to_state=CaseState.FAILED,
            actor=ActorType.SYSTEM,
            occurred_at=occurred_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_audio_intake_terminal_transcription_failure_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audio-transcription-failure-positive.db"
    _service, repository, disclosed, _clock = _disclosed_audio_case(database_path)
    failed = repository.commit_terminal_provider_failure(
        TerminalProviderFailureCommand(
            case_id=disclosed.case_id,
            expected_version=disclosed.version,
            event=_transcription_operational_failure(),
            provider_events=(),
            approved_evidence=(),
            g2_attempts=(),
            claim_packet=None,
            occurred_at=disclosed.updated_at + timedelta(seconds=1),
        )
    ).case

    assert failed.state is CaseState.FAILED
    assert SqliteCaseRepository(database_path).get_case(failed.case_id) == failed


def test_audio_intake_rejects_direct_disclosed_to_analyzing_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audio-direct-analysis-reopen.db"
    _service, repository, disclosed, _clock = _disclosed_audio_case(database_path)
    occurred_at = disclosed.updated_at + timedelta(seconds=1)
    with repository._write_connection() as connection:
        connection.execute(
            "UPDATE cases SET version = ?, state = ?, updated_at = ? WHERE case_id = ?",
            (
                disclosed.version + 1,
                CaseState.ANALYZING.value,
                occurred_at.isoformat(),
                disclosed.case_id,
            ),
        )
        _insert_state_transition(
            repository,
            connection,
            case_id=disclosed.case_id,
            from_state=CaseState.DISCLOSED,
            to_state=CaseState.ANALYZING,
            actor=ActorType.SYSTEM,
            occurred_at=occurred_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "scenario",
    ("audio_failed", "text_success", "audio_success", "extraction_failed"),
)
@pytest.mark.parametrize("gate_id", (GateId.G0_INTAKE, GateId.G1_PRIVACY))
def test_every_intake_followup_rejects_duplicate_g0_or_g1_on_reopen(
    tmp_path: Path,
    scenario: str,
    gate_id: GateId,
) -> None:
    database_path = tmp_path / f"intake-{scenario}-duplicate-{gate_id.value}.db"
    repository, current = _canonical_intake_followup(database_path, scenario)
    _inject_duplicate_intake_gate(
        database_path,
        repository,
        case_id=current.case_id,
        gate_id=gate_id,
    )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("scenario", ("text_success", "audio_success"))
@pytest.mark.parametrize("tampering", ("swapped", "missing"))
def test_intake_followups_reject_swapped_or_missing_g0_g1_on_reopen(
    tmp_path: Path,
    scenario: str,
    tampering: str,
) -> None:
    database_path = tmp_path / f"intake-{scenario}-{tampering}.db"
    repository, current = _canonical_intake_followup(database_path, scenario)
    if tampering == "swapped":
        _swap_intake_gate_order(
            database_path,
            repository,
            case_id=current.case_id,
        )
    else:
        _remove_g1_intake_gate(
            database_path,
            repository,
            case_id=current.case_id,
        )

    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "scenario",
    ("audio_failed", "text_success", "audio_success", "extraction_failed"),
)
def test_legitimate_exact_g0_g1_intake_histories_reopen(
    tmp_path: Path,
    scenario: str,
) -> None:
    database_path = tmp_path / f"intake-{scenario}-positive.db"
    _repository, current = _canonical_intake_followup(database_path, scenario)

    assert SqliteCaseRepository(database_path).get_case(current.case_id) == current


def test_second_call_terminal_failure_persists_complete_retry_prefix(
    tmp_path: Path,
) -> None:
    service, repository, _prefix, current = _analysis_case(tmp_path / "terminal-retry.db")
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(
            "provider_unavailable",
            call_sequence=2,
            retry_attempt=1,
        ),
        provider_events=(
            ProviderWorkflowEmission(_provider_call(), NOW + timedelta(seconds=1)),
            ProviderWorkflowEmission(_retry(), NOW + timedelta(seconds=1)),
        ),
        approved_evidence=_approved_evidence_for(current),
        g2_attempts=(_invalid_g2_attempt(decided_at=NOW + timedelta(seconds=1)),),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(CaseSnapshotValidationError, match="single failed G2 attempt"):
        service.commit_terminal_provider_failure(replace(command, g2_attempts=()))

    result = service.commit_terminal_provider_failure(command)

    assert [event.event.kind for event in result.workflow_events] == [
        "provider_call",
        "retry",
        "operational_failure",
        "state",
    ]
    assert [item.status for item in repository.list_provider_usage(current.case_id)] == [
        "succeeded",
        "retry_scheduled",
        "failed",
    ]
    assert repository.list_gate_decisions(current.case_id)[-1].decision.gate_id is GateId.G1_PRIVACY


@pytest.mark.parametrize("fault", ["ledger", "projection"])
def test_terminal_failure_faults_roll_back_every_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    service, repository, _prefix, current = _analysis_case(tmp_path / f"terminal-{fault}.db")
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        approved_evidence=_approved_evidence_for(current),
        g2_attempts=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )
    before = _counts(repository, current.case_id)
    if fault == "ledger":

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected ledger failure")

        monkeypatch.setattr(repository, "_insert_provider_usage_projection", fail)
    else:

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(repository, "_insert_workflow_projection", fail)

    with pytest.raises(RuntimeError, match="injected"):
        service.commit_terminal_provider_failure(command)
    assert service.get_case(current.case_id) == current
    assert _counts(repository, current.case_id) == before


def test_generic_operational_append_and_analysis_append_cannot_split_boundaries(
    tmp_path: Path,
) -> None:
    _service, repository, _prefix, current = _analysis_case(tmp_path / "generic.db")
    for event in (_operational_failure(), _provider_call()):
        with pytest.raises(WorkflowAtomicityError, match="atomic"):
            repository.append_workflow_event(
                case_id=current.case_id,
                expected_case_version=current.version,
                event=event,
                actor=ActorType.SYSTEM,
                occurred_at=NOW + timedelta(seconds=1),
            )
    assert repository.list_provider_usage(current.case_id) == ()


def test_generic_success_events_cannot_forge_filling_or_verification_authority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "generic-success.db"
    service, repository, prefix, current = _analysis_case(database_path)
    filling = service.commit_analysis_workflow(
        _initial_command(current, prefix, target=CaseState.READY_TO_FILL)
    ).case
    tool = ToolCallWorkflowEvent.model_validate(
        {
            "kind": "tool_call",
            "invocationId": "invoke-generic-success",
            "sequence": 1,
            "tool": "fill_until_review",
            "status": "succeeded",
            "durationMs": 1,
        }
    )
    portal = PortalFillWorkflowEvent.model_validate(
        {
            "kind": "portal_fill",
            "variant": "A",
            "portalVersion": 1,
            "writtenFields": (RequiredClaimField.INCIDENT_DATE,),
        }
    )
    before = _counts(repository, filling.case_id)
    for event in (tool, portal):
        with pytest.raises(WorkflowAtomicityError, match="dedicated atomic"):
            repository.append_workflow_event(
                case_id=filling.case_id,
                expected_case_version=filling.version,
                event=event,
                actor=ActorType.AGENT,
                occurred_at=filling.updated_at,
            )
        assert _counts(repository, filling.case_id) == before

    verification = VerificationWorkflowEvent.model_validate(
        {
            "kind": "verification",
            "attemptNumber": 1,
            "status": "verified",
            "deterministicMatch": True,
            "modelReportedMismatch": False,
            "repairUsed": False,
            "final": True,
        }
    )
    before = _counts(repository, filling.case_id)
    with pytest.raises(WorkflowAtomicityError, match="dedicated atomic"):
        repository.append_workflow_event(
            case_id=filling.case_id,
            expected_case_version=filling.version,
            event=verification,
            actor=ActorType.SYSTEM,
            occurred_at=filling.updated_at,
        )
    assert _counts(repository, filling.case_id) == before


@pytest.mark.parametrize(
    "event_family",
    (
        "provider",
        "retry",
        "operational",
        "plan",
        "clarification",
        "tool",
        "portal",
        "verification",
    ),
)
def test_created_case_rejects_every_non_gate_workflow_event_family_on_reopen(
    tmp_path: Path,
    event_family: str,
) -> None:
    database_path = tmp_path / f"created-event-{event_family}.db"
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-created-event",
    )
    created = service.create_case()
    event: Any
    if event_family == "provider":
        event = _provider_call()
    elif event_family == "retry":
        event = _retry()
    elif event_family == "operational":
        event = _operational_failure()
    elif event_family == "plan":
        event = PlanStepWorkflowEvent.model_validate(
            {"kind": "plan_step", "sequence": 1, "tool": "fill_until_review"}
        )
    elif event_family == "clarification":
        event = _clarification_event(
            ClarificationStatus.REQUESTED,
            round_number=1,
        )
    elif event_family == "tool":
        event = ToolCallWorkflowEvent.model_validate(
            {
                "kind": "tool_call",
                "invocationId": "invocation-created-event",
                "sequence": 1,
                "tool": "fill_until_review",
                "status": "succeeded",
                "durationMs": 1,
            }
        )
    elif event_family == "portal":
        event = PortalFillWorkflowEvent.model_validate(
            {
                "kind": "portal_fill",
                "variant": "A",
                "portalVersion": 1,
                "writtenFields": (RequiredClaimField.INCIDENT_DATE,),
            }
        )
    else:
        event = VerificationWorkflowEvent.model_validate(
            {
                "kind": "verification",
                "attemptNumber": 1,
                "status": "verified",
                "deterministicMatch": True,
                "modelReportedMismatch": False,
                "repairUsed": False,
                "final": True,
            }
        )
    actor = (
        ActorType.SYSTEM
        if event_family in {"operational", "clarification", "verification"}
        else ActorType.AGENT
    )
    with repository._write_connection() as connection:
        repository._insert_redacted_workflow_event(
            connection,
            case_id=created.case_id,
            event=event,
            actor=actor,
            occurred_at=created.updated_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    ("target", "actor"),
    (
        (CaseState.ABANDONED, ActorType.SYSTEM),
        (CaseState.ANALYZING, ActorType.HUMAN),
    ),
)
def test_reopen_rejects_state_transitions_without_an_exact_canonical_writer(
    tmp_path: Path,
    target: CaseState,
    actor: ActorType,
) -> None:
    database_path = tmp_path / f"state-writer-{target.value}-{actor.value}.db"
    _service, repository, _prefix, disclosed = _disclosed_text_case(database_path)
    occurred_at = disclosed.updated_at + timedelta(seconds=1)
    with repository._write_connection() as connection:
        connection.execute(
            "UPDATE cases SET version = ?, state = ?, updated_at = ? WHERE case_id = ?",
            (
                disclosed.version + 1,
                target.value,
                occurred_at.isoformat(),
                disclosed.case_id,
            ),
        )
        _insert_state_transition(
            repository,
            connection,
            case_id=disclosed.case_id,
            from_state=CaseState.DISCLOSED,
            to_state=target,
            actor=actor,
            occurred_at=occurred_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("event_family", ("provider", "operational", "plan"))
def test_allowed_state_rejects_unconsumed_partial_event_boundaries(
    tmp_path: Path,
    event_family: str,
) -> None:
    database_path = tmp_path / f"partial-event-{event_family}.db"
    _service, repository, _prefix, analyzing = _analysis_case(database_path)
    event: Any
    actor = ActorType.AGENT
    if event_family == "provider":
        event = _provider_call()
    elif event_family == "operational":
        event = _operational_failure()
        actor = ActorType.SYSTEM
    else:
        event = PlanStepWorkflowEvent.model_validate(
            {"kind": "plan_step", "sequence": 1, "tool": "inspect_evidence"}
        )
    with repository._write_connection() as connection:
        repository._insert_redacted_workflow_event(
            connection,
            case_id=analyzing.case_id,
            event=event,
            actor=actor,
            occurred_at=analyzing.updated_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_provider_event_with_human_actor_fails_closed_on_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "provider-human-actor.db"
    _service, repository, _prefix, analyzing = _analysis_case(database_path)
    with repository._write_connection() as connection:
        repository._insert_redacted_workflow_event(
            connection,
            case_id=analyzing.case_id,
            event=_provider_call(),
            actor=ActorType.HUMAN,
            occurred_at=analyzing.updated_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "event_family",
    (
        "provider",
        "retry",
        "operational",
        "plan",
        "clarification",
        "tool",
        "portal",
        "verification",
    ),
)
def test_every_non_gate_event_family_requires_its_exact_actor_on_reopen(
    tmp_path: Path,
    event_family: str,
) -> None:
    database_path = tmp_path / f"wrong-actor-{event_family}.db"
    repository = SqliteCaseRepository(database_path)
    service = CaseService(
        repository,
        now=lambda: NOW,
        case_id_factory=lambda: "case-wrong-actor",
    )
    created = service.create_case()
    expected_actor = _expected_actor_for_event_family(event_family)
    wrong_actor = (
        ActorType.AGENT
        if expected_actor is ActorType.SYSTEM
        else ActorType.SYSTEM
    )
    with repository._write_connection() as connection:
        repository._insert_redacted_workflow_event(
            connection,
            case_id=created.case_id,
            event=_non_gate_event(event_family, suffix=f"wrong-actor-{event_family}"),
            actor=wrong_actor,
            occurred_at=created.updated_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize("ordering", ("clarification_before_plan", "gate_inside_retry"))
def test_reopen_rejects_workflow_stage_order_swaps(
    tmp_path: Path,
    ordering: str,
) -> None:
    database_path = tmp_path / f"workflow-order-{ordering}.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    result = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix, retry=ordering == "gate_inside_retry")
    )
    events = repository.list_workflow_events(result.case.case_id)
    if ordering == "clarification_before_plan":
        first = max(
            item.sequence
            for item in events
            if isinstance(item.envelope.event, PlanStepWorkflowEvent)
        )
        second = next(
            item.sequence
            for item in events
            if isinstance(item.envelope.event, ClarificationWorkflowEvent)
            and item.envelope.event.status is ClarificationStatus.REQUESTED
        )
    else:
        first = next(
            item.sequence
            for item in events
            if isinstance(item.envelope.event, RetryWorkflowEvent)
        )
        second = next(
            item.sequence
            for item in events
            if isinstance(item.envelope.event, GateWorkflowEvent)
            and item.envelope.event.decision.gate_id is GateId.G2_OUTPUT_CONTRACT
        )
    with sqlite3.connect(database_path) as connection:
        _swap_projection_sequences(connection, first, second)

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_reopen_rejects_timeout_as_the_canonical_extraction_retry_reason(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "retry-timeout.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    result = service.commit_analysis_workflow(
        _initial_command(
            analyzing,
            prefix,
            target=CaseState.READY_TO_FILL,
            retry=True,
        )
    )
    retry_projection = next(
        item
        for item in repository.list_workflow_events(result.case.case_id)
        if isinstance(item.envelope.event, RetryWorkflowEvent)
    )
    timeout = _timeout_retry()
    tampered = retry_projection.envelope.model_copy(update={"event": timeout})
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE workflow_events SET event_json = ? "
            "WHERE source_audit_sequence = ?",
            (
                tampered.model_dump_json(by_alias=True),
                retry_projection.sequence,
            ),
        )
        connection.execute(
            "UPDATE provider_usage_ledger SET failure_category = ? "
            "WHERE source_audit_sequence = ?",
            ("timeout", retry_projection.sequence),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_plan_events_must_share_their_packet_mutation_timestamp(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plan-mutation-time.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    shifted = waiting.updated_at - timedelta(seconds=1)
    plan_sequences = tuple(
        item.sequence
        for item in repository.list_workflow_events(waiting.case_id)
        if isinstance(item.envelope.event, PlanStepWorkflowEvent)
    )
    with sqlite3.connect(database_path) as connection:
        for sequence in plan_sequences:
            _rewrite_projection_timestamp(
                connection,
                sequence=sequence,
                occurred_at=shifted,
            )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_clarification_events_must_share_their_packet_mutation_timestamp(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "clarification-mutation-time.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    waiting = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix)
    ).case
    clarification_sequence = next(
        item.sequence
        for item in repository.list_workflow_events(waiting.case_id)
        if isinstance(item.envelope.event, ClarificationWorkflowEvent)
    )
    with sqlite3.connect(database_path) as connection:
        _rewrite_projection_timestamp(
            connection,
            sequence=clarification_sequence,
            occurred_at=waiting.updated_at - timedelta(seconds=1),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_operational_failure_must_be_time_aligned_directly_before_failed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "operational-failure-time.db"
    service, repository, _prefix, analyzing = _analysis_case(database_path)
    failed = service.commit_terminal_provider_failure(
        TerminalProviderFailureCommand(
            case_id=analyzing.case_id,
            expected_version=analyzing.version,
            event=_operational_failure(),
            provider_events=(),
            approved_evidence=_approved_evidence_for(analyzing),
            g2_attempts=(),
            claim_packet=None,
            occurred_at=analyzing.updated_at + timedelta(seconds=2),
        )
    ).case
    operational = next(
        item
        for item in repository.list_workflow_events(failed.case_id)
        if isinstance(item.envelope.event, OperationalFailureWorkflowEvent)
    )
    with sqlite3.connect(database_path) as connection:
        _rewrite_projection_timestamp(
            connection,
            sequence=operational.sequence,
            occurred_at=failed.updated_at - timedelta(seconds=1),
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "event_family",
    (
        "provider",
        "retry",
        "operational",
        "plan",
        "clarification",
        "tool",
        "portal",
        "verification",
    ),
)
def test_terminal_case_rejects_every_non_gate_workflow_event_family_on_reopen(
    tmp_path: Path,
    event_family: str,
) -> None:
    database_path = tmp_path / f"terminal-event-{event_family}.db"
    service, repository, _prefix, analyzing = _analysis_case(database_path)
    failed = service.commit_terminal_provider_failure(
        TerminalProviderFailureCommand(
            case_id=analyzing.case_id,
            expected_version=analyzing.version,
            event=_operational_failure(),
            provider_events=(),
            approved_evidence=_approved_evidence_for(analyzing),
            g2_attempts=(),
            claim_packet=None,
            occurred_at=analyzing.updated_at + timedelta(seconds=2),
        )
    ).case
    with repository._write_connection() as connection:
        repository._insert_redacted_workflow_event(
            connection,
            case_id=failed.case_id,
            event=_non_gate_event(
                event_family,
                suffix=f"terminal-{event_family}",
            ),
            actor=_expected_actor_for_event_family(event_family),
            occurred_at=failed.updated_at,
        )

    with pytest.raises(PersistedDataIntegrityError, match="canonical JSON"):
        SqliteCaseRepository(database_path)


def test_blocked_state_rejects_every_generic_authority_event(
    tmp_path: Path,
) -> None:
    service, repository, prefix, current = _analysis_case(tmp_path / "generic-blocked.db")
    command = _initial_command(current, prefix, target=CaseState.BLOCKED)
    blocked = service.commit_analysis_workflow(command).case
    tool = ToolCallWorkflowEvent.model_validate(
        {
            "kind": "tool_call",
            "invocationId": "invoke-after-block",
            "sequence": 1,
            "tool": "fill_until_review",
            "status": "succeeded",
            "durationMs": 1,
        }
    )
    events = (
        command.plan_steps[0],
        _provider_call(),
        _retry(),
        _operational_failure(),
        tool,
    )
    before = _counts(repository, blocked.case_id)
    for event in events:
        with pytest.raises(WorkflowAtomicityError, match="atomic command|Terminal"):
            repository.append_workflow_event(
                case_id=blocked.case_id,
                expected_case_version=blocked.version,
                event=event,
                actor=ActorType.AGENT,
                occurred_at=blocked.updated_at + timedelta(seconds=1),
            )
        assert _counts(repository, blocked.case_id) == before

    assert not hasattr(repository, "replace_snapshot")
    assert not hasattr(service, "replace_redacted_metadata")
    assert service.get_case(blocked.case_id) == blocked
    assert _counts(repository, blocked.case_id) == before


@pytest.mark.parametrize("invalid_shape", ("first_call_sequence", "timeout_retry"))
def test_analysis_writer_enforces_the_same_provider_batch_shape_as_replay(
    tmp_path: Path,
    invalid_shape: str,
) -> None:
    database_path = tmp_path / f"writer-provider-shape-{invalid_shape}.db"
    service, repository, prefix, analyzing = _analysis_case(database_path)
    command = _initial_command(
        analyzing,
        prefix,
        target=CaseState.READY_TO_FILL,
        retry=invalid_shape == "timeout_retry",
    )
    if invalid_shape == "first_call_sequence":
        command = replace(
            command,
            provider_events=(
                ProviderWorkflowEmission(
                    _provider_call(call_sequence=2),
                    command.provider_events[0].occurred_at,
                ),
            ),
        )
    else:
        command = replace(
            command,
            provider_events=(
                command.provider_events[0],
                ProviderWorkflowEmission(
                    _timeout_retry(),
                    command.provider_events[1].occurred_at,
                ),
                command.provider_events[2],
            ),
        )
    before = _counts(repository, analyzing.case_id)

    with pytest.raises(CaseSnapshotValidationError):
        service.commit_analysis_workflow(command)

    assert service.get_case(analyzing.case_id) == analyzing
    assert _counts(repository, analyzing.case_id) == before


def test_terminal_writer_requires_call_sequence_one_for_a_first_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "terminal-first-call-sequence.db"
    service, repository, _prefix, analyzing = _analysis_case(database_path)
    before = _counts(repository, analyzing.case_id)

    with pytest.raises(CaseSnapshotValidationError, match="callSequence one"):
        service.commit_terminal_provider_failure(
            TerminalProviderFailureCommand(
                case_id=analyzing.case_id,
                expected_version=analyzing.version,
                event=_operational_failure(call_sequence=2),
                provider_events=(),
                approved_evidence=_approved_evidence_for(analyzing),
                g2_attempts=(),
                claim_packet=None,
                occurred_at=analyzing.updated_at + timedelta(seconds=2),
            )
        )

    assert service.get_case(analyzing.case_id) == analyzing
    assert _counts(repository, analyzing.case_id) == before


def test_legitimate_text_analysis_boundary_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "positive-text-reopen.db"
    _service, _repository, _prefix, analyzing = _analysis_case(database_path)

    assert SqliteCaseRepository(database_path).get_case(analyzing.case_id) == analyzing


@pytest.mark.parametrize("confirmed", (False, True))
def test_legitimate_audio_transcript_boundaries_reopen(
    tmp_path: Path,
    confirmed: bool,
) -> None:
    database_path = tmp_path / f"positive-audio-reopen-{confirmed}.db"
    _service, _repository, current = _audio_transcript_case(
        database_path,
        confirmed=confirmed,
    )

    assert SqliteCaseRepository(database_path).get_case(current.case_id) == current


def test_legitimate_retry_boundary_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "positive-retry-reopen.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    ready = service.commit_analysis_workflow(
        _initial_command(
            analyzing,
            prefix,
            target=CaseState.READY_TO_FILL,
            retry=True,
        )
    ).case

    assert SqliteCaseRepository(database_path).get_case(ready.case_id) == ready


def test_legitimate_clarification_same_state_and_close_boundaries_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "positive-clarification-reopen.db"
    service, _repository, prefix, analyzing = _analysis_case(database_path)
    round_one = service.commit_analysis_workflow(
        _initial_command(
            analyzing,
            prefix,
            missing_fields=(
                RequiredClaimField.INCIDENT_TIME,
                RequiredClaimField.LOCATION,
            ),
        )
    ).case
    round_two = service.commit_analysis_workflow(
        _continuation_command(
            round_one,
            target=CaseState.AWAITING_CLARIFICATION,
        )
    ).case
    assert SqliteCaseRepository(database_path).get_case(round_two.case_id) == round_two

    ready = service.commit_analysis_workflow(
        _continuation_command(round_two, target=CaseState.READY_TO_FILL)
    ).case
    assert SqliteCaseRepository(database_path).get_case(ready.case_id) == ready


def test_legitimate_terminal_provider_failure_boundary_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "positive-terminal-reopen.db"
    service, _repository, _prefix, analyzing = _analysis_case(database_path)
    failed = service.commit_terminal_provider_failure(
        TerminalProviderFailureCommand(
            case_id=analyzing.case_id,
            expected_version=analyzing.version,
            event=_operational_failure(),
            provider_events=(),
            approved_evidence=_approved_evidence_for(analyzing),
            g2_attempts=(),
            claim_packet=None,
            occurred_at=analyzing.updated_at + timedelta(seconds=2),
        )
    ).case

    assert SqliteCaseRepository(database_path).get_case(failed.case_id) == failed


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_version": True},
        {"target": True},
        {"updated_at": "2026-07-14T12:00:10Z"},
        {"provider_events": [ProviderWorkflowEmission(_provider_call(), NOW)]},
        {"active_clarification": {"round": True}},
    ],
)
def test_command_shape_rejects_bool_and_invalid_types_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
) -> None:
    _service, repository, prefix, current = _analysis_case(tmp_path / "strict.db")
    command = _initial_command(current, prefix)

    def must_not_query(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("CAS lookup must not run for an invalid command shape")

    monkeypatch.setattr(repository, "_require_current", must_not_query)
    invalid = replace(command, **cast(Any, mutation))
    with pytest.raises(WorkflowAtomicityError):
        repository.commit_analysis_workflow(invalid)


@pytest.mark.parametrize("mutation", ["version", "event"])
def test_terminal_command_shape_rejects_invalid_values_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _service, repository, _prefix, current = _analysis_case(
        tmp_path / f"terminal-strict-{mutation}.db"
    )
    command = TerminalProviderFailureCommand(
        case_id=current.case_id,
        expected_version=current.version,
        event=_operational_failure(),
        provider_events=(),
        approved_evidence=_approved_evidence_for(current),
        g2_attempts=(),
        claim_packet=None,
        occurred_at=NOW + timedelta(seconds=2),
    )
    invalid = (
        replace(command, expected_version=True)
        if mutation == "version"
        else replace(
            command,
            event=command.event.model_copy(update={"retry_attempt": True}),
        )
    )

    def must_not_query(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("CAS lookup must not run for an invalid command shape")

    monkeypatch.setattr(repository, "_require_current", must_not_query)
    with pytest.raises(WorkflowAtomicityError):
        repository.commit_terminal_provider_failure(invalid)


def test_contract_rejects_boolean_clarification_round() -> None:
    with pytest.raises(ValidationError):
        ClarificationWorkflowEvent.model_validate(
            {
                "kind": "clarification",
                "round": True,
                "field": "incident_time",
                "status": "requested",
            }
        )
