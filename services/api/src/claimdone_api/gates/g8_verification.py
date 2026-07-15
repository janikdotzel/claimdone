"""Independent deterministic rendered-value verification for G8."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from pydantic import ValidationError

from claimdone_api.contracts import (
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    GateReasonCode,
    PortalState,
    PortalVariant,
    RenderedPortalSnapshot,
    RequiredClaimField,
    VerificationFieldResult,
    VerificationFieldStatus,
    VerificationReport,
    VerificationState,
)

from .registry import make_gate_decision

_PORTAL_FIELD_BY_REQUIRED = {
    RequiredClaimField.INCIDENT_DATE: "incident_date",
    RequiredClaimField.INCIDENT_TIME: "incident_time",
    RequiredClaimField.LOCATION: "location",
    RequiredClaimField.CLAIMANT_NAME: "claimant_name",
    RequiredClaimField.POLICY_REFERENCE: "policy_reference",
    RequiredClaimField.VEHICLE_REGISTRATION: "vehicle_registration",
    RequiredClaimField.COUNTERPARTY_KNOWN: "counterparty_known",
    RequiredClaimField.NARRATIVE: "narrative",
}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_PATTERN = re.compile(
    r"^\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$"
)
MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS = 5.0


class VerificationInputError(ValueError):
    """A content-free failure for stale, forged, or foreign G8 inputs."""

    def __init__(self) -> None:
        super().__init__("G8 verification input is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VerificationResult:
    """Canonical immutable report and its independently derived G8 decision."""

    report: VerificationReport
    decision: GateDecision

    def __post_init__(self) -> None:
        if self.decision.gate_id is not GateId.G8_VERIFICATION:
            raise ValueError("VerificationResult requires a G8 decision")
        if self.decision.passed is not self.report.review_allowed:
            raise ValueError("G8 decision and report authority must agree")


def evaluate_g8(
    packet: ClaimPacket,
    snapshot: RenderedPortalSnapshot,
    *,
    expected_variant: PortalVariant,
    expected_portal_version: int,
    snapshot_requested_at: datetime,
    snapshot_received_at: datetime,
    model_reported_mismatch: bool,
    verified_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> VerificationResult:
    """Compare a fresh bound snapshot; the model signal may only add a block."""

    timestamp = verified_at or datetime.now(UTC)
    decision_timestamp = decided_at or timestamp
    canonical_packet, canonical_snapshot = _validate_inputs(
        packet,
        snapshot,
        expected_variant=expected_variant,
        expected_portal_version=expected_portal_version,
        snapshot_requested_at=snapshot_requested_at,
        snapshot_received_at=snapshot_received_at,
        model_reported_mismatch=model_reported_mismatch,
        verified_at=timestamp,
        decided_at=decision_timestamp,
    )

    claim_values = canonical_packet.claim.model_dump(mode="json", by_alias=False)
    portal_values = canonical_snapshot.fields.model_dump(mode="json", by_alias=False)
    provenance_by_field = {
        entry.field: entry.source_refs
        for entry in canonical_packet.claim.field_provenance
    }

    field_results: list[VerificationFieldResult] = []
    reasons: set[GateReasonCode] = set()
    evidence_refs: list[str] = []
    for field, portal_field in _PORTAL_FIELD_BY_REQUIRED.items():
        expected = claim_values[field.value]
        actual = portal_values[portal_field]
        if type(expected) is not str or type(actual) is not str:
            raise VerificationInputError from None
        source_refs = provenance_by_field.get(field)
        if not source_refs:
            raise VerificationInputError from None
        evidence_refs.extend(source_refs)

        normalized_actual = _normalize_scalar(field, actual)
        if normalized_actual is None:
            status = VerificationFieldStatus.MISSING
            reported_actual: str | None = None
            reasons.add(GateReasonCode.G8_REQUIRED_FIELD_MISSING)
        elif _scalar_values_match(field, expected, normalized_actual):
            status = VerificationFieldStatus.MATCH
            reported_actual = expected
        else:
            status = VerificationFieldStatus.MISMATCH
            reported_actual = normalized_actual
            reasons.add(GateReasonCode.G8_FIELD_MISMATCH)

        field_results.append(
            VerificationFieldResult.model_validate(
                {
                    "field": field.value,
                    "expected": expected,
                    "actual": reported_actual,
                    "status": status.value,
                    "sourceRefs": list(source_refs),
                }
            )
        )

    expected_attachments = canonical_packet.claim.attachments
    actual_attachments = canonical_snapshot.fields.attachments
    attachment_source_refs = provenance_by_field.get(RequiredClaimField.ATTACHMENTS)
    if not attachment_source_refs:
        raise VerificationInputError from None
    evidence_refs.extend(attachment_source_refs)
    if actual_attachments != expected_attachments:
        reasons.add(GateReasonCode.G8_ATTACHMENT_MISMATCH)

    deterministic_match = not reasons
    review_allowed = deterministic_match and not model_reported_mismatch
    report = VerificationReport.model_validate(
        {
            "status": (
                VerificationState.VERIFIED.value
                if review_allowed
                else VerificationState.MISMATCH.value
            ),
            "deterministicMatch": deterministic_match,
            "modelReportedMismatch": model_reported_mismatch,
            "fieldResults": [
                result.model_dump(mode="json", by_alias=True)
                for result in field_results
            ],
            "expectedAttachmentCount": len(expected_attachments),
            "expectedAttachmentIds": list(expected_attachments),
            "actualAttachmentCount": len(actual_attachments),
            "actualAttachmentIds": list(actual_attachments),
            "reviewAllowed": review_allowed,
            "verifiedAt": timestamp,
        }
    )
    decision = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=tuple(reasons),
        model_blocked=model_reported_mismatch,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        decided_at=decision_timestamp,
    )
    return VerificationResult(report=report, decision=decision)


def _validate_inputs(
    packet: ClaimPacket,
    snapshot: RenderedPortalSnapshot,
    *,
    expected_variant: PortalVariant,
    expected_portal_version: int,
    snapshot_requested_at: datetime,
    snapshot_received_at: datetime,
    model_reported_mismatch: bool,
    verified_at: datetime,
    decided_at: datetime,
) -> tuple[ClaimPacket, RenderedPortalSnapshot]:
    if (
        not isinstance(packet, ClaimPacket)
        or not isinstance(snapshot, RenderedPortalSnapshot)
        or not isinstance(expected_variant, PortalVariant)
        or type(expected_portal_version) is not int
        or expected_portal_version < 1
        or type(snapshot_requested_at) is not datetime
        or snapshot_requested_at.utcoffset() is None
        or type(snapshot_received_at) is not datetime
        or snapshot_received_at.utcoffset() is None
        or type(model_reported_mismatch) is not bool
        or type(verified_at) is not datetime
        or verified_at.utcoffset() is None
        or type(decided_at) is not datetime
        or decided_at.utcoffset() is None
        or decided_at < verified_at
    ):
        raise VerificationInputError from None
    try:
        canonical_packet = ClaimPacket.model_validate(
            packet.model_dump(mode="json", by_alias=True)
        )
        canonical_snapshot = RenderedPortalSnapshot.model_validate(
            snapshot.model_dump(mode="json", by_alias=True)
        )
    except (ValidationError, TypeError, ValueError):
        raise VerificationInputError from None

    if (
        canonical_packet.state is not CaseState.VERIFYING
        or canonical_packet.portal_state is not PortalState.REVIEW
        or canonical_snapshot.case_id != canonical_packet.case_id
        or canonical_snapshot.variant is not expected_variant
        or canonical_snapshot.version != expected_portal_version
        or not (
            snapshot_requested_at
            <= canonical_snapshot.rendered_at
            <= snapshot_received_at
            <= verified_at
        )
        or snapshot_received_at - snapshot_requested_at
        > timedelta(seconds=MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS)
    ):
        raise VerificationInputError from None
    if any(
        claim_value is None
        for field, claim_value in canonical_packet.claim.model_dump(
            mode="json", by_alias=False
        ).items()
        if field in _PORTAL_FIELD_BY_REQUIRED.values()
    ):
        raise VerificationInputError from None
    return canonical_packet, canonical_snapshot


def _normalize_scalar(field: RequiredClaimField, value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    if field is RequiredClaimField.INCIDENT_DATE:
        return _canonical_date(normalized)
    if field is RequiredClaimField.INCIDENT_TIME:
        return _canonical_time(normalized)
    return normalized


def _scalar_values_match(
    field: RequiredClaimField,
    expected: str,
    actual: str,
) -> bool:
    normalized_expected = _normalize_scalar(field, expected)
    if normalized_expected is None:
        raise VerificationInputError from None
    if field is RequiredClaimField.INCIDENT_DATE:
        return _parsed_date(normalized_expected) == _parsed_date(actual)
    if field is RequiredClaimField.INCIDENT_TIME:
        return _parsed_time(normalized_expected) == _parsed_time(actual)
    return normalized_expected == actual


def _canonical_date(value: str) -> str:
    parsed = _parsed_date(value)
    return parsed.isoformat() if parsed is not None else value


def _canonical_time(value: str) -> str:
    parsed = _parsed_time(value)
    if parsed is None:
        return value
    normalized = parsed.isoformat()
    return normalized.removesuffix("+00:00") + "Z" if normalized.endswith("+00:00") else normalized


def _parsed_date(value: str) -> date | None:
    if _DATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parsed_time(value: str) -> time | None:
    if _TIME_PATTERN.fullmatch(value) is None:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None
