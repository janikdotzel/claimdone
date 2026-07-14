"""Deterministic synthetic extraction used by INT-001; no model is called."""

from dataclasses import dataclass
from hashlib import sha256

from claimdone_api.contracts import (
    CounterpartyKnown,
    EvidenceFact,
    EvidenceField,
    EvidenceItem,
    EvidenceKind,
    FactStatus,
    FieldProvenance,
    ProvenanceRef,
    RequiredClaimField,
)
from claimdone_api.gates import ModelExtraction
from claimdone_api.media import PreparedMedia, StoredAssetRef

_FIXTURE_TEXT = (
    "Synthetic ClaimDone fixture v1. incident_date=2026-07-14; "
    "location=Demo Street 1, Berlin; claimant_name=Demo Claimant; "
    "policy_reference=DEMO-POLICY-001; vehicle_registration=DEMO-CD-1; "
    "counterparty_known=yes; narrative=A staged second vehicle contacted the rear "
    "of the demo vehicle in Berlin."
)
_FIXTURE_SHA256 = sha256(_FIXTURE_TEXT.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StatementSource:
    local_ref: str
    sha256: str
    text: str
    kind: EvidenceKind


def deterministic_extraction(
    prepared: PreparedMedia,
    statement: StatementSource,
    *,
    incident_time: str | None,
    clarification_ref: StoredAssetRef | None = None,
) -> ModelExtraction:
    """Return one fixed demo claim grounded in approved local evidence."""

    image_evidence = tuple(
        EvidenceItem.model_validate(
            {
                "evidenceId": f"evidence-image-{index}",
                "kind": "image",
                "localRef": image.local_ref,
                "mediaType": image.media_type,
                "sha256": image.sha256,
                "text": None,
                "modelCopyApproved": True,
            }
        )
        for index, image in enumerate(prepared.model_images, start=1)
    )
    statement_evidence = EvidenceItem.model_validate(
        {
            "evidenceId": "evidence-statement",
            "kind": statement.kind,
            "localRef": statement.local_ref,
            "mediaType": "text/plain",
            "sha256": statement.sha256,
            "text": statement.text,
            "modelCopyApproved": True,
        }
    )
    fixture_evidence = EvidenceItem.model_validate(
        {
            "evidenceId": "evidence-synthetic-fixture",
            "kind": "user_statement",
            "localRef": "fixture-claim-v1",
            "mediaType": "text/plain",
            "sha256": _FIXTURE_SHA256,
            "text": _FIXTURE_TEXT,
            "modelCopyApproved": True,
        }
    )
    clarification_evidence: tuple[EvidenceItem, ...] = ()
    if incident_time is not None:
        if clarification_ref is None:
            raise ValueError("A completed incident time requires stored clarification evidence")
        clarification_evidence = (
            EvidenceItem.model_validate(
                {
                    "evidenceId": "evidence-clarification-time",
                    "kind": "clarification",
                    "localRef": clarification_ref.file_id,
                    "mediaType": "text/plain",
                    "sha256": clarification_ref.sha256,
                    "text": incident_time,
                    "modelCopyApproved": True,
                }
            ),
        )
    evidence = (
        *image_evidence,
        statement_evidence,
        fixture_evidence,
        *clarification_evidence,
    )

    image_provenance = tuple(
        ProvenanceRef.model_validate(
            {
                "provenanceId": f"prov-image-{index}",
                "evidenceId": f"evidence-image-{index}",
                "locator": f"approved image {index}",
                "userConfirmed": False,
            }
        )
        for index in range(1, 4)
    )
    statement_provenance = ProvenanceRef.model_validate(
        {
            "provenanceId": "prov-statement",
            "evidenceId": "evidence-statement",
            "locator": "staged statement",
            "userConfirmed": True,
        }
    )
    fixture_provenance = ProvenanceRef.model_validate(
        {
            "provenanceId": "prov-synthetic-fixture",
            "evidenceId": "evidence-synthetic-fixture",
            "locator": "versioned server-owned synthetic demo fixture",
            "userConfirmed": False,
        }
    )
    clarification_provenance: tuple[ProvenanceRef, ...] = ()
    if incident_time is not None:
        clarification_provenance = (
            ProvenanceRef.model_validate(
                {
                    "provenanceId": "prov-clarification-time",
                    "evidenceId": "evidence-clarification-time",
                    "locator": "clarification answer",
                    "userConfirmed": True,
                }
            ),
        )
    provenance = (
        *image_provenance,
        statement_provenance,
        fixture_provenance,
        *clarification_provenance,
    )

    claim_values: dict[RequiredClaimField, object] = {
        RequiredClaimField.INCIDENT_DATE: "2026-07-14",
        RequiredClaimField.INCIDENT_TIME: incident_time,
        RequiredClaimField.LOCATION: "Demo Street 1, Berlin",
        RequiredClaimField.CLAIMANT_NAME: "Demo Claimant",
        RequiredClaimField.POLICY_REFERENCE: "DEMO-POLICY-001",
        RequiredClaimField.VEHICLE_REGISTRATION: "DEMO-CD-1",
        RequiredClaimField.COUNTERPARTY_KNOWN: CounterpartyKnown.YES.value,
        RequiredClaimField.NARRATIVE: (
            "A staged second vehicle contacted the rear of the demo vehicle in Berlin."
        ),
    }
    provenance_by_field: dict[RequiredClaimField, tuple[str, ...]] = {
        field: (
            ("prov-clarification-time",)
            if field is RequiredClaimField.INCIDENT_TIME
            else ("prov-synthetic-fixture",)
        )
        for field, value in claim_values.items()
        if value is not None
    }
    provenance_by_field[RequiredClaimField.ATTACHMENTS] = tuple(
        f"prov-image-{index}" for index in range(1, 4)
    )
    field_provenance = tuple(
        FieldProvenance.model_validate(
            {
                "field": field.value,
                "sourceRefs": sources,
            }
        )
        for field, sources in provenance_by_field.items()
    )
    missing = (RequiredClaimField.INCIDENT_TIME.value,) if incident_time is None else ()
    claim = {
        "incidentDate": claim_values[RequiredClaimField.INCIDENT_DATE],
        "incidentTime": incident_time,
        "location": claim_values[RequiredClaimField.LOCATION],
        "claimantName": claim_values[RequiredClaimField.CLAIMANT_NAME],
        "policyReference": claim_values[RequiredClaimField.POLICY_REFERENCE],
        "vehicleRegistration": claim_values[RequiredClaimField.VEHICLE_REGISTRATION],
        "counterpartyKnown": claim_values[RequiredClaimField.COUNTERPARTY_KNOWN],
        "narrative": claim_values[RequiredClaimField.NARRATIVE],
        "attachments": tuple(image.local_ref for image in prepared.model_images),
        "missingRequiredFields": missing,
        "fieldProvenance": field_provenance,
    }

    evidence_field = {
        RequiredClaimField.INCIDENT_DATE: EvidenceField.INCIDENT_DATE,
        RequiredClaimField.INCIDENT_TIME: EvidenceField.INCIDENT_TIME,
        RequiredClaimField.LOCATION: EvidenceField.LOCATION,
        RequiredClaimField.CLAIMANT_NAME: EvidenceField.CLAIMANT_NAME,
        RequiredClaimField.POLICY_REFERENCE: EvidenceField.POLICY_REFERENCE,
        RequiredClaimField.VEHICLE_REGISTRATION: EvidenceField.VEHICLE_REGISTRATION,
        RequiredClaimField.COUNTERPARTY_KNOWN: EvidenceField.COUNTERPARTY_KNOWN,
        RequiredClaimField.NARRATIVE: EvidenceField.NARRATIVE,
    }
    facts = tuple(
        EvidenceFact.model_validate(
            {
                "factId": f"fact-{field.value.replace('_', '-')}",
                "field": evidence_field[field].value,
                "value": value,
                "status": FactStatus.USER_STATED.value,
                "sourceRefs": provenance_by_field[field],
                "confidence": None,
            }
        )
        for field, value in claim_values.items()
        if value is not None
    )
    return ModelExtraction.model_validate(
        {
            "contractVersion": "1.0.0",
            "evidence": evidence,
            "provenance": provenance,
            "facts": facts,
            "claim": claim,
        }
    )
