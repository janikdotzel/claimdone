"""AUTH-001 role, one-time capability, transaction, and HTTP attack tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_workflow_atomic import (
    STATEMENT_TEXT,
    _analysis_case,
    _image_bytes,
    _initial_command,
)

from claimdone_api.audit import build_state_change_event
from claimdone_api.authority import AuthorityError, AuthorityService, create_authority_router
from claimdone_api.cases import CaseService, create_workflow_router
from claimdone_api.cases.workflow_events import EventStreamConfig
from claimdone_api.contracts import (
    ActorType,
    AuditEventType,
    CaseState,
    ClaimPacket,
    GateDecision,
    GateId,
    GateWorkflowEvent,
    PortalState,
    PortalVariant,
    RequiredClaimField,
    SandboxReceipt,
    StateWorkflowEvent,
    WorkflowEventKind,
)
from claimdone_api.gates import make_gate_decision
from claimdone_api.main import ApiSettings, create_app
from claimdone_api.media import (
    ExifChoice,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
    PrivacyReview,
)
from claimdone_api.persistence import (
    CaseRecord,
    CaseSnapshot,
    IncompatiblePersistedContractError,
    IntakeDisclosureCommand,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
)
from claimdone_api.walking_skeleton.body_limit import RequestBodyLimitMiddleware

CREATED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
REVIEW_AT = CREATED_AT + timedelta(minutes=2)
AGENT_SECRET = "A" * 43
HUMAN_SECRET = "H" * 43
SECOND_HUMAN_SECRET = "J" * 43
CASE_ID = "case-auth-001"
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _repository(tmp_path: Path) -> SqliteCaseRepository:
    repository = SqliteCaseRepository(
        tmp_path / "cases.db",
        media_root=tmp_path / "media",
    )
    repository.create_case(
        case_id=CASE_ID,
        redacted_metadata={},
        created_at=CREATED_AT,
    )
    return repository


def _packet(case_id: str, state: CaseState) -> ClaimPacket:
    data = cast(
        dict[str, Any],
        json.loads((REPO_ROOT / "contracts/examples/happy_path.json").read_text()),
    )
    data["caseId"] = case_id
    if state is CaseState.FILLING:
        data["state"] = CaseState.FILLING.value
        data["portalState"] = PortalState.DRAFT.value
        data["gateDecisions"] = data["gateDecisions"][:6]
        data["verification"] = {
            "status": "pending",
            "deterministicMatch": None,
            "modelReportedMismatch": False,
            "fieldResults": [],
            "expectedAttachmentCount": 3,
            "actualAttachmentCount": None,
            "reviewAllowed": False,
            "verifiedAt": None,
        }
    elif state is not CaseState.REVIEW:
        raise AssertionError(f"Unsupported test packet state: {state}")
    return ClaimPacket.model_validate(data)


def _seed_state(
    repository: SqliteCaseRepository,
    state: CaseState,
    *,
    version: int,
    updated_at: datetime,
) -> None:
    packet = _packet(CASE_ID, state)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE cases
            SET version = ?, state = ?, portal_state = ?, claim_packet_json = ?,
                intake_summary_json = '{}', active_clarification_json = NULL,
                updated_at = ?
            WHERE case_id = ?
            """,
            (
                version,
                state.value,
                packet.portal_state.value,
                packet.model_dump_json(by_alias=True),
                updated_at.isoformat(),
                CASE_ID,
            ),
        )


def _canonical_review_repository(
    tmp_path: Path,
) -> tuple[SqliteCaseRepository, CaseRecord]:
    """Build the temporary G6-G8 bridge until CU-002/VER-001 own writers."""

    database_path = tmp_path / "canonical-review.db"
    repository, ready = _canonical_ready_repository(database_path)
    review = _append_test_only_review_authority(repository, ready)

    return _reopen_case(repository, review)


def _canonical_ready_repository(
    database_path: Path,
) -> tuple[SqliteCaseRepository, CaseRecord]:
    """Use the real canonical intake and analysis writers through G5."""

    service, repository, prefix, analyzing = _analysis_case(database_path)
    ready = service.commit_analysis_workflow(
        _initial_command(analyzing, prefix, target=CaseState.READY_TO_FILL)
    ).case
    return _reopen_case(repository, ready)


def _reopen_case(
    repository: SqliteCaseRepository,
    expected: CaseRecord,
) -> tuple[SqliteCaseRepository, CaseRecord]:
    """Force complete canonical replay and media preflight for a fixture."""

    database_path = repository.database_path
    repository.media_store.close()
    reopened = SqliteCaseRepository(database_path)
    reopened_case = reopened.get_case(expected.case_id)
    assert reopened_case is not None and reopened_case == expected
    return reopened, reopened_case


