"""Canonical WorkflowSnapshot, SSE replay, and closed HTTP boundary tests."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    CaseState,
    ClaimPacket,
    GateId,
    PortalSessionView,
    PortalState,
    SandboxReceipt,
    VerificationAttemptSeries,
    WorkflowEventEnvelope,
)
from claimdone_api.gates.registry import make_gate_decision
from claimdone_api.media import CaseMediaStore
from claimdone_api.persistence import (
    CaseRecord,
    CaseSnapshot,
    PersistedDataIntegrityError,
    SandboxReceiptRecord,
    SequencedWorkflowEvent,
    SqliteCaseRepository,
    TranscriptRecord,
)
from claimdone_api.workflow import (
    MAX_TRANSCRIPT_TEXT_BYTES,
    EventStreamConfig,
    MediaTranscriptTextReader,
    SnapshotAssembler,
    WorkflowDataIntegrityError,
    WorkflowEventStreamer,
    WorkflowVersionChurnError,
    create_workflow_router,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"
CASE_ID = "case-workflow-001"
CREATED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
UPDATED_AT = CREATED_AT + timedelta(seconds=20)
TRANSCRIPT_TEXT = "Synthetic transcript"
TRANSCRIPT_DIGEST = hashlib.sha256(TRANSCRIPT_TEXT.encode()).hexdigest()


def _happy_packet_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def _packet(state: CaseState) -> ClaimPacket:
    data = _happy_packet_data()
    data["caseId"] = CASE_ID
    data["state"] = state.value
    if state in {
        CaseState.ANALYZING,
        CaseState.AWAITING_CLARIFICATION,
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
    }:
        data["portalState"] = "draft"
    elif state in {CaseState.VERIFYING, CaseState.REVIEW}:
        data["portalState"] = "review"
    elif state is CaseState.HUMAN_APPROVED:
        data["portalState"] = "human_approved"
        data["gateDecisions"].append(
            make_gate_decision(
                GateId.G9_HUMAN_APPROVAL,
                decided_at=CREATED_AT + timedelta(seconds=10),
            ).model_dump(mode="json", by_alias=True)
        )
    return ClaimPacket.model_validate(data)


def _clarification(version: int = 7) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "clarificationId": "clarification-001",
        "caseId": CASE_ID,
        "field": "incident_date",
        "round": 1,
        "question": "What was the incident date?",
        "status": "requested",
        "expectedVersion": version,
        "requestedAt": CREATED_AT + timedelta(seconds=10),
    }


def _portal_data(*, state: str = "review", version: int = 3) -> dict[str, Any]:
    claim = _happy_packet_data()["claim"]
    fields = {
        "incidentDate": claim["incidentDate"],
        "incidentTime": claim["incidentTime"],
        "location": claim["location"],
        "claimantName": claim["claimantName"],
        "policyReference": claim["policyReference"],
        "vehicleRegistration": claim["vehicleRegistration"],
        "counterpartyKnown": claim["counterpartyKnown"],
        "narrative": claim["narrative"],
        "attachments": claim["attachments"],
    }
    if state == "draft":
        fields = {
            "incidentDate": "",
            "incidentTime": "",
            "location": "",
            "claimantName": "",
            "policyReference": "",
            "vehicleRegistration": "",
            "counterpartyKnown": "",
            "narrative": "",
            "attachments": [],
        }
    return {
        "contractVersion": CONTRACT_VERSION,
        "caseId": CASE_ID,
        "variant": "A",
        "state": state,
        "version": version,
        "fields": fields,
        "updatedAt": CREATED_AT + timedelta(seconds=15),
        "auditCount": 4,
    }


def _portal(*, state: str = "review", version: int = 3) -> PortalSessionView:
    return PortalSessionView.model_validate(_portal_data(state=state, version=version))


def _verification(*, portal_version: int = 3) -> VerificationAttemptSeries:
    packet = _happy_packet_data()
    packet["caseId"] = CASE_ID
    return VerificationAttemptSeries.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": CASE_ID,
            "attempts": [
                {
                    "contractVersion": CONTRACT_VERSION,
                    "attemptId": "verification-001",
                    "caseId": CASE_ID,
                    "attemptNumber": 1,
                    "caseState": "verifying",
                    "portalVersion": portal_version,
                    "report": packet["verification"],
                    "final": True,
                    "repair": None,
                    "repairedFromAttemptId": None,
                    "gateDecision": packet["gateDecisions"][-1],
                }
            ],
        }
    )


def _receipt() -> SandboxReceipt:
    return SandboxReceipt.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "receiptId": "receipt-001",
            "caseId": CASE_ID,
            "approvalId": "approval-001",
            "variant": "A",
            "state": "receipt",
            "version": 2,
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
            "approvedAt": CREATED_AT + timedelta(seconds=17),
            "renderedAt": CREATED_AT + timedelta(seconds=18),
        }
    )


def _record(state: CaseState, *, version: int = 7) -> CaseRecord:
    packet = (
        _packet(state)
        if state
        in {
            CaseState.AWAITING_CLARIFICATION,
            CaseState.READY_TO_FILL,
            CaseState.FILLING,
            CaseState.VERIFYING,
            CaseState.REVIEW,
            CaseState.HUMAN_APPROVED,
        }
        else None
    )
    portal_state = packet.portal_state if packet is not None else PortalState.DRAFT
    if state is CaseState.RECEIPT:
        portal_state = PortalState.RECEIPT
    intake_summary = None
    if state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
        transcript = _transcript(version)
        intake_summary = {
            "images": [],
            "text": None,
            "audio": {
                "fileId": f"audio-{'2' * 32}.wav",
                "mediaType": "audio/wav",
                "sha256": "c" * 64,
            },
            "statement": {
                "fileId": transcript.local_ref,
                "mediaType": "text/plain",
                "sha256": transcript.transcript_sha256,
            },
        }
    return CaseRecord(
        case_id=CASE_ID,
        version=version,
        state=state,
        snapshot=CaseSnapshot(
            portal_state=portal_state,
            redacted_metadata={},
            claim_packet=packet,
            intake_summary=intake_summary,
            active_clarification=(
                _clarification(version)
                if state is CaseState.AWAITING_CLARIFICATION
                else None
            ),
        ),
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _transcript(version: int = 7) -> TranscriptRecord:
    local_ref = f"transcript-{'1' * 32}.txt"
    identity = hashlib.sha256(
        (
            f"claimdone-transcript-v1\0{CASE_ID}\0{local_ref}\0"
            f"{TRANSCRIPT_DIGEST}"
        ).encode()
    ).hexdigest()
    return TranscriptRecord(
        transcript_id=f"transcript-{identity[:32]}",
        case_id=CASE_ID,
        version=1,
        bound_case_version=version,
        transcript_sha256=TRANSCRIPT_DIGEST,
        local_ref=local_ref,
        confirmed=False,
        created_at=CREATED_AT + timedelta(seconds=5),
        confirmed_at=None,
    )


@dataclass
class FakeRepository:
    case: CaseRecord | None
    transcript: TranscriptRecord | None = None
    receipt: SandboxReceiptRecord | None = None
    events: tuple[SequencedWorkflowEvent, ...] = ()
    case_reads: list[CaseRecord | None | Exception] = field(default_factory=list)
    event_error: Exception | None = None
    event_calls: list[tuple[str, int, int]] = field(default_factory=list)

    def get_case(self, case_id: str) -> CaseRecord | None:
        del case_id
        if self.case_reads:
            result = self.case_reads.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return self.case

    def get_transcript(self, case_id: str) -> TranscriptRecord | None:
        del case_id
        return self.transcript

    def get_sandbox_receipt(self, case_id: str) -> SandboxReceiptRecord | None:
        del case_id
        return self.receipt

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        self.event_calls.append((case_id, after, limit))
        if self.event_error is not None:
            raise self.event_error
        return tuple(item for item in self.events if item.sequence > after)[:limit]


@dataclass
class FakeTranscriptReader:
    text: str = TRANSCRIPT_TEXT
    error: Exception | None = None

    def read_verified_text(self, transcript: TranscriptRecord) -> str:
        del transcript
        if self.error is not None:
            raise self.error
        return self.text


@dataclass
class FakePortalReader:
    portal: PortalSessionView | None = None
    error: Exception | None = None

    def get_portal_session(self, case_id: str) -> PortalSessionView | None:
        del case_id
        if self.error is not None:
            raise self.error
        return self.portal


@dataclass
class FakeVerificationReader:
    verification: VerificationAttemptSeries | None = None

    def get_verification_attempts(
        self,
        case_id: str,
    ) -> VerificationAttemptSeries | None:
        del case_id
        return self.verification


def _assembler(
    repository: FakeRepository,
    *,
    portal: PortalSessionView | None = None,
    verification: VerificationAttemptSeries | None = None,
    transcript_reader: FakeTranscriptReader | None = None,
) -> SnapshotAssembler:
    return SnapshotAssembler(
        repository,
        transcript_reader=transcript_reader,
        portal_reader=FakePortalReader(portal),
        verification_reader=FakeVerificationReader(verification),
        request_id_factory=lambda: "request-001",
    )


@pytest.mark.parametrize("state", tuple(CaseState))
def test_assembler_builds_every_constructible_workflow_state(state: CaseState) -> None:
    record = _record(state)
    repository = FakeRepository(case=record)
    portal = None
    verification = None
    transcript_reader = None
    if state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
        repository.transcript = _transcript()
        transcript_reader = FakeTranscriptReader()
    elif state in {CaseState.READY_TO_FILL, CaseState.FILLING}:
        portal = _portal(state="draft", version=1)
    elif state in {CaseState.VERIFYING, CaseState.REVIEW}:
        portal = _portal()
        if state is CaseState.REVIEW:
            verification = _verification()
    elif state is CaseState.RECEIPT:
        repository.receipt = SandboxReceiptRecord(
            receipt=_receipt(),
            created_at=CREATED_AT + timedelta(seconds=18),
        )

    snapshot = _assembler(
        repository,
        portal=portal,
        verification=verification,
        transcript_reader=transcript_reader,
    ).assemble(CASE_ID)

    assert snapshot.case.state is state
    assert snapshot.case.version == record.version
    assert snapshot.request_id == "request-001"
    if state is CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
        assert snapshot.transcript_confirmation is not None
        assert repository.transcript is not None
        assert (
            snapshot.transcript_confirmation.transcript_id
            == repository.transcript.transcript_id
        )
        assert (
            snapshot.transcript_confirmation.transcript_sha256
            == repository.transcript.transcript_sha256
        )
        assert snapshot.transcript_confirmation.version == record.version
        assert snapshot.transcript_confirmation.text == TRANSCRIPT_TEXT
        assert snapshot.transcript_confirmation.confirmed is False


@pytest.mark.parametrize(
    "mutation",
    ["id", "case", "hash", "version", "confirmed", "text"],
)
def test_transcript_identity_hash_version_and_text_tamper_fail_closed(
    mutation: str,
) -> None:
    record = _record(CaseState.AWAITING_TRANSCRIPT_CONFIRMATION)
    transcript = _transcript()
    reader = FakeTranscriptReader()
    if mutation == "id":
        transcript = replace(transcript, transcript_id="transcript-other")
    elif mutation == "case":
        transcript = replace(transcript, case_id="case-other")
    elif mutation == "hash":
        transcript = replace(transcript, transcript_sha256="b" * 64)
    elif mutation == "version":
        transcript = replace(transcript, bound_case_version=6)
    elif mutation == "confirmed":
        transcript = replace(
            transcript,
            version=2,
            confirmed=True,
            confirmed_at=CREATED_AT + timedelta(seconds=6),
        )
    elif mutation == "text":
        reader.text = "Changed transcript"

    with pytest.raises(WorkflowDataIntegrityError):
        _assembler(
            FakeRepository(case=record, transcript=transcript),
            transcript_reader=reader,
        ).assemble(CASE_ID)


def test_clarification_raw_extras_are_never_best_effort_parsed() -> None:
    record = _record(CaseState.AWAITING_CLARIFICATION)
    assert record.snapshot.active_clarification is not None
    record.snapshot.active_clarification["answer"] = "private answer"

    with pytest.raises(WorkflowDataIntegrityError):
        _assembler(FakeRepository(case=record)).assemble(CASE_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("location", "Berln"),
        ("attachments", ["local-ref-1", "local-ref-2", "wrong-ref"]),
    ],
)
def test_review_rejects_portal_values_that_do_not_match_g8_packet(
    field: str,
    value: object,
) -> None:
    portal_data = _portal_data()
    portal_data["fields"][field] = value
    portal = PortalSessionView.model_validate(portal_data)

    with pytest.raises(WorkflowDataIntegrityError):
        _assembler(
            FakeRepository(case=_record(CaseState.REVIEW)),
            portal=portal,
            verification=_verification(),
        ).assemble(CASE_ID)


def test_snapshot_double_read_retries_once_then_reports_version_churn() -> None:
    v7 = _record(CaseState.CREATED, version=7)
    v8 = _record(CaseState.CREATED, version=8)
    v9 = _record(CaseState.CREATED, version=9)
    repository = FakeRepository(
        case=v9,
        case_reads=[v7, v8, v8, v9],
    )

    with pytest.raises(WorkflowVersionChurnError) as caught:
        _assembler(repository).assemble(CASE_ID)
    assert caught.value.current_version == 9
    assert not repository.case_reads


def test_snapshot_double_read_retries_once_and_returns_new_stable_version() -> None:
    v7 = _record(CaseState.CREATED, version=7)
    v8 = _record(CaseState.CREATED, version=8)
    repository = FakeRepository(
        case=v8,
        case_reads=[v7, v8, v8, v8],
    )

    snapshot = _assembler(repository).assemble(CASE_ID)

    assert snapshot.case.version == 8
    assert not repository.case_reads


@dataclass
class FakeOwnership:
    storage_name: str | None

    def get_case_media_handle(self, case_id: str) -> str | None:
        del case_id
        return self.storage_name


@pytest.mark.parametrize("content", [b"\xff", b"x" * (MAX_TRANSCRIPT_TEXT_BYTES + 1)])
def test_media_transcript_reader_rejects_invalid_utf8_and_oversize(
    tmp_path: Path,
    content: bytes,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    asset = store.write_bytes(
        handle,
        content,
        role="transcript",
        suffix=".txt",
        media_type="text/plain",
    )
    transcript = replace(
        _transcript(),
        transcript_sha256=asset.sha256,
        local_ref=asset.file_id,
    )
    reader = MediaTranscriptTextReader(FakeOwnership(handle.storage_name), store)

    with pytest.raises(WorkflowDataIntegrityError):
        reader.read_verified_text(transcript)


def _event(cursor: int, *, case_id: str = CASE_ID) -> SequencedWorkflowEvent:
    envelope = WorkflowEventEnvelope.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "eventId": f"projection-{cursor}",
            "caseId": case_id,
            "sourceAuditEventId": f"audit-{cursor}",
            "sourceAuditEventType": "clarification",
            "sourceAuditSequence": cursor,
            "cursor": cursor,
            "occurredAt": CREATED_AT,
            "event": {
                "kind": "clarification",
                "round": 1,
                "field": "incident_date",
                "status": "requested",
            },
        }
    )
    return SequencedWorkflowEvent(sequence=cursor, envelope=envelope)


def _client(
    repository: FakeRepository,
    *,
    heartbeat_interval: float = 15.0,
    portal_reader: FakePortalReader | None = None,
) -> TestClient:
    assembler = SnapshotAssembler(
        repository,
        portal_reader=portal_reader,
        request_id_factory=lambda: "request-http-001",
    )
    streamer = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(
            one_shot=True,
            heartbeat_interval_seconds=heartbeat_interval,
        ),
    )
    app = FastAPI()
    app.include_router(create_workflow_router(assembler, streamer))
    return TestClient(app)


def _error_body(code: str, message: str, current_version: int | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "reasonCodes": [],
            "fieldErrors": [],
            "gateDecision": None,
            "currentVersion": current_version,
        }
    }


def test_snapshot_router_returns_canonical_snapshot_and_closed_not_found() -> None:
    client = _client(FakeRepository(case=_record(CaseState.CREATED)))
    response = client.get(f"/api/workflow/cases/{CASE_ID}")
    assert response.status_code == 200
    assert response.json()["contractVersion"] == CONTRACT_VERSION
    assert response.json()["case"]["state"] == "created"

    missing = _client(FakeRepository(case=None)).get(
        "/api/workflow/cases/missing-case"
    )
    assert missing.status_code == 404
    assert missing.json() == _error_body(
        "WORKFLOW_CASE_NOT_FOUND",
        "The workflow case does not exist.",
    )


def test_snapshot_and_sse_reject_repository_cross_case_lookup() -> None:
    wrong_case = replace(_record(CaseState.CREATED), case_id="case-other")
    client = _client(FakeRepository(case=wrong_case))

    snapshot = client.get(f"/api/workflow/cases/{CASE_ID}")
    events = client.get(f"/api/workflow/cases/{CASE_ID}/events")

    assert snapshot.status_code == 500
    assert events.status_code == 500
    assert snapshot.json()["error"]["code"] == "WORKFLOW_DATA_INVALID"
    assert events.json()["error"]["code"] == "WORKFLOW_DATA_INVALID"


def test_snapshot_router_maps_repeated_version_churn_to_closed_409() -> None:
    v7 = _record(CaseState.CREATED, version=7)
    v8 = _record(CaseState.CREATED, version=8)
    v9 = _record(CaseState.CREATED, version=9)
    repository = FakeRepository(
        case=v9,
        case_reads=[v7, v8, v8, v9],
    )

    response = _client(repository).get(f"/api/workflow/cases/{CASE_ID}")

    assert response.status_code == 409
    assert response.json() == _error_body(
        "WORKFLOW_VERSION_CONFLICT",
        "The workflow case changed while it was loaded.",
        current_version=9,
    )


@pytest.mark.parametrize(
    "failure",
    [
        PersistedDataIntegrityError("private transcript text"),
        RuntimeError("private adapter value"),
    ],
)
def test_unexpected_repository_and_adapter_failures_map_to_generic_500(
    failure: Exception,
) -> None:
    if isinstance(failure, PersistedDataIntegrityError):
        repository = FakeRepository(case=None, case_reads=[failure])
        client = _client(repository)
    else:
        repository = FakeRepository(case=_record(CaseState.READY_TO_FILL))
        client = _client(repository, portal_reader=FakePortalReader(error=failure))

    response = client.get(f"/api/workflow/cases/{CASE_ID}")

    assert response.status_code == 500
    assert response.json() == _error_body(
        "WORKFLOW_DATA_INVALID",
        "The workflow data could not be read safely.",
    )
    assert "private" not in response.text


def test_sse_replays_exact_envelopes_with_gaps_and_required_headers() -> None:
    events = (_event(2), _event(5))
    repository = FakeRepository(case=_record(CaseState.CREATED), events=events)
    response = _client(repository).get(f"/api/workflow/cases/{CASE_ID}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == "".join(
        f"id: {item.sequence}\nevent: workflow\n"
        f"data: {item.envelope.model_dump_json(by_alias=True)}\n\n"
        for item in events
    )
    assert repository.event_calls == [(CASE_ID, 0, 100)]
    assert "prompt" not in response.text
    assert TRANSCRIPT_TEXT not in response.text


@pytest.mark.parametrize(
    ("query", "headers", "expected_cursors"),
    [
        ("?after=2", {}, (5, 9)),
        ("", {"Last-Event-ID": "5"}, (9,)),
        ("?after=5", {"Last-Event-ID": "5"}, (9,)),
    ],
)
def test_sse_reconnect_supports_query_or_last_event_id(
    query: str,
    headers: dict[str, str],
    expected_cursors: tuple[int, ...],
) -> None:
    repository = FakeRepository(
        case=_record(CaseState.CREATED),
        events=(_event(2), _event(5), _event(9)),
    )
    response = _client(repository).get(
        f"/api/workflow/cases/{CASE_ID}/events{query}",
        headers=headers,
    )

    assert response.status_code == 200
    assert tuple(
        int(line.removeprefix("id: "))
        for line in response.text.splitlines()
        if line.startswith("id: ")
    ) == expected_cursors


@pytest.mark.parametrize(
    "suffix",
    [
        "?after=true",
        "?after=-1",
        "?after=+1",
        "?after=01",
        "?after=9223372036854775808",
        "?after=1&after=1",
    ],
)
def test_sse_rejects_malformed_boolean_overflow_and_duplicate_cursors(
    suffix: str,
) -> None:
    response = _client(FakeRepository(case=_record(CaseState.CREATED))).get(
        f"/api/workflow/cases/{CASE_ID}/events{suffix}"
    )
    assert response.status_code == 400
    assert response.json() == _error_body(
        "WORKFLOW_CURSOR_INVALID",
        "The workflow replay cursor is invalid.",
    )


def test_sse_rejects_mismatched_header_and_query_before_streaming_headers() -> None:
    response = _client(FakeRepository(case=_record(CaseState.CREATED))).get(
        f"/api/workflow/cases/{CASE_ID}/events?after=2",
        headers={"Last-Event-ID": "3"},
    )
    assert response.status_code == 400
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_sse_rejects_malformed_last_event_id() -> None:
    response = _client(FakeRepository(case=_record(CaseState.CREATED))).get(
        f"/api/workflow/cases/{CASE_ID}/events",
        headers={"Last-Event-ID": "true"},
    )
    assert response.status_code == 400
    assert not response.headers["content-type"].startswith("text/event-stream")


@pytest.mark.parametrize("unsafe", ["duplicate", "cross_case"])
def test_sse_rejects_duplicate_and_cross_case_persisted_events_before_headers(
    unsafe: str,
) -> None:
    event = _event(2)
    events = (event, event) if unsafe == "duplicate" else (_event(2, case_id="case-other"),)

    class UnsafeRepository(FakeRepository):
        def list_workflow_events(
            self,
            case_id: str,
            *,
            after: int = 0,
            limit: int = 100,
        ) -> tuple[SequencedWorkflowEvent, ...]:
            self.event_calls.append((case_id, after, limit))
            return events

    response = _client(UnsafeRepository(case=_record(CaseState.CREATED))).get(
        f"/api/workflow/cases/{CASE_ID}/events"
    )
    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == _error_body(
        "WORKFLOW_DATA_INVALID",
        "The workflow data could not be read safely.",
    )


def test_sse_repository_corruption_is_generic_500_before_streaming() -> None:
    repository = FakeRepository(
        case=_record(CaseState.CREATED),
        event_error=PersistedDataIntegrityError("raw event data private-value"),
    )
    response = _client(repository).get(f"/api/workflow/cases/{CASE_ID}/events")

    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert "private-value" not in response.text


def test_sse_empty_replay_emits_comment_only_heartbeat() -> None:
    response = _client(
        FakeRepository(case=_record(CaseState.CREATED)),
        heartbeat_interval=0,
    ).get(f"/api/workflow/cases/{CASE_ID}/events")

    assert response.status_code == 200
    assert response.text == ": heartbeat\n\n"
    assert "id:" not in response.text
    assert "data:" not in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll", float("nan")),
        ("poll", float("inf")),
        ("heartbeat", float("-inf")),
    ],
)
def test_event_stream_config_rejects_non_finite_intervals(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        if field == "poll":
            EventStreamConfig(poll_interval_seconds=value)
        else:
            EventStreamConfig(heartbeat_interval_seconds=value)


def test_event_stream_page_size_matches_real_sqlite_repository_limit(
    tmp_path: Path,
) -> None:
    repository = SqliteCaseRepository(tmp_path / "workflow-events.db")
    repository.create_case(
        case_id=CASE_ID,
        redacted_metadata={},
        created_at=CREATED_AT,
    )
    accepted = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(page_size=500, one_shot=True),
    ).prepare(CASE_ID, 0)

    assert accepted.initial_events == ()
    with pytest.raises(ValueError, match="between 1 and 500"):
        EventStreamConfig(page_size=501)


def test_sse_disconnect_exits_without_emitting_buffered_events() -> None:
    repository = FakeRepository(
        case=_record(CaseState.CREATED),
        events=(_event(2),),
    )
    streamer = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(one_shot=False, poll_interval_seconds=0),
    )
    replay = streamer.prepare(CASE_ID, 0)
    disconnect_calls = 0

    async def disconnected() -> bool:
        nonlocal disconnect_calls
        disconnect_calls += 1
        return True

    async def collect() -> tuple[bytes, ...]:
        stream: AsyncIterator[bytes] = streamer.stream(
            replay,
            disconnected=disconnected,
        )
        return tuple([frame async for frame in stream])

    assert asyncio.run(collect()) == ()
    assert disconnect_calls == 1


def test_sse_closes_if_later_poll_is_corrupt_without_emitting_error_data() -> None:
    class LaterCorruptionRepository(FakeRepository):
        def list_workflow_events(
            self,
            case_id: str,
            *,
            after: int = 0,
            limit: int = 100,
        ) -> tuple[SequencedWorkflowEvent, ...]:
            self.event_calls.append((case_id, after, limit))
            if len(self.event_calls) > 1:
                raise PersistedDataIntegrityError("private event projection value")
            return (_event(2),)

    repository = LaterCorruptionRepository(case=_record(CaseState.CREATED))

    async def no_sleep(_seconds: float) -> None:
        return

    async def connected() -> bool:
        return False

    streamer = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(poll_interval_seconds=0),
        sleep=no_sleep,
    )
    replay = streamer.prepare(CASE_ID, 0)

    async def collect() -> tuple[bytes, ...]:
        return tuple(
            [
                frame
                async for frame in streamer.stream(
                    replay,
                    disconnected=connected,
                )
            ]
        )

    frames = asyncio.run(collect())
    assert len(frames) == 1
    assert b"private event projection value" not in frames[0]
    assert repository.event_calls == [(CASE_ID, 0, 100), (CASE_ID, 2, 100)]
