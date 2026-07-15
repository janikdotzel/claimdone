"""Deterministic tool-authority and full-payload portal-write tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    CaseState,
    ClaimPacket,
    GateReasonCode,
    PortalState,
    PortalVariant,
)
from claimdone_api.gates import (
    PortalWriteInputError,
    ToolAuthorityContext,
    canonical_portal_case_url,
    evaluate_g6,
    evaluate_g7,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
DECIDED_AT = datetime(2026, 7, 15, 10, tzinfo=UTC)


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


def _filling_packet() -> ClaimPacket:
    data = _happy_data()
    data["state"] = CaseState.FILLING.value
    data["portalState"] = PortalState.DRAFT.value
    data["gateDecisions"] = data["gateDecisions"][:6]
    data["verification"] = _pending_verification(data)
    return ClaimPacket.model_validate(data)


def _invocation(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contractVersion": CONTRACT_VERSION,
        "invocationId": "invocation-fill-1",
        "sequence": 5,
        "tool": "fill_until_review",
        "arguments": {},
    }
    payload.update(updates)
    return payload


def _context(packet: ClaimPacket | None = None, **updates: object) -> ToolAuthorityContext:
    bound_packet = packet or _filling_packet()
    values: dict[str, object] = {
        "packet": bound_packet,
        "case_state": CaseState.FILLING,
        "portal_variant": PortalVariant.A,
        "current_url": canonical_portal_case_url(bound_packet.case_id, PortalVariant.A),
        "action": "click",
        "proposed_action_number": 1,
        "elapsed_seconds": 1.0,
    }
    values.update(updates)
    return ToolAuthorityContext(**values)  # type: ignore[arg-type]


def _portal_payload(packet: ClaimPacket | None = None) -> dict[str, object]:
    bound_packet = packet or _filling_packet()
    claim = bound_packet.claim.model_dump(mode="json", by_alias=True)
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


def test_g6_accepts_only_exact_planned_fill_invocation_and_is_immutable() -> None:
    context = _context(proposed_action_number=40, elapsed_seconds=90.0)
    result = evaluate_g6(_invocation(), context=context, decided_at=DECIDED_AT)

    assert result.decision.passed
    assert result.invocation is not None
    assert result.invocation.sequence == 5
    assert result.decision.reason_codes == ()
    assert "sandbox" not in repr(context)
    with pytest.raises(FrozenInstanceError):
        result.decision = result.decision  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tool", "reasons"),
    [
        (
            "approve_claim",
            (
                GateReasonCode.G6_TOOL_UNKNOWN,
                GateReasonCode.G6_FORBIDDEN_ACTION,
            ),
        ),
        ("inspect_form", (GateReasonCode.G6_FORBIDDEN_ACTION,)),
    ],
)
def test_g6_blocks_unknown_and_known_but_unauthorized_tools(
    tool: str,
    reasons: tuple[GateReasonCode, ...],
) -> None:
    result = evaluate_g6(
        _invocation(tool=tool),
        context=_context(),
        decided_at=DECIDED_AT,
    )

    assert result.invocation is None
    assert result.decision.reason_codes == reasons


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"caseId": "case-foreign"}),
        lambda payload: payload.update({"arguments": {"url": "local"}}),
        lambda payload: payload.update({"sequence": 4}),
        lambda payload: payload.update({"sequence": True}),
        lambda payload: payload.pop("arguments"),
        lambda payload: payload.update({"contractVersion": "4.0.0 "}),
    ],
)
def test_g6_rejects_extras_manipulated_arguments_and_plan_mismatch(
    mutate: Any,
) -> None:
    payload = _invocation()
    mutate(payload)

    result = evaluate_g6(payload, context=_context(), decided_at=DECIDED_AT)

    assert result.decision.reason_codes == (GateReasonCode.G6_ARGUMENTS_INVALID,)


@pytest.mark.parametrize(
    "context",
    [
        _context(case_state=CaseState.READY_TO_FILL),
        _context(
            packet=_filling_packet().model_copy(
                update={"state": CaseState.VERIFYING, "portal_state": PortalState.REVIEW}
            )
        ),
    ],
)
def test_g6_requires_filling_draft_state(context: ToolAuthorityContext) -> None:
    result = evaluate_g6(_invocation(), context=context, decided_at=DECIDED_AT)

    assert result.decision.reason_codes == (GateReasonCode.G6_STATE_INVALID,)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000/sandbox/A/cases/case-happy-001",
        "https://127.0.0.1:3000/sandbox/A/cases/case-happy-001",
        "http://127.0.0.1:3000/sandbox/B/cases/case-happy-001",
        "http://127.0.0.1:3000/sandbox/A/cases/case-foreign",
        "http://127.0.0.1:3000/sandbox/A/cases/case-happy-001/receipt",
        "http://127.0.0.1:3000/sandbox/A/cases/case-happy-001?review=true",
    ],
)
def test_g6_requires_exact_origin_path_case_and_variant(url: str) -> None:
    result = evaluate_g6(
        _invocation(),
        context=_context(current_url=url),
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G6_URL_NOT_ALLOWED,)


@pytest.mark.parametrize(
    "action",
    ["approve", "submit", "receipt", "reset", "delete", "navigate", "CLICK"],
)
def test_g6_blocks_forbidden_or_nonclosed_actions(action: str) -> None:
    result = evaluate_g6(
        _invocation(),
        context=_context(action=action),
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G6_FORBIDDEN_ACTION,)


@pytest.mark.parametrize(
    ("proposed_action_number", "elapsed_seconds", "passed"),
    [
        (40, 90.0, True),
        (0, 1.0, False),
        (41, 90.0, False),
        (40, 90.000001, False),
        (True, 1.0, False),
        (1, float("inf"), False),
    ],
)
def test_g6_action_number_and_time_limits_are_inclusive_and_strict(
    proposed_action_number: object,
    elapsed_seconds: object,
    passed: bool,
) -> None:
    result = evaluate_g6(
        _invocation(),
        context=_context(
            proposed_action_number=proposed_action_number,
            elapsed_seconds=elapsed_seconds,
        ),
        decided_at=DECIDED_AT,
    )

    assert result.decision.passed is passed
    assert (GateReasonCode.G6_LIMIT_EXCEEDED in result.decision.reason_codes) is not passed


def test_g7_accepts_one_exact_complete_packet_derived_write() -> None:
    packet = _filling_packet()
    result = evaluate_g7(
        _portal_payload(packet),
        packet=packet,
        case_state=CaseState.FILLING,
        portal_state=PortalState.DRAFT,
        decided_at=DECIDED_AT,
    )

    assert result.decision.passed
    assert result.fields is not None
    assert result.fields.attachments == packet.claim.attachments
    assert result.decision.evidence_refs == (
        "prov-date",
        "prov-statement",
        "prov-image-2",
        "prov-image-1",
        "prov-image-3",
    )
    assert "Demo Claimant" not in repr(result)


@pytest.mark.parametrize(
    "attachments",
    [
        ["wrong-1", "wrong-2", "wrong-3"],
        ["local-ref-2", "local-ref-1", "local-ref-3"],
        ["local-ref-1", "local-ref-2"],
        ["local-ref-1", "local-ref-1", "local-ref-3"],
    ],
)
def test_g7_compares_raw_ordered_attachment_identity_not_count(
    attachments: list[str],
) -> None:
    packet = _filling_packet()
    payload = _portal_payload(packet)
    payload["attachments"] = attachments

    result = evaluate_g7(
        payload,
        packet=packet,
        case_state=CaseState.FILLING,
        portal_state=PortalState.DRAFT,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G7_ATTACHMENT_MISMATCH,)


@pytest.mark.parametrize("operation", ["extra", "missing"])
def test_g7_rejects_extra_and_missing_fields_as_closed_set_violations(
    operation: str,
) -> None:
    packet = _filling_packet()
    payload = _portal_payload(packet)
    if operation == "extra":
        payload["approval"] = True
    else:
        payload.pop("location")

    result = evaluate_g7(
        payload,
        packet=packet,
        case_state=CaseState.FILLING,
        portal_state=PortalState.DRAFT,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G7_FIELD_NOT_ALLOWED,)


@pytest.mark.parametrize("value", [" Berlin ", "berlin", 7, True, None])
def test_g7_rejects_wrong_types_free_values_and_even_outer_trimming(value: object) -> None:
    packet = _filling_packet()
    payload = _portal_payload(packet)
    payload["location"] = value

    result = evaluate_g7(
        payload,
        packet=packet,
        case_state=CaseState.FILLING,
        portal_state=PortalState.DRAFT,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G7_VALUE_NOT_FROM_PACKET,)


def test_g7_rejects_missing_provenance_model_copy_before_deriving_values() -> None:
    packet = _filling_packet()
    forged_claim = packet.claim.model_copy(
        update={"field_provenance": packet.claim.field_provenance[:-1]}
    )
    forged_packet = packet.model_copy(update={"claim": forged_claim})

    with pytest.raises(PortalWriteInputError) as raised:
        evaluate_g7(
            _portal_payload(packet),
            packet=forged_packet,
            case_state=CaseState.FILLING,
            portal_state=PortalState.DRAFT,
            decided_at=DECIDED_AT,
        )

    assert str(raised.value) == "G7 portal-write input is invalid"


@pytest.mark.parametrize(
    "attachments",
    [
        ("foreign-ref-1", "foreign-ref-2", "foreign-ref-3"),
        ("local-ref-2", "local-ref-1", "local-ref-3"),
    ],
)
def test_g7_revalidates_forged_packet_attachment_identity_before_matching_payload(
    attachments: tuple[str, str, str],
) -> None:
    packet = _filling_packet()
    forged_claim = packet.claim.model_copy(update={"attachments": attachments})
    forged_packet = packet.model_copy(update={"claim": forged_claim})
    matching_forged_payload = _portal_payload(packet)
    matching_forged_payload["attachments"] = list(attachments)

    with pytest.raises(PortalWriteInputError) as raised:
        evaluate_g7(
            matching_forged_payload,
            packet=forged_packet,
            case_state=CaseState.FILLING,
            portal_state=PortalState.DRAFT,
            decided_at=DECIDED_AT,
        )

    assert "foreign" not in repr(raised.value)


@pytest.mark.parametrize(
    ("case_state", "portal_state"),
    [
        (CaseState.READY_TO_FILL, PortalState.DRAFT),
        (CaseState.FILLING, PortalState.REVIEW),
        (CaseState.REVIEW, PortalState.REVIEW),
    ],
)
def test_g7_is_editable_only_in_filling_and_draft(
    case_state: CaseState,
    portal_state: PortalState,
) -> None:
    packet = _filling_packet()
    result = evaluate_g7(
        _portal_payload(packet),
        packet=packet,
        case_state=case_state,
        portal_state=portal_state,
        decided_at=DECIDED_AT,
    )

    assert result.decision.reason_codes == (GateReasonCode.G7_FIELD_NOT_EDITABLE,)