def _append_test_only_review_authority(
    repository: SqliteCaseRepository,
    ready: CaseRecord,
) -> CaseRecord:
    """Persist closed contract events only; never exposed as a product writer."""

    ready_packet = ready.snapshot.claim_packet
    assert ready.state is CaseState.READY_TO_FILL and ready_packet is not None
    filling_at = ready.updated_at + timedelta(seconds=1)
    verifying_at = filling_at + timedelta(seconds=1)
    review_at = verifying_at + timedelta(seconds=1)

    filling_packet = _packet_for_state(
        ready_packet,
        state=CaseState.FILLING,
        portal_state=PortalState.DRAFT,
        gates=ready_packet.gate_decisions,
        verification=ready_packet.verification.model_dump(mode="json", by_alias=True),
    )
    g6 = make_gate_decision(
        GateId.G6_TOOL_AUTHORITY,
        decided_at=filling_at + timedelta(microseconds=1),
    )
    g7 = make_gate_decision(
        GateId.G7_PORTAL_WRITE,
        decided_at=filling_at + timedelta(microseconds=2),
    )
    verifying_packet = _packet_for_state(
        filling_packet,
        state=CaseState.VERIFYING,
        portal_state=PortalState.REVIEW,
        gates=(*filling_packet.gate_decisions, g6, g7),
        verification=filling_packet.verification.model_dump(mode="json", by_alias=True),
    )
    g8 = make_gate_decision(
        GateId.G8_VERIFICATION,
        decided_at=verifying_at + timedelta(microseconds=1),
    )
    review_packet = _packet_for_state(
        verifying_packet,
        state=CaseState.REVIEW,
        portal_state=PortalState.REVIEW,
        gates=(*verifying_packet.gate_decisions, g8),
        verification=_verified_report(verifying_packet, verified_at=g8.decided_at),
    )

    with repository._write_connection() as connection:
        filling = _persist_test_only_packet_transition(
            repository,
            connection,
            current=ready,
            target=CaseState.FILLING,
            packet=filling_packet,
            actor=ActorType.AGENT,
            occurred_at=filling_at,
        )
        repository._insert_authority_gate(
            connection,
            case_id=ready.case_id,
            decision=g6,
        )
        repository._insert_authority_gate(
            connection,
            case_id=ready.case_id,
            decision=g7,
        )
        verifying = _persist_test_only_packet_transition(
            repository,
            connection,
            current=filling,
            target=CaseState.VERIFYING,
            packet=verifying_packet,
            actor=ActorType.AGENT,
            occurred_at=verifying_at,
        )
        repository._insert_authority_gate(
            connection,
            case_id=ready.case_id,
            decision=g8,
        )
        review = _persist_test_only_packet_transition(
            repository,
            connection,
            current=verifying,
            target=CaseState.REVIEW,
            packet=review_packet,
            actor=ActorType.SYSTEM,
            occurred_at=review_at,
        )
    return review


def _packet_for_state(
    packet: ClaimPacket,
    *,
    state: CaseState,
    portal_state: PortalState,
    gates: tuple[GateDecision, ...],
    verification: dict[str, Any],
) -> ClaimPacket:
    data = packet.model_dump(mode="json", by_alias=True)
    data.update(
        {
            "state": state.value,
            "portalState": portal_state.value,
            "gateDecisions": gates,
            "verification": verification,
        }
    )
    return ClaimPacket.model_validate(data)


def _verified_report(packet: ClaimPacket, *, verified_at: datetime) -> dict[str, Any]:
    claim = packet.claim.model_dump(mode="json", by_alias=False)
    source_by_field = {
        item.field: item.source_refs for item in packet.claim.field_provenance
    }
    fields = tuple(
        {
            "field": field.value,
            "expected": claim[field.value],
            "actual": claim[field.value],
            "status": "match",
            "sourceRefs": source_by_field[field],
        }
        for field in RequiredClaimField
        if field is not RequiredClaimField.ATTACHMENTS
    )
    return {
        "status": "verified",
        "deterministicMatch": True,
        "modelReportedMismatch": False,
        "fieldResults": fields,
        "expectedAttachmentCount": 3,
        "actualAttachmentCount": 3,
        "reviewAllowed": True,
        "verifiedAt": verified_at,
    }


def _persist_test_only_packet_transition(
    repository: SqliteCaseRepository,
    connection: sqlite3.Connection,
    *,
    current: CaseRecord,
    target: CaseState,
    packet: ClaimPacket,
    actor: ActorType,
    occurred_at: datetime,
) -> CaseRecord:
    snapshot = CaseSnapshot(
        portal_state=packet.portal_state,
        redacted_metadata=current.snapshot.redacted_metadata,
        claim_packet=packet,
        intake_summary=current.snapshot.intake_summary,
        active_clarification=None,
    )
    repository._insert_packet_authority(
        connection,
        case_id=current.case_id,
        bound_case_version=current.version + 1,
        packet=packet,
        created_at=occurred_at,
    )
    repository._update_case_row(
        connection,
        current=current,
        state=target,
        snapshot=snapshot,
        updated_at=occurred_at,
    )
    audit = build_state_change_event(
        case_id=current.case_id,
        current=current.state,
        target=target,
        actor=actor,
        occurred_at=occurred_at,
    )
    audit_sequence = repository._insert_audit_event(connection, audit)
    repository._insert_workflow_projection(
        connection,
        audit_sequence=audit_sequence,
        audit=audit,
        event=StateWorkflowEvent.model_validate(
            {
                "kind": WorkflowEventKind.STATE,
                "actor": actor,
                "fromState": current.state,
                "toState": target,
            }
        ),
    )
    return repository._require_current(connection, current.case_id, current.version + 1)


def _service(
    repository: SqliteCaseRepository,
    clock: MutableClock,
    *,
    secrets: list[str] | None = None,
) -> AuthorityService:
    values = iter(secrets or [HUMAN_SECRET])
    return AuthorityService(
        repository,
        now=clock,
        secret_factory=lambda: next(values),
        approval_id_factory=lambda variant: f"approval-{variant.value.lower()}-test",
        receipt_id_factory=lambda: "receipt-auth-test",
    )


def _http_client(
    service: AuthorityService,
    *,
    body_limit: int | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(create_authority_router(service))
    if body_limit is not None:
        app.add_middleware(
            RequestBodyLimitMiddleware,
            global_limit=body_limit,
            intake_limit=body_limit,
        )
    return TestClient(app)


def _workflow_http_client(repository: SqliteCaseRepository) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_workflow_router(
            CaseService(repository),
            event_stream_config=EventStreamConfig(
                one_shot=True,
                heartbeat_interval_seconds=0,
            ),
        )
    )
    return TestClient(app)


def _assert_workflow_corruption_fails_before_sse_headers(
    repository: SqliteCaseRepository,
    *,
    case_id: str,
    after: int,
) -> None:
    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(case_id, after=after)

    response = _workflow_http_client(repository).get(
        f"/api/cases/{case_id}/events",
        headers={"Last-Event-ID": str(after)},
    )
    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json()["error"]["code"] == "WORKFLOW_DATA_INVALID"
    assert "event: workflow" not in response.text


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _database_bytes(repository: SqliteCaseRepository) -> bytes:
    prefix = repository.database_path.name
    return b"".join(
        path.read_bytes()
        for path in sorted(repository.database_path.parent.glob(f"{prefix}*"))
        if path.is_file()
    )


