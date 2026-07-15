"""Independent G8 rendered-value comparison and authority tests."""

from __future__ import annotations

import json
import unicodedata
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    CaseState,
    ClaimPacket,
    GateReasonCode,
    PortalState,
    PortalVariant,
    RenderedPortalSnapshot,
    RequiredClaimField,
    VerificationFieldStatus,
    VerificationState,
)
from claimdone_api.gates import (
    MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS,
    VerificationInputError,
    VerificationResult,
    evaluate_g8,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
RENDERED_AT = datetime(2026, 7, 15, 10, 0, 9, tzinfo=UTC)
SNAPSHOT_REQUESTED_AT = RENDERED_AT - timedelta(seconds=1)
SNAPSHOT_RECEIVED_AT = RENDERED_AT + timedelta(milliseconds=500)
VERIFIED_AT = RENDERED_AT + timedelta(seconds=1)
DECIDED_AT = VERIFIED_AT + timedelta(microseconds=1)
PORTAL_VERSION = 7


def _happy_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def _pending_verification(data: dict[str, Any]) -> dict[str, object]:
    return {
        "status": "pending",
        "deterministicMatch": None,
        "modelReportedMismatch": False,
        "fieldResults": [],
        "expectedAttachmentCount": 3,
        "expectedAttachmentIds": data["claim"]["attachments"],
        "actualAttachmentCount": None,
        "actualAttachmentIds": None,
        "reviewAllowed": False,
        "verifiedAt": None,
    }


def _verifying_packet(**claim_updates: object) -> ClaimPacket:
    data = _happy_data()
    data["state"] = CaseState.VERIFYING.value
    data["portalState"] = PortalState.REVIEW.value
    data["gateDecisions"] = data["gateDecisions"][:8]
    data["claim"].update(claim_updates)
    data["verification"] = _pending_verification(data)
    return ClaimPacket.model_validate(data)


def _portal_fields(packet: ClaimPacket) -> dict[str, object]:
    claim = packet.claim.model_dump(mode="json", by_alias=True)
    return {
        key: deepcopy(claim[key])
        for key in (
            "incidentDate",
            "incidentTime",
            "location",
            "claimantName",
            "policyReference",
            "vehicleRegistration",
            "counterpartyKnown",
            "narrative",
            "attachments",
        )
    }


def _snapshot(
    packet: ClaimPacket,
    *,
    field_updates: dict[str, object] | None = None,
    case_id: str | None = None,
    variant: PortalVariant = PortalVariant.A,
    version: int = PORTAL_VERSION,
    rendered_at: datetime = RENDERED_AT,
) -> RenderedPortalSnapshot:
    fields = _portal_fields(packet)
    fields.update(field_updates or {})
    return RenderedPortalSnapshot.model_validate(
        {
            "contractVersion": "4.0.0",
            "caseId": case_id or packet.case_id,
            "variant": variant.value,
            "state": PortalState.REVIEW.value,
            "version": version,
            "fields": fields,
            "renderedAt": rendered_at,
        }
    )


def _evaluate(
    packet: ClaimPacket,
    snapshot: RenderedPortalSnapshot,
    *,
    model_reported_mismatch: bool = False,
    snapshot_requested_at: datetime = SNAPSHOT_REQUESTED_AT,
    snapshot_received_at: datetime = SNAPSHOT_RECEIVED_AT,
    verified_at: datetime = VERIFIED_AT,
    decided_at: datetime = DECIDED_AT,
) -> VerificationResult:
    return evaluate_g8(
        packet,
        snapshot,
        expected_variant=PortalVariant.A,
        expected_portal_version=PORTAL_VERSION,
        snapshot_requested_at=snapshot_requested_at,
        snapshot_received_at=snapshot_received_at,
        model_reported_mismatch=model_reported_mismatch,
        verified_at=verified_at,
        decided_at=decided_at,
    )


def test_g8_success_builds_exact_canonical_report_sources_and_decision() -> None:
    packet = _verifying_packet()
    result = _evaluate(packet, _snapshot(packet))

    assert result.report.status is VerificationState.VERIFIED
    assert result.report.deterministic_match is True
    assert result.report.review_allowed is True
    assert result.report.verified_at == VERIFIED_AT
    assert result.report.expected_attachment_ids == packet.claim.attachments
    assert result.report.actual_attachment_ids == packet.claim.attachments
    assert result.decision.passed
    assert result.decision.reason_codes == ()
    assert result.decision.evidence_refs == (
        "prov-date",
        "prov-statement",
        "prov-image-2",
        "prov-image-1",
        "prov-image-3",
    )

    expected_fields = tuple(
        field for field in RequiredClaimField if field is not RequiredClaimField.ATTACHMENTS
    )
    assert tuple(item.field for item in result.report.field_results) == expected_fields
    provenance = {
        entry.field: entry.source_refs for entry in packet.claim.field_provenance
    }
    claim_values = packet.claim.model_dump(mode="json", by_alias=False)
    for item in result.report.field_results:
        assert item.expected == claim_values[item.field.value]
        assert item.actual == item.expected
        assert item.status is VerificationFieldStatus.MATCH
        assert item.source_refs == provenance[item.field]

    with pytest.raises(ValidationError, match="frozen"):
        result.report.review_allowed = False


def test_g8_normalizes_only_line_endings_outer_whitespace_and_iso_values() -> None:
    packet = _verifying_packet(narrative="Line one\nLine two")
    snapshot = _snapshot(
        packet,
        field_updates={
            "incidentDate": "2026-07-14",
            "incidentTime": " 14:30 ",
            "location": "\tBerlin \r\n",
            "narrative": " \r\nLine one\r\nLine two\r ",
        },
    )

    result = _evaluate(packet, snapshot)

    assert result.decision.passed
    assert all(
        item.status is VerificationFieldStatus.MATCH
        for item in result.report.field_results
    )
    incident_time = next(
        item
        for item in result.report.field_results
        if item.field is RequiredClaimField.INCIDENT_TIME
    )
    assert incident_time.actual == "14:30:00"


@pytest.mark.parametrize(
    ("field_updates", "field"),
    [
        ({"location": "berlin"}, RequiredClaimField.LOCATION),
        ({"location": "Ber  lin"}, RequiredClaimField.LOCATION),
        ({"incidentDate": "14.07.2026"}, RequiredClaimField.INCIDENT_DATE),
        ({"incidentTime": "14.30"}, RequiredClaimField.INCIDENT_TIME),
    ],
)
def test_g8_does_not_casefold_parse_locale_or_collapse_internal_whitespace(
    field_updates: dict[str, object],
    field: RequiredClaimField,
) -> None:
    packet = _verifying_packet()
    result = _evaluate(packet, _snapshot(packet, field_updates=field_updates))

    assert result.decision.reason_codes == (GateReasonCode.G8_FIELD_MISMATCH,)
    compared = next(item for item in result.report.field_results if item.field is field)
    assert compared.status is VerificationFieldStatus.MISMATCH


def test_g8_does_not_rewrite_unicode() -> None:
    composed = "Café"
    decomposed = unicodedata.normalize("NFD", composed)
    packet = _verifying_packet(location=composed)

    result = _evaluate(
        packet,
        _snapshot(packet, field_updates={"location": decomposed}),
    )

    assert result.decision.reason_codes == (GateReasonCode.G8_FIELD_MISMATCH,)
    location = next(
        item
        for item in result.report.field_results
        if item.field is RequiredClaimField.LOCATION
    )
    assert location.actual == decomposed
    assert location.actual != location.expected


def test_g8_missing_scalar_is_required_missing_not_field_mismatch() -> None:
    packet = _verifying_packet()
    result = _evaluate(
        packet,
        _snapshot(packet, field_updates={"location": " \r\n\t "}),
    )

    assert result.decision.reason_codes == (
        GateReasonCode.G8_REQUIRED_FIELD_MISSING,
    )
    location = next(
        item
        for item in result.report.field_results
        if item.field is RequiredClaimField.LOCATION
    )
    assert location.status is VerificationFieldStatus.MISSING
    assert location.actual is None


@pytest.mark.parametrize(
    "attachments",
    [
        ["wrong-1", "wrong-2", "wrong-3"],
        ["local-ref-2", "local-ref-1", "local-ref-3"],
        ["local-ref-1", "local-ref-2"],
        [],
    ],
)
def test_g8_compares_exact_raw_attachment_identity_and_order(
    attachments: list[str],
) -> None:
    packet = _verifying_packet()
    result = _evaluate(
        packet,
        _snapshot(packet, field_updates={"attachments": attachments}),
    )

    assert result.report.actual_attachment_count == len(attachments)
    assert result.report.actual_attachment_ids == tuple(attachments)
    assert result.decision.reason_codes == (
        GateReasonCode.G8_ATTACHMENT_MISMATCH,
    )
    assert GateReasonCode.G8_REQUIRED_FIELD_MISSING not in result.decision.reason_codes


def test_g8_model_signal_can_add_a_block_but_false_cannot_clear_mismatch() -> None:
    packet = _verifying_packet()
    model_block = _evaluate(
        packet,
        _snapshot(packet),
        model_reported_mismatch=True,
    )
    deterministic_block = _evaluate(
        packet,
        _snapshot(packet, field_updates={"location": "Other"}),
        model_reported_mismatch=False,
    )

    assert model_block.report.deterministic_match is True
    assert not model_block.report.review_allowed
    assert model_block.decision.reason_codes == (GateReasonCode.G8_MODEL_MISMATCH,)
    assert deterministic_block.report.deterministic_match is False
    assert not deterministic_block.report.review_allowed
    assert deterministic_block.decision.reason_codes == (
        GateReasonCode.G8_FIELD_MISMATCH,
    )


def test_g8_all_reason_types_are_complete_and_registry_ordered() -> None:
    packet = _verifying_packet()
    result = _evaluate(
        packet,
        _snapshot(
            packet,
            field_updates={
                "location": "Other",
                "claimantName": "",
                "attachments": ["local-ref-1", "local-ref-2"],
            },
        ),
        model_reported_mismatch=True,
    )

    assert result.decision.reason_codes == (
        GateReasonCode.G8_FIELD_MISMATCH,
        GateReasonCode.G8_ATTACHMENT_MISMATCH,
        GateReasonCode.G8_REQUIRED_FIELD_MISSING,
        GateReasonCode.G8_MODEL_MISMATCH,
    )
    assert result.report.status is VerificationState.MISMATCH


@pytest.mark.parametrize("binding", ["case", "variant", "version", "state"])
def test_g8_foreign_or_stale_identity_fails_closed_without_a_report(
    binding: str,
) -> None:
    packet = _verifying_packet()
    snapshot = _snapshot(packet)
    expected_variant = PortalVariant.A
    expected_version = PORTAL_VERSION
    if binding == "case":
        snapshot = snapshot.model_copy(update={"case_id": "foreign-case"})
    elif binding == "variant":
        snapshot = snapshot.model_copy(update={"variant": PortalVariant.B})
    elif binding == "version":
        expected_version += 1
    else:
        packet = packet.model_copy(update={"state": CaseState.REVIEW})

    with pytest.raises(VerificationInputError, match="G8 verification input is invalid"):
        evaluate_g8(
            packet,
            snapshot,
            expected_variant=expected_variant,
            expected_portal_version=expected_version,
            snapshot_requested_at=SNAPSHOT_REQUESTED_AT,
            snapshot_received_at=SNAPSHOT_RECEIVED_AT,
            model_reported_mismatch=False,
            verified_at=VERIFIED_AT,
            decided_at=DECIDED_AT,
        )


def test_g8_revalidates_safe_model_copies_and_keeps_errors_content_free() -> None:
    packet = _verifying_packet()
    snapshot = _snapshot(packet)
    forged_fields = snapshot.fields.model_copy(
        update={"attachments": ("private-ref", "private-ref", "other-ref")}
    )
    forged = snapshot.model_copy(
        update={"case_id": "private-case", "fields": forged_fields}
    )

    with pytest.raises(VerificationInputError) as raised:
        _evaluate(packet, forged)

    assert str(raised.value) == "G8 verification input is invalid"
    assert "private" not in repr(raised.value)


@pytest.mark.parametrize(
    ("requested_at", "rendered_at", "received_at", "verified_at"),
    [
        (
            RENDERED_AT,
            RENDERED_AT - timedelta(microseconds=1),
            SNAPSHOT_RECEIVED_AT,
            VERIFIED_AT,
        ),
        (
            SNAPSHOT_REQUESTED_AT,
            SNAPSHOT_RECEIVED_AT + timedelta(microseconds=1),
            SNAPSHOT_RECEIVED_AT,
            VERIFIED_AT,
        ),
        (
            SNAPSHOT_RECEIVED_AT,
            SNAPSHOT_RECEIVED_AT,
            SNAPSHOT_REQUESTED_AT,
            VERIFIED_AT,
        ),
        (
            RENDERED_AT - timedelta(seconds=6),
            RENDERED_AT - timedelta(seconds=5),
            RENDERED_AT,
            VERIFIED_AT,
        ),
        (
            SNAPSHOT_REQUESTED_AT,
            RENDERED_AT,
            VERIFIED_AT + timedelta(microseconds=1),
            VERIFIED_AT,
        ),
    ],
)
def test_g8_same_version_stale_future_order_and_duration_fail_closed(
    requested_at: datetime,
    rendered_at: datetime,
    received_at: datetime,
    verified_at: datetime,
) -> None:
    packet = _verifying_packet()
    same_version_snapshot = _snapshot(packet, rendered_at=rendered_at)

    with pytest.raises(VerificationInputError):
        _evaluate(
            packet,
            same_version_snapshot,
            snapshot_requested_at=requested_at,
            snapshot_received_at=received_at,
            verified_at=verified_at,
            decided_at=verified_at,
        )


def test_g8_accepts_exact_five_second_snapshot_round_trip_boundary() -> None:
    packet = _verifying_packet()
    requested_at = RENDERED_AT - timedelta(seconds=2)
    received_at = requested_at + timedelta(
        seconds=MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS
    )

    result = _evaluate(
        packet,
        _snapshot(packet),
        snapshot_requested_at=requested_at,
        snapshot_received_at=received_at,
        verified_at=received_at,
        decided_at=received_at,
    )

    assert result.decision.passed
    assert MAX_G8_SNAPSHOT_ROUND_TRIP_SECONDS == 5.0


def test_g8_rejects_non_boolean_model_signal_and_bad_timestamps() -> None:
    packet = _verifying_packet()
    with pytest.raises(VerificationInputError):
        evaluate_g8(
            packet,
            _snapshot(packet),
            expected_variant=PortalVariant.A,
            expected_portal_version=PORTAL_VERSION,
            snapshot_requested_at=SNAPSHOT_REQUESTED_AT,
            snapshot_received_at=SNAPSHOT_RECEIVED_AT,
            model_reported_mismatch=cast(bool, 1),
            verified_at=VERIFIED_AT,
            decided_at=DECIDED_AT,
        )
    with pytest.raises(VerificationInputError):
        evaluate_g8(
            packet,
            _snapshot(packet),
            expected_variant=PortalVariant.A,
            expected_portal_version=PORTAL_VERSION,
            snapshot_requested_at=SNAPSHOT_REQUESTED_AT,
            snapshot_received_at=SNAPSHOT_RECEIVED_AT,
            model_reported_mismatch=False,
            verified_at=VERIFIED_AT,
            decided_at=VERIFIED_AT - timedelta(microseconds=1),
        )
    with pytest.raises(VerificationInputError):
        evaluate_g8(
            packet,
            _snapshot(packet),
            expected_variant=PortalVariant.A,
            expected_portal_version=PORTAL_VERSION,
            snapshot_requested_at=cast(datetime, SNAPSHOT_REQUESTED_AT.date()),
            snapshot_received_at=SNAPSHOT_RECEIVED_AT,
            model_reported_mismatch=False,
            verified_at=VERIFIED_AT,
            decided_at=DECIDED_AT,
        )