def _database_identity_and_dump(path: Path) -> tuple[int, int, tuple[str, ...]]:
    with sqlite3.connect(path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
        dump = tuple(connection.iterdump())
    assert application_id is not None and user_version is not None
    return int(application_id[0]), int(user_version[0]), dump


def _json_time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _completed_receipt(
    tmp_path: Path,
    *,
    variant: PortalVariant = PortalVariant.A,
) -> tuple[SqliteCaseRepository, AuthorityService, SandboxReceipt]:
    repository, review = _canonical_review_repository(tmp_path)
    clock = MutableClock(review.updated_at)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=variant,
    )
    authorization = service.authorize_human_bearer(issued.token)
    receipt = service.approve_authorized(
        review.case_id,
        authorization=authorization,
    )
    return repository, service, receipt


def _append_completed_receipt_case(
    repository: SqliteCaseRepository,
    *,
    case_id: str,
) -> SandboxReceipt:
    case_service = CaseService(
        repository,
        now=lambda: CREATED_AT,
        case_id_factory=lambda: case_id,
    )
    created = case_service.create_case()
    disclosed = case_service.commit_intake_disclosure(
            IntakeDisclosureCommand(
                case_id=created.case_id,
                expected_version=created.version,
            request=IntakeRequest(
                images=tuple(
                    ImageUpload(
                        content=_image_bytes(index + 10),
                        media_type="image/png",
                    )
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
            g0_decided_at=CREATED_AT,
            g1_decided_at=CREATED_AT,
            updated_at=CREATED_AT,
        )
    )
    analyzing = case_service.begin_text_analysis(
        disclosed.case_id,
        expected_version=disclosed.version,
    )
    prefix_rows = repository.list_gate_decisions(disclosed.case_id)
    prefix = (prefix_rows[0].decision, prefix_rows[1].decision)
    ready = case_service.commit_analysis_workflow(
        _initial_command(analyzing, prefix, target=CaseState.READY_TO_FILL)
    ).case
    review = _append_test_only_review_authority(repository, ready)
    clock = MutableClock(review.updated_at)
    authority_service = AuthorityService(
        repository,
        now=clock,
        secret_factory=lambda: SECOND_HUMAN_SECRET,
        approval_id_factory=lambda variant: (
            f"approval-{variant.value.lower()}-second"
        ),
        receipt_id_factory=lambda: "receipt-auth-second",
    )
    issued = authority_service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.B,
    )
    return authority_service.approve_authorized(
        review.case_id,
        authorization=authority_service.authorize_human_bearer(issued.token),
    )


def _final_workflow_source_sequence(
    repository: SqliteCaseRepository,
    case_id: str,
    selector: str,
) -> int:
    for item in repository.list_workflow_events(case_id):
        event = item.envelope.event
        if (
            selector in {"G9", "G10"}
            and isinstance(event, GateWorkflowEvent)
            and event.decision.gate_id.value == selector
        ) or (
            selector == "receipt_state"
            and isinstance(event, StateWorkflowEvent)
            and event.from_state is CaseState.HUMAN_APPROVED
            and event.to_state is CaseState.RECEIPT
        ):
            return item.sequence
    raise AssertionError(f"Missing final workflow selector: {selector}")


def test_server_only_issuance_is_state_and_version_bound_and_digest_only(
    tmp_path: Path,
) -> None:
    created_repository = _repository(tmp_path / "created")
    created_clock = MutableClock(CREATED_AT)
    created_service = _service(created_repository, created_clock)

    with pytest.raises(AuthorityError) as human_before_review:
        created_service.issue_human_approval_capability(
            CASE_ID,
            expected_version=1,
            variant=PortalVariant.A,
        )
    assert human_before_review.value.code == "AUTH_STATE_CONFLICT"

    database_path = tmp_path / "canonical.db"
    repository, ready = _canonical_ready_repository(database_path)
    clock = MutableClock(ready.updated_at)
    service = _service(repository, clock, secrets=[AGENT_SECRET])
    agent = service.issue_agent_capability(
        ready.case_id,
        expected_version=ready.version,
    )
    assert agent.role == "agent" and agent.purpose == "portal_run"
    assert agent.token not in repr(agent)

    review = _append_test_only_review_authority(repository, ready)
    repository, review = _reopen_case(repository, review)
    clock.value = review.updated_at
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    with pytest.raises(AuthorityError) as stale:
        service.issue_human_approval_capability(
            review.case_id,
            expected_version=ready.version,
            variant=PortalVariant.A,
        )
    assert stale.value.code == "AUTH_STATE_CONFLICT"
    human = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.A,
    )
    assert human.role == "human" and human.purpose == "human_approve"
    assert human.token not in repr(human)

    with sqlite3.connect(repository.database_path) as connection:
        rows = connection.execute(
            "SELECT capability_digest, role, purpose FROM authority_capabilities "
            "ORDER BY role"
        ).fetchall()
    assert [(len(row[0]), row[1], row[2]) for row in rows] == [
        (32, "agent", "portal_run"),
        (32, "human", "human_approve"),
    ]
    persisted_bytes = _database_bytes(repository)
    assert agent.token.encode() not in persisted_bytes
    assert human.token.encode() not in persisted_bytes


def test_valid_agent_token_always_returns_the_same_403_before_body_and_case_oracles(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical-agent.db"
    repository, ready = _canonical_ready_repository(database_path)
    clock = MutableClock(ready.updated_at)
    service = _service(repository, clock, secrets=[AGENT_SECRET])
    agent = service.issue_agent_capability(
        ready.case_id,
        expected_version=ready.version,
    )
    review = _append_test_only_review_authority(repository, ready)
    repository, review = _reopen_case(repository, review)
    service = _service(repository, clock)
    client = _http_client(service)

    responses = [
        client.post(
            f"/api/sandbox/cases/{review.case_id}/human-approve",
            content=b"{broken",
            headers=_auth(agent.token),
        ),
        client.post(
            "/api/sandbox/cases/case-does-not-exist/human-approve",
            json={"unexpected": "body"},
            headers=_auth(agent.token),
        ),
    ]

    clock.value = agent.expires_at + timedelta(seconds=1)
    responses.append(
        client.post(
            f"/api/sandbox/cases/{review.case_id}/human-approve",
            headers=_auth(agent.token),
        )
    )
    digest = hashlib.sha256(agent.token.encode("ascii")).digest()
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE authority_capabilities SET bound_case_version = 1, revoked_at = ? "
            "WHERE capability_digest = ?",
            ((ready.updated_at + timedelta(seconds=1)).isoformat(), digest),
        )
    responses.append(
        client.post(
            "/api/sandbox/cases/another-case/human-approve",
            content=b"not-json",
            headers=_auth(agent.token),
        )
    )

    expected = responses[0].json()
    assert expected["error"]["code"] == "AUTH_AGENT_FORBIDDEN"
    assert expected["error"]["currentVersion"] is None
    assert all(response.status_code == 403 for response in responses)
    assert all(response.json() == expected for response in responses)
    assert agent.token not in json.dumps(expected)


def test_human_approval_consumes_once_and_returns_only_a_redacted_receipt(
    tmp_path: Path,
) -> None:
    repository, review = _canonical_review_repository(tmp_path)
    clock = MutableClock(review.updated_at)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.A,
    )
    assert repository.get_sandbox_receipt(review.case_id) is None
    client = _http_client(service)

    response = client.post(
        f"/api/sandbox/cases/{review.case_id}/human-approve",
        headers=_auth(issued.token),
    )
    assert response.status_code == 200, response.text
    receipt = cast(dict[str, Any], response.json())
    consumed_at = review.updated_at + timedelta(microseconds=1)
    approved_at = consumed_at + timedelta(microseconds=1)
    rendered_at = approved_at + timedelta(microseconds=1)
    assert receipt == {
        "contractVersion": "3.0.0",
        "receiptId": "receipt-auth-test",
        "caseId": review.case_id,
        "approvalId": "approval-a-test",
        "variant": "A",
        "state": "receipt",
        "version": review.version + 2,
        "environment": "sandbox",
        "sandboxOnly": True,
        "submittedToRealInsurer": False,
        "humanApproved": True,
        "redacted": True,
        "summary": {
            "completedFieldCount": 8,
            "attachmentCount": 3,
            "verificationPassed": True,
            "finalActionOwner": "human",
        },
        "approvedAt": _json_time(approved_at),
        "renderedAt": _json_time(rendered_at),
    }
    serialized_receipt = json.dumps(receipt)
    for forbidden in (
        issued.token,
        "Demo Claimant",
        "DEMO-42",
        "DEMO-CD-1",
        "local-ref-1",
    ):
        assert forbidden not in serialized_receipt

    capability = repository.get_authority_capability(
        hashlib.sha256(issued.token.encode("ascii")).digest()
    )
    assert capability is not None and capability.consumed_at is not None
    final_case = repository.get_case(review.case_id)
    assert final_case is not None and final_case.state is CaseState.RECEIPT

    reused = client.post(
        f"/api/sandbox/cases/{review.case_id}/human-approve",
        headers=_auth(issued.token),
    )
    assert reused.status_code == 403
    assert reused.json()["error"] == {
        "code": "AUTH_TOKEN_INVALID",
        "message": "The human approval capability is invalid or no longer usable.",
        "reasonCodes": ["G9_TOKEN_INVALID"],
        "fieldErrors": [],
        "gateDecision": None,
        "currentVersion": None,
    }

    # Reopening is the end-to-end integrity proof for media, replay, packet
    # authority, one-time capability binding, receipt redaction, and G0-G10.
    repository, final_case = _reopen_case(repository, final_case)
    stored = repository.get_sandbox_receipt(review.case_id)
    assert stored is not None and stored.receipt.redacted
    snapshot = repository.get_workflow_snapshot(
        review.case_id,
        request_id="request-auth-test",
    )
    assert snapshot.case.state is CaseState.RECEIPT
    assert snapshot.claim_packet is None
    assert snapshot.portal_session is None
    assert snapshot.verification_attempts is None
    assert snapshot.receipt == stored.receipt

    gates = repository.list_gate_decisions(review.case_id)
    assert tuple(item.decision.gate_id for item in gates) == tuple(
        GateId(f"G{index}") for index in range(11)
    )
    assert all(item.decision.passed for item in gates)
    events = repository.list_workflow_events(review.case_id)
    assert tuple(item.envelope.event.kind for item in events[-4:]) == (
        WorkflowEventKind.GATE,
        WorkflowEventKind.STATE,
        WorkflowEventKind.GATE,
        WorkflowEventKind.STATE,
    )
    audits = repository.list_audit_events(review.case_id)
    assert tuple(item.event.event_type for item in audits[-6:]) == (
        AuditEventType.GATE_DECISION,
        AuditEventType.HUMAN_APPROVAL,
        AuditEventType.CASE_STATE_CHANGED,
        AuditEventType.GATE_DECISION,
        AuditEventType.RECEIPT,
        AuditEventType.CASE_STATE_CHANGED,
    )
    assert next(
        item
        for item in audits
        if item.event.event_type is AuditEventType.HUMAN_APPROVAL
    ).event.actor is ActorType.HUMAN
    serialized_events = json.dumps(
        [
            item.envelope.model_dump(mode="json", by_alias=True)
            for item in events
        ]
    )
    serialized_audits = json.dumps(
        [item.event.model_dump(mode="json", by_alias=True) for item in audits]
    )
    assert issued.token not in serialized_events + serialized_audits


@pytest.mark.parametrize("selector", ("G9", "G10", "receipt_state"))
def test_workflow_replay_rejects_orphaned_final_source_audits_for_every_cursor(
    tmp_path: Path,
    selector: str,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    source_sequence = _final_workflow_source_sequence(
        repository,
        receipt.case_id,
        selector,
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM audit_events WHERE sequence = ?",
            (source_sequence,),
        )

    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(receipt.case_id)
    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(
            receipt.case_id,
            after=source_sequence,
        )


def test_real_sse_replay_and_reconnect_fail_before_headers_after_g9_source_deletion(
    tmp_path: Path,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    client = _workflow_http_client(repository)
    path = f"/api/cases/{receipt.case_id}/events"

    initial = client.get(path)
    assert initial.status_code == 200
    assert initial.headers["content-type"].startswith("text/event-stream")
    initial_cursors = tuple(
        int(line.removeprefix("id: "))
        for line in initial.text.splitlines()
        if line.startswith("id: ")
    )
    g9_source = _final_workflow_source_sequence(repository, receipt.case_id, "G9")
    reconnect = client.get(path, headers={"Last-Event-ID": str(g9_source)})
    reconnect_cursors = tuple(
        int(line.removeprefix("id: "))
        for line in reconnect.text.splitlines()
        if line.startswith("id: ")
    )
    assert reconnect.status_code == 200
    assert initial_cursors
    assert reconnect_cursors
    assert all(cursor > g9_source for cursor in reconnect_cursors)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM audit_events WHERE sequence = ?",
            (g9_source,),
        )

    corrupted = client.get(path, headers={"Last-Event-ID": str(g9_source)})
    assert corrupted.status_code == 500
    assert not corrupted.headers["content-type"].startswith("text/event-stream")
    assert corrupted.json()["error"]["code"] == "WORKFLOW_DATA_INVALID"
    assert "event: workflow" not in corrupted.text


def test_reconnect_validates_corruption_before_its_replay_cursor(
    tmp_path: Path,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    events = repository.list_workflow_events(receipt.case_id)
    g9_source = _final_workflow_source_sequence(repository, receipt.case_id, "G9")
    earlier = next(item for item in events if item.sequence < g9_source)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE workflow_events
            SET event_json = json_set(
                event_json,
                '$.cursor', ?,
                '$.sourceAuditSequence', ?
            )
            WHERE source_audit_sequence = ?
            """,
            (earlier.sequence + 1, earlier.sequence + 1, earlier.sequence),
        )

    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(receipt.case_id, after=g9_source)


@pytest.mark.parametrize("tampering", ("audit_id", "audit_occurred_at"))
def test_reconnect_rejects_coherent_audit_identity_tampering_before_cursor(
    tmp_path: Path,
    tampering: str,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    events = repository.list_workflow_events(receipt.case_id)
    g9_source = _final_workflow_source_sequence(repository, receipt.case_id, "G9")
    earlier = next(item for item in events if item.sequence < g9_source)

    with sqlite3.connect(repository.database_path) as connection:
        if tampering == "audit_id":
            connection.execute(
                """
                UPDATE audit_events
                SET event_id = ?,
                    event_json = json_set(event_json, '$.eventId', ?)
                WHERE sequence = ?
                """,
                (
                    "tampered-audit-source",
                    "tampered-audit-source",
                    earlier.sequence,
                ),
            )
        else:
            row = connection.execute(
                "SELECT occurred_at FROM audit_events WHERE sequence = ?",
                (earlier.sequence,),
            ).fetchone()
            assert row is not None
            changed_at = _json_time(
                datetime.fromisoformat(str(row[0]))
                + timedelta(microseconds=1)
            )
            connection.execute(
                """
                UPDATE audit_events
                SET occurred_at = ?,
                    event_json = json_set(event_json, '$.occurredAt', ?)
                WHERE sequence = ?
                """,
                (changed_at, changed_at, earlier.sequence),
            )

    _assert_workflow_corruption_fails_before_sse_headers(
        repository,
        case_id=receipt.case_id,
        after=g9_source,
    )


def test_reconnect_binds_gate_evidence_refs_to_full_history_before_cursor(
    tmp_path: Path,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    events = repository.list_workflow_events(receipt.case_id)
    g9_source = _final_workflow_source_sequence(repository, receipt.case_id, "G9")
    earlier_gate = next(
        item
        for item in events
        if item.sequence < g9_source
        and isinstance(item.envelope.event, GateWorkflowEvent)
    )
    assert isinstance(earlier_gate.envelope.event, GateWorkflowEvent)

    with sqlite3.connect(repository.database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE workflow_events
            SET event_json = json_set(
                event_json,
                '$.event.decision.evidenceRefs',
                json_array('tampered-evidence-ref')
            )
            WHERE source_audit_sequence = ?
            """,
            (earlier_gate.sequence,),
        )
        assert cursor.rowcount == 1

    _assert_workflow_corruption_fails_before_sse_headers(
        repository,
        case_id=receipt.case_id,
        after=g9_source,
    )


def test_reconnect_rejects_noncontiguous_full_state_history_before_cursor(
    tmp_path: Path,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    events = repository.list_workflow_events(receipt.case_id)
    g9_source = _final_workflow_source_sequence(repository, receipt.case_id, "G9")
    first_state = next(
        item
        for item in events
        if isinstance(item.envelope.event, StateWorkflowEvent)
        and item.envelope.event.from_state is CaseState.CREATED
    )
    first_state_event = first_state.envelope.event
    assert isinstance(first_state_event, StateWorkflowEvent)
    assert first_state_event.to_state is CaseState.DISCLOSED
    assert any(
        isinstance(item.envelope.event, StateWorkflowEvent)
        and item.envelope.event.from_state is CaseState.DISCLOSED
        and item.sequence > first_state.sequence
        for item in events
    )

    with sqlite3.connect(repository.database_path) as connection:
        workflow_cursor = connection.execute(
            """
            UPDATE workflow_events
            SET event_json = json_set(event_json, '$.event.toState', 'abandoned')
            WHERE source_audit_sequence = ?
            """,
            (first_state.sequence,),
        )
        audit_cursor = connection.execute(
            """
            UPDATE audit_events
            SET event_json = json_set(event_json, '$.toState', 'abandoned')
            WHERE sequence = ?
            """,
            (first_state.sequence,),
        )
        assert workflow_cursor.rowcount == 1
        assert audit_cursor.rowcount == 1

    _assert_workflow_corruption_fails_before_sse_headers(
        repository,
        case_id=receipt.case_id,
        after=g9_source,
    )


def test_workflow_read_never_mixes_authority_from_two_wal_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    expected_full_replay = repository.list_workflow_events(receipt.case_id)
    deleted_source = expected_full_replay[0].sequence
    original = SqliteCaseRepository._validate_case_workflow_source_bindings
    injected = False

    def validate_then_delete(
        selected: SqliteCaseRepository,
        connection: sqlite3.Connection,
        *,
        case_id: str,
    ) -> None:
        nonlocal injected
        original(selected, connection, case_id=case_id)
        if not injected:
            injected = True
            with sqlite3.connect(repository.database_path) as writer:
                writer.execute(
                    "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
                    (deleted_source,),
                )

    monkeypatch.setattr(
        SqliteCaseRepository,
        "_validate_case_workflow_source_bindings",
        validate_then_delete,
    )
    first_snapshot = repository.list_workflow_events(receipt.case_id)
    assert first_snapshot == expected_full_replay
    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(receipt.case_id)


def test_two_receipt_cases_reopen_globally_but_event_reads_remain_case_scoped(
    tmp_path: Path,
) -> None:
    repository, _service_instance, first_receipt = _completed_receipt(tmp_path)
    second_receipt = _append_completed_receipt_case(
        repository,
        case_id="case-auth-second",
    )
    database_path = repository.database_path
    repository.media_store.close()
    reopened = SqliteCaseRepository(database_path)
    first_stored = reopened.get_sandbox_receipt(first_receipt.case_id)
    second_stored = reopened.get_sandbox_receipt(second_receipt.case_id)
    assert first_stored is not None and first_stored.receipt == first_receipt
    assert second_stored is not None and second_stored.receipt == second_receipt
    first_events = reopened.list_workflow_events(first_receipt.case_id)
    second_g9_source = _final_workflow_source_sequence(
        reopened,
        second_receipt.case_id,
        "G9",
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM audit_events WHERE sequence = ?",
            (second_g9_source,),
        )

    # Product event reads validate only the selected case, avoiding a
    # cross-case corruption oracle while startup/reopen remains global.
    assert reopened.list_workflow_events(first_receipt.case_id) == first_events
    with pytest.raises(PersistedDataIntegrityError):
        reopened.list_workflow_events(second_receipt.case_id)
    reopened.media_store.close()
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


@pytest.mark.parametrize(
    "tampering",
    (
        "delete_human_approval_audit",
        "delete_receipt_audit",
        "mutate_variant_and_ids",
        "mutate_gate_time",
        "swap_gate_sequences",
        "mutate_audit_time",
        "swap_audit_sequences",
        "add_human_approval_audit",
    ),
)
def test_receipt_authority_rejects_deleted_added_swapped_and_mutated_rows_on_read_and_reopen(
    tmp_path: Path,
    tampering: str,
) -> None:
    repository, _service_instance, receipt = _completed_receipt(tmp_path)
    database_path = repository.database_path
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        authority = connection.execute(
            "SELECT * FROM sandbox_receipt_authority WHERE case_id = ?",
            (receipt.case_id,),
        ).fetchone()
        assert authority is not None
        if tampering == "delete_human_approval_audit":
            connection.execute(
                "DELETE FROM audit_events WHERE sequence = ?",
                (authority["human_approval_audit_sequence"],),
            )
        elif tampering == "delete_receipt_audit":
            connection.execute(
                "DELETE FROM audit_events WHERE sequence = ?",
                (authority["receipt_audit_sequence"],),
            )
        elif tampering == "mutate_variant_and_ids":
            connection.execute(
                """
                UPDATE sandbox_receipts
                SET receipt_json = json_set(
                    receipt_json,
                    '$.variant', 'B',
                    '$.approvalId', 'approval-b-tampered',
                    '$.receiptId', 'receipt-tampered'
                )
                WHERE case_id = ?
                """,
                (receipt.case_id,),
            )
            mutated = connection.execute(
                "SELECT receipt_json FROM sandbox_receipts WHERE case_id = ?",
                (receipt.case_id,),
            ).fetchone()
            assert mutated is not None
            mutated_json = str(mutated["receipt_json"])
            connection.execute(
                """
                UPDATE sandbox_receipt_authority
                SET portal_variant = 'B',
                    approval_id = 'approval-b-tampered',
                    receipt_id = 'receipt-tampered',
                    receipt_json = ?,
                    receipt_sha256 = ?
                WHERE case_id = ?
                """,
                (
                    mutated_json,
                    hashlib.sha256(mutated_json.encode("utf-8")).hexdigest(),
                    receipt.case_id,
                ),
            )
        elif tampering == "mutate_gate_time":
            changed_at = (receipt.approved_at + timedelta(seconds=1)).isoformat()
            connection.execute(
                """
                UPDATE gate_decisions
                SET decided_at = ?,
                    decision_json = json_set(decision_json, '$.decidedAt', ?)
                WHERE sequence = ?
                """,
                (changed_at, changed_at, authority["g9_gate_sequence"]),
            )
        elif tampering == "swap_gate_sequences":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            g9_sequence = int(authority["g9_gate_sequence"])
            g10_sequence = int(authority["g10_gate_sequence"])
            connection.execute(
                "UPDATE sandbox_receipt_authority SET g9_gate_sequence = -1 "
                "WHERE case_id = ?",
                (receipt.case_id,),
            )
            connection.execute(
                "UPDATE sandbox_receipt_authority SET g10_gate_sequence = ? "
                "WHERE case_id = ?",
                (g9_sequence, receipt.case_id),
            )
            connection.execute(
                "UPDATE sandbox_receipt_authority SET g9_gate_sequence = ? "
                "WHERE case_id = ?",
                (g10_sequence, receipt.case_id),
            )
        elif tampering == "mutate_audit_time":
            changed_at = (receipt.approved_at + timedelta(seconds=1)).isoformat()
            connection.execute(
                """
                UPDATE audit_events
                SET occurred_at = ?,
                    event_json = json_set(event_json, '$.occurredAt', ?)
                WHERE sequence = ?
                """,
                (
                    changed_at,
                    changed_at,
                    authority["human_approval_audit_sequence"],
                ),
            )
        elif tampering == "swap_audit_sequences":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            approval_sequence = int(authority["human_approval_audit_sequence"])
            receipt_sequence = int(authority["receipt_audit_sequence"])
            connection.execute(
                "UPDATE sandbox_receipt_authority "
                "SET human_approval_audit_sequence = -1 WHERE case_id = ?",
                (receipt.case_id,),
            )
            connection.execute(
                "UPDATE sandbox_receipt_authority SET receipt_audit_sequence = ? "
                "WHERE case_id = ?",
                (approval_sequence, receipt.case_id),
            )
            connection.execute(
                "UPDATE sandbox_receipt_authority "
                "SET human_approval_audit_sequence = ? WHERE case_id = ?",
                (receipt_sequence, receipt.case_id),
            )
        elif tampering == "add_human_approval_audit":
            source = connection.execute(
                "SELECT * FROM audit_events WHERE sequence = ?",
                (authority["human_approval_audit_sequence"],),
            ).fetchone()
            assert source is not None
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, case_id, occurred_at, event_json
                ) VALUES (?, ?, ?, json_set(?, '$.eventId', ?))
                """,
                (
                    "event_extra_human_approval",
                    receipt.case_id,
                    source["occurred_at"],
                    source["event_json"],
                    "event_extra_human_approval",
                ),
            )
        else:  # pragma: no cover - closed parametrization above
            raise AssertionError(tampering)

    with pytest.raises(PersistedDataIntegrityError):
        repository.get_sandbox_receipt(receipt.case_id)
    with pytest.raises(PersistedDataIntegrityError):
        repository.list_workflow_events(receipt.case_id)
    repository.media_store.close()
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


def test_two_preauthorized_concurrent_reuses_close_exactly_once_without_version_oracle(
    tmp_path: Path,
) -> None:
    repository, review = _canonical_review_repository(tmp_path)
    clock = MutableClock(review.updated_at)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.A,
    )
    authorizations = (
        service.authorize_human_bearer(issued.token),
        service.authorize_human_bearer(issued.token),
    )
    barrier = Barrier(2)

    def approve(authorization: Any) -> SandboxReceipt | AuthorityError:
        barrier.wait()
        try:
            return service.approve_authorized(
                review.case_id,
                authorization=authorization,
            )
        except AuthorityError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(approve, authorizations))

    receipts = tuple(item for item in outcomes if isinstance(item, SandboxReceipt))
    failures = tuple(item for item in outcomes if isinstance(item, AuthorityError))
    assert len(receipts) == 1
    assert len(failures) == 1
    assert failures[0].code == "AUTH_TOKEN_INVALID"
    assert failures[0].status_code == 403
    assert failures[0].current_version is None
    final = repository.get_case(review.case_id)
    assert final is not None and final.state is CaseState.RECEIPT
    repository, final = _reopen_case(repository, final)
    stored = repository.get_sandbox_receipt(review.case_id)
    assert stored is not None and stored.receipt == receipts[0]


def test_transport_body_limits_precede_auth_but_bounded_bodies_remain_auth_first(
    tmp_path: Path,
) -> None:
    repository, ready = _canonical_ready_repository(tmp_path / "transport.db")
    clock = MutableClock(ready.updated_at)
    service = _service(repository, clock, secrets=[AGENT_SECRET])
    agent = service.issue_agent_capability(
        ready.case_id,
        expected_version=ready.version,
    )
    client = _http_client(service, body_limit=4)
    path = f"/api/sandbox/cases/{ready.case_id}/human-approve"

    invalid_length = client.post(
        path,
        content=b"x",
        headers={**_auth(agent.token), "Content-Length": "invalid"},
    )
    oversized = client.post(
        path,
        content=b"12345",
        headers=_auth(agent.token),
    )
    bounded = client.post(
        path,
        content=b"x",
        headers=_auth(agent.token),
    )

    assert invalid_length.status_code == 400
    assert invalid_length.json()["error"]["code"] == "CONTENT_LENGTH_INVALID"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
    assert bounded.status_code == 403
    assert bounded.json()["error"]["code"] == "AUTH_AGENT_FORBIDDEN"


def test_v5_final_receipt_reaches_explicit_v6_rejection_and_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _service_instance, _receipt = _completed_receipt(tmp_path)
    database_path = repository.database_path
    repository.media_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE sandbox_receipt_authority")
        connection.execute(
            "ALTER TABLE authority_capabilities DROP COLUMN portal_variant"
        )
        connection.execute("PRAGMA user_version = 5")
    before = _database_identity_and_dump(database_path)
    original = SqliteCaseRepository._migrate_v5_to_v6
    migration_called = False

    def observe_migration(
        selected: SqliteCaseRepository,
        connection: sqlite3.Connection,
    ) -> None:
        nonlocal migration_called
        migration_called = True
        original(selected, connection)

    monkeypatch.setattr(
        SqliteCaseRepository,
        "_migrate_v5_to_v6",
        observe_migration,
    )
    with pytest.raises(IncompatiblePersistedContractError, match="make reset"):
        SqliteCaseRepository(database_path)

    assert migration_called
    after = _database_identity_and_dump(database_path)
    assert after == before
    assert after[1] == 5


def test_human_body_is_rejected_after_auth_without_consuming_or_persisting_token(
    tmp_path: Path,
) -> None:
    repository, review = _canonical_review_repository(tmp_path)
    clock = MutableClock(review.updated_at)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.B,
    )
    client = _http_client(service)

    response = client.post(
        f"/api/sandbox/cases/{review.case_id}/human-approve",
        json={"token": issued.token},
        headers=_auth(issued.token),
    )
    assert response.status_code == 422
    capability = repository.get_authority_capability(
        hashlib.sha256(issued.token.encode("ascii")).digest()
    )
    assert capability is not None and capability.consumed_at is None
    assert repository.get_sandbox_receipt(review.case_id) is None
    persisted_bytes = _database_bytes(repository)
    assert issued.token.encode() not in persisted_bytes


def test_wrong_state_and_expired_human_tokens_fail_closed_without_receipt(
    tmp_path: Path,
) -> None:
    wrong_state_repository = _repository(tmp_path / "wrong-state")
    wrong_state_clock = MutableClock(CREATED_AT)
    wrong_state_token = f"cdcap_h_a_{HUMAN_SECRET}"
    wrong_state_repository.issue_authority_capability(
        case_id=CASE_ID,
        expected_case_version=1,
        digest=hashlib.sha256(wrong_state_token.encode("ascii")).digest(),
        role="human",
        purpose="human_approve",
        portal_variant=PortalVariant.A,
        issued_at=CREATED_AT,
        expires_at=CREATED_AT + timedelta(seconds=90),
    )
    wrong_state_service = _service(wrong_state_repository, wrong_state_clock)
    wrong_state_client = _http_client(wrong_state_service)
    wrong_state = wrong_state_client.post(
        f"/api/sandbox/cases/{CASE_ID}/human-approve",
        headers=_auth(wrong_state_token),
    )
    assert wrong_state.status_code == 403
    assert wrong_state.json()["error"]["code"] == "AUTH_TOKEN_INVALID"
    wrong_capability = wrong_state_repository.get_authority_capability(
        hashlib.sha256(wrong_state_token.encode("ascii")).digest()
    )
    assert wrong_capability is not None and wrong_capability.consumed_at is None
    assert wrong_state_repository.get_sandbox_receipt(CASE_ID) is None

    expired_repository, review = _canonical_review_repository(tmp_path / "expired")
    expired_clock = MutableClock(review.updated_at)
    expired_service = _service(
        expired_repository,
        expired_clock,
        secrets=[SECOND_HUMAN_SECRET],
    )
    expired = expired_service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.A,
    )
    expired_clock.value = expired.expires_at
    expired_response = _http_client(expired_service).post(
        f"/api/sandbox/cases/{review.case_id}/human-approve",
        headers=_auth(expired.token),
    )
    assert expired_response.status_code == 403
    assert expired_response.json()["error"] == wrong_state.json()["error"]
    assert expired_repository.get_sandbox_receipt(review.case_id) is None


def test_corrupt_direct_review_seed_is_rejected_before_capability_consumption(
    tmp_path: Path,
) -> None:
    """A review-looking row cannot substitute for canonical G0-G8 authority."""

    repository = _repository(tmp_path)
    _seed_state(repository, CaseState.REVIEW, version=9, updated_at=REVIEW_AT)
    clock = MutableClock(REVIEW_AT)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        CASE_ID,
        expected_version=9,
        variant=PortalVariant.A,
    )

    response = _http_client(service).post(
        f"/api/sandbox/cases/{CASE_ID}/human-approve",
        headers=_auth(issued.token),
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "AUTH_STATE_CONFLICT",
        "message": "The sandbox case is not at the required approval boundary.",
        "reasonCodes": [],
        "fieldErrors": [],
        "gateDecision": None,
        "currentVersion": None,
    }
    capability = repository.get_authority_capability(
        hashlib.sha256(issued.token.encode("ascii")).digest()
    )
    assert capability is not None and capability.consumed_at is None
    assert repository.get_sandbox_receipt(CASE_ID) is None
    with sqlite3.connect(repository.database_path) as connection:
        for table in (
            "gate_decisions",
            "audit_events",
            "workflow_events",
            "case_packet_authority",
            "sandbox_receipts",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

    database_path = repository.database_path
    repository.media_store.close()
    with pytest.raises(PersistedDataIntegrityError):
        SqliteCaseRepository(database_path)


def test_failure_after_g9_rolls_back_token_gates_states_audits_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, review = _canonical_review_repository(tmp_path)
    clock = MutableClock(review.updated_at)
    service = _service(repository, clock, secrets=[HUMAN_SECRET])
    issued = service.issue_human_approval_capability(
        review.case_id,
        expected_version=review.version,
        variant=PortalVariant.A,
    )
    authorization = service.authorize_human_bearer(issued.token)
    before_counts: dict[str, int] = {}
    with sqlite3.connect(repository.database_path) as connection:
        for table in (
            "gate_decisions",
            "audit_events",
            "workflow_events",
            "case_packet_authority",
            "sandbox_receipts",
        ):
            before_counts[table] = cast(
                int,
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
            )
    original = SqliteCaseRepository._insert_authority_gate
    calls = 0

    def fail_second_gate(
        selected: SqliteCaseRepository,
        connection: sqlite3.Connection,
        *,
        case_id: str,
        decision: Any,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected post-G9 failure")
        original(selected, connection, case_id=case_id, decision=decision)

    monkeypatch.setattr(SqliteCaseRepository, "_insert_authority_gate", fail_second_gate)
    with pytest.raises(RuntimeError, match="post-G9"):
        service.approve_authorized(review.case_id, authorization=authorization)

    current = repository.get_case(review.case_id)
    assert current == review
    capability = repository.get_authority_capability(
        hashlib.sha256(issued.token.encode("ascii")).digest()
    )
    assert capability is not None and capability.consumed_at is None
    assert repository.get_sandbox_receipt(review.case_id) is None
    with sqlite3.connect(repository.database_path) as connection:
        for table, count in before_counts.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                count,
            )

    reopened_repository, reopened = _reopen_case(repository, review)
    assert reopened == review
    assert reopened_repository.get_case(review.case_id) == review


def test_production_exposes_one_approval_route_no_issue_route_and_blocks_browser_auth_cors(
    tmp_path: Path,
) -> None:
    origin = "http://127.0.0.1:3000"
    app = create_app(
        ApiSettings(
            data_dir=tmp_path / "state",
            web_origin=origin,
            portal_origin=origin,
        )
    )
    paths = set(app.openapi()["paths"])
    approval_path = "/api/sandbox/cases/{case_id}/human-approve"
    assert approval_path in paths
    assert sum("human-approve" in path for path in paths) == 1
    assert all("approval-token" not in path and "capability" not in path for path in paths)

    with TestClient(app) as client:
        preflight = client.options(
            "/api/sandbox/cases/case-auth-001/human-approve",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
    assert preflight.status_code == 400
    assert "authorization" not in preflight.headers.get("access-control-allow-headers", "").lower()
